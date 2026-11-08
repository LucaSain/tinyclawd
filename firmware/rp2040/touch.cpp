#include "touch.h"

#include "hardware/gpio.h"
#include "pico/time.h"
#include "pico/util/queue.h"

static const uint PIN = 26;   // XIAO RP2040 D0
static const int32_t POLL_MS = 10;
static const uint32_t DEBOUNCE_MS = 30;
// tunable over serial (hw double / hw long), plain words so the reads are atomic
static volatile uint32_t s_double_ms = 280;   // gap allowed between two taps
static volatile uint32_t s_long_ms = 700;
static volatile bool s_raw_log = false;

static queue_t s_q;
static repeating_timer_t s_timer;

static struct {
    bool raw_last, down, long_sent, tap_waiting;
    uint32_t changed, pressed_at, released_at;
} s;

static void emit(uint8_t v) { queue_try_add(&s_q, &v); }   // dropped if the loop is way behind

// runs in the timer interrupt, so the timing holds even while a frame goes out over i2c
static bool poll(repeating_timer_t*) {
    uint32_t now = to_ms_since_boot(get_absolute_time());
    bool raw = gpio_get(PIN);

    if (raw != s.raw_last) {
        s.raw_last = raw;
        s.changed = now;
        if (s_raw_log) emit(touch::RAW | raw);
    }

    if (raw != s.down && now - s.changed >= DEBOUNCE_MS) {
        s.down = raw;
        emit(s.down ? TOUCH_DOWN : TOUCH_UP);
        if (s.down) {
            s.pressed_at = now;
            s.long_sent = false;
        } else if (!s.long_sent) {
            if (s.tap_waiting) {
                s.tap_waiting = false;
                emit(TOUCH_DOUBLE);
            } else {
                s.tap_waiting = true;
                s.released_at = now;
            }
        }
    }

    if (s.down && !s.long_sent && now - s.pressed_at >= s_long_ms) {
        s.long_sent = true;
        s.tap_waiting = false;   // tap then long press counts as just the long press
        emit(TOUCH_LONG);
    }

    // single tap only once the double tap window is over.
    // raw high means a second touch is starting, give it time to debounce.
    if (s.tap_waiting && !s.down && !raw && now - s.released_at > s_double_ms) {
        s.tap_waiting = false;
        emit(TOUCH_TAP);
    }
    return true;
}

void touch::start() {
    queue_init(&s_q, 1, 16);
    gpio_init(PIN);
    gpio_set_dir(PIN, GPIO_IN);
    gpio_pull_down(PIN);   // reads untouched if the sensor is unplugged
    add_repeating_timer_ms(-POLL_MS, poll, nullptr, &s_timer);
}

bool touch::next(uint8_t* ev) { return queue_try_remove(&s_q, ev); }

void touch::set_times(uint32_t double_ms, uint32_t long_ms) {
    s_double_ms = double_ms;
    s_long_ms = long_ms;
}

void touch::set_raw_log(bool on) { s_raw_log = on; }

const char* touch::name(Touch t) {
    switch (t) {
    case TOUCH_TAP: return "tap";
    case TOUCH_DOUBLE: return "double";
    case TOUCH_LONG: return "long";
    case TOUCH_DOWN: return "down";
    case TOUCH_UP: return "up";
    }
    return "?";
}
