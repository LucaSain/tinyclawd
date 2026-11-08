// XIAO RP2040 board main. no rtos: one loop on core 0 does what the three esp tasks do.
//   every 25 ms (40 fps): read complete usb lines, take queued touches, draw, send changed pages
//   touch is polled by a 10 ms timer interrupt (touch.cpp), so i2c flushes don't skew it
#include <stdio.h>
#include <string.h>

#include "pico/rand.h"
#include "pico/stdlib.h"

#include "brain.h"
#include "canvas.h"
#include "hw.h"
#include "link.h"
#include "proto.h"
#include "sh1106.h"
#include "touch.h"

static const uint32_t FRAME_MS = 25;

static Brain s_brain;
static Canvas s_canvas;

static uint32_t now_ms() { return to_ms_since_boot(get_absolute_time()); }

// "hw shot": the last frame as 32 lines of hex, "shot <n> <32 bytes>", for screenshots
static void send_shot() {
    char out[80];
    const uint8_t* b = &s_canvas.buf[0][0];
    for (int i = 0; i < 32; i++) {
        int w = snprintf(out, sizeof(out), "shot %02d ", i);
        for (int k = 0; k < 32; k++) w += snprintf(out + w, sizeof(out) - w, "%02x", b[i * 32 + k]);
        usblink::send(out, 50);
    }
}

static void handle(usblink::Line& line, uint32_t now) {
    char reply[usblink::MAX_LINE + 1];
    reply[0] = 0;
    if (!strcmp(line.s, "hw shot")) {
        send_shot();
        usblink::send("ok shot");
        return;
    }
    if (!hw::line(line.s, reply, sizeof(reply)))
        proto_line(s_brain, line.s, now, reply, sizeof(reply));
    if (reply[0]) usblink::send(reply);
}

int main() {
    usblink::start();
    hw::load();       // saved screen + touch settings
    sh1106::init();   // if the screen is missing, flush keeps retrying
    touch::start();

    s_brain.begin(now_ms(), get_rand_32());
    usblink::send("boot tinyclawd 1");

    usblink::Line line;
    char msg[24];
    uint8_t t;
    absolute_time_t wake = get_absolute_time();

    for (;;) {
        uint32_t now = now_ms();

        // a few lines per frame like the esp queue, the rest wait in the usb buffer
        for (int i = 0; i < 8 && usblink::poll(&line); i++) handle(line, now);

        while (touch::next(&t)) {
            if (t & touch::RAW) {
                usblink::send(t & 1 ? "raw 1" : "raw 0");
                continue;
            }
            s_brain.touch((Touch)t, now);
            if (t == TOUCH_DOWN || t == TOUCH_UP) continue;   // keep the link quiet
            snprintf(msg, sizeof(msg), "touch %s", touch::name((Touch)t));
            usblink::send(msg);
        }

        uint64_t t0 = time_us_64();
        if (hw::test()) hw::draw_test(s_canvas, now);
        else s_brain.render(s_canvas, now);
        uint64_t t1 = time_us_64();
        int pages = sh1106::flush(s_canvas);
        hw::frame((uint32_t)(t1 - t0), (uint32_t)(time_us_64() - t1), pages);
        hw::scores_tick(now);

        // if a frame ran long, don't try to catch up
        wake = delayed_by_ms(wake, FRAME_MS);
        if (absolute_time_diff_us(get_absolute_time(), wake) <= 0) wake = get_absolute_time();
        else sleep_until(wake);
    }
}
