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
//! ## Strike policy — one strike for evidence, two for a measurement
//!
//! A floor-primed session that misbehaves is *usually* evidence the host on THIS
//! port is no longer (or was never) the compliant host the proof was written for
//! — a replug to a different machine, a different USB port, an OS that stopped
//! honouring the ctl. For DIRECT floor-failure evidence — a DLL demotion
//! (saturated wrong-way slope) or a CONFIRMED unlock→relock churn cycle — the
//! safe response is to distrust the proof immediately (ONE strike): delete it,
//! snap back to the ceiling, and let the normal descent re-prove from scratch.
//!
//! A probe FAIL is different: it is a MEASUREMENT, and the lock-gated probe can
//! spuriously fail if it runs while the resampler's correction is railed (the
//! jts.local 2026-07-03 false-fail — a floor-primed session whose held target
//! snapped to the ceiling post-lock railed at −500 ppm while the DLL rebuilt the
//! fill, so the probe read baseline ≈ step ≈ −500 → response_ratio ≈ 0 → FAIL; that
//! specific rail's mechanism — the post-lock `NotL0` snap — is now root-FIXED by the
//! prime-aware `NotL0` hold (#1161), so it no longer occurs). This TWO-strike
//! tolerance — NOT a settle-time rail guard — is what keeps a residual spurious
//! probe read from costing the floor: an earlier CORRECTION-mode unrailed-settle
//! guard in `jasper-host-clock` also targeted this rail, but was REMOVED 2026-07-05
//! (it deadlocked beyond-authority hosts whose correction rails steady-state), so
//! do NOT rely on it here. Costing the household the ~2.5-min descent on ONE
//! ambiguous read is the wrong trade. So a probe fail is TWO-strike (see
//! [`classify_strike`] / [`PROBE_FAIL_STRIKE_LIMIT`]): the first fail RETAINS the
//! proof but persists an incremented `consecutive_failures` (and `flag_present`
//! stays true, so the NEXT session still primes at the floor); only the SECOND
//! consecutive probe fail — two independent sessions disagreeing with the proof,
//! which IS a host change worth distrusting — deletes it. A probe PASS resets the
//! counter to 0. The current session ALWAYS snaps back to the ceiling and
//! re-descends on any strike (retained or not), so the audible behaviour of the
//! session that took the strike is identical either way; only the on-disk proof
//! and the next session's prime differ. In the steady state the on-disk counter
//! is 0 (a healthy proof) or the file is absent (revoked); it transiently reads 1
//! between a first spurious probe fail and the next session's pass/second-fail.

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
    /// caller's wall clock (kept a parameter so this is pure/testable). A freshly
    /// written proof always has `consecutive_failures == 0` — a clean proof.
    pub fn new(proved_at_epoch_s: u64, probe_response_ratio: f64, floor_frames: u64) -> Self {
        Self {
            schema: SCHEMA_VERSION,
            proved_at_epoch_s,
            probe_response_ratio,
            floor_frames,
            consecutive_failures: 0,
        }
    }

    /// Clone this record with `consecutive_failures` set to `count` — the
    /// two-strike RETAIN write ([`StrikeAction::RetainWithStrike`]). Preserves the
    /// original proof evidence (`proved_at_epoch_s`, `probe_response_ratio`,
    /// `floor_frames`) so the retained proof still primes the next session at the
    /// same floor; only the strike counter advances. Pure.
    pub fn with_consecutive_failures(&self, count: u32) -> Self {
        Self {
            consecutive_failures: count,
            ..self.clone()
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
    /// A CONFIRMED churn cycle: an early-window underfill unlock FOLLOWED BY a
    /// relock within the confirmation horizon — proof the host is still delivering
    /// yet the floor prime cannot hold (unlock→relock cycling). A bare terminal
    /// unlock (a stream that simply ended, no relock) is NOT this — it expires the
    /// pending strike harmlessly. See [`RevalidationTracker`].
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

/// The consecutive-probe-FAIL count at which a floor-primed proof is DELETED
/// (the two-strike limit). A single probe FAIL is a MEASUREMENT, not proof the
/// host changed — the lock-gated probe can spuriously fail if it runs during a
/// railed acquisition (the hardware-diagnosed jts.local 2026-07-03 false-fail).
/// That specific rail is now root-fixed by the prime-aware `NotL0` hold (#1161),
/// and floor-prime seating remains as defense-in-depth; an unrailed-settle guard
/// that also landed for it was REMOVED 2026-07-05 (it deadlocked beyond-authority
/// hosts), so this two-strike tolerance is the standing net for a residual
/// spurious read. Costing the household the ~2.5-min descent on ONE bad
/// measurement is the wrong trade, so
/// a probe fail RETAINS the proof (bumping this counter) the first time and only
/// deletes on the SECOND consecutive fail — by then two independent sessions
/// disagreed with the proof, which IS a host change worth distrusting. Value 2:
/// one retained strike, delete on the next.
///
/// This tolerance is SPECIFIC to `ProbeFail` (a measurement). `DllDemotion` and a
/// CONFIRMED `EarlyUnlock` churn cycle stay ONE-strike — they are direct
/// positive evidence the floor itself is failing on this host (a saturated
/// wrong-way slope / unlock→relock cycling), not an ambiguous probe read.
pub const PROBE_FAIL_STRIKE_LIMIT: u32 = 2;

/// What the mixer should DO on a floor-primed revalidation failure, decided
/// purely from the [`RevokeReason`] and the proof's current `consecutive_failures`
/// count. Keeps the two-strike policy in one testable place; the mixer performs
/// the I/O (delete vs re-persist) the variant asks for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StrikeAction {
    /// DELETE the proof and clear `flag_present` — the one-strike revoke. Used
    /// for `DllDemotion`, a confirmed `EarlyUnlock`, and the SECOND consecutive
    /// `ProbeFail`. The next session descends from the ceiling and re-proves.
    Revoke,
    /// RETAIN the proof but PERSIST an incremented `consecutive_failures` — the
    /// first `ProbeFail` strike. `flag_present` STAYS true, so the NEXT session
    /// still primes at the floor (one bad measurement must not cost the floor).
    /// The CURRENT session still snaps its held target back to the ceiling and
    /// runs the normal descent — identical user-visible behaviour to a revoke for
    /// this session; only the on-disk proof (and the next session's prime)
    /// differs. Carries the new count to write.
    RetainWithStrike { consecutive_failures: u32 },
}

/// Decide the two-strike outcome for a floor-primed revalidation failure. PURE —
/// the mixer enacts the returned action. `current_failures` is the proof's
/// on-disk `consecutive_failures` before this strike.
///
/// - `ProbeFail`: a measurement. First fail → [`StrikeAction::RetainWithStrike`]
///   (count → `current_failures + 1`); reaching [`PROBE_FAIL_STRIKE_LIMIT`] →
///   [`StrikeAction::Revoke`].
/// - `DllDemotion` / `EarlyUnlock`: positive floor-failure evidence → always
///   [`StrikeAction::Revoke`] (one strike), regardless of the counter.
pub fn classify_strike(reason: RevokeReason, current_failures: u32) -> StrikeAction {
    match reason {
        RevokeReason::ProbeFail => {
            let next = current_failures.saturating_add(1);
            if next >= PROBE_FAIL_STRIKE_LIMIT {
                StrikeAction::Revoke
            } else {
                StrikeAction::RetainWithStrike {
                    consecutive_failures: next,
                }
            }
        }
        RevokeReason::DllDemotion | RevokeReason::EarlyUnlock => StrikeAction::Revoke,
    }
}

/// The per-tick IMMEDIATE-trigger inputs for a floor-primed session, sampled by
/// the mixer from the servo reverse signals. These are the triggers that revoke on
/// the CURRENT lock the period the evidence appears: a LIVE probe FAIL and a live
/// L2 demotion. The third trigger — the early-window underfill unlock — is NOT
/// here: it is a two-phase discriminator (arm on the unlock, confirm on a relock)
/// owned by [`RevalidationTracker`]'s state machine, because a bare terminal
/// unlock (a stream that just ended) must NOT be a strike.
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
}

/// Decide whether a floor-primed session's IMMEDIATE revalidation triggers FAIL
/// this period, and if so, why. Ordering is by directness of evidence: a fresh
/// (LIVE) probe FAIL first, then a live L2 demotion. Returns `None` when neither
/// immediate trigger fires (the early-unlock churn discriminator is decided
/// separately in [`RevalidationTracker::step`]). PURE — the mixer enacts the
/// returned reason (snap-back + revoke); this only decides. `probe_fail` is the
/// exact `host_clock::probe_result_code` FAIL value (2), kept a plain literal here
/// so the pure module needs no dependency on the host_clock adapter.
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
    } else {
        None
    }
}

/// The PURE lock-edge + revalidation tracker for a floor-primed session — the
/// wiring the mixer runs once per render period around [`compute_revoke_reason`].
/// Extracted from the mixer so the lock-edge bookkeeping (which is where the
/// early-unlock churn discriminator lives) is testable WITHOUT ALSA: a test can
/// drive it with a real [`crate::lane_resampler::LaneResampler`]'s
/// `is_locked()` / `unlock_count()` sequence and confirm a simulated churn cycle
/// actually revokes while a terminal stream-end does not.
///
/// ## The EarlyUnlock churn discriminator (the fix for spurious terminal-unlock
/// strikes)
///
/// A floor-primed session that simply ENDS — the host stops streaming, so
/// deliveries stop, the fill drains below `minimum_safe_fill` within ~ms, and
/// `unlock_for_underfill` fires — is NOT evidence the floor prime failed. On
/// macOS, CoreAudio stops the device stream seconds after the last client, so
/// notification dings / previews present as sub-60 s sessions that always end this
/// way. The prior "any early-window underfill unlock revokes on the falling edge"
/// rule burned the proof on EVERY such short session (hardware-diagnosed on
/// jts.local 2026-07-03). The discriminator distinguishes CHURN (the host is
/// STILL delivering and the floor cannot hold → the lane unlocks *and relocks*)
/// from a terminal stream-end (the host stopped → the lane unlocks and never
/// relocks):
///
///  - An early-window underfill unlock **ARMS a pending strike** (records that a
///    strike is awaiting confirmation, and resets the tick-clock `periods_since_arm`).
///  - The strike **CONFIRMS** (revoke `EarlyUnlock`) only if a **RELOCK** (rising
///    edge) arrives within `confirm_horizon_periods` — a short tick-clock horizon.
///    Unlock→relock cycling proves the host is present and the floor is failing.
///  - If no relock arrives within the horizon, the pending strike **EXPIRES
///    harmlessly** (the stream died — no churn); the next lock is armed clean. The
///    honest bound is NOT an absolute "never survives a session": a strike survives
///    only into a relock arriving ≤ `confirm_horizon_periods` after the arming
///    unlock. The tracker cannot distinguish a genuinely-new stream's first relock
///    (a fresh clip starting just after the prior one stopped) from a churn relock —
///    both are "armed strike + rising edge inside the horizon" — so a restart inside
///    that window WILL confirm the prior session's strike (one spurious revoke,
///    self-healing via re-prove on that session's descent). Accepted residual.
///
/// A churn STORM (many unlock/relock cycles) revokes on the FIRST confirmed cycle:
/// the confirming relock clears `flag_present` (via the mixer's `on_revoked`) and
/// this tracker latches `floor_primed = floor_primed_now && revoke.is_none()`, so
/// the relocked session is no longer floor-primed and does not revalidate again —
/// exactly one revoke. `ProbeFail` / `DllDemotion` are UNCHANGED — they are direct
/// host-non-compliance evidence and still revoke immediately on the current lock.
///
/// ## The load-bearing ordering it encodes
///  1. Read the rising (idle→lock, the confirming relock) and falling
///     (lock→underfill-unlock, the arming unlock) edges from `was_locked` BEFORE
///     mutating it.
///  2. Run the revalidation against the PRE-reset baseline. The arming unlock is
///     seen on the falling edge — because `unlock_for_underfill` sets
///     `locked=false` in the same render period it bumps `unlock_count`, so the
///     ONLY period that carries the churn evidence is the one where `locked` is
///     already false. Gating `within_early_window` on `locked` alone would make
///     the arm unreachable.
///  3. THEN apply the lock-edge bookkeeping: a fresh lock re-arms the window,
///     unlock baseline, and clears the per-lock revoke latch (so a re-proven
///     session can revoke again — the latch is per-lock, not per-daemon-lifetime).
///     On a rising edge that CONFIRMED a revoke, `floor_primed` latches false (the
///     proof is dead) so the relocked lock cannot run a redundant second strike.
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
    /// The early-revalidation window in render periods (the arming window — an
    /// underfill unlock only ARMS a pending strike while inside this).
    early_window_periods: u64,
    /// Render periods since the most recent lock, capped at the early window + 1.
    periods_since_lock: u64,
    /// The resampler `unlock_count` at the most recent lock — the churn baseline.
    unlock_baseline_at_lock: u64,
    /// The resampler `locked` state observed last period (lock-edge detection).
    was_locked: bool,
    /// The EarlyUnlock CONFIRMATION horizon in render periods (tick clock, NOT wall
    /// time): a relock must arrive within this many periods of the arming underfill
    /// unlock for the pending strike to confirm. Short (≤ a few seconds of periods)
    /// so a terminal stream-end's pending strike expires well inside a fresh
    /// stream's restart gap.
    confirm_horizon_periods: u64,
    /// Whether an early-window underfill unlock is currently AWAITING relock
    /// confirmation. Set on the arming unlock, cleared on confirm / expiry / a fresh
    /// lock. Never survives a session (the terminal-unlock case expires it).
    pending_strike_armed: bool,
    /// Render periods since the pending strike was armed — the confirmation clock.
    /// Advances every period while armed regardless of lock state (the arm→relock
    /// gap is spent unlocked/priming); compared against `confirm_horizon_periods`.
    periods_since_arm: u64,
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
    /// `early_window_periods` is the derived early-revalidation (arming) budget and
    /// `confirm_horizon_periods` the derived churn-confirmation horizon (both in
    /// render periods — the tracker never reads a wall clock).
    pub fn new(
        floor_primed: bool,
        early_window_periods: u64,
        confirm_horizon_periods: u64,
    ) -> Self {
        Self {
            floor_primed,
            revoked_this_lock: false,
            early_window_periods,
            periods_since_lock: 0,
            unlock_baseline_at_lock: 0,
            was_locked: false,
            confirm_horizon_periods,
            pending_strike_armed: false,
            periods_since_arm: 0,
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

    /// True iff an early-window underfill unlock is currently awaiting relock
    /// confirmation. Test-only: the mixer never reads this — a pending strike is an
    /// internal, self-clearing state (confirm / expire / fresh lock). Gated
    /// `#[cfg(test)]` to stay out of the `-D warnings` binary build.
    #[cfg(test)]
    pub fn pending_strike_armed(&self) -> bool {
        self.pending_strike_armed
    }

    /// Advance one render period. Reads the live resampler lock/unlock state plus
    /// the servo reverse signals, decides whether to revoke, and updates the
    /// lock-edge bookkeeping. Returns the revoke decision plus the observed edges so
    /// the mixer can drive the proof machine's per-lock reset off the same edges.
    /// The mixer enacts a returned `revoke` (snap-back + file delete +
    /// observability). Pure: no I/O, no clock.
    ///
    /// Three revoke triggers, two shapes:
    ///  - IMMEDIATE (`ProbeFail`, `DllDemotion`): direct host-non-compliance
    ///    evidence via [`compute_revoke_reason`] — revoke on the current lock the
    ///    period the evidence appears.
    ///  - TWO-PHASE (`EarlyUnlock`): the churn discriminator. An early-window
    ///    underfill unlock ARMS a pending strike; a RELOCK within
    ///    `confirm_horizon_periods` CONFIRMS it (revoke). A pending strike whose
    ///    horizon elapses with no relock EXPIRES harmlessly (a terminal stream-end
    ///    — the host stopped, not floor churn). This is the fix for spurious
    ///    strikes on the short sessions macOS produces (notification dings /
    ///    previews).
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
            // (1) IMMEDIATE triggers (probe FAIL / L2 demotion) — unchanged. A LIVE
            // probe FAIL always coincides with the ladder at L2; a stale carryover
            // FAIL sits in Probing (`ladder_l2 == false`) and is ignored so a fresh
            // lock on a new compliant host is not revoked on a previous host's
            // verdict.
            revoke = compute_revoke_reason(RevalidationSignals {
                probe_result_code,
                probe_verdict_is_live: ladder_l2,
                ladder_l2,
            });

            // (2) EarlyUnlock CHURN discriminator. The arming window spans the
            // primed lock's first `early_window_periods` render periods AND its
            // immediate lock-loss period (the falling edge), where the underfill
            // unlock that ended the lock lands.
            let within_early_window =
                (locked || falling_edge) && self.periods_since_lock <= self.early_window_periods;
            let unlock_advanced = unlock_count > self.unlock_baseline_at_lock;

            // CONFIRM first: a relock (rising edge) while a strike is armed within
            // the horizon proves the host is still delivering yet the floor failed —
            // unlock→relock churn. `compute_revoke_reason` never returns EarlyUnlock,
            // so an immediate trigger above wins the reason only if it also fired.
            if revoke.is_none()
                && rising_edge
                && self.pending_strike_armed
                && self.periods_since_arm <= self.confirm_horizon_periods
            {
                revoke = Some(RevokeReason::EarlyUnlock);
            }
            // ARM: a fresh early-window underfill unlock. On the falling edge, so it
            // never collides with the rising-edge CONFIRM above. Idempotent while
            // already armed (a strike stays anchored to its first unlock's clock).
            if within_early_window && unlock_advanced && !self.pending_strike_armed {
                self.pending_strike_armed = true;
                self.periods_since_arm = 0;
            }

            if revoke.is_some() {
                self.revoked_this_lock = true;
                // Consume any pending strike: a revoke fired (this cycle or an
                // immediate trigger), so nothing is left awaiting confirmation.
                self.pending_strike_armed = false;
            }
        }

        // Expire a pending strike whose confirmation horizon elapsed with no relock:
        // the terminal stream-end case (the host stopped streaming; no churn). The
        // clock advances every period while armed regardless of lock state — the
        // arm→relock gap is spent unlocked/priming. Runs after the confirm read so a
        // relock exactly at the horizon still counts.
        if self.pending_strike_armed {
            self.periods_since_arm = self.periods_since_arm.saturating_add(1);
            if self.periods_since_arm > self.confirm_horizon_periods {
                self.pending_strike_armed = false;
            }
        }

        // Lock-edge bookkeeping AFTER the revalidation read. Latch the per-lock
        // prime state HERE (not before the read): the read above intentionally runs
        // against the PRE-reset baseline to catch the falling-edge underfill, so
        // re-sampling `floor_primed` before it would arm the freshly-latched value
        // against the PRIOR lock's stale unlock baseline. On a rising edge that
        // CONFIRMED a revoke, latch `floor_primed=false`: the mixer clears
        // `flag_present` right after `step` returns (the revoke's `on_revoked`), so
        // the relocked lock is NOT floor-primed and must not run a redundant second
        // strike — this is the same SSOT #1154 uses (a revoke ⇒ next snap lands at
        // the ceiling ⇒ not floor-primed). It also pins the revoke-before-relock
        // ordering: the floor consideration for the relocked lock resolves to
        // "ceiling," matching the flag the mixer is about to clear.
        if rising_edge {
            self.periods_since_lock = 0;
            self.unlock_baseline_at_lock = unlock_count;
            self.revoked_this_lock = false;
            self.floor_primed = floor_primed_now && revoke.is_none();
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
    /// visible for a post-mortem even after a re-prove. Set on a REVOKE (delete)
    /// AND on a RETAINED probe-fail strike (so a single spurious fail that only
    /// bumped the counter is still visible in STATUS).
    pub revoked_reason_last_code: Arc<AtomicU64>,
    /// The proof's current `consecutive_failures` — the two-strike counter, live
    /// in STATUS (`compliance.consecutive_failures`). `0` for a clean proof (or
    /// no proof); `1` after a first spurious probe fail whose proof was retained;
    /// reset to `0` on the next clean write or an explicit probe-pass reset.
    /// Reaching [`PROBE_FAIL_STRIKE_LIMIT`] deletes the proof (so it never sits at
    /// `≥ 2` on disk in the steady state).
    pub consecutive_failures: Arc<AtomicU64>,
}

impl HostComplianceObservability {
    /// A fresh observable seeded from the boot-time load: `flag_present` reflects
    /// whether a valid proof was loaded, `proved_at` its timestamp, the strike
    /// counter its recorded `consecutive_failures`, and no revoke has happened
    /// yet.
    pub fn new(flag_present: bool, proved_at_epoch_s: u64, consecutive_failures: u32) -> Self {
        Self {
            flag_present: Arc::new(AtomicBool::new(flag_present)),
            proved_at_epoch_s: Arc::new(AtomicU64::new(proved_at_epoch_s)),
            revoked_reason_last_code: Arc::new(AtomicU64::new(0)),
            consecutive_failures: Arc::new(AtomicU64::new(consecutive_failures as u64)),
        }
    }

    /// Clone the `Arc` handles (cheap) for the resampler observability snapshot.
    pub fn clone_handles(&self) -> Self {
        Self {
            flag_present: Arc::clone(&self.flag_present),
            proved_at_epoch_s: Arc::clone(&self.proved_at_epoch_s),
            revoked_reason_last_code: Arc::clone(&self.revoked_reason_last_code),
            consecutive_failures: Arc::clone(&self.consecutive_failures),
        }
    }

    /// Record a successful proof write — a clean proof, so the strike counter
    /// resets to 0.
    pub fn on_written(&self, proved_at_epoch_s: u64) {
        self.proved_at_epoch_s
            .store(proved_at_epoch_s, Ordering::Relaxed);
        self.flag_present.store(true, Ordering::Relaxed);
        self.consecutive_failures.store(0, Ordering::Relaxed);
    }

    /// Record a revocation (flag cleared, reason recorded, counter reset — the
    /// proof is gone, so its strike count is moot).
    pub fn on_revoked(&self, reason: RevokeReason) {
        self.flag_present.store(false, Ordering::Relaxed);
        self.revoked_reason_last_code
            .store(revoke_reason_code(reason), Ordering::Relaxed);
        self.consecutive_failures.store(0, Ordering::Relaxed);
    }

    /// Record a RETAINED probe-fail strike ([`StrikeAction::RetainWithStrike`]):
    /// the proof stays present (`flag_present` UNCHANGED — still true), the strike
    /// reason is recorded for STATUS/post-mortem, and the counter advances to the
    /// retained value. The next session still primes at the floor off the retained
    /// proof.
    pub fn on_strike_retained(&self, reason: RevokeReason, consecutive_failures: u32) {
        self.revoked_reason_last_code
            .store(revoke_reason_code(reason), Ordering::Relaxed);
        self.consecutive_failures
            .store(consecutive_failures as u64, Ordering::Relaxed);
    }

    /// Record a probe-PASS reset of the strike counter on a floor-primed session:
    /// the proof stays present, and the counter clears to 0 (a healthy pass
    /// forgives an earlier spurious fail). Does NOT touch `flag_present` or the
    /// last-revoke reason (a pass is not a revoke).
    pub fn on_pass_reset(&self) {
        self.consecutive_failures.store(0, Ordering::Relaxed);
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
    fn with_consecutive_failures_preserves_evidence() {
        let rec = HostCompliance::new(1_700_000_000, 1.312, 576);
        assert_eq!(rec.consecutive_failures, 0);
        let struck = rec.with_consecutive_failures(1);
        assert_eq!(struck.consecutive_failures, 1);
        // The proof evidence is preserved so the retained proof still primes the
        // next session at the same floor with the same ratio/timestamp.
        assert_eq!(struck.proved_at_epoch_s, rec.proved_at_epoch_s);
        assert_eq!(struck.probe_response_ratio, rec.probe_response_ratio);
        assert_eq!(struck.floor_frames, rec.floor_frames);
        assert_eq!(struck.schema, rec.schema);
        // Still a valid prime authority for the same floor.
        assert!(struck.valid_for(576));
    }

    // ---- Two-strike probe-fail policy (classify_strike) --------------------

    #[test]
    fn probe_fail_is_two_strike_retain_then_revoke() {
        // First probe fail (counter 0 → 1): RETAIN, bump the counter. One bad
        // measurement must not cost the floor.
        assert_eq!(
            classify_strike(RevokeReason::ProbeFail, 0),
            StrikeAction::RetainWithStrike {
                consecutive_failures: 1
            },
        );
        // Second consecutive probe fail (counter 1 → 2 == limit): REVOKE. Two
        // independent sessions disagreeing with the proof IS a host change.
        assert_eq!(
            classify_strike(RevokeReason::ProbeFail, 1),
            StrikeAction::Revoke,
        );
        // Defensive: a somehow-higher stored counter still revokes.
        assert_eq!(
            classify_strike(RevokeReason::ProbeFail, 5),
            StrikeAction::Revoke,
        );
    }

    #[test]
    fn dll_demotion_and_early_unlock_are_one_strike() {
        // Direct floor-failure evidence revokes on the FIRST strike regardless of
        // the counter — the two-strike tolerance is probe-fail-specific.
        for count in [0u32, 1, 2, 9] {
            assert_eq!(
                classify_strike(RevokeReason::DllDemotion, count),
                StrikeAction::Revoke,
                "DLL demotion is always one-strike (count={count})",
            );
            assert_eq!(
                classify_strike(RevokeReason::EarlyUnlock, count),
                StrikeAction::Revoke,
                "confirmed churn is always one-strike (count={count})",
            );
        }
    }

    #[test]
    fn strike_limit_is_two() {
        // The limit is load-bearing: at 2, a first fail retains and a second
        // deletes. Pin it so a change to the const is a deliberate, reviewed edit.
        assert_eq!(PROBE_FAIL_STRIKE_LIMIT, 2);
    }

    /// The full ON-DISK two-strike lifecycle, composing `classify_strike` +
    /// `with_consecutive_failures` + `store`/`load`/`revoke` EXACTLY as
    /// `mixer::service_host_compliance` wires them — the faithful end-to-end proof
    /// without the (macOS-uncompilable) mixer. Two sequences:
    ///   (a) fail → RETAIN (proof kept, counter 1) → pass → reset (counter 0);
    ///   (b) fail → RETAIN → fail → REVOKE (file deleted).
    /// Mutation guard both directions: making a probe fail one-strike breaks (a)'s
    /// "proof kept"; making it never delete breaks (b)'s "file gone".
    #[test]
    fn two_strike_on_disk_lifecycle() {
        let dir = std::env::temp_dir().join(format!("jts-compl-2strike-{}", std::process::id()));
        let path = dir.join("host_compliance.json");
        let _ = std::fs::remove_dir_all(&dir);

        // A clean proof is written (session A proved + settled at the floor).
        let proof = HostCompliance::new(1_700_000_000, 1.312, 576);
        proof.store(&path).expect("store clean proof");
        assert_eq!(HostCompliance::load(&path).unwrap().consecutive_failures, 0);

        // --- (a) First probe fail → RETAIN. The mixer keeps the proof, persists a
        // bumped counter, and leaves flag_present true. ---
        let loaded = HostCompliance::load(&path).unwrap();
        match classify_strike(RevokeReason::ProbeFail, loaded.consecutive_failures) {
            StrikeAction::RetainWithStrike {
                consecutive_failures,
            } => {
                assert_eq!(consecutive_failures, 1);
                loaded
                    .with_consecutive_failures(consecutive_failures)
                    .store(&path)
                    .expect("persist retained strike");
            }
            StrikeAction::Revoke => panic!("first probe fail must RETAIN, not revoke"),
        }
        // The proof is STILL on disk (the next session still primes at the floor),
        // now carrying the strike, and still a valid prime authority.
        let after_strike = HostCompliance::load(&path).expect("proof retained after first fail");
        assert_eq!(after_strike.consecutive_failures, 1);
        assert!(
            after_strike.valid_for(576),
            "a retained proof still primes the next session at the same floor"
        );

        // --- A probe PASS on the next floor-primed session resets the counter. ---
        let cleared = after_strike.with_consecutive_failures(0);
        cleared.store(&path).expect("persist pass reset");
        assert_eq!(HostCompliance::load(&path).unwrap().consecutive_failures, 0);

        // --- (b) Now fail twice in a row from the clean counter. ---
        // First fail → retain (counter 0 → 1).
        let l0 = HostCompliance::load(&path).unwrap();
        let StrikeAction::RetainWithStrike {
            consecutive_failures: c1,
        } = classify_strike(RevokeReason::ProbeFail, l0.consecutive_failures)
        else {
            panic!("first fail retains");
        };
        l0.with_consecutive_failures(c1)
            .store(&path)
            .expect("persist strike 1");
        assert!(
            HostCompliance::load(&path).is_some(),
            "still present after 1"
        );

        // Second consecutive fail → REVOKE (counter 1 → limit): the mixer deletes.
        let l1 = HostCompliance::load(&path).unwrap();
        assert_eq!(l1.consecutive_failures, 1);
        match classify_strike(RevokeReason::ProbeFail, l1.consecutive_failures) {
            StrikeAction::Revoke => {
                HostCompliance::revoke(&path).expect("delete on the second fail");
            }
            StrikeAction::RetainWithStrike { .. } => {
                panic!("the SECOND consecutive probe fail must REVOKE (delete)")
            }
        }
        assert!(
            HostCompliance::load(&path).is_none(),
            "the proof is deleted on the second consecutive probe fail"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// A DLL demotion on a floor-primed session with a clean counter deletes the
    /// proof on the FIRST strike — the one-strike path is unchanged for direct
    /// floor-failure evidence (contrast the two-strike probe-fail lifecycle).
    #[test]
    fn dll_demotion_deletes_on_first_strike_on_disk() {
        let dir = std::env::temp_dir().join(format!("jts-compl-dll-{}", std::process::id()));
        let path = dir.join("host_compliance.json");
        let _ = std::fs::remove_dir_all(&dir);
        HostCompliance::new(1, 1.0, 576).store(&path).unwrap();
        match classify_strike(RevokeReason::DllDemotion, 0) {
            StrikeAction::Revoke => HostCompliance::revoke(&path).unwrap(),
            StrikeAction::RetainWithStrike { .. } => panic!("DLL demotion is one-strike"),
        }
        assert!(
            HostCompliance::load(&path).is_none(),
            "a DLL demotion deletes on the first strike"
        );
        let _ = std::fs::remove_dir_all(&dir);
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
        }
    }

    #[test]
    fn compute_revoke_reason_healthy_session_never_revokes() {
        // Neither immediate trigger fires on a passing probe / non-L2 ladder.
        assert_eq!(compute_revoke_reason(healthy()), None);
    }

    #[test]
    fn compute_revoke_reason_never_returns_early_unlock() {
        // The EarlyUnlock churn discriminator is owned by RevalidationTracker::step
        // (arm on unlock + confirm on relock), NEVER by this immediate decider — so
        // no combination of the immediate inputs can produce EarlyUnlock here. This
        // is a mutation guard: a refactor that folded the old unlock branch back in
        // would resurrect the terminal-unlock false-revoke.
        for probe in [0u64, 1, 2, 3] {
            for live in [false, true] {
                for l2 in [false, true] {
                    let r = compute_revoke_reason(RevalidationSignals {
                        probe_result_code: probe,
                        probe_verdict_is_live: live,
                        ladder_l2: l2,
                    });
                    assert_ne!(
                        r,
                        Some(RevokeReason::EarlyUnlock),
                        "compute_revoke_reason must never decide EarlyUnlock \
                         (probe={probe} live={live} l2={l2})"
                    );
                }
            }
        }
    }

    #[test]
    fn compute_revoke_reason_probe_fail_takes_precedence() {
        // A fresh (LIVE) probe FAIL (code 2) revokes as ProbeFail even if L2 is also
        // set. A live probe fail always coincides with L2, so both are set here.
        let s = RevalidationSignals {
            probe_result_code: 2,
            probe_verdict_is_live: true,
            ladder_l2: true,
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
        };
        assert_eq!(compute_revoke_reason(stale), None);
    }

    #[test]
    fn compute_revoke_reason_l2_demotion_after_pass() {
        // No fresh probe fail, but the DLL demoted mid-stream → DllDemotion.
        let s = RevalidationSignals {
            probe_result_code: 1,
            probe_verdict_is_live: false,
            ladder_l2: true,
        };
        assert_eq!(compute_revoke_reason(s), Some(RevokeReason::DllDemotion));
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
        let obs = HostComplianceObservability::new(false, 0, 0);
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
    fn observability_tracks_strike_counter() {
        // Seeded from a loaded proof with a retained strike (counter 1).
        let obs = HostComplianceObservability::new(true, 1_700_000_000, 1);
        assert!(obs.flag_present.load(Ordering::Relaxed));
        assert_eq!(obs.consecutive_failures.load(Ordering::Relaxed), 1);

        // A RETAINED probe-fail strike keeps the flag TRUE, records the reason,
        // and bumps the counter — the "one bad measurement keeps the floor" path.
        obs.on_strike_retained(RevokeReason::ProbeFail, 1);
        assert!(
            obs.flag_present.load(Ordering::Relaxed),
            "a retained strike must NOT clear flag_present"
        );
        assert_eq!(obs.consecutive_failures.load(Ordering::Relaxed), 1);
        assert_eq!(
            obs.revoked_reason_last_code.load(Ordering::Relaxed),
            revoke_reason_code(RevokeReason::ProbeFail)
        );

        // A probe PASS reset clears the counter but leaves the flag/reason alone.
        obs.on_pass_reset();
        assert!(obs.flag_present.load(Ordering::Relaxed));
        assert_eq!(obs.consecutive_failures.load(Ordering::Relaxed), 0);

        // A clean write resets the counter to 0 as well.
        obs.on_strike_retained(RevokeReason::ProbeFail, 1);
        assert_eq!(obs.consecutive_failures.load(Ordering::Relaxed), 1);
        obs.on_written(1_700_000_100);
        assert_eq!(obs.consecutive_failures.load(Ordering::Relaxed), 0);

        // A revoke (delete) clears the counter (proof gone) and the flag.
        obs.on_strike_retained(RevokeReason::ProbeFail, 1);
        obs.on_revoked(RevokeReason::ProbeFail);
        assert!(!obs.flag_present.load(Ordering::Relaxed));
        assert_eq!(obs.consecutive_failures.load(Ordering::Relaxed), 0);
    }

    #[test]
    fn settle_periods_clamped_to_at_least_one() {
        let mut p = ComplianceProof::new(0);
        // One clean period at floor writes immediately (clamp floor is 1).
        assert!(matches!(p.tick(floor_l0(0)), ProofOutcome::Write { .. }));
    }

    // ---- RevalidationTracker (churn discriminator + one-strike wiring) -----

    const EARLY_WINDOW: u64 = 20;
    // The churn-confirmation horizon for the pure tests (render periods). Deliberately
    // smaller than EARLY_WINDOW so a relock can land inside the arming window yet
    // OUTSIDE the confirm horizon (the boundary test). A real build derives this from
    // `HOST_COMPLIANCE_CHURN_CONFIRM_SECS` via `ms_to_periods`.
    const CONFIRM: u64 = 8;

    /// A locked healthy tick (probe passed, ladder L0, no churn). Returns the four
    /// resampler/servo args; the fifth `step` arg (`floor_primed_now`) is passed
    /// explicitly at each call site to make the per-lock prime state visible.
    fn ok_lock(unlock_count: u64) -> (bool, u64, u64, bool) {
        // (locked, unlock_count, probe_code=pass, ladder_l2=false)
        (true, unlock_count, 1, false)
    }

    /// Build a floor-primed tracker with the test window + horizon.
    fn primed_tracker() -> RevalidationTracker {
        RevalidationTracker::new(true, EARLY_WINDOW, CONFIRM)
    }

    #[test]
    fn tracker_cold_session_never_revokes() {
        // A NOT-floor-primed session has no persisted authority to distrust — even
        // an underfill unlock inside the early window (then a relock) just re-earns
        // the proof. `floor_primed_now=false` (no live proof) is the cold signal.
        let mut t = RevalidationTracker::new(false, EARLY_WINDOW, CONFIRM);
        let (l, u, p, l2) = ok_lock(0);
        assert_eq!(t.step(l, u, p, l2, false).revoke, None);
        // Underfill unlock (falling edge, count advances): no arm (cold) → no revoke.
        assert_eq!(t.step(false, 1, 1, false, false).revoke, None);
        assert!(
            !t.pending_strike_armed(),
            "a cold session never arms a strike"
        );
        // Even a fast relock does not confirm anything on a cold lane.
        assert_eq!(t.step(true, 1, 1, false, false).revoke, None);
    }

    #[test]
    fn tracker_terminal_stream_end_unlock_does_not_revoke() {
        // THE HARDWARE FIX (jts.local 2026-07-03): a floor-primed session that locks,
        // then underfill-unlocks inside the early window because the STREAM ENDED
        // (the host stopped; NO relock follows) must NOT revoke. The unlock ARMS a
        // pending strike; with no relock, the strike EXPIRES harmlessly once the
        // confirmation horizon elapses. The proof survives — the next real stream
        // still primes at the floor.
        let mut t = primed_tracker();
        // A ~2 s stream: lock cleanly for a few periods (baseline unlock_count = 0).
        for _ in 0..3 {
            let (l, u, p, l2) = ok_lock(0);
            assert_eq!(t.step(l, u, p, l2, true).revoke, None);
        }
        // Stream ends → underfill unlock on the falling edge. This ARMS a strike but
        // does NOT revoke (no relock yet).
        let end = t.step(false, 1, 1, false, true);
        assert_eq!(
            end.revoke, None,
            "a terminal stream-end unlock must NOT revoke — it only arms a pending strike"
        );
        assert!(end.falling_edge);
        assert!(
            t.pending_strike_armed(),
            "the early-window underfill armed a pending strike awaiting relock"
        );
        assert!(!t.revoked_this_lock());
        // The host stays stopped (no relock). Tick out the whole confirmation horizon:
        // the strike must expire, still no revoke.
        for _ in 0..CONFIRM {
            assert_eq!(
                t.step(false, 1, 1, false, true).revoke,
                None,
                "no relock → the pending strike never confirms"
            );
        }
        assert!(
            !t.pending_strike_armed(),
            "the pending strike expires after the confirmation horizon with no relock"
        );
    }

    #[test]
    fn tracker_churn_unlock_then_relock_revokes_exactly_once() {
        // CHURN: a floor-primed session locks, underfill-unlocks inside the early
        // window (arm), then RELOCKS within the confirmation horizon (the host is
        // still delivering yet the floor cannot hold) → revoke EarlyUnlock, exactly
        // once, on the confirming relock.
        let mut t = primed_tracker();
        for _ in 0..3 {
            let (l, u, p, l2) = ok_lock(0);
            assert_eq!(t.step(l, u, p, l2, true).revoke, None);
        }
        // Underfill unlock → arm (no revoke yet).
        assert_eq!(t.step(false, 1, 1, false, true).revoke, None);
        assert!(t.pending_strike_armed());
        // A couple of priming periods still unlocked, inside the horizon.
        assert_eq!(t.step(false, 1, 1, false, true).revoke, None);
        // RELOCK within the horizon → confirm. `floor_primed_now` is still true (the
        // mixer clears flag_present only AFTER step returns).
        let relock = t.step(true, 1, 1, false, true);
        assert_eq!(
            relock.revoke,
            Some(RevokeReason::EarlyUnlock),
            "an unlock+relock churn cycle within the horizon must revoke on the relock"
        );
        assert!(relock.rising_edge);
        assert!(
            !t.pending_strike_armed(),
            "the strike is consumed on confirm"
        );
    }

    #[test]
    fn tracker_relock_just_outside_horizon_does_not_revoke() {
        // BOUNDARY: an underfill unlock arms a strike, but the relock arrives just
        // AFTER the confirmation horizon — the strike has already expired, so the
        // relock does not confirm. No revoke; the proof survives.
        let mut t = primed_tracker();
        for _ in 0..3 {
            let (l, u, p, l2) = ok_lock(0);
            assert_eq!(t.step(l, u, p, l2, true).revoke, None);
        }
        // Arm on the underfill (periods_since_arm resets to 0, then increments to 1
        // at the bottom of this same period).
        assert_eq!(t.step(false, 1, 1, false, true).revoke, None);
        assert!(t.pending_strike_armed());
        // Stay unlocked for exactly CONFIRM more periods → periods_since_arm exceeds
        // the horizon and the strike expires WITHOUT a relock.
        for _ in 0..CONFIRM {
            assert_eq!(t.step(false, 1, 1, false, true).revoke, None);
        }
        assert!(
            !t.pending_strike_armed(),
            "the strike expired one period past the horizon"
        );
        // The (late) relock now finds no armed strike → no confirm, no revoke.
        let relock = t.step(true, 1, 1, false, true);
        assert_eq!(
            relock.revoke, None,
            "a relock just outside the confirmation horizon must not revoke"
        );
        assert!(relock.rising_edge);
    }

    #[test]
    fn tracker_churn_storm_revokes_once_total() {
        // STORM: many unlock/relock cycles. The FIRST confirmed cycle revokes; the
        // mixer then clears flag_present (on_revoked), so every subsequent lock is
        // NOT floor-primed (floor_primed_now=false) and cannot revoke again. Exactly
        // one revoke across the whole storm.
        let mut t = primed_tracker();
        for _ in 0..3 {
            let (l, u, p, l2) = ok_lock(0);
            assert_eq!(t.step(l, u, p, l2, true).revoke, None);
        }
        // Cycle 1: unlock (arm) → relock (confirm → revoke #1). flag_present still
        // true across this relock (the mixer clears it AFTER step returns).
        assert_eq!(t.step(false, 1, 1, false, true).revoke, None);
        assert_eq!(
            t.step(true, 1, 1, false, true).revoke,
            Some(RevokeReason::EarlyUnlock),
            "the first confirmed churn cycle revokes"
        );
        // From here the mixer has cleared flag_present → floor_primed_now=false for
        // every subsequent period. Drive several more unlock/relock cycles; NONE of
        // them may revoke again (the lane is no longer floor-primed).
        let mut extra_revokes = 0u32;
        let mut uc = 2u64;
        for _cycle in 0..5 {
            // Underfill unlock (count advances), proof now absent.
            if t.step(false, uc, 1, false, false).revoke.is_some() {
                extra_revokes += 1;
            }
            uc += 1;
            // Relock, proof still absent.
            if t.step(true, uc, 1, false, false).revoke.is_some() {
                extra_revokes += 1;
            }
            uc += 1;
        }
        assert_eq!(
            extra_revokes, 0,
            "a storm revokes exactly once — later cycles run on a non-primed lane"
        );
    }

    #[test]
    fn tracker_underfill_after_early_window_never_arms() {
        // Past the early window, a single underfill unlock is ordinary transient
        // churn (the probe already ran and passed) — it does not even ARM a strike,
        // so a following relock cannot confirm one.
        let mut t = primed_tracker();
        // Hold lock past the early window (live proof present every period).
        for _ in 0..(EARLY_WINDOW + 5) {
            let (l, u, p, l2) = ok_lock(0);
            assert_eq!(t.step(l, u, p, l2, true).revoke, None);
        }
        // Underfill outside the window: no arm, no revoke.
        assert_eq!(t.step(false, 1, 1, false, true).revoke, None);
        assert!(
            !t.pending_strike_armed(),
            "an out-of-window underfill must not arm a strike"
        );
        // Even a fast relock does not confirm (nothing was armed).
        assert_eq!(t.step(true, 1, 1, false, true).revoke, None);
    }

    #[test]
    fn tracker_live_probe_fail_revokes() {
        // A LIVE probe FAIL (code 2 corroborated by ladder L2) revokes as ProbeFail,
        // immediately on the current lock — the two-phase EarlyUnlock path does not
        // gate the direct host-non-compliance triggers.
        let mut t = primed_tracker();
        assert_eq!(t.step(true, 0, 1, false, true).revoke, None);
        let step = t.step(true, 0, 2, true, true);
        assert_eq!(step.revoke, Some(RevokeReason::ProbeFail));
    }

    #[test]
    fn tracker_dll_demotion_revokes_immediately() {
        // A mid-stream L2 demotion (no fresh probe fail) revokes as DllDemotion,
        // immediately while locked — unchanged by the churn discriminator.
        let mut t = primed_tracker();
        assert_eq!(t.step(true, 0, 1, false, true).revoke, None);
        assert_eq!(
            t.step(true, 0, 1, true, true).revoke,
            Some(RevokeReason::DllDemotion)
        );
    }

    #[test]
    fn tracker_ignores_stale_probe_fail_on_fresh_lock() {
        // The stale-verdict trap: a fresh lock on a new compliant host must NOT
        // revoke on a previous session's carried-over FAIL. The servo leaves
        // `probe_result=Fail` across a session boundary but the ladder is back in
        // Probing (`ladder_l2=false`), so the FAIL is not live and no revoke fires.
        let mut t = primed_tracker();
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
        // The per-lock revoke latch must reset on a fresh lock so a re-proven session
        // can strike again if the host later misbehaves — it is NOT a daemon-lifetime
        // latch. (Uses probe fail, which stays live across the relock here because the
        // test keeps floor_primed_now=true to exercise the latch, not the lifecycle.)
        let mut t = primed_tracker();
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
        //    — an early churn cycle just re-earns the proof;
        //  - session B (floor-primed off A's fresh proof) DOES revalidate — the same
        //    churn cycle revokes.
        // The tracker is constructed `floor_primed=false` (no boot proof); the live
        // value is what each rising edge latches.
        let mut t = RevalidationTracker::new(false, EARLY_WINDOW, CONFIRM);

        // Session A: lock with the live proof STILL ABSENT (floor_primed_now=false).
        for _ in 0..3 {
            assert_eq!(t.step(true, 0, 1, false, false).revoke, None);
        }
        // An early churn cycle on the cold session must NOT revoke — nothing to
        // distrust; the session is proving from scratch. It arms nothing (cold) and
        // ends UNLOCKED so session B below begins with a fresh rising edge.
        assert_eq!(t.step(false, 1, 1, false, false).revoke, None); // unlock (no arm — cold)
        assert_eq!(
            t.step(true, 1, 1, false, false).revoke,
            None,
            "a cold (not floor-primed) relock never revokes"
        );
        assert_eq!(t.step(false, 2, 1, false, false).revoke, None); // session A ends (unlocked)

        // Session A wrote its proof; session B LOCKS FRESH (rising edge) with the
        // proof now LIVE (floor_primed_now=true). The rising edge latches
        // floor_primed=true; these clean periods do not revoke.
        for _ in 0..3 {
            assert_eq!(t.step(true, 2, 1, false, true).revoke, None);
        }
        // The SAME early churn cycle now revokes — session B was floor-primed.
        assert_eq!(t.step(false, 3, 1, false, true).revoke, None, "arm");
        assert_eq!(
            t.step(true, 3, 1, false, true).revoke,
            Some(RevokeReason::EarlyUnlock),
            "a floor-primed session B (proof live at its rising edge) revalidates + revokes"
        );
    }

    #[test]
    fn tracker_session_after_revoke_does_not_revalidate() {
        // After a revoke clears the proof, the NEXT session locks with the live
        // signal false, so its rising edge latches floor_primed=false and it runs
        // NO one-strike revalidation — it descends + re-proves like any cold lane.
        let mut t = primed_tracker();
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
        // An early churn cycle on this re-proving session does NOT revoke.
        assert_eq!(t.step(false, 1, 1, false, false).revoke, None);
        assert_eq!(
            t.step(true, 1, 1, false, false).revoke,
            None,
            "the session after a revoke is not floor-primed, so it never revokes"
        );
    }

    #[test]
    fn tracker_confirmed_relock_latches_floor_primed_false_for_lock_b() {
        // THE ORDERING PIN (interaction with #1154's snap-destination SSOT): on the
        // relock that CONFIRMS a churn strike, `step` latches `floor_primed=false`
        // even though `floor_primed_now` is still true (the mixer clears flag_present
        // only after step returns). This is what makes the revoke "win" the relocked
        // lock's floor consideration: lock B is NOT floor-primed, so it does not run a
        // second (redundant) strike, and the very next session-boundary snap lands at
        // the ceiling — matching the flag the mixer is about to clear.
        let mut t = primed_tracker();
        for _ in 0..3 {
            assert_eq!(t.step(true, 0, 1, false, true).revoke, None);
        }
        assert_eq!(t.step(false, 1, 1, false, true).revoke, None, "arm");
        assert_eq!(
            t.step(true, 1, 1, false, true).revoke,
            Some(RevokeReason::EarlyUnlock),
            "confirm on the relock"
        );
        // Lock B continues with the proof now cleared (floor_primed_now=false). A
        // fresh underfill+relock on lock B must NOT revoke again — lock B latched
        // floor_primed=false on the confirming edge, so it never revalidates.
        assert_eq!(t.step(false, 2, 1, false, false).revoke, None);
        assert_eq!(
            t.step(true, 2, 1, false, false).revoke,
            None,
            "lock B is not floor-primed after the confirmed revoke — no second strike"
        );
    }

    // ---- Faithful wiring tests: a REAL resampler drives the discriminator -----

    /// Shared REAL-resampler harness geometry + tone. Returns a locked resampler and
    /// a matching output buffer, exactly as the mixer would have after acquisition.
    fn build_locked_real_resampler() -> (crate::lane_resampler::LaneResampler, Vec<i16>) {
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
        let mut out = vec![0i16; PERIOD as usize * 2];
        let deep = TARGET + CUSHION + 8 + 1;
        r.push_input(&real_tone(deep + 64));
        assert_eq!(
            r.render_period(&mut out),
            PERIOD as usize,
            "locks + renders"
        );
        assert!(r.is_locked(), "the lane is locked after the deep prefill");
        (r, out)
    }

    fn real_tone(frames: usize) -> Vec<i16> {
        let mut v = Vec::with_capacity(frames * 2);
        for n in 0..frames {
            let x = ((n as f64) * 0.02).sin();
            let s = (x * 8000.0) as i16;
            v.push(s);
            v.push(s);
        }
        v
    }

    #[test]
    fn real_resampler_churn_relock_drives_early_unlock_revoke() {
        // The wiring-level regression: build a REAL `LaneResampler`, lock it, feed the
        // RevalidationTracker the resampler's OWN `is_locked()` / `unlock_count()` each
        // period exactly as the mixer does, STARVE it so it underfill-unlocks (arm),
        // then RE-FEED it so it RE-LOCKS within the confirmation horizon — and assert
        // the tracker revokes on the relock. This proves the churn combination the
        // pure tests use (falling edge → arm, rising edge within horizon → confirm) is
        // one the live wiring PRODUCES, not a synthetic one the plumbing cannot reach.
        const PERIOD: usize = 256;
        let (mut r, mut out) = build_locked_real_resampler();
        let mut t = primed_tracker();

        let baseline_unlocks = r.unlock_count();

        // A healthy locked period → no revoke.
        assert_eq!(
            t.step(r.is_locked(), r.unlock_count(), 1, false, true)
                .revoke,
            None,
            "a healthy locked period does not revoke"
        );
        // A few clean periods (still locked, inside the early window).
        for _ in 0..3 {
            r.push_input(&real_tone(PERIOD));
            r.render_period(&mut out);
            assert!(r.is_locked());
            assert_eq!(
                t.step(r.is_locked(), r.unlock_count(), 1, false, true)
                    .revoke,
                None
            );
        }

        // STARVE → the resampler underfill-unlocks (arm the pending strike). No
        // revoke on this falling edge.
        let mut armed = false;
        for _ in 0..64 {
            r.render_period(&mut out);
            let s = t.step(r.is_locked(), r.unlock_count(), 1, false, true);
            assert_eq!(s.revoke, None, "the arming underfill must not revoke");
            if !r.is_locked() {
                assert!(
                    r.unlock_count() > baseline_unlocks,
                    "the underfill actually advanced the unlock count"
                );
                assert!(
                    t.pending_strike_armed(),
                    "the underfill armed a pending strike"
                );
                armed = true;
                break;
            }
        }
        assert!(
            armed,
            "the lane underfill-unlocked within the starve budget"
        );

        // RE-FEED → the resampler re-primes and RE-LOCKS within the confirm horizon.
        // The confirming rising edge revokes EarlyUnlock through the live wiring.
        let mut revoked = None;
        for _ in 0..64 {
            r.push_input(&real_tone(PERIOD * 4));
            r.render_period(&mut out);
            let s = t.step(r.is_locked(), r.unlock_count(), 1, false, true);
            if let Some(reason) = s.revoke {
                revoked = Some(reason);
                assert!(
                    s.rising_edge,
                    "the revoke lands on the relock (rising edge)"
                );
                assert!(r.is_locked(), "the revoke coincides with the relock");
                break;
            }
        }
        assert_eq!(
            revoked,
            Some(RevokeReason::EarlyUnlock),
            "a real resampler unlock→relock churn cycle inside the horizon must revoke \
             via the tracker — the churn discriminator is reachable through the wiring"
        );
    }

    #[test]
    fn real_resampler_terminal_unlock_does_not_revoke() {
        // The complement of the churn test AND the hardware scenario: a REAL resampler
        // that underfill-unlocks and is NEVER re-fed (the stream ended — the host
        // stopped) must NOT revoke. The tracker keeps ticking on the (still-running,
        // DAC-paced) mixer loop with the lane unlocked; the pending strike expires
        // after the confirmation horizon and the proof is never revoked.
        let (mut r, mut out) = build_locked_real_resampler();
        let mut t = primed_tracker();

        // A healthy locked period, then starve to the underfill unlock (arm).
        assert_eq!(
            t.step(r.is_locked(), r.unlock_count(), 1, false, true)
                .revoke,
            None
        );
        let mut armed = false;
        for _ in 0..64 {
            r.render_period(&mut out);
            let s = t.step(r.is_locked(), r.unlock_count(), 1, false, true);
            assert_eq!(s.revoke, None, "the arming underfill must not revoke");
            if !r.is_locked() {
                armed = true;
                break;
            }
        }
        assert!(armed, "the lane underfill-unlocked");
        assert!(
            t.pending_strike_armed(),
            "the underfill armed a pending strike"
        );

        // The host is gone — NO re-feed. Keep rendering silence (the mixer loop runs
        // regardless of the input lane) well past the confirmation horizon. The lane
        // stays unlocked, the strike expires, and NO revoke ever fires.
        for _ in 0..(CONFIRM as usize + 8) {
            r.render_period(&mut out);
            assert!(!r.is_locked(), "no input → the lane stays unlocked");
            assert_eq!(
                t.step(r.is_locked(), r.unlock_count(), 1, false, true)
                    .revoke,
                None,
                "a terminal stream-end (no relock) never revokes through the wiring"
            );
        }
        assert!(
            !t.pending_strike_armed(),
            "the pending strike expired with no relock — the proof survives"
        );
    }

    /// Build a REAL, decay-ARMED, FLOOR-PRIMED resampler locked AT the floor via
    /// the Part 1 floor-prime seating (jts.local 2026-07-03) — the deeper seating
    /// the fix produces. The lock seats at the floor depth (not the shallow
    /// bounded-prime fall-through), so this exercises the interaction between the
    /// deeper seat and the #1156 churn discriminator.
    fn build_floor_primed_locked_resampler() -> (crate::lane_resampler::LaneResampler, Vec<i16>) {
        use crate::lane_resampler::{DecayParams, LaneResampler};
        const PERIOD: u32 = 256;
        const TARGET: usize = 512;
        const CUSHION: usize = 256;
        const FLOOR: u64 = (TARGET + 32) as u64;
        const RING: usize = 8192;
        let params = DecayParams {
            enabled: true,
            floor_frames: FLOOR,
            step_frames: 16,
            interval_ms: 1,
            stability_ms: 1,
            cascade_guard_ppm: 400.0,
        };
        let mut r =
            LaneResampler::new(2, PERIOD, 48_000, TARGET, CUSHION, 500.0, RING, params).unwrap();
        // Prime at the floor as a valid proof would, so `try_lock` seats at the
        // floor depth (the Part 1 path), not shallow.
        r.prime_decay_at_floor();
        let mut out = vec![0i16; PERIOD as usize * 2];
        // Feed exactly the floor prefill (+ slack). Under the fix the lane seats at
        // the floor from this depth.
        r.push_input(&real_tone(FLOOR as usize + 8 + 1 + 64));
        assert_eq!(r.render_period(&mut out), PERIOD as usize, "locks at floor");
        assert!(r.is_locked());
        // The lock seated at the floor depth (the Part 1 fix): the cursor-relative
        // fill is near the floor, not the fat ceiling. `hold_fill_frames` is
        // private to lane_resampler; the seat depth itself is asserted directly in
        // `lane_resampler::tests::floor_primed_lock_does_not_seat_shallow_via_fallthrough`.
        // Here the public `fill_frames_gauge` confirms the seated fill is shallow
        // (near the floor), not the ceiling.
        assert!(
            r.fill_frames_gauge() <= FLOOR + 32,
            "the floor-primed lock seated near the floor (Part 1), not the ceiling"
        );
        (r, out)
    }

    /// Part 1 × #1156 interaction: a FLOOR-SEATED lock (deeper seating from the
    /// floor-prime fix) that underfill-unlocks and RELOCKS within the horizon must
    /// STILL confirm churn and revoke. The deeper seat does not weaken the churn
    /// discriminator — it operates on lock/unlock edges + the unlock count, which
    /// are independent of seat depth.
    #[test]
    fn floor_seated_lock_still_confirms_churn_on_relock() {
        const PERIOD: usize = 256;
        let (mut r, mut out) = build_floor_primed_locked_resampler();
        let mut t = primed_tracker();
        let baseline_unlocks = r.unlock_count();

        // Healthy locked period → no revoke.
        assert_eq!(
            t.step(r.is_locked(), r.unlock_count(), 1, false, true)
                .revoke,
            None
        );

        // STARVE → underfill unlock (arm), no revoke on the falling edge.
        let mut armed = false;
        for _ in 0..64 {
            r.render_period(&mut out);
            let s = t.step(r.is_locked(), r.unlock_count(), 1, false, true);
            assert_eq!(s.revoke, None, "the arming underfill must not revoke");
            if !r.is_locked() {
                assert!(r.unlock_count() > baseline_unlocks);
                assert!(t.pending_strike_armed(), "the underfill armed a strike");
                armed = true;
                break;
            }
        }
        assert!(armed, "the floor-seated lane underfill-unlocked");

        // RE-FEED → relock within the horizon → CONFIRM churn (revoke).
        let mut revoked = None;
        for _ in 0..64 {
            r.push_input(&real_tone(PERIOD * 4));
            r.render_period(&mut out);
            let s = t.step(r.is_locked(), r.unlock_count(), 1, false, true);
            if let Some(reason) = s.revoke {
                revoked = Some(reason);
                assert!(s.rising_edge, "the revoke lands on the relock");
                break;
            }
        }
        assert_eq!(
            revoked,
            Some(RevokeReason::EarlyUnlock),
            "a floor-SEATED lock's unlock→relock churn inside the horizon must still \
             confirm (Part 1's deeper seating does not weaken the #1156 discriminator)"
        );
    }
}
