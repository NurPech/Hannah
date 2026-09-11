idf_component_register(
    SRCS "main.c"
    INCLUDE_DIRS "."
    REQUIRES
        hannah_config
        hannah_webserver
        hannah_audio
        hannah_net
        hannah_led
        hannah_sensors
        hannah_ota
        hannah_ble
        hannah_asset
        hannah_sd
        nvs_flash
        esp_driver_gpio
        esp_wifi
        esp_event
        esp_netif
        esp_system
        esp_partition
        spi_flash
        espcoredump
        app_update
)

# Eingebettete Ziel-Partitionstabelle für den einmaligen Selfupdate-Boot (#280) —
# App-Image und Ziel-Tabelle sind so immer im Lockstep, kein separater Download
# nötig. Symbole: _binary_coredump_partition_table_{start,end} (siehe main.c).
idf_build_get_property(hannah_ptable_bin_path PARTITION_TABLE_BIN_PATH)
target_add_binary_data(${COMPONENT_LIB} "${hannah_ptable_bin_path}" BINARY
    RENAME_TO coredump_partition_table
    DEPENDS partition_table_bin)
