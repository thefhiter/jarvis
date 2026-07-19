"""Configuration for J.A.R.V.I.S. — loaded from config.json at the project root.

Every tunable lives here so the assistant can be reconfigured without touching
code. Missing keys fall back to sensible defaults, and unknown keys are ignored.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"


@dataclass
class Config:
    # ── identity ────────────────────────────────────────────────
    user_title: str = "sir"            # how JARVIS addresses you
    assistant_name: str = "Jarvis"

    # ── brain (LLM) ─────────────────────────────────────────────
    # "claude"  -> uses the locally installed Claude Code CLI (your subscription)
    # "groq"    -> free Groq API (needs groq_api_key)
    # "ollama"  -> local Ollama server
    brain: str = "claude"
    claude_model: str = ""             # "" = CLI default; else e.g. "claude-sonnet-4-6"
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    ollama_model: str = "llama3.2"
    ollama_url: str = "http://localhost:11434"

    # ── voice input ─────────────────────────────────────────────
    enable_voice: bool = True          # False = keyboard-only (mic never opened)
    whisper_model: str = "base"        # tiny | base | small | medium
    whisper_compute: str = "int8"
    input_device: int | None = None    # None = system default mic
    sample_rate: int = 16000

    # ── wake word ───────────────────────────────────────────────
    enable_wakeword: bool = True
    wakeword: str = "hey_jarvis"
    wakeword_threshold: float = 0.5

    # endpointing: stop capturing a command after this much trailing silence
    silence_ms: int = 800
    max_command_ms: int = 12000
    energy_threshold: float = 0.012     # RMS floor that counts as "speech"

    # ── voice output (TTS) ──────────────────────────────────────
    tts_engine: str = "edge"           # "edge" (neural, online) | "sapi" (offline)
    tts_voice: str = "en-GB-RyanNeural"
    tts_rate: str = "+8%"
    tts_pitch: str = "+0Hz"
    output_device: int | None = None

    # ── HUD / network ───────────────────────────────────────────
    ws_host: str = "127.0.0.1"
    ws_port: int = 8765
    hud_fullscreen: bool = False
    hud_always_on_top: bool = True

    # ── powers ──────────────────────────────────────────────────
    allow_shutdown: bool = True        # allow lock/sleep/shutdown/restart commands
    allow_agentic: bool = True         # allow "jarvis, do <task>" -> Claude Code with tools
    weather_city: str = ""             # "" = auto-detect via IP

    def save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def load() -> "Config":
    cfg = Config()
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in data.items():
                if hasattr(cfg, k):
                    setattr(cfg, k, v)
        except Exception as e:  # pragma: no cover - defensive
            print(f"[config] failed to read config.json ({e}); using defaults")
    else:
        cfg.save()  # write a template on first run
    # allow env override for the Groq key
    cfg.groq_api_key = os.environ.get("GROQ_API_KEY", cfg.groq_api_key)
    return cfg
