"""Mouth — text-to-speech with a live audio-reactive feed to the HUD.

Primary: edge-tts neural voice (en-GB-RyanNeural, the British-butler voice).
The MP3 is decoded to PCM with ffmpeg, played through sounddevice, and every
block's RMS + spectrum is streamed to the HUD so the arc reactor pulses in time
with JARVIS's voice. Falls back to offline Windows SAPI5 (pyttsx3) if edge-tts
or the network is unavailable.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
import numpy as np
import sounddevice as sd

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
PLAY_SR = 22050
HOP = 1024


class Mouth:
    def __init__(self, cfg, hud):
        self.cfg = cfg
        self.hud = hud
        self._sapi = None
        self.stop = threading.Event()

    def interrupt(self) -> None:
        self.stop.set()

    # ── public ──────────────────────────────────────────────────
    def speak(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.stop.clear()
        engine = self.cfg.tts_engine
        ok = False
        if engine == "edge":
            ok = self._speak_edge(text)
        if not ok:
            self._speak_sapi(text)
        self.hud.level(0.0)

    # ── edge-tts (neural) ───────────────────────────────────────
    def _speak_edge(self, text: str) -> bool:
        try:
            mp3 = asyncio.run(self._edge_bytes(text))
            if not mp3:
                return False
            pcm = self._decode(mp3)
            if pcm is None or len(pcm) == 0:
                return False
            self._play(pcm)
            return True
        except Exception as e:
            print(f"[tts] edge failed ({e}); using offline voice")
            return False

    async def _edge_bytes(self, text: str) -> bytes:
        import edge_tts
        comm = edge_tts.Communicate(
            text, self.cfg.tts_voice, rate=self.cfg.tts_rate, pitch=self.cfg.tts_pitch
        )
        buf = bytearray()
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf += chunk["data"]
        return bytes(buf)

    def _decode(self, mp3: bytes):
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-f", "f32le", "-ac", "1", "-ar", str(PLAY_SR), "pipe:1"],
            input=mp3, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        return np.frombuffer(proc.stdout, dtype=np.float32)

    def _play(self, pcm: np.ndarray) -> None:
        with sd.OutputStream(samplerate=PLAY_SR, channels=1, dtype="float32",
                             device=self.cfg.output_device) as out:
            for i in range(0, len(pcm), HOP):
                if self.stop.is_set():
                    break
                block = pcm[i:i + HOP]
                out.write(block)
                rms = float(np.sqrt(np.mean(block ** 2)) + 1e-9)
                self.hud.spectrum(_spectrum(block), level=min(1.0, rms * 4.0))

    # ── offline SAPI5 fallback ──────────────────────────────────
    def _speak_sapi(self, text: str) -> None:
        try:
            if self._sapi is None:
                import pyttsx3
                self._sapi = pyttsx3.init()
                voices = self._sapi.getProperty("voices")
                male = next((v for v in voices if "david" in v.name.lower()
                             or getattr(v, "gender", "") == "male"), voices[0] if voices else None)
                if male:
                    self._sapi.setProperty("voice", male.id)
                self._sapi.setProperty("rate", 170)
            # pulse the HUD while it speaks (no per-sample data from SAPI)
            pulse_stop = threading.Event()
            t = threading.Thread(target=self._pulse, args=(pulse_stop,), daemon=True)
            t.start()
            self._sapi.say(text)
            self._sapi.runAndWait()
            pulse_stop.set()
        except Exception as e:
            print(f"[tts] offline voice failed too: {e}")

    def _pulse(self, stop: threading.Event) -> None:
        phase = 0.0
        while not stop.is_set():
            phase += 0.5
            lvl = 0.35 + 0.35 * abs(np.sin(phase))
            self.hud.level(lvl)
            sd.sleep(60)


def _spectrum(block: np.ndarray, nbins: int = 32) -> list[float]:
    n = len(block)
    if n < 8:
        return [0.0] * nbins
    win = block * np.hanning(n)
    mag = np.abs(np.fft.rfft(win))
    mag = mag[: len(mag) // 2 + 1]
    if len(mag) < nbins:
        mag = np.pad(mag, (0, nbins - len(mag)))
    buckets = np.array_split(mag, nbins)
    vals = np.log1p(np.array([b.mean() for b in buckets]))
    m = vals.max()
    if m > 0:
        vals = vals / m
    return [round(float(v), 3) for v in vals]
