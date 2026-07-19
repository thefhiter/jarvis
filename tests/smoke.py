"""Headless smoke test — verifies config, the skills matcher and the brain
without needing a microphone, speakers or the GUI.

    .venv\\Scripts\\python tests\\smoke.py
"""
import sys
import types
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import truststore
truststore.inject_into_ssl()

from core import config


class FakeHud:
    def __getattr__(self, _):
        return lambda *a, **k: None


def test_skills():
    # Neutralise every real side-effect so the matcher can be exercised safely and
    # repeatably (no windows opening, no screen lock, no volume/brightness change).
    # IMPORTANT: never patch subprocess.Popen globally — subprocess.run() (used by
    # the brain) depends on it. We patch the skills module's own reference only.
    import core.skills as S
    import webbrowser, os, subprocess
    noop = lambda *a, **k: None
    # subprocess is a shared singleton — save Popen and restore it after this test,
    # otherwise the brain's subprocess.run() (which internally uses Popen) breaks.
    _orig_popen = subprocess.Popen
    webbrowser.open = lambda *a, **k: True
    subprocess.Popen = lambda *a, **k: types.SimpleNamespace(pid=0)
    if hasattr(os, "startfile"):
        os.startfile = noop                        # type: ignore[attr-defined]
    # import these BEFORE touching ctypes (their import chains need the real windll)
    import pyautogui; pyautogui.hotkey = noop; pyautogui.typewrite = noop
    import screen_brightness_control as sbc
    sbc.set_brightness = noop; sbc.get_brightness = lambda *a, **k: [50]
    import ctypes
    try:
        ctypes.windll.user32.LockWorkStation = noop   # surgical, keep ctypes intact
    except Exception:
        pass

    from core.skills import Skills
    cfg = config.load()
    said = []
    sk = Skills(cfg, FakeHud(), say=lambda t: said.append(t))

    class FakeVol:  # avoid touching the real system volume
        def GetMasterVolumeLevelScalar(self): return 0.5
        def SetMasterVolumeLevelScalar(self, *a): pass
        def SetMute(self, *a): pass
    sk._vol_iface = lambda: FakeVol()
    phrases = [
        "what time is it", "what's the date", "who are you", "what can you do",
        "open notepad", "open youtube", "search for quantum computing",
        "play daft punk on youtube", "set volume to 40", "volume up",
        "increase the brightness", "take a screenshot", "system status",
        "battery status", "minimize everything", "make a note buy milk",
        "read my notes", "set a timer for 2 minutes", "what's the weather",
        "copy hello world to the clipboard", "lock the computer",
        "thank you jarvis", "hello jarvis",
    ]
    print("── skills matcher ──")
    hits = 0
    for p in phrases:
        r = sk.handle(p)
        tag = "OK " if r is not None else "MISS"
        if r is not None:
            hits += 1
        print(f"  [{tag}] {p!r:45} -> {str(r)[:70]}")
    print(f"  matched {hits}/{len(phrases)}")
    # a non-command should fall through to the brain (None)
    assert sk.handle("what is the tallest mountain on earth") is None, "should escalate to brain"
    print("  fall-through to brain: OK")
    subprocess.Popen = _orig_popen        # restore for the brain test


def test_brain():
    from core.brain import Brain
    cfg = config.load()
    b = Brain(cfg)
    print("\n── brain ──")
    reply = b.ask("In one short sentence, what is the capital of France?")
    print(f"  engine used : {b.active}")
    print(f"  reply       : {reply}")
    assert reply and "paris" in reply.lower(), "brain did not answer correctly"
    print("  brain: OK")


if __name__ == "__main__":
    test_skills()
    test_brain()
    print("\nALL SMOKE TESTS PASSED")
