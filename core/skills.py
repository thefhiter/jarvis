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


class Skills:
    def __init__(self, cfg, hud, say, last_reply=None, forget=None):
        self.cfg = cfg
        self.hud = hud
        self.say = say                # callable(text) -> speaks via the mouth
        self._last_reply = last_reply or (lambda: "")   # getter for JARVIS's last line
        self._forget = forget or (lambda: None)         # clears brain conversation memory
        self.should_exit = False
        self._reminders: list[dict] = []   # active timers/reminders/alarms
        self._rid = 0
        self._rlock = threading.Lock()     # registry touched from Timer threads too
        self._load_persisted()             # restore reminders/alarms from a prior run

    # ── main dispatch ───────────────────────────────────────────
    def handle(self, text: str):
        t = text.lower().strip().rstrip(".!?")
        if not t:
            return None
        for fn in (
            self._exit, self._help, self._identity, self._time,
            self._datecalc, self._date, self._repeat, self._reset, self._ipaddr,
            self._media, self._open, self._close_app, self._search, self._youtube,
            self._volume, self._brightness, self._screenshot, self._system,
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
                    "and I'll happily just chat and answer whatever else is on your mind."
                    .format(u=self.cfg.user_title))
        return None

    def _identity(self, t, _):
        if re.search(r"\b(who are you|what is your name|what's your name|introduce yourself)\b", t):
            return (f"I am JARVIS — your just-a-rather-very-intelligent-system. "
                    f"At your service, {self.cfg.user_title}.")
        return None

    def _repeat(self, t, _):
        if re.search(r"\b(repeat that|say (that|it) again|what did you say|come again|"
                     r"can you repeat that|repeat what you said)\b", t):
            last = (self._last_reply() or "").strip()
            if not last:
                return f"I haven't said anything yet, {self.cfg.user_title}."
            return last
        return None

    def _reset(self, t, _):
        if re.search(r"\b(forget (our|the|this) (conversation|chat|context)|"
                     r"reset (our|the|your)? ?(conversation|chat|context|memory)|"
                     r"clear (your )?memory|start over|new conversation|"
                     r"forget what we (talked|said|discussed))\b", t):
            self._forget()
            return f"Done, {self.cfg.user_title}. I've cleared our conversation — a clean slate."
        return None

    def _time(self, t, _):
        if re.search(r"\b(what('?s| is) the time|what time is it|the time)\b", t):
            return f"It is {datetime.now().strftime('%-I:%M %p') if os.name != 'nt' else datetime.now().strftime('%I:%M %p').lstrip('0')}."
        return None

    def _date(self, t, _):
        if re.search(r"\b(what('?s| is) the date|what day is it|today's date)\b", t):
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
        # last resort: hand it to Windows' start
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            return f"Opening {target}."
        except Exception:
            self._launch(f"start {target}")
            return f"Attempting to open {target}, {self.cfg.user_title}."

    def _launch(self, cmd: str):
        if cmd.startswith("start "):
            subprocess.Popen(["cmd", "/c", cmd], shell=False)
        else:
            try:
                subprocess.Popen(cmd, shell=True)
            except Exception:
                subprocess.Popen(["cmd", "/c", "start", "", cmd])

    def _search(self, t, original):
        m = re.search(r"\b(?:search(?: for)?|google|look up)\s+(.+)", t)
        if not m:
            return None
        if "youtube" in t:      # handled by _youtube
            return None
        q = m.group(1).strip()
        webbrowser.open("https://www.google.com/search?q=" + requests.utils.quote(q))
        return f"Here are the results for {q}, {self.cfg.user_title}."

    def _youtube(self, t, original):
        m = re.search(r"\b(?:play|search youtube for|find on youtube|youtube)\s+(.+?)(?:\s+on youtube)?$", t)
        if not m or ("youtube" not in t and "play" not in t):
            return None
        q = re.sub(r"\bon youtube\b", "", m.group(1)).strip()
        if not q:
            return None
        webbrowser.open("https://www.youtube.com/results?search_query=" + requests.utils.quote(q))
        return f"Playing {q} on YouTube, {self.cfg.user_title}."

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
        if not re.search(r"\b(system status|how('?s| is) the (system|pc|computer)|cpu|memory|ram|status report)\b", t):
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
    def _schedule(self, secs: float, spoken: str, label: str, kind: str) -> int:
        """Fire ``spoken`` via the mouth after ``secs`` seconds; track it so it can
        be listed or cancelled. Returns the reminder id. The registry is touched
        from Timer callback threads too, so all access is guarded by _rlock."""
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
        for e in data if isinstance(data, list) else []:
            try:
                due = datetime.fromisoformat(e["due"])
            except Exception:
                continue
            if due <= now:
                continue
            self._schedule((due - now).total_seconds(), e.get("spoken", ""),
                           e.get("label", "reminder"), e.get("kind", "reminder"))

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
        m = re.search(r"\b(?:run (?:a )?task|engineer|do a task|code task)[:\s]+(.+)", original, re.IGNORECASE)
        if not m:
            return None
        task = m.group(1).strip()
        workspace = ROOT / "workspace"
        workspace.mkdir(exist_ok=True)
        self.say(f"On it, {self.cfg.user_title}. Working on that now.")
        try:
            import shutil as _sh
            claude = _sh.which("claude") or "claude"
            proc = subprocess.run(
                [claude, "-p", "--permission-mode", "acceptEdits", "--output-format", "json", task],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=600, cwd=str(workspace),
            )
            import json as _json
            try:
                res = _json.loads(proc.stdout).get("result", "")
            except Exception:
                res = (proc.stdout or "").strip()
            summary = res.split("\n")[0][:220] if res else "the task is complete"
            return f"Task complete, {self.cfg.user_title}. {summary}"
        except Exception as e:
            return f"The task ran into trouble: {e}"

    # ── pleasantries ────────────────────────────────────────────
    def _pleasantries(self, t, _):
        if re.fullmatch(r"(thanks|thank you|cheers|nice|great|perfect|awesome)( jarvis)?", t):
            return f"My pleasure, {self.cfg.user_title}."
        if re.fullmatch(r"(hi|hey|hello|good morning|good evening|good afternoon)( jarvis)?", t):
            return f"Hello, {self.cfg.user_title}. How can I help?"
        return None
