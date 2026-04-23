# Hannah — Voice Assistant Middleware

Hannah ist eine selbst gehostete Sprachassistenten-Middleware für ioBroker.  
Konzept: offline, kein Cloud-Zwang, kein API-Key — wie Alexa, aber für dein eigenes Smart Home.

## Architektur

```
┌─────────────────────────────────────────────────────────┐
│  Satellit (RPi / ESP32)                                 │
│                                                         │
│  Mikrofon → Wake-Word (OpenWakeWord) → UDP-Stream ──────┼──→ Hannah
│                                                         │
│  ←── TTS-Audio (UDP) ←── LED-Status (MQTT) ←───────────┼──── Hannah
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  Hannah (Server)                                        │
│                                                         │
│  UDP-Audio → STT (Whisper) → NLU → ioBroker-Steuerung  │
│                                  → TTS (Piper) → UDP   │
│                                                         │
│  State-Cache ← MQTT (javascript/0/virtualDevice/#)      │
└─────────────────────────────────────────────────────────┘
```

### Server-Module

| Modul | Aufgabe |
|---|---|
| `hannah/stt.py` | Speech-to-Text mit faster-whisper |
| `hannah/nlu.py` | Intent-Erkennung (Raum, Gerät, Aktion, Farbe, Helligkeit, Sensoren) |
| `hannah/iobroker.py` | Geräteindex, State-Cache, Steuerung, Query-Antworten |
| `hannah/mqtt_handler.py` | MQTT: Audio-Empfang, Status-Publishing, Satellit-Verwaltung |
| `hannah/udp_server.py` | UDP: Audio-Streaming, TTS-Rücksendung, Satellit-Registrierung |
| `hannah/tts.py` | Text-to-Speech mit Piper (ONNX) |
| `hannah/audio.py` | Audio-Dekodierung (PCM/WAV/MP3) |

---

## Hannah-Server

### Voraussetzungen

```bash
sudo apt install portaudio19-dev
pip install -r requirements.txt
```

### Konfiguration (`config.yaml`)

```yaml
iobroker:
  host: "192.168.8.1"
  port: 8093
  virtual_device_prefix: "javascript.0.virtualDevice"
  feedback_timeout: 3.0

tts:
  backend: piper          # piper (offline) | azure | polly

  # Piper — immer als Fallback konfigurieren
  model: /pfad/de_DE-kerstin-low.onnx
  length_scale: 1.0

  # Azure Cognitive Services (500.000 Zeichen/Monat kostenlos)
  # azure_key: "..."
  # azure_region: westeurope
  # azure_voice: de-DE-KatjaNeural

  # Amazon Polly
  # polly_key_id: "..."
  # polly_secret_key: "..."
  # polly_region: eu-central-1
  # polly_voice: Vicki
  # polly_engine: neural

  # Disk-Cache + Vorsynthetiisierung beim Start
  cache_dir: .tts_cache
  warm_phrases:
    - "Ich habe dich nicht verstanden."
    - "Es wurden keine Geräte gefunden."

  confirmation_sound: /pfad/pling.mp3   # leer = synthetisiert

stt:
  model: "base"       # tiny | base | small | medium | large-v3
  language: "de"
  device: "cpu"       # cpu | cuda
  compute_type: "int8"

mqtt:
  host: "192.168.8.1"
  port: 1883
  username: "mqtt"
  password: "geheim"

udp:
  port: 7775
  advertise_host: ""   # leer = eigene IP automatisch ermitteln
```

### Start (manuell)

```bash
python main.py -c config.yaml
python main.py -c config.yaml --log-level DEBUG
```

### Deployment

```bash
export REPO_URL="https://github.com/OWNER/hannah.git"
# Für private Forks mit Token:
# export REPO_TOKEN="dein-token"

git clone --depth=1 "$REPO_URL" /tmp/hannah
sudo -E bash /tmp/hannah/core/deploy/install.sh
```

Das Script klont das Repo nach `/opt/hannah-core/`, legt einen System-User `hannah` an und installiert den systemd-Service. Startet erst wenn `/etc/hannah/config.yaml` vorhanden ist.

**Update (aus vorhandenem Clone):**
```bash
sudo -E bash /tmp/hannah/core/deploy/install.sh   # git pull + pip install + Service-Restart
```

---

## TTS-Backends

Hannah unterstützt drei TTS-Backends: **Piper** (offline, lokal), **Azure Cognitive Services** und **Amazon Polly**.  
Backend per `tts.backend` in `config.yaml` wählen. Piper ist immer der automatische Fallback wenn ein Cloud-Backend nicht erreichbar ist.

### Fallback-Kette

```
Anfrage → Disk-Cache → primäres Backend (azure/polly/piper)
                              ↓ Fehler
                        Piper (Fallback)
```

1. Liegt die Phrase bereits im Disk-Cache, wird sie sofort abgespielt — kein Cloud-Aufruf.
2. Schlägt das Cloud-Backend fehl (Netz weg, Quota erschöpft), übernimmt Piper automatisch.
3. Ist auch Piper nicht konfiguriert, bleibt die Antwort stumm (nur MQTT-Publish).

### Backend: Piper (offline)

Vollständig offline, kein API-Key, kein Internet. Empfohlen für datenschutzbewusste Setups.

```bash
pip install piper-tts
```

Deutsches Modell herunterladen: [rhasspy/piper-voices](https://github.com/rhasspy/piper-voices/tree/master/de/de_DE)  
Empfehlung: `de_DE-kerstin-low.onnx` (klein, schnell, gut verständlich)

```yaml
tts:
  backend: piper
  model: /home/pi/de_DE-kerstin-low.onnx
  length_scale: 1.0      # Sprechgeschwindigkeit (>1 = langsamer)
  noise_scale: 0.667
  noise_w: 0.8
  padding_secs: 0.4      # Stille am Ende (verhindert abgeschnittene Silben)
```

**Verfügbare deutsche Stimmen (Auswahl):**

| Modell | Stimme | Größe |
|---|---|---|
| `de_DE-kerstin-low` | Kerstin (weiblich) | ~60 MB |
| `de_DE-thorsten-low` | Thorsten (männlich) | ~60 MB |
| `de_DE-eva_k-x_low` | Eva (weiblich) | ~30 MB |
| `de_DE-karlsson-low` | Karlsson (männlich) | ~60 MB |

### Backend: Azure Cognitive Services

Hochwertige Neuralstimmen über die Azure REST-API. Kein SDK nötig — nur `requests` (bereits in `requirements.txt`).

**Freies Tier:** 500.000 Zeichen/Monat (kein Ablaufdatum, keine Kreditkarte für F0-Tier nötig)

**Einrichtung:**
1. [portal.azure.com](https://portal.azure.com) → Ressource erstellen → *Speech*
2. Region wählen (z.B. `westeurope`)
3. API-Key unter *Schlüssel und Endpunkt* kopieren

```yaml
tts:
  backend: azure
  azure_key: "abc123..."
  azure_region: westeurope
  azure_voice: de-DE-KatjaNeural

  # Piper als Fallback bei Netzproblemen
  model: /home/pi/de_DE-kerstin-low.onnx
  cache_dir: .tts_cache
```

**Verfügbare deutsche Stimmen:**

| Voice-Name | Geschlecht | Stil |
|---|---|---|
| `de-DE-KatjaNeural` | weiblich | Standard |
| `de-DE-ConradNeural` | männlich | Standard |
| `de-DE-AmalaNeural` | weiblich | Freundlich |
| `de-DE-BerndNeural` | männlich | Ruhig |
| `de-DE-ChristophNeural` | männlich | Professionell |
| `de-DE-LouisaNeural` | weiblich | Jugendlich |

Vollständige Liste: `az cognitiveservices account list-kinds` oder im Azure Speech Studio.

### Backend: Amazon Polly

Neuralstimmen über AWS Polly. Benötigt `boto3`.

```bash
pip install boto3
```

**Freies Tier:** 1 Million Zeichen/Monat für 12 Monate (Neural: 1 Million Zeichen/Monat dauerhaft kostenlos im Free Tier)

**Einrichtung:**
1. AWS-Konto → IAM → Benutzer erstellen mit Policy `AmazonPollyReadOnlyAccess`
2. Zugriffsschlüssel (Key ID + Secret) generieren

```yaml
tts:
  backend: polly
  polly_key_id: "AKIAIOSFODNN7EXAMPLE"
  polly_secret_key: "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
  polly_region: eu-central-1
  polly_voice: Vicki
  polly_engine: neural

  # Piper als Fallback bei AWS-Problemen
  model: /home/pi/de_DE-kerstin-low.onnx
  cache_dir: .tts_cache
```

**Verfügbare deutsche Stimmen:**

| Voice-ID | Geschlecht | Engine |
|---|---|---|
| `Vicki` | weiblich | standard / neural |
| `Daniel` | männlich | neural |
| `Marlene` | weiblich | standard |
| `Hans` | männlich | standard |

### Disk-Cache

Cloud-Synthesen werden als `.pcm`-Dateien lokal gespeichert. Bei erneutem Aufruf wird die gecachte Datei verwendet — kein Cloud-Aufruf, keine Latenz.

```
.tts_cache/
  azure_de-DE-KatjaNeural/
    3f2a8b1c9e7d4a6b.pcm    ← SHA256(text)[:20]
    ...
  polly_Vicki/
    ...
```

Cache-Verzeichnis per `cache_dir` konfigurierbar. Bei Piper wird kein Cache angelegt (Synthese ist bereits lokal und schnell).

### Warm Phrases

Beim Start synthetisiert Hannah eine Liste von Standard-Phrasen und speichert sie im Cache. Danach stehen diese Antworten sofort und ohne Cloud-Aufruf zur Verfügung.

```yaml
tts:
  warm_phrases:
    - "Ich habe dich nicht verstanden."
    - "Tut mir leid, ich weiß nicht was du meinst."
    - "Es wurden keine Geräte gefunden."
```

Sinnvoll für alle Cloud-Backends: Fehlerantworten kommen auch offline schnell und ohne Netzlatenz.

### Bestätigungston

Nach erfolgreicher Gerätesteuerung spielt Hannah einen kurzen Ton ab.

```yaml
tts:
  confirmation_sound: /home/pi/pling.wav   # MP3/WAV/OGG/FLAC — leer = generierter Piepton
```

Ohne `confirmation_sound` (oder wenn die Datei nicht ladbar ist) wird ein synthetischer 1318-Hz-Ton mit Hüllkurve abgespielt.

---

## Hannah Proxy

Der Proxy ist ein optionaler Go-Dienst der zwischen Satelliten und Hannah geschaltet werden kann.  
Typische Use-Cases: Proxy läuft auf demselben Host wie Hannah (teilt den UDP-Port), oder auf einem separaten Host näher an den Satelliten.

```
Satellit → UDP → Hannah Proxy → gRPC → Hannah
                 (gleicher Host oder anderer Pi)
```

Solange der Proxy verbunden ist, deaktiviert Hannah automatisch ihren eigenen UDP-Server und delegiert Audio/TTS vollständig an den Proxy. Trennt sich der Proxy, reaktiviert Hannah ihren UDP-Server. Satelliten verbinden sich automatisch neu sobald sich das MQTT-Discovery-Topic ändert.

### Deployment

Das Binary muss aus dem Quellcode kompiliert werden (Go 1.21+):

```bash
cd proxy
go build ./cmd/proxy
sudo cp hannah-proxy /usr/local/bin/
```

Danach Service und Config einrichten:
```bash
# Config-Verzeichnis und Service-File anlegen
sudo mkdir -p /etc/hannah-proxy
sudo cp proxy/deploy/hannah-proxy.service /etc/systemd/system/
sudo systemctl daemon-reload
```

Das `proxy/deploy/install.sh` setzt eine Binary-Distribution voraus (Package Registry) und ist für Selbst-Compiler nicht direkt nutzbar — einfach das Binary manuell ablegen wie oben beschrieben.

**Deinstallieren:**
```bash
sudo systemctl stop hannah-proxy && sudo systemctl disable hannah-proxy
sudo rm /usr/local/bin/hannah-proxy /etc/systemd/system/hannah-proxy.service
```

### Konfiguration (`/etc/hannah-proxy/config.yaml`)

```yaml
proxy_id: hannah-proxy          # eindeutiger Name, erscheint in Hannah-Logs

hannah:
  address: "127.0.0.1:50051"    # gRPC-Adresse von Hannah Core

udp:
  listen_addr: ":7775"          # UDP-Port für Satelliten
  advertise_host: "192.168.8.15"  # IP die Satelliten via MQTT-Discovery erhalten
```

---

## Hannah Telegram

Telegram-Bot der Hannah über gRPC steuert. Unterstützt Text- und Sprachnachrichten, Auto-Status, Gerätesteuerung per Inline-Menü und Event-Push (Auto geparkt, Resident angekommen/abgegangen).

### Deployment

```bash
export REPO_URL="https://github.com/OWNER/hannah.git"
# export REPO_TOKEN="dein-token"   # nur für private Forks

git clone --depth=1 "$REPO_URL" /tmp/hannah
sudo -E bash /tmp/hannah/telegram/deploy/install.sh
```

Klont nach `/opt/hannah-telegram/`, System-User `hannah-telegram`. Startet erst wenn `/etc/hannah-telegram/config.yaml` vorhanden ist.

### Konfiguration (`/etc/hannah-telegram/config.yaml`)

```yaml
telegram:
  token: "123456789:AAF..."     # BotFather-Token
  allowed_users: [123456789]    # Telegram chat_id-Whitelist (leer = alle)

hannah:
  address: "127.0.0.1:50051"   # gRPC-Adresse von Hannah Core
```

---

## Hannah Voice-ID

Optionaler Sprechererkennungs-Service (ECAPA-TDNN via SpeechBrain). Identifiziert den Sprecher eines Satellit-Audio-Streams und übergibt die Roomie-ID an Hannah, sodass das LLM die Antwort personalisieren kann.

> **Abhängigkeit:** Voice-ID ist nur in Kombination mit dem Hannah-Proxy sinnvoll. Der Proxy ist der einzige Aufrufer von `/identify` — ohne Proxy wird kein Audio zur Erkennung eingereicht. Hannah Core selbst kennt Voice-ID nicht.

Der Proxy ruft den Service vor jedem `SubmitSatelliteAudio` auf. Bei unbekanntem Sprecher oder zu niedriger Konfidenz läuft die Pipeline anonym weiter.

Profile werden auf einer RAM-Disk (`/mnt/hannah_mem`) gehalten und beim Start von der SD-Karte geladen. Enrollment schreibt immer sofort auf die SD-Karte — kein Datenverlust bei Neustart, minimaler Schreibverschleiß.

### Deployment

```bash
export REPO_URL="https://github.com/OWNER/hannah.git"
# export REPO_TOKEN="dein-token"   # nur für private Forks

git clone --depth=1 "$REPO_URL" /tmp/hannah
sudo -E bash /tmp/hannah/voiceid/deploy/install.sh
```

Klont nach `/opt/hannah-voiceid/`, legt RAM-Disk (`/mnt/hannah_mem`, 128 MB) via `/etc/fstab` dauerhaft an, System-User `hannah-voiceid`. Keine separate Konfigurationsdatei nötig — Service läuft sofort auf Port `8080`.

### Sprecher enrollen

```bash
# Aufnahme und Enrollment in einem Schritt (via enroll_voice.py im Repo):
cd /opt/hannah-voiceid/voiceid
source /opt/hannah-voiceid/venv/bin/activate
python enroll_voice.py --roomie leonie --host localhost
```

### Proxy konfigurieren

Im Proxy `config.yaml` den Voice-ID-Service aktivieren:

```yaml
voice_id:
  enabled: true
  base_url: "http://localhost:8080"
  timeout_sec: 3.0
  min_confidence: 0.45
```

---

## Satellit

Der Satellit läuft auf einem Raspberry Pi (oder PC) und übernimmt:
- Wake-Word-Erkennung lokal (OpenWakeWord, kein Cloud-Dienst)
- Mikrofon → UDP-Streaming an Hannah
- TTS-Wiedergabe via Lautsprecher
- Status-LED-Steuerung (optional)

### Installation nach Plattform

**aarch64 — Raspberry Pi 3B/4/5 (64-bit OS), PC x86_64**
```bash
sudo apt install portaudio19-dev libopenblas0
pip install PyAudio miniaudio "numpy<2" paho-mqtt
pip install --no-deps openwakeword
pip install onnxruntime scipy requests tqdm scikit-learn
```

**armv7l — Raspberry Pi 2B / Pi 3B (32-bit OS)**
```bash
sudo apt install portaudio19-dev libopenblas0
pip install PyAudio miniaudio "numpy<2" paho-mqtt
pip install --no-deps openwakeword
pip install tflite-runtime scipy requests tqdm
```
> onnxruntime hat kein armv7l-Wheel — stattdessen tflite-runtime verwenden und `--framework tflite` beim Start angeben.

### Modelle herunterladen (einmalig)

```bash
python3 satellite.py --download-models
# auf armv7l:
python3 satellite.py --framework tflite --download-models
```

### Start

```bash
# aarch64 (Standard)
python3 satellite.py \
  --device wohnzimmer-pi \
  --room Wohnzimmer \
  --broker 192.168.8.1 \
  --mqtt-user mqtt --mqtt-pass geheim \
  --wakeword-model hey_jarvis_v0.1.onnx

# armv7l (Pi 2B / Pi 3B 32-bit)
python3 satellite.py \
  --framework tflite \
  --device wohnzimmer-pi \
  --room Wohnzimmer \
  --broker 192.168.8.1 \
  --mqtt-user mqtt --mqtt-pass geheim \
  --wakeword-model hey_jarvis_v0.1.tflite

# Mit LED-Status und angepassten Audio-Geräten
python3 satellite.py \
  --device wohnzimmer-pi \
  --room Wohnzimmer \
  --broker 192.168.8.1 \
  --mqtt-user mqtt --mqtt-pass geheim \
  --wakeword-model hey_jarvis_v0.1.onnx \
  --mic 1 \
  --speaker 2 \
  --sample-rate 48000 \
  --tts-rate 44100 \
  --led-pin 17

# Wake-Word-Scores debuggen
python3 satellite.py ... -v
```

### Satellit-Argumente

| Argument | Standard | Beschreibung |
|---|---|---|
| `--device` | `rpi-test` | Gerätename (muss eindeutig sein) |
| `--room` | `Wohnzimmer` | Raum des Satelliten (Raum-Fallback für Befehle ohne Raumnennung) |
| `--broker` | `192.168.8.1` | MQTT-Broker (für Discovery und Status) |
| `--host` | _(leer)_ | Hannah-IP direkt (leer = MQTT-Discovery) |
| `--wakeword-model` | _(alle)_ | Modell-Dateiname oder Pfad (`hey_jarvis_v0.1.onnx`) |
| `--wakeword-score` | `0.5` | Erkennungsschwelle 0.0–1.0 |
| `--framework` | `onnx` | `onnx` (aarch64/x86) oder `tflite` (armv7l) |
| `--mic` | _(Standard)_ | PyAudio-Index des Mikrofons |
| `--speaker` | _(Standard)_ | PyAudio-Index des Lautsprechers |
| `--sample-rate` | `16000` | Mikrofon-Sample-Rate |
| `--tts-rate` | `44100` | Lautsprecher-Sample-Rate (44100 für RPi-Klinke, 48000 für USB-Audio) |
| `--led-pin` | `0` | GPIO-Pin für Status-LED (BCM, 0 = deaktiviert) |
| `--pling-sound` | _(synthetisiert)_ | Zuhör-Ton (MP3/WAV/OGG/FLAC) |
| `--min-secs` | `0.8` | Mindestaufnahmedauer bevor Stille-Erkennung aktiv |
| `--silence` | `1.2` | Stille-Dauer in Sekunden bis Aufnahme endet |
| `--threshold` | `300` | RMS-Schwellwert für Stille-Erkennung |
| `--download-models` | — | Modelle herunterladen und beenden |
| `-v` / `--verbose` | — | DEBUG-Logging inkl. Wake-Word-Scores |

### Verfügbare Wake-Words (vortrainiert, englisch)

| Modell | Wake-Word |
|---|---|
| `hey_jarvis_v0.1` | "Hey Jarvis" |
| `alexa_v0.1` | "Alexa" |
| `hey_mycroft_v0.1` | "Hey Mycroft" |
| `hey_rhasspy_v0.1` | "Hey Rhasspy" |

Eigene Modelle (ONNX/tflite) können mit `--wakeword-model /pfad/modell.onnx` angegeben werden.

### Audio-Geräte auflisten

```bash
python3 list_devices.py
```

---

## MQTT-Topics

### Hannah-Server

| Topic | Richtung | Inhalt |
|---|---|---|
| `hannah/server` | ← Hannah | Hannah's UDP-Adresse (retained, für Discovery) |
| `hannah/+/audio` | → Hannah | Eingehendes Audio (PCM/WAV, base64, MQTT-Pfad) |
| `hannah/{device}/text` | ← Hannah | STT-Ergebnis |
| `hannah/{device}/intent` | ← Hannah | Erkannter Intent (JSON) |
| `hannah/{device}/answer` | ← Hannah | Textantwort auf Abfragen |
| `hannah/{device}/error` | ← Hannah | Fehlermeldungen |
| `hannah/commands/textcommand` | → Hannah | Text-Befehl (bypasses STT, zum Testen) |
| `hannah/commands/answer` | ← Hannah | Antwort auf Text-Befehl |
| `hannah/satelite/{device}/announcement` | → Hannah | Text per TTS auf Satellit ausgeben |
| `hannah/satelite/all/announcement` | → Hannah | Text per TTS auf allen Satelliten ausgeben |

### Satellit-Status (vom Satellit publiziert)

| Topic | Inhalt |
|---|---|
| `hannah/satelite/{device}/online` | `true` / `false` (retained, LWT) |
| `hannah/satelite/{device}/status` | `idle` / `listening` / `processing` / `speaking` (retained) |
| `hannah/satelite/{device}/command` | Eingehende Kommandos an den Satellit |

### ioBroker State-Cache

| Topic | Richtung | Inhalt |
|---|---|---|
| `javascript/0/virtualDevice/#` | → Hannah | State-Updates (Cache-Aktualisierung) |
| `hannah/set/<Kategorie>/<Etage>/<Raum>/<Gerät>/<State>` | ← Hannah | Steuerbefehl |

---

## Gerätestruktur in ioBroker

Geräte müssen unter folgendem Pfad liegen:

```
javascript.0.virtualDevice.<Kategorie>.<Etage>.<Raum>.<Gerätename>.<State>
```

**Beispiele:**
```
javascript.0.virtualDevice.Licht.EG.Wohnzimmer.DeckeSeite.on
javascript.0.virtualDevice.Licht.EG.Wohnzimmer.DeckeSeite.level
javascript.0.virtualDevice.Stecker.EG.Küche.Kaffeemaschine.on
javascript.0.virtualDevice.Temperaturen.OG.Schlafzimmer.Raumtemperatur.current
javascript.0.virtualDevice.Helligkeit.EG.Wohnzimmer.Sensor.illuminance
javascript.0.virtualDevice.Fenster.EG.Wohnzimmer.Terrassentür.open
```

CamelCase-Gerätenamen werden automatisch aufgelöst: `DeckeSeite` → "decke seite"

### Unterstützte Sensor-Kategorien

| Kategorie | State | Antwortformat |
|---|---|---|
| `Temperaturen` | `current`, `expected` | "Im Schlafzimmer: 21 Grad" |
| `Helligkeit` | `illuminance` | "Im Wohnzimmer: 320 Lux" |
| `Fenster` | `open` | "Terrassentür ist offen" |

---

## Sprachbefehle

### Steuerung

```
"Licht an"                        → alle Geräte im Raum des Satelliten
"Wohnzimmer Licht an"             → nur Licht-Kategorie im Wohnzimmer
"Schlafzimmer Stehlampe an"       → einzelnes Gerät
"Wohnzimmer aus"                  → alle Geräte im Raum
"Decke Seite 50 Prozent"          → Helligkeit setzen
"Decke Seite rot"                 → Farbe setzen
"Decke Seite warm"                → Farbtemperatur warm
```

Unterstützte Farben: rot, grün, blau, gelb, orange, lila, pink, magenta, cyan, türkis, weiß, warm/warmweiß, kalt/kaltweiß

### Abfragen

```
"Ist das Licht im Wohnzimmer an?"          → "Im Wohnzimmer: DeckeSeite ist an …"
"Welche Fenster sind offen?"               → "Terrassentür ist offen, Küchenfenster ist zu"
"Wie warm ist es im Schlafzimmer?"         → "Im Schlafzimmer: 21 Grad"
"Wie hell ist es?"                         → Helligkeitswert aller Räume
"Welche Lichter sind an?"                  → Globale Übersicht aller Räume
```

---

## Text-Befehle (Testen ohne Sprache)

```bash
mosquitto_pub -h 192.168.8.1 -u mqtt -P geheim \
  -t "hannah/commands/textcommand" \
  -m "Wohnzimmer Licht an"

# Antwort auf:
# hannah/commands/answer
```

---

## Hilfsskripte

| Skript | Beschreibung |
|---|---|
| `list_devices.py` | Listet alle PyAudio-Audiogeräte mit Index |
| `satellite-pi/test_wakeword.py` | Live-Anzeige der Wake-Word-Scores (zum Testen) |

---

## Roadmap

### Abgeschlossen
- [x] Audio-Empfang via MQTT und UDP
- [x] STT mit faster-whisper
- [x] NLU: Raum, Gerät, Aktion, Farbe, Helligkeit, Sensoren
- [x] ioBroker-Steuerung via MQTT (`hannah/set/...`)
- [x] Raum-Fallback via Satellit-Standort
- [x] State-Cache (abonniert virtualDevice/#)
- [x] Cache-Vorwärmung beim Start via REST API
- [x] Query-Antworten: Licht, Stecker, Temperaturen, Helligkeit, Fenster
- [x] TTS mit Piper (ONNX), Bestätigungston
- [x] UDP-Streaming: Satellit → Hannah (Audio) und Hannah → Satellit (TTS)
- [x] Wake-Word-Erkennung lokal auf dem Satellit (OpenWakeWord)
- [x] Plattform-Support: x86, aarch64 (onnx), armv7l (tflite)
- [x] MQTT Control-Channel: Satellit-Status, Online/Offline mit LWT
- [x] LED-Status-Steuerung am Satellit (GPIO)
- [x] Text-Befehl via MQTT (Bypass STT, zum Testen)
- [x] systemd-Services + Installer für Core, Proxy, Telegram, Voice-ID
- [x] Go-Proxy: Satelliten-Audio via gRPC, UDP-Fallback, automatischer Reconnect
- [x] Telegram-Bot: Text/Sprache, Auto-Status, Geräte-Menü, Event-Push
- [x] Voice-ID: Sprechererkennung (ECAPA-TDNN), RAM-Disk-Profile, LLM-Personalisierung
- [x] Heartbeat: Satellit erkennt verlorene Hannah-Verbindung und restartet
- [x] LLM-Integration: Smalltalk-Backend (Ollama, self-hosted), DummyLLM-Fallback
- [x] System-Prompt-Variablen: `{{TIME}}`, `{{DATE}}`, `{{WEEKDAY}}`, `{{KW}}`, `{{iob.STATE_ID}}`
- [x] Trust-Level + Speaker-Kontext im LLM-System-Prompt
- [x] Gesprächskontext: Smalltalk-Modus mit TTL und automatischer Deaktivierung
- [x] Playback-Steuerung am ESP32-Satelliten (Stop/Pause/Resume per UDP)
- [x] Routinen + Gruppen in config.yaml
- [x] ioBroker Notification-Adapter (`iobroker.hannah-notification`)
- [x] System-Notification-Pipeline: ioBroker → LLM-Reformulierung → TTS (DND-gefiltert) → Telegram
- [x] Folgefragen: Raumkontext bleibt erhalten ("Wohnzimmer Licht an" → "und die Küche auch")
- [x] Remote-STT: faster-whisper-server als optionales Backend (Apple Silicon, NVIDIA GPU)
- [x] Langzeitgedächtnis Phase 1: Gesprächszusammenfassungen per LLM → SQLite → System-Prompt

### Im Test
- [ ] ESP32-Satellit-Firmware (Hardware unterwegs, Phase 1)

### Offen
- [ ] Trigger-Engine: ioBroker State-Änderungen / Uhrzeiten als Auslöser für proaktive Ansagen
- [ ] Rückfragen bei Mehrdeutigkeit ("Welchen Flur meinst du — EG oder OG?")
- [ ] Szenen: Vordefinierte Gerätezustände per Sprache abrufen
- [ ] Telegram Mini App: Slider und Farbwähler statt InlineKeyboard (braucht HTTPS)
- [ ] NeoPixel/WS2812B LED-Ring als Statusanzeige am Satelliten
- [ ] Mood-System + Relationship-Score: Hannahs emotionaler Zustand beeinflusst Ton
- [ ] Hannah-Agent: Hintergrundprozess für proaktives Verhalten und autonome Aktionen
- [ ] Langzeitgedächtnis Phase 2: Vektordatenbank (Chroma) für semantische Suche bei vielen Einträgen
- [ ] Mustererkennung: History-Adapter + LLM erkennt Gewohnheiten
