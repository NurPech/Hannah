#!/usr/bin/env python3
"""
Testskript: Sendet eine WAV-Datei als raw PCM per UDP an einen Satelliten.
Damit lässt sich die Audio-Qualität unabhängig von TTS testen.

Verwendung:
  python send_wav.py <wav-datei> <satellit-ip> [--port 7775]

Beispiel:
  python send_wav.py test.wav 192.168.8.42
"""
import argparse
import io
import json
import socket
import sys
import wave

TYPE_CONTROL = 0x01
TYPE_TTS     = 0x03
CHUNK_SIZE   = 4000  # Bytes pro UDP-Paket (~125ms bei 16kHz 16-bit)


def send(wav_path: str, host: str, port: int):
    with wave.open(wav_path, "rb") as wf:
        channels    = wf.getnchannels()
        sampwidth   = wf.getsampwidth()
        framerate   = wf.getframerate()
        pcm         = wf.readframes(wf.getnframes())

    print(f"WAV: {channels}ch, {sampwidth*8}-bit, {framerate}Hz, {len(pcm)} Bytes PCM")

    if channels != 1 or sampwidth != 2 or framerate != 16000:
        print(
            f"WARNUNG: Satellit erwartet mono/16-bit/16kHz. "
            f"Diese Datei ist {channels}ch/{sampwidth*8}-bit/{framerate}Hz — "
            f"Wiedergabe könnte verzerrt klingen."
        )

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    addr = (host, port)

    # PCM in Chunks senden
    offset = 0
    chunks = 0
    while offset < len(pcm):
        chunk = pcm[offset:offset + CHUNK_SIZE]
        sock.sendto(bytes([TYPE_TTS]) + chunk, addr)
        offset += CHUNK_SIZE
        chunks += 1

    # tts_end senden (mit korrekter Sample-Rate der WAV-Datei)
    end_msg = bytes([TYPE_CONTROL]) + json.dumps({"type": "tts_end", "sample_rate": framerate}).encode()
    sock.sendto(end_msg, addr)
    sock.close()

    print(f"Gesendet: {chunks} Pakete → {host}:{port}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WAV per UDP an Satellit senden")
    parser.add_argument("wav",  help="Pfad zur WAV-Datei")
    parser.add_argument("host", help="IP-Adresse des Satelliten")
    parser.add_argument("--port", default=7775, type=int, help="UDP-Port (Standard: 7775)")
    args = parser.parse_args()
    send(args.wav, args.host, args.port)
