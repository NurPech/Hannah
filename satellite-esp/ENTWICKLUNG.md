# Hannah Satellite ESP32-S3 — Entwicklungs-Guide

Für Einsteiger in ESP-IDF (Vorkenntnis: Arduino).

---

## ESP-IDF vs. Arduino — was sich ändert

| | Arduino | ESP-IDF |
|---|---|---|
| Einstiegspunkt | `setup()` + `loop()` | `app_main()` |
| Nebenläufigkeit | — | FreeRTOS Tasks |
| Build-System | versteckt | CMake + `idf.py` |
| Konfiguration | `#define` im Code | `menuconfig` → `sdkconfig` |
| Libraries | Arduino Library Manager | Components (lokal oder IDF Component Manager) |
| Flash/Monitor | Arduino IDE Button | `idf.py flash monitor` |
| Debug-Ausgabe | `Serial.println()` | `ESP_LOGI(TAG, "...")` |

Die Konzepte (GPIO, I2C, SPI, UART, Interrupts) sind dieselben — nur die API-Namen
sind andere.

---

## Installation (Windows, einmalig)

Empfohlener Weg: **VS Code + ESP-IDF Extension**. Kein WSL nötig, USB-Flashing
funktioniert direkt über den Windows-COM-Port.

1. [VS Code](https://code.visualstudio.com/) installieren
2. Extension **ESP-IDF** von Espressif installieren
   (Extension ID: `espressif.esp-idf-extension`)
3. `Ctrl+Shift+P` → **"ESP-IDF: Configure ESP-IDF Extension"**
   - Modus: **Express**
   - Version: **ESP-IDF v5.3** (oder aktuell stabile)
   - Installationspfad z.B. `C:\esp\esp-idf`
   - Der Wizard installiert IDF, Toolchain und Python-Umgebung automatisch
4. Nach Abschluss: VS Code Terminal neu öffnen
5. Prüfen: `idf.py --version` → sollte eine Versionsnummer ausgeben

> **Warum nicht WSL?** USB-Passthrough Windows → WSL erfordert `usbipd-win`
> und manuelle Konfiguration. Für Flash + Monitor ist der native COM-Port
> unkomplizierter.

---

## Projekt zum ersten Mal aufsetzen

```powershell
cd C:\Users\gessinger\git\hannah\satellite-esp

# Einmalig: Target auf ESP32-S3 setzen (erzeugt sdkconfig im Projektordner)
idf.py set-target esp32s3

# Konfiguration öffnen (Terminal-GUI, Pfeiltasten + Enter zur Navigation)
idf.py menuconfig
```

In menuconfig unter **Component config** die Hannah-Werte eintragen:

```
Component config →
  Hannah Network
    WiFi SSID          ← WLAN-Name eintragen
    WiFi Passwort      ← WLAN-Passwort eintragen
    Geräte-ID          ← z.B. "wohnzimmer-esp"
    MQTT Broker        ← IP des Hannah-Servers (z.B. 192.168.8.1)
    MQTT Benutzername  ← mqtt
    MQTT Passwort      ← eintragen
```

`Q` drücken → `Y` bestätigen → speichert in `sdkconfig` (nicht eingecheckt,
bleibt lokal).

---

## Täglicher Entwicklungs-Workflow

```powershell
# Bauen
idf.py build

# Flashen + Monitor öffnen (DevKit per USB-C anschließen)
idf.py flash monitor

# Monitor beenden: Ctrl + ]
```

- Erster Build dauert ~60–90 Sekunden (kompiliert das gesamte IDF)
- Inkrementelle Builds danach: ~5–10 Sekunden
- `flash monitor` kombiniert Flashen + seriellen Monitor in einem Schritt

### Debug-Ausgaben lesen

```c
// Im Code:
static const char *TAG = "mein_modul";
ESP_LOGI(TAG, "Wert: %d", wert);   // Info
ESP_LOGW(TAG, "Warnung: %s", msg); // Warning
ESP_LOGE(TAG, "Fehler: %d", err);  // Error
```

Im Monitor erscheint das als:
```
I (1234) mein_modul: Wert: 42
```

---

## Projektstruktur

```
satellite-esp/
  CMakeLists.txt              Projekt-Root
  sdkconfig.defaults          Vorgabewerte (eingecheckt)
  sdkconfig                   Lokale Konfiguration (NICHT eingecheckt, in .gitignore)
  main/
    CMakeLists.txt
    main.c                    app_main() — Einstiegspunkt
  components/
    hannah_audio/             I2S Mic-Array, Speaker, Wake-Word
      Kconfig                 menuconfig-Einträge für Audio-GPIOs
      CMakeLists.txt
      hannah_audio.h/.c
    hannah_net/               WiFi, MQTT-Discovery, UDP-Stream, Mute
      Kconfig                 menuconfig-Einträge für WiFi/MQTT
      CMakeLists.txt
      hannah_net.h/.c
    hannah_led/               WS2812B-Ring, Zustands-Anzeige
      Kconfig                 menuconfig-Einträge für LED-GPIO
      CMakeLists.txt
      hannah_led.h/.c
  hardware/
    BOM.md                    Einkaufsliste Phase 1 + Phase 2
```

---

## FreeRTOS — das wichtigste Konzept

ESP-IDF läuft auf FreeRTOS. Statt `loop()` gibt es Tasks:

```c
// Task erstellen (entspricht einem dauerhaften Thread)
xTaskCreate(
    mein_task,      // Funktion
    "mein_task",    // Name (für Debug)
    4096,           // Stack-Größe in Bytes
    NULL,           // Parameter
    5,              // Priorität (1=niedrig, 24=hoch)
    NULL            // Task-Handle (optional)
);

// Task-Funktion — darf nie zurückkehren
void mein_task(void *arg) {
    while (1) {
        // Arbeit erledigen
        vTaskDelay(pdMS_TO_TICKS(100)); // 100ms warten
    }
}
```

Tasks blockieren sich gegenseitig nicht — die Audio-Task liest I2S, die Net-Task
sendet UDP, die LED-Task animiert den Ring, alles gleichzeitig.

---

## Implementierungs-Reihenfolge (Phase 1)

Die Komponenten sind bewusst so strukturiert dass sie unabhängig voneinander
entwickelt und getestet werden können.

### Schritt 1 — hannah_net: WiFi verbinden

Ziel: DevKit verbindet sich mit dem WLAN und loggt die IP.

```c
// hannah_net.c — WiFi-Init (vereinfacht)
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"

void hannah_net_init(void) {
    esp_netif_init();
    esp_event_loop_create_default();
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    esp_wifi_init(&cfg);

    wifi_config_t wifi_cfg = {
        .sta = {
            .ssid     = CONFIG_HANNAH_WIFI_SSID,
            .password = CONFIG_HANNAH_WIFI_PASS,
        },
    };
    esp_wifi_set_mode(WIFI_MODE_STA);
    esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg);
    esp_wifi_start();
    esp_wifi_connect();
    // Auf IP warten: Event-Handler oder EventGroup
}
```

Wenn `I (xxxx) hannah_net: IP: 192.168.8.xxx` im Monitor erscheint — fertig.

### Schritt 2 — hannah_net: MQTT + Discovery

Ziel: Subscribe auf `hannah/server` (retained) → Proxy-IP und Port extrahieren.

ESP-IDF hat einen eingebauten MQTT-Client (`esp_mqtt`). Topic `hannah/server`
enthält dieselbe Payload wie beim Pi-Satelliten — kein Protokollwechsel nötig.

### Schritt 3 — hannah_net: UDP-Socket

Ziel: `hannah_net_send_audio()` sendet echte Bytes zum Go-Proxy.

```c
// UDP-Socket aufbauen
int sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
struct sockaddr_in proxy_addr = {
    .sin_family = AF_INET,
    .sin_port   = htons(proxy_port),
};
inet_aton(proxy_ip, &proxy_addr.sin_addr);

// Senden (in hannah_net_send_audio):
sendto(sock, pcm, len, 0, (struct sockaddr *)&proxy_addr, sizeof(proxy_addr));
```

### Schritt 4 — hannah_audio: I2S lesen + senden

Ziel: Mikrofon-Daten über UDP zum Proxy — erster End-to-End-Test.

```c
// I2S konfigurieren (Phase 1, ein Mic, kein Beamforming)
i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(
    CONFIG_HANNAH_MIC_I2S_PORT, I2S_ROLE_MASTER);
i2s_new_channel(&chan_cfg, NULL, &rx_handle);

i2s_std_config_t std_cfg = {
    .clk_cfg  = I2S_STD_CLK_DEFAULT_CONFIG(CONFIG_HANNAH_AUDIO_SAMPLE_RATE),
    .slot_cfg = I2S_STD_MSB_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                 I2S_SLOT_MODE_STEREO),
    .gpio_cfg = {
        .bclk = CONFIG_HANNAH_MIC_BCK_GPIO,
        .ws   = CONFIG_HANNAH_MIC_WS_GPIO,
        .dout = I2S_GPIO_UNUSED,
        .din  = CONFIG_HANNAH_MIC_DATA_GPIO,
    },
};
i2s_channel_init_std_mode(rx_handle, &std_cfg);
i2s_channel_enable(rx_handle);

// In der Audio-Task:
uint8_t buf[960]; // 30ms @ 16kHz, 16-bit, mono
size_t bytes_read;
i2s_channel_read(rx_handle, buf, sizeof(buf), &bytes_read, portMAX_DELAY);
hannah_net_send_audio(buf, bytes_read);
```

### Schritt 5 — hannah_led: Zustände anzeigen

Kann parallel zu Schritt 1–4 entwickelt werden. LED-Ring gibt sofort visuelles
Feedback ob WiFi, MQTT oder Audio-Stream aktiv ist.

---

## Phase 2 — ESP-SR Beamforming (nach Phase 1)

ESP-SR ist Espressifs Audio-Front-End-Bibliothek (AEC, Beamforming, VAD,
Wake-Word). Sie ersetzt in Phase 2 den einfachen I2S-Read aus Schritt 4.

Dokumentation: `esp-sr` Komponente im IDF Component Manager, oder direkt im
ESP-Skainet GitHub Repository von Espressif.

Für das Wake-Word "Hey Hannah": microWakeWord-Modell mit OpenWakeWord-Pipeline
auf eigener Hardware trainieren, als `.tflite` in das Projekt einbinden.
Kein Espressif-Account oder Online-Dienst nötig.

---

## Häufige Fehler

| Fehler | Ursache | Lösung |
|---|---|---|
| `idf.py: command not found` | IDF-Umgebung nicht aktiv | VS Code Terminal neu öffnen oder `export.bat` ausführen |
| `CONFIG_HANNAH_* undeclared` | Kconfig-Datei fehlt oder Build-Cache alt | `idf.py fullclean && idf.py build` |
| Flash schlägt fehl | Falscher COM-Port oder Boot-Mode | Boot-Taster am DevKit halten während Flash startet |
| Kein WLAN | Falsches SSID/Passwort in menuconfig | `idf.py menuconfig` → Werte prüfen |
| Monitor zeigt Garbage | Falsche Baud-Rate | ESP32-S3 default: 115200 — sollte automatisch passen |

---

## Nützliche Links

- ESP-IDF Programmierhandbuch: https://docs.espressif.com/projects/esp-idf/
- ESP32-S3 Technisches Referenzhandbuch: https://www.espressif.com/sites/default/files/documentation/esp32-s3_technical_reference_manual_en.pdf
- ESP32-S3-DevKitC-1 Pinout: https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32s3/esp32-s3-devkitc-1/
- microWakeWord (Wake-Word Training): https://github.com/kahrendt/microWakeWord
- ESP-SR (Beamforming, AEC, VAD): https://github.com/espressif/esp-sr
