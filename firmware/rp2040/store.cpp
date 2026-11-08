#include "store.h"

#include <string.h>

#include "hardware/flash.h"
#include "pico/flash.h"

static const uint32_t MAGIC = 0x7C1A3D01;
static const uint32_t SECTOR = PICO_FLASH_SIZE_BYTES - FLASH_SECTOR_SIZE;
static const int SLOTS = FLASH_SECTOR_SIZE / FLASH_PAGE_SIZE;

struct Rec {
    uint32_t magic;
    uint32_t seq;
    store::Data data;
    uint32_t sum;
};
static_assert(sizeof(Rec) <= FLASH_PAGE_SIZE, "one record per flash page");

static const Rec* slot(int i) {
    return (const Rec*)(XIP_BASE + SECTOR + (uint32_t)i * FLASH_PAGE_SIZE);
}

static uint32_t checksum(const Rec& r) {
    // fnv-1a over everything before sum
    uint32_t h = 2166136261u;
    const uint8_t* p = (const uint8_t*)&r;
    for (size_t i = 0; i < offsetof(Rec, sum); i++) h = (h ^ p[i]) * 16777619u;
    return h;
}

static bool valid(const Rec* r) { return r->magic == MAGIC && r->sum == checksum(*r); }

static bool erased(const Rec* r) {
    const uint8_t* p = (const uint8_t*)r;
    for (int i = 0; i < FLASH_PAGE_SIZE; i++)
        if (p[i] != 0xFF) return false;
    return true;
}

// newest valid record, or -1
static int newest() {
    int best = -1;
    for (int i = 0; i < SLOTS; i++)
        if (valid(slot(i)) && (best < 0 || (int32_t)(slot(i)->seq - slot(best)->seq) > 0)) best = i;
    return best;
}

bool store::load(Data* out) {
    int i = newest();
    if (i < 0) return false;
    memcpy(out, &slot(i)->data, sizeof(Data));
    return true;
}

struct Job {
    int slot;
    bool erase;
    uint8_t page[FLASH_PAGE_SIZE];
};

static void write_job(void* p) {
    Job* j = (Job*)p;
    if (j->erase) flash_range_erase(SECTOR, FLASH_SECTOR_SIZE);
    flash_range_program(SECTOR + (uint32_t)j->slot * FLASH_PAGE_SIZE, j->page, FLASH_PAGE_SIZE);
}

bool store::save(const Data& d) {
    int last = newest();
    static Job job;
    memset(&job, 0xFF, sizeof(job));
    job.erase = false;
    job.slot = -1;
    for (int i = 0; i < SLOTS; i++)
        if (erased(slot(i))) {
            job.slot = i;
            break;
        }
    if (job.slot < 0) {
        job.slot = 0;
        job.erase = true;
    }

    Rec r;
    memset(&r, 0, sizeof(r));
    r.magic = MAGIC;
    r.seq = last >= 0 ? slot(last)->seq + 1 : 1;
    r.data = d;
    r.sum = checksum(r);
    memcpy(job.page, &r, sizeof(r));

    // interrupts are off while the flash is busy (an erase takes ~50 ms, the usb host retries)
    if (flash_safe_execute(write_job, &job, 100) != PICO_OK) return false;
    return valid(slot(job.slot)) && slot(job.slot)->seq == r.seq;
}
