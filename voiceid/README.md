# Hannah VoiceID

Optionaler Sprechererkennungs-Dienst (ECAPA-TDNN via SpeechBrain, FastAPI/REST). Identifiziert den Sprecher eines Satelliten-Audio-Streams; [Hannah Core](../core/README.md) ruft ihn selbst auf (`voice_id`-Config-Block) und personalisiert damit Antworten/LLM-Kontext — unabhängig davon, ob Satelliten direkt mit Core oder über den [Proxy](../proxy/README.md) verbunden sind.

Profile werden auf einer RAM-Disk gehalten und beim Start von der SD-Karte/Disk geladen. Enrollment schreibt immer sofort auf die Disk — kein Datenverlust bei Neustart, minimaler Schreibverschleiß.

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/NurPech/hannah/master/voiceid/deploy/install.sh | sudo bash
```

Lädt VoiceID vom Update-Server (Channel `voiceid-stable`), legt eine RAM-Disk (`/mnt/hannah_mem`, 128 MB) via `/etc/fstab` dauerhaft an, System-User `hannah-voiceid`. Keine separate Konfigurationsdatei zwingend nötig — Service läuft mit den Defaults aus `config.yaml` (Port `8080`).

## Sprecher enrollen

`enroll_voice.py` ist ein interaktives Aufnahme-Skript für den lokalen Rechner (nicht für den Server selbst gedacht — braucht ein Mikrofon):

```bash
cd voiceid
python enroll_voice.py
```

Vorher in `enroll_voice.py` `PI_URL` (Adresse des laufenden VoiceID-Diensts) und `USER_ID` (Hannah `users.id`, nicht der Username) auf die eigenen Werte anpassen. Das Skript listet verfügbare Mikrofone, nimmt 10 Sekunden auf und schickt die Probe an `/enroll`.

## Core konfigurieren

Im Core-`config.yaml` den VoiceID-Dienst aktivieren:

```yaml
voice_id:
  enabled: true
  base_url: "http://localhost:8765"
  timeout_sec: 3.0
```

## Konfiguration (`config.yaml`)

```yaml
server:
  host: "0.0.0.0"
  port: 8080

recognition:
  unknown_threshold: 0.25    # darunter gilt der Sprecher als unbekannt
  uncertain_threshold: 0.40  # darunter wird die Erkennung nur als unsicher geloggt
```
