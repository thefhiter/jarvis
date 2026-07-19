"""One-time model download: openWakeWord ('hey jarvis') + faster-whisper base.

Run once after install:  .venv\\Scripts\\python setup_models.py
Uses the Windows certificate store so downloads work behind TLS-inspecting
antivirus/proxies.
"""
import truststore
truststore.inject_into_ssl()

import sys


def main():
    print("[setup] downloading openWakeWord models (hey_jarvis + feature extractors)...")
    try:
        import openwakeword.utils
        openwakeword.utils.download_models()
        print("[setup] openWakeWord models ready.")
    except Exception as e:
        print(f"[setup] openWakeWord download failed: {e}")

    print("[setup] downloading faster-whisper 'base' model (~140 MB, first run only)...")
    try:
        from faster_whisper import WhisperModel
        WhisperModel("base", device="cpu", compute_type="int8")
        print("[setup] whisper base model ready.")
    except Exception as e:
        print(f"[setup] whisper download failed: {e}")

    print("[setup] done.")


if __name__ == "__main__":
    sys.exit(main())
