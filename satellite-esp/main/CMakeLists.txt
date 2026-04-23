idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES
        hannah_audio
        hannah_net
        hannah_led
        nvs_flash
        esp_wifi
        esp_event
        esp_netif
)
