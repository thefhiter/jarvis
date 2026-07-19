"""Powers — deterministic control over the PC.

Every command is matched here FIRST (fast, no LLM round-trip). Anything that
doesn't match returns None and is escalated to the brain for a conversational
answer. Handlers return the sentence JARVIS should say back.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "notes.txt"

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


class Skills:
    def __init__(self, cfg, hud, say):
        self.cfg = cfg
        self.hud = hud
        self.say = say                # callable(text) -> speaks via the mouth
        self.should_exit = False

    # ── main dispatch ───────────────────────────────────────────
    def handle(self, text: str):
        t = text.lower().strip().rstrip(".!?")
        if not t:
            return None
        for fn in (
            self._exit, self._help, self._identity, self._time, self._date,
            self._open, self._search, self._youtube, self._volume, self._brightness,
            self._screenshot, self._system, self._battery, self._window,
            self._type_text, self._clipboard, self._note, self._timer,
            self._weather, self._power, self._agentic, self._pleasantries,
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
            return ("I can open apps and websites, search the web, play things on "
                    "YouTube, control volume and brightness, take screenshots, report "
                    "system status and battery, take notes, set timers, check the weather, "
                    "type for you, lock or shut down the machine, and answer anything else "
                    f"you ask, {self.cfg.user_title}.")
        return None

    def _identity(self, t, _):
        if re.search(r"\b(who are you|what is your name|what's your name|introduce yourself)\b", t):
            return (f"I am JARVIS — your just-a-rather-very-intelligent-system. "
                    f"At your service, {self.cfg.user_title}.")
        return None

    def _time(self, t, _):
        if re.search(r"\b(what('?s| is) the time|what time is it|the time)\b", t):
            return f"It is {datetime.now().strftime('%-I:%M %p') if os.name != 'nt' else datetime.now().strftime('%I:%M %p').lstrip('0')}."
        return None

    def _date(self, t, _):
        if re.search(r"\b(what('?s| is) the date|what day is it|today's date)\b", t):
            return f"Today is {datetime.now().strftime('%A, %B %d, %Y')}."
        return None

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

    # ── timers ──────────────────────────────────────────────────
    def _timer(self, t, _):
        m = re.search(r"\b(?:set a |start a )?timer for (\d+)\s*(second|seconds|minute|minutes|hour|hours)\b", t)
        if not m:
            return None
        n = int(m.group(1)); unit = m.group(2)
        secs = n * (3600 if "hour" in unit else 60 if "minute" in unit else 1)

        def fire():
            self.say(f"{self.cfg.user_title.capitalize()}, your timer for {n} {unit} is up.")
        threading.Timer(secs, fire).start()
        return f"Timer set for {n} {unit}, {self.cfg.user_title}."

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
