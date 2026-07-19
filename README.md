<div align="center">

# J.A.R.V.I.S.

### A cinematic, voice-controlled AI assistant for Windows

*Just A Rather Very Intelligent System* — local wake word, local speech-to-text,
a neural British-butler voice, an Iron-Man-style holographic HUD, and real control
over your PC. Powered by your Claude subscription (with free local/API fallbacks).

</div>

---

## What it does

Say **“Hey Jarvis”** (or click the arc reactor / press **Space**), speak, and JARVIS
answers out loud while a glowing arc-reactor HUD reacts to the conversation in real time.

- 🎙 **Wake word** — “hey jarvis”, detected **locally** (openWakeWord, no cloud, no key).
- 🧠 **Speech-to-text** — **local** faster-whisper (`base`, int8). Nothing leaves your PC.
- 💬 **The brain** — routes to the **Claude Code CLI** (your subscription, no API key), with
  **Groq** and **Ollama** as pluggable fallbacks.
- 🗣 **Voice** — premium neural **edge-tts** (`en-GB-RyanNeural`) with an offline **SAPI5** fallback.
- 🌀 **HUD** — a frameless, always-on-top holographic arc reactor: rotating rings, an
  audio-reactive waveform, live subtitles, telemetry and a boot sequence. Pure canvas, no CDNs.
- ⚡ **Powers** — open apps & sites, web/YouTube search, volume, brightness, screenshots,
  system & battery status, notes, timers, weather, clipboard, typing, window control,
  lock/sleep/shutdown, and an agentic “run a task” mode that dispatches to Claude Code.

## Powers (things you can say)

| Say… | It does |
|------|---------|
| “what time / date is it” | tells the time / date |
| “open notepad / chrome / youtube / spotify …” | launches apps & websites |
| “search for quantum computing” | Google search in your browser |
| “play daft punk on youtube” | YouTube search/play |
| “set volume to 40”, “volume up”, “mute” | system volume (pycaw) |
| “set brightness to 70”, “brighter / dimmer” | display brightness |
| “take a screenshot” | saves to `Pictures/Jarvis` |
| “system status”, “battery status” | CPU / memory / battery |
| “minimize everything”, “maximize”, “switch window” | window control |
| “type hello world” | types for you |
| “copy X to the clipboard”, “read my clipboard” | clipboard |
| “make a note buy milk”, “read my notes” | notes → `notes.txt` |
| “set a timer for 5 minutes” | spoken timer alert |
| “what’s the weather [in Tunis]” | live weather (wttr.in) |
| “lock / sleep / shut down / restart the computer” | power (guarded) |
| “run a task: create a python script that …” | agentic Claude Code in `workspace/` |
| “who are you”, “what can you do” | JARVIS introduces itself |
| anything else | answered conversationally by the brain |

Say **“goodbye Jarvis”** to power down.

## Quick start

```bat
:: 1. one-time setup (build venv, install deps, download voice models)
setup.bat

:: 2. launch
start.bat
```

Then just say **“Hey Jarvis”**. `start-debug.bat` runs the same thing with a visible
console if you want to watch the logs.

> Already set up on this machine — the `.venv` and models are in place, so you can run
> `start.bat` directly. To preview just the HUD (no mic), run
> `.venv\Scripts\python demo_hud.py` and open the printed URL.

## Configuration

All settings live in **`config.json`** (created on first run). Highlights:

| Key | Default | Meaning |
|-----|---------|---------|
| `brain` | `"claude"` | `claude` \| `groq` \| `ollama` |
| `groq_api_key` | `""` | free key from console.groq.com (or set `GROQ_API_KEY` env) |
| `ollama_model` | `"llama3.2"` | model name if using local Ollama |
| `whisper_model` | `"base"` | `tiny` \| `base` \| `small` \| `medium` |
| `wakeword_threshold` | `0.5` | lower = more sensitive |
| `tts_engine` | `"edge"` | `edge` (neural, online) \| `sapi` (offline) |
| `tts_voice` | `"en-GB-RyanNeural"` | any edge-tts voice |
| `user_title` | `"sir"` | how JARVIS addresses you |
| `hud_fullscreen` | `false` | launch the HUD fullscreen |
| `allow_shutdown` | `true` | permit lock/sleep/shutdown commands |
| `allow_agentic` | `true` | permit “run a task” → Claude Code |
| `weather_city` | `""` | default city (blank = auto by IP) |

## Architecture

```
run.py                 entry — starts HUD bridge, launches WebView2 window, runs the loop
core/
  config.py            settings (config.json)
  hud.py               WebSocket server → streams state to the HUD
  voice.py             mic stream · openWakeWord · faster-whisper STT
  brain.py             Claude CLI / Groq / Ollama (pluggable)
  speech.py            edge-tts → ffmpeg → sounddevice, live level/spectrum → HUD; SAPI fallback
  skills.py            deterministic “powers” (matched before the brain)
  assistant.py         orchestrator: wake → listen → route → speak
ui/jarvis.html         the cinematic HUD (self-contained canvas, no CDNs)
demo_hud.py            preview the HUD without a microphone
setup_models.py        one-time model download
tests/smoke.py         headless verification (skills + brain)
```

**Flow:** `idle → (wake / click / Space) → listening → capture → thinking →
skills.handle() or brain.ask() → speaking → idle`. Deterministic commands run
instantly; only open-ended questions incur an LLM round-trip.

## Notes & troubleshooting

- **TLS-inspecting antivirus/proxy?** Handled — the app uses the Windows certificate
  store via `truststore`, so downloads and edge-tts work.
- **No neural voice?** edge-tts needs internet; it falls back to the offline Windows
  voice automatically. Keep `edge-tts` up to date (`pip install -U edge-tts`) if you ever
  see a `403`.
- **Wake word too sensitive / not firing?** Tune `wakeword_threshold` in `config.json`.
- **Verified on this machine:** skills matcher (23/23), Claude brain, live weather,
  edge-tts → ffmpeg → whisper chain, openWakeWord, and the HUD render + WebSocket bridge.

## Tech

Python 3.11 · faster-whisper · openWakeWord · edge-tts · pyttsx3 · pywebview (WebView2) ·
websockets · sounddevice · ffmpeg · pycaw · psutil · Claude Code CLI.

<div align="center"><sub>Built for the arc-reactor life. “Sometimes you gotta run before you can walk.”</sub></div>
