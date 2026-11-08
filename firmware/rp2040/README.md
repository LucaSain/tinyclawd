# firmware for the Seeed XIAO RP2040

same pet as the ESP32-C3 build: the engine in `../components/pet` is compiled unchanged, this
folder only swaps the board part (screen, touch, usb link, saved settings). the serial protocol
and every `hw ...` line work the same (`../PROTOCOL.md`, `../README.md`).

## build

pico-sdk 2.x and `arm-none-eabi-gcc`. the sdk is looked up in `PICO_SDK_PATH`, else `~/pico/pico-sdk`:

```bash
git clone --depth 1 --branch 2.1.1 https://github.com/raspberrypi/pico-sdk ~/pico/pico-sdk
git -C ~/pico/pico-sdk submodule update --init --depth 1 lib/tinyusb

cd firmware/rp2040
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
```

the first configure also fetches and builds `picotool` (the sdk needs it to make the uf2).

## flash

hold **B** (boot) on the XIAO while plugging it in. it shows up as a drive called `RPI-RP2`,
copy `build/tinyclawd.uf2` onto it and it reboots into the pet.

once the pet is running you don't need the button again: opening the port at 1200 baud
reboots it into the `RPI-RP2` drive:

```bash
stty -F /dev/ttyACM0 1200
```

## pins

| what | XIAO pin | gpio |
|---|---|---|
| OLED SDA | D4 | 6 (i2c1) |
| OLED SCL | D5 | 7 (i2c1) |
| touch (TTP223, high while touched) | D0 | 26 |
| OLED / touch power | 3V3, GND | |

change `PIN` in `touch.cpp` or `PIN_SDA` / `PIN_SCL` in `sh1106.cpp` if you wire it differently
(the i2c pins have to be an i2c1 pair, or switch `BUS` to `i2c0`).

## how it differs from the esp build

- no rtos. one loop on core 0 runs at 40 fps: reads up to 8 usb lines, takes queued touches,
  draws, sends the changed pages. touch is polled by a 10 ms timer interrupt, so a slow i2c
  flush doesn't skew tap / double / long timing.
- usb shows up as `2e8a:000a`, product and manufacturer `tinyclawd`. the bridge and
  `install.sh` find it by that name, a plain pico is never opened.
- `hw save` and the game high scores go into the last 4 KB flash sector (`store.cpp`), as
  256 byte records, so the sector is erased once every 16 saves.
- `hw temp` reads the rp2040's own sensor (rough, a few degrees off).
