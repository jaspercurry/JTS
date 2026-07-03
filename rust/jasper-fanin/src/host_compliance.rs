// SPDX-FileCopyrightText: 2026 Jasper Curry
//
// SPDX-License-Identifier: Apache-2.0

//! DEFAULT-OFF host-compliance persistence for the USB DIRECT resampler lane.
//!
//! Extends the post-lock cushion decay (`lane_resampler::decay`): once a session
//! has PROVEN the host honours the pitch-steer command AND the decay has walked
//! the held target all the way to the floor with zero lock churn, that proof is
//! persisted. The NEXT session primes the resampler AT the decay floor instead of
//! at the full acquisition ceiling — skipping the ~2.5-minute descent that is
//! otherwise paid at every session start. The servo's per-session probe
//! (#1142's post-lock `AwaitLock` gate) is the immediate revalidation: a
//! floor-primed session that fails the probe, has its DLL demoted, or
//! underfill-unlocks in its first seconds snaps back to the full ceiling AND
//! deletes the proof (one strike → fail toward safety).
//!
//! This module is PURE + the file I/O shell. Two halves, both testable without
//! ALSA (fan-in can't compile on macOS — the scratch-crate convention):
//!
//! - [`HostCompliance`] — the on-disk record (schema 1) + atomic
//!   tempfile-then-rename writes + corrupt-/missing-tolerant reads. A malformed
//!   or absent file resolves to `None` (no flag), degrading to today's
//!   always-descend-from-ceiling behaviour — never a crash, never a stale prime.
//! - [`ComplianceProof`] — the PURE state machine the mixer ticks once per
//!   render period. It owns the "full proof" gate (decay at floor AND DLL-l0 held
//!   for a settle window AND zero unlock delta over the descent → write once) and
//!   the one-strike revocation predicate. No clock, no I/O, no atomics.
//!
//! ## Why one strike, not a failure count
//!
//! A floor-primed session that misbehaves is evidence the host on THIS port is no
//! longer (or was never) the compliant host the proof was written for — a replug
//! to a different machine, a different USB port, an OS that stopped honouring the
//! ctl. The safe response is to distrust the proof immediately: delete it, snap
//! back to the ceiling, and let the normal descent re-prove from scratch. A
//! multi-strike tolerance would keep priming at the floor across a genuine host
//! change for N sessions, each paying an audible acquisition churn. The record
//! still carries a `consecutive_failures` counter (incremented before the delete)
//! so a post-mortem can see a strike happened; it is written and then the file is
//! removed, so on disk it never exceeds 0 in the steady state.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

use serde::{Deserialize, Serialize};

/// The persistence schema version. Bump ONLY on an incompatible field change; a
/// record whose `schema` does not match is treated as corrupt (→ `None`), so an
/// old-schema file from a rolled-back build can never mis-prime a new one.
pub const SCHEMA_VERSION: u32 = 1;

/// The persisted host-compliance record. Written once per session (at most) when
/// the full proof lands; read once at lane build time. All fields are plain data
/// so the file is greppable and a human can eyeball it.
///
/// `serde` (de)serialisation is the parse boundary: an unknown/extra field is
/// ignored on read (forward-compat), a missing required field fails the parse (→
/// `None`, safe). The `schema` guard is checked explicitly by
/// [`HostCompliance::load`], not by serde, so a wrong version reads as corrupt
/// rather than erroring the whole file open.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct HostCompliance {
    /// Schema version. Must equal [`SCHEMA_VERSION`] or the record is rejected.
    pub schema: u32,
    /// Wall-clock epoch seconds the proof was written. Diagnostic only (surfaced
    /// in STATUS as `proved_at`); the prime decision never depends on its age —
    /// the per-session probe revalidates freshness, so a stale timestamp is not a
    /// reason to distrust the proof.
    pub proved_at_epoch_s: u64,
    /// The probe response ratio measured on the session that wrote the proof
    /// (≥ the pass band). Diagnostic evidence of HOW compliant the host was.
    pub probe_response_ratio: f64,
    /// The decay floor (frames) the proving session settled to — the geometry the
    /// next session primes at. If the live config's floor no longer matches this
    /// (an operator retuned the floor knob between sessions), the proof is treated
    /// as stale ([`HostCompliance::valid_for`]).
    pub floor_frames: u64,
    /// Consecutive floor-primed sessions that failed revalidation. Incremented in
    /// the record just before the file is deleted on a strike, so a fetched file
    /// captured mid-delete shows the strike; in the steady state the on-disk value
    /// is always 0 (proof present) or the file is absent (proof revoked).
    pub consecutive_failures: u32,
}

impl HostCompliance {
    /// Build a fresh proof record at the current geometry. `now_epoch_s` is the
    /// caller's wall clock (kept a parameter so this is pure/testable).
    pub fn new(proved_at_epoch_s: u64, probe_response_ratio: f64, floor_frames: u64) -> Self {
        Self {
            schema: SCHEMA_VERSION,
            proved_at_epoch_s,
            probe_response_ratio,
            floor_frames,
            consecutive_failures: 0,
        }
    }

    /// Whether this record is a VALID prime-at-floor authority for a lane whose
    /// live decay floor is `live_floor_frames`. Requires the schema to match AND
    /// the recorded floor to equal the live floor: if an operator retuned
    /// `JASPER_FANIN_RESAMPLER_CUSHION_DECAY_FLOOR_FRAMES` between sessions, the
    /// old proof's geometry is stale and we descend normally rather than prime at
    /// a floor the current config would never settle to.
    pub fn valid_for(&self, live_floor_frames: u64) -> bool {
        self.schema == SCHEMA_VERSION && self.floor_frames == live_floor_frames
    }

    /// Parse a record from raw JSON bytes, enforcing the schema guard. Returns
    /// `None` on any parse failure OR a schema mismatch — the caller treats both
    /// as "no flag" (fail toward today's descend-from-ceiling behaviour). Pure.
    pub fn from_json_bytes(bytes: &[u8]) -> Option<Self> {
        let rec: HostCompliance = serde_json::from_slice(bytes).ok()?;
        if rec.schema != SCHEMA_VERSION {
            return None;
        }
        Some(rec)
    }

    /// Serialise to pretty JSON bytes (with a trailing newline) for the atomic
    /// write. Pretty because the file is small, human-inspected, and never on a
    /// hot path. Infallible in practice (a fixed struct of plain scalars); an
    /// unexpected serialisation error propagates as `None` so a write is skipped
    /// rather than truncating the file.
    pub fn to_json_bytes(&self) -> Option<Vec<u8>> {
        let mut v = serde_json::to_vec_pretty(self).ok()?;
        v.push(b'\n');
        Some(v)
    }

    /// Load the record from `path`, returning `None` if the file is missing,
    /// unreadable, malformed, or a schema mismatch. NEVER errors — a bad byte on
    /// disk must degrade to "no proof", never block the lane build. The file I/O
    /// shell around the pure [`from_json_bytes`](Self::from_json_bytes).
    pub fn load(path: &Path) -> Option<Self> {
        let bytes = std::fs::read(path).ok()?;
        Self::from_json_bytes(&bytes)
    }

    /// Atomically write the record to `path`: serialise, write a sibling
    /// tempfile, fsync it, then rename over `path`. Mirrors `XrunLog`'s
    /// tempfile-then-rename write so a crash mid-write leaves the prior good file
    /// (or no file), never a torn one. Best-effort: any I/O error is returned so
    /// the caller can log it, but a failed persist NEVER affects audio — the flag
    /// simply isn't written this session and the normal descent still ran.
    ///
    /// The file is chmod'd `0644` (world-readable) so an operator can `cat` it on
    /// the Pi without sudo — it holds no secret, only diagnostic proof state. That
    /// overrides the daemon's `UMask=0007` (which would otherwise yield `0660`).
    pub fn store(&self, path: &Path) -> std::io::Result<()> {
        use std::io::Write;
        let bytes = self.to_json_bytes().ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "compliance serialize failed",
            )
        })?;
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let tmp = tmp_path(path);
        {
            let mut f = std::fs::File::create(&tmp)?;
            f.write_all(&bytes)?;
            f.sync_all()?;
            // World-readable diagnostic file (no secret). Explicit so the daemon's
            // 0007 umask doesn't strip other-read. Unix-only; a no-op elsewhere.
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = f.set_permissions(std::fs::Permissions::from_mode(0o644));
            }
        }
        std::fs::rename(&tmp, path)?;
        Ok(())
    }

    /// Delete the persisted record (the one-strike revocation). A missing file is
    /// success (idempotent — revoking an already-absent proof is a no-op). Any
    /// other I/O error is returned for logging; it never affects audio.
    pub fn revoke(path: &Path) -> std::io::Result<()> {
        match std::fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(e),
        }
    }
}

/// The sibling tempfile path for the atomic write (`<path>.tmp`). Kept adjacent
/// so the rename is same-filesystem (atomic). Pure.
fn tmp_path(path: &Path) -> PathBuf {
    let mut s = path.as_os_str().to_owned();
    s.push(".tmp");
    PathBuf::from(s)
}

/// The default persistence path — a sibling of the xrun log under the fan-in
/// state dir (`/var/lib/jasper/fanin/`), which the daemon already owns and writes
/// (root, `ReadWritePaths=/var/lib/jasper`, `create_dir_all` on first write). No
/// new StateDirectory / privilege grant is needed: this reuses the exact posture
/// the xrun ring established. Overridable via `JASPER_FANIN_HOST_COMPLIANCE_PATH`.
pub const DEFAULT_COMPLIANCE_PATH: &str = "/var/lib/jasper/fanin/host_compliance.json";

/// The per-tick outer signals [`ComplianceProof`] reads that it cannot derive
/// itself, sampled once per render period on the mixer thread. All come from
/// state the mixer already snapshots for the decay tick plus the resampler's own
/// live gauges.
#[derive(Debug, Clone, Copy)]
pub struct ProofSignals {
    /// The decay is at (or below) its floor this period — `held == floor` and the
    /// decay's frozen reason is `AtFloor`. The proof's "descent complete" gate.
    pub decay_at_floor: bool,
    /// The DLL ladder is `l0_locked` this period (the same reverse signal the
    /// decay tick reads). The proof requires a SUSTAINED l0 at the floor.
    pub dll_l0_locked: bool,
    /// The resampler's cumulative underfill-unlock count as of this period. The
    /// proof watches the DELTA of this across the settle window: a single unlock
    /// during the descent-to-settle window disqualifies the proof (churn).
    pub unlock_count: u64,
    /// The probe response ratio the servo measured this session (`Some` once the
    /// probe has a verdict; `None` before then). Recorded into the proof as
    /// evidence when it lands.
    pub probe_response_ratio: Option<f64>,
}

/// The reason a floor-primed session's revalidation FAILED — the one-strike
/// trigger. Surfaced in the `event=fanin.host_compliance.revoked reason=…` log
/// and STATUS (`revoked_reason_last`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RevokeReason {
    /// The servo's per-session compliance probe returned FAIL.
    ProbeFail,
    /// The DLL ladder demoted to L2 (probe fail OR mid-stream demotion evidence).
    DllDemotion,
    /// An underfill unlock inside the early-session revalidation window.
    EarlyUnlock,
}

impl RevokeReason {
    /// The stable STATUS / log token. Append, never renumber.
    pub fn as_str(self) -> &'static str {
        match self {
            RevokeReason::ProbeFail => "probe_fail",
            RevokeReason::DllDemotion => "dll_demotion",
            RevokeReason::EarlyUnlock => "early_unlock",
        }
    }
}

/// The per-tick revalidation inputs for a floor-primed session, sampled by the
/// mixer from the servo reverse signals and the resampler's unlock counter.
#[derive(Debug, Clone, Copy)]
pub struct RevalidationSignals {
    /// The servo's last probe verdict as a code (`2` == FAIL; see
    /// `host_clock::probe_result_code`). A fresh probe FAIL is the strongest
    /// evidence the host is not honouring the pitch command.
    pub probe_result_code: u64,
    /// Whether the `probe_result_code` was produced by a probe that ran DURING the
    /// current lock (i.e. it is a LIVE verdict, not one carried over from a prior
    /// session). The servo deliberately leaves `probe_result = Fail` across a
    /// session boundary (`jasper_host_clock::end_session` only overwrites a verdict
    /// that was actively measuring), so a fresh lock on a NEW, compliant host would
    /// otherwise read the previous host's stale FAIL and spuriously revoke. A live
    /// probe FAIL always coincides with the ladder sitting at L2 (the fail
    /// transitions to `L2Fallback` and holds there until the idle boundary
    /// re-promotes to `Probing`), so the mixer sets this from `ladder_l2`; a stale
    /// FAIL sits in `Probing` with `ladder_l2 == false` and is ignored.
    pub probe_verdict_is_live: bool,
    /// The DLL ladder demoted to L2 (probe-fail-into-L2 OR a mid-stream demotion).
    pub ladder_l2: bool,
    /// True while inside the early-revalidation window since the last lock (the
    /// underfill-unlock trigger only fires here — the acquisition-adjacent phase).
    pub within_early_window: bool,
    /// The resampler's unlock count advanced since the lock baseline (churn — the
    /// floor prime did not hold on this host).
    pub unlock_advanced: bool,
}

/// Decide whether a floor-primed session's revalidation FAILS this period, and if
/// so, why. Ordering is by directness of evidence: a fresh (LIVE) probe FAIL first,
/// then a live L2 demotion, then an early-window underfill unlock. Returns `None`
/// when the session is still healthy. PURE — the mixer enacts the returned reason
/// (snap-back + revoke); this only decides. `probe_fail` is the exact
/// `host_clock::probe_result_code` FAIL value (2), kept a plain literal here so
/// the pure module needs no dependency on the host_clock adapter.
///
/// The probe-FAIL trigger is gated on `probe_verdict_is_live` so a STALE FAIL left
/// on the reverse signal from a prior session's non-compliant host cannot revoke a
/// fresh lock on a new, compliant host before its own probe completes.
pub fn compute_revoke_reason(s: RevalidationSignals) -> Option<RevokeReason> {
    const PROBE_RESULT_FAIL: u64 = 2;
    if s.probe_result_code == PROBE_RESULT_FAIL && s.probe_verdict_is_live {
        Some(RevokeReason::ProbeFail)
    } else if s.ladder_l2 {
        Some(RevokeReason::DllDemotion)
    } else if s.within_early_window && s.unlock_advanced {
        Some(RevokeReason::EarlyUnlock)
    } else {
        None
    }
}

/// The PURE lock-edge + revalidation tracker for a floor-primed session — the
/// wiring the mixer runs once per render period around [`compute_revoke_reason`].
/// Extracted from the mixer so the lock-edge bookkeeping (which is where the
/// early-unlock trigger's reachability lives) is testable WITHOUT ALSA: a test can
/// drive it with a real [`crate::lane_resampler::LaneResampler`]'s
/// `is_locked()` / `unlock_count()` sequence and confirm a simulated underfill
/// actually revokes.
///
/// The load-bearing ordering it encodes (the fix for the "early-unlock is dead
/// code" defect):
///  1. Read the rising (idle→lock) and falling (lock→underfill-unlock) edges from
///     `was_locked` BEFORE mutating it.
///  2. Run the revalidation against the PRE-reset baseline, treating the falling
///     edge as inside the early window — because `unlock_for_underfill` sets
///     `locked=false` in the same render period it bumps `unlock_count`, so the
///     ONLY period that carries the churn evidence is the one where `locked` is
///     already false. Gating `within_early_window` on `locked` alone (the original
///     bug) made that period invisible and the trigger unreachable.
///  3. THEN apply the lock-edge bookkeeping: a fresh lock re-arms the window,
///     unlock baseline, and clears the per-lock revoke latch (so a re-proven
///     session can revoke again — the latch is per-lock, not per-daemon-lifetime).
#[derive(Debug, Clone)]
pub struct RevalidationTracker {
    /// Whether the CURRENT lock was primed at the floor from a valid, unrevoked
    /// persisted proof — re-sampled from the live `flag_present` signal at EVERY
    /// rising edge (not fixed for the daemon lifetime), because the prime is now
    /// PER-SESSION: session B primes at the floor off session A's fresh proof, so
    /// it too must run the aggressive one-strike revalidation; a session after a
    /// revoke (proof cleared) primes from the ceiling and must NOT. The mixer
    /// passes the live value into [`step`](Self::step); this field latches it at
    /// each lock. Only a floor-primed lock runs the one-strike revalidation.
    floor_primed: bool,
    /// Revoked already since the most recent lock (revoke-at-most-once-per-lock).
    /// Cleared on every fresh lock so a re-proven session can strike again.
    revoked_this_lock: bool,
    /// The early-revalidation window in render periods.
    early_window_periods: u64,
    /// Render periods since the most recent lock, capped at the early window + 1.
    periods_since_lock: u64,
    /// The resampler `unlock_count` at the most recent lock — the churn baseline.
    unlock_baseline_at_lock: u64,
    /// The resampler `locked` state observed last period (lock-edge detection).
    was_locked: bool,
}

/// The tracker's decision for one render period: the revocation outcome plus the
/// lock edges it observed, so the mixer can drive the pure proof machine's per-lock
/// reset off the SAME edge detection (no duplicate `was_locked` bookkeeping).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RevalidationStep {
    /// Revoke the persisted proof now (snap back + delete + observability), or not.
    pub revoke: Option<RevokeReason>,
    /// This period was the idle→lock rising edge (a fresh session begins).
    pub rising_edge: bool,
    /// This period was the lock→unlock falling edge (the session ends).
    pub falling_edge: bool,
}

impl RevalidationTracker {
    /// Build a tracker. `floor_primed` is the INITIAL prime state (whether the
    /// build-time load primed the lane at the floor); it is re-sampled from the
    /// live `flag_present` at each rising edge in [`step`](Self::step), so this
    /// only seeds the value the first lock uses before any rising edge is observed.
    /// `early_window_periods` is the derived early-revalidation budget.
    pub fn new(floor_primed: bool, early_window_periods: u64) -> Self {
        Self {
            floor_primed,
            revoked_this_lock: false,
            early_window_periods,
            periods_since_lock: 0,
            unlock_baseline_at_lock: 0,
            was_locked: false,
        }
    }

    /// True iff a revocation has already fired since the most recent lock. The
    /// mixer relies on the `RevalidationStep::revoke` return to enact a revoke,
    /// never this accessor, so it is gated `#[cfg(test)]` to stay out of the
    /// `-D warnings` binary build (mirrors `ComplianceProof::written_this_session`).
    #[cfg(test)]
    pub fn revoked_this_lock(&self) -> bool {
        self.revoked_this_lock
    }

    /// Advance one render period. Reads the live resampler lock/unlock state plus
    /// the servo reverse signals, decides whether to revoke, and updates the
    /// lock-edge bookkeeping. Returns the revoke decision plus the observed edges so
    /// the mixer can drive the proof machine's per-lock reset off the same edges.
    /// The mixer enacts a returned `revoke` (snap-back + file delete +
    /// observability). Pure: no I/O, no clock.
    ///
    /// `floor_primed_now` is the LIVE proof-present signal (the same `flag_present`
    /// the snap-back honours and the revoke path clears). It is latched into
    /// `self.floor_primed` at the rising edge so each fresh lock's one-strike
    /// revalidation is armed exactly when — and only when — that lock actually
    /// primed at the floor: a session-B lock off session A's proof arms it; a lock
    /// after a revoke (proof cleared) does not.
    pub fn step(
        &mut self,
        locked: bool,
        unlock_count: u64,
        probe_result_code: u64,
        ladder_l2: bool,
        floor_primed_now: bool,
    ) -> RevalidationStep {
        let rising_edge = locked && !self.was_locked;
        let falling_edge = !locked && self.was_locked;

        let mut revoke = None;
        if self.floor_primed && !self.revoked_this_lock {
            // The early window spans the primed lock's first `early_window_periods`
            // render periods AND its immediate lock-loss period (the falling edge),
            // where the underfill unlock that ended the lock lands.
            let within_early_window =
                (locked || falling_edge) && self.periods_since_lock <= self.early_window_periods;
            revoke = compute_revoke_reason(RevalidationSignals {
                probe_result_code,
                // A LIVE probe FAIL always coincides with the ladder at L2; a stale
                // carryover FAIL sits in Probing (`ladder_l2 == false`) and is
                // ignored so a fresh lock on a new compliant host is not revoked on
                // a previous host's verdict.
                probe_verdict_is_live: ladder_l2,
                ladder_l2,
                within_early_window,
                unlock_advanced: unlock_count > self.unlock_baseline_at_lock,
            });
            if revoke.is_some() {
                self.revoked_this_lock = true;
            }
        }

        // Lock-edge bookkeeping AFTER the revalidation read. Latch the per-lock
        // prime state from the LIVE signal HERE (not before the read): the read
        // above intentionally runs against the PRE-reset baseline to catch the
        // falling-edge underfill, so re-sampling `floor_primed` before it would arm
        // the freshly-latched value against the PRIOR lock's stale unlock baseline
        // and spuriously revoke on the rising-edge period. A rising edge carries no
        // same-lock underfill (the lane just locked) and no fresh probe verdict yet,
        // so deferring the arm to here costs nothing — session B's early window runs
        // from its second period onward, which is where any real evidence lands.
        if rising_edge {
            self.periods_since_lock = 0;
            self.unlock_baseline_at_lock = unlock_count;
            self.revoked_this_lock = false;
            self.floor_primed = floor_primed_now;
        }
        self.was_locked = locked;
        if locked {
            self.periods_since_lock = self
                .periods_since_lock
                .saturating_add(1)
                .min(self.early_window_periods.saturating_add(1));
        }
        RevalidationStep {
            revoke,
            rising_edge,
            falling_edge,
        }
    }
}

/// Live host-compliance state for STATUS (`resampler.compliance`). Shared
/// (`Arc`) between the mixer thread (single writer) and the state-server thread
/// (reader), mirroring the resampler's other observability atomics. `Some` on the
/// resampler observability only when the feature is armed; `None` (no block) when
/// it is off — byte-identical to today's STATUS.
///
/// `Clone` clones the `Arc` handles (cheap, shared state — same semantics as
/// [`clone_handles`](Self::clone_handles)). Required because the enclosing
/// `LaneResamplerObservability` derives `Clone` for the STATUS snapshot.
#[derive(Debug, Clone)]
pub struct HostComplianceObservability {
    /// Whether a persisted proof is currently believed present on disk: `true`
    /// after a successful write (or a valid load at build), `false` after a
    /// revoke / when none was ever written. Reflects the mixer's own actions, not
    /// a live `stat()` — the daemon is the only writer.
    pub flag_present: Arc<AtomicBool>,
    /// The `proved_at_epoch_s` of the current proof (0 when absent).
    pub proved_at_epoch_s: Arc<AtomicU64>,
    /// The last revoke reason as a stable code (`0` none, else
    /// [`RevokeReason`]'s discriminant + 1: 1 probe_fail, 2 dll_demotion, 3
    /// early_unlock). Sticky for the daemon lifetime — the last strike stays
    /// visible for a post-mortem even after a re-prove.
    pub revoked_reason_last_code: Arc<AtomicU64>,
}

impl HostComplianceObservability {
    /// A fresh observable seeded from the boot-time load: `flag_present` reflects
    /// whether a valid proof was loaded, `proved_at` its timestamp, and no revoke
    /// has happened yet.
    pub fn new(flag_present: bool, proved_at_epoch_s: u64) -> Self {
        Self {
            flag_present: Arc::new(AtomicBool::new(flag_present)),
            proved_at_epoch_s: Arc::new(AtomicU64::new(proved_at_epoch_s)),
            revoked_reason_last_code: Arc::new(AtomicU64::new(0)),
        }
    }

    /// Clone the `Arc` handles (cheap) for the resampler observability snapshot.
    pub fn clone_handles(&self) -> Self {
        Self {
            flag_present: Arc::clone(&self.flag_present),
            proved_at_epoch_s: Arc::clone(&self.proved_at_epoch_s),
            revoked_reason_last_code: Arc::clone(&self.revoked_reason_last_code),
        }
    }

    /// Record a successful proof write.
    pub fn on_written(&self, proved_at_epoch_s: u64) {
        self.proved_at_epoch_s
            .store(proved_at_epoch_s, Ordering::Relaxed);
        self.flag_present.store(true, Ordering::Relaxed);
    }

    /// Record a revocation (flag cleared, reason recorded).
    pub fn on_revoked(&self, reason: RevokeReason) {
        self.flag_present.store(false, Ordering::Relaxed);
        self.revoked_reason_last_code
            .store(revoke_reason_code(reason), Ordering::Relaxed);
    }
}

/// The STATUS wire code for a [`RevokeReason`] (`1` probe_fail, `2` dll_demotion,
/// `3` early_unlock; `0` reserved for "no revoke"). Append, never renumber.
pub fn revoke_reason_code(reason: RevokeReason) -> u64 {
    match reason {
        RevokeReason::ProbeFail => 1,
        RevokeReason::DllDemotion => 2,
        RevokeReason::EarlyUnlock => 3,
    }
}

/// Map a STATUS revoke-reason code back to its token (`""` for `0`/unknown).
pub fn revoke_reason_code_str(code: u64) -> &'static str {
    match code {
        1 => "probe_fail",
        2 => "dll_demotion",
        3 => "early_unlock",
        _ => "",
    }
}

/// The PURE per-session proof state machine. Ticked once per render period by the
/// mixer while a compliance-capable session is running; owns the write gate
/// (descent-complete + sustained l0 + zero-unlock-delta) and emits a single
/// [`WriteProof`] request when the full proof lands. It does NO I/O — the mixer
/// performs the write/delete when the machine asks.
///
/// One instance per lane, reset (via [`ComplianceProof::reset`]) whenever the
/// resampler loses lock so a fresh session re-earns the proof from scratch.
#[derive(Debug, Clone)]
pub struct ComplianceProof {
    /// Render periods that must pass with `decay_at_floor && dll_l0_locked && no
    /// new unlock` before the proof is written — the settle window. Derived from
    /// the same stability-window ms the decay's warm-up uses, so the two share one
    /// "how long is stable long enough" number.
    settle_periods: u64,
    /// Consecutive settle periods accrued so far (resets on any disqualifier).
    settled_periods: u64,
    /// The `unlock_count` observed when the settle window most recently (re)armed.
    /// The proof requires the live count to still equal this at the end of the
    /// window — any increment is churn and disqualifies.
    unlock_baseline: Option<u64>,
    /// True once the proof has been written this session — the "write at most
    /// once per session" latch. Cleared only by [`reset`](Self::reset).
    written_this_session: bool,
}

/// The mixer's marching order from a [`ComplianceProof::tick`]: write the proof
/// now (the full gate is satisfied and it hasn't been written this session), or
/// do nothing.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum ProofOutcome {
    /// Keep watching — the proof is not (yet) complete this period.
    Pending,
    /// The full proof landed: write a record with this response ratio + floor.
    Write {
        /// The probe response ratio to record (0.0 if the servo had no verdict —
        /// should not happen at l0, but recorded honestly rather than assumed).
        probe_response_ratio: f64,
    },
}

impl ComplianceProof {
    /// Build the machine. `settle_periods` is the sustained-at-floor window in
    /// render periods (the caller converts ms → periods once, mirroring the decay
    /// build). Clamped to ≥ 1 so a tiny value still requires one clean period.
    pub fn new(settle_periods: u64) -> Self {
        Self {
            settle_periods: settle_periods.max(1),
            settled_periods: 0,
            unlock_baseline: None,
            written_this_session: false,
        }
    }

    /// Reset for a fresh session (called from the lock-loss paths, mirroring the
    /// decay snap-back): clear the settle progress, the unlock baseline, and the
    /// written latch so the next descent re-earns the proof independently.
    pub fn reset(&mut self) {
        self.settled_periods = 0;
        self.unlock_baseline = None;
        self.written_this_session = false;
    }

    /// True iff the proof has already been written this session (no more writes
    /// until reset). Test-only: the mixer relies on the `ProofOutcome::Write`
    /// return to know a write happened, never this accessor, so gating it
    /// `#[cfg(test)]` keeps it out of the `-D warnings` binary build (mirrors
    /// `lane_resampler::decay::DecayParams::disabled`).
    #[cfg(test)]
    pub fn written_this_session(&self) -> bool {
        self.written_this_session
    }

    /// Advance one render period. Returns [`ProofOutcome::Write`] exactly once per
    /// session — on the first period where the FULL proof is satisfied:
    /// (1) the decay is at its floor (descent complete); (2) the DLL has held
    /// `l0_locked` for `settle_periods` consecutive periods at the floor; and
    /// (3) the resampler's unlock count has not advanced across that window (zero
    /// churn over the descent-to-settle).
    ///
    /// Any disqualifier (not at floor, l0 lost, a new unlock) re-arms the window.
    /// Pure: no clock, no I/O.
    pub fn tick(&mut self, s: ProofSignals) -> ProofOutcome {
        if self.written_this_session {
            return ProofOutcome::Pending;
        }
        // Disqualifiers first: any loss of the steady floor regime re-arms the
        // settle window from zero and forgets the unlock baseline, so the next
        // clean run measures a fresh zero-unlock window.
        if !s.decay_at_floor || !s.dll_l0_locked {
            self.settled_periods = 0;
            self.unlock_baseline = None;
            return ProofOutcome::Pending;
        }
        // At floor + l0. Arm the unlock baseline on the first such period, then
        // require the count to stay pinned to it: any increment is lock churn and
        // disqualifies the whole window (re-arm).
        match self.unlock_baseline {
            None => {
                self.unlock_baseline = Some(s.unlock_count);
                self.settled_periods = 1;
            }
            Some(baseline) if s.unlock_count != baseline => {
                // A new unlock during the window → churn. Re-arm from this period,
                // re-baselining to the new (higher) count.
                self.unlock_baseline = Some(s.unlock_count);
                self.settled_periods = 1;
            }
            Some(_) => {
                self.settled_periods = self.settled_periods.saturating_add(1);
            }
        }
        if self.settled_periods < self.settle_periods {
            return ProofOutcome::Pending;
        }
        // Full proof: descent complete + sustained clean l0 + zero unlock delta.
        self.written_this_session = true;
        ProofOutcome::Write {
            probe_response_ratio: s.probe_response_ratio.unwrap_or(0.0),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // ---- HostCompliance record + I/O --------------------------------------

    #[test]
    fn record_roundtrips_through_json() {
        let rec = HostCompliance::new(1_700_000_000, 1.66, 576);
        let bytes = rec.to_json_bytes().expect("serialize");
        let back = HostCompliance::from_json_bytes(&bytes).expect("parse");
        assert_eq!(rec, back);
    }

    #[test]
    fn record_carries_schema_1() {
        let rec = HostCompliance::new(0, 0.0, 0);
        assert_eq!(rec.schema, SCHEMA_VERSION);
        assert_eq!(SCHEMA_VERSION, 1);
    }

    #[test]
    fn corrupt_bytes_parse_to_none() {
        assert!(HostCompliance::from_json_bytes(b"").is_none());
        assert!(HostCompliance::from_json_bytes(b"not json").is_none());
        assert!(HostCompliance::from_json_bytes(b"{\"schema\":1").is_none());
        // Valid JSON, wrong shape (missing required fields) → None.
        assert!(HostCompliance::from_json_bytes(b"{\"schema\":1}").is_none());
    }

    #[test]
    fn wrong_schema_parses_to_none() {
        // A record from a future/rolled-back schema must not mis-prime.
        let json = br#"{"schema":999,"proved_at_epoch_s":0,"probe_response_ratio":1.0,"floor_frames":576,"consecutive_failures":0}"#;
        assert!(HostCompliance::from_json_bytes(json).is_none());
    }

    #[test]
    fn valid_for_requires_matching_floor_and_schema() {
        let rec = HostCompliance::new(1, 1.66, 576);
        assert!(rec.valid_for(576), "matching floor is valid");
        assert!(!rec.valid_for(544), "a different live floor is stale");
        // A schema mismatch would have failed the parse, but guard defensively.
        let mut bad = rec.clone();
        bad.schema = 2;
        assert!(!bad.valid_for(576));
    }

    #[test]
    fn missing_file_loads_as_none() {
        let dir = std::env::temp_dir().join(format!("jts-compl-{}", std::process::id()));
        let path = dir.join("does_not_exist.json");
        assert!(HostCompliance::load(&path).is_none());
    }

    #[test]
    fn store_then_load_roundtrips_atomically() {
        let dir = std::env::temp_dir().join(format!("jts-compl-store-{}", std::process::id()));
        let path = dir.join("nested").join("host_compliance.json");
        let rec = HostCompliance::new(42, 1.66, 576);
        rec.store(&path).expect("store");
        // The tempfile must not linger after a successful rename.
        assert!(!tmp_path(&path).exists(), "tempfile removed by rename");
        let back = HostCompliance::load(&path).expect("load");
        assert_eq!(rec, back);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn revoke_deletes_and_is_idempotent() {
        let dir = std::env::temp_dir().join(format!("jts-compl-rev-{}", std::process::id()));
        let path = dir.join("host_compliance.json");
        HostCompliance::new(1, 1.0, 576)
            .store(&path)
            .expect("store");
        assert!(path.exists());
        HostCompliance::revoke(&path).expect("revoke");
        assert!(!path.exists(), "revoke deletes the file");
        // Revoking an absent file is success (idempotent).
        HostCompliance::revoke(&path).expect("revoke idempotent");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn corrupt_file_on_disk_loads_as_none() {
        let dir = std::env::temp_dir().join(format!("jts-compl-corrupt-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("host_compliance.json");
        std::fs::write(&path, b"{ this is not valid json").unwrap();
        assert!(
            HostCompliance::load(&path).is_none(),
            "a corrupt file must load as None (fail toward no-flag)"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    // ---- ComplianceProof state machine ------------------------------------

    const SETTLE: u64 = 5;

    fn floor_l0(unlock_count: u64) -> ProofSignals {
        ProofSignals {
            decay_at_floor: true,
            dll_l0_locked: true,
            unlock_count,
            probe_response_ratio: Some(1.66),
        }
    }

    #[test]
    fn writes_once_after_settle_at_floor_with_zero_unlock_delta() {
        let mut p = ComplianceProof::new(SETTLE);
        // Below the window: pending.
        for _ in 0..SETTLE - 1 {
            assert_eq!(p.tick(floor_l0(3)), ProofOutcome::Pending);
        }
        // The SETTLE-th clean period fires the write.
        match p.tick(floor_l0(3)) {
            ProofOutcome::Write {
                probe_response_ratio,
            } => assert_eq!(probe_response_ratio, 1.66),
            other => panic!("expected Write, got {other:?}"),
        }
        assert!(p.written_this_session());
        // Never writes twice in one session.
        for _ in 0..100 {
            assert_eq!(p.tick(floor_l0(3)), ProofOutcome::Pending);
        }
    }

    #[test]
    fn a_single_unlock_during_the_window_disqualifies_and_rearms() {
        let mut p = ComplianceProof::new(SETTLE);
        // Accrue partway with unlock_count pinned at 2.
        for _ in 0..SETTLE - 1 {
            assert_eq!(p.tick(floor_l0(2)), ProofOutcome::Pending);
        }
        // An unlock bumps the count → re-arm; this period is period 1 of a NEW
        // window, so no write even though we'd otherwise have hit SETTLE.
        assert_eq!(p.tick(floor_l0(3)), ProofOutcome::Pending);
        // Need SETTLE-1 MORE clean periods at the new baseline to write.
        for _ in 0..SETTLE - 2 {
            assert_eq!(p.tick(floor_l0(3)), ProofOutcome::Pending);
        }
        assert!(
            matches!(p.tick(floor_l0(3)), ProofOutcome::Write { .. }),
            "after the re-armed clean window completes, the proof writes"
        );
    }

    #[test]
    fn losing_floor_rearms_the_window() {
        let mut p = ComplianceProof::new(SETTLE);
        for _ in 0..SETTLE - 1 {
            p.tick(floor_l0(0));
        }
        // Decay leaves the floor (e.g. a snap-back raised the held target).
        let off_floor = ProofSignals {
            decay_at_floor: false,
            ..floor_l0(0)
        };
        assert_eq!(p.tick(off_floor), ProofOutcome::Pending);
        // Back at floor: must re-earn the FULL window.
        for _ in 0..SETTLE - 1 {
            assert_eq!(p.tick(floor_l0(0)), ProofOutcome::Pending);
        }
        assert!(matches!(p.tick(floor_l0(0)), ProofOutcome::Write { .. }));
    }

    #[test]
    fn losing_l0_rearms_the_window() {
        let mut p = ComplianceProof::new(SETTLE);
        for _ in 0..SETTLE - 1 {
            p.tick(floor_l0(0));
        }
        let demoted = ProofSignals {
            dll_l0_locked: false,
            ..floor_l0(0)
        };
        assert_eq!(p.tick(demoted), ProofOutcome::Pending);
        for _ in 0..SETTLE - 1 {
            assert_eq!(p.tick(floor_l0(0)), ProofOutcome::Pending);
        }
        assert!(matches!(p.tick(floor_l0(0)), ProofOutcome::Write { .. }));
    }

    #[test]
    fn reset_forgets_the_written_latch_and_progress() {
        let mut p = ComplianceProof::new(SETTLE);
        for _ in 0..SETTLE {
            p.tick(floor_l0(0));
        }
        assert!(p.written_this_session());
        p.reset();
        assert!(!p.written_this_session());
        // A fresh session can write again after settling.
        for _ in 0..SETTLE - 1 {
            assert_eq!(p.tick(floor_l0(1)), ProofOutcome::Pending);
        }
        assert!(matches!(p.tick(floor_l0(1)), ProofOutcome::Write { .. }));
    }

    #[test]
    fn missing_probe_ratio_records_zero() {
        let mut p = ComplianceProof::new(SETTLE);
        let no_probe = ProofSignals {
            probe_response_ratio: None,
            ..floor_l0(0)
        };
        for _ in 0..SETTLE - 1 {
            assert_eq!(p.tick(no_probe), ProofOutcome::Pending);
        }
        match p.tick(no_probe) {
            ProofOutcome::Write {
                probe_response_ratio,
            } => assert_eq!(probe_response_ratio, 0.0),
            other => panic!("expected Write, got {other:?}"),
        }
    }

    #[test]
    fn revoke_reason_tokens_are_stable() {
        assert_eq!(RevokeReason::ProbeFail.as_str(), "probe_fail");
        assert_eq!(RevokeReason::DllDemotion.as_str(), "dll_demotion");
        assert_eq!(RevokeReason::EarlyUnlock.as_str(), "early_unlock");
    }

    fn healthy() -> RevalidationSignals {
        RevalidationSignals {
            probe_result_code: 1, // pass
            probe_verdict_is_live: true,
            ladder_l2: false,
            within_early_window: true,
            unlock_advanced: false,
        }
    }

    #[test]
    fn compute_revoke_reason_healthy_session_never_revokes() {
        assert_eq!(compute_revoke_reason(healthy()), None);
        // Outside the early window, an unlock alone is not a revoke either.
        let late = RevalidationSignals {
            within_early_window: false,
            unlock_advanced: true,
            ..healthy()
        };
        assert_eq!(compute_revoke_reason(late), None);
    }

    #[test]
    fn compute_revoke_reason_probe_fail_takes_precedence() {
        // A fresh (LIVE) probe FAIL (code 2) revokes as ProbeFail even if L2 is also
        // set. A live probe fail always coincides with L2, so both are set here.
        let s = RevalidationSignals {
            probe_result_code: 2,
            probe_verdict_is_live: true,
            ladder_l2: true,
            ..healthy()
        };
        assert_eq!(compute_revoke_reason(s), Some(RevokeReason::ProbeFail));
    }

    #[test]
    fn compute_revoke_reason_ignores_stale_probe_fail() {
        // A STALE probe FAIL (code 2) that was NOT produced during the current lock
        // must not revoke: the servo leaves `probe_result=Fail` across a session
        // boundary, and a fresh lock on a NEW compliant host would otherwise read
        // that carryover and revoke before its own probe runs. `probe_verdict_is_live`
        // is false (the ladder is back in Probing, `ladder_l2=false`), so no revoke.
        let stale = RevalidationSignals {
            probe_result_code: 2,
            probe_verdict_is_live: false,
            ladder_l2: false,
            within_early_window: true,
            unlock_advanced: false,
        };
        assert_eq!(compute_revoke_reason(stale), None);
        // The SAME stale FAIL code, still not corroborated by L2, does not revoke
        // even inside the early window with no unlock — nothing else fires.
        assert_eq!(
            compute_revoke_reason(RevalidationSignals {
                unlock_advanced: false,
                ..stale
            }),
            None
        );
    }

    #[test]
    fn compute_revoke_reason_l2_demotion_after_pass() {
        // No fresh probe fail, but the DLL demoted mid-stream → DllDemotion.
        let s = RevalidationSignals {
            probe_result_code: 1,
            ladder_l2: true,
            ..healthy()
        };
        assert_eq!(compute_revoke_reason(s), Some(RevokeReason::DllDemotion));
    }

    #[test]
    fn compute_revoke_reason_early_unlock_only_inside_window() {
        // An unlock inside the early window revokes as EarlyUnlock.
        let inside = RevalidationSignals {
            within_early_window: true,
            unlock_advanced: true,
            ..healthy()
        };
        assert_eq!(
            compute_revoke_reason(inside),
            Some(RevokeReason::EarlyUnlock)
        );
        // The SAME unlock outside the window does NOT revoke (the probe already
        // ran and passed; a much-later unlock is ordinary transient churn).
        let outside = RevalidationSignals {
            within_early_window: false,
            unlock_advanced: true,
            ..healthy()
        };
        assert_eq!(compute_revoke_reason(outside), None);
    }

    #[test]
    fn revoke_reason_status_codes_roundtrip() {
        // The STATUS codes are a wire contract: 0 none, then 1/2/3.
        assert_eq!(revoke_reason_code_str(0), "");
        for r in [
            RevokeReason::ProbeFail,
            RevokeReason::DllDemotion,
            RevokeReason::EarlyUnlock,
        ] {
            let code = revoke_reason_code(r);
            assert_ne!(code, 0);
            assert_eq!(revoke_reason_code_str(code), r.as_str());
        }
    }

    #[test]
    fn observability_tracks_write_and_revoke() {
        let obs = HostComplianceObservability::new(false, 0);
        assert!(!obs.flag_present.load(Ordering::Relaxed));
        obs.on_written(1_700_000_000);
        assert!(obs.flag_present.load(Ordering::Relaxed));
        assert_eq!(obs.proved_at_epoch_s.load(Ordering::Relaxed), 1_700_000_000);
        obs.on_revoked(RevokeReason::EarlyUnlock);
        assert!(!obs.flag_present.load(Ordering::Relaxed));
        assert_eq!(
            obs.revoked_reason_last_code.load(Ordering::Relaxed),
            revoke_reason_code(RevokeReason::EarlyUnlock)
        );
    }

    #[test]
    fn settle_periods_clamped_to_at_least_one() {
        let mut p = ComplianceProof::new(0);
        // One clean period at floor writes immediately (clamp floor is 1).
        assert!(matches!(p.tick(floor_l0(0)), ProofOutcome::Write { .. }));
    }

    // ---- RevalidationTracker (lock-edge + one-strike wiring) ---------------

    const EARLY_WINDOW: u64 = 20;

    /// A locked healthy tick (probe passed, ladder L0, no churn). Returns the four
    /// resampler/servo args; the fifth `step` arg (`floor_primed_now`) is passed
    /// explicitly at each call site to make the per-lock prime state visible.
    fn ok_lock(unlock_count: u64) -> (bool, u64, u64, bool) {
        // (locked, unlock_count, probe_code=pass, ladder_l2=false)
        (true, unlock_count, 1, false)
    }

    #[test]
    fn tracker_cold_session_never_revokes() {
        // A NOT-floor-primed session has no persisted authority to distrust — even
        // an underfill unlock inside the early window just re-earns the proof.
        // `floor_primed_now=false` (no live proof) is the live signal for a cold lane.
        let mut t = RevalidationTracker::new(false, EARLY_WINDOW);
        let (l, u, p, l2) = ok_lock(0);
        assert_eq!(t.step(l, u, p, l2, false).revoke, None);
        // Underfill unlock (falling edge, count advances): still no revoke — cold.
        assert_eq!(t.step(false, 1, 1, false, false).revoke, None);
    }

    #[test]
    fn tracker_early_underfill_unlock_revokes_on_the_falling_edge() {
        // THE BLOCKER FIX: a floor-primed session that locks, then underfill-unlocks
        // inside the early window, revokes as EarlyUnlock — even though the unlock is
        // observed on the SAME period `locked` flips to false (the falling edge).
        let mut t = RevalidationTracker::new(true, EARLY_WINDOW);
        // Lock cleanly for a few periods (baseline unlock_count = 0). The live proof
        // is present, so `floor_primed_now=true` every period.
        for _ in 0..3 {
            let (l, u, p, l2) = ok_lock(0);
            assert_eq!(t.step(l, u, p, l2, true).revoke, None);
        }
        // Underfill: the resampler set locked=false AND bumped unlock_count in the
        // same render period. The tracker must catch this falling-edge unlock.
        let step = t.step(false, 1, 1, false, true);
        assert_eq!(
            step.revoke,
            Some(RevokeReason::EarlyUnlock),
            "a floor-primed early underfill unlock must revoke on the falling edge"
        );
        assert!(step.falling_edge);
        assert!(t.revoked_this_lock());
    }

    #[test]
    fn tracker_underfill_after_early_window_does_not_revoke() {
        // Past the early window, a single underfill unlock is ordinary transient
        // churn (the probe already ran and passed) — not a revoke.
        let mut t = RevalidationTracker::new(true, EARLY_WINDOW);
        // Hold lock past the early window (live proof present every period).
        for _ in 0..(EARLY_WINDOW + 5) {
            let (l, u, p, l2) = ok_lock(0);
            assert_eq!(t.step(l, u, p, l2, true).revoke, None);
        }
        // Now underfill: outside the window, EarlyUnlock does not fire.
        assert_eq!(t.step(false, 1, 1, false, true).revoke, None);
    }

    #[test]
    fn tracker_relock_after_in_window_revoke_does_not_double_revoke() {
        // The revalidation runs BEFORE the rising-edge re-baseline, so a relock must
        // not see the just-revoked lock's advanced unlock count against the OLD
        // baseline and fire a second EarlyUnlock. The per-lock latch (set by the
        // falling-edge revoke, still true on the relock's rising-edge period, cleared
        // only after that period's revalidation read) is what prevents it.
        let mut t = RevalidationTracker::new(true, EARLY_WINDOW);
        // Lock, then an in-window underfill → revoke #1 on the falling edge. The
        // proof is still live on the relock (the mixer clears it via `on_revoked`
        // only AFTER `step` returns the revoke), so `floor_primed_now=true` here;
        // the per-lock latch — not the prime flag — is what blocks the double-revoke.
        for _ in 0..3 {
            assert_eq!(t.step(true, 0, 1, false, true).revoke, None);
        }
        assert_eq!(
            t.step(false, 1, 1, false, true).revoke,
            Some(RevokeReason::EarlyUnlock)
        );
        assert!(t.revoked_this_lock());
        // Re-prime + relock (rising edge) with unlock_count STILL advanced past the
        // old baseline. The rising-edge period must NOT double-revoke; the latch
        // clears only after this period's (skipped) revalidation.
        let relock = t.step(true, 1, 1, false, true);
        assert_eq!(
            relock.revoke, None,
            "the relock rising edge must not double-revoke off the old unlock baseline"
        );
        assert!(relock.rising_edge);
        assert!(!t.revoked_this_lock(), "the latch clears on the fresh lock");
        // A clean re-proven session then runs quietly (baseline re-armed to 1).
        assert_eq!(t.step(true, 1, 1, false, true).revoke, None);
    }

    #[test]
    fn tracker_live_probe_fail_revokes() {
        // A LIVE probe FAIL (code 2 corroborated by ladder L2) revokes as ProbeFail.
        let mut t = RevalidationTracker::new(true, EARLY_WINDOW);
        assert_eq!(t.step(true, 0, 1, false, true).revoke, None);
        let step = t.step(true, 0, 2, true, true);
        assert_eq!(step.revoke, Some(RevokeReason::ProbeFail));
    }

    #[test]
    fn tracker_ignores_stale_probe_fail_on_fresh_lock() {
        // The Finding-2 stale-verdict trap: a fresh lock on a new compliant host
        // must NOT revoke on a previous session's carried-over FAIL. The servo
        // leaves `probe_result=Fail` across a session boundary but the ladder is
        // back in Probing (`ladder_l2=false`), so the FAIL is not live and no revoke
        // fires.
        let mut t = RevalidationTracker::new(true, EARLY_WINDOW);
        // Fresh lock: probe_code is a STALE 2 but ladder_l2 is false (re-probing).
        for _ in 0..5 {
            assert_eq!(
                t.step(true, 0, 2, false, true).revoke,
                None,
                "a stale (not-L2-corroborated) probe FAIL must not revoke a fresh lock"
            );
        }
        // When the new probe finishes and passes, all clear — still no revoke.
        assert_eq!(t.step(true, 0, 1, false, true).revoke, None);
    }

    #[test]
    fn tracker_revoke_latch_resets_on_relock() {
        // Finding 2: the per-lock revoke latch must reset on a fresh lock so a
        // re-proven session can strike again if the host later misbehaves — it is
        // NOT a daemon-lifetime latch.
        let mut t = RevalidationTracker::new(true, EARLY_WINDOW);
        // Lock, then a live probe fail → revoke #1. Proof stays live across the
        // relock here (this test exercises the latch, not the proof lifecycle), so
        // `floor_primed_now=true` throughout.
        assert_eq!(t.step(true, 0, 1, false, true).revoke, None);
        assert_eq!(
            t.step(true, 0, 2, true, true).revoke,
            Some(RevokeReason::ProbeFail)
        );
        assert!(t.revoked_this_lock());
        // Lose lock (session ends), then re-lock (rising edge) — latch clears.
        let _ = t.step(false, 0, 1, false, true);
        let relock = t.step(true, 0, 1, false, true);
        assert!(relock.rising_edge);
        assert!(
            !t.revoked_this_lock(),
            "the revoke latch resets on a fresh lock"
        );
        // A new live probe fail on the RE-locked session revokes again.
        assert_eq!(
            t.step(true, 0, 2, true, true).revoke,
            Some(RevokeReason::ProbeFail),
            "a re-proven session can strike again — the latch is per-lock"
        );
    }

    #[test]
    fn tracker_floor_primed_is_resampled_per_lock_from_the_live_signal() {
        // PER-SESSION: `floor_primed` is re-sampled from the LIVE proof signal at
        // each rising edge, NOT fixed for the daemon lifetime. So:
        //  - session A (cold, no proof yet) does NOT run the one-strike revalidation
        //    — an early underfill just re-earns the proof;
        //  - session B (floor-primed off A's fresh proof) DOES revalidate — the same
        //    early underfill revokes.
        // The tracker is constructed `floor_primed=false` (no boot proof); the live
        // value is what each rising edge latches.
        let mut t = RevalidationTracker::new(false, EARLY_WINDOW);

        // Session A: lock with the live proof STILL ABSENT (floor_primed_now=false).
        for _ in 0..3 {
            assert_eq!(t.step(true, 0, 1, false, false).revoke, None);
        }
        // An early underfill on the cold session must NOT revoke — nothing to
        // distrust; the session is proving from scratch.
        assert_eq!(
            t.step(false, 1, 1, false, false).revoke,
            None,
            "a cold (not floor-primed) session end never revokes"
        );

        // Session A wrote its proof; session B locks with the proof now LIVE
        // (floor_primed_now=true). The rising edge latches floor_primed=true (armed
        // from the second locked period onward); these clean periods do not revoke.
        for _ in 0..3 {
            assert_eq!(t.step(true, 1, 1, false, true).revoke, None);
        }
        // The SAME early underfill now revokes — session B was floor-primed.
        assert_eq!(
            t.step(false, 2, 1, false, true).revoke,
            Some(RevokeReason::EarlyUnlock),
            "a floor-primed session B (proof live at its rising edge) revalidates and revokes"
        );
    }

    #[test]
    fn tracker_session_after_revoke_does_not_revalidate() {
        // After a revoke clears the proof, the NEXT session locks with the live
        // signal false, so its rising edge latches floor_primed=false and it runs
        // NO one-strike revalidation — it descends + re-proves like any cold lane.
        let mut t = RevalidationTracker::new(true, EARLY_WINDOW);
        // Floor-primed session: a live probe fail revokes.
        assert_eq!(t.step(true, 0, 1, false, true).revoke, None);
        assert_eq!(
            t.step(true, 0, 2, true, true).revoke,
            Some(RevokeReason::ProbeFail)
        );
        // Session ends; the mixer cleared flag_present on the revoke, so the next
        // lock sees floor_primed_now=false.
        let _ = t.step(false, 0, 1, false, false);
        for _ in 0..3 {
            assert_eq!(t.step(true, 0, 1, false, false).revoke, None);
        }
        // An early underfill on this re-proving session does NOT revoke.
        assert_eq!(
            t.step(false, 1, 1, false, false).revoke,
            None,
            "the session after a revoke is not floor-primed, so it never revokes"
        );
    }

    // ---- Faithful wiring test: a REAL resampler underfill drives a revoke -----

    #[test]
    fn real_resampler_underfill_drives_early_unlock_revoke() {
        // The wiring-level regression the BLOCKER demands: build a REAL
        // `LaneResampler`, lock it, feed the RevalidationTracker the resampler's OWN
        // `is_locked()` / `unlock_count()` each period exactly as the mixer does, then
        // starve the input so the resampler underfill-unlocks — and assert the
        // tracker actually revokes. This proves the input combination the pure tests
        // use (`falling_edge` + `unlock_advanced`) is one the wiring PRODUCES, not a
        // synthetic one the plumbing can never reach.
        use crate::lane_resampler::{DecayParams, LaneResampler};

        const RATE: u32 = 48_000;
        const PERIOD: u32 = 256;
        const TARGET: usize = 512;
        const CUSHION: usize = PERIOD as usize;
        const MAX_PPM: f64 = 500.0;
        const RING: usize = 8192;

        let mut r = LaneResampler::new(
            2,
            PERIOD,
            RATE,
            TARGET,
            CUSHION,
            MAX_PPM,
            RING,
            DecayParams::disabled(),
        )
        .expect("resampler builds");
        // Simulate a floor-primed session's tracker (the mixer sets floor_primed from
        // a valid persisted proof; here we assert the tracker treats this lane as
        // primed so the one-strike revalidation is armed).
        let mut t = RevalidationTracker::new(true, EARLY_WINDOW);

        // Deterministic stereo tone.
        let tone = |frames: usize| -> Vec<i16> {
            let mut v = Vec::with_capacity(frames * 2);
            for n in 0..frames {
                let x = ((n as f64) * 0.02).sin();
                let s = (x * 8000.0) as i16;
                v.push(s);
                v.push(s);
            }
            v
        };
        let mut out = vec![0i16; PERIOD as usize * 2];

        // Prefill deep enough to lock, then render one period → the lane locks.
        let deep = TARGET + CUSHION + 8 + 1;
        r.push_input(&tone(deep + 64));
        assert_eq!(
            r.render_period(&mut out),
            PERIOD as usize,
            "locks + renders"
        );
        assert!(r.is_locked(), "the lane is locked after the deep prefill");
        let baseline_unlocks = r.unlock_count();

        // The mixer's exact per-period call: read the resampler's live state, then
        // step the tracker. `floor_primed_now=true` — this session primed at the
        // floor (the live proof is present). A healthy locked period → no revoke.
        let step = t.step(r.is_locked(), r.unlock_count(), 1, false, true);
        assert_eq!(step.revoke, None, "a healthy locked period does not revoke");

        // Feed one more sub-period of input, render a few clean periods (still
        // locked, still inside the early window), tracker stays quiet.
        for _ in 0..3 {
            r.push_input(&tone(PERIOD as usize));
            r.render_period(&mut out);
            assert!(r.is_locked());
            assert_eq!(
                t.step(r.is_locked(), r.unlock_count(), 1, false, true)
                    .revoke,
                None
            );
        }

        // Now STARVE the lane: render with no fresh input until the cursor outruns
        // the buffered frames → `render_period` calls `unlock_for_underfill`, which
        // sets locked=false AND bumps unlock_count IN THE SAME period. This is the
        // exact falling-edge the BLOCKER showed the old wiring could not catch.
        let mut revoked = None;
        for _ in 0..64 {
            r.render_period(&mut out);
            let s = t.step(r.is_locked(), r.unlock_count(), 1, false, true);
            if let Some(reason) = s.revoke {
                revoked = Some(reason);
                // The revoke must land on the period the resampler unlocked.
                assert!(!r.is_locked(), "revoke coincides with the unlock");
                assert!(
                    r.unlock_count() > baseline_unlocks,
                    "the underfill actually advanced the unlock count"
                );
                break;
            }
        }
        assert_eq!(
            revoked,
            Some(RevokeReason::EarlyUnlock),
            "a real resampler underfill inside the early window must revoke via the \
             tracker — the EarlyUnlock trigger is reachable through the live wiring"
        );
    }
}
