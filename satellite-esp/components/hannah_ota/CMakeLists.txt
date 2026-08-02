idf_component_register(
    SRCS "hannah_ota.c"
    INCLUDE_DIRS "."
    REQUIRES
        hannah_config
        hannah_net
        hannah_audio
        hannah_wakeword
        hannah_asset
        hannah_webserver
        esp_http_client
        esp_https_ota
        esp_app_format
        app_update
        espressif__cjson
        nvs_flash
        mbedtls
        spiffs
)
