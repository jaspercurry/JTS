// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Ring B content source: the SHM ping-pong ring reader half of Ring B — the
//! one central transport from CamillaDSP to the DAC (ADR-0100).
//!
//! CamillaDSP writes its post-DSP program into an n-slot SHM ping-pong ring
//! through a custom ALSA ioplug (`c/jts-ring-ioplug/`, the WRITER). This module
//! is the READER: outputd calls [`ShmRingSource::read_period`] once per DAC
//! period to try-consume exactly one slot, zero-filling on empty. It NEVER
//! blocks — the DAC blocking write is the pacer.
//!
//! This is an "optional content source, empty->silence, metrics into /state"
//! reader: there is no FIFO and no stale-drop heuristic — the ring is a bounded
//! n-slot queue by construction, so the only "drop" is the attach-time resync
//! `jasper_ring` already performs.
//!
//! Declared via `JASPER_OUTPUTD_CONTENT_BRIDGE`; undeclared resolves to the
//! ring, which is what the shipped unit leaves it at.

use std::io;

use anyhow::Result;
use jasper_ring::{
    Geometry, RingMetrics, RingReader, SlotRead, SAMPLE_FORMAT_S16LE, SAMPLE_FORMAT_S32LE,
};

use crate::types::{widen_period, ProgramSample, SampleFormat};

/// The wire an `S24_3Le` declaration would ask for and the ring cannot carry.
///
/// Refused park-class, at the same exit code every other declaration fault
/// takes: `JASPER_OUTPUTD_CONTENT_FORMAT`
/// parses all three widths of the crate's one format vocabulary, `S24_3LE`
/// among them, but neither the ring layout (`jasper_ring::Geometry` defines two
/// sample-format ids, S16LE and S32LE) nor this reader has a packed-24 path. A
/// hand-set value is the only way to reach this: the coupling reconciler
/// answers only `S16_LE` or `S32_LE` on this key.
const PACKED_RING_WIRE_UNSUPPORTED: &str =
    "outputd content bridge declares S24_3LE, which the SHM ring has no wire \
     format for; the ring must be S16_LE or S32_LE (S24_3LE is an output-edge \
     width only)";

/// The ring carries its samples LITTLE-endian by name (`S16LE` / `S32LE`), and
/// [`ShmRingSource::read_period`]'s wide path consumes a slot straight into the
/// program period's own bytes — an identity copy only where the host's native
/// integer byte order already is the wire's. Every JTS host is little-endian
/// (aarch64 and x86_64 Linux, and CI's x86_64); this makes a big-endian port a
/// compile error here rather than a silent byte-swap at the speaker.
// PANIC-AUDITED: const context: a big-endian target fails to build, so this never runs
const _: () = assert!(cfg!(target_endian = "little"), "the ring wire is LE");

/// Which of the ring's two wire widths this reader attached to, and therefore
/// which read path a period takes.
///
/// It is a resolved kind rather than a re-read of the format on every period:
/// the wire cannot change while a reader lives (attach validated it, and a
/// re-attach is a new process), so the branch is decided once at construction.
#[derive(Debug, Clone, Copy)]
enum WireKind {
    /// S16LE ring: consume into the i16 scratch, then widen onto the spine.
    S16,
    /// S32LE ring: consume straight into the spine's bytes.
    S32,
}

/// Map one declared [`SampleFormat`] onto the ring layout's sample-format id
/// and this reader's period path, or refuse the declaration.
///
/// Total over the format vocabulary on purpose — a `_ =>` arm would have
/// silently answered "S16" for a width the ring cannot carry, which is the one
/// outcome that must never happen quietly on a wire whose reader and writer
/// agree only by declaring the same tuple.
fn ring_wire(format: SampleFormat) -> io::Result<(u32, WireKind)> {
    match format {
        SampleFormat::S16Le => Ok((SAMPLE_FORMAT_S16LE, WireKind::S16)),
        SampleFormat::S32Le => Ok((SAMPLE_FORMAT_S32LE, WireKind::S32)),
        SampleFormat::S24_3Le => Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            PACKED_RING_WIRE_UNSUPPORTED,
        )),
    }
}

/// Reinterpret one program period as the little-endian wire bytes an `S32LE`
/// ring slot carries.
///
/// **Deliberately monomorphic in [`ProgramSample`]**, for the same reason
/// `main.rs`'s `i16_bytes` is monomorphic in `i16`: a type-adaptive helper here
/// would happily accept an `&mut [i16]` and hand [`RingReader::try_consume_slot_bytes`]
/// a view HALF the slot's length, which the byte path would then fill short —
/// silently, with every counter still reporting a filled slot. The signature is
/// the guard.
fn program_bytes_mut(samples: &mut [ProgramSample]) -> &mut [u8] {
    let bytes = std::mem::size_of_val(samples);
    // SAFETY: `ProgramSample` is `i32`, which has no padding and no invalid bit
    // patterns, so every byte of the slice is initialized and every byte the
    // ring writes back through this view leaves `samples` initialized. The
    // `&mut` borrow of `samples` is consumed for the lifetime of the returned
    // view, so no aliasing `[ProgramSample]` reference exists while it lives.
    // Length is taken from the slice itself, so the view covers exactly the
    // period and not a byte more. (The same construction `jasper_ring` uses for
    // its own i16-typed slot view.)
    unsafe { std::slice::from_raw_parts_mut(samples.as_mut_ptr() as *mut u8, bytes) }
}

/// The reader-side content source: owns a [`RingReader`] and serves one slot
/// per DAC period.
pub struct ShmRingSource {
    reader: RingReader,
    samples_per_slot: usize,
    kind: WireKind,
    /// The wire this reader is attached to. Declaration and ring header carry
    /// the same pair BECAUSE [`RingReader::create_or_attach`] validated every
    /// geometry field against the declaration and refused any other outcome —
    /// so this is what STATUS reports the ring lane as running, not a drift
    /// detector.
    format: SampleFormat,
    channels: u16,
    /// S16 staging: one period at the narrow wire's width, allocated ONCE at
    /// construction and empty on a wide ring.
    ///
    /// It did not disappear when the spine went wide; it moved here from
    /// `run_alsa`, next to the branch that is the only thing that can touch it.
    /// Zero bytes on an S32 ring matters for the same reason it mattered in
    /// `run_alsa`: outputd runs `mlockall`, so a period-sized buffer is
    /// resident forever on every box that carries it.
    s16_scratch: Vec<i16>,
}

impl ShmRingSource {
    /// Attach to (or create) the ring at `path` with the given geometry.
    ///
    /// The geometry is a per-box DECLARATION, not a crate property: the layout
    /// accept-set is S16LE or S32LE, 2..=`jasper_ring::MAX_RING_CHANNELS`
    /// channels and 48 kHz (`jasper_ring::Geometry::validate_self`), and what
    /// THIS constructor pins out of that set is the rate — 48 kHz, the only
    /// rate the JTS graph runs at. Format and channels come from the caller,
    /// which sources both from the same `JASPER_OUTPUTD_CONTENT_*` declaration
    /// the reconciler renders the ring's other three ends from.
    ///
    /// A geometry / version / size mismatch against an existing ring is a hard
    /// error — the caller maps it to a config-class startup failure (exit 78)
    /// so systemd parks instead of reboot-looping (see the module doc's
    /// fail-closed answer). The crate's field-by-field attach IS the
    /// negotiation; there is no widest-common-width step and no ranking, so a
    /// wire the writer declares differently on ANY field fails here rather than
    /// being reinterpreted.
    pub fn new(
        path: &str,
        period_frames: u32,
        channels: u16,
        format: SampleFormat,
        n_slots: u32,
    ) -> io::Result<Self> {
        let (sample_format, kind) = ring_wire(format)?;
        let geometry = Geometry {
            rate: 48_000,
            channels: u32::from(channels),
            sample_format,
            period_frames,
            n_slots,
        };
        let reader = RingReader::create_or_attach(path, geometry)?;
        let samples_per_slot = geometry.samples_per_slot();
        let s16_scratch = match kind {
            WireKind::S16 => vec![0i16; samples_per_slot],
            WireKind::S32 => Vec::new(),
        };
        Ok(Self {
            reader,
            samples_per_slot,
            kind,
            format,
            channels,
            s16_scratch,
        })
    }

    pub fn path(&self) -> &str {
        self.reader.path()
    }

    pub fn metrics(&self) -> RingMetrics {
        self.reader.metrics()
    }

    /// The attached ring's sample format — see the [`Self::format`] field.
    pub fn wire_format(&self) -> SampleFormat {
        self.format
    }

    /// The attached ring's channel count — see the [`Self::format`] field.
    pub fn channels(&self) -> u16 {
        self.channels
    }

    /// Try to consume one slot into the program period `out` (`out.len()` must
    /// be `period_frames * channels`). Slot available -> copies it and returns
    /// the frame count; ring empty -> zero-fills and returns 0. Never blocks,
    /// and a ring fault is never a runtime error (it degrades to silence +
    /// counters, never a crash — `StartLimitAction=reboot` discipline).
    ///
    /// The `Result` is the CALLER-contract fault only, and it is exactly the one
    /// the run loop already propagated when the widening lived at the call site:
    /// a staging length that does not match the period would emit a short or
    /// stale period, so it fails loud rather than playing it. That check is
    /// hoisted ABOVE the wire branch — one `bail!` covering both arms, in the
    /// same family as [`widen_period`]'s message — rather than a `debug_assert`
    /// plus two different release behaviours. CI builds this crate with
    /// `cargo test --release`, where a debug assertion is compiled out; on the
    /// wide arm the byte view is derived from `out` itself, so a mismatched
    /// destination there would have been silently wrong either way, with
    /// every counter still reporting a filled slot: the crate's
    /// `copy_slot_bytes` clamps to the shorter side, so an over-sized
    /// destination would be silently under-filled with a stale tail and an
    /// under-sized one would be fully filled from a truncated slot. Both
    /// directions are now refused by the guard above.
    ///
    /// Both wires land on the same i32 spine, and neither adds a conversion:
    /// - **S16LE** — consume into the narrow scratch, then widen by `<< 16`
    ///   through the shared [`widen_period`], exactly like every other S16
    ///   ingress.
    /// - **S32LE** — consume straight into the period's own bytes. An `S32LE`
    ///   slot is full-scale little-endian i32, which is exactly what
    ///   [`ProgramSample`] is (see its "left-justified" doc), so the copy is an
    ///   identity: no shift, no scale, nothing left to convert.
    pub fn read_period(&mut self, out: &mut [ProgramSample]) -> Result<usize> {
        if out.len() != self.samples_per_slot {
            anyhow::bail!(
                "outputd ring period is {} samples but the slot is {}; writing \
                 that would emit a short or stale period",
                out.len(),
                self.samples_per_slot
            );
        }
        let read = match self.kind {
            WireKind::S16 => {
                let read = self.reader.try_consume_slot(&mut self.s16_scratch);
                widen_period(&self.s16_scratch, out)?;
                read
            }
            WireKind::S32 => self.reader.try_consume_slot_bytes(program_bytes_mut(out)),
        };
        Ok(match read {
            SlotRead::Filled => self.reader.geometry().period_frames as usize,
            SlotRead::Empty => 0,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use jasper_ring::{PublishOutcome, RingWriter, TestRingWriter};
    use std::sync::atomic::{AtomicU64, Ordering};

    static SEQ: AtomicU64 = AtomicU64::new(0);

    fn tmp_path() -> String {
        let dir = std::env::temp_dir().join(format!(
            "outputd-shm-ring-{}-{}",
            std::process::id(),
            SEQ.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("content.ring").to_string_lossy().into_owned()
    }

    fn cleanup(path: &str) {
        let _ = std::fs::remove_file(path);
        if let Some(p) = std::path::Path::new(path).parent() {
            let _ = std::fs::remove_dir(p);
        }
    }

    /// The narrow wire, at the stereo shape the S16 regressions below share.
    fn narrow(path: &str) -> ShmRingSource {
        ShmRingSource::new(path, 128, 2, SampleFormat::S16Le, 2).unwrap()
    }

    /// The BYTE-oriented product writer, at any width.
    ///
    /// `TestRingWriter` cannot serve a wide ring: its `try_publish_slot`
    /// measures the payload in `i16`s and goes through the crate's S16-typed
    /// slot view, which is a slot-length mismatch (and trips the typed-view
    /// tripwire) on any format but S16LE. The product Ring A writer's byte
    /// entry point has no such restriction, so every test that needs a ring
    /// this crate's i16-typed twin cannot create OR publish to uses this one —
    /// including the S16 half of the mismatch pair below, so both directions
    /// are created the same way.
    fn byte_writer(path: &str, geometry: Geometry) -> RingWriter {
        RingWriter::create_or_attach(path, geometry).unwrap()
    }

    #[test]
    fn empty_ring_is_silence_with_startup_counter() {
        let path = tmp_path();
        let mut src = narrow(&path);
        let mut out = vec![5 as ProgramSample; 256];
        assert_eq!(src.read_period(&mut out).unwrap(), 0);
        assert!(out.iter().all(|&s| s == 0));
        assert_eq!(src.metrics().startup_empty_reads, 1);
        assert_eq!(src.metrics().empty_reads, 0);
        cleanup(&path);
    }

    #[test]
    fn consumes_a_published_slot() {
        let path = tmp_path();
        let mut src = narrow(&path);
        let mut writer = TestRingWriter::create_or_attach(&path, src.reader.geometry()).unwrap();
        let payload: Vec<i16> = (0..256).map(|i| i as i16).collect();
        assert!(writer.try_publish_slot(&payload));

        let mut out = vec![0 as ProgramSample; 256];
        assert_eq!(src.read_period(&mut out).unwrap(), 128);
        let widened: Vec<ProgramSample> = payload
            .iter()
            .map(|&s| (s as ProgramSample) << 16)
            .collect();
        assert_eq!(out, widened);
        assert_eq!(src.metrics().frames_read, 128);
        // Next period is empty again -> steady-state empty counter.
        assert_eq!(src.read_period(&mut out).unwrap(), 0);
        assert_eq!(src.metrics().empty_reads, 1);
        cleanup(&path);
    }

    #[test]
    fn occupancy_visible_in_metrics() {
        let path = tmp_path();
        let mut src = narrow(&path);
        let mut writer = TestRingWriter::create_or_attach(&path, src.reader.geometry()).unwrap();
        let payload = vec![1i16; 256];
        assert!(writer.try_publish_slot(&payload));
        assert!(writer.try_publish_slot(&payload));
        let mut out = vec![0 as ProgramSample; 256];
        src.read_period(&mut out).unwrap();
        assert_eq!(src.metrics().occupancy, 1); // 2 written, 1 read
        cleanup(&path);
    }

    #[test]
    fn geometry_mismatch_is_a_hard_error() {
        let path = tmp_path();
        // Writer creates a 128-frame ring; reader expects 256 -> fail loud.
        let g = Geometry {
            rate: 48_000,
            channels: 2,
            sample_format: SAMPLE_FORMAT_S16LE,
            period_frames: 128,
            n_slots: 2,
        };
        let _writer = TestRingWriter::create_or_attach(&path, g).unwrap();
        let err = match ShmRingSource::new(&path, 256, 2, SampleFormat::S16Le, 2) {
            Ok(_) => panic!("geometry mismatch must be a hard error"),
            Err(e) => e,
        };
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
        cleanup(&path);
    }

    /// The FORMAT axis of the attach negotiation, both directions.
    ///
    /// This is the axis ring v2 adds, and it is the one a coherence check that
    /// compares only slots + period cannot see: the two rings below differ from
    /// their reader on NOTHING except the wire width, so if the format were not
    /// compared field-by-field they would attach and the reader would
    /// reinterpret the writer's bytes at the wrong stride — 90 dB down one way,
    /// garbage the other. Both directions, because a one-way test passes with
    /// the comparison written as `>=`.
    #[test]
    fn a_format_mismatch_refuses_to_attach_in_either_direction() {
        for (ring_format, declared) in [
            (SAMPLE_FORMAT_S16LE, SampleFormat::S32Le),
            (SAMPLE_FORMAT_S32LE, SampleFormat::S16Le),
        ] {
            let path = tmp_path();
            let g = Geometry {
                rate: 48_000,
                channels: 2,
                sample_format: ring_format,
                period_frames: 128,
                n_slots: 2,
            };
            let _writer = byte_writer(&path, g);
            let err = match ShmRingSource::new(&path, 128, 2, declared, 2) {
                Ok(_) => panic!("format mismatch must be a hard error (ring={ring_format})"),
                Err(e) => e,
            };
            // The SAME error class the period/slot axes already take, which is
            // what `main`'s `ConfigClassError` context maps to exit 78 — so the
            // new axis parks the unit rather than reboot-looping it, with no
            // new mapping needed.
            assert_eq!(err.kind(), io::ErrorKind::InvalidData, "ring={ring_format}");
            cleanup(&path);
        }
    }

    /// A packed-24 declaration is refused BEFORE the filesystem is touched.
    #[test]
    fn a_packed_24_declaration_is_refused_without_creating_a_ring() {
        let path = tmp_path();
        let err = match ShmRingSource::new(&path, 128, 2, SampleFormat::S24_3Le, 2) {
            Ok(_) => panic!("S24_3LE must not resolve to a ring wire"),
            Err(e) => e,
        };
        assert_eq!(err.kind(), io::ErrorKind::InvalidInput);
        assert!(err.to_string().contains("S24_3LE"), "{err}");
        assert!(
            !std::path::Path::new(&path).exists(),
            "a refused declaration must not have created {path}"
        );
        cleanup(&path);
    }

    /// GOLDEN, narrow wire: a known slot -> known program samples.
    ///
    /// The regression that carries R4's inertness claim: on the narrow wire a
    /// slot must reach the spine exactly as it did when `run_alsa` owned the
    /// widening — left-justified by `<< 16`, both rails intact, sign
    /// preserved. It pins the SHIFT, not the wire's byte order:
    /// writer and reader share the crate's S16-typed slot view here, so a
    /// swap would be symmetric and invisible to this test (the wide golden
    /// below spells its bytes out for exactly that reason).
    #[test]
    fn golden_s16_slot_reaches_the_spine_left_justified() {
        let path = tmp_path();
        let mut src = ShmRingSource::new(&path, 4, 2, SampleFormat::S16Le, 2).unwrap();
        let mut writer = TestRingWriter::create_or_attach(&path, src.reader.geometry()).unwrap();
        // 8 samples = 4 frames x 2 ch: both rails, both signs, and three
        // asymmetric values whose widened form a truncation or a half-shift
        // would move.
        let payload: Vec<i16> = vec![i16::MAX, i16::MIN, 1, -1, 0, 0x0100, 0x3412, 0x00FF];
        assert!(writer.try_publish_slot(&payload));

        let mut out = vec![0 as ProgramSample; 8];
        assert_eq!(src.read_period(&mut out).unwrap(), 4);
        assert_eq!(
            out,
            vec![
                0x7FFF_0000u32 as ProgramSample,
                i32::MIN,
                0x0001_0000,
                0xFFFF_0000u32 as ProgramSample,
                0x0000_0000,
                0x0100_0000,
                0x3412_0000,
                0x00FF_0000,
            ]
        );
        cleanup(&path);
    }

    /// GOLDEN, wide wire: known ring bytes -> the SAME program samples, with no
    /// shift and no conversion.
    ///
    /// Paired with the narrow golden above on the same numeric values, so the
    /// two together state the whole width story: the S16 wire's samples arrive
    /// left-justified by `<< 16`, and the S32 wire's arrive as written. A wide
    /// read that accidentally kept the widen (or narrowed first) fails here on
    /// every element.
    #[test]
    fn golden_s32_slot_bytes_reach_the_spine_unshifted() {
        let path = tmp_path();
        let mut src = ShmRingSource::new(&path, 4, 2, SampleFormat::S32Le, 2).unwrap();
        let geometry = src.reader.geometry();
        let mut writer = byte_writer(&path, geometry);
        let period: Vec<ProgramSample> = vec![
            0x7FFF_0000u32 as ProgramSample,
            i32::MIN,
            0x0001_0000,
            0xFFFF_0000u32 as ProgramSample,
            0x0000_0000,
            0x0100_0000,
            0x3412_0000,
            0x00FF_0000,
        ];
        // The WIRE, spelled as literal little-endian bytes rather than derived
        // from `period` — a derivation would agree with a byte-swapped reader
        // for the same reason the reader is wrong.
        let wire: Vec<u8> = vec![
            0x00, 0x00, 0xFF, 0x7F, // 0x7FFF_0000
            0x00, 0x00, 0x00, 0x80, // i32::MIN
            0x00, 0x00, 0x01, 0x00, // 0x0001_0000
            0x00, 0x00, 0xFF, 0xFF, // 0xFFFF_0000
            0x00, 0x00, 0x00, 0x00, // 0
            0x00, 0x00, 0x00, 0x01, // 0x0100_0000
            0x00, 0x00, 0x12, 0x34, // 0x3412_0000
            0x00, 0x00, 0xFF, 0x00, // 0x00FF_0000
        ];
        assert_eq!(wire.len(), geometry.slot_bytes().unwrap());
        assert_eq!(writer.publish_bytes(&wire), PublishOutcome::Published);

        let mut out = vec![0 as ProgramSample; 8];
        assert_eq!(src.read_period(&mut out).unwrap(), 4);
        assert_eq!(out, period);
        cleanup(&path);
    }

    /// A destination that is not one slot is an ERROR on BOTH wires — in
    /// release, where a debug assertion is not there to catch it.
    ///
    /// The wide arm is why this is a `bail!` and not a `debug_assert`: its byte
    /// view is derived from `out`, so a short destination would be quietly
    /// under-filled (`copy_slot_bytes` clamps to the destination) and the period
    /// written short at the speaker, with `frames_read` still counting a full
    /// slot. Both directions of wrong on each wire — short and long — since a
    /// one-sided check passes written as `<`.
    #[test]
    fn a_destination_that_is_not_one_slot_is_an_error_on_either_wire() {
        for (format, label) in [(SampleFormat::S16Le, "S16"), (SampleFormat::S32Le, "S32")] {
            let path = tmp_path();
            // 4 frames x 2 ch = 8 samples per slot.
            let mut src = ShmRingSource::new(&path, 4, 2, format, 2).unwrap();

            for wrong in [7usize, 9] {
                let mut out = vec![0 as ProgramSample; wrong];
                let err = match src.read_period(&mut out) {
                    Ok(_) => panic!("{label} wire must refuse a {wrong}-sample destination"),
                    Err(e) => e,
                };
                let text = err.to_string();
                assert!(
                    text.contains(&format!("ring period is {wrong} samples")),
                    "{label}: {text}"
                );
                assert!(text.contains("the slot is 8"), "{label}: {text}");
            }

            // ...and the right length still reads (so the guard is a boundary,
            // not a blanket refusal).
            let mut out = vec![0 as ProgramSample; 8];
            assert_eq!(src.read_period(&mut out).unwrap(), 0, "{label}");
            cleanup(&path);
        }
    }

    /// A wide ring pays no narrow-staging residency, and a narrow one still
    /// stages exactly one period.
    ///
    /// The residency claim is the reason the scratch moved into this struct
    /// instead of staying a `run_alsa` local, and outputd `mlockall`s — so an
    /// allocation that quietly returned on the wide path would be resident
    /// forever on every wide box. Steady state allocates nothing on either
    /// wire: both buffers below are sized once, here.
    #[test]
    fn the_narrow_staging_is_one_period_and_a_wide_ring_carries_none() {
        let narrow_path = tmp_path();
        let narrow = ShmRingSource::new(&narrow_path, 128, 2, SampleFormat::S16Le, 2).unwrap();
        assert_eq!(narrow.s16_scratch.len(), 256);
        assert_eq!(narrow.wire_format(), SampleFormat::S16Le);
        assert_eq!(narrow.channels(), 2);
        cleanup(&narrow_path);

        let wide_path = tmp_path();
        let wide = ShmRingSource::new(&wide_path, 128, 6, SampleFormat::S32Le, 2).unwrap();
        assert!(wide.s16_scratch.is_empty());
        assert_eq!(wide.s16_scratch.capacity(), 0);
        assert_eq!(wide.wire_format(), SampleFormat::S32Le);
        assert_eq!(wide.channels(), 6);
        cleanup(&wide_path);
    }
}
