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
// PRODUCT-INSTALLED, coupling-gated: this .so is shipped on every box via
// deploy/lib/install/ring-platform.sh's /etc/alsa/conf.d/60-jts-ring.conf, but
// stays INERT until the coupling reconciler arms shm_ring on a ring-eligible
// box (see pcm_jts_ring.c's banner and ring-platform.sh for the arm path).

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
// Ceiling raised 4 -> 16 (2026-07-02): CamillaDSP's playback BufferManager
// negotiates buffer = next_pow2(max(3*chunksize, 4*min_period)) and then drives
// its rate controller toward `target_level` frames of device delay. With
// slot_frames pinned at 128 (the outputd DAC-period contract), n_slots is the
// ONLY axis for buffer depth (buffer = n_slots * period_frames). At n_slots=4
// the buffer was 512 frames — smaller than both camilla's negotiated 1024 and
// its target_level (1536), so the rate controller chased an unreachable target,
// wound up, and drove the writer full (full_waits ~= every publish) into
// stall/underrun flapping. 16 slots => 2048-frame buffer >= target_level with
// headroom. Must be kept in lockstep with MAX_N_SLOTS in
// rust/jasper-ring/src/layout.rs and MAX_SHM_RING_SLOTS in
// rust/jasper-outputd/src/config.rs. Unlike the header OFFSETS (pinned
// bit-for-bit by the golden-layout _Static_assert here and the Rust layout
// test), these three MAX constants are tied by a source-grep CI test —
// tests/test_ring_slot_ceiling_pin.py asserts all three are EQUAL, so a drift
// fails in CI rather than only at RUNTIME on-Pi (the reader's geometry
// validation rejects an out-of-range n_slots the writer created). Change all
// three in the same commit and the pin keeps passing.
#define JTS_RING_MAX_SLOTS 16u

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

// The reader's attached ring (Ring A CAPTURE direction). Mirrors the Rust
// jasper_ring::RingReader: attach resyncs read_seq = write_seq, stamps
// reader_pid + heartbeat every consume, consumes the OLDEST unread slot, and
// releases read_seq with Release. Unlike the writer struct this carries a LOCAL
// read_seq mirror the reader owns while live (the writer only borrows read_seq
// on its no-live-reader free-run path — see the SPSC contract in
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
// dead/absent -> FREE-RUN by dropping the OLDEST slot: advance read_seq on the
// absent reader's behalf (Release), then publish the new slot over the freed
// lap (return JTS_RING_PUBLISH_DROPPED). This keeps occupancy bounded so Camilla
// never wedges when outputd's flag is off. Always updates writer_heartbeat_ns.
//
// AVAIL-GATE INTERACTION (the B1 contract — read with pcm_jts_ring.c Q1/Q7 and
// jts_ring_pointer_report below). This free-run branch is the DATA-PATH half of
// readerless survival, but it is only REACHABLE when the ioplug keeps calling
// publish. ALSA's `transfer` (publish's caller during playback) is gated on
// `avail`, which the ioplug's `pointer` callback computes via
// jts_ring_pointer_report. That function keeps the gate open in TWO ways, both
// required: (1) DUAL-MODE in_flight — while the reader is heartbeat-dead it
// discounts published-but-unread slots to 0, so a readerless full ring reports
// avail ~= full instead of the 0 an honest occupancy*period in_flight would pin;
// and (2) a REPORTED-POSITION clamp — because ALSA infers hw motion as a
// mod-buffer delta, a raw pointer jump of exactly buffer_size (which the
// dead-mode discount flip produces at occupancy == n_slots) would alias to a
// zero delta and re-pin avail at 0 permanently, so each reported advance is
// capped below buffer_size. Only with BOTH does transfer keep calling publish so
// this branch can bound the ring. Do not "optimize" this into a bare
// drop-newest: advancing read_seq (not just discarding) is what makes occupancy
// bounded, which the dual-mode pointer and the honest live-reader delay both
// depend on.
jts_ring_publish_result_t jts_ring_writer_publish(jts_ring_writer_t *w,
                                                  const int16_t *samples);

// Frames of buffering currently in-flight (W - R), for the ioplug `delay`
// callback: (W - R) * period_frames.
uint64_t jts_ring_writer_occupancy_slots(const jts_ring_writer_t *w);

// True (1) iff a reader is currently live: reader_pid != 0 AND its heartbeat is
// younger than JTS_RING_WRITER_LIVENESS_TIMEOUT_NS. Exposes the same predicate
// jts_ring_writer_publish/can_accept use, so the ioplug's `pointer`/`delay` can
// run the DUAL-MODE avail contract (see pcm_jts_ring.c jts_ring_pointer): report
// honest occupancy-derived in-flight while a reader is live, but discount
// published-but-unread slots to 0 in-flight while the reader is absent so ALSA's
// `avail` never sticks at 0 on a readerless ring (the B1 wedge). Same-process
// convenience wrapper over the writer's mmap; reads relaxed atomics only.
int jts_ring_writer_reader_is_live(const jts_ring_writer_t *w);

// True (1) iff a publish would proceed without blocking right now: either the
// ring has space (occupancy < n_slots) OR there is no live reader (in which case
// publish free-run-drops immediately). Used by the ioplug's poll_revents to
// report POLLOUT honestly — space-or-free-run is "writable"; a full ring WITH a
// live reader is genuinely not-yet-writable, so we withhold POLLOUT and let the
// timerfd re-poll rather than busy-spinning the app on a slot it cannot take.
int jts_ring_writer_can_accept(const jts_ring_writer_t *w);

// Detach: clear writer_pid (if ours), munmap, close. Safe on a zeroed struct.
void jts_ring_writer_close(jts_ring_writer_t *w);

// --- Reader attach / consume / close (Ring A CAPTURE direction) ---

// Create-or-attach as the READER: O_EXCL create (init + magic-last) or attach
// (bounded magic wait + geometry validation against `expected`), then:
//   - resync read_seq = write_seq (drop the <= n_slots stale slots accumulated
//     while the reader was down; count attach_resyncs) and publish it (Release)
//     so the writer's space check is correct;
//   - stamp reader_pid + reader_heartbeat so the writer's liveness gate sees us;
//   - snapshot writer_epoch for reattach detection.
// SPSC GUARD: the ring tolerates EXACTLY ONE reader. If a live foreign
// reader_pid is already stamped (pid != 0, pid != getpid(), heartbeat younger
// than the liveness window), open refuses with -EBUSY and does NOT stamp
// anything — a stray second `arecord -D jts_ring_capture` while CamillaDSP is
// attached would otherwise corrupt read_seq. (The Rust reader has no such guard
// because outputd owns Ring B's reader singleton by construction; Ring A's
// capture device is operator-openable, so the guard is load-bearing here.)
// Returns 0 on success (fills *out), <0 (negative errno-ish) on a fatal error
// (-EBUSY on a live foreign reader, -EINVAL on geometry mismatch). `path` must
// be an absolute /dev/shm/jts-ring/... path for the magic-invalid reclaim.
int jts_ring_reader_open(const char *path, const jts_ring_geometry_t *expected,
                         jts_ring_reader_t *out);

// Consume the OLDEST unread slot into `out` (jts_ring_samples_per_slot
// interleaved i16). NEVER blocks. Stamps reader_heartbeat + observes epoch every
// call (filled or not — the writer's block-vs-drop gate reads the heartbeat, so
// it must bump even on empty periods, exactly like the Rust reader). Defensive:
// if W - R > n_slots (a correct writer never lets this happen), fast-forwards
// read_seq = write_seq and counts reader_resyncs rather than reading a slot the
// writer may be mid-overwriting. Returns JTS_RING_SLOT_FILLED (copied + advanced
// read_seq with Release) or JTS_RING_SLOT_EMPTY (zero-filled `out`).
jts_ring_slot_read_t jts_ring_reader_consume(jts_ring_reader_t *r, int16_t *out);

// Self-heal an out-of-range occupancy: if W - R > n_slots (a correct writer
// never lets this happen, but a reader that wedged past the liveness window
// while the writer free-ran drop-oldest can observe it on resume), fast-forward
// the local read_seq to the tip and publish it (Release), counting one
// reader_resync. Returns 1 iff a resync happened. This is the SAME operation
// jts_ring_reader_consume performs defensively — extracted so the CAPTURE
// ioplug's per-wake service tick can run it too. Without a proactive resync the
// reader is stuck: the avail paths correctly report 0 readable on an out-of-
// range occupancy (jts_ring_capture_occupancy_bounded), but at avail 0 alsa-lib
// never calls transfer, so consume never runs, so the local read_seq never
// catches up — a permanent-silence wedge with a LIVE writer. Running it from the
// tick (which fires on every wake, avail 0 or not) gives the reader an
// avail-visible recovery path. Never discards readable data: it only fires once
// the writer has already lapped the reader (those slots are unreadable anyway).
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

// Detach: clear reader_pid (if ours — a second reader that stamped its own pid
// and this instance dropping must not clear the new reader's presence, mirroring
// the writer close `cur == mine` guard and the Rust RingReader Drop), munmap,
// close. Safe on a zeroed struct.
void jts_ring_reader_close(jts_ring_reader_t *r);

// CLOCK_MONOTONIC nanoseconds (shared by the writer heartbeat and the wait
// helper). Exposed for the bench + host test.
uint64_t jts_ring_monotonic_ns(void);

// --- ioplug pointer core (shared by pcm_jts_ring.c AND test_ring_core.c) ---
//
// The one function that computes the value the ioplug `pointer` callback
// returns to ALSA. It is a `static inline` in the header (not a .c symbol) so
// BOTH the plugin (compiled only on-Pi with alsa-lib) and the host test
// (compiled on any host) call the SAME code. A regression in this logic fails
// the host `make test` — the round-3 review's "the test hand-copies plugin
// logic, so a plugin regression leaves host tests green" finding.
//
// It owns ALL of the reported-position discipline in one place:
//
//   1. DUAL-MODE in_flight (the B1/round-3 avail-gate fix). Reader LIVE ->
//      honest occupancy-derived in_flight (occupancy*period + stage); reader
//      DEAD -> stage-only in_flight, so published-but-unread slots discount to
//      0 and ALSA's `avail` never sticks at 0 on a readerless ring.
//
//   2. REPORTED-POSITION clamp (the round-4 mod-buffer-alias fix). ALSA infers
//      hw motion in snd_pcm_ioplug_hw_ptr_update as
//        delta = (this_pointer_return - last_pointer_return) mod buffer_size
//      (verbatim: `if (hw >= last_hw) delta = hw - last_hw; else delta =
//      buffer_size + hw - last_hw;`). A RAW advance of exactly buffer_size
//      between two pointer reads aliases to the SAME value mod buffer_size, so
//      delta reads 0 — ALSA's hw_ptr falls one whole lap behind, `avail` pins
//      at 0 permanently, and the writer wedges (the round-3 review's Blocker).
//      An advance > buffer_size in one step is even worse (it can alias to a
//      backward apparent delta). Three shapes produce an exactly-buffer_size
//      raw jump: (a) a live reader drains a full ring during an app gap >= one
//      buffer duration (in_flight: n_slots*period -> 0); (b) the dead-mode
//      discount flip at occupancy == n_slots when the reader dies mid-play
//      (in_flight: n_slots*period -> ~0); (c) the dead->live recovery.
//
//      The fix: never let the REPORTED position advance >= buffer_size in one
//      call. We track the last reported (pre-modulo) position in
//      `last_reported` and clamp each call's forward step to at most
//      buffer_size - period_frames. A true jump of a full buffer then completes
//      over successive ~period/4 ticks (each tick reveals one more period of
//      drain), so ALSA sees a sequence of visible sub-buffer deltas instead of
//      one aliased-to-zero lap. The clamp ALSO subsumes round-3's monotonic
//      floor: because we only ever move `last_reported` FORWARD (by a bounded
//      amount) and never below it, the reported position is non-decreasing by
//      construction — one unified state, not two clamps.
//
// The caller returns `reported % buffer_size` to ALSA. The raw `last_reported`
// is what this function reads/writes; the modulo happens at the call site
// (exactly where ALSA wants a mod-buffer value, and where the raw value is
// available for the delta math above to reason about).

// The reported-position state the pointer core carries across calls. One field:
// the last RAW (pre-modulo) position reported to ALSA. Reset to 0 (with
// appl_frames) on (re)prepare. The plugin embeds this inside jts_ring_pcm_t;
// the host test embeds it inside its ioplug model. Same type, same reset rule.
typedef struct {
    uint64_t last_reported; // last raw hw_ptr handed to ALSA (pre-modulo)
} jts_ring_pointer_state_t;

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

    // 2. Reported-position clamp. The reported value only ever moves FORWARD,
    // and never by >= buffer_size in one call (which would alias to a zero — or
    // negative — delta in ALSA's mod-buffer hw_ptr inference).
    uint64_t last = st->last_reported;
    uint64_t reported;
    if (honest <= last) {
        // Honest position went backward (dead->live regrow, or a live reader
        // lagging) or stayed put: hold at last_reported. Non-decreasing floor.
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

// --- ioplug CAPTURE pointer core (Ring A; shared by pcm_jts_ring.c AND
//     test_ring_core.c, exactly like the playback core above) ---
//
// The capture direction is the MIRROR of the playback pointer discipline, with
// two things flipped:
//
//   * ROLES. On playback the ioplug is the WRITER and hw_ptr tracks the READER's
//     drain (appl - in_flight). On capture the ioplug is the READER and hw_ptr
//     tracks the WRITER's PUBLISH: hw = appl_frames + readable, where `readable`
//     is what the app can consume right now. ALSA's capture avail is
//     hw_ptr - appl_ptr = readable, and it grants `transfer` at most `avail`
//     frames — so `readable` is the gate that lets camilla pull data.
//
//   * THE DUAL MODE. On playback a DEAD reader discounts in_flight to 0 so avail
//     stays OPEN (the writer must keep flowing to bound the ring). On capture a
//     DEAD WRITER is the case that must keep avail open the OTHER way: the ring
//     is empty and never refills, so an honest `readable` (= 0) would pin avail
//     at 0 forever and camilla would block in poll on a producer that is gone —
//     pushing it toward capture-error/prepare flap during a routine fanin
//     restart. So writer-dead FABRICATES one period of readable per silence tick
//     (the caller supplies `silence_frames`, incremented on the timer path),
//     which advances hw_ptr and arms POLLIN so `transfer` pulls a period of
//     zeros. Writer ALIVE + ring empty is DIFFERENT and correct: `readable` is
//     honestly 0, avail is 0, camilla blocks in poll — that block IS the pacing
//     (the writer, DAC-paced transitively, will publish the next slot). We never
//     fabricate silence while the writer is alive.
//
// THE ALIAS HAZARD MIRRORS EXACTLY. ALSA infers capture hw motion the same way
// (delta = (this - last) mod buffer_size in snd_pcm_ioplug_hw_ptr_update). A
// writer BURST of exactly buffer_size frames between two pointer reads (a fanin
// step that publishes a full buffer while the app was mid-gap) makes the raw
// hw advance by exactly buffer_size in one call -> aliases to a ZERO delta ->
// ALSA's accumulated hw_ptr falls a lap behind -> avail pins at 0 permanently ->
// camilla wedges reading a producer that is actually full. Same fix: never let
// the REPORTED position advance >= buffer_size in one call; a full-buffer catch-
// up spreads over successive ~period/4 ticks as visible sub-buffer deltas. The
// clamp is also the non-decreasing floor (hw_ptr never steps backward across a
// writer reattach / epoch flip). One unified reported-position state, same
// jts_ring_pointer_state_t the playback path uses.
typedef struct {
    uint64_t appl_frames;    // ALSA appl_ptr mirror (frames the app has READ, real + silence)
    uint64_t occupancy_slots; // write_seq - read_seq (published-but-unread)
    uint64_t destage_frames; // frames staged from a slot but not yet returned to the app
    uint64_t pending_silence_frames; // fabricated writer-dead silence armed but not yet consumed
    uint32_t period_frames;  // frames per slot
    uint64_t buffer_size;    // n_slots * period_frames (== ALSA buffer)
} jts_ring_capture_pointer_inputs_t;

// Bound a raw capture occupancy (write_seq - local read_seq) to what the reader
// will actually SERVE. A correct writer never lets W - R exceed n_slots, and
// jts_ring_reader_consume resolves an out-of-range value by resyncing to the
// tip (readable collapses to 0) rather than reading slots the writer may be
// mid-overwriting. The avail/readable paths (pointer core, poll readable, the
// silence-arm emptiness check) MUST apply the same resolution BEFORE reporting,
// or a transient garbage occupancy (a wedged-then-resumed reader racing the
// writer's free-run, or a u64 underflow) gets ratcheted into `last_reported`
// (forward-only by design — the alias clamp) and becomes PERMANENT phantom
// avail: ALSA then grants `transfer` frames the refill path cannot serve, and
// its rw loop spins hot on a 0-frame transfer without ever polling (the
// RLIMIT_RTTIME SIGKILL class). Shared here so the host test pins it.
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
    //     remainder are readable whether the writer is live or dead (already-
    //     published frames are valid to drain either way).
    //   - WRITER-DEAD SILENCE: `pending_silence_frames` is the fabricated
    //     "virtual writer" output the caller ARMS one period per timer tick while
    //     the writer is heartbeat-dead and the real ring is empty (wall-clock
    //     paced, exactly like a live writer publishing one slot per period). It is
    //     already 0 whenever the writer is alive OR real data is available (the
    //     caller only arms it in the writer-dead-and-empty branch and consumes it
    //     as the app reads), so it needs no liveness flag here: adding it always
    //     is correct because it is only ever nonzero in the case it must open the
    //     gate. This is what makes even a COLD-START dead-writer ring (no fanin,
    //     the `arecord` resolvability probe) advance hw and terminate — the pointer
    //     is not itself time-aware, but the value it reads is, so it stays pure.
    //   - WRITER-ALIVE + empty: occupancy 0 + destage 0 + pending 0 -> readable 0
    //     -> avail 0 -> camilla blocks in poll = the pacing.
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
