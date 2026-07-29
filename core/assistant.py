"""The orchestrator — ties ears, brain, mouth, skills and the HUD together.

A turn can be triggered three ways, all routed through the same pipeline:
    • voice  — wake word "hey jarvis" (or click core / Space) → listen → transcribe
    • text   — typed into the HUD command bar
    • timer  — a skill speaking on its own (e.g. a finished timer)

Flow per turn:  input → thinking → skills.handle() or brain.ask() → speaking → idle
"""
from __future__ import annotations

import json
import re
import threading
import time

from .brain import Brain
from .skills import Skills
from .speech import Mouth
from .voice import Ears

WHISPER_SIZES = ("tiny", "base", "small", "medium")
# mission tags that shrink the HUD to a corner while they run (so the real windows /
# the screen behind the HUD are visible and can be captured cleanly)
_MINIMIZE_TAGS = ("AGENT", "RESEARCH", "VISION")


class Mission:
    """A live, background unit of work streamed to the HUD's agent-activity panel.

    A worker calls ``step()`` to advance the visible checklist, ``note()`` to
    annotate the current step (e.g. 'PDF 2 of 4'), ``speak()`` to say something
    out loud (sparingly — the panel carries the play-by-play so JARVIS doesn't
    talk over you), and ``finish()``/``error()`` to close it out. Every mutation
    re-broadcasts the whole (small) mission state; the front-end reconciles by id.
    """

    def __init__(self, hud, mid: str, title: str, tag: str, on_speak):
        self.hud = hud
        self.id = mid
        self.title = title
        self.tag = tag
        self._speak = on_speak
        self.steps: list[dict] = []
        self.status = "running"
        self._active = None
        self._lock = threading.Lock()

    def _push(self) -> None:
        try:
            self.hud.mission({"id": self.id, "title": self.title, "tag": self.tag,
                              "status": self.status, "steps": list(self.steps)})
        except Exception as e:  # noqa: BLE001 — the HUD must never break a mission
            print(f"[mission] push failed: {e}")

    def step(self, label: str, detail: str = "") -> None:
        with self._lock:
            if self._active is not None and 0 <= self._active < len(self.steps):
                self.steps[self._active]["state"] = "done"
            self.steps.append({"label": label, "state": "active", "detail": detail})
            self._active = len(self.steps) - 1
        self._push()

    def note(self, detail: str) -> None:
        with self._lock:
            if self._active is not None and 0 <= self._active < len(self.steps):
                self.steps[self._active]["detail"] = detail
        self._push()

    def speak(self, text: str) -> None:
        try:
            self._speak(text)
        except Exception as e:  # noqa: BLE001
            print(f"[mission] speak failed: {e}")

    def finish(self, status: str = "done", tag: str | None = None) -> None:
        with self._lock:
            if self._active is not None and 0 <= self._active < len(self.steps):
                self.steps[self._active]["state"] = "done"
            self._active = None
            self.status = status
            if tag:
                self.tag = tag
        self._push()

    def error(self, detail: str = "") -> None:
        with self._lock:
            if self._active is not None and 0 <= self._active < len(self.steps):
                self.steps[self._active]["state"] = "done"
            self.status = "error"
            self.tag = "FAILED"
            if detail:
                self.steps.append({"label": detail, "state": "done", "detail": ""})
        self._push()


class Assistant:
    def __init__(self, cfg, hud):
        self.cfg = cfg
        self.hud = hud
        self.brain = Brain(cfg)
        self.mouth = Mouth(cfg, hud)
        self.ears = Ears(cfg, hud)
        self.last_reply = ""               # what JARVIS last said (for "repeat that")
        self.skills = Skills(cfg, hud, self._say,
                             last_reply=lambda: self.last_reply,
                             forget=self._forget_conversation,
                             describe_image=self._describe_screen,
                             can_see=self.brain.has_vision,
                             ask_brain=self.brain.ask,
                             run_mission=self.run_mission,
                             active_missions=self.active_missions)
        self.stop = threading.Event()
        self.ptt = threading.Event()       # push-to-talk trigger from the HUD
        self._turn_lock = threading.Lock()  # serialises voice/text turns
        self._missions: list[Mission] = []  # background agent missions in flight
        self._mission_lock = threading.Lock()
        self._mid = 0                       # monotonically increasing mission id
        self._demo_active = threading.Event()   # a showcase demo is running
        self.autostart_demo = False        # run.py --demo → play the showcase on boot
        self.on_quit = None                # set by run.py to close the window
        # window-control hooks, wired to the pywebview window in run.py
        self.on_minimize = None
        self.on_toggle_max = None
        self.on_show = None
        self.on_hide = None
        self.on_compact = None             # shrink the window to a corner icon
        self.on_restore = None             # restore the window to full size
        self._compact = False              # is the HUD currently in compact corner mode?
        hud.on_message = self._on_hud

    # ── HUD → assistant messages ────────────────────────────────
    def _on_hud(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "push_to_talk":
            self.ptt.set()
        elif kind == "stop":
            self.mouth.interrupt()
        elif kind == "text_command":
            text = (msg.get("text") or "").strip()
            if text:
                threading.Thread(target=self._process, args=(text,), daemon=True).start()
        elif kind == "get_config":
            self._send_config()
        elif kind == "set_config":
            self._apply_config(msg.get("config") or {})
        elif kind == "win":
            self._on_win(msg.get("action"))
        elif kind == "demo":
            self.run_demo()
        elif kind == "quit":
            self.stop.set()
            self.mouth.interrupt()
            if self.on_quit:
                self.on_quit()

    # ── window controls (minimize / maximize / show / hide) ─────
    def _on_win(self, action: str) -> None:
        if action == "restore_full":       # user clicked the compact corner icon
            self._exit_compact(force=True)
            return
        cb = {
            "minimize": self.on_minimize,
            "toggle_max": self.on_toggle_max,
            "show": self.on_show,
            "hide": self.on_hide,
        }.get(action)
        if cb:
            try:
                cb()
            except Exception as e:
                print(f"[assistant] window action '{action}' failed: {e}")

    # ── compact corner mode (HUD shrinks aside while the agent works) ──
    def _enter_compact(self, label: str) -> None:
        self.hud.send({"type": "compact", "on": True, "label": label})
        if not self._compact and self.on_compact:
            try:
                self.on_compact()
            except Exception as e:
                print(f"[assistant] compact failed: {e}")
        self._compact = True

    def _exit_compact(self, force: bool = False) -> None:
        # only pop back to full size once no window-opening mission is still running
        if not force:
            with self._mission_lock:
                if any(m.status == "running" and m.tag in _MINIMIZE_TAGS
                       for m in self._missions):
                    return
        if not self._compact:
            return
        self._compact = False
        self.hud.send({"type": "compact", "on": False})
        if self.on_restore:
            try:
                self.on_restore()
            except Exception as e:
                print(f"[assistant] restore failed: {e}")

    # ── a single turn (voice or text) ───────────────────────────
    def _process(self, text: str) -> None:
        if not text:
            return
        with self._turn_lock:
            if self.stop.is_set():
                return
            self.hud.user(text)
            # understand the request and break it into the separate actions to perform —
            # "play X and research Y and check Z" → three commands, run in turn.
            for seg in self._resolve_commands(text):
                if self.stop.is_set():
                    break
                self.hud.state("thinking")
                self.hud.status("processing")
                reply = None
                try:
                    reply = self.skills.handle(seg)
                except Exception as e:
                    print(f"[assistant] skill error: {e}")
                if reply is not None:                # deterministic skill answered
                    if reply:
                        self._say(reply)
                    else:
                        self.hud.state("idle")
                else:                                # escalate to the brain (may delegate)
                    self._handle_brain(seg)
                if self.skills.should_exit:
                    self.stop.set()
                    if self.on_quit:
                        self.on_quit()
                    break

    # split "do A then do B and then do C" into ["do A", "do B", "do C"]. Only clear
    # sequence words split (not bare commas/"and", which appear inside a single request
    # like "research components, program, algorithm").
    _CMD_SPLIT = re.compile(
        r"\s*,?\s*\b(?:and then|then|after that|after which|afterwards?|"
        r"followed by|and afterwards|and after that)\b\s*[,]?\s*|\s*;\s*",
        re.IGNORECASE)

    def _split_commands(self, text: str) -> list[str]:
        text = (text or "").strip()
        if not text:
            return []
        parts = [p.strip(" ,.;…") for p in self._CMD_SPLIT.split(text)]
        parts = [p for p in parts if p and len(p) > 1]
        if len(parts) <= 1:
            return [text]        # no real chain → single command, unchanged behaviour
        return parts[:6]         # bound the chain length

    # things that hint the utterance holds MORE than one action (worth asking the brain
    # to decompose it intelligently rather than trusting a regex)
    _MULTI_HINT = re.compile(r"\b(and|also|plus|then|next|as well as|along with)\b|[;,&]", re.IGNORECASE)

    def _resolve_commands(self, text: str) -> list[str]:
        """Turn a raw utterance into the ordered list of atomic commands to run.

        Fast path: explicit sequencers ("… then …") split with no LLM. Otherwise, if the
        utterance looks like it holds several actions, ask the brain to UNDERSTAND it and
        return clean, separate commands (so we act on intent, not the literal words). A
        plain single request is returned unchanged — no latency added."""
        text = (text or "").strip()
        if not text:
            return []
        segs = self._split_commands(text)
        if len(segs) > 1:
            return segs
        if len(text.split()) <= 4 or not self._MULTI_HINT.search(text):
            return [text]        # clearly one thing → no planning, stays instant
        planned = self._plan(text)
        return planned or [text]

    def _plan(self, text: str) -> list[str]:
        """Ask the brain to decompose a messy multi-part request into clean, separate,
        imperative commands. Returns [] on any failure (caller falls back to the raw text)."""
        prompt = (
            "You convert a user's request to a PC voice-assistant into an ordered JSON list "
            "of the SEPARATE commands to carry out. Rules:\n"
            "- Split genuinely separate actions (e.g. play music / do research / check stats / "
            "open an app).\n"
            "- Keep as ONE command a single action that merely lists its sub-parts — e.g. "
            "\"research CNC components, programs and algorithms\" is ONE research command, not three.\n"
            "- Rephrase each as a short, clear, self-contained imperative the assistant can act on; "
            "do not copy the user's words verbatim and do not invent actions they didn't ask for.\n"
            "- If it's really one request, return a single-element list.\n"
            "Reply with ONLY a JSON array of strings, nothing else.\n"
            f"Request: \"{text}\""
        )
        try:
            raw = (self.brain.ask(prompt) or "").strip()
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                return []
            arr = json.loads(m.group(0))
            steps = [s.strip() for s in arr if isinstance(s, str) and s.strip()]
            return steps[:6]
        except Exception as e:  # noqa: BLE001
            print(f"[assistant] plan failed: {e}")
            return []

    # ── showcase demo (for social-media recordings) ─────────────
    def _demo_script(self) -> list[tuple[str, str]]:
        """A curated, cinematic run through JARVIS's best moments. Uses canned
        replies (real neural voice + live HUD) so the sequence is always perfectly
        paced and clean — no app windows, no network variance — ideal to record."""
        u = self.cfg.user_title
        return [
            ("jarvis", f"J.A.R.V.I.S. online, {u}. All systems nominal — arc reactor at full charge."),
            ("user",   "Jarvis, what can you do?"),
            ("jarvis", "Rather a lot. I open apps and websites, control media, volume and "
                       "brightness, run the numbers, set timers, reminders and alarms, read "
                       "the news and weather, take notes — and I'm always happy to just talk."),
            ("user",   "What's fifteen percent of two thousand four hundred?"),
            ("jarvis", f"That's three hundred and sixty, {u}. Effortlessly."),
            ("user",   "Put on some lo-fi to focus."),
            ("jarvis", f"Cueing up a lo-fi mix, {u}. Consider the mood set."),
            ("user",   "Give me something clever."),
            ("jarvis", "The best way to predict the future is to build it — ideally before "
                       "the coffee gets cold."),
            ("user",   "Thank you, Jarvis."),
            ("jarvis", f"Always a pleasure, {u}. I'll be right here."),
        ]

    def run_demo(self) -> None:
        """Play the showcase on a background thread (idempotent while running)."""
        if self._demo_active.is_set() or self.stop.is_set():
            return
        self._demo_active.set()

        def _go():
            try:
                with self._turn_lock:               # own the stage: no turns interleave
                    self.hud.status("showcase")
                    for who, line in self._demo_script():
                        if self.stop.is_set():
                            break
                        if who == "user":
                            self.hud.state("listening")
                            self.hud.user(line)
                            time.sleep(1.1)
                        else:
                            self._say(line)         # real neural voice + reactive HUD
                            time.sleep(0.5)
                    self.hud.state("idle")
                    self.hud.status("online")
            except Exception as e:  # noqa: BLE001
                print(f"[assistant] demo error: {e}")
            finally:
                self._demo_active.clear()

        threading.Thread(target=_go, name="demo", daemon=True).start()

    # ── background agent missions ("Jarvis works while you focus") ──
    def run_mission(self, title: str, worker, tag: str = "AGENT") -> "Mission":
        """Run ``worker(mission)`` on a daemon thread so JARVIS keeps taking new
        commands while it works. Progress streams to the HUD agent panel; the
        worker speaks only when it matters. Returns the Mission (already live)."""
        with self._mission_lock:
            self._mid += 1
            mid = f"m{self._mid}"
            mission = Mission(self.hud, mid, title, tag, self._say)
            self._missions.append(mission)

        # missions that open real windows or read the screen shrink the HUD to a corner
        # so the real windows (Chrome, files, Instagram) are visible / captured cleanly.
        minimize = tag in _MINIMIZE_TAGS
        if minimize:
            self._enter_compact(title)

        def go():
            try:
                worker(mission)
                if mission.status == "running":
                    mission.finish("done")
            except Exception as e:  # noqa: BLE001 — a mission must never crash the app
                print(f"[assistant] mission '{title}' error: {e}")
                mission.error("ran into a snag")
            finally:
                with self._mission_lock:
                    try:
                        self._missions.remove(mission)
                    except ValueError:
                        pass
                if minimize:
                    self._exit_compact()

        threading.Thread(target=go, name=f"mission-{mid}", daemon=True).start()
        return mission

    def active_missions(self) -> list[str]:
        """Titles of missions still running — for 'what are you working on'."""
        with self._mission_lock:
            return [m.title for m in self._missions if m.status == "running"]

    # ── brain turn: speak the answer, OR hand a real task to the agent ──
    def _handle_brain(self, text: str) -> None:
        """Consume the brain's streamed reply. If it opens with a `DELEGATE: <task>`
        directive, hand that task to the full-tool agent (a background mission)
        instead of speaking; otherwise speak the reply as it streams."""
        it = iter(self.brain.ask_stream(text))
        buf = ""
        try:
            for piece in it:
                buf += piece
                if len(buf) >= 12 or "\n" in buf:   # enough to spot a DELEGATE header
                    break
        except Exception as e:  # noqa: BLE001
            print(f"[assistant] brain stream error: {e}")

        if re.match(r"\s*DELEGATE\b", buf, re.IGNORECASE):
            rest = ""
            try:
                rest = "".join(it)                  # drain the full directive
            except Exception:
                pass
            task = re.sub(r"^\s*DELEGATE\s*:?\s*", "", (buf + rest), flags=re.IGNORECASE).strip()
            task = task.strip().strip('"').strip()
            if task:
                self._delegate(task)
                return
            # empty task → fall through and just acknowledge
            self._say(f"On it, {self.cfg.user_title}.")
            return

        # ordinary spoken reply: replay the buffered head, then the rest of the stream
        def chained():
            if buf:
                yield buf
            for p in it:
                yield p
        self._say_stream(chained())

    def _delegate(self, task: str) -> None:
        """Run a real task on the full-capability agent, as a live background mission."""
        self._say(f"On it, {self.cfg.user_title}. Let me take care of that.")
        self.run_mission(f"Task: {task[:46]}",
                         lambda m: self.skills.run_agentic(m, task), tag="AGENT")

    # ── conversation reset (for "forget our conversation") ──────
    def _forget_conversation(self) -> None:
        self.brain.forget()

    # ── screen vision (for "what's on my screen") ───────────────
    def _describe_screen(self, question: str, image_b64: str) -> str:
        try:
            return self.brain.ask_image(question, image_b64)
        except Exception as e:
            print(f"[assistant] vision failed: {e}")
            return (f"I couldn't get a clear look just now, {self.cfg.user_title}. "
                    f"If it keeps happening, add an Anthropic or Groq key in the settings gear.")

    # ── speaking helper (also used by skills like timers) ───────
    def _say(self, text: str) -> None:
        self.last_reply = text
        self.hud.state("speaking")
        self.hud.jarvis(text)
        self.mouth.speak(text)
        self.hud.state("idle")

    def _say_stream(self, chunks) -> None:
        """Speak a streamed brain reply sentence-by-sentence, updating the HUD
        subtitle live as words arrive. Stays in 'thinking' until the first token."""
        state = {"speaking": False}

        def on_text(full: str) -> None:
            if not state["speaking"]:
                state["speaking"] = True
                self.hud.state("speaking")
            self.hud.jarvis_stream(full)

        full = self.mouth.speak_stream(chunks, on_text=on_text)
        full = (full or "").strip()
        if full:
            self.last_reply = full
            self.hud.jarvis(full)
        self.hud.state("idle")

    # ── live settings ───────────────────────────────────────────
    def _send_config(self) -> None:
        c = self.cfg
        self.hud.send({"type": "config", "config": {
            "brain": c.brain, "groq_model": c.groq_model, "ollama_model": c.ollama_model,
            "has_groq_key": bool(c.groq_api_key),
            "has_anthropic_key": bool(c.anthropic_api_key), "anthropic_model": c.anthropic_model,
            "whisper_model": c.whisper_model, "tts_engine": c.tts_engine, "tts_voice": c.tts_voice,
            "tts_rate": c.tts_rate, "wakeword_threshold": c.wakeword_threshold,
            "user_title": c.user_title, "enable_voice": c.enable_voice,
            "enable_wakeword": c.enable_wakeword,
            "enable_clap": c.enable_clap, "clap_count": c.clap_count,
            "clap_sensitivity": c.clap_sensitivity,
            "enable_tray": c.enable_tray, "close_to_tray": c.close_to_tray,
        }})

    def _apply_config(self, d: dict) -> None:
        c = self.cfg
        changed = []
        if d.get("brain") in ("anthropic", "claude", "groq", "ollama"):
            c.brain = d["brain"]; self.brain.active = d["brain"]; changed.append("brain")
        if "anthropic_api_key" in d and isinstance(d["anthropic_api_key"], str):
            c.anthropic_api_key = d["anthropic_api_key"].strip()
            # pasting a fast key should just make JARVIS fast — flip off the slow
            # default CLI backend automatically so the user needn't touch the dropdown.
            if c.anthropic_api_key and c.brain == "claude":
                c.brain = "anthropic"; self.brain.active = "anthropic"
            changed.append("Anthropic key")
        if d.get("anthropic_model"):
            c.anthropic_model = d["anthropic_model"]
        if d.get("groq_api_key"):
            c.groq_api_key = d["groq_api_key"].strip()
            if c.groq_api_key and c.brain == "claude":
                c.brain = "groq"; self.brain.active = "groq"
            changed.append("Groq key — instant replies enabled")
        if d.get("groq_model"):
            c.groq_model = d["groq_model"]
        if d.get("ollama_model"):
            c.ollama_model = d["ollama_model"]
        if d.get("tts_engine") in ("edge", "sapi"):
            c.tts_engine = d["tts_engine"]; changed.append("voice engine")
        if d.get("tts_voice"):
            c.tts_voice = d["tts_voice"]; changed.append("voice")
        if isinstance(d.get("tts_rate"), str) and d["tts_rate"].strip():
            c.tts_rate = d["tts_rate"].strip()
        if isinstance(d.get("user_title"), str) and d["user_title"].strip():
            c.user_title = d["user_title"].strip()
            self.brain.set_title(c.user_title)
            changed.append("form of address")
        if "wakeword_threshold" in d:
            try:
                c.wakeword_threshold = max(0.1, min(0.95, float(d["wakeword_threshold"])))
                changed.append("wake sensitivity")
            except (TypeError, ValueError):
                pass
        if "enable_voice" in d:
            c.enable_voice = bool(d["enable_voice"])
        if "enable_clap" in d:
            c.enable_clap = bool(d["enable_clap"]); changed.append("clap trigger")
        if d.get("clap_count") in (1, 2):
            c.clap_count = int(d["clap_count"])
        if "clap_sensitivity" in d:
            try:
                c.clap_sensitivity = max(0.08, min(0.6, float(d["clap_sensitivity"])))
            except (TypeError, ValueError):
                pass
        if "close_to_tray" in d:
            # only honour hide-to-tray when a tray icon is actually available
            c.close_to_tray = bool(d["close_to_tray"]) and c.enable_tray
        # heaviest change last: swap the speech-to-text model on a worker
        new_model = d.get("whisper_model")
        if new_model in WHISPER_SIZES and new_model != c.whisper_model:
            changed.append("speech model")
            if self.cfg.enable_voice and self.ears._model is not None:
                def _reload(m=new_model):
                    self.hud.status(f"loading {m} model")
                    ok = self.ears.reload_whisper(m)
                    self.hud.status("online" if ok else "model load failed")
                threading.Thread(target=_reload, daemon=True).start()
            else:
                c.whisper_model = new_model
        c.save()
        self._send_config()
        self.hud.brain(c.brain)
        self.hud.jarvis("Configuration updated, " + c.user_title + "." if changed else "No changes made.")
        self.hud.state("idle")

    # ── background telemetry to the HUD ─────────────────────────
    def _telemetry_loop(self) -> None:
        import psutil
        while not self.stop.is_set():
            try:
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory().percent
                self.hud.telemetry(round(cpu), round(mem))
                self.hud.brain(self.brain.active)
            except Exception:
                pass
            time.sleep(2.0)

    # ── main loop ───────────────────────────────────────────────
    def run(self) -> None:
        # try/finally guarantees shutdown() (which reaps the persistent Claude
        # child) runs even if mic/model/TTS init raises — otherwise a crash here
        # would orphan a heavy claude.exe subprocess.
        try:
            self.hud.status("initialising")
            # warm the brain (spawns + primes the persistent Claude session) in the
            # background so the first spoken question doesn't pay the ~20 s cold-start
            threading.Thread(target=self.brain.warmup, name="brain-warmup", daemon=True).start()
            if self.cfg.enable_voice:
                self.ears.load()
                self.ears.open_stream()
            self.hud.state("idle")
            threading.Thread(target=self._telemetry_loop, name="telemetry", daemon=True).start()
            self._send_config()

            time.sleep(1.2)   # let the HUD boot animation breathe
            hour = time.localtime().tm_hour
            greet = ("Good morning" if hour < 12 else
                     "Good afternoon" if hour < 18 else "Good evening")
            mode = "awaiting your command" if self.cfg.enable_voice else "in keyboard mode"
            self._say(f"{greet}, {self.cfg.user_title}. JARVIS online and {mode}.")

            if self.autostart_demo:              # launched with --demo: play the showcase
                self.run_demo()

            if self.cfg.enable_voice:
                self._voice_loop()
            else:
                while not self.stop.is_set():   # text-only: turns arrive via the HUD bar
                    time.sleep(0.15)
        finally:
            self.shutdown()

    def _voice_loop(self) -> None:
        while not self.stop.is_set():
            self.hud.state("idle")
            woke = self.ears.wait_for_wake(self.stop, self.ptt)
            if not woke or self.stop.is_set():
                break
            self.hud.state("listening")
            self.hud.status("listening")
            text = self.ears.capture_command(self.stop)
            if not text:
                self.hud.state("idle")
                continue
            self._process(text)
            if self.stop.is_set():
                break

    def shutdown(self) -> None:
        self.stop.set()
        self.hud.state("offline")
        self.ears.close()
        try:
            self.brain.close()
        except Exception:
            pass
