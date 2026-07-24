#include "hannah_sensors.h"
#include "hannah_net.h"
#include "hannah_config.h"

#include <math.h>
#include <string.h>
#include <stdio.h>
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2c_master.h"

static const char *TAG = "sensors";

#define I2C_TIMEOUT_MS  50

static i2c_master_bus_handle_t s_bus      = NULL;
static bool                    s_ready    = false;
static hannah_sensor_data_t    s_last     = {0};
static volatile bool           s_has_data = false;

/* ========================================================================
 * BME680 + BSEC2
 * ======================================================================== */
#if CONFIG_HANNAH_SENSOR_TYPE_BME680

#include "bme68x.h"
#include "bsec_interface.h"
#include "nvs.h"
#include "esp_timer.h"

#define BME680_ADDR          CONFIG_HANNAH_SENSOR_BME680_ADDR
#define BSEC_STATE_SAVE_MS   (30LL * 60 * 1000)
#define NVS_NS               "bsec_ns"
#define NVS_KEY              "state"

static struct bme68x_dev       s_bme = {0};
static i2c_master_dev_handle_t s_dev = NULL;

/* ── BME68x I2C-Callbacks ─────────────────────────────────────────────── */

static BME68X_INTF_RET_TYPE bme_read(uint8_t reg, uint8_t *data, uint32_t len, void *p)
{
    return i2c_master_transmit_receive(*(i2c_master_dev_handle_t *)p,
                                       &reg, 1, data, (size_t)len,
                                       pdMS_TO_TICKS(I2C_TIMEOUT_MS)) == ESP_OK ? 0 : 1;
}

static BME68X_INTF_RET_TYPE bme_write(uint8_t reg, const uint8_t *data, uint32_t len, void *p)
{
    uint8_t buf[32];
    if (len + 1 > sizeof(buf)) return 1;
    buf[0] = reg;
    memcpy(&buf[1], data, (size_t)len);
    return i2c_master_transmit(*(i2c_master_dev_handle_t *)p,
                                buf, len + 1,
                                pdMS_TO_TICKS(I2C_TIMEOUT_MS)) == ESP_OK ? 0 : 1;
}

static void bme_delay(uint32_t us, void *p)
{
    (void)p;
    vTaskDelay(pdMS_TO_TICKS((us + 999) / 1000));
}

/* ── NVS-Persistenz für BSEC2-Kalibrierungszustand ───────────────────── */

static void bsec_load_state(void)
{
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READONLY, &h) != ESP_OK) return;
    uint8_t state[BSEC_MAX_STATE_BLOB_SIZE], work[BSEC_MAX_STATE_BLOB_SIZE];
    size_t len = sizeof(state);
    if (nvs_get_blob(h, NVS_KEY, state, &len) == ESP_OK &&
        bsec_set_state(state, (uint32_t)len, work, sizeof(work)) == BSEC_OK)
        ESP_LOGI(TAG, "BSEC2 Zustand geladen (%u B)", (unsigned)len);
    nvs_close(h);
}

static void bsec_save_state(void)
{
    uint8_t state[BSEC_MAX_STATE_BLOB_SIZE], work[BSEC_MAX_STATE_BLOB_SIZE];
    uint32_t len = 0;
    if (bsec_get_state(0, state, sizeof(state), work, sizeof(work), &len) != BSEC_OK) return;
    nvs_handle_t h;
    if (nvs_open(NVS_NS, NVS_READWRITE, &h) != ESP_OK) return;
    nvs_set_blob(h, NVS_KEY, state, len);
    nvs_commit(h);
    nvs_close(h);
    ESP_LOGI(TAG, "BSEC2 Zustand gespeichert (%u B)", (unsigned)len);
}

/* ── Init ─────────────────────────────────────────────────────────────── */

static bool sensor_init(void)
{
    if (bsec_init() != BSEC_OK) { ESP_LOGE(TAG, "bsec_init fehlgeschlagen"); return false; }

    bsec_version_t v; bsec_get_version(&v);
    ESP_LOGI(TAG, "BSEC2 v%d.%d.%d.%d", v.major, v.minor, v.major_bugfix, v.minor_bugfix);

    extern const uint8_t _binary_bme680_iaq_33v_3s_4d_bin_start[];
    extern const uint8_t _binary_bme680_iaq_33v_3s_4d_bin_end[];
    static uint8_t work_buf[BSEC_MAX_WORKBUFFER_SIZE];
    uint32_t cfg_size = (uint32_t)(_binary_bme680_iaq_33v_3s_4d_bin_end - _binary_bme680_iaq_33v_3s_4d_bin_start);
    bsec_library_return_t cfg_ret = bsec_set_configuration(
        _binary_bme680_iaq_33v_3s_4d_bin_start, cfg_size, work_buf, sizeof(work_buf));
    if (cfg_ret != BSEC_OK)
        ESP_LOGW(TAG, "bsec_set_configuration: %d", (int)cfg_ret);

    bsec_load_state();

    bsec_sensor_configuration_t req[7], required[BSEC_MAX_PHYSICAL_SENSOR];
    uint8_t n_req = 0;
    uint8_t n_required = BSEC_MAX_PHYSICAL_SENSOR;
    req[n_req++] = (bsec_sensor_configuration_t){ .sensor_id = BSEC_OUTPUT_IAQ,                                 .sample_rate = BSEC_SAMPLE_RATE_LP };
    req[n_req++] = (bsec_sensor_configuration_t){ .sensor_id = BSEC_OUTPUT_STATIC_IAQ,                          .sample_rate = BSEC_SAMPLE_RATE_LP };
    req[n_req++] = (bsec_sensor_configuration_t){ .sensor_id = BSEC_OUTPUT_CO2_EQUIVALENT,                      .sample_rate = BSEC_SAMPLE_RATE_LP };
    req[n_req++] = (bsec_sensor_configuration_t){ .sensor_id = BSEC_OUTPUT_BREATH_VOC_EQUIVALENT,               .sample_rate = BSEC_SAMPLE_RATE_LP };
    req[n_req++] = (bsec_sensor_configuration_t){ .sensor_id = BSEC_OUTPUT_SENSOR_HEAT_COMPENSATED_TEMPERATURE, .sample_rate = BSEC_SAMPLE_RATE_LP };
    req[n_req++] = (bsec_sensor_configuration_t){ .sensor_id = BSEC_OUTPUT_SENSOR_HEAT_COMPENSATED_HUMIDITY,    .sample_rate = BSEC_SAMPLE_RATE_LP };
    req[n_req++] = (bsec_sensor_configuration_t){ .sensor_id = BSEC_OUTPUT_RAW_PRESSURE,                        .sample_rate = BSEC_SAMPLE_RATE_LP };
    bsec_library_return_t bsec_ret = bsec_update_subscription(req, n_req, required, &n_required);
    if (bsec_ret < BSEC_OK) {
        ESP_LOGE(TAG, "bsec_update_subscription fehlgeschlagen (%d)", (int)bsec_ret); return false;
    } else if (bsec_ret > BSEC_OK) {
        ESP_LOGW(TAG, "bsec_update_subscription warning (%d)", (int)bsec_ret);
    }

    s_bme.read     = bme_read;
    s_bme.write    = bme_write;
    s_bme.delay_us = bme_delay;
    s_bme.intf     = BME68X_I2C_INTF;
    s_bme.intf_ptr = &s_dev;
    s_bme.amb_temp = 25;
    if (bme68x_init(&s_bme) != BME68X_OK) {
        ESP_LOGE(TAG, "bme68x_init fehlgeschlagen"); return false;
    }
    ESP_LOGI(TAG, "BME680 + BSEC2 bereit");
    return true;
}

/* ========================================================================
 * BMP280 + AHT20
 * ======================================================================== */
#else

#define BMP280_ADDR      CONFIG_HANNAH_SENSOR_BMP280_ADDR
#define BMP280_REG_ID    0xD0
#define BMP280_REG_CTRL  0xF4
#define BMP280_REG_CONF  0xF5
#define BMP280_REG_DATA  0xF7
#define BMP280_REG_CALIB 0x88
#define AHT20_ADDR       0x38
#define AHT20_CMD_INIT   0xBE
#define AHT20_CMD_TRIG   0xAC

typedef struct {
    uint16_t dig_T1; int16_t dig_T2, dig_T3;
    uint16_t dig_P1; int16_t dig_P2, dig_P3, dig_P4, dig_P5, dig_P6, dig_P7, dig_P8, dig_P9;
} bmp280_calib_t;

static i2c_master_dev_handle_t s_bmp280 = NULL;
static i2c_master_dev_handle_t s_aht20  = NULL;
static bmp280_calib_t          s_calib  = {0};

static esp_err_t bmp_read(uint8_t reg, uint8_t *buf, size_t len) {
    return i2c_master_transmit_receive(s_bmp280, &reg, 1, buf, len, pdMS_TO_TICKS(I2C_TIMEOUT_MS));
}
static esp_err_t bmp_write(uint8_t reg, uint8_t val) {
    uint8_t b[2] = {reg, val};
    return i2c_master_transmit(s_bmp280, b, 2, pdMS_TO_TICKS(I2C_TIMEOUT_MS));
}

static bool sensor_init(void) {
    uint8_t id = 0;
    if (bmp_read(BMP280_REG_ID, &id, 1) != ESP_OK || (id != 0x58 && id != 0x60)) {
        ESP_LOGE(TAG, "BMP280 nicht gefunden (ID=0x%02x)", id); return false;
    }
    uint8_t cb[24]; bmp_read(BMP280_REG_CALIB, cb, 24);
    s_calib.dig_T1 = (uint16_t)(cb[1]<<8|cb[0]); s_calib.dig_T2 = (int16_t)(cb[3]<<8|cb[2]);
    s_calib.dig_T3 = (int16_t)(cb[5]<<8|cb[4]);  s_calib.dig_P1 = (uint16_t)(cb[7]<<8|cb[6]);
    s_calib.dig_P2 = (int16_t)(cb[9]<<8|cb[8]);  s_calib.dig_P3 = (int16_t)(cb[11]<<8|cb[10]);
    s_calib.dig_P4 = (int16_t)(cb[13]<<8|cb[12]); s_calib.dig_P5 = (int16_t)(cb[15]<<8|cb[14]);
    s_calib.dig_P6 = (int16_t)(cb[17]<<8|cb[16]); s_calib.dig_P7 = (int16_t)(cb[19]<<8|cb[18]);
    s_calib.dig_P8 = (int16_t)(cb[21]<<8|cb[20]); s_calib.dig_P9 = (int16_t)(cb[23]<<8|cb[22]);
    bmp_write(BMP280_REG_CTRL, 0x27);
    bmp_write(BMP280_REG_CONF, 0xA0);
    ESP_LOGI(TAG, "BMP280 gefunden (ID=0x%02x)", id);

    i2c_device_config_t aht_cfg = { .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                                     .device_address  = AHT20_ADDR, .scl_speed_hz = 100000 };
    if (i2c_master_bus_add_device(s_bus, &aht_cfg, &s_aht20) == ESP_OK) {
        uint8_t cmd[3] = {AHT20_CMD_INIT, 0x08, 0x00};
        if (i2c_master_transmit(s_aht20, cmd, 3, pdMS_TO_TICKS(I2C_TIMEOUT_MS)) == ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(10));
            ESP_LOGI(TAG, "AHT20 gefunden");
        } else {
            i2c_master_bus_rm_device(s_aht20); s_aht20 = NULL;
        }
    } else { s_aht20 = NULL; }
    return true;
}

static bool sensor_measure(hannah_sensor_data_t *out) {
    uint8_t raw[6];
    if (bmp_read(BMP280_REG_DATA, raw, 6) != ESP_OK) return false;
    int32_t adc_P = ((int32_t)raw[0]<<12)|((int32_t)raw[1]<<4)|(raw[2]>>4);
    int32_t adc_T = ((int32_t)raw[3]<<12)|((int32_t)raw[4]<<4)|(raw[5]>>4);
    int32_t v1 = ((((adc_T>>3)-((int32_t)s_calib.dig_T1<<1)))*(int32_t)s_calib.dig_T2)>>11;
    int32_t v2 = (((((adc_T>>4)-(int32_t)s_calib.dig_T1)*((adc_T>>4)-(int32_t)s_calib.dig_T1))>>12)*(int32_t)s_calib.dig_T3)>>14;
    int32_t t_fine = v1 + v2;
    out->temperature = (float)((t_fine*5+128)>>8)/100.0f;
    int64_t p1 = (int64_t)t_fine - 128000;
    int64_t p2 = p1*p1*(int64_t)s_calib.dig_P6; p2 += (p1*(int64_t)s_calib.dig_P5)<<17;
    p2 += ((int64_t)s_calib.dig_P4)<<35;
    p1  = ((p1*p1*(int64_t)s_calib.dig_P3)>>8)+((p1*(int64_t)s_calib.dig_P2)<<12);
    p1  = ((((int64_t)1<<47)+p1)*(int64_t)s_calib.dig_P1)>>33;
    if (p1 == 0) { out->pressure = 0; } else {
        int64_t p = 1048576 - adc_P;
        p = (((p<<31)-p2)*3125)/p1;
        p1 = ((int64_t)s_calib.dig_P9*(p>>13)*(p>>13))>>25;
        p2 = ((int64_t)s_calib.dig_P8*p)>>19;
        p  = ((p+p1+p2)>>8)+((int64_t)s_calib.dig_P7<<4);
        out->pressure = (float)p/25600.0f;
    }
    out->gas_resistance = NAN;
    out->iaq = NAN; out->iaq_static = NAN; out->co2_equiv = NAN; out->voc_equiv = NAN;
    if (s_aht20) {
        uint8_t trig[3] = {AHT20_CMD_TRIG, 0x33, 0x00};
        i2c_master_transmit(s_aht20, trig, 3, pdMS_TO_TICKS(I2C_TIMEOUT_MS));
        vTaskDelay(pdMS_TO_TICKS(80));
        uint8_t h[6];
        if (i2c_master_receive(s_aht20, h, 6, pdMS_TO_TICKS(I2C_TIMEOUT_MS)) == ESP_OK && !(h[0]&0x80)) {
            uint32_t hraw = ((uint32_t)h[1]<<12)|((uint32_t)h[2]<<4)|(h[3]>>4);
            out->humidity = (float)hraw/1048576.0f*100.0f;
        } else { out->humidity = NAN; }
    } else { out->humidity = NAN; }
    return true;
}
#endif /* HANNAH_SENSOR_TYPE */

/* ========================================================================
 * Sensor-Task
 * ======================================================================== */

static void sensor_task(void *arg)
{
    while (!s_ready) vTaskDelay(pdMS_TO_TICKS(100));

    const hannah_config_t *cfg = hannah_config_get();
    char topic[96];
    snprintf(topic, sizeof(topic), "hannah/satellite/%s/sensors", cfg->device_id);

#if CONFIG_HANNAH_SENSOR_TYPE_BME680

    int64_t last_publish_ms = 0;
    int64_t last_save_ms    = esp_timer_get_time() / 1000LL;

    while (1) {
        bsec_bme_settings_t bme_s = {0};
        int64_t now_ns = esp_timer_get_time() * 1000LL;
        if (bsec_sensor_control(now_ns, &bme_s) != BSEC_OK) {
            vTaskDelay(pdMS_TO_TICKS(3000));
            continue;
        }

        if (bme_s.trigger_measurement) {
            struct bme68x_conf conf = {
                .filter  = BME68X_FILTER_OFF,
                .odr     = BME68X_ODR_NONE,
                .os_hum  = bme_s.humidity_oversampling,
                .os_temp = bme_s.temperature_oversampling,
                .os_pres = bme_s.pressure_oversampling,
            };
            bme68x_set_conf(&conf, &s_bme);

            struct bme68x_heatr_conf heatr = {
                .enable     = bme_s.run_gas ? BME68X_ENABLE : BME68X_DISABLE,
                .heatr_temp = bme_s.heater_temperature,
                .heatr_dur  = bme_s.heater_duration,
            };
            bme68x_set_heatr_conf(BME68X_FORCED_MODE, &heatr, &s_bme);
            bme68x_set_op_mode(BME68X_FORCED_MODE, &s_bme);

            uint32_t del_us = bme68x_get_meas_dur(BME68X_FORCED_MODE, &conf, &s_bme)
                              + (uint32_t)bme_s.heater_duration * 1000;
            s_bme.delay_us(del_us, s_bme.intf_ptr);

            struct bme68x_data data[3];
            uint8_t n_data = 0;
            if (bme68x_get_data(BME68X_FORCED_MODE, data, &n_data, &s_bme) == BME68X_OK && n_data > 0) {
                bsec_input_t inp[6]; uint8_t n_inp = 0;
                int64_t ts = esp_timer_get_time() * 1000LL;

                if (bme_s.process_data & BSEC_PROCESS_TEMPERATURE) {
                    inp[n_inp++] = (bsec_input_t){ .time_stamp = ts, .signal = 0.0f,                .sensor_id = BSEC_INPUT_HEATSOURCE };
                    inp[n_inp++] = (bsec_input_t){ .time_stamp = ts, .signal = data[0].temperature, .sensor_id = BSEC_INPUT_TEMPERATURE };
                }
                if (bme_s.process_data & BSEC_PROCESS_HUMIDITY)
                    inp[n_inp++] = (bsec_input_t){ .time_stamp = ts, .signal = data[0].humidity,    .sensor_id = BSEC_INPUT_HUMIDITY };
                if (bme_s.process_data & BSEC_PROCESS_PRESSURE)
                    inp[n_inp++] = (bsec_input_t){ .time_stamp = ts, .signal = data[0].pressure,    .sensor_id = BSEC_INPUT_PRESSURE };
                if ((bme_s.process_data & BSEC_PROCESS_GAS) && (data[0].status & BME68X_GASM_VALID_MSK))
                    inp[n_inp++] = (bsec_input_t){ .time_stamp = ts, .signal = data[0].gas_resistance, .sensor_id = BSEC_INPUT_GASRESISTOR };

                bsec_output_t out[BSEC_NUMBER_OUTPUTS]; uint8_t n_out = BSEC_NUMBER_OUTPUTS;
                if (n_inp > 0 && bsec_do_steps(inp, n_inp, out, &n_out) == BSEC_OK) {
                    hannah_sensor_data_t d = {
                        .gas_resistance = NAN, .iaq = NAN, .iaq_static = NAN,
                        .co2_equiv = NAN, .voc_equiv = NAN, .iaq_accuracy = 0
                    };
                    for (uint8_t i = 0; i < n_out; i++) {
                        switch (out[i].sensor_id) {
                            case BSEC_OUTPUT_SENSOR_HEAT_COMPENSATED_TEMPERATURE:
                                d.temperature = out[i].signal; break;
                            case BSEC_OUTPUT_SENSOR_HEAT_COMPENSATED_HUMIDITY:
                                d.humidity = out[i].signal; break;
                            case BSEC_OUTPUT_RAW_PRESSURE:
                                d.pressure = out[i].signal / 100.0f; break; /* Pa → hPa */
                            case BSEC_OUTPUT_IAQ:
                                d.iaq = out[i].signal; d.iaq_accuracy = out[i].accuracy; break;
                            case BSEC_OUTPUT_STATIC_IAQ:
                                d.iaq_static = out[i].signal; break;
                            case BSEC_OUTPUT_CO2_EQUIVALENT:
                                d.co2_equiv = out[i].signal; break;
                            case BSEC_OUTPUT_BREATH_VOC_EQUIVALENT:
                                d.voc_equiv = out[i].signal; break;
                            default: break;
                        }
                    }
                    s_last = d;
                    s_has_data = true;
                }
            }
        }

        int64_t now_ms = esp_timer_get_time() / 1000LL;

        if (s_has_data && (now_ms - last_publish_ms) >= 30000) {
            last_publish_ms = now_ms;
            hannah_sensor_data_t *d = &s_last;
            char payload[220];
            snprintf(payload, sizeof(payload),
                "{\"temperature\":%.2f,\"pressure\":%.2f,\"humidity\":%.2f"
                ",\"iaq\":%.1f,\"iaq_accuracy\":%u,\"co2_equiv\":%.1f,\"voc_equiv\":%.3f}",
                d->temperature, d->pressure, d->humidity,
                d->iaq, (unsigned)d->iaq_accuracy, d->co2_equiv, d->voc_equiv);
            hannah_net_mqtt_publish(topic, payload, 1, 1);
            ESP_LOGI(TAG, "T=%.1f°C  P=%.1fhPa  H=%.1f%%  IAQ=%.0f(acc=%u)  CO2=%.0fppm  VOC=%.2fppm",
                     d->temperature, d->pressure, d->humidity,
                     d->iaq, (unsigned)d->iaq_accuracy, d->co2_equiv, d->voc_equiv);
        }

        if ((now_ms - last_save_ms) >= BSEC_STATE_SAVE_MS) {
            last_save_ms = now_ms;
            bsec_save_state();
        }

        /* Warte bis zum nächsten BSEC-Call-Zeitpunkt */
        now_ns = esp_timer_get_time() * 1000LL;
        int64_t wait_us = (bme_s.next_call - now_ns) / 1000LL;
        if (wait_us > 1000) vTaskDelay(pdMS_TO_TICKS(wait_us / 1000));
    }

#else  /* BMP280+AHT20 */

    while (1) {
        hannah_sensor_data_t data = {0};
        if (sensor_measure(&data)) {
            s_last     = data;
            s_has_data = true;
            ESP_LOGI(TAG, "T=%.1f°C  P=%.1f hPa  H=%.1f%%",
                     data.temperature, data.pressure, data.humidity);
            char payload[128];
            snprintf(payload, sizeof(payload),
                     "{\"temperature\":%.2f,\"pressure\":%.2f,\"humidity\":%.2f}",
                     data.temperature, data.pressure, data.humidity);
            hannah_net_mqtt_publish(topic, payload, 1, 1);
        }
        vTaskDelay(pdMS_TO_TICKS(30000));
    }

#endif
}

/* ========================================================================
 * I2C-Scan + öffentliche API
 * ======================================================================== */

static void i2c_scan(void)
{
    ESP_LOGD(TAG, "I2C-Scan (SDA=%d SCL=%d):", CONFIG_HANNAH_SENSOR_SDA_GPIO, CONFIG_HANNAH_SENSOR_SCL_GPIO);
    for (uint8_t addr = 0x08; addr <= 0x77; addr++) {
        i2c_master_dev_handle_t probe;
        i2c_device_config_t cfg = { .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                                    .device_address  = addr, .scl_speed_hz = 100000 };
        if (i2c_master_bus_add_device(s_bus, &cfg, &probe) == ESP_OK) {
            uint8_t dummy;
            if (i2c_master_receive(probe, &dummy, 1, pdMS_TO_TICKS(10)) == ESP_OK)
                ESP_LOGD(TAG, "  Gerät gefunden: 0x%02x", addr);
            i2c_master_bus_rm_device(probe);
        }
    }
}

void hannah_sensors_init(void)
{
    i2c_master_bus_config_t bus_cfg = {
        .i2c_port              = I2C_NUM_0,
        .sda_io_num            = CONFIG_HANNAH_SENSOR_SDA_GPIO,
        .scl_io_num            = CONFIG_HANNAH_SENSOR_SCL_GPIO,
        .clk_source            = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt     = 7,
        .flags.enable_internal_pullup = true,
    };
    if (i2c_new_master_bus(&bus_cfg, &s_bus) != ESP_OK) {
        ESP_LOGE(TAG, "I2C-Bus konnte nicht initialisiert werden"); return;
    }
    i2c_scan();

#if CONFIG_HANNAH_SENSOR_TYPE_BME680
    i2c_device_config_t dev_cfg = { .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                                     .device_address  = BME680_ADDR, .scl_speed_hz = 400000 };
    if (i2c_master_bus_add_device(s_bus, &dev_cfg, &s_dev) != ESP_OK) {
        ESP_LOGE(TAG, "BME680 konnte nicht zum I2C-Bus hinzugefügt werden"); return;
    }
#else
    i2c_device_config_t dev_cfg = { .dev_addr_length = I2C_ADDR_BIT_LEN_7,
                                     .device_address  = BMP280_ADDR, .scl_speed_hz = 100000 };
    if (i2c_master_bus_add_device(s_bus, &dev_cfg, &s_bmp280) != ESP_OK) {
        ESP_LOGE(TAG, "BMP280 konnte nicht zum I2C-Bus hinzugefügt werden"); return;
    }
#endif

    s_ready = sensor_init();
    if (!s_ready) { ESP_LOGE(TAG, "Sensor-Initialisierung fehlgeschlagen"); return; }

    xTaskCreate(sensor_task, "sensors", 8192, NULL, 3, NULL);
    ESP_LOGI(TAG, "Sensor-Task gestartet");
}

bool hannah_sensors_get(hannah_sensor_data_t *out)
{
    if (!s_has_data) return false;
    *out = s_last;
    return true;
}

i2c_master_bus_handle_t hannah_sensors_get_i2c_bus(void)
{
    return s_bus;
}
