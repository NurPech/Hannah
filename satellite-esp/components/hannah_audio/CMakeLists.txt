idf_component_register(
    SRCS "hannah_audio.c"
    INCLUDE_DIRS "."
    REQUIRES
        nurpech__audiolib
        hannah_config
        driver
        esp_driver_i2s
        esp_driver_i2c
        esp_driver_gpio
        hannah_net
        hannah_led
        hannah_wakeword
        esp_ringbuf
)

# hannah_sensors absichtlich nicht in REQUIRES: IDF 6.0 component_requirements.py
# hat einen Windows-Pfad-Bug der REQUIRES-Propagation für hannah_sensors bricht
# (siehe hannah_sensors/CMakeLists.txt) — direkt per target_link_libraries verlinkt,
# um den Bug zu umgehen. Nötig für den ADAU7118-I2C-Bus-Zugriff (Rev.5, TDM-Mics).
target_include_directories(${COMPONENT_LIB} PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}/../hannah_sensors
)
target_link_libraries(${COMPONENT_LIB} PRIVATE idf::hannah_sensors)
