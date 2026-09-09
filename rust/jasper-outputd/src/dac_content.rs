// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! The DAC-content return lane — a grouping LEADER's round-trip ingress.
//!
//! On a grouping LEADER the music the DAC plays must come back OUT of the
//! sync engine, so the leader is sample-locked with its followers: the
//! leader's own localhost snapclient re-plays the bond's shared stereo, and
//! this module reads it one DAC period at a time. Without it a leader plays
//! its program ahead of every follower.
//!
//! ## One transport
//!
//! snapclient writes the SHM ring `jasper::multiroom::dac_content_ring`
//! names, through the C ioplug, and this module attaches as its reader
//! (ADR-0100). Undeclared ⇒ this module does not run at all: no attach, no
//! syscalls, no per-period work.
//!
//! ## Starvation is SILENCE (owner ruling D4)
//!
//! A period the lane cannot fill is emitted as silence (one journal line
//! per process); there is no last-good replay and
//! no fallback source. The lane IS the content source on an armed box, so
//! there is nothing to fall back TO. Health is self-reported on the STATUS
//! surface (`DacContentMetrics` → the `dac_content` block) — daemon truth,
//! never a Python mirror of env intent.
//!
//! ## Timing
//!
//! All I/O is non-blocking and happens on the DAC loop thread; the DAC write
//! remains the sole pacer (inv-1). Worst case per period is one try-consume
//! on the ring — never a blocking wait on the producer.
//!
//! ## Channel pick
//!
//! The lane carries the bond's SHARED stereo program (L = leader-seat
//! corrected, R = follower-seat corrected). A stereo-pair leader plays
//! only ITS channel, and — unlike a follower, whose snapclient plays
//! through an ALSA `ttable` plug — this lane has no ALSA hop to do the
//! drop. `ChannelPick` therefore mirrors the channel-split vocabulary:
//! `left`/`right` duplicate that program channel onto both DAC channels;
//! `mono` averages (the clip-safe L+R sum at −6.02 dB, matching
//! `jasper.camilla_emit.MONO_SUM_GAIN_DB`); `stereo` is passthrough.

use std::io;

use anyhow::Result;

use crate::shm_ring_source::ShmRingSource;
use crate::types::{ProgramSample, SampleFormat};

/// Which channel of the shared stereo program this speaker plays.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ChannelPick {
    /// Passthrough — both program channels as-is (solo / lab use).
    Stereo,
    /// Program channel 0 duplicated to both DAC channels (a LEFT member).
    Left,
    /// Program channel 1 duplicated to both DAC channels (a RIGHT member).
    Right,
    /// Clip-safe average of both program channels (a mono member).
    Mono,
}

impl ChannelPick {
    /// Stable wire name for STATUS/logs — the `BackendMode::as_str`
    /// precedent (never a Debug-derived string, which silently changes
    /// if a variant is renamed).
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Stereo => "stereo",
            Self::Left => "left",
            Self::Right => "right",
            Self::Mono => "mono",
        }
    }

    /// Parse the channel-split vocabulary. Unknown values are a
    /// configuration error — fail loud at startup, never guess a channel
    /// (playing the WRONG channel is the silent failure class
    /// `check_grouping_channel_pick` exists for).
    pub fn parse(raw: &str) -> Result<Self, String> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "" | "stereo" => Ok(Self::Stereo),
            "left" => Ok(Self::Left),
            "right" => Ok(Self::Right),
            "mono" => Ok(Self::Mono),
            other => Err(format!(
                "JASPER_OUTPUTD_DAC_CONTENT_CHANNEL must be one of \
                 stereo|left|right|mono, got {other:?}"
            )),
        }
    }

    /// Clip-safe mono average of one interleaved-stereo frame: (L+R)/2 in
    /// i64 then narrow to the spine width — the same −6.02 dB sum `Mono` uses,
    /// so a full-scale-correlated pair stays full scale with no overflow.
    ///
    /// i64, not i32: two i32 samples sum past the i32 rail, and a wrap there
    /// would turn a loud correlated pair into full-scale opposite polarity.
    #[inline]
    fn mono_avg(frame: &[ProgramSample]) -> ProgramSample {
        (((frame[0] as i64) + (frame[1] as i64)) / 2) as ProgramSample
    }

    /// Apply the pick in place to one interleaved-stereo period.
    fn apply(self, period: &mut [ProgramSample]) {
        match self {
            Self::Stereo => {}
            Self::Left => {
                for frame in period.chunks_exact_mut(2) {
                    frame[1] = frame[0];
                }
            }
            Self::Right => {
                for frame in period.chunks_exact_mut(2) {
                    frame[0] = frame[1];
                }
            }
            Self::Mono => {
                for frame in period.chunks_exact_mut(2) {
                    let avg = Self::mono_avg(frame);
                    frame[0] = avg;
                    frame[1] = avg;
                }
            }
        }
    }
}

/// Counters + gauges for the STATUS `dac_content` block. Plain data —
/// `OutputdState::mark_dac_content` copies it into atomics.
#[derive(Debug, Clone, Copy)]
pub struct DacContentMetrics {
    /// True when the lane filled the LAST period with real audio.
    ///
    /// **The name is the wire's, and it is load-bearing.** Python reads
    /// `dac_content.serving_fifo` for the pair-lock verdict
    /// (`jasper.multiroom.state`, `jasper.control.grouping_supervisor`), where
    /// it means "bytes are flowing" and explicitly NOT "sample lock proven".
    /// The field keeps this name because renaming it would break those
    /// readers.
    ///
    /// A per-period fact, not a damped mode: under D4 there is no mode to be
    /// in, so a poll landing on a starved period honestly reports false.
    pub serving_fifo: bool,
    /// Periods the lane filled with real audio.
    pub fifo_periods: u64,
}

/// The DAC-content source. One instance per daemon, owned by the DAC loop;
/// all I/O non-blocking on that thread.
///
/// The ring is read through the SAME reader the central content hop uses
/// ([`ShmRingSource`]) rather than a second attach/widen/counter
/// implementation: it already is "attach a declared geometry, try-consume one
/// slot per DAC period, zero-fill on empty, never block".
pub struct DacContentSource {
    reader: ShmRingSource,
    channel: ChannelPick,
    served_periods: u64,
    last_period_served: bool,
    logged_first_starvation: bool,
}

impl DacContentSource {
    /// Attach (or create) the return ring at `path` at the lane's pinned
    /// geometry.
    ///
    /// The geometry is NOT negotiated here: the wire is S16LE stereo by
    /// contract (snapclient decodes to the snapserver-pinned `48000:16:2`)
    /// and the slot is one DAC period, so the only free field is `n_slots`,
    /// which the caller passes from the same constant the conf.d block and
    /// `jasper.multiroom.dac_content_ring` spell. `RingReader::create_or_attach`
    /// compares every field against the live header and refuses a mismatch
    /// with `InvalidData`, so a writer that disagrees on ANY of them parks
    /// this daemon instead of being reinterpreted at the wrong stride.
    pub fn ring(
        path: &str,
        channel: ChannelPick,
        period_frames: u32,
        n_slots: u32,
    ) -> io::Result<Self> {
        let reader = ShmRingSource::new(path, period_frames, 2, SampleFormat::S16Le, n_slots)?;
        Ok(Self {
            reader,
            channel,
            served_periods: 0,
            last_period_served: false,
            logged_first_starvation: false,
        })
    }

    /// Fill `out` with this lane's period. Never blocks.
    ///
    /// An armed lane IS the content source, so it always answers: real audio
    /// when the producer kept up, SILENCE when it did not (D4 — no replay, no
    /// fallback, a counter instead). The caller therefore has no "not served"
    /// branch to take.
    ///
    /// The `Err` is the ring's slot-length contract — a destination that is
    /// not exactly one slot would emit a short or stale period, so it fails
    /// loud rather than playing it. Publish [`Self::metrics`] before
    /// propagating it, as the central ring's call site does, so `/state`'s
    /// last sample stays honest through a fatal period.
    pub fn fill_period(&mut self, out: &mut [ProgramSample]) -> Result<()> {
        // `read_period` zero-fills on an empty ring, so `out` is left complete
        // either way.
        let served = self.reader.read_period(out)? > 0;
        self.last_period_served = served;
        if served {
            self.served_periods += 1;
        } else if !self.logged_first_starvation {
            // Once per process, so a chronically dry producer cannot spam
            // the journal.
            eprintln!(
                "event=outputd.dac_content.starved transport=ring action=emit_silence \
                 detail=D4: the return lane has no fallback source"
            );
            self.logged_first_starvation = true;
        }
        self.channel.apply(out);
        Ok(())
    }

    pub fn metrics(&self) -> DacContentMetrics {
        DacContentMetrics {
            serving_fifo: self.last_period_served,
            fifo_periods: self.served_periods,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    /// One S16 wire sample at the program spine's scale — the ring's S16 wire
    /// is an ingress into the spine.
    fn w(sample: i16) -> ProgramSample {
        jasper_resampler::widen_i16_to_i32(sample)
    }

    /// A whole period of wire samples at the spine's scale.
    fn wv(samples: &[i16]) -> Vec<ProgramSample> {
        samples.iter().copied().map(w).collect()
    }

    // ---------- pure: ChannelPick ----------

    #[test]
    fn channel_pick_parses_the_channel_split_vocabulary() {
        assert_eq!(ChannelPick::parse(""), Ok(ChannelPick::Stereo));
        assert_eq!(ChannelPick::parse("stereo"), Ok(ChannelPick::Stereo));
        assert_eq!(ChannelPick::parse("LEFT"), Ok(ChannelPick::Left));
        assert_eq!(ChannelPick::parse("right"), Ok(ChannelPick::Right));
        assert_eq!(ChannelPick::parse("mono"), Ok(ChannelPick::Mono));
        assert!(ChannelPick::parse("sub").is_err());
        assert!(ChannelPick::parse("both").is_err());
    }

    #[test]
    fn channel_pick_left_right_duplicate_and_mono_averages_clip_safe() {
        let mut p = wv(&[100, -200, 1000, 2000]);
        ChannelPick::Left.apply(&mut p);
        assert_eq!(p, wv(&[100, 100, 1000, 1000]));

        let mut p = wv(&[100, -200, 1000, 2000]);
        ChannelPick::Right.apply(&mut p);
        assert_eq!(p, wv(&[-200, -200, 2000, 2000]));

        let mut p = wv(&[100, -200, i16::MAX, i16::MAX]);
        ChannelPick::Mono.apply(&mut p);
        assert_eq!(p[0], w(-50));
        assert_eq!(p[1], w(-50));
        // Full-scale L==R averages back to full scale, no overflow.
        assert_eq!(p[2], w(i16::MAX));
        assert_eq!(p[3], w(i16::MAX));

        let mut p = wv(&[1, 2, 3, 4]);
        ChannelPick::Stereo.apply(&mut p);
        assert_eq!(p, wv(&[1, 2, 3, 4]));
    }

    #[test]
    fn mono_average_cannot_overflow_at_the_spine_rails() {
        // The i64 accumulator's reason to exist: two i32 samples sum past the
        // i32 rail. In i32 this wraps — a correlated full-scale pair would come
        // out full-scale OPPOSITE polarity, the loudest defect a mono fold can
        // produce. The old i16 version had the same argument one width down.
        let mut p = [ProgramSample::MAX, ProgramSample::MAX];
        ChannelPick::Mono.apply(&mut p);
        assert_eq!(p, [ProgramSample::MAX, ProgramSample::MAX]);

        let mut p = [ProgramSample::MIN, ProgramSample::MIN];
        ChannelPick::Mono.apply(&mut p);
        assert_eq!(p, [ProgramSample::MIN, ProgramSample::MIN]);

        // And the -6.02 dB sum of an anti-correlated full-scale pair is silence,
        // not a wrap to a rail.
        let mut p = [ProgramSample::MAX, ProgramSample::MIN];
        ChannelPick::Mono.apply(&mut p);
        assert_eq!(p, [0, 0]);
    }

    // ---------- the ring transport ----------

    use jasper_ring::{Geometry, TestRingWriter, SAMPLE_FORMAT_S16LE, SAMPLE_FORMAT_S32LE};

    /// 4-frame periods keep the byte math tiny: 16 bytes per period.
    const TEST_PERIOD_FRAMES: u32 = 4;

    fn temp_ring_path(tag: &str) -> std::path::PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let dir = std::env::temp_dir().join(format!(
            "jts-dac-content-ring-{tag}-{}-{nonce}",
            std::process::id()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir.join("dac-content.ring")
    }

    struct TempRing {
        path: std::path::PathBuf,
    }

    impl TempRing {
        fn create(tag: &str) -> Self {
            Self {
                path: temp_ring_path(tag),
            }
        }

        fn path_str(&self) -> &str {
            self.path.to_str().unwrap()
        }
    }

    impl Drop for TempRing {
        fn drop(&mut self) {
            let _ = std::fs::remove_file(&self.path);
            if let Some(p) = self.path.parent() {
                let _ = std::fs::remove_dir(p);
            }
        }
    }

    /// The lane's wire at test scale: S16LE stereo, `TEST_PERIOD_FRAMES` slots.
    fn ring_geometry(period_frames: u32, n_slots: u32) -> Geometry {
        Geometry {
            rate: jasper_ring::RATE_HZ,
            channels: 2,
            sample_format: SAMPLE_FORMAT_S16LE,
            period_frames,
            n_slots,
        }
    }

    fn ring_source(ring: &TempRing, channel: ChannelPick) -> DacContentSource {
        DacContentSource::ring(ring.path_str(), channel, TEST_PERIOD_FRAMES, 2).unwrap()
    }

    #[test]
    fn ring_consumes_exactly_one_slot_per_period() {
        let ring = TempRing::create("one-slot");
        let mut src = ring_source(&ring, ChannelPick::Stereo);
        let mut writer =
            TestRingWriter::create_or_attach(ring.path_str(), ring_geometry(TEST_PERIOD_FRAMES, 2))
                .unwrap();

        // Two slots published; each period must take exactly ONE of them.
        assert!(writer.try_publish_slot(&[1i16; 8]));
        assert!(writer.try_publish_slot(&[2i16; 8]));

        let mut out = vec![0 as ProgramSample; 8];
        src.fill_period(&mut out).unwrap();
        assert_eq!(out, vec![w(1); 8], "first period must take the first slot");
        src.fill_period(&mut out).unwrap();
        assert_eq!(
            out,
            vec![w(2); 8],
            "second period must take the second slot"
        );

        let m = src.metrics();
        assert_eq!(m.fifo_periods, 2);
        assert!(m.serving_fifo);
    }

    #[test]
    fn ring_starvation_is_silence_and_counts() {
        let ring = TempRing::create("starve");
        let mut src = ring_source(&ring, ChannelPick::Stereo);
        let mut writer =
            TestRingWriter::create_or_attach(ring.path_str(), ring_geometry(TEST_PERIOD_FRAMES, 2))
                .unwrap();
        assert!(writer.try_publish_slot(&[i16::MAX; 8]));

        let mut out = vec![0 as ProgramSample; 8];
        src.fill_period(&mut out).unwrap();
        assert_eq!(out, vec![w(i16::MAX); 8]);
        assert!(src.metrics().serving_fifo);

        // Ring now empty: silence, a counter, and NO replay of the loud
        // period just served (D4 — the whole point of the ruling).
        for i in 1..=3 {
            src.fill_period(&mut out).unwrap();
            assert_eq!(
                out,
                vec![0 as ProgramSample; 8],
                "period {i} must be silent"
            );
            let m = src.metrics();
            assert!(!m.serving_fifo);
            assert_eq!(m.fifo_periods, 1);
        }
    }

    /// A geometry the ring cannot serve is refused at construction, typed —
    /// the class `main` maps to a config-class park (exit 78) rather than a
    /// restart loop.
    #[test]
    fn ring_geometry_mismatch_is_refused_typed() {
        // Every axis the writer can disagree on, one at a time, against a
        // reader that declares S16 / 2ch / TEST_PERIOD_FRAMES / 2 slots.
        let cases: [(&str, Geometry); 3] = [
            ("period", ring_geometry(TEST_PERIOD_FRAMES * 2, 2)),
            ("slots", ring_geometry(TEST_PERIOD_FRAMES, 4)),
            (
                "format",
                Geometry {
                    sample_format: SAMPLE_FORMAT_S32LE,
                    ..ring_geometry(TEST_PERIOD_FRAMES, 2)
                },
            ),
        ];
        for (label, written) in cases {
            let ring = TempRing::create(&format!("mismatch-{label}"));
            let _writer =
                jasper_ring::RingWriter::create_or_attach(ring.path_str(), written).unwrap();
            let err = match DacContentSource::ring(
                ring.path_str(),
                ChannelPick::Stereo,
                TEST_PERIOD_FRAMES,
                2,
            ) {
                Ok(_) => panic!("{label} mismatch must be refused"),
                Err(e) => e,
            };
            assert_eq!(err.kind(), io::ErrorKind::InvalidData, "{label}");
        }
    }

    /// The pick reaches the DAC through the ring: what comes out of a served
    /// period is the widened wire with the pick applied, for every pick. A ring
    /// arm that forgot the pick, or applied it at a different point in the
    /// chain, differs here on the first frame.
    #[test]
    fn the_pick_reaches_the_dac_through_the_ring() {
        let wire: [i16; 8] = [100, -200, 3000, -4000, i16::MAX, i16::MIN, 0, 7];
        for pick in [
            ChannelPick::Stereo,
            ChannelPick::Left,
            ChannelPick::Right,
            ChannelPick::Mono,
        ] {
            let ring = TempRing::create(&format!("pick-{}", pick.as_str()));
            let mut src = ring_source(&ring, pick);
            let mut writer = TestRingWriter::create_or_attach(
                ring.path_str(),
                ring_geometry(TEST_PERIOD_FRAMES, 2),
            )
            .unwrap();
            assert!(writer.try_publish_slot(&wire));

            let mut out = vec![0 as ProgramSample; 8];
            src.fill_period(&mut out).unwrap();
            assert!(src.metrics().serving_fifo, "{}", pick.as_str());

            let mut expected = wv(&wire);
            pick.apply(&mut expected);
            assert_eq!(out, expected, "pick {}", pick.as_str());
        }
    }

    /// A starved period REPLACES whatever the caller left in the buffer:
    /// the lane's silence is exactly zeros, which is the D4 half that says an
    /// outage is silence rather than a replay.
    #[test]
    fn a_starved_period_replaces_the_callers_buffer_with_silence() {
        let ring = TempRing::create("starved-silence");
        let mut src = ring_source(&ring, ChannelPick::Stereo);
        let mut out = vec![w(12_345); (TEST_PERIOD_FRAMES as usize) * 2];
        src.fill_period(&mut out).unwrap();
        assert_eq!(out, vec![0 as ProgramSample; 8]);
        let m = src.metrics();
        assert!(!m.serving_fifo);
        assert_eq!(m.fifo_periods, 0);
    }
}
