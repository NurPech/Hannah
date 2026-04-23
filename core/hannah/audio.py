import base64
import io
import struct
import wave
import numpy as np


def decode(b64_payload: str | bytes, cfg: dict) -> np.ndarray:
    """
    Dekodiert base64-kodierten Audio-Payload (raw PCM oder WAV) zu
    einem float32-Numpy-Array normalisiert auf [-1.0, 1.0].
    """
    raw = base64.b64decode(b64_payload)
    fmt = cfg.get("format", "auto")

    if fmt == "wav" or (fmt == "auto" and _is_wav(raw)):
        return _from_wav(raw)

    return _from_raw_pcm(raw, cfg)


def _is_wav(data: bytes) -> bool:
    return len(data) >= 4 and data[:4] == b"RIFF"


def _from_wav(data: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(data), "rb") as wf:
        sample_width = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    return _bytes_to_float32(frames, sample_width)


def from_raw_pcm(data: bytes, cfg: dict) -> np.ndarray:
    """
    Konvertiert raw PCM-Bytes (ohne Header, wie sie per UDP ankommen)
    direkt zu einem float32-Numpy-Array normalisiert auf [-1.0, 1.0].
    """
    sample_width = cfg.get("sample_width", 2)
    return _bytes_to_float32(data, sample_width)


def _from_raw_pcm(data: bytes, cfg: dict) -> np.ndarray:
    return from_raw_pcm(data, cfg)


def _bytes_to_float32(data: bytes, sample_width: int) -> np.ndarray:
    if sample_width == 2:
        samples = np.frombuffer(data, dtype=np.int16)
        return samples.astype(np.float32) / 32768.0
    elif sample_width == 4:
        samples = np.frombuffer(data, dtype=np.int32)
        return samples.astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Nicht unterstützte sample_width: {sample_width}")
