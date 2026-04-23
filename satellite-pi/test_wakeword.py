#!/usr/bin/env python3
"""
Testet Wake-Word-Erkennung live — zeigt Scores in Echtzeit.
Sprich das Wake-Word und beobachte ob der Score über den Schwellwert steigt.

Verwendung:
  python satelite/test_wakeword.py --mic 127 --sample-rate 44100
  python satelite/test_wakeword.py --mic 127 --sample-rate 44100 --model hey_jarvis_v0.1.onnx
"""

import argparse
import sys
import numpy as np
import pyaudio
from openwakeword.model import Model as WakeWordModel

OWW_RATE  = 16000
OWW_CHUNK = 1280


def resample(data: np.ndarray, src_rate: int) -> np.ndarray:
    if src_rate == OWW_RATE:
        return data
    target_len = int(len(data) * OWW_RATE / src_rate)
    return np.interp(
        np.linspace(0, len(data), target_len),
        np.arange(len(data)),
        data,
    ).astype(np.int16)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mic",         default=None,  type=int)
    parser.add_argument("--sample-rate", default=44100, type=int)
    parser.add_argument("--model",       default="",    help="Modellname oder Pfad (leer = alle)")
    parser.add_argument("--threshold",   default=0.5,   type=float)
    args = parser.parse_args()

    if args.model:
        oww = WakeWordModel(wakeword_models=[args.model], inference_framework="onnx")
    else:
        oww = WakeWordModel(inference_framework="onnx")

    print(f"Modelle geladen: {list(oww.models.keys())}")
    print(f"Mikrofon-Index: {args.mic}, Sample-Rate: {args.sample_rate}Hz")
    print(f"Schwellwert: {args.threshold}")
    print("─" * 50)
    print("Sprich das Wake-Word ... (CTRL+C zum Beenden)\n")

    pa = pyaudio.PyAudio()
    mic_chunk = int(OWW_CHUNK * args.sample_rate / OWW_RATE)
    stream = pa.open(
        rate=args.sample_rate,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        input_device_index=args.mic,
        frames_per_buffer=mic_chunk,
    )

    try:
        while True:
            pcm_bytes = stream.read(mic_chunk, exception_on_overflow=False)
            pcm = np.frombuffer(pcm_bytes, dtype=np.int16)
            pcm = resample(pcm, args.sample_rate)
            scores = oww.predict(pcm)

            # Nur anzeigen wenn irgendein Score > 0.1
            active = {k: v for k, v in scores.items() if v > 0.1}
            if active:
                bars = {k: f"{v:.3f} {'█' * int(v * 20)}" for k, v in active.items()}
                for name, bar in bars.items():
                    marker = " ← ERKANNT!" if scores[name] >= args.threshold else ""
                    print(f"  {name}: {bar}{marker}")

    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


if __name__ == "__main__":
    main()
