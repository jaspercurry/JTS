//! Fan-in assistant/cue playout ledger — the per-segment accounting
//! behind the `FLUSH_SYNC` barge-in acknowledgement.
//!
//! ## Why this lives here (the design fork)
//!
//! `jasper-outputd` owns a DAC-clock-true `PlayoutLedger` (it drains
//! against the real `snd_pcm_delay`). But in the SOLO production topology
//! the assistant path is
//! `voice -> /run/jasper-fanin/tts.sock -> jasper-fanin (mix, PRE-CamillaDSP)
//!  -> jasper-camilla -> jasper-outputd -> DAC`,
//! so `jasper-outputd` never sees fan-in's TTS as a distinct segment — it
//! only reads the already-summed content lane and has no TTS bridge/ledger
//! instantiated unless a bonded multiroom member routes TTS straight to it.
//! The playout ledger for the solo TTS path therefore has to live where the
//! TTS is actually mixed: here. (Per `jasper-tts-protocol`'s own contract,
//! flush-ack summaries and ledgers are deliberately per-daemon — only the
//! wire vocabulary and loudness policy are shared.)
//!
//! ## The honest drain point
//!
//! fan-in sits before CamillaDSP and cannot see the DAC clock, so the most
//! downstream point it can observe is its own MIX-COMMIT: a frame counts as
//! "played" the instant [`crate::tts::TtsMixer::mix_period`] pops it into
//! the program sum bound for snd-aloop. That pop is paced by fan-in's
//! blocking snd-aloop write, which is itself pull-driven by the DAC at the
//! bottom of the chain, so the count is a real, DAC-rate-paced playout
//! measure — NOT a queued-frame estimate (the distinction the barge-in
//! contract draws between "what reached the speaker" and "bytes received").
//!
//! It OVER-reads true acoustic playout by the FIXED downstream pipeline
//! depth (CamillaDSP + the two snd-aloop rings + outputd's content ring +
//! the DAC buffer/hw delay), on the order of tens to ~150 ms. That is the
//! conservative direction for truncation (slightly more "heard" than
//! reality, so the assistant will not wrongly repeat) and a large
//! improvement over the previous hardcoded `0`. Closing that fixed offset
//! to exact DAC-clock precision (subtracting outputd's reported DAC delay)
//! is a documented follow-up — see
//! `docs/HANDOFF-speaker-output-reference.md` "Robust Barge-In Contract".
//!
//! ## Shape parity with outputd
//!
//! The emitted events mirror `jasper-outputd`'s `PlayoutEvent` JSON
//! (segment id, provider item id, queued/written/drained/flushed frames)
//! so the FLUSH_SYNC ack is the same shape whichever daemon owns playout.
//! At fan-in's single commit point "written" == "drained" == played, so
//! both fields carry the mix-commit count.
//!
//! ## Bounded for months on a 1 GB Pi
//!
//! A segment is pruned the moment it is fully played AND ended, so the
//! steady-state ledger holds only in-flight segments (typically one — the
//! wire only opens a new segment per provider item, not per audio chunk).
//! Every segment reliably ends (the client sends `SEGMENT_END`, and a new
//! `SEGMENT_START` is preceded by one), so the ledger returns to empty
//! after each turn even though a turn ends without a flush. [`MAX_SEGMENTS`]
//! is a defensive backstop against a pathological flood of never-ended
//! segments; the pending-audio budget already bounds how much can queue.

use std::collections::VecDeque;

use jasper_tts_protocol::SegmentKind;

/// Defensive cap on retained segments. The pending-frame budget (2 s of
/// audio) plus per-provider-item segmenting already keep the in-flight
/// count to a handful; this only guards against a pathological flood of
/// tiny never-ended segments so the ledger can never grow without bound.
const MAX_SEGMENTS: usize = 1024;

/// One flushed assistant/cue segment, reported in the `FLUSH_SYNC` ack's
/// `events` array. Field names mirror `jasper-outputd`'s `PlayoutEvent`
/// so the two daemons' acks are the same shape.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PlayoutEvent {
    pub local_segment_id: u64,
    pub provider_item_id: Option<String>,
    pub kind: SegmentKind,
    pub queued_frames: u64,
    /// Frames committed downstream at the mix point ("played" estimate).
    pub played_frames: u64,
    /// Queued frames that were dropped by the flush (never committed).
    pub flushed_frames: u64,
    /// `played_frames` converted to milliseconds at the mix rate.
    pub audio_played_ms: u64,
}

#[derive(Debug)]
struct Segment {
    local_segment_id: u64,
    provider_item_id: Option<String>,
    kind: SegmentKind,
    /// `queued_total` at the moment this segment opened, i.e. its start
    /// position on the FIFO queue timeline. `played_total - queue_start`
    /// (clamped) is how many of this segment's frames have been committed.
    queue_start: u64,
    queued_frames: u64,
    ended: bool,
}

impl Segment {
    fn played(&self, played_total: u64) -> u64 {
        played_total
            .saturating_sub(self.queue_start)
            .min(self.queued_frames)
    }

    fn fully_played(&self, played_total: u64) -> bool {
        played_total >= self.queue_start.saturating_add(self.queued_frames)
    }
}

/// Per-segment playout accounting for the solo fan-in TTS path.
///
/// Two monotonic watermarks on a single FIFO timeline drive everything:
/// `queued_total` (frames accepted onto the audio queue) and `played_total`
/// (frames popped into the program by the mixer). A segment occupies
/// `[queue_start, queue_start + queued_frames)`; its played count is
/// `clamp(played_total - queue_start, 0, queued_frames)`. This is the same
/// watermark model `jasper-outputd`'s ledger uses, minus the DAC-delay
/// subtraction it can afford because it owns the DAC clock.
pub struct PlayoutLedger {
    sample_rate: u32,
    next_id: u64,
    queued_total: u64,
    played_total: u64,
    segments: VecDeque<Segment>,
}

impl PlayoutLedger {
    pub fn new(sample_rate: u32) -> Self {
        assert!(sample_rate > 0, "sample rate must be > 0");
        Self {
            sample_rate,
            next_id: 1,
            queued_total: 0,
            played_total: 0,
            segments: VecDeque::new(),
        }
    }

    /// Open a new segment, ending any still-open predecessor. The wire
    /// already sends `SEGMENT_END` before a new `SEGMENT_START`; ending
    /// here too is belt-and-suspenders so an implicit cue segment cannot
    /// strand the ledger.
    pub fn start_segment(&mut self, provider_item_id: Option<String>, kind: SegmentKind) {
        self.end_open_segment();
        let id = self.next_id;
        self.next_id = self.next_id.wrapping_add(1);
        self.segments.push_back(Segment {
            local_segment_id: id,
            provider_item_id,
            kind,
            queue_start: self.queued_total,
            queued_frames: 0,
            ended: false,
        });
        self.enforce_cap();
    }

    /// Record `frames` accepted onto the audio queue (call only for frames
    /// that actually enqueue — not stale-epoch or over-budget drops, so the
    /// ledger total stays equal to the live queue depth). Opens an implicit
    /// Assistant segment for the legacy GAIN+AUDIO cue path that sends no
    /// `SEGMENT_START`, mirroring outputd's `TtsBridge`.
    pub fn note_queued(&mut self, frames: u64) {
        if frames == 0 {
            return;
        }
        if self.open_segment_mut().is_none() {
            self.start_segment(None, SegmentKind::Assistant);
        }
        if let Some(seg) = self.open_segment_mut() {
            seg.queued_frames = seg.queued_frames.saturating_add(frames);
        }
        self.queued_total = self.queued_total.saturating_add(frames);
    }

    /// Mark the current open segment ended (no more audio is coming for it).
    pub fn end_segment(&mut self) {
        self.end_open_segment();
    }

    /// Advance the mix-commit watermark by `frames` popped into the program
    /// this period, then prune any segment that is now fully played AND
    /// ended (it has nothing left to flush).
    pub fn advance_played(&mut self, frames: u64) {
        if frames == 0 {
            return;
        }
        self.played_total = self
            .played_total
            .saturating_add(frames)
            .min(self.queued_total);
        self.prune_terminal();
    }

    /// Snapshot every live segment as a flush event, then reset. Called
    /// when the mixer clears the audio queue, so both watermarks reset
    /// together and stay consistent for the next batch. Frames not yet
    /// committed become `flushed_frames` (dropped, unheard).
    pub fn flush(&mut self) -> Vec<PlayoutEvent> {
        let played_total = self.played_total;
        let sample_rate = self.sample_rate;
        let events = self
            .segments
            .iter()
            .map(|seg| {
                let played = seg.played(played_total);
                PlayoutEvent {
                    local_segment_id: seg.local_segment_id,
                    provider_item_id: seg.provider_item_id.clone(),
                    kind: seg.kind,
                    queued_frames: seg.queued_frames,
                    played_frames: played,
                    flushed_frames: seg.queued_frames.saturating_sub(played),
                    audio_played_ms: frames_to_ms(played, sample_rate),
                }
            })
            .collect();
        self.segments.clear();
        self.queued_total = 0;
        self.played_total = 0;
        events
    }

    fn open_segment_mut(&mut self) -> Option<&mut Segment> {
        match self.segments.back_mut() {
            Some(seg) if !seg.ended => Some(seg),
            _ => None,
        }
    }

    fn end_open_segment(&mut self) {
        if let Some(seg) = self.segments.back_mut() {
            seg.ended = true;
        }
    }

    fn prune_terminal(&mut self) {
        let played_total = self.played_total;
        while let Some(front) = self.segments.front() {
            if front.ended && front.fully_played(played_total) {
                self.segments.pop_front();
            } else {
                break;
            }
        }
    }

    fn enforce_cap(&mut self) {
        // FIFO order means the front is the closest-to-drained; dropping it
        // is the least-lossy backstop. Unreachable in normal operation.
        while self.segments.len() > MAX_SEGMENTS {
            self.segments.pop_front();
        }
    }

    #[cfg(test)]
    fn segment_count(&self) -> usize {
        self.segments.len()
    }
}

/// Integer frames -> milliseconds at `sample_rate`. Saturating multiply so
/// a degenerate frame count can never panic.
fn frames_to_ms(frames: u64, sample_rate: u32) -> u64 {
    frames.saturating_mul(1000) / (sample_rate as u64)
}

#[cfg(test)]
mod tests {
    use super::*;

    const RATE: u32 = 48_000;

    fn item(id: &str) -> Option<String> {
        Some(id.to_string())
    }

    #[test]
    fn flush_reports_played_and_unheard_frames_mid_segment() {
        let mut ledger = PlayoutLedger::new(RATE);
        ledger.start_segment(item("item-1"), SegmentKind::Assistant);
        ledger.note_queued(48_000); // 1000 ms queued
        ledger.advance_played(14_400); // 300 ms committed downstream

        let events = ledger.flush();

        assert_eq!(events.len(), 1);
        let e = &events[0];
        assert_eq!(e.provider_item_id.as_deref(), Some("item-1"));
        assert_eq!(e.queued_frames, 48_000);
        assert_eq!(e.played_frames, 14_400);
        assert_eq!(e.flushed_frames, 33_600);
        assert_eq!(e.audio_played_ms, 300);
    }

    #[test]
    fn flush_resets_watermarks_for_next_batch() {
        let mut ledger = PlayoutLedger::new(RATE);
        ledger.start_segment(item("a"), SegmentKind::Assistant);
        ledger.note_queued(9_600);
        ledger.advance_played(4_800);
        let _ = ledger.flush();

        // A second, independent segment must account from zero, not from
        // the prior batch's absolute frame positions.
        ledger.start_segment(item("b"), SegmentKind::Assistant);
        ledger.note_queued(9_600);
        ledger.advance_played(2_400); // 50 ms
        let events = ledger.flush();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].played_frames, 2_400);
        assert_eq!(events[0].audio_played_ms, 50);
    }

    #[test]
    fn played_is_capped_at_queued_even_if_mixer_overshoots() {
        let mut ledger = PlayoutLedger::new(RATE);
        ledger.start_segment(None, SegmentKind::Cue);
        ledger.note_queued(4_800);
        ledger.advance_played(9_600); // more than queued

        let events = ledger.flush();
        assert_eq!(events[0].played_frames, 4_800);
        assert_eq!(events[0].flushed_frames, 0);
        assert_eq!(events[0].audio_played_ms, 100);
    }

    #[test]
    fn implicit_segment_opens_for_legacy_cue_audio_without_segment_start() {
        let mut ledger = PlayoutLedger::new(RATE);
        // No start_segment: a bare cue is GAIN+AUDIO.
        ledger.note_queued(2_400);
        let events = ledger.flush();
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].provider_item_id, None);
        assert_eq!(events[0].kind, SegmentKind::Assistant);
        assert_eq!(events[0].flushed_frames, 2_400);
    }

    #[test]
    fn multiple_segments_attribute_played_frames_in_order() {
        let mut ledger = PlayoutLedger::new(RATE);
        ledger.start_segment(item("first"), SegmentKind::Assistant);
        ledger.note_queued(4_800); // [0, 4800)
        ledger.end_segment();
        ledger.start_segment(item("second"), SegmentKind::Assistant);
        ledger.note_queued(4_800); // [4800, 9600)

        // Play 1.5 segments worth: all of "first", half of "second".
        ledger.advance_played(7_200);

        let events = ledger.flush();
        // "first" is fully played AND ended -> pruned; only "second" remains.
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].provider_item_id.as_deref(), Some("second"));
        assert_eq!(events[0].played_frames, 2_400);
        assert_eq!(events[0].flushed_frames, 2_400);
    }

    #[test]
    fn fully_played_ended_segment_is_pruned_without_a_flush() {
        // The steady-state bound: a completed turn drains and ends, so the
        // ledger empties even though a normal turn never flushes.
        let mut ledger = PlayoutLedger::new(RATE);
        ledger.start_segment(item("done"), SegmentKind::Assistant);
        ledger.note_queued(4_800);
        ledger.end_segment();
        ledger.advance_played(4_800);
        assert_eq!(ledger.segment_count(), 0);

        // A still-open (not ended) but fully-played segment is retained,
        // because more audio may still arrive for it.
        ledger.start_segment(item("open"), SegmentKind::Assistant);
        ledger.note_queued(4_800);
        ledger.advance_played(4_800);
        assert_eq!(ledger.segment_count(), 1);
    }

    #[test]
    fn flush_of_empty_ledger_is_empty() {
        let mut ledger = PlayoutLedger::new(RATE);
        assert!(ledger.flush().is_empty());
    }

    #[test]
    fn frames_to_ms_matches_sample_rate() {
        assert_eq!(frames_to_ms(48_000, 48_000), 1000);
        assert_eq!(frames_to_ms(24_000, 48_000), 500);
        assert_eq!(frames_to_ms(0, 48_000), 0);
    }
}
