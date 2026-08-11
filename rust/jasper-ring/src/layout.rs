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

/// `sample_format` = 1: interleaved signed 16-bit little-endian (S16LE). The
/// ring wire ships this format because `resolve_ring_wire` (the Python
/// control plane) holds it narrow by policy — NOT because it matches the
/// box's general ALSA playback lane, which is wide
/// (`DEFAULT_PLAYBACK_FORMAT = "S32_LE"` since the wide-output-path program).
/// The narrow peer it does agree with is the pipe/File sink
/// (`DEFAULT_PIPE_SINK_FORMAT = "S16_LE"`).
pub const SAMPLE_FORMAT_S16LE: u32 = 1;

/// `sample_format` = 2: interleaved signed 32-bit little-endian (S32LE).
/// Accepted by [`Geometry::validate_self`]; [`Geometry::bytes_per_sample`]
/// reports 4 bytes for it.
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

/// Ceiling on `channels`. 8 is the widest registered DAC channel count
/// (HiFiBerry DAC8x) and matches the `2..=8` bound outputd applies to
/// `JASPER_OUTPUTD_ACTIVE_CHANNELS`, so the layout accept-set IS the product
/// envelope rather than a looser superset of it. The floor is 2 — a literal in
/// [`Geometry::validate_self`], not a constant, because excluding mono is a
/// product policy rather than a property of the layout (the header could carry
/// `channels = 1` perfectly well; nothing in JTS produces one).
pub const MAX_RING_CHANNELS: u32 = 8;

/// Ceiling on one slot's payload bytes
/// (`period_frames * channels * bytes_per_sample`).
///
/// It bounds the WRITER-CREATE path: [`Geometry::validate_self`] runs before
/// the creator's `ftruncate` sizes the file in `/dev/shm`, so a geometry
/// demanding an absurd tmpfs allocation is refused before a page is charged.
/// The ATTACH path allocates nothing — it validates the header's declared
/// geometry against the size the file already has on disk — so a torn header
/// drives no allocation through this cap.
///
/// 64 KiB is deliberately roomy, and the headroom is real rather than
/// decorative. `period_frames` is the one unbounded input to a slot's size, so
/// it sets the binding constraint: outputd's default period is 1024 frames, and
/// at the widest accepted geometry (8 ch, S32) that is a 32 KiB slot — exactly
/// half the cap. Do not tighten toward the narrow stereo cases (fan-in's
/// 128-frame S16 stereo slot is 512 B); the cap exists to reject a geometry no
/// DAC period could justify, not to fence in the shipped ones.
pub const MAX_SLOT_BYTES: usize = 65_536;

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
/// slots. [`Geometry::validate_self`] owns the accept-set; a given ring's
/// instance values are resolved by whichever daemon creates it.
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
    pub fn bytes_per_sample(&self) -> io::Result<usize> {
        match self.sample_format {
            SAMPLE_FORMAT_S16LE => Ok(S16LE_BYTES_PER_SAMPLE),
            SAMPLE_FORMAT_S32LE => Ok(4),
            other => Err(cfg_err(format!("unsupported ring sample format {other}"))),
        }
    }

    /// Interleaved samples in one slot (`period_frames * channels`).
    pub fn samples_per_slot(&self) -> usize {
        (self.period_frames as usize) * (self.channels as usize)
    }

    /// Bytes in one slot payload.
    pub fn slot_bytes(&self) -> io::Result<usize> {
        self.samples_per_slot()
            .checked_mul(self.bytes_per_sample()?)
            .ok_or_else(|| cfg_err("ring slot byte size overflows usize".to_string()))
    }

    /// Total mapped file size: header + all slots.
    pub fn file_size(&self) -> io::Result<usize> {
        (self.n_slots as usize)
            .checked_mul(self.slot_bytes()?)
            .and_then(|slots| HEADER_BYTES.checked_add(slots))
            .ok_or_else(|| cfg_err("ring file size overflows usize".to_string()))
    }

    /// Validate the geometry the caller wants BEFORE touching the filesystem.
    ///
    /// The accept-set is S16LE or S32LE, 2..=[`MAX_RING_CHANNELS`] channels,
    /// 48 kHz, a non-zero `period_frames`, an `n_slots` in
    /// [`MIN_N_SLOTS`]..=[`MAX_N_SLOTS`], and a slot no larger than
    /// [`MAX_SLOT_BYTES`]. Anything outside it returns an
    /// [`io::ErrorKind::InvalidInput`] error — a config-class fault, distinct
    /// from a runtime one. This crate's contract ends there: how a caller
    /// surfaces that error (an exit code, a park, a fall back to another
    /// transport) belongs to the caller and its unit, not here.
    pub fn validate_self(&self) -> io::Result<()> {
        if self.sample_format != SAMPLE_FORMAT_S16LE && self.sample_format != SAMPLE_FORMAT_S32LE {
            return Err(cfg_err(format!(
                "ring sample_format {} unsupported (S16LE={SAMPLE_FORMAT_S16LE} or \
                 S32LE={SAMPLE_FORMAT_S32LE})",
                self.sample_format
            )));
        }
        // The floor is a literal 2: mono is excluded by product policy (no JTS
        // source or sink produces a one-channel ring), not by the layout — see
        // MAX_RING_CHANNELS.
        if !(2..=MAX_RING_CHANNELS).contains(&self.channels) {
            return Err(cfg_err(format!(
                "ring channels {} out of range 2..={MAX_RING_CHANNELS}",
                self.channels
            )));
        }
        if self.rate != 48_000 {
            return Err(cfg_err(format!(
                "ring rate {} unsupported (only 48000)",
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
        // Last: the derived size. Every input it multiplies has been bounded
        // above, so a failure here is a genuinely oversized slot rather than a
        // knock-on from a bad field.
        let slot_bytes = self.slot_bytes()?;
        if slot_bytes > MAX_SLOT_BYTES {
            return Err(cfg_err(format!(
                "ring slot {slot_bytes} bytes exceeds the {MAX_SLOT_BYTES}-byte cap \
                 (period_frames={} channels={} sample_format={})",
                self.period_frames, self.channels, self.sample_format
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

        // The accept-set envelope. These bound no offset, so no `offsetof`
        // assertion can catch a drift in them; pinning the literals here is
        // what makes widening either bound a deliberate, reviewed edit.
        assert_eq!(MAX_RING_CHANNELS, 8);
        assert_eq!(MAX_SLOT_BYTES, 65_536);

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
        assert_eq!(g.bytes_per_sample().unwrap(), 2);
        assert_eq!(g.samples_per_slot(), 256); // 128 frames * 2 ch
        assert_eq!(g.slot_bytes().unwrap(), 512); // 256 samples * 2 bytes
        assert_eq!(g.file_size().unwrap(), 128 + 2 * 512); // header + 2 slots = 1152
        g.validate_self().unwrap();
    }

    /// Byte-math golden table. Every expected value is computed BY HAND here,
    /// never by calling the functions under test, so a change to
    /// `bytes_per_sample` / `samples_per_slot` / `slot_bytes` / `file_size`
    /// fails against a fixed reference instead of against itself.
    ///
    /// The S16 / 6ch row is deliberate, and so is keeping wide S32 rows. Exactly
    /// two geometries in this table have `channels == bytes_per_sample` — S16/2
    /// (2 and 2) and S32/4 (4 and 4) — so a bug that swaps those two factors
    /// gives the right answer on both and is invisible to a table built only
    /// from them. Verified by mutating `slot_bytes` to multiply by `channels`
    /// instead of `bytes_per_sample`: S16/2 passed and S16/6 caught it
    /// (4608 != 1536), and S32/4 passes it too (512 * 4 == 2048 == expected).
    /// S16/6 is the only S16 row that breaks the coincidence; S32/2, S32/6 and
    /// S32/8 break it on the S32 side.
    #[test]
    fn golden_byte_math_table() {
        // (sample_format, channels, period_frames, n_slots)
        //   -> (bytes_per_sample, samples_per_slot, slot_bytes, file_size)
        let rows = [
            ((SAMPLE_FORMAT_S16LE, 2, 128, 2), (2, 256, 512, 1152)),
            ((SAMPLE_FORMAT_S16LE, 6, 128, 2), (2, 768, 1536, 3200)),
            ((SAMPLE_FORMAT_S32LE, 2, 128, 2), (4, 256, 1024, 2176)),
            ((SAMPLE_FORMAT_S32LE, 4, 128, 2), (4, 512, 2048, 4224)),
            ((SAMPLE_FORMAT_S32LE, 6, 128, 2), (4, 768, 3072, 6272)),
            ((SAMPLE_FORMAT_S32LE, 8, 128, 2), (4, 1024, 4096, 8320)),
        ];
        for ((sample_format, channels, period_frames, n_slots), (bps, samples, slot, file)) in rows
        {
            let g = Geometry {
                rate: 48_000,
                channels,
                sample_format,
                period_frames,
                n_slots,
            };
            let label =
                format!("fmt={sample_format} ch={channels} period={period_frames} slots={n_slots}");
            assert_eq!(
                g.bytes_per_sample().unwrap(),
                bps,
                "bytes_per_sample {label}"
            );
            assert_eq!(g.samples_per_slot(), samples, "samples_per_slot {label}");
            assert_eq!(g.slot_bytes().unwrap(), slot, "slot_bytes {label}");
            assert_eq!(g.file_size().unwrap(), file, "file_size {label}");
            g.validate_self()
                .unwrap_or_else(|e| panic!("{label} must be inside the accept-set: {e}"));
        }

        // MAX_SLOT_BYTES boundary. `slot_bytes` is always a multiple of
        // `channels * bytes_per_sample`, so the accept-set cannot express a
        // geometry exactly one BYTE over the cap; the finest step it can take is
        // 4 bytes (S16 stereo). Both pairs land exactly ON the cap and then take
        // their smallest possible step past it.
        for (label, sample_format, channels, period_frames, slot_bytes, accepted) in [
            (
                "S16 stereo on the cap",
                SAMPLE_FORMAT_S16LE,
                2u32,
                16_384u32,
                65_536usize,
                true,
            ),
            (
                "S16 stereo one 4-byte step over",
                SAMPLE_FORMAT_S16LE,
                2,
                16_385,
                65_540,
                false,
            ),
            (
                "S32 8ch on the cap",
                SAMPLE_FORMAT_S32LE,
                8,
                2_048,
                65_536,
                true,
            ),
            (
                "S32 8ch one 32-byte step over",
                SAMPLE_FORMAT_S32LE,
                8,
                2_049,
                65_568,
                false,
            ),
        ] {
            let g = Geometry {
                rate: 48_000,
                channels,
                sample_format,
                period_frames,
                n_slots: 2,
            };
            assert_eq!(g.slot_bytes().unwrap(), slot_bytes, "slot_bytes {label}");
            match (g.validate_self(), accepted) {
                (Ok(()), true) => {}
                (Err(error), false) => {
                    assert_eq!(error.kind(), io::ErrorKind::InvalidInput, "{label}");
                    assert!(
                        error.to_string().contains("exceeds the 65536-byte cap"),
                        "{label}: {error}"
                    );
                }
                (result, _) => panic!("{label}: expected accepted={accepted}, got {result:?}"),
            }
        }
    }

    #[test]
    fn unknown_sample_format_is_a_controlled_error() {
        let g = Geometry {
            rate: 48_000,
            channels: 2,
            sample_format: u32::MAX,
            period_frames: 128,
            n_slots: 2,
        };
        let error = g.bytes_per_sample().unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert!(error.to_string().contains("unsupported ring sample format"));
    }

    /// The accept-set boundary, one axis at a time.
    ///
    /// S32LE and 4 channels used to be REJECTED here and are now accepted: the
    /// ring wire carries S32 and up to [`MAX_RING_CHANNELS`] channels, so the
    /// old assertions asserted the opposite of the contract. The guard is not
    /// weakened by that inversion — the negatives below cover both edges of
    /// each widened axis (channels 0/1/9, an unknown format id) plus every
    /// unchanged axis.
    #[test]
    fn geometry_accept_set_boundary() {
        let base = Geometry {
            rate: 48_000,
            channels: 2,
            sample_format: SAMPLE_FORMAT_S16LE,
            period_frames: 128,
            n_slots: 2,
        };

        // --- Accepted: the widened axes ---
        assert!(Geometry {
            sample_format: SAMPLE_FORMAT_S32LE,
            ..base
        }
        .validate_self()
        .is_ok());
        assert!(Geometry {
            channels: 4,
            ..base
        }
        .validate_self()
        .is_ok());
        assert!(Geometry {
            channels: MAX_RING_CHANNELS,
            ..base
        }
        .validate_self()
        .is_ok());
        // The raised ceiling accepts the full 2..=16 slot range (regression for
        // the 4 -> 16 bump that gives camilla's playback buffer enough depth).
        assert!(Geometry { n_slots: 4, ..base }.validate_self().is_ok());
        assert!(Geometry {
            n_slots: MAX_N_SLOTS,
            ..base
        }
        .validate_self()
        .is_ok());

        // --- Rejected: outside every axis ---
        // Channels: below the policy floor (0 and mono) and above the ceiling.
        for channels in [0, 1, MAX_RING_CHANNELS + 1] {
            let error = match (Geometry { channels, ..base }).validate_self() {
                Ok(()) => panic!("channels {channels} must be rejected"),
                Err(error) => error,
            };
            assert_eq!(
                error.kind(),
                io::ErrorKind::InvalidInput,
                "channels {channels}"
            );
            assert!(
                error.to_string().contains("channels"),
                "channels {channels}: {error}"
            );
        }
        // An unknown format id, adjacent to the two real ones.
        let error = match (Geometry {
            sample_format: 3,
            ..base
        })
        .validate_self()
        {
            Ok(()) => panic!("sample_format 3 must be rejected"),
            Err(error) => error,
        };
        assert_eq!(error.kind(), io::ErrorKind::InvalidInput);
        assert!(error.to_string().contains("sample_format"), "{error}");
        // Unchanged axes: rate, period_frames, n_slots.
        assert!(Geometry {
            rate: 44_100,
            ..base
        }
        .validate_self()
        .is_err());
        assert!(Geometry {
            period_frames: 0,
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
    }
}
