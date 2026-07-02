// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0
//
// JTS Ring B — SHM ping-pong ring, C11 WRITER core (pure, no ALSA).
//
// This header is the C side of the SHM contract v1 documented in
// rust/jasper-ring/src/lib.rs. Every offset here is `_Static_assert`ed against
// the same numbers the Rust `jasper_ring::layout` module pins in its
// golden-layout test — that pair is the cross-language drift guard. If you
// change an offset, change BOTH sides in the same commit or one of the two
// gates (this compile OR the Rust test) fails.
//
// The WRITER is CamillaDSP via the ALSA ioplug (pcm_jts_ring.c); the reader is
// jasper-outputd (rust/jasper-ring). SPSC ping-pong: the writer publishes one
// slot at a time with Release on write_seq; the reader consumes with Acquire on
// write_seq and Release on read_seq. See the ownership discipline in the Rust
// module doc; this core implements the WRITER half plus the shared create/attach
// logic.
//
// PROTOTYPE, flag-gated: this .so is only loaded via a lab-only asound drop-in;
// nothing in the product path references it.

#ifndef JTS_RING_SHM_H
#define JTS_RING_SHM_H

#include <stdatomic.h>
#include <stddef.h>
#include <stdint.h>

// 8-byte atomics must be lock-free for the cross-process SPSC discipline to be
// sound (a locked fallback would not be shared-memory-safe). aarch64 and x86-64
// both provide this; assert it so a surprising target fails to compile rather
// than silently mis-synchronizing.
_Static_assert(ATOMIC_LLONG_LOCK_FREE == 2,
               "JTS ring requires lock-free 8-byte atomics");

#define JTS_RING_MAGIC 0x4A52494Eu /* "JRIN" little-endian */
#define JTS_RING_VERSION 1u
#define JTS_RING_HEADER_BYTES 128u
#define JTS_RING_SAMPLE_FORMAT_S16LE 1u
#define JTS_RING_SAMPLE_FORMAT_S32LE 2u
#define JTS_RING_MIN_SLOTS 2u
#define JTS_RING_MAX_SLOTS 4u

// Writer liveness window (ns): past this heartbeat age the reader is treated as
// gone and the writer free-runs (drops frames) instead of blocking. Mirrors the
// Rust WRITER_LIVENESS_TIMEOUT_NS.
#define JTS_RING_WRITER_LIVENESS_TIMEOUT_NS 2000000000ull

// Bounded spin for the creator's magic during attach (mirrors the Rust reader).
#define JTS_RING_MAGIC_WAIT_TIMEOUT_MS 100ull
#define JTS_RING_MAGIC_WAIT_STEP_US 200ull

// The SHM header. All multi-byte fields are little-endian (the only targets are
// LE). The layout is fixed at 128 bytes; slots begin at JTS_RING_HEADER_BYTES.
// The atomics are declared as _Atomic so the compiler emits ldar/stlr on the
// explicit-order operations; the u32 config fields are plain (init-only).
typedef struct {
    uint32_t magic;                        // 0
    uint32_t version;                      // 4
    uint32_t rate;                         // 8
    uint32_t channels;                     // 12
    uint32_t sample_format;                // 16
    uint32_t period_frames;                // 20
    uint32_t n_slots;                      // 24
    uint32_t _pad;                         // 28
    _Atomic uint64_t writer_epoch;         // 32
    _Atomic uint64_t write_seq;            // 40
    _Atomic uint64_t read_seq;             // 48
    _Atomic uint64_t writer_pid;           // 56
    _Atomic uint64_t writer_heartbeat_ns;  // 64
    _Atomic uint64_t reader_pid;           // 72
    _Atomic uint64_t reader_heartbeat_ns;  // 80
    uint32_t futex_word;                   // 88 (reserved, zero in v1)
    uint8_t reserved[JTS_RING_HEADER_BYTES - 92]; // 92..128
} jts_ring_header_t;

// Golden-layout pins — the same offsets the Rust layout module asserts.
_Static_assert(sizeof(jts_ring_header_t) == JTS_RING_HEADER_BYTES,
               "ring header must be exactly 128 bytes");
_Static_assert(offsetof(jts_ring_header_t, magic) == 0, "magic@0");
_Static_assert(offsetof(jts_ring_header_t, version) == 4, "version@4");
_Static_assert(offsetof(jts_ring_header_t, rate) == 8, "rate@8");
_Static_assert(offsetof(jts_ring_header_t, channels) == 12, "channels@12");
_Static_assert(offsetof(jts_ring_header_t, sample_format) == 16, "sample_format@16");
_Static_assert(offsetof(jts_ring_header_t, period_frames) == 20, "period_frames@20");
_Static_assert(offsetof(jts_ring_header_t, n_slots) == 24, "n_slots@24");
_Static_assert(offsetof(jts_ring_header_t, _pad) == 28, "_pad@28");
_Static_assert(offsetof(jts_ring_header_t, writer_epoch) == 32, "writer_epoch@32");
_Static_assert(offsetof(jts_ring_header_t, write_seq) == 40, "write_seq@40");
_Static_assert(offsetof(jts_ring_header_t, read_seq) == 48, "read_seq@48");
_Static_assert(offsetof(jts_ring_header_t, writer_pid) == 56, "writer_pid@56");
_Static_assert(offsetof(jts_ring_header_t, writer_heartbeat_ns) == 64, "writer_heartbeat_ns@64");
_Static_assert(offsetof(jts_ring_header_t, reader_pid) == 72, "reader_pid@72");
_Static_assert(offsetof(jts_ring_header_t, reader_heartbeat_ns) == 80, "reader_heartbeat_ns@80");
_Static_assert(offsetof(jts_ring_header_t, futex_word) == 88, "futex_word@88");
_Static_assert(offsetof(jts_ring_header_t, reserved) == 92, "reserved@92");

// The geometry a caller wants; validated before touching the filesystem.
typedef struct {
    uint32_t rate;
    uint32_t channels;
    uint32_t sample_format;
    uint32_t period_frames;
    uint32_t n_slots;
} jts_ring_geometry_t;

// The writer's attached ring: the mmap + geometry + a local write_seq mirror
// plus running counters the ioplug/bench print at close.
typedef struct {
    void *base;          // mmap base (the header, then slots)
    size_t map_len;      // mmapped byte length
    int fd;              // the shm fd
    jts_ring_geometry_t geometry;
    uint64_t write_seq;  // local mirror of the header write_seq
    size_t slot_bytes;
    size_t samples_per_slot;
    // Counters (writer-side observability).
    uint64_t published_slots;
    uint64_t drop_no_reader;   // slots discarded because no live reader
    uint64_t full_waits;       // publish attempts that had to wait for space
} jts_ring_writer_t;

// Result of jts_ring_writer_publish.
typedef enum {
    JTS_RING_PUBLISH_OK = 0,      // published into the ring
    JTS_RING_PUBLISH_DROPPED = 1, // no live reader: free-ran, dropped the frames
    JTS_RING_PUBLISH_ERROR = -1,  // fatal (should not happen mid-run)
} jts_ring_publish_result_t;

// --- Geometry helpers (pure) ---

size_t jts_ring_slot_bytes(const jts_ring_geometry_t *g);
size_t jts_ring_samples_per_slot(const jts_ring_geometry_t *g);
size_t jts_ring_file_size(const jts_ring_geometry_t *g);
// Returns 0 on valid, non-zero (a static reason string is set via *reason) on
// an unsupported geometry.
int jts_ring_geometry_validate(const jts_ring_geometry_t *g, const char **reason);

// --- Writer attach / publish / close ---

// Create-or-attach as the WRITER: O_EXCL create (init + magic-last) or attach
// (bounded magic wait + geometry validation against `expected`). On attach the
// writer bumps writer_epoch, stamps writer_pid, and continues from the stored
// write_seq. Returns 0 on success (fills *out), <0 (negative errno-ish) on a
// fatal error. `path` must be an absolute /dev/shm/jts-ring/... path for the
// magic-invalid reclaim to be permitted.
int jts_ring_writer_open(const char *path, const jts_ring_geometry_t *expected,
                         jts_ring_writer_t *out);

// Publish one slot from `samples` (jts_ring_samples_per_slot interleaved i16).
// Space discipline: load read_seq (Acquire); if W - R < n_slots, memcpy the
// payload and store write_seq+1 (Release). If full: check reader liveness
// (reader_pid != 0 AND heartbeat < 2 s). Reader alive -> clamped nanosleep,
// re-check up to a bounded number of tries (productization: FUTEX_WAIT). Reader
// dead/absent -> free-run DROP (return JTS_RING_PUBLISH_DROPPED) so Camilla
// never wedges when outputd's flag is off. Always updates writer_heartbeat_ns.
jts_ring_publish_result_t jts_ring_writer_publish(jts_ring_writer_t *w,
                                                  const int16_t *samples);

// Frames of buffering currently in-flight (W - R), for the ioplug `delay`
// callback: (W - R) * period_frames.
uint64_t jts_ring_writer_occupancy_slots(const jts_ring_writer_t *w);

// True (1) iff a publish would proceed without blocking right now: either the
// ring has space (occupancy < n_slots) OR there is no live reader (in which case
// publish free-run-drops immediately). Used by the ioplug's poll_revents to
// report POLLOUT honestly — space-or-free-run is "writable"; a full ring WITH a
// live reader is genuinely not-yet-writable, so we withhold POLLOUT and let the
// timerfd re-poll rather than busy-spinning the app on a slot it cannot take.
int jts_ring_writer_can_accept(const jts_ring_writer_t *w);

// Detach: clear writer_pid (if ours), munmap, close. Safe on a zeroed struct.
void jts_ring_writer_close(jts_ring_writer_t *w);

// CLOCK_MONOTONIC nanoseconds (shared by the writer heartbeat and the wait
// helper). Exposed for the bench + host test.
uint64_t jts_ring_monotonic_ns(void);

#endif // JTS_RING_SHM_H
