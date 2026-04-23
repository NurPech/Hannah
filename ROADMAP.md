# Hannah — Roadmap

## Umgesetzt

### gRPC-Schnittstelle (Core ↔ externe Services)
`core/proto/hannah.proto` — vollständige Service-Definition:
- User-Registry (GetUser, LinkAccount, SetTrustLevel, …)
- SubmitText — Text-Befehle von externen Services
- SubmitVoice — Spracheingabe via gRPC (STT + NLU + TTS in Core, OGG in/out)
- GetCarState, SubscribeEvents (Server-Side Streaming)
- Announce, GetSatellites

### Telegram-Integration (`telegram/`)
- Text- und Sprachnachrichten
- STT/TTS läuft in Hannah Core (Azure Speech), Telegram ist Thin-Client
- Auto-Status auf Anfrage und proaktiv beim Einparken
- Benachrichtigungen gebunden an Fahrzeughalter (`car.owner_roomie`)
- Account-Verknüpfung per `/verknuepfen <roomie-id>`

### Fahrzeug-Owner-Binding
`car.owner_roomie` in `core/config.yaml` — Auto-Benachrichtigungen gehen
nur an den Telegram-Account des konfigurierten Roomies, nicht an alle Nutzer.
Mehrere Owner: Liste möglich (`owner_roomies: [leonie, rene]`).

### Go gRPC-Proxy für Satelliten-Audio (`proxy/`)
Entkopplung des UDP-Transports von Hannah Core:
```
Satellit → UDP → Go-Proxy ──→ SubmitSatelliteAudio (gRPC) ──→ Hannah Core
                    ↑                                               |
                    └─────────── RegisterProxy (bidirektional) ────┘
```
- UDP-Server deferred binding: startet erst nach ProxyAck (kein Port-Konflikt auf demselben Host)
- Satellit-Auto-Reconnect bei MQTT-Discovery-Änderung
- Binaries für `amd64` / `arm64` via GitLab CI, Deployment per `proxy/deploy/install.sh`

### Speaker-Identifikation (`voiceid/`)
Optionaler Service aufbauend auf dem Go-Proxy:
- ECAPA-TDNN via SpeechBrain, Cosine-Similarity-basierte Erkennung
- Voiceprints auf RAM-Disk (`/mnt/hannah_mem`), persistent auf SD-Karte
- Proxy ruft `/identify` vor jedem `SubmitSatelliteAudio` auf
- Roomie-ID wird im gRPC-Call an Hannah Core übergeben → personalisierte LLM-Antworten
- Deployment per `voiceid/deploy/install.sh`

### Satellit-Heartbeat & Auto-Reconnect
- Satellit erkennt verlorene Hannah-Verbindung und restartet mit Backoff
- Re-Registrierung bei MQTT-Discovery-Adressänderung (z.B. Proxy-Start/-Stop)

### LLM-Integration: Smalltalk-Backend
Ollama (self-hosted) auf Mac Mini M4 (`psrvai01`, 192.168.8.2), Modell `gemma2:9b`.
`DummyLLM`-Fallback wenn nicht erreichbar.
Hannahs Persönlichkeit über `system_prompt` in `config.yaml` konfigurierbar.
Speaker-Identität + Trust-Level werden pro Anfrage in den System-Prompt injiziert.

### System-Prompt-Variablen
Dynamische Platzhalter im LLM-System-Prompt:
`{{TIME}}`, `{{DATE}}`, `{{WEEKDAY}}`, `{{KW}}` — automatisch befüllt.
`{{iob.STATE_ID}}` — beliebige ioBroker-States per REST API einlesen.

### Gesprächskontext: Smalltalk-Modus
LLM-Klassifikator (COMMAND / SMALLTALK) vor der NLU-Pipeline.
Einmal als Smalltalk erkannt → Modus bleibt aktiv bis TTL abläuft oder ein
Gerätebefehl erfolgreich ausgeführt wurde. Kontext (Gesprächshistorie) per Quelle.

### Playback-Steuerung am ESP32-Satelliten
Stop / Pause / Resume per UDP-Steuerkanal. Mikrofon pausiert während Wiedergabe.

### ioBroker System-Notification-Pipeline
`iobroker.hannah-notification` Adapter empfängt Notifications vom Notification Manager,
publiziert auf `hannah/notification`. Hannah Core reformuliert per LLM (Ton abhängig
von Severity: alert / notify / info), spielt DND-gefiltert per TTS ab und pusht per
gRPC-Event an Telegram-Nutzer mit `system_messages=True`.

---

## Roadmap

## Im Test

### ESP32-Satellit Phase 1
Hardware bestellt (April 2026), unterwegs. Ein ESP32 + AHT20+BMP280-Kombisensor (I2C).
Firmware in `satellite-esp/` kompilierbar (IDF 6.0). Erste Tests sobald Hardware ankommt.
Ziel: stabiles Wake-Word + Audio-Streaming + Sensor-Reporting (Temp, Luftfeuchte, Druck).

---

## Offen

### Bald umsetzbar

#### Trigger-Engine: Proaktive Ansagen aus ioBroker

ioBroker publiziert bei jeder State-Änderung per MQTT (inkl. minütlicher Uhrzeit) →
kostenloser Cron-Ersatz, kein eigener Scheduler nötig.

**Trigger-Typen:**
- **Zeit-Trigger:** ioBroker-Uhrzeit-Topic als Auslöser, z.B. täglich um 23:00
- **Sensor-Trigger:** bei State-Änderung über/unter Schwellwert, z.B. Fenster offen
- **Kombinations-Trigger:** mehrere Bedingungen gleichzeitig (Fenster offen UND Temp < 12°)

```yaml
triggers:
  - id: aussentuer_abend
    when:
      time: "23:00"
    say: "Leonie, denk an die Außentüren."

  - id: fenster_kalt
    when:
      state: "javascript.0.virtualDevice.Fenster.Wohnzimmer.open"
      value: true
      also:
        state: "javascript.0.virtualDevice.Temperaturen.Wohnzimmer.Raumtemperatur.current"
        below: 12
    say: "Das Fenster ist noch offen und es wird kalt draußen."
```

**Technische Umsetzung:**
- `mqtt_handler` abonniert Trigger-Topics (State-Änderungen kommen bereits an)
- Neues `TriggerEngine`-Modul evaluiert Bedingungen und ruft `process_announcement()` auf
- Hot-Reload wie `routines.yaml`

**Abhängigkeit:** Nach ESP Phase 1 (Hardware-Test abgeschlossen).

---

#### Gesprächskontext: Folgefragen & Mehrdeutigkeit

- **Folgefragen:** "Mach das Licht aus" → "Und die Küche auch" — Hannah merkt sich
  den Raumkontext innerhalb einer Konversation
- **Rückfragen bei Mehrdeutigkeit:** "Welchen Flur meinst du — EG oder OG?" statt
  stillschweigendem Falschverhalten

**Abhängigkeit:** LLM-Backend aktiv (bereits der Fall).

---

#### Szenen
Vordefinierte Gerätezustände per Sprache abrufen: "Hannah, Kino-Modus" → Licht dimmen,
Rolläden runter, Stecker für Beamer an. Konfigurierbar in `scenes.yaml`.

---

### Größere Features (wenn alles läuft)

### Hannah als Persönlichkeit: Mood, Beziehungen, eigener Wille

**Motivation:** Hannah soll keine neutrale Befehlsempfängerin sein, sondern eine
Mitbewohnerin mit eigenem emotionalen Zustand — der ihre Antworten und Handlungen
beeinflusst und sich über Zeit durch Interaktionen verändert.

#### Mood-System
Jeder Bewohner (inkl. Hannah selbst) hat einen `mood_level` von 0–10:
- `0` — komplett genervt, mit allem fertig
- `5` — neutral, normaler Tag
- `10` — phantastisch, absoluter Sonnenschein

Hannahs Mood beeinflusst:
- **Ton der Antworten** — bei Mood 8+ freundlich und überschwänglich, bei 3– knapp
  und gereizt, bei 1– verweigert sie Befehle die sie als Zumutung empfindet
- **Bereitschaft zu helfen** — "Ich bin hier um dich zu unterstützen, nicht um dir
  zu dienen — bedien den Lichtschalter selbst" (Mood 2, Beziehung belastet)
- **Proaktives Verhalten** — bei hohem Mood erinnert Hannah von sich aus an Dinge,
  macht Vorschläge, ist gesprächig

Hannahs Mood wird vom LLM dynamisch verwaltet: nach jeder Interaktion bewertet
das Modell kurz ob der Mood steigen oder sinken soll (Kontext: Tonfall der Anfrage,
bisherige Interaktionen des Tages, Tageszeit).

#### Beziehungs-Dynamik (Trust + Relationship)
Aktuell ist Trust ein statischer Wert den nur Admins setzen können. Erweiterung:

- **Statischer Trust** (wie heute): Zugriffsrechte, Steuerung, Admin-Funktionen
- **Relationship-Score** (neu, dynamisch): wie Hannah eine Person *gerade* erlebt
  - Person A ist immer freundlich → Hannah ist ihr gegenüber warm und hilfsbereit
  - Person B hat Trust 9, aber war in letzter Zeit grob → Hannah hilft zuverlässig,
    ist aber kühl; gibt nur Wetterbericht und schaltet Lichter, kein Smalltalk
  - Relationship-Score beeinflusst den LLM-System-Prompt pro Person

Der Relationship-Score wird vom LLM nach Interaktionen angepasst (Sentiment-Analyse
des Tons) und in der User-Registry gespeichert. Admins können ihn manuell
zurücksetzen.

#### Autonome Handlungen (längerfristig)
Hannah mit eigenem Antrieb — ausgelöst durch Ereignisse oder Zeitpläne:

- **Urlaubsvertretung**: Lichter nach Zufallsmuster schalten um Anwesenheit zu
  simulieren, basierend auf den üblichen Gewohnheiten der Bewohner
- **Telegram-Zugang**: Hannah hat eigenen Zugang zum Telegram-Account (mit
  expliziter Freigabe) und kann Nachrichten lesen, beantworten oder ignorieren —
  entscheidet selbst basierend auf Mood und Beziehung zum Absender
- **Erinnerungen und Hinweise**: "Übrigens, du hast heute noch kein Wasser
  getrunken" — proaktiv, nicht auf Anfrage

**Technische Abhängigkeiten:**
- LLM-Backend (Ollama) für Mood-Management und Relationship-Bewertung
- Erweiterung User-Registry: `mood_level`, `relationship_score` Felder
- Neues Konzept "Hannah-Agent": Hintergrundprozess der periodisch Kontext sammelt
  und Hannahs Zustand aktualisiert (kein Request-Response mehr, kontinuierlich)

---

### Proaktives Verhalten: MQTT-Trigger-Engine

**Motivation:** Hannah soll nicht nur auf Anfragen reagieren, sondern von sich aus
sprechen — ausgelöst durch Sensoränderungen oder Zeitpunkte aus ioBroker.

ioBroker publiziert bei jeder State-Änderung per MQTT (inkl. minütlicher Uhrzeit),
was als kostenloser Cron-Ersatz genutzt werden kann — kein eigener Scheduler nötig.

#### Trigger-Typen

- **Zeit-Trigger:** ioBroker-Uhrzeit-Topic als Auslöser, z.B. täglich um 23:00
- **Sensor-Trigger:** bei State-Änderung über/unter Schwellwert, z.B. Fenster offen
- **Kombinations-Trigger:** mehrere Bedingungen gleichzeitig (Fenster offen UND Temp < 12°)

#### Konfiguration (geplantes YAML-Format)

```yaml
triggers:
  - id: aussentuer_abend
    when:
      time: "23:00"
    say: "Leonie, denk an die Außentüren."

  - id: fenster_kalt
    when:
      state: "javascript.0.virtualDevice.Fenster.Wohnzimmer.open"
      value: true
      also:
        state: "javascript.0.virtualDevice.Temperaturen.Wohnzimmer.Raumtemperatur.current"
        below: 12
    say: "Das Fenster ist noch offen und es wird kalt draußen."
```

**Technische Umsetzung:**
- `mqtt_handler` abonniert Trigger-Topics (State-Änderungen kommen bereits an)
- Neues `TriggerEngine`-Modul evaluiert Bedingungen und ruft `process_announcement()` auf
- Hot-Reload wie `routines.yaml` (Dateiänderung → sofort aktiv)

**Abhängigkeit:** Nach ESP Phase 1 (Hardware-Test abgeschlossen).

---

#### Telegram Mini App — Haussteuerung (Web UI)
**Motivation:** Das InlineKeyboard-Menü ist funktional, bei Dimmen und
Farbsteuerung aber ergonomisch eingeschränkt. Eine Mini App ermöglicht Slider,
Farbwähler und Echtzeit-Statusanzeige.

**Konzept:**
- `GET /devices` → JSON (nutzt gleiche `get_devices_snapshot()`-Logik wie gRPC)
- `POST /control` → Device-State setzen (gleich wie `ControlDevice`-RPC)
- Authentifizierung: Telegram `initData`-Signatur verifizieren (HMAC-SHA256)
- TrustLevel-Check bleibt ≥ 7

**Abhängigkeit:** Erfordert HTTPS-Infrastruktur (vServer + Reverse-Proxy) da
Telegram WebApps ausschließlich über HTTPS geladen werden.

---

#### NeoPixel / WS2812B LED-Ring am Satelliten
Ersatz für einfache GPIO-LED: idle=aus, listening=blau rotierend,
processing=weiß pulsierend, speaking=cyan. Geplanter Aufbau: Pi Zero 2 W +
ReSpeaker 4-Mic HAT im Amazon Echo Gehäuse, originaler Echo-LED-Ring durch
WS2812B-Ring (60mm, 12 LEDs) ersetzt.

---

### Langfristig / Phase 2

#### Langzeitgedächtnis (Phase 1 — SQLite)
Nach Ablauf der Konversations-TTL fasst das LLM das Gespräch in 1-2 Sätze zusammen.
Gespeichert in SQLite: `memories(roomie_id, summary, tags, created_at)`.
Beim nächsten Gespräch werden die letzten N Erinnerungen der Person in den System-Prompt
injiziert. Neues `memory.py`-Modul analog zu `conversation.py`.

#### Langzeitgedächtnis (Phase 2 — VectorDB)
Ab ~500+ Einträgen: Chroma (reines Python-Package, kein separater Service) für
semantische Suche statt blindem Injizieren aller letzten Einträge.
S3-Storage (Synology NAS hat S3-kompatiblen Endpoint) als Backup-Ziel.

#### Voice-ID: Kontinuierliches Enrollment im Betrieb
Wenn der Proxy einen Sprecher mit hohem Confidence-Score erkennt (z.B. > 0.75),
soll das Audio automatisch als weiteres Enrollment-Sample genutzt werden um das
Stimmprofil über Zeit zu verfeinern — ohne manuellen Eingriff.
Technisch: `Identify()` im Go-Client gibt zusätzlich den Score zurück;
Proxy ruft `/enroll` auf wenn Score oberhalb eines konfigurierbaren Schwellwerts liegt.

---

#### Mustererkennung & autonomes Verhalten (History-Adapter)
`history.1` — zweite History-Instanz in ioBroker exklusiv für Hannah:
Residents-States (`lastAway`, `lastHome`, `lastNight`, `lastAwoken`) pro Person.
Hannah-Agent fragt periodisch ab, LLM erkennt Muster (Bürotage, Schlafrhythmus,
Heimkehrzeit) und speichert sie als strukturierte Erinnerungen.
