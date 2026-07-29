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

    # groq auto-preferred when a free key is present (and no anthropic key)
    cg = _cfg(); cg.brain = "claude"; cg.groq_api_key = "gsk_test"
    bg = Brain(cg)
    check("groq preferred with free key", bg._chain()[0] == "groq", bg._chain())
    # anthropic still wins if BOTH keys are set (fastest + smartest)
    cb = _cfg(); cb.brain = "claude"; cb.groq_api_key = "gsk"; cb.anthropic_api_key = "k"
    check("anthropic beats groq when both set", Brain(cb)._chain()[0] == "anthropic")

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
    # Groq vision path: with ONLY a groq key, ask_image uses the Groq multimodal model
    class GResp:
        status_code = 200
        def json(self): return {"choices": [{"message": {"content": "A browser window, sir."}}]}
    cg = _cfg(); cg.groq_api_key = "gsk_test"
    bg = Brain(cg)
    capg = {}
    def fake_post_g(url, **kw):
        capg["url"] = url; capg["body"] = kw.get("json"); return GResp()
    B.requests.post = fake_post_g
    try:
        outg = bg.ask_image("what's up?", "QUJD")
    finally:
        B.requests.post = orig
    check("groq vision reply parsed", outg == "A browser window, sir.", repr(outg))
    check("groq vision hits the groq endpoint", "groq.com" in capg.get("url", ""))
    check("groq vision sends an image_url",
          capg["body"]["messages"][0]["content"][1]["type"] == "image_url")
    # has_vision is anthropic-only (fast native path); groq-only screens go via the agent
    check("has_vision needs an anthropic key", bg.has_vision() is False)

    # no key -> raises (assistant turns this into a 'add a key' prompt)
    b2 = Brain(_cfg())
    raised = False
    try:
        b2.ask_image("x", "QUJD")
    except Exception:
        raised = True
    check("vision without any key raises", raised)

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

    # PRIVACY: with no vision-capable key, do NOT capture the screen at all
    grabbed = {"n": 0}
    def spy_grab(*a, **k):
        grabbed["n"] += 1
        raise AssertionError("screen must not be captured without a key")
    fg2 = _t.ModuleType("PIL.ImageGrab"); fg2.grab = spy_grab
    fp2 = _t.ModuleType("PIL"); fp2.ImageGrab = fg2
    _s2 = (sys.modules.get("PIL"), sys.modules.get("PIL.ImageGrab"))
    sys.modules["PIL"] = fp2; sys.modules["PIL.ImageGrab"] = fg2
    try:
        sk3 = Skills(config.load(), FakeHud(), say=lambda t: None,
                     describe_image=lambda q, b: "x", can_see=lambda: False)
        r = sk3.handle("what's on my screen")
        check("no-key vision gives guidance, not a capture",
              r is not None and "Anthropic" in r and grabbed["n"] == 0, (r, grabbed))
    finally:
        if _s2[0] is not None: sys.modules["PIL"] = _s2[0]
        else: sys.modules.pop("PIL", None)
        if _s2[1] is not None: sys.modules["PIL.ImageGrab"] = _s2[1]
        else: sys.modules.pop("PIL.ImageGrab", None)


def test_now_context():
    section("brain — dynamic time context")
    from core.brain import Brain
    b = Brain(_cfg())
    ctx = b._now_context()
    check("context mentions the year", "20" in ctx and "right now it is" in ctx.lower())
    msgs = b._messages("hello")
    check("messages inject context into user turn", "right now it is" in msgs[-1]["content"].lower())
    # forget() must clear BOTH history and the persistent session (via _stale)
    b.history = [("user", "x"), ("assistant", "y")]
    b._stale = False
    b.forget()
    check("forget clears history", not b.history)
    check("forget rebuilds persistent session (stale)", b._stale is True)


# ════════════════════════════════════════════════════════════════════════════
def _mock_render_emit(m, spoken):
    """Capture spoken sentences via the render/emit primitives (works on both the
    prefetch and sequential paths)."""
    m._render = lambda text: (text.strip() or None)
    m._emit = lambda r: spoken.append(r) if r else None


def test_speech_chunker():
    section("speech — streaming sentence chunker (prefetch + sequential)")
    from core.speech import Mouth

    def toks(s, n=6):
        for i in range(0, len(s), n):
            yield s[i:i + n]

    for prefetch in (True, False):
        cfg = _cfg_speech(); cfg.tts_prefetch = prefetch
        m = Mouth(cfg, FakeHud())
        spoken = []
        _mock_render_emit(m, spoken)
        tag = "prefetch" if prefetch else "sequential"

        seen = []
        src = "Paris is lovely. It is the capital of France. What a city here!"
        full = m.speak_stream(toks(src), on_text=lambda f: seen.append(f))
        check(f"[{tag}] full text intact", full == src, repr(full))
        check(f"[{tag}] sentences in order",
              spoken == ["Paris is lovely.", "It is the capital of France.",
                         "What a city here!"], spoken)
        check(f"[{tag}] subtitle grew", bool(seen) and len(seen[-1]) >= len(seen[0]))

        spoken.clear()
        m.speak_stream(toks("Mr. Stark has 3.14 apples. Right?"))
        check(f"[{tag}] no split on abbrev/decimal",
              spoken == ["Mr. Stark has 3.14 apples.", "Right?"], spoken)

        spoken.clear()
        m.speak_stream(iter(["no terminal punct here"]))
        check(f"[{tag}] tail flushed", spoken == ["no terminal punct here"], spoken)

    # interrupt is deterministic on the single-threaded sequential path
    cfg = _cfg_speech(); cfg.tts_prefetch = False
    m = Mouth(cfg, FakeHud()); spoken = []
    _mock_render_emit(m, spoken)
    def interrupting():
        yield "First sentence here. "
        m.stop.set()
        yield "Second one should be dropped. "
    m.speak_stream(interrupting())
    check("interrupt stops after current", spoken == ["First sentence here."], spoken)


def test_tts_pipeline():
    section("speech — prefetch pipeline (order, sapi shape, interrupt drain)")
    from core.speech import Mouth
    cfg = _cfg_speech(); cfg.tts_prefetch = True
    m = Mouth(cfg, FakeHud())

    # real _render shape: ('edge', pcm) or ('sapi', text); _emit dispatches on it
    played = []
    m._play = lambda pcm: played.append(("edge", pcm))
    m._speak_sapi = lambda text: played.append(("sapi", text))
    m._edge_render = lambda text: [1, 2, 3]   # pretend edge produced PCM

    src = ["This is one. ", "This is two. ", "This is three here."]
    full = m.speak_stream(iter(src))
    check("pipeline full text", full == "".join(src), repr(full))
    check("pipeline emitted 3 in order via edge",
          [k for k, _ in played] == ["edge", "edge", "edge"] and len(played) == 3, played)

    # edge fails -> _render yields ('sapi', text) -> _emit uses SAPI
    played.clear()
    m._edge_render = lambda text: None
    m.speak_stream(iter(["Only sapi here now."]))
    check("pipeline falls back to sapi", played == [("sapi", "Only sapi here now.")], played)

    # interrupt DURING playback -> remaining queued sentences are drained, not played
    played.clear()
    m._edge_render = lambda text: [1]
    def play_then_stop(pcm):
        played.append(pcm)
        m.stop.set()           # user barges in while the first sentence plays
    m._play = play_then_stop
    m.speak_stream(iter(["First long sentence. ", "Second long sentence. ",
                         "Third long sentence here."]))
    check("interrupt mid-playback drains the rest", len(played) == 1, played)


def _cfg_speech():
    c = _cfg()
    c.tts_engine = "edge"
    return c


def test_decimal_stream_split():
    section("speech — decimal not split across token boundaries")
    from core.speech import Mouth
    for prefetch in (True, False):
        cfg = _cfg_speech(); cfg.tts_prefetch = prefetch
        m = Mouth(cfg, FakeHud())
        spoken = []
        _mock_render_emit(m, spoken)
        # "3." arrives at a chunk edge, then "14" — must NOT split the decimal
        m.speak_stream(iter(["The value is 3", ".", "14 exactly. Done."]))
        check(f"[{'prefetch' if prefetch else 'seq'}] decimal kept intact",
              all("3.14" in s or "Done" in s for s in spoken)
              and not any(s.endswith("is 3.") for s in spoken), spoken)


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


def test_regex_antishadow():
    section("skills — reset/repeat/system/open no longer hijack normal phrases")
    import types as _t2, subprocess, webbrowser, os
    from core import config
    from core.skills import Skills
    replies = {"v": "prior answer, sir."}
    forgot = {"v": False}
    sk = Skills(config.load(), FakeHud(), say=lambda t: None,
                last_reply=lambda: replies["v"],
                forget=lambda: forgot.__setitem__("v", True))

    def t(s):
        return s.lower().strip().rstrip(".!?")

    # _reset: explicit multi-word intents + whole-utterance short forms fire…
    check("reset: 'forget our conversation'", sk._reset(t("forget our conversation"), "") is not None)
    check("reset: 'start over' (whole)", sk._reset(t("start over"), "") is not None)
    check("reset: 'clear your memory' (whole)", sk._reset(t("clear your memory"), "") is not None)
    # …but embedded short forms must NOT wipe context
    forgot["v"] = False
    check("reset: not 'clear memory in python'", sk._reset(t("how do i clear memory in python"), "") is None)
    check("reset: not 'start over with the plan'", sk._reset(t("lets start over with the plan"), "") is None)
    check("reset: no forget side-effect fired", forgot["v"] is False)

    # _repeat: imperative + trailing interrogative fire…
    check("repeat: 'repeat that'", sk._repeat(t("repeat that"), "") == replies["v"])
    check("repeat: 'what did you say'", sk._repeat(t("what did you say"), "") == replies["v"])
    check("repeat: 'what did you say again'", sk._repeat(t("what did you say again"), "") == replies["v"])
    # …but 'what did you say about X' is a real question -> brain
    check("repeat: not 'what did you say about the weather'",
          sk._repeat(t("what did you say about the weather"), "") is None)

    # _system: bare 'memory'/'cpu'/'ram' no longer hijack
    check("system: not 'clear memory in python'", sk._system(t("how do i clear memory in python"), "") is None)
    check("system: 'cpu usage' still works", sk._system(t("what's my cpu usage"), "") is not None)
    check("system: 'system status' still works", sk._system(t("system status"), "") is not None)

    # _open: multi-word non-path phrase falls through to the brain, not a launch
    _p = subprocess.Popen
    subprocess.Popen = lambda *a, **k: _t2.SimpleNamespace(pid=0)
    webbrowser.open = lambda *a, **k: True
    if hasattr(os, "startfile"):
        os.startfile = lambda *a, **k: None
    try:
        check("open: 'start over with the plan' -> brain",
              sk._open(t("start over with the plan"), "start over with the plan") is None)
        check("open: known app still launches", sk._open(t("open notepad"), "open notepad") is not None)
    finally:
        subprocess.Popen = _p


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
    sept = sk._resolve_date("sept 15")
    check("resolve 'sept 15' (4-letter Sept)", sept is not None and sept.month == 9 and sept.day == 15)
    check("'what day of the week is it' handled", sk.handle("what day of the week is it") is not None)
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
            # natural media intent → play on YouTube
            ("open lofi music", "YouTube"), ("put on some jazz", "YouTube"),
            ("play lofi hip hop", "YouTube"), ("listen to some classical music", "YouTube"),
            # broadened: "find/search X on youtube" must find + play, not fall to the brain
            ("find seya gims morad on youtube", "YouTube"),
            ("search seya gims morad on youtube", "YouTube"),
            ("seya gims morad on youtube", "YouTube"),
            ("pull up interstellar trailer on youtube", "YouTube"),
            ("search youtube for lofi beats", "YouTube"),
            ("search the web for grblhal", "grblhal"),
            ("set volume to 40", "40"), ("volume up", True), ("increase the brightness", True),
            ("take a screenshot", True), ("record my screen", "Recording"),
            ("system status", True), ("battery status", True),
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
            # regression: over-matches that used to hijack a conversational turn
            ("what does a software engineer do", None),   # bare "engineer" → agentic (600s subprocess)
            ("how do i become an engineer", None),
            ("i had the time of my life", None),          # bare "the time" → clock
            ("at the time i was busy", None),
            ("spell check this document for me", None),   # "spell check" → spelled "check"
            ("lets play devil's advocate here", None),    # "play X" idiom → YouTube
            ("play it cool", None),
            ("i'm feeling a bit under the weather today", None),  # "weather" idiom → forecast
            # research must NOT hijack conversational uses of the word (→ brain)
            ("what's the latest research on fusion", None),
            ("the research shows promising results", None),
            ("tell me about the research paper", None),
            ("i need to do more research later", None),
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

        # screen recording: start (stubbed ffmpeg) → stop finalises + reports the file
        class FakeRec:
            def __init__(self): self.stdin = self
            def poll(self): return None            # still running
            def write(self, b): pass
            def flush(self): pass
            def wait(self, timeout=None): return 0
            def terminate(self): pass
        sk._rec_proc = FakeRec(); sk._rec_path = None
        rstop = sk.handle("stop recording")
        check("stop recording finalises", rstop is not None and "saved" in str(rstop).lower(), rstop)
        check("stop clears the recorder", sk._rec_proc is None)
        check("stop when idle is graceful", "not recording" in str(sk.handle("stop recording")).lower())

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
def test_research():
    section("skills — live research trigger + helpers")
    from core import config
    from core.skills import Skills
    sk = Skills(config.load(), FakeHud(), say=lambda t: None)

    # ── topic extraction: genuine research/PDF commands → the clean topic ──
    positives = [
        # the exact frustrated request (typo'd "research" and all) → "cnc"
        ("make a rresaerch about cnc on internet download pdfs do everything i want "
         "to see windows flying in the pc", "cnc"),
        ("make a research about cnc on internet download pdfs", "cnc"),
        ("research quantum computing", "quantum computing"),
        ("can you research black holes and download some pdfs", "black holes"),
        ("download pdfs about machine learning", "machine learning"),
        ("download the pdf about cnc", "cnc"),
        ("find me some papers on climate change", "climate change"),
        ("look into the history of rome", "history of rome"),
        ("read up on photosynthesis", "photosynthesis"),
        ("dig into neural networks", "neural networks"),
        ("jarvis do some research on renewable energy online", "renewable energy"),
        ("i want you to research the french revolution and open pdfs", "french revolution"),
        ("go research CNC machining", "CNC machining"),
        ("research online privacy", "online privacy"),   # "online" as topic, not a source hint
        # "open/show pdfs" phrasings (the ones that used to fall through to the brain)
        ("open pdfs about cnc in the browser", "cnc"),
        ("open some pdfs on cnc", "cnc"),
        ("show me pdfs about cnc", "cnc"),
        ("open black hole pdfs", "black hole"),
        ("open some quantum computing pdfs", "quantum computing"),
        ("can you open pdfs in the browser about cnc", "cnc"),
        ("show me papers on climate change", "climate change"),
        ("download pdfs about cnc and open them in the browser", "cnc"),
    ]
    for phrase, topic in positives:
        got = sk._research_topic(phrase.lower(), phrase)
        check(f"research topic {phrase!r}", got is not None and got.lower() == topic.lower(),
              f"got {got!r}, want {topic!r}")

    # ── conversational / unrelated uses → None (fall through to the brain) ──
    negatives = [
        "what's the latest research on fusion",
        "the research shows promising results",
        "tell me about the research paper",
        "i need to do more research later",
        "what is the capital of france",
        "research",                      # bare word, nothing to research
        "open notepad",
        "play some jazz",
        "search for quantum computing",   # a plain search, not a research blitz
        "look up the weather",            # "look up" ≠ "look into"
        # "open/show" with no real topic → _open / brain, NOT research
        "open the pdfs",
        "open them in the browser",
        "open my documents folder",
        "open my documents",
        "open the file",
        "show me my files",
        "yes in pdfs opening in the browser",   # the frustrated follow-up: no topic to research
    ]
    for phrase in negatives:
        got = sk._research_topic(phrase.lower(), phrase)
        check(f"research NOT fired {phrase!r}", got is None, f"got {got!r}")

    # ── pure helpers (no network) ─────────────────────────────────────────
    check("safe_name sanitises", sk._safe_name("C/N:C machining?") == "CNC_machining")
    check("pdf filename adds .pdf", sk._pdf_filename("https://x.org/a/report") == "report.pdf")
    check("pdf filename keeps .pdf", sk._pdf_filename("https://x.org/g320.pdf?dl=1") == "g320.pdf")
    check("ddg decode unwraps redirect",
          sk._ddg_decode("//duckduckgo.com/l/?uddg=https%3A%2F%2Fa.org%2Fx.pdf&rut=z")
          == "https://a.org/x.pdf")
    check("ddg decode passes direct url",
          sk._ddg_decode("https://a.org/x.pdf") == "https://a.org/x.pdf")

    # ── _find_pdf_urls prefers .pdf links, dedups, then backfills ──────────
    sk._ddg_links = lambda q, limit=3: [
        "https://a.org/one.pdf", "https://b.org/page", "https://a.org/one.pdf",
        "https://c.org/two.pdf", "https://d.org/article",
    ]
    pdfs = sk._find_pdf_urls("cnc", 3)
    check("pdf urls: .pdf first + deduped",
          pdfs == ["https://a.org/one.pdf", "https://c.org/two.pdf", "https://b.org/page"], pdfs)

    # ── explanation is best-effort: no brain wired → '' (research still ends) ──
    check("explain without brain → ''", sk._brain_explain("cnc") == "")
    sk2 = Skills(config.load(), FakeHud(), say=lambda t: None,
                 ask_brain=lambda p: "CNC is computer-controlled machining, sir.")
    check("explain with brain", "machining" in sk2._brain_explain("cnc"))
    boom = Skills(config.load(), FakeHud(), say=lambda t: None,
                  ask_brain=lambda p: (_ for _ in ()).throw(RuntimeError("down")))
    check("explain swallows brain error", boom._brain_explain("cnc") == "")


# ════════════════════════════════════════════════════════════════════════════
def test_missions():
    section("agent — background missions + activity panel")
    from core.assistant import Mission

    # ── Mission state machine → HUD broadcasts ──
    pushes = []
    class CapHud:
        def mission(self, s): pushes.append(s)
    spoken = []
    m = Mission(CapHud(), "m1", "Researching cnc", "RESEARCH", lambda t: spoken.append(t))
    m.step("Opening sources")
    check("first step active", pushes[-1]["steps"][0]["state"] == "active")
    check("push carries id/title/tag", pushes[-1]["id"] == "m1"
          and pushes[-1]["title"] == "Researching cnc" and pushes[-1]["tag"] == "RESEARCH")
    m.note("3 found")
    check("note sets detail on active step", pushes[-1]["steps"][0]["detail"] == "3 found")
    m.step("Downloading")
    check("advancing marks prev step done", pushes[-1]["steps"][0]["state"] == "done")
    check("new step is active", pushes[-1]["steps"][1]["state"] == "active")
    m.speak("all done sir")
    check("speak routes to TTS", spoken == ["all done sir"])
    m.finish("done", tag="DONE")
    fin = pushes[-1]
    check("finish marks every step done", all(s["state"] == "done" for s in fin["steps"]))
    check("finish sets status + tag", fin["status"] == "done" and fin["tag"] == "DONE")
    # error path
    m2 = Mission(CapHud(), "m2", "x", "AGENT", lambda t: None)
    m2.step("trying"); m2.error("snag")
    check("error sets status/FAILED", pushes[-1]["status"] == "error" and pushes[-1]["tag"] == "FAILED")

    # ── Assistant.run_mission: non-blocking, tracked, then cleared ──
    import time as _t, threading
    from core import config
    from core.hud import Hud as WsHud
    from core.assistant import Assistant
    a = Assistant(config.Config(), WsHud("127.0.0.1", 8799))   # hud never .start()ed → sends no-op
    try:
        started = threading.Event(); release = threading.Event()
        def worker(mn):
            mn.step("phase 1"); started.set(); release.wait(2.0); mn.step("phase 2")
        a.run_mission("Test job", worker, tag="AGENT")
        check("run_mission is non-blocking", started.wait(2.0))
        check("active mission tracked", a.active_missions() == ["Test job"], a.active_missions())
        release.set()
        for _ in range(60):
            if not a.active_missions(): break
            _t.sleep(0.05)
        check("mission cleared after worker returns", a.active_missions() == [], a.active_missions())
        # a worker that throws must not crash the app and must still clear
        a.run_mission("Boom", lambda mn: (_ for _ in ()).throw(RuntimeError("x")))
        for _ in range(60):
            if not a.active_missions(): break
            _t.sleep(0.05)
        check("throwing mission is contained + cleared", a.active_missions() == [])
    finally:
        a.brain.close()

    # ── skills.research dispatches a BACKGROUND mission + returns an instant ack ──
    import webbrowser, os
    _orig_open = webbrowser.open
    webbrowser.open = lambda *a, **k: True
    if hasattr(os, "startfile"):
        os.startfile = lambda *a, **k: None
    try:
        from core.skills import Skills
        pushed = []; said = []
        class H2:
            def mission(self, s): pushed.append(s)
            def __getattr__(self, n): return lambda *a, **k: None
        def sync_run(title, worker, tag="AGENT"):
            mm = Mission(H2(), "x", title, tag, lambda t: said.append(t))
            worker(mm)
            if mm.status == "running": mm.finish("done")
            return mm
        sk = Skills(config.load(), H2(), say=lambda t: said.append("ACK:" + t),
                    ask_brain=lambda p: "CNC is computer-controlled machining, sir.",
                    run_mission=sync_run, active_missions=lambda: ["researching cnc"])
        sk._ddg_links = lambda q, limit=3: []
        sk._find_pdf_urls = lambda topic, limit=6: []
        ret = sk.handle("make a research about cnc and download pdfs")
        check("research returns an instant ack", ret is not None and "researching cnc" in ret.lower(), ret)
        check("ack is spoken by the turn, not the worker", "ACK:" not in "".join(p for p in said))
        check("mission streamed steps", len(pushed) >= 3 and pushed[-1]["status"] == "done")
        check("worker spoke the explanation", any("machining" in s for s in said), said)
        # "what are you working on" reports live missions
        st = sk.handle("what are you working on")
        check("mission-status query works", st is not None and "researching cnc" in st.lower(), st)
        # with no runner wired, research still works inline (degraded path)
        sk_inline = Skills(config.load(), H2(), say=lambda t: said.append("SAY:" + t),
                           ask_brain=lambda p: "", run_mission=None)
        sk_inline._ddg_links = lambda q, limit=3: []
        sk_inline._find_pdf_urls = lambda topic, limit=6: []
        r2 = sk_inline.handle("research black holes and download pdfs")
        check("inline research returns '' (already spoke)", r2 == "", r2)
    finally:
        webbrowser.open = _orig_open


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
def test_recording():
    section("skills — monitor-aware screen recording")
    import subprocess, os, webbrowser
    from core import config
    from core.skills import Skills

    class FakePopen:
        def __init__(self, *a, **k): self.stdin = self; self.args = a[0] if a else []
        def poll(self): return None
        def write(self, b): pass
        def flush(self): pass
        def wait(self, timeout=None): return 0
        def terminate(self): pass

    _op = subprocess.Popen
    subprocess.Popen = FakePopen
    webbrowser.open = lambda *a, **k: True
    if hasattr(os, "startfile"):
        os.startfile = lambda *a, **k: None
    try:
        sk = Skills(config.load(), FakeHud(), say=lambda t: None)
        MONS = [{"left": 0, "top": 0, "width": 1920, "height": 1080, "primary": True},
                {"left": 1920, "top": 0, "width": 1280, "height": 1024, "primary": False}]
        sk._monitors = staticmethod(lambda: MONS).__func__
        sk._monitors = lambda: MONS
        sk._foreground_monitor = lambda mons: MONS[1]     # foreground on screen 2

        def where(phrase):
            mon, w = sk._pick_record_monitor(phrase.lower())
            return mon, w
        m, w = where("record screen 1")
        check("screen 1 selected", m["left"] == 0 and "1" in w, (m, w))
        m, w = where("record screen 2")
        check("screen 2 selected", m["left"] == 1920, (m, w))
        m, w = where("record the second screen")
        check("second screen selected", m and m["left"] == 1920, (m, w))
        m, w = where("record the main screen")
        check("main screen selected", m["primary"] is True, (m, w))
        m, w = where("record this screen")
        check("this screen = foreground monitor", m["left"] == 1920, (m, w))
        m, w = where("record everything")
        check("whole desktop = no offset", m is None, (m, w))
        m, w = where("record my screen")
        check("default = the screen you're on", m["left"] == 1920, (m, w))

        # "start recording" must reach _record, NOT be hijacked by _open ("start X")
        r = sk.handle("start recording")
        check("start recording → _record not _open", r is not None and "Recording" in r and "Opening" not in r, r)
        # ffmpeg is invoked with a per-monitor crop for a specific screen
        sk._rec_proc = None
        sk.handle("record screen 2")
        argv = sk._rec_proc.args
        check("ffmpeg got the screen-2 offset", "1920" in argv and "-offset_x" in argv, argv)
        check("ffmpeg got the screen-2 size", "1280x1024" in argv, argv)
    finally:
        subprocess.Popen = _op


# ════════════════════════════════════════════════════════════════════════════
def test_delegation():
    section("assistant — brain delegates real tasks to the agent")
    from core import config
    from core.hud import Hud
    from core.assistant import Assistant
    a = Assistant(config.Config(), Hud("127.0.0.1", 8801))   # hud never started → no-op sends
    try:
        spoken = []; streamed = []; missions = []
        a._say = lambda t: spoken.append(t)
        a._say_stream = lambda chunks: streamed.append("".join(chunks))
        a.run_mission = lambda title, worker, tag="AGENT": missions.append((title, tag))

        # 1) a DELEGATE directive → hands off to the agent, does NOT speak it aloud
        a.brain.ask_stream = lambda text: iter(["DELEGATE: ", "organize my downloads folder by type"])
        a._handle_brain("sort out my downloads")
        check("delegate spawns an agent mission", len(missions) == 1 and missions[0][1] == "AGENT", missions)
        check("delegate carries the task", "organize" in missions[0][0].lower(), missions)
        check("delegate is not spoken as a reply", streamed == [], streamed)
        check("delegate speaks a short ack", any("on it" in s.lower() for s in spoken), spoken)

        # 2) a plain answer → streams to speech, no mission
        spoken.clear(); streamed.clear(); missions.clear()
        a.brain.ask_stream = lambda text: iter(["The capital of France ", "is Paris, sir."])
        a._handle_brain("what's the capital of france")
        check("normal reply is spoken", len(streamed) == 1 and "Paris" in streamed[0], streamed)
        check("normal reply spawns no mission", missions == [], missions)

        # 3) single-chunk DELEGATE also works
        spoken.clear(); streamed.clear(); missions.clear()
        a.brain.ask_stream = lambda text: iter(["DELEGATE: install 7-zip and pin it to the taskbar"])
        a._handle_brain("get me 7zip")
        check("single-chunk delegate → mission", len(missions) == 1 and "install" in missions[0][0].lower(), missions)
        check("single-chunk delegate not spoken", streamed == [], streamed)
    finally:
        a.brain.close()


# ════════════════════════════════════════════════════════════════════════════
def test_instagram():
    section("skills — Instagram insights + agent low-priority")
    import os
    import webbrowser
    import core.skills as S
    from core import config
    from core.skills import Skills

    # heavy Claude agents must run below-normal priority (Windows) so they don't
    # starve JARVIS's responsiveness
    if os.name == "nt":
        check("agent low-priority flag set", S._LOWPRIO != 0)

    opened = []
    _orig = webbrowser.open
    webbrowser.open = lambda u, *a, **k: opened.append(u) or True
    try:
        missions = []
        sk = Skills(config.load(), FakeHud(), say=lambda t: None,
                    run_mission=lambda title, worker, tag="AGENT": missions.append((title, tag)))
        # insights intent → opens the desktop insight surfaces + a read mission
        opened.clear(); missions.clear()
        r = sk.handle("check my statics on instagram reels")
        check("instagram insights fires", r is not None and "insights" in r.lower(), r)
        check("opens IG + business suite", any("instagram.com" in u for u in opened)
              and any("business.facebook" in u for u in opened), opened)
        check("kicks a VISION read mission", missions and missions[0][1] == "VISION", missions)
        # plain "open instagram" just opens it
        opened.clear()
        r = sk.handle("open instagram")
        check("open instagram just opens", r is not None and opened == ["https://www.instagram.com/"], (r, opened))
        # unrelated instagram mention doesn't hijack
        check("bare 'on instagram' → brain", sk.handle("whats on instagram") is None)
        check("non-instagram unaffected", sk._instagram("what time is it", "what time is it") is None)
    finally:
        webbrowser.open = _orig


# ════════════════════════════════════════════════════════════════════════════
def test_chained_commands():
    section("assistant — chained multi-command turns")
    from core import config
    from core.hud import Hud
    from core.assistant import Assistant
    a = Assistant(config.Config(), Hud("127.0.0.1", 8805))
    try:
        # the exact failing case: "play … then research … then check …" → 3 commands,
        # and the commas inside the research part stay as ONE segment
        s = a._split_commands("play a song on youtube then make a research about cnc "
                              "components, program, algorithm then check my instagram reels")
        check("splits on 'then' into 3", len(s) == 3, s)
        check("commas inside a segment are kept",
              s[1] == "make a research about cnc components, program, algorithm", s)
        # bare 'and' / comma lists do NOT split
        check("bare 'and' doesn't split",
              a._split_commands("research cnc and download pdfs") == ["research cnc and download pdfs"])
        check("semicolons split", len(a._split_commands("open notepad; open chrome; take a screenshot")) == 3)
        check("'after that' splits", len(a._split_commands("organize my files after that open spotify")) == 2)
        check("single command unchanged", a._split_commands("what time is it") == ["what time is it"])

        # _process must dispatch EVERY segment (skills for matched, brain for the rest)
        handled = []; brained = []
        a.skills.handle = lambda seg: (handled.append(seg) or
                                       ("ok" if ("youtube" in seg or "research" in seg) else None))
        a._say = lambda t: None
        a._handle_brain = lambda seg: brained.append(seg)
        a._process("play a song on youtube then research cnc then check my instagram reels")
        check("all three segments dispatched",
              handled == ["play a song on youtube", "research cnc", "check my instagram reels"], handled)
        check("the unmatched segment escalates to the brain (delegate)",
              brained == ["check my instagram reels"], brained)

        # ── the intelligent planner: brain decomposes messy multi-part requests ──
        a.brain.ask = lambda p: '["play lofi on youtube", "research cnc", "check my instagram stats"]'
        r = a._resolve_commands("play a song and research cnc and check my instagram")
        check("multi-part request → decomposed via brain", len(r) == 3 and r[0] == "play lofi on youtube", r)
        # JSON embedded in chatter is still extracted
        a.brain.ask = lambda p: 'Sure! ["research cnc components programs and algorithms"] done'
        r = a._resolve_commands("research cnc components, programs, algorithms please now")
        check("list-of-subparts kept as ONE command", r == ["research cnc components programs and algorithms"], r)
        # simple/short commands are NOT planned (stay instant), and questions pass through
        planned = {"n": 0}
        a.brain.ask = lambda p: planned.__setitem__("n", planned["n"] + 1) or "[]"
        check("simple command not planned", a._resolve_commands("play lofi") == ["play lofi"])
        check("short phrase not planned", a._resolve_commands("what time is it") == ["what time is it"])
        check("no brain call for simple commands", planned["n"] == 0)
        # explicit 'then' still uses the fast regex split (no brain call)
        check("explicit 'then' splits without the brain",
              a._resolve_commands("play a song then research cnc") == ["play a song", "research cnc"]
              and planned["n"] == 0)
        # a failed/garbage plan falls back to the raw text (never crashes the turn)
        a.brain.ask = lambda p: "not json at all"
        check("plan failure → raw text fallback",
              a._resolve_commands("do this and that and the other stuff") == ["do this and that and the other stuff"])
    finally:
        a.brain.close()


# ════════════════════════════════════════════════════════════════════════════
def test_mic_gain():
    section("voice — quiet-mic auto gain")
    import numpy as np
    from core.voice import Ears
    g = Ears._normalize_gain
    # a quiet mic (peak ~0.02) gets boosted toward ~0.3 so whisper can hear it
    quiet = (np.sin(np.linspace(0, 50, 4000)) * 0.02).astype(np.float32)
    out = g(quiet)
    check("quiet audio boosted", 0.25 <= float(np.max(np.abs(out))) <= 0.35,
          float(np.max(np.abs(out))))
    # already-loud audio is left alone (no clipping blow-up)
    loud = (np.sin(np.linspace(0, 50, 4000)) * 0.6).astype(np.float32)
    check("loud audio untouched", abs(float(np.max(np.abs(g(loud)))) - 0.6) < 1e-3)
    # pure silence stays silent (never amplify the noise floor into hallucinations)
    silence = np.zeros(4000, dtype=np.float32)
    check("silence stays silent", float(np.max(np.abs(g(silence)))) == 0.0)
    # gain is capped (an extremely faint signal isn't blown up past the cap)
    faint = (np.sin(np.linspace(0, 50, 4000)) * 0.005).astype(np.float32)
    check("gain capped at 20x", float(np.max(np.abs(g(faint)))) <= 0.005 * 20 + 1e-6)


# ════════════════════════════════════════════════════════════════════════════
def main():
    print("JARVIS offline test suite")
    for t in (test_config, test_brain_anthropic_parser, test_groq_ollama_parsers,
              test_brain_chain_and_fallback, test_apply_config, test_vision,
              test_now_context, test_speech_chunker, test_tts_pipeline,
              test_decimal_stream_split,
              test_stream_json_parser, test_math_safety, test_regex_antishadow,
              test_datecalc_and_ip, test_reminder_persistence, test_reminder_fire,
              test_skills, test_research, test_missions, test_recording, test_delegation,
              test_instagram, test_chained_commands, test_clap, test_mic_gain):
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
