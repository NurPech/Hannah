idf_component_register(
    SRCS "hannah_net.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_wifi
        esp_event
        esp_netif
        espressif__mqtt
        lwip
        nvs_flash
        espressif__cjson
        esp_system
)
