// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0
//
// JTS Ring B bench writer — feeds a tone/click pattern into the SHM ring using
// ONLY the writer core (no ALSA). Two jobs:
//   1. Validate the jasper-outputd reader end-to-end WITHOUT CamillaDSP: run
//      this against a live outputd in shm_ring mode and listen for the tone,
//      watch /state.shm_ring occupancy/frames_read climb.
//   2. Double as the C-writer -> Rust-reader interop proof (the cross-language
//      half the golden-layout test cannot cover — real bytes through real mmap).
//
// Usage:
//   ring-writer-bench [--path P] [--slots N] [--period F] [--seconds S]
//                     [--pattern tone|click|silence] [--freq HZ] [--paced|--flood]
// Defaults: path=/dev/shm/jts-ring/content.ring, slots=2, period=128,
//           seconds=10, pattern=tone, freq=440, paced (one slot per period of
//           real time — matches a DAC pacer).
//
// Prints writer counters (published/dropped/full_waits) at the end.

#include "jts_ring_shm.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

typedef enum { PAT_TONE, PAT_CLICK, PAT_SILENCE } pattern_t;

static void fill_slot(int16_t *buf, size_t frames, uint32_t channels, pattern_t pat,
                      double freq, uint64_t frame_base) {
    for (size_t f = 0; f < frames; f++) {
        int16_t v = 0;
        switch (pat) {
            case PAT_TONE: {
                double t = (double)(frame_base + f) / 48000.0;
                // -12 dBFS tone so a lab listen is comfortable.
                v = (int16_t)(8192.0 * sin(2.0 * M_PI * freq * t));
                break;
            }
            case PAT_CLICK:
                // A short click at the top of each ~0.5 s so drops are audible.
                v = ((frame_base + f) % 24000 < 24) ? 12000 : 0;
                break;
            case PAT_SILENCE:
                v = 0;
                break;
        }
        for (uint32_t c = 0; c < channels; c++) buf[f * channels + c] = v;
    }
}

int main(int argc, char **argv) {
    const char *path = "/dev/shm/jts-ring/content.ring";
    uint32_t slots = 2;
    uint32_t period = 128;
    double seconds = 10.0;
    double freq = 440.0;
    pattern_t pat = PAT_TONE;
    int paced = 1;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--path") && i + 1 < argc) {
            path = argv[++i];
        } else if (!strcmp(argv[i], "--slots") && i + 1 < argc) {
            slots = (uint32_t)atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--period") && i + 1 < argc) {
            period = (uint32_t)atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--seconds") && i + 1 < argc) {
            seconds = atof(argv[++i]);
        } else if (!strcmp(argv[i], "--freq") && i + 1 < argc) {
            freq = atof(argv[++i]);
        } else if (!strcmp(argv[i], "--pattern") && i + 1 < argc) {
            const char *p = argv[++i];
            if (!strcmp(p, "tone")) pat = PAT_TONE;
            else if (!strcmp(p, "click")) pat = PAT_CLICK;
            else if (!strcmp(p, "silence")) pat = PAT_SILENCE;
            else { fprintf(stderr, "unknown pattern %s\n", p); return 2; }
        } else if (!strcmp(argv[i], "--paced")) {
            paced = 1;
        } else if (!strcmp(argv[i], "--flood")) {
            paced = 0;
        } else {
            fprintf(stderr,
                    "usage: %s [--path P] [--slots N] [--period F] [--seconds S] "
                    "[--pattern tone|click|silence] [--freq HZ] [--paced|--flood]\n",
                    argv[0]);
            return 2;
        }
    }

    jts_ring_geometry_t g = {
        .rate = 48000,
        .channels = 2,
        .sample_format = JTS_RING_SAMPLE_FORMAT_S16LE,
        .period_frames = period,
        .n_slots = slots,
    };
    jts_ring_writer_t w;
    int rc = jts_ring_writer_open(path, &g, &w);
    if (rc != 0) {
        fprintf(stderr, "event=bench.open_failed rc=%d path=%s\n", rc, path);
        return 1;
    }
    fprintf(stderr,
            "event=bench.started path=%s slots=%u period=%u seconds=%.1f pattern=%d "
            "freq=%.0f paced=%d\n",
            path, slots, period, seconds, (int)pat, freq, paced);

    size_t n = jts_ring_samples_per_slot(&g);
    int16_t *buf = malloc(n * sizeof(int16_t));
    if (!buf) { jts_ring_writer_close(&w); return 1; }

    uint64_t total_periods = (uint64_t)(seconds * 48000.0 / (double)period);
    // period duration in ns for the paced loop.
    uint64_t period_ns = (uint64_t)period * 1000000000ull / 48000ull;
    uint64_t frame_base = 0;
    uint64_t next_deadline = jts_ring_monotonic_ns();

    for (uint64_t p = 0; p < total_periods; p++) {
        fill_slot(buf, period, g.channels, pat, freq, frame_base);
        jts_ring_writer_publish(&w, buf);
        frame_base += period;
        if (paced) {
            next_deadline += period_ns;
            uint64_t now = jts_ring_monotonic_ns();
            if (next_deadline > now) {
                uint64_t sleep_ns = next_deadline - now;
                struct timespec ts = {.tv_sec = (time_t)(sleep_ns / 1000000000ull),
                                      .tv_nsec = (long)(sleep_ns % 1000000000ull)};
                nanosleep(&ts, NULL);
            }
        }
    }

    fprintf(stderr,
            "event=bench.done published_slots=%llu drop_no_reader=%llu full_waits=%llu "
            "occupancy_slots=%llu\n",
            (unsigned long long)w.published_slots,
            (unsigned long long)w.drop_no_reader, (unsigned long long)w.full_waits,
            (unsigned long long)jts_ring_writer_occupancy_slots(&w));

    free(buf);
    jts_ring_writer_close(&w);
    return 0;
}
