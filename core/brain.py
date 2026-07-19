"""The 'brain' — turns a spoken question into a spoken answer.

Pluggable across three back-ends, tried in order of preference:

  1. claude  — the locally installed Claude Code CLI (uses your subscription,
               no API key needed).  Best quality.
  2. groq    — free Groq API (needs a free key).
  3. ollama  — a local model server, fully offline.

Only general conversation / knowledge questions reach the brain; deterministic
"powers" (open apps, volume, etc.) are handled first by skills.py.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent

# phrases that mean "the model is temporarily unavailable / throttled", not a real answer
RATE_HINTS = ("usage limit", "rate limit", "session limit", "limit reached", "quota",
              "overloaded", "resets", "too many requests", "429", "try again later")

SYSTEM = (
    "You are JARVIS, a witty, hyper-competent AI butler modelled on the assistant "
    "from Iron Man. You are speaking OUT LOUD to your user, whom you address as "
    "'{title}'. Keep replies SHORT — one to three spoken sentences, no lists, no "
    "markdown, no emoji, no stage directions. Be warm, precise and lightly dry. "
    "If asked to do something on the computer you cannot directly do, briefly say so."
)


class Brain:
    def __init__(self, cfg):
        self.cfg = cfg
        self.claude = shutil.which("claude") or "claude"
        self.history: list[tuple[str, str]] = []
        self.active = cfg.brain
        self._system = SYSTEM.format(title=cfg.user_title)
        # run the CLI from an empty scratch dir so it does NOT load the project's
        # CLAUDE.md / memory on every question (that tripled cost + latency)
        self._cwd = Path(tempfile.gettempdir()) / "jarvis_brain"
        try:
            self._cwd.mkdir(exist_ok=True)
        except Exception:
            self._cwd = ROOT

    # ── public ──────────────────────────────────────────────────
    def ask(self, prompt: str) -> str:
        chain = [self.cfg.brain] + [b for b in ("claude", "groq", "ollama") if b != self.cfg.brain]
        errors: dict[str, str] = {}
        for engine in chain:
            try:
                fn = getattr(self, f"_ask_{engine}")
                reply = (fn(prompt) or "").strip()
                if reply:
                    self.active = engine
                    self._remember(prompt, reply)
                    return reply
                errors[engine] = "empty reply"
            except Exception as e:  # try next engine
                errors[engine] = str(e)
                print(f"[brain] {engine} unavailable: {e}")
                continue
        return self._fallback_message(errors)

    def _fallback_message(self, errors: dict[str, str]) -> str:
        """Pick the most helpful spoken message for why every engine failed."""
        title = self.cfg.user_title
        blob = " ".join(errors.values()).lower()
        if any(h in blob for h in RATE_HINTS):
            return (f"My uplink to Claude has hit its usage limit, {title}. It resets "
                    f"shortly — or add a free Groq key in settings and I'll switch over instantly.")
        if any(s in blob for s in ("not found", "winerror 2", "no such file", "cannot find")):
            return (f"I can't find the Claude command, {title}. Make sure Claude Code is "
                    f"installed and on your PATH, or set a Groq key in settings.")
        return (f"My cognitive uplink is offline at the moment, {title}. No model is reachable — "
                f"add a free Groq key or start Ollama in settings and I'll be right back.")

    # ── memory ──────────────────────────────────────────────────
    def _remember(self, user: str, reply: str) -> None:
        self.history.append(("user", user))
        self.history.append(("assistant", reply))
        self.history = self.history[-8:]  # keep last 4 exchanges

    def _context(self) -> str:
        if not self.history:
            return ""
        lines = []
        for role, text in self.history[-6:]:
            who = self.cfg.user_title.capitalize() if role == "user" else "JARVIS"
            lines.append(f"{who}: {text}")
        return "Recent conversation:\n" + "\n".join(lines) + "\n\n"

    # ── back-ends ───────────────────────────────────────────────
    def _ask_claude(self, prompt: str) -> str:
        # --strict-mcp-config (with no --mcp-config) loads zero MCP servers → lean, cheap call
        cmd = [self.claude, "-p", "--output-format", "json", "--strict-mcp-config"]
        if self.cfg.claude_model:
            cmd += ["--model", self.cfg.claude_model]
        cmd += ["--append-system-prompt", self._system]
        cmd += [self._context() + prompt]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=90, cwd=str(self._cwd),
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"claude command not found ({e})")
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if not out:
            raise RuntimeError(f"claude exited {proc.returncode}, no output (stderr: {err[:200]})")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            low = out.lower()
            if any(h in low for h in RATE_HINTS) or low.startswith(("error", "usage")):
                raise RuntimeError(f"claude error output: {out[:200]}")
            return out  # genuine plain-text answer
        if data.get("is_error") or data.get("subtype", "").startswith("error"):
            detail = data.get("result") or data.get("error") or err or "unknown error"
            raise RuntimeError(f"claude reported an error: {str(detail)[:200]}")
        return data.get("result") or data.get("response") or ""

    def _ask_groq(self, prompt: str) -> str:
        if not self.cfg.groq_api_key:
            raise RuntimeError("no groq api key")
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.cfg.groq_api_key}"},
            json={
                "model": self.cfg.groq_model,
                "messages": self._messages(prompt),
                "temperature": 0.6,
                "max_tokens": 300,
            },
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def _ask_ollama(self, prompt: str) -> str:
        r = requests.post(
            f"{self.cfg.ollama_url}/api/chat",
            json={"model": self.cfg.ollama_model, "messages": self._messages(prompt), "stream": False},
            timeout=120,
        )
        r.raise_for_status()
        return r.json()["message"]["content"]

    def _messages(self, prompt: str) -> list[dict]:
        msgs = [{"role": "system", "content": self._system}]
        for role, text in self.history[-6:]:
            msgs.append({"role": role, "content": text})
        msgs.append({"role": "user", "content": prompt})
        return msgs
