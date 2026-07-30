# Hannah — Projekt-Kontext

## Überblick

Hannah ist ein **lokal betriebener, deutschsprachiger Sprachassistent** für das Smart Home (selbst gehosteter Ersatz für Google Assistant / Amazon Echo). Betrieben auf einem Raspberry Pi, integriert mit ioBroker. Satelliten sind ESP32-S3-Geräte, die per Wake-Word / PTT Sprachbefehle aufnehmen und an Hannah Core senden.

**Langfristiges Ziel:** Vollständige Bidirektionalität (Sprache + Text), Persönlichkeit, autonome Aktionen, Speaker-ID, proaktives Verhalten.

**Persona:** Hannah ist die echte Mitbewohnerin der Entwicklerin. Die KI-Hannah ist freundlich, direkt und eigenständig — studiert Informationswissenschaft an der TU München. Persona-Text: `core/forum_post.md`.

---

## Repository-Struktur

```
hannah/                          ← Mono-Repo
├── core/                        ← Hannah Core (Python, Raspberry Pi)
│   ├── hannah/
│   │   ├── nlu.py               ← NLU (regelbasiert)
│   │   ├── iobroker.py          ← ioBroker REST API Client (Port 8093)
│   │   ├── mqtt_handler.py      ← MQTT Pub/Sub
│   │   ├── udp_server.py        ← UDP Audio-Empfang von Satelliten
│   │   ├── grpc_server.py       ← gRPC Server
│   │   ├── tts.py               ← TTS (Azure / Piper)
│   │   ├── stt.py               ← STT (faster-whisper)
│   │   ├── llm.py               ← LLM-Integration (Ollama)
│   │   ├── car_tracker.py       ← Auto-Status per MQTT (VW Connect)
│   │   ├── user_registry.py     ← SQLite-Benutzer-Registry
│   │   ├── residents.py         ← ioBroker Residents-Adapter
│   │   ├── conversation.py      ← Konversations-Kontext (Multi-Turn)
│   │   ├── memory.py            ← Langzeit-Erinnerungen (SQLite)
│   │   ├── trigger_engine.py    ← Proaktive Trigger
│   │   ├── routines.py          ← Geplante Routinen (SQLite, hannah.db)
│   │   ├── room_manager.py      ← Räume/Gruppen/Satellit-Zuordnung (SQLite)
│   │   ├── alarms.py            ← Wecker (AlarmManager)
│   │   ├── ble_location.py      ← BLE-Indoor-Lokalisierung
│   │   ├── tool_agent.py        ← LLM-Tool-Calling-Agent (ioBroker-Aktionen)
│   │   ├── weather.py           ← Wetter (OpenWeatherMap)
│   │   └── audio.py             ← Audio-Utilities (Resampling, VAD)
|   ├── main.py                  ← Einstiegspunkt, Orchestrierung
│   └── config.yaml              ← Infra-/Bootstrap-Config; NLU-Wortlisten, llm.system_prompt, ble.tags, cars und iobroker.state_names liegen in der DB (hannah.db, Settings-Tabelle) statt hier — siehe GetSettings/UpdateConfig gRPC. nlu/iobroker.state_names werden bei leerer DB automatisch mit generischen Defaults befüllt (SettingsManager.seed_defaults(), #114)
│
├── proxy/                       ← Go gRPC-Proxy (UDP-Satelliten → gRPC → Core)
│
├── satellite-esp/               ← ESP32-S3 Firmware (ESP-IDF, C)
│   ├── main/main.c
│   ├── components/
│   │   ├── hannah_net/          ← WiFi, MQTT-Discovery, UDP-Streaming
│   │   ├── hannah_audio/        ← PDM-Mic (SPH0641 ×2), Speaker (MAX98357A), VAD (WebRTC/libfvad via AudioLib)
│   │   ├── hannah_led/          ← WS2812B LED-Ring State-Machine
│   │   ├── hannah_sensors/      ← BME680 + BSEC2 (IAQ, CO₂eq, VOCeq), I2C
│   │   ├── hannah_wakeword/     ← microWakeWord (TFLite Micro)
│   │   └── microfrontend/       ← Audio-Frontend (Spektrogramm für WW)
│── hardware/
│   ├── Phase2/              ← KiCad PCB Rev. 4 (aktuell verbaut)
│   └── Enclosure/           ← FreeCAD Gehäuse
│       ├── Enclosure.FCStd
│       └── Enclosure-Deckel.step
│
├── satellite-pi/                ← Raspberry Pi Satellit (Python, Legacy)
├── telegram/                    ← Telegram-Bot Microservice (Python)
├── voiceid/                     ← Speaker-ID Service (Python)
├── iobroker.hannah/             ← ioBroker-Adapter (TypeScript, eigenes Repo als Submodule)
└── scripts/                     ← Build- und Release-Scripts
```

---

## Software-Architektur

### Hannah Core (`core/`)

Python-Dienst auf Raspberry Pi. Datenfluss:

```
Satellit (Audio/PTT)
    → UDP (raw PCM, 16kHz, 16-bit, mono)
    → STT (faster-whisper)
    → NLU (regelbasiert, nlu.py)
    → Intent-Handler (ioBroker / Antwort-Generator)
    → TTS (Azure Cognitive Services oder Piper)
    → UDP/gRPC zurück zum Satelliten
```

**Schlüsselmodule:**
- `nlu.py` — Drei-Ebenen-Matching: Raum + Gerätename + Aktion. Intents: TurnOn/TurnOff/SetLevel/SetColor, QuerySensor, CarQuery, WeatherQuery u.a.
- `iobroker.py` — Lädt Gerätebaum via REST (`/v1/enum/rooms`, `/v1/state/`). States per `PATCH` setzen (ack=false).
- `grpc_server.py` — Servicer für externe Services (Telegram, Proxy). Subscriber-Registry für Event-Streams.
- `trigger_engine.py` — Abonniert ioBroker States per MQTT, prüft die `triggers`-Tabelle (SQLite, hannah.db), feuert Aktionen.
- `user_registry.py` — SQLite, synct Roomies aus Residents-Adapter, speichert Trust-Level und Linked Accounts.

**STT/TTS:**
- STT: `faster-whisper` (lokal, 16kHz Mono, kein Upsampling nötig)
- TTS: Azure Cognitive Services (Hauptpfad) oder Piper (lokal, niedrigere Qualität)
- Piper-Modelle: `kerstin-medium` / `thorsten-medium` (22050Hz)

### Satellite Firmware (`satellite-esp/`)

ESP-IDF (C), FreeRTOS, **ESP32-S3** (AI-Beschleuniger + mehr RAM benötigt).

| Komponente | Funktion |
|---|---|
| `hannah_net` | WiFi STA, MQTT-Discovery via `hannah/server` (retained), UDP-Registrierung + Heartbeat (30s) |
| `hannah_audio` | I2S0 PDM-Mic (SPH0641 ×2), I2S1 (MAX98357A). PTT: halten → streamen, loslassen → audio_end. VAD (WebRTC/libfvad, AudioLib) für Silence-Erkennung im Stream |
| `hannah_led` | WS2812B, 7 Zustände: BOOT / IDLE / WAKE / STREAM / SPEAK / MUTE / ERROR |
| `hannah_sensors` | BME680 + BSEC2 (IAQ, Static IAQ, CO₂eq, VOCeq, Accuracy), I2C |
| `hannah_wakeword` | microWakeWord (TFLite Micro) — produktiv seit v0.6.0 |

**ESP-IDF Umgebung aktivieren:**
```powershell
C:\esp\v6.0.1\esp-idf\export.ps1
```

---

## Kommunikations-Protokoll

### UDP (Satellit ↔ Hannah Core)

| Typ | Inhalt |
|---|---|
| `0x01` | JSON-Steuernachricht (Register, Heartbeat, Status) |
| `0x02` | Audio-Chunk (raw PCM, 16kHz, 16-bit, mono) |
| `0x03` | TTS-Audio-Chunk von Hannah → Satellit |

Registrierung:
```json
{"type": "register", "device": "kueche-esp", "room": "Küche", "ip": "...", "port": 5005}
```

Kein TLS auf UDP (zu teuer für ESP32, im LAN akzeptabel).

### gRPC (Hannah Core ↔ externe Services)

- **Proto:** eigenes Repo [hannah-proto](https://dev.kernstock.net/gessinger/voice/hannah-proto) (seit #43) — einzige Source of Truth, nach Scope in mehrere `.proto`-Dateien aufgeteilt (`shared`, `user_registry`, `control`, `car_state`, `event_stream`, `satellite_proxy`, `device_control_menu`, `satellite_provisioning`, `speaker_enrollment`, `agent`, `wakeword_capture`, `timer_service`), per `import` verknüpft; `hannah.proto` selbst enthält nur noch den einen `service HannahService`. Distribution ausschließlich über Package-Registries (#60): `pip install hannah-proto` (PyPI, **Core** + **Telegram**), `github.com/NurPech/hannah-proto-go` (eigenes Go-Modul-Repo, **Proxy**), `npm i @m1kad0/hannah-proto` (ts-proto-Codegen, **iobroker.hannah**). Alle vier Komponenten sind auf die jeweiligen Packages umgestellt — kein lokaler Codegen, kein Submodule-Bezug mehr für Proto irgendwo im Monorepo. Public-Mirror-Pendant: [github.com/NurPech/hannah-proto](https://github.com/NurPech/hannah-proto) (Push per `sync:public` CI-Job bei Tag im `hannah-proto`-Repo selbst).
- **Port:** 50051 (lokal)

| Methode | Funktion |
|---|---|
| `SubmitText` / `SubmitVoice` | Text/Voice-Befehl → Intent + Antwort |
| `Announce` / `Notify` | TTS-Ansage an Satellit(en) / System-Notification von ioBroker |
| `GetDevices` / `ControlDevice` | Geräteliste für Steuer-Menüs / direktes Setzen eines State (umgeht NLU) |
| `GetUsers` / `GetUser` / `LinkAccount` / `UnlinkAccount` / `SetTrustLevel` / `SetSystemMessages` | User-Registry-Verwaltung |
| `GetSatellites` | Liste aller registrierten Satelliten inkl. Hardware-Serial |
| `GetCarState` / `GetAllCarStates` | Live-Autostatus (VW Connect) |
| `SubscribeEvents` | Server-Side Stream für Events (`car.parked`, `resident.arrived`, `satellite.firmware`, …) |
| `TriggerFirmwareUpdate` | Erzwingt sofortiges OTA-Update für einen Satelliten |
| `RequestSatelliteCapture` / `ReleaseSatelliteCapture` / `StreamSatelliteAudio` / `TriggerPlink` | Wakeword-Trainingsdaten-Sammlung: Satellit in Capture-Modus versetzen, Rohaudio durchreichen, geführtes Plink+PTT für hey-hannah-Aufnahmen |
| `RegisterProxy` | Bidirektionaler Keep-alive-Stream. Proxy sendet Heartbeats, Hannah sendet `PlayAudioCommand` zurück. Solange offen: UDP-Server deaktiviert; bei Disconnect: UDP reaktiviert. |
| `SubmitSatelliteAudio` | Proxy reicht Audio durch → STT+NLU+TTS in einer Transaktion |
| `NotifySatelliteRegistered` / `NotifySatelliteGone` | Proxy meldet Satellit-Connect/Disconnect |
| `ProvisionSatellite` | Adapter pre-registriert Seed + Displayname + Raum vor dem WebFlash (Issue #26) |
| `EnrollVoiceprint` | Sprach-Enrollment für Speaker-ID |
| `TimerConnect` | Bidirektionaler Stream zum Timer-Service (Timer/Wecker-Events) |
| `AgentConnect` | Bidirektionaler Stream zum ioBroker-Adapter — State-Updates rein, Control-Commands + `resident_answered`-Events raus |

### MQTT (Hannah Core ↔ Satelliten)

**Steuerkommandos laufen grundsätzlich über MQTT, nicht UDP** — einziger Kanal, der sowohl UDP-direkte als auch Proxy-verbundene Satelliten erreicht (siehe Architektur-Entscheidung 9, entstanden aus Issue #18).

| Topic | Zweck |
|---|---|
| `hannah/server` (retained) | Discovery: Proxy-Host:Port |
| `hannah/announce` / `hannah/announceSSML` | Extern → Core: Raum-Announcement (Text/SSML) |
| `hannah/notification` | Extern → Core: System-Notification (severity: alert/notify/info) |
| `hannah/volume` (+ `/state`) | Globale Lautstärke setzen/lesen |
| `hannah/satellite/{device}/announcement` | Extern → Core: Text-Announcement an einen einzelnen Satelliten |
| `hannah/satellite/{device}/volume/set` (+ `/state`) | Lautstärke pro Satellit |
| `hannah/satellite/{device}/mute/set` (+ `/state`) | Mute pro Satellit |
| `hannah/satellite/{device}/dnd` (+ `/state`) | Do-Not-Disturb |
| `hannah/satellite/{device}/listen` | Core → Satellit: virtuelles PTT aktivieren (nach TTS-Frage, Issue #18) |
| `hannah/satellite/{device}/ptt` | Core → Satellit: virtuelles PTT an/aus |
| `hannah/satellite/{device}/sampling` | Core → Satellit: Wakeword-Trainingsdaten-Sammelmodus |
| `hannah/satellite/{device}/play_asset` | Core → Satellit: Sound-Asset abspielen |
| `hannah/satellite/{device}/ota/pending` / `/ok` | OTA-Update-Anfrage/-Freigabe |
| `hannah/satellite/{device}/firmware` | Satellit → Core: aktuelle Firmware-Version |
| `hannah/satellite/{device}/playback_done` | Satellit → Core: eine Wiedergabe (TTS/Plink/Asset) ist auf dem Speaker wirklich fertig abgespielt (DMA drained) — generisches Ack, ersetzt geratene feste Sleeps (z.B. `TriggerPlink`, #155) |
| `hannah/satellite/{device}/ble/watchlist` / `/report` | BLE-Scanner-Konfiguration / Scan-Treffer |
| `hannah/satellite/{device}/sensors` | Satellit → Core: BME680/BSEC2-Sensordaten |

### Asset-Server (Hannah Core ↔ ESP-Satelliten)

HTTP-API für Sound-Assets. Authentifizierung per Token.

| Endpunkt | Funktion |
|---|---|
| `GET /manifest?namespace=satellite` | Manifest nur für Satellite-Assets |
| `GET /manifest?namespace=core` | Manifest nur für Core-Assets |
| `GET /manifest` | Vollständiges Manifest (alle Namespaces laut Token-Rechten) |
| `GET /asset/$key` | Asset-Download per Key aus dem Manifest |

**Namespaces:**
- `satellite` — Sounds für den ESP-Satelliten (werden gecacht in LittleFS)
- `core` — Sounds für Hannah Core

**Manifest-Format (v1):**
```json
{
  "version": 1,
  "generated_at": "2026-06-04T12:37:05Z",
  "assets": {
    "timer_jingle": {
      "namespaces": ["satellite"],
      "sha256": "e137b95866524a68181f9371070b2df72669f58d7f0144c31b333739e12ea3eb",
      "size": 30960,
      "mime": "audio/wav",
      "meta": {
        "duration_s": 0.97,
        "sample_rate": 16000,
        "channels": 1,
        "bits_per_sample": 16
      }
    }
  }
}
```

`GET /manifest` ohne Parameter liefert alle Assets namespaceübergreifend — damit kann Hannah Core Metadaten (z.B. `duration_s`) generisch abfragen ohne Namespace hartcodieren zu müssen. Abwärtskompatibel: namespace-gefilterte Abfragen funktionieren weiterhin.

### ioBroker-Integration

Hannah bekommt die Geräte über gRPC von dem Adapter über gRPC gemeldet. Sämtliche ioBroker-Integration läuft über den Adapter.

**Wichtige gRPC-Methode:**
- `AgentConnect`: Bidirektionaler Stream zwischen Adapter und Hannah (Nachrichtentyp `AgentMessage` rein, `AgentCommand` raus)

#### Sensor-/Geräte-Datenfluss (Debugging-Referenz)

Vollständiger Pfad von einem Satelliten-Sensorwert bis zur Sprachantwort — als Referenz, weil der Pfad durch mehrere Repos/Prozesse läuft und Debugging sonst sehr lange dauert (Bug-Suche zum Air-Quality-Feature, 2026-06-20, hat zwei unabhängige Bugs über diesen Pfad zutage gefördert).

1. **Satellit → MQTT**: Sensor-Task published periodisch auf `hannah/satellite/{device}/sensors`.
2. **MQTT → Hannah Core**: Core ist Subscriber, leitet weiter per gRPC `AgentSensorUpdate` an den Adapter.
3. **Adapter → ioBroker (Rohdaten)**: `iobroker.hannah/src/sensors.ts` (`SensorWatcher.handleSensorUpdate`) schreibt die Rohwerte nach `hannah.<instance>.satellites.sensors.<device>.*` (`temperature`, `humidity`, `pressure`, `iaq`, `iaq_accuracy`, `co2_equiv`, `voc_equiv`).
4. **Optional: VirtualDevice-Mirror**: eigene ioBroker-Scripte (z.B. `javascript.0.virtualDevice.AirQuality.*`, `Temperaturen.*`) spiegeln diese Rohwerte in die `virtualDevice`-Struktur, damit sie über das normale Enum-System (Schritt 5) für Hannah sichtbar werden. Nur nötig, wenn der Wert über die generische Kategorie-Abfrage (NLU) erreichbar sein soll.
5. **Enum-Discovery (Adapter)**: `state-watcher.ts` → `_subscribeEnumStates()` liest `enum.rooms.*`/`enum.functions.*` **einmalig beim Adapter-Start** (kein Live-Listener auf Enum-Änderungen!). Neue Geräte/Funktionen, die angelegt werden während der Adapter schon läuft, werden nie abonniert — **Adapter-Neustart nötig**, damit neue States überhaupt live ankommen.
6. **Device-Type-Erkennung**: `_resolveDeviceMeta()`/`resolveType()` ermittelt die Hannah-Kategorie pro State: zuerst `common.custom["<adapter-namespace>"]` (z.B. `"hannah.0": {enabled: true, type: "..."}` — offizielle ioBroker-Doku-Konvention, **nicht** ein loses `common.hannah`-Feld, das nicht garantiert persistiert wird), dann `common.role` (feste Liste), dann Function-Namen-Keywords als Fallback.
7. **Live-Updates (Adapter)**: `onStateChange()` prüft `ack` — bei Enum-discovered Device-States (`subscribedIds`) wird `ack:false` verworfen (Schutz vor Feedback-Schleifen, da Hannah selbst Befehle mit `ack:false` schreibt, siehe `handleSetState`). Bei `AgentWatchMore`-States (`watchMoreIds`, trigger_engine) wird jede Änderung weitergeleitet, unabhängig von `ack` — Hannah schreibt diese nie selbst. Manuell gesetzte Flags ohne bestätigendes Gerät (z.B. `0_userdata`-Booleans) bleiben bei `ack:false` stehen, wenn das Script sie nicht explizit mit `ack:true` setzt.
8. **Adapter → Hannah Core (gRPC)**: `AgentStateUpdate` über den `AgentConnect`-Stream.
9. **Hannah Core (Empfang)**: `main.py:_on_agent_state` → `_on_state_update` → ruft beides auf: `iobroker.handle_state_update()` und `trigger_engine.on_state_update()`.
10. **Hannah Core (Cache)**: `IoBrokerClient.handle_state_update()` (`core/hannah/iobroker.py`) übersetzt den rohen State-Suffix über `config.yaml`'s `iobroker.state_names` zurück auf den kanonischen Key, bevor `device.current[canon]` geschrieben wird. **Fehlt der Suffix in `state_names`, wird das Update lautlos verworfen** — der Wert bleibt für immer auf dem Stand des letzten Snapshots stehen (`handle_device_snapshot` schreibt den rohen Suffix direkt als Key, ohne diese Übersetzung). Neue Sensor-/Kategorie-Felder brauchen also **immer** einen `state_names`-Eintrag, auch wenn der Name 1:1 identisch zum ioBroker-Suffix ist.
11. **NLU-Abfrage**: `category_filter` (`nlu.py`) + `_CATEGORY_STATES`/`_describe_category` (`iobroker.py`) lesen `device.current` und bauen die Sprachantwort.

**Häufigste Fallstricke:**
- Neuer State-Suffix ohne `state_names`-Eintrag → Wert friert nach dem ersten Snapshot ein
- Neues Gerät/Enum-Mitglied nach Adapter-Start angelegt → nie abonniert, Adapter-Neustart nötig
- `common.custom`-Override ohne `enabled: true` → wird von ioBroker verworfen
- Gerätename überlappt mit Raumnamen (z.B. Licht "Bad" in Raum "Bad oben") → Sprachantwort klingt doppelt; kein Bug, sondern Datenmodellierung (fehlende Geräte-Ebene im virtualDevice-Pfad)
- Direkt gesetzter State ohne `ack:true` über den Enum-discovered-Device-Pfad → wird verworfen (über den WatchMore-Pfad inzwischen nicht mehr)

---

## Hardware

### PCB-Revisionen

| Rev | Status | Größe | Anmerkung |
|---|---|---|---|
| Rev. 1 | Prototyp (nicht bestellt) | — | Machbarkeitsstudie |
| Rev. 2 | Geliefert | 114mm rund | Maßfehler (57mm Radius statt 75mm Durchmesser); nur Elektrotest |
| Rev. 3 | Geliefert, enthält Bugs | 88mm rund | Zieldesign; nicht nutzbar |
| Rev. 4 | Geliefert, aktuell verbaut | 88mm rund | Zieldesign |
| Rev. 5 | Bestellt bei PCBWay, Fertigung ausstehend | 88mm rund | 4× TDM-Mikrofone (ADAU7118), WROOM-1U (externe Antenne), USB-C→Lötpads, SD-Slot-Orientierung gefixt |

### PCB Rev. 4 (`hardware/Phase2/`)

**Abmessungen:** 88mm Durchmesser, eine Seite leicht abgeflacht (USB-C). 4-lagig: F.Cu / In1.Cu (GND) / In2.Cu (3.3V) / B.Cu. ENIG.

**Oberseite:**
- 4× ALPS ALPINE SKRPABE010 SMD-Taster (Mute, Vol+, Vol−, PTT) — brauchen Membran im Deckel
- ~24× WS2812B LED-Ring
- MAX98357A (I2S-Verstärker)
- BME680 (Temp, Feuchte, Luftdruck, Gas-Widerstand, I2C)

**Unterseite:**
- ESP32-S3-WROOM-1-N16R8
- 2× SPH0641LU4H-1 (PDM-Mikrofon)
- LD2410 (mmWave-Radar, 24GHz, UART) — Female Header
- AMS1117-3.3 SOT-223
- SN74AHCT125D (Level-Shifter 3.3V→5V für WS2812B) — auf B.Cu
- USB-C HRO TYPE-C-31-M-12
- 2-Pin JST PH (Speaker), 6-Pin Header (optionaler externer Verstärker)
- 2× ALPS SKRPABE010 für EN + IO0 (Boot/Reset) — ab Rev. 5/6 entfallen (OTA)
- 4× Montagelöcher

**Geparkte Ideen:**
- RP2350 als Co-Prozessor — **definitiv verworfen**, ESP32-S3 reicht

### PCB Rev. 5 (`hardware/Phase2/`) — bestellt bei PCBWay, Fertigung ausstehend

Gleiche Grundabmessung wie Rev.4 (88mm rund), passt ins bestehende Gehäuse. Änderungen ggü. Rev.4, GPIO-Belegung siehe `satellite-esp/sdkconfig.defaults.rev5` (von Leonie bestätigt, Issue #160):

- **Chip:** ESP32-S3-**WROOM-1U**-N16R8 (externe Antenne) — Rev.4 nutzte die interne-Antenne-Variante ohne "U"
- **Mikrofone:** 4× SPH0641LU4H-1 (PDM) → **ADAU7118** (PDM→TDM-Wandler, LFCSP-16 3×3mm) → TDM direkt an ESP32-S3 I2S (Port 0, WS=GPIO12, BCK=GPIO13, DATA=GPIO14) — ermöglicht Beamforming. Ersetzt die 2× PDM-Mics mit gemeinsamer Clock/Data-Leitung von Rev.4
- **Tasten neu belegt:** Die TDM-Mikrofonleitungen belegen jetzt GPIO12–14 (vorher PTT/Vol+/Vol− auf Rev.4) → umverdrahtet auf PTT=GPIO40, Vol+=GPIO39, Vol−=GPIO18 (Mute bleibt GPIO11)
- **Status-LED:** eigener Pin GPIO1 (Rev.4-Default GPIO18 ist jetzt Vol−). Neue zweite, rein passive Power-LED (R12) fest an 3.3V/GND, kein GPIO nötig
- **LED-Ring:** SK6812MINI-**RV** (Rev.4: SK6812MINI-E) — durch die neuen/umsortierten Komponenten mussten die LEDs neu verkabelt werden; 5V-Trace-Breite platinenweit auf 0,8mm vergrößert
- **SD-Karten-Slot:** gleiche GPIOs wie Rev.4 (CS=4, MOSI=5, CLK=6, MISO=7), aber anderer Steckertyp — der auf Rev.4 verbaute Slot hatte seine Lötpads auf der offenen (nach innen zeigenden) Seite, was Traces unnötig unter der Karte durchlaufen ließ; neuer Slot hat die Lötpads auf der gegenüberliegenden Seite und vereinfacht dadurch das Routing
- **USB-C entfällt:** stattdessen Lötpads 5V + GND für externes Kabel zu Panel-Mount USB-C im Gehäuse; AMS1117 + Sicherungen bleiben; UART0 (Debug-Header J4) bleibt fürs Erst-Flashen
- **Unverändert ggü. Rev.4:** Speaker (MAX98357A), BME680-Sensor (teilt sich den I2C-Bus jetzt mit dem ADAU7118), LD2410-Radar
- **OTA-Channel:** in `sdkconfig.defaults.rev5` bewusst nicht gesetzt — übernimmt den Server-Default `satellite-esp-stable`, sobald Rev.4 vollständig auf seinen eigenen Channel (`satellite-esp-stable-rev4`) umgezogen ist (Refs #160)

### Wichtige Bauteil-Entscheidungen

| Entscheidung | Gewählt | Verworfen | Grund |
|---|---|---|---|
| Mikrofon | SPH0641 (PDM) | INMP441 (I2S) | INMP441 EOL, nicht mehr bei LCSC |
| Wake-Word ESP | microWakeWord | OWW ONNX | OWW benötigt Google Speech Embedding, zu rechenintensiv |
| LED-Typ Rev. 3 | SK6812-Mini-E | WS2812B | SK6812 ist 3.3V-kompatibel → Level-Shifter entfällt |
| Prozessor | ESP32-S3 | Original ESP32 | S3 hat AI-Beschleuniger + PSRAM (nötig für TFLite) |
| Audio-ADC Rev. 3 | ES7210 | STM32G031 | STM32G031 hat kein DFSDM für PDM |
| Gehäuse | Eigenes FreeCAD | FPH Satellite1 | FPH nutzt XMOS + ESPHome, inkompatibel |
| Satellit-Platform | ESP32-S3 Custom PCB | Pi Zero 2 W | Kosten (~4€ vs 18€), Verbrauch (0.1W vs 1W) |

### Gehäuse (`satellite-esp/hardware/Enclosure/`)

Zweiteilig: **Topf (Unterteil) + Deckel (Oberteil)**, eigenes FreeCAD-Design orientiert an FPH Satellite1 (Innendurchmesser 88mm, 4 Montageloch-Bosses).

**Status: Für Rev. 4 fertig geplant und gedruckt** (Topf + Deckel), keine offenen Punkte mehr. Speaker-Öffnung am Topf fertig modelliert, Speaker-Gitter separat gedruckt (steckbar, auswechselbar). Deckel: Ring-Schlitz für Membran-Taster (1mm — 0.5mm war zu eng für 0.4mm Nozzle), USB-C-Ausschnitt, 4× Montagelöcher — alles modelliert.

**Taster-Geometrie:** ALPS SKRPABE010, Höhe 2.5mm. Pocket-Tiefe 2.5mm, Membrandicke 0.5mm, Deckeldicke 3mm → PCB-Oberseite liegt direkt an Deckel-Innenfläche an (keine Stützen nötig). Fixierung durch 4 Montageschrauben.

**FreeCAD-Workflow:**
1. KiCad STEP exportieren → FreeCAD importieren
2. Maße per Python-Konsole: `obj.Shape.BoundBox`
3. Part Design: Sketch → Pad (Körper), Pocket (Aussparungen)
4. Sketch muss vor Pad/Pocket vollständig geschlossen und bestimmt sein (keine roten Linien)
5. STEP export für Weitergabe, STL für 3D-Druck

**Druckmaterial:** PLA (Prototyp), PETG (finale Version).

---

## Architektur-Entscheidungen

1. **gRPC für externe Services** — MQTT bleibt für ioBroker/Events; gRPC wo bidirektionales Streaming oder Typed API benötigt wird. Eingeführt April 2026 mit Telegram-Microservice.
2. **Telegram als eigenständiger Microservice** — separater Lebenszyklus, Absturz darf Hannah nicht mitreißen.
3. **RegisterProxy als Keepalive + Push-Kanal** — Proxy sendet Heartbeats, Hannah schickt `PlayAudioCommand` über denselben Stream zurück. Solange verbunden: UDP deaktiviert.
4. **User-Registry mit Trust-Level** — UUID, Roomie-ID, Trust-Level (0–10), Linked Accounts. Differenzierte Berechtigungen (z.B. "Alarm deaktivieren" nur ab Level 8).
5. **STT lokal (faster-whisper)** — kein Cloud-Zwang. 16kHz Mono, kein Upsampling nötig.
6. **LLM optional via Ollama** — Fallback: DummyLLM. Regelbasiertes NLU funktioniert ohne LLM.
7. **Virtuelle Devices statt Enum-API** — drei-gliedriges Matching (Raum + Gerät + Aktion), Sensor-States pro Gerät.
8. **16kHz Mono, kein TLS auf UDP** — Modell-Anforderung (Whisper, OWW); UDP ohne TLS spart RAM/CPU auf ESP32.
9. **MQTT als universeller Steuerkanal** — alle Steuerkommandos (mute, volume, ptt, sampling, listen, …) laufen über MQTT statt UDP, weil UDP nur direkt verbundene Satelliten erreicht, MQTT aber auch Proxy-verbundene. TTS-Audio wird weiterhin korrekt je nach Verbindungstyp geroutet. Erkannt durch Issue #18.

---

## Git-Workflow

Gilt für das Hannah-Mono-Repo **und** das `iobroker.hannah`-Submodule (eigener Branch-Stand).

1. **Nie direkt auf `master`/`main` arbeiten.** Immer zuerst einen Feature-/Topic-Branch anlegen — in beiden Repos.
2. **Changelog parallel zu jeder Änderung pflegen.** `CHANGELOG.md` im Hannah-Repo, `README.md` im `iobroker.hannah`-Submodule — jeweils Englisch, WIP-Sektion. `en.json` im Adapter manuell pflegen; andere Sprachen kommen vom ioBroker-Translator.
3. **In sinnvollen, thematisch gruppierten Paketen committen** — kein Mega-Commit, kein Commit-pro-Keystroke. Changelog-Eintrag gehört in denselben Commit wie die Änderung, die er dokumentiert.
4. **Commits nur auf explizite Anfrage**, nie unaufgefordert.
5. **Pushen, wenn:** die Arbeit abgeschlossen ist, ODER eine Pause eingelegt wird, ODER Arbeit dezentral gesichert werden soll (auch zwischendurch, auf Zuruf) — Standard-Erwartung, kein Einzelfall.
6. **Landung auf master ausschließlich über Merge Request (Hannah-Repo) bzw. Pull Request (`iobroker.hannah`-Submodule).** Nie direkt mergen.
7. **Für jede funktionale Änderung muss ein Work Item existieren.** Anlegen ist Aufgabe von Claude, proaktiv, bevor mit der Umsetzung begonnen wird — nicht erst hinterher. Tracker richtet sich danach, was geändert wird:
   - Änderungen am Mono-Repo (Core, Proto, Firmware, ...) — auch wenn sie nebenbei den Adapter mit anfassen: **GitLab Issue, project 319.**
   - Reine `iobroker.hannah`-Adapter-Änderungen (nichts im Mono-Repo angefasst): **GitHub Issue im Adapter-Repo** (`NurPech/ioBroker.hannah`), nicht GitLab — sonst im Adapter-Repo nicht nachvollziehbar.
8. **Commit Messages bei funktionalen Änderungen referenzieren das Work Item** mit `Refs #ID` (im jeweils zuständigen Tracker aus Punkt 7).
9. **MR/PR-Beschreibungen schließen das Work Item** mit `Closes #ID`.
10. **Adapter-Changelog (`README.md` in `iobroker.hannah`) enthält keine Issue-/Ticket-Referenzen**, egal welcher Tracker — das sind öffentliche, nutzerseitige Release Notes (npm/ioBroker-Nutzer ohne Zugriff auf interne Tracker), keine internen Entwickler-Notizen.
11. **Der Submodule-Pointer im Hannah-Repo (`iobroker.hannah` — einziges verbliebenes Submodule) zeigt immer auf einen Release-Tag**, nie auf einen Branch-/Feature-Commit. Ablauf: PR im Submodule mergen → Release schneiden (Tag entsteht) → erst dann den Pointer im Hannah-Repo auf diesen Tag bumpen (eigener, fokussierter Commit, getrennt von der eigentlichen Feature-Arbeit). **Dabei immer auch das `branch`-Feld in `.gitmodules` auf denselben Tag aktualisieren** — sonst hält Renovate den Pointer für veraltet (vergleicht gegen `.gitmodules`) und versucht ihn auf den alten Tag zurückzudrehen.

Punkte 7–9 gelten für funktionale Änderungen (Features, Bugfixes) — nicht für reine Doku-/Chore-Änderungen wie diesen Abschnitt selbst.

Nach MR-Erstellung: siehe CI-Pipeline-Hinweise unten zur Beobachtung.

### Public Mirror

Damit Dritte Pull Requests schicken können (der eigentliche Zweck des Mirrors), gibt es einen öffentlichen GitHub-Mirror des Hannah-Monorepos: [github.com/NurPech/hannah](https://github.com/NurPech/hannah). Erzeugt/aktualisiert wird er per `scripts/copy_public.py`, das bei jedem Tag automatisch von der CI (`sync:public`-Job) ausgeführt wird. Es kopiert den getrackten Dateibaum, filtert dabei Sensibles raus (Config-Dateien, KiCad-Dateien, `.gitlab-ci.yml`, Binärdateien) und schreibt eine eigene `.gitmodules` fürs Public Repo: Submodule mit einem öffentlichen Pendant würden dabei auf ihre öffentliche GitHub-URL umgebogen (`MIRRORED_SUBMODULES` im Script) — aktuell aber leer, da `iobroker.hannah` (das einzige verbliebene Submodule) bewusst nicht gelistet ist: es ist bereits ein eigenständig verteiltes Projekt (npm/ioBroker-veröffentlicht), keine Build-Abhängigkeit von irgendwas hier, und fällt daher wie jedes nicht gelistete Submodule einfach raus.

`audiolib` und `proto` (`hannah-proto`) sind eigene GitLab-Projekte (project 321 bzw. 338) und keine Submodule des Monorepos mehr — beide werden stattdessen als veröffentlichte Packages konsumiert (ESP-IDF Component Registry bzw. PyPI/Go-Modul/npm, siehe oben). Sie haben in ihren eigenen Repos jeweils einen eigenen `sync:public`-CI-Job (nur bei `$CI_COMMIT_TAG`, kein Filtering nötig, da unkritischer Inhalt), der sie nach [github.com/NurPech/AudioLib](https://github.com/NurPech/AudioLib) bzw. [github.com/NurPech/hannah-proto](https://github.com/NurPech/hannah-proto) synct — unabhängig vom Monorepo-Mirror. Pull Requests von Dritten gegen einen dieser öffentlichen Mirrors werden manuell zurück ins jeweilige private GitLab-Repo cherry-gepickt (bislang rein theoretischer Fall, noch nicht vorgekommen).

---

## Status

Aktueller Versionsstand und Änderungshistorie: siehe `CHANGELOG.md`. Offene Bugs/Features/Ideen: siehe GitLab Issues (project 319, `mcp__gitlab-private-voice__*`) — nicht hier duplizieren, GitLab ist die live abrufbare Quelle.

---

## Bekannte Probleme & Workarounds

### ESP Firmware (IDF 6.0)
- `Phase2/.history` enthält eingebettetes Git-Repo (KiCad History) — nicht committen, `.gitignore` deckt ab.

### FreeCAD
- Sketch vollständig geschlossen + bestimmt vor Pad/Pocket (keine roten Linien).
- Pocket schlägt fehl? Objekt ausblenden (Leertaste), Fläche anklicken, wieder einblenden.
- Korrupte FCStd-Datei: `python -m zipfile -e Datei.FCStd /tmp/extract/` (FCStd ist ZIP-Archiv).

### CI-Pipeline
- Immer mit `/loop` + `ScheduleWakeup` beobachten — **nicht** mit `Monitor`.
- `ScheduleWakeup` funktioniert nur innerhalb eines laufenden `/loop`-Kontexts.
- Intervall: 270s (unter 300s Cache-TTL); bei `test:esp32` eher 300s (Build dauert 5–8 min).
- Nach Pipeline-Ende: MR-Status prüfen; bei `merged` → Release auslösen.
- Auto-Merge nach MR-Erstellung sofort aktivieren.

Siehe `## Git-Workflow` oben für Branch-/Commit-/Push-/Changelog-Regeln.