// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0
//
// JTS Ring B — SHM ping-pong ring, C11 WRITER core (pure, no ALSA).
//
// The WRITER + shared create/attach logic behind the ALSA ioplug
// (pcm_jts_ring.c). Host-compilable (no alsa-lib), so test_ring_core.c and the
// bench exercise it on any host. See jts_ring_shm.h for the contract.

#include "jts_ring_shm.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

// The owned SHM root: only a magic-invalid file directly under here may be
// unlinked and recreated (narrow-path reclaim, mirroring the Rust reader's
// is_owned_ring_path / outputd's is_owned_runtime_pipe_path).
#define JTS_RING_OWNED_DIR "/dev/shm/jts-ring"

// Bounded number of full-ring wait ticks before a live-reader publish gives up
// and drops (defends against a reader that stamps a heartbeat but never
// advances read_seq). At <=2 ms/tick this caps the writer stall at ~64 ms.
#define JTS_RING_MAX_FULL_WAIT_TICKS 32

uint64_t jts_ring_monotonic_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

size_t jts_ring_samples_per_slot(const jts_ring_geometry_t *g) {
    return (size_t)g->period_frames * (size_t)g->channels;
}

static size_t bytes_per_sample(const jts_ring_geometry_t *g) {
    switch (g->sample_format) {
        case JTS_RING_SAMPLE_FORMAT_S16LE:
            return 2;
        case JTS_RING_SAMPLE_FORMAT_S32LE:
            return 4;
        default:
            return 2;
    }
}

size_t jts_ring_slot_bytes(const jts_ring_geometry_t *g) {
    return jts_ring_samples_per_slot(g) * bytes_per_sample(g);
}

size_t jts_ring_file_size(const jts_ring_geometry_t *g) {
    return (size_t)JTS_RING_HEADER_BYTES + (size_t)g->n_slots * jts_ring_slot_bytes(g);
}

int jts_ring_geometry_validate(const jts_ring_geometry_t *g, const char **reason) {
    if (g->sample_format != JTS_RING_SAMPLE_FORMAT_S16LE) {
        if (reason) *reason = "sample_format unsupported (only S16LE)";
        return 1;
    }
    if (g->channels != 2) {
        if (reason) *reason = "channels unsupported (only stereo)";
        return 1;
    }
    if (g->rate != 48000) {
        if (reason) *reason = "rate unsupported (only 48000)";
        return 1;
    }
    if (g->period_frames == 0) {
        if (reason) *reason = "period_frames must be > 0";
        return 1;
    }
    if (g->n_slots < JTS_RING_MIN_SLOTS || g->n_slots > JTS_RING_MAX_SLOTS) {
        if (reason) *reason = "n_slots out of range 2..=16";
        return 1;
    }
    if (reason) *reason = NULL;
    return 0;
}

static jts_ring_header_t *hdr(const jts_ring_writer_t *w) {
    return (jts_ring_header_t *)w->base;
}

// The magic (offset 0, low half) + version (offset 4, high half) share one
// 8-byte-aligned qword. The Rust reader loads this qword as a single
// AtomicU64 (Acquire); the C side MUST publish/read it as the same atomic
// qword so the cross-process, cross-language access is a well-defined
// atomic-vs-atomic pairing, not a non-atomic-store / atomic-load data race.
// `base` is the mmap base (page-aligned), so offset 0 is 8-byte aligned; the
// _Atomic uint64_t access lowers to the same aarch64 ldar/stlr the Rust
// AtomicU64 emits.
static _Atomic uint64_t *magic_qword_ptr(void *base) {
    return (_Atomic uint64_t *)base;
}

static int16_t *slot_ptr(const jts_ring_writer_t *w, uint32_t slot_index) {
    uint8_t *base = (uint8_t *)w->base;
    return (int16_t *)(base + JTS_RING_HEADER_BYTES + (size_t)slot_index * w->slot_bytes);
}

static void clamped_nanosleep(uint32_t period_frames) {
    // 1/4 period, clamped to <= 2 ms (the prototype poll; productization is a
    // FUTEX_WAIT on futex_word). period_ns = period_frames / 48000 * 1e9.
    uint64_t period_ns = (uint64_t)period_frames * 1000000000ull / 48000ull;
    uint64_t nap_ns = period_ns / 4;
    if (nap_ns > 2000000ull) nap_ns = 2000000ull; // 2 ms cap
    if (nap_ns == 0) nap_ns = 1000ull;            // never spin hot
    struct timespec ts = {.tv_sec = 0, .tv_nsec = (long)nap_ns};
    nanosleep(&ts, NULL);
}

static int owned_ring_path(const char *path) {
    // True iff `path` is directly under JTS_RING_OWNED_DIR (no nesting), i.e.
    // dirname(path) == JTS_RING_OWNED_DIR. Narrow, string-based, mirroring the
    // Rust reader.
    const size_t dlen = sizeof(JTS_RING_OWNED_DIR) - 1;
    if (strncmp(path, JTS_RING_OWNED_DIR, dlen) != 0) return 0;
    if (path[dlen] != '/') return 0;
    const char *rest = path + dlen + 1;
    if (rest[0] == '\0') return 0;          // the dir itself
    if (strchr(rest, '/') != NULL) return 0; // nested
    return 1;
}

// Create `path`'s parent directory (mkdir -p of dirname) before the O_EXCL
// create. Mirrors the Rust reader's ensure_parent_dir: on a fresh box (or after
// disarm.sh's `rm -rf /dev/shm/jts-ring`) the tmpfs directory does not exist, so
// the ioplug's create would fail ENOENT. Also heals the reboot-while-armed edge
// where CamillaDSP opens the ring before outputd (tmpfs is empty on boot). Best
// effort per component so an already-existing parent (EEXIST) is not an error.
// Returns 0 on success (parent exists afterward), negative errno on failure.
static int ensure_parent_dir(const char *path) {
    const char *slash = strrchr(path, '/');
    if (!slash || slash == path) return 0; // no parent, or parent is "/"
    size_t plen = (size_t)(slash - path);
    char buf[PATH_MAX];
    if (plen >= sizeof(buf)) return -ENAMETOOLONG;
    memcpy(buf, path, plen);
    buf[plen] = '\0';
    // Walk each component, mkdir-ing as we go (mkdir -p). Skip the leading '/'.
    for (char *p = buf + 1; *p; p++) {
        if (*p == '/') {
            *p = '\0';
            if (mkdir(buf, 0770) < 0 && errno != EEXIST) return -errno;
            *p = '/';
        }
    }
    if (mkdir(buf, 0770) < 0 && errno != EEXIST) return -errno;
    return 0;
}

// Map an already-open fd of `len` bytes.
static void *map_fd(int fd, size_t len) {
    void *base = mmap(NULL, len, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (base == MAP_FAILED) return NULL;
    return base;
}

// Init a freshly-created (O_EXCL) fd: ftruncate, map, write config fields, then
// publish magic LAST with release. Returns 0 on success.
static int init_created(int fd, const jts_ring_geometry_t *g, jts_ring_writer_t *out) {
    size_t file_size = jts_ring_file_size(g);
    if (ftruncate(fd, (off_t)file_size) < 0) return -errno;
    void *base = map_fd(fd, file_size);
    if (!base) return -errno;
    jts_ring_header_t *h = (jts_ring_header_t *)base;

    // ftruncate zeroed everything; set the config fields explicitly, then the
    // atomics start at 0 (which is correct: seqs/epoch/pids/heartbeats).
    h->version = JTS_RING_VERSION;
    h->rate = g->rate;
    h->channels = g->channels;
    h->sample_format = g->sample_format;
    h->period_frames = g->period_frames;
    h->n_slots = g->n_slots;
    h->_pad = 0;
    h->futex_word = 0;
    // Publish magic LAST with a single Release store of the magic+version
    // qword (magic in the low 4 bytes, version in the high 4). This is the
    // exact mirror of the Rust creator's write_u32_release_magic: an attacher
    // whose Acquire load of the qword observes the magic observes the whole
    // fully-initialized header, and — critically — it is an atomic store to
    // the same location the Rust reader loads atomically, so there is no
    // non-atomic-store / atomic-load data race across the C-writer <-> Rust-
    // reader boundary. The config fields above are ordinary stores ordered
    // before this by the Release.
    uint64_t magic_qword = (uint64_t)JTS_RING_MAGIC | ((uint64_t)JTS_RING_VERSION << 32);
    atomic_store_explicit(magic_qword_ptr(base), magic_qword, memory_order_release);

    out->base = base;
    out->map_len = file_size;
    out->fd = fd;
    out->geometry = *g;
    return 0;
}

// Wait (bounded) for the creator's magic. Returns 1 if seen, 0 on timeout.
// `base` is the mmap base (offset 0 = the magic+version qword).
static int wait_for_magic(void *base) {
    uint64_t deadline = jts_ring_monotonic_ns() + JTS_RING_MAGIC_WAIT_TIMEOUT_MS * 1000000ull;
    for (;;) {
        // Acquire-load the magic+version qword (mirrors the Rust reader). The
        // Acquire pairs with the creator's Release qword store, so observing
        // the magic in the low 4 bytes establishes happens-before against the
        // whole initialized header.
        uint64_t qword = atomic_load_explicit(magic_qword_ptr(base), memory_order_acquire);
        uint32_t magic = (uint32_t)qword;
        if (magic == JTS_RING_MAGIC) return 1;
        if (jts_ring_monotonic_ns() >= deadline) return 0;
        struct timespec ts = {.tv_sec = 0,
                              .tv_nsec = (long)(JTS_RING_MAGIC_WAIT_STEP_US * 1000ull)};
        nanosleep(&ts, NULL);
    }
}

// Attach to an existing fd. On a valid ring, fills *out and returns 0. On a
// creator-crashed-mid-init file (no magic / too small), returns 1 (caller may
// reclaim if owned). On a genuine geometry mismatch (valid magic, wrong
// shape/size), returns -1 (fatal).
static int attach_existing(int fd, const jts_ring_geometry_t *expected,
                           jts_ring_writer_t *out, const char **reason) {
    struct stat st;
    if (fstat(fd, &st) < 0) {
        if (reason) *reason = "fstat failed";
        return -1;
    }
    if ((uint64_t)st.st_size < (uint64_t)JTS_RING_HEADER_BYTES) {
        // Mid-init (or not a ring): reclaimable-as-torn.
        return 1;
    }
    size_t actual = (size_t)st.st_size;
    void *base = map_fd(fd, actual);
    if (!base) {
        if (reason) *reason = "mmap failed";
        return -1;
    }
    jts_ring_header_t *h = (jts_ring_header_t *)base;
    if (!wait_for_magic(base)) {
        munmap(base, actual);
        return 1; // torn init
    }
    // Magic present -> header fully written. Cross-check the size the header's
    // own declared geometry implies (a valid-magic corrupt/truncated ring is
    // fatal, not reclaimable).
    jts_ring_geometry_t header_g = {.rate = h->rate,
                                    .channels = h->channels,
                                    .sample_format = h->sample_format,
                                    .period_frames = h->period_frames,
                                    .n_slots = h->n_slots};
    if (jts_ring_file_size(&header_g) != actual) {
        munmap(base, actual);
        if (reason) *reason = "file size inconsistent with header geometry";
        return -1;
    }
    if (h->version != JTS_RING_VERSION || header_g.rate != expected->rate ||
        header_g.channels != expected->channels ||
        header_g.sample_format != expected->sample_format ||
        header_g.period_frames != expected->period_frames ||
        header_g.n_slots != expected->n_slots) {
        munmap(base, actual);
        if (reason) *reason = "ring header does not match expected geometry";
        return -1;
    }
    out->base = base;
    out->map_len = actual;
    out->fd = fd;
    out->geometry = *expected;
    return 0;
}

int jts_ring_writer_open(const char *path, const jts_ring_geometry_t *expected,
                         jts_ring_writer_t *out) {
    memset(out, 0, sizeof(*out));
    const char *reason = NULL;
    if (jts_ring_geometry_validate(expected, &reason) != 0) {
        fprintf(stderr, "event=jts_ring.writer.bad_geometry reason=%s\n",
                reason ? reason : "(unknown)");
        return -EINVAL;
    }

    // Ensure /dev/shm/jts-ring/ exists before O_EXCL create (fresh boot, or
    // after disarm.sh removed the tmpfs dir). Mirrors the Rust reader.
    int mkrc = ensure_parent_dir(path);
    if (mkrc != 0) {
        fprintf(stderr, "event=jts_ring.writer.mkdir_failed rc=%d path=%s\n", mkrc, path);
        return mkrc;
    }

    for (int attempt = 0; attempt < 8; attempt++) {
        int create_fd = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC, 0660);
        if (create_fd >= 0) {
            int rc = init_created(create_fd, expected, out);
            if (rc != 0) {
                close(create_fd);
                unlink(path); // drop the half-baked file
                fprintf(stderr, "event=jts_ring.writer.create_failed rc=%d\n", rc);
                return rc;
            }
            break; // created + initialized; fall through to writer-attach stamp
        }
        if (errno != EEXIST) {
            fprintf(stderr, "event=jts_ring.writer.create_open_failed errno=%d\n", errno);
            return -errno;
        }

        int fd = open(path, O_RDWR | O_CLOEXEC);
        if (fd < 0) {
            if (errno == ENOENT) continue; // raced an unlink; retry create
            return -errno;
        }
        int rc = attach_existing(fd, expected, out, &reason);
        if (rc == 0) break; // attached to a valid ring
        close(fd);
        if (rc < 0) {
            fprintf(stderr, "event=jts_ring.writer.attach_fatal reason=%s\n",
                    reason ? reason : "(unknown)");
            return -EINVAL;
        }
        // rc == 1: torn init. Only the owner may reclaim, only under the owned
        // path. Otherwise fatal (do not clobber a foreign file).
        if (!owned_ring_path(path)) {
            fprintf(stderr, "event=jts_ring.writer.torn_not_reclaimable path=%s\n", path);
            return -EINVAL;
        }
        unlink(path);
        fprintf(stderr, "event=jts_ring.writer.reclaimed_magic_invalid path=%s\n", path);
        // loop and re-create
    }

    // Guard against attempt exhaustion: if every one of the 8 iterations ended
    // in `continue` (raced an unlink) or reclaim-and-loop (pathological racing),
    // the loop falls through here with out->base still NULL. Dereferencing it
    // via hdr(out) below would segfault inside CamillaDSP/aplay. Fail loud
    // instead — the caller (ioplug open / bench) maps this to an open error.
    if (out->base == NULL) {
        fprintf(stderr, "event=jts_ring.writer.attach_exhausted path=%s\n", path);
        return -EAGAIN;
    }

    // Writer-attach stamp: epoch++ (release), pid, heartbeat, continue from the
    // stored write_seq (file-lifetime monotonic).
    jts_ring_header_t *h = hdr(out);
    out->slot_bytes = jts_ring_slot_bytes(&out->geometry);
    out->samples_per_slot = jts_ring_samples_per_slot(&out->geometry);
    out->write_seq = atomic_load_explicit(&h->write_seq, memory_order_acquire);
    uint64_t epoch = atomic_load_explicit(&h->writer_epoch, memory_order_acquire);
    atomic_store_explicit(&h->writer_epoch, epoch + 1, memory_order_release);
    atomic_store_explicit(&h->writer_pid, (uint64_t)getpid(), memory_order_relaxed);
    atomic_store_explicit(&h->writer_heartbeat_ns, jts_ring_monotonic_ns(),
                          memory_order_relaxed);
    return 0;
}

static int reader_is_live(const jts_ring_header_t *h, uint64_t now_ns) {
    uint64_t pid = atomic_load_explicit(&h->reader_pid, memory_order_relaxed);
    if (pid == 0) return 0;
    uint64_t hb = atomic_load_explicit(&h->reader_heartbeat_ns, memory_order_relaxed);
    if (hb == 0) return 0;
    // Saturating subtraction: the reader stamps its heartbeat concurrently, so a
    // heartbeat taken AFTER this writer sampled now_ns would make (now_ns - hb)
    // underflow to a huge unsigned value — spuriously classifying a live reader
    // as dead and dropping a slot on the normal full-ring back-pressure path.
    // Mirrors the Rust reader's now_ns.saturating_sub(hb): a future heartbeat
    // clamps age to 0 (definitely live).
    uint64_t age = (now_ns > hb) ? (now_ns - hb) : 0;
    return age < JTS_RING_WRITER_LIVENESS_TIMEOUT_NS;
}

jts_ring_publish_result_t jts_ring_writer_publish(jts_ring_writer_t *w,
                                                  const int16_t *samples) {
    jts_ring_header_t *h = hdr(w);
    uint64_t now = jts_ring_monotonic_ns();
    atomic_store_explicit(&h->writer_heartbeat_ns, now, memory_order_relaxed);

    uint64_t wseq = w->write_seq;
    int waited = 0;
    int dropped_oldest = 0;
    for (;;) {
        uint64_t rseq = atomic_load_explicit(&h->read_seq, memory_order_acquire);
        if (wseq - rseq < (uint64_t)w->geometry.n_slots) {
            break; // space available
        }
        // Full. If no live reader, FREE-RUN by dropping the OLDEST slot: advance
        // read_seq on the absent reader's behalf so the new slot has room, then
        // publish over it (the reader will never read it — it is overwritten on
        // the next lap — but the ring stays bounded). This is what prevents
        // Camilla wedging when outputd's flag is off and lets the aplay
        // resolvability probe finish. CRITICALLY it also keeps the ioplug's
        // pointer/delay HONEST: occupancy = write_seq - read_seq stays bounded at
        // n_slots (not pinned at "full forever" with read_seq stuck at 0), so
        // jts_ring_pointer's in_flight stays bounded and hw_ptr advances with
        // appl_frames — ALSA's `avail` never sticks at 0, so the WRITER (aplay /
        // Camilla) never blocks forever waiting for a pointer that cannot move.
        // Writing read_seq here is safe against a reader reattach: attach resyncs
        // read_seq = write_seq unconditionally, so our advance is discarded (and
        // occupancy collapses to 0), which only jumps hw_ptr FORWARD (benign —
        // avail grows). No LIVE reader advances read_seq, so on the steady
        // readerless path this store has no concurrent writer. The one exception
        // is a reader whose heartbeat went stale (wedged > liveness timeout) and
        // then resumes: it may store read_seq from its stale local mirror
        // concurrently with this free-run store. That race is bounded to at most
        // one torn 128-frame slot and is self-healing (the reader's next Acquire
        // load of write_seq re-establishes ordering; real drift trips its
        // > n_slots defensive resync) — documented in the SPSC contract in
        // rust/jasper-ring/src/lib.rs "Torn-write safety".
        if (!reader_is_live(h, jts_ring_monotonic_ns())) {
            atomic_store_explicit(&h->read_seq, rseq + 1, memory_order_release);
            w->drop_no_reader++;
            dropped_oldest = 1;
            break; // room made; publish the new slot over the dropped-oldest lap
        }
        if (waited == 0) w->full_waits++;
        if (++waited > JTS_RING_MAX_FULL_WAIT_TICKS) {
            // A reader that heartbeats but never advances: drop rather than
            // stall Camilla unboundedly. (Should not happen with our reader.)
            w->drop_no_reader++;
            return JTS_RING_PUBLISH_DROPPED;
        }
        clamped_nanosleep(w->geometry.period_frames);
        // refresh heartbeat while we wait so the reader sees us alive
        atomic_store_explicit(&h->writer_heartbeat_ns, jts_ring_monotonic_ns(),
                              memory_order_relaxed);
    }

    // Space confirmed. memcpy payload into slot (wseq % n_slots) with plain
    // stores, then store write_seq+1 with release (the reader's Acquire load of
    // write_seq synchronizes-with this and sees the complete payload).
    uint32_t slot_index = (uint32_t)(wseq % (uint64_t)w->geometry.n_slots);
    memcpy(slot_ptr(w, slot_index), samples, w->slot_bytes);
    uint64_t next = wseq + 1;
    atomic_store_explicit(&h->write_seq, next, memory_order_release);
    w->write_seq = next;
    // A free-run drop-oldest still WROTE the payload into the ring and advanced
    // write_seq (so the pointer stays honest), but the frames it displaced will
    // never reach a reader — report DROPPED, not OK, so counters/observability
    // reflect that no live reader consumed this lap. Do not count it as a
    // published-to-a-reader slot.
    if (dropped_oldest) {
        return JTS_RING_PUBLISH_DROPPED;
    }
    w->published_slots++;
    return JTS_RING_PUBLISH_OK;
}

uint64_t jts_ring_writer_occupancy_slots(const jts_ring_writer_t *w) {
    const jts_ring_header_t *h = (const jts_ring_header_t *)w->base;
    uint64_t rseq = atomic_load_explicit(&h->read_seq, memory_order_acquire);
    return w->write_seq - rseq;
}

int jts_ring_writer_reader_is_live(const jts_ring_writer_t *w) {
    const jts_ring_header_t *h = (const jts_ring_header_t *)w->base;
    return reader_is_live(h, jts_ring_monotonic_ns());
}

int jts_ring_writer_can_accept(const jts_ring_writer_t *w) {
    const jts_ring_header_t *h = (const jts_ring_header_t *)w->base;
    uint64_t rseq = atomic_load_explicit(&h->read_seq, memory_order_acquire);
    uint64_t occ = w->write_seq - rseq;
    if (occ < (uint64_t)w->geometry.n_slots) return 1; // space
    // Full: only truly not-writable if a live reader is expected to drain it.
    // With no live reader, publish free-run-drops immediately, so still accept.
    return reader_is_live(h, jts_ring_monotonic_ns()) ? 0 : 1;
}

void jts_ring_writer_close(jts_ring_writer_t *w) {
    if (!w || !w->base) return;
    jts_ring_header_t *h = hdr(w);
    // Clear writer_pid only if it is ours (a re-attached writer with a bumped
    // epoch owns it now).
    uint64_t mine = (uint64_t)getpid();
    uint64_t cur = atomic_load_explicit(&h->writer_pid, memory_order_relaxed);
    if (cur == mine) {
        atomic_store_explicit(&h->writer_pid, 0, memory_order_relaxed);
    }
    munmap(w->base, w->map_len);
    if (w->fd >= 0) close(w->fd);
    w->base = NULL;
    w->fd = -1;
}
