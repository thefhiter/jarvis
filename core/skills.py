"""Powers — deterministic control over the PC.

Every command is matched here FIRST (fast, no LLM round-trip). Anything that
doesn't match returns None and is escalated to the brain for a conversational
answer. Handlers return the sentence JARVIS should say back.
"""
from __future__ import annotations

import ast
import json
import math
import operator
import os
import random
import re
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "notes.txt"
REMINDERS_FILE = ROOT / "reminders.json"   # reminders/alarms survive a restart

# name -> launcher.  urls open in the browser, others are Windows commands.
APPS = {
    "notepad": "notepad", "calculator": "calc", "calc": "calc",
    "paint": "mspaint", "command prompt": "cmd", "terminal": "wt",
    "explorer": "explorer", "file explorer": "explorer", "task manager": "taskmgr",
    "control panel": "control", "settings": "start ms-settings:",
    "camera": "start microsoft.windows.camera:", "photos": "start ms-photos:",
    "chrome": "chrome", "edge": "msedge", "firefox": "firefox",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
    "vs code": "code", "vscode": "code", "code": "code", "spotify": "spotify",
    "steam": "steam", "discord": "discord",
}
SITES = {
    "youtube": "https://youtube.com", "google": "https://google.com",
    "gmail": "https://mail.google.com", "github": "https://github.com",
    "chatgpt": "https://chat.openai.com", "claude": "https://claude.ai",
    "twitter": "https://twitter.com", "x": "https://x.com",
    "reddit": "https://reddit.com", "instagram": "https://instagram.com",
    "maps": "https://maps.google.com", "translate": "https://translate.google.com",
    "wikipedia": "https://wikipedia.org", "netflix": "https://netflix.com",
    "linkedin": "https://linkedin.com", "whatsapp": "https://web.whatsapp.com",
}

# name -> Windows image name, for "close/quit <app>" via taskkill. JARVIS itself and
# its interpreter are deliberately absent so it can never be told to close itself.
CLOSE_APPS = {
    "notepad": "notepad.exe", "calculator": "CalculatorApp.exe", "calc": "CalculatorApp.exe",
    "paint": "mspaint.exe", "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "edge": "msedge.exe", "firefox": "firefox.exe", "word": "winword.exe",
    "excel": "excel.exe", "powerpoint": "powerpnt.exe", "spotify": "Spotify.exe",
    "steam": "steam.exe", "discord": "Discord.exe", "vs code": "Code.exe",
    "vscode": "Code.exe", "code": "Code.exe", "explorer": "explorer.exe",
    "file explorer": "explorer.exe", "task manager": "Taskmgr.exe",
}

# words that mark an "open X" as a media request → play it on YouTube
_MEDIA_HINTS = ("music", "song", "songs", "playlist", "mix", "radio", "soundtrack",
                "album", "track", "tracks", "tune", "tunes", "podcast", "lofi",
                "lo-fi", "beats", "livestream", "live stream", "audiobook", "anthem")

# "play X" idioms that are conversation, not media/YouTube requests
_PLAY_IDIOMS = {
    "devil's advocate", "devils advocate", "it cool", "it safe", "it by ear",
    "dumb", "along", "nice", "fair", "hard to get", "hardball", "the fool",
    "god", "dead", "possum", "house", "pretend", "favorites", "favourites",
}

# ── live research ("research X", "download pdfs about X") ─────────────────
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
# fuzzy spellings of "research" — voice transcription and quick typing mangle it
_RESEARCH = (r"(?:research|reserch|reasearch|researh|resarch|rechearch|"
             r"rresaerch|rersearch|rreserch|reaserch)")
RESEARCH_DIR = Path.home() / "Documents" / "Jarvis Research"
# run heavy Claude-agent subprocesses BELOW normal priority (+ no console window) so a
# working agent never starves JARVIS's own voice/UI responsiveness. Windows-only flags.
_LOWPRIO = 0
if os.name == "nt":
    _LOWPRIO = (getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x00004000)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
# a captured "topic" that reduces to one of these is noise, not a subject to research
_NOISE_TOPICS = {
    "the", "a", "an", "some", "my", "your", "our", "this", "that", "these", "those",
    "it", "them", "stuff", "thing", "things", "one", "ones", "here", "there",
    "pdf", "pdfs", "document", "documents", "doc", "docs", "file", "files", "folder",
    "article", "articles", "paper", "papers", "page", "pages", "browser", "internet",
}

JOKES = [
    "Why did the programmer quit his job? He didn't get arrays.",
    "I told my computer I needed a break, and now it won't stop sending me KitKat ads.",
    "There are only ten kinds of people in this world: those who understand binary and those who don't.",
    "Why do Java developers wear glasses? Because they don't C sharp.",
    "I would tell you a UDP joke, but you might not get it.",
    "Why was the function sad after a party? It had too many arguments.",
    "A SQL query walks into a bar, sidles up to two tables and asks: may I join you?",
    "I'd tell you a joke about the arc reactor, but it's a bit too energetic.",
    "Why did the developer go broke? Because he used up all his cache.",
    "To understand recursion, you must first understand recursion.",
]

# ── unit conversion tables (everything reduces to a base unit) ───────────
_LENGTH = {  # base: metre
    "mm": 0.001, "millimeter": 0.001, "millimetre": 0.001,
    "cm": 0.01, "centimeter": 0.01, "centimetre": 0.01,
    "m": 1.0, "meter": 1.0, "metre": 1.0,
    "km": 1000.0, "kilometer": 1000.0, "kilometre": 1000.0,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
    "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
    "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
}
_WEIGHT = {  # base: gram
    "mg": 0.001, "g": 1.0, "gram": 1.0, "grams": 1.0,
    "kg": 1000.0, "kilo": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "oz": 28.3495, "ounce": 28.3495, "ounces": 28.3495,
    "lb": 453.592, "lbs": 453.592, "pound": 453.592, "pounds": 453.592,
    "stone": 6350.29, "ton": 1_000_000.0, "tonne": 1_000_000.0,
}
_VOLUME = {  # base: millilitre
    "ml": 1.0, "milliliter": 1.0, "millilitre": 1.0,
    "l": 1000.0, "liter": 1000.0, "litre": 1000.0, "liters": 1000.0, "litres": 1000.0,
    "cup": 236.588, "cups": 236.588, "pint": 473.176, "pints": 473.176,
    "quart": 946.353, "gallon": 3785.41, "gallons": 3785.41,
    "tbsp": 14.7868, "tablespoon": 14.7868, "tsp": 4.92892, "teaspoon": 4.92892,
    "floz": 29.5735,
}
_UNIT_TABLES = {"length": _LENGTH, "weight": _WEIGHT, "volume": _VOLUME}
_TEMP_UNITS = {"c", "celsius", "centigrade", "f", "fahrenheit", "k", "kelvin"}

# (month, day) for "how many days until …"
_HOLIDAYS = {
    "christmas": (12, 25), "christmas eve": (12, 24), "new year": (1, 1),
    "new year's": (1, 1), "new years": (1, 1), "halloween": (10, 31),
    "valentine's day": (2, 14), "valentines day": (2, 14), "april fools": (4, 1),
}
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9   # September's common 4-letter abbreviation (vs the 3-letter "sep")

# safe arithmetic — only these node/operator types are ever evaluated
_MATH_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_MATH_FUNCS = {"sqrt": math.sqrt, "abs": abs, "round": round, "pow": pow,
               "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log,
               "floor": math.floor, "ceil": math.ceil, "factorial": math.factorial}


def _guard_pow(base, exp):
    """Reject exponentiations whose result would be enormous BEFORE computing them,
    so a typed 'pow(9, 99999999)' or '2 ** (3 ** 50)' can't hang the thread."""
    try:
        if exp > 300 or (base not in (0, 1, -1) and abs(exp) * math.log10(abs(base) + 1) > 300):
            raise ValueError("result too large")
    except (TypeError, ValueError) as e:
        raise ValueError(f"unsafe exponent: {e}")


def _safe_math(expr: str) -> float:
    """Evaluate a pure arithmetic expression safely (no names, no attributes) with
    magnitude guards so it can never hang or exhaust memory."""
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
                return node.value
            raise ValueError("non-numeric constant")
        if isinstance(node, ast.BinOp) and type(node.op) in _MATH_OPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow):
                _guard_pow(left, right)
            return _MATH_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _MATH_OPS:
            return _MATH_OPS[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in _MATH_FUNCS and not node.keywords:
            args = [ev(a) for a in node.args]
            name = node.func.id
            if name == "pow" and len(args) >= 2:
                _guard_pow(args[0], args[1])
            if name == "factorial" and (not args or args[0] > 90):
                raise ValueError("factorial too large")
            return _MATH_FUNCS[name](*args)
        raise ValueError("unsupported expression")
    return ev(ast.parse(expr, mode="eval"))


class _InlineMission:
    """A no-op Mission stand-in for when there's no background runner (unit tests
    or a degraded launch): step/note are silent, speak() goes straight to TTS."""

    def __init__(self, say):
        self.status = "running"
        self._say = say

    def step(self, label, detail=""): pass
    def note(self, detail): pass

    def speak(self, text):
        try:
            self._say(text)
        except Exception:
            pass

    def finish(self, status="done", tag=None): self.status = status
    def error(self, detail=""): self.status = "error"


class Skills:
    def __init__(self, cfg, hud, say, last_reply=None, forget=None,
                 describe_image=None, can_see=None, ask_brain=None,
                 run_mission=None, active_missions=None):
        self.cfg = cfg
        self.hud = hud
        self.say = say                # callable(text) -> speaks via the mouth
        self._last_reply = last_reply or (lambda: "")   # getter for JARVIS's last line
        self._forget = forget or (lambda: None)         # clears brain conversation memory
        self._describe_image = describe_image           # callable(question, b64)->str (vision)
        self._can_see = can_see or (lambda: True)        # is a vision-capable key configured?
        self._ask_brain = ask_brain                     # callable(prompt)->str, for research explanations
        self._run_mission = run_mission                 # callable(title, worker, tag)->Mission (background)
        self._active_missions = active_missions or (lambda: [])  # titles of running missions
        self.should_exit = False
        self._reminders: list[dict] = []   # active timers/reminders/alarms
        self._rid = 0
        self._rlock = threading.Lock()     # registry touched from Timer threads too
        self._rec_proc = None              # ffmpeg screen-recording process, if any
        self._rec_path = None
        self._agent_sem = threading.Semaphore(1)   # serialise heavy Claude agents (CPU)
        self._load_persisted()             # restore reminders/alarms from a prior run

    # ── main dispatch ───────────────────────────────────────────
    def handle(self, text: str):
        t = text.lower().strip().rstrip(".!?")
        if not t:
            return None
        for fn in (
            self._exit, self._help, self._identity, self._time,
            self._datecalc, self._date, self._repeat, self._reset, self._ipaddr,
            self._research,   # before generic open/search/youtube so it can claim the whole request
            self._record,     # before _open, else "start recording" is caught by "start X"
            self._instagram,  # before _open, so "open instagram insights" reads them
            self._media, self._open, self._close_app, self._search, self._youtube,
            self._volume, self._brightness, self._vision, self._screenshot, self._system,
            self._battery, self._window, self._type_text, self._clipboard,
            self._note, self._reminder, self._timer, self._weather, self._power,
            self._math, self._convert, self._define, self._news, self._fun,
            self._spell, self._recycle, self._agentic, self._pleasantries,
        ):
            try:
                out = fn(t, text)
                if out is not None:
                    return out
            except Exception as e:
                print(f"[skills] {fn.__name__} error: {e}")
        return None

    # ── conversation-ish quick wins ─────────────────────────────
    def _exit(self, t, _):
        if re.search(r"\b(goodbye jarvis|power (yourself )?down|shut (yourself )?down|go to sleep jarvis|that is all|dismissed|exit jarvis)\b", t):
            self.should_exit = True
            return f"Very good, {self.cfg.user_title}. Powering down. Call me when you need me."
        return None

    def _help(self, t, _):
        if re.search(r"\b(what can you do|help me|your capabilities|what are your powers)\b", t):
            return ("Quite a lot, {u}. I open apps and websites, control media, volume and "
                    "brightness, take screenshots, do maths and unit conversions, set timers, "
                    "reminders and alarms, take notes, define words, read the news and the "
                    "weather, tell the odd joke, close apps, lock or shut down the machine — "
                    "and I can research a topic live, opening the pages and downloading the "
                    "PDFs right in front of you. Beyond that, I'll happily just chat and "
                    "answer whatever else is on your mind."
                    .format(u=self.cfg.user_title))
        return None

    def _identity(self, t, _):
        if re.search(r"\b(who are you|what is your name|what's your name|introduce yourself)\b", t):
            return (f"I am JARVIS — your just-a-rather-very-intelligent-system. "
                    f"At your service, {self.cfg.user_title}.")
        return None

    def _repeat(self, t, _):
        # imperative forms match anywhere; interrogative forms ("what did you say")
        # must END the utterance so "what did you say about X" still reaches the brain.
        if re.search(r"\b(repeat that|say (that|it) again|can you repeat that|repeat what you said)\b"
                     r"|\b(what did you say|come again)( that| again| jarvis)*\s*$", t):
            last = (self._last_reply() or "").strip()
            if not last:
                return f"I haven't said anything yet, {self.cfg.user_title}."
            return last
        return None

    def _reset(self, t, _):
        # explicit multi-word intents can appear mid-sentence; the ambiguous short
        # forms ("start over", "clear memory") only count as a WHOLE utterance so we
        # never silently wipe context on "how do I clear memory in python".
        explicit = re.search(
            r"\b(forget (our|the|this) (conversation|chat|context)"
            r"|reset (our|the|your) (conversation|chat|context|memory)"
            r"|forget what we (talked|said|discussed))\b", t)
        short = re.fullmatch(r"(clear (your |our )?memory|start over|new conversation)( jarvis)?", t)
        if explicit or short:
            self._forget()
            return f"Done, {self.cfg.user_title}. I've cleared our conversation — a clean slate."
        return None

    def _time(self, t, _):
        if re.search(r"\b(what('?s| is) the time|what time is it|tell me the time"
                     r"|(?:have you |do you have )the time|got the time)\b", t):
            return f"It is {datetime.now().strftime('%-I:%M %p') if os.name != 'nt' else datetime.now().strftime('%I:%M %p').lstrip('0')}."
        return None

    def _date(self, t, _):
        if re.search(r"\b(what('?s| is) the date|what day (?:of the week )?is it|today's date)\b", t):
            return f"Today is {datetime.now().strftime('%A, %B %d, %Y')}."
        return None

    # ── date maths: countdowns, "date in N days", "what day is X" ─
    def _datecalc(self, t, _):
        u = self.cfg.user_title
        m = re.search(r"how many days\s+(?:until|till|til|to)\s+(.+)", t)
        if m:
            target = self._resolve_date(m.group(1))
            if target is None:
                return None
            days = (target - datetime.now().date()).days
            if days <= 0:
                return f"That's today, {u}!"
            return f"{days} day{'s' if days != 1 else ''} until {m.group(1).strip()}, {u}."
        m = (re.search(r"(?:what(?:'s| is) the date|what date)\s+(?:in|after)\s+(\d+)\s+days?", t)
             or re.search(r"(\d+)\s+days?\s+from\s+(?:now|today)", t))
        if m:
            d = datetime.now().date() + timedelta(days=int(m.group(1)))
            return f"That would be {d.strftime('%A, %B %d, %Y')}, {u}."
        m = re.search(r"what day (?:of the week )?is\s+(.+)", t)
        if m:
            target = self._resolve_date(m.group(1))
            if target is None:
                return None
            return f"{m.group(1).strip()} falls on a {target.strftime('%A')}, {u}."
        return None

    def _resolve_date(self, s: str):
        """Parse a holiday or a 'Month day' / 'day Month' into the next such date."""
        s = s.strip().lower().rstrip("?.! ")
        today = datetime.now().date()
        for name, (mo, da) in _HOLIDAYS.items():
            if name in s:
                d = datetime(today.year, mo, da).date()
                return d if d >= today else datetime(today.year + 1, mo, da).date()
        m = (re.search(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?", s)
             or re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)", s))
        if not m:
            return None
        a, b = m.group(1), m.group(2)
        month = _MONTHS.get(a) if a in _MONTHS else _MONTHS.get(b)
        day = int(b) if a in _MONTHS else (int(a) if a.isdigit() else None)
        if not month or not day or day > 31:
            return None
        try:
            d = datetime(today.year, month, day).date()
        except ValueError:
            return None
        return d if d >= today else datetime(today.year + 1, month, day).date()

    def _ipaddr(self, t, _):
        if not re.search(r"\b(what('?s| is) my ip|my ip address|my public ip)\b", t):
            return None
        try:
            r = requests.get("https://api.ipify.org", timeout=6)
            r.raise_for_status()
            return f"Your public IP address is {r.text.strip()}, {self.cfg.user_title}."
        except Exception:
            return "I couldn't reach the IP service just now."

    # ── live research: open pages, download & open PDFs, explain ─
    #
    # "research CNC and download some PDFs", "look into black holes", "download
    # pdfs about quantum computing" → JARVIS narrates, throws the reference /
    # video windows open on screen, pulls real PDFs off the web into your
    # Documents and opens each one, then explains the topic in its own voice.
    _PREFIX_RE = re.compile(
        r"^(?:jarvis[,\s]+|hey jarvis[,\s]+|ok jarvis[,\s]+|okay jarvis[,\s]+|please\s+|"
        r"kindly\s+|can you\s+|could you\s+|would you\s+|will you\s+|"
        r"go\s+(?:ahead\s+and\s+|and\s+)?|i want you to\s+|i'd like you to\s+|"
        r"i would like you to\s+|i need you to\s+|i want to\s+|i'd like to\s+|"
        r"let'?s\s+|now\s+|help me\s+)+", re.IGNORECASE)

    _TOPIC_CUT_RE = re.compile(
        r"\b(?:on the internet|on internet|in the internet|from the internet|off the internet|"
        r"on the web|on google|over the web|across the web|"
        r"do everything|i want|i wanna|i would like|i'd like|i really want|show me|for me|"
        r"so i|so that|because|right now|"
        r"for (?:your|the|my|our|a|an|his|her|their)\b|"
        r"to (?:make|help|write|build|understand|learn|study|prepare|do|see)\b|"
        r"and (?:download|open|show|find|get|save|explain|give|then|pull|read)\b|"
        r"then (?:download|open|show|find|get|save|explain|pull|read)\b|"
        r"download|please|explanation|and explain|open them|open the pdfs?|open pdfs?)\b",
        re.IGNORECASE)

    _TOPIC_BAD_LEAD = re.compile(
        r"^(?:shows?|showed|suggests?|suggested|found|finds?|indicates?|says?|said|proves?|"
        r"proved|confirms?|is|are|was|were|has|have|had|paper|papers|study|studies|"
        r"tells?|told|means?|about it|that|this|it)\b", re.IGNORECASE)

    def _research_topic(self, t, original):
        """Extract the topic from a genuine research/PDF command, or None.

        Fires on imperative research phrasings ("research X", "look into X"),
        explicit document fetches ("download pdfs about X"), and "research …"
        paired with an action signal — while leaving conversational uses
        ("what's the latest research on X", "the research shows …") to the brain.
        """
        original = (original or "").strip()
        core = self._PREFIX_RE.sub("", original).strip()
        raw = None

        _VERB = (r"find|get|download|grab|pull|fetch|save|gather|collect|"
                 r"open|show|bring up|pull up|look up|give")
        _DOC = r"pdfs?|articles?|papers?|documents?|research(?:\s+papers?)?"
        _QTY = r"(?:the\s+|a\s+|some\s+|a few\s+|a couple of\s+|me\s+)*"

        # (A) imperative research: "research X", "do research on X". FIRST, so
        #     "research black holes and download some pdfs" captures the real topic
        #     rather than the greedy topic-before-doc pattern grabbing "some".
        m = re.match(
            rf"(?:do|make|run|conduct|start|begin|carry out|perform)\s+"
            rf"(?:some\s+|a\s+|an\s+)?{_RESEARCH}"
            rf"(?:\s+(?:about|on|into|for|regarding|of|around))?\s+(.+)", core, re.IGNORECASE)
        if not m:
            m = re.match(
                rf"{_RESEARCH}(?:\s+(?:about|on|into|for|regarding|of|around))?\s+(.+)",
                core, re.IGNORECASE)
        if m:
            raw = m.group(1)
        # (B1) docs-then-topic: "download/open/show pdfs about X"
        if raw is None:
            m = re.search(
                rf"\b(?:{_VERB})\s+{_QTY}(?:{_DOC})\s+"
                rf"(?:about|on|regarding|for|related to|of|covering)\s+(.+)", original, re.IGNORECASE)
            if m:
                raw = m.group(1)
        # (C) "look into X" / "read up on X" / "dig into X" / "deep dive on X"
        if raw is None:
            m = re.match(
                r"(?:look into|read up on|dig into|deep dive (?:on|into)|"
                r"do a deep dive (?:on|into)|find out (?:all )?about)\s+(.+)", core, re.IGNORECASE)
            if m:
                raw = m.group(1)
        # (B2) topic-then-docs: "open some cnc pdfs" / "get X papers" (after A/B1/C
        #      so a research/"about X" phrasing wins over this greedier pattern)
        if raw is None:
            m = re.search(rf"\b(?:{_VERB})\s+{_QTY}(.+?)\s+(?:{_DOC})\b", original, re.IGNORECASE)
            if m:
                raw = m.group(1)
        # (B3) fetch verb + doc word both present but separated by filler
        #      ("open pdfs in the browser about cnc") → take the "about X" tail.
        if raw is None and re.search(rf"\b(?:{_VERB})\b", original, re.IGNORECASE) \
                and re.search(rf"\b(?:{_DOC})\b", original, re.IGNORECASE):
            m = re.search(r"\b(?:about|regarding|covering|on the subject of|on the topic of)\s+(.+)$",
                          original, re.IGNORECASE)
            if m:
                raw = m.group(1)
        # (D) safety net: "research" anywhere + an unambiguous action signal
        if raw is None and re.search(rf"\b{_RESEARCH}\b", original, re.IGNORECASE) and \
                re.search(r"\b(download|pdfs?|on the internet|online|from the internet|"
                          r"pull up|show me|open them|articles?|papers?|documents?)\b",
                          original, re.IGNORECASE):
            m = re.search(rf"\b{_RESEARCH}\s+(?:about|on|into|for|regarding|of|around)?\s*(.+)",
                          original, re.IGNORECASE)
            if m:
                raw = m.group(1)

        return self._clean_topic(raw) if raw else None

    def _clean_topic(self, raw):
        """Strip command noise off a captured topic ('cnc on internet download pdfs'
        → 'cnc'); return None if what's left looks conversational, not a topic."""
        s = raw.strip().strip("?.!,\"'")
        s = re.sub(r"^(?:about|on|into|for|regarding|of|around|the topic of|some|a|an|the|me|my|please)\s+",
                   "", s, flags=re.IGNORECASE)
        cut = self._TOPIC_CUT_RE.search(s)
        if cut:
            s = s[:cut.start()]
        # trailing command/source noise ("… online", "… in the browser", "… for me")
        # is not part of the topic — peel it off (possibly several layers).
        for _ in range(3):
            s = re.sub(r"\s+(?:online|on the internet|on the web|from the internet|"
                       r"in (?:the |my )?browser|in (?:chrome|edge|firefox)|"
                       r"on (?:the )?screen|on my screen|for me|please|now|"
                       r"and open (?:them|it|the pdfs?)|open (?:them|it)|"
                       r"and explain(?:\s+(?:it|them))?)\s*$",
                       "", s.strip(), flags=re.IGNORECASE)
        s = re.sub(r"\s+(?:and|on|about|of|the|for|to|in)\s*$", "", s.strip(), flags=re.IGNORECASE)
        # strip any leftover leading article/possessive ("the pdfs"→"", "my cnc"→"cnc")
        s = re.sub(r"^(?:the|a|an|some|my|your|our|this|that|these|those)\s+", "", s.strip(),
                   flags=re.IGNORECASE)
        s = s.strip(" ,.-\"'")
        # a "topic" that's just a stopword or a document/UI word is no topic at all —
        # leave "open the pdfs" / "open my documents folder" to the brain / _open.
        if s.lower() in _NOISE_TOPICS or len(s) < 2:
            return None
        if not s or self._TOPIC_BAD_LEAD.match(s):
            return None
        words = s.split()
        if len(words) > 6:               # noise leaked in — keep the head as the topic
            s = " ".join(words[:6])
        return s or None

    def _research(self, t, original):
        # "what are you working on / researching" → report live missions
        status = self._mission_status(t)
        if status:
            return status
        topic = self._research_topic(t, original)
        if not topic or not getattr(self.cfg, "allow_research", True):
            return None
        u = self.cfg.user_title
        ack = (f"On it, {u}. I'm researching {topic} now — opening the sources and pulling "
               f"the PDFs while you carry on. I'll explain what I find.")
        if self._run_mission is not None:
            # go work in the BACKGROUND so JARVIS stays responsive ("works while
            # you focus"); the play-by-play streams to the HUD agent panel.
            self._run_mission(f"Researching {topic}",
                              lambda m: self._research_worker(m, topic), tag="RESEARCH")
            return ack
        # degraded / unit-test path: no background runner → run inline, synchronously
        self.say(ack)
        try:
            self._research_worker(_InlineMission(self.say), topic)
        except Exception as e:  # noqa: BLE001
            print(f"[skills] research error: {e}")
            return f"I got partway through researching {topic}, {u}, but hit a snag."
        return ""   # the worker has already spoken the summary + explanation

    def _mission_status(self, t) -> str | None:
        if not re.search(r"\b(what are you (working on|doing|researching)|"
                         r"what('?s| is) (running|in progress)|any missions? running)\b", t):
            return None
        titles = self._active_missions()
        u = self.cfg.user_title
        if not titles:
            return f"Nothing running right now, {u} — standing by."
        if len(titles) == 1:
            return f"Right now I'm {titles[0][0].lower() + titles[0][1:]}, {u}."
        return f"I've got {len(titles)} on the go, {u}: " + "; ".join(titles) + "."

    def _research_worker(self, mission, topic: str) -> None:
        """The actual multi-step research, streaming progress to the mission panel.
        Speaks only the payoff (summary + explanation) so it doesn't talk over you."""
        u = self.cfg.user_title
        q = requests.utils.quote(topic)

        # 1) throw the reference + video windows open on screen ("windows flying")
        mission.step("Opening web, encyclopaedia & video sources")
        for url in (
            "https://www.google.com/search?q=" + q,
            "https://en.wikipedia.org/wiki/Special:Search?search=" + q,
            "https://www.youtube.com/results?search_query="
            + requests.utils.quote(topic + " explained"),
        ):
            self._open_window(url)

        # 2) open the top few live articles from a real search
        mission.step("Scanning the top live articles")
        links = self._ddg_links(topic, 3)
        mission.note(f"{len(links)} article{'s' if len(links) != 1 else ''} found")
        for url in links:
            self._open_window(url)

        # 3) hunt down, download and open real PDFs
        mission.step("Pulling PDFs off the internet")
        folder = RESEARCH_DIR / self._safe_name(topic)
        candidates = self._find_pdf_urls(topic, 6)
        saved = []
        for url in candidates:
            mission.note(f"Downloading PDF {len(saved) + 1}…")
            path = self._download_pdf(url, folder)
            if path:
                saved.append(path)
                mission.note(f"Saved {len(saved)}: {path.name[:44]}")
                try:
                    os.startfile(str(path))              # type: ignore[attr-defined]
                except Exception:
                    pass
                time.sleep(0.4)
            if len(saved) >= 4:
                break
        if saved:
            mission.step(f"Downloaded {len(saved)} PDF"
                         f"{'s' if len(saved) != 1 else ''} -> Documents\\Jarvis Research")
        else:
            mission.step("No clean PDFs — left the web sources open")

        # 4) explain it, in JARVIS's own voice
        mission.step("Explaining the topic")
        explanation = self._brain_explain(topic)
        mission.finish("done", tag="DONE")

        # spoken payoff (serialised through the mouth so it never overlaps a turn)
        if saved:
            n = len(saved)
            mission.speak(f"Done, {u}. I pulled {n} PDF{'s' if n != 1 else ''} on {topic} into "
                          f"your Documents and opened {'them' if n != 1 else 'it'}.")
        else:
            mission.speak(f"I've laid out the web sources on {topic} for you, {u}.")
        if explanation:
            mission.speak(explanation)

    def _open_window(self, url: str) -> None:
        """Open a URL in the browser, pausing briefly so each window registers on
        screen (cinematic, and gentler on the browser than a burst)."""
        try:
            webbrowser.open(url)
            time.sleep(0.45)
        except Exception as e:
            print(f"[skills] open window failed: {e}")

    @staticmethod
    def _safe_name(topic: str) -> str:
        name = re.sub(r"[^\w\- ]+", "", topic).strip().replace(" ", "_")
        return (name or "topic")[:60]

    @staticmethod
    def _ddg_decode(href: str):
        """DuckDuckGo HTML results wrap the real URL in a redirect
        (//duckduckgo.com/l/?uddg=<encoded>). Return the underlying http(s) URL."""
        if href.startswith("//"):
            href = "https:" + href
        m = re.search(r"[?&]uddg=([^&]+)", href)
        if m:
            try:
                return requests.utils.unquote(m.group(1))
            except Exception:
                return None
        return href if href.startswith("http") else None

    def _ddg_links(self, query: str, limit: int = 3) -> list:
        """Top organic result URLs for a query, via DuckDuckGo's HTML endpoint
        (Bing as a fallback). Best-effort and quiet on failure."""
        urls: list = []
        seen = set()

        def add(u):
            if not u or not u.startswith("http"):
                return
            base = u.split("#")[0]
            if base in seen or any(h in base for h in
                                   ("duckduckgo.com", "bing.com", "microsoft.com", "google.com/search")):
                return
            seen.add(base)
            urls.append(u)

        try:
            r = requests.post("https://html.duckduckgo.com/html/", data={"q": query},
                              timeout=8, headers={"User-Agent": _UA})
            if r.status_code == 200:
                for m in re.finditer(r'result__a[^>]+href="([^"]+)"', r.text):
                    add(self._ddg_decode(m.group(1)))
                    if len(urls) >= limit:
                        return urls[:limit]
        except Exception:
            pass
        if len(urls) < limit:
            try:
                r = requests.get("https://www.bing.com/search?q=" + requests.utils.quote(query),
                                 timeout=8, headers={"User-Agent": _UA})
                if r.status_code == 200:
                    for m in re.finditer(r'<h2>\s*<a[^>]+href="(https?://[^"]+)"', r.text):
                        add(m.group(1))
                        if len(urls) >= limit:
                            break
            except Exception:
                pass
        return urls[:limit]

    def _find_pdf_urls(self, topic: str, limit: int = 6) -> list:
        """Candidate PDF URLs for a topic — those advertising .pdf first, then the
        rest of the results (some PDFs don't show .pdf in the URL) as a backstop."""
        found = self._ddg_links(topic + " filetype:pdf", limit * 3)
        ordered = [u for u in found if ".pdf" in u.lower()]
        ordered += [u for u in found if u not in ordered]
        seen, out = set(), []
        for u in ordered:
            if u not in seen:
                seen.add(u)
                out.append(u)
        return out[:limit]

    def _download_pdf(self, url: str, folder):
        """Download a PDF (verifying it really is one) into ``folder``; return the
        saved Path or None. Bounded by timeout and a 30 MB cap so a hostile or huge
        file can neither hang the turn nor fill the disk."""
        try:
            r = requests.get(url, timeout=(6, 25), stream=True,
                             headers={"User-Agent": _UA}, allow_redirects=True)
        except Exception:
            return None
        try:
            if r.status_code != 200:
                return None
            ct = (r.headers.get("Content-Type") or "").lower()
            if "pdf" not in ct and not url.lower().split("?")[0].endswith(".pdf"):
                return None
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / self._pdf_filename(url)
            total = 0
            try:
                with path.open("wb") as f:
                    for chunk in r.iter_content(65536):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > 30_000_000:       # 30 MB cap
                            break
                        f.write(chunk)
            except Exception:
                try:
                    path.unlink()
                except Exception:
                    pass
                return None
        finally:
            r.close()
        # verify the magic bytes so we never open a saved 404/HTML page as a PDF
        try:
            with path.open("rb") as f:
                if not f.read(5).startswith(b"%PDF"):
                    path.unlink()
                    return None
        except Exception:
            return None
        return path

    @staticmethod
    def _pdf_filename(url: str) -> str:
        tail = url.split("?")[0].rstrip("/").split("/")[-1] or "document"
        try:
            tail = requests.utils.unquote(tail)
        except Exception:
            pass
        tail = re.sub(r"[^\w\-. ]+", "_", tail).strip() or "document"
        if not tail.lower().endswith(".pdf"):
            tail += ".pdf"
        return tail[:80]

    def _brain_explain(self, topic: str) -> str:
        """A compact spoken explanation of the topic, via the brain. Best-effort:
        returns '' if no brain is wired or it fails, so research still concludes."""
        if not self._ask_brain:
            return ""
        prompt = (f"Give {self.cfg.user_title} a clear, engaging spoken explanation of "
                  f"\"{topic}\" in about four sentences — what it is, why it matters, and one "
                  f"genuinely interesting detail. Natural spoken English only: no lists, no "
                  f"markdown, no headings.")
        try:
            return (self._ask_brain(prompt) or "").strip()
        except Exception as e:  # noqa: BLE001
            print(f"[skills] research explanation failed: {e}")
            return ""

    # ── launching ───────────────────────────────────────────────
    def _open(self, t, original):
        m = re.search(r"\b(?:open|launch|start|run)\s+(.+)", t)
        if not m:
            return None
        target = m.group(1).strip()
        # website?
        for name, url in SITES.items():
            if target == name or target.startswith(name + " ") or target == name + ".com":
                webbrowser.open(url)
                return f"Opening {name}, {self.cfg.user_title}."
        # known app?
        for name, cmd in APPS.items():
            if target == name or target.startswith(name):
                self._launch(cmd)
                return f"Opening {name}, {self.cfg.user_title}."
        # a bare domain?
        if re.match(r"^[\w-]+\.\w{2,}$", target):
            webbrowser.open("https://" + target)
            return f"Opening {target}."
        # "open lofi music", "open some jazz playlist" → that's a media request;
        # play it on YouTube rather than treating it as an app to launch.
        if any(h in target for h in _MEDIA_HINTS):
            return self._play_on_youtube(re.sub(r"^(?:some|the|a|an)\s+", "", target).strip())
        # last resort: only for a PLAUSIBLE single target (an app token or a path),
        # never a multi-word phrase like "start over with the plan" — those are
        # conversation, not launch commands, so let the brain field them.
        if " " in target and not re.search(r"[\\/]|\.\w{2,4}$", target):
            return None
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            return f"Opening {target}."
        except Exception:
            return None

    def _launch(self, cmd: str):
        if cmd.startswith("start "):
            subprocess.Popen(["cmd", "/c", cmd], shell=False)
        else:
            try:
                subprocess.Popen(cmd, shell=True)
            except Exception:
                subprocess.Popen(["cmd", "/c", "start", "", cmd])

    def _search(self, t, original):
        if "youtube" in t or "you tube" in t:      # handled by _youtube
            return None
        m = re.search(r"\b(?:search (?:the )?(?:web|internet|net) for|search youtube for|"
                      r"search for|search|google|look up|look for|find me)\s+(.+)", t)
        if not m:
            return None
        q = m.group(1).strip(" ,.?!")
        q = re.sub(r"^(?:for|me|the\s+(?:web|internet|net)\s+for)\s+", "", q).strip()
        if not q:
            return None
        webbrowser.open("https://www.google.com/search?q=" + requests.utils.quote(q))
        return f"Here are the results for {q}, {self.cfg.user_title}."

    def _youtube(self, t, original):
        # Every natural way of asking for a video/song → find it on YouTube and play
        # it: "play X", "put on X", "find/search/pull up X on youtube",
        # "search youtube for X", "X on youtube", "watch X on youtube".
        has_yt = "youtube" in t or "you tube" in t
        m = None
        if has_yt:
            m = (re.search(r"\b(?:search you\s?tube for|on you\s?tube search for)\s+(.+)", t)
                 or re.search(r"\b(?:play|put on|listen to|find|search|search for|look up|"
                              r"look for|pull up|bring up|open|watch|show me|get)\s+"
                              r"(.+?)\s+on\s+you\s?tube\b", t)
                 or re.search(r"^\s*(.+?)\s+on\s+you\s?tube\b", t)
                 or re.search(r"\byou\s?tube\s+(.+)", t))
        if m is None:
            # natural media intent WITHOUT the word "youtube": "play/put on/listen to X"
            m = re.search(r"\b(?:play|put on|listen to|watch)\s+(.+?)(?:\s+on repeat|\s+please)?$", t)
            if not m or not re.search(r"\b(?:play|put on|listen to|watch)\b", t):
                return None
        q = re.sub(r"\b(?:on\s+)?you\s?tube\b", "", m.group(1), flags=re.IGNORECASE).strip()
        q = re.sub(r"^(?:some|the|a|an|me|for)\s+", "", q).strip(" ,.?!")
        if not q:
            return None
        # "play devil's advocate", "play it cool", "play along" … are idioms, not
        # media requests — let the brain field them rather than opening YouTube.
        ql = q.lower()
        if not has_yt and any(ql == p or ql.startswith(p + " ") for p in _PLAY_IDIOMS):
            return None
        return self._play_on_youtube(q)

    def _play_on_youtube(self, query: str) -> str:
        """Open the FIRST YouTube result for ``query`` so it starts playing straight
        away — we scrape the results page for the top videoId and open the watch
        page. Falls back to the plain results page if the scrape fails."""
        query = query.strip()
        watch = None
        try:
            r = requests.get(
                "https://www.youtube.com/results?search_query=" + requests.utils.quote(query),
                timeout=6, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                m = re.search(r'"videoId":"([\w-]{11})"', r.text)
                if m:
                    watch = f"https://www.youtube.com/watch?v={m.group(1)}"
        except Exception:
            watch = None
        if watch:
            webbrowser.open(watch)
            return f"Playing {query} on YouTube, {self.cfg.user_title}."
        webbrowser.open("https://www.youtube.com/results?search_query=" + requests.utils.quote(query))
        return f"Here's {query} on YouTube, {self.cfg.user_title}."

    # ── volume ──────────────────────────────────────────────────
    def _volume(self, t, _):
        if "volume" not in t and not re.search(r"\b(mute|unmute)\b", t):
            return None
        vol = self._vol_iface()
        if vol is None:
            return "I can't reach the audio device just now."
        if re.search(r"\bunmute\b", t):
            vol.SetMute(0, None); return "Unmuted."
        if re.search(r"\bmute\b", t):
            vol.SetMute(1, None); return "Muted."
        cur = vol.GetMasterVolumeLevelScalar()
        m = re.search(r"(\d{1,3})\s*(?:percent|%)?", t)
        if re.search(r"\b(max|maximum|full)\b", t):
            vol.SetMasterVolumeLevelScalar(1.0, None); return "Volume at maximum."
        if "set" in t and m:
            lvl = max(0, min(100, int(m.group(1)))) / 100
            vol.SetMasterVolumeLevelScalar(lvl, None)
            return f"Volume set to {int(lvl*100)} percent."
        if re.search(r"\b(up|increase|louder|raise)\b", t):
            vol.SetMasterVolumeLevelScalar(min(1.0, cur + 0.1), None); return "Turning it up."
        if re.search(r"\b(down|decrease|lower|quieter|reduce)\b", t):
            vol.SetMasterVolumeLevelScalar(max(0.0, cur - 0.1), None); return "Turning it down."
        return f"The volume is at {int(cur*100)} percent."

    def _vol_iface(self):
        try:
            import comtypes
            try:
                comtypes.CoInitialize()
            except Exception:
                pass
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices = AudioUtilities.GetSpeakers()
            iface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            return cast(iface, POINTER(IAudioEndpointVolume))
        except Exception as e:
            print(f"[skills] volume iface error: {e}")
            return None

    # ── brightness ──────────────────────────────────────────────
    def _brightness(self, t, _):
        if "brightness" not in t:
            return None
        try:
            import screen_brightness_control as sbc
            m = re.search(r"(\d{1,3})", t)
            if re.search(r"\b(max|maximum|full)\b", t):
                sbc.set_brightness(100); return "Brightness at maximum."
            if "set" in t and m:
                lvl = max(0, min(100, int(m.group(1)))); sbc.set_brightness(lvl)
                return f"Brightness set to {lvl} percent."
            cur = sbc.get_brightness()[0]
            if re.search(r"\b(up|increase|brighter|raise)\b", t):
                sbc.set_brightness(min(100, cur + 15)); return "Brightening the display."
            if re.search(r"\b(down|decrease|dimmer|lower|dim)\b", t):
                sbc.set_brightness(max(0, cur - 15)); return "Dimming the display."
            return f"Brightness is at {cur} percent."
        except Exception as e:
            return f"I couldn't adjust the brightness — {e}."

    # ── screen vision ("what's on my screen") ───────────────────
    def _vision(self, t, _):
        if not re.search(r"\b(what('?s| is) on (my |the )?screen|describe (my |the )?screen|"
                         r"read (my |the )?screen|what am i looking at|look at (my |the )?screen|"
                         r"can you see (my |the )?screen|see my screen)\b", t):
            return None
        u = self.cfg.user_title
        # fast native vision (Anthropic API) if a key is set; otherwise let the agent
        # read a screenshot (works with just the Claude subscription, a bit slower).
        fast = self._can_see() and self._describe_image is not None
        agent_ok = self._run_mission is not None and getattr(self.cfg, "allow_agentic", True)
        if not fast and not agent_ok:
            if self._describe_image is None:
                return None      # nothing wired → let the brain field it
            return (f"To see your screen I need an Anthropic key, {u} — add one in the gear "
                    f"for instant vision, or keep the agent on and I'll read it for you.")
        question = "In two or three short spoken sentences, describe what is currently on my screen."
        if self._run_mission is None:
            # degraded / tests: synchronous fast path (HUD may be in the frame)
            try:
                from PIL import ImageGrab
                import base64, io
                img = ImageGrab.grab()
                img.thumbnail((1280, 1280))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=70)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception as e:
                return f"I couldn't capture the screen — {e}."
            return self._describe_image(question, b64)
        # mission path: the HUD shrinks aside first (VISION tag), THEN we capture the
        # screen behind it — otherwise the reactor fills the shot.
        self._run_mission("Looking at your screen",
                          lambda m: self._screen_read_worker(m, fast, question), tag="VISION")
        return f"Let me take a look, {u}."

    def _screen_read_worker(self, mission, fast: bool, question: str) -> None:
        """Wait for the HUD to shrink out of the way, capture the screen, and read it —
        fast (Anthropic) or via the Read-only agent."""
        u = self.cfg.user_title
        time.sleep(1.3)                      # let the HUD actually collapse to the corner first
        try:
            from PIL import ImageGrab
            shot = ImageGrab.grab()
        except Exception as e:
            mission.error("couldn't capture the screen")
            mission.speak(f"I couldn't grab the screen, {u} — {e}.")
            return
        if fast:
            try:
                import base64, io
                shot.thumbnail((1280, 1280))
                buf = io.BytesIO()
                shot.convert("RGB").save(buf, format="JPEG", quality=70)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                desc = (self._describe_image(question, b64) or "").strip()
            except Exception:
                desc = ""
            mission.finish("done", tag="SEEN")
            mission.speak(desc or f"I couldn't quite read the screen, {u}.")
            return
        try:
            folder = Path.home() / "Pictures" / "Jarvis"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"_screen_{int(time.time())}.png"
            shot.save(path)
        except Exception as e:
            mission.error("couldn't save the screen")
            mission.speak(f"I couldn't save the screen to look at, {u}.")
            return
        self._vision_worker(mission, path, ask=question)

    # ── Instagram insights ("check my instagram reels stats") ───
    def _instagram(self, t, original):
        if not re.search(r"\b(instagram|insta|\big\b)\b", t):
            return None
        u = self.cfg.user_title
        wants = re.search(
            r"\b(insight|insights|stat|stats|statistic|statistics|analytic|analytics|performance|"
            r"reels?|views|plays|reach|impression|impressions|engagement|followers?|"
            r"how('?s| is| are) (?:my|it|they)|doing|numbers|metrics)\b", t)
        if not wants:
            if re.search(r"\b(open|go to|launch|show me|pull up|check)\b", t):
                webbrowser.open("https://www.instagram.com/")
                return f"Opening Instagram, {u}."
            return None
        # open the desktop surfaces where Reels numbers live (uses your logged-in browser)
        for url in ("https://business.facebook.com/latest/insights/content",
                    "https://www.instagram.com/"):
            try:
                webbrowser.open(url)
                time.sleep(0.4)
            except Exception:
                pass
        if self._run_mission is None:
            return (f"I've opened your Instagram insights, {u}. Say 'what's on my screen' once "
                    f"the numbers are up and I'll read them.")
        ask = ("This screenshot shows my Instagram / Meta Business Suite insights. Read out the key "
               "numbers for my recent Reels — views or plays, reach, likes and engagement — in a "
               "few short spoken sentences. If the insights aren't visible yet, tell me to open "
               "the Reels insights.")

        def worker(mission):
            mission.step("Opening your Instagram insights")
            time.sleep(9)                    # dashboard load + HUD shrink aside
            mission.step("Reading your Reels numbers")
            try:
                from PIL import ImageGrab
                folder = Path.home() / "Pictures" / "Jarvis"
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"_ig_{int(time.time())}.png"
                ImageGrab.grab().save(path)
            except Exception as e:
                mission.error("couldn't capture the screen")
                mission.speak(f"I couldn't grab your Instagram screen, {u}.")
                return
            self._vision_worker(mission, path, ask=ask)

        self._run_mission("Instagram insights", worker, tag="VISION")
        return f"Pulling up your Instagram insights, {u} — I'll read what I can see."

    def _vision_worker(self, mission, img_path, ask: str = None) -> None:
        """Have the Claude agent read a screenshot and speak an answer about it. Uses the
        Read tool only (no system access needed just to look), so it's the lighter
        agent invocation. ``ask`` customises the question (e.g. Instagram insights)."""
        u = self.cfg.user_title
        mission.step("Looking at the screen")
        question = ask or ("In two or three short, natural spoken sentences, tell me what is "
                           "on the screen.")
        prompt = (f"Read the image file at {img_path} — it is a screenshot of my screen. "
                  f"{question} Output ONLY the answer, nothing else.")
        try:
            import shutil as _sh
            claude = _sh.which("claude") or "claude"
            proc = subprocess.run(
                [claude, "-p", "--output-format", "json", "--allowedTools", "Read",
                 "--permission-mode", "acceptEdits", prompt],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=150, cwd=str(Path.home()), creationflags=_LOWPRIO,
            )
            import json as _json
            try:
                res = _json.loads(proc.stdout).get("result", "")
            except Exception:
                res = (proc.stdout or "").strip()
            mission.finish("done", tag="SEEN")
            mission.speak(res.strip() or f"I couldn't quite make out the screen, {u}.")
        except Exception as e:  # noqa: BLE001
            print(f"[skills] vision worker error: {e}")
            mission.error("couldn't read the screen")
            mission.speak(f"I couldn't get a clear look at the screen, {u}.")
        finally:
            try:
                os.remove(img_path)
            except Exception:
                pass

    # ── screen recording (ffmpeg gdigrab → mp4) ─────────────────
    def _record(self, t, _):
        u = self.cfg.user_title
        start = re.search(
            r"\b(?:start (?:the )?recording|record a video|record my video|take a video|"
            r"capture (?:a )?video|screen record|"
            r"record(?:ing)?\s+(?:[\w']+\s+){0,3}?(?:screen|monitor|display|desktop))\b", t)
        stop = re.search(r"\b(stop recording|stop the recording|end recording|"
                         r"finish recording|stop the video|end the recording)\b", t)
        if not start and not stop:
            return None
        if stop:
            if not self._rec_proc or self._rec_proc.poll() is not None:
                self._rec_proc = None
                return f"I'm not recording anything right now, {u}."
            path = self._rec_path
            try:
                # ffmpeg stops cleanly (and finalises the mp4) when it receives 'q'
                if self._rec_proc.stdin:
                    self._rec_proc.stdin.write(b"q")
                    self._rec_proc.stdin.flush()
                self._rec_proc.wait(timeout=8)
            except Exception:
                try:
                    self._rec_proc.terminate()
                    self._rec_proc.wait(timeout=5)
                except Exception:
                    pass
            self._rec_proc = None
            if path:
                try:
                    os.startfile(str(path))          # type: ignore[attr-defined]
                except Exception:
                    pass
            return f"Recording saved to your Videos, under Jarvis, {u}."
        # start
        if self._rec_proc and self._rec_proc.poll() is None:
            return f"I'm already recording, {u}. Say 'stop recording' when you're done."
        import shutil as _sh
        ffmpeg = _sh.which("ffmpeg") or "ffmpeg"
        folder = Path.home() / "Videos" / "Jarvis"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"recording_{int(time.time())}.mp4"
        # figure out WHICH screen to record
        mon, where = self._pick_record_monitor(t)
        grab = ["-f", "gdigrab", "-framerate", "30"]
        if mon is not None:
            grab += ["-offset_x", str(mon["left"]), "-offset_y", str(mon["top"]),
                     "-video_size", f'{mon["width"]}x{mon["height"]}']
        grab += ["-i", "desktop"]
        try:
            self._rec_proc = subprocess.Popen(
                [ffmpeg, "-y", *grab,
                 "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._rec_path = path
        except Exception as e:
            self._rec_proc = None
            return f"I couldn't start recording — {e}."
        return f"Recording {where} now, {u}. Say 'stop recording' when you're done."

    # ── monitor discovery + which-screen-to-record logic ────────
    def _pick_record_monitor(self, t):
        """Decide which monitor to record from the phrasing. Returns (monitor|None,
        spoken_where). ``None`` monitor means the whole (multi-screen) desktop."""
        mons = self._monitors()
        whole = re.search(r"\b(whole|entire|all|both|everything|all screens?|all monitors?)\b", t)
        if len(mons) <= 1:
            return (mons[0] if mons else None, "your screen")
        if whole:
            return None, "all your screens"
        m = re.search(r"\b(?:screen|monitor|display)\s*(?:number\s*)?(\d+)\b", t) \
            or re.search(r"\b(\d+)(?:st|nd|rd|th)?\s+(?:screen|monitor|display)\b", t)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(mons):
                return mons[idx], f"screen {idx + 1}"
        if re.search(r"\b(this|current|active|the one i'm on|where i am)\b", t):
            fg = self._foreground_monitor(mons)
            if fg:
                return fg, "this screen"
        if re.search(r"\b(other|second|2nd|next)\b", t):
            other = next((x for x in mons if not x.get("primary")), None)
            if other:
                return other, "the second screen"
        if re.search(r"\b(main|primary|first)\b", t):
            prim = next((x for x in mons if x.get("primary")), mons[0])
            return prim, "your main screen"
        # default: the screen you're actually working on (foreground window), else primary
        fg = self._foreground_monitor(mons)
        if fg:
            return fg, "the screen you're on"
        prim = next((x for x in mons if x.get("primary")), mons[0])
        return prim, "your main screen"

    @staticmethod
    def _monitors():
        """Enumerate physical monitor rectangles, left→right. Empty on non-Windows."""
        out = []
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
            CB = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                                    ctypes.POINTER(wintypes.RECT), ctypes.c_double)

            def _cb(hMon, hdc, lprc, lparam):
                r = lprc.contents
                out.append({"left": int(r.left), "top": int(r.top),
                            "width": int(r.right - r.left), "height": int(r.bottom - r.top)})
                return 1
            user32.EnumDisplayMonitors(0, 0, CB(_cb), 0)
        except Exception as e:
            print(f"[skills] monitor enum failed: {e}")
            return []
        for mo in out:
            mo["primary"] = (mo["left"] == 0 and mo["top"] == 0)
        out.sort(key=lambda mo: (mo["left"], mo["top"]))
        return out

    @staticmethod
    def _foreground_monitor(mons):
        """The monitor containing the centre of the current foreground window."""
        try:
            import ctypes
            from ctypes import wintypes
            u = ctypes.windll.user32
            hwnd = u.GetForegroundWindow()
            r = wintypes.RECT()
            u.GetWindowRect(hwnd, ctypes.byref(r))
            cx = (r.left + r.right) // 2
            cy = (r.top + r.bottom) // 2
            for mo in mons:
                if mo["left"] <= cx < mo["left"] + mo["width"] and \
                        mo["top"] <= cy < mo["top"] + mo["height"]:
                    return mo
        except Exception:
            pass
        return None

    # ── screenshot ──────────────────────────────────────────────
    def _screenshot(self, t, _):
        if not re.search(r"\b(screenshot|screen shot|capture (the |my )?screen|grab the screen)\b", t):
            return None
        from PIL import ImageGrab
        folder = Path.home() / "Pictures" / "Jarvis"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"screenshot_{int(time.time())}.png"
        ImageGrab.grab().save(path)
        return f"Screenshot saved to your Pictures, {self.cfg.user_title}."

    # ── system status ───────────────────────────────────────────
    def _system(self, t, _):
        # require a status-query framing — bare "memory"/"cpu"/"ram" would hijack
        # ordinary sentences like "how do I clear memory in python".
        if not re.search(r"\b(system status|status report|how('?s| is) the (system|pc|computer)"
                         r"|(cpu|processor|memory|ram) (usage|load|status)"
                         r"|how much (memory|ram|cpu))\b", t):
            return None
        import psutil
        cpu = psutil.cpu_percent(interval=0.4)
        mem = psutil.virtual_memory().percent
        return (f"All systems nominal, {self.cfg.user_title}. "
                f"Processor at {cpu:.0f} percent, memory at {mem:.0f} percent.")

    def _battery(self, t, _):
        if "battery" not in t and "power" not in t:
            return None
        if "battery" not in t:
            return None
        import psutil
        b = psutil.sensors_battery()
        if b is None:
            return "This machine doesn't report a battery."
        state = "charging" if b.power_plugged else "on battery"
        return f"Battery is at {b.percent:.0f} percent, {state}."

    # ── window control ──────────────────────────────────────────
    def _window(self, t, _):
        try:
            import pyautogui
        except Exception:
            return None
        if re.search(r"\b(show desktop|minimi[sz]e (all|everything))\b", t):
            pyautogui.hotkey("win", "d"); return "Showing the desktop."
        if re.search(r"\b(close (this|the) window)\b", t):
            pyautogui.hotkey("alt", "f4"); return "Closing the window."
        if re.search(r"\b(maximi[sz]e)\b", t):
            pyautogui.hotkey("win", "up"); return "Maximised."
        if re.search(r"\b(switch (window|app)|next window)\b", t):
            pyautogui.hotkey("alt", "tab"); return "Switching."
        return None

    def _type_text(self, t, original):
        m = re.search(r"\b(?:type|write)\s+(.+)", original, re.IGNORECASE)
        if not m or not t.startswith(("type", "write")):
            return None
        try:
            import pyautogui
            time.sleep(0.4)
            pyautogui.typewrite(m.group(1), interval=0.02)
            return "Done."
        except Exception as e:
            return f"I couldn't type that — {e}."

    # ── clipboard ───────────────────────────────────────────────
    def _clipboard(self, t, original):
        try:
            import pyperclip
        except Exception:
            return None
        if re.search(r"\b(what('?s| is) (on |in )?(my )?clipboard|read (my )?clipboard)\b", t):
            content = pyperclip.paste()
            return f"Your clipboard says: {content}" if content else "Your clipboard is empty."
        m = re.search(r"\bcopy\s+(.+?)\s+to (the )?clipboard", original, re.IGNORECASE)
        if m:
            pyperclip.copy(m.group(1)); return "Copied to your clipboard."
        return None

    # ── notes ───────────────────────────────────────────────────
    def _note(self, t, original):
        m = re.search(r"\b(?:make a note|note that|remember that|take a note)\s+(.+)", original, re.IGNORECASE)
        if m:
            with NOTES.open("a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%Y-%m-%d %H:%M}] {m.group(1).strip()}\n")
            return f"Noted, {self.cfg.user_title}."
        if re.search(r"\b(read (my )?notes|what are my notes)\b", t):
            if not NOTES.exists():
                return "You have no notes yet."
            lines = [l.strip() for l in NOTES.read_text(encoding="utf-8").splitlines() if l.strip()]
            if not lines:
                return "You have no notes yet."
            recent = lines[-5:]
            return "Your recent notes: " + "; ".join(re.sub(r"^\[.*?\]\s*", "", l) for l in recent)
        return None

    # ── scheduling core (timers / reminders / alarms share this) ─
    def _schedule(self, secs: float, spoken: str, label: str, kind: str,
                  persist: bool = True) -> int:
        """Fire ``spoken`` via the mouth after ``secs`` seconds; track it so it can
        be listed or cancelled. Returns the reminder id. The registry is touched
        from Timer callback threads too, so all access is guarded by _rlock.
        ``persist=False`` skips the disk write (used during bulk restore)."""
        due = datetime.now() + timedelta(seconds=secs)

        def fire():
            with self._rlock:
                self._reminders[:] = [r for r in self._reminders if r["id"] != rid]
                self._persist()
            self.say(spoken)
        timer = threading.Timer(max(0.0, secs), fire)
        timer.daemon = True
        with self._rlock:
            self._rid += 1
            rid = self._rid
            self._reminders.append({"id": rid, "kind": kind, "label": label,
                                    "spoken": spoken, "due": due, "timer": timer})
            if persist:
                self._persist()
        timer.start()
        return rid

    # ── persistence (reminders & alarms survive a restart) ──────
    def _persist(self) -> None:
        """Write the durable (reminder/alarm) entries to disk. Call under _rlock."""
        keep = [{"kind": r["kind"], "label": r["label"], "spoken": r.get("spoken", ""),
                 "due": r["due"].isoformat()}
                for r in self._reminders if r["kind"] in ("reminder", "alarm")]
        try:
            REMINDERS_FILE.write_text(json.dumps(keep, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[skills] could not save reminders: {e}")

    def _load_persisted(self) -> None:
        """Reschedule still-future reminders/alarms from a previous session; drop
        any that fell due while JARVIS was off."""
        try:
            data = json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        now = datetime.now()
        restored = 0
        for e in data if isinstance(data, list) else []:
            try:
                due = datetime.fromisoformat(e["due"])
            except Exception:
                continue
            if due <= now:
                continue
            self._schedule((due - now).total_seconds(), e.get("spoken", ""),
                           e.get("label", "reminder"), e.get("kind", "reminder"),
                           persist=False)      # avoid a write per entry…
            restored += 1
        if restored:
            with self._rlock:
                self._persist()                # …persist once at the end instead


    def _pending(self, t) -> str | None:
        if not re.search(r"\b(list|show|what are|any)\b.*\b(timer|timers|reminder|reminders|alarm|alarms)\b", t):
            return None
        with self._rlock:
            snapshot = sorted(self._reminders, key=lambda x: x["due"])
        if not snapshot:
            return f"You have nothing scheduled, {self.cfg.user_title}."
        parts = [f"{r['label']} at {r['due'].strftime('%I:%M %p').lstrip('0')}" for r in snapshot]
        return "You have " + "; ".join(parts) + f", {self.cfg.user_title}."

    def _cancel_scheduled(self, t) -> str | None:
        if not re.search(r"\bcancel (all |my |the )?(timer|timers|reminder|reminders|alarm|alarms)\b", t):
            return None
        with self._rlock:
            pending = list(self._reminders)
            self._reminders.clear()
            self._persist()
        for r in pending:
            try:
                r["timer"].cancel()
            except Exception:
                pass
        n = len(pending)
        if n == 0:
            return f"There was nothing to cancel, {self.cfg.user_title}."
        return f"Cancelled {n} {'item' if n == 1 else 'items'}, {self.cfg.user_title}."

    # ── timers ──────────────────────────────────────────────────
    def _timer(self, t, _):
        hit = self._pending(t) or self._cancel_scheduled(t)
        if hit:
            return hit
        m = re.search(r"\btimer for (\d+)\s*(second|seconds|minute|minutes|hour|hours)\b", t)
        if not m:
            return None
        n = int(m.group(1)); unit = m.group(2)
        secs = n * (3600 if "hour" in unit else 60 if "minute" in unit else 1)
        spoken = f"{self.cfg.user_title.capitalize()}, your {n} {unit} timer is up."
        self._schedule(secs, spoken, f"{n}-{unit} timer", "timer")
        return f"Timer set for {n} {unit}, {self.cfg.user_title}."

    # ── reminders & alarms ──────────────────────────────────────
    def _reminder(self, t, original):
        # relative:  "remind me to call mum in 10 minutes"
        m = re.search(r"remind me (?:to |that )?(.+?)\s+in\s+(\d+)\s*(second|seconds|minute|minutes|hour|hours)\b",
                      original, re.IGNORECASE)
        if m:
            task = m.group(1).strip(); n = int(m.group(2)); unit = m.group(3).lower()
            secs = n * (3600 if "hour" in unit else 60 if "minute" in unit else 1)
            self._schedule(secs, f"Reminder, {self.cfg.user_title}: {task}.",
                           f"reminder to {task}", "reminder")
            return f"I'll remind you to {task} in {n} {unit}, {self.cfg.user_title}."
        # absolute:  "remind me to X at 3:30 pm"  /  "set an alarm for 7 am"
        m = re.search(r"remind me (?:to |that )?(.+?)\s+at\s+(.+)$", original, re.IGNORECASE)
        if m:
            task = m.group(1).strip()
            when = self._parse_clock(m.group(2))
            if when is None:
                return None
            secs = (when - datetime.now()).total_seconds()
            self._schedule(secs, f"Reminder, {self.cfg.user_title}: {task}.",
                           f"reminder to {task}", "reminder")
            return f"I'll remind you to {task} at {when.strftime('%I:%M %p').lstrip('0')}, {self.cfg.user_title}."
        m = re.search(r"\b(?:set an? )?alarm(?: for| at)?\s+(.+)$", t)
        if m:
            when = self._parse_clock(m.group(1))
            if when is None:
                return None
            secs = (when - datetime.now()).total_seconds()
            self._schedule(secs, f"{self.cfg.user_title.capitalize()}, your alarm is going off. Time to move.",
                           "alarm", "alarm")
            return f"Alarm set for {when.strftime('%I:%M %p').lstrip('0')}, {self.cfg.user_title}."
        return None

    @staticmethod
    def _parse_clock(s: str):
        """Parse '7am', '7:30 pm', '15:00', '3 pm' -> the next datetime it occurs."""
        s = s.strip().lower().rstrip(".!?")
        m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
        if not m:
            return None
        hour = int(m.group(1)); minute = int(m.group(2) or 0)
        ap = m.group(3)
        if minute > 59 or hour > 23:
            return None
        if ap == "pm" and hour < 12:
            hour += 12
        elif ap == "am" and hour == 12:
            hour = 0
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)   # next occurrence
        return target

    # ── weather ─────────────────────────────────────────────────
    def _weather(self, t, _):
        if "weather" not in t and "temperature" not in t and "forecast" not in t:
            return None
        # idioms that contain "weather" but aren't a forecast request
        if "under the weather" in t or "weather the storm" in t:
            return None
        city = self.cfg.weather_city
        m = re.search(r"\b(?:weather|temperature|forecast)\s+(?:in|for|at)\s+(.+)", t)
        if m:
            city = m.group(1).strip()
        try:
            url = f"https://wttr.in/{requests.utils.quote(city)}?format=%C,+%t,+feels+like+%f"
            r = requests.get(url, timeout=8, headers={"User-Agent": "curl"})
            r.raise_for_status()
            place = city or "your area"
            return f"The weather in {place}: {r.text.strip()}."
        except Exception:
            return "I couldn't reach the weather service just now."

    # ── power (guarded) ─────────────────────────────────────────
    def _power(self, t, _):
        if re.search(r"\bcancel (the )?shutdown\b", t):
            subprocess.Popen(["shutdown", "/a"]); return "Shutdown cancelled."
        if re.search(r"\block (the )?(computer|screen|pc|workstation)\b", t):
            import ctypes
            ctypes.windll.user32.LockWorkStation(); return "Locking the workstation."
        if not self.cfg.allow_shutdown:
            return None
        if re.search(r"\b(go to sleep|sleep the (computer|pc)|put (the )?(computer|pc) to sleep)\b", t):
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
            return "Going to sleep."
        if re.search(r"\b(shut ?down the (computer|pc|machine)|power off the (computer|pc))\b", t):
            subprocess.Popen(["shutdown", "/s", "/t", "8"])
            return f"Shutting down in eight seconds, {self.cfg.user_title}. Say 'cancel shutdown' to stop me."
        if re.search(r"\brestart the (computer|pc|machine)\b", t):
            subprocess.Popen(["shutdown", "/r", "/t", "8"])
            return f"Restarting in eight seconds, {self.cfg.user_title}. Say 'cancel shutdown' to stop me."
        return None

    # ── media transport (play/pause/next/previous) ─────────────
    def _media(self, t, _):
        actions = [
            (r"^(pause|pause (the )?(music|song|track|video|playback))$", "playpause", "Paused, {u}."),
            (r"^(resume|unpause|continue|resume (the )?(music|playback)|play (the )?music|play some music)$",
             "playpause", "Playing, {u}."),
            (r"^(next|next (track|song)|skip( it| this| song| track)?)$", "nexttrack", "Next track."),
            (r"^(previous|previous (track|song)|last (track|song)|go back a (track|song))$",
             "prevtrack", "Previous track."),
            (r"^(stop (the )?(music|playback|song))$", "stop", "Stopped, {u}."),
        ]
        for pat, key, reply in actions:
            if re.fullmatch(pat, t):
                try:
                    import pyautogui
                    pyautogui.press(key)
                except Exception:
                    return "I can't reach the media controls just now."
                return reply.format(u=self.cfg.user_title)
        return None

    # ── close / quit an app ─────────────────────────────────────
    def _close_app(self, t, _):
        m = re.search(r"\b(?:close|quit|kill|terminate)\s+(.+)", t)
        if not m:
            return None
        target = m.group(1).strip()
        exe = CLOSE_APPS.get(target)
        if exe is None:
            for name, e in CLOSE_APPS.items():
                if target == name or target.startswith(name + " "):
                    exe = e
                    break
        if exe is None:
            return None   # unknown / "the window" / "jarvis" → let others handle
        try:
            subprocess.Popen(["taskkill", "/IM", exe, "/F"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            return f"I couldn't close {target} — {e}."
        return f"Closing {target}, {self.cfg.user_title}."

    # ── mental arithmetic ───────────────────────────────────────
    def _math(self, t, _):
        # "15 percent of 240"
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:percent|%)\s+of\s+(\d+(?:\.\d+)?)", t)
        if m:
            val = float(m.group(1)) / 100.0 * float(m.group(2))
            return f"That's {self._fmt_num(val)}, {self.cfg.user_title}."
        s = t
        s = re.sub(r"^(what('?s| is)|whats|calculate|compute|work out|how much is|solve)\s+", "", s)
        s = s.replace("plus", "+").replace("minus", "-")
        s = re.sub(r"\b(times|multiplied by|x)\b", "*", s)
        s = re.sub(r"\b(divided by|over)\b", "/", s)
        s = re.sub(r"\b(to the power of|power)\b", "**", s)
        s = re.sub(r"\b(mod|modulo)\b", "%", s)
        s = re.sub(r"square root of\s+", "sqrt ", s)
        s = re.sub(r"\bsquared\b", "**2", s)
        s = re.sub(r"\bcubed\b", "**3", s)
        s = s.replace("sqrt ", "sqrt")
        s = re.sub(r"sqrt\s*(\d+(?:\.\d+)?)", r"sqrt(\1)", s)
        s = s.rstrip(" =?")
        # must contain a digit AND an operator or function to count as a calculation
        if not re.search(r"\d", s) or not re.search(r"[+\-*/%]|sqrt|pow|abs", s):
            return None
        if not re.fullmatch(r"[\d\s.+\-*/%()sqrtpowabsround,]+", s):
            return None
        try:
            # _safe_math bounds exponent/factorial magnitude internally, so a
            # pathological 'pow(9, 99999999)' raises instead of hanging the thread.
            result = _safe_math(s)
        except Exception:
            return None
        return f"That's {self._fmt_num(result)}, {self.cfg.user_title}."

    @staticmethod
    def _fmt_num(v: float) -> str:
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if isinstance(v, int):
            return str(v)
        # fixed-point, trimmed — never scientific notation (e.g. "1e-05"), which is
        # unspeakable. Round to 4 dp and drop trailing zeros.
        s = f"{v:.4f}".rstrip("0").rstrip(".")
        return s or "0"

    # ── unit conversion ─────────────────────────────────────────
    def _convert(self, t, _):
        m = re.search(r"\b(?:convert\s+)?(-?\d+(?:\.\d+)?)\s*(?:degrees?\s+)?([a-z]+)\s+(?:to|in|into)\s+([a-z]+)", t)
        if not m:
            return None
        value = float(m.group(1)); u_from = m.group(2); u_to = m.group(3)
        # temperature is affine, handle on its own
        if u_from in _TEMP_UNITS or u_to in _TEMP_UNITS:
            out = self._convert_temp(value, u_from, u_to)
            if out is None:
                return None
            return (f"{self._fmt_num(value)} degrees {self._temp_name(u_from)} is "
                    f"{self._fmt_num(out)} degrees {self._temp_name(u_to)}, {self.cfg.user_title}.")
        for table in _UNIT_TABLES.values():
            if u_from in table and u_to in table:
                out = value * table[u_from] / table[u_to]
                return f"{self._fmt_num(value)} {u_from} is {self._fmt_num(out)} {u_to}, {self.cfg.user_title}."
        return None

    @staticmethod
    def _convert_temp(v, a, b):
        a = a[0]; b = b[0]   # c/f/k
        if a not in "cfk" or b not in "cfk":
            return None
        c = v if a == "c" else (v - 32) * 5 / 9 if a == "f" else v - 273.15
        return c if b == "c" else c * 9 / 5 + 32 if b == "f" else c + 273.15

    @staticmethod
    def _temp_name(u):
        return {"c": "Celsius", "f": "Fahrenheit", "k": "Kelvin"}[u[0]]

    # ── dictionary definitions ──────────────────────────────────
    def _define(self, t, _):
        m = (re.search(r"\bwhat does\s+(.+?)\s+mean\b", t)
             or re.search(r"\b(?:define|definition of|meaning of)\s+(.+)", t))
        if not m:
            return None
        word = m.group(1).strip().strip("?\"'")
        if not word or len(word.split()) > 3:
            return None
        try:
            r = requests.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{requests.utils.quote(word)}",
                             timeout=8)
            if r.status_code != 200:
                return f"I couldn't find a definition for {word}, {self.cfg.user_title}."
            data = r.json()
            meaning = data[0]["meanings"][0]
            pos = meaning.get("partOfSpeech", "")
            definition = meaning["definitions"][0]["definition"]
            lead = f"{word}, {pos}: " if pos else f"{word}: "
            return lead + definition
        except Exception:
            return f"I couldn't reach the dictionary just now, {self.cfg.user_title}."

    # ── news headlines ──────────────────────────────────────────
    def _news(self, t, _):
        if not re.search(r"\b(news|headlines|what('?s| is) happening in the world)\b", t):
            return None
        try:
            r = requests.get("https://feeds.bbci.co.uk/news/world/rss.xml",
                             timeout=8, headers={"User-Agent": "curl"})
            r.raise_for_status()
            titles = re.findall(r"<item>.*?<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>",
                                r.text, re.DOTALL)
            titles = [re.sub(r"<.*?>", "", x).strip() for x in titles if x.strip()]
            if not titles:
                return "I couldn't parse the headlines just now."
            top = titles[:3]
            return f"Today's top headlines, {self.cfg.user_title}: " + "; ".join(top) + "."
        except Exception:
            return "I couldn't reach the news just now."

    # ── fun: jokes, coins, dice, random ─────────────────────────
    def _fun(self, t, _):
        if re.search(r"\b(tell me a joke|another joke|make me laugh|say something funny)\b", t):
            return random.choice(JOKES)
        if re.search(r"\b(flip|toss) (a )?coin\b", t):
            return f"{random.choice(['Heads', 'Tails'])}, {self.cfg.user_title}."
        m = re.search(r"\broll (?:a |an )?(?:(\d+)-sided )?(?:dice|die|d(\d+))\b", t)
        if m:
            sides = int(m.group(1) or m.group(2) or 6)
            sides = max(2, min(1000, sides))
            return f"You rolled a {random.randint(1, sides)}, {self.cfg.user_title}."
        m = re.search(r"\b(?:random number|pick a number)\b.*?(\d+)\D+(\d+)", t)
        if m:
            lo, hi = sorted((int(m.group(1)), int(m.group(2))))
            return f"{random.randint(lo, hi)}, {self.cfg.user_title}."
        if re.search(r"\b(random number|pick a number)\b", t):
            return f"{random.randint(1, 100)}, {self.cfg.user_title}."
        if re.search(r"\b(heads or tails)\b", t):
            return f"{random.choice(['Heads', 'Tails'])}, {self.cfg.user_title}."
        return None

    # ── spelling ────────────────────────────────────────────────
    def _spell(self, t, original):
        m = re.search(r"\bspell\s+(?:the word\s+)?([a-zA-Z]+)", original, re.IGNORECASE)
        if not m:
            return None
        word = m.group(1)
        # "spell check/checker this", "spell it correctly" etc. aren't spell-out requests
        if word.lower() in {"check", "checker", "checking", "correctly",
                            "it", "that", "this", "out", "me"}:
            return None
        return f"{word} is spelled: " + "-".join(word.upper())

    # ── empty recycle bin ───────────────────────────────────────
    def _recycle(self, t, _):
        if not re.search(r"\bempty (the )?(recycle bin|recycling bin|trash|bin)\b", t):
            return None
        try:
            subprocess.Popen(["powershell", "-NoProfile", "-Command",
                             "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            return f"I couldn't empty the recycle bin — {e}."
        return f"Recycle bin emptied, {self.cfg.user_title}."

    # ── agentic dispatch to Claude Code ─────────────────────────
    def _agentic(self, t, original):
        if not self.cfg.allow_agentic:
            return None
        # Only fire on an UNAMBIGUOUS agentic command. The bare word "engineer"
        # used to match here, so "what does a software engineer do" hijacked the
        # turn into a 10-minute Claude Code subprocess — hence "engineer" is now
        # accepted ONLY as an imperative at the very start of the utterance.
        m = (re.search(r"\b(?:run (?:a )?task|do a task|code task)[:\s]+(.+)",
                       original, re.IGNORECASE)
             or re.match(r"\s*engineer\s+(.+)", original, re.IGNORECASE))
        if not m:
            return None
        task = m.group(1).strip()
        u = self.cfg.user_title
        ack = (f"On it, {u}. I'll work on that in the background and let you know the "
               f"moment it's done — carry on.")
        if self._run_mission is not None:
            self._run_mission(f"Task: {task[:46]}",
                              lambda mission: self._agentic_worker(mission, task), tag="AGENT")
            return ack
        # degraded path: no background runner → run inline (blocks the turn)
        self.say(ack)
        self._agentic_worker(_InlineMission(self.say), task)
        return ""

    # public entry so the assistant's brain-delegation can drive the agent directly
    def run_agentic(self, mission, task: str) -> None:
        self._agentic_worker(mission, task)

    def _agentic_worker(self, mission, task: str) -> None:
        """Drive a Claude agent on a real task, streaming progress to the mission panel
        and speaking only the final summary.

        Two safety tiers (see config.allow_full_control):
          • default — sandboxed: runs in JARVIS's own ``workspace`` folder under
            ``acceptEdits`` (it can write code/files and use web tools there, but has no
            unrestricted, no-approval access to the wider system).
          • full control — runs from your home dir with ``--dangerously-skip-permissions``
            (can do anything, no prompts). Only when you explicitly opt in.
        """
        u = self.cfg.user_title
        full = getattr(self.cfg, "allow_full_control", False)
        if full:
            cwd = str(Path.home())
            cmd = ["-p", "--output-format", "json", "--dangerously-skip-permissions", task]
        else:
            workspace = ROOT / "workspace"
            workspace.mkdir(exist_ok=True)
            cwd = str(workspace)
            cmd = ["-p", "--output-format", "json", "--permission-mode", "acceptEdits", task]
        mission.step("Bringing the agent online" + ("  (full control)" if full else ""))
        # only ONE heavy Claude agent runs at a time — several at once peg the CPU and
        # make JARVIS sluggish to answer. Extra tasks queue here rather than pile on.
        if not self._agent_sem.acquire(blocking=False):
            mission.step("Queued — finishing the current task first")
            self._agent_sem.acquire()
        try:
            import shutil as _sh
            claude = _sh.which("claude") or "claude"
            mission.step("Working on the task")
            proc = subprocess.run(
                [claude, *cmd],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=900, cwd=cwd, creationflags=_LOWPRIO,   # below-normal priority
            )
            import json as _json
            try:
                res = _json.loads(proc.stdout).get("result", "")
            except Exception:
                res = (proc.stdout or "").strip()
            summary = res.split("\n")[0][:240] if res else "it's done"
            mission.step("Task complete")
            mission.finish("done", tag="DONE")
            mission.speak(f"Done, {u}. {summary}")
        except subprocess.TimeoutExpired:
            mission.error("the task took too long")
            mission.speak(f"That one took longer than I allowed, {u} — I've stopped it for now.")
        except Exception as e:  # noqa: BLE001
            print(f"[skills] agentic error: {e}")
            mission.error("the task ran into trouble")
            mission.speak(f"That task ran into trouble, {u}. Have a look when you get a moment.")
        finally:
            self._agent_sem.release()

    # ── pleasantries ────────────────────────────────────────────
    def _pleasantries(self, t, _):
        if re.fullmatch(r"(thanks|thank you|cheers|nice|great|perfect|awesome)( jarvis)?", t):
            return f"My pleasure, {self.cfg.user_title}."
        if re.fullmatch(r"(hi|hey|hello|good morning|good evening|good afternoon)( jarvis)?", t):
            return f"Hello, {self.cfg.user_title}. How can I help?"
        return None
