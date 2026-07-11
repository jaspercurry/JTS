// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! Ring B content source: the SHM ping-pong ring reader half of Ring B.
//! Shipped default on eligible stereo topologies (P4 LANDED — see
//! docs/HANDOFF-audio-graph-consolidation.md); off elsewhere by resolved
//! policy.
//!
//! CamillaDSP writes its post-DSP S16LE stereo program into a 2-slot SHM
//! ping-pong ring through a custom ALSA ioplug (`c/jts-ring-ioplug/`, the
//! WRITER). This module is the READER: outputd calls [`ShmRingSource::read_period`]
//! once per DAC period to try-consume exactly one slot, zero-filling on empty.
//! It NEVER blocks — the DAC blocking write is the pacer.
//!
//! This is an "optional content source, empty->silence, metrics into /state"
//! reader: there is no FIFO and no stale-drop heuristic — the ring is a bounded
//! 2-slot queue by construction, so the only "drop" is the attach-time resync
//! `jasper_ring` already performs.
//!
//! Flag-gated: only constructed when `JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring`
//! (the coupling reconciler resolves this by default on eligible stereo
//! topologies; `None` on other topologies leaves the DAC loop
//! byte-identical to the pre-ring behavior).

use std::io;

use jasper_ring::{Geometry, RingMetrics, RingReader, SlotRead, SAMPLE_FORMAT_S16LE};

/// The reader-side content source: owns a [`RingReader`] and serves one slot
/// per DAC period.
pub struct ShmRingSource {
    reader: RingReader,
    samples_per_slot: usize,
}

impl ShmRingSource {
    /// Attach to (or create) the ring at `path` with the given geometry
    /// (S16LE / stereo / 48 kHz enforced by [`Geometry::validate_self`]).
    ///
    /// A geometry / version / size mismatch against an existing ring is a hard
    /// error — the caller maps it to a config-class startup failure (exit 78)
    /// so systemd parks instead of reboot-looping (see the module doc's
    /// fail-closed answer).
    pub fn new(path: &str, period_frames: u32, channels: u16, n_slots: u32) -> io::Result<Self> {
        let geometry = Geometry {
            rate: 48_000,
            channels: u32::from(channels),
            sample_format: SAMPLE_FORMAT_S16LE,
            period_frames,
            n_slots,
        };
        let reader = RingReader::create_or_attach(path, geometry)?;
        let samples_per_slot = geometry.samples_per_slot();
        Ok(Self {
            reader,
            samples_per_slot,
        })
    }

    pub fn path(&self) -> &str {
        self.reader.path()
    }

    pub fn metrics(&self) -> RingMetrics {
        self.reader.metrics()
    }

    /// Try to consume one slot into `out` (`out.len()` must be
    /// `period_frames * channels`). Slot available -> copies it and returns the
    /// frame count; ring empty -> zero-fills and returns 0. Never blocks, never
    /// errors at runtime (a runtime ring fault degrades to silence + counters,
    /// never a crash — `StartLimitAction=reboot` discipline).
    pub fn read_period(&mut self, out: &mut [i16]) -> usize {
        debug_assert_eq!(out.len(), self.samples_per_slot);
        match self.reader.try_consume_slot(out) {
            SlotRead::Filled => self.reader.geometry().period_frames as usize,
            SlotRead::Empty => 0,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use jasper_ring::TestRingWriter;
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

    #[test]
    fn empty_ring_is_silence_with_startup_counter() {
        let path = tmp_path();
        let mut src = ShmRingSource::new(&path, 128, 2, 2).unwrap();
        let mut out = vec![5i16; 256];
        assert_eq!(src.read_period(&mut out), 0);
        assert!(out.iter().all(|&s| s == 0));
        assert_eq!(src.metrics().startup_empty_reads, 1);
        assert_eq!(src.metrics().empty_reads, 0);
        cleanup(&path);
    }

    #[test]
    fn consumes_a_published_slot() {
        let path = tmp_path();
        let mut src = ShmRingSource::new(&path, 128, 2, 2).unwrap();
        let mut writer = TestRingWriter::create_or_attach(&path, src.reader.geometry()).unwrap();
        let payload: Vec<i16> = (0..256).map(|i| i as i16).collect();
        assert!(writer.try_publish_slot(&payload));

        let mut out = vec![0i16; 256];
        assert_eq!(src.read_period(&mut out), 128);
        assert_eq!(out, payload);
        assert_eq!(src.metrics().frames_read, 128);
        // Next period is empty again -> steady-state empty counter.
        assert_eq!(src.read_period(&mut out), 0);
        assert_eq!(src.metrics().empty_reads, 1);
        cleanup(&path);
    }

    #[test]
    fn occupancy_visible_in_metrics() {
        let path = tmp_path();
        let mut src = ShmRingSource::new(&path, 128, 2, 2).unwrap();
        let mut writer = TestRingWriter::create_or_attach(&path, src.reader.geometry()).unwrap();
        let payload = vec![1i16; 256];
        assert!(writer.try_publish_slot(&payload));
        assert!(writer.try_publish_slot(&payload));
        let mut out = vec![0i16; 256];
        src.read_period(&mut out);
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
        let err = match ShmRingSource::new(&path, 256, 2, 2) {
            Ok(_) => panic!("geometry mismatch must be a hard error"),
            Err(e) => e,
        };
        assert_eq!(err.kind(), io::ErrorKind::InvalidData);
        cleanup(&path);
    }
}
