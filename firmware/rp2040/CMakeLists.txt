cmake_minimum_required(VERSION 3.13)

# Seeed XIAO RP2040 build, same pet engine as the ESP32-C3 one (components/pet).
# pico-sdk 2.x: PICO_SDK_PATH from the environment or -D, else ~/pico/pico-sdk
if(NOT PICO_SDK_PATH)
    if(DEFINED ENV{PICO_SDK_PATH})
        set(PICO_SDK_PATH $ENV{PICO_SDK_PATH})
    else()
        set(PICO_SDK_PATH $ENV{HOME}/pico/pico-sdk)
    endif()
endif()
set(PICO_BOARD seeed_xiao_rp2040 CACHE STRING "board")
include(${PICO_SDK_PATH}/pico_sdk_init.cmake)

project(tinyclawd C CXX ASM)
# c++17 like the sim and the idf build (gnu++2b clashes with lerp() in motion.h)
set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_EXTENSIONS ON)
pico_sdk_init()

set(PET ${CMAKE_CURRENT_LIST_DIR}/../components/pet)
file(GLOB PET_SRCS ${PET}/src/*.cpp)

add_executable(tinyclawd
    main.cpp link.cpp sh1106.cpp touch.cpp hw.cpp store.cpp
    ${PET_SRCS}
)
# sh1106.h and hw.h are shared with the esp build (no esp-idf headers in them)
target_include_directories(tinyclawd PRIVATE
    ${CMAKE_CURRENT_LIST_DIR}
    ${CMAKE_CURRENT_LIST_DIR}/../main
    ${PET}/include
)
target_compile_options(tinyclawd PRIVATE -O2)
target_compile_definitions(tinyclawd PRIVATE
    # the bridge finds the pet by this product name (and the raspberry pi usb id)
    USBD_MANUFACTURER="tinyclawd"
    USBD_PRODUCT="tinyclawd"
    # if the host stops reading, drop replies quickly instead of stalling a frame
    PICO_STDIO_USB_STDOUT_TIMEOUT_US=5000
    PICO_STDIO_DEFAULT_CRLF=0
)
target_link_libraries(tinyclawd
    pico_stdlib pico_rand pico_flash
    hardware_i2c hardware_adc hardware_flash
)
pico_enable_stdio_usb(tinyclawd 1)
pico_enable_stdio_uart(tinyclawd 0)
pico_add_extra_outputs(tinyclawd)   # tinyclawd.uf2
