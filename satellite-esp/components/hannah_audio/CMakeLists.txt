idf_component_register(
    SRCS "hannah_audio.c"
    INCLUDE_DIRS "."
    REQUIRES
        nurpech__audiolib
        hannah_config
        driver
        esp_driver_i2s
        esp_driver_gpio
        hannah_net
        hannah_led
        hannah_wakeword
        esp_ringbuf
)
