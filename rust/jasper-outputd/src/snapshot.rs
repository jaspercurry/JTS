// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

use super::{rate_per_hour, unpack_optional_u64, OutputdState};
use jasper_daemon::json::{
    event_age_ms, push_kv_bool, push_kv_f64, push_kv_str, push_kv_u64, push_kv_u64_opt,
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
}
