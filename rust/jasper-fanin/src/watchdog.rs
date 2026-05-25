//! Heartbeat with progress sentinel — the JTS-standard Tier 1/2
//! watchdog pattern.
//!
//! Mirrors `jasper/watchdog.py:Heartbeat` (the Python daemons' shared
//! implementation). The contract:
//!
//!   1. The work loop calls `bump_progress()` after every successful
//!      unit of work (per-frame, per-iteration, etc.).
//!   2. A separate heartbeat thread wakes every `HEARTBEAT_INTERVAL`
//!      and calls `sd_notify(WATCHDOG=1)` ONLY IF the sentinel says
//!      recent progress happened.
//!   3. If the work loop wedges, `bump_progress()` stops firing, the
//!      sentinel ages past `STALE_THRESHOLD`, the heartbeat thread
//!      stops pinging, systemd's WatchdogSec expires, and
//!      `Restart=on-failure` brings the daemon back fresh in ~2 s.
//!
//! Why the sentinel matters: a naive "ping every N seconds" heartbeat
//! masks the failure mode that matters most — a deadlocked work loop
//! that's not making forward progress. The sentinel pattern makes
//! the work-loop liveness visible to systemd, not just the heartbeat
//! thread's liveness.
//!
//! 2026-05-11 reference: jasper-aec-bridge's earlier failure mode
//! (PortAudio InputStream dead after USB underrun, main thread
//! blocked in `out_stream.write()`, Python GIL held, SIGTERM
//! ineffective). The Python heartbeat caught it because the work
//! loop's progress-bump stopped firing even though the process was
//! still "alive" from systemd's view. Same pattern, same rationale,
//! Rust port.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use log::{info, warn};
use sd_notify::NotifyState;

/// Maximum permitted age of the work loop's last progress bump before
/// the heartbeat thread STOPS pinging systemd. systemd's WatchdogSec
/// is 30 s; 5 s gives the work loop generous slack while still
/// catching real hangs within 2 × HEARTBEAT_INTERVAL.
const STALE_THRESHOLD: Duration = Duration::from_secs(5);

/// How often the heartbeat thread checks the sentinel and (if fresh)
/// pings systemd. Must be well below the systemd unit's
/// WatchdogSec=30s — 10 s gives 3 ping opportunities per watchdog
/// window.
const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(10);

pub struct Heartbeat {
    /// Nanoseconds since `epoch`, recorded by the work loop on every
    /// successful unit of work via `bump_progress()`. The heartbeat
    /// thread reads this to gate its ping.
    ///
    /// Relaxed ordering is correct: writer (work loop) and reader
    /// (heartbeat thread) only need eventual consistency. There's no
    /// happens-before relationship to enforce; staleness is the
    /// signal of interest.
    last_progress_ns: AtomicU64,

    /// Monotonic clock reference for both writer and reader. Captured
    /// once at construction; never changes thereafter.
    epoch: Instant,

    /// Diagnostic counters surfaced via the UDS STATUS endpoint.
    /// `pings_sent` increments on each successful WATCHDOG=1; `pings_skipped`
    /// increments when the sentinel is stale and we deliberately
    /// don't ping.
    pings_sent: AtomicU64,
    pings_skipped: AtomicU64,
}

impl Heartbeat {
    pub fn new() -> Self {
        Self {
            last_progress_ns: AtomicU64::new(0),
            epoch: Instant::now(),
            pings_sent: AtomicU64::new(0),
            pings_skipped: AtomicU64::new(0),
        }
    }

    /// Call from the work loop after every successful unit of work.
    /// Safe to call thousands of times per second — it's one atomic
    /// store with Relaxed ordering. Cheap enough to bump after every
    /// ALSA frame.
    pub fn bump_progress(&self) {
        let now_ns = self.epoch.elapsed().as_nanos() as u64;
        self.last_progress_ns.store(now_ns, Ordering::Relaxed);
    }

    /// Returns the age of the last progress bump in milliseconds.
    /// Used by the UDS STATUS endpoint (Phase 2 chunk 3) for
    /// observability — a value above ~30 ms in steady state is
    /// suspicious; above 5 s means we're about to be reaped by
    /// systemd.
    pub fn last_progress_age_ms(&self) -> u64 {
        let last = self.last_progress_ns.load(Ordering::Relaxed);
        let now_ns = self.epoch.elapsed().as_nanos() as u64;
        now_ns.saturating_sub(last) / 1_000_000
    }

    pub fn pings_sent(&self) -> u64 {
        self.pings_sent.load(Ordering::Relaxed)
    }

    pub fn pings_skipped(&self) -> u64 {
        self.pings_skipped.load(Ordering::Relaxed)
    }

    /// Send READY=1 to systemd (Type=notify contract), then spawn the
    /// heartbeat thread. The thread is detached — on process exit it
    /// goes with the process; we don't bother joining.
    ///
    /// `&Arc<Self>` parameter so the spawned thread can hold its own
    /// reference, keeping the Heartbeat alive for the daemon's
    /// lifetime even if main drops its handle.
    pub fn spawn(self: &Arc<Self>) {
        // READY=1: systemd considers us up. Without this, systemd's
        // Type=notify times out and the daemon gets killed during
        // startup.
        match sd_notify::notify(false, &[NotifyState::Ready]) {
            Ok(_) => info!("event=fanin.sd_notify_ready_sent"),
            Err(e) => warn!("event=fanin.sd_notify_ready_failed detail={}", e),
        }

        let me = Arc::clone(self);
        std::thread::Builder::new()
            .name("fanin-heartbeat".into())
            .spawn(move || me.run())
            .expect("heartbeat thread spawn failed");
    }

    /// The heartbeat thread's main loop. Wakes every
    /// `HEARTBEAT_INTERVAL`, checks the sentinel, pings (or
    /// deliberately doesn't).
    fn run(&self) {
        loop {
            std::thread::sleep(HEARTBEAT_INTERVAL);
            let age_ms = self.last_progress_age_ms();
            if age_ms < STALE_THRESHOLD.as_millis() as u64 {
                match sd_notify::notify(false, &[NotifyState::Watchdog]) {
                    Ok(_) => {
                        self.pings_sent.fetch_add(1, Ordering::Relaxed);
                    }
                    Err(e) => {
                        warn!(
                            "event=fanin.sd_notify_watchdog_failed detail={}",
                            e
                        );
                    }
                }
            } else {
                // Sentinel is stale — DON'T ping. Let systemd's
                // WatchdogSec expire and Restart=on-failure restart
                // us. Log so the wedge is visible in journald
                // post-mortem.
                warn!("event=fanin.watchdog.stale age_ms={}", age_ms);
                self.pings_skipped.fetch_add(1, Ordering::Relaxed);
            }
        }
    }

    /// Send STOPPING=1 at graceful shutdown. systemd uses this to
    /// distinguish "clean exit" from "crashed" for restart-policy
    /// accounting (StartLimitBurst).
    pub fn notify_stopping(&self) {
        if let Err(e) = sd_notify::notify(false, &[NotifyState::Stopping]) {
            warn!("event=fanin.sd_notify_stopping_failed detail={}", e);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bump_progress_resets_age() {
        let hb = Heartbeat::new();
        // Let the epoch age a bit so there's something to reset.
        std::thread::sleep(Duration::from_millis(30));
        let age_before = hb.last_progress_age_ms();
        hb.bump_progress();
        let age_after = hb.last_progress_age_ms();
        assert!(
            age_after < age_before,
            "bump_progress should reset the sentinel age (before={}, after={})",
            age_before,
            age_after,
        );
        assert!(
            age_after < 10,
            "age right after bump_progress should be <10ms, got {}",
            age_after,
        );
    }

    #[test]
    fn pre_bump_age_reflects_epoch_elapsed() {
        // Before any bump, the sentinel reads zero — which means
        // "as old as the entire epoch elapsed time." This ensures a
        // daemon that crashes before its first bump is correctly
        // judged unhealthy by the heartbeat gating.
        let hb = Heartbeat::new();
        std::thread::sleep(Duration::from_millis(50));
        let age_ms = hb.last_progress_age_ms();
        assert!(
            age_ms >= 50,
            "pre-bump age should reflect full epoch elapsed (≥50ms), got {}",
            age_ms,
        );
    }

    #[test]
    fn counters_start_at_zero() {
        let hb = Heartbeat::new();
        assert_eq!(hb.pings_sent(), 0);
        assert_eq!(hb.pings_skipped(), 0);
    }
}
