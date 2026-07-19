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


SAMPLE_CONFIG = {
    "brain": "claude", "groq_model": "llama-3.3-70b-versatile", "ollama_model": "llama3.2",
    "has_groq_key": False, "whisper_model": "base", "tts_engine": "edge",
    "tts_voice": "en-GB-RyanNeural", "wakeword_threshold": 0.5, "user_title": "sir",
    "enable_voice": True, "enable_wakeword": True,
    "enable_clap": True, "clap_count": 2, "clap_sensitivity": 0.22,
}


def main():
    hud = Hud()

    def on_msg(m):
        t = m.get("type")
        if t == "get_config":
            hud.send({"type": "config", "config": SAMPLE_CONFIG})
        elif t == "set_config":
            SAMPLE_CONFIG.update(m.get("config") or {})
            hud.send({"type": "config", "config": SAMPLE_CONFIG})
            hud.jarvis("Configuration updated (demo).")
        elif t == "text_command":
            hud.state("thinking")
            hud.jarvis(f"You typed: {m.get('text','')}. (Preview mode — no real brain.)")
    hud.on_message = on_msg

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
            if state == "listening":
                hud.send({"type": "pulse"})       # simulate a clap shockwave
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
