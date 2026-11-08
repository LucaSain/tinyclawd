#include "link.h"

#include <string.h>

#include "pico/stdio.h"
#include "pico/stdio/driver.h"
#include "pico/stdio_usb.h"
#include "pico/stdlib.h"

static usblink::Line s_line;
static int s_len = 0;
static bool s_too_long = false;

void usblink::start() { stdio_usb_init(); }

bool usblink::poll(Line* out) {
    for (;;) {
        int ch = getchar_timeout_us(0);
        if (ch < 0) return false;   // nothing waiting
        char c = (char)ch;
        if (c == '\r') continue;
        if (c == '\n') {
            bool got = !s_too_long && s_len > 0;
            if (s_too_long) send("err too long");
            if (got) {
                s_line.s[s_len] = 0;
                memcpy(out, &s_line, sizeof(Line));
            }
            s_len = 0;
            s_too_long = false;
            if (got) return true;
            continue;
        }
        if (s_too_long) continue;
        if (s_len >= MAX_LINE) {
            s_too_long = true;   // skip the rest of this line
            continue;
        }
        s_line.s[s_len++] = c;
    }
}

void usblink::send(const char* s, int) {
    // straight to the usb driver: no crlf translation, dropped when no host has the port
    // open, and PICO_STDIO_USB_STDOUT_TIMEOUT_US caps the wait when the host stops reading
    char out[MAX_LINE + 2];
    int n = strnlen(s, MAX_LINE);
    memcpy(out, s, n);
    out[n++] = '\n';
    stdio_usb.out_chars(out, n);
    if (stdio_usb.out_flush) stdio_usb.out_flush();
}
