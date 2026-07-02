// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0
//
// JTS Ring B — ALSA ioplug PLAYBACK plugin (`pcm.jts_ring`).
//
// CamillaDSP (or aplay, for the resolvability probe) opens the ALSA PCM
// `jts_ring_playback` and writes S16LE/2ch/48 kHz interleaved frames; this
// plugin stages them into whole slots and publishes each full slot into the SHM
// ping-pong ring (jts_ring_shm.c, the WRITER core). jasper-outputd is the
// reader (rust/jasper-ring) and the DAC pacer. This replaces the outputd
// content snd-aloop hop.
//
// PROTOTYPE, flag-gated: only reachable via the lab-only asound drop-in
// (scripts/ring-proto/arm.sh installs /etc/alsa/conf.d/98-jts-ring-proto.conf).
// Nothing in the product ALSA config references `type jts_ring`.
//
// ---- The eight questions ----
//
// 1. What breaks if the reader (outputd) dies? The writer's space check fails
//    and, seeing no live reader heartbeat, FREE-RUNS by dropping the OLDEST slot
//    (advancing read_seq on the absent reader's behalf, writer_drop_no_reader++)
//    rather than blocking Camilla in writei. Advancing read_seq — not just
//    discarding the new slot — is load-bearing for the honest pointer (Q7): it
//    keeps occupancy bounded at n_slots so in_flight/avail keep advancing, so
//    aplay/Camilla never wedge on a readerless ring waiting for a pointer that
//    cannot move. This is what keeps Camilla healthy when outputd's flag is off
//    and what makes the `aplay -D jts_ring_playback ... /dev/zero`
//    resolvability probe terminate.
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
//    camilla's rate controller, and tripped its stall detector).
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
    jts_ring_writer_t writer;
    char path[256];
    uint32_t period_frames;
    uint32_t n_slots;
    int opened; // writer attached
    // Frame staging: Camilla may writei() fewer than a whole slot at a time; we
    // accumulate into `stage` until a full slot (period_frames) is ready, then
    // publish. `stage_frames` counts frames buffered.
    int16_t *stage;
    size_t stage_frames;
    size_t stage_capacity_frames; // == period_frames
    // Total frames ACCEPTED from the app (== ALSA appl_ptr). `pointer` derives
    // the honest hw_ptr (frames the READER has drained) from this minus the
    // in-flight count, so ALSA's avail/delay reflect the reader's real progress
    // rather than "everything accepted is already played" (see jts_ring_pointer).
    snd_pcm_uframes_t appl_frames;
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
    // Honest hw_ptr = appl_frames - in_flight, where in_flight = the frames
    // published-but-unread (occupancy_slots * period_frames) plus frames staged
    // but not yet published. That equals what `delay` already reports; deriving
    // both from the reader's read_seq keeps avail/delay mutually consistent.
    uint64_t in_flight =
        p->opened
            ? jts_ring_writer_occupancy_slots(&p->writer) * (uint64_t)p->period_frames +
                  (uint64_t)p->stage_frames
            : 0;
    // appl_frames is monotonic and always >= in_flight (the reader cannot drain
    // more than was accepted), but clamp defensively against a transient race
    // where occupancy is sampled a hair before appl_frames is updated.
    snd_pcm_uframes_t hw_ptr =
        (p->appl_frames >= in_flight) ? (p->appl_frames - (snd_pcm_uframes_t)in_flight)
                                      : 0;
    // ALSA wants the position modulo the buffer. NIT (for the futex
    // productization): if the app is absent while EXACTLY buffer_size frames
    // drain, two successive `pointer` reads see the same value mod buffer_size —
    // a whole lap looks like zero delta, so avail momentarily reads 0 until the
    // next real advance (or camilla's stall recovery) unsticks it. This is
    // self-limiting (one lap, and only under a precisely-full-buffer drain with
    // no polling app) and harmless in the timerfd-poll prototype, which re-polls
    // sub-period. A futex-based productization that wakes exactly on read_seq
    // advance should report an absolute (non-wrapped) hw_ptr, or wake often
    // enough that a full lap cannot pass between reads.
    return (snd_pcm_sframes_t)(hw_ptr % io->buffer_size);
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
        SNDERR("jts_ring: closing published_slots=%llu drop_no_reader=%llu full_waits=%llu",
               (unsigned long long)p->writer.published_slots,
               (unsigned long long)p->writer.drop_no_reader,
               (unsigned long long)p->writer.full_waits);
        jts_ring_writer_close(&p->writer);
        free(p->stage);
        p->stage = NULL;
        p->opened = 0;
    }
    if (p->timer_fd >= 0) close(p->timer_fd);
    free(p);
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

static int jts_ring_set_hw_constraints(jts_ring_pcm_t *p) {
    snd_pcm_ioplug_t *io = &p->io;
    int rc;

    static const unsigned int accesses[] = {SND_PCM_ACCESS_RW_INTERLEAVED,
                                            SND_PCM_ACCESS_MMAP_INTERLEAVED};
    rc = snd_pcm_ioplug_set_param_list(io, SND_PCM_IOPLUG_HW_ACCESS,
                                       sizeof(accesses) / sizeof(accesses[0]), accesses);
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

    if (stream != SND_PCM_STREAM_PLAYBACK) {
        SNDERR("jts_ring: playback only");
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
    p->io.name = "JTS Ring B playback (SHM ping-pong)";
    p->io.mmap_rw = 0;
    p->io.callback = &jts_ring_callback;
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
