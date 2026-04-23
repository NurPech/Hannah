import argparse
import os
import shutil

import torch
import numpy as np
import yaml
from fastapi import FastAPI, Request, Header
import uvicorn

# ── CLI ───────────────────────────────────────────────────────────────────────

parser = argparse.ArgumentParser(description="Hannah Voice-ID Service")
parser.add_argument("--config", default="", help="Pfad zur config.yaml")
args, _ = parser.parse_known_args()

# ── Config ────────────────────────────────────────────────────────────────────

def _load_config(path: str) -> dict:
    if path and os.path.exists(path):
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {}

_cfg = _load_config(args.config)
_server  = _cfg.get("server", {})
_recog   = _cfg.get("recognition", {})

HOST                = _server.get("host", "0.0.0.0")
PORT                = int(_server.get("port", 8080))
UNKNOWN_THRESHOLD   = float(_recog.get("unknown_threshold",   0.25))
UNCERTAIN_THRESHOLD = float(_recog.get("uncertain_threshold", 0.40))

# ── Pfade (Deployment-Konstanten, nicht per Config änderbar) ──────────────────

MEM_PATH  = "/mnt/hannah_mem"
DISK_PATH = os.path.expanduser("~/hannah/voice_profiles")
os.makedirs(DISK_PATH, exist_ok=True)

# ── Modell ────────────────────────────────────────────────────────────────────

print("Lade Sprach-Modell (ECAPA-TDNN) auf CPU ...")
from speechbrain.inference.speaker import EncoderClassifier
classifier = EncoderClassifier.from_hparams(
    source="speechbrain/spkrec-ecapa-voxceleb",
    run_opts={"device": "cpu"},
)
torch.set_num_threads(4)
print("✅ Modell bereit.")

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI()


@app.on_event("startup")
async def load_profiles_to_ram():
    """Beim Start: Profile von SD-Karte in RAM-Disk laden."""
    os.makedirs(MEM_PATH, exist_ok=True)
    loaded = 0
    for file in os.listdir(DISK_PATH):
        if file.endswith(".pt"):
            shutil.copy2(os.path.join(DISK_PATH, file), os.path.join(MEM_PATH, file))
            loaded += 1
    print(f"✅ {loaded} Stimmprofil(e) in RAM-Disk geladen ({MEM_PATH}).")


def get_embedding(audio_bytes: bytes) -> torch.Tensor:
    signal = torch.from_numpy(np.frombuffer(audio_bytes, dtype=np.int16)).float()
    return classifier.encode_batch(signal).squeeze()


@app.post("/enroll")
async def enroll(request: Request, x_roomie_id: str = Header(...)):
    audio_data = await request.body()
    print(f"Enrollment-Probe empfangen für: {x_roomie_id}")

    new_emb   = get_embedding(audio_data)
    filename  = f"{x_roomie_id}.pt"
    disk_file = os.path.join(DISK_PATH, filename)
    ram_file  = os.path.join(MEM_PATH,  filename)

    if os.path.exists(disk_file):
        old_emb      = torch.load(disk_file, map_location="cpu").squeeze()
        combined_emb = (old_emb * 0.8) + (new_emb * 0.2)
        print(f"Update: Bestehendes Profil für {x_roomie_id} verfeinert.")
    else:
        combined_emb = new_emb
        print(f"Neu: Erstes Profil für {x_roomie_id} erstellt.")

    torch.save(combined_emb, disk_file)
    torch.save(combined_emb, ram_file)
    return {"ok": True, "message": f"Profil für {x_roomie_id} gespeichert."}


@app.post("/identify")
async def identify(request: Request):
    audio_data  = await request.body()
    current_emb = get_embedding(audio_data)

    best_match = "unknown"
    max_score  = 0.0

    for file in os.listdir(MEM_PATH):
        if file.endswith(".pt"):
            stored_emb = torch.load(os.path.join(MEM_PATH, file), map_location="cpu").squeeze()
            score = torch.nn.functional.cosine_similarity(current_emb, stored_emb, dim=0).item()
            if score > max_score:
                max_score  = score
                best_match = file.replace(".pt", "")

    if max_score < UNKNOWN_THRESHOLD:
        best_match = "unknown"
    elif max_score < UNCERTAIN_THRESHOLD:
        print(f"⚠️  Unsichere Erkennung: {best_match} ({max_score:.4f})")

    print(f"Ergebnis: {best_match} (Score: {max_score:.4f})")
    return {"roomie_id": best_match, "confidence": max_score}


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)
