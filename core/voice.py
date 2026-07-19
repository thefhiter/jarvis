"""Ears — microphone capture, wake-word detection and speech-to-text.

A single 16 kHz mono input stream stays open for the life of the app. While
idle it feeds 80 ms frames to openWakeWord ("hey jarvis"); once triggered it
records the following command until the user stops speaking (energy-based
end-pointing) and transcribes it with faster-whisper. During capture it streams
a live spectrum to the HUD so the ring reacts to your voice.
"""
from __future__ import annotations

import threading
import numpy as np
import sounddevice as sd

BLOCK = 1280          # 80 ms @ 16 kHz — required frame size for openWakeWord


class Ears:
    def __init__(self, cfg, hud):
        self.cfg = cfg
        self.hud = hud
        self.sr = cfg.sample_rate
        self._stream = None
        self._model = None      # faster-whisper
        self._oww = None        # openWakeWord

    # ── heavy init (models) ─────────────────────────────────────
    def load(self) -> None:
        from faster_whisper import WhisperModel
        self._model = WhisperModel(
            self.cfg.whisper_model, device="cpu", compute_type=self.cfg.whisper_compute
        )
        if self.cfg.enable_wakeword:
            self._load_wake()

    def reload_whisper(self, model_name: str) -> bool:
        """Swap the STT model at runtime (downloads it on first use). Returns ok."""
        from faster_whisper import WhisperModel
        try:
            self._model = WhisperModel(model_name, device="cpu", compute_type=self.cfg.whisper_compute)
            self.cfg.whisper_model = model_name
            return True
        except Exception as e:
            print(f"[ears] could not load whisper '{model_name}': {e}")
            return False

    def _load_wake(self) -> None:
        try:
            from openwakeword.model import Model as OWW
            self._oww = OWW(wakeword_models=[self.cfg.wakeword], inference_framework="onnx")
        except Exception as e:
            print(f"[ears] wake-word disabled ({e}); using click/hotkey to talk")
            self._oww = None

    def open_stream(self) -> None:
        self._stream = sd.InputStream(
            samplerate=self.sr, channels=1, dtype="float32",
            blocksize=BLOCK, device=self.cfg.input_device,
        )
        self._stream.start()

    def close(self) -> None:
        if self._stream:
            try:
                self._stream.stop(); self._stream.close()
            except Exception:
                pass

    def _read(self) -> np.ndarray:
        data, _ = self._stream.read(BLOCK)
        return data[:, 0].copy()

    # ── wake word ───────────────────────────────────────────────
    def wait_for_wake(self, stop: threading.Event, trigger: threading.Event | None = None) -> bool:
        """Block until 'hey jarvis' is heard OR the manual trigger fires.

        Returns True to start listening, False if stop is set.
        """
        if self._oww:
            self._oww.reset()
        while not stop.is_set():
            if trigger is not None and trigger.is_set():
                trigger.clear()
                return True
            block = self._read()          # keeps the mic warm even with no wake engine
            if self._oww:
                scores = self._oww.predict(block)
                if scores.get(self.cfg.wakeword, 0.0) >= self.cfg.wakeword_threshold:
                    return True
        return False

    # ── command capture + STT ───────────────────────────────────
    def capture_command(self, stop: threading.Event) -> str:
        frames: list[np.ndarray] = []
        heard = False
        trailing_silence = 0.0
        elapsed = 0.0
        frame_ms = BLOCK / self.sr * 1000.0
        silence_limit = self.cfg.silence_ms
        # a little grace at the start so we don't cut off a slow starter
        start_grace = 1600.0

        while not stop.is_set():
            block = self._read()
            frames.append(block)
            rms = float(np.sqrt(np.mean(block ** 2)) + 1e-9)
            elapsed += frame_ms

            # drive the HUD listening visuals
            self.hud.spectrum(self._spectrum(block), level=min(1.0, rms * 7))

            speaking = rms > self.cfg.energy_threshold
            if speaking:
                heard = True
                trailing_silence = 0.0
            else:
                trailing_silence += frame_ms

            if heard and trailing_silence >= silence_limit:
                break
            if not heard and elapsed >= start_grace:
                break  # user never spoke
            if elapsed >= self.cfg.max_command_ms:
                break

        if not heard:
            return ""
        audio = np.concatenate(frames).astype(np.float32)
        return self.transcribe(audio)

    def transcribe(self, audio: np.ndarray) -> str:
        segments, _ = self._model.transcribe(
            audio, language="en", vad_filter=True, beam_size=1,
        )
        return " ".join(s.text for s in segments).strip()

    # ── helpers ─────────────────────────────────────────────────
    @staticmethod
    def _spectrum(block: np.ndarray, nbins: int = 32) -> list[float]:
        win = block * np.hanning(len(block))
        mag = np.abs(np.fft.rfft(win))
        # voice energy sits low; take the lower half and bucket it
        mag = mag[: len(mag) // 2 + 1]
        if len(mag) < nbins:
            mag = np.pad(mag, (0, nbins - len(mag)))
        buckets = np.array_split(mag, nbins)
        vals = np.array([b.mean() for b in buckets])
        vals = np.log1p(vals)
        m = vals.max()
        if m > 0:
            vals = vals / m
        return [round(float(v), 3) for v in vals]
