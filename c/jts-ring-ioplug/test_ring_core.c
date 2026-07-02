// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0
//
// Host unit test for the JTS Ring B writer core (jts_ring_shm.c). No ALSA, no
// Rust — compiles and runs on any host (macOS/Linux) via the Makefile `test`
// target and scripts/ring-proto/host-check.sh. It exercises:
//   - the `_Static_assert`ed header layout (compiled in from the header),
//   - geometry validation,
//   - create + writer publish + a simulated reader consume with the exact
//     seq/ordering discipline (the reader half is inlined here so the test does
//     not depend on the Rust crate),
//   - ping-pong bounding at n_slots,
//   - the no-live-reader free-run DROP path (the aplay-resolvability behavior).
//
// The cross-language C-writer -> Rust-reader interop is proven separately by
// ring_writer_bench.c feeding jasper-outputd (on-Pi). This host test proves the
// core is self-consistent.

#include "jts_ring_shm.h"

#include <assert.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static int g_failures = 0;

#define CHECK(cond, msg)                                                        \
    do {                                                                        \
        if (!(cond)) {                                                          \
            fprintf(stderr, "FAIL: %s (%s:%d)\n", msg, __FILE__, __LINE__);     \
            g_failures++;                                                       \
        }                                                                       \
    } while (0)

// A minimal in-process reader mirroring rust/jasper-ring's try_consume_slot:
// Acquire write_seq, if empty -> silence, else copy slot (r % n_slots), then
// Release read_seq. Stamps reader pid/heartbeat so the writer sees it live.
typedef struct {
    void *base;
    jts_ring_geometry_t geometry;
    uint64_t read_seq;
    size_t slot_bytes;
    size_t samples_per_slot;
} test_reader_t;

static void reader_attach(test_reader_t *r, const jts_ring_writer_t *w) {
    r->base = w->base;
    r->geometry = w->geometry;
    r->slot_bytes = w->slot_bytes;
    r->samples_per_slot = w->samples_per_slot;
    jts_ring_header_t *h = (jts_ring_header_t *)r->base;
    // Resync to the writer tip (drop stale) — mirrors the Rust reader attach.
    uint64_t wseq = atomic_load_explicit(&h->write_seq, memory_order_acquire);
    r->read_seq = wseq;
    atomic_store_explicit(&h->read_seq, wseq, memory_order_release);
    atomic_store_explicit(&h->reader_pid, (uint64_t)getpid(), memory_order_relaxed);
    atomic_store_explicit(&h->reader_heartbeat_ns, jts_ring_monotonic_ns(),
                          memory_order_relaxed);
}

// Returns 1 if a slot was consumed into `out`, 0 if empty (out zero-filled).
static int reader_consume(test_reader_t *r, int16_t *out) {
    jts_ring_header_t *h = (jts_ring_header_t *)r->base;
    atomic_store_explicit(&h->reader_heartbeat_ns, jts_ring_monotonic_ns(),
                          memory_order_relaxed);
    uint64_t wseq = atomic_load_explicit(&h->write_seq, memory_order_acquire);
    uint64_t rr = r->read_seq;
    if (wseq == rr) {
        memset(out, 0, r->slot_bytes);
        return 0;
    }
    uint32_t slot_index = (uint32_t)(rr % (uint64_t)r->geometry.n_slots);
    const uint8_t *base = (const uint8_t *)r->base;
    const int16_t *slot =
        (const int16_t *)(base + JTS_RING_HEADER_BYTES + (size_t)slot_index * r->slot_bytes);
    memcpy(out, slot, r->slot_bytes);
    uint64_t next = rr + 1;
    r->read_seq = next;
    atomic_store_explicit(&h->read_seq, next, memory_order_release);
    return 1;
}

static jts_ring_geometry_t proto_geometry(void) {
    jts_ring_geometry_t g = {
        .rate = 48000,
        .channels = 2,
        .sample_format = JTS_RING_SAMPLE_FORMAT_S16LE,
        .period_frames = 128,
        .n_slots = 2,
    };
    return g;
}

// Build a unique /tmp path (host test — not /dev/shm; the owned-path reclaim is
// unit-tested separately below with the literal string).
static void tmp_path(char *buf, size_t buflen, const char *tag) {
    snprintf(buf, buflen, "/tmp/jts-ring-ctest-%d-%s.ring", (int)getpid(), tag);
    unlink(buf); // fresh
}

static void test_geometry_math_and_validation(void) {
    jts_ring_geometry_t g = proto_geometry();
    CHECK(jts_ring_samples_per_slot(&g) == 256, "samples_per_slot");
    CHECK(jts_ring_slot_bytes(&g) == 512, "slot_bytes");
    CHECK(jts_ring_file_size(&g) == 128 + 2 * 512, "file_size");

    const char *reason = NULL;
    CHECK(jts_ring_geometry_validate(&g, &reason) == 0, "valid geometry");

    jts_ring_geometry_t bad = g;
    bad.channels = 4;
    CHECK(jts_ring_geometry_validate(&bad, &reason) != 0, "reject 4ch");
    bad = g;
    bad.n_slots = 1;
    CHECK(jts_ring_geometry_validate(&bad, &reason) != 0, "reject 1 slot");
    bad = g;
    bad.n_slots = 17; // ceiling is 16 (raised 4 -> 16 on 2026-07-02)
    CHECK(jts_ring_geometry_validate(&bad, &reason) != 0, "reject 17 slots (> ceiling 16)");
    bad = g;
    bad.sample_format = JTS_RING_SAMPLE_FORMAT_S32LE;
    CHECK(jts_ring_geometry_validate(&bad, &reason) != 0, "reject S32LE (prototype)");
}

static void test_publish_consume_roundtrip(void) {
    char path[256];
    tmp_path(path, sizeof(path), "roundtrip");
    jts_ring_geometry_t g = proto_geometry();
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");

    test_reader_t r;
    reader_attach(&r, &w);

    size_t n = w.samples_per_slot;
    int16_t *payload = malloc(n * sizeof(int16_t));
    for (size_t i = 0; i < n; i++) payload[i] = (int16_t)(i * 3 - 5);

    jts_ring_publish_result_t pr = jts_ring_writer_publish(&w, payload);
    CHECK(pr == JTS_RING_PUBLISH_OK, "publish ok");
    CHECK(w.published_slots == 1, "published_slots == 1");

    int16_t *out = calloc(n, sizeof(int16_t));
    CHECK(reader_consume(&r, out) == 1, "consume filled");
    CHECK(memcmp(out, payload, n * sizeof(int16_t)) == 0, "payload roundtrip");
    // Ring empty again.
    CHECK(reader_consume(&r, out) == 0, "consume empty after drain");

    free(payload);
    free(out);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_ping_pong_bounding(void) {
    char path[256];
    tmp_path(path, sizeof(path), "pingpong");
    jts_ring_geometry_t g = proto_geometry();
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    test_reader_t r;
    reader_attach(&r, &w);

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    for (size_t i = 0; i < n; i++) s[i] = 1;

    CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish slot 0");
    CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish slot 1");
    CHECK(jts_ring_writer_occupancy_slots(&w) == 2, "occupancy 2 (full)");

    // The ring is full and the reader IS live (attached above). A third publish
    // must wait then, since the reader never advances here, DROP after the
    // bounded tick cap (not hang). This proves the bounded-wait -> drop guard.
    jts_ring_publish_result_t pr = jts_ring_writer_publish(&w, s);
    CHECK(pr == JTS_RING_PUBLISH_DROPPED, "full-ring bounded wait -> drop");
    CHECK(w.full_waits >= 1, "counted a full wait");

    // Consume one; now a publish succeeds again (ping-pong).
    int16_t *out = calloc(n, sizeof(int16_t));
    CHECK(reader_consume(&r, out) == 1, "consume one");
    CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish after consume");

    free(s);
    free(out);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_no_reader_free_run_drop(void) {
    // No reader ever attaches (reader_pid stays 0). The writer must fill the
    // ring, then FREE-RUN DROP rather than block — this is the behavior that
    // keeps Camilla from wedging when outputd's flag is off and that makes the
    // `aplay -D jts_ring_playback ... /dev/zero` resolvability probe terminate.
    char path[256];
    tmp_path(path, sizeof(path), "noreader");
    jts_ring_geometry_t g = proto_geometry();
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    // Publish more slots than the ring holds; the overflow must drop, not hang.
    int ok = 0, dropped = 0;
    for (int i = 0; i < 10; i++) {
        jts_ring_publish_result_t pr = jts_ring_writer_publish(&w, s);
        if (pr == JTS_RING_PUBLISH_OK) ok++;
        else if (pr == JTS_RING_PUBLISH_DROPPED) dropped++;
    }
    CHECK(ok == (int)g.n_slots, "filled exactly n_slots before dropping");
    CHECK(dropped == 10 - (int)g.n_slots, "dropped the rest (no live reader)");
    CHECK(w.drop_no_reader == (uint64_t)dropped, "drop_no_reader counter");

    free(s);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_no_reader_pointer_keeps_advancing(void) {
    // B1 regression (Blocker): the honest-pointer fix must NOT re-break the Q1
    // free-run invariant. With no live reader, the writer drop-OLDEST path
    // advances BOTH write_seq and read_seq, so occupancy stays bounded at
    // n_slots (never pinned "full forever" with read_seq stuck at 0). That is
    // what keeps the ioplug's in_flight bounded and hw_ptr = appl_frames -
    // in_flight ADVANCING, so ALSA's avail never sticks at 0 and aplay/Camilla
    // never wedge on a readerless ring. Before the fix, occupancy pinned at
    // n_slots forever, in_flight pinned at buffer_size, avail pinned at 0 -> the
    // writer wedged waiting for a pointer that could never move.
    char path[256];
    tmp_path(path, sizeof(path), "noreaderptr");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    const uint32_t period = g.period_frames;

    uint64_t appl_frames = 0;   // mirrors the ioplug accept counter
    uint64_t prev_hw_ptr = 0;   // hw_ptr = appl_frames - in_flight (ioplug pointer)
    // Publish well past the ring depth. Every publish must return promptly (no
    // hang) and the derived hw_ptr must be monotonically non-decreasing AND
    // strictly advance once the ring is full (the drop-oldest lap frees a slot
    // per publish).
    for (int i = 0; i < 50; i++) {
        jts_ring_publish_result_t pr = jts_ring_writer_publish(&w, s);
        CHECK(pr == JTS_RING_PUBLISH_OK || pr == JTS_RING_PUBLISH_DROPPED,
              "publish returns (never hangs) with no reader");
        appl_frames += period;
        uint64_t in_flight = jts_ring_writer_occupancy_slots(&w) * (uint64_t)period;
        // Occupancy stays bounded at the ring depth — it is NOT pinned to the
        // buffer with read_seq stuck at 0.
        CHECK(in_flight <= (uint64_t)g.n_slots * period,
              "in_flight bounded by ring depth (occupancy not pinned)");
        uint64_t hw_ptr = appl_frames - in_flight;
        CHECK(hw_ptr >= prev_hw_ptr, "hw_ptr monotonic non-decreasing (never back-jumps)");
        prev_hw_ptr = hw_ptr;
    }
    // After steady free-run, the ring is full (occupancy == n_slots) but hw_ptr
    // has advanced far past 0 — avail = buffer - (appl - hw) is stable, not 0.
    CHECK(jts_ring_writer_occupancy_slots(&w) == (uint64_t)g.n_slots,
          "ring full at steady free-run");
    CHECK(prev_hw_ptr == appl_frames - (uint64_t)g.n_slots * period,
          "hw_ptr trails appl_frames by exactly the ring depth (honest, advancing)");

    free(s);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_reader_returns_after_free_run_resyncs(void) {
    // B1 regression (Blocker) part (b): after a stretch of readerless free-run
    // (writer advanced read_seq on the absent reader's behalf), a reader that
    // attaches must resync cleanly to the writer tip — read_seq = write_seq,
    // occupancy collapses to 0 — with no lost-lap corruption, and normal
    // publish/consume ping-pong resumes.
    char path[256];
    tmp_path(path, sizeof(path), "resyncafterfreerun");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));

    // Free-run past the ring depth with NO reader: write_seq and read_seq both
    // climb (drop-oldest), so occupancy stays full but bounded.
    for (int i = 0; i < 20; i++) (void)jts_ring_writer_publish(&w, s);
    jts_ring_header_t *h = (jts_ring_header_t *)w.base;
    uint64_t wseq_before_attach = atomic_load_explicit(&h->write_seq, memory_order_acquire);
    uint64_t rseq_before_attach = atomic_load_explicit(&h->read_seq, memory_order_acquire);
    CHECK(wseq_before_attach - rseq_before_attach == (uint64_t)g.n_slots,
          "occupancy bounded at n_slots after free-run (read_seq advanced)");
    CHECK(rseq_before_attach > 0, "read_seq advanced on absent reader's behalf");

    // Reader attaches now: mirrors the Rust reader's attach resync
    // (read_seq = write_seq, dropping the stale in-ring laps).
    test_reader_t r;
    reader_attach(&r, &w);
    CHECK(r.read_seq == wseq_before_attach, "reader resynced read_seq to write tip");
    CHECK(jts_ring_writer_occupancy_slots(&w) == 0, "occupancy collapses to 0 on attach");
    // Empty read right after attach (nothing new published yet).
    CHECK(reader_consume(&r, out) == 0, "empty read immediately after resync");

    // Distinct payload, publish one, reader must read exactly it — no stale lap.
    for (size_t i = 0; i < n; i++) s[i] = (int16_t)(i + 100);
    CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK,
          "publish OK to a live reader after free-run");
    CHECK(reader_consume(&r, out) == 1, "reader consumes the post-resync slot");
    CHECK(memcmp(out, s, n * sizeof(int16_t)) == 0, "post-resync payload is intact");

    free(s);
    free(out);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_attach_second_writer_bumps_epoch(void) {
    char path[256];
    tmp_path(path, sizeof(path), "epoch");
    jts_ring_geometry_t g = proto_geometry();
    jts_ring_writer_t w1;
    CHECK(jts_ring_writer_open(path, &g, &w1) == 0, "writer 1 open");
    jts_ring_header_t *h = (jts_ring_header_t *)w1.base;
    uint64_t e1 = atomic_load_explicit(&h->writer_epoch, memory_order_acquire);
    // A second writer attaching to the SAME file bumps the epoch again.
    jts_ring_writer_t w2;
    CHECK(jts_ring_writer_open(path, &g, &w2) == 0, "writer 2 attach");
    uint64_t e2 = atomic_load_explicit(&h->writer_epoch, memory_order_acquire);
    CHECK(e2 > e1, "epoch bumped on second writer attach");
    // write_seq is file-lifetime monotonic: the second writer continues from it.
    CHECK(w2.write_seq == w1.write_seq, "second writer continues from stored write_seq");
    jts_ring_writer_close(&w2);
    jts_ring_writer_close(&w1);
    unlink(path);
}

static void test_geometry_mismatch_is_fatal(void) {
    char path[256];
    tmp_path(path, sizeof(path), "mismatch");
    jts_ring_geometry_t g = proto_geometry();
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open 128");
    // Second opener expecting period_frames=256 -> fatal mismatch.
    jts_ring_geometry_t wrong = g;
    wrong.period_frames = 256;
    jts_ring_writer_t w2;
    int rc = jts_ring_writer_open(path, &wrong, &w2);
    CHECK(rc < 0, "geometry mismatch is fatal (rc < 0)");
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_writer_creates_missing_parent_dir(void) {
    // B1 regression: the writer must mkdir -p its parent before O_EXCL create.
    // On a fresh box (or after disarm.sh's `rm -rf /dev/shm/jts-ring`) the
    // directory does not exist; without ensure_parent_dir the create fails
    // ENOENT and arm.sh's aplay probe (step 3) dies before outputd ever runs.
    char dir[256];
    char path[320];
    snprintf(dir, sizeof(dir), "/tmp/jts-ring-ctest-%d-mkparent", (int)getpid());
    // Best-effort clean any prior run's tree.
    char rm[512];
    snprintf(rm, sizeof(rm), "rm -rf '%s'", dir);
    (void)!system(rm);
    // Two missing levels below /tmp so the mkdir -p walk is exercised.
    snprintf(path, sizeof(path), "%s/nested/content.ring", dir);

    jts_ring_geometry_t g = proto_geometry();
    jts_ring_writer_t w;
    int rc = jts_ring_writer_open(path, &g, &w);
    CHECK(rc == 0, "writer_open creates the missing parent dir (no ENOENT)");
    if (rc == 0) jts_ring_writer_close(&w);
    (void)!system(rm);
}

static void test_can_accept_semantics(void) {
    // N1 regression: jts_ring_writer_can_accept must be TRUE when space exists,
    // FALSE when full WITH a live reader (so the ioplug withholds POLLOUT and
    // re-polls instead of busy-spinning), and TRUE when full with NO live reader
    // (free-run drop is "writable"). This is the honest poll the ioplug reports.
    char path[256];
    tmp_path(path, sizeof(path), "canaccept");
    jts_ring_geometry_t g = proto_geometry();
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));

    // Empty ring, no reader -> space available -> can accept.
    CHECK(jts_ring_writer_can_accept(&w) == 1, "empty ring accepts");

    test_reader_t r;
    reader_attach(&r, &w); // live reader, fresh heartbeat
    CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish 0");
    CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish 1");
    CHECK(jts_ring_writer_occupancy_slots(&w) == 2, "full");
    // Full WITH a live reader that stamped a fresh heartbeat -> not writable.
    CHECK(jts_ring_writer_can_accept(&w) == 0, "full+live-reader does NOT accept");

    // Now make the reader look dead (stale heartbeat) -> free-run-drop path ->
    // reports writable again.
    jts_ring_header_t *h = (jts_ring_header_t *)w.base;
    atomic_store_explicit(&h->reader_heartbeat_ns, 1, memory_order_relaxed);
    CHECK(jts_ring_writer_can_accept(&w) == 1, "full+dead-reader accepts (free-run drop)");

    free(s);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_deep_ring_16_slots(void) {
    // 2026-07-02: the ceiling was raised 4 -> 16 so CamillaDSP's playback
    // BufferManager gets an ALSA buffer (n_slots * period_frames = 16*128 =
    // 2048 frames) that clears its negotiated buffer and target_level. Prove the
    // core is correct at the new ceiling: geometry validates, file size is right,
    // and publish/consume ping-pongs cleanly all the way to full and back.
    char path[256];
    tmp_path(path, sizeof(path), "deep16");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 16;
    const char *reason = NULL;
    CHECK(jts_ring_geometry_validate(&g, &reason) == 0, "16 slots is valid");
    CHECK(jts_ring_file_size(&g) == 128 + 16 * 512, "16-slot file size");

    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open 16 slots");
    test_reader_t r;
    reader_attach(&r, &w);

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));

    // Fill exactly to the brim (16 slots), occupancy tracks each publish.
    for (uint32_t i = 0; i < 16; i++) {
        CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish to brim");
        CHECK(jts_ring_writer_occupancy_slots(&w) == (uint64_t)(i + 1), "occupancy climbs");
    }
    // Full with a live reader -> not writable (honest poll withholds POLLOUT).
    CHECK(jts_ring_writer_can_accept(&w) == 0, "16/16 full+live does NOT accept");
    // Drain one, publish one — ping-pong holds at depth.
    CHECK(reader_consume(&r, out) == 1, "drain one from a deep ring");
    CHECK(jts_ring_writer_occupancy_slots(&w) == 15, "occupancy drops after drain");
    CHECK(jts_ring_writer_can_accept(&w) == 1, "space freed -> writable");
    CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish after drain");
    CHECK(jts_ring_writer_occupancy_slots(&w) == 16, "back to full");

    free(s);
    free(out);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_occupancy_tracks_reader_drain(void) {
    // The ioplug `pointer` callback derives the honest hardware pointer as
    //   hw_ptr = appl_frames - in_flight
    //   in_flight = occupancy_slots * period_frames + stage_frames
    // so ALSA's avail/delay reflect the READER's real drain progress, not
    // "everything accepted is already played". This test pins the core invariant
    // the pointer depends on: occupancy_slots == write_seq - read_seq falls by
    // exactly one per reader consume, so appl_frames - in_flight advances by one
    // period each time the reader drains a slot (a monotonic, non-stalling
    // hardware pointer). Regression for the accept-tracking pointer bug that made
    // camilla see delay ~= 0 and flap between stalled/resumed.
    char path[256];
    tmp_path(path, sizeof(path), "drainptr");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    test_reader_t r;
    reader_attach(&r, &w);

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));

    const uint32_t period = g.period_frames;
    uint64_t appl_frames = 0; // mirrors the ioplug's accept counter

    // Accept (publish) 3 slots. in_flight == 3*period; hw_ptr lags by that much.
    for (int i = 0; i < 3; i++) {
        CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish");
        appl_frames += period;
    }
    uint64_t in_flight = jts_ring_writer_occupancy_slots(&w) * (uint64_t)period;
    CHECK(in_flight == 3ull * period, "in_flight == 3 periods before any drain");
    uint64_t hw_ptr = appl_frames - in_flight;
    CHECK(hw_ptr == 0, "hw_ptr still 0 (reader has drained nothing)");

    // Reader drains one slot: hw_ptr must advance by exactly one period.
    CHECK(reader_consume(&r, out) == 1, "drain one");
    in_flight = jts_ring_writer_occupancy_slots(&w) * (uint64_t)period;
    CHECK(in_flight == 2ull * period, "in_flight fell one period");
    hw_ptr = appl_frames - in_flight;
    CHECK(hw_ptr == (uint64_t)period, "hw_ptr advanced one period on drain");

    // Drain the rest: hw_ptr catches up to appl_frames (device fully drained).
    CHECK(reader_consume(&r, out) == 1, "drain two");
    CHECK(reader_consume(&r, out) == 1, "drain three");
    in_flight = jts_ring_writer_occupancy_slots(&w) * (uint64_t)period;
    CHECK(in_flight == 0, "ring empty -> in_flight 0");
    hw_ptr = appl_frames - in_flight;
    CHECK(hw_ptr == appl_frames, "hw_ptr caught appl_frames when fully drained");

    free(s);
    free(out);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_drain_flush_partial_slot(void) {
    // S1 regression: the ioplug's `.drain` callback publishes the partial
    // staged slot (zero-padding the remainder) so drain can reach an empty ring
    // — without it, a partially-staged slot leaves `delay` pinned above 0 and
    // ALSA's drain loop HANGS. This test pins the CORE primitive the drain
    // callback relies on: a zero-padded final slot publishes as a normal whole
    // slot, the reader consumes exactly it (real samples then silence pad), and
    // the ring drains to empty (occupancy 0) — the terminal state drain waits
    // for. (The ioplug's flush + bounded-wait loop itself is ALSA-linked and
    // Pi-only; this proves its building blocks on the host.)
    char path[256];
    tmp_path(path, sizeof(path), "drainflush");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    test_reader_t r;
    reader_attach(&r, &w);

    size_t n = w.samples_per_slot;            // frames*channels in a whole slot
    const uint32_t period = g.period_frames;  // frames per slot
    // Simulate a PARTIAL stage: k real frames of nonzero audio, the rest zero-
    // padded exactly as jts_ring_drain does before publishing.
    const uint32_t k = period / 3; // a non-slot-aligned tail (e.g. an odd WAV)
    int16_t *stage = calloc(n, sizeof(int16_t));
    for (uint32_t f = 0; f < k; f++)
        for (uint32_t c = 0; c < g.channels; c++)
            stage[f * g.channels + c] = (int16_t)(f + 1); // nonzero real audio
    // frames [k, period) stay zero (the drain zero-pad).

    CHECK(jts_ring_writer_publish(&w, stage) == JTS_RING_PUBLISH_OK,
          "padded partial slot publishes as a whole slot");
    CHECK(jts_ring_writer_occupancy_slots(&w) == 1, "one slot in flight after flush");

    int16_t *out = calloc(n, sizeof(int16_t));
    CHECK(reader_consume(&r, out) == 1, "reader consumes the flushed slot");
    // Real audio survived; the pad tail is silence.
    int real_ok = 1, pad_ok = 1;
    for (uint32_t f = 0; f < k; f++)
        for (uint32_t c = 0; c < g.channels; c++)
            if (out[f * g.channels + c] != (int16_t)(f + 1)) real_ok = 0;
    for (uint32_t f = k; f < period; f++)
        for (uint32_t c = 0; c < g.channels; c++)
            if (out[f * g.channels + c] != 0) pad_ok = 0;
    CHECK(real_ok, "flushed slot preserves the real (pre-pad) frames");
    CHECK(pad_ok, "flushed slot zero-pads the tail (no stale/garbage frames)");
    // Ring drained to empty — the terminal state jts_ring_drain waits for.
    CHECK(jts_ring_writer_occupancy_slots(&w) == 0, "ring empty after drain flush+consume");

    free(stage);
    free(out);
    jts_ring_writer_close(&w);
    unlink(path);
}

int main(void) {
    test_geometry_math_and_validation();
    test_publish_consume_roundtrip();
    test_ping_pong_bounding();
    test_no_reader_free_run_drop();
    test_no_reader_pointer_keeps_advancing();
    test_reader_returns_after_free_run_resyncs();
    test_attach_second_writer_bumps_epoch();
    test_geometry_mismatch_is_fatal();
    test_writer_creates_missing_parent_dir();
    test_can_accept_semantics();
    test_deep_ring_16_slots();
    test_occupancy_tracks_reader_drain();
    test_drain_flush_partial_slot();

    if (g_failures == 0) {
        printf("ok: all jts_ring core tests passed\n");
        return 0;
    }
    fprintf(stderr, "FAILED: %d check(s)\n", g_failures);
    return 1;
}
