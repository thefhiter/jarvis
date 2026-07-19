"""Preview the JARVIS HUD without a microphone or the assistant.

Starts the HUD WebSocket bridge + a tiny web server, then cycles through the
idle -> listening -> thinking -> speaking states with fake audio so you can see
the arc reactor react. Open the printed URL in a browser.

    .venv\\Scripts\\python demo_hud.py
"""
import functools
import http.server
import math
import threading
import time
from pathlib import Path

from core.hud import Hud

ROOT = Path(__file__).resolve().parent
UI_DIR = ROOT / "ui"
WEB_PORT = 8766


def serve_ui():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(UI_DIR))
    httpd = http.server.HTTPServer(("127.0.0.1", WEB_PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()


def fake_spectrum(t, energy):
    bins = []
    for i in range(32):
        v = (0.5 + 0.5 * math.sin(t * 6 + i * 0.6)) * energy * (1 - i / 48)
        bins.append(round(max(0.0, v), 3))
    return bins


def main():
    hud = Hud()
    hud.start()
    serve_ui()
    url = f"http://127.0.0.1:{WEB_PORT}/jarvis.html"
    print(f"\n  JARVIS HUD preview running.\n  Open:  {url}\n  Ctrl+C to stop.\n")

    hud.brain("demo")
    scenes = [
        ("idle", "", "", 3),
        ("listening", "what's the weather like today", "", 3),
        ("thinking", "", "", 2),
        ("speaking", "", "It's a clear evening, sir — 24 degrees and calm.", 4),
        ("idle", "", "Standing by.", 2),
        ("listening", "jarvis, launch the arc reactor diagnostics", "", 3),
        ("thinking", "", "", 2),
        ("speaking", "", "Diagnostics complete. All systems at optimal capacity.", 4),
    ]
    t0 = time.time()
    while True:
        for state, user, jarvis, dur in scenes:
            hud.state(state)
            if user:
                hud.user(user)
            if jarvis:
                hud.jarvis(jarvis)
            end = time.time() + dur
            while time.time() < end:
                t = time.time() - t0
                cpu = 20 + 15 * (0.5 + 0.5 * math.sin(t * 0.5))
                hud.telemetry(round(cpu), 47)
                hud.brain("demo")
                if state in ("listening", "speaking"):
                    energy = 0.75 if state == "speaking" else 0.55
                    hud.spectrum(fake_spectrum(t, energy), level=0.4 + 0.4 * abs(math.sin(t * 3)))
                time.sleep(1 / 30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped.")
