// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0
//
// JTS Ring — ALSA ioplug (`pcm.jts_ring`), BOTH directions.
//
// PLAYBACK (Ring B): CamillaDSP (or aplay, for the resolvability probe) opens
// the ALSA PCM `jts_ring_playback` and writes S16LE/2ch/48 kHz interleaved
// frames; this plugin stages them into whole slots and publishes each full slot
// into the SHM ping-pong ring (jts_ring_shm.c, the WRITER core). jasper-outputd
// is the reader (rust/jasper-ring) and the DAC pacer. This replaces the outputd
// content snd-aloop hop.
//
// CAPTURE (Ring A): CamillaDSP (or arecord, for the resolvability probe) opens
// `jts_ring_capture` and READS S16LE/2ch/48 kHz frames; this plugin attaches the
// SHM reader core (jts_ring_shm.c), destages slots the WRITER (jasper-fanin,
// rust/jasper-ring RingWriter) published, and — when the writer is heartbeat-
// dead — fabricates timer-paced silence so camilla stays DAC-paced through a
// fanin restart. This replaces the fan-in -> camilla dsnoop capture hop. The
// capture direction is the exact MIRROR of the playback pointer/avail/alias
// discipline (roles flipped); see jts_ring_capture_pointer_report in the header.
// Ring A and Ring B are SEPARATE ring instances (program.ring vs content.ring);
// the SPSC contract, the mod-buffer clamp, and the writer/reader-dead survival
// discipline are shared code.
//
// PROTOTYPE, flag-gated: only reachable via the lab-only asound drop-in
// (scripts/ring-proto/arm.sh installs /etc/alsa/conf.d/98-jts-ring-proto.conf).
// Nothing in the product ALSA config references `type jts_ring`.
//
// ---- The eight questions ----
//
// 1. What breaks if the reader (outputd) dies? Three cooperating mechanisms keep
//    the writer (Camilla, or aplay's resolvability probe) from wedging on a
//    readerless ring — two at the ALSA `avail` gate, one at the data path:
//    (a) DUAL-MODE avail (jts_ring_pointer_report, Q7): `avail` is computed ONLY
//        from the `pointer` callback, and ALSA grants `transfer` (the only
//        publish caller) at most `avail` frames. With period==128/
//        periods==n_slots the ALSA buffer == ring depth exactly, so an HONEST
//        in_flight (occupancy*period) pins `avail` at 0 the instant a readerless
//        ring fills — and then `transfer` stops, so publish's own drop path can
//        never run. The pointer core therefore gates in_flight on reader
//        liveness: reader dead -> in_flight discounts published-but-unread slots
//        to 0 (counts only staged frames), so `avail` stays ~full and `transfer`
//        keeps flowing. Reader live -> honest occupancy-derived in_flight (Q7's
//        honest pointer), so a real pacer's delay/rate-controller sees the truth.
//    (b) REPORTED-POSITION clamp (jts_ring_pointer_report, Q7): even with (a),
//        ALSA infers hw motion as delta = (this - last) mod buffer_size, so a
//        raw pointer advance of EXACTLY buffer_size in one step aliases to a zero
//        delta and `avail` sticks at 0 permanently (the round-3 review's deeper
//        wedge). The core clamps each reported advance to < buffer_size so a
//        full-lap catch-up is spread over several ticks and every step is a
//        visible sub-buffer delta. (a) and (b) together keep the gate open on
//        EVERY readerless shape (steady fill, mid-play reader death, reattach).
//    (c) Given (a)+(b) keep transfer flowing, the writer's space check (in
//        jts_ring_writer_publish) FREE-RUNS by dropping the OLDEST slot
//        (advancing read_seq on the absent reader's behalf,
//        writer_drop_no_reader++) so occupancy stays bounded and each publish has
//        a free slot to memcpy into. (a)+(b) open the gate; (c) does the ring
//        bookkeeping. None alone is sufficient — (c) landed first but was
//        unreachable behind (a)'s gate, and (a) alone still wedged on (b)'s alias.
//    This is what keeps Camilla healthy when outputd's flag is off and what makes
//    the `aplay -D jts_ring_playback ... /dev/zero` resolvability probe
//    terminate. The reported-position clamp (b) also keeps hw_ptr non-decreasing,
//    so reader reattach never steps it backward.
// 2. What breaks if the writer (this plugin) dies? write_seq stops advancing;
//    the reader sees the ring empty and emits silence. On close we clear
//    writer_pid so the reader reports writer_alive:false.
// 3. Latency: the ring depth is n_slots * period_frames (16*128 = 2048 frames,
//    ~42.7 ms) but that is the CEILING, not the steady-state latency. Steady
//    state is set by the WRITER's own buffering target: CamillaDSP parks its
//    device delay at `target_level` (1536 frames ~= 32 ms in the current
//    ring_proto config), so observed occupancy sits ~12/16 slots. n_slots MUST
//    be >= ceil(target_level / period_frames) with headroom, or camilla's rate
//    controller chases a target the reported delay can never reach (see the
//    n_slots 4 -> 16 note in jts_ring_shm.h). Effective latency == the writer's
//    target_level, not the ring depth.
// 4. Observability: writer counters (published/dropped/full_waits) are logged
//    at close; the reader publishes occupancy/empty_reads/writer_alive to
//    /state.shm_ring.
// 5. Fail-closed: a geometry mismatch against an existing ring is an open()
//    error surfaced to Camilla/aplay; a torn (magic-less) file under the owned
//    /dev/shm/jts-ring/ path is reclaimed. HW constraints pin S16LE/2ch/48 kHz.
// 6. Default-off: the .so is never loaded outside the lab drop-in.
// 7. Memory ordering: publish is Release on write_seq after the payload memcpy;
//    the core documents the pairing with the reader's Acquire. C11 atomics ->
//    aarch64 ldar/stlr. The `pointer` callback reports the READER's drain
//    position (hw_ptr = appl_frames - in_flight), derived from the same read_seq
//    the reader releases, so ALSA's avail/delay reflect real drain progress —
//    NOT frames merely accepted (which read as "instantly played", starved
//    camilla's rate controller, and tripped its stall detector). The reported
//    position is computed by ONE shared function, jts_ring_pointer_report
//    (jts_ring_shm.h), which owns: (i) the DUAL-MODE in_flight so `avail`
//    free-runs instead of sticking at 0 while the reader is heartbeat-dead
//    (Q1a); (ii) the round-4 clamp bounding each reported advance to <
//    buffer_size so ALSA's mod-buffer hw_ptr inference never aliases a full-lap
//    jump to a zero delta (Q1b) — the same clamp keeps hw_ptr non-decreasing.
//    The host test compiles against that shared function, so a regression in it
//    fails `make test` rather than only showing up on hardware.
// 8. Productization delta: the timerfd poll (period/4) becomes a FUTEX_WAIT on
//    the reserved header futex_word; the lab drop-in becomes a reconciler-owned
//    device. No SHM header change. The reconciler must size n_slots from the
//    active camilla config's target_level (Q3), not a fixed ping-pong 2.
//
// Poll model: cross-process SHM means the reader cannot arm an eventfd in this
// process, so we cannot signal "space became available" the way an in-process
// plugin would. The honest prototype uses a timerfd firing every ~period/4;
// poll_revents reports POLLOUT iff the ring currently has space. ALSA's
// mmap/rw loop tolerates this (it re-polls); it is a poll, not a precise
// wakeup. The productization is the futex wait noted above.

#include "jts_ring_shm.h"

#include <alsa/asoundlib.h>
#include <alsa/pcm_external.h> // SND_PCM_PLUGIN_DEFINE_FUNC / _SYMBOL
#include <alsa/pcm_ioplug.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/timerfd.h>
#include <time.h>
#include <unistd.h>

#define JTS_RING_DEFAULT_PATH "/dev/shm/jts-ring/content.ring"
#define JTS_RING_DEFAULT_PERIOD 128
#define JTS_RING_DEFAULT_SLOTS 2

typedef struct {
    snd_pcm_ioplug_t io;
    // Exactly ONE of these is used per PCM, selected by io.stream:
    //   PLAYBACK -> writer (Ring B: CamillaDSP writes -> outputd reads)
    //   CAPTURE  -> reader (Ring A: CamillaDSP reads  <- fanin writes)
    jts_ring_writer_t writer;
    jts_ring_reader_t reader;
    char path[256];
    uint32_t period_frames;
    uint32_t n_slots;
    int opened; // writer/reader attached
    // --- PLAYBACK staging (writer) ---
    // Camilla may writei() fewer than a whole slot at a time; we accumulate into
    // `stage` until a full slot (period_frames) is ready, then publish.
    // On CAPTURE `stage` is the DESTAGE buffer instead: one slot copied out of
    // the ring, drained to the app across possibly-multiple readi() calls, with
    // `stage_frames` = frames still UNREAD in the destage buffer (so the slot
    // origin index for the next read is stage_capacity - stage_frames). See
    // jts_ring_capture_transfer.
    int16_t *stage;
    size_t stage_frames;
    size_t stage_capacity_frames; // == period_frames
    // Total frames ACCEPTED from / DELIVERED to the app (== ALSA appl_ptr).
    // Playback: frames written by the app. Capture: frames read by the app. The
    // pointer callbacks derive the honest hw_ptr from this ± in-flight/readable.
    snd_pcm_uframes_t appl_frames;
    // CAPTURE writer-dead silence (the "virtual writer"). When the writer is
    // heartbeat-dead and the real ring is empty, the poll tick ARMS one period of
    // pending silence (wall-clock paced, exactly as a live writer would publish
    // one slot per period), bounded so avail never runs away. The pointer adds
    // `pending_silence_frames` to `readable` so avail opens; the transfer callback
    // consumes it (zeros to the app, appl advances, pending decrements) and bumps
    // `silence_periods` (observability, logged at close). Decoupling the
    // fabrication (poll, time-paced) from the pointer (pure) is what makes a
    // COLD-START dead-writer ring (the `arecord` resolvability probe with no
    // fanin) advance hw and terminate, not just the mid-stream fanin-restart case.
    uint64_t pending_silence_frames; // armed-but-unconsumed fabricated silence
    uint64_t silence_periods;        // total fabricated-silence periods (observability)
    // Wall-clock pacing for the writer-dead silence: a new period is armed only
    // after one period of REAL time has elapsed since the last (CLOCK_MONOTONIC),
    // so silence flows at ~48 kHz instead of as-fast-as-the-app-asks. Without this
    // an unpaced consumer (arecord) drains fabricated silence at multiples of
    // realtime, so a writer returning mid-capture finds no live reader and its
    // audio is dropped. 0 = not yet armed since the last real data.
    uint64_t last_silence_ns;
    // Reported-position state for the pointer callback, carried across calls.
    // The shared cores (jts_ring_pointer_report for playback,
    // jts_ring_capture_pointer_report for capture, both in jts_ring_shm.h) own
    // the whole discipline: dual-mode / silence-mode readable, a non-decreasing
    // reported floor, AND the round-4 clamp that keeps any single reported
    // advance below buffer_size so ALSA's mod-buffer hw_ptr inference never
    // aliases a full-buffer jump to a zero delta. Reset to 0 on (re)prepare.
    jts_ring_pointer_state_t ptr_state;
    int timer_fd;
} jts_ring_pcm_t;

static const unsigned int JTS_RING_RATE = 48000;
static const unsigned int JTS_RING_CHANNELS = 2;

// ---- helpers ----

static void arm_timer(jts_ring_pcm_t *p) {
    if (p->timer_fd < 0) return;
    // period/4, clamped to [0.25 ms, 2 ms]. Repeating.
    uint64_t period_ns = (uint64_t)p->period_frames * 1000000000ull / JTS_RING_RATE;
    uint64_t tick_ns = period_ns / 4;
    if (tick_ns < 250000ull) tick_ns = 250000ull;
    if (tick_ns > 2000000ull) tick_ns = 2000000ull;
    struct itimerspec its;
    its.it_interval.tv_sec = 0;
    its.it_interval.tv_nsec = (long)tick_ns;
    its.it_value.tv_sec = 0;
    its.it_value.tv_nsec = (long)tick_ns;
    timerfd_settime(p->timer_fd, 0, &its, NULL);
}

static void disarm_timer(jts_ring_pcm_t *p) {
    if (p->timer_fd < 0) return;
    struct itimerspec its;
    memset(&its, 0, sizeof(its));
    timerfd_settime(p->timer_fd, 0, &its, NULL);
}

// ---- ioplug callbacks ----

static int jts_ring_prepare(snd_pcm_ioplug_t *io) {
    jts_ring_pcm_t *p = io->private_data;
    if (!p->opened) {
        jts_ring_geometry_t g = {
            .rate = JTS_RING_RATE,
            .channels = JTS_RING_CHANNELS,
            .sample_format = JTS_RING_SAMPLE_FORMAT_S16LE,
            .period_frames = p->period_frames,
            .n_slots = p->n_slots,
        };
        int rc = jts_ring_writer_open(p->path, &g, &p->writer);
        if (rc != 0) {
            SNDERR("jts_ring: writer_open(%s) failed rc=%d", p->path, rc);
            return rc < 0 ? rc : -EIO;
        }
        p->stage_capacity_frames = p->period_frames;
        p->stage = calloc(p->stage_capacity_frames * JTS_RING_CHANNELS, sizeof(int16_t));
        if (!p->stage) {
            jts_ring_writer_close(&p->writer);
            return -ENOMEM;
        }
        p->opened = 1;
    }
    // Reset the staging + pointer on (re)prepare.
    p->stage_frames = 0;
    p->appl_frames = 0;
    p->ptr_state.last_reported = 0;
    return 0;
}

static int jts_ring_start(snd_pcm_ioplug_t *io) {
    jts_ring_pcm_t *p = io->private_data;
    arm_timer(p);
    return 0;
}

static int jts_ring_stop(snd_pcm_ioplug_t *io) {
    jts_ring_pcm_t *p = io->private_data;
    disarm_timer(p);
    return 0;
}

static snd_pcm_sframes_t jts_ring_pointer(snd_pcm_ioplug_t *io) {
    jts_ring_pcm_t *p = io->private_data;
    // ioplug `pointer` must return the PLAYBACK hw_ptr: the frame position the
    // hardware (here: the outputd reader) has DRAINED, not the frames we merely
    // accepted. ALSA derives avail = buffer_size - (appl_ptr - hw_ptr); a
    // playback writer (CamillaDSP) then treats current_delay = buffer_size -
    // avail = appl_ptr - hw_ptr as the frames still buffered in the device, and
    // feeds that to its rate controller and stall detector.
    //
    // The original code advanced hw_ptr on ACCEPT (hw_ptr == appl_ptr), so
    // avail was always ~full and current_delay always ~0. CamillaDSP saw the
    // device as instantly drained, wound its rate controller up toward
    // target_level (1536) that the reported delay could never reach, and read a
    // genuinely-full ring (poll withholds POLLOUT) as a stall -> the
    // "device stalled"/"resumed"/"Prepare after underrun" flapping.
    //
    // The three-part discipline that computes the reported hw_ptr lives in ONE
    // shared function, jts_ring_pointer_report (jts_ring_shm.h), so the host
    // test drives the exact same logic (a plugin regression fails `make test`):
    //   - HONEST hw_ptr = appl_frames - in_flight, in_flight = occupancy*period
    //     + stage. Deriving avail/delay from the reader's read_seq keeps them
    //     mutually consistent and reflects the reader's real drain.
    //   - DUAL-MODE avail (the round-3 B1 fix). `pointer` is the ONE place
    //     ALSA's `avail` gate is computed, and `transfer` (publish's only
    //     playback caller) is granted at most `avail` frames. With period==128/
    //     periods==n_slots (buffer_size == ring depth exactly), an HONEST
    //     in_flight pins avail at 0 the instant a readerless ring fills, so
    //     transfer stops and publish's free-run drop is unreachable. So while
    //     the reader is heartbeat-dead the core discounts published-but-unread
    //     slots to 0 in-flight -> avail stays ~full -> transfer keeps calling
    //     publish -> publish's drop-oldest reclaim bounds the ring.
    //   - REPORTED-POSITION clamp (the round-4 fix). ALSA infers hw motion as
    //     delta = (this_return - last_return) mod buffer_size. A raw advance of
    //     exactly buffer_size between two reads aliases to a ZERO delta, ALSA's
    //     hw_ptr falls a lap behind, avail pins at 0 PERMANENTLY, and the writer
    //     wedges — the same B1 symptom, one layer down. This bit three shapes on
    //     jts.local: (a) a live reader draining a full ring during an app gap >=
    //     one buffer duration; (b) the dead-mode discount flip at occ==n_slots
    //     when the reader dies mid-play; (c) the dead->live recovery. The core
    //     clamps each reported advance to <= buffer_size - period so a full-lap
    //     catch-up completes over several ticks as visible sub-buffer deltas,
    //     never one aliased-to-zero lap. The same clamp is the non-decreasing
    //     floor (it only moves the reported position forward), so it subsumes
    //     round-3's separate monotonic clamp — one unified reported-position
    //     state.
    // Before the writer is attached there is no mmap to read occupancy/liveness
    // from, so both stay 0 (the core then sees in_flight = stage = 0 and hw_ptr
    // tracks appl — fine for the open/prepare handshake).
    int reader_live = p->opened ? jts_ring_writer_reader_is_live(&p->writer) : 0;
    uint64_t occupancy =
        p->opened ? jts_ring_writer_occupancy_slots(&p->writer) : 0;
    jts_ring_pointer_inputs_t in = {
        .appl_frames = (uint64_t)p->appl_frames,
        .occupancy_slots = occupancy,
        .stage_frames = (uint64_t)p->stage_frames,
        .period_frames = p->period_frames,
        .buffer_size = (uint64_t)io->buffer_size,
        .reader_live = reader_live,
    };
    uint64_t reported = jts_ring_pointer_report(&p->ptr_state, &in);
    // ALSA wants the position modulo the buffer. The clamp above guarantees the
    // reported value never advanced >= buffer_size since the last call, so the
    // mod projection here can never alias a full lap to a zero/backward delta.
    return (snd_pcm_sframes_t)(reported % io->buffer_size);
}

static snd_pcm_sframes_t jts_ring_transfer(snd_pcm_ioplug_t *io,
                                           const snd_pcm_channel_area_t *areas,
                                           snd_pcm_uframes_t offset,
                                           snd_pcm_uframes_t size) {
    jts_ring_pcm_t *p = io->private_data;
    // Interleaved S16LE: one contiguous buffer, all channels in areas[0].addr.
    const int16_t *src =
        (const int16_t *)((const char *)areas[0].addr + (areas[0].first / 8) +
                          (size_t)offset * (areas[0].step / 8));

    snd_pcm_uframes_t consumed = 0;
    while (consumed < size) {
        size_t room = p->stage_capacity_frames - p->stage_frames;
        size_t take = (size - consumed) < room ? (size - consumed) : room;
        memcpy(p->stage + p->stage_frames * JTS_RING_CHANNELS,
               src + consumed * JTS_RING_CHANNELS,
               take * JTS_RING_CHANNELS * sizeof(int16_t));
        p->stage_frames += take;
        consumed += take;

        if (p->stage_frames == p->stage_capacity_frames) {
            // A full slot is staged: publish it. Free-run drop (no live reader)
            // still returns "accepted" so Camilla/aplay never wedge.
            jts_ring_writer_publish(&p->writer, p->stage);
            p->stage_frames = 0;
        }
    }
    p->appl_frames += size;
    return (snd_pcm_sframes_t)size;
}

// The ioplug `.delay` callback is `int (*)(snd_pcm_ioplug_t *,
// snd_pcm_sframes_t *)` — it returns an int status (0 = ok) and writes the
// frame count through *delayp. It is NOT the sframes-returning shape; matching
// the field type exactly is required (gcc -Werror rejects the mismatch).
static int jts_ring_delay(snd_pcm_ioplug_t *io, snd_pcm_sframes_t *delayp) {
    jts_ring_pcm_t *p = io->private_data;
    // In-flight = published-but-unread slots * period_frames + staged frames.
    // This is intentionally the HONEST occupancy-derived delay, NOT the
    // dual-mode value the `pointer`/avail path uses while the reader is dead.
    // Rationale (round-3 review nit #3): `.delay` is only consulted by a LIVE
    // pacer's rate controller (CamillaDSP), and a live reader IS the honest
    // case — so the two agree exactly when `.delay` is actually read. On a
    // readerless ring there is no rate controller polling delay; what governs
    // writer progress there is the `avail` GATE (which jts_ring_pointer_report's
    // dual-mode + clamp keep open), not this delay value. Reporting honest
    // occupancy here therefore never gates a readerless writer, and mirroring
    // the pointer's dead-mode discount would only add a code path with no
    // consumer. If a future consumer reads `.delay` on a readerless ring, revisit
    // (discount it the same way jts_ring_pointer_report does).
    uint64_t slots = p->opened ? jts_ring_writer_occupancy_slots(&p->writer) : 0;
    snd_pcm_sframes_t delay =
        (snd_pcm_sframes_t)(slots * p->period_frames + p->stage_frames);
    if (delayp) *delayp = delay;
    return 0;
}

// snd_pcm_drain(): flush whatever is staged/in-flight and return once the ring
// is empty (or the reader is gone). Without this callback ALSA's default ioplug
// drain spins until hw_avail (== our `delay`) reaches 0 — but the stage only
// publishes on FULL slots, so a partially-staged slot (stage_frames > 0, the
// common case for an arbitrary-length WAV via aplay) leaves delay pinned above
// 0 forever and drain HANGS, even with a live reader. We instead:
//   1. Zero-pad and publish the partial slot so no staged audio is lost and
//      stage_frames returns to 0 (a whole slot is required by the SPSC core).
//   2. Wait (bounded) for the reader to drain the ring to empty. If the reader
//      is absent the ring cannot drain, so we stop immediately — the honest
//      free-run contract (Q1) already covers a readerless ring, and blocking
//      here would reintroduce the very wedge B1 fixed.
// The callback is `int (*drain)(snd_pcm_ioplug_t *)` (0 = ok); it must be
// bounded so aplay/Camilla never hang on close.
static int jts_ring_drain(snd_pcm_ioplug_t *io) {
    jts_ring_pcm_t *p = io->private_data;
    if (!p->opened) return 0;
    // 1. Flush the partial staged slot (zero-pad the remainder). Do NOT bump
    // appl_frames for the padding: appl_frames mirrors ALSA's appl_ptr (real
    // app-submitted frames only), and drain is a blocking terminal op — ALSA
    // does not interleave pointer/transfer reads while this callback runs, so
    // the transient in-flight inconsistency (one extra published period vs the
    // k real frames) is never observed. Once the ring drains to empty below,
    // in_flight == 0 and hw_ptr == appl_frames == ALSA appl_ptr — the correct
    // fully-drained terminal state.
    if (p->stage_frames > 0) {
        size_t pad_from = p->stage_frames * JTS_RING_CHANNELS;
        size_t total = p->stage_capacity_frames * JTS_RING_CHANNELS;
        memset(p->stage + pad_from, 0, (total - pad_from) * sizeof(int16_t));
        jts_ring_writer_publish(&p->writer, p->stage);
        p->stage_frames = 0;
    }
    // 2. Bounded wait for the reader to drain the ring to empty. period/4 per
    // tick (matches the poll cadence); the tick BUDGET is the bound that makes
    // this safe for BOTH failure shapes: a reader draining at DAC pace empties
    // well within budget, while an absent/wedged reader (the ring can't drain)
    // simply exhausts the budget and returns rather than hanging the app. The
    // occupancy read is the only progress signal we need — no can_accept /
    // liveness branch, which would only distinguish two cases that both resolve
    // to "stop when drained or when the budget runs out."
    uint64_t period_ns = (uint64_t)p->period_frames * 1000000000ull / JTS_RING_RATE;
    uint64_t tick_ns = period_ns / 4;
    if (tick_ns < 250000ull) tick_ns = 250000ull;
    if (tick_ns > 2000000ull) tick_ns = 2000000ull;
    struct timespec nap = {.tv_sec = 0, .tv_nsec = (long)tick_ns};
    int max_ticks = (int)p->n_slots * 8; // ~2 ring-depths of real time, bounded
    for (int i = 0; i < max_ticks; i++) {
        if (jts_ring_writer_occupancy_slots(&p->writer) == 0) break; // fully drained
        nanosleep(&nap, NULL);
    }
    return 0;
}

static int jts_ring_hw_params(snd_pcm_ioplug_t *io, snd_pcm_hw_params_t *params) {
    // The HW constraints below already pin format/channels/rate; nothing extra
    // to negotiate. (Kept as a hook so a future active/S32 lane can validate.)
    (void)io;
    (void)params;
    return 0;
}

static int jts_ring_close(snd_pcm_ioplug_t *io) {
    jts_ring_pcm_t *p = io->private_data;
    if (p->opened) {
        if (io->stream == SND_PCM_STREAM_CAPTURE) {
            SNDERR("jts_ring: closing (capture) frames_read_slots=%llu "
                   "silence_periods=%llu empty_reads=%llu startup_empty_reads=%llu "
                   "reader_resyncs=%llu epoch_resets=%llu",
                   (unsigned long long)p->reader.frames_read_slots,
                   (unsigned long long)p->silence_periods,
                   (unsigned long long)p->reader.empty_reads,
                   (unsigned long long)p->reader.startup_empty_reads,
                   (unsigned long long)p->reader.reader_resyncs,
                   (unsigned long long)p->reader.epoch_resets);
            jts_ring_reader_close(&p->reader);
        } else {
            SNDERR("jts_ring: closing published_slots=%llu drop_no_reader=%llu full_waits=%llu",
                   (unsigned long long)p->writer.published_slots,
                   (unsigned long long)p->writer.drop_no_reader,
                   (unsigned long long)p->writer.full_waits);
            jts_ring_writer_close(&p->writer);
        }
        free(p->stage);
        p->stage = NULL;
        p->opened = 0;
    }
    if (p->timer_fd >= 0) close(p->timer_fd);
    free(p);
    return 0;
}

// ============================================================================
// CAPTURE direction callbacks (Ring A: fanin writes -> CamillaDSP reads).
//
// The mirror of the playback set above. The ioplug is now the READER: it
// attaches the SHM reader core, and its `pointer` advances hw_ptr on the
// WRITER's PUBLISH (so ALSA's capture avail = readable frames), draining slots
// into a per-slot DESTAGE buffer for sub-slot readi() support. When the writer
// is heartbeat-dead the transfer callback fabricates timer-paced silence
// periods (silence_periods++) so camilla stays up and DAC-paced through a fanin
// restart instead of flapping into capture-error/prepare; real audio resumes
// seamlessly on writer reattach (epoch observed, silence stops).
// ============================================================================

static int jts_ring_capture_prepare(snd_pcm_ioplug_t *io) {
    jts_ring_pcm_t *p = io->private_data;
    if (!p->opened) {
        jts_ring_geometry_t g = {
            .rate = JTS_RING_RATE,
            .channels = JTS_RING_CHANNELS,
            .sample_format = JTS_RING_SAMPLE_FORMAT_S16LE,
            .period_frames = p->period_frames,
            .n_slots = p->n_slots,
        };
        int rc = jts_ring_reader_open(p->path, &g, &p->reader);
        if (rc != 0) {
            // -EBUSY (a live foreign reader already owns the ring) is the SPSC
            // guard firing — surface it verbatim so a stray second capture opener
            // sees EBUSY rather than corrupting the incumbent's read_seq.
            SNDERR("jts_ring: reader_open(%s) failed rc=%d%s", p->path, rc,
                   rc == -EBUSY ? " (ring already has a live reader — EBUSY)" : "");
            return rc < 0 ? rc : -EIO;
        }
        p->stage_capacity_frames = p->period_frames;
        p->stage = calloc(p->stage_capacity_frames * JTS_RING_CHANNELS, sizeof(int16_t));
        if (!p->stage) {
            jts_ring_reader_close(&p->reader);
            return -ENOMEM;
        }
        p->opened = 1;
    }
    // Reset the destage + pointer + silence state on (re)prepare. stage_frames is
    // the count of frames still UNREAD in the current destage slot; 0 means "no
    // slot currently destaged" (transfer pulls a fresh one).
    p->stage_frames = 0;
    p->appl_frames = 0;
    p->pending_silence_frames = 0;
    p->silence_periods = 0;
    p->last_silence_ns = 0;
    p->ptr_state.last_reported = 0;
    return 0;
}

static snd_pcm_sframes_t jts_ring_capture_pointer(snd_pcm_ioplug_t *io) {
    jts_ring_pcm_t *p = io->private_data;
    // Capture hw_ptr advances on the WRITER's PUBLISH: hw = appl_frames +
    // readable, so ALSA's capture avail = hw - appl = readable. The three-part
    // discipline lives in the shared jts_ring_capture_pointer_report (see the
    // header): (1) readable = in-ring unread + destage remainder + fabricated
    // writer-dead silence; (2) writer-dead silence keeps avail open on a gone
    // producer while writer-alive+empty honestly reports 0 (camilla blocks =
    // pacing); (3) the round-4 clamp bounds each reported advance below
    // buffer_size so ALSA's mod-buffer hw_ptr inference never aliases a
    // full-buffer writer burst to a zero delta. The host test drives the same
    // function so a regression fails `make test`.
    uint64_t occupancy = p->opened ? jts_ring_reader_occupancy_slots(&p->reader) : 0;
    // destage remainder = frames copied out of a slot but not yet returned to the
    // app. stage_frames counts UNREAD destage frames, so that is exactly the
    // remainder (already-read frames were consumed by prior readi()s and folded
    // into appl_frames).
    uint64_t destage_remainder = (uint64_t)p->stage_frames;
    jts_ring_capture_pointer_inputs_t in = {
        .appl_frames = (uint64_t)p->appl_frames,
        .occupancy_slots = occupancy,
        .destage_frames = destage_remainder,
        .pending_silence_frames = p->pending_silence_frames,
        .period_frames = p->period_frames,
        .buffer_size = (uint64_t)io->buffer_size,
    };
    uint64_t reported = jts_ring_capture_pointer_report(&p->ptr_state, &in);
    return (snd_pcm_sframes_t)(reported % io->buffer_size);
}

// Refill the destage buffer with one slot when it is empty. Real ring data takes
// priority (a writer that came back is served before any pending silence). On an
// empty real ring: if silence has been ARMED (pending_silence_frames >= a period,
// set by the poll tick while the writer is dead) fabricate that period of zeros
// and CONSUME the pending arm; otherwise return 0 (the caller reports a short read
// and re-polls — the writer-alive-and-empty pacing block). Returns readable frames
// now in the destage buffer (period_frames on success, 0 if it must block).
static size_t capture_refill_destage(jts_ring_pcm_t *p) {
    if (p->stage_frames > 0) return p->stage_frames; // still draining a slot
    jts_ring_slot_read_t got = jts_ring_reader_consume(&p->reader, p->stage);
    if (got == JTS_RING_SLOT_FILLED) {
        p->stage_frames = p->stage_capacity_frames;
        // Real data flowed: reset the silence pacing clock so a later writer-death
        // gap arms its FIRST silence period immediately (no dead-air lag), then
        // paces subsequent ones from that point.
        p->last_silence_ns = 0;
        // Discard any silence that was ARMED but not yet CONSUMED. The poll tick
        // arms one period of pending silence when the writer is dead + the ring is
        // empty; if the writer then publishes a real slot BEFORE that armed period
        // is consumed, the arm is stale. Leaving it set would splice a spurious
        // silence period into live audio the next time the ring momentarily drains
        // between two paced real slots (real data wins here, so the pending frames
        // would otherwise survive until an empty read fired the pending>=period
        // branch below). Real data supersedes any pending silence — clear it.
        p->pending_silence_frames = 0;
        return p->stage_frames;
    }
    // Empty real ring. If a silence period has been armed (writer dead, poll tick
    // advanced pending_silence_frames), deliver it: p->stage is already zeros from
    // reader_consume's empty-fill. Consume one period of the pending arm.
    if (p->pending_silence_frames >= p->period_frames) {
        p->pending_silence_frames -= p->period_frames;
        p->stage_frames = p->stage_capacity_frames;
        p->silence_periods++;
        return p->stage_frames;
    }
    return 0; // writer alive + empty (no armed silence): block (short read), re-poll
}

static snd_pcm_sframes_t jts_ring_capture_transfer(snd_pcm_ioplug_t *io,
                                                   const snd_pcm_channel_area_t *areas,
                                                   snd_pcm_uframes_t offset,
                                                   snd_pcm_uframes_t size) {
    jts_ring_pcm_t *p = io->private_data;
    // Interleaved S16LE: one contiguous destination, all channels in areas[0].
    int16_t *dst =
        (int16_t *)((char *)areas[0].addr + (areas[0].first / 8) +
                    (size_t)offset * (areas[0].step / 8));

    snd_pcm_uframes_t delivered = 0;
    while (delivered < size) {
        size_t avail = capture_refill_destage(p);
        if (avail == 0) {
            // Writer alive + ring empty: cannot serve more right now. Return what
            // we delivered so far (a short read); ALSA re-polls and the timerfd/
            // POLLIN gate serves the rest once the writer publishes. Returning 0
            // (nothing yet) is also valid — ALSA treats it as "try again".
            break;
        }
        // Copy from the destage buffer starting at the already-read offset.
        size_t origin = p->stage_capacity_frames - p->stage_frames; // frames already read
        size_t take = (size - delivered) < p->stage_frames ? (size - delivered) : p->stage_frames;
        memcpy(dst + delivered * JTS_RING_CHANNELS,
               p->stage + origin * JTS_RING_CHANNELS,
               take * JTS_RING_CHANNELS * sizeof(int16_t));
        p->stage_frames -= take;
        delivered += take;
    }
    p->appl_frames += delivered;
    return (snd_pcm_sframes_t)delivered;
}

static int jts_ring_capture_delay(snd_pcm_ioplug_t *io, snd_pcm_sframes_t *delayp) {
    jts_ring_pcm_t *p = io->private_data;
    // Capture delay = frames already captured/available but not yet read by the
    // app = readable = in-ring unread + destage remainder. Honest occupancy-
    // derived, same rationale as the playback delay (a live consumer's rate
    // controller reads it; the writer-dead silence path is governed by the avail
    // GATE, not this value).
    uint64_t slots = p->opened ? jts_ring_reader_occupancy_slots(&p->reader) : 0;
    snd_pcm_sframes_t delay =
        (snd_pcm_sframes_t)(slots * p->period_frames + p->stage_frames);
    if (delayp) *delayp = delay;
    return 0;
}

static int jts_ring_capture_poll_revents(snd_pcm_ioplug_t *io, struct pollfd *pfd,
                                         unsigned int nfds, unsigned short *revents) {
    jts_ring_pcm_t *p = io->private_data;
    *revents = 0;
    if (nfds >= 1 && (pfd[0].revents & POLLIN)) {
        uint64_t expirations = 0;
        ssize_t r = read(p->timer_fd, &expirations, sizeof(expirations));
        (void)r;
    }
    // ARM SILENCE (the virtual writer), WALL-CLOCK PACED. If the writer is dead
    // and the real ring is empty, arm one period of pending silence — but only
    // once one period of REAL time (CLOCK_MONOTONIC) has elapsed since the last
    // fabrication, so silence flows at ~48 kHz. This pacing is load-bearing:
    // without it an unpaced consumer (arecord, which re-polls immediately) drains
    // fabricated silence at multiples of realtime, so a writer that returns
    // mid-capture finds its whole audio window already consumed as silence and
    // gets no live reader (drop_no_reader). Bounded to <= one period so avail
    // never runs away. This is also what makes a COLD-START dead-writer ring (the
    // `arecord` resolvability probe with no fanin) advance hw and terminate — just
    // at realtime pace.
    //
    // PACING ERROR IS INTENTIONALLY IN THE SAFE (SLOW) DIRECTION. `now` is only
    // sampled on a timerfd tick (arm_timer's period/4 cadence, plus scheduler
    // jitter), and `last_silence_ns = now` re-anchors to that tick rather than to
    // the ideal period boundary, so the observed silence rate runs ~14% SLOW of
    // realtime (measured: 4 s of silence ~= 4.66 s wall). That is the SAFE
    // direction: slightly-slow silence makes the consumer block marginally longer
    // and NEVER over-drains, so a returning writer's real audio is never
    // pre-consumed as silence (the fast direction would). It is warn-only
    // (`stop_on_rate_change` is unset). The honest-prototype timerfd poll (Q8) is
    // approximate on purpose; the FUTEX_WAIT productization removes the tick
    // quantization and the drift with it. Do NOT "fix" this by advancing
    // last_silence_ns past `now` to catch up — that pushes the error toward the
    // UNSAFE fast direction and can re-open the pre-consumed-audio drop.
    if (p->opened) {
        int writer_live = jts_ring_reader_writer_is_live(&p->reader);
        uint64_t occ = jts_ring_reader_occupancy_slots(&p->reader);
        int real_empty = (occ == 0) && (p->stage_frames == 0);
        if (!writer_live && real_empty &&
            p->pending_silence_frames < (uint64_t)p->period_frames) {
            uint64_t now = jts_ring_monotonic_ns();
            uint64_t period_ns = (uint64_t)p->period_frames * 1000000000ull / JTS_RING_RATE;
            // First silence period after real data (last_silence_ns == 0) arms
            // immediately so the gate opens without a period of dead air; each
            // subsequent one waits a full period of realtime.
            if (p->last_silence_ns == 0 || now - p->last_silence_ns >= period_ns) {
                p->pending_silence_frames = (uint64_t)p->period_frames;
                p->last_silence_ns = now;
            }
        }
    }
    // Report POLLIN (readable) iff the app can consume a frame right now:
    //   - a slot is in the ring (occupancy > 0), OR
    //   - the destage buffer still has unread frames (stage_frames > 0), OR
    //   - a silence period is armed (pending_silence_frames > 0).
    // Empty ring WITH a live writer + no armed silence -> withhold POLLIN: camilla
    // blocks in poll, and that block IS the pacing (the DAC-paced writer publishes
    // the next slot). Before attach (prepare not yet run) optimistically report
    // readable so the open handshake is not stalled.
    int readable;
    if (!p->opened) {
        readable = 1;
    } else {
        uint64_t occ = jts_ring_reader_occupancy_slots(&p->reader);
        readable = (occ > 0) || (p->stage_frames > 0) || (p->pending_silence_frames > 0);
    }
    if (readable) *revents |= POLLIN;
    return 0;
}

static int jts_ring_poll_descriptors_count(snd_pcm_ioplug_t *io) {
    (void)io;
    return 1;
}

static int jts_ring_poll_descriptors(snd_pcm_ioplug_t *io, struct pollfd *pfd,
                                     unsigned int space) {
    jts_ring_pcm_t *p = io->private_data;
    if (space < 1 || p->timer_fd < 0) return 0;
    pfd[0].fd = p->timer_fd;
    pfd[0].events = POLLIN;
    pfd[0].revents = 0;
    return 1;
}

static int jts_ring_poll_revents(snd_pcm_ioplug_t *io, struct pollfd *pfd,
                                 unsigned int nfds, unsigned short *revents) {
    jts_ring_pcm_t *p = io->private_data;
    *revents = 0;
    if (nfds >= 1 && (pfd[0].revents & POLLIN)) {
        // Drain the timerfd expirations.
        uint64_t expirations = 0;
        ssize_t r = read(p->timer_fd, &expirations, sizeof(expirations));
        (void)r;
    }
    // Report POLLOUT (writable) iff a publish would proceed without blocking:
    // the ring has space, OR there is no live reader (in which case publish
    // free-run-drops immediately — also "writable" from the app's view, and
    // what lets a stalled/absent reader never block the app on poll). A FULL
    // ring WITH a live reader is genuinely not-yet-writable: we withhold POLLOUT
    // and let the timerfd re-poll rather than reporting a false writable and
    // busy-spinning the app on a slot it cannot take. This is the honest
    // prototype poll; a futex wait is the productization. Before the writer is
    // attached (prepare not yet run) we optimistically report writable so the
    // open/prepare handshake is not stalled.
    int writable = p->opened ? jts_ring_writer_can_accept(&p->writer) : 1;
    if (writable) *revents |= POLLOUT;
    return 0;
}

static const snd_pcm_ioplug_callback_t jts_ring_callback = {
    .start = jts_ring_start,
    .stop = jts_ring_stop,
    .pointer = jts_ring_pointer,
    .transfer = jts_ring_transfer,
    .delay = jts_ring_delay,
    .drain = jts_ring_drain,
    .prepare = jts_ring_prepare,
    .hw_params = jts_ring_hw_params,
    .close = jts_ring_close,
    .poll_descriptors_count = jts_ring_poll_descriptors_count,
    .poll_descriptors = jts_ring_poll_descriptors,
    .poll_revents = jts_ring_poll_revents,
};

// CAPTURE table (Ring A). start/stop/hw_params/close and the poll-descriptor
// pair are shared with playback (timer-based, stream-agnostic); the
// direction-specific callbacks are the capture set. No `.drain`: draining a
// capture stream has nothing to flush OUTWARD (the app pulls remaining frames
// via ordinary reads), so ALSA's default capture drain (stop) is correct — a
// custom bounded drain would only duplicate that with no benefit and risk
// blocking on a live-but-empty ring the way the playback drain deliberately
// avoids.
static const snd_pcm_ioplug_callback_t jts_ring_capture_callback = {
    .start = jts_ring_start,
    .stop = jts_ring_stop,
    .pointer = jts_ring_capture_pointer,
    .transfer = jts_ring_capture_transfer,
    .delay = jts_ring_capture_delay,
    .prepare = jts_ring_capture_prepare,
    .hw_params = jts_ring_hw_params,
    .close = jts_ring_close,
    .poll_descriptors_count = jts_ring_poll_descriptors_count,
    .poll_descriptors = jts_ring_poll_descriptors,
    .poll_revents = jts_ring_capture_poll_revents,
};

static int jts_ring_set_hw_constraints(jts_ring_pcm_t *p) {
    snd_pcm_ioplug_t *io = &p->io;
    int rc;

    // Access modes. PLAYBACK advertises RW + MMAP: the emulated mmap area a
    // playback app writes into is APP-authored, so alsa-lib's mmap-commit path
    // only ever hands our `transfer` the frames the app itself wrote — no stale
    // bytes can reach the ring. CAPTURE advertises RW ONLY. With mmap_rw=0 the
    // capture mmap area is filled by OUR `transfer`, and this transfer legitimately
    // returns SHORT (delivered < requested) on the writer-alive-empty pacing block.
    // alsa-lib's ioplug mmap-capture avail/commit accounting can expose the mmap
    // region beyond `delivered` — stale/uninitialised bytes camilla would read as
    // audio — whereas the RW path's return value directly bounds what the app sees,
    // so a short read never leaks unfilled bytes. Forcing camilla (and the arecord
    // probe) onto the bounded RW path closes that stale-bytes lane; both use RW
    // already, so this costs nothing.
    static const unsigned int accesses_rw_only[] = {SND_PCM_ACCESS_RW_INTERLEAVED};
    static const unsigned int accesses_rw_mmap[] = {SND_PCM_ACCESS_RW_INTERLEAVED,
                                                    SND_PCM_ACCESS_MMAP_INTERLEAVED};
    const unsigned int *accesses;
    unsigned int n_accesses;
    if (io->stream == SND_PCM_STREAM_CAPTURE) {
        accesses = accesses_rw_only;
        n_accesses = sizeof(accesses_rw_only) / sizeof(accesses_rw_only[0]);
    } else {
        accesses = accesses_rw_mmap;
        n_accesses = sizeof(accesses_rw_mmap) / sizeof(accesses_rw_mmap[0]);
    }
    rc = snd_pcm_ioplug_set_param_list(io, SND_PCM_IOPLUG_HW_ACCESS, n_accesses, accesses);
    if (rc < 0) return rc;

    static const unsigned int formats[] = {SND_PCM_FORMAT_S16_LE};
    rc = snd_pcm_ioplug_set_param_list(io, SND_PCM_IOPLUG_HW_FORMAT,
                                       sizeof(formats) / sizeof(formats[0]), formats);
    if (rc < 0) return rc;

    rc = snd_pcm_ioplug_set_param_minmax(io, SND_PCM_IOPLUG_HW_CHANNELS,
                                         JTS_RING_CHANNELS, JTS_RING_CHANNELS);
    if (rc < 0) return rc;

    rc = snd_pcm_ioplug_set_param_minmax(io, SND_PCM_IOPLUG_HW_RATE, JTS_RING_RATE,
                                         JTS_RING_RATE);
    if (rc < 0) return rc;

    // Period = exactly one slot (period_frames). Buffer = n_slots periods.
    unsigned int period_bytes =
        p->period_frames * JTS_RING_CHANNELS * (unsigned int)sizeof(int16_t);
    rc = snd_pcm_ioplug_set_param_minmax(io, SND_PCM_IOPLUG_HW_PERIOD_BYTES, period_bytes,
                                         period_bytes);
    if (rc < 0) return rc;

    rc = snd_pcm_ioplug_set_param_minmax(io, SND_PCM_IOPLUG_HW_PERIODS, p->n_slots,
                                         p->n_slots);
    if (rc < 0) return rc;

    return 0;
}

SND_PCM_PLUGIN_DEFINE_FUNC(jts_ring) {
    snd_config_iterator_t i, next;
    const char *path = JTS_RING_DEFAULT_PATH;
    long period_frames = JTS_RING_DEFAULT_PERIOD;
    long n_slots = JTS_RING_DEFAULT_SLOTS;
    int rc;

    // `root` is part of the plugin-open signature (the top-level ALSA config
    // tree) but this plugin resolves everything from its own `conf` node; mark
    // it used so `-Werror` does not trip on the unused macro parameter.
    (void)root;

    // Both directions are supported: PLAYBACK is Ring B (this plugin WRITES the
    // ring, outputd reads) and CAPTURE is Ring A (this plugin READS the ring,
    // fanin writes). The callback table + io.name are chosen by `stream` below.
    if (stream != SND_PCM_STREAM_PLAYBACK && stream != SND_PCM_STREAM_CAPTURE) {
        SNDERR("jts_ring: unsupported stream direction");
        return -EINVAL;
    }

    snd_config_for_each(i, next, conf) {
        snd_config_t *n = snd_config_iterator_entry(i);
        const char *id;
        if (snd_config_get_id(n, &id) < 0) continue;
        if (!strcmp(id, "comment") || !strcmp(id, "type") || !strcmp(id, "hint"))
            continue;
        if (!strcmp(id, "path")) {
            if (snd_config_get_string(n, &path) < 0) {
                SNDERR("jts_ring: path must be a string");
                return -EINVAL;
            }
            continue;
        }
        if (!strcmp(id, "period_frames")) {
            if (snd_config_get_integer(n, &period_frames) < 0) {
                SNDERR("jts_ring: period_frames must be an integer");
                return -EINVAL;
            }
            continue;
        }
        if (!strcmp(id, "n_slots")) {
            if (snd_config_get_integer(n, &n_slots) < 0) {
                SNDERR("jts_ring: n_slots must be an integer");
                return -EINVAL;
            }
            continue;
        }
        SNDERR("jts_ring: unknown field %s", id);
        return -EINVAL;
    }

    if (period_frames <= 0 || period_frames > 65536) {
        SNDERR("jts_ring: period_frames out of range");
        return -EINVAL;
    }
    if (n_slots < (long)JTS_RING_MIN_SLOTS || n_slots > (long)JTS_RING_MAX_SLOTS) {
        SNDERR("jts_ring: n_slots out of range 2..=16");
        return -EINVAL;
    }

    jts_ring_pcm_t *p = calloc(1, sizeof(*p));
    if (!p) return -ENOMEM;
    p->timer_fd = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC);
    // A missing timerfd is not fatal — poll degrades to ALSA's own timeout — but
    // log it.
    if (p->timer_fd < 0) {
        SNDERR("jts_ring: timerfd_create failed (poll will fall back): %s",
               strerror(errno));
    }
    snprintf(p->path, sizeof(p->path), "%s", path);
    p->period_frames = (uint32_t)period_frames;
    p->n_slots = (uint32_t)n_slots;

    p->io.version = SND_PCM_IOPLUG_VERSION;
    if (stream == SND_PCM_STREAM_CAPTURE) {
        p->io.name = "JTS Ring A capture (SHM ping-pong)";
        p->io.callback = &jts_ring_capture_callback;
    } else {
        p->io.name = "JTS Ring B playback (SHM ping-pong)";
        p->io.callback = &jts_ring_callback;
    }
    p->io.mmap_rw = 0;
    p->io.private_data = p;

    rc = snd_pcm_ioplug_create(&p->io, name, stream, mode);
    if (rc < 0) {
        if (p->timer_fd >= 0) close(p->timer_fd);
        free(p);
        return rc;
    }

    rc = jts_ring_set_hw_constraints(p);
    if (rc < 0) {
        snd_pcm_ioplug_delete(&p->io);
        return rc;
    }

    *pcmp = p->io.pcm;
    return 0;
}

SND_PCM_PLUGIN_SYMBOL(jts_ring);
