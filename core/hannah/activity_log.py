import logging
import os
import time
import wave

import numpy as np

from hannah.models.activity_log import ActivityLog
from hannah.utils.activity_db import get_activity_db

log = logging.getLogger(__name__)

_audio_dir = "activity_audio"


def configure(audio_dir: str) -> None:
    global _audio_dir
    _audio_dir = audio_dir or "activity_audio"


def save_audio_wav(audio_array: np.ndarray, sample_rate: int, stem: str) -> str:
    """Schreibt audio_array als WAV, analog stt.py::_to_wav(). Legt _audio_dir bei
    Bedarf selbst an — externes NFS-Mount wird administrativ vorab bereitgestellt."""
    os.makedirs(_audio_dir, exist_ok=True)
    path = os.path.join(_audio_dir, f"{stem}.wav")
    pcm = (audio_array * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return path


def log_activity(
    *,
    channel_type: str,
    channel_id: str = "",
    user_id: str = "",
    raw_text: str = "",
    intent=None,
    answer_text: str = "",
    audio_array: "np.ndarray | None" = None,
    sample_rate: int = 16000,
) -> None:
    """Best-effort: DB nicht konfiguriert oder down → loggt höchstens eine Warning,
    wirft nie. Monitoring darf die Sprachpipeline nie blockieren/stören."""
    db = get_activity_db()
    if db is None:
        return

    try:
        audio_path = ""
        if audio_array is not None:
            stem = f"{int(time.time() * 1000)}_{channel_type}"
            audio_path = save_audio_wav(audio_array, sample_rate, stem)

        ActivityLog.create(
            db,
            channel_type=channel_type,
            channel_id=channel_id or None,
            user_id=user_id or None,
            raw_text=raw_text,
            intent_name=(intent.name if intent else None),
            intent_meta=(intent.to_dict() if intent else {}),
            answer_text=answer_text,
            audio_path=audio_path or None,
        )
    except Exception as e:
        log.warning(f"[activity_log] Schreiben fehlgeschlagen: {e}")
    finally:
        db.close()
