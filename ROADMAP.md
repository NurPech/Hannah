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

#### Hannah-Agent: Nativer ioBroker-Adapter als MQTT-Ersatz

Ersetzt den fragilen externen MQTT-Kanal zwischen ioBroker und Hannah vollständig
durch gRPC. Internes MQTT (Hannah ↔ Satelliten) bleibt unverändert.

**Problem mit aktuellem MQTT-Ansatz:**
- `ack=true`-States (von Geräten bestätigt, z.B. VW Connect) werden vom MQTT-Adapter
  standardmäßig nicht publiziert
- `ack=true` aktivieren → Endlosschleife (Adapter empfängt eigene Nachrichten)
- Workaround "nur bei Änderung + ack=true" ist stabil aber fragil

**Scope — was ersetzt wird:**

| Richtung | Inhalt | Ersatz |
|----------|--------|--------|
| ioBroker → Hannah | Smart-Home-Geräte (enum-basiert) | gRPC `StateUpdate`-Stream |
| ioBroker → Hannah | Residents-Präsenz | gRPC `SetResidentPresence` |
| ioBroker → Hannah | Text-Command-State | gRPC `SubmitText` |
| ioBroker → Hannah | Extra-Prefixes (0_userdata.0 etc.) | gRPC `StateUpdate`-Stream |
| Hannah → ioBroker | Geräte schalten (Routinen, NLU) | gRPC `SetState` auf Adapter-Server |
| Hannah → ioBroker | Extra States abonnieren (Trigger-Engine) | gRPC `WatchStates` auf Adapter-Server |
| ioBroker → Hannah | Notifications | notificationmanager Adapter ✓ bereits erledigt |

**Bleibt MQTT** (intern, stabil, unter Hannahs Kontrolle):
- Hannah ↔ Satelliten: Discovery, Status, DND/Volume/Mute

**Architektur — beide sind gRPC-Server, Hannah ist Master:**
```
┌─────────────────────────────────────────────┐
│  HannahAgent-Adapter (gRPC Server)          │
│  ┌──────────────────────────────────────┐   │
│  │ WatchStates([state_ids])             │◄──┼── Hannah connected rein
│  │ SetState(state_id, value)            │   │   (wenn Hannah bereit)
│  └──────────────────────────────────────┘   │
└─────────────────────────────────────────────┘
         │ Adapter connected zu Hannah
         ▼
┌─────────────────────────────────────────────┐
│  Hannah Core (gRPC Server, Port 50051)      │
│  ┌──────────────────────────────────────┐   │
│  │ StateUpdate(state_id, value) stream  │   │
│  │ SetResidentPresence(roomie_id, state) │   │
│  │ SubmitText(text)                     │   │
│  └──────────────────────────────────────┘   │
└─────────────────────────────────────────────┘
```

**State-Discovery — Hybrid-Modell:**

ioBroker ist Source of Truth. Der Adapter weiß selbst welche States relevant sind:

1. **Enum-basiert** (Hauptmenge): Adapter liest `enum.functions` + `enum.rooms`,
   bildet Union aller referenzierten State-IDs → alle Smart-Home-Geräte unabhängig
   von ihrem Pfad (`javascript.0.virtualDevice`, `0_userdata.0`, etc.)

2. **Typed Special Cases** (explizite Config-Sektionen):
   - `residents.state_prefix` → mapped auf `SetResidentPresence`-RPC
   - `text_command.state_id` → mapped auf `SubmitText`-RPC
   (Residents haben weder Raum noch Function — passen nicht in Enums)

3. **Extra-Prefixes** (Catch-all für alles andere):
   - Konfigurierbare Liste von Prefixes (z.B. `0_userdata.0`, `javascript.0.cars`)
   - Alle States darunter → generischer `StateUpdate`-Stream

4. **On-Demand via `WatchStates`** (Trigger-Engine):
   - Hannah lädt `triggers.yaml`, sammelt alle referenzierten State-IDs
     (z.B. `unless.state: "0_userdata.0.feeded"`)
   - Ruft `WatchStates([ids])` auf dem Adapter-gRPC-Server auf
   - Adapter subscribed zusätzlich, streamt in denselben `StateUpdate`-Kanal

**Adapter-Konfiguration:**
```yaml
hannah_grpc: "192.168.1.x:50051"

enums:
  functions: enum.functions
  rooms: enum.rooms

residents:
  state_prefix: "residents.0.roomie"

text_command:
  state_id: "0_userdata.0.hannah.set.textCommand"

extra_state_prefixes:
  - "0_userdata.0"
```

**Startup-Sequenz:**
1. Adapter startet → eigener gRPC-Server ist oben
2. Hannah startet → connected zu Adapter: `WatchStates([trigger-referenzierte IDs])`
3. Adapter connected zu Hannah: `StateUpdate`-Stream beginnt (Enum + Prefixes + Extra)
4. Beide Streams laufen dauerhaft; bei Hannah-Neustart werden sie neu aufgebaut

**Neue gRPC-Endpunkte:**

*In Hannah Core (Adapter ruft Hannah an):*
- `StateUpdate(state_id, value, ack)` — generischer State-Stream (ack-Flag für Verifikation!)
- `SetResidentPresence(roomie_id, presence_state)` — Residents

*Im HannahAgent-Adapter (Hannah ruft Adapter an):*
- `SetState(state_id, value)` — Gerät in ioBroker schalten
- `WatchStates(state_ids)` — On-Demand-Subscription für Trigger-States

**Steuer-Verifikation (ersetzt `hannah/set/#` + `javascript.0/#`-Loop):**

Aktuell publiziert Hannah auf `hannah/set/{device}` und überwacht die Antwort auf
`javascript.0/#` (ack=true) um zu prüfen ob der Schaltvorgang erfolgreich war.

Mit gRPC wird dieser Loop sauber abgebildet — und das `ack`-Problem entfällt:

```
Hannah:  SetState("javascript.0.virtualDevice.EG.Wohnzimmer.Licht", true)
Adapter: setForeignState() → ioBroker → Gerät schaltet
Gerät:   bestätigt → ioBroker State: value=true, ack=true
Adapter: streamt StateUpdate(state_id, value=true, ack=true) zurück
Hannah:  empfängt ack=true → Verifikation erfolgreich, antwortet Nutzer
         (kein Update innerhalb Timeout → Fehler melden)
```

Das `ack`-Flag im `StateUpdate`-Proto-Message ist damit kritisch — ohne es kann Hannah
nicht zwischen "Befehl gesendet" (ack=false) und "Gerät bestätigt" (ack=true)
unterscheiden. Der native Adapter subscribed auf alle State-Änderungen unabhängig vom
ack-Flag (anders als der MQTT-Adapter) — das ist einer der Hauptvorteile.

**Vorteile:**
- Kein externer MQTT-Kanal mehr, kein ack-Problem, kein Loop
- Verifikations-Loop funktioniert zuverlässig (ack=true nativ verfügbar)
- ioBroker ist und bleibt Source of Truth (Enum-Discovery, kein Drift zur config.yaml)
- Trigger-Engine kann beliebige State-IDs referenzieren ohne Adapter-Neustart
- Klare Trennung: Enums für Smart Home, explizite Config für Sonderfälle

---

#### Zeitgefühl: Dynamische Trigger aus dem Gespräch

Hannah kennt die aktuelle Uhrzeit (via `{{TIME}}` im System-Prompt) aber hat kein
Konzept von Dauer oder geplanter Rückkehr. Wenn Leonie sagt "wir gehen spazieren,
etwa eine Stunde", soll Hannah das verstehen und entsprechend reagieren.

**Konzept:**
Das LLM erkennt aus dem Gesprächskontext dass ein Ereignis mit erwarteter Dauer
stattfindet und erzeugt intern einen Einmal-Trigger für den Rückzeitpunkt.

**Technische Umsetzung:**
- LLM gibt strukturierte Metadaten zurück wenn es eine zeitliche Absicht erkennt:
  `{ "event": "spaziergang", "duration_minutes": 60 }`
- Hannah Core registriert einen dynamischen Einmal-Trigger (kein YAML, zur Laufzeit)
- Bei Rückkehr (Residents-State wechselt zu "home") oder nach Ablauf der Zeit:
  Hannah begrüßt proaktiv oder fragt nach

**Integration mit Residents:**
- Residents `wayhome`-State signalisiert Heimweg — Hannah kann früher reagieren
- Kombination: Trigger feuert wenn `wayhome=true` ODER Zeit abgelaufen

**Abhängigkeiten:**
- Trigger-Engine (bereits implementiert, statische Trigger)
- Erweiterung um dynamische Laufzeit-Trigger (neue API)
- LLM-Erkennung von Zeitintentionen im Gesprächskontext

---

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

### ~~Proaktives Verhalten: MQTT-Trigger-Engine~~ ✅ Erledigt

Zeit-Trigger (`days`-Filter), Sensor-Trigger (`value`/`above`/`below`), Kombinations-Trigger
(`also:`), `unless`-Bedingung, Cooldown, Hot-Reload und `extra_state_prefixes` für beliebige
ioBroker-Topics — alles implementiert und produktiv.

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

#### TTS Streaming-Playback (Pi + ESP32)

Aktuell puffert der Satellit alle TTS-Chunks und spielt erst nach `tts_end` ab.
Bei langen Antworten (>500ms Audio) überläuft der OS-Socket-Buffer — Chunks
gehen verloren oder werden verzögert abgespielt.

**Ziel:** Hannah sendet TTS-Chunks während der Generierung, Satellit spielt
sofort ab — wie Spotify-Buffering statt Download-then-Play.

**Aufwand:**
- Pi-Satellit: 3–5 Tage (Hauptproblem: stateful Streaming-Resampler)
- Hannah Core: 2–3 Tage (TTS-Backend muss Chunks streamen)
- Go-Proxy: 0,5 Tage (minimale Änderungen)
- ESP32: 1–2 Wochen (I2S DMA Streaming + Memory-Management)
- Latenz-Tuning: 2–3 Tage

**Zwingend für ESP32** — der hat zu wenig RAM um eine vollständige TTS-Antwort
zu puffern. Für den Pi-Satelliten ein Quality-of-Life-Fix.

---

#### NeoPixel / WS2812B LED-Ring am Satelliten
Ersatz für einfache GPIO-LED: idle=aus, listening=blau rotierend,
processing=weiß pulsierend, speaking=cyan. Geplanter Aufbau: Pi Zero 2 W +
ReSpeaker 4-Mic HAT im Amazon Echo Gehäuse, originaler Echo-LED-Ring durch
WS2812B-Ring (60mm, 12 LEDs) ersetzt.

---

### Langfristig / Phase 2

#### libhannah_audio — Gemeinsame C-Bibliothek für Audio-Operationen

Plattformübergreifende C-Bibliothek für Audio-Verarbeitung, einmal schreiben —
überall verwenden:

```c
// hannah_audio.h
int hannah_resample(const int16_t *in,  int in_samples,  int src_rate,
                          int16_t *out, int out_samples, int dst_rate);
int hannah_rms(const int16_t *pcm, int samples);
int hannah_vad(const int16_t *pcm, int samples, int threshold);
```

**Zielplattformen:**
- **ESP32:** direkt als IDF-Komponente (`satellite-lib/` → CMakeLists.txt)
- **Pi-Satellit:** Python-Binding via `ctypes` oder `cffi`
- **Go-Proxy:** optional via `cgo`

**Motivation:** Resampling, VAD und RMS werden aktuell in Python und C separat
implementiert und verhalten sich leicht unterschiedlich. Eine gemeinsame Basis
eliminiert Divergenz und macht Streaming-Playback wartbar.

**Voraussetzung:** TTS Streaming-Playback (oben) — der Resampler ist der
Haupttreiber für diese Bibliothek.

---

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

---

### Langfristig / Weit in der Zukunft

#### Offline-Audio am ESP32-Satelliten (SD-Karte oder Flash)

Wenn Core nicht erreichbar ist soll der Satellit trotzdem akustisches Feedback geben
statt still zu bleiben.

**Mögliche Offline-Töne:**
- "Hannah ist gerade nicht erreichbar"
- "Verbindung wird hergestellt..." (beim Boot)
- Fehlerton bei Registrierungs-Timeout
- Wake-Word erkannt, aber offline → kurzer Ton als Feedback

**Umsetzungsvarianten:**
- **Flash (Phase 1.x):** WAV-Dateien als eingebettete C-Arrays (`xxd -i audio.wav`),
  kein zusätzliches Hardware nötig, aber limitiert auf ~4MB Flash gesamt
- **SD-Karte (Phase 2+):** Micro-SD-Slot per SPI (`esp_vfs_fat_sdmmc`), ~0,50€ Bauteil,
  beliebig viele Audiodateien, austauschbar ohne Firmware-Update

---

#### OTA-Firmware-Updates für ESP32-Satelliten (HannahDeviceManager)

ESP32 IDF hat OTA eingebaut (`esp_ota_ops.h`). Hannah Core hostet die aktuelle
Firmware, ESP meldet seine Version per Heartbeat, Core triggert Update per UDP.

**Ablauf:**
1. Hannah Core (oder separater Service) hostet aktuelle `.bin` via HTTP
2. ESP meldet beim Heartbeat seine Firmware-Version mit
3. Core vergleicht — bei neuer Version: `{"type": "ota_update", "url": "...", "version": "x.y.z"}`
4. ESP lädt Firmware in zweite OTA-Partition, restartet, bestätigt neue Version

**Voraussetzungen:**
- Stabile ESP-Firmware (sinnlos OTA zu verteilen wenn Firmware noch in aktiver Entwicklung)
- Partition Table mit zwei OTA-Partitionen (`factory` + `ota_0`/`ota_1`)
- Build-Pipeline: `idf.py build` → `.bin` automatisch versioniert
- Hannah Core: HTTP-Server + Device-Registry mit Firmware-Versionen pro Gerät

**Sinnvoll ab:** Mehrere ESP32-Satelliten im Betrieb, Firmware-Änderungen werden
sonst zum manuellen Aufwand.

---

#### Mustererkennung & autonomes Verhalten (History-Adapter)
`history.1` — zweite History-Instanz in ioBroker exklusiv für Hannah:
Residents-States (`lastAway`, `lastHome`, `lastNight`, `lastAwoken`) pro Person.
Hannah-Agent fragt periodisch ab, LLM erkennt Muster (Bürotage, Schlafrhythmus,
Heimkehrzeit) und speichert sie als strukturierte Erinnerungen.
