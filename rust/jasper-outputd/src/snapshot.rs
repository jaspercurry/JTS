// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

use super::{
    frames_to_ms_opt, rate_per_hour, unpack_optional_i64, unpack_optional_u64, OutputdState,
};
use jasper_daemon::json::{
    event_age_ms, push_kv_bool, push_kv_f64, push_kv_f64_opt, push_kv_i64, push_kv_i64_opt,
    push_kv_str, push_kv_str_opt, push_kv_u64, push_kv_u64_opt,
};
use std::sync::atomic::Ordering;

impl OutputdState {
    pub(super) fn content_json(&self, buf: &mut String, uptime_ms: u64, content_xrun_count: u64) {
        buf.push_str(r#""content":{"#);
        // The resolved bridge mode IS the source — same string as
        // `content_bridge.mode` below, not a re-derived guess from
        // `shm_ring_path` (which stayed false for `DacContentRing`, #4807 R-261).
        push_kv_str(buf, "source", &self.content_bridge_mode);
        buf.push(',');
        // The DECLARED wire of the content hop. Nothing negotiates it here: the
        // ring's own attach validates the declaration against the writer's
        // header, so this is the value that got the ring open.
        push_kv_str(buf, "format", self.declared_content_format.as_str());
        buf.push(',');
        push_kv_u64(
            buf,
            "period_frames",
            self.content_period_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            buf,
            "buffer_frames",
            self.content_buffer_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            buf,
            "frames_read",
            self.content_frames_read.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            buf,
            "empty_periods",
            self.content_empty_period_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            buf,
            "consecutive_empty_periods",
            self.content_consecutive_empty_periods
                .load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_bool(buf, "deaf", self.content_deaf.load(Ordering::Relaxed));
        buf.push(',');
        push_kv_u64(
            buf,
            "partial_periods",
            self.content_partial_period_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            buf,
            "eagain_count",
            self.content_eagain_count.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(buf, "xrun_count", content_xrun_count);
        buf.push(',');
        push_kv_u64_opt(
            buf,
            "last_xrun_age_ms",
            event_age_ms(uptime_ms, self.last_content_xrun_ms.load(Ordering::Relaxed)),
        );
        buf.push(',');
        push_kv_f64(
            buf,
            "xrun_rate_per_hour",
            rate_per_hour(content_xrun_count, uptime_ms),
            3,
        );
        // Ring B honesty contract (latency/ring-proto-shm): under the shm_ring
        // content source, outputd reads the post-DSP program from an n-slot SHM
        // ping-pong ring, NOT an ALSA capture PCM — so `content.buffer_frames`
        // above is a synthetic period-sized stand-in (neither sink opens a content
        // PCM at all — ADR-0100 leaves the ring as outputd's one upstream).
        // This sub-block reports the TRUE Ring B capacity that
        // outputd requires of the writer — n_slots x slot_frames — so the synthetic
        // is clearly labeled and jasper-doctor validates the ring geometry instead
        // of mis-applying the ALSA ">= 2x period" jitter floor (which a bounded
        // n-slot queue is not). Full runtime health (occupancy, empty reads, writer
        // liveness) stays in the top-level `shm_ring` block; this is the buffering
        // capacity contract that sits next to `content.buffer_frames`.
        if self.shm_ring_path.is_some() {
            let slots = self.shm_ring_slots.load(Ordering::Relaxed);
            let slot_frames = self.shm_ring_slot_frames.load(Ordering::Relaxed);
            buf.push(',');
            buf.push_str(r#""ring":{"#);
            push_kv_u64(buf, "slots", slots);
            buf.push(',');
            push_kv_u64(buf, "slot_frames", slot_frames);
            buf.push(',');
            push_kv_u64(buf, "capacity_frames", slots.saturating_mul(slot_frames));
            buf.push('}');
        }
        buf.push('}');
        buf.push(',');
    }

    pub(super) fn content_bridge_json(&self, buf: &mut String) {
        buf.push_str(r#""content_bridge":{"#);
        push_kv_str(buf, "mode", &self.content_bridge_mode);
        buf.push('}');
        buf.push(',');
    }

    pub(super) fn shm_ring_json(&self, buf: &mut String) {
        // PROTOTYPE (latency/ring-proto-shm): SHM ping-pong ring reader health.
        // enabled:false with no further fields when unconfigured (default-off,
        // zero noise), full metrics when the flag armed it. `occupancy` is the
        // live W-R depth; empty_reads split startup vs steady like the local
        // pipe; writer_alive/pid/heartbeat_age surface the cross-process writer.
        buf.push_str(r#""shm_ring":{"#);
        match self.shm_ring_path.as_deref() {
            Some(path) => {
                push_kv_bool(buf, "enabled", true);
                buf.push(',');
                push_kv_str(buf, "path", path);
                buf.push(',');
                push_kv_bool(
                    buf,
                    "attached",
                    self.shm_ring_attached.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(buf, "slots", self.shm_ring_slots.load(Ordering::Relaxed));
                buf.push(',');
                push_kv_u64(
                    buf,
                    "slot_frames",
                    self.shm_ring_slot_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                // The wire: the two axes ring v2 made per-box, read out of the
                // one cell together so the pair is always the SAME source. See
                // the `shm_ring_wire` field for their provenance.
                let (wire_format, wire_channels) = self.shm_ring_wire.get().copied().unwrap_or((
                    self.declared_content_format.as_str(),
                    self.declared_content_channels,
                ));
                push_kv_str(buf, "format", wire_format);
                buf.push(',');
                push_kv_u64(buf, "channels", wire_channels);
                buf.push(',');
                push_kv_u64(
                    buf,
                    "occupancy",
                    self.shm_ring_occupancy.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    buf,
                    "frames_read",
                    self.shm_ring_frames_read.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    buf,
                    "startup_empty_reads",
                    self.shm_ring_startup_empty_reads.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    buf,
                    "empty_reads",
                    self.shm_ring_empty_reads.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    buf,
                    "epoch_resets",
                    self.shm_ring_epoch_resets.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    buf,
                    "reader_resyncs",
                    self.shm_ring_reader_resyncs.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_bool(
                    buf,
                    "writer_alive",
                    self.shm_ring_writer_alive.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    buf,
                    "writer_pid",
                    self.shm_ring_writer_pid.load(Ordering::Relaxed),
                );
                buf.push(',');
                // u64::MAX = "writer never heartbeated" (see jasper_ring's
                // RingMetrics). Serialize the sentinel as JSON null rather than
                // 18446744073709551615, which exceeds JS Number.MAX_SAFE_INTEGER
                // and would deserialize lossily in the /state dashboard. Uses the
                // same OPTIONAL_U64_NONE convention as the pcm_delay fields.
                push_kv_u64_opt(
                    buf,
                    "writer_heartbeat_age_ms",
                    unpack_optional_u64(
                        self.shm_ring_writer_heartbeat_age_ms
                            .load(Ordering::Relaxed),
                    ),
                );
            }
            None => {
                push_kv_bool(buf, "enabled", false);
            }
        }
        buf.push('}');
        buf.push(',');
    }

    pub(super) fn dac_content_json(&self, buf: &mut String) {
        // Multi-room round-trip lane — DAEMON-TRUTH health
        // for /state + jasper-doctor (never a Python mirror of env
        // intent). enabled:false with no further fields when the lane is
        // not configured (solo — zero cost, zero noise).
        buf.push_str(r#""dac_content":{"#);
        match self.dac_content_lane.as_ref() {
            Some(path) => {
                push_kv_bool(buf, "enabled", true);
                buf.push(',');
                push_kv_str(buf, "transport", "ring");
                buf.push(',');
                push_kv_str(buf, "ring", path);
                buf.push(',');
                push_kv_str(buf, "channel", &self.dac_content_channel);
                buf.push(',');
                buf.push_str(&format!("\"trim_db\":{:.1}", self.dac_content_trim_db()));
                buf.push(',');
                push_kv_bool(
                    buf,
                    "serving_fifo",
                    self.dac_content_serving_fifo.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    buf,
                    "fifo_periods",
                    self.dac_content_fifo_periods.load(Ordering::Relaxed),
                );
            }
            None => {
                push_kv_bool(buf, "enabled", false);
            }
        }
        buf.push('}');
        buf.push(',');
    }

    pub(super) fn tts_json(&self, buf: &mut String) {
        // Bonded-member TTS lane — daemon truth for /state +
        // doctor. enabled:false when the lane is off (solo: fanin owns
        // TTS) — zero noise, mirroring dac_content.
        buf.push_str(r#""tts":{"#);
        match self.tts.get() {
            Some((socket, m)) => {
                push_kv_bool(buf, "enabled", true);
                buf.push(',');
                push_kv_str(buf, "socket", socket);
                buf.push(',');
                push_kv_u64(
                    buf,
                    "pending_frames",
                    m.pending_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(buf, "budget_frames", m.max_pending_frames);
                buf.push(',');
                push_kv_u64(buf, "requests", m.requests.load(Ordering::Relaxed));
                buf.push(',');
                let counters = &m.counters;
                push_kv_u64(buf, "dropped_audio_frames", counters.dropped_audio_frames());
                buf.push(',');
                push_kv_u64(buf, "dropped_commands", counters.dropped_commands());
                buf.push(',');
                push_kv_u64(buf, "connections_rejected", counters.connections_rejected());
                buf.push(',');
                push_kv_u64(buf, "tts_clients", counters.tts_clients());
                buf.push(',');
                push_kv_u64(buf, "frame_timeouts", counters.frame_timeouts());
                buf.push(',');
                push_kv_u64(buf, "protocol_errors", counters.protocol_errors());
                buf.push(',');
                push_kv_u64(
                    buf,
                    "flush_requests",
                    m.flush_requests.load(Ordering::Relaxed),
                );
                buf.push(',');
                push_kv_u64(
                    buf,
                    "flushed_frames",
                    m.flushed_frames.load(Ordering::Relaxed),
                );
                buf.push(',');
                // The same assistant_loudness object fan-in exposes, rendered
                // through the shared writer so the two /state shapes cannot
                // drift (pinned by ASSISTANT_LOUDNESS_STATUS_KEYS on both).
                buf.push_str(r#""assistant_loudness":"#);
                jasper_tts_protocol::loudness::render_assistant_loudness(
                    buf,
                    &m.loudness_snapshot(),
                );
            }
            None => {
                push_kv_bool(buf, "enabled", false);
            }
        }
        buf.push('}');
        buf.push(',');
    }

    pub(super) fn mix_json(&self, buf: &mut String) {
        buf.push_str(r#""mix":{"#);
        push_kv_u64(
            buf,
            "reference_sequence",
            self.reference_sequence.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            buf,
            "last_period_clipped_samples",
            self.last_period_clipped_samples.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            buf,
            "clipped_samples",
            self.total_clipped_samples.load(Ordering::Relaxed),
        );
        buf.push('}');
        buf.push(',');
    }

    pub(super) fn watchdog_json(&self, buf: &mut String, uptime_ms: u64) {
        buf.push_str(r#""watchdog":{"#);
        let last_progress_ms = self.last_progress_ms.load(Ordering::Relaxed);
        let age_ms = uptime_ms.saturating_sub(last_progress_ms);
        push_kv_u64(
            buf,
            "pings_sent",
            self.watchdog_pings_sent.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(buf, "last_progress_age_ms", age_ms);
        buf.push('}');
    }

    pub(super) fn dac_json(
        &self,
        buf: &mut String,
        sample_rate: u64,
        uptime_ms: u64,
        dac_xrun_count: u64,
    ) -> Option<u64> {
        buf.push_str(r#""dac":{"#);
        push_kv_str(buf, "pcm", &self.dac_pcm);
        buf.push(',');
        // NEGOTIATED once outputd has opened its edge: the format read back off
        // the installed hw_params, not the declaration that asked for it.
        // Consumers that must know what edge is running (chiefly the chip-AEC
        // alignment identity, which records it for forensics — ADR-0190
        // excludes it from comparison) read it here.
        //
        // Falls back to the registry declaration whenever no edge is open yet —
        // which is the fake backend for its whole life, AND the ALSA backend for
        // its pre-open window: the state socket binds before the sink opens, so
        // a STATUS read in that window is answered with the declaration.
        push_kv_str(
            buf,
            "format",
            self.negotiated_dac_format
                .get()
                .copied()
                .unwrap_or(self.declared_dac_format.as_str()),
        );
        buf.push(',');
        push_kv_u64(buf, "sample_rate", sample_rate);
        buf.push(',');
        push_kv_u64(
            buf,
            "period_frames",
            self.dac_period_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            buf,
            "buffer_frames",
            self.dac_buffer_frames.load(Ordering::Relaxed),
        );
        buf.push(',');
        push_kv_u64(
            buf,
            "frames_written",
            self.dac_frames_written.load(Ordering::Relaxed),
        );
        buf.push(',');
        let dac_delay_frames =
            unpack_optional_u64(self.dac_snd_pcm_delay_frames.load(Ordering::Relaxed));
        push_kv_u64_opt(buf, "snd_pcm_delay_frames", dac_delay_frames);
        buf.push(',');
        push_kv_f64_opt(
            buf,
            "snd_pcm_delay_ms",
            frames_to_ms_opt(dac_delay_frames, sample_rate),
            3,
        );
        buf.push(',');
        push_kv_u64_opt(
            buf,
            "snd_pcm_delay_sample_age_ms",
            event_age_ms(
                uptime_ms,
                self.dac_snd_pcm_delay_sample_ms.load(Ordering::Relaxed),
            ),
        );
        buf.push(',');
        push_kv_u64(buf, "xrun_count", dac_xrun_count);
        buf.push(',');
        push_kv_u64_opt(
            buf,
            "last_xrun_age_ms",
            event_age_ms(uptime_ms, self.last_dac_xrun_ms.load(Ordering::Relaxed)),
        );
        buf.push(',');
        push_kv_f64(
            buf,
            "xrun_rate_per_hour",
            rate_per_hour(dac_xrun_count, uptime_ms),
            3,
        );
        buf.push('}');
        buf.push(',');
        dac_delay_frames
    }

    pub(super) fn dual_apple_json(&self, buf: &mut String) {
        if self.sink_mode == "dual_apple" {
            buf.push_str(r#""dual_apple":{"#);
            push_kv_str_opt(buf, "dac_a_pcm", self.dual_dac_a_pcm.as_deref());
            buf.push(',');
            push_kv_str_opt(buf, "dac_b_pcm", self.dual_dac_b_pcm.as_deref());
            buf.push(',');
            push_kv_bool(buf, "linked", self.dual_linked.load(Ordering::Relaxed));
            buf.push(',');
            push_kv_i64_opt(
                buf,
                "delay_delta_frames",
                unpack_optional_i64(self.dual_delay_delta_frames.load(Ordering::Relaxed)),
            );
            buf.push(',');
            push_kv_i64_opt(
                buf,
                "delay_delta_baseline_frames",
                unpack_optional_i64(
                    self.dual_delay_delta_baseline_frames
                        .load(Ordering::Relaxed),
                ),
            );
            buf.push(',');
            push_kv_i64_opt(
                buf,
                "delay_delta_error_frames",
                unpack_optional_i64(self.dual_delay_delta_error_frames.load(Ordering::Relaxed)),
            );
            buf.push(',');
            push_kv_i64(
                buf,
                "max_delay_delta_frames",
                self.dual_max_delay_delta_frames.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                buf,
                "dac_a_xruns",
                self.dual_dac_a_xruns.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                buf,
                "dac_b_xruns",
                self.dual_dac_b_xruns.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                buf,
                "group_recoveries",
                self.dual_group_recoveries.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                buf,
                "delay_baseline_relatches",
                self.dual_delay_baseline_relatches.load(Ordering::Relaxed),
            );
            buf.push(',');
            push_kv_u64(
                buf,
                "reprime_alignment_failures",
                self.dual_reprime_alignment_failures.load(Ordering::Relaxed),
            );
            buf.push('}');
            buf.push(',');
        }
    }
}
