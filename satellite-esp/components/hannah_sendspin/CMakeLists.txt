idf_component_register(
    SRCS "hannah_sendspin.cpp"
    INCLUDE_DIRS "."
    REQUIRES
        hannah_audio
        hannah_config
        nvs_flash
        esp_netif
        esp_wifi
        esp_event
        esp_timer
        esp_app_format
        lwip
)
