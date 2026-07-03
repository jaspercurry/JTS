// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0
//
// JTS Ring A bench READER — drains the SHM ring using ONLY the reader core (no
// ALSA). The capture-direction mirror of ring_writer_bench.c. Two jobs:
//   1. Prove the jasper-fanin WRITER end-to-end WITHOUT CamillaDSP: run this
//      against a live fanin in shm_ring mode (or the C writer bench) and watch
//      frames_read / silence_periods / occupancy. It is the on-Pi interop proof
//      the golden-layout test cannot cover — real bytes through a real mmap,
//      the Rust RingWriter feeding the C reader core.
//   2. Model the capture ioplug's DAC-pace consumer for scratch-harness testing:
//      attach, drain one slot per period of real time, and — when the writer is
//      heartbeat-dead — count fabricated-silence periods exactly as the capture
//      ioplug would, so the writer-death / writer-return shapes can be exercised
//      without a full CamillaDSP capture path.
//
// Usage:
//   ring-reader-bench [--path P] [--slots N] [--period F] [--seconds S]
//                     [--paced|--flood] [--verbose]
// Defaults: path=/dev/shm/jts-ring/program.ring (Ring A), slots=8, period=128,
//           seconds=10, paced (one slot per period of real time — matches a DAC
//           pacer; --flood drains as fast as possible for a throughput smoke).
//
// It prints reader counters (frames_read_slots / empty_reads /
// startup_empty_reads / silence_periods / reader_resyncs / epoch_resets /
// occupancy) at the end. A nonzero exit means the reader could not attach
// (-EBUSY if a live reader already owns the ring — the SPSC guard).

#include "jts_ring_shm.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

int main(int argc, char **argv) {
    const char *path = "/dev/shm/jts-ring/program.ring";
    uint32_t slots = 8;
    uint32_t period = 128;
    double seconds = 10.0;
    int paced = 1;
    int verbose = 0;

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--path") && i + 1 < argc) {
            path = argv[++i];
        } else if (!strcmp(argv[i], "--slots") && i + 1 < argc) {
            slots = (uint32_t)atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--period") && i + 1 < argc) {
            period = (uint32_t)atoi(argv[++i]);
        } else if (!strcmp(argv[i], "--seconds") && i + 1 < argc) {
            seconds = atof(argv[++i]);
        } else if (!strcmp(argv[i], "--paced")) {
            paced = 1;
        } else if (!strcmp(argv[i], "--flood")) {
            paced = 0;
        } else if (!strcmp(argv[i], "--verbose")) {
            verbose = 1;
        } else {
            fprintf(stderr,
                    "usage: %s [--path P] [--slots N] [--period F] [--seconds S] "
                    "[--paced|--flood] [--verbose]\n",
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
    jts_ring_reader_t r;
    int rc = jts_ring_reader_open(path, &g, &r);
    if (rc != 0) {
        fprintf(stderr, "event=reader_bench.open_failed rc=%d path=%s%s\n", rc, path,
                rc == -EBUSY ? " (ring already has a live reader — EBUSY)" : "");
        return 1;
    }
    fprintf(stderr,
            "event=reader_bench.started path=%s slots=%u period=%u seconds=%.1f paced=%d "
            "attach_resyncs=%llu\n",
            path, slots, period, seconds, paced,
            (unsigned long long)r.attach_resyncs);

    size_t n = jts_ring_samples_per_slot(&g);
    int16_t *buf = malloc(n * sizeof(int16_t));
    if (!buf) {
        jts_ring_reader_close(&r);
        return 1;
    }

    uint64_t total_periods = (uint64_t)(seconds * 48000.0 / (double)period);
    uint64_t period_ns = (uint64_t)period * 1000000000ull / 48000ull;
    uint64_t next_deadline = jts_ring_monotonic_ns();
    // Count fabricated-silence periods exactly as the capture ioplug would: an
    // empty read WHILE the writer is heartbeat-dead is a silence period.
    uint64_t silence_periods = 0;

    for (uint64_t p = 0; p < total_periods; p++) {
        jts_ring_slot_read_t got = jts_ring_reader_consume(&r, buf);
        if (got == JTS_RING_SLOT_EMPTY && !jts_ring_reader_writer_is_live(&r)) {
            silence_periods++;
        }
        if (verbose && (p % 100 == 0)) {
            fprintf(stderr,
                    "  tick=%llu got=%s occ=%llu frames_read=%llu silence=%llu "
                    "writer_live=%d\n",
                    (unsigned long long)p, got == JTS_RING_SLOT_FILLED ? "filled" : "empty",
                    (unsigned long long)jts_ring_reader_occupancy_slots(&r),
                    (unsigned long long)r.frames_read_slots,
                    (unsigned long long)silence_periods,
                    jts_ring_reader_writer_is_live(&r));
        }
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
            "event=reader_bench.done frames_read_slots=%llu empty_reads=%llu "
            "startup_empty_reads=%llu silence_periods=%llu reader_resyncs=%llu "
            "epoch_resets=%llu occupancy=%llu\n",
            (unsigned long long)r.frames_read_slots, (unsigned long long)r.empty_reads,
            (unsigned long long)r.startup_empty_reads, (unsigned long long)silence_periods,
            (unsigned long long)r.reader_resyncs, (unsigned long long)r.epoch_resets,
            (unsigned long long)jts_ring_reader_occupancy_slots(&r));

    free(buf);
    jts_ring_reader_close(&r);
    return 0;
}
