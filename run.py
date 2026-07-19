"""J.A.R.V.I.S. entry point.

Starts the HUD WebSocket bridge, launches the cinematic front-end in a
frameless always-on-top WebView2 window with proper app window controls
(minimize / maximize / close) and a system-tray icon, and runs the voice
assistant loop on a background thread.

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
from core.appicon import ensure_icon
from core.tray import Tray

ROOT = Path(__file__).resolve().parent
UI = ROOT / "ui" / "jarvis.html"
ICON = ROOT / "assets" / "jarvis.ico"


def main() -> int:
    cfg = config.load()
    icon_path = ensure_icon(str(ICON))

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

    # ── window controls (all safe to call from the WS / tray threads) ──
    win_state = {"max": False}

    def _safe(fn, *a):
        try:
            fn(*a)
        except Exception as e:
            print(f"[window] {fn.__name__} failed: {e}")

    def do_minimize():
        _safe(window.minimize)

    def do_show():
        _safe(window.restore)          # un-minimise if needed
        _safe(window.show)             # reveal if hidden to tray
        win_state["max"] = False

    def do_hide():
        _safe(window.hide)

    def do_toggle_max():
        if win_state["max"]:
            _safe(window.restore)
            win_state["max"] = False
        else:
            _safe(window.maximize)
            win_state["max"] = True

    tray_ref = {"tray": None}

    def do_quit():
        assistant.stop.set()
        try:
            assistant.mouth.interrupt()
        except Exception:
            pass
        if tray_ref["tray"]:
            tray_ref["tray"].stop()
        try:
            window.destroy()
        except Exception:
            pass

    assistant.on_minimize = do_minimize
    assistant.on_toggle_max = do_toggle_max
    assistant.on_show = do_show
    assistant.on_hide = do_hide
    assistant.on_quit = do_quit

    # ── system tray (persistent-app feel; degrades to None if unavailable) ──
    if cfg.enable_tray:
        tray = Tray(icon_path, {
            "show": do_show,
            "hide": do_hide,
            "minimize": do_minimize,
            "quit": do_quit,
        })
        if tray.start():
            tray_ref["tray"] = tray
            tray.notify("JARVIS is online. Right-click the tray icon for options.")
    if tray_ref["tray"] is None:
        cfg.enable_tray = False
        cfg.close_to_tray = False      # can't hide safely without a tray to bring it back

    # webview.start(func) runs func on a background thread once the GUI is ready
    def start_assistant():
        try:
            assistant.run()
        except Exception:
            import traceback; traceback.print_exc()

    webview.start(start_assistant, icon=icon_path)  # blocks until the window closes
    assistant.stop.set()
    if tray_ref["tray"]:
        tray_ref["tray"].stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
