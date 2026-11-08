// SH1106 over the rp2040 i2c block. same behaviour as main/sh1106.cpp: only changed pages
// go out, a missing screen or a bus glitch never blocks for long, it retries every second.
#include "sh1106.h"

#include <string.h>

#include "hardware/gpio.h"
#include "hardware/i2c.h"
#include "pico/time.h"

// XIAO RP2040: D4 = GPIO6 = SDA, D5 = GPIO7 = SCL, both on i2c1
static i2c_inst_t* const BUS = i2c1;
static const uint PIN_SDA = 6;
static const uint PIN_SCL = 7;
static const uint8_t ADDR = 0x3C;
static const uint XFER_TIMEOUT_US = 50000;
static const uint32_t RETRY_MS = 1000;    // wait this long after an error before trying again

static uint8_t s_last[Canvas::PAGES][Canvas::W];
static bool s_bus = false;        // i2c block is set up
static bool s_ready = false;      // init sequence went through
static bool s_full = true;        // next flush sends every page
static bool s_need_on = false;    // send display-on after the next good flush
static uint32_t s_retry_at = 0;
static sh1106::Conf s_conf;

static uint32_t now_ms() { return to_ms_since_boot(get_absolute_time()); }

static void fail() {
    s_ready = false;
    s_bus = false;   // reset the i2c block too, it can be stuck after a glitch
    s_retry_at = now_ms() + RETRY_MS;
}

static bool send(const uint8_t* buf, size_t len) {
    if (i2c_write_timeout_us(BUS, ADDR, buf, len, false, XFER_TIMEOUT_US) == (int)len) return true;
    fail();
    return false;
}

static bool cmds(const uint8_t* c, size_t n) {
    uint8_t buf[32];
    buf[0] = 0x00;  // control byte: command stream
    if (n > sizeof(buf) - 1) return false;
    memcpy(buf + 1, c, n);
    return send(buf, n + 1);
}

static void bus_setup() {
    if (s_bus) return;
    i2c_deinit(BUS);
    i2c_init(BUS, s_conf.hz);
    gpio_set_function(PIN_SDA, GPIO_FUNC_I2C);
    gpio_set_function(PIN_SCL, GPIO_FUNC_I2C);
    gpio_pull_up(PIN_SDA);
    gpio_pull_up(PIN_SCL);
    s_bus = true;
}

static bool init_seq() {
    // same sequence as the old mochi oled.c, it is proven on this panel
    const uint8_t seq[] = {
        0xAE,        // display off
        0xD5, 0x80,  // clock divide / osc
        0xA8, 0x3F,  // multiplex 64
        0xD3, 0x00,  // display offset 0
        0x40,        // start line 0
        0xAD, 0x8B,  // dc-dc on
        (uint8_t)(s_conf.mirror != s_conf.flip ? 0xA0 : 0xA1),  // segment remap
        (uint8_t)(s_conf.flip ? 0xC0 : 0xC8),                   // com scan direction
        0xDA, 0x12,  // com pins
        0x81, s_conf.contrast,
        0xD9, 0x22,  // pre-charge
        0xDB, 0x35,  // vcomh
        0xA4,        // show ram
        0xA6,        // not inverted
    };
    if (!cmds(seq, sizeof(seq))) return false;
    s_ready = true;
    s_full = true;
    s_need_on = true;  // turn on only after the ram has a real frame in it
    return true;
}

bool sh1106::init() {
    bus_setup();
    if (!init_seq()) return false;
    Canvas blank;
    blank.clear();
    flush(blank);
    return s_ready;
}

static bool send_page(int p, const uint8_t* row) {
    const uint8_t addr[] = {
        (uint8_t)(0xB0 | p),
        (uint8_t)(0x00 | (s_conf.offset & 0x0F)),
        (uint8_t)(0x10 | (s_conf.offset >> 4)),
    };
    if (!cmds(addr, sizeof(addr))) return false;

    uint8_t buf[Canvas::W + 1];
    buf[0] = 0x40;  // control byte: data stream
    memcpy(buf + 1, row, Canvas::W);
    return send(buf, sizeof(buf));
}

int sh1106::flush(const Canvas& c) {
    if (!s_ready) {
        if ((int32_t)(now_ms() - s_retry_at) < 0) return 0;
        bus_setup();
        if (!init_seq()) return 0;
    }

    int sent = 0;
    for (int p = 0; p < Canvas::PAGES; p++) {
        if (!s_full && memcmp(s_last[p], c.buf[p], Canvas::W) == 0) continue;
        if (!send_page(p, c.buf[p])) return sent;  // s_full stays set, all pages go next time
        memcpy(s_last[p], c.buf[p], Canvas::W);
        sent++;
    }
    s_full = false;
    if (s_need_on) {
        const uint8_t on = 0xAF;
        if (!cmds(&on, 1)) return sent;
    }
    s_need_on = false;
    return sent;
}

void sh1106::configure(const Conf& c) {
    bool speed = c.hz != s_conf.hz;
    s_conf = c;
    if (speed && s_bus) {
        // reconnect at the new clock on the next flush
        s_bus = false;
        s_ready = false;
        s_retry_at = 0;
        return;
    }
    if (s_ready) init_seq();  // offset, flip, mirror, contrast, then a full redraw
}

bool sh1106::ok() { return s_ready; }

int sh1106::scan(uint8_t* found, int max) {
    bus_setup();
    int n = 0;
    uint8_t rx;
    for (uint8_t a = 0x08; a < 0x78 && n < max; a++)
        if (i2c_read_timeout_us(BUS, a, &rx, 1, false, 20000) >= 0) found[n++] = a;
    return n;
}
