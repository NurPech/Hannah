# Hannah Core

Python-Dienst, das Herzstück von Hannah: nimmt Audio/Text von Satelliten, Telegram oder ioBroker entgegen, verarbeitet es durch STT → NLU → Gerätesteuerung/Antwort → TTS, und orchestriert alle anderen Komponenten.

Siehe [Root-README](../README.md) für den Gesamtüberblick und die Architektur; [CLAUDE.md](../CLAUDE.md) für Protokoll-/API-Details.

## Voraussetzungen

- Ein Linux-Host (Raspberry Pi 4/5 empfohlen — Pi 5, falls STT lokal laufen soll)
- Python 3.11+
- ioBroker mit installiertem [`iobroker.hannah`](../iobroker.hannah/)-Adapter — meldet Geräte per gRPC an Core
- MQTT-Broker (z. B. Mosquitto)

```bash
sudo apt install portaudio19-dev
pip install -r requirements.txt
```

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/NurPech/hannah/master/core/deploy/install.sh | sudo bash
```

Lädt Core vom Update-Server (Channel `core-stable`), legt System-User `hannah` an, installiert den systemd-Service. Startet erst, wenn `/etc/hannah/config.yaml` vorhanden ist. Erneuter Aufruf aktualisiert auf die neueste Version.

```bash
# config.yaml aus config.example.yaml ableiten und anpassen, dann:
sudo systemctl enable --now hannah
```

Manueller Start (Entwicklung):

```bash
python main.py -c config.yaml
python main.py -c config.yaml --log-level DEBUG
```

## Konfiguration

`config.yaml` deckt Infrastruktur/Bootstrap ab (Ports, Backend-URLs, Feature-Flags) — siehe `config.example.yaml` für alle Optionen. Feinere, laufzeitveränderliche Einstellungen — NLU-Wortlisten, LLM-System-Prompt, Räume/Gruppen, Fahrzeuge, BLE-Tags — liegen in der Datenbank und werden über die WebUI (Admin-Bereich) verwaltet, nicht in der Datei. Bei leerer DB werden generische Defaults automatisch befüllt.

### TTS-Backends

Drei TTS-Backends: **Piper** (offline, lokal), **Azure Cognitive Services**, **Amazon Polly**. Piper ist immer der automatische Fallback, wenn ein Cloud-Backend nicht erreichbar ist.

```yaml
tts:
  backend: piper          # piper | azure | polly
  model: /pfad/de_DE-kerstin-low.onnx   # Piper-Modell, auch als Fallback für Cloud-Backends nötig
  length_scale: 1.0

  # azure_key: "..."
  # azure_region: westeurope
  # azure_voice: de-DE-KatjaNeural

  # polly_key_id: "..."
  # polly_secret_key: "..."
  # polly_region: eu-central-1
  # polly_voice: Vicki
  # polly_engine: neural

  cache_dir: .tts_cache   # Disk-Cache für Cloud-Synthesen (SHA256(text) → .pcm)
  warm_phrases:
    - "Ich habe dich nicht verstanden."
  confirmation_sound: /pfad/pling.wav   # leer = synthetisierter Ton
```

Piper-Modell (deutsch, empfohlen): [`de_DE-kerstin-low`](https://github.com/rhasspy/piper-voices/tree/master/de/de_DE) (~60 MB).

### Speaker-ID (VoiceID)

Optional — löst den Sprecher eines Audio-Streams selbst auf, unabhängig davon, ob Satelliten direkt oder über den [Proxy](../proxy/README.md) verbunden sind. Braucht einen laufenden [VoiceID-Dienst](../voiceid/README.md).

```yaml
voice_id:
  enabled: false
  base_url: "http://localhost:8765"
  timeout_sec: 3.0
```

### Text-Befehle ohne Sprache

Zum Testen ohne Mikrofon: den `textCommand`-Datenpunkt der `iobroker.hannah`-Adapterinstanz mit `ack=false` beschreiben (z. B. per Vis-Widget) — der Text geht direkt in dieselbe NLU-Pipeline wie Satellit/Telegram.

## Beispiel-Sprachbefehle

```
"Licht an"                        → alle Geräte im Raum des Satelliten
"Wohnzimmer Licht an"             → nur Licht-Kategorie im Wohnzimmer
"Schlafzimmer Stehlampe an"       → einzelnes Gerät
"Decke Seite 50 Prozent"          → Helligkeit setzen
"Decke Seite rot"                 → Farbe setzen
"Ist das Licht im Wohnzimmer an?" → Statusabfrage
"Welche Fenster sind offen?"      → "Terrassentür ist offen, Küchenfenster ist zu"
"Wie warm ist es im Schlafzimmer?"→ "Im Schlafzimmer: 21 Grad"
```
