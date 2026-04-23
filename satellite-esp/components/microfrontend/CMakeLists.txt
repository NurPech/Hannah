set(LIB "tensorflow/lite/experimental/microfrontend/lib")

idf_component_register(
    SRCS
        "${LIB}/fft.cc"
        "${LIB}/fft_util.cc"
        "${LIB}/filterbank.c"
        "${LIB}/filterbank_util.c"
        "${LIB}/frontend.c"
        "${LIB}/frontend_util.c"
        "${LIB}/kiss_fft_int16.cc"
        "${LIB}/log_lut.c"
        "${LIB}/log_scale.c"
        "${LIB}/log_scale_util.c"
        "${LIB}/noise_reduction.c"
        "${LIB}/noise_reduction_util.c"
        "${LIB}/pcan_gain_control.c"
        "${LIB}/pcan_gain_control_util.c"
        "${LIB}/window.c"
        "${LIB}/window_util.c"
    INCLUDE_DIRS "."
    REQUIRES
        esp_common
        espressif__esp-tflite-micro
)
