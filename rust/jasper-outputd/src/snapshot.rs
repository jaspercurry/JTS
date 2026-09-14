// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

use super::{rate_per_hour, OutputdState};
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
}
