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
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdatomic.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static int g_failures = 0;
static char g_owned_dir[256];
static char g_test_paths[128][512];
static size_t g_test_path_count = 0;

typedef struct {
    uint64_t dev;
    uint64_t ino;
    int64_t size;
} test_inode_observation_t;

#define CHECK(cond, msg)                                                        \
    do {                                                                        \
        if (!(cond)) {                                                          \
            fprintf(stderr, "FAIL: %s (%s:%d)\n", msg, __FILE__, __LINE__);     \
            g_failures++;                                                       \
        }                                                                       \
    } while (0)

static int read_observation(int fd, test_inode_observation_t *observation) {
    // The timeout is only a deadlock guard: ordering comes from the production
    // hook writing after fstat observes the zero-size fd, never from elapsed time.
    struct pollfd pfd = {.fd = fd, .events = POLLIN};
    int poll_rc;
    do {
        poll_rc = poll(&pfd, 1, 2000);
    } while (poll_rc < 0 && errno == EINTR);
    if (poll_rc <= 0 || !(pfd.revents & POLLIN)) return -1;

    uint8_t *cursor = (uint8_t *)observation;
    size_t remaining = sizeof(*observation);
    while (remaining > 0) {
        ssize_t n = read(fd, cursor, remaining);
        if (n > 0) {
            cursor += (size_t)n;
            remaining -= (size_t)n;
        } else if (n < 0 && errno == EINTR) {
            continue;
        } else {
            return -1;
        }
    }
    return 0;
}

static int read_bytes_bounded(int fd, void *out, size_t len) {
    uint8_t *cursor = (uint8_t *)out;
    size_t remaining = len;
    while (remaining > 0) {
        struct pollfd pfd = {.fd = fd, .events = POLLIN};
        int poll_rc;
        do {
            poll_rc = poll(&pfd, 1, 2000);
        } while (poll_rc < 0 && errno == EINTR);
        if (poll_rc <= 0 || !(pfd.revents & POLLIN)) return -1;
        ssize_t n = read(fd, cursor, remaining);
        if (n > 0) {
            cursor += (size_t)n;
            remaining -= (size_t)n;
        } else if (n < 0 && errno == EINTR) {
            continue;
        } else {
            return -1;
        }
    }
    return 0;
}

static void report_fd_identity(int report_fd, int ring_fd) {
    struct stat st;
    if (fstat(ring_fd, &st) < 0) return;
    test_inode_observation_t observed = {
        .dev = (uint64_t)st.st_dev,
        .ino = (uint64_t)st.st_ino,
        .size = (int64_t)st.st_size,
    };
    (void)write(report_fd, &observed, sizeof(observed));
}

static void cleanup_owned_test_locks(void) {
    DIR *dir = opendir(g_owned_dir);
    if (dir == NULL) return;
    struct dirent *entry;
    while ((entry = readdir(dir)) != NULL) {
        if (strstr(entry->d_name, JTS_RING_OPEN_LOCK_SUFFIX) == NULL) continue;
        char path[512];
        snprintf(path, sizeof(path), "%s/%s", g_owned_dir, entry->d_name);
        (void)unlink(path);
    }
    closedir(dir);
}

static void remember_test_path(const char *path) {
    if (g_test_path_count >= sizeof(g_test_paths) / sizeof(g_test_paths[0])) return;
    snprintf(g_test_paths[g_test_path_count], sizeof(g_test_paths[0]), "%s", path);
    g_test_path_count++;
}

static void cleanup_all_test_paths(void) {
    for (size_t i = 0; i < g_test_path_count; i++) {
        (void)unlink(g_test_paths[i]);
        char lock_path[576];
        snprintf(lock_path, sizeof(lock_path), "%s%s", g_test_paths[i],
                 JTS_RING_OPEN_LOCK_SUFFIX);
        (void)unlink(lock_path);
    }
}

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

// Build a unique /tmp path outside the test-owned root.
static void tmp_path(char *buf, size_t buflen, const char *tag) {
    snprintf(buf, buflen, "/tmp/jts-ring-ctest-%d-%s.ring", (int)getpid(), tag);
    unlink(buf); // fresh
    remember_test_path(buf);
}

static void owned_tmp_path(char *buf, size_t buflen, const char *tag) {
    CHECK(mkdir(g_owned_dir, 0770) == 0 || errno == EEXIST,
          "create portable test-owned ring directory");
    snprintf(buf, buflen, "%s/%s.ring", g_owned_dir, tag);
    unlink(buf);
    remember_test_path(buf);
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
    CHECK(w2.base == NULL, "failed writer attach leaves mapping detached");
    CHECK(w2.fd == -1, "failed writer attach leaves fd detached");

    jts_ring_reader_t r2;
    rc = jts_ring_reader_open(path, &wrong, &r2);
    CHECK(rc < 0, "reader geometry mismatch is fatal (rc < 0)");
    CHECK(r2.base == NULL, "failed reader attach leaves mapping detached");
    CHECK(r2.fd == -1, "failed reader attach leaves fd detached");
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

static void test_reader_creates_missing_parent_then_writer_attaches(void) {
    // Reboot-while-armed contract: Ring A's capture reader may beat fanin's
    // writer to an empty tmpfs. The reader must create both the parent and the
    // byte-identical ring; a later writer must attach and apply only its own
    // epoch/pid/heartbeat stamps.
    char dir[256];
    char path[320];
    snprintf(dir, sizeof(dir), "/tmp/jts-ring-ctest-%d-reader-first", (int)getpid());
    char rm[512];
    snprintf(rm, sizeof(rm), "rm -rf '%s'", dir);
    (void)!system(rm);
    snprintf(path, sizeof(path), "%s/nested/program.ring", dir);

    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_reader_t r;
    int rc = jts_ring_reader_open(path, &g, &r);
    CHECK(rc == 0, "reader-first open creates missing parent and ring");
    if (rc == 0) {
        jts_ring_header_t *h = (jts_ring_header_t *)r.base;
        uint64_t magic_version = atomic_load_explicit(
            (_Atomic uint64_t *)r.base, memory_order_acquire);
        CHECK(r.map_len == jts_ring_file_size(&g), "reader-created map has expected size");
        CHECK((uint32_t)magic_version == JTS_RING_MAGIC,
              "reader creator publishes ring magic");
        CHECK((uint32_t)(magic_version >> 32) == JTS_RING_VERSION,
              "reader creator publishes ring version");
        CHECK(atomic_load_explicit(&h->reader_pid, memory_order_relaxed) ==
                  (uint64_t)getpid(),
              "reader creator stamps reader ownership");

        jts_ring_writer_t w;
        int wrc = jts_ring_writer_open(path, &g, &w);
        CHECK(wrc == 0, "writer attaches to reader-created ring");
        if (wrc == 0) {
            CHECK(w.map_len == r.map_len, "both roles agree on reader-created map size");
            CHECK(w.geometry.n_slots == g.n_slots,
                  "writer sees reader-created geometry");
            CHECK(atomic_load_explicit(&h->writer_epoch, memory_order_acquire) == 1,
                  "writer attach bumps epoch on reader-created ring");
            CHECK(atomic_load_explicit(&h->writer_pid, memory_order_relaxed) ==
                      (uint64_t)getpid(),
                  "writer attaches with its own ownership stamp");
            CHECK(atomic_load_explicit(&h->reader_pid, memory_order_relaxed) ==
                      (uint64_t)getpid(),
                  "writer attach preserves reader ownership stamp");
            jts_ring_writer_close(&w);
        }
        jts_ring_reader_close(&r);
    }
    (void)!system(rm);
}

static void test_magicless_foreign_file_is_rejected_without_reclaim(void) {
    // A full-size file whose creator never published magic is torn. Under /tmp
    // it is foreign, so both roles must fail closed without unlinking it and
    // without leaking their temporary mapping/fd into the public result.
    char path[256];
    tmp_path(path, sizeof(path), "foreign-torn");
    jts_ring_geometry_t g = proto_geometry();
    int fd = open(path, O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0660);
    CHECK(fd >= 0, "create full-size foreign torn file");
    if (fd < 0) return;
    int truncate_rc = ftruncate(fd, (off_t)jts_ring_file_size(&g));
    CHECK(truncate_rc == 0, "size foreign torn file without publishing magic");
    close(fd);
    if (truncate_rc != 0) {
        unlink(path);
        return;
    }

    jts_ring_writer_t w;
    int rc = jts_ring_writer_open(path, &g, &w);
    CHECK(rc == -EINVAL, "writer rejects non-owned magicless file");
    CHECK(w.base == NULL, "rejected writer mapping is detached");
    CHECK(w.fd == -1, "rejected writer fd is detached");
    CHECK(access(path, F_OK) == 0, "writer does not reclaim foreign torn file");

    jts_ring_reader_t r;
    rc = jts_ring_reader_open(path, &g, &r);
    CHECK(rc == -EINVAL, "reader rejects non-owned magicless file");
    CHECK(r.base == NULL, "rejected reader mapping is detached");
    CHECK(r.fd == -1, "rejected reader fd is detached");
    CHECK(access(path, F_OK) == 0, "reader does not reclaim foreign torn file");
    unlink(path);
}

static void test_simultaneous_first_open_waits_for_creator_ftruncate(void) {
    // Exact A -> B -> C regression: A holds the transaction lock after O_EXCL
    // create but before ftruncate; B and C both prove they are blocked on that
    // SAME adjacent lock. Releasing A lets it initialize + verify pathname
    // ownership, after which B and C attach serially. Nobody may classify A's
    // zero-size live inode as torn or replace it.
    char path[320];
    owned_tmp_path(path, sizeof(path), "first-open-race");
    jts_ring_geometry_t g = proto_geometry();

    int creator_ready[2] = {-1, -1};
    int creator_release[2] = {-1, -1};
    int lock_wait_ready[2] = {-1, -1};
    CHECK(pipe(creator_ready) == 0, "create creator-ready barrier pipe");
    CHECK(pipe(creator_release) == 0, "create creator-release barrier pipe");
    CHECK(pipe(lock_wait_ready) == 0, "create lock-wait barrier pipe");
    if (creator_ready[0] < 0 || creator_release[0] < 0 || lock_wait_ready[0] < 0) {
        unlink(path);
        return;
    }

    char creator_ready_fd[32];
    char creator_release_fd[32];
    snprintf(creator_ready_fd, sizeof(creator_ready_fd), "%d", creator_ready[1]);
    snprintf(creator_release_fd, sizeof(creator_release_fd), "%d", creator_release[0]);
    CHECK(setenv("JTS_RING_TEST_CREATOR_READY_FD", creator_ready_fd, 1) == 0,
          "arm creator-ready hook");
    CHECK(setenv("JTS_RING_TEST_CREATOR_RELEASE_FD", creator_release_fd, 1) == 0,
          "arm creator-release hook");
    pid_t creator = fork();
    CHECK(creator >= 0, "fork barrier-held public creator");
    if (creator == 0) {
        close(creator_ready[0]);
        close(creator_release[1]);
        close(lock_wait_ready[0]);
        close(lock_wait_ready[1]);
        jts_ring_writer_t w;
        int rc = jts_ring_writer_open(path, &g, &w);
        if (rc == 0) jts_ring_writer_close(&w);
        _exit(rc == 0 ? 0 : 2);
    }
    unsetenv("JTS_RING_TEST_CREATOR_READY_FD");
    unsetenv("JTS_RING_TEST_CREATOR_RELEASE_FD");
    close(creator_ready[1]);
    close(creator_release[0]);
    if (creator < 0) {
        close(creator_ready[0]);
        close(creator_release[1]);
        close(lock_wait_ready[0]);
        close(lock_wait_ready[1]);
        unlink(path);
        return;
    }

    test_inode_observation_t creator_observation = {0};
    int creator_ready_rc = read_observation(creator_ready[0], &creator_observation);
    CHECK(creator_ready_rc == 0, "creator reports its O_EXCL inode before ftruncate");
    CHECK(creator_observation.size == 0, "O_EXCL creator inode is still zero-size");

    char lock_wait_fd[32];
    snprintf(lock_wait_fd, sizeof(lock_wait_fd), "%d", lock_wait_ready[1]);
    CHECK(setenv("JTS_RING_TEST_LOCK_WAIT_FD", lock_wait_fd, 1) == 0,
          "arm competitor lock-wait hook");

    pid_t attacher_b = fork();
    CHECK(attacher_b >= 0, "fork simultaneous public attacher B");
    if (attacher_b == 0) {
        close(lock_wait_ready[0]);
        close(creator_ready[0]);
        close(creator_release[1]);
        jts_ring_writer_t w;
        int rc = jts_ring_writer_open(path, &g, &w);
        if (rc == 0) jts_ring_writer_close(&w);
        _exit(rc == 0 ? 0 : 3);
    }
    pid_t attacher_c = fork();
    CHECK(attacher_c >= 0, "fork simultaneous public attacher C");
    if (attacher_c == 0) {
        close(lock_wait_ready[0]);
        close(creator_ready[0]);
        close(creator_release[1]);
        jts_ring_writer_t w;
        int rc = jts_ring_writer_open(path, &g, &w);
        if (rc == 0) jts_ring_writer_close(&w);
        _exit(rc == 0 ? 0 : 4);
    }
    unsetenv("JTS_RING_TEST_LOCK_WAIT_FD");
    close(lock_wait_ready[1]);
    if (attacher_b < 0 || attacher_c < 0) {
        CHECK(write(creator_release[1], "x", 1) == 1, "release creator after fork failure");
        int creator_status = 0;
        waitpid(creator, &creator_status, 0);
        close(creator_ready[0]);
        close(creator_release[1]);
        close(lock_wait_ready[0]);
        unlink(path);
        return;
    }

    char waits[2] = {0};
    CHECK(read_bytes_bounded(lock_wait_ready[0], waits, sizeof(waits)) == 0,
          "B and C both report production open-lock contention");

    CHECK(write(creator_release[1], "x", 1) == 1,
          "release creator only after B and C are serialized behind it");
    close(creator_ready[0]);
    close(creator_release[1]);
    close(lock_wait_ready[0]);

    int creator_status = 0;
    int attacher_b_status = 0;
    int attacher_c_status = 0;
    CHECK(waitpid(creator, &creator_status, 0) == creator,
          "join delayed public creator");
    CHECK(waitpid(attacher_b, &attacher_b_status, 0) == attacher_b,
          "join simultaneous public attacher B");
    CHECK(waitpid(attacher_c, &attacher_c_status, 0) == attacher_c,
          "join simultaneous public attacher C");
    CHECK(WIFEXITED(creator_status) && WEXITSTATUS(creator_status) == 0,
          "O_EXCL-winning public opener succeeds");
    CHECK(WIFEXITED(attacher_b_status) && WEXITSTATUS(attacher_b_status) == 0,
          "competing public opener B waits and attaches");
    CHECK(WIFEXITED(attacher_c_status) && WEXITSTATUS(attacher_c_status) == 0,
          "competing public opener C waits and attaches");
    struct stat path_st;
    CHECK(creator_ready_rc == 0 && stat(path, &path_st) == 0 &&
              (uint64_t)path_st.st_dev == creator_observation.dev &&
              (uint64_t)path_st.st_ino == creator_observation.ino,
          "owned path still names the original creator inode");

    unlink(path);
}

static void test_stale_reclaimer_a_cannot_delete_replacement_for_b_and_c(void) {
    char path[320];
    owned_tmp_path(path, sizeof(path), "stale-reclaimer-a-b-c");
    jts_ring_geometry_t g = proto_geometry();
    int torn_fd = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0660);
    CHECK(torn_fd >= 0, "create stale-reclaimer torn inode");
    if (torn_fd < 0) return;
    CHECK(ftruncate(torn_fd, (off_t)jts_ring_file_size(&g)) == 0,
          "size stale-reclaimer torn inode");
    struct stat torn_st;
    CHECK(fstat(torn_fd, &torn_st) == 0, "stat stale-reclaimer torn inode");
    close(torn_fd);

    int reclaim_ready[2] = {-1, -1};
    int reclaim_release[2] = {-1, -1};
    int lock_wait[2] = {-1, -1};
    int results[2] = {-1, -1};
    CHECK(pipe(reclaim_ready) == 0, "create reclaim-ready pipe");
    CHECK(pipe(reclaim_release) == 0, "create reclaim-release pipe");
    CHECK(pipe(lock_wait) == 0, "create stale-reclaimer lock-wait pipe");
    CHECK(pipe(results) == 0, "create stale-reclaimer result pipe");
    if (reclaim_ready[0] < 0 || reclaim_release[0] < 0 || lock_wait[0] < 0 ||
        results[0] < 0) {
        unlink(path);
        return;
    }

    char ready_fd[32], release_fd[32], result_fd[32];
    snprintf(ready_fd, sizeof(ready_fd), "%d", reclaim_ready[1]);
    snprintf(release_fd, sizeof(release_fd), "%d", reclaim_release[0]);
    snprintf(result_fd, sizeof(result_fd), "%d", results[1]);
    CHECK(setenv("JTS_RING_TEST_RECLAIM_READY_FD", ready_fd, 1) == 0,
          "arm reclaimer A ready hook");
    CHECK(setenv("JTS_RING_TEST_RECLAIM_RELEASE_FD", release_fd, 1) == 0,
          "arm reclaimer A release hook");
    pid_t a = fork();
    CHECK(a >= 0, "fork stale reclaimer A");
    if (a == 0) {
        jts_ring_writer_t w;
        int rc = jts_ring_writer_open(path, &g, &w);
        if (rc == 0) {
            test_inode_observation_t observed;
            struct stat st;
            if (fstat(w.fd, &st) == 0) {
                observed = (test_inode_observation_t){.dev = (uint64_t)st.st_dev,
                                                      .ino = (uint64_t)st.st_ino,
                                                      .size = (int64_t)st.st_size};
                (void)write(results[1], &observed, sizeof(observed));
            }
            jts_ring_writer_close(&w);
        }
        _exit(rc == 0 ? 0 : 6);
    }
    unsetenv("JTS_RING_TEST_RECLAIM_READY_FD");
    unsetenv("JTS_RING_TEST_RECLAIM_RELEASE_FD");
    close(reclaim_ready[1]);
    close(reclaim_release[0]);
    char reclaim_signal = 0;
    CHECK(read_bytes_bounded(reclaim_ready[0], &reclaim_signal, 1) == 0,
          "A holds lock after torn classification and before reclaim");

    char lock_fd_text[32];
    snprintf(lock_fd_text, sizeof(lock_fd_text), "%d", lock_wait[1]);
    CHECK(setenv("JTS_RING_TEST_LOCK_WAIT_FD", lock_fd_text, 1) == 0,
          "arm stale B/C lock-wait hook");
    pid_t b = fork();
    if (b == 0) {
        jts_ring_writer_t w;
        int rc = jts_ring_writer_open(path, &g, &w);
        if (rc == 0) {
            report_fd_identity(results[1], w.fd);
            jts_ring_writer_close(&w);
        }
        _exit(rc == 0 ? 0 : 7);
    }
    pid_t c = fork();
    if (c == 0) {
        jts_ring_writer_t w;
        int rc = jts_ring_writer_open(path, &g, &w);
        if (rc == 0) {
            report_fd_identity(results[1], w.fd);
            jts_ring_writer_close(&w);
        }
        _exit(rc == 0 ? 0 : 8);
    }
    unsetenv("JTS_RING_TEST_LOCK_WAIT_FD");
    close(lock_wait[1]);
    char waits[2] = {0};
    CHECK(read_bytes_bounded(lock_wait[0], waits, sizeof(waits)) == 0,
          "stale B and C serialize behind reclaimer A");
    CHECK(write(reclaim_release[1], "x", 1) == 1, "release reclaimer A");
    close(reclaim_ready[0]);
    close(reclaim_release[1]);
    close(lock_wait[0]);
    close(results[1]);

    test_inode_observation_t observed[3] = {{0}};
    CHECK(read_bytes_bounded(results[0], observed, sizeof(observed)) == 0,
          "A, B, and C report their final mapped inode");
    close(results[0]);
    int sa = 0, sb = 0, sc = 0;
    CHECK(waitpid(a, &sa, 0) == a && WIFEXITED(sa) && WEXITSTATUS(sa) == 0,
          "stale reclaimer A succeeds");
    CHECK(waitpid(b, &sb, 0) == b && WIFEXITED(sb) && WEXITSTATUS(sb) == 0,
          "stale contender B succeeds");
    CHECK(waitpid(c, &sc, 0) == c && WIFEXITED(sc) && WEXITSTATUS(sc) == 0,
          "stale contender C succeeds");
    CHECK(observed[0].dev == observed[1].dev &&
              observed[0].ino == observed[1].ino &&
              observed[0].dev == observed[2].dev &&
              observed[0].ino == observed[2].ino,
          "A, B, and C all map one replacement inode");
    CHECK(observed[0].dev != (uint64_t)torn_st.st_dev ||
              observed[0].ino != (uint64_t)torn_st.st_ino,
          "serialized reclaim replaced the original torn inode once");
    unlink(path);
}

static void test_creator_refuses_success_after_path_replacement(void) {
    char path[320];
    char orphan[352];
    owned_tmp_path(path, sizeof(path), "creator-path-replaced");
    snprintf(orphan, sizeof(orphan), "%s.orphan", path);
    jts_ring_geometry_t g = proto_geometry();
    int ready[2] = {-1, -1};
    int release[2] = {-1, -1};
    CHECK(pipe(ready) == 0, "create post-init ready pipe");
    CHECK(pipe(release) == 0, "create post-init release pipe");
    if (ready[0] < 0 || release[0] < 0) return;

    char ready_fd[32];
    char release_fd[32];
    snprintf(ready_fd, sizeof(ready_fd), "%d", ready[1]);
    snprintf(release_fd, sizeof(release_fd), "%d", release[0]);
    CHECK(setenv("JTS_RING_TEST_POST_INIT_READY_FD", ready_fd, 1) == 0,
          "arm post-init ready hook");
    CHECK(setenv("JTS_RING_TEST_POST_INIT_RELEASE_FD", release_fd, 1) == 0,
          "arm post-init release hook");
    pid_t creator = fork();
    CHECK(creator >= 0, "fork path-replaced creator");
    if (creator == 0) {
        close(ready[0]);
        close(release[1]);
        jts_ring_writer_t w;
        int rc = jts_ring_writer_open(path, &g, &w);
        if (rc == 0) jts_ring_writer_close(&w);
        _exit(rc == 0 ? 0 : 5);
    }
    unsetenv("JTS_RING_TEST_POST_INIT_READY_FD");
    unsetenv("JTS_RING_TEST_POST_INIT_RELEASE_FD");
    close(ready[1]);
    close(release[0]);
    if (creator < 0) {
        close(ready[0]);
        close(release[1]);
        return;
    }

    test_inode_observation_t created = {0};
    CHECK(read_observation(ready[0], &created) == 0,
          "creator reports initialized fd before ownership verification");
    CHECK(rename(path, orphan) == 0, "replace linked creator pathname");
    CHECK(mkdir(path, 0770) == 0, "install non-ring replacement at pathname");
    CHECK(write(release[1], "x", 1) == 1,
          "release creator after pathname replacement");
    close(ready[0]);
    close(release[1]);
    int status = 0;
    CHECK(waitpid(creator, &status, 0) == creator, "join path-replaced creator");
    CHECK(WIFEXITED(status) && WEXITSTATUS(status) != 0,
          "creator never reports success for an fd no longer linked at path");
    struct stat orphan_st;
    CHECK(stat(orphan, &orphan_st) == 0 &&
              (uint64_t)orphan_st.st_dev == created.dev &&
              (uint64_t)orphan_st.st_ino == created.ino,
          "initialized orphan is the creator fd that failed ownership proof");
    rmdir(path);
    unlink(orphan);
}

static void test_open_retry_exhaustion_releases_lock(void) {
    char path[320];
    owned_tmp_path(path, sizeof(path), "retry-exhaustion");
    jts_ring_geometry_t g = proto_geometry();
    CHECK(setenv("JTS_RING_TEST_FORCE_RETRY", "1", 1) == 0,
          "arm deterministic retry exhaustion");
    jts_ring_writer_t exhausted;
    int rc = jts_ring_writer_open(path, &g, &exhausted);
    unsetenv("JTS_RING_TEST_FORCE_RETRY");
    CHECK(rc == -EAGAIN, "eight open attempts exhaust with stable EAGAIN");
    CHECK(exhausted.base == NULL && exhausted.fd == -1,
          "retry exhaustion leaves public mapping detached");

    jts_ring_writer_t recovered;
    rc = jts_ring_writer_open(path, &g, &recovered);
    CHECK(rc == 0, "lock is released after retry-exhaustion error");
    if (rc == 0) jts_ring_writer_close(&recovered);
    char lock_path[384];
    snprintf(lock_path, sizeof(lock_path), "%s%s", path,
             JTS_RING_OPEN_LOCK_SUFFIX);
    struct stat lock_st;
    CHECK(stat(lock_path, &lock_st) == 0 && (lock_st.st_mode & 0777) == 0660,
          "C opener heals transaction lock mode to group-writable 0660");
    unlink(path);
}

static void test_owned_magicless_file_is_reclaimed(void) {
    char path[320];
    owned_tmp_path(path, sizeof(path), "owned-torn");
    jts_ring_geometry_t g = proto_geometry();
    int fd = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0660);
    CHECK(fd >= 0, "create owned magicless file");
    if (fd < 0) return;
    CHECK(ftruncate(fd, (off_t)jts_ring_file_size(&g)) == 0,
          "size owned magicless file");
    struct stat old_st;
    int stat_rc = fstat(fd, &old_st);
    CHECK(stat_rc == 0, "stat owned magicless inode");
    close(fd);
    if (stat_rc != 0) {
        unlink(path);
        return;
    }

    jts_ring_reader_t r;
    int rc = jts_ring_reader_open(path, &g, &r);
    CHECK(rc == 0, "reader reclaims owned magicless file");
    if (rc == 0) {
        struct stat new_st;
        CHECK(fstat(r.fd, &new_st) == 0 &&
                  (new_st.st_dev != old_st.st_dev || new_st.st_ino != old_st.st_ino),
              "owned reclaim replaced the torn inode");
        uint64_t magic_version = atomic_load_explicit(
            (_Atomic uint64_t *)r.base, memory_order_acquire);
        CHECK((uint32_t)magic_version == JTS_RING_MAGIC,
              "reclaimed owned ring publishes valid magic");
        jts_ring_reader_close(&r);
    }
    unlink(path);
}

static void test_owned_reclaim_enoent_retries_after_concurrent_reclaimer(void) {
    // Exercise the ENOENT branch directly: the test seam removes the torn inode
    // as a competing reclaimer would, then reports ENOENT to this opener. The
    // opener must retry create/attach rather than treating it like EACCES.
    char path[320];
    owned_tmp_path(path, sizeof(path), "reclaim-enoent");
    jts_ring_geometry_t g = proto_geometry();
    int fd = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0660);
    CHECK(fd >= 0, "create owned ring for concurrent-reclaimer retry");
    if (fd < 0) return;
    CHECK(ftruncate(fd, (off_t)jts_ring_file_size(&g)) == 0,
          "size owned ring for concurrent-reclaimer retry");
    struct stat old_st;
    int stat_rc = fstat(fd, &old_st);
    CHECK(stat_rc == 0, "stat pre-reclaim torn inode");
    close(fd);
    if (stat_rc != 0) {
        unlink(path);
        return;
    }

    char forced_errno[32];
    snprintf(forced_errno, sizeof(forced_errno), "%d", ENOENT);
    CHECK(setenv("JTS_RING_TEST_UNLINK_ERRNO", forced_errno, 1) == 0,
          "arm one-shot concurrent-reclaimer ENOENT");
    jts_ring_reader_t r;
    int rc = jts_ring_reader_open(path, &g, &r);
    unsetenv("JTS_RING_TEST_UNLINK_ERRNO");
    CHECK(rc == 0, "ENOENT from a concurrent reclaimer retries and succeeds");
    if (rc == 0) {
        struct stat new_st;
        CHECK(fstat(r.fd, &new_st) == 0 &&
                  (new_st.st_dev != old_st.st_dev || new_st.st_ino != old_st.st_ino),
              "concurrent-reclaimer retry maps a replacement inode");
        uint64_t magic_version = atomic_load_explicit(
            (_Atomic uint64_t *)r.base, memory_order_acquire);
        CHECK((uint32_t)magic_version == JTS_RING_MAGIC,
              "concurrent-reclaimer retry publishes valid magic");
        jts_ring_reader_close(&r);
    }
    unlink(path);
}

static void test_owned_reclaim_failure_is_logged_and_fail_closed(void) {
    // Force the test-only unlink seam to fail after the full attach/magic
    // timeout. Product builds compile this seam out and call unlink directly;
    // the successful-owned-reclaim test above exercises the real syscall.
    char path[320];
    char log_path[320];
    owned_tmp_path(path, sizeof(path), "reclaim-failure");
    snprintf(log_path, sizeof(log_path), "%s/reclaim-failure.log", g_owned_dir);
    unlink(log_path);
    jts_ring_geometry_t g = proto_geometry();
    int fd = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0660);
    CHECK(fd >= 0, "create owned ring for unlink failure");
    if (fd < 0) return;
    CHECK(ftruncate(fd, (off_t)jts_ring_file_size(&g)) == 0,
          "size owned ring for unlink failure");
    close(fd);

    int log_fd = open(log_path, O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0660);
    CHECK(log_fd >= 0, "open reclaim failure event capture");
    int saved_stderr = dup(STDERR_FILENO);
    CHECK(saved_stderr >= 0, "save stderr for reclaim failure event");
    if (log_fd >= 0 && saved_stderr >= 0) {
        char forced_errno[32];
        snprintf(forced_errno, sizeof(forced_errno), "%d", EACCES);
        CHECK(setenv("JTS_RING_TEST_UNLINK_ERRNO", forced_errno, 1) == 0,
              "force owned unlink failure");
        fflush(stderr);
        CHECK(dup2(log_fd, STDERR_FILENO) >= 0, "capture reclaim failure event");
        jts_ring_writer_t w;
        int rc = jts_ring_writer_open(path, &g, &w);
        fflush(stderr);
        CHECK(dup2(saved_stderr, STDERR_FILENO) >= 0, "restore stderr");
        unsetenv("JTS_RING_TEST_UNLINK_ERRNO");
        CHECK(rc == -EACCES, "unlink failure returns its permission errno");
        CHECK(w.base == NULL && w.fd == -1,
              "unlink failure leaves writer detached");
        CHECK(access(path, F_OK) == 0, "unlink failure preserves torn file");

        CHECK(lseek(log_fd, 0, SEEK_SET) == 0, "rewind reclaim failure event");
        char log_buf[512] = {0};
        ssize_t got = read(log_fd, log_buf, sizeof(log_buf) - 1);
        CHECK(got > 0 && strstr(log_buf,
                                "event=jts_ring.writer.reclaim_failed errno=") != NULL,
              "unlink failure emits stable reclaim_failed event");
    }
    if (saved_stderr >= 0) close(saved_stderr);
    if (log_fd >= 0) close(log_fd);
    unlink(path);
    unlink(log_path);
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

// ============================================================================
// Ring A CAPTURE-direction tests (the reader core + the capture pointer core).
//
// These mirror the playback tests above with roles flipped: the REAL
// jts_ring_writer_* is the producer, the REAL jts_ring_reader_* is the consumer
// (no hand-copied reader — the SPSC discipline is exercised through the shipped
// code), and a capture ioplug model drives the SHARED
// jts_ring_capture_pointer_report so a plugin regression fails `make test`.
// ============================================================================

// Fill a slot buffer with a distinct per-slot marker so a roundtrip can prove
// the CORRECT (oldest-first) slot came out, not just "some 512 bytes".
static void mark_slot(int16_t *buf, size_t samples, int16_t marker) {
    for (size_t i = 0; i < samples; i++) buf[i] = (int16_t)(marker + (int16_t)(i & 0x7));
}

static void test_reader_roundtrip_vs_writer(void) {
    // Real writer publishes distinct slots; real reader consumes them oldest-
    // first with exact payload fidelity. Proves the C-writer<->C-reader wire
    // format (the same format the Rust writer emits — proven on-Pi by the reader
    // bench).
    char path[256];
    tmp_path(path, sizeof(path), "rdr-roundtrip");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader open");

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));

    // Publish three marked slots, consume, assert oldest-first + fidelity.
    for (int16_t k = 0; k < 3; k++) {
        mark_slot(s, n, (int16_t)(1000 + k * 100));
        CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish marked slot");
    }
    CHECK(jts_ring_reader_occupancy_slots(&r) == 3, "reader sees 3 unread slots");
    for (int16_t k = 0; k < 3; k++) {
        mark_slot(s, n, (int16_t)(1000 + k * 100)); // expected
        CHECK(jts_ring_reader_consume(&r, out) == JTS_RING_SLOT_FILLED, "consume filled");
        CHECK(memcmp(out, s, n * sizeof(int16_t)) == 0, "oldest-first payload fidelity");
    }
    // Empty now.
    CHECK(jts_ring_reader_consume(&r, out) == JTS_RING_SLOT_EMPTY, "empty after drain");
    // Empty-read zero-fills.
    int all_zero = 1;
    for (size_t i = 0; i < n; i++) if (out[i] != 0) all_zero = 0;
    CHECK(all_zero, "empty read zero-fills out");
    CHECK(r.frames_read_slots == 3, "frames_read_slots counter");

    free(s);
    free(out);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_reader_attach_resync_drops_stale(void) {
    // The writer runs ahead (fills the ring, no reader). A reader attaching LATER
    // must resync read_seq = write_seq (drop the <= n_slots stale slots a pacer
    // has no use for), count one attach_resync, and see the ring as EMPTY — not
    // replay old audio. Mirrors the Rust RingReader attach.
    char path[256];
    tmp_path(path, sizeof(path), "rdr-resync");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    // Writer publishes with no reader (free-run drop keeps write_seq climbing).
    for (int i = 0; i < 10; i++) {
        mark_slot(s, n, (int16_t)(i * 10));
        (void)jts_ring_writer_publish(&w, s);
    }
    uint64_t wseq_at_attach =
        atomic_load_explicit(&((jts_ring_header_t *)w.base)->write_seq, memory_order_acquire);
    CHECK(wseq_at_attach == 10, "writer advanced write_seq to 10 (free-run)");

    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader attach after writer ran ahead");
    CHECK(r.read_seq == wseq_at_attach, "reader resynced read_seq = write_seq");
    CHECK(r.attach_resyncs == 1, "counted one attach resync");
    int16_t *out = calloc(n, sizeof(int16_t));
    CHECK(jts_ring_reader_consume(&r, out) == JTS_RING_SLOT_EMPTY,
          "post-resync ring is EMPTY (stale slots dropped, no replay)");

    free(s);
    free(out);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_reader_defensive_resync_on_overrun(void) {
    // A wedged reader whose local read_seq fell far behind while the writer
    // free-ran drop-oldest: W - R > n_slots. The next consume must fast-forward
    // to the tip and count a reader_resync rather than read a slot the writer may
    // be mid-overwriting. Mirrors the Rust reader's defensive branch.
    char path[256];
    tmp_path(path, sizeof(path), "rdr-defensive");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader open");

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));

    // Force W - R > n_slots by hand: the reader's local read_seq is 0, and we
    // drive write_seq far ahead directly in the header (simulating a writer that
    // free-ran while this reader was wedged, without the reader observing it).
    jts_ring_header_t *h = (jts_ring_header_t *)w.base;
    atomic_store_explicit(&h->write_seq, (uint64_t)g.n_slots + 3, memory_order_release);
    r.read_seq = 0; // wedged mirror
    CHECK(jts_ring_reader_consume(&r, out) == JTS_RING_SLOT_EMPTY,
          "defensive resync fast-forwards to tip -> empty (not a torn slot)");
    CHECK(r.reader_resyncs == 1, "counted one defensive resync");
    CHECK(r.read_seq == (uint64_t)g.n_slots + 3, "read_seq fast-forwarded to write_seq");

    free(s);
    free(out);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_reader_ebusy_second_reader(void) {
    // The SPSC guard. Reader 1 attaches (stamps a fresh pid+heartbeat). Reader 2
    // opening the SAME ring must be refused with -EBUSY and must NOT corrupt
    // reader 1's read_seq/pid. This is the guard the Rust reader lacks (outputd
    // owns Ring B by construction) but Ring A's operator-openable capture device
    // needs. A stray `arecord -D jts_ring_capture` while camilla is attached is
    // exactly the shape.
    char path[256];
    tmp_path(path, sizeof(path), "rdr-ebusy");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");

    jts_ring_reader_t r1;
    CHECK(jts_ring_reader_open(path, &g, &r1) == 0, "reader 1 attaches");
    // Advance reader 1's read_seq to a nonzero value so a corrupting second
    // attach would be observable (a resync would zero the wrong thing).
    jts_ring_header_t *h = (jts_ring_header_t *)r1.base;
    uint64_t pid1 = atomic_load_explicit(&h->reader_pid, memory_order_relaxed);
    uint64_t rseq_before = atomic_load_explicit(&h->read_seq, memory_order_relaxed);

    // NOTE: reader 1's pid == getpid() here (same process), so foreign_reader_is_live
    // returns 0 for OUR pid — that is correct for re-prepare in the SAME process.
    // To model a DIFFERENT process holding the ring, overwrite reader_pid with a
    // foreign live pid (any nonzero != getpid()) and a fresh heartbeat, then try
    // to open: it must return -EBUSY.
    uint64_t foreign = (uint64_t)getpid() + 1; // definitely not us
    atomic_store_explicit(&h->reader_pid, foreign, memory_order_relaxed);
    atomic_store_explicit(&h->reader_heartbeat_ns, jts_ring_monotonic_ns(),
                          memory_order_relaxed);

    jts_ring_reader_t r2;
    memset(&r2, 0, sizeof(r2));
    int rc = jts_ring_reader_open(path, &g, &r2);
    CHECK(rc == -EBUSY, "second live reader refused with -EBUSY");
    // The incumbent's state is untouched: pid + read_seq unchanged from the
    // foreign values we stamped (the guard bailed BEFORE any resync/stamp).
    CHECK(atomic_load_explicit(&h->reader_pid, memory_order_relaxed) == foreign,
          "EBUSY did not clobber the incumbent reader_pid");
    CHECK(atomic_load_explicit(&h->read_seq, memory_order_relaxed) == rseq_before,
          "EBUSY did not clobber read_seq");
    CHECK(r2.base == NULL, "refused reader struct left detached");
    CHECK(r2.fd == -1, "refused reader fd left detached");

    // A DEAD foreign reader (stale heartbeat) must NOT block a fresh attach —
    // ownership is takeable when the incumbent is gone.
    atomic_store_explicit(&h->reader_heartbeat_ns, 1, memory_order_relaxed); // ancient
    jts_ring_reader_t r3;
    CHECK(jts_ring_reader_open(path, &g, &r3) == 0,
          "dead foreign reader does not block a fresh attach");
    CHECK(atomic_load_explicit(&h->reader_pid, memory_order_relaxed) == (uint64_t)getpid(),
          "fresh attach took ownership (our pid)");

    (void)pid1;
    jts_ring_reader_close(&r3);
    // r1's pid was overwritten by `foreign`/us above; close only clears if ours.
    jts_ring_reader_close(&r1);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_reader_close_clears_pid_only_if_ours(void) {
    // Close must clear reader_pid ONLY if it is still ours — a second reader that
    // stamped its own pid then this instance dropping must not clear the new
    // reader's presence. Mirrors the writer close guard + the Rust RingReader
    // Drop.
    char path[256];
    tmp_path(path, sizeof(path), "rdr-close-guard");
    jts_ring_geometry_t g = proto_geometry();
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader open");
    // Read the header through the WRITER's still-valid mapping (w.base), NOT the
    // reader's — close() munmaps r.base, so a post-close read through r.base would
    // touch freed memory. w and r map the same file, so w.base sees the reader's
    // header writes.
    jts_ring_header_t *h = (jts_ring_header_t *)w.base;
    // Simulate a takeover: some OTHER reader stamped its pid after us.
    uint64_t other = (uint64_t)getpid() + 7;
    atomic_store_explicit(&h->reader_pid, other, memory_order_relaxed);
    jts_ring_reader_close(&r);
    CHECK(atomic_load_explicit(&h->reader_pid, memory_order_relaxed) == other,
          "close did not clear a foreign reader_pid (takeover safe)");
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_reader_epoch_reset_on_writer_reattach(void) {
    // A writer reattach bumps writer_epoch; the reader must observe the change and
    // count an epoch_reset on its next consume. This is the seamless
    // writer-returns path the silence contract relies on.
    char path[256];
    tmp_path(path, sizeof(path), "rdr-epoch");
    jts_ring_geometry_t g = proto_geometry();
    jts_ring_writer_t w1;
    CHECK(jts_ring_writer_open(path, &g, &w1) == 0, "writer 1 open");
    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader open");
    int16_t *out = calloc(g.period_frames * g.channels, sizeof(int16_t));
    (void)jts_ring_reader_consume(&r, out); // observes epoch 1
    CHECK(r.epoch_resets == 0, "no epoch reset yet");

    // Second writer attaches to the SAME ring (writer 1 still mapped) -> epoch++.
    jts_ring_writer_t w2;
    CHECK(jts_ring_writer_open(path, &g, &w2) == 0, "writer 2 reattach (epoch++)");
    (void)jts_ring_reader_consume(&r, out); // observes the epoch change
    CHECK(r.epoch_resets == 1, "reader counted the writer reattach epoch reset");

    free(out);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w2);
    jts_ring_writer_close(&w1);
    unlink(path);
}

// --- capture ioplug model (drives the SHARED jts_ring_capture_pointer_report) ---
//
// The MIRROR of ioplug_model_t: models ALSA's real capture hw_ptr inference
// (snd_pcm_ioplug_hw_ptr_update accumulates delta = (ret - last) mod buffer),
// and derives CAPTURE avail = hw_ptr - appl_ptr (readable). It calls the shared
// jts_ring_capture_pointer_report (the exact function the plugin's capture
// `pointer` returns from) so a regression in the capture core fails `make test`.
// The model tracks the ioplug's DESTAGE + ARMED-silence state the same way the
// plugin does: a slot destaged is one period of readable; the poll tick ARMS a
// period of pending silence when the writer is dead and the real ring is empty
// (bounded to one period); the transfer CONSUMES the armed silence, advancing
// appl. cap_model_avail includes cap_model_poll_arm so every avail read reflects
// the poll-tick arming, exactly as the ALSA rw loop interleaves poll + pointer.
typedef struct {
    uint64_t appl_frames;         // ALSA appl_ptr mirror (frames the app READ)
    jts_ring_pointer_state_t ptr; // reported-position state
    uint64_t alsa_hw_ptr;         // ALSA's accumulated (boundary-space) hw_ptr
    uint64_t alsa_last_hw;        // last pointer() return ALSA stored (mod-buffer)
    int alsa_last_hw_valid;
    uint64_t buffer_size;
    uint32_t period;
    uint64_t pending_silence_frames; // armed-but-unconsumed fabricated silence
    uint64_t silence_periods;        // total fabricated-silence periods delivered (observability)
    uint64_t destage_frames;         // unread frames in the current destage slot
} cap_model_t;

static cap_model_t cap_model_new(const jts_ring_geometry_t *g) {
    cap_model_t m;
    memset(&m, 0, sizeof(m));
    m.buffer_size = (uint64_t)g->n_slots * g->period_frames;
    m.period = g->period_frames;
    return m;
}

// Mirror capture_service_tick's arm step (run by the plugin from BOTH
// poll_revents and the `pointer` callback): on a (virtual) service tick, if the
// writer is dead and the real ring is empty, arm one period of pending silence
// (bounded to one period). The emptiness check uses the BOUNDED occupancy,
// exactly as the plugin does — an out-of-range W - R resolves to a consume
// resync (nothing readable), so it must count as empty here too.
static void cap_model_poll_arm(cap_model_t *m, jts_ring_reader_t *r) {
    // Mirror capture_service_tick's first step: self-heal an out-of-range
    // occupancy so the reader recovers on a wake even when avail is 0 (alsa-lib
    // never calls transfer there, so consume's own resync cannot run).
    jts_ring_reader_resync_if_overrun(r);
    int writer_live = jts_ring_reader_writer_is_live(r);
    uint64_t occ = jts_ring_capture_occupancy_bounded(
        jts_ring_reader_occupancy_slots(r), (uint32_t)(m->buffer_size / m->period));
    int real_empty = (occ == 0) && (m->destage_frames == 0);
    if (!writer_live && real_empty && m->pending_silence_frames < (uint64_t)m->period) {
        m->pending_silence_frames = (uint64_t)m->period;
    }
}

// One capture `pointer` read + ALSA's accumulation, EXACTLY as
// snd_pcm_ioplug_hw_ptr_update does it. Reads occupancy off the REAL reader
// handle; pending-silence off the model (the plugin reads its own field).
static void cap_model_pointer_tick(cap_model_t *m, jts_ring_reader_t *r) {
    jts_ring_capture_pointer_inputs_t in = {
        .appl_frames = m->appl_frames,
        .occupancy_slots = jts_ring_reader_occupancy_slots(r),
        .destage_frames = m->destage_frames,
        .pending_silence_frames = m->pending_silence_frames,
        .period_frames = m->period,
        .buffer_size = m->buffer_size,
    };
    uint64_t raw = jts_ring_capture_pointer_report(&m->ptr, &in);
    uint64_t ret = raw % m->buffer_size;
    if (!m->alsa_last_hw_valid) {
        m->alsa_last_hw = ret;
        m->alsa_last_hw_valid = 1;
        return;
    }
    uint64_t delta = (ret >= m->alsa_last_hw) ? (ret - m->alsa_last_hw)
                                              : (m->buffer_size + ret - m->alsa_last_hw);
    m->alsa_hw_ptr += delta;
    m->alsa_last_hw = ret;
}

// ALSA capture avail off the ACCUMULATED hw_ptr: hw_ptr - appl_ptr (readable).
// PURE pointer read (no service work) in the MODEL. The real plugin's `pointer`
// runs capture_service_tick (drain + arm + resync), but the model deliberately
// keeps this call pure and drives the service work through cap_model_poll_arm
// (cap_model_poll_then_avail) so the ALSA rw-loop ordering the silence tests
// depend on is preserved: an initial `pointer` read (baseline, pending==0)
// BEFORE the first arming, so the first avail read establishes hw_ptr==0 and
// armed silence only ever shows up as a POSITIVE delta on a later read. This
// stays faithful because neither arming nor a resync can fire at baseline
// (writer live, occupancy 0); a real plugin pointer at baseline is likewise a
// no-op service tick.
static uint64_t cap_model_avail(cap_model_t *m, jts_ring_reader_t *r) {
    cap_model_pointer_tick(m, r);
    uint64_t readable =
        (m->alsa_hw_ptr >= m->appl_frames) ? (m->alsa_hw_ptr - m->appl_frames) : 0;
    return (readable <= m->buffer_size) ? readable : m->buffer_size;
}

// A poll tick THEN an avail read — the ALSA rw-loop cadence when the app is
// waiting for data (poll_revents arms silence, then the next pointer read
// reflects it). Use this in tests that drive the writer-dead silence path so the
// arming happens in the right order relative to the baseline.
static uint64_t cap_model_poll_then_avail(cap_model_t *m, jts_ring_reader_t *r) {
    cap_model_poll_arm(m, r);
    return cap_model_avail(m, r);
}

// Model the plugin's capture transfer of ONE period: refill the destage buffer
// (ARMED silence first — it is a delivery commitment the pointer has already
// reported readable — then real ring data), then the app reads a period.
// Returns 1 if a period was delivered (real or silence), 0 if the app must
// block (writer alive + ring empty + no armed silence). Mirrors
// capture_refill_destage + the transfer copy loop for one period. Arms silence
// first (a transfer is preceded by a service tick in the plugin — poll_revents
// or the pointer prologue) so a writer-dead read fabricates without a separate
// avail call.
static int cap_model_read_period(cap_model_t *m, jts_ring_reader_t *r, int16_t *out) {
    cap_model_poll_arm(m, r);
    if (m->destage_frames == 0) {
        if (m->pending_silence_frames >= m->period) {
            // Mirror capture_refill_destage: an ARMED period was already
            // reported to ALSA as readable (hw advanced, forward-only), so it
            // MUST be served — discarding it would leave permanent phantom
            // avail (the RTTIME-spin debt). Serve it before any real slot; the
            // plugin memsets its destage, mirrored here on `out`.
            memset(out, 0, (size_t)m->period * 2 * sizeof(int16_t));
            m->pending_silence_frames -= m->period;
            m->destage_frames = m->period;
            m->silence_periods++;
        } else {
            jts_ring_slot_read_t got = jts_ring_reader_consume(r, out);
            if (got == JTS_RING_SLOT_FILLED) {
                m->destage_frames = m->period;
            } else {
                return 0; // writer alive + empty + no armed silence: block
            }
        }
    }
    // Deliver a period from the destage buffer.
    m->destage_frames -= m->period; // one whole period consumed
    m->appl_frames += m->period;
    return 1;
}

static void test_capture_pointer_advances_on_publish(void) {
    // The core capture-pointer honesty: hw_ptr advances on the WRITER's PUBLISH,
    // so ALSA's capture avail = readable frames. Publish slots (no read yet) and
    // avail must climb to occupancy*period; read them and avail must fall back.
    char path[256];
    tmp_path(path, sizeof(path), "cap-pointer");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader open");
    cap_model_t m = cap_model_new(&g);
    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));

    // Prime the model's first pointer read (ALSA seeds last_hw, hw stays 0).
    (void)cap_model_avail(&m, &r);
    CHECK(cap_model_avail(&m, &r) == 0, "empty ring + live writer: avail 0 (block=pacing)");

    // Publish 3 slots; avail climbs toward 3 periods as the pointer reflects the
    // writer's publish.
    for (int i = 0; i < 3; i++) {
        jts_ring_writer_publish(&w, s);
    }
    uint64_t avail = 0;
    for (int tick = 0; tick < 6; tick++) avail = cap_model_avail(&m, &r); // let the clamp catch up
    CHECK(avail == 3 * (uint64_t)g.period_frames, "avail == 3 periods after 3 publishes");

    // App reads all 3 periods; avail falls to 0.
    for (int i = 0; i < 3; i++) CHECK(cap_model_read_period(&m, &r, out) == 1, "read a period");
    for (int tick = 0; tick < 6; tick++) avail = cap_model_avail(&m, &r);
    CHECK(avail == 0, "avail back to 0 after reading everything");

    free(s);
    free(out);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_capture_alias_writer_burst_gap(void) {
    // CAPTURE alias TRIGGER (a) — mirror of the playback drain-gap: while the app
    // is mid-gap (no pointer read), the WRITER publishes a full buffer of slots.
    // The next pointer read would jump hw_ptr forward by exactly buffer_size — the
    // alias to a ZERO delta that pins avail at 0 and wedges camilla reading a
    // producer that is actually full. The clamp must spread it into sub-buffer
    // deltas so avail reopens.
    char path[256];
    tmp_path(path, sizeof(path), "cap-alias-burst");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader open");
    cap_model_t m = cap_model_new(&g);
    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));

    // Seed the pointer at an empty ring (hw_ptr == appl == 0).
    (void)cap_model_avail(&m, &r);
    uint64_t raw_before = m.ptr.last_reported;

    // The GAP: the writer publishes a FULL buffer of slots while no pointer read
    // happens (the app is outside a PCM call).
    for (uint32_t i = 0; i < g.n_slots; i++) jts_ring_writer_publish(&w, s);
    CHECK(jts_ring_reader_occupancy_slots(&r) == (uint64_t)g.n_slots, "ring full after burst");

    // Not-a-tautology: an UNCLAMPED honest capture pointer would now report
    // appl + occupancy*period = 0 + buffer_size, a raw jump of exactly
    // buffer_size -> aliases to delta 0 (would wedge avail at 0 permanently).
    uint64_t honest_unclamped = m.appl_frames + (uint64_t)g.n_slots * g.period_frames;
    CHECK(honest_unclamped - raw_before == m.buffer_size,
          "unclamped capture burst jump is exactly one buffer (alias precondition)");
    CHECK(alsa_delta(raw_before, honest_unclamped, m.buffer_size) == 0,
          "unclamped: full-buffer writer burst aliases to ZERO delta (would wedge)");

    // Clamped: avail reopens over successive ticks (sub-buffer deltas), hw_ptr
    // monotonic, and eventually reflects the full buffer of readable data.
    uint64_t avail = 0;
    int saw_open = 0;
    for (int tick = 0; tick < (int)g.n_slots + 2; tick++) {
        uint64_t hw_prev = m.alsa_hw_ptr;
        avail = cap_model_avail(&m, &r);
        CHECK(m.alsa_hw_ptr >= hw_prev, "clamped: capture hw_ptr monotonic across catch-up");
        if (avail > 0) saw_open = 1;
    }
    CHECK(saw_open, "clamped: avail reopens after the writer-burst gap (no alias wedge)");
    CHECK(avail == m.buffer_size, "clamped: full buffer of readable eventually reflected");

    free(s);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_capture_alias_writer_death_flip(void) {
    // CAPTURE alias TRIGGER (b) — mirror of the playback dead-flip: the ring is
    // full of unread slots and the WRITER dies. The app must keep pulling those
    // real slots, then transition to fabricated silence. The alias risk is the
    // readable value stepping by a full buffer in one pointer read across the
    // silence transition; the clamp must keep avail open (POLLIN armed) the whole
    // time so the app never wedges — this is the "fanin restart while the ring
    // was full" operational shape.
    char path[256];
    tmp_path(path, sizeof(path), "cap-alias-death");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader open");
    cap_model_t m = cap_model_new(&g);
    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));

    (void)cap_model_avail(&m, &r);
    for (uint32_t i = 0; i < g.n_slots; i++) jts_ring_writer_publish(&w, s);
    for (int tick = 0; tick < (int)g.n_slots + 2; tick++) (void)cap_model_avail(&m, &r);

    // The WRITER dies (stale heartbeat). occupancy unchanged (n_slots real slots
    // still unread), so the writer-dead classification is now true but there is
    // still real data.
    jts_ring_header_t *h = (jts_ring_header_t *)w.base;
    atomic_store_explicit(&h->writer_heartbeat_ns, 1, memory_order_relaxed);
    CHECK(!jts_ring_reader_writer_is_live(&r), "writer now dead (stale heartbeat)");

    // The app keeps reading: first the real slots, then fabricated silence — avail
    // must stay open (POLLIN) the whole time (no wedge on a gone producer).
    int silence_seen = 0, real_seen = 0;
    for (int i = 0; i < 3 * (int)g.n_slots; i++) {
        uint64_t before_sil = m.silence_periods;
        CHECK(cap_model_read_period(&m, &r, out) == 1,
              "read a period through writer death (real or fabricated silence)");
        if (m.silence_periods > before_sil) silence_seen = 1;
        else real_seen = 1;
        // Poll re-arms (silence if the ring has drained; a no-op while real slots
        // remain), then avail must be open: real data OR a freshly-armed silence
        // period — never a wedge on the gone producer.
        uint64_t avail = cap_model_poll_then_avail(&m, &r);
        CHECK(avail > 0, "clamped: writer-dead keeps capture avail open (no wedge)");
    }
    CHECK(real_seen, "read the real slots that were in the ring at death");
    CHECK(silence_seen, "transitioned to fabricated silence once the ring drained");

    free(s);
    free(out);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_capture_alias_dead_to_live_recovery(void) {
    // CAPTURE alias TRIGGER (c) — writer dies, app free-runs on fabricated
    // silence, then a NEW writer reattaches (epoch++). hw_ptr must never regress
    // across the transition and real audio must resume once the writer publishes.
    char path[256];
    tmp_path(path, sizeof(path), "cap-alias-recover");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w1;
    CHECK(jts_ring_writer_open(path, &g, &w1) == 0, "writer 1 open");
    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader open");
    cap_model_t m = cap_model_new(&g);
    size_t n = w1.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));

    // Writer 1 dies immediately (stale heartbeat); the app free-runs on silence.
    jts_ring_header_t *h = (jts_ring_header_t *)w1.base;
    atomic_store_explicit(&h->writer_heartbeat_ns, 1, memory_order_relaxed);
    (void)cap_model_avail(&m, &r);
    uint64_t hw_before = m.alsa_hw_ptr;
    uint64_t prev = m.alsa_hw_ptr;
    int silence_periods = 0;
    for (int i = 0; i < 6; i++) {
        CHECK(cap_model_read_period(&m, &r, out) == 1, "free-run on silence");
        silence_periods++;
        // A poll tick re-arms silence, then avail reflects it: the writer-dead
        // silence free-run keeps avail open (one period) so POLLIN re-fires each
        // wait, and it is bounded by the buffer.
        uint64_t avail = cap_model_poll_then_avail(&m, &r);
        CHECK(avail > 0 && avail <= m.buffer_size, "silence free-run: avail open + bounded");
        CHECK(m.alsa_hw_ptr >= prev, "silence free-run: hw_ptr never regresses");
        prev = m.alsa_hw_ptr;
    }
    CHECK(silence_periods == 6, "fabricated silence periods while writer dead");

    // A NEW writer reattaches (epoch++), fresh heartbeat. It publishes real slots.
    jts_ring_writer_t w2;
    CHECK(jts_ring_writer_open(path, &g, &w2) == 0, "writer 2 reattach (epoch++)");
    for (int i = 0; i < 3; i++) {
        mark_slot(s, n, (int16_t)(2000 + i));
        jts_ring_writer_publish(&w2, s);
    }
    CHECK(jts_ring_reader_writer_is_live(&r), "writer live again after reattach");

    // Real audio resumes; hw_ptr keeps climbing, never regresses, no silence now.
    int real_periods = 0;
    for (int i = 0; i < 6; i++) {
        uint64_t before_sil = m.silence_periods;
        if (cap_model_read_period(&m, &r, out) == 1) {
            if (m.silence_periods == before_sil) real_periods++;
        }
        CHECK(m.alsa_hw_ptr >= prev, "recovery: hw_ptr monotonic across writer reattach");
        prev = m.alsa_hw_ptr;
        (void)cap_model_avail(&m, &r);
    }
    CHECK(real_periods >= 3, "real audio resumed after writer reattach (no silence)");
    CHECK(m.alsa_hw_ptr > hw_before, "recovery made real forward progress (never wedged)");

    free(s);
    free(out);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w2);
    jts_ring_writer_close(&w1);
    unlink(path);
}

static void test_capture_silence_mode_entry_exit(void) {
    // The writer-dead silence decision in isolation: empty + writer ALIVE ->
    // withhold (block=pacing, read_period returns 0); empty + writer DEAD ->
    // fabricate a period of silence (read_period returns 1, silence_periods bumps);
    // writer returns -> silence stops, real audio flows.
    char path[256];
    tmp_path(path, sizeof(path), "cap-silence");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader open");
    cap_model_t m = cap_model_new(&g);
    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));

    // Empty + writer ALIVE: the app must BLOCK (no fabricated silence).
    CHECK(cap_model_read_period(&m, &r, out) == 0,
          "empty + writer alive: block (no silence), that IS the pacing");
    CHECK(m.silence_periods == 0, "no silence fabricated while writer is alive");

    // Writer DIES: now the app fabricates timer-paced silence and never blocks.
    jts_ring_header_t *h = (jts_ring_header_t *)w.base;
    atomic_store_explicit(&h->writer_heartbeat_ns, 1, memory_order_relaxed);
    CHECK(cap_model_read_period(&m, &r, out) == 1, "writer dead: fabricate a silence period");
    CHECK(m.silence_periods == 1, "one silence period fabricated");
    int all_zero = 1;
    for (size_t i = 0; i < n; i++) if (out[i] != 0) all_zero = 0;
    CHECK(all_zero, "fabricated silence is zeros");

    // Writer RETURNS (fresh heartbeat + a real slot): silence stops, audio flows.
    atomic_store_explicit(&h->writer_heartbeat_ns, jts_ring_monotonic_ns(),
                          memory_order_relaxed);
    mark_slot(s, n, 4242);
    jts_ring_writer_publish(&w, s);
    uint64_t sil_before = m.silence_periods;
    CHECK(cap_model_read_period(&m, &r, out) == 1, "writer back: read a real slot");
    CHECK(m.silence_periods == sil_before, "no new silence fabricated once the writer is back");
    CHECK(memcmp(out, s, n * sizeof(int16_t)) == 0, "real audio resumes seamlessly");

    free(s);
    free(out);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w);
    unlink(path);
}

// Model the plugin's jts_ring_capture_poll_revents ARM predicate exactly, so the
// wall-clock silence pacing is host-tested (finding 5 closed the "host-untested"
// gap). Given a monotonic clock `now`, arm one period iff the writer is dead, the
// ring is empty, no period is already armed, and either this is the first arm
// (last_silence_ns == 0) or a full period_ns has elapsed. On arm, re-anchor
// last_silence_ns to `now` (the tick time — the source of the ~14% slow, safe-
// direction drift). Returns 1 if a period was armed this tick, 0 otherwise.
static int cap_pacing_arm_tick(uint64_t now, uint64_t period_ns,
                               int writer_dead, int ring_empty,
                               uint64_t *pending, uint64_t *last_silence_ns,
                               uint32_t period_frames) {
    if (writer_dead && ring_empty && *pending < (uint64_t)period_frames) {
        if (*last_silence_ns == 0 || now - *last_silence_ns >= period_ns) {
            *pending = (uint64_t)period_frames;
            *last_silence_ns = now;
            return 1;
        }
    }
    return 0;
}

static void test_capture_silence_pacing_never_faster_than_realtime(void) {
    // Finding 5: the writer-dead silence pacing was host-untested. Pin the two
    // load-bearing properties of jts_ring_capture_poll_revents' arm step:
    //   (1) the per-tick BOUND — pending_silence_frames never exceeds one period
    //       (avail can never run away), AND
    //   (2) the SAFE-DIRECTION guarantee — over any real-time window, silence is
    //       never armed FASTER than realtime. Slow is fine (measured ~14% slow);
    //       fast would pre-consume a returning writer's audio as silence.
    // We simulate the ALSA rw loop's poll cadence (a tick every period/4) over a
    // fixed wall-clock window with the writer dead + ring empty, and count arms.
    const uint32_t period_frames = 128;
    const uint64_t period_ns = (uint64_t)period_frames * 1000000000ull / 48000; // 2666666 ns
    const uint64_t tick_ns = period_ns / 4; // the plugin's arm_timer cadence
    uint64_t pending = 0;
    uint64_t last_silence_ns = 0;

    // Consume the armed period on the tick AFTER it is armed (mirrors the transfer
    // draining one period), so `pending < period` re-opens for the next arm — the
    // steady writer-dead free-run the arecord probe drives.
    const int ticks = 4000; // 4000 * tick_ns ~= 2.667 s of simulated wall time
    uint64_t window_ns = 0;
    int arms = 0;
    for (int t = 0; t < ticks; t++) {
        uint64_t now = (uint64_t)t * tick_ns;
        window_ns = now;
        arms += cap_pacing_arm_tick(now, period_ns, /*writer_dead=*/1,
                                    /*ring_empty=*/1, &pending, &last_silence_ns,
                                    period_frames);
        // (1) The per-tick bound: never more than one period armed at once.
        CHECK(pending <= (uint64_t)period_frames, "pending silence bounded to <= one period");
        // Drain the armed period (the app read it) so the next tick can re-arm.
        if (pending >= (uint64_t)period_frames) pending -= (uint64_t)period_frames;
    }
    // (2) Safe direction: the number of armed periods over the window must not
    // exceed what realtime would produce (window/period + 1 for the immediate
    // first arm). Faster-than-realtime would fail here.
    uint64_t realtime_periods = window_ns / period_ns + 1;
    CHECK((uint64_t)arms <= realtime_periods,
          "silence armed no FASTER than realtime (safe direction; slow is fine)");
    // And it actually paced (not stuck): at least half of realtime, proving the
    // gate opens (a totally stuck pacer would fail the writer-return handoff too).
    CHECK((uint64_t)arms >= realtime_periods / 2,
          "silence pacing actually advances (not wedged)");
}

static void test_capture_armed_silence_commitment_no_phantom_avail(void) {
    // The RTTIME-spin debt regression (2026-07-11 camilla SIGKILL diagnosis).
    // Once the service tick ARMS a period of silence, the pointer REPORTS it to
    // ALSA as readable — and the reported position is forward-only by design
    // (the alias clamp). The pre-fix refill DISCARDED an armed-but-unconsumed
    // period when the writer's first slot raced in, leaving hw_ptr one period
    // ahead of anything the refill could ever serve: PERMANENT phantom avail.
    // ALSA's rw loop only poll-waits at avail == 0, so every genuinely-empty
    // moment then became a hot 0-frame-transfer spin — on camilla's SCHED_FIFO
    // capture thread, the RLIMIT_RTTIME SIGKILL. The fixed contract: an armed
    // period is a delivery COMMITMENT, served BEFORE real data (it belongs to
    // the silence gap that just ended), so the books stay exact and the one
    // extra ~2.7 ms of zeros lands contiguous with the gap — never spliced into
    // steady-state music later (the concern that motivated the old discard;
    // pinned in step 5).
    char path[256];
    tmp_path(path, sizeof(path), "cap-armed-commitment");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader open");
    cap_model_t m = cap_model_new(&g);
    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));
    jts_ring_header_t *h = (jts_ring_header_t *)w.base;

    // Seed the pointer baseline (ALSA's first read establishes last_hw).
    (void)cap_model_avail(&m, &r);

    // 1. Writer DIES and the ring is empty: a service tick ARMS one period and
    // the pointer REPORTS it (avail opens) — armed but not yet consumed, exactly
    // the rw loop's poll-before-transfer window.
    atomic_store_explicit(&h->writer_heartbeat_ns, 1, memory_order_relaxed);
    uint64_t avail = cap_model_poll_then_avail(&m, &r);
    CHECK(m.pending_silence_frames == g.period_frames, "silence armed while writer dead");
    CHECK(avail == g.period_frames, "armed period REPORTED readable (hw advanced)");
    CHECK(m.silence_periods == 0, "armed but not yet consumed");

    // 2. Writer RETURNS (fresh heartbeat) and PUBLISHES a real slot BEFORE the
    // armed silence is consumed — the race that used to mint the debt.
    atomic_store_explicit(&h->writer_heartbeat_ns, jts_ring_monotonic_ns(),
                          memory_order_relaxed);
    mark_slot(s, n, 1717);
    CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "writer publishes a real slot");

    // 3. First read serves the COMMITTED silence period (zeros), not the real slot.
    CHECK(cap_model_read_period(&m, &r, out) == 1, "read the committed silence period");
    CHECK(m.silence_periods == 1, "the armed period was SERVED, not discarded");
    int all_zero = 1;
    for (size_t i = 0; i < n; i++) if (out[i] != 0) all_zero = 0;
    CHECK(all_zero, "committed period is zeros");
    CHECK(m.pending_silence_frames == 0, "commitment consumed");

    // 4. Second read serves the real slot, payload intact.
    CHECK(cap_model_read_period(&m, &r, out) == 1, "read the real slot");
    CHECK(memcmp(out, s, n * sizeof(int16_t)) == 0, "real audio intact after the boundary");

    // 5. THE DEBT REGRESSION: everything reported was delivered, so avail must
    // return to EXACTLY 0 — and stay 0 across service ticks (writer alive: no
    // new arms, no spurious silence in live audio). With the old discard, hw
    // sat one period ahead of appl forever: avail pinned at period_frames with
    // read_period returning 0 — the poll-less spin precondition.
    for (int tick = 0; tick < 6; tick++) avail = cap_model_poll_then_avail(&m, &r);
    CHECK(avail == 0, "books exact after the boundary: avail 0, no phantom debt");
    CHECK(cap_model_read_period(&m, &r, out) == 0,
          "empty + writer alive: BLOCK (pacing) — no spurious silence spliced in");
    CHECK(m.silence_periods == 1, "no fabricated silence in steady-state live audio");

    free(s);
    free(out);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_capture_avail_implies_deliverable(void) {
    // The spin-precondition invariant, swept across a full writer death /
    // silence free-run / reattach cycle: whenever the reported avail is > 0,
    // the transfer path MUST be able to deliver at least one period. An
    // avail > 0 / deliver-nothing divergence is the state alsa-lib's rw loop
    // cannot escape without spinning (it only poll-waits at avail == 0) — the
    // RLIMIT_RTTIME SIGKILL class. The plugin additionally carries a bounded
    // starvation nap as defense in depth, but the invariant itself must hold.
    char path[256];
    tmp_path(path, sizeof(path), "cap-avail-deliverable");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader open");
    cap_model_t m = cap_model_new(&g);
    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));
    jts_ring_header_t *h = (jts_ring_header_t *)w.base;
    (void)cap_model_avail(&m, &r);

    // Scripted sequence covering: live publishes, mid-drain writer death,
    // silence free-run, an arm racing the writer's return, and live resume.
    for (int step = 0; step < 24; step++) {
        switch (step) {
            case 0: case 1: case 2:
                jts_ring_writer_publish(&w, s); // live publishes
                break;
            case 5: // writer dies mid-drain (slots may remain)
                atomic_store_explicit(&h->writer_heartbeat_ns, 1, memory_order_relaxed);
                break;
            case 14: // writer returns AND publishes into the armed window
                atomic_store_explicit(&h->writer_heartbeat_ns, jts_ring_monotonic_ns(),
                                      memory_order_relaxed);
                jts_ring_writer_publish(&w, s);
                break;
            case 18: case 19:
                jts_ring_writer_publish(&w, s); // live steady state again
                break;
            default:
                break;
        }
        uint64_t avail = cap_model_poll_then_avail(&m, &r);
        if (avail > 0) {
            CHECK(cap_model_read_period(&m, &r, out) == 1,
                  "avail > 0 implies a period is deliverable (no poll-less spin state)");
        } else {
            // avail == 0 is the legitimate block: alsa-lib poll-waits here, and
            // nothing may be armed-but-unreported (a commitment must be visible).
            CHECK(m.pending_silence_frames == 0,
                  "avail == 0 implies no invisible armed commitment");
        }
    }

    free(s);
    free(out);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_capture_occupancy_clamp_prevents_phantom_avail(void) {
    // A transient out-of-range occupancy (W - local R > n_slots — a wedged
    // reader whose heartbeat staled while the writer free-ran, or u64 garbage)
    // must (1) be REPORTED as 0 readable — an unbounded report would ratchet the
    // forward-only reported position and mint permanent phantom avail (same
    // RTTIME-spin debt class as the armed-silence discard) — AND (2) SELF-HEAL
    // on the per-wake service tick, NOT only when transfer runs. The wedge SF-A
    // (2026-07-11 review) is the second half: at avail 0 alsa-lib never calls
    // transfer, so if the resync lived only in consume the reader would sit
    // permanently silent with a live writer. jts_ring_reader_resync_if_overrun
    // in capture_service_tick (modeled here in cap_model_poll_arm) gives it an
    // avail-visible recovery path.
    CHECK(jts_ring_capture_occupancy_bounded(0, 4) == 0, "bounded: 0 -> 0");
    CHECK(jts_ring_capture_occupancy_bounded(4, 4) == 4, "bounded: n_slots passes");
    CHECK(jts_ring_capture_occupancy_bounded(5, 4) == 0, "bounded: n_slots+1 -> 0 (resync outcome)");
    CHECK(jts_ring_capture_occupancy_bounded(UINT64_MAX, 4) == 0, "bounded: underflow garbage -> 0");

    char path[256];
    tmp_path(path, sizeof(path), "cap-occ-clamp");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader open");
    cap_model_t m = cap_model_new(&g);
    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    int16_t *out = calloc(n, sizeof(int16_t));
    jts_ring_header_t *h = (jts_ring_header_t *)w.base;
    (void)cap_model_avail(&m, &r);

    // Wedge the reader (stale heartbeat, local read_seq frozen at 0) and let
    // the writer fill + free-run: header read_seq advances on the reader's
    // behalf, but the reader's LOCAL mirror does not — its raw occupancy view
    // goes out of range. The writer STAYS ALIVE (it is fan-in, camilla's pacer).
    atomic_store_explicit(&h->reader_heartbeat_ns, 1, memory_order_relaxed);
    for (uint32_t i = 0; i < g.n_slots + 3; i++) jts_ring_writer_publish(&w, s);
    CHECK(jts_ring_reader_occupancy_slots(&r) > (uint64_t)g.n_slots,
          "precondition: raw local occupancy out of range");

    // (1) The garbage is reported as 0 readable — never phantom avail.
    uint64_t avail = cap_model_poll_then_avail(&m, &r);
    CHECK(avail == 0, "out-of-range occupancy reports 0 readable (no phantom avail)");
    // (2) SELF-HEAL: that same wake resynced the reader WITHOUT any transfer at
    // avail 0. This is the SF-A fix — recovery through an avail-visible flow, not
    // a read alsa-lib would never issue.
    CHECK(r.reader_resyncs == 1, "the per-wake service tick self-healed via resync");
    CHECK(jts_ring_reader_occupancy_slots(&r) == 0, "local read_seq caught up to the tip");

    // With the mirror healed and the writer still alive, fresh publishes reopen
    // avail over the next wakes and delivery resumes — no read was ever issued at
    // avail 0, and reader_resyncs does NOT climb further (steady state).
    jts_ring_writer_publish(&w, s);
    for (int tick = 0; tick < 3; tick++) avail = cap_model_poll_then_avail(&m, &r);
    CHECK(avail == g.period_frames, "post-resync: honest readable resumes");
    CHECK(cap_model_read_period(&m, &r, out) == 1, "post-resync: delivery resumes");
    CHECK(r.reader_resyncs == 1, "no repeated resync once healed (not a resync loop)");

    free(s);
    free(out);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_capture_destage_partial_reads(void) {
    // Sub-slot reads: the app reads FEWER frames than a whole slot at a time. The
    // destage buffer must serve the slot across multiple readi()s with exact byte
    // continuity (no dropped or duplicated frames at the sub-slot boundary). This
    // models the plugin's jts_ring_capture_transfer copy loop directly (the
    // cap_model reads whole periods, so this test drives the real reader + a
    // hand destage to prove the partial-read arithmetic).
    char path[256];
    tmp_path(path, sizeof(path), "cap-partial");
    jts_ring_geometry_t g = proto_geometry();
    g.period_frames = 128;
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "reader open");
    size_t spp = w.samples_per_slot; // 128 * 2 = 256
    int16_t *s = calloc(spp, sizeof(int16_t));
    // Distinct per-frame content so a boundary bug is visible.
    for (size_t f = 0; f < g.period_frames; f++) {
        s[f * 2 + 0] = (int16_t)(f + 1);       // L
        s[f * 2 + 1] = (int16_t)(-(int)(f + 1)); // R
    }
    CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish one slot");

    // Destage the slot once, then drain it in 30-frame chunks (128 = 30*4 + 8),
    // exactly the plugin's origin = capacity - remaining arithmetic.
    int16_t *destage = calloc(spp, sizeof(int16_t));
    CHECK(jts_ring_reader_consume(&r, destage) == JTS_RING_SLOT_FILLED, "destage the slot");
    size_t remaining = g.period_frames; // frames unread in destage
    size_t total_read = 0;
    int16_t *appbuf = calloc(g.period_frames * 2, sizeof(int16_t));
    while (remaining > 0) {
        size_t chunk = remaining < 30 ? remaining : 30;
        size_t origin = g.period_frames - remaining; // frames already read
        memcpy(appbuf + total_read * 2, destage + origin * 2, chunk * 2 * sizeof(int16_t));
        remaining -= chunk;
        total_read += chunk;
    }
    CHECK(total_read == g.period_frames, "read the whole slot in sub-slot chunks");
    CHECK(memcmp(appbuf, s, spp * sizeof(int16_t)) == 0,
          "sub-slot destage preserved every frame in order (no boundary bug)");

    free(s);
    free(destage);
    free(appbuf);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w);
    unlink(path);
}

int main(void) {
    snprintf(g_owned_dir, sizeof(g_owned_dir), "/tmp/jts-ring-ctest-owned-%d",
             (int)getpid());
    CHECK(setenv("JTS_RING_TEST_OWNED_DIR", g_owned_dir, 1) == 0,
          "configure per-process test-owned ring root");
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
    test_reader_creates_missing_parent_then_writer_attaches();
    test_magicless_foreign_file_is_rejected_without_reclaim();
    test_simultaneous_first_open_waits_for_creator_ftruncate();
    test_stale_reclaimer_a_cannot_delete_replacement_for_b_and_c();
    test_creator_refuses_success_after_path_replacement();
    test_open_retry_exhaustion_releases_lock();
    test_owned_magicless_file_is_reclaimed();
    test_owned_reclaim_enoent_retries_after_concurrent_reclaimer();
    test_owned_reclaim_failure_is_logged_and_fail_closed();
    test_can_accept_semantics();
    test_deep_ring_16_slots();
    test_occupancy_tracks_reader_drain();
    test_drain_flush_partial_slot();
    // Ring A CAPTURE-direction (reader core + capture pointer core).
    test_reader_roundtrip_vs_writer();
    test_reader_attach_resync_drops_stale();
    test_reader_defensive_resync_on_overrun();
    test_reader_ebusy_second_reader();
    test_reader_close_clears_pid_only_if_ours();
    test_reader_epoch_reset_on_writer_reattach();
    test_capture_pointer_advances_on_publish();
    test_capture_alias_writer_burst_gap();
    test_capture_alias_writer_death_flip();
    test_capture_alias_dead_to_live_recovery();
    test_capture_silence_mode_entry_exit();
    test_capture_silence_pacing_never_faster_than_realtime();
    test_capture_armed_silence_commitment_no_phantom_avail();
    test_capture_avail_implies_deliverable();
    test_capture_occupancy_clamp_prevents_phantom_avail();
    test_capture_destage_partial_reads();
    cleanup_all_test_paths();
    cleanup_owned_test_locks();
    rmdir(g_owned_dir);

    if (g_failures == 0) {
        printf("ok: all jts_ring core tests passed\n");
        return 0;
    }
    fprintf(stderr, "FAILED: %d check(s)\n", g_failures);
    return 1;
}
