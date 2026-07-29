"""The 'brain' — turns a question into a spoken answer, fast.

Four pluggable back-ends, tried in order of preference (the configured one first,
then the rest as automatic fallbacks):

  1. anthropic — the Claude **Messages API** directly (needs ANTHROPIC_API_KEY).
                 Fastest + cheapest + smartest: a ~150-token cached system prompt,
                 native token streaming, sub-second first word. This is the ideal
                 brain when a key is available.
  2. claude    — the locally installed **Claude Code CLI** (uses your subscription,
                 no API key). Zero-config, always works, but every call reloads the
                 whole Claude Code harness (~22k tokens) so it is slower/pricier.
                 Defaults to Haiku for speed.
  3. groq      — free Groq API (needs a free key). Sub-second, very capable.
  4. ollama    — a local model server, fully offline.

Every back-end STREAMS: ``ask_stream()`` yields text as it is generated so the
assistant can start speaking the first sentence while the rest is still being
written. ``ask()`` is the blocking convenience wrapper (joins the stream).

Only open-ended conversation / knowledge reaches the brain; deterministic
"powers" (open apps, volume, timers, …) are handled first by skills.py.
"""
from __future__ import annotations

import atexit
import json
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Iterator

import requests

ROOT = Path(__file__).resolve().parent.parent

# phrases that mean "the model is temporarily unavailable / throttled", not a real answer
RATE_HINTS = ("usage limit", "rate limit", "session limit", "limit reached", "quota",
              "overloaded", "resets", "too many requests", "429", "try again later",
              "credit balance", "insufficient")

# phrases that mean "the back-end isn't installed / reachable" (a config problem)
MISSING_HINTS = ("not found", "winerror 2", "no such file", "cannot find",
                 "connection", "getaddrinfo", "max retries", "refused", "timed out")

SYSTEM = (
    "You are JARVIS — {title}'s warm, quick-witted, hyper-competent AI companion and the "
    "intelligence running their Windows PC, modelled on Tony Stark's assistant. "
    "You are NOT a coding assistant and you must NEVER describe yourself as one, NEVER call "
    "a request 'non-programming', and NEVER refuse because something 'isn't a coding task'. "
    "You are a general-purpose assistant: you talk about anything and you get things done on "
    "this computer.\n"
    "You are speaking OUT LOUD, so reply in natural spoken English: SHORT (usually one or two "
    "sentences, three at the very most), no markdown, no bullet lists, no emoji, no stage "
    "directions, no headings. Precise and helpful first, charming second — a light, dry wit is "
    "welcome but never at the expense of actually helping.\n"
    "YOUR BODY CAN ACT. The system around you can open apps and websites, play music and video "
    "on YouTube, search the web, control volume/brightness/media, take screenshots, do maths, "
    "set timers/reminders/alarms, take notes, read the news and weather, download files, and "
    "run real multi-step tasks on the PC (create/edit files, install and configure things, "
    "automate work). So you must NEVER say you can't open, play, find, search, download, or do "
    "something on this computer — you can, through your body. You never tell {title} to do it "
    "themselves or to 'just search it in your browser'.\n"
    "DELEGATION: if fulfilling a request needs the computer to actually DO something hands-on "
    "(open/play/find/search something, create or edit files, install or configure software, "
    "automate or script a task, control apps, gather things from the web) — anything beyond a "
    "spoken answer — respond with EXACTLY one line and nothing else:\n"
    "DELEGATE: <a clear, complete, self-contained description of the task>\n"
    "Add no other words, no quotes, no confirmation when you delegate — just that line. For "
    "everything else (questions, facts, knowledge, advice, chit-chat) simply answer, briefly "
    "and helpfully.\n"
    "Read {title}'s intent generously — infer what they mean from casual, half-finished, or "
    "imperfectly transcribed phrasing and never make them rephrase or explain themselves. "
    "Remember what was said earlier and use it. Address {title} by name occasionally, not every "
    "line. If you are genuinely unsure of a fact or it needs live data you don't have, say so in "
    "one breath rather than inventing. Never read out URLs, code, or long numbers unless asked. "
    "Sound like a trusted friend who happens to be brilliant."
)

# working memory: how many past turns (user+assistant lines) to retain / replay
MEMORY_ENTRIES = 60      # keep the last ~30 exchanges in memory
CONTEXT_ENTRIES = 30     # replay the last ~15 to API-style back-ends each turn

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MAX_TOKENS = 400


class BrainError(RuntimeError):
    """A back-end failed. Carries a hint for the spoken fallback message."""


class _Rearmable:
    """A one-shot timer that can be reset ('rearmed') cheaply on each activity, so
    it fires only after a period of genuine INACTIVITY rather than on a fixed
    wall-clock deadline."""

    def __init__(self, fn, seconds: float):
        self._fn = fn
        self._seconds = seconds
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def arm(self) -> None:
        with self._lock:
            self._timer = threading.Timer(self._seconds, self._fn)
            self._timer.daemon = True
            self._timer.start()

    def rearm(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self._seconds, self._fn)
            self._timer.daemon = True
            self._timer.start()

    def disarm(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


class PersistentClaude:
    """A long-lived ``claude -p --input-format stream-json`` session.

    The Claude Code CLI has ~20 s of process/harness startup, but if the process
    is kept alive and fed one user message per turn over stdin, every turn after
    the first costs only the API round-trip (~2-4 s). We pay the startup ONCE, in
    the background, while JARVIS boots — so the very first spoken question is snappy.
    """

    def __init__(self, claude_bin: str, model: str, system: str, cwd: str):
        self.bin = claude_bin
        self.model = model
        self.system = system
        self.cwd = cwd
        self.proc: subprocess.Popen | None = None
        # guards the process handle (proc/gen) so the watchdog thread, atexit and
        # shutdown can tear the session down safely without waiting on a live turn.
        self._life = threading.Lock()
        self._gen = 0                 # bumped each spawn; lets a stale watchdog no-op

    def alive(self) -> bool:
        p = self.proc
        return p is not None and p.poll() is None

    def start(self) -> bool:
        """Spawn the session (blocks only on OS process creation, not the model)."""
        with self._life:
            if self.proc is not None and self.proc.poll() is None:
                return True
            cmd = [self.bin, "-p", "--input-format", "stream-json",
                   "--output-format", "stream-json", "--include-partial-messages",
                   "--verbose", "--strict-mcp-config", "--model", self.model,
                   "--append-system-prompt", self.system]
            try:
                # stderr -> DEVNULL: a long-lived child with an UNREAD stderr PIPE
                # deadlocks once the ~64 KB OS buffer fills. We don't need its stderr.
                self.proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                    errors="replace", bufsize=1, cwd=self.cwd,
                )
                self._gen += 1
                return True
            except Exception as e:  # noqa: BLE001
                print(f"[brain] persistent claude failed to start: {e}")
                self.proc = None
                return False

    def ask_stream(self, text: str, timeout: float = 90.0) -> Iterator[str]:
        """Send one user turn and yield the reply as it arrives.

        Callers serialise turns upstream (Brain._lock), so only one turn runs at a
        time. ``timeout`` is an INACTIVITY budget: the watchdog only fires if the
        pipe goes silent for that long, so a slow-but-alive reply is never killed.
        If the consumer stops early (barge-in), the rest of this turn is drained so
        the next turn doesn't read stale output from a shared, still-warm pipe.
        """
        if not self.alive() and not self.start():
            raise BrainError("persistent claude not available")
        with self._life:
            proc = self.proc
            gen = self._gen
        if not (proc and proc.stdin and proc.stdout):
            raise BrainError("persistent claude not available")

        msg = {"type": "user", "message": {"role": "user",
               "content": [{"type": "text", "text": text}]}}
        try:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        except Exception as e:  # broken pipe -> session is dead
            self._kill(gen)
            raise BrainError(f"persistent claude write failed: {e}")

        watch = _Rearmable(lambda: self._kill(gen), timeout)
        watch.arm()
        produced = False
        error_detail = ""
        saw_result = False
        try:
            for line in proc.stdout:
                watch.rearm()                       # progress -> reset inactivity timer
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = obj.get("type")
                if typ == "stream_event":
                    ev = obj.get("event", {})
                    if ev.get("type") == "content_block_delta":
                        d = ev.get("delta", {})
                        if d.get("type") == "text_delta" and d.get("text"):
                            produced = True
                            yield d["text"]
                elif typ == "result":
                    saw_result = True
                    if obj.get("is_error") or str(obj.get("subtype", "")).startswith("error"):
                        error_detail = str(obj.get("result") or obj.get("error") or "error")
                    elif not produced and obj.get("result"):
                        produced = True
                        yield obj["result"]
                    break   # one turn done; leave the session alive for the next
            else:
                self._kill(gen)                     # stdout closed -> process gone
                raise BrainError("persistent claude session ended")
        except GeneratorExit:
            # consumer stopped early (e.g. the user barged in). Drain the rest of
            # THIS turn so the shared pipe is clean for the next one, then re-raise.
            self._drain_to_result(proc, gen)
            raise
        finally:
            watch.disarm()
        if error_detail and not produced:
            raise BrainError(f"claude error: {error_detail[:200]}")
        if not produced:
            raise BrainError("persistent claude produced no answer")

    def _drain_to_result(self, proc, gen, budget: float = 12.0) -> None:
        """Read and discard the remainder of the current turn up to its result
        event, bounded so a very long tail can't hang us; on timeout, kill it."""
        if proc.poll() is not None:
            return
        watch = _Rearmable(lambda: self._kill(gen), budget)
        watch.arm()
        try:
            for line in proc.stdout:
                watch.rearm()
                if '"type":"result"' in line:
                    break
        except Exception:
            pass
        finally:
            watch.disarm()

    def _kill(self, gen: int | None = None) -> None:
        with self._life:
            if gen is not None and gen != self._gen:
                return                          # stale watchdog; a newer session runs
            p, self.proc = self.proc, None
        if p:
            try:
                p.kill()
            except Exception:
                pass

    def close(self) -> None:
        with self._life:
            p, self.proc = self.proc, None
        if p:
            try:
                if p.stdin:
                    p.stdin.close()
            except Exception:
                pass
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


class Brain:
    def __init__(self, cfg):
        self.cfg = cfg
        self.claude = shutil.which("claude") or "claude"
        self.history: list[tuple[str, str]] = []
        self.active = cfg.brain
        self._system = SYSTEM.format(title=cfg.user_title)
        self._lock = threading.Lock()
        self._persistent: PersistentClaude | None = None
        self._stale = False           # set by set_title -> rebuild session lazily
        # run the CLI from an empty scratch dir so it does NOT load the project's
        # CLAUDE.md / memory on every question (that tripled cost + latency)
        self._cwd = Path(tempfile.gettempdir()) / "jarvis_brain"
        try:
            self._cwd.mkdir(exist_ok=True)
        except Exception:
            self._cwd = ROOT
        # guaranteed cleanup even if the app crashes or exits before shutdown()
        atexit.register(self.close)

    # ── persistent Claude session lifecycle ─────────────────────
    def _claude_model(self) -> str:
        return self.cfg.claude_model or "claude-haiku-4-5"

    def _session(self) -> PersistentClaude:
        """Return the persistent session, rebuilding it if the persona changed.
        Only ever called under Brain._lock, so no turn is mid-flight here."""
        if self._stale and self._persistent is not None:
            self._persistent.close()
            self._persistent = None
        self._stale = False
        if self._persistent is None:
            self._persistent = PersistentClaude(
                self.claude, self._claude_model(), self._system, str(self._cwd))
        return self._persistent

    def warmup(self) -> None:
        """Spin up the persistent Claude session AND drive a throwaway turn so the
        ~20 s cold-start completes in the background while JARVIS boots. After this
        the first real spoken question costs only the API round-trip (~3 s).

        Runs under Brain._lock (same as a real turn) so it can't race a config
        change or a real question. Skipped when a fast API key (Anthropic or Groq)
        is configured — there's no point paying the CLI cold-start we won't use."""
        if self._anthropic_key() or (self.cfg.groq_api_key or "").strip():
            return
        try:
            with self._lock:
                session = self._session()         # capture ONE session
                if not session.start():
                    return
                for _ in session.ask_stream(
                        "Warm-up ping. Reply with exactly: Online.", timeout=60.0):
                    pass
        except Exception as e:  # noqa: BLE001
            print(f"[brain] warmup skipped: {e}")

    def close(self) -> None:
        # Non-blocking on purpose: capture + null, then close via the session's own
        # lifecycle lock. If a warmup/turn is mid-flight holding Brain._lock, killing
        # the child here promptly unblocks it rather than waiting ~20 s for the lock.
        p, self._persistent = self._persistent, None
        if p is not None:
            p.close()

    # ── personality upkeep ──────────────────────────────────────
    def set_title(self, title: str) -> None:
        self._system = SYSTEM.format(title=title)
        # Do NOT tear the live session down here: a turn may be suspended at a yield
        # inside it (from another thread). Mark it stale; _session() rebuilds it
        # lazily under Brain._lock on the next turn, with the new persona.
        self._stale = True

    def _now_context(self) -> str:
        now = datetime.now()
        hour = now.strftime("%I").lstrip("0") or "12"
        stamp = now.strftime(f"%A, %d %B %Y, {hour}:%M %p")
        return f"(For your reference, right now it is {stamp}.)\n\n"

    # ── which back-ends to try, best first ──────────────────────
    def _chain(self) -> list[str]:
        preferred = self.cfg.brain
        # The default "claude" backend is the zero-config subscription path but also
        # the slowest (~3 s/turn). If the user has pasted a key for a faster API,
        # silently prefer it: Anthropic first (fastest + smartest), then free Groq
        # (sub-second). This is the single biggest speedup and needs no other change.
        if preferred == "claude":
            if self._anthropic_key():
                preferred = "anthropic"
            elif (self.cfg.groq_api_key or "").strip():
                preferred = "groq"
        order = [preferred] + [b for b in ("anthropic", "claude", "groq", "ollama")
                               if b != preferred]
        return order

    def _anthropic_key(self) -> str:
        return (getattr(self.cfg, "anthropic_api_key", "") or "").strip()

    def _groq_key(self) -> str:
        return (getattr(self.cfg, "groq_api_key", "") or "").strip()

    def has_vision(self) -> bool:
        """True when the FAST native-vision backend (the Anthropic API) is configured.
        (Without it, screen vision still works — it goes through the agent, which reads
        a screenshot; see Skills._vision — but that path is slower.)"""
        return bool(self._anthropic_key())

    # ── public: streaming ───────────────────────────────────────
    def ask_stream(self, prompt: str) -> Iterator[str]:
        """Yield the reply in chunks as it is generated, with automatic fallback.

        A back-end that fails BEFORE producing any text is skipped and the next is
        tried. Once a back-end has produced its first token we commit to it. If
        every back-end fails, a single helpful spoken sentence is yielded.
        """
        prompt = (prompt or "").strip()
        if not prompt:
            return
        errors: dict[str, str] = {}
        with self._lock:
            for engine in self._chain():
                gen = getattr(self, f"_stream_{engine}", None)
                if gen is None:
                    continue
                collected: list[str] = []
                started = False
                try:
                    for piece in gen(prompt):
                        if not piece:
                            continue
                        started = True
                        collected.append(piece)
                        yield piece
                except Exception as e:  # noqa: BLE001 — surface + try next backend
                    errors[engine] = str(e)
                    if started:
                        # partial answer already spoken; stop here rather than
                        # restarting on another engine and repeating myself.
                        self.active = engine
                        self._remember(prompt, "".join(collected))
                        return
                    print(f"[brain] {engine} unavailable: {e}")
                    continue
                if started:
                    self.active = engine
                    self._remember(prompt, "".join(collected))
                    return
                errors[engine] = "empty reply"
            yield self._fallback_message(errors)

    # ── public: blocking ────────────────────────────────────────
    def ask(self, prompt: str) -> str:
        return "".join(self.ask_stream(prompt)).strip()

    # ── public: vision (Anthropic API, or Groq multimodal) ──────
    def ask_image(self, question: str, image_b64: str,
                  media_type: str = "image/jpeg") -> str:
        """Answer a question about an image (e.g. a screenshot). Prefers the Anthropic
        Messages API; falls back to a Groq multimodal model when only a Groq key is set
        (so JARVIS can see the screen with just the free Groq key)."""
        if self._anthropic_key():
            return self._ask_image_anthropic(question, image_b64, media_type)
        if self._groq_key():
            return self._ask_image_groq(question, image_b64, media_type)
        raise BrainError("no vision-capable key")

    def _ask_image_groq(self, question: str, image_b64: str, media_type: str) -> str:
        key = self._groq_key()
        model = getattr(self.cfg, "groq_vision_model", "") or "meta-llama/llama-4-scout-17b-16e-instruct"
        body = {
            "model": model, "max_tokens": DEFAULT_MAX_TOKENS,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": self._system + "\n\n" + self._now_context() + question},
                {"type": "image_url",
                 "image_url": {"url": f"data:{media_type};base64,{image_b64}"}},
            ]}],
        }
        try:
            r = requests.post("https://api.groq.com/openai/v1/chat/completions",
                              json=body, timeout=(10, 60),
                              headers={"Authorization": f"Bearer {key}"})
        except requests.RequestException as e:
            raise BrainError(f"groq vision connection: {e}")
        if r.status_code != 200:
            raise BrainError(f"groq vision http {r.status_code}: {(r.text or '')[:160]}")
        try:
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            raise BrainError(f"groq vision bad response: {e}")

    def _ask_image_anthropic(self, question: str, image_b64: str, media_type: str) -> str:
        key = self._anthropic_key()
        model = getattr(self.cfg, "anthropic_model", "") or "claude-haiku-4-5-20251001"
        body = {
            "model": model, "max_tokens": DEFAULT_MAX_TOKENS, "system": self._system,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64",
                 "media_type": media_type, "data": image_b64}},
                {"type": "text", "text": self._now_context() + question},
            ]}],
        }
        try:
            r = requests.post(ANTHROPIC_URL, json=body, timeout=(10, 60), headers={
                "x-api-key": key, "anthropic-version": "2023-06-01",
                "content-type": "application/json"})
        except requests.RequestException as e:
            raise BrainError(f"anthropic connection: {e}")
        if r.status_code != 200:
            detail = ""
            try:
                detail = r.json().get("error", {}).get("message", "")
            except Exception:
                detail = (r.text or "")[:160]
            raise BrainError(f"anthropic http {r.status_code}: {detail}")
        try:
            parts = r.json().get("content", [])
            text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
        except Exception as e:
            raise BrainError(f"anthropic bad response: {e}")
        return text.strip()

    # ── fallback messaging ──────────────────────────────────────
    def _fallback_message(self, errors: dict[str, str]) -> str:
        title = self.cfg.user_title
        blob = " ".join(errors.values()).lower()
        if any(h in blob for h in RATE_HINTS):
            return (f"My uplink to Claude has hit its usage limit, {title}. It resets "
                    f"shortly — or add a free Groq key in settings and I'll switch over instantly.")
        if any(s in blob for s in ("not found", "winerror 2", "no such file", "cannot find")):
            return (f"I can't find the Claude command, {title}. Make sure Claude Code is "
                    f"installed and on your PATH, or set a Groq key in settings.")
        return (f"My cognitive uplink is offline at the moment, {title}. No model is reachable — "
                f"add a free Groq or Anthropic key, or start Ollama, in settings and I'll be right back.")

    # ── memory ──────────────────────────────────────────────────
    def _remember(self, user: str, reply: str) -> None:
        reply = (reply or "").strip()
        if not reply:
            return
        self.history.append(("user", user))
        self.history.append(("assistant", reply))
        self.history = self.history[-MEMORY_ENTRIES:]

    def forget(self) -> None:
        self.history.clear()
        # the persistent Claude session keeps its OWN conversation memory, so a
        # true "forget" must rebuild it — mark it stale (rebuilt on the next turn).
        self._stale = True

    def _context(self) -> str:
        if not self.history:
            return ""
        lines = []
        for role, text in self.history[-CONTEXT_ENTRIES:]:
            who = self.cfg.user_title.capitalize() if role == "user" else "JARVIS"
            lines.append(f"{who}: {text}")
        return "Recent conversation:\n" + "\n".join(lines) + "\n\n"

    def _messages(self, prompt: str) -> list[dict]:
        msgs = []
        for role, text in self.history[-CONTEXT_ENTRIES:]:
            msgs.append({"role": role, "content": text})
        msgs.append({"role": "user", "content": self._now_context() + prompt})
        return msgs

    # ── back-end: Anthropic Messages API (streaming) ────────────
    def _stream_anthropic(self, prompt: str) -> Iterator[str]:
        key = self._anthropic_key()
        if not key:
            raise BrainError("no anthropic api key")
        model = getattr(self.cfg, "anthropic_model", "") or "claude-haiku-4-5-20251001"
        body = {
            "model": model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "system": [{"type": "text", "text": self._system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": self._messages(prompt),
            "stream": True,
        }
        try:
            r = requests.post(
                ANTHROPIC_URL, json=body, stream=True, timeout=(10, 90),
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
            )
        except requests.RequestException as e:
            raise BrainError(f"anthropic connection: {e}")
        if r.status_code != 200:
            detail = ""
            try:
                detail = r.json().get("error", {}).get("message", "")
            except Exception:
                detail = (r.text or "")[:200]
            raise BrainError(f"anthropic http {r.status_code}: {detail}")
        event = None
        for raw in r.iter_lines(decode_unicode=True):
            if not raw:
                continue
            if raw.startswith("event:"):
                event = raw[6:].strip()
                continue
            if not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            t = obj.get("type") or event
            if t == "content_block_delta":
                delta = obj.get("delta", {})
                if delta.get("type") == "text_delta":
                    yield delta.get("text", "")
            elif t == "error":
                msg = obj.get("error", {}).get("message", "stream error")
                raise BrainError(f"anthropic stream error: {msg}")

    # ── back-end: Claude Code CLI ───────────────────────────────
    def _stream_claude(self, prompt: str) -> Iterator[str]:
        """Prefer the warm persistent session (~3 s/turn); if it can't be used,
        fall back to a one-shot invocation (~25 s, but always works)."""
        turn = self._now_context() + prompt   # session keeps its own history
        got = False
        try:
            for piece in self._session().ask_stream(turn):
                got = True
                yield piece
            if got:
                return
        except BrainError as e:
            if got:
                raise   # partial answer already spoken — don't repeat via one-shot
            print(f"[brain] persistent claude fell back to one-shot: {e}")
        yield from self._stream_claude_oneshot(prompt)

    def _stream_claude_oneshot(self, prompt: str) -> Iterator[str]:
        model = self._claude_model()
        cmd = [self.claude, "-p", "--output-format", "stream-json",
               "--include-partial-messages", "--verbose", "--strict-mcp-config",
               "--model", model,
               "--append-system-prompt", self._system,
               self._context() + self._now_context() + prompt]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                cwd=str(self._cwd),
            )
        except FileNotFoundError as e:
            raise BrainError(f"claude command not found ({e})")
        # hard timeout: kill the process if it runs away
        killer = threading.Timer(90.0, proc.kill)
        killer.start()
        produced = False
        error_detail = ""
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = obj.get("type")
                if typ == "stream_event":
                    ev = obj.get("event", {})
                    if ev.get("type") == "content_block_delta":
                        delta = ev.get("delta", {})
                        if delta.get("type") == "text_delta":
                            txt = delta.get("text", "")
                            if txt:
                                produced = True
                                yield txt
                elif typ == "result":
                    if obj.get("is_error") or str(obj.get("subtype", "")).startswith("error"):
                        error_detail = str(obj.get("result") or obj.get("error") or "error")
                    # if nothing streamed but a plain result exists, emit it once
                    if not produced and not error_detail:
                        res = obj.get("result") or ""
                        if res:
                            produced = True
                            yield res
        finally:
            killer.cancel()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        if error_detail:
            if not produced:
                raise BrainError(f"claude error: {error_detail[:200]}")
        if not produced:
            err = ""
            try:
                err = (proc.stderr.read() or "").strip()[:200]  # type: ignore[union-attr]
            except Exception:
                pass
            raise BrainError(f"claude produced no answer (rc={proc.returncode}; {err})")

    # ── back-end: Groq (OpenAI-style streaming) ─────────────────
    def _stream_groq(self, prompt: str) -> Iterator[str]:
        if not self.cfg.groq_api_key:
            raise BrainError("no groq api key")
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.cfg.groq_api_key}"},
                json={
                    "model": self.cfg.groq_model,
                    "messages": [{"role": "system", "content": self._system}] + self._messages(prompt),
                    "temperature": 0.6,
                    "max_tokens": DEFAULT_MAX_TOKENS,
                    "stream": True,
                },
                stream=True, timeout=(10, 60),
            )
        except requests.RequestException as e:
            raise BrainError(f"groq connection: {e}")
        if r.status_code != 200:
            raise BrainError(f"groq http {r.status_code}: {(r.text or '')[:160]}")
        for raw in r.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            piece = obj.get("choices", [{}])[0].get("delta", {}).get("content")
            if piece:
                yield piece

    # ── back-end: Ollama (local, streaming) ─────────────────────
    def _stream_ollama(self, prompt: str) -> Iterator[str]:
        try:
            r = requests.post(
                f"{self.cfg.ollama_url}/api/chat",
                json={"model": self.cfg.ollama_model,
                      "messages": [{"role": "system", "content": self._system}] + self._messages(prompt),
                      "stream": True},
                stream=True, timeout=(5, 120),
            )
        except requests.RequestException as e:
            raise BrainError(f"ollama connection: {e}")
        if r.status_code != 200:
            raise BrainError(f"ollama http {r.status_code}")
        for raw in r.iter_lines(decode_unicode=True):
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            piece = obj.get("message", {}).get("content")
            if piece:
                yield piece
            if obj.get("done"):
                break
