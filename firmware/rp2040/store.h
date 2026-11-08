#pragma once
#include <stdint.h>

// what the esp build keeps in nvs, here in the last 4 KB sector of the flash.
// every save appends a 256 byte record, the sector is only erased when it is full
// (16 saves per erase), the newest record with a good checksum wins.
namespace store {

static const uint8_t VERSION = 1;
static const int GAMES = 8;

struct Data {
    // hw settings, same fields and ranges as the esp Saved struct. version 0 = never saved
    uint8_t version, offset, flip, mirror, contrast;
    uint16_t khz, double_ms, long_ms;
    int32_t best[GAMES];   // game high scores by game index
};

bool load(Data* out);         // false if nothing was saved yet
bool save(const Data& d);

}
