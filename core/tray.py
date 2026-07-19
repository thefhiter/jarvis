"""System-tray icon for JARVIS — makes it behave like a persistent app.

While JARVIS runs, an arc-reactor icon sits in the Windows tray. Left-click (or
the "Show" item) brings the HUD forward; the menu can hide it to the tray,
minimise it, or power the whole assistant down.

pystray runs its own message loop, so the icon lives on a daemon thread. If
pystray/Pillow are unavailable the whole thing degrades to ``None`` and the app
still works with just the in-window controls.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional


class Tray:
    def __init__(self, icon_path: str, actions: dict[str, Callable[[], None]]):
        self._icon_path = icon_path
        self._actions = actions
        self._icon = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        """Spin up the tray icon on a daemon thread. Returns False if unavailable."""
        try:
            import pystray
            from PIL import Image
        except Exception as e:                       # pystray/PIL missing
            print(f"[tray] disabled ({e})")
            return False

        try:
            image = Image.open(self._icon_path)
        except Exception as e:
            print(f"[tray] could not load icon ({e})")
            return False

        a = self._actions

        def do(key):
            return lambda *_: a.get(key, lambda: None)()

        menu = pystray.Menu(
            pystray.MenuItem("Show JARVIS", do("show"), default=True),
            pystray.MenuItem("Hide to tray", do("hide")),
            pystray.MenuItem("Minimize", do("minimize")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Power Down (Quit)", do("quit")),
        )
        self._icon = pystray.Icon("jarvis", image, "J.A.R.V.I.S.", menu)

        def run():
            try:
                self._icon.run()
            except Exception as e:
                print(f"[tray] loop ended ({e})")

        self._thread = threading.Thread(target=run, name="tray", daemon=True)
        self._thread.start()
        return True

    def notify(self, message: str, title: str = "J.A.R.V.I.S.") -> None:
        if self._icon is not None:
            try:
                self._icon.notify(message, title)
            except Exception:
                pass

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None
