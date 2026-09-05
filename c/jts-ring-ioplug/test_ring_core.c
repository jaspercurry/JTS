// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0
//
// Host unit test for the JTS ring core (jts_ring_shm.c). Links neither ALSA nor
// Rust, so it builds and runs on any host (macOS/Linux) via the Makefile `test`
// target; the reader half of the playback tests is inlined here for the same
// reason rather than taken from the Rust crate.
//
// The cross-language C-writer -> Rust-reader interop is proven separately by
// ring_writer_bench.c feeding jasper-outputd (on-Pi).

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

static int write_bytes(int fd, const void *data, size_t len) {
    const uint8_t *cursor = (const uint8_t *)data;
    size_t remaining = len;
    while (remaining > 0) {
        ssize_t n = write(fd, cursor, remaining);
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

static int report_fd_identity(int report_fd, int ring_fd) {
    struct stat st;
    if (fstat(ring_fd, &st) < 0) return -1;
    test_inode_observation_t observed = {
        .dev = (uint64_t)st.st_dev,
        .ino = (uint64_t)st.st_ino,
        .size = (int64_t)st.st_size,
    };
    return write_bytes(report_fd, &observed, sizeof(observed));
}

// Every adjacent lock file a ring can accumulate. BOTH must be listed here and
// in cleanup_all_test_paths: a suffix this array does not know is a per-run leak
// in /tmp that nothing ever collects (measured at 34 files per clean run when
// the writer lock was missing from it).
static const char *const k_ring_lock_suffixes[] = {
    JTS_RING_OPEN_LOCK_SUFFIX,
    JTS_RING_WRITER_LOCK_SUFFIX,
};
#define K_RING_LOCK_SUFFIX_COUNT \
    (sizeof(k_ring_lock_suffixes) / sizeof(k_ring_lock_suffixes[0]))

static void cleanup_owned_test_locks(void) {
    DIR *dir = opendir(g_owned_dir);
    if (dir == NULL) return;
    struct dirent *entry;
    while ((entry = readdir(dir)) != NULL) {
        int is_lock = 0;
        for (size_t i = 0; i < K_RING_LOCK_SUFFIX_COUNT; i++) {
            if (strstr(entry->d_name, k_ring_lock_suffixes[i]) != NULL) {
                is_lock = 1;
                break;
            }
        }
        if (!is_lock) continue;
        // Same truncation rule as compose_path below, and the same reason: this
        // string is unlinked. `d_name` is unbounded from the compiler's view
        // (up to NAME_MAX), so gcc flags the composition; a short buffer here
        // would also be a real hazard, not just a warning.
        char path[512];
        int n = snprintf(path, sizeof(path), "%s/%s", g_owned_dir,
                         entry->d_name);
        if (n < 0 || (size_t)n >= sizeof(path)) continue;
        (void)unlink(path);
    }
    closedir(dir);
}

static void remember_test_path(const char *path) {
    if (g_test_path_count >= sizeof(g_test_paths) / sizeof(g_test_paths[0])) return;
    snprintf(g_test_paths[g_test_path_count], sizeof(g_test_paths[0]), "%s", path);
    g_test_path_count++;
}

// TRUNCATION MUST NOT UNLINK: a truncated path is a DIFFERENT path, and these
// composed strings are fed straight to unlink(2), so a silently shortened
// `/tmp/…/foo.ring.writer.lock` could name some other file entirely and delete
// it. Refusing instead leaks one lock file in a case the fixed-size test paths
// this file builds cannot reach.
//
// Also silences gcc's -Wformat-truncation, which fires here and not under
// clang: the suffix is a `const char *` the compiler cannot bound, so it
// assumes up to 65535 bytes.
static int compose_path(char *out, size_t out_size, const char *base,
                        const char *suffix) {
    int n = snprintf(out, out_size, "%s%s", base, suffix);
    return n >= 0 && (size_t)n < out_size;
}

static void cleanup_all_test_paths(void) {
    for (size_t i = 0; i < g_test_path_count; i++) {
        (void)unlink(g_test_paths[i]);
        for (size_t j = 0; j < K_RING_LOCK_SUFFIX_COUNT; j++) {
            char lock_path[600];
            if (!compose_path(lock_path, sizeof(lock_path), g_test_paths[i],
                              k_ring_lock_suffixes[j])) {
                continue;
            }
            (void)unlink(lock_path);
        }
    }
}

// A minimal in-process reader mirroring rust/jasper-ring's try_consume_slot:
// Acquire write_seq, if empty -> silence, else copy slot (r % n_slots), then
// Release read_seq.
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
    bad.channels = 1; // mono: representable in the layout, refused by policy
    CHECK(jts_ring_geometry_validate(&bad, &reason) != 0, "reject 1ch (below the 2 floor)");
    bad = g;
    bad.channels = JTS_RING_MAX_CHANNELS + 1;
    CHECK(jts_ring_geometry_validate(&bad, &reason) != 0, "reject 9ch (> ceiling 8)");
    bad = g;
    bad.channels = 0;
    CHECK(jts_ring_geometry_validate(&bad, &reason) != 0, "reject 0 channels");
    bad = g;
    bad.n_slots = 1;
    CHECK(jts_ring_geometry_validate(&bad, &reason) != 0, "reject 1 slot");
    bad = g;
    bad.n_slots = 17; // ceiling is 16
    CHECK(jts_ring_geometry_validate(&bad, &reason) != 0, "reject 17 slots (> ceiling 16)");
    bad = g;
    bad.sample_format = 3; // neither S16LE (1) nor S32LE (2)
    CHECK(jts_ring_geometry_validate(&bad, &reason) != 0, "reject unknown format id 3");
    bad = g;
    bad.sample_format = 0;
    CHECK(jts_ring_geometry_validate(&bad, &reason) != 0, "reject format id 0");
    bad = g;
    bad.rate = 44100;
    CHECK(jts_ring_geometry_validate(&bad, &reason) != 0, "reject 44100 Hz");
    bad = g;
    bad.period_frames = 0;
    CHECK(jts_ring_geometry_validate(&bad, &reason) != 0, "reject 0 period_frames");

    jts_ring_geometry_t wide = g;
    wide.sample_format = JTS_RING_SAMPLE_FORMAT_S32LE;
    CHECK(jts_ring_geometry_validate(&wide, &reason) == 0, "accept S32LE");
    wide = g;
    wide.channels = 6;
    CHECK(jts_ring_geometry_validate(&wide, &reason) == 0, "accept 6ch");
    wide.sample_format = JTS_RING_SAMPLE_FORMAT_S32LE;
    CHECK(jts_ring_geometry_validate(&wide, &reason) == 0, "accept S32LE x 6ch");
    wide.channels = JTS_RING_MAX_CHANNELS;
    CHECK(jts_ring_geometry_validate(&wide, &reason) == 0, "accept S32LE x 8ch (the ceiling)");
}

// GOLDEN BYTE-MATH TABLE. Each row is computed by hand rather than by
// re-deriving the formula the code uses, so a wrong stride cannot agree with a
// wrong expectation.
//
// The S16/6ch row is deliberate: with 2 channels an "S16" and a "2-channel"
// stride bug are indistinguishable (both give 4 bytes/frame), so a surviving
// hardcoded `* 2` channel stride is INVISIBLE to every 2-channel row. S16/6 is
// the row that catches it.
//
// The jasper-ring crate carries the matching table on the Rust side of the
// wire; keep the two row sets in step when either end changes, so the same
// numbers are asserted in both languages.
static void test_golden_byte_math_table(void) {
    struct {
        uint32_t sample_format;
        uint32_t channels;
        size_t bytes_per_sample;  // hand-computed
        size_t samples_per_slot;  // period_frames * channels
        size_t slot_bytes;        // samples_per_slot * bytes_per_sample
        size_t file_size;         // 128 + n_slots * slot_bytes
        const char *label;
    } rows[] = {
        // period_frames = 128, n_slots = 2 for every row.
        {JTS_RING_SAMPLE_FORMAT_S16LE, 2, 2, 256, 512, 128 + 2 * 512, "S16/2ch"},
        {JTS_RING_SAMPLE_FORMAT_S16LE, 6, 2, 768, 1536, 128 + 2 * 1536, "S16/6ch"},
        {JTS_RING_SAMPLE_FORMAT_S32LE, 2, 4, 256, 1024, 128 + 2 * 1024, "S32/2ch"},
        {JTS_RING_SAMPLE_FORMAT_S32LE, 4, 4, 512, 2048, 128 + 2 * 2048, "S32/4ch"},
        {JTS_RING_SAMPLE_FORMAT_S32LE, 6, 4, 768, 3072, 128 + 2 * 3072, "S32/6ch"},
        {JTS_RING_SAMPLE_FORMAT_S32LE, 8, 4, 1024, 4096, 128 + 2 * 4096, "S32/8ch"},
    };
    for (size_t i = 0; i < sizeof(rows) / sizeof(rows[0]); i++) {
        jts_ring_geometry_t g = {
            .rate = 48000,
            .channels = rows[i].channels,
            .sample_format = rows[i].sample_format,
            .period_frames = 128,
            .n_slots = 2,
        };
        char msg[128];
        const char *reason = NULL;
        snprintf(msg, sizeof(msg), "%s: inside the accept-set", rows[i].label);
        CHECK(jts_ring_geometry_validate(&g, &reason) == 0, msg);
        snprintf(msg, sizeof(msg), "%s: bytes_per_sample", rows[i].label);
        CHECK(jts_ring_bytes_per_sample(g.sample_format) == rows[i].bytes_per_sample, msg);
        snprintf(msg, sizeof(msg), "%s: samples_per_slot", rows[i].label);
        CHECK(jts_ring_samples_per_slot(&g) == rows[i].samples_per_slot, msg);
        snprintf(msg, sizeof(msg), "%s: slot_bytes", rows[i].label);
        CHECK(jts_ring_slot_bytes(&g) == rows[i].slot_bytes, msg);
        snprintf(msg, sizeof(msg), "%s: file_size", rows[i].label);
        CHECK(jts_ring_file_size(&g) == rows[i].file_size, msg);
    }

    // The JTS_RING_MAX_SLOT_BYTES boundary, both sides. 65536 bytes at 8ch S32
    // is 32 bytes/frame, so exactly 2048 frames is at the cap and 2049 is one
    // frame (32 bytes) over.
    jts_ring_geometry_t at_cap = {
        .rate = 48000,
        .channels = 8,
        .sample_format = JTS_RING_SAMPLE_FORMAT_S32LE,
        .period_frames = 2048,
        .n_slots = 2,
    };
    const char *reason = NULL;
    CHECK(jts_ring_slot_bytes(&at_cap) == 65536, "boundary: slot_bytes is exactly the cap");
    CHECK(jts_ring_slot_bytes(&at_cap) == (size_t)JTS_RING_MAX_SLOT_BYTES,
          "boundary: the cap constant is 65536");
    CHECK(jts_ring_geometry_validate(&at_cap, &reason) == 0,
          "boundary: exactly at the slot-bytes cap is accepted");

    jts_ring_geometry_t over_cap = at_cap;
    over_cap.period_frames = 2049;
    CHECK(jts_ring_slot_bytes(&over_cap) == 65536 + 32,
          "boundary: one frame over is 32 bytes over the cap");
    CHECK(jts_ring_geometry_validate(&over_cap, &reason) != 0,
          "boundary: one frame over the slot-bytes cap is rejected");
}

// The data-path half of the golden table: publish + consume must move
// slot_bytes of S32 6-channel payload with every byte in place. A copy path
// that still strided as i16/2ch would move a quarter of the slot and mismatch.
static void test_wide_slot_publish_consume_roundtrip(void) {
    char path[256];
    tmp_path(path, sizeof(path), "wide-roundtrip");
    jts_ring_geometry_t g = {
        .rate = 48000,
        .channels = 6,
        .sample_format = JTS_RING_SAMPLE_FORMAT_S32LE,
        .period_frames = 128,
        .n_slots = 2,
    };
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "wide writer open");
    CHECK(w.slot_bytes == 3072, "wide writer slot_bytes == 128 * 6 * 4");
    CHECK(w.map_len == jts_ring_file_size(&g), "wide map covers the whole file");

    jts_ring_reader_t r;
    CHECK(jts_ring_reader_open(path, &g, &r) == 0, "wide reader open");

    // Hand-computed, not read off `w` (the code under test): a mutation that
    // corrupts samples_per_slot must fail the assertion below, not silently
    // undersize this malloc into a heap overflow a few lines down.
    size_t n = 128 * 6; // period_frames * channels == 768 i32 samples
    CHECK(w.samples_per_slot == n, "wide writer samples_per_slot == 128 * 6");
    int32_t *payload = malloc(n * sizeof(int32_t));
    // Values outside the i16 range in every channel: a narrow copy path would
    // truncate them, and the per-channel offset makes a channel-stride bug
    // visible as a misplaced value rather than a lost one.
    for (size_t f = 0; f < g.period_frames; f++) {
        for (uint32_t c = 0; c < g.channels; c++) {
            payload[f * g.channels + c] = (int32_t)((f + 1) * 100000 + (int32_t)c);
        }
    }
    CHECK(jts_ring_writer_publish(&w, payload) == JTS_RING_PUBLISH_OK, "wide publish ok");

    int32_t *out = calloc(n, sizeof(int32_t));
    CHECK(jts_ring_reader_consume(&r, out) == JTS_RING_SLOT_FILLED, "wide consume filled");
    CHECK(memcmp(out, payload, n * sizeof(int32_t)) == 0,
          "wide payload roundtrips byte-for-byte (no narrow stride)");
    CHECK(jts_ring_reader_consume(&r, out) == JTS_RING_SLOT_EMPTY,
          "wide ring empty after drain");
    int all_zero = 1;
    for (size_t i = 0; i < n; i++)
        if (out[i] != 0) all_zero = 0;
    CHECK(all_zero, "wide empty read zero-fills every sample");

    free(payload);
    free(out);
    jts_ring_reader_close(&r);
    jts_ring_writer_close(&w);
    unlink(path);
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

    // Full ring, LIVE reader that never advances: publish waits, then DROPs
    // after the bounded tick cap rather than hanging.
    jts_ring_publish_result_t pr = jts_ring_writer_publish(&w, s);
    CHECK(pr == JTS_RING_PUBLISH_DROPPED, "full-ring bounded wait -> drop");
    CHECK(w.full_waits >= 1, "counted a full wait");

    int16_t *out = calloc(n, sizeof(int16_t));
    CHECK(reader_consume(&r, out) == 1, "consume one");
    CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish after consume");

    free(s);
    free(out);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_no_reader_free_run_drop(void) {
    // No reader ever attaches (reader_pid stays 0), so the writer fills the ring
    // then FREE-RUN DROPs rather than blocking. That is what keeps CamillaDSP
    // from wedging when outputd's flag is off, and what makes the
    // `aplay -D jts_ring_playback ... /dev/zero` resolvability probe terminate.
    char path[256];
    tmp_path(path, sizeof(path), "noreader");
    jts_ring_geometry_t g = proto_geometry();
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
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
// ALSA grants `transfer` (publish's only playback caller) at most `avail`
// frames, so the wedge these tests reproduce lives at the `avail` gate, not in
// publish: a test that calls publish UNCONDITIONALLY cannot reach it. The model
// reproduces the gate in two respects:
//
//   1. It calls the SHARED jts_ring_pointer_report (jts_ring_shm.h) — the exact
//      function the plugin returns from `pointer` — rather than hand-copying the
//      dual-mode/clamp logic, so a regression in pcm_jts_ring.c's core fails
//      `make test`.
//
//   2. It models ALSA's hw_ptr inference (snd_pcm_ioplug_hw_ptr_update):
//      `pointer()` returns a value mod buffer_size; ALSA computes
//        delta = (ret >= last_hw) ? ret - last_hw : buffer_size + ret - last_hw
//      and ADDS it to a running (boundary-space) hw_ptr, then stores ret as
//      last_hw. avail is derived from THAT accumulated hw_ptr, which is the
//      layer the alias wedges live in: a raw report advance of exactly
//      buffer_size makes ret == last_hw (mod buffer_size) -> delta 0 -> the
//      accumulated hw_ptr falls a lap behind -> avail pins at 0. Reading avail
//      off the raw pre-modulo hw_ptr instead cannot SEE the alias.
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
    // Pacing governor. The plugin samples CLOCK_MONOTONIC_RAW in its `pointer`
    // prologue; the model lets the TEST advance `now_ns` instead, which is what
    // makes a rate property checkable without sleeping. pace_nominal 0 =>
    // ungoverned.
    int pace_nominal;
    uint64_t now_ns;
} ioplug_model_t;

// One `pointer` read + ALSA's hw_ptr inference, EXACTLY as
// snd_pcm_ioplug_hw_ptr_update does it (no BOUNDARY_WA flag, so wrap_point ==
// buffer_size).
static void ioplug_model_pointer_tick(ioplug_model_t *m, jts_ring_writer_t *w) {
    jts_ring_pointer_inputs_t in = {
        .appl_frames = m->appl_frames,
        .occupancy_slots = jts_ring_writer_occupancy_slots(w),
        .stage_frames = 0, // core-only model stages nothing
        .period_frames = m->period,
        .buffer_size = m->buffer_size,
        .reader_live = jts_ring_writer_reader_is_live(w),
        .pace_nominal = m->pace_nominal,
        .now_ns = m->now_ns,
        .rate = 48000,
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
// raw pre-modulo value): buffer_size - (appl_ptr - hw_ptr).
static uint64_t ioplug_model_avail(ioplug_model_t *m, jts_ring_writer_t *w) {
    ioplug_model_pointer_tick(m, w);
    uint64_t used = (m->appl_frames >= m->alsa_hw_ptr) ? (m->appl_frames - m->alsa_hw_ptr)
                                                       : 0; // frames queued, not drained
    return (used <= m->buffer_size) ? (m->buffer_size - used) : 0;
}

static ioplug_model_t ioplug_model_new(const jts_ring_geometry_t *g) {
    ioplug_model_t m;
    memset(&m, 0, sizeof(m));
    m.buffer_size = (uint64_t)g->n_slots * g->period_frames;
    m.period = g->period_frames;
    return m;
}

static void test_no_reader_pointer_keeps_advancing(void) {
    // Gate-faithful: `avail` comes from the pointer path exactly as
    // pcm_jts_ring.c computes it, and publish runs ONLY when a whole period of
    // avail exists (the ALSA discipline). With a dead reader the dual-mode
    // pointer discounts in_flight to 0, which is what keeps avail open on a full
    // ring; an honest in_flight pins avail at 0 the moment occupancy hits
    // n_slots and this loop makes no progress at all.
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

    int publishes = 0;
    uint64_t prev_hw_ptr = 0;
    for (int tick = 0; tick < 200; tick++) {
        uint64_t avail = ioplug_model_avail(&m, &w);
        CHECK(avail >= period,
              "readerless avail stays >= one period (dual-mode pointer, no wedge)");
        uint64_t hw = m.alsa_hw_ptr; // ALSA's accumulated hw_ptr after this read
        CHECK(hw >= prev_hw_ptr, "hw_ptr monotonic non-decreasing (never back-jumps)");
        prev_hw_ptr = hw;
        // ALSA would transfer up to `avail` and the ioplug publishes whole slots,
        // so model one period per tick.
        jts_ring_publish_result_t pr = jts_ring_writer_publish(&w, s);
        CHECK(pr == JTS_RING_PUBLISH_OK || pr == JTS_RING_PUBLISH_DROPPED,
              "publish returns (never hangs) with no reader");
        m.appl_frames += period;
        publishes++;
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
    // On a full readerless ring the gate must be OPEN with zero writer activity:
    // the dual-mode pointer discounts the (unreadable) published slots to 0
    // in-flight. Probing avail without any publish isolates that to the
    // pointer/avail path.
    char path[256];
    tmp_path(path, sizeof(path), "gatefaithful");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");

    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));
    const uint32_t period = g.period_frames;

    // Tick the pointer on every publish so ALSA's accumulated hw_ptr keeps up
    // with appl: the dead-mode discount makes honest hw_ptr == appl each tick,
    // and the clamp lets it track since each step is one period < buffer_size.
    ioplug_model_t m = ioplug_model_new(&g);
    for (uint32_t i = 0; i < g.n_slots + 2; i++) { // +2 = force the full state
        (void)jts_ring_writer_publish(&w, s);
        m.appl_frames += period;
        (void)ioplug_model_avail(&m, &w); // tick the pointer / accumulate hw_ptr
    }
    CHECK(jts_ring_writer_occupancy_slots(&w) == (uint64_t)g.n_slots,
          "ring full (occupancy == n_slots) before the no-publish avail probe");
    CHECK(!jts_ring_writer_reader_is_live(&w), "reader is dead (never attached)");

    // With appl_frames frozen and the reader dead, honest hw_ptr == appl is also
    // frozen, so the pointer returns the SAME value each tick and ALSA's
    // mod-buffer delta inference adds 0 (ret == last_hw). The gate must stay open
    // at that steady value: a dead-mode discount delivering ONE full-buffer jump
    // on the flip would alias to delta 0 and park avail at 0 permanently.
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
    // A reader that appears MID-WRITE must not make ALSA's hw_ptr jump backward
    // (ALSA requires it monotonic) AND must not alias a full-buffer step to a
    // zero delta (which would pin avail at 0 forever). While dead, the pointer
    // runs hw_ptr near appl (in_flight = 0). On attach the reader resyncs
    // read_seq = write_seq (occupancy -> 0), so honest hw_ptr == appl. Then,
    // before the reader consumes, occupancy re-grows and honest hw_ptr would
    // step BACK; the reported-position clamp holds it.
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

    // Phase 1: readerless free-run. hw_ptr tracks appl (dead -> in_flight 0) one
    // period per tick, so every step is < buffer and no step can alias.
    uint64_t prev = 0;
    for (int i = 0; i < 12; i++) {
        (void)jts_ring_writer_publish(&w, s);
        m.appl_frames += period;
        uint64_t avail = ioplug_model_avail(&m, &w); // ticks + accumulates hw_ptr
        CHECK(avail >= period, "phase1 avail open (dead reader, no wedge)");
        CHECK(m.alsa_hw_ptr >= prev, "phase1 hw_ptr monotonic (dead reader)");
        prev = m.alsa_hw_ptr;
    }

    test_reader_t r;
    reader_attach(&r, &w);
    CHECK(jts_ring_writer_occupancy_slots(&w) == 0, "occupancy collapses to 0 on attach");
    CHECK(jts_ring_writer_reader_is_live(&w), "reader now live");
    uint64_t avail_first_live = ioplug_model_avail(&m, &w);
    CHECK(avail_first_live > 0, "dead->live transition keeps avail open (no alias)");
    CHECK(m.alsa_hw_ptr >= prev, "dead->live transition is monotonic (no back-jump)");
    prev = m.alsa_hw_ptr;

    // Phase 3: the writer publishes ahead of the reader. Honest hw_ptr WOULD
    // step back one period per publish; the clamp holds the reported position at
    // its floor. A full ring with a LIVE lagging reader is legitimately
    // not-writable — honest back-pressure, not a wedge — so only monotonicity is
    // asserted here; the alias tests own the avail-reopens property.
    for (int i = 0; i < 6; i++) {
        (void)jts_ring_writer_publish(&w, s); // occupancy climbs (reader idle)
        m.appl_frames += period;
        (void)ioplug_model_avail(&m, &w);
        CHECK(m.alsa_hw_ptr >= prev, "phase3 hw_ptr never regresses while reader lags (clamped)");
        prev = m.alsa_hw_ptr;
    }
    while (jts_ring_writer_occupancy_slots(&w) > 0) {
        CHECK(reader_consume(&r, out) == 1, "reader drains a slot");
        (void)ioplug_model_avail(&m, &w);
        CHECK(m.alsa_hw_ptr >= prev, "drain phase hw_ptr monotonic non-decreasing");
        prev = m.alsa_hw_ptr;
    }
    // avail settles to buffer minus ALSA's one-period first-read seed lag — a
    // constant of ALSA's own hw_ptr model, not a wedge — so the assertion is
    // near-full and STABLE rather than an exact == buffer.
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

// --- mod-buffer full-lap ALIAS regressions ---
//
// Each of the three below constructs the exact state where the HONEST reported
// position would advance by ~one full buffer between two consecutive pointer
// reads. Returned mod buffer_size, that aliases to a ZERO (or backward) delta in
// ALSA's snd_pcm_ioplug_hw_ptr_update, pinning avail at 0 permanently. The
// reported-position clamp (jts_ring_pointer_report) spreads the catch-up over
// several sub-buffer ticks so ALSA always sees a positive delta and avail stays
// open. Each test also computes what an UNCLAMPED honest pointer WOULD have
// returned at the alias step and asserts it aliases, so the clamp — not the
// test's own arithmetic — is what keeps these green.

// Helper: what ALSA's mod-buffer delta inference yields for two raw reported
// positions (verbatim snd_pcm_ioplug_hw_ptr_update, no BOUNDARY_WA).
static uint64_t alsa_delta(uint64_t prev_raw, uint64_t cur_raw, uint64_t buffer) {
    uint64_t prev = prev_raw % buffer;
    uint64_t cur = cur_raw % buffer;
    return (cur >= prev) ? (cur - prev) : (buffer + cur - prev);
}

static void test_alias_live_reader_drain_gap(void) {
    // A LIVE reader drains a full ring during an app-side gap >= one buffer
    // duration. Before the gap: occupancy == n_slots, honest hw_ptr lags
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

    for (uint32_t i = 0; i < g.n_slots; i++) {
        CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish to brim");
        m.appl_frames += period;
        (void)ioplug_model_avail(&m, &w);
    }
    CHECK(jts_ring_writer_occupancy_slots(&w) == (uint64_t)g.n_slots, "ring full");
    uint64_t raw_before = m.ptr.last_reported;      // honest hw_ptr lags appl by buffer
    uint64_t hw_before = m.alsa_hw_ptr;

    // The app-side GAP: no pointer read happens while the reader drains the WHOLE
    // ring — the window where the app is outside a PCM call.
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

    int saw_progress = 0;
    for (int tick = 0; tick < (int)g.n_slots + 2; tick++) {
        uint64_t hw_prev = m.alsa_hw_ptr;
        uint64_t avail = ioplug_model_avail(&m, &w);
        CHECK(avail > 0, "clamped: avail reopens after the full-drain gap (no alias wedge)");
        CHECK(m.alsa_hw_ptr >= hw_prev, "clamped: hw_ptr monotonic across catch-up");
        if (m.alsa_hw_ptr > hw_before) saw_progress = 1;
    }
    CHECK(saw_progress, "clamped: hw_ptr made real forward progress (unwedged)");
    CHECK(m.alsa_hw_ptr - hw_before == m.buffer_size,
          "clamped: the full buffer of drain is eventually reflected (spread, not lost)");

    free(s);
    free(out);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_alias_dead_flip_at_full_ring(void) {
    // The reader dies MID-PLAY at a full ring (occupancy == n_slots) — the
    // operational outputd-restart case. While live+full, honest hw_ptr lags
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
    // dead->live recovery. A readerless free-run stream (dead-mode,
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

    for (int i = 0; i < 12; i++) {
        uint64_t avail = ioplug_model_avail(&m, &w);
        CHECK(avail >= period, "dead free-run avail open");
        (void)jts_ring_writer_publish(&w, s);
        m.appl_frames += period;
    }
    uint64_t hw_before = m.alsa_hw_ptr;

    test_reader_t r;
    reader_attach(&r, &w);
    CHECK(jts_ring_writer_reader_is_live(&w), "reader live after attach");
    uint64_t prev = m.alsa_hw_ptr;
    for (int i = 0; i < (int)g.n_slots; i++) {
        (void)jts_ring_writer_publish(&w, s); // reader idle: occupancy climbs
        m.appl_frames += period;
        (void)ioplug_model_avail(&m, &w);
        // The reader is idle, so the ring legitimately fills and avail shrinks
        // toward 0 (honest back-pressure). Only hw_ptr monotonicity across the
        // discount flip is asserted here; the paced phase below proves avail
        // reopens.
        CHECK(m.alsa_hw_ptr >= prev, "recovery: hw_ptr never regresses");
        prev = m.alsa_hw_ptr;
    }

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
    // After a stretch of readerless free-run the writer has advanced read_seq on
    // the absent reader's behalf; a reader attaching then must resync to the
    // writer tip with no lost-lap corruption.
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
    CHECK(reader_consume(&r, out) == 0, "empty read immediately after resync");

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
    uint64_t wseq1 = w1.write_seq;
    // Writer 1 is CLOSED first: that is what a reattach IS in production (the
    // old writer's process exits, or the ioplug closes its PCM). A writer holds
    // an EXCLUSIVE flock for the life of its mapping, so two live writers on one
    // ring is refused rather than modelled; the epoch and write_seq both live in
    // the HEADER and survive the close.
    jts_ring_writer_close(&w1);
    h = NULL; // w1's mapping is gone; do not read through it again.
    jts_ring_writer_t w2;
    CHECK(jts_ring_writer_open(path, &g, &w2) == 0, "writer 2 attach");
    uint64_t e2 = atomic_load_explicit(
        &((jts_ring_header_t *)w2.base)->writer_epoch, memory_order_acquire);
    CHECK(e2 > e1, "epoch bumped on second writer attach");
    // write_seq is file-lifetime monotonic: the second writer continues from it.
    CHECK(w2.write_seq == wseq1, "second writer continues from stored write_seq");
    jts_ring_writer_close(&w2);
    unlink(path);
}

static void test_geometry_mismatch_is_fatal(void) {
    char path[256];
    tmp_path(path, sizeof(path), "mismatch");
    jts_ring_geometry_t g = proto_geometry();
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open 128");
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
    // The writer must mkdir -p its parent before O_EXCL create. On a fresh box
    // (or after disarm.sh's `rm -rf /dev/shm/jts-ring`) the directory does not
    // exist; without ensure_parent_dir the create fails ENOENT and arm.sh's
    // aplay probe dies before outputd ever runs.
    char dir[256];
    char path[320];
    snprintf(dir, sizeof(dir), "/tmp/jts-ring-ctest-%d-mkparent", (int)getpid());
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
    // A holds the transaction lock after O_EXCL create but before ftruncate; B
    // and C both prove they are blocked on that SAME adjacent lock. Releasing A
    // lets it initialize + verify pathname ownership, after which B and C attach
    // serially. Nobody may classify A's zero-size live inode as torn or replace
    // it.
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
        close(torn_fd);
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
                if (write_bytes(results[1], &observed, sizeof(observed)) < 0) rc = -1;
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
            if (report_fd_identity(results[1], w.fd) < 0) rc = -1;
            jts_ring_writer_close(&w);
        }
        _exit(rc == 0 ? 0 : 7);
    }
    pid_t c = fork();
    if (c == 0) {
        jts_ring_writer_t w;
        int rc = jts_ring_writer_open(path, &g, &w);
        if (rc == 0) {
            if (report_fd_identity(results[1], w.fd) < 0) rc = -1;
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
    close(torn_fd);
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
    if (stat_rc != 0) {
        close(fd);
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
    close(fd);
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
    if (stat_rc != 0) {
        close(fd);
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
    close(fd);
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
    // jts_ring_writer_can_accept must be TRUE when space exists,
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

    CHECK(jts_ring_writer_can_accept(&w) == 1, "empty ring accepts");

    test_reader_t r;
    reader_attach(&r, &w); // live reader, fresh heartbeat
    CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish 0");
    CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish 1");
    CHECK(jts_ring_writer_occupancy_slots(&w) == 2, "full");
    CHECK(jts_ring_writer_can_accept(&w) == 0, "full+live-reader does NOT accept");

    jts_ring_header_t *h = (jts_ring_header_t *)w.base;
    atomic_store_explicit(&h->reader_heartbeat_ns, 1, memory_order_relaxed);
    CHECK(jts_ring_writer_can_accept(&w) == 1, "full+dead-reader accepts (free-run drop)");

    free(s);
    jts_ring_writer_close(&w);
    unlink(path);
}

static void test_deep_ring_16_slots(void) {
    // The 16-slot ceiling gives CamillaDSP's playback BufferManager an ALSA
    // buffer (n_slots * period_frames = 16*128 = 2048 frames) that clears its
    // negotiated buffer and target_level.
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

    for (uint32_t i = 0; i < 16; i++) {
        CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish to brim");
        CHECK(jts_ring_writer_occupancy_slots(&w) == (uint64_t)(i + 1), "occupancy climbs");
    }
    CHECK(jts_ring_writer_can_accept(&w) == 0, "16/16 full+live does NOT accept");
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
    // "everything accepted is already played". An accept-tracking pointer makes
    // camilla see delay ~= 0 and flap between stalled and resumed.
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

    for (int i = 0; i < 3; i++) {
        CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "publish");
        appl_frames += period;
    }
    uint64_t in_flight = jts_ring_writer_occupancy_slots(&w) * (uint64_t)period;
    CHECK(in_flight == 3ull * period, "in_flight == 3 periods before any drain");
    uint64_t hw_ptr = appl_frames - in_flight;
    CHECK(hw_ptr == 0, "hw_ptr still 0 (reader has drained nothing)");

    CHECK(reader_consume(&r, out) == 1, "drain one");
    in_flight = jts_ring_writer_occupancy_slots(&w) * (uint64_t)period;
    CHECK(in_flight == 2ull * period, "in_flight fell one period");
    hw_ptr = appl_frames - in_flight;
    CHECK(hw_ptr == (uint64_t)period, "hw_ptr advanced one period on drain");

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
    // The ioplug's `.drain` callback publishes the partial staged slot
    // (zero-padding the remainder) so drain can reach an empty ring — without
    // it, a partially-staged slot leaves `delay` pinned above 0 and ALSA's drain
    // loop HANGS. The flush + bounded-wait loop itself is ALSA-linked and
    // Pi-only; only its building blocks are host-testable.
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

    CHECK(jts_ring_writer_publish(&w, stage) == JTS_RING_PUBLISH_OK,
          "padded partial slot publishes as a whole slot");
    CHECK(jts_ring_writer_occupancy_slots(&w) == 1, "one slot in flight after flush");

    int16_t *out = calloc(n, sizeof(int16_t));
    CHECK(reader_consume(&r, out) == 1, "reader consumes the flushed slot");
    int real_ok = 1, pad_ok = 1;
    for (uint32_t f = 0; f < k; f++)
        for (uint32_t c = 0; c < g.channels; c++)
            if (out[f * g.channels + c] != (int16_t)(f + 1)) real_ok = 0;
    for (uint32_t f = k; f < period; f++)
        for (uint32_t c = 0; c < g.channels; c++)
            if (out[f * g.channels + c] != 0) pad_ok = 0;
    CHECK(real_ok, "flushed slot preserves the real (pre-pad) frames");
    CHECK(pad_ok, "flushed slot zero-pads the tail (no stale/garbage frames)");
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
    // The C-writer <-> C-reader wire format is the same format the Rust writer
    // emits; that half is proven on-Pi by the reader bench.
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
    CHECK(jts_ring_reader_consume(&r, out) == JTS_RING_SLOT_EMPTY, "empty after drain");
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

    // reader 1's pid == getpid() here (same process), so foreign_reader_is_live returns 0
    // for OUR pid — that is correct for re-prepare in the SAME process. To model a
    // DIFFERENT process holding the ring, overwrite reader_pid with a foreign live pid (any
    // nonzero != getpid()) and a fresh heartbeat, then try to open: it must return -EBUSY.
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

static void test_writer_ebusy_second_writer(void) {
    // The SPSC guard's WRITER half — the mirror of
    // test_reader_ebusy_second_reader above.
    //
    // A renderer lane's ring is written by a renderer whose ALSA device name is
    // public and DELIBERATELY probed: jasper-doctor runs `aplay -D <device>` as
    // the renderer user on every install. Without this guard that probe's open
    // would SUCCEED and two writers advancing write_seq would interleave their
    // slots, injecting the probe's silence into live music. On an snd-aloop lane
    // the same probe bounces off with EBUSY, which the doctor accepts as proof
    // the incumbent owns the lane.
    char path[256];
    tmp_path(path, sizeof(path), "wtr-ebusy");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 4;

    jts_ring_writer_t w1;
    CHECK(jts_ring_writer_open(path, &g, &w1) == 0, "writer 1 attaches");
    jts_ring_header_t *h = (jts_ring_header_t *)w1.base;
    // Publish a slot so write_seq/epoch are nonzero and a corrupting second open
    // would be observable.
    unsigned char slot[JTS_RING_MAX_SLOT_BYTES];
    memset(slot, 0x5A, jts_ring_slot_bytes(&g));
    (void)jts_ring_writer_publish(&w1, slot);
    uint64_t wseq_before = atomic_load_explicit(&h->write_seq, memory_order_relaxed);
    uint64_t epoch_before =
        atomic_load_explicit(&h->writer_epoch, memory_order_relaxed);
    CHECK(wseq_before > 0, "writer 1 advanced write_seq");

    // --- (1) THE LOCK, in isolation --------------------------------------
    //
    // Writer 1 is still open and its pid is OURS, so `foreign_writer_is_live`
    // returns 0 (a same-process re-prepare is deliberately not a pid conflict).
    // The ONLY thing that can refuse here is the fd-scoped flock — which is the
    // point: this is the `aplay -D` probe arriving while the renderer holds its
    // ring.
    jts_ring_writer_t w2;
    memset(&w2, 0, sizeof(w2));
    // TIME the refusal. The wait's only requirement is that it is FINITE (see
    // acquire_writer_lock), and nothing else in this suite pins that: raising
    // the ceiling 10x leaves every assertion green and merely slower, and
    // removing the deadline entirely hangs the whole binary.
    //
    // The bound is deliberately generous. This is ONE lock wait
    // (JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS = 500 ms; measured ~501 ms here), and
    // 1.5 s leaves room for a loaded CI box without admitting a 10x regression.
    uint64_t t0 = jts_ring_monotonic_ns();
    int rc = jts_ring_writer_open(path, &g, &w2);
    uint64_t elapsed_ms = (jts_ring_monotonic_ns() - t0) / 1000000ull;
    CHECK(rc == -EBUSY, "a second live writer is refused with -EBUSY by the lock");
    CHECK(elapsed_ms < 1500,
          "the writer-lock wait must be BOUNDED — a refusal took too long, "
          "which means the deadline was raised or removed (an unbounded wait "
          "hangs the daemon inside snd_pcm_prepare, not just this test)");
    CHECK(elapsed_ms >= 400,
          "...and it must actually WAIT: a refusal that returns instantly means "
          "the bounded wait became fail-fast again, which spuriously fails the "
          "transient concurrent-open case this suite also covers");
    CHECK(w2.base == NULL, "refused writer struct left detached");
    CHECK(w2.fd == -1, "refused writer fd left detached");
    CHECK(atomic_load_explicit(&h->write_seq, memory_order_relaxed) == wseq_before,
          "EBUSY did not clobber write_seq");
    CHECK(atomic_load_explicit(&h->writer_epoch, memory_order_relaxed) == epoch_before,
          "EBUSY did not bump writer_epoch (a reader must not see a phantom "
          "reattach because someone probed the device)");

    // --- (2) A PAUSED-BUT-OPEN writer stays protected ---------------------
    //
    // `writer_heartbeat_ns` is stamped on PUBLISH paths only, so a renderer that
    // holds its PCM open and stops writing — an ordinary pause — goes
    // heartbeat-stale within JTS_RING_WRITER_LIVENESS_TIMEOUT_NS. Under the
    // heartbeat test alone its ring would become takeable while it still owned
    // the device, and a probe could then interleave slots into a stream it
    // resumes into. The lock is held for the LIFE OF THE MAPPING, so it survives
    // the pause.
    atomic_store_explicit(&h->writer_heartbeat_ns, 1, memory_order_relaxed); // ancient
    jts_ring_writer_t w_paused;
    memset(&w_paused, 0, sizeof(w_paused));
    CHECK(jts_ring_writer_open(path, &g, &w_paused) == -EBUSY,
          "a PAUSED but still-open writer keeps its ring (the heartbeat has gone "
          "stale; the fd-scoped lock has not)");
    CHECK(w_paused.base == NULL, "refused paused-case writer left detached");

    // --- (3) A DEAD writer's ring IS reclaimable --------------------------
    //
    // Death closes the fd and the kernel drops the flock, so the reclaim
    // property survives without depending on heartbeat timing — a restarted
    // renderer takes its own lane back with no operator. Closing is exactly what
    // a crash/SIGKILL does to the lock.
    jts_ring_writer_close(&w1);
    jts_ring_writer_t w3;
    CHECK(jts_ring_writer_open(path, &g, &w3) == 0,
          "a dead writer's ring is reclaimable");
    jts_ring_header_t *h3 = (jts_ring_header_t *)w3.base;
    CHECK(atomic_load_explicit(&h3->writer_pid, memory_order_relaxed)
              == (uint64_t)getpid(),
          "fresh attach took ownership (our pid)");
    // Assert the WRITER'S OWN mirror, not the header's. The header value is
    // never reset by an open, so testing it is true by construction and admits
    // a writer that zeroed `out->write_seq` — which would make the next
    // publish compute a huge `write_seq - read_seq` occupancy and free-run
    // drop-oldest against a reader that is perfectly healthy.
    CHECK(w3.write_seq == wseq_before,
          "the takeover writer's own seq continues from the header rather than "
          "resetting to 0");
    CHECK(atomic_load_explicit(&h3->write_seq, memory_order_relaxed) == wseq_before,
          "the header's file-lifetime write_seq is untouched by a takeover");

    // --- (4) The HEARTBEAT guard still covers the cross-implementation case -
    //
    // A Rust `RingWriter` does not take this lock, so on a ring whose writer is
    // the Rust side the heartbeat is the only signal available and the guard
    // must still fire. Model that: release the lock (close), then stamp a
    // FOREIGN live pid + fresh heartbeat through a reader's mapping.
    jts_ring_writer_close(&w3);
    jts_ring_reader_t rr;
    CHECK(jts_ring_reader_open(path, &g, &rr) == 0, "reader mapping for step 4");
    jts_ring_header_t *hf = (jts_ring_header_t *)rr.base;
    uint64_t foreign = (uint64_t)getpid() + 1; // definitely not us
    atomic_store_explicit(&hf->writer_pid, foreign, memory_order_relaxed);
    atomic_store_explicit(&hf->writer_heartbeat_ns, jts_ring_monotonic_ns(),
                          memory_order_relaxed);
    jts_ring_writer_t w4;
    memset(&w4, 0, sizeof(w4));
    CHECK(jts_ring_writer_open(path, &g, &w4) == -EBUSY,
          "a live FOREIGN writer with no lock (the Rust writer) is still refused "
          "by the heartbeat guard");
    CHECK(atomic_load_explicit(&hf->writer_pid, memory_order_relaxed) == foreign,
          "EBUSY did not clobber the incumbent writer_pid");

    // --- (5) A DEAD foreign writer must NOT block -------------------------
    //
    // The other half of the heartbeat guard, and the one that keeps a ring
    // RECLAIMABLE: a foreign pid with a STALE heartbeat is a crashed renderer or
    // a ring left behind by a previous boot, and it must not wedge the lane
    // until an operator intervenes. Same foreign pid as step 4, ancient
    // heartbeat — the only thing that changed is liveness.
    atomic_store_explicit(&hf->writer_heartbeat_ns, 1, memory_order_relaxed);
    jts_ring_writer_t w5;
    CHECK(jts_ring_writer_open(path, &g, &w5) == 0,
          "a DEAD foreign writer (stale heartbeat) does not block a fresh attach");
    CHECK(atomic_load_explicit(&hf->writer_pid, memory_order_relaxed)
              == (uint64_t)getpid(),
          "the fresh attach took ownership from the dead foreign writer");
    jts_ring_writer_close(&w5);

    jts_ring_reader_close(&rr);
    unlink(path);
}

static void test_writer_lock_unopenable_fails_open_and_is_logged(void) {
    // The -2 path: the lock FILE cannot be opened at all. Reachable in
    // production when an out-of-unit first creator (the doctor's sudo aplay
    // probe, an operator shell — neither carries UMask=0007) lands the lock file
    // 0640 under a different uid; the renderer then takes EACCES forever, and
    // the sticky bit stops it deleting the file, so only a reboot clears tmpfs.
    //
    // We FAIL OPEN there — refusing would take a renderer down over a lock file
    // — which means the ring silently loses fd-scoped exclusivity. That is
    // exactly why it must be LOUD: this pins the journal line, because a box in
    // that state is otherwise indistinguishable from a healthy one.
    char path[256];
    tmp_path(path, sizeof(path), "wtr-lock-unopenable");
    char lock_path[320];
    snprintf(lock_path, sizeof(lock_path), "%s%s", path,
             JTS_RING_WRITER_LOCK_SUFFIX);
    char log_path[320];
    snprintf(log_path, sizeof(log_path), "%s.log", path);
    unlink(lock_path);
    unlink(log_path);

    // Make the lock path unopenable for THIS process by putting a directory
    // where the file must go: open(O_RDWR) on a directory returns EISDIR.
    CHECK(mkdir(lock_path, 0700) == 0, "stage an unopenable writer-lock path");

    jts_ring_geometry_t g = proto_geometry();
    int log_fd = open(log_path, O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0660);
    CHECK(log_fd >= 0, "open fail-open event capture");
    int saved_stderr = dup(STDERR_FILENO);
    CHECK(saved_stderr >= 0, "save stderr");
    jts_ring_writer_t w;
    int rc = -1;
    if (log_fd >= 0 && saved_stderr >= 0) {
        fflush(stderr);
        CHECK(dup2(log_fd, STDERR_FILENO) >= 0, "capture fail-open event");
        rc = jts_ring_writer_open(path, &g, &w);
        fflush(stderr);
        CHECK(dup2(saved_stderr, STDERR_FILENO) >= 0, "restore stderr");
    }

    CHECK(rc == 0, "an unopenable lock file FAILS OPEN rather than refusing "
                   "(a renderer must not die over a lock file)");
    CHECK(w.writer_lock_fd == -1,
          "the fail-open writer records that it holds NO lock, so the sentinel "
          "cannot be mistaken for a held fd (0 would be stdin)");

    if (log_fd >= 0) {
        CHECK(lseek(log_fd, 0, SEEK_SET) == 0, "rewind fail-open event");
        char log_buf[1024] = {0};
        ssize_t got = read(log_fd, log_buf, sizeof(log_buf) - 1);
        CHECK(got > 0 &&
                  strstr(log_buf, "event=jts_ring.writer.lock_unavailable") != NULL,
              "losing fd-scoped exclusivity emits a stable, greppable event — "
              "a box running without it must be VISIBLE");
        // ...and the errno in it must be the REAL cause, not whatever close(2)
        // last set. A directory where the lock file belongs makes open(O_RDWR)
        // fail EISDIR; an operator reading `errno=0` (or a stale EBADF) would
        // be sent looking in the wrong place entirely.
        char want_errno[32];
        snprintf(want_errno, sizeof(want_errno), "errno=%d", EISDIR);
        CHECK(got > 0 && strstr(log_buf, want_errno) != NULL,
              "the fail-open event must carry the REAL errno (EISDIR here); "
              "close(2) clobbers errno, so every -2 path has to preserve it");
    }

    if (rc == 0) jts_ring_writer_close(&w);
    if (saved_stderr >= 0) close(saved_stderr);
    if (log_fd >= 0) close(log_fd);
    rmdir(lock_path);
    unlink(log_path);
    unlink(path);
}

static void test_writer_lock_survives_a_sigkilled_incumbent(void) {
    // SIGKILL truth. The LOCK is windowless — the kernel drops it with the fd,
    // so a killed writer's ring is immediately claimable. The SECONDARY
    // heartbeat guard is NOT: SIGKILL leaves writer_pid stamped and the
    // heartbeat frozen at its last publish, so for up to
    // JTS_RING_WRITER_LIVENESS_TIMEOUT_NS a fresh writer is still refused.
    //
    // That is the real shape (a close() model would clear writer_pid and hide
    // it), and it is why a ring-writing renderer's RestartSec must exceed that
    // window: it moved from the lock to the secondary guard, it did not vanish.
    char path[256];
    tmp_path(path, sizeof(path), "wtr-sigkill");
    jts_ring_geometry_t g = proto_geometry();

    // Model the survivor state directly: no lock held (the kill dropped it),
    // but a FOREIGN pid stamped with a FRESH heartbeat.
    jts_ring_writer_t seed;
    CHECK(jts_ring_writer_open(path, &g, &seed) == 0, "seed the ring");
    jts_ring_writer_close(&seed);

    jts_ring_reader_t rr;
    CHECK(jts_ring_reader_open(path, &g, &rr) == 0, "reader mapping");
    jts_ring_header_t *h = (jts_ring_header_t *)rr.base;
    uint64_t killed = (uint64_t)getpid() + 11; // a pid that is not us
    atomic_store_explicit(&h->writer_pid, killed, memory_order_relaxed);
    atomic_store_explicit(&h->writer_heartbeat_ns, jts_ring_monotonic_ns(),
                          memory_order_relaxed);

    jts_ring_writer_t w;
    memset(&w, 0, sizeof(w));
    CHECK(jts_ring_writer_open(path, &g, &w) == -EBUSY,
          "a SIGKILLed incumbent's frozen-fresh heartbeat still refuses a new "
          "writer even though its lock is already free — the secondary guard's "
          "<=2s window is real, and this is where RestartSec matters");

    atomic_store_explicit(&h->writer_heartbeat_ns, 1, memory_order_relaxed);
    CHECK(jts_ring_writer_open(path, &g, &w) == 0,
          "past the liveness window the killed writer's ring is reclaimable");
    jts_ring_writer_close(&w);

    jts_ring_reader_close(&rr);
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

    // Writer 1 is CLOSED first, which is what a reattach actually is in
    // production: the old writer's process exits (or the ioplug closes its PCM)
    // and a new one opens. A writer holds an EXCLUSIVE flock for the life of its
    // mapping, so two live writers on one ring is refused rather than simulated;
    // the epoch lives in the HEADER and survives the close.
    jts_ring_writer_close(&w1);
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
// appl. The service work lives in cap_model_poll_arm, which
// cap_model_poll_then_avail runs before the avail read, mirroring the ALSA rw
// loop's poll-then-pointer interleave.
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
// runs capture_service_tick (drain + arm + resync); keeping this call pure and
// driving the service work through cap_model_poll_arm
// (cap_model_poll_then_avail) preserves the ALSA rw-loop ordering the silence
// tests depend on: an initial `pointer` read (baseline, pending == 0) BEFORE the
// first arming, so the first avail read establishes hw_ptr == 0 and armed
// silence only ever shows up as a POSITIVE delta on a later read. That stays
// faithful because neither arming nor a resync can fire at baseline (writer
// live, occupancy 0); a real plugin pointer at baseline is likewise a no-op
// service tick.
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
    m->destage_frames -= m->period; // one whole period consumed
    m->appl_frames += m->period;
    return 1;
}

static void test_capture_pointer_advances_on_publish(void) {
    // The core capture-pointer honesty: hw_ptr advances on the WRITER's PUBLISH,
    // so ALSA's capture avail = readable frames.
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

    for (int i = 0; i < 3; i++) {
        jts_ring_writer_publish(&w, s);
    }
    uint64_t avail = 0;
    for (int tick = 0; tick < 6; tick++) avail = cap_model_avail(&m, &r); // let the clamp catch up
    CHECK(avail == 3 * (uint64_t)g.period_frames, "avail == 3 periods after 3 publishes");

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
    // Mirror of the playback drain-gap: while the app is mid-gap (no pointer
    // read), the WRITER publishes a full buffer of slots.
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
    // Mirror of the playback dead-flip: the ring is full of unread slots and the
    // WRITER dies. The app must keep pulling those
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
    // Writer dies, app free-runs on fabricated silence, then a NEW writer
    // reattaches (epoch++). hw_ptr must never regress
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

    // Writer 1 DIES, modelled by CLOSING the writer rather than by stamping a
    // stale heartbeat under a still-open mapping. That is what dying is: the
    // process exits, its fd closes, and the kernel drops the writer's flock —
    // the property that lets a new writer reclaim the ring without waiting on
    // heartbeat timing. The reader therefore sees a dead writer via the
    // `pid == 0` branch of `writer_is_live` (close clears writer_pid), not the
    // heartbeat-age branch; test_capture_alias_writer_death_flip covers that
    // branch by stamping an ancient heartbeat without closing.
    jts_ring_writer_close(&w1);
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

    jts_ring_writer_t w2;
    CHECK(jts_ring_writer_open(path, &g, &w2) == 0, "writer 2 reattach (epoch++)");
    for (int i = 0; i < 3; i++) {
        mark_slot(s, n, (int16_t)(2000 + i));
        jts_ring_writer_publish(&w2, s);
    }
    CHECK(jts_ring_reader_writer_is_live(&r), "writer live again after reattach");

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

    CHECK(cap_model_read_period(&m, &r, out) == 0,
          "empty + writer alive: block (no silence), that IS the pacing");
    CHECK(m.silence_periods == 0, "no silence fabricated while writer is alive");

    jts_ring_header_t *h = (jts_ring_header_t *)w.base;
    atomic_store_explicit(&h->writer_heartbeat_ns, 1, memory_order_relaxed);
    CHECK(cap_model_read_period(&m, &r, out) == 1, "writer dead: fabricate a silence period");
    CHECK(m.silence_periods == 1, "one silence period fabricated");
    int all_zero = 1;
    for (size_t i = 0; i < n; i++) if (out[i] != 0) all_zero = 0;
    CHECK(all_zero, "fabricated silence is zeros");

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
// wall-clock silence pacing is host-tested. Given a monotonic clock `now`, arm
// one period iff the writer is dead, the ring is empty, no period is already
// armed, and either this is the first arm (last_silence_ns == 0) or a full
// period_ns has elapsed. On arm, re-anchor last_silence_ns to `now` (the tick
// time — the source of the ~14% slow, safe-direction drift).
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
    // The two load-bearing properties of jts_ring_capture_poll_revents' arm step:
    //   (1) the per-tick BOUND — pending_silence_frames never exceeds one period
    //       (avail can never run away), AND
    //   (2) the SAFE-DIRECTION guarantee — over any real-time window, silence is
    //       never armed FASTER than realtime. Slow is fine (measured ~14% slow);
    //       fast would pre-consume a returning writer's audio as silence.
    // The ALSA rw loop's poll cadence is one tick every period/4.
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
    CHECK((uint64_t)arms >= realtime_periods / 2,
          "silence pacing actually advances (not wedged)");
}

static void test_capture_armed_silence_commitment_no_phantom_avail(void) {
    // The RTTIME-spin debt regression. Once the service tick ARMS a period of
    // silence, the pointer REPORTS it to ALSA as readable — and the reported
    // position is forward-only by design (the alias clamp). A refill that
    // DISCARDS an armed-but-unconsumed period when the writer's first slot races
    // in leaves hw_ptr one period ahead of anything the refill can ever serve:
    // PERMANENT phantom avail. ALSA's rw loop only poll-waits at avail == 0, so
    // every genuinely-empty moment then becomes a hot 0-frame-transfer spin — on
    // camilla's SCHED_FIFO capture thread, the RLIMIT_RTTIME SIGKILL. The
    // contract: an armed period is a delivery COMMITMENT, served BEFORE real data
    // (it belongs to the silence gap that just ended), so the books stay exact
    // and the one extra ~2.7 ms of zeros lands contiguous with the gap — never
    // spliced into steady-state music later.
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

    // 1. Writer DIES with an empty ring: a service tick ARMS one period and the
    // pointer REPORTS it — armed but not yet consumed, the rw loop's
    // poll-before-transfer window.
    atomic_store_explicit(&h->writer_heartbeat_ns, 1, memory_order_relaxed);
    uint64_t avail = cap_model_poll_then_avail(&m, &r);
    CHECK(m.pending_silence_frames == g.period_frames, "silence armed while writer dead");
    CHECK(avail == g.period_frames, "armed period REPORTED readable (hw advanced)");
    CHECK(m.silence_periods == 0, "armed but not yet consumed");

    // 2. Writer RETURNS (fresh heartbeat) and PUBLISHES a real slot BEFORE the
    // armed silence is consumed — the race that mints the debt.
    atomic_store_explicit(&h->writer_heartbeat_ns, jts_ring_monotonic_ns(),
                          memory_order_relaxed);
    mark_slot(s, n, 1717);
    CHECK(jts_ring_writer_publish(&w, s) == JTS_RING_PUBLISH_OK, "writer publishes a real slot");

    CHECK(cap_model_read_period(&m, &r, out) == 1, "read the committed silence period");
    CHECK(m.silence_periods == 1, "the armed period was SERVED, not discarded");
    int all_zero = 1;
    for (size_t i = 0; i < n; i++) if (out[i] != 0) all_zero = 0;
    CHECK(all_zero, "committed period is zeros");
    CHECK(m.pending_silence_frames == 0, "commitment consumed");

    CHECK(cap_model_read_period(&m, &r, out) == 1, "read the real slot");
    CHECK(memcmp(out, s, n * sizeof(int16_t)) == 0, "real audio intact after the boundary");

    // Everything reported was delivered, so avail must return to EXACTLY 0 — and
    // stay 0 across service ticks (writer alive: no new arms, no spurious
    // silence in live audio). Under a discard, hw sits one period ahead of appl
    // forever: avail pinned at period_frames with read_period returning 0, the
    // poll-less spin precondition.
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
    // forward-only reported position and mint permanent phantom avail (the same
    // RTTIME-spin debt class as the armed-silence discard) — AND (2) SELF-HEAL
    // on the per-wake service tick, NOT only when transfer runs: at avail 0
    // alsa-lib never calls transfer, so a resync living only in consume would
    // leave the reader permanently silent with a live writer.
    // jts_ring_reader_resync_if_overrun in capture_service_tick (modeled here in
    // cap_model_poll_arm) gives it an avail-visible recovery path.
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

    uint64_t avail = cap_model_poll_then_avail(&m, &r);
    CHECK(avail == 0, "out-of-range occupancy reports 0 readable (no phantom avail)");
    // SELF-HEAL: that same wake resynced the reader WITHOUT any transfer at
    // avail 0 — recovery through an avail-visible flow, not a read alsa-lib
    // would never issue.
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
    // continuity (no dropped or duplicated frames at the sub-slot boundary).
    // cap_model reads whole periods, so this drives the real reader plus a hand
    // destage to reach the plugin's jts_ring_capture_transfer copy arithmetic.
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


// ============================================================================
// PACING GOVERNOR (jts_ring_pace_apply, PLAYBACK only)
//
// The governor is a token bucket on the PLAYBACK pointer core. Capture is
// ungoverned by construction — its inputs carry no governor fields at all — and
// test_pace_capture_core_is_ungoverned asserts that end of it against a frozen
// copy of the pre-governor clamp.
//
// The hardware numbers these tests stand in for are stated once, in
// jts_ring_shm.h's governor banner, and measured in
// captures/8.7-EVIDENCE-grouping-ring-2026-08-20.md. Not restated here.
// ============================================================================

// The grouping ring's own geometry — the only PCM that declares pace_nominal.
#define PACE_PERIOD 128u
#define PACE_BUFFER (16ull * PACE_PERIOD) /* n_slots 16 */
#define PACE_RATE 48000u
#define PACE_NS_PER_S 1000000000ull

// One playback `pointer` call with an arbitrary honest value: occupancy and stage
// are 0 and the reader is dead, so in_flight is 0 and honest == appl_frames. That
// makes `honest` a direct knob and keeps these tests about the governor.
static uint64_t pace_report(jts_ring_pointer_state_t *st, uint64_t honest,
                            uint64_t buffer, uint32_t period, int pace_nominal,
                            uint64_t now_ns) {
    jts_ring_pointer_inputs_t in = {
        .appl_frames = honest,
        .occupancy_slots = 0,
        .stage_frames = 0,
        .period_frames = period,
        .buffer_size = buffer,
        .reader_live = 0,
        .pace_nominal = pace_nominal,
        .now_ns = now_ns,
        .rate = PACE_RATE,
    };
    return jts_ring_pointer_report(st, &in);
}

// A governed state anchored at t=1 ns (jts_ring_pace_arm's own forced-nonzero
// floor), so a test's first call has a well-defined dt.
static jts_ring_pointer_state_t pace_state_started(void) {
    jts_ring_pointer_state_t st;
    memset(&st, 0, sizeof(st));
    jts_ring_pace_arm(&st, 1, 1, 1, PACE_BUFFER);
    return st;
}

// FROZEN copy of the reported-position clamp as it stood BEFORE the governor,
// identical in both cores then. Byte-identity against a frozen copy is the only
// way to assert an ungoverned path did not move; this deliberately does NOT call
// the header (that would make the comparison a tautology) and must never be
// "shared" with it.
static uint64_t legacy_clamp(jts_ring_pointer_state_t *st, uint64_t honest,
                             uint64_t buffer_size, uint32_t period_frames) {
    uint64_t last = st->last_reported;
    uint64_t reported;
    if (honest <= last) {
        reported = last;
    } else {
        uint64_t advance = honest - last;
        uint64_t max_advance = (buffer_size > (uint64_t)period_frames)
                                   ? (buffer_size - (uint64_t)period_frames)
                                   : 0;
        if (advance > max_advance) advance = max_advance;
        reported = last + advance;
    }
    st->last_reported = reported;
    return reported;
}

// pace_report with the reader's liveness under the test's control — the dead->live
// edge is what re-seeds the bucket, so it has to be drivable.
static uint64_t pace_report_rl(jts_ring_pointer_state_t *st, uint64_t honest,
                               uint64_t buffer, uint32_t period, int pace_nominal,
                               uint64_t now_ns, int reader_live) {
    jts_ring_pointer_inputs_t in = {
        .appl_frames = honest,
        .occupancy_slots = 0,
        .stage_frames = 0,
        .period_frames = period,
        .buffer_size = buffer,
        .reader_live = reader_live,
        .pace_nominal = pace_nominal,
        .now_ns = now_ns,
        .rate = PACE_RATE,
    };
    return jts_ring_pointer_report(st, &in);
}

static uint64_t pace_lcg(uint64_t *s) {
    *s = *s * 6364136223846793005ull + 1442695040888963407ull;
    return *s >> 11;
}

// Frames of nominal at PACE_RATE for a whole number of seconds, plus the declared
// headroom — written out longhand so a widened JTS_RING_PACE_HEADROOM_PPM fails
// the rate bounds below instead of moving them with it.
static uint64_t pace_expected_frames(uint64_t seconds) {
    uint64_t base = seconds * PACE_RATE;
    return base + (base * 2500ull) / 1000000ull;
}

static void test_pace_clock_source_advances(void) {
    // The governor's time source must actually move. A raw clock stuck at a
    // constant is the one failure that turns the bucket into a WEDGE: every dt
    // would be 0, the bucket would never refill, and a governed writer would stop
    // for good. (WHICH clock it is — CLOCK_MONOTONIC_RAW, not the NTP-slewed one —
    // is a source-level choice the host test cannot observe; this covers that the
    // function is wired and advancing.)
    uint64_t first = jts_ring_monotonic_raw_ns();
    CHECK(first > 0, "raw monotonic clock is nonzero");
    uint64_t later = first;
    for (int i = 0; i < 1000000 && later == first; i++) later = jts_ring_monotonic_raw_ns();
    CHECK(later > first, "raw monotonic clock advances");
}

static void test_pace_refill_overflow_bound(void) {
    // The elapsed->frames refill must stay exact across the whole range a u64
    // nanosecond count can reach. The caller passes a per-call delta, but a paused
    // stream can go arbitrarily long between pointer calls, so the bound is taken
    // over the whole input range. The naive `elapsed_ns * rate / 1e9` wraps past
    // 4.4 days, which would silently zero a refill.
    const uint64_t tpf = 1024ull; // JTS_RING_PACE_TOKENS_PER_FRAME, longhand
    CHECK(jts_ring_pace_refill_tokens(0, PACE_RATE) == 0, "zero elapsed, zero tokens");
    CHECK(jts_ring_pace_refill_tokens(PACE_NS_PER_S, PACE_RATE) ==
              48000ull * tpf + (48000ull * tpf) / 400ull,
          "one second == 48000 frames + 2500 ppm, in tokens");

    // 5 days: the first round number PAST the naive form's overflow point.
    CHECK(jts_ring_pace_refill_tokens(432000ull * PACE_NS_PER_S, PACE_RATE) ==
              20736000000ull * tpf + (20736000000ull * tpf) / 400ull,
          "5 days (past the naive overflow point) is still exact");
    // 180 days — the multi-month bound.
    CHECK(jts_ring_pace_refill_tokens(15552000ull * PACE_NS_PER_S, PACE_RATE) ==
              746496000000ull * tpf + (746496000000ull * tpf) / 400ull,
          "180 days is still exact");
    // Sub-frame resolution is what keeps a per-CALL refill from losing the
    // remainder of every call: one poll period must refill MORE than the whole
    // frames it contains, not fewer.
    CHECK(jts_ring_pace_refill_tokens(2666666ull, PACE_RATE) > 128ull * tpf,
          "one period of elapsed refills more than one period of frames");

    // Strictly increasing across the whole range, including the u64 ceiling: a
    // wrap anywhere shows up here as a value that went DOWN.
    const uint64_t marks[] = {
        PACE_NS_PER_S,                  // 1 s
        380000000000000ull,             // 4.4 days — the naive wrap point
        432000ull * PACE_NS_PER_S,      // 5 days
        15552000ull * PACE_NS_PER_S,    // 180 days
        31536000ull * PACE_NS_PER_S,    // 1 year
        UINT64_MAX,
    };
    uint64_t prev = 0;
    for (size_t i = 0; i < sizeof(marks) / sizeof(marks[0]); i++) {
        uint64_t f = jts_ring_pace_refill_tokens(marks[i], PACE_RATE);
        CHECK(f > prev, "refill frames strictly increase with elapsed (no wrap)");
        prev = f;
    }
    CHECK(jts_ring_pace_refill_tokens(UINT64_MAX, PACE_RATE) < UINT64_MAX / 16ull,
          "even the u64 elapsed ceiling keeps the 20x margin the proof claims");
    CHECK(jts_ring_pace_refill_tokens(PACE_NS_PER_S, 0) == 0, "rate 0 answers 0");
}

static void test_pace_bucket_binds_when_the_app_outruns_the_clock(void) {
    // A maximally greedy app against a bucket refilled at 1 ms per call: the report
    // must track the refill, not the demand. Asserted against longhand frame
    // arithmetic, so the bound does not move with the constant it is bounding.
    jts_ring_pointer_state_t st = pace_state_started();
    uint64_t now = 1;
    uint64_t reported = 0;
    for (uint64_t ms = 1; ms <= 1000; ms++) {
        now = 1 + ms * 1000000ull;
        // Four greedy calls per millisecond — any per-CALL leak shows up as
        // excess frames here rather than as a rate.
        for (int spin = 0; spin < 4; spin++) {
            reported = pace_report(&st, 1000000000000ull, PACE_BUFFER, PACE_PERIOD, 1, now);
        }
    }
    // One second of refill, plus at most the bucket (the standing burst) and one
    // period of quantization slack.
    CHECK(reported <= pace_expected_frames(1) + PACE_BUFFER + PACE_PERIOD,
          "a greedy app is held to the refill rate, not its demand");
    CHECK(reported + PACE_BUFFER + PACE_PERIOD >= pace_expected_frames(1),
          "a greedy app still gets the whole refill (the bucket is not a brake)");
    CHECK(st.pace_bound_ns > 0, "bound time accrues while the bucket binds");
    CHECK(st.pace_tokens <= PACE_BUFFER * 1024ull, "the bucket never holds more than one buffer");
}

static void test_pace_is_inert_when_the_device_binds_first(void) {
    // A DAC-clocked reader already holds the writer at nominal, and the governor
    // must then be invisible: identical output to the pre-governor core, including
    // the carried state — no quantization, no lag, bit for bit.
    jts_ring_pointer_state_t gov = pace_state_started();
    jts_ring_pointer_state_t ref;
    memset(&ref, 0, sizeof(ref));
    uint64_t honest = 0;
    uint64_t now = 1;
    for (int tick = 0; tick < 500; tick++) {
        // One period per 2.667 ms — exactly nominal, the paced case.
        now += (uint64_t)PACE_PERIOD * PACE_NS_PER_S / PACE_RATE;
        honest += PACE_PERIOD;
        uint64_t got = pace_report(&gov, honest, PACE_BUFFER, PACE_PERIOD, 1, now);
        uint64_t want = legacy_clamp(&ref, honest, PACE_BUFFER, PACE_PERIOD);
        CHECK(got == want, "governor is inert while the device binds first");
        CHECK(got == honest, "a device-paced app is reported honestly");
    }
    CHECK(gov.last_reported == ref.last_reported, "carried position matches too");
    CHECK(gov.pace_bound_ns == 0, "an inert governor accrues no bound time");

    // ...and it must still be inert against a device running as far off nominal as
    // this fleet's own hardware does. The dongle measures ~667 ppm,
    // which is what the headroom is sized for; a
    // headroom picked off a crystal's +-100 ppm datasheet line would bind here and
    // throttle a perfectly healthy stream.
    jts_ring_pointer_state_t fast = pace_state_started();
    uint64_t fast_honest = 0;
    uint64_t fast_now = 1;
    for (int tick = 0; tick < 20000; tick++) {
        fast_now += (uint64_t)PACE_PERIOD * PACE_NS_PER_S / PACE_RATE;
        // One period per period of wall clock, but the device's crystal is 667 ppm
        // fast, so it asks for that much more over the run.
        fast_honest = ((uint64_t)(tick + 1) * PACE_PERIOD);
        fast_honest += (fast_honest * 667ull) / 1000000ull;
        pace_report(&fast, fast_honest, PACE_BUFFER, PACE_PERIOD, 1, fast_now);
    }
    CHECK(fast.pace_bound_ns == 0,
          "a 667 ppm fast device — this fleet's measured drift — never binds");
    CHECK(fast.last_reported == fast_honest, "and is reported honestly throughout");
}

static void test_pace_prepared_writer_is_paced_before_start(void) {
    // THE HOLE prepare-arming closes. A PCM can transfer while still PREPARED —
    // with start_threshold > period and a dead reader, ALSA's start condition
    // never trips against the dead-reader discount, so the stream may never leave
    // PREPARED at all. pace_apply early-returns on an unarmed bucket, so arming
    // only at `start` leaves that window ungoverned; the prepare transition has
    // to arm. Driven here exactly as the plugin's prepare callback does, with NO
    // start step at all after it.
    jts_ring_pointer_state_t st;
    memset(&st, 0, sizeof(st));
    jts_ring_pointer_prepare(&st, 1, 1, 1, PACE_BUFFER);

    uint64_t reported = 0;
    for (uint64_t ms = 1; ms <= 1000; ms++) {
        uint64_t now = 1 + ms * 1000000ull;
        for (int spin = 0; spin < 4; spin++) {
            reported = pace_report_rl(&st, 1000000000000ull, PACE_BUFFER, PACE_PERIOD,
                                      1, now, 0);
        }
    }
    // Paced: one second of refill plus the one-time seed, not the ~4000 calls the
    // loop made. Ungoverned this would be 4000 * (buffer - period) frames.
    CHECK(reported <= pace_expected_frames(1) + PACE_BUFFER + PACE_PERIOD,
          "a PREPARED writer is paced, not free-running");
    CHECK(st.pace_bound_ns > 0, "and the governor is demonstrably engaged there");
}

static void test_pace_start_grants_the_prefill_without_binding(void) {
    // A clean start must absorb its prefill the way a real device does —
    // instantly, with no bind edge. Starting the bucket empty rations legitimate
    // prefill at the ceiling: measured on hardware as a 57 s startup bind where
    // the ungoverned build had back-pressure in ~5 s.
    jts_ring_pointer_state_t st;
    memset(&st, 0, sizeof(st));
    jts_ring_pace_arm(&st, 1, 1, 1, PACE_BUFFER);
    jts_ring_pace_log_state_t ls;
    memset(&ls, 0, sizeof(ls));

    // One buffer LESS ONE PERIOD of demand, a microsecond after start — the most a
    // single call can report anyway (the alias clamp caps an advance at
    // buffer - period), so what is measured here is the governor and not that clamp.
    const uint64_t prefill = PACE_BUFFER - PACE_PERIOD;
    uint64_t got = pace_report_rl(&st, prefill, PACE_BUFFER, PACE_PERIOD, 1, 1 + 1000, 1);
    CHECK(got == prefill, "the prefill lands in ONE call, ungated");
    CHECK(st.pace_bound_ns == 0, "and the governor never bound doing it");
    CHECK(jts_ring_pace_log_step(&ls, st.pace_bound_ns, 1 + 1000, 60 * PACE_NS_PER_S) ==
              JTS_RING_PACE_LOG_NONE,
          "a clean start with a live reader produces NO edge at all");
}

static void test_pace_reader_reattach_reseeds_the_prefill(void) {
    // The reader coming back is the same event that resyncs read_seq: the device was
    // re-prepared, so it gets its prefill again. Without this the recovery after a
    // stall is rationed at the ceiling — the hardware pass saw no sub-second re-lock
    // after SIGCONT.
    jts_ring_pointer_state_t st;
    memset(&st, 0, sizeof(st));
    jts_ring_pace_arm(&st, 1, 1, 1, PACE_BUFFER);
    uint64_t now = 1;
    for (uint64_t ms = 1; ms <= 200; ms++) {
        now = 1 + ms * 1000000ull;
        pace_report_rl(&st, 1000000000000ull, PACE_BUFFER, PACE_PERIOD, 1, now, 0);
    }
    CHECK(st.pace_tokens < PACE_BUFFER * 1024ull, "the stall left the bucket drained");
    uint64_t before = st.last_reported;

    // Reattach, with essentially no time passing: only a re-seed can pay for this.
    now += 1000;
    pace_report_rl(&st, 1000000000000ull, PACE_BUFFER, PACE_PERIOD, 1, now, 1);
    CHECK(st.last_reported - before == PACE_BUFFER - PACE_PERIOD,
          "reattach catch-up lands in one call, not rationed at the ceiling");
}

static void test_pace_reattach_flapping_stays_bounded(void) {
    // The re-seed is EDGE-only, and the edge rate is capped by the liveness window
    // (a reader must be heartbeat-dead 2 s before it can come live again). So the
    // worst an adversarial flap can add is one buffer per transition — asserted here
    // against N buffers plus the honest refill, not against a rate.
    jts_ring_pointer_state_t st;
    memset(&st, 0, sizeof(st));
    jts_ring_pace_arm(&st, 1, 1, 1, PACE_BUFFER);
    const uint64_t transitions = 30;
    const uint64_t dead_calls = 100, live_calls = 50;
    const uint64_t cycle_ns = 2 * PACE_NS_PER_S; // the liveness floor
    uint64_t now = 1;
    for (uint64_t i = 0; i < transitions; i++) {
        for (uint64_t k = 0; k < dead_calls; k++) {
            now += cycle_ns / (dead_calls + live_calls);
            pace_report_rl(&st, 1000000000000ull, PACE_BUFFER, PACE_PERIOD, 1, now, 0);
        }
        // ...then a run of LIVE calls. Only the FIRST is an edge; a re-seed that
        // fired on every live call instead would grant a buffer `live_calls` times.
        for (uint64_t k = 0; k < live_calls; k++) {
            now += cycle_ns / (dead_calls + live_calls);
            pace_report_rl(&st, 1000000000000ull, PACE_BUFFER, PACE_PERIOD, 1, now, 1);
        }
    }
    uint64_t elapsed_s = transitions * 2;
    uint64_t bound = pace_expected_frames(elapsed_s) + transitions * PACE_BUFFER +
                     transitions * PACE_PERIOD;
    CHECK(st.last_reported <= bound,
          "flapping adds at most one buffer per transition, never per call");
    // And the standing overhead is the 2.1% the contract derives: N buffers over
    // N*2 s is 1024 f/s against 48000.
    uint64_t excess = st.last_reported - pace_expected_frames(elapsed_s);
    CHECK(excess <= transitions * (PACE_BUFFER + PACE_PERIOD),
          "the flap overhead matches the derived per-transition bound");
}

static void test_pace_bound_ns_measures_time_not_call_rate(void) {
    // The operator-facing number. It must be O(truth), not O(how often ALSA asked:
    // `want` is a STANDING backlog re-presented on every call, so a frames-withheld
    // sum counts one shortfall once per pointer call and inflates without bound as
    // the consumer polls harder. Same 10 simulated seconds of solid binding, driven
    // at two call rates two orders apart; both must read ~10 s and agree.
    const uint64_t window_s = 10;
    const uint64_t rates[] = {375, 50000}; // calls/s: the poll cadence, and a spinner
    uint64_t measured[2];
    for (size_t i = 0; i < 2; i++) {
        jts_ring_pointer_state_t st = pace_state_started();
        uint64_t calls = window_s * rates[i];
        uint64_t step_ns = PACE_NS_PER_S / rates[i];
        for (uint64_t c = 1; c <= calls; c++) {
            pace_report(&st, 1000000000000ull, PACE_BUFFER, PACE_PERIOD, 1,
                        1 + c * step_ns);
        }
        measured[i] = st.pace_bound_ns;
        CHECK(measured[i] + step_ns >= window_s * PACE_NS_PER_S &&
                  measured[i] <= window_s * PACE_NS_PER_S,
              "bound time reads the wall-clock window it was bound for");
    }
    uint64_t hi = measured[0] > measured[1] ? measured[0] : measured[1];
    uint64_t lo = measured[0] > measured[1] ? measured[1] : measured[0];
    CHECK(hi - lo < PACE_NS_PER_S / 100,
          "and the two call rates agree — the number is not O(call rate)");
}

static void test_pace_start_only_anchors_a_governed_playback_pcm(void) {
    // `start` is shared by both directions and by every ungoverned PCM, so the
    // gate lives inside jts_ring_pace_arm. Without it the bucket is anchored on
    // rings that must never be governed, which is both a false claim in the
    // header and a live governor one conf.d edit away.
    const struct { int governed, playback, anchors; } cases[] = {
        {1, 1, 1}, // the grouping ring's playback direction: the only anchored case
        {1, 0, 0}, // same PCM, capture direction
        {0, 1, 0}, // every other ring's playback
        {0, 0, 0}, // every other ring's capture
    };
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        jts_ring_pointer_state_t st;
        memset(&st, 0, sizeof(st));
        jts_ring_pace_arm(&st, cases[i].governed, cases[i].playback, 7 * PACE_NS_PER_S, PACE_BUFFER);
        if (cases[i].anchors) {
            CHECK(st.pace_last_ns == 7 * PACE_NS_PER_S, "governed playback anchors");
        } else {
            CHECK(st.pace_last_ns == 0,
                  "an ungoverned or capture stream leaves the bucket untouched");
        }
    }
}

static void test_pace_log_step_edges_and_rate_limit(void) {
    // Edge detection, the rate limit, and the boot-window sentinel. The plugin
    // only prints what this decides, so the decision is checkable here.
    const uint64_t interval = 60 * PACE_NS_PER_S;
    jts_ring_pace_log_state_t ls;
    memset(&ls, 0, sizeof(ls));

    for (int i = 0; i < 10; i++) {
        CHECK(jts_ring_pace_log_step(&ls, 0, (uint64_t)i * PACE_NS_PER_S, interval) ==
                  JTS_RING_PACE_LOG_NONE,
              "a governor that never binds says nothing");
    }

    // BOOT WINDOW: `now` is CLOCK_MONOTONIC_RAW, so a PCM opened seconds after boot
    // sits well inside `interval` of zero. The first bind must still be announced —
    // last_log_ns == 0 is a sentinel, not a timestamp to subtract from.
    CHECK(jts_ring_pace_log_step(&ls, 100, 5 * PACE_NS_PER_S, interval) ==
              JTS_RING_PACE_LOG_BIND,
          "the first bind is announced even seconds after boot");
    CHECK(jts_ring_pace_log_step(&ls, 200, 6 * PACE_NS_PER_S, interval) ==
              JTS_RING_PACE_LOG_NONE,
          "a standing bind is not re-announced per call");
    // Release closes the pair, and is never rate-limited.
    CHECK(jts_ring_pace_log_step(&ls, 200, 7 * PACE_NS_PER_S, interval) ==
              JTS_RING_PACE_LOG_RELEASE,
          "the release is announced when binding stops");

    // A re-bind inside the window is SUPPRESSED — and because it is, its release
    // must be suppressed too, or the journal shows an unmatched release.
    CHECK(jts_ring_pace_log_step(&ls, 300, 8 * PACE_NS_PER_S, interval) ==
              JTS_RING_PACE_LOG_NONE,
          "a re-bind inside the window is suppressed");
    CHECK(jts_ring_pace_log_step(&ls, 300, 9 * PACE_NS_PER_S, interval) ==
              JTS_RING_PACE_LOG_NONE,
          "and its release is suppressed with it — pairs stay balanced");

    CHECK(jts_ring_pace_log_step(&ls, 400, 5 * PACE_NS_PER_S + interval + 1, interval) ==
              JTS_RING_PACE_LOG_BIND,
          "the next window's bind is announced");
}

static void test_pace_log_survives_a_reprepare_with_a_bind_standing(void) {
    // A (re)prepare while a bind is STANDING is ordinary XRUN recovery against a
    // dead reader, and it zeroes pace_bound_ns. The ledger is DERIVED from that
    // counter, so the log state has to be cleared with it. Left behind, the next
    // call sees bound_ns fall from large to 0, reads that as a release edge, and
    // reports it against the previous stream incarnation — with a delta that
    // underflowed through zero into ~584 years.
    const uint64_t interval = 60 * PACE_NS_PER_S;
    jts_ring_pace_log_state_t ls;
    memset(&ls, 0, sizeof(ls));
    CHECK(jts_ring_pace_log_step(&ls, 1000000, 5 * PACE_NS_PER_S, interval) ==
              JTS_RING_PACE_LOG_BIND,
          "a bind is standing before the re-prepare");
    uint64_t bind_bound_ns = 1000000; // what the plugin anchors its delta on

    // THE HAZARD: the counter resets, the log state does not, and the very next
    // call manufactures a release out of nothing.
    jts_ring_pace_log_state_t stale = ls;
    CHECK(jts_ring_pace_log_step(&stale, 0, 6 * PACE_NS_PER_S, interval) ==
              JTS_RING_PACE_LOG_RELEASE,
          "a stale log state turns the counter reset into a phantom release");
    CHECK(0ull - bind_bound_ns > 100ull * 365 * 24 * 3600 * PACE_NS_PER_S,
          "...and its delta underflows to a centuries-long duration");

    // THE FIX: both cleared together, exactly as jts_ring_prepare does it. The next
    // call is silent, and a delta computed from the cleared anchor is zero.
    memset(&ls, 0, sizeof(ls));
    bind_bound_ns = 0;
    for (int tick = 0; tick < 8; tick++) {
        CHECK(jts_ring_pace_log_step(&ls, 0, (uint64_t)(6 + tick) * PACE_NS_PER_S,
                                     interval) == JTS_RING_PACE_LOG_NONE,
              "after a re-prepare clears both, no release is manufactured");
    }
    CHECK(0ull - bind_bound_ns == 0, "and the delta anchor cannot underflow");

    // The new incarnation's first genuine bind still announces immediately — the
    // cleared last_log_ns is the sentinel, not a timestamp inside the window.
    CHECK(jts_ring_pace_log_step(&ls, 500, 14 * PACE_NS_PER_S, interval) ==
              JTS_RING_PACE_LOG_BIND,
          "and the fresh incarnation's first bind is not swallowed by the old window");
}

static void test_pace_burst_is_capped_at_one_buffer_however_long_the_idle(void) {
    // The burst bound, and the reason the bucket has a cap at all. However long a
    // governed stream sits idle, ONE wake may advance the report by at most one
    // buffer. An uncapped accumulator would hand back the whole idle window at
    // once — which for a 60 s gap is hundreds of buffers of instantaneous burst.
    //
    // Driven as a real app catches up — ONE PERIOD PER CALL with the clock FROZEN
    // — not as a single enormous jump, for two reasons: the reported-position
    // clamp already bounds any ONE call to buffer - period, so a single-call
    // assertion measures the clamp rather than the bucket; and a single huge
    // `honest` drains the bucket in that one call, so an UNCAPPED bucket would
    // destroy its own surplus and look capped. Period-by-period is the shape that
    // actually spends a surplus, which is the shape a returning app has.
    const uint64_t idles_s[] = {1, 10, 60, 600};
    for (size_t i = 0; i < sizeof(idles_s) / sizeof(idles_s[0]); i++) {
        jts_ring_pointer_state_t st = pace_state_started();
        uint64_t now = 1 + 1000000ull;
        pace_report(&st, 0, PACE_BUFFER, PACE_PERIOD, 1, now);
        uint64_t before = st.last_reported;
        now += idles_s[i] * PACE_NS_PER_S;
        uint64_t honest = before;
        for (int spin = 0; spin < 200; spin++) {
            honest += PACE_PERIOD;
            pace_report(&st, honest, PACE_BUFFER, PACE_PERIOD, 1, now);
        }
        CHECK(st.last_reported - before <= PACE_BUFFER,
              "any idle, then a burst of wakes at one instant: at most one buffer");
        CHECK(st.pace_tokens <= PACE_BUFFER * 1024ull, "and the bucket itself stays capped");
    }
}

static void test_pace_short_stall_catches_up_in_one_call(void) {
    // The other half of the burst decision: a gap SHORTER than one buffer is
    // caught up in a single call rather than rationed a period per wake. 1024
    // frames is under one buffer (2048) and under the pre-existing alias clamp's
    // per-call cap (buffer - period = 1920), so this measures the bucket.
    const uint64_t gap_frames = 1024;
    jts_ring_pointer_state_t st = pace_state_started();
    uint64_t now = 1;
    // Drain the bucket to a known-empty state with a greedy settled app.
    for (uint64_t ms = 1; ms <= 100; ms++) {
        now = 1 + ms * 1000000ull;
        pace_report(&st, 1000000000000ull, PACE_BUFFER, PACE_PERIOD, 1, now);
    }
    uint64_t settled = st.last_reported;
    now += gap_frames * PACE_NS_PER_S / PACE_RATE;
    uint64_t after = pace_report(&st, 1000000000000ull, PACE_BUFFER, PACE_PERIOD, 1, now);
    uint64_t advanced = after - settled;
    CHECK(advanced >= gap_frames - PACE_PERIOD && advanced <= gap_frames + PACE_PERIOD,
          "a sub-buffer stall is caught up in ONE call, not rationed");
}

static void test_pace_sustained_rate_stays_within_nominal_plus_headroom(void) {
    // The rate bound over a simulated 10 s of a maximally greedy app, measured
    // between two interior marks so the one-time startup burst is outside the
    // window. Longhand arithmetic, so widening the headroom constant fails here.
    jts_ring_pointer_state_t st = pace_state_started();
    uint64_t at_1s = 0, reported = 0;
    for (uint64_t ms = 1; ms <= 10000; ms++) {
        uint64_t now = 1 + ms * 1000000ull;
        for (int spin = 0; spin < 4; spin++) {
            reported = pace_report(&st, 1000000000000ull, PACE_BUFFER, PACE_PERIOD, 1, now);
        }
        if (ms == 1000) at_1s = reported;
    }
    // THE DERIVED BOUND, not a flat 2500 ppm. The asymptotic rate IS the headroom —
    // every truncation in the path rounds down and carries — so what a finite
    // window adds is granularity: one period of grant quantization at each mark.
    //   observed <= nominal*T*(1 + headroom) + 2*period_frames
    // Over this 9 s window that is 2500 + 1e6*256/(48000*9) = 2500 + 592 = 3092 ppm.
    // Written longhand so widening the headroom constant fails here, and stated as
    // the number a hardware bar should be set against.
    const uint64_t window_s = 9;
    const uint64_t nominal_9s = window_s * PACE_RATE;
    const uint64_t quantization = 2ull * PACE_PERIOD;
    const uint64_t bound = nominal_9s + (nominal_9s * 2500ull) / 1000000ull + quantization;
    const uint64_t measured = reported - at_1s;
    CHECK(measured <= bound, "sustained rate stays within the derived ceiling");
    // Pin the derivation itself: the excess over nominal must be inside headroom +
    // quantization, and quantization alone must be the smaller term at this window.
    CHECK(measured >= nominal_9s - quantization, "sustained rate is not held below nominal");
    CHECK(quantization * 1000000ull / nominal_9s < 2500ull,
          "at this window quantization is the smaller term — the headroom dominates");
}

static void test_pace_quantizes_only_when_it_binds(void) {
    // A PARTIAL grant is rounded down to a period multiple, so a bucket at the cap
    // releases whole periods instead of limit-cycling in sub-period steps. The
    // clock is stepped by 1 ms (48 frames — deliberately NOT a period multiple), so
    // an unquantized partial grant would hand out 48-frame steps.
    jts_ring_pointer_state_t st = pace_state_started();
    uint64_t prev = 0;
    for (uint64_t ms = 1; ms <= 500; ms++) {
        uint64_t now = 1 + ms * 1000000ull;
        uint64_t got = pace_report(&st, 1000000000000ull, PACE_BUFFER, PACE_PERIOD, 1, now);
        CHECK(got % PACE_PERIOD == 0, "a binding bucket reports whole periods only");
        CHECK(got >= prev, "quantized report is still non-decreasing");
        prev = got;
    }
    // ...and a grant that COVERS the request is passed through unrounded, which is
    // what keeps the inert case exact (a sub-period honest advance is reported as
    // itself, not floored to zero).
    jts_ring_pointer_state_t inert = pace_state_started();
    uint64_t now = 1 + PACE_NS_PER_S; // a full second of refill: the bucket is capped
    uint64_t got = pace_report(&inert, 7, PACE_BUFFER, PACE_PERIOD, 1, now);
    CHECK(got == 7, "an unbound grant is exact, not period-floored");
    CHECK(inert.pace_bound_ns == 0, "and no bound time is accrued");
}

static void test_pace_report_is_monotonic_across_mode_changes(void) {
    // The report is non-decreasing however the inputs move: the honest value
    // collapsing (a reader dying mid-play, a dead->live regrow), the bucket
    // binding and releasing, the governor being switched under it, and —
    // defensively — a clock that goes BACKWARD, which a raw monotonic clock cannot
    // do but which must never un-report frames if it did.
    jts_ring_pointer_state_t st = pace_state_started();
    uint64_t seed = 0x5eed1234u;
    uint64_t prev = 0;
    uint64_t now = 1;
    for (int tick = 0; tick < 2000; tick++) {
        uint64_t honest = pace_lcg(&seed) % (8ull * PACE_BUFFER);
        now += 500000ull;
        if (tick % 37 == 0 && now > 10 * PACE_NS_PER_S) now -= 4 * PACE_NS_PER_S;
        int governed = (tick % 3) != 0; // flip the mode under it as well
        uint64_t got = pace_report(&st, honest, PACE_BUFFER, PACE_PERIOD, governed, now);
        CHECK(got >= prev, "reported position is non-decreasing across mode changes");
        prev = got;
    }
}

static void test_pace_lifecycle_prepare_start_pointer(void) {
    // The governor's lifecycle, driven exactly as the plugin drives it. Two
    // failures this pins, both of which leave every rate test above green:
    //   - a stream whose `start` never anchors the bucket must stay INERT, not
    //     bind on a garbage dt measured from zero;
    //   - `prepare` must clear the bucket, so a restarted stream cannot inherit
    //     tokens (or a throttle count) from the previous run.
    jts_ring_pointer_state_t st;
    memset(&st, 0, sizeof(st));

    // NEVER STAMPED: pace_last_ns == 0. Greedy demand, a clock far in the future,
    // and the governor must not touch the report.
    jts_ring_pointer_state_t ref;
    memset(&ref, 0, sizeof(ref));
    for (int tick = 0; tick < 8; tick++) {
        uint64_t now = (uint64_t)(tick + 1) * PACE_NS_PER_S;
        uint64_t got = pace_report(&st, 1000000000000ull, PACE_BUFFER, PACE_PERIOD, 1, now);
        uint64_t want = legacy_clamp(&ref, 1000000000000ull, PACE_BUFFER, PACE_PERIOD);
        CHECK(got == want, "an UNARMED stream is ungoverned — the hole prepare-arming closes");
    }
    CHECK(st.pace_bound_ns == 0, "an unarmed governor accrues no bound time");

    jts_ring_pointer_state_reset(&st);
    jts_ring_pace_arm(&st, 1, 1, 5 * PACE_NS_PER_S, PACE_BUFFER);
    CHECK(st.pace_last_ns == 5 * PACE_NS_PER_S, "arming anchors the bucket at now");
    CHECK(st.pace_tokens == PACE_BUFFER * 1024ull,
          "arming seeds the bucket FULL — one buffer of device prefill");
    // 100 ms of refill: enough to grant several whole periods, so the report moves
    // and the state below is genuinely dirty. (One millisecond buys 48 frames —
    // under a period — and would grant nothing at all.)
    uint64_t got = pace_report(&st, 1000000000000ull, PACE_BUFFER, PACE_PERIOD, 1,
                               5 * PACE_NS_PER_S + 100000000ull);
    CHECK(got < 1000000000000ull, "an armed governor binds a greedy app");
    CHECK(got > 0, "and still grants the periods the refill paid for");
    CHECK(st.pace_bound_ns > 0, "and accrues bound time");

    // PREPARE clears everything the run above dirtied — asserted HERE, with a
    // genuinely dirty bucket, not before one existed. Leaving tokens or a throttle
    // count behind would let a restarted stream inherit credit it never earned.
    CHECK(st.pace_last_ns != 0 && st.pace_bound_ns != 0 && st.last_reported != 0,
          "the state really is dirty before the reset (the assertion below is live)");
    // Give it a surplus too, so a reset that skipped `pace_tokens` alone is caught.
    pace_report(&st, st.last_reported, PACE_BUFFER, PACE_PERIOD, 1,
                5 * PACE_NS_PER_S + 2 * PACE_NS_PER_S);
    CHECK(st.pace_tokens != 0, "...including a nonzero bucket");
    jts_ring_pointer_state_reset(&st);
    CHECK(st.last_reported == 0 && st.pace_last_ns == 0 && st.pace_tokens == 0 &&
              st.pace_bound_ns == 0,
          "prepare clears the reported position and the whole bucket");

    // A zero clock sample still anchors (0 is the not-armed sentinel).
    jts_ring_pointer_state_t zero_st;
    memset(&zero_st, 0, sizeof(zero_st));
    jts_ring_pace_arm(&zero_st, 1, 1, 0, PACE_BUFFER);
    CHECK(zero_st.pace_last_ns != 0, "arming forces a nonzero anchor");
}

static void test_pace_timer_cadence(void) {
    // The poll cadence decision, which lives in the header precisely so it is
    // checkable here rather than only on a Pi. A governed PLAYBACK PCM polls at
    // the period (the grain the bucket releases in); everything else — including a
    // governed PCM's CAPTURE direction, whose wake also drives the wall-clock
    // silence gate — keeps period/4.
    const uint64_t period_ns = jts_ring_period_ns(PACE_PERIOD, PACE_RATE);
    const uint64_t tick_ns = jts_ring_tick_ns(PACE_PERIOD, PACE_RATE);
    CHECK(period_ns == 2666666ull, "128 frames at 48 kHz is 2.667 ms");
    CHECK(tick_ns == period_ns / 4, "the oversampled tick is period/4 here");

    CHECK(jts_ring_timer_cadence_ns(1, 1, PACE_PERIOD, PACE_RATE) == period_ns,
          "governed PLAYBACK polls at the period");
    CHECK(jts_ring_timer_cadence_ns(1, 0, PACE_PERIOD, PACE_RATE) == tick_ns,
          "governed CAPTURE keeps period/4 (its wake drives the silence gate)");
    CHECK(jts_ring_timer_cadence_ns(0, 1, PACE_PERIOD, PACE_RATE) == tick_ns,
          "ungoverned playback keeps period/4");
    CHECK(jts_ring_timer_cadence_ns(0, 0, PACE_PERIOD, PACE_RATE) == tick_ns,
          "ungoverned capture keeps period/4");

    // The tick's clamps still hold at both extremes, and the governed cadence is
    // deliberately NOT clamped — it is a whole period by definition, which is what
    // makes arm_timer's tv_sec/tv_nsec split load-bearing above 48000 frames.
    CHECK(jts_ring_tick_ns(8, PACE_RATE) == 250000ull, "tick floors at 0.25 ms");
    CHECK(jts_ring_tick_ns(65536, PACE_RATE) == 2000000ull, "tick ceilings at 2 ms");
    CHECK(jts_ring_timer_cadence_ns(1, 1, 65536, PACE_RATE) > PACE_NS_PER_S,
          "a governed cadence CAN exceed one second (the itimerspec split's reason)");
    CHECK(jts_ring_period_ns(PACE_PERIOD, 0) == 0, "rate 0 answers 0");
}

static void test_pace_off_is_byte_identical_to_the_pre_governor_core(void) {
    // Opt-out is bit-for-bit. Every ring PCM except the grouping one omits
    // `pace_nominal`, so this is what keeps the governor off Ring A, Ring B, the
    // renderer lanes and the active ring. The state is ANCHORED and the clock is
    // driven, so a leaked governor would bind.
    static const uint32_t periods[] = {128, 256};
    static const uint32_t slots[] = {2, 16, 1}; // 1 => the degenerate buffer == period
    for (size_t pi = 0; pi < sizeof(periods) / sizeof(periods[0]); pi++) {
        for (size_t si = 0; si < sizeof(slots) / sizeof(slots[0]); si++) {
            uint32_t period = periods[pi];
            uint64_t buffer = (uint64_t)slots[si] * period;
            jts_ring_pointer_state_t got_st = pace_state_started();
            jts_ring_pointer_state_t want_st;
            memset(&want_st, 0, sizeof(want_st));
            uint64_t seed = 0xabcdef01u + pi * 7 + si * 13;
            for (int tick = 0; tick < 500; tick++) {
                uint64_t honest = pace_lcg(&seed) % (8ull * buffer + 1);
                uint64_t now = 1 + (uint64_t)tick * 1000ull; // 1 us/call: a starved bucket
                uint64_t got = pace_report(&got_st, honest, buffer, period, 0, now);
                uint64_t want = legacy_clamp(&want_st, honest, buffer, period);
                CHECK(got == want,
                      "pace_nominal 0 is byte-identical to the pre-governor core");
                CHECK(got_st.last_reported == want_st.last_reported,
                      "pace_nominal 0 leaves the carried state identical too");
            }
            // The state was anchored AND seeded by pace_state_started, so "untouched"
            // means still holding its seed — not zero. A leaked governor would have
            // spent from it.
            CHECK(got_st.pace_tokens == PACE_BUFFER * 1024ull && got_st.pace_bound_ns == 0,
                  "an ungoverned call never touches the bucket");
        }
    }
}

static void test_pace_capture_core_is_ungoverned(void) {
    // Capture reverts to EXACTLY its pre-governor behavior, and stays there. Its
    // inputs carry no governor fields (a compile-level fact), and its report must
    // equal the frozen clamp over an arbitrary readable/appl sweep — including the
    // shapes a governor would have throttled: a huge readable arriving in one step.
    static const uint32_t slots[] = {2, 16};
    for (size_t si = 0; si < sizeof(slots) / sizeof(slots[0]); si++) {
        uint64_t buffer = (uint64_t)slots[si] * PACE_PERIOD;
        jts_ring_pointer_state_t got_st;
        jts_ring_pointer_state_t want_st;
        memset(&got_st, 0, sizeof(got_st));
        memset(&want_st, 0, sizeof(want_st));
        uint64_t seed = 0x1234abcdu + si;
        uint64_t appl = 0;
        for (int tick = 0; tick < 500; tick++) {
            uint64_t occ = pace_lcg(&seed) % (uint64_t)(slots[si] + 1);
            uint64_t destage = pace_lcg(&seed) % PACE_PERIOD;
            uint64_t silence = (pace_lcg(&seed) % 2) ? PACE_PERIOD : 0;
            appl += pace_lcg(&seed) % (2ull * PACE_PERIOD);
            jts_ring_capture_pointer_inputs_t in = {
                .appl_frames = appl,
                .occupancy_slots = occ,
                .destage_frames = destage,
                .pending_silence_frames = silence,
                .period_frames = PACE_PERIOD,
                .buffer_size = buffer,
            };
            uint64_t got = jts_ring_capture_pointer_report(&got_st, &in);
            uint64_t readable = jts_ring_capture_occupancy_bounded(occ, slots[si]) *
                                    PACE_PERIOD + destage + silence;
            uint64_t want = legacy_clamp(&want_st, appl + readable, buffer, PACE_PERIOD);
            CHECK(got == want, "the capture core is byte-identical to its pre-wave self");
        }
        CHECK(got_st.pace_tokens == 0 && got_st.pace_last_ns == 0 &&
                  got_st.pace_bound_ns == 0,
              "the capture core never touches the governor's bucket");
    }
}

static void test_pace_readerless_writer_is_clock_paced_not_free_running(void) {
    // The governor at the ALSA gate rather than at the core. A readerless ring
    // with the governor on: the gate must grant frames at the nominal rate
    // instead of as fast as the loop spins. The model's loop never sleeps, which
    // is the shape that stormed on metal.
    //
    // Anti-wedge is asserted in the same loop: the gate must REOPEN on every clock
    // advance, so the writer is paced, never blocked, and occupancy stays bounded
    // by the free-run drop underneath.
    char path[256];
    tmp_path(path, sizeof(path), "pace-noreader");
    jts_ring_geometry_t g = proto_geometry();
    g.n_slots = 16;
    jts_ring_writer_t w;
    CHECK(jts_ring_writer_open(path, &g, &w) == 0, "writer open");
    size_t n = w.samples_per_slot;
    int16_t *s = calloc(n, sizeof(int16_t));

    ioplug_model_t m = ioplug_model_new(&g);
    m.pace_nominal = 1;
    m.now_ns = 1;
    jts_ring_pace_arm(&m.ptr, 1, 1, m.now_ns, m.buffer_size);
    const uint64_t buffer = m.buffer_size;
    uint64_t accepted = 0, accepted_at_half = 0;
    const uint64_t window_ms = 200;
    for (uint64_t ms = 1; ms <= window_ms; ms++) {
        m.now_ns = 1 + ms * 1000000ull;
        // 50 spins per simulated millisecond — a consumer that never sleeps.
        for (int spin = 0; spin < 50; spin++) {
            uint64_t avail = ioplug_model_avail(&m, &w);
            if (avail < g.period_frames) break; // the gate closed: this is the pacing
            jts_ring_publish_result_t pr = jts_ring_writer_publish(&w, s);
            CHECK(pr == JTS_RING_PUBLISH_OK || pr == JTS_RING_PUBLISH_DROPPED,
                  "publish still returns (never hangs) with no reader");
            m.appl_frames += g.period_frames;
            accepted += g.period_frames;
            CHECK(jts_ring_writer_occupancy_slots(&w) <= (uint64_t)g.n_slots,
                  "occupancy stays bounded by the free-run drop");
        }
        if (ms == window_ms / 2) accepted_at_half = accepted;
    }

    // STANDING SLACK IS TWO BUFFERS, and this is where that figure is asserted:
    // ALSA lets appl lead the reported position by one buffer regardless of the
    // governor, and the bucket can release another. Beyond that it is rate.
    // Ungoverned, this loop would accept 200 * 50 * 128 = 1 280 000 frames.
    const uint64_t nominal = (window_ms * PACE_RATE) / 1000; // 9600 frames in 200 ms
    const uint64_t slack = 2ull * g.period_frames;
    const uint64_t bound =
        nominal + (nominal * 2500ull) / 1000000ull + 2ull * buffer + slack;
    CHECK(accepted <= bound, "a governed readerless writer is paced, not free-running");
    // The SUSTAINED half — past the startup burst, the standing slack cancels and
    // what is left is rate.
    const uint64_t half_nominal = nominal / 2;
    CHECK(accepted - accepted_at_half <=
              half_nominal + (half_nominal * 2500ull) / 1000000ull + slack,
          "the second half of the window runs at nominal, not at burst rate");
    // ANTI-WEDGE: the writer kept moving throughout. A frozen gate leaves this at
    // (or near) zero, which is the failure the governor must not create.
    CHECK(accepted >= half_nominal,
          "the gate reopened on every clock advance (paced, never wedged)");
    CHECK(m.ptr.pace_bound_ns > 0,
          "the throttle counter records the storm the log line announces");

    free(s);
    jts_ring_writer_close(&w);
    unlink(path);
}
int main(void) {
    // BACKSTOP for a true hang: a wait whose holder never releases, where no
    // elapsed bound is ever reached because the CHECK is never evaluated.
    //
    // It is NOT what catches a lost or raised deadline. The elapsed-time bound
    // in test_writer_ebusy_second_writer is the primary detector there: with the
    // deadline removed the incumbent still eventually releases, so the mutated
    // wait returns LATE (measured at 11 s), not never.
    //
    // The budget is far above any legitimate run — the whole suite is ~1 s
    // wall-clock and the slowest single test is one 500 ms lock wait.
    alarm(120);
    snprintf(g_owned_dir, sizeof(g_owned_dir), "/tmp/jts-ring-ctest-owned-%d",
             (int)getpid());
    CHECK(setenv("JTS_RING_TEST_OWNED_DIR", g_owned_dir, 1) == 0,
          "configure per-process test-owned ring root");
    test_geometry_math_and_validation();
    test_golden_byte_math_table();
    test_wide_slot_publish_consume_roundtrip();
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
    test_writer_ebusy_second_writer();
    test_writer_lock_unopenable_fails_open_and_is_logged();
    test_writer_lock_survives_a_sigkilled_incumbent();
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
    // Pacing governor: a token bucket on the PLAYBACK core.
    test_pace_clock_source_advances();
    test_pace_refill_overflow_bound();
    test_pace_bucket_binds_when_the_app_outruns_the_clock();
    test_pace_is_inert_when_the_device_binds_first();
    test_pace_prepared_writer_is_paced_before_start();
    test_pace_start_grants_the_prefill_without_binding();
    test_pace_reader_reattach_reseeds_the_prefill();
    test_pace_reattach_flapping_stays_bounded();
    test_pace_bound_ns_measures_time_not_call_rate();
    test_pace_start_only_anchors_a_governed_playback_pcm();
    test_pace_log_step_edges_and_rate_limit();
    test_pace_log_survives_a_reprepare_with_a_bind_standing();
    test_pace_burst_is_capped_at_one_buffer_however_long_the_idle();
    test_pace_short_stall_catches_up_in_one_call();
    test_pace_sustained_rate_stays_within_nominal_plus_headroom();
    test_pace_quantizes_only_when_it_binds();
    test_pace_report_is_monotonic_across_mode_changes();
    test_pace_lifecycle_prepare_start_pointer();
    test_pace_timer_cadence();
    test_pace_off_is_byte_identical_to_the_pre_governor_core();
    test_pace_capture_core_is_ungoverned();
    test_pace_readerless_writer_is_clock_paced_not_free_running();
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
