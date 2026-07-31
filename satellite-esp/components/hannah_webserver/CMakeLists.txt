idf_component_register(
    SRCS "hannah_webserver.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_http_server
        app_update
        esp_app_format
        hannah_config
        hannah_net
        hannah_sensors
        esp_wifi
        esp_system
        freertos
        espressif__cjson
        lwip
)
