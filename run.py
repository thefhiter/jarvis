"""J.A.R.V.I.S. entry point.

Starts the HUD WebSocket bridge, launches the cinematic front-end in a
frameless always-on-top WebView2 window, and runs the voice assistant loop on
a background thread.

    .venv\\Scripts\\python run.py
"""
import truststore                      # make Python trust the Windows cert store
truststore.inject_into_ssl()

import sys
import threading
from pathlib import Path

import webview

from core import config
from core.hud import Hud
from core.assistant import Assistant

ROOT = Path(__file__).resolve().parent
UI = ROOT / "ui" / "jarvis.html"


def main() -> int:
    cfg = config.load()

    hud = Hud(cfg.ws_host, cfg.ws_port)
    hud.start()

    assistant = Assistant(cfg, hud)

    window = webview.create_window(
        "J.A.R.V.I.S.",
        str(UI),
        width=1200, height=780,
        frameless=True, easy_drag=True,
        on_top=cfg.hud_always_on_top,
        background_color="#02060b",
        fullscreen=cfg.hud_fullscreen,
        min_size=(760, 520),
    )

    def on_quit():
        assistant.stop.set()
        try:
            window.destroy()
        except Exception:
            pass
    assistant.on_quit = on_quit

    # webview.start(func) runs func on a background thread once the GUI is ready
    def start_assistant():
        try:
            assistant.run()
        except Exception:
            import traceback; traceback.print_exc()

    webview.start(start_assistant)     # blocks on the main thread until the window closes
    assistant.stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
