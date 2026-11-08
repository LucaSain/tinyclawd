#pragma once

// line link over the usb cdc port (pico stdio_usb). no tasks on the rp2040:
// the main loop polls for complete lines.
namespace usblink {

static const int MAX_LINE = 120;

struct Line {
    char s[MAX_LINE + 1];
};

void start();
bool poll(Line* out);   // true when a complete line is in out. answers "err too long" itself
void send(const char* s, int wait_ms = 2);   // writes s + "\n", dropped if no one is reading

}
