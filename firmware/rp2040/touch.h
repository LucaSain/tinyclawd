#pragma once
#include <stdint.h>

#include "brain.h"

// TTP223 on XIAO D0 (GPIO26), high while touched. a 10 ms timer interrupt polls it and
// queues Touch values (TOUCH_DOWN / TOUCH_UP right away, then TAP / DOUBLE / LONG).
namespace touch {

static const uint8_t RAW = 0x80;   // RAW | level: a raw pin change, only with set_raw_log

void start();
bool next(uint8_t* ev);             // pops one event (a Touch, or RAW | level)
const char* name(Touch t);          // "tap", "double", "long"
void set_times(uint32_t double_ms, uint32_t long_ms);
void set_raw_log(bool on);

}
