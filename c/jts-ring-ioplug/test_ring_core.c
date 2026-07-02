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

// --- ioplug pointer/avail model (drives the SHARED jts_ring_pointer_report) ---
//
// The B1 wedge lives at the ALSA `avail` gate, NOT in publish: ALSA grants
// `transfer` (publish's only playback caller) at most `avail` frames, so a test
// that calls publish UNCONDITIONALLY can never reproduce the hang. This model
// reproduces the gate faithfully in TWO respects the round-3 review found the
// old model missing:
//
//   1. It calls the SHARED jts_ring_pointer_report (jts_ring_shm.h) — the exact
//      function the plugin returns from `pointer` — rather than hand-copying the
//      dual-mode/clamp logic. A regression in pcm_jts_ring.c's core now fails
//      `make test` (it did not before; the model was a parallel copy).
//
//   2. It models ALSA's REAL hw_ptr inference (snd_pcm_ioplug_hw_ptr_update):
//      `pointer()` returns a value mod buffer_size; ALSA computes
//        delta = (ret >= last_hw) ? ret - last_hw : buffer_size + ret - last_hw
//      and ADDS it to a running (boundary-space) hw_ptr, then stores ret as
//      last_hw. avail is derived from THAT accumulated hw_ptr. This is the layer
//      all three round-4 alias wedges live in: a raw report advance of exactly
//      buffer_size makes ret == last_hw (mod buffer_size) -> delta 0 -> the
//      accumulated hw_ptr falls a lap behind -> avail pins at 0. The old model
//      read avail straight off the raw pre-modulo hw_ptr, so it could not SEE
//      the alias (green tests while hardware hung).
//
// stage_frames is 0 here (the writer core stages nothing; the ioplug does), so
// jts_ring_pointer_report's in_flight is purely occupancy-derived when live and
// 0 when dead.
typedef struct {
    uint64_t appl_frames;            // ALSA appl_ptr mirror
    jts_ring_pointer_state_t ptr;    // the plugin's reported-position state
    uint64_t alsa_hw_ptr;            // ALSA's accumulated (boundary-space) hw_ptr
    uint64_t alsa_last_hw;           // last pointer() return ALSA stored (mod-buffer)
    int alsa_last_hw_valid;          // 0 until the first pointer() read
    uint64_t buffer_size;            // n_slots * period_frames (ALSA buffer)
    uint32_t period;
} ioplug_model_t;

// One `pointer` read + ALSA's hw_ptr inference, EXACTLY as
// snd_pcm_ioplug_hw_ptr_update does it (no BOUNDARY_WA flag, so wrap_point ==
// buffer_size). Calls the shared core for the mod-buffer return value, then
// accumulates the mod-buffer delta into m->alsa_hw_ptr. Returns nothing; read
// m->alsa_hw_ptr / ioplug_model_avail after.
static void ioplug_model_pointer_tick(ioplug_model_t *m, jts_ring_writer_t *w) {
    jts_ring_pointer_inputs_t in = {
        .appl_frames = m->appl_frames,
        .occupancy_slots = jts_ring_writer_occupancy_slots(w),
        .stage_frames = 0, // core-only model stages nothing
        .period_frames = m->period,
        .buffer_size = m->buffer_size,
        .reader_live = jts_ring_writer_reader_is_live(w),
    };
    uint64_t raw = jts_ring_pointer_report(&m->ptr, &in);
    uint64_t ret = raw % m->buffer_size; // what the plugin returns to ALSA
    if (!m->alsa_last_hw_valid) {
        // First read: ALSA seeds last_hw from it (no forward), hw_ptr stays 0.
        m->alsa_last_hw = ret;
        m->alsa_last_hw_valid = 1;
        return;
    }
    uint64_t delta = (ret >= m->alsa_last_hw) ? (ret - m->alsa_last_hw)
                                              : (m->buffer_size + ret - m->alsa_last_hw);
    m->alsa_hw_ptr += delta;
    m->alsa_last_hw = ret;
}

// ALSA hw_avail for a playback ioplug, off ALSA's ACCUMULATED hw_ptr (not the
// raw pre-modulo value): buffer_size - (appl_ptr - hw_ptr). Ticks the pointer
// first so the accumulation reflects this call.
static uint64_t ioplug_model_avail(ioplug_model_t *m, jts_ring_writer_t *w) {
    ioplug_model_pointer_tick(m, w);
    uint64_t used = (m->appl_frames >= m->alsa_hw_ptr) ? (m->appl_frames - m->alsa_hw_ptr)
                                                       : 0; // frames queued, not drained
    return (used <= m->buffer_size) ? (m->buffer_size - used) : 0;
}

// Zero-init an ioplug model for a given geometry.
static ioplug_model_t ioplug_model_new(const jts_ring_geometry_t *g) {
    ioplug_model_t m;
    memset(&m, 0, sizeof(m));
    m.buffer_size = (uint64_t)g->n_slots * g->period_frames;
    m.period = g->period_frames;
    return m;
}

static void test_no_reader_pointer_keeps_advancing(void) {
    // B1 regression (Blocker), GATE-FAITHFUL. The prior version of this test
    // called publish unconditionally and passed while the hardware HUNG — because
    // the wedge is at ALSA's `avail` gate, upstream of publish. This version
    // models that gate: it computes `avail` from the pointer path exactly as
    // pcm_jts_ring.c does, drives publish ONLY when a whole period of avail
    // exists (the ALSA discipline), and asserts the avail/pointer path opens on
    // a readerless full ring WITHOUT any special publish call — i.e. the dual-
    // mode pointer (in_flight = 0 when the reader is dead) is what unwedges it.
    //
    // Pre-fix (honest in_flight even with no reader): avail would pin at 0 the
    // moment occupancy hit n_slots and this loop would spin forever making no
    // progress. With the dual-mode fix, avail stays ~= buffer, publish keeps
    // being called, and its drop-oldest branch bounds occupancy.
    char path[256];
    tmp_path(path, sizeof(path), "noreaderptr");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    const uint32_t period = g.period_frames;

    ioplug_model_t m = ioplug_model_new(&g);

    // Fill the ring readerless via the gate FIRST, with NO publish helper — this
    // is the exact ALSA wait loop. Assert that once the ring is full the avail
    // path still reports >= one period open (the dual-mode pointer), so the loop
    // below never stalls. If avail ever hit 0 here (the pre-fix wedge) the assert
    // would fire immediately on a full readerless ring.
    int publishes = 0;
    uint64_t prev_hw_ptr = 0;
    for (int tick = 0; tick < 200; tick++) {
        uint64_t avail = ioplug_model_avail(&m, &w);
        // On a readerless ring the dual-mode pointer MUST keep avail open. This
        // is the core B1 assertion: the gate never wedges without any publish.
        CHECK(avail >= period,
              "readerless avail stays >= one period (dual-mode pointer, no wedge)");
        uint64_t hw = m.alsa_hw_ptr; // ALSA's accumulated hw_ptr after this read
        CHECK(hw >= prev_hw_ptr, "hw_ptr monotonic non-decreasing (never back-jumps)");
        prev_hw_ptr = hw;
        // ALSA would now transfer up to `avail`; the ioplug stages+publishes whole
        // slots. Model one period per tick.
        jts_ring_publish_result_t pr = jts_ring_writer_publish(&w, s);
        CHECK(pr == JTS_RING_PUBLISH_OK || pr == JTS_RING_PUBLISH_DROPPED,
              "publish returns (never hangs) with no reader");
        m.appl_frames += period;
        publishes++;
        // Occupancy stays bounded at the ring depth — never pinned to the buffer
        // with read_seq stuck at 0.
        CHECK(jts_ring_writer_occupancy_slots(&w) <= (uint64_t)g.n_slots,
              "occupancy bounded by ring depth (not pinned full-forever)");
    }
    CHECK(publishes == 200, "gate stayed open for every tick (no wedge)");
    CHECK(jts_ring_writer_occupancy_slots(&w) == (uint64_t)g.n_slots,
          "ring full at steady free-run");

    free(s);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_gate_faithful_dead_ring_opens_without_publish(void) {
    // B1 regression, the SHARPEST form the mandate asks for: fill the ring
    // readerless (occupancy == n_slots), then WITHOUT calling publish at all,
    // read avail from the pointer path repeatedly and assert it is non-zero and
    // stays non-zero across ticks. This isolates the fix to the pointer/avail
    // path itself: on a full readerless ring the gate must be OPEN with zero
    // writer activity, because the dual-mode pointer discounts the (unreadable)
    // published slots to 0 in-flight. Pre-fix this avail would be exactly 0.
    char path[256];
    tmp_path(path, sizeof(path), "gatefaithful");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    const uint32_t period = g.period_frames;

    // Fill to the brim via the drop-oldest free-run (no reader). appl_frames
    // tracks each accepted period so the model's used = appl - hw is correct.
    // The pointer is ticked each publish so ALSA's accumulated hw_ptr keeps up
    // with appl exactly as it would in live playback (the dead-mode discount
    // makes honest hw_ptr == appl each tick, and the clamp lets it track since
    // each step is one period < buffer_size).
    ioplug_model_t m = ioplug_model_new(&g);
    for (uint32_t i = 0; i < g.n_slots + 2; i++) { // +2 = force the full state
        (void)jts_ring_writer_publish(&w, s);
        m.appl_frames += period;
        (void)ioplug_model_avail(&m, &w); // tick the pointer / accumulate hw_ptr
    }
    CHECK(jts_ring_writer_occupancy_slots(&w) == (uint64_t)g.n_slots,
          "ring full (occupancy == n_slots) before the no-publish avail probe");
    CHECK(!jts_ring_writer_reader_is_live(&w), "reader is dead (never attached)");

    // Now probe avail across several ticks with NO further publish. The gate must
    // be open AND STAY open every time — this is the SHARPEST alias assertion:
    // with appl_frames frozen and the reader dead, honest hw_ptr == appl is also
    // frozen, so the pointer returns the SAME value each tick. ALSA's mod-buffer
    // delta inference therefore adds 0 each tick (ret == last_hw). The gate must
    // remain open at that steady value — pre-round-4 the dead-mode discount could
    // have delivered ONE full-buffer jump on the flip that aliased to delta 0 and
    // parked avail at 0 forever. Here we assert avail is a stable positive value
    // (>= a period of headroom) that never decays across the probe.
    uint64_t first_avail = ioplug_model_avail(&m, &w);
    CHECK(first_avail >= period,
          "full readerless ring: avail is OPEN (>= one period) without any publish");
    for (int tick = 0; tick < 8; tick++) {
        uint64_t avail = ioplug_model_avail(&m, &w);
        CHECK(avail > 0, "readerless avail stays OPEN across probe ticks (no alias to 0)");
        CHECK(avail == first_avail,
              "readerless avail is STABLE with no publish (frozen appl -> delta 0)");
    }

    free(s);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_reader_attach_midplay_hw_ptr_monotonic(void) {
    // B1 transition edge (round-4 alias trigger (c): dead->live recovery). A
    // reader that appears MID-WRITE must not make ALSA's hw_ptr jump backward
    // (ALSA requires it monotonic) AND must not alias a full-buffer step to a
    // zero delta (which would pin avail at 0 forever). While dead, the pointer
    // runs hw_ptr near appl (in_flight = 0). On attach the reader resyncs
    // read_seq = write_seq (occupancy -> 0), so honest hw_ptr == appl
    // (convergent). Then, before the reader consumes, occupancy re-grows and
    // honest hw_ptr would step BACK; the reported-position clamp holds it. This
    // test drives the shared pointer core through ALSA's real accumulated-hw_ptr
    // inference and asserts (i) ALSA's hw_ptr never regresses, (ii) avail never
    // pins at 0 through the transition, and (iii) after full drain the gate is
    // healthy (avail == buffer, ring empty).
    char path[256];
    tmp_path(path, sizeof(path), "attachmidplay");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));
    const uint32_t period = g.period_frames;

    ioplug_model_t m = ioplug_model_new(&g);

    // Phase 1: readerless free-run for a while. ALSA's hw_ptr tracks appl (dead
    // -> in_flight 0), one period per tick — every step visible (< buffer), so
    // no alias, and monotonic.
    uint64_t prev = 0;
    for (int i = 0; i < 12; i++) {
        (void)jts_ring_writer_publish(&w, s);
        m.appl_frames += period;
        uint64_t avail = ioplug_model_avail(&m, &w); // ticks + accumulates hw_ptr
        CHECK(avail >= period, "phase1 avail open (dead reader, no wedge)");
        CHECK(m.alsa_hw_ptr >= prev, "phase1 hw_ptr monotonic (dead reader)");
        prev = m.alsa_hw_ptr;
    }

    // Phase 2: reader attaches mid-play (resyncs read_seq = write_seq).
    test_reader_t r;
    reader_attach(&r, &w);
    CHECK(jts_ring_writer_occupancy_slots(&w) == 0, "occupancy collapses to 0 on attach");
    CHECK(jts_ring_writer_reader_is_live(&w), "reader now live");
    // First live tick: occupancy 0 -> honest hw_ptr == appl. Convergent, no
    // jump, avail stays open (the dead->live edge does NOT alias).
    uint64_t avail_first_live = ioplug_model_avail(&m, &w);
    CHECK(avail_first_live > 0, "dead->live transition keeps avail open (no alias)");
    CHECK(m.alsa_hw_ptr >= prev, "dead->live transition is monotonic (no back-jump)");
    prev = m.alsa_hw_ptr;

    // Phase 3: writer publishes ahead of the reader (occupancy grows); honest
    // hw_ptr WOULD step back one period per publish, but the clamp holds the
    // reported position at its floor, so ALSA's hw_ptr never regresses. As the
    // ring genuinely fills with a LIVE lagging reader, avail correctly shrinks
    // toward 0 (a full ring with a live reader IS not-writable — that is the
    // honest back-pressure, not a wedge). We assert only the monotonicity here;
    // the no-wedge (avail reopens) property is the dedicated alias tests' job.
    for (int i = 0; i < 6; i++) {
        (void)jts_ring_writer_publish(&w, s); // occupancy climbs (reader idle)
        m.appl_frames += period;
        (void)ioplug_model_avail(&m, &w);
        CHECK(m.alsa_hw_ptr >= prev, "phase3 hw_ptr never regresses while reader lags (clamped)");
        prev = m.alsa_hw_ptr;
    }
    // Reader now drains everything; hw_ptr must climb, monotonic, and the gate
    // must reopen (avail grows back).
    while (jts_ring_writer_occupancy_slots(&w) > 0) {
        CHECK(reader_consume(&r, out) == 1, "reader drains a slot");
        (void)ioplug_model_avail(&m, &w);
        CHECK(m.alsa_hw_ptr >= prev, "drain phase hw_ptr monotonic non-decreasing");
        prev = m.alsa_hw_ptr;
    }
    // Fully drained with a live reader + a settle tick for the clamp to finish
    // catching up: the ring is empty and the gate is wide open again. (avail
    // settles to buffer minus ALSA's one-period first-read seed lag — a benign
    // constant of ALSA's own hw_ptr model, not a wedge — so we assert avail is
    // near-full and STABLE rather than an exact == buffer.)
    (void)ioplug_model_avail(&m, &w);
    uint64_t avail_a = ioplug_model_avail(&m, &w);
    uint64_t avail_b = ioplug_model_avail(&m, &w);
    CHECK(jts_ring_writer_occupancy_slots(&w) == 0, "ring empty after full drain");
    CHECK(avail_a >= m.buffer_size - period,
          "fully drained + settled: avail is near-full (honest accounting restored)");
    CHECK(avail_b == avail_a, "drained avail is STABLE across ticks (no residual alias)");

    free(s);
    free(out);
    jts_ring_writer_close(&w);
    unlink(path);
}

// --- Round-4 mod-buffer full-lap ALIAS regressions (the round-3 review Blocker)
//
// Each of the three below constructs the exact state where the HONEST reported
// position would advance by ~one full buffer between two consecutive pointer
// reads. Returned mod buffer_size, that aliases to a ZERO (or backward) delta in
// ALSA's snd_pcm_ioplug_hw_ptr_update, pinning avail at 0 permanently. The
// reported-position clamp (jts_ring_pointer_report) spreads the catch-up over
// several sub-buffer ticks so ALSA always sees a positive delta and avail stays
// open. Each test also computes what an UNCLAMPED honest pointer WOULD have
// returned at the alias step and asserts it aliases — the not-a-tautology proof
// that the clamp is what keeps these green (remove the clamp and these fail).

// Helper: what ALSA's mod-buffer delta inference yields for two raw reported
// positions (verbatim snd_pcm_ioplug_hw_ptr_update, no BOUNDARY_WA).
static uint64_t alsa_delta(uint64_t prev_raw, uint64_t cur_raw, uint64_t buffer) {
    uint64_t prev = prev_raw % buffer;
    uint64_t cur = cur_raw % buffer;
    return (cur >= prev) ? (cur - prev) : (buffer + cur - prev);
}

static void test_alias_live_reader_drain_gap(void) {
    // TRIGGER (a): a LIVE reader drains a full ring during an app-side gap >= one
    // buffer duration. Before the gap: occupancy == n_slots, honest hw_ptr lags
    // appl by a full buffer. During the gap the reader drains everything
    // (occupancy -> 0), so the NEXT pointer read jumps honest hw_ptr forward by
    // exactly buffer_size — the alias. The clamp must spread it so avail reopens
    // smoothly instead of pinning at 0.
    char path[256];
    tmp_path(path, sizeof(path), "aliasdraingap");
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
    ioplug_model_t m = ioplug_model_new(&g);

    // Fill the ring to the brim with a live reader that does NOT yet drain.
    for (uint32_t i = 0; i < g.n_slots; i++) {
        CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish to brim");
        m.appl_frames += period;
        (void)ioplug_model_avail(&m, &w);
    }
    CHECK(jts_ring_writer_occupancy_slots(&w) == (uint64_t)g.n_slots, "ring full");
    uint64_t raw_before = m.ptr.last_reported;      // honest hw_ptr lags appl by buffer
    uint64_t hw_before = m.alsa_hw_ptr;

    // The app-side GAP: no pointer read happens while the reader drains the WHOLE
    // ring. (This is the "app is outside a PCM call" window the review named.)
    while (jts_ring_writer_occupancy_slots(&w) > 0) {
        CHECK(reader_consume(&r, out) == 1, "reader drains during app gap");
    }
    CHECK(jts_ring_writer_occupancy_slots(&w) == 0, "reader emptied the ring in the gap");

    // Not-a-tautology: an UNCLAMPED honest pointer would now report appl (occ 0),
    // a raw jump of exactly buffer_size from raw_before -> aliases to delta 0.
    uint64_t honest_unclamped = m.appl_frames; // occ 0, stage 0, reader live
    CHECK(honest_unclamped - raw_before == m.buffer_size,
          "unclamped honest jump is exactly one buffer (the alias precondition)");
    CHECK(alsa_delta(raw_before, honest_unclamped, m.buffer_size) == 0,
          "unclamped: full-buffer jump aliases to ZERO delta (would wedge)");

    // The CLAMPED path: the next pointer read advances ALSA's hw_ptr by a visible
    // (sub-buffer) delta, not 0. Drive several ticks with no publish; avail must
    // climb back open and hw_ptr must strictly advance until it catches appl.
    int saw_progress = 0;
    for (int tick = 0; tick < (int)g.n_slots + 2; tick++) {
        uint64_t hw_prev = m.alsa_hw_ptr;
        uint64_t avail = ioplug_model_avail(&m, &w);
        CHECK(avail > 0, "clamped: avail reopens after the full-drain gap (no alias wedge)");
        CHECK(m.alsa_hw_ptr >= hw_prev, "clamped: hw_ptr monotonic across catch-up");
        if (m.alsa_hw_ptr > hw_before) saw_progress = 1;
    }
    CHECK(saw_progress, "clamped: hw_ptr made real forward progress (unwedged)");
    // Fully caught up: one buffer of drain is now reflected.
    CHECK(m.alsa_hw_ptr - hw_before == m.buffer_size,
          "clamped: the full buffer of drain is eventually reflected (spread, not lost)");

    free(s);
    free(out);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_alias_dead_flip_at_full_ring(void) {
    // TRIGGER (b): the reader dies MID-PLAY at a full ring (occupancy == n_slots)
    // — the operational outputd-restart case. While live+full, honest hw_ptr lags
    // appl by a full buffer (in_flight = n_slots*period). The instant the reader
    // heartbeat goes stale, the dual-mode discount flips in_flight to 0, so honest
    // hw_ptr jumps forward by exactly buffer_size — the alias. free-run never even
    // ran yet (drop_no_reader == 0 at the flip), so this is purely the pointer's
    // problem. The clamp must keep avail open so transfer resumes and free-run can
    // then bound the ring.
    char path[256];
    tmp_path(path, sizeof(path), "aliasdeadflip");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    test_reader_t r;
    reader_attach(&r, &w);

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    const uint32_t period = g.period_frames;
    ioplug_model_t m = ioplug_model_new(&g);

    // Fill to full with a live reader (occupancy == n_slots), ticking the pointer.
    for (uint32_t i = 0; i < g.n_slots; i++) {
        CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish to brim");
        m.appl_frames += period;
        (void)ioplug_model_avail(&m, &w);
    }
    CHECK(jts_ring_writer_occupancy_slots(&w) == (uint64_t)g.n_slots, "ring full+live");
    CHECK(w.drop_no_reader == 0, "no free-run drops yet (pure pointer case)");
    uint64_t raw_before = m.ptr.last_reported;
    uint64_t hw_before = m.alsa_hw_ptr;

    // The reader DIES: stale its heartbeat so reader_is_live flips to false. No
    // read_seq change, so occupancy is still n_slots — the discount is the only
    // thing that moves.
    jts_ring_header_t *h = (jts_ring_header_t *)w.base;
    atomic_store_explicit(&h->reader_heartbeat_ns, 1, memory_order_relaxed);
    CHECK(!jts_ring_writer_reader_is_live(&w), "reader now dead (stale heartbeat)");

    // Not-a-tautology: unclamped honest hw_ptr with the dead discount == appl
    // (in_flight 0), a raw jump of exactly buffer_size -> aliases to delta 0.
    uint64_t honest_unclamped = m.appl_frames;
    CHECK(honest_unclamped - raw_before == m.buffer_size,
          "unclamped dead-flip jump is exactly one buffer (alias precondition)");
    CHECK(alsa_delta(raw_before, honest_unclamped, m.buffer_size) == 0,
          "unclamped: dead-flip full-buffer jump aliases to ZERO delta (would wedge)");

    // Clamped: transfer resumes (avail > 0). Now model ALSA's transfer -> publish
    // loop: each tick, if avail >= a period, publish one slot (free-run drop). The
    // gate must stay open every tick and free-run must bound the ring.
    int publishes = 0;
    for (int tick = 0; tick < 40; tick++) {
        uint64_t avail = ioplug_model_avail(&m, &w);
        CHECK(avail > 0, "clamped: dead-flip keeps avail open (no alias wedge)");
        if (avail >= period) {
            jts_ring_publish_result_t pr = jts_ring_writer_publish(&w, s);
            CHECK(pr == JTS_RING_PUBLISH_OK || pr == JTS_RING_PUBLISH_DROPPED,
                  "publish returns (free-run) after dead flip");
            m.appl_frames += period;
            publishes++;
        }
        CHECK(jts_ring_writer_occupancy_slots(&w) <= (uint64_t)g.n_slots,
              "occupancy bounded by free-run after dead flip");
    }
    CHECK(publishes > 0, "clamped: transfer/publish resumed after the reader died");
    CHECK(w.drop_no_reader > 0, "free-run reclaim engaged once the gate reopened");
    CHECK(m.alsa_hw_ptr > hw_before, "clamped: hw_ptr advanced past the pre-death lag");

    free(s);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_alias_dead_to_live_recovery(void) {
    // TRIGGER (c): dead->live recovery. A readerless free-run stream (dead-mode,
    // hw_ptr near appl) then a reader attaches. The attach resyncs occupancy -> 0
    // (honest hw_ptr == appl, convergent) but the reader has NOT drained yet, so
    // the next few ticks the writer keeps publishing and occupancy grows while the
    // reader is momentarily idle — honest hw_ptr would step back a full buffer's
    // worth over the transition. The clamp keeps hw_ptr monotonic and avail open
    // through the whole recovery, and once the reader paces normally the gate
    // tracks real drain.
    char path[256];
    tmp_path(path, sizeof(path), "aliasdeadtolive");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));
    const uint32_t period = g.period_frames;
    ioplug_model_t m = ioplug_model_new(&g);

    // Dead free-run phase: fill+overflow with no reader. avail stays open the
    // whole time (dead-mode discount), hw_ptr tracks appl.
    for (int i = 0; i < 12; i++) {
        uint64_t avail = ioplug_model_avail(&m, &w);
        CHECK(avail >= period, "dead free-run avail open");
        (void)jts_ring_writer_publish(&w, s);
        m.appl_frames += period;
    }
    uint64_t hw_before = m.alsa_hw_ptr;

    // Reader attaches (resync occupancy -> 0) then stays idle for a stretch while
    // the writer keeps publishing (the recovery window). avail must never pin at 0
    // and hw_ptr must never regress.
    test_reader_t r;
    reader_attach(&r, &w);
    CHECK(jts_ring_writer_reader_is_live(&w), "reader live after attach");
    uint64_t prev = m.alsa_hw_ptr;
    for (int i = 0; i < (int)g.n_slots; i++) {
        (void)jts_ring_writer_publish(&w, s); // reader idle: occupancy climbs
        m.appl_frames += period;
        (void)ioplug_model_avail(&m, &w);
        // The reader is idle so the ring legitimately fills and avail correctly
        // shrinks toward 0 (honest back-pressure); the invariant that matters
        // across the transition is that hw_ptr never REGRESSES (no back-jump from
        // the discount flip). The no-wedge (avail reopens) property is proven by
        // the paced phase below.
        CHECK(m.alsa_hw_ptr >= prev, "recovery: hw_ptr never regresses");
        prev = m.alsa_hw_ptr;
    }

    // Reader now paces: drain one, publish one, repeatedly. The gate tracks real
    // drain, avail stays open, hw_ptr climbs monotonically.
    for (int i = 0; i < 12; i++) {
        if (jts_ring_writer_occupancy_slots(&w) > 0)
            CHECK(reader_consume(&r, out) == 1, "reader paces a drain");
        uint64_t avail = ioplug_model_avail(&m, &w);
        CHECK(avail > 0, "paced recovery: avail open");
        CHECK(m.alsa_hw_ptr >= prev, "paced recovery: hw_ptr monotonic");
        prev = m.alsa_hw_ptr;
        (void)jts_ring_writer_publish(&w, s);
        m.appl_frames += period;
    }
    CHECK(m.alsa_hw_ptr > hw_before, "recovery made real forward progress (never wedged)");

    free(s);
    free(out);
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
    test_gate_faithful_dead_ring_opens_without_publish();
    test_reader_attach_midplay_hw_ptr_monotonic();
    test_alias_live_reader_drain_gap();
    test_alias_dead_flip_at_full_ring();
    test_alias_dead_to_live_recovery();
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
