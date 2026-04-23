idf_component_register(
    SRCS "hannah_led.c"
    INCLUDE_DIRS "."
    REQUIRES
        driver
        espressif__led_strip
)
