#pragma once

#include <stdbool.h>
#include <stdint.h>
#include "driver/i2c_master.h"

typedef struct {
    float temperature;    /* °C */
    float pressure;       /* hPa */
    float humidity;       /* % rel. */
    float gas_resistance; /* Ω — BMP280+AHT20-Pfad; NAN = ungültig */
    float iaq;            /* IAQ 0–500 (BSEC2); NAN = noch nicht kalibriert */
    float iaq_static;     /* Static IAQ Langzeit-Baseline (BSEC2); NAN = ungültig */
    float co2_equiv;      /* CO2-Äquivalent ppm (BSEC2); NAN = ungültig */
    float voc_equiv;      /* Atemluft-VOC ppm (BSEC2); NAN = ungültig */
    uint8_t iaq_accuracy; /* 0=unsicher, 1=niedrig, 2=mittel, 3=genau (BSEC2) */
} hannah_sensor_data_t;

void hannah_sensors_init(void);

/* Letzte gelesene Werte — thread-safe. Gibt false zurück wenn noch keine Daten. */
bool hannah_sensors_get(hannah_sensor_data_t *out);

/* I2C-Bus-Handle für Komponenten, die sich denselben Bus teilen (z.B.
 * hannah_audio/ADAU7118 auf PCB Rev.5+). NULL, falls hannah_sensors_init()
 * noch nicht (erfolgreich) gelaufen ist — Aufrufreihenfolge in main.c beachten. */
i2c_master_bus_handle_t hannah_sensors_get_i2c_bus(void);
