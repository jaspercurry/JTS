// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0
//
// JTS Ring B — SHM ping-pong ring, C11 WRITER core (pure, no ALSA).
//
// C side of the SHM contract v1 in rust/jasper-ring/src/lib.rs. Every offset
// here is `_Static_assert`ed against the same numbers the Rust
// `jasper_ring::layout` module pins in its golden-layout test; change an
// offset on both sides in the same commit or one of the two gates fails.
//
// WRITER: CamillaDSP via the ALSA ioplug (pcm_jts_ring.c). READER:
// jasper-outputd (rust/jasper-ring). SPSC ping-pong: the writer publishes one
// slot at a time with Release on write_seq; the reader consumes with Acquire
// on write_seq and Release on read_seq.

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

// The one rate a JTS ring carries; `jts_ring_geometry_validate` accepts no
// other and the paced waits divide by it. Rust twin: `jasper_ring::RATE_HZ`.
#define JTS_RING_RATE_HZ 48000u

// Channel accept-set for the wire: 2..=8. Floor 2: every producer in the graph
// is at least a stereo program; explicit mono is representable in the layout
// but refused by policy. Ceiling 8: the widest registered DAC (DAC8x) is
// 8-channel, and jasper-outputd bounds JASPER_OUTPUTD_ACTIVE_CHANNELS by
// reading this ceiling out of `jasper_ring`. test_ring_core asserts
// JTS_RING_MAX_CHANNELS == the generated ring ABI's max_ring_channels
// (rust/jasper-ring/layout.json); change both in the same commit.
#define JTS_RING_MIN_CHANNELS 2u
#define JTS_RING_MAX_CHANNELS 8u

// Slot-payload ceiling (bytes), enforced by jts_ring_geometry_validate. Bounds
// the WRITER-CREATE path: create ftruncates
// JTS_RING_HEADER_BYTES + n_slots * slot_bytes straight from the requested
// geometry (period_frames is otherwise checked only "> 0"), so this cap is
// what keeps a bad geometry from asking the kernel for an unbounded file.
// Shipped worst case: 8 ch x S32 x 128 frames = 4096 B; 64 KiB leaves room to
// grow the slot floor (issue #2147) without removing the bound.
#define JTS_RING_MAX_SLOT_BYTES 65536u

#define JTS_RING_MIN_SLOTS 2u
// Ceiling 16: CamillaDSP negotiates
// buffer = next_pow2(max(3*chunksize, 4*min_period)) and drives its rate
// controller toward `target_level` frames of device delay. With slot_frames
// pinned at 128 (the outputd DAC-period contract), n_slots is the ONLY axis
// for buffer depth (buffer = n_slots * period_frames). At n_slots=4 the
// 512-frame buffer sat below both camilla's negotiated 1024 and its
// target_level (1536), so the rate controller chased an unreachable target and
// drove the writer full into stall/underrun flapping. 16 slots => 2048 frames
// >= target_level with headroom. See ADR-0261.
// Must stay in lockstep with MAX_N_SLOTS (rust/jasper-ring/src/layout.rs);
// test_ring_core asserts it against the generated ring ABI's max_n_slots.
#define JTS_RING_MAX_SLOTS 16u

// Writer liveness window (ns): past this heartbeat age the reader treats the
// writer as gone and free-runs (drops frames) instead of blocking. Mirrors the
// Rust WRITER_LIVENESS_TIMEOUT_NS.
//
// THE HEARTBEAT OWNS OBSERVABILITY, THE LOCK OWNS EXCLUSIVITY. A C writer holds
// an EXCLUSIVE flock on <ring>.writer.lock for the life of its mapping, so this
// window decides only what a reader REPORTS (`writer_alive` in /state) and when
// a blocked writer gives up on a dead reader. The lock is fd-scoped
// (kernel-released on process death, including SIGKILL); the heartbeat is
// stamped on publish, so a SIGKILLed writer leaves it frozen and for up to this
// window a fresh writer is still refused by foreign_writer_is_live even though
// the lock is already free.
//
// A ring writer's RestartSec must not fall below this window, or a fast
// respawn races its SIGKILLed predecessor's frozen heartbeat into an avoidable
// -EBUSY; if the unit's start limit is tight, that loop PARKS it (the one
// non-self-healing shape here). Every ring writer, checked:
//
//   - jasper-camilla               RestartSec=2  — sits ON the boundary
//   - jasper-snapclient.service    RestartSec=3  — the grouping ingress
//     ring on a bonded endpoint, StartLimitBurst=6: a follower whose leader
//     is powered off retries into this window, so the burst decides
//     whether it re-joins or parks
//     (pinned by tests/test_grouping_ring_platform.py)
// The window itself is pinned by
// test_writer_lock_survives_a_sigkilled_incumbent.
//
// A Rust `RingWriter` does not take the lock (fan-in owns Ring A by
// construction), so on a Rust-written ring the heartbeat is the only
// exclusivity signal — why `foreign_writer_is_live` still runs there.
#define JTS_RING_WRITER_LIVENESS_TIMEOUT_NS 2000000000ull

// One bounded attach budget covers the O_EXCL creator's ftruncate and its
// magic-last publish (mirrors the Rust reader). A size<header file is not torn
// until this budget expires: the live creator may still be pre-ftruncate.
#define JTS_RING_MAGIC_WAIT_TIMEOUT_MS 100ull
#define JTS_RING_MAGIC_WAIT_STEP_US 200ull

// Cross-language create/attach transaction lock. Both the C ioplug and the
// Rust reader/writer take `<ring path>.open.lock` before classifying an
// existing inode, reclaiming it, creating/initializing a replacement, and
// verifying that the initialized fd still owns the linked pathname. Bounded so
// a wedged opener cannot stall audio startup indefinitely.
#define JTS_RING_OPEN_LOCK_SUFFIX ".open.lock"

// Adjacent lock file whose EXCLUSIVE flock a C writer holds for the LIFE of its
// mapping — the fd-scoped half of the SPSC writer guard. Distinct from
// JTS_RING_OPEN_LOCK_SUFFIX, which is released as soon as the open transaction
// completes.
#define JTS_RING_WRITER_LOCK_SUFFIX ".writer.lock"
#define JTS_RING_OPEN_LOCK_MODE 0660
#define JTS_RING_OPEN_LOCK_WAIT_TIMEOUT_MS 500ull
#define JTS_RING_OPEN_LOCK_WAIT_STEP_US 1000ull
#define JTS_RING_OPEN_MAX_ATTEMPTS 8u

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
    // EXCLUSIVE flock held for the life of this mapping (see
    // JTS_RING_WRITER_LOCK_SUFFIX). -1 means NOT HELD: acquire_writer_lock
    // could not open or chmod the lock file, so the open proceeded fail-open on
    // the heartbeat guard alone. A writer REFUSED the lock never gets a struct,
    // so a live writer with -1 here is running WITHOUT fd-scoped exclusivity.
    //
    // Release ordering: jts_ring_writer_close releases this fd AFTER its
    // `if (!w || !w->base) return;` guard, so never store the fd before the
    // mapping without moving the release ahead of that guard.
    int writer_lock_fd;
} jts_ring_writer_t;

// Result of jts_ring_writer_publish.
typedef enum {
    JTS_RING_PUBLISH_OK = 0,      // published into the ring
    JTS_RING_PUBLISH_DROPPED = 1, // no live reader: free-ran, dropped the frames
    JTS_RING_PUBLISH_ERROR = -1,  // fatal (should not happen mid-run)
} jts_ring_publish_result_t;

// The reader's attached ring (Ring A CAPTURE direction). Mirrors the Rust
// jasper_ring::RingReader. Unlike the writer struct this carries a LOCAL
// read_seq mirror the reader owns while live; the writer only borrows read_seq
// on its no-live-reader free-run path (the SPSC contract is in
// rust/jasper-ring/src/lib.rs).
typedef struct {
    void *base;          // mmap base (the header, then slots)
    size_t map_len;      // mmapped byte length
    int fd;              // the shm fd
    jts_ring_geometry_t geometry;
    uint64_t read_seq;   // local mirror of the header read_seq (reader-owned while live)
    uint64_t last_epoch; // last-observed writer_epoch; a change = writer reattach
    size_t slot_bytes;
    size_t samples_per_slot;
    int saw_filled;      // 0 until the first Filled read (startup-vs-steady empty split)
    // Counters (reader-side observability; the capture ioplug + reader bench
    // print these at close).
    uint64_t frames_read_slots;  // slots consumed (== reader-owned read_seq advances)
    uint64_t empty_reads;        // ring-empty reads AFTER the first fill (steady slips)
    uint64_t startup_empty_reads; // ring-empty reads BEFORE the first fill (priming)
    uint64_t reader_resyncs;     // defensive resyncs (W - R > n_slots — should be 0)
    uint64_t attach_resyncs;     // resyncs at attach (1 iff write_seq > 0)
    uint64_t epoch_resets;       // observed writer_epoch changes (writer reattached)
    uint64_t occupancy;          // W - R at the last read (0..=n_slots)
} jts_ring_reader_t;

// Result of jts_ring_reader_consume.
typedef enum {
    JTS_RING_SLOT_FILLED = 1, // a slot was copied into `out`; read_seq advanced
    JTS_RING_SLOT_EMPTY = 0,  // ring empty; `out` zero-filled (caller emits silence)
} jts_ring_slot_read_t;

// --- Geometry helpers (pure) ---

size_t jts_ring_slot_bytes(const jts_ring_geometry_t *g);
size_t jts_ring_samples_per_slot(const jts_ring_geometry_t *g);
size_t jts_ring_file_size(const jts_ring_geometry_t *g);
// Bytes per sample for a ring sample_format id — the ONE place the format enum
// becomes a width, shared by the geometry math here and the ioplug's staging
// strides. An unrecognized id answers 2 and never reaches a copy path
// (jts_ring_geometry_validate rejects it before any mapping). Mirrors
// Geometry::bytes_per_sample in rust/jasper-ring/src/layout.rs on the valid ids
// {S16LE=1, S32LE=2}; the two diverge deliberately on an unrecognized id (this
// returns 2 for bounded diagnostic sizing, Rust returns Err).
size_t jts_ring_bytes_per_sample(uint32_t sample_format);
// Returns 0 on valid, non-zero (a static reason string is set via *reason) on
// an unsupported geometry. Accept-set: sample_format in {S16LE, S32LE},
// channels 2..=8, rate 48000, period_frames > 0, n_slots 2..=16, and a slot
// payload within JTS_RING_MAX_SLOT_BYTES. Both ends of the wire must accept
// exactly this set — `jasper_ring::Geometry::validate_self` is the Rust half,
// and a geometry one end creates but the other refuses fails at attach on-Pi.
// The channel ceiling, the rate and the format ids are pinned across the two by
// test_ring_core's test_header_matches_the_generated_ring_abi.
int jts_ring_geometry_validate(const jts_ring_geometry_t *g, const char **reason);

// --- Writer attach / publish / close ---

// Create-or-attach as the WRITER: O_EXCL create (init + magic-last) or attach
// (bounded size+magic wait + geometry validation against `expected`). On attach the
// writer bumps writer_epoch, stamps writer_pid, and continues from the stored
// write_seq. Returns 0 on success (fills *out), <0 (negative errno-ish) on a
// fatal error. `path` must be an absolute /dev/shm/jts-ring/... path for the
// magic-invalid reclaim to be permitted.
int jts_ring_writer_open(const char *path, const jts_ring_geometry_t *expected,
                         jts_ring_writer_t *out);

// Publish one slot from `slot` — exactly jts_ring_slot_bytes(&w->geometry)
// bytes of interleaved samples in the ring's declared format. Byte-oriented:
// memcpys the payload and never interprets a sample, so the caller owns the
// typed view at its own boundary.
// Space discipline: load read_seq (Acquire); if W - R < n_slots, memcpy and
// store write_seq+1 (Release). If full: check reader liveness (reader_pid != 0
// AND heartbeat < JTS_RING_WRITER_LIVENESS_TIMEOUT_NS). Reader alive ->
// clamped nanosleep, bounded retries. Reader dead/absent -> FREE-RUN: advance
// read_seq on its behalf (Release), drop the oldest slot, publish over the
// freed lap (JTS_RING_PUBLISH_DROPPED), so occupancy stays bounded and
// CamillaDSP never wedges when outputd's flag is off. Always updates
// writer_heartbeat_ns.
//
// Do not "optimize" the free-run into a bare drop-newest: advancing read_seq —
// not just discarding — is what bounds occupancy, and both mechanisms in
// jts_ring_pointer_report below depend on that.
jts_ring_publish_result_t jts_ring_writer_publish(jts_ring_writer_t *w,
                                                  const void *slot);

// Frames of buffering currently in-flight (W - R), for the ioplug `delay`
// callback: (W - R) * period_frames.
uint64_t jts_ring_writer_occupancy_slots(const jts_ring_writer_t *w);

// True (1) iff a reader is currently live: reader_pid != 0 AND its heartbeat is
// younger than JTS_RING_WRITER_LIVENESS_TIMEOUT_NS. Exposes the same predicate
// jts_ring_writer_publish/can_accept use, so the ioplug's `pointer`/`delay` can
// run the dual-mode avail contract. Reads relaxed atomics only.
int jts_ring_writer_reader_is_live(const jts_ring_writer_t *w);

// True (1) iff a publish would proceed without blocking right now: either the
// ring has space (occupancy < n_slots) OR there is no live reader (publish
// free-run-drops immediately). Used by the ioplug's poll_revents to report
// POLLOUT honestly — space-or-free-run is "writable"; a full ring WITH a live
// reader is genuinely not-yet-writable, so POLLOUT is withheld and the timerfd
// re-polls rather than busy-spinning the app on a slot it cannot take.
int jts_ring_writer_can_accept(const jts_ring_writer_t *w);

// Detach: clear writer_pid (if ours), munmap, close. Safe on a zeroed struct.
void jts_ring_writer_close(jts_ring_writer_t *w);

// --- Reader attach / consume / close (Ring A CAPTURE direction) ---

// Create-or-attach as the READER: O_EXCL create (init + magic-last) or attach
// (bounded size+magic wait + geometry validation against `expected`), then
// resync read_seq = write_seq and publish it (Release), stamp reader_pid +
// reader_heartbeat, and snapshot writer_epoch for reattach detection.
// SPSC GUARD: the ring tolerates EXACTLY ONE reader. A live foreign reader_pid
// (pid != 0, pid != getpid(), heartbeat younger than the liveness window)
// refuses the open with -EBUSY and stamps nothing, or a stray second
// `arecord -D jts_ring_capture` would corrupt read_seq. The Rust `RingReader`
// runs the same predicate over the same fields and window.
// Returns 0 on success (fills *out), <0 (negative errno-ish) on a fatal error
// (-EBUSY on a live foreign reader, -EINVAL on geometry mismatch). `path` must
// be an absolute /dev/shm/jts-ring/... path for the magic-invalid reclaim.
int jts_ring_reader_open(const char *path, const jts_ring_geometry_t *expected,
                         jts_ring_reader_t *out);

// Consume the OLDEST unread slot into `out` — exactly
// jts_ring_slot_bytes(&r->geometry) bytes of interleaved samples in the ring's
// declared format (byte-oriented, same contract as publish above).
// NEVER blocks. Stamps reader_heartbeat + observes epoch on EVERY call, filled
// or not: the writer's block-vs-drop gate reads that heartbeat. Defensive: if
// W - R > n_slots, fast-forwards read_seq = write_seq and counts
// reader_resyncs rather than reading a slot the writer may be mid-overwriting.
// Returns JTS_RING_SLOT_FILLED (copied + advanced read_seq with Release) or
// JTS_RING_SLOT_EMPTY (zero-filled `out`).
jts_ring_slot_read_t jts_ring_reader_consume(jts_ring_reader_t *r, void *out);

// Self-heal an out-of-range occupancy (observable by a reader that wedged past
// the liveness window while the writer free-ran drop-oldest): fast-forward the
// local read_seq to the tip and publish it (Release), counting one
// reader_resync. Returns 1 iff a resync happened.
// Extracted from jts_ring_reader_consume so the capture ioplug's per-wake
// service tick can run it without a consume call: at avail 0 alsa-lib never
// calls transfer, so an out-of-range occupancy would otherwise wedge the reader
// in permanent silence against a LIVE writer. Never discards readable data —
// only fires once the writer has already lapped the reader.
int jts_ring_reader_resync_if_overrun(jts_ring_reader_t *r);

// Frames of buffering readable right now (W - R) * period_frames, for the
// capture ioplug's avail/pointer honesty. Reads read_seq from the local mirror
// (the reader owns it) and write_seq with Acquire.
uint64_t jts_ring_reader_occupancy_slots(const jts_ring_reader_t *r);

// True (1) iff the WRITER is currently live: writer_pid != 0 AND its heartbeat
// is younger than JTS_RING_WRITER_LIVENESS_TIMEOUT_NS. The capture side uses
// this to decide the writer-dead silence path (empty + writer dead -> fabricate
// timer-paced silence; empty + writer alive -> withhold POLLIN so camilla blocks
// = the pacing). Same-process convenience wrapper over the reader's mmap.
int jts_ring_reader_writer_is_live(const jts_ring_reader_t *r);

// Detach: clear reader_pid (only if ours — a later reader that stamped its own
// pid must keep its presence), munmap, close. Safe on a zeroed struct.
void jts_ring_reader_close(jts_ring_reader_t *r);

// CLOCK_MONOTONIC nanoseconds (shared by the writer heartbeat and the wait
// helper). Exposed for the bench + host test.
uint64_t jts_ring_monotonic_ns(void);

// CLOCK_MONOTONIC_RAW ns — the governor's base. NOT the clock above: that one is
// NTP-disciplined, and slew would bind the governor against healthy hardware.
uint64_t jts_ring_monotonic_raw_ns(void);

// Rate headroom, ppm. The governed quantity is a DIFFERENCE of independent clocks
// (the wire's crystal vs this Pi's), so a crystal's own +-100 ppm spec is the wrong
// size: this fleet's dongle measures ~667 ppm and the same two-crystal problem took
// ~4x that (2667 ppm). An exact reciprocal of 1e6 keeps the refill integral.
// See ADR-0261.
//
// BARS BELONG AGAINST THE DERIVED BOUND, not against this number flat. Every
// truncation in the path rounds DOWN, so what a finite measurement adds is
// granularity — one period of grant quantization at each end of a window:
//     observed_ppm <= HEADROOM_PPM + 1e6*(2*period_frames)/(rate*T) + instrument
// At a 128-frame period that is 2589 ppm over 60 s.
// THAT FORM IS THE INTERIOR ONE: it assumes a window with no stream start, no
// reattach, and no STARVATION EXIT in or immediately before it. Each of those
// releases a one-time quantity, not rate:
//   - start or reattach: + 1e6*(2*buffer_size)/(rate*T) — the seed plus the app's
//     standing one-buffer lead. 1422 ppm over 60 s here, so a from-start window is
//     bounded by 2589 + 1422 = 4011 ppm.
//   - starvation exit: + 1e6*(buffer_size - period_frames)/(rate*T) — the alias
//     clamp's single catch-up step across the boundary, 667 ppm over 60 s, carried
//     by the CLAMP and not the bucket.
#define JTS_RING_PACE_HEADROOM_PPM 2500ull
#define JTS_RING_PACE_HEADROOM_DIVISOR (1000000ull / JTS_RING_PACE_HEADROOM_PPM)
_Static_assert(JTS_RING_PACE_HEADROOM_DIVISOR * JTS_RING_PACE_HEADROOM_PPM == 1000000ull,
               "pace headroom must divide 1e6 exactly (the refill math is integer)");

// Sub-frame tokens, because the refill is per CALL: at frame resolution every
// call's remainder is discarded — ~0.78% loss against 0.25% headroom, which
// throttles a healthy stream. Both figures scale with CALL RATE, quoted at the
// ~375/s the governed poll cadence enforces; 1/1024 frame puts the loss at ~8 ppm.
#define JTS_RING_PACE_TOKENS_PER_FRAME 1024ull

// Tokens a device at `rate` clocks in `elapsed_ns`, plus the headroom.
//
// OVERFLOW BOUND at the shipped rate 48000, over the whole u64 input range (a
// paused stream can go arbitrarily long between calls). `elapsed_ns * rate/1e9`
// wraps past 4.4 days, silently zeroing a refill; splitting on the second does not:
//   secs             <= (2^64-1)/1e9            = 1.845e10  (584.9 yr)
//   secs*rate*1024   <= 1.845e10 * 48000 * 1024 = 9.07e17
//   rem_ns*rate*1024 <  1e9 * 48000 * 1024      = 4.92e16
//   base + base/400                             <= 9.09e17  = 20x below UINT64_MAX
static inline uint64_t jts_ring_pace_refill_tokens(uint64_t elapsed_ns, uint32_t rate) {
    if (rate == 0) return 0;
    uint64_t secs = elapsed_ns / 1000000000ull;
    uint64_t rem_ns = elapsed_ns % 1000000000ull;
    uint64_t scaled = (uint64_t)rate * JTS_RING_PACE_TOKENS_PER_FRAME;
    uint64_t base = secs * scaled + (rem_ns * scaled) / 1000000000ull;
    return base + base / JTS_RING_PACE_HEADROOM_DIVISOR;
}

// Poll/pacing cadence — pure, so the host test drives the plugin's own choice.
static inline uint64_t jts_ring_period_ns(uint32_t period_frames, uint32_t rate) {
    if (rate == 0) return 0;
    return (uint64_t)period_frames * 1000000000ull / (uint64_t)rate;
}

// period/4, clamped to [0.25 ms, 2 ms]: every ungoverned poll and both naps.
static inline uint64_t jts_ring_tick_ns(uint32_t period_frames, uint32_t rate) {
    uint64_t tick_ns = jts_ring_period_ns(period_frames, rate) / 4;
    if (tick_ns < 250000ull) tick_ns = 250000ull;
    if (tick_ns > 2000000ull) tick_ns = 2000000ull;
    return tick_ns;
}

// The timerfd interval. A governed PLAYBACK PCM polls at the period: it waits on
// the bucket, which releases in period grains. CAPTURE is excluded even when the
// field is set — its wake also drives capture_service_tick's wall-clock silence
// gate, and one period critically-samples that gate.
static inline uint64_t jts_ring_timer_cadence_ns(int pace_nominal, int stream_is_playback,
                                                 uint32_t period_frames, uint32_t rate) {
    if (pace_nominal && stream_is_playback) return jts_ring_period_ns(period_frames, rate);
    return jts_ring_tick_ns(period_frames, rate);
}

// --- ioplug pointer core (shared by pcm_jts_ring.c and test_ring_core.c) ---
//
// The one function that computes the value the ioplug `pointer` callback
// returns to ALSA. `static inline` in the header (not a .c symbol) so the
// plugin (compiled only on-Pi with alsa-lib) and the host test call the SAME
// code. It owns the reported-position discipline in one place:
//
//   1. DUAL-MODE in_flight. Reader LIVE -> honest occupancy-derived in_flight
//      (occupancy*period + stage); reader DEAD -> stage-only in_flight, so
//      published-but-unread slots discount to 0 and ALSA's `avail` never
//      sticks at 0 on a readerless ring.
//
//   2. REPORTED-POSITION clamp. ALSA infers hw motion in
//      snd_pcm_ioplug_hw_ptr_update as
//        delta = (this_pointer_return - last_pointer_return) mod buffer_size
//      so a RAW advance of exactly buffer_size between two pointer reads
//      aliases to delta 0 — ALSA's hw_ptr falls a lap behind and `avail` pins
//      at 0 permanently; an advance > buffer_size aliases to an apparent
//      backward delta, which is worse. Three shapes produce an
//      exactly-buffer_size raw jump: a live reader draining a full ring during
//      an app gap >= one buffer duration, the dead-mode discount flip at
//      occupancy == n_slots, and the dead->live recovery.
//
//      So `last_reported` (pre-modulo) advances at most
//      buffer_size - period_frames per call, and a true full-buffer jump
//      completes over successive poll ticks (jts_ring_timer_cadence_ns) as
//      sub-buffer deltas. Forward-only, so the clamp is also the monotonic
//      floor — one unified state, not two clamps.
//
// The caller returns `reported % buffer_size` to ALSA; `last_reported` stays
// the raw value, so the delta math above always has the pre-modulo position.

// The state the pointer core carries across calls, cleared on (re)prepare. The
// plugin embeds it in jts_ring_pcm_t; the host test embeds it in its ioplug model.
typedef struct {
    uint64_t last_reported; // last raw hw_ptr handed to ALSA (pre-modulo)
    // Governor bucket; all three stay zero on an ungoverned PCM. pace_last_ns is
    // also the started flag — 0 means `start` has not run and the governor is inert.
    uint64_t pace_last_ns; // clock sample at the previous governed call
    uint64_t pace_tokens;  // credit, in 1/1024-frame tokens
    // Cumulative wall time spent binding. NOT frames: `want` is a standing backlog
    // re-presented every call, so summing `want - grant` reads O(call rate).
    uint64_t pace_bound_ns;
    int pace_prev_reader_live; // for the dead->live re-seed edge; 1 after start
} jts_ring_pointer_state_t;

static inline void jts_ring_pointer_state_reset(jts_ring_pointer_state_t *st) {
    st->last_reported = 0;
    st->pace_last_ns = 0;
    st->pace_tokens = 0;
    st->pace_bound_ns = 0;
    st->pace_prev_reader_live = 0;
}

// Arm the bucket and seed it FULL — a real device absorbs its prefill at once;
// an empty bucket would pace that prefill at the ceiling (a 57 s startup bind
// measured on hardware, see test_ring_core.c). One buffer, once, so it costs no
// rate. `prev_reader_live` starts at 1 so an already-live reader is not read as
// a dead->live edge and re-seeded on top. Clock is forced nonzero: 0 is the
// not-armed sentinel.
static inline void jts_ring_pace_arm(jts_ring_pointer_state_t *st, int pace_nominal,
                                     int stream_is_playback, uint64_t now_ns,
                                     uint64_t buffer_size) {
    if (!pace_nominal || !stream_is_playback) return;
    st->pace_last_ns = (now_ns == 0) ? 1 : now_ns;
    st->pace_tokens = buffer_size * JTS_RING_PACE_TOKENS_PER_FRAME;
    st->pace_prev_reader_live = 1;
}

// The (re)prepare transition as one step: clear the report, then arm. Arming
// belongs at PREPARE, not at START: an unarmed bucket makes jts_ring_pace_apply
// early-return, and a PCM can transfer indefinitely while still PREPARED (with
// start_threshold > period and a dead reader, ALSA's start condition never
// trips against the dead-reader discount), which armed-at-start left as
// ungoverned free-run.
static inline void jts_ring_pointer_prepare(jts_ring_pointer_state_t *st, int pace_nominal,
                                            int stream_is_playback, uint64_t now_ns,
                                            uint64_t buffer_size) {
    jts_ring_pointer_state_reset(st);
    jts_ring_pace_arm(st, pace_nominal, stream_is_playback, now_ns, buffer_size);
}

// THE PACING GOVERNOR — one owner, PLAYBACK only. A floor under the failure a
// DAC-clocked reader does not have: a stalled reader let this ring's writer
// storm at 763x where a live one held it to 1.00x (See ADR-0261). CAPTURE never
// calls it: a bind there would starve camilla on a DAC-vs-Pi clock difference.
//
// A token bucket anchored to the PREVIOUS call. Each call refills by what a
// nominal device would have clocked in `now_ns - pace_last_ns` plus the
// headroom, caps at `buffer_size`, and grants at most what it holds. Returns
// `honest` unchanged when the bucket covers the whole advance (a device-paced
// app reads bit-for-bit as it would with no governor); otherwise
// `last_reported + granted`. `pace_nominal == 0`, or an unanchored bucket,
// returns `honest` untouched.
//
// Runs BEFORE the clamp so it sees the app's real demand: token spend and
// bound accounting are functions of `want`, which the clamp has not yet
// truncated to buffer - period.
//
// Per-call anchoring (not an absolute one) is what keeps this from
// integrating: an absolute anchor would accumulate a persistent clock
// difference without bound — a deficit that binds a healthy stream, or a
// surplus released in one burst when the reader dies. The cap bounds burst
// outright instead: one wake advances at most one buffer, however long the
// stream idled. A partial grant is floored to a period multiple.
//
// Re-seeds on reader dead->live — the same event that resyncs read_seq, so the
// re-prepared device gets its prefill again. Edge only, bounded by the liveness
// window (2 s), so flapping caps at one buffer per 2 s = 1024 f/s against
// 48000, ~2.1%.
//
// A restarted reader is not a resumed one: a fresh process starts with empty
// buffers that only the headroom surplus refills, so re-lock takes at least
// downstream_buffer/headroom — 42.67 ms / 2500 ppm = ~17 s at today's
// constants (~44 s observed on hardware). Sizing the re-seed to the consumer's
// buffer would shorten it and is deliberately NOT done: the plugin cannot know
// foreign buffers.
static inline uint64_t jts_ring_pace_apply(jts_ring_pointer_state_t *st, uint64_t honest,
                                           int pace_nominal, uint64_t now_ns, uint32_t rate,
                                           uint64_t buffer_size, uint32_t period_frames,
                                           int reader_live) {
    if (!pace_nominal || st->pace_last_ns == 0) return honest;
    // Refill by real elapsed time, then cap. A backward clock sample refills
    // nothing rather than underflowing.
    uint64_t dt = (now_ns > st->pace_last_ns) ? (now_ns - st->pace_last_ns) : 0;
    st->pace_last_ns = now_ns;
    uint64_t cap = buffer_size * JTS_RING_PACE_TOKENS_PER_FRAME;
    if (reader_live && !st->pace_prev_reader_live) st->pace_tokens = cap; // reattach
    st->pace_prev_reader_live = reader_live;
    st->pace_tokens += jts_ring_pace_refill_tokens(dt, rate);
    if (st->pace_tokens > cap) st->pace_tokens = cap;

    uint64_t want = (honest > st->last_reported) ? (honest - st->last_reported) : 0;
    if (want == 0) return honest; // nothing to grant; the clamp holds the floor
    uint64_t affordable = st->pace_tokens / JTS_RING_PACE_TOKENS_PER_FRAME;
    if (want <= affordable) {
        st->pace_tokens -= want * JTS_RING_PACE_TOKENS_PER_FRAME; // spend what we grant
        return honest;                                            // governor inert
    }
    uint64_t grant = affordable;
    if (period_frames > 0) grant -= grant % (uint64_t)period_frames;
    st->pace_tokens -= grant * JTS_RING_PACE_TOKENS_PER_FRAME;
    st->pace_bound_ns += dt; // this call's interval was spent binding
    return st->last_reported + grant;
}

// Bind/release edge detection (pure; the plugin only prints the result). Needed
// because a governed PCM is QUIET where the storm above was self-announcing.
typedef enum {
    JTS_RING_PACE_LOG_NONE = 0,
    JTS_RING_PACE_LOG_BIND = 1,
    JTS_RING_PACE_LOG_RELEASE = 2,
} jts_ring_pace_log_event_t;

typedef struct {
    uint64_t prev_bound_ns; // pace_bound_ns at the previous call
    uint64_t last_log_ns;   // when the standing bind was announced; 0 = never
    int bound;              // last announced state
} jts_ring_pace_log_state_t;

// EDGES only (a bound governor is the steady state under a dead reader), one
// bind per `interval_ns`; a suppressed bind leaves `bound` clear so its release
// is suppressed with it. `last_log_ns == 0` is a SENTINEL, never a timestamp to
// subtract from — `now_ns` is CLOCK_MONOTONIC_RAW, so a PCM opened inside the
// first interval after boot would otherwise lose its first bind. An edge inside
// a prior edge's window is deliberately silent and re-announces on the next
// window: a real bind can go unlogged while the governor is working.
static inline jts_ring_pace_log_event_t
jts_ring_pace_log_step(jts_ring_pace_log_state_t *ls, uint64_t bound_ns, uint64_t now_ns,
                       uint64_t interval_ns) {
    int bound_now = bound_ns > ls->prev_bound_ns;
    ls->prev_bound_ns = bound_ns;
    if (bound_now == ls->bound) return JTS_RING_PACE_LOG_NONE;
    if (bound_now && ls->last_log_ns != 0 && now_ns - ls->last_log_ns < interval_ns) {
        return JTS_RING_PACE_LOG_NONE;
    }
    ls->bound = bound_now;
    if (bound_now) {
        ls->last_log_ns = now_ns;
        return JTS_RING_PACE_LOG_BIND;
    }
    return JTS_RING_PACE_LOG_RELEASE;
}

// Inputs the pointer core needs, gathered by the caller (which owns the ALSA
// io object / the writer handle). Keeping them in a struct lets the host test
// drive the exact same function without an ALSA io or a live writer mmap.
typedef struct {
    uint64_t appl_frames;    // ALSA appl_ptr mirror (frames accepted from app)
    uint64_t occupancy_slots; // write_seq - read_seq (published-but-unread)
    uint64_t stage_frames;   // frames staged but not yet a whole slot
    uint32_t period_frames;  // frames per slot
    uint64_t buffer_size;    // n_slots * period_frames (== ALSA buffer)
    int reader_live;         // 1 iff a reader heartbeat is fresh
    int pace_nominal;        // governor opt-in (jts_ring_pace_apply); 0 = ungoverned
    uint64_t now_ns;         // jts_ring_monotonic_raw_ns() sample for this call
    uint32_t rate;           // wire rate, for the bucket refill
} jts_ring_pointer_inputs_t;

// Compute the RAW (pre-modulo) hw_ptr to report to ALSA, advancing/clamping
// `st->last_reported`. The caller returns `result % buffer_size`. Pure: no ALSA,
// no atomics — the caller samples occupancy/liveness and passes them in.
static inline uint64_t jts_ring_pointer_report(jts_ring_pointer_state_t *st,
                                               const jts_ring_pointer_inputs_t *in) {
    // 1. Dual-mode in_flight.
    uint64_t in_flight;
    if (in->reader_live) {
        in_flight = in->occupancy_slots * (uint64_t)in->period_frames + in->stage_frames;
    } else {
        in_flight = in->stage_frames; // discount published-but-unread slots to 0
    }
    // Honest hw_ptr = appl - in_flight. appl_frames is monotonic and normally
    // >= in_flight, but clamp defensively against a transient sample race where
    // occupancy is read a hair before appl_frames is updated.
    uint64_t honest = (in->appl_frames >= in_flight) ? (in->appl_frames - in_flight) : 0;

    // 1b. Pacing governor (jts_ring_pace_apply owns it). Before step 2, never after.
    honest = jts_ring_pace_apply(st, honest, in->pace_nominal, in->now_ns, in->rate,
                                 in->buffer_size, in->period_frames, in->reader_live);

    // 2. Reported-position clamp. The reported value only ever moves FORWARD,
    // and never by >= buffer_size in one call (which would alias to a zero — or
    // negative — delta in ALSA's mod-buffer hw_ptr inference).
    uint64_t last = st->last_reported;
    uint64_t reported;
    if (honest <= last) {
        // Honest position went backward (dead->live regrow, a live reader lagging,
        // or a governed bucket holding less than one period) or stayed put: hold at
        // last_reported. Non-decreasing floor.
        reported = last;
    } else {
        uint64_t advance = honest - last;
        // Cap the per-call advance so ALSA always sees a sub-buffer delta.
        // period_frames <= buffer_size always (n_slots >= 1), so the cap is
        // strictly less than buffer_size. A larger true jump catches up over the
        // next few ticks.
        uint64_t max_advance =
            (in->buffer_size > (uint64_t)in->period_frames)
                ? (in->buffer_size - (uint64_t)in->period_frames)
                : 0; // pathological buffer_size == period: no advance headroom
        if (advance > max_advance) advance = max_advance;
        reported = last + advance;
    }
    st->last_reported = reported;
    return reported;
}

// --- ioplug CAPTURE pointer core (Ring A; shared by pcm_jts_ring.c and
//     test_ring_core.c, exactly like the playback core above) ---
//
// The capture direction MIRRORS the playback pointer discipline, with two
// things flipped:
//
//   * ROLES. On capture the ioplug is the READER and hw_ptr tracks the
//     WRITER's PUBLISH: hw = appl_frames + readable. ALSA's capture avail is
//     hw_ptr - appl_ptr = readable, and it grants `transfer` at most `avail`
//     frames, so `readable` is the gate that lets camilla pull data.
//
//   * THE DUAL MODE. On capture a DEAD WRITER is the case that must keep avail
//     open: the ring is empty and never refills, so an honest `readable` (= 0)
//     would pin avail at 0 forever and camilla would block in poll on a
//     producer that is gone — pushing it toward capture-error/prepare flap
//     during a routine fanin restart. So writer-dead FABRICATES one period of
//     readable per silence tick, which advances hw_ptr and arms POLLIN so
//     `transfer` pulls a period of zeros. Writer ALIVE + ring empty is
//     different and correct: readable is honestly 0, camilla blocks in poll,
//     and that block IS the pacing. Silence is never fabricated while the
//     writer is alive.
//
// The alias hazard mirrors exactly, so does the fix: a writer BURST of exactly
// buffer_size frames between two pointer reads aliases to a ZERO delta and
// pins avail at 0 permanently, so the REPORTED position never advances
// >= buffer_size in one call and a full-buffer catch-up spreads over successive
// ~period/4 ticks. One unified reported-position state, the same
// jts_ring_pointer_state_t the playback path uses.
typedef struct {
    uint64_t appl_frames;    // ALSA appl_ptr mirror (frames the app has READ, real + silence)
    uint64_t occupancy_slots; // write_seq - read_seq (published-but-unread)
    uint64_t destage_frames; // frames staged from a slot but not yet returned to the app
    uint64_t pending_silence_frames; // fabricated writer-dead silence armed but not yet consumed
    uint32_t period_frames;  // frames per slot
    uint64_t buffer_size;    // n_slots * period_frames (== ALSA buffer)
    // No governor fields — PLAYBACK-only, see jts_ring_pace_apply.
} jts_ring_capture_pointer_inputs_t;

// Bound a raw capture occupancy (write_seq - local read_seq) to what the reader
// will actually SERVE: jts_ring_reader_consume resolves an out-of-range W - R
// by resyncing to the tip (readable collapses to 0). Every avail/readable path
// (pointer core, poll readable, the silence-arm emptiness check) MUST apply the
// same resolution BEFORE reporting, or a transient garbage occupancy gets
// ratcheted into the forward-only `last_reported` and becomes PERMANENT phantom
// avail: ALSA then grants `transfer` frames the refill path cannot serve, and
// its rw loop spins hot on a 0-frame transfer without ever polling (the
// RLIMIT_RTTIME SIGKILL class).
static inline uint64_t jts_ring_capture_occupancy_bounded(uint64_t occupancy_slots,
                                                          uint32_t n_slots) {
    return (occupancy_slots > (uint64_t)n_slots) ? 0 : occupancy_slots;
}

// Compute the RAW (pre-modulo) capture hw_ptr to report to ALSA, advancing/
// clamping `st->last_reported`. The caller returns `result % buffer_size`. Pure:
// no ALSA, no atomics — the caller samples occupancy/destage/pending-silence and
// passes them in.
static inline uint64_t
jts_ring_capture_pointer_report(jts_ring_pointer_state_t *st,
                                const jts_ring_capture_pointer_inputs_t *in) {
    // 0. Bound the occupancy to what consume will actually serve (see
    // jts_ring_capture_occupancy_bounded): an out-of-range W - R resolves to a
    // resync-to-tip (0 readable) in the refill path, so reporting it as
    // readable here would mint phantom avail the forward-only clamp below can
    // never take back.
    uint64_t occupancy = jts_ring_capture_occupancy_bounded(
        in->occupancy_slots,
        (in->period_frames > 0) ? (uint32_t)(in->buffer_size / in->period_frames) : 0);
    // 1. Readable = what the app can consume right now:
    //   - In-ring unread slots (occupancy*period) + the sub-slot destage
    //     remainder, readable whether the writer is live or dead.
    //   - WRITER-DEAD SILENCE: `pending_silence_frames` is fabricated by the
    //     caller, one period per timer tick, only while the writer is
    //     heartbeat-dead and the ring is empty — so it needs no liveness flag
    //     here; it is nonzero only in the case it must open the gate. This is
    //     what makes even a COLD-START dead-writer ring advance hw and
    //     terminate, while the pointer itself stays pure.
    //   - WRITER-ALIVE + empty: readable 0 -> avail 0 -> camilla blocks in
    //     poll = the pacing.
    uint64_t readable = occupancy * (uint64_t)in->period_frames +
                        in->destage_frames + in->pending_silence_frames;
    // Honest capture hw_ptr = appl + readable (frames available to be captured).
    uint64_t honest = in->appl_frames + readable;

    // 2. Reported-position clamp (identical shape to the playback core): forward-
    // only, and never by >= buffer_size in one call so ALSA's mod-buffer delta
    // inference never aliases a full-buffer writer burst to a zero delta.
    uint64_t last = st->last_reported;
    uint64_t reported;
    if (honest <= last) {
        // Held or regressed (can't happen for an honest appl+readable, but the
        // clamp keeps the floor unconditional): hold at last_reported.
        reported = last;
    } else {
        uint64_t advance = honest - last;
        uint64_t max_advance =
            (in->buffer_size > (uint64_t)in->period_frames)
                ? (in->buffer_size - (uint64_t)in->period_frames)
                : 0;
        if (advance > max_advance) advance = max_advance;
        reported = last + advance;
    }
    st->last_reported = reported;
    return reported;
}

#endif // JTS_RING_SHM_H
