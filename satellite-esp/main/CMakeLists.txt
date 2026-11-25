idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES
        hannah_config
        hannah_webserver
        hannah_audio
        hannah_net
        hannah_led
        hannah_sensors
        hannah_ota
        hannah_ble
        hannah_asset
        hannah_sd
        hannah_sendspin
        nvs_flash
        esp_driver_gpio
        esp_wifi
        esp_event
        esp_netif
        esp_system
        esp_partition
        spi_flash
        espcoredump
        app_update
)
