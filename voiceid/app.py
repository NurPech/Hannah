import argparse
import copy
import logging
import os
import shutil
import sys
from contextlib import asynccontextmanager

import torch
import numpy as np
import yaml
from fastapi import APIRouter, FastAPI, Request, Header
import uvicorn

import log_shipping


log = logging.getLogger(log_shipping.LOGGER_NAME)

_ENV_PREFIX = "HANNAH_VOICEID_"

# CI-stamped by the `upload:voiceid` job (tarball) and the Dockerfile (container);
# absent in a local dev checkout, where get_version() falls back to "dev".
_VERSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")


def get_version() -> str:
    try:
        with open(_VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except FileNotFoundError:
        return "dev"


def _load_config(path: str) -> dict:
    if path and os.path.exists(path):
        with open(path) as f:
            parsed = yaml.safe_load(f) or {}
    else:
        parsed = {}
    return _apply_env_overrides(parsed)


def _apply_env_overrides(cfg: dict) -> dict:
    """Jeder Config-Pfad ist per Env überschreibbar, ohne Allowlist. Namensschema:
    HANNAH_VOICEID_<PATH> — komponentenspezifisches Präfix (nicht bloß "HANNAH_"), sonst
    Kollision mit anderen, unabhängigen HANNAH_*-Variablen im Projekt (siehe Core, #327).
    "." zwischen Verschachtelungsebenen wird IMMER zu "__" (auch ohne Not), einfache "_"
    innerhalb eines Key-Namens bleiben erhalten (recognition.unknown_threshold ->
    HANNAH_VOICEID_RECOGNITION__UNKNOWN_THRESHOLD)."""
    result = copy.deepcopy(cfg)
    for env_key, raw_value in os.environ.items():
        if not env_key.startswith(_ENV_PREFIX):
            continue
        path = [segment.lower() for segment in env_key[len(_ENV_PREFIX):].split("__")]
        if not path or not all(path):
            continue
        node = result
        for segment in path[:-1]:
            child = node.get(segment)
            if not isinstance(child, dict):
                child = {}
                node[segment] = child
            node = child
        node[path[-1]] = _coerce(raw_value)
    return result


def _coerce(value: str):
    """Env-Werte sind immer Strings — ohne Schema die beste Annäherung an den Typ,
    den derselbe Wert in YAML hätte (int/float/bool vor String-Fallback)."""
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _load_model():
    log.info("Lade Sprach-Modell (ECAPA-TDNN) auf CPU ...")
    from speechbrain.inference.speaker import EncoderClassifier
    model = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        run_opts={"device": "cpu"},
    )
    torch.set_num_threads(4)
    log.info("✅ Modell bereit.")
    return model


@asynccontextmanager
async def _lifespan(app: FastAPI):
    log.info("Hannah VoiceID %s", app.version)
    cfg    = _load_config(getattr(app.state, "config_path", ""))
    _recog = cfg.get("recognition", {})

    if "unknown_threshold" in _recog:
        app.state.unknown_threshold = float(_recog["unknown_threshold"])
    if "uncertain_threshold" in _recog:
        app.state.uncertain_threshold = float(_recog["uncertain_threshold"])

    disk_path = app.state.disk_path
    mem_path  = app.state.mem_path
    os.makedirs(disk_path, exist_ok=True)
    os.makedirs(mem_path,  exist_ok=True)

    if app.state.classifier is None:
        app.state.classifier = _load_model()

    loaded = 0
    for file in os.listdir(disk_path):
        if file.endswith(".pt"):
            shutil.copy2(os.path.join(disk_path, file), os.path.join(mem_path, file))
            loaded += 1
    log.info("✅ %d Stimmprofil(e) in RAM-Disk geladen (%s).", loaded, mem_path)

    yield


router = APIRouter()


def get_embedding(classifier, audio_bytes: bytes) -> torch.Tensor:
    signal = torch.from_numpy(np.frombuffer(audio_bytes, dtype=np.int16).copy()).float()
    return classifier.encode_batch(signal).squeeze()


@router.post("/enroll")
async def enroll(request: Request, x_user_id: str = Header(...)):
    audio_data  = await request.body()
    classifier  = request.app.state.classifier
    disk_path   = request.app.state.disk_path
    mem_path    = request.app.state.mem_path

    log.info("Enrollment-Probe empfangen für: %s", x_user_id)
    new_emb   = get_embedding(classifier, audio_data)
    filename  = f"{x_user_id}.pt"
    disk_file = os.path.join(disk_path, filename)
    ram_file  = os.path.join(mem_path,  filename)

    if os.path.exists(disk_file):
        old_emb      = torch.load(disk_file, map_location="cpu").squeeze()
        combined_emb = (old_emb * 0.8) + (new_emb * 0.2)
        log.info("Update: Bestehendes Profil für %s verfeinert.", x_user_id)
    else:
        combined_emb = new_emb
        log.info("Neu: Erstes Profil für %s erstellt.", x_user_id)

    torch.save(combined_emb, disk_file)
    torch.save(combined_emb, ram_file)
    return {"ok": True, "message": f"Profil für {x_user_id} gespeichert."}


@router.post("/identify")
async def identify(request: Request):
    audio_data  = await request.body()
    classifier  = request.app.state.classifier
    mem_path    = request.app.state.mem_path
    unknown_threshold   = request.app.state.unknown_threshold
    uncertain_threshold = request.app.state.uncertain_threshold

    current_emb = get_embedding(classifier, audio_data)
    best_match  = "unknown"
    max_score   = 0.0

    for file in os.listdir(mem_path):
        if file.endswith(".pt"):
            stored_emb = torch.load(os.path.join(mem_path, file), map_location="cpu").squeeze()
            score = torch.nn.functional.cosine_similarity(current_emb, stored_emb, dim=0).item()
            if score > max_score:
                max_score  = score
                best_match = file.replace(".pt", "")

    if max_score < unknown_threshold:
        best_match = "unknown"
    elif max_score < uncertain_threshold:
        log.warning("Unsichere Erkennung: %s (%.4f)", best_match, max_score)

    log.info("Ergebnis: %s (Score: %.4f)", best_match, max_score)
    return {"user_id": best_match, "confidence": max_score}


def create_app(
    *,
    config_path: str = "",
    classifier=None,
    disk_path: str | None = None,
    mem_path: str | None = None,
    unknown_threshold: float = 0.25,
    uncertain_threshold: float = 0.40,
) -> FastAPI:
    """Factory — pass classifier=<mock> in tests to skip model loading."""
    _app = FastAPI(lifespan=_lifespan, version=get_version())
    _app.state.config_path          = config_path
    _app.state.classifier           = classifier
    _app.state.disk_path            = disk_path or os.environ.get(
        "VOICEID_DISK_PATH", os.path.expanduser("~/hannah/voice_profiles")
    )
    _app.state.mem_path             = mem_path or os.environ.get(
        "VOICEID_MEM_PATH", "/mnt/hannah_mem"
    )
    _app.state.unknown_threshold    = unknown_threshold
    _app.state.uncertain_threshold  = uncertain_threshold
    _app.include_router(router)
    return _app


app = create_app(config_path=os.environ.get("VOICEID_CONFIG", ""))


if __name__ == "__main__":
    _parser = argparse.ArgumentParser(description="Hannah Voice-ID Service")
    _parser.add_argument("--config", default="", help="Pfad zur config.yaml")
    _args = _parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    _cfg    = _load_config(_args.config)
    # Puffert ab hier; verschickt wird, sobald Hannah (hannah.address) einen Collector meldet.
    log_shipping.install(get_version(), hannah_address=log_shipping.hannah_address(_cfg), cfg=_cfg)
    _server = _cfg.get("server", {})
    _host   = _server.get("host", "0.0.0.0")
    _port   = int(_server.get("port", 8080))

    _app = create_app(config_path=_args.config)
    uvicorn.run(_app, host=_host, port=_port)
