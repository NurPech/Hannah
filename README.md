# Hannah — Voice Assistant Middleware

[![pipeline status](https://dev.kernstock.net/gessinger/voice/hannah/badges/master/pipeline.svg)](https://dev.kernstock.net/gessinger/voice/hannah/-/commits/master)
[![Latest Release](https://dev.kernstock.net/gessinger/voice/hannah/-/badges/release.svg)](https://dev.kernstock.net/gessinger/voice/hannah/-/releases)

Hannah ist ein **lokal betriebener, deutschsprachiger Sprachassistent** für das Smart Home — ein selbst gehosteter Ersatz für Google Assistant / Amazon Echo, gebaut für [ioBroker](https://www.iobroker.net/). Kein Cloud-Zwang, kein API-Key-Vendor-Lock-in: STT, NLU und (optional) TTS laufen komplett im eigenen Netz. Gesteuert wird per Sprache über eigene ESP32-S3-Satelliten, per Telegram-Sprachnachricht/Text, oder direkt per Text aus ioBroker/Vis.

---

## Repository-Struktur

Mono-Repo. Die wichtigsten Komponenten (jede mit eigenem README für Installation/Konfiguration):

```
hannah/
├── core/            ← Hannah Core (Python) — STT/NLU/TTS/LLM, Orchestrierung        → core/README.md
├── proxy/            ← Go gRPC-Proxy (UDP-Satelliten → gRPC → Core, optional)       → proxy/README.md
├── satellite-esp/    ← ESP32-S3-Satelliten-Firmware (ESP-IDF, C)                    → satellite-esp/ENTWICKLUNG.md
├── telegram/          ← Telegram-Bot Microservice (Python, optional)                 → telegram/README.md
├── voiceid/           ← Speaker-ID Service (Python, ECAPA-TDNN, optional)            → voiceid/README.md
├── hardware/         ← Stückliste, GPIO-Zuordnung (PCB-Design-Quelldateien noch nicht öffentlich)
└── scripts/           ← Build-/Release-Scripts
```

Nicht Teil dieses Repos, aber Teil des Gesamtsystems: [hannah-webui](https://github.com/NurPech/hannah-webui) (Browser-Frontend für Einstellungen, Nutzerverwaltung, Konto-Verknüpfung).

---

## Architektur

```
Satellit (ESP32-S3, eigenes PCB)
    └─ UDP / gRPC ──→  Hannah Proxy (optional) ──→  Hannah Core
                                                        ├─ STT   (faster-whisper, lokal oder Remote)
                                                        ├─ NLU   (regelbasiert, kein ML)
                                                        ├─ LLM   (Ollama self-hosted oder Cloud, optional)
                                                        ├─ TTS   (Piper / Azure / Polly)
                                                        ├─ VoiceID (Speaker-Erkennung, optional)
                                                        ├─ ioBroker REST-API
                                                        └─ gRPC ──→ Telegram-Bot / WebUI / ioBroker-Adapter
```

- **Satellit**: ESP32-S3-Custom-PCB mit PDM-Mikrofonen, Wake-Word-Erkennung (microWakeWord, läuft komplett auf dem Chip), LED-Ring, Lautsprecher. Streamt Audio per UDP (direkt zu Core oder über den Proxy).
- **Proxy** (optional): Go-Dienst, bindet UDP-Satelliten per gRPC an Core an. Ohne Proxy läuft Core mit eigenem UDP-Server — funktional gleichwertig, der Proxy ist für größere Installationen mit mehreren Satelliten/Netzsegmenten gedacht.
- **Core**: Python, orchestriert STT → NLU → Gerätesteuerung/Antwort → TTS.
- **ioBroker-Integration**: läuft über einen eigenen Adapter (`iobroker.hannah`), der Geräte per gRPC an Core meldet und State-Updates in beide Richtungen synchronisiert.

Kommunikation im Detail (Protokolle, gRPC-Methoden, MQTT-Topics): siehe [CLAUDE.md](CLAUDE.md).

---

## Was kann Hannah?

- **Sprachsteuerung** per Satellit oder Telegram-Sprachnachricht
- **Gerätesteuerung** über ioBroker virtualDevice-Skripte (Licht, Stecker, Dimmen, Farben, Sensoren)
- **Abfragen** — Temperatur, Fensterstatus, Helligkeit, Autoposition, Wetter
- **Smalltalk** per lokalem LLM (Ollama — aber auch OpenAI, Groq, Mistral möglich), optional
- **Telegram-Integration** — Text, Sprache, Gerätesteuerung per Inline-Menü, System-Benachrichtigungen
- **TTS** — Piper (lokal, offline), Azure Cognitive Services oder Amazon Polly
- **STT** — faster-whisper lokal oder per Remote-Server
- **Langzeitgedächtnis** — Gesprächszusammenfassungen landen in SQLite und fließen in zukünftige Gespräche
- **Sprechererkennung** (optional) — ECAPA-TDNN erkennt wer spricht und personalisiert Antworten
- **Folgefragen** — "Wohnzimmer Licht an" → "und die Küche auch" funktioniert
- **Timer, Wecker, passive Mailbox** für Nutzer-zu-Nutzer-Nachrichten

Beispiel-Sprachbefehle, Konfigurationsdetails: siehe [core/README.md](core/README.md).

---

## Was braucht man?

**Pflicht:**

- Ein Linux-Host für Hannah Core (z. B. Raspberry Pi 4/5 — Pi 5 empfohlen, falls STT lokal laufen soll)
- Mindestens ein Satellit
- ioBroker mit installiertem [`iobroker.hannah`](iobroker.hannah/)-Adapter — meldet Geräte per gRPC an Core
- Python 3.11+
- MQTT-Broker (z. B. Mosquitto)

**Zu den Satelliten:** Die Satelliten laufen auf einer eigenen ESP32-S3-Platine (Custom-PCB). Das PCB-Design ist aktuell noch nicht öffentlich zum Nachbau freigegeben — bei Interesse gerne im GitHub-Issue melden, Gerber/BOM/Bestückungsdaten können individuell bereitgestellt werden. Alternativ läuft die Firmware auch auf einem **ESP32-S3-DevKitC-1** mit extern angeschlossenen INMP441-Mikrofonen und einem MAX98357A-Verstärker — siehe [`satellite-esp/ENTWICKLUNG.md`](satellite-esp/ENTWICKLUNG.md) für den Build-/Flash-Weg.

**Optional aber empfohlen:**

- Ollama auf einem stärkeren Rechner (LLM/Smalltalk)
- Telegram-Bot-Token (für Fernzugriff, via [@BotFather](https://t.me/BotFather))
- faster-whisper-server auf einer stärkeren GPU (deutlich bessere STT-Qualität als lokal auf dem Pi)

---

## Installation

Jede Komponente installiert sich über einen einzigen Befehl. Das Skript zieht sich das aktuellste Release vom Update-Server und richtet alles ein: venv, systemd-Service, System-User, Config-Verzeichnis. Root-Rechte werden benötigt.

```bash
curl -fsSL https://raw.githubusercontent.com/NurPech/hannah/master/core/deploy/install.sh | sudo bash        # Hannah Core (Pflicht)
curl -fsSL https://raw.githubusercontent.com/NurPech/hannah/master/proxy/deploy/install.sh | sudo bash       # Proxy (optional)
curl -fsSL https://raw.githubusercontent.com/NurPech/hannah/master/telegram/deploy/install.sh | sudo bash    # Telegram-Bot (optional)
curl -fsSL https://raw.githubusercontent.com/NurPech/hannah/master/voiceid/deploy/install.sh | sudo bash     # Sprechererkennung (optional)
curl -fsSL https://raw.githubusercontent.com/NurPech/hannah-webui/refs/heads/main/deploy/install.sh | sudo bash  # WebUI (optional, auch als Docker-Container verfügbar)
```

**Disclaimer:** Für den Update-Server gibt es keine garantierte SLA — läuft auf Best-Effort-Basis.

Konfiguration, Deinstallation und Details je Komponente: siehe deren eigenes README ([core](core/README.md), [proxy](proxy/README.md), [telegram](telegram/README.md), [voiceid](voiceid/README.md)).

---

## Firmware-Entwicklung

ESP-IDF (C), FreeRTOS, ESP32-S3. Umgebung aktivieren und Devkit-Build:

```bash
# ESP-IDF-Umgebung (Pfad je nach Installation)
export.ps1   # oder export.sh unter Linux/macOS

cd satellite-esp
idf.py -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;sdkconfig.defaults.devkit" build
```

Details, Pinbelegung und Debugging-Hinweise: [`satellite-esp/ENTWICKLUNG.md`](satellite-esp/ENTWICKLUNG.md).

---

## Roadmap

Aktueller Stand und Änderungshistorie: [`CHANGELOG.md`](CHANGELOG.md). Offene Ideen/Bugs werden im internen Issue-Tracker geführt und hier bewusst nicht dupliziert, um nicht wie dieses README selbst zu veralten — aktiv in Arbeit sind unter anderem eine proaktive Trigger-Engine, Szenen und ein Skill-System für Drittanbieter-Erweiterungen.

---

## Feedback & Mitmachen

Hannah ist ein Hobbyprojekt, das über Zeit gewachsen ist. Fragen, Ideen, Bug-Reports: gerne als [GitHub Issue](https://github.com/NurPech/hannah/issues). Pull Requests sind willkommen — die Entwicklung findet an anderer Stelle statt und wird zyklisch hierher gespiegelt; PRs werden manuell zurück übernommen.
