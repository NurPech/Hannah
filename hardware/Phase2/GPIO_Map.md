# Hannah Satellite — GPIO Map

**Chip:** ESP32-S3-WROOM-1U-N16R8 (externe Antenne)
**Board:** Rev.5 PCB, 88 mm rund (bestellt bei PCBWay, Fertigung ausstehend) — GPIO-Belegung gemäß `satellite-esp/sdkconfig.defaults.rev5`, von Leonie bestätigt (Issue #160)

Für die aktuell verbaute Rev.4-Hardware (ESP32-S3-WROOM-1-N16R8, PDM-Mics statt TDM) siehe `satellite-esp/sdkconfig.defaults.rev4` bzw. die Git-Historie dieser Datei.

---

## Vollständige Pin-Tabelle

| GPIO | Net / Funktion    | Richtung | Beschreibung |
|-----:|-------------------|----------|--------------|
|  0   | Boot-Strapping    | —        | Tied to 3,3 V → immer Normal-Boot |
|  1   | STATUS_LED        | Out      | Einzel-Status-LED — auf Rev.5 hierher verschoben (Rev.4: GPIO18, dort jetzt Vol−) |
|  2   | NC                | —        | — |
|  3   | LED_DATA          | Out      | SK6812MINI-RV Ring (24 LEDs), 5V-Trace platinenweit auf 0,8mm verbreitert |
|  4   | SD_CS             | Out      | Micro-SD SPI — gleiche GPIOs wie Rev.4, aber neuer Steckertyp (Lötpads jetzt auf der Außenseite statt nach innen zeigend, vereinfacht Routing) |
|  5   | SD_MOSI           | Out      | Micro-SD SPI |
|  6   | SD_CLK            | Out      | Micro-SD SPI |
|  7   | SD_MISO           | In       | Micro-SD SPI |
|  8   | I2C_SDA           | I/O      | BME680 (Temperatur, Feuchte, Luftdruck) — Bus jetzt geteilt mit ADAU7118-Konfiguration |
|  9   | I2C_SCL           | Out      | BME680 / ADAU7118 |
| 10   | MIC_MUTE          | Out      | NPN-Transistor: HIGH = Mics aktiv, LOW = Hardware-Stumm |
| 11   | IO_MUTE           | In       | Mute-Taste, active-low, interner Pull-up — unverändert ggü. Rev.4 |
| 12   | MIC_WS            | Out      | ADAU7118 TDM Word-Select — Rev.4: hier lag Vol+ (jetzt GPIO39) |
| 13   | MIC_BCK           | Out      | ADAU7118 TDM Bit-Clock — Rev.4: hier lag Vol− (jetzt GPIO18) |
| 14   | MIC_DATA          | In       | ADAU7118 TDM Data (4 Slots, ein Mic pro Slot) — Rev.4: hier lag PTT (jetzt GPIO40) |
| 15   | LD2410_OUT        | In       | Digitales Präsenzsignal des LD2410 |
| 16   | LD2410_RX (UART2) | In       | UART2 RX — ESP empfängt von LD2410 TX |
| 17   | LD2410_TX (UART2) | Out      | UART2 TX — ESP sendet an LD2410 RX |
| 18   | IO_VOL-           | In       | Leiser-Taste, active-low, interner Pull-up — auf Rev.5 hierher verschoben (Rev.4: Status-LED) |
| 19   | USB_D−            | —        | Nicht bestückt auf Rev.5 — USB-C-Connector entfällt (durch 5V/GND-Lötpads ersetzt), Pin liegt brach |
| 20   | USB_D+            | —        | Nicht bestückt auf Rev.5, s.o. |
| 21   | AMP_DATA          | Out      | MAX98357A I2S DIN (Audio-Daten) — unverändert |
| 35   | ⚠ PSRAM           | —        | Intern mit PSRAM verbunden — nie verwenden |
| 36   | ⚠ PSRAM           | —        | Intern mit PSRAM verbunden — nie verwenden |
| 37   | ⚠ PSRAM           | —        | Intern mit PSRAM verbunden — nie verwenden |
| 38   | AMP_LRC           | Out      | MAX98357A I2S LRC / Word Select — unverändert |
| 39   | IO_VOL+           | In       | Lauter-Taste, active-low, interner Pull-up — auf Rev.5 hierher verschoben (Rev.4: MIC_CLOCK) |
| 40   | IO_PTT            | In       | Push-to-Talk-Taste, active-low, interner Pull-up — auf Rev.5 hierher verschoben (Rev.4: MIC_DATA) |
| 41   | NC                | —        | — |
| 42   | NC                | —        | — |
| 43   | UART0_TXD         | Out      | Debug-Header (J4), ROM-Bootloader TX — Initial-Flashing, einziger Flash-Weg da kein USB-C mehr |
| 44   | UART0_RXD         | In       | Debug-Header (J4), ROM-Bootloader RX — Initial-Flashing |
| 45   | NC                | —        | Strapping: NC = SPI-Boot (normal) |
| 46   | NC                | —        | Strapping: NC = normale Log-Ausgabe |
| 47   | AMP_BCLK          | Out      | MAX98357A I2S Bit Clock — unverändert |
| 48   | NC                | —        | — |

Zusätzlich: eine zweite, rein passive Power-LED (R12) hängt fest an 3.3V/GND — kein GPIO involviert.

---

## Funktionsgruppen

### I2C — BME680 + ADAU7118-Konfiguration
| Signal | GPIO |
|--------|-----:|
| SDA    |  8   |
| SCL    |  9   |

### TDM-Mikrofone — 4× SPH0641LU4H-1 via ADAU7118 (PDM→TDM-Wandler)
| Signal       | GPIO | Hinweis |
|--------------|-----:|---------|
| WS           | 12   | Rev.4: Vol+ |
| BCK          | 13   | Rev.4: Vol− |
| DATA         | 14   | Rev.4: PTT |
| HW-Mute Out  | 10   | NPN-Transistor-Steuerung, unverändert |

Ersetzt die 2× PDM-Mics mit gemeinsamer Clock/Data-Leitung (GPIO39/40) von Rev.4 — ermöglicht Beamforming.

### I2S-Verstärker — MAX98357A (unverändert)
| Signal | GPIO |
|--------|-----:|
| BCLK   | 47   |
| LRC    | 38   |
| DATA   | 21   |

### Tasten (alle active-low, interner Pull-up) — auf Rev.5 umverdrahtet
| Taste  | GPIO | Rev.4-GPIO |
|--------|-----:|-----------:|
| Mute   | 11   | 11 (unverändert) |
| PTT    | 40   | 12 |
| Vol+   | 39   | 13 |
| Vol−   | 18   | 14 |

Grund: GPIO12–14 werden auf Rev.5 vom ADAU7118-TDM-Interface belegt.

### LD2410 mmWave-Radar (UART2, unverändert)
| Signal | GPIO |
|--------|-----:|
| OUT    | 15   |
| RX     | 16   |
| TX     | 17   |

### LEDs
| Signal           | GPIO | Hinweis |
|------------------|-----:|---------|
| SK6812MINI-RV-Ring | 3  | 24 LEDs, 5V-Trace platinenweit auf 0,8mm verbreitert |
| Status-LED       | 1    | Auf Rev.5 hierher verschoben (Rev.4: GPIO18) |
| Power-LED (R12)  | —    | Rein passiv, fest an 3.3V/GND, kein GPIO |

### SD-Karte (SPI, gleiche GPIOs wie Rev.4)
| Signal | GPIO |
|--------|-----:|
| CS     | 4    |
| MOSI   | 5    |
| CLK    | 6    |
| MISO   | 7    |

Neuer Steckertyp: Lötpads auf der Außenseite statt (wie auf Rev.4) auf der nach innen zeigenden Seite — Traces mussten auf Rev.4 unnötig unter der Karte durchlaufen, das entfällt jetzt.

### Debug / Flashing
| Signal    | GPIO | Hinweis |
|-----------|-----:|---------|
| UART0 TX  | 43   | J4 Debug-Header, 4-Pin 2,54 mm Right-Angle — einziger Flash-Weg auf Rev.5 |
| UART0 RX  | 44   | J4 Debug-Header |
| USB D−    | 19   | Nicht bestückt auf Rev.5 (kein USB-C-Connector mehr) |
| USB D+    | 20   | Nicht bestückt auf Rev.5 (kein USB-C-Connector mehr) |
