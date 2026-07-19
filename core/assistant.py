"""The orchestrator — ties ears, brain, mouth, skills and the HUD together.

Flow per turn:
    idle → (wake word "hey jarvis" | click core | Space) → listening → capture
         → thinking → skills.handle(text) or brain.ask(text) → speaking → idle
"""
from __future__ import annotations

import threading
import time

from .brain import Brain
from .skills import Skills
from .speech import Mouth
from .voice import Ears


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
        self.on_quit = None                # set by run.py to close the window
        hud.on_message = self._on_hud

    # ── HUD → assistant messages ────────────────────────────────
    def _on_hud(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "push_to_talk":
            self.ptt.set()
        elif kind == "stop":
            self.mouth.interrupt()
        elif kind == "quit":
            self.stop.set()
            self.mouth.interrupt()
            if self.on_quit:
                self.on_quit()

    # ── speaking helper (used by skills like timers too) ────────
    def _say(self, text: str) -> None:
        self.hud.state("speaking")
        self.hud.jarvis(text)
        self.mouth.speak(text)
        self.hud.state("idle")

    # ── background telemetry to the HUD ─────────────────────────
    def _telemetry_loop(self) -> None:
        import psutil
        self.hud.brain(self.brain.active)
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
        self.ears.load()
        self.ears.open_stream()
        self.hud.state("idle")
        threading.Thread(target=self._telemetry_loop, name="telemetry", daemon=True).start()

        # let the HUD boot animation breathe, then greet
        time.sleep(1.2)
        self._say(f"Good day, {self.cfg.user_title}. JARVIS online and awaiting your command.")

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

            self.hud.user(text)
            self.hud.state("thinking")
            self.hud.status("processing")

            reply = None
            try:
                reply = self.skills.handle(text)
            except Exception as e:
                print(f"[assistant] skill error: {e}")

            if reply is None:                     # not a local command → ask the brain
                reply = self.brain.ask(text)

            if reply:
                self._say(reply)

            if self.skills.should_exit:
                self.stop.set()
                break

        self.shutdown()

    def shutdown(self) -> None:
        self.stop.set()
        self.hud.state("offline")
        self.ears.close()
