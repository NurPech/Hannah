# Hannah Satellite ESP32-S3 — Bill of Materials

Zwei Phasen: **Phase 1** ist der Prototyp auf DevKit-Basis (kein SMD-Löten, sofort
einsatzbereit). **Phase 2** ist die eigene Platine für den produktiven Einsatz.

---

## Phase 1 — Prototyp (DevKit + Breakout-Boards)

Alle Teile steckbar auf Breadboard oder per Dupont-Kabel verbunden.
Kein Löten nötig. Ziel: Firmware entwickeln und testen.

| # | Bezeichnung | Teilenummer / Suchbegriff | Menge | ~Preis | Bezugsquelle |
|---|---|---|---|---|---|
| 1 | ESP32-S3 DevKit | **ESP32-S3-DevKitC-1-N16R8** (unbedingt N16R8, nicht N8R2) | 1 | ~10 € | LCSC, Mouser, AliExpress |
| 2 | I2S MEMS-Mikrofon | **INMP441 I2S Microphone Breakout** | 2 | ~2 €/Stk | AliExpress, Amazon |
| 3 | I2S Verstärker | **MAX98357A I2S Amplifier Breakout** | 1 | ~2 € | AliExpress, Adafruit |
| 4 | Lautsprecher | **4 Ohm 3W Speaker 40mm** (Durchmesser 40 mm) | 1 | ~2 € | AliExpress, Amazon |
| 5 | LED-Ring | **WS2812B 12-Bit LED Ring** (12 LEDs, ~44 mm Außen-Ø) | 1 | ~2 € | AliExpress, Amazon |
| 6 | Temp/Feuchte-Sensor | **AHT20 I2C Breakout** | 1 | ~2 € | AliExpress, Amazon |
| 7 | Breadboard | **830-Point Breadboard** | 1 | ~2 € | AliExpress, Amazon |
| 8 | Kabel | **Jumper Wire Set** (M-M + M-F + F-F, je 40 Stk) | 1 | ~2 € | AliExpress, Amazon |
| 9 | USB-C Kabel | USB-C Datenkabel (kein reines Ladekabel — für Flashing nötig) | 1 | ~3 € | — |
| **Σ** | | | | **~27 €** | |

> **Hinweis INMP441:** Zwei Stück kaufen — sie werden als Stereo-Array betrieben
> (L/R-Kanal, ca. 60 mm Abstand auf dem Breadboard), um Beamforming via ESP-SR
> zu ermöglichen. Einzeln reicht für Phase 1 auch, schränkt aber die Reichweite ein.

> **Hinweis USB-C:** Viele günstige Kabel sind reine Ladekabel ohne Datenleitungen.
> Immer auf "data capable" oder "USB 2.0 data" achten.

---

## Optional — Erweiterungen (Phase 1)

Nicht zwingend für die Kern-Funktion, aber empfohlen wenn die Sensoren getestet
werden sollen.

| # | Bezeichnung | Suchbegriff | Menge | ~Preis | Anmerkung |
|---|---|---|---|---|---|
| 10 | mmWave Präsenzsensor | **LD2410 Breakout** oder **LD2410C** | 1 | ~5 € | Human-Presence-Detection; UART an ESP32-S3 |
| 11 | PoE-Splitter | **PoE Splitter USB-C 5V 2A** (IEEE 802.3af) | 1 | ~6–8 € | Stromversorgung über Ethernet; Satellite bleibt WiFi |
| 12 | Taster | **6×6 mm Tactile Push Button** (Through-Hole) | 2 | < 1 € | Mute + Reserve; im DevKit-Kit oft enthalten |

---

## Phase 2 — Eigene Platine (SMD, JLCPCB-Bestückung)

Komponenten für eine eigene Platine. Bestückung der SMD-Teile durch JLCPCB
(LCSC-Teilenummern für JLCPCB Basic/Extended Parts).
Through-Hole-Teile (Stecker, Buchsen) können selbst eingelötet werden.

### Aktive Komponenten

| # | Bezeichnung | LCSC-Part | Menge/Satellit | ~Preis (qty 5) |
|---|---|---|---|---|
| 1 | ESP32-S3-WROOM-1-N16R8 Modul | C2913202 | 1 | ~3,50 € |
| 2 | INMP441ACEZ-T (I2S MEMS Mic) | C964647 | 2 | ~0,80 €/Stk |
| 3 | MAX98357AEWL+T (I2S Class-D Amp) | C91474 | 1 | ~0,80 € |
| 4 | AHT20 (Temp/Feuchte, I2C) | C654673 | 1 | ~0,50 € |
| 5 | WS2812B-2020 (SMD LED, 2×2 mm) | C965555 | 12 | ~0,08 €/Stk |

### Passive Komponenten & Stecker

| # | Bezeichnung | Wert | Menge/Satellit | Anmerkung |
|---|---|---|---|---|
| 6 | Abblockkondensatoren 0402 | 100 nF | ~10 | Für jeden IC |
| 7 | Abblockkondensatoren 0402 | 10 µF | ~4 | Bulk decoupling |
| 8 | Pull-up Widerstände 0402 | 4,7 kΩ | 2 | I2C SDA/SCL |
| 9 | Vorwiderstände WS2812B 0402 | 33 Ω | 1 | Datensignal |
| 10 | USB-C Buchse (SMD, 16-pin) | GCT USB4135-GF-A | 1 | Power + Flash |
| 11 | Lautsprecher-Buchse | JST-PH 2-pin, 2 mm | 1 | Für 4Ω/3W Speaker |
| 12 | LD2410-Header | Pin-Header 2,54 mm, 4-pin | 1 | Optional, kann unbest. bleiben |
| 13 | Debug-Header | Pin-Header 2,54 mm, 3-pin | 1 | TX/RX/GND für UART-Debug |

### Geschätzte Gesamtkosten Phase 2

| Position | qty 5 gesamt |
|---|---|
| Komponenten (aktiv + passiv) | ~45 € |
| PCB 5 Stück (JLCPCB, 2-lagig) | ~5 € |
| SMD-Bestückung JLCPCB | ~15–20 € |
| Lautsprecher (4Ω 3W, 40 mm, 5×) | ~10 € |
| **Gesamt** | **~75–80 €** |

> JLCPCB-Preise variieren je nach Komponentenverfügbarkeit. Basic Parts sind
> günstiger als Extended Parts. INMP441 und MAX98357A sind im JLCPCB-Lager
> verfügbar (Stand 2025), vor Platinen-Order Verfügbarkeit prüfen.

---

## GPIO-Pinout (ESP32-S3-DevKitC-1, Phase 1)

Vorgeschlagene Verdrahtung für den Prototyp. Pinout in `sdkconfig.defaults`
hinterlegt und dort änderbar.

| Signal | GPIO | Kabel-Farbe (Empfehlung) | Anmerkung |
|---|---|---|---|
| I2S0 BCK (Mic) | 4 | Gelb | Clock für beide INMP441 |
| I2S0 WS (Mic) | 5 | Orange | L/R-Select: Mic1=GND, Mic2=3V3 |
| I2S0 DATA (Mic) | 6 | Grün | Beide INMP441 am selben Datenpin |
| I2S1 BCK (Speaker) | 15 | Gelb | |
| I2S1 WS (Speaker) | 16 | Orange | |
| I2S1 DATA (Speaker) | 7 | Grün | |
| WS2812B Data | 48 | Weiß | RGB LED |
| Mute-Taster | 0 | Blau | Gegen GND, interner Pull-up |
| I2C SDA (AHT20) | 8 | Lila | |
| I2C SCL (AHT20) | 9 | Grau | |
| LD2410 TX→ESP RX | 17 | — | Optional |
| LD2410 RX→ESP TX | 18 | — | Optional |

> **INMP441 L/R-Kanal:** Der LR-Pin bestimmt auf welchem I2S-Kanal das Mic
> sendet. Mic1: LR=GND (linker Kanal), Mic2: LR=3V3 (rechter Kanal).
> Beide teilen BCK, WS und DATA.

---

## Weiterführendes

- **Wake-Word Training:** microWakeWord-Modelle mit eigenem Hardware-Training
  über OpenWakeWord-Pipeline; kein Espressif-Account nötig.
- **ESP-SR AFE:** Beamforming + AEC erst in Phase 2 der Firmware aktiviert
  (erfordert beide Mics korrekt verdrahtet).
- **Display (langfristig):** SPI-Header auf der Platine vorsehen (Footprint
  ohne Bestückung); kompatibel mit ST7789 und GC9A01 (Rund-Display).
