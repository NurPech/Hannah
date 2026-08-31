# Hannah Satellite — ESP32-S3 Firmware

ESP-IDF-Firmware für die Hannah-Satelliten (Wake-Word/PTT-Aufnahme, LED-Ring,
Sensorik). Für Einstieg/Entwicklung siehe [ENTWICKLUNG.md](ENTWICKLUNG.md).

## Hardware-Status

Aktuell verbaut: **PCB Rev. 5** (löst Rev. 4 ab). Details zu allen Revisionen
(Bauteile, Entscheidungen, GPIO-Belegung) siehe `## Hardware` in der
Root-[CLAUDE.md](../CLAUDE.md).

### PCB Rev. 5

Voll in Betrieb. Bestellung/Fertigungsdaten als öffentliches
PCBWay-Shared-Project:

https://www.pcbway.com/project/shareproject/Hannah_Satellite_Revsion_5_676db7ad.html

GPIO-Belegung: `sdkconfig.defaults.rev5`. OTA-Channel: Server-Default
`satellite-esp-stable` (bewusst nicht überschrieben). Rev. 4 läuft weiterhin
auf ihrem eigenen Channel `satellite-esp-stable-rev4`.
