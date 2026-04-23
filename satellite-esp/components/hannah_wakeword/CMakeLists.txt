idf_component_register(
    SRCS "hannah_wakeword.cpp"
    INCLUDE_DIRS "."
    REQUIRES
        esp_common
        esp-tflite-micro
        microfrontend
)
