"""The orchestrator — ties ears, brain, mouth, skills and the HUD together.

A turn can be triggered three ways, all routed through the same pipeline:
    • voice  — wake word "hey jarvis" (or click core / Space) → listen → transcribe
    • text   — typed into the HUD command bar
    • timer  — a skill speaking on its own (e.g. a finished timer)

Flow per turn:  input → thinking → skills.handle() or brain.ask() → speaking → idle
"""
from __future__ import annotations

import threading
import time

from .brain import Brain, SYSTEM
from .skills import Skills
from .speech import Mouth
from .voice import Ears

WHISPER_SIZES = ("tiny", "base", "small", "medium")


class Assistant:
    def __init__(self, cfg, hud):
        self.cfg = cfg
        self.hud = hud
        self.brain = Brain(cfg)
        self.mouth = Mouth(cfg, hud)
        self.ears = Ears(cfg, hud)
        self.skills = Skills(cfg, hud, self._say)
        self.stop = threading.Event()
        self.ptt = threading.Event()       # push-to-talk trigger from the HUD
        self._turn_lock = threading.Lock()  # serialises voice/text turns
        self.on_quit = None                # set by run.py to close the window
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
        elif kind == "quit":
            self.stop.set()
            self.mouth.interrupt()
            if self.on_quit:
                self.on_quit()

    # ── a single turn (voice or text) ───────────────────────────
    def _process(self, text: str) -> None:
        if not text:
            return
        with self._turn_lock:
            if self.stop.is_set():
                return
            self.hud.user(text)
            self.hud.state("thinking")
            self.hud.status("processing")
            reply = None
            try:
                reply = self.skills.handle(text)
            except Exception as e:
                print(f"[assistant] skill error: {e}")
            if reply is None:                    # not a local command → ask the brain
                reply = self.brain.ask(text)
            if reply:
                self._say(reply)
            else:
                self.hud.state("idle")
            if self.skills.should_exit:
                self.stop.set()
                if self.on_quit:
                    self.on_quit()

    # ── speaking helper (also used by skills like timers) ───────
    def _say(self, text: str) -> None:
        self.hud.state("speaking")
        self.hud.jarvis(text)
        self.mouth.speak(text)
        self.hud.state("idle")

    # ── live settings ───────────────────────────────────────────
    def _send_config(self) -> None:
        c = self.cfg
        self.hud.send({"type": "config", "config": {
            "brain": c.brain, "groq_model": c.groq_model, "ollama_model": c.ollama_model,
            "has_groq_key": bool(c.groq_api_key),
            "whisper_model": c.whisper_model, "tts_engine": c.tts_engine, "tts_voice": c.tts_voice,
            "tts_rate": c.tts_rate, "wakeword_threshold": c.wakeword_threshold,
            "user_title": c.user_title, "enable_voice": c.enable_voice,
            "enable_wakeword": c.enable_wakeword,
            "enable_clap": c.enable_clap, "clap_count": c.clap_count,
            "clap_sensitivity": c.clap_sensitivity,
        }})

    def _apply_config(self, d: dict) -> None:
        c = self.cfg
        changed = []
        if d.get("brain") in ("claude", "groq", "ollama"):
            c.brain = d["brain"]; self.brain.active = d["brain"]; changed.append("brain")
        if d.get("groq_api_key"):
            c.groq_api_key = d["groq_api_key"]
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
            self.brain._system = SYSTEM.format(title=c.user_title)
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
        self.hud.status("initialising")
        if self.cfg.enable_voice:
            self.ears.load()
            self.ears.open_stream()
        self.hud.state("idle")
        threading.Thread(target=self._telemetry_loop, name="telemetry", daemon=True).start()
        self._send_config()

        time.sleep(1.2)   # let the HUD boot animation breathe
        mode = "awaiting your command" if self.cfg.enable_voice else "in keyboard mode"
        self._say(f"Good day, {self.cfg.user_title}. JARVIS online and {mode}.")

        if self.cfg.enable_voice:
            self._voice_loop()
        else:
            while not self.stop.is_set():   # text-only: turns arrive via the HUD bar
                time.sleep(0.15)

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
