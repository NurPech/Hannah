#pragma once

#ifdef __cplusplus
extern "C" {
#endif

/**
 * hannah_sendspin — Sendspin-Player (Musik von Music Assistant), Refs #440
 *
 * Meldet den Satelliten per mDNS (_sendspin._tcp, Port 8928) bei Music Assistant
 * an und spielt dessen Stream (PCM, mono, 16 Bit, 48 kHz) über
 * hannah_audio_music_write() ab. Verbindung ohne Pairing (Gastzugriff),
 * server-initiiert. Braucht MA ab 2.11.0.
 *
 * Läuft in einem eigenen Task, wartet selbst auf das Netzwerk. Ohne
 * CONFIG_HANNAH_SENDSPIN_ENABLED ist die Funktion leer.
 */
void hannah_sendspin_start(void);

#ifdef __cplusplus
}
#endif
