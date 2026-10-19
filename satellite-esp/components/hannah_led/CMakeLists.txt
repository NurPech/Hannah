idf_component_register(
    SRCS "hannah_led.c"
    INCLUDE_DIRS "."
    REQUIRES
        driver
        espressif__led_strip
        freertos
    PRIV_REQUIRES
        esp_common
        esp_driver_gpio
        esp_timer
        hannah_config
)
