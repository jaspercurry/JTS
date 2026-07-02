// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! JTS Ring SHM header layout — the single source of truth for header offsets,
//! constants, and geometry math, shared by the Rust reader ([`crate`]) and
//! pinned against the C writer's `_Static_assert`ed header
//! (`c/jts-ring-ioplug/jts_ring_shm.h`) by the golden-layout test below.
//!
//! Every offset here is a compile-time `const` and is asserted in
//! [`tests::golden_layout_matches_the_shm_contract`]. If the C header and this
//! module ever disagree, the on-disk bytes the writer produces and the reader
//! expects diverge silently — the golden test is the drift guard that keeps
//! them byte-for-byte identical. **When you change an offset here, change the C
//! header AND its `_Static_assert` in the same commit.**

use std::io;

/// `"JRIN"` little-endian. Written LAST during init (Release); the attach
/// validity gate.
pub const MAGIC: u32 = 0x4A52_494E;

/// Header contract version. Bumped on any incompatible layout change.
pub const VERSION: u32 = 1;

/// Total header size in bytes. Slots begin at this offset. 8-byte aligned; the
/// mmap base is page-aligned, so every atomic field is naturally aligned.
pub const HEADER_BYTES: usize = 128;

/// `sample_format` = 1: interleaved signed 16-bit little-endian (S16LE). Chosen
/// because it matches what CamillaDSP emits on its ALSA playback lane today
/// (`DEFAULT_PLAYBACK_FORMAT="S16_LE"`), so the reader copy is conversion-free.
pub const SAMPLE_FORMAT_S16LE: u32 = 1;

/// `sample_format` = 2: S32LE. Reserved for future wide/active lanes; not used
/// by the prototype.
pub const SAMPLE_FORMAT_S32LE: u32 = 2;

/// Bytes per sample for [`SAMPLE_FORMAT_S16LE`].
pub const S16LE_BYTES_PER_SAMPLE: usize = 2;

/// Prototype floor / ceiling on `n_slots`: 2 (ping-pong) through 16. 3 is the
/// documented degraded widening; the ceiling was raised 4 -> 16 on 2026-07-02
/// so the ALSA playback buffer (`n_slots * period_frames`) can clear
/// CamillaDSP's negotiated buffer size and its `target_level` (see
/// `c/jts-ring-ioplug/jts_ring_shm.h` `JTS_RING_MAX_SLOTS` — kept in lockstep,
/// and `MAX_SHM_RING_SLOTS` in the outputd config).
pub const MIN_N_SLOTS: u32 = 2;
pub const MAX_N_SLOTS: u32 = 16;

// --- Header field offsets (bytes from the start of the mapping) ---

/// `magic` (u32) — offset 0. Also the low half of the [`OFF_MAGIC_QWORD`]
/// 8-byte atomic used for the Release publish.
pub const OFF_MAGIC: usize = 0;
/// `version` (u32) — offset 4. High half of [`OFF_MAGIC_QWORD`].
pub const OFF_VERSION: usize = 4;
/// The 8-byte aligned qword covering `magic` (low) + `version` (high). The
/// creator publishes magic via a Release store of this qword so an attacher
/// that observes the magic observes a fully-initialized header.
pub const OFF_MAGIC_QWORD: usize = 0;
/// `rate` (u32) — offset 8.
pub const OFF_RATE: usize = 8;
/// `channels` (u32) — offset 12.
pub const OFF_CHANNELS: usize = 12;
/// `sample_format` (u32) — offset 16.
pub const OFF_SAMPLE_FORMAT: usize = 16;
/// `period_frames` (u32) — offset 20.
pub const OFF_PERIOD_FRAMES: usize = 20;
/// `n_slots` (u32) — offset 24.
pub const OFF_N_SLOTS: usize = 24;
/// `_pad` (u32) — offset 28. Zero.
pub const OFF_PAD: usize = 28;
/// `writer_epoch` (atomic u64) — offset 32.
pub const OFF_WRITER_EPOCH: usize = 32;
/// `write_seq` (atomic u64) — offset 40.
pub const OFF_WRITE_SEQ: usize = 40;
/// `read_seq` (atomic u64) — offset 48.
pub const OFF_READ_SEQ: usize = 48;
/// `writer_pid` (atomic u64) — offset 56.
pub const OFF_WRITER_PID: usize = 56;
/// `writer_heartbeat_ns` (atomic u64) — offset 64.
pub const OFF_WRITER_HEARTBEAT_NS: usize = 64;
/// `reader_pid` (atomic u64) — offset 72.
pub const OFF_READER_PID: usize = 72;
/// `reader_heartbeat_ns` (atomic u64) — offset 80.
pub const OFF_READER_HEARTBEAT_NS: usize = 80;
/// `futex_word` (u32, reserved, zero in v1) — offset 88.
pub const OFF_FUTEX_WORD: usize = 88;
/// Reserved bytes 92..128 (zero).
pub const OFF_RESERVED: usize = 92;

/// The ring's on-disk geometry: everything needed to size the file and index
/// slots. The prototype instance is S16LE / 2ch / 48 kHz, `period_frames` =
/// outputd's runtime period, `n_slots` = 2.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Geometry {
    pub rate: u32,
    pub channels: u32,
    pub sample_format: u32,
    pub period_frames: u32,
    pub n_slots: u32,
}

impl Geometry {
    /// Bytes per sample for this geometry's `sample_format`.
    pub fn bytes_per_sample(&self) -> usize {
        match self.sample_format {
            SAMPLE_FORMAT_S16LE => S16LE_BYTES_PER_SAMPLE,
            SAMPLE_FORMAT_S32LE => 4,
            _ => S16LE_BYTES_PER_SAMPLE,
        }
    }

    /// Interleaved samples in one slot (`period_frames * channels`).
    pub fn samples_per_slot(&self) -> usize {
        (self.period_frames as usize) * (self.channels as usize)
    }

    /// Bytes in one slot payload.
    pub fn slot_bytes(&self) -> usize {
        self.samples_per_slot() * self.bytes_per_sample()
    }

    /// Total mapped file size: header + all slots.
    pub fn file_size(&self) -> usize {
        HEADER_BYTES + (self.n_slots as usize) * self.slot_bytes()
    }

    /// Validate the geometry the caller wants BEFORE touching the filesystem.
    /// The prototype supports only S16LE / 2ch / 48 kHz with a bounded
    /// `n_slots`; anything else is a fail-loud config error (the daemon maps it
    /// to a config-class startup failure so systemd parks, not reboot-loops).
    pub fn validate_self(&self) -> io::Result<()> {
        if self.sample_format != SAMPLE_FORMAT_S16LE {
            return Err(cfg_err(format!(
                "ring sample_format {} unsupported by the prototype (only S16LE={})",
                self.sample_format, SAMPLE_FORMAT_S16LE
            )));
        }
        if self.channels != 2 {
            return Err(cfg_err(format!(
                "ring channels {} unsupported by the prototype (only stereo)",
                self.channels
            )));
        }
        if self.rate != 48_000 {
            return Err(cfg_err(format!(
                "ring rate {} unsupported by the prototype (only 48000)",
                self.rate
            )));
        }
        if self.period_frames == 0 {
            return Err(cfg_err("ring period_frames must be > 0".to_string()));
        }
        if !(MIN_N_SLOTS..=MAX_N_SLOTS).contains(&self.n_slots) {
            return Err(cfg_err(format!(
                "ring n_slots {} out of range {MIN_N_SLOTS}..={MAX_N_SLOTS}",
                self.n_slots
            )));
        }
        Ok(())
    }
}

fn cfg_err(msg: String) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidInput, msg)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The cross-language drift guard. These constants are duplicated in the C
    /// header (`jts_ring_shm.h`) as `_Static_assert(offsetof(...) == N)`; if
    /// either side changes an offset without the other, this test (Rust) and
    /// the C compile (via `_Static_assert`) both fail. Do not "fix" one side to
    /// pass — reconcile both.
    #[test]
    fn golden_layout_matches_the_shm_contract() {
        assert_eq!(MAGIC, 0x4A52_494E, "magic 'JRIN' LE");
        assert_eq!(VERSION, 1);
        assert_eq!(HEADER_BYTES, 128);

        assert_eq!(OFF_MAGIC, 0);
        assert_eq!(OFF_VERSION, 4);
        assert_eq!(OFF_MAGIC_QWORD, 0);
        assert_eq!(OFF_RATE, 8);
        assert_eq!(OFF_CHANNELS, 12);
        assert_eq!(OFF_SAMPLE_FORMAT, 16);
        assert_eq!(OFF_PERIOD_FRAMES, 20);
        assert_eq!(OFF_N_SLOTS, 24);
        assert_eq!(OFF_PAD, 28);
        assert_eq!(OFF_WRITER_EPOCH, 32);
        assert_eq!(OFF_WRITE_SEQ, 40);
        assert_eq!(OFF_READ_SEQ, 48);
        assert_eq!(OFF_WRITER_PID, 56);
        assert_eq!(OFF_WRITER_HEARTBEAT_NS, 64);
        assert_eq!(OFF_READER_PID, 72);
        assert_eq!(OFF_READER_HEARTBEAT_NS, 80);
        assert_eq!(OFF_FUTEX_WORD, 88);
        assert_eq!(OFF_RESERVED, 92);

        assert_eq!(SAMPLE_FORMAT_S16LE, 1);
        assert_eq!(SAMPLE_FORMAT_S32LE, 2);

        // Every atomic u64 field is 8-byte aligned.
        for off in [
            OFF_WRITER_EPOCH,
            OFF_WRITE_SEQ,
            OFF_READ_SEQ,
            OFF_WRITER_PID,
            OFF_WRITER_HEARTBEAT_NS,
            OFF_READER_PID,
            OFF_READER_HEARTBEAT_NS,
        ] {
            assert_eq!(off % 8, 0, "atomic field at {off} must be 8-byte aligned");
        }
    }

    // The reserved tail plus futex_word fit within the 128-byte header. These
    // are compile-time invariants (const expressions), so pin them as `const`
    // assertions rather than runtime `assert!` (which clippy flags as
    // optimized-out on constants).
    const _: () = assert!(OFF_RESERVED < HEADER_BYTES);
    const _: () = assert!(OFF_FUTEX_WORD + 4 <= HEADER_BYTES);

    #[test]
    fn geometry_sizes_the_prototype_instance() {
        let g = Geometry {
            rate: 48_000,
            channels: 2,
            sample_format: SAMPLE_FORMAT_S16LE,
            period_frames: 128,
            n_slots: 2,
        };
        assert_eq!(g.bytes_per_sample(), 2);
        assert_eq!(g.samples_per_slot(), 256); // 128 frames * 2 ch
        assert_eq!(g.slot_bytes(), 512); // 256 samples * 2 bytes
        assert_eq!(g.file_size(), 128 + 2 * 512); // header + 2 slots = 1152
        g.validate_self().unwrap();
    }

    #[test]
    fn geometry_rejects_unsupported_shapes() {
        let base = Geometry {
            rate: 48_000,
            channels: 2,
            sample_format: SAMPLE_FORMAT_S16LE,
            period_frames: 128,
            n_slots: 2,
        };
        assert!(Geometry {
            sample_format: SAMPLE_FORMAT_S32LE,
            ..base
        }
        .validate_self()
        .is_err());
        assert!(Geometry {
            channels: 4,
            ..base
        }
        .validate_self()
        .is_err());
        assert!(Geometry {
            rate: 44_100,
            ..base
        }
        .validate_self()
        .is_err());
        assert!(Geometry { n_slots: 1, ..base }.validate_self().is_err());
        assert!(Geometry {
            n_slots: 17,
            ..base
        }
        .validate_self()
        .is_err());
        // The raised ceiling accepts the full 2..=16 range (regression for the
        // 4 -> 16 bump that gives camilla's playback buffer enough depth).
        assert!(Geometry { n_slots: 4, ..base }.validate_self().is_ok());
        assert!(Geometry {
            n_slots: MAX_N_SLOTS,
            ..base
        }
        .validate_self()
        .is_ok());
    }
}
