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


class ClapDetector:
    """Detects a clap (or double clap) from the transient shape of each 80 ms block.

    A clap is a short, loud, broadband burst → high peak, high crest factor
    (peak-to-RMS), well above the ambient floor. Requiring two claps within a
    short window makes it robust against stray loud noises.
    """
    def __init__(self, cfg):
        self.cfg = cfg
        self.bg = 0.02              # ambient RMS (slow EMA)
        self.frame = 0
        self.last_clap = -100
        self.refractory_until = 0

    def _is_clap(self, block) -> bool:
        peak = float(np.max(np.abs(block)))
        rms = float(np.sqrt(np.mean(block ** 2)) + 1e-9)
        crest = peak / rms
        return (peak > self.cfg.clap_sensitivity) and (crest > 3.2) and (peak > self.bg * 6)

    def feed(self, block) -> bool:
        self.frame += 1
        rms = float(np.sqrt(np.mean(block ** 2)) + 1e-9)
        in_refractory = self.frame < self.refractory_until
        clap = (not in_refractory) and self._is_clap(block)
        if not clap:
            # Learn the noise floor ONLY from genuinely quiet, non-refractory frames.
            # The rms < bg*3 clamp keeps a loud clap tail, speech, or music from being
            # averaged in — otherwise bg ratchets up and peak > bg*6 stops firing.
            if not in_refractory and rms < self.bg * 3:
                self.bg = 0.95 * self.bg + 0.05 * rms
            return False
        self.refractory_until = self.frame + 4          # ignore this clap's ~320 ms tail
        if self.cfg.clap_count <= 1:
            self.last_clap = self.frame
            return True
        gap = self.frame - self.last_clap               # frames since the previous clap
        self.last_clap = self.frame
        # refractory enforces gap >= 4, so this is a genuine second clap ~320–720 ms later
        if 2 <= gap <= 9:
            self.last_clap = -100
            return True
        return False


class Ears:
    def __init__(self, cfg, hud):
        self.cfg = cfg
        self.hud = hud
        self.sr = cfg.sample_rate
        self._stream = None
        self._model = None      # faster-whisper
        self._oww = None        # openWakeWord
        self._clap = ClapDetector(cfg)
        self._stt_lock = threading.Lock()      # one whisper decode at a time (partials + final)
        self._partial_busy = threading.Event() # a live partial transcription is running

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
            if self.cfg.enable_clap and self._clap.feed(block):
                self.hud.send({"type": "pulse"})   # shockwave cue on the HUD
                return True
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
        max_rms = 0.0
        last_partial = 0.0
        frame_ms = BLOCK / self.sr * 1000.0
        silence_limit = self.cfg.silence_ms
        start_grace = float(getattr(self.cfg, "start_grace_ms", 2200))
        live = getattr(self.cfg, "live_transcribe", True)
        # show a live "listening…" cue so the user KNOWS the mic is open
        self.hud.user_partial("")

        while not stop.is_set():
            block = self._read()
            frames.append(block)
            rms = float(np.sqrt(np.mean(block ** 2)) + 1e-9)
            max_rms = max(max_rms, rms)
            elapsed += frame_ms

            # drive the HUD listening visuals
            self.hud.spectrum(self._spectrum(block), level=min(1.0, rms * 7))

            speaking = rms > self.cfg.energy_threshold
            if speaking:
                heard = True
                trailing_silence = 0.0
            else:
                trailing_silence += frame_ms

            # live transcription: every ~0.6 s, transcribe what we have so far on a
            # worker thread and show it, so the user SEES what JARVIS is hearing.
            if live and heard and (elapsed - last_partial) >= 600 \
                    and not self._partial_busy.is_set():
                last_partial = elapsed
                self._emit_partial(list(frames))

            if heard and trailing_silence >= silence_limit:
                break
            if not heard and elapsed >= start_grace:
                break  # user never spoke
            if elapsed >= self.cfg.max_command_ms:
                break

        # Forgiving capture: transcribe if we clearly heard speech OR there was any
        # audible energy at all (a quiet mic that never crossed the threshold) — the
        # whisper VAD drops true silence, so a dead-quiet room still returns "".
        if not frames or (not heard and max_rms < self.cfg.energy_threshold * 0.5):
            self.hud.user("")          # clear the "listening…" cue
            return ""
        audio = np.concatenate(frames).astype(np.float32)
        text = self.transcribe(audio)
        if not text:
            self.hud.user("")
        return text

    def _emit_partial(self, frames: list) -> None:
        """Transcribe the audio-so-far on a background thread and push it to the HUD
        as a live 'this is what I'm hearing' subtitle. At most one runs at a time."""
        if self._partial_busy.is_set():
            return
        self._partial_busy.set()

        def work():
            try:
                audio = np.concatenate(frames).astype(np.float32)
                txt = self.transcribe(audio)
                if txt:
                    self.hud.user_partial(txt)
            except Exception as e:  # noqa: BLE001 — a partial must never break capture
                print(f"[ears] partial transcription: {e}")
            finally:
                self._partial_busy.clear()

        threading.Thread(target=work, name="stt-partial", daemon=True).start()

    def transcribe(self, audio: np.ndarray) -> str:
        # Speed + robustness: greedy (beam_size=1), no cross-segment conditioning
        # (avoids the whisper repetition-loop that turns a short command into a wall
        # of repeated words), temperature 0 for determinism. vad_filter trims the
        # leading/trailing silence we captured, so decode has less audio to chew on.
        # The lock serialises the (possibly concurrent) live-partial and final decodes.
        with self._stt_lock:
            audio = self._normalize_gain(audio)   # boost a quiet mic so whisper can hear it
            segments, _ = self._model.transcribe(
                audio, language="en", beam_size=1, vad_filter=True,
                temperature=0.0, condition_on_previous_text=False,
                # bias the decoder toward short spoken commands and the name "Jarvis",
                # which noticeably improves recognition of terse, real-world phrasing.
                initial_prompt="A short spoken command to a desktop assistant named Jarvis.",
            )
            return " ".join(s.text for s in segments).strip()

    # ── helpers ─────────────────────────────────────────────────
    @staticmethod
    def _normalize_gain(audio: np.ndarray) -> np.ndarray:
        """Scale up a quiet recording so whisper hears it clearly (a laptop far-field
        mic array is often well below line level). Only boosts genuinely quiet-but-
        present audio: pure silence and already-loud audio pass through untouched, and
        gain is capped so we don't blow up the noise floor."""
        if audio is None or len(audio) == 0:
            return audio
        peak = float(np.max(np.abs(audio)))
        if 0.003 < peak < 0.3:
            gain = min(0.3 / peak, 20.0)
            audio = np.clip(audio * gain, -1.0, 1.0)
        return audio.astype(np.float32)

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
