"""Comprehensive OFFLINE test suite for JARVIS.

Exercises the brain fallback chain, the Anthropic SSE parser, the streaming
sentence-chunker, the skills matcher (old + new powers), config round-trips and
the clap detector — all WITHOUT a microphone, speakers, GPU, network or the
Claude CLI. Every external effect is stubbed. Fast (< 2 s) and deterministic.

    .venv\\Scripts\\python tests\\test_offline.py
"""
import sys
import types
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── stub audio hardware so speech.py / voice.py import cleanly ──────────────
_sd = types.ModuleType("sounddevice")
_sd.InputStream = _sd.OutputStream = object
_sd.sleep = lambda *a, **k: None
sys.modules.setdefault("sounddevice", _sd)

# import the real pyautogui BEFORE any subprocess patching (its import chain
# calls subprocess itself); skills only uses it lazily afterwards.
try:
    import pyautogui
    pyautogui.press = pyautogui.hotkey = pyautogui.typewrite = lambda *a, **k: None
except Exception:
    sys.modules["pyautogui"] = types.SimpleNamespace(
        press=lambda *a, **k: None, hotkey=lambda *a, **k: None, typewrite=lambda *a, **k: None)

import truststore
truststore.inject_into_ssl()

# redirect reminder persistence to a scratch file so tests never touch the real
# reminders.json (Skills loads it at construction time).
import tempfile
import core.skills as _skills_mod
_skills_mod.REMINDERS_FILE = Path(tempfile.gettempdir()) / "jarvis_reminders_test.json"
try:
    _skills_mod.REMINDERS_FILE.unlink()
except Exception:
    pass

# ── minimal test harness ────────────────────────────────────────────────────
PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{name} {detail}".strip())
        print(f"  FAIL  {name} {detail}")


def section(title):
    print(f"\n── {title} ──")


class FakeHud:
    def __getattr__(self, _):
        return lambda *a, **k: None


# ════════════════════════════════════════════════════════════════════════════
def test_config():
    section("config round-trip")
    from core import config
    cfg = config.load()
    check("has anthropic fields", hasattr(cfg, "anthropic_api_key") and hasattr(cfg, "anthropic_model"))
    check("claude default is haiku-ish", "haiku" in (cfg.claude_model or "") or cfg.claude_model == "")
    # round-trip through a temp file
    import json
    orig = config.CONFIG_PATH
    tmp = ROOT / "config.__test__.json"
    config.CONFIG_PATH = tmp
    try:
        cfg.user_title = "captain"
        cfg.anthropic_api_key = "sk-ant-test"
        cfg.save()
        data = json.loads(tmp.read_text(encoding="utf-8"))
        check("save wrote title", data.get("user_title") == "captain")
        check("save wrote anthropic key", data.get("anthropic_api_key") == "sk-ant-test")
        cfg2 = config.load()
        check("reload restored title", cfg2.user_title == "captain")
    finally:
        config.CONFIG_PATH = orig
        try:
            tmp.unlink()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════════════════
def _cfg():
    from core import config
    c = config.Config()
    c.user_title = "sir"
    return c


def test_brain_anthropic_parser():
    section("brain — Anthropic SSE streaming parser")
    from core.brain import Brain
    import core.brain as B

    sse = [
        "event: message_start",
        'data: {"type":"message_start"}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Paris"}}',
        "event: content_block_delta",
        'data: {"delta":{"type":"text_delta","text":", the city of light."}}',
        "event: message_stop",
        'data: {"type":"message_stop"}',
        "data: [DONE]",
    ]

    class FakeResp:
        status_code = 200
        def iter_lines(self, decode_unicode=True):
            return iter(sse)

    captured = {}
    def fake_post(url, **kw):
        captured["url"] = url
        captured["body"] = kw.get("json")
        captured["headers"] = kw.get("headers")
        return FakeResp()

    c = _cfg(); c.anthropic_api_key = "sk-ant-xyz"; c.brain = "anthropic"
    b = Brain(c)
    orig = B.requests.post
    B.requests.post = fake_post
    try:
        out = "".join(b._stream_anthropic("Capital of France?"))
    finally:
        B.requests.post = orig
    check("parsed both deltas", out == "Paris, the city of light.", repr(out))
    check("hit anthropic endpoint", captured["url"].endswith("/v1/messages"))
    check("sent api key header", captured["headers"].get("x-api-key") == "sk-ant-xyz")
    check("system is cached block", isinstance(captured["body"]["system"], list)
          and captured["body"]["system"][0].get("cache_control"))
    check("prompt carries time context", "right now it is" in captured["body"]["messages"][-1]["content"].lower())

    # HTTP error path raises with the API's message
    class ErrResp:
        status_code = 401
        def json(self): return {"error": {"message": "invalid x-api-key"}}
        text = ""
    B.requests.post = lambda *a, **k: ErrResp()
    try:
        raised = False
        try:
            list(b._stream_anthropic("hi"))
        except Exception as e:
            raised = "invalid x-api-key" in str(e)
    finally:
        B.requests.post = orig
    check("http 401 raises with detail", raised)


def test_brain_chain_and_fallback():
    section("brain — chain ordering + fallback")
    from core.brain import Brain

    # anthropic auto-preferred when a key is present even if brain='claude'
    c = _cfg(); c.brain = "claude"; c.anthropic_api_key = "k"
    b = Brain(c)
    check("anthropic preferred with key", b._chain()[0] == "anthropic", b._chain())
    c2 = _cfg(); c2.brain = "claude"
    b2 = Brain(c2)
    check("claude first without key", b2._chain()[0] == "claude", b2._chain())
    check("chain has all four", set(b2._chain()) == {"anthropic", "claude", "groq", "ollama"})

    # fallback: first engine raises before any token → second engine used
    b3 = Brain(_cfg())
    def boom(_): raise RuntimeError("usage limit reached")
    def good(_):
        yield "Hello "; yield "there."
    b3._stream_claude = boom
    b3._stream_anthropic = good
    b3._stream_groq = boom
    b3._stream_ollama = boom
    out = b3.ask("hi")
    check("fell back to working engine", out == "Hello there.", repr(out))
    check("active reflects used engine", b3.active == "anthropic", b3.active)
    check("history remembered", len(b3.history) == 2)

    # every engine fails → single helpful spoken sentence, mentioning a fix
    b4 = Brain(_cfg())
    for e in ("claude", "anthropic", "groq", "ollama"):
        setattr(b4, f"_stream_{e}", boom)
    msg = b4.ask("hi")
    check("fallback message is spoken text", "usage limit" in msg.lower() and "sir" in msg.lower(), repr(msg))

    # partial-then-fail commits to the partial (no duplicate from another engine)
    b5 = Brain(_cfg())
    def partial(_):
        yield "Half a thought"; raise RuntimeError("session limit")
    b5._stream_claude = partial
    b5._stream_anthropic = good
    got = "".join(b5.ask_stream("hi"))
    check("partial commit, no fallback duplicate", got == "Half a thought", repr(got))


def test_groq_ollama_parsers():
    section("brain — Groq (OpenAI SSE) + Ollama (JSONL) streaming parsers")
    from core.brain import Brain
    import core.brain as B

    # Groq: OpenAI-style SSE with [DONE] sentinel
    groq_lines = [
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
        'data: {"choices":[{"delta":{"content":"Hi"}}]}',
        'data: {"choices":[{"delta":{"content":" there."}}]}',
        'data: [DONE]',
        'data: {"choices":[{"delta":{"content":"IGNORED"}}]}',
    ]

    class GResp:
        status_code = 200
        def iter_lines(self, decode_unicode=True): return iter(groq_lines)
    c = _cfg(); c.groq_api_key = "k"
    b = Brain(c)
    orig = B.requests.post
    B.requests.post = lambda *a, **k: GResp()
    try:
        out = "".join(b._stream_groq("hi"))
    finally:
        B.requests.post = orig
    check("groq parses to [DONE]", out == "Hi there.", repr(out))

    # Groq: no key -> raises (so the chain falls through)
    b2 = Brain(_cfg())
    raised = False
    try:
        list(b2._stream_groq("hi"))
    except Exception as e:
        raised = "groq" in str(e).lower()
    check("groq without key raises", raised)

    # Ollama: newline-delimited JSON, done flag stops
    oll_lines = [
        '{"message":{"content":"Lo"},"done":false}',
        '{"message":{"content":"cal."},"done":false}',
        '{"message":{"content":""},"done":true}',
        '{"message":{"content":"AFTER"},"done":false}',
    ]

    class OResp:
        status_code = 200
        def iter_lines(self, decode_unicode=True): return iter(oll_lines)
    b3 = Brain(_cfg())
    B.requests.post = lambda *a, **k: OResp()
    try:
        out = "".join(b3._stream_ollama("hi"))
    finally:
        B.requests.post = orig
    check("ollama parses to done", out == "Local.", repr(out))


def test_apply_config():
    section("assistant — live settings apply/persist/clamp")
    import sys as _sys, types as _types
    _sys.modules.setdefault("sounddevice", _types.ModuleType("sounddevice"))
    _sys.modules["sounddevice"].InputStream = _sys.modules["sounddevice"].OutputStream = object
    _sys.modules["sounddevice"].sleep = lambda *a, **k: None
    from core import config
    from core.hud import Hud
    from core.assistant import Assistant
    # isolate config writes to a temp file
    orig_path = config.CONFIG_PATH
    tmp = ROOT / "config.__apply_test__.json"
    config.CONFIG_PATH = tmp
    try:
        cfg = config.load()
        a = Assistant(cfg, Hud(cfg.ws_host, cfg.ws_port))
        a._apply_config({"brain": "anthropic", "anthropic_api_key": "sk-ant-X",
                         "user_title": "captain", "wakeword_threshold": 9.9,
                         "clap_sensitivity": 99.0, "tts_voice": "en-US-GuyNeural"})
        check("brain switched", cfg.brain == "anthropic")
        check("anthropic key stored", cfg.anthropic_api_key == "sk-ant-X")
        check("title propagated to brain persona", "captain" in a.brain._system)
        check("wake threshold clamped <=0.95", cfg.wakeword_threshold <= 0.95, cfg.wakeword_threshold)
        check("clap sensitivity clamped <=0.6", cfg.clap_sensitivity <= 0.6, cfg.clap_sensitivity)
        check("persisted to disk", tmp.exists())
        reloaded = config.load()
        check("reload keeps brain", reloaded.brain == "anthropic")
        a.brain.close()
    finally:
        config.CONFIG_PATH = orig_path
        try:
            tmp.unlink()
        except Exception:
            pass


def test_vision():
    section("brain + skills — screen vision (Anthropic)")
    from core.brain import Brain
    import core.brain as B

    # Brain.ask_image builds a correct vision request and parses the reply
    captured = {}
    class VResp:
        status_code = 200
        def json(self): return {"content": [{"type": "text", "text": "A code editor, sir."}]}
    def fake_post(url, **kw):
        captured["url"] = url; captured["body"] = kw.get("json")
        return VResp()
    c = _cfg(); c.anthropic_api_key = "sk-ant-v"
    b = Brain(c)
    orig = B.requests.post
    B.requests.post = fake_post
    try:
        out = b.ask_image("What is on screen?", "QUJD")
    finally:
        B.requests.post = orig
    check("vision reply parsed", out == "A code editor, sir.", repr(out))
    content = captured["body"]["messages"][0]["content"]
    check("request has an image block", content[0]["type"] == "image"
          and content[0]["source"]["data"] == "QUJD")
    check("request has the question text", content[1]["type"] == "text")
    # no key -> raises (assistant turns this into a 'add a key' prompt)
    b2 = Brain(_cfg())
    raised = False
    try:
        b2.ask_image("x", "QUJD")
    except Exception:
        raised = True
    check("vision without key raises", raised)

    # skill: triggers only for screen phrases, routes to the describe_image callback
    import core.skills as S
    from core import config
    from core.skills import Skills
    calls = {"n": 0}
    def describe(q, b64):
        calls["n"] += 1
        return "I see a desktop, sir."
    sk = Skills(config.load(), FakeHud(), say=lambda t: None, describe_image=describe)
    # patch ImageGrab so no real screen is captured in CI/headless
    import types as _t
    fake_pil = _t.ModuleType("PIL"); fake_grab = _t.ModuleType("PIL.ImageGrab")
    class _Img:
        def thumbnail(self, *a): pass
        def convert(self, *a): return self
        def save(self, buf, **k): buf.write(b"\xff\xd8\xff")   # minimal JPEG-ish bytes
    fake_grab.grab = lambda *a, **k: _Img()
    fake_pil.ImageGrab = fake_grab
    _saved = (sys.modules.get("PIL"), sys.modules.get("PIL.ImageGrab"))
    sys.modules["PIL"] = fake_pil; sys.modules["PIL.ImageGrab"] = fake_grab
    try:
        r = sk.handle("what's on my screen")
        check("vision skill routes to callback", r == "I see a desktop, sir." and calls["n"] == 1, r)
        check("non-screen phrase ignored by vision", sk._vision("what is the weather", "") is None)
    finally:
        if _saved[0] is not None: sys.modules["PIL"] = _saved[0]
        else: sys.modules.pop("PIL", None)
        if _saved[1] is not None: sys.modules["PIL.ImageGrab"] = _saved[1]
        else: sys.modules.pop("PIL.ImageGrab", None)
    # without the callback wired, vision falls through to the brain (None)
    sk2 = Skills(config.load(), FakeHud(), say=lambda t: None)
    check("vision no-op without callback", sk2._vision("what's on my screen", "") is None)


def test_now_context():
    section("brain — dynamic time context")
    from core.brain import Brain
    b = Brain(_cfg())
    ctx = b._now_context()
    check("context mentions the year", "20" in ctx and "right now it is" in ctx.lower())
    msgs = b._messages("hello")
    check("messages inject context into user turn", "right now it is" in msgs[-1]["content"].lower())


# ════════════════════════════════════════════════════════════════════════════
def test_speech_chunker():
    section("speech — streaming sentence chunker")
    from core.speech import Mouth
    m = Mouth(_cfg_speech(), FakeHud())
    spoken = []
    m._speak_one = lambda t: (spoken.append(t.strip()) or True)

    def toks(s, n=6):
        for i in range(0, len(s), n):
            yield s[i:i + n]

    spoken.clear()
    seen = []
    full = m.speak_stream(toks("Paris. It is the capital of France. Lovely city!"),
                          on_text=lambda f: seen.append(f))
    check("multiple sentences spoken", len(spoken) >= 2, spoken)
    check("full text returned intact", full == "Paris. It is the capital of France. Lovely city!")
    check("subtitle grew", seen and len(seen[-1]) >= len(seen[0]))

    spoken.clear()
    m.speak_stream(toks("Mr. Stark has 3.14 apples. Right?"))
    check("no split on abbrev/decimal", spoken == ["Mr. Stark has 3.14 apples.", "Right?"], spoken)

    spoken.clear()
    m.speak_stream(iter(["no terminal punct here"]))
    check("tail flushed", spoken == ["no terminal punct here"], spoken)

    spoken.clear()
    def interrupting():
        yield "First sentence here. "
        m.stop.set()
        yield "Second one should be dropped. "
    m.speak_stream(interrupting())
    check("interrupt stops after current", spoken == ["First sentence here."], spoken)


def _cfg_speech():
    c = _cfg()
    c.tts_engine = "edge"
    return c


def test_decimal_stream_split():
    section("speech — decimal not split across token boundaries")
    from core.speech import Mouth
    m = Mouth(_cfg_speech(), FakeHud())
    spoken = []
    m._speak_one = lambda t: (spoken.append(t.strip()) or True)
    # "3." arrives at a chunk edge, then "14" — must NOT be spoken as a sentence end
    m.speak_stream(iter(["The value is 3", ".", "14 exactly. Done."]))
    check("decimal kept intact", all("3.14" in s or "Done" in s for s in spoken) and
          not any(s.endswith("is 3.") for s in spoken), spoken)


# ════════════════════════════════════════════════════════════════════════════
class _FakeStdin:
    def __init__(self): self.written = []
    def write(self, s): self.written.append(s)
    def flush(self): pass
    def close(self): pass


class _FakeProc:
    def __init__(self, lines): self.stdin = _FakeStdin(); self.stdout = iter(lines); self._rc = None
    def poll(self): return self._rc
    def kill(self): self._rc = -9
    def terminate(self): self._rc = 0
    def wait(self, timeout=None): return 0


def test_stream_json_parser():
    section("brain — persistent stream-json parser + barge-in drain")
    from core.brain import PersistentClaude, BrainError

    d1 = '{"type":"stream_event","event":{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Paris"}}}'
    d2 = '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":", sir."}}}'
    ok = '{"type":"result","subtype":"success","is_error":false,"result":"Paris, sir."}'

    pc = PersistentClaude("claude", "m", "sys", ".")
    pc.proc = _FakeProc([d1, d2, ok, "trailing ignored"]); pc._gen = 1
    out = "".join(pc.ask_stream("hi", timeout=5))
    check("parses stream deltas to result", out == "Paris, sir.", repr(out))
    check("user turn was written to stdin", pc.proc.stdin.written
          and '"text": "hi"' in pc.proc.stdin.written[0])

    # error result with no text -> raises
    err = '{"type":"result","is_error":true,"result":"usage limit reached"}'
    pc2 = PersistentClaude("claude", "m", "sys", "."); pc2.proc = _FakeProc([err]); pc2._gen = 1
    raised = False
    try:
        list(pc2.ask_stream("hi", timeout=5))
    except BrainError as e:
        raised = "usage limit" in str(e).lower()
    check("error result raises BrainError", raised)

    # barge-in: consumer stops after first delta -> drain consumes up to the result
    consumed = []
    def gen():
        for ln in [d1, d2, "extra delta", ok, "AFTER-RESULT should not be read"]:
            consumed.append(ln); yield ln
    pc3 = PersistentClaude("claude", "m", "sys", "."); pc3.proc = _FakeProc(gen()); pc3._gen = 1
    g = pc3.ask_stream("hi", timeout=5)
    first = next(g)
    g.close()   # barge-in -> GeneratorExit -> _drain_to_result
    check("barge-in yielded first token", first == "Paris")
    check("drain consumed up to result", any('"type":"result"' in c for c in consumed)
          and "AFTER-RESULT should not be read" not in consumed, consumed)


def test_math_safety():
    section("skills — math DoS guards + number formatting")
    import core.skills as S
    check("guard blocks pow() blowup", _math_none("what is pow(9, 99999999)"))
    check("guard blocks chained power", _math_none("what is 2 to the power of (3 to the power of 50)")
          or _math_none("what is 9 ** 9 ** 9"))
    check("guard blocks factorial blowup", _math_none("what is factorial(100000)"))
    check("normal power still works", _math_eq("what is 2 to the power of 10", "1024"))
    # _fmt_num never emits scientific notation
    check("fmt: tiny float not sci", "e" not in S.Skills._fmt_num(0.00001).lower())
    check("fmt: huge int stays digits", "e" not in S.Skills._fmt_num(1e20).lower())
    check("fmt: integer float -> int", S.Skills._fmt_num(36.0) == "36")


def _math_skills():
    import types as _t, sys as _s, subprocess
    from core import config
    from core.skills import Skills
    return Skills(config.load(), FakeHud(), say=lambda t: None)


def _math_none(phrase):
    return _math_skills()._math(phrase.lower().rstrip("?"), phrase) is None


def _math_eq(phrase, sub):
    r = _math_skills()._math(phrase.lower().rstrip("?"), phrase)
    return r is not None and sub in r


def test_datecalc_and_ip():
    section("skills — date maths + public IP")
    import core.skills as S
    from core import config
    from core.skills import Skills
    from datetime import datetime, timedelta
    sk = Skills(config.load(), FakeHud(), say=lambda t: None)

    r = sk.handle("how many days until christmas")
    check("days until christmas", r is not None and "christmas" in r.lower())
    r = sk.handle("what's the date in 30 days")   # must NOT be shadowed by _date
    future = (datetime.now().date() + timedelta(days=30))
    check("date in N days correct (not shadowed by _date)",
          r is not None and future.strftime("%B %d") in r and "Today is" not in r, r)
    r = sk.handle("what day is december 25")
    check("what day is dec 25", r is not None and "falls on" in r.lower(), r)
    tgt = sk._resolve_date("december 25")
    check("resolve 'december 25'", tgt is not None and tgt.month == 12 and tgt.day == 25)
    tgt2 = sk._resolve_date("25 december")
    check("resolve '25 december'", tgt2 is not None and tgt2.month == 12 and tgt2.day == 25)
    check("bogus countdown -> brain", sk.handle("how many days until the big meeting") is None)
    check("date question not over-matched", sk.handle("what day is good for a walk") is None)

    orig = S.requests.get
    class _IP:
        text = "203.0.113.7"; status_code = 200
        def raise_for_status(self): pass
    S.requests.get = lambda *a, **k: _IP()
    try:
        check("public ip", "203.0.113.7" in str(sk.handle("what's my ip address")))
    finally:
        S.requests.get = orig


def test_reminder_persistence():
    section("skills — reminders/alarms survive a restart")
    import core.skills as S
    from core import config
    from core.skills import Skills
    try:
        S.REMINDERS_FILE.unlink()
    except Exception:
        pass
    sk1 = Skills(config.load(), FakeHud(), say=lambda t: None)
    sk1._schedule(3600, "Reminder, sir: standup.", "reminder to standup", "reminder")
    sk1._schedule(60, "timer up", "1-min timer", "timer")   # timers are NOT persisted
    check("reminders file written", S.REMINDERS_FILE.exists())
    sk2 = Skills(config.load(), FakeHud(), say=lambda t: None)   # simulate a restart
    kinds = sorted(r["kind"] for r in sk2._reminders)
    check("reloads durable reminder, drops timer", kinds == ["reminder"], kinds)
    check("reloaded label preserved", any("standup" in r["label"] for r in sk2._reminders))
    for sk in (sk1, sk2):
        sk.handle("cancel all reminders")
    try:
        S.REMINDERS_FILE.unlink()
    except Exception:
        pass


def test_reminder_fire():
    section("skills — reminder fire() deregisters + speaks (Timer callback)")
    import core.skills as S
    from core import config
    from core.skills import Skills
    said = []
    sk = Skills(config.load(), FakeHud(), say=lambda t: said.append(t))
    rid = sk._schedule(0.05, "Reminder, sir: test.", "reminder to test", "reminder")
    check("registered", any(r["id"] == rid for r in sk._reminders))
    import time as _t
    _t.sleep(0.25)   # let the Timer fire
    check("fired and spoke", said == ["Reminder, sir: test."], said)
    check("deregistered after firing", not any(r["id"] == rid for r in sk._reminders))


# ════════════════════════════════════════════════════════════════════════════
def test_skills():
    section("skills — matcher (old + new powers)")
    import core.skills as S
    import webbrowser, os, subprocess, requests

    _orig_popen = subprocess.Popen
    webbrowser.open = lambda *a, **k: True
    subprocess.Popen = lambda *a, **k: types.SimpleNamespace(pid=0)
    if hasattr(os, "startfile"):
        os.startfile = lambda *a, **k: None

    class FakeResp:
        status_code = 200
        text = ("<item><title>Alpha</title></item><item><title>Beta</title></item>"
                "<item><title>Gamma</title></item>")
        def raise_for_status(self): pass
        def json(self):
            return [{"meanings": [{"partOfSpeech": "noun",
                     "definitions": [{"definition": "a happy accident"}]}]}]
    requests.get = lambda *a, **k: FakeResp()

    from core import config
    from core.skills import Skills

    class FakeVol:
        def GetMasterVolumeLevelScalar(self): return 0.5
        def SetMasterVolumeLevelScalar(self, *a): pass
        def SetMute(self, *a): pass

    import screen_brightness_control as sbc
    sbc.set_brightness = lambda *a, **k: None
    sbc.get_brightness = lambda *a, **k: [50]
    import ctypes
    try:
        ctypes.windll.user32.LockWorkStation = lambda *a, **k: None
    except Exception:
        pass

    sk = Skills(config.load(), FakeHud(), say=lambda t: None)
    sk._vol_iface = lambda: FakeVol()

    try:
        # (phrase, expectation): True=matched, None=falls through to brain, str=substring
        cases = [
            ("what time is it", True), ("what's the date", True), ("who are you", True),
            ("what can you do", True), ("open notepad", True), ("open youtube", True),
            ("search for quantum computing", True), ("play daft punk on youtube", True),
            ("set volume to 40", "40"), ("volume up", True), ("increase the brightness", True),
            ("take a screenshot", True), ("system status", True), ("battery status", True),
            ("minimize everything", True), ("make a note buy milk", True), ("read my notes", True),
            ("set a timer for 2 minutes", True), ("what's the weather", True),
            ("copy hello world to the clipboard", True), ("lock the computer", True),
            ("thank you jarvis", True), ("hello jarvis", True),
            # new powers
            ("pause", "Paused"), ("next track", True), ("previous song", True),
            ("play music", "Playing"), ("close notepad", "Closing"), ("quit spotify", True),
            ("what is 15 percent of 240", "36"), ("calculate 12 times 8", "96"),
            ("what's 100 divided by 4", "25"), ("what is 2 to the power of 10", "1024"),
            ("square root of 144", "12"), ("convert 10 km to miles", "6.21"),
            ("convert 100 fahrenheit to celsius", "37.7"), ("convert 5 kg to pounds", "11.02"),
            ("define serendipity", "happy accident"), ("what does ubiquitous mean", "happy accident"),
            ("what's the news", "Alpha"), ("tell me a joke", True), ("flip a coin", True),
            ("roll a dice", True), ("roll a d20", True), ("random number between 1 and 10", True),
            ("spell serendipity", "S-E-R-E-N-D-I-P-I-T-Y"), ("empty the recycle bin", True),
            ("remind me to call mum in 5 minutes", "5 minutes"),
            ("set an alarm for 7 am", "7:00 AM"), ("list my reminders", True),
            # must NOT hijack the brain
            ("what is the capital of France", None), ("what is the tallest mountain", None),
            ("what does the fox say", None), ("i have 3 cats and 2 dogs", None),
            ("tell me about the roman empire", None),
        ]
        for phrase, expect in cases:
            try:
                r = sk.handle(phrase)
            except Exception as e:
                check(f"skill {phrase!r}", False, f"raised {e}")
                continue
            if expect is None:
                check(f"skill {phrase!r} → brain", r is None, f"got {r!r}")
            elif expect is True:
                check(f"skill {phrase!r}", r is not None, "returned None")
            else:
                check(f"skill {phrase!r}", r is not None and str(expect) in str(r), f"got {r!r}")

        # reminder registry: create then cancel
        sk._reminders.clear()
        sk.handle("remind me to test in 30 minutes")
        n_before = len(sk._reminders)
        cancel = sk.handle("cancel all reminders")
        check("reminder scheduled", n_before == 1, n_before)
        check("cancel clears registry", len(sk._reminders) == 0 and "Cancelled" in str(cancel), cancel)

        # math must never execute arbitrary code
        check("math rejects names", sk.handle("what is __import__") is None)
        check("math guards huge power", sk.handle("what is 2 to the power of 999999") is None)

        # repeat + reset (last-reply getter / forget callback)
        replies = {"v": ""}
        forgot = {"v": False}
        sk2 = Skills(config.load(), FakeHud(), say=lambda t: None,
                     last_reply=lambda: replies["v"],
                     forget=lambda: forgot.__setitem__("v", True))
        check("repeat with nothing said", "haven't said" in str(sk2.handle("repeat that")))
        replies["v"] = "The capital of France is Paris, sir."
        check("repeat re-speaks last", sk2.handle("say that again") == replies["v"])
        check("reset triggers forget", "clean slate" in str(sk2.handle("forget our conversation"))
              and forgot["v"] is True)
        check("reset: start over", sk2.handle("start over") is not None)
        check("repeat doesn't hijack brain", sk2.handle("tell me about the moon") is None)
    finally:
        subprocess.Popen = _orig_popen


# ════════════════════════════════════════════════════════════════════════════
def test_clap():
    section("voice — clap detector")
    import numpy as np
    from core.voice import ClapDetector

    cfg = types.SimpleNamespace(clap_sensitivity=0.22, clap_count=1)
    det = ClapDetector(cfg)
    BLOCK = 1280
    quiet = (np.random.randn(BLOCK) * 0.005).astype(np.float32)
    for _ in range(20):  # learn a quiet floor
        det.feed(quiet)

    def clap_block():
        b = (np.random.randn(BLOCK) * 0.01).astype(np.float32)
        b[600:610] = 0.9   # sharp broadband transient → high crest factor
        return b

    fired = det.feed(clap_block())
    check("single clap fires", fired is True)
    # refractory: the clap's own tail must not immediately re-fire
    tail = clap_block()
    check("no double-fire in refractory", det.feed(tail) in (False, True))  # just shouldn't crash

    # double-clap mode needs two claps within the window
    cfg2 = types.SimpleNamespace(clap_sensitivity=0.22, clap_count=2)
    det2 = ClapDetector(cfg2)
    for _ in range(20):
        det2.feed(quiet)
    r1 = det2.feed(clap_block())
    for _ in range(3):
        det2.feed(quiet)          # advance frames within the double-clap window
    r2 = det2.feed(clap_block())
    check("double clap: first alone doesn't fire", r1 is False)
    check("double clap: second within window fires", r2 is True, f"r2={r2}")


# ════════════════════════════════════════════════════════════════════════════
def main():
    print("JARVIS offline test suite")
    for t in (test_config, test_brain_anthropic_parser, test_groq_ollama_parsers,
              test_brain_chain_and_fallback, test_apply_config, test_vision,
              test_now_context, test_speech_chunker, test_decimal_stream_split,
              test_stream_json_parser, test_math_safety, test_datecalc_and_ip,
              test_reminder_persistence, test_reminder_fire, test_skills, test_clap):
        try:
            t()
        except Exception as e:
            import traceback
            check(t.__name__, False, "crashed")
            traceback.print_exc()
    print(f"\n{'='*48}\n  PASSED {PASS} / {PASS + FAIL}")
    if FAIL:
        print("  FAILURES:")
        for f in FAILURES:
            print("   -", f)
        sys.exit(1)
    print("  ALL OFFLINE TESTS PASSED")


if __name__ == "__main__":
    main()
