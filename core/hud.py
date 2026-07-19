"""HUD bridge — a tiny WebSocket server that streams JARVIS state to the
cinematic front-end (ui/jarvis.html) and receives clicks/keys back.

Runs its own asyncio loop on a daemon thread so the rest of the app (voice
loop + pywebview) can call ``hud.send(...)`` from any thread safely.
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Callable, Optional

import websockets


class Hud:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self._clients: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self.on_message: Optional[Callable[[dict], None]] = None
        self._ready = threading.Event()

    # ── lifecycle ───────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="hud-ws", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())
        self._loop.run_forever()

    async def _serve(self) -> None:
        self._server = await websockets.serve(self._handler, self.host, self.port)
        self._ready.set()

    async def _handler(self, ws) -> None:
        self._clients.add(ws)
        try:
            async for raw in ws:
                if not self.on_message:
                    continue
                try:
                    self.on_message(json.loads(raw))
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._clients.discard(ws)

    # ── outbound ────────────────────────────────────────────────
    def send(self, obj: dict) -> None:
        """Thread-safe broadcast to every connected HUD client."""
        if not self._loop:
            return
        data = json.dumps(obj)
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(data), self._loop)
        except RuntimeError:
            pass

    async def _broadcast(self, data: str) -> None:
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    # ── convenience helpers ─────────────────────────────────────
    def state(self, mode: str) -> None:      self.send({"type": "state", "state": mode})
    def level(self, v: float) -> None:       self.send({"type": "level", "value": float(v)})
    def spectrum(self, bins, level=0.0):     self.send({"type": "spectrum", "bins": list(bins), "level": float(level)})
    def user(self, text: str) -> None:       self.send({"type": "user", "text": text})
    def jarvis(self, text: str) -> None:     self.send({"type": "jarvis", "text": text})
    def telemetry(self, cpu, mem) -> None:   self.send({"type": "telemetry", "cpu": cpu, "mem": mem})
    def brain(self, name: str) -> None:      self.send({"type": "brain", "name": name})
    def status(self, text: str) -> None:     self.send({"type": "status", "text": text})
