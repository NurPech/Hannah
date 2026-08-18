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


def save_audio_wav(audio_array: np.ndarray, sample_rate: int, stem: str, subdir: str = "") -> str:
    """Schreibt audio_array als WAV, analog stt.py::_to_wav(). Legt _audio_dir bei
    Bedarf selbst an — externes NFS-Mount wird administrativ vorab bereitgestellt.
    subdir: optionaler Unterordner (z.B. für Ad-hoc-Debug-Dumps getrennt von den
    regulären Activity-Log-WAVs)."""
    target_dir = os.path.join(_audio_dir, subdir) if subdir else _audio_dir
    os.makedirs(target_dir, exist_ok=True)
    path = os.path.join(target_dir, f"{stem}.wav")
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


class ActivityLogManager:
    """Auth-geprüfter Lesezugriff aufs Activity Log für externe gRPC-Consumer (WebUI, #228).
    log_activity()/save_audio_wav() oben bleiben unverändert — das ist der Fire-and-forget-
    Schreibpfad aus der Pipeline, hier geht's nur um den Read-Pfad."""

    _AUDIO_CHUNK_BYTES = 8000  # ~250ms PCM bei 16kHz/16-bit mono
    _DEFAULT_PAGE_SIZE = 50
    _MAX_PAGE_SIZE = 200

    def __init__(self, user_manager):
        self._user_manager = user_manager

    def _trust_level(self, user_id) -> int:
        user = self._user_manager.get_user_by_id(user_id) if user_id else None
        return user.trust_level if user else 0

    def _owned_device_ids(self, user_id) -> list:
        user = self._user_manager.get_user_by_id(user_id) if user_id else None
        return [s.device_id for s in user.satellites] if user else []

    def _apply_visibility(self, query, target_user_id: int):
        """Beschränkt query auf Einträge, die target_user_id sehen darf: direkt zugeordnet
        (user_id), oder über einen ihr gehörenden Satelliten, sofern der Eintrag noch
        keinem anderen User zugeordnet ist. Spiegelt _is_visible() unten — beide Stellen
        müssen bei Änderungen an der Zugriffsregel synchron bleiben."""
        device_ids = self._owned_device_ids(target_user_id)
        if device_ids:
            placeholders = ", ".join(["?"] * len(device_ids))
            clause = (
                "(user_id = ? OR (channel_type = 'satellite' "
                f"AND channel_id IN ({placeholders}) AND (user_id IS NULL OR user_id = ?)))"
            )
            params = [target_user_id, *device_ids, target_user_id]
        else:
            clause = "user_id = ?"
            params = [target_user_id]
        return query.where(clause, *params)

    def _is_visible(self, entry: ActivityLog, user_id) -> bool:
        """Einzeleintrags-Variante derselben Regel wie _apply_visibility(), für den
        Audio-Stream-Zugriff (dort steht schon eine geladene Row bereit, keine neue Query
        nötig) — muss aber trotzdem unabhängig vom "darf grundsätzlich listen" geprüft
        werden, sonst wäre eine erratene activity_log_id ausreichend."""
        if entry.user_id:
            return int(entry.user_id) == int(user_id)
        if entry.channel_type != "satellite":
            return False
        return entry.channel_id in self._owned_device_ids(user_id)

    def list_activity(self, requestor_id, filter_user_id, page_size, before_id):
        """Gibt (entries: list[dict], has_more: bool) zurück. entries sind ActivityLog.to_dict()."""
        db = get_activity_db()
        if db is None:
            return [], False
        try:
            trust = self._trust_level(requestor_id)
            if trust >= 10:
                target_user_id = filter_user_id or None
            else:
                target_user_id = requestor_id

            query = ActivityLog.select(db)
            if target_user_id is not None:
                query = self._apply_visibility(query, target_user_id)
            if before_id:
                query = query.where("id < ?", before_id)

            size = max(1, min(page_size or self._DEFAULT_PAGE_SIZE, self._MAX_PAGE_SIZE))
            rows = query.order_by("id DESC").limit(size + 1).all()
            has_more = len(rows) > size
            return [r.to_dict() for r in rows[:size]], has_more
        finally:
            db.close()

    def get_audio_chunks(self, requestor_id, activity_log_id):
        """Gibt eine Liste von (pcm_bytes, sample_rate)-Tupeln zurück, oder None wenn der
        Eintrag nicht existiert/kein Audio hat/requestor_id nicht berechtigt ist."""
        db = get_activity_db()
        try:
            entry = ActivityLog.get(db, id=activity_log_id)
        finally:
            db.close()

        if not entry or not entry.audio_path:
            return None
        if self._trust_level(requestor_id) < 10 and not self._is_visible(entry, requestor_id):
            return None
        return self._read_wav_chunks(entry.audio_path)

    def _read_wav_chunks(self, path: str):
        with wave.open(path, "rb") as w:
            sample_rate = w.getframerate()
            sampwidth = w.getsampwidth()
            frames_per_chunk = max(1, self._AUDIO_CHUNK_BYTES // sampwidth)
            while True:
                frames = w.readframes(frames_per_chunk)
                if not frames:
                    break
                yield frames, sample_rate
