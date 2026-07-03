# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-language contract for the fan-in -> Camilla transport pipe.

The Rust ``FifoWriter`` writes the fan-in -> Camilla named pipe and the Python
emitter describes it as a CamillaDSP RawFile capture. If the default path, env
names, token, or S32_LE wire width diverge, fan-in writes a pipe nobody reads
or CamillaDSP misreads the byte stream.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jasper.camilla_config_contract import DEFAULT_CAPTURE_FORMAT
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_SHM_RING,
    COUPLING_TRANSPORT_PIPE,
    DEFAULT_FANIN_CAMILLA_PIPE,
    DEFAULT_FANIN_RING_PATH,
    DEFAULT_FANIN_RING_SLOTS,
    PIPE_PATH_ENV_VAR,
    PIPE_WIRE_FORMAT,
    RING_PATH_ENV_VAR,
    RING_SLOTS_ENV_VAR,
    RING_SLOTS_MAX,
    RING_SLOTS_MIN,
    resolve_ring_slots,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FANIN_CONFIG_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "config.rs"
_FANIN_FIFO_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "fifo.rs"
_FANIN_LANE_RESAMPLER_RS = (
    _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "lane_resampler.rs"
)
_FANIN_MIXER_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "mixer.rs"
_FANIN_STATE_RS = _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "state.rs"
_FANIN_HOST_COMPLIANCE_RS = (
    _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "host_compliance.rs"
)


def _config_rs_text() -> str:
    if not _FANIN_CONFIG_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_CONFIG_RS}")
    return _FANIN_CONFIG_RS.read_text(encoding="utf-8")


def _fifo_rs_text() -> str:
    if not _FANIN_FIFO_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_FIFO_RS}")
    return _FANIN_FIFO_RS.read_text(encoding="utf-8")


def _lane_resampler_rs_text() -> str:
    if not _FANIN_LANE_RESAMPLER_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_LANE_RESAMPLER_RS}")
    return _FANIN_LANE_RESAMPLER_RS.read_text(encoding="utf-8")


def _mixer_rs_text() -> str:
    if not _FANIN_MIXER_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_MIXER_RS}")
    return _FANIN_MIXER_RS.read_text(encoding="utf-8")


def _state_rs_text() -> str:
    if not _FANIN_STATE_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_STATE_RS}")
    return _FANIN_STATE_RS.read_text(encoding="utf-8")


def _host_compliance_rs_text() -> str:
    if not _FANIN_HOST_COMPLIANCE_RS.exists():
        pytest.skip(f"rust source not present: {_FANIN_HOST_COMPLIANCE_RS}")
    return _FANIN_HOST_COMPLIANCE_RS.read_text(encoding="utf-8")


def test_default_pipe_path_agrees_between_rust_and_python():
    # The Rust default is a string literal in Config::from_env (env_str fallback).
    text = _config_rs_text()
    assert f'"{DEFAULT_FANIN_CAMILLA_PIPE}"' in text, (
        "Rust jasper-fanin config.rs must default the Camilla pipe to the same "
        f"path Python uses ({DEFAULT_FANIN_CAMILLA_PIPE})"
    )


def test_pipe_path_env_var_name_agrees():
    # Both sides resolve the override from the SAME env var name.
    text = _config_rs_text()
    assert f'"{PIPE_PATH_ENV_VAR}"' in text, (
        f"Rust must read the pipe path override from {PIPE_PATH_ENV_VAR}"
    )


def test_coupling_selector_env_var_name_agrees():
    text = _config_rs_text()
    assert f'"{COUPLING_ENV_VAR}"' in text, (
        f"Rust must read the coupling selector from {COUPLING_ENV_VAR}"
    )


def test_coupling_transport_pipe_token_agrees():
    text = _config_rs_text()
    assert f'Some("{COUPLING_TRANSPORT_PIPE}")' in text, (
        f"Rust coupling parse must accept the {COUPLING_TRANSPORT_PIPE!r} token"
    )
    assert 'Some("fifo") => Coupling::Fifo' not in text


def test_coupling_shm_ring_token_agrees():
    # Ring A: the Rust normalizer MUST accept the same shm_ring token Python's
    # resolve_coupling accepts, or the daemon and the emitted/armed config
    # disagree on the transport of the SHARED realtime capture.
    text = _config_rs_text()
    assert f'Some("{COUPLING_SHM_RING}") => Coupling::ShmRing' in text, (
        f"Rust coupling parse must map the {COUPLING_SHM_RING!r} token to "
        "Coupling::ShmRing"
    )


def test_shm_ring_env_var_names_and_defaults_agree():
    # The Rust daemon resolves the ring path + slot count from the SAME env var
    # names, with the SAME defaults, that Python fanin_coupling exposes — the
    # n_slots <-> JASPER_FANIN_RING_SLOTS pairing is the drift axis.
    text = _config_rs_text()
    assert f'"{RING_PATH_ENV_VAR}"' in text, (
        f"Rust must read the ring path from {RING_PATH_ENV_VAR}"
    )
    assert f'"{RING_SLOTS_ENV_VAR}"' in text, (
        f"Rust must read the ring slots from {RING_SLOTS_ENV_VAR}"
    )
    assert f'"{DEFAULT_FANIN_RING_PATH}"' in text, (
        f"Rust must default the ring path to {DEFAULT_FANIN_RING_PATH}"
    )
    # The default slot count is a bare integer literal in the env_u32 fallback.
    assert f'"{RING_SLOTS_ENV_VAR}", {DEFAULT_FANIN_RING_SLOTS}' in text, (
        f"Rust must default JASPER_FANIN_RING_SLOTS to {DEFAULT_FANIN_RING_SLOTS}"
    )


def test_shm_ring_slots_out_of_range_fails_loud_on_both_sides():
    # SF-1: the JASPER_FANIN_RING_SLOTS normalizer is a declared must-agree axis.
    # BOTH sides fail loud on a present out-of-range value — no silent clamp,
    # per repo doctrine. Otherwise a future arm script that resolved slots via the
    # Python resolver could write an N-slot ioplug conf.d geometry while the
    # daemon refuses to start on the same env (split-brain, fail-closed but
    # maximally confusing on-Pi). This pins the exact agreed behavior:
    #   unset      -> the same default (8) on both sides
    #   2 and 16   -> accepted on both sides
    #   17 (and other out-of-range) -> rejected on both sides

    # Python side (runs live).
    assert resolve_ring_slots(None) == DEFAULT_FANIN_RING_SLOTS
    assert resolve_ring_slots(str(RING_SLOTS_MIN)) == RING_SLOTS_MIN
    assert resolve_ring_slots(str(RING_SLOTS_MAX)) == RING_SLOTS_MAX
    for bad in (RING_SLOTS_MAX + 1, RING_SLOTS_MIN - 1, 0, 100):
        with pytest.raises(ValueError):
            resolve_ring_slots(str(bad))

    # Rust side (source pin — the crate does not build on macOS). The daemon
    # bails on the same range with the same bound constants, and its from_env
    # fail-loud is exercised by the Rust unit test in the CI rust job.
    text = _config_rs_text()
    assert f"pub const RING_SLOTS_MIN: u32 = {RING_SLOTS_MIN};" in text, (
        "Rust RING_SLOTS_MIN must match the Python RING_SLOTS_MIN bound"
    )
    assert f"pub const RING_SLOTS_MAX: u32 = {RING_SLOTS_MAX};" in text, (
        "Rust RING_SLOTS_MAX must match the Python RING_SLOTS_MAX bound"
    )
    # The out-of-range guard bails (anyhow::bail!), it does NOT clamp.
    assert "if !(RING_SLOTS_MIN..=RING_SLOTS_MAX).contains(&ring_slots) {" in text, (
        "Rust must range-check JASPER_FANIN_RING_SLOTS against the shared bounds"
    )
    guard = text.split(
        "if !(RING_SLOTS_MIN..=RING_SLOTS_MAX).contains(&ring_slots) {", 1
    )[1].split("}", 1)[0]
    assert "anyhow::bail!" in guard, (
        "Rust out-of-range ring slots must FAIL LOUD (anyhow::bail!), not clamp"
    )
    assert "clamp" not in guard.lower(), "Rust must not silently clamp ring slots"


def test_shm_ring_status_block_emitted_by_rust_state():
    # The Rust STATUS snapshot emits the ring counter block under shm_ring —
    # the /state.transport + ring:{...} contract the doctor/dashboard read.
    text = _state_rs_text()
    assert '"shm_ring"' in text, "Rust STATUS must echo transport shm_ring"
    assert '"ring":{' in text, "Rust STATUS must emit a ring block"
    for field in (
        "path",
        "slots",
        "occupancy",
        "published",
        "full_waits",
        "drops",
        "mirror_frames",
        "mirror_drops",
    ):
        assert f'"{field}"' in text, f"ring block missing {field!r} key"


def test_shm_ring_mixer_publishes_slots_and_keeps_mirror():
    # The mixer's Output::Ring arm publishes period_frames/128 slots and keeps
    # the lossy aloop mirror (write_music_only-shaped, never the pacer).
    text = _mixer_rs_text()
    assert "Output::Ring" in text
    assert "RingWriter" in text
    assert ".publish(" in text
    # The mirror uses the same write_music_only side-tap shape as the multiroom
    # tap, so it can never back-pressure the loop.
    assert "write_music_only(" in text
    # The 128-frame slot is pinned via the shared RING_SLOT_FRAMES constant.
    assert "RING_SLOT_FRAMES" in text


def test_wire_format_is_s32le_on_both_sides():
    # Python declares the File-capture format as S32_LE (== the shared ALSA
    # capture format); the Rust writer widens i16->i32-LE to match. Pin both.
    assert PIPE_WIRE_FORMAT == "S32_LE"
    assert PIPE_WIRE_FORMAT == DEFAULT_CAPTURE_FORMAT
    fifo_text = _fifo_rs_text()
    # The Rust doc + widening function pin the S32_LE contract; the writer never
    # emits any other width. Assert the doc references the shared constant name
    # so a future format change forces a doc/code update on the Rust side too.
    assert "PIPE_WIRE_FORMAT" in fifo_text
    assert "S32_LE" in fifo_text
    # The widening helper is the actual i16->i32-LE promotion (4 bytes/sample).
    assert "widen_i16_to_i32le" in fifo_text
    assert "S32_BYTES: usize = 4" in fifo_text


def test_input_resampler_status_exports_live_lock_state():
    resampler_text = _lane_resampler_rs_text()
    state_text = _state_rs_text()

    assert "pub locked: Arc<AtomicBool>" in resampler_text
    assert "locked_state.store(true, Ordering::Relaxed)" in resampler_text
    assert "locked_state.store(false, Ordering::Relaxed)" in resampler_text
    assert '"locked"' in state_text
    assert "r.locked.load(Ordering::Relaxed)" in state_text


def test_cushion_decay_held_target_is_single_source_of_truth():
    """The DEFAULT-OFF post-lock cushion decay's held target must be ONE value.

    The resampler owns the live held-target gauge; `hold_fill_frames` reads it (so
    render/trim discipline toward it); the outer host-clock DLL re-pins its
    setpoint from the SAME gauge each tick (never a duplicated config value); and
    STATUS surfaces both the live held target and the decay block. If any of these
    wires drifts, the two controllers can disagree about where the fill sits — the
    documented two-controller oscillation class this design avoids.
    """
    resampler_text = _lane_resampler_rs_text()
    host_clock_text = (
        _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "host_clock.rs"
    ).read_text(encoding="utf-8")
    mixer_text = _mixer_rs_text()
    state_text = _state_rs_text()

    # 1. The resampler OWNS the live held-target gauge, and hold_fill_frames reads
    #    it (the setpoint render_period / trim_ring discipline toward).
    assert "held_target_frames: Arc<AtomicU64>" in resampler_text
    assert "self.held_target_frames.load(Ordering::Relaxed) as usize" in resampler_text, (
        "hold_fill_frames must read the live held-target gauge, not a static field"
    )
    # 2. The decay is a render-PERIOD-clocked pure state machine ticked by the mixer.
    assert "pub fn tick_decay(" in resampler_text
    assert "r.tick_decay(decay_l0, decay_commanded_ppm_abs)" in mixer_text, (
        "the mixer must tick the decay once per render period with the DLL signals"
    )
    # 3. The outer DLL re-pins its setpoint from the SAME live gauge each tick.
    assert "pub held_target_frames: Arc<AtomicU64>" in host_clock_text
    assert "hc.set_target_fill_frames(signals.held_target_frames.load(Ordering::Relaxed)" in (
        host_clock_text
    ), "the servo thread must re-pin its setpoint from the live held-target gauge"
    # 4. STATUS surfaces the live held target AND the decay block (additive).
    assert '"held_target_frames"' in state_text
    assert '"decay":{' in state_text
    assert '"frozen_reason"' in state_text


def test_cushion_decay_notl0_snap_back_is_prime_aware():
    """The `NotL0` snap-back must be PRIME-AWARE — the floor-prime railed-regime fix.

    A floor-primed session locks at the floor, but the host-clock ladder is
    necessarily still Probing (`dll_l0=false`) at session start (l0 arrives only
    after the ~21 s probe). Before this fix the FIRST locked tick took the `NotL0`
    branch and SNAPPED the held target floor→ceiling, railing the resampler's ±500
    ppm authority for ~40 s while it rebuilt the fill — the observed −500 ppm probe
    rail. The fix: while a floor prime is live, the `NotL0` branch HOLDS the floor
    (STATUS `prime_hold`) until the ladder reaches l0. This pins that contract in the
    Rust source so it can't silently regress to the unconditional snap.
    """
    resampler_text = _lane_resampler_rs_text()
    state_text = _state_rs_text()

    # The prime-aware hold branch guards the NotL0 snap on `floor_prime_pending`.
    assert "if self.floor_prime_pending && !s.dll_l0_locked {" in resampler_text, (
        "the NotL0 branch must HOLD the floor while a floor prime is live and the "
        "ladder is still Probing (the floor-prime railed-regime fix)"
    )
    # The unconditional NotL0 snap-back is STILL present for the UNPRIMED case (it is
    # load-bearing for a genuine mid-stream DLL demotion / cold acquisition — the
    # #1145 bit-identical invariant depends on it).
    assert "self.snap_back(DecayFrozenReason::NotL0);" in resampler_text, (
        "the unconditional NotL0 snap must remain for the UNPRIMED case"
    )
    # The honest STATUS token for the primed-holding state exists and rides an
    # append-only wire code (6).
    assert "PrimeHold," in resampler_text, "the PrimeHold frozen-reason variant must exist"
    assert "Some(DecayFrozenReason::PrimeHold) => 6," in resampler_text, (
        "PrimeHold must map to the append-only wire code 6"
    )
    assert '6 => "prime_hold",' in resampler_text, (
        "wire code 6 must render as the prime_hold STATUS token"
    )
    # The token reaches STATUS via the shared code_str mapper (no separate emitter).
    assert "DecayFrozenReason::code_str(" in state_text, (
        "STATUS must render frozen_reason via the shared code_str mapper"
    )


def test_cushion_decay_session_boundary_snap_honours_the_live_proof():
    """Session-boundary paths must snap the held target via the proof-honouring
    primitive, NOT the unconditional ceiling snap.

    Per-session prime-at-floor (PR #1146 → per-session): `reset` (idle/host-pause /
    device-loss) and `unlock_for_underfill` (starvation = the natural session end)
    both call `snap_decay_back_honoring_proof`, which re-primes at the FLOOR when a
    live valid proof is present (skip the ~2.5-min descent every session) and the
    CEILING otherwise. The unconditional `snap_decay_back` (ceiling) is reserved for
    the revocation escape (`snap_decay_to_ceiling`).
    """
    resampler_text = _lane_resampler_rs_text()
    assert "fn snap_decay_back(" in resampler_text
    assert "fn snap_decay_back_honoring_proof(" in resampler_text, (
        "the per-session honouring snap primitive must exist"
    )
    # Both session-boundary paths route through the honouring snap so a live proof
    # re-primes at the floor.
    reset_start = resampler_text.index("pub fn reset(")
    reset_end = resampler_text.index("fn ", reset_start + 10)
    assert "snap_decay_back_honoring_proof" in resampler_text[reset_start:reset_end], (
        "reset() must snap via snap_decay_back_honoring_proof (floor when a proof is live)"
    )
    unlock_start = resampler_text.index("fn unlock_for_underfill(")
    unlock_end = resampler_text.index("fn render_silence(", unlock_start)
    assert (
        "snap_decay_back_honoring_proof" in resampler_text[unlock_start:unlock_end]
    ), (
        "unlock_for_underfill() must snap via snap_decay_back_honoring_proof "
        "(the natural session end honours the live proof)"
    )

    # The revocation escape stays the UNCONDITIONAL ceiling snap — a distrusted
    # proof must always re-acquire deep, never re-prime at the floor.
    ceiling_start = resampler_text.index("pub fn snap_decay_to_ceiling(")
    ceiling_end = resampler_text.index("fn ", ceiling_start + 10)
    ceiling_body = resampler_text[ceiling_start:ceiling_end]
    assert "self.snap_decay_back(" in ceiling_body, (
        "snap_decay_to_ceiling must call the UNCONDITIONAL snap_decay_back (ceiling)"
    )
    assert "snap_decay_back_honoring_proof" not in ceiling_body, (
        "the revoke escape must NOT honour the proof — it always snaps to the ceiling"
    )

    # The proof-validity signal at snap time is the SAME `flag_present` atomic the
    # revoke path clears — single source of truth (no second copy).
    assert "fn live_proof_present(" in resampler_text
    live_start = resampler_text.index("fn live_proof_present(")
    live_end = resampler_text.index("fn ", live_start + 10)
    assert "flag_present" in resampler_text[live_start:live_end], (
        "live_proof_present must read the shared flag_present atomic (the revoke SSOT)"
    )


def test_host_compliance_status_block_and_wiring():
    """The DEFAULT-OFF host-compliance persistence surfaces `resampler.compliance`
    and rides the cushion-decay flag (no new top-level feature gate).

    The prime-at-floor + revalidation feature persists a proof to a
    fan-in-owned JSON file, primes the decay at its floor when a valid proof
    exists, and revokes on the per-session probe fail / DLL demotion / early
    unlock (a probe fail is TWO-strike; DLL demotion / confirmed churn are one).
    STATUS must additively expose the four fields the operator watches
    (`flag_present` / `proved_at` / `revoked_reason_last` /
    `consecutive_failures`); the mixer must service it once per period; and the
    persistence must be gated behind `input_resampler_cushion_decay_enabled`, not
    a separate flag.
    """
    config_text = _config_rs_text()
    mixer_text = _mixer_rs_text()
    state_text = _state_rs_text()
    resampler_text = _lane_resampler_rs_text()

    # 1. STATUS surfaces the additive compliance fields under resampler, including
    #    the two-strike probe-fail counter.
    assert '"compliance":{' in state_text
    for field in (
        '"flag_present"',
        '"proved_at"',
        '"revoked_reason_last"',
        '"consecutive_failures"',
    ):
        assert field in state_text, f"STATUS resampler.compliance must carry {field}"

    # 2. The mixer services the compliance state once per period.
    assert "fn service_host_compliance(" in mixer_text
    assert "self.service_host_compliance(" in mixer_text, (
        "the mixer step must call service_host_compliance once per render period"
    )

    # 3. Persistence rides the cushion-decay flag — NO separate top-level gate.
    assert "config.input_resampler_cushion_decay_enabled" in mixer_text, (
        "prime-at-floor / persistence must be gated behind the cushion-decay flag"
    )

    # 4. The prime-at-floor primitive exists on the resampler and is invoked only
    #    from the gated build helper.
    assert "pub fn prime_decay_at_floor(" in resampler_text
    assert "resampler.prime_decay_at_floor()" in mixer_text, (
        "the gated build helper must prime the decay at the floor for a valid proof"
    )

    # 5. The persistence path is a config knob defaulting under the fan-in state
    #    dir (already-owned write path — no new StateDirectory / privilege grant).
    assert "JASPER_FANIN_HOST_COMPLIANCE_PATH" in config_text
    assert "/var/lib/jasper/fanin/host_compliance.json" in _host_compliance_rs_text(), (
        "the default compliance path must be a sibling of the xrun log under the "
        "fan-in state dir (the already-owned ReadWritePaths=/var/lib/jasper posture)"
    )

    # 6. The write/revoke/strike journal events are present (observability
    #    contract). The two-strike probe-fail RETAIN and the probe-PASS reset are
    #    their own events so an operator can tell a retained strike (proof kept)
    #    from a delete-revoke and a counter reset.
    for event in (
        "event=fanin.host_compliance.written",
        "event=fanin.host_compliance.revoked",
        "event=fanin.host_compliance.prime_at_floor",
        "event=fanin.host_compliance.strike_retained",
        "event=fanin.host_compliance.pass_reset",
    ):
        assert event in mixer_text, f"mixer must emit {event}"

    # 7. The two-strike probe-fail policy is a pure, testable classifier that the
    #    mixer consults: a probe FAIL retains the proof the first time (a
    #    measurement), a DLL demotion / confirmed churn revoke on one strike, and
    #    the proof is deleted only at the strike limit. The mixer keeps
    #    `flag_present` TRUE on a retained strike (so the next session still
    #    primes) — the SSOT interaction with #1154 (a counter=1 session's snap
    #    still lands at the floor).
    host_compliance_text = _host_compliance_rs_text()
    assert "pub fn classify_strike(" in host_compliance_text, (
        "the two-strike policy must be a pure classifier"
    )
    assert "pub const PROBE_FAIL_STRIKE_LIMIT: u32 = 2;" in host_compliance_text, (
        "a probe fail must be two-strike (retain once, delete on the second)"
    )
    assert "RetainWithStrike" in host_compliance_text
    assert "classify_strike(reason, current_failures)" in mixer_text, (
        "the mixer must decide keep-vs-delete via the pure classifier"
    )
    assert "on_strike_retained(" in mixer_text, (
        "a retained probe-fail strike must persist the bumped counter and KEEP "
        "flag_present true (the next session still primes at the floor)"
    )


def test_host_compliance_prime_is_per_session_not_construction_only():
    """The prime-at-floor is PER-SESSION: the session-boundary snap honours the
    live proof and the revalidation `floor_primed` is re-sampled per lock.

    Hardware evidence (jts.local 2026-07-03): a second session in ONE fanin daemon
    lifetime was descending from the full ceiling because the prime happened only at
    lane construction. The fix routes both session-boundary snaps through the
    proof-honouring primitive AND feeds the tracker the LIVE `flag_present` at each
    lock so session B (primed off session A's fresh proof) both seats at the floor
    and runs the one-strike revalidation.
    """
    mixer_text = _mixer_rs_text()
    resampler_text = _lane_resampler_rs_text()
    host_compliance_text = _host_compliance_rs_text()

    # The RevalidationTracker.step takes the live per-lock prime signal, and the
    # mixer feeds it from the SAME flag_present atomic (single source of truth).
    assert "floor_primed_now: bool" in host_compliance_text, (
        "RevalidationTracker::step must take the live per-lock floor_primed signal"
    )
    assert "self.floor_primed = floor_primed_now" in host_compliance_text, (
        "the tracker must re-sample floor_primed at the rising edge (per-lock)"
    )
    assert "hc.obs.flag_present.load(Ordering::Relaxed)" in mixer_text, (
        "the mixer must feed step() the live flag_present (the revoke SSOT), so the "
        "snap destination and the per-lock revalidation gate share one signal"
    )
    # The honouring snap re-primes at the floor via the same prime_at_floor the
    # construction path uses (one floor-prime mechanism, called per session).
    honor_start = resampler_text.index("fn snap_decay_back_honoring_proof(")
    honor_end = resampler_text.index("fn live_proof_present(", honor_start)
    honor_body = resampler_text[honor_start:honor_end]
    assert "self.decay.prime_at_floor()" in honor_body, (
        "the honouring snap must re-prime at the floor via prime_at_floor when a "
        "live proof is present"
    )


def test_servo_thread_exit_clears_reverse_signals():
    """A stopped `fanin-host-clock` servo thread must clear its REVERSE signals.

    The exit path (graceful shutdown OR caught panic) neutralizes the pitch ctl so
    the host free-runs. It must ALSO clear the outer-loop signals the mixer's decay
    tick + compliance revalidation read (`ladder_l0`, `commanded_milli_ppm`, and —
    since the host-compliance persistence landed — `ladder_l2`, `probe_result_code`,
    `probe_response_ratio_milli`). Otherwise a dead thread leaves `ladder_l0=true`
    frozen (driving the thin-cushion free-run churn loop) OR a stale `ladder_l2=true`
    / probe FAIL that would make the compliance revalidation spuriously REVOKE a
    proof for a session whose ladder was fine when the daemon was told to stop —
    revocation must be driven only by a LIVE L2/probe-fail signal, not by the servo
    shutting down. All stores sit AFTER the `catch_unwind` block so they run on both
    exit paths.
    """
    host_clock_text = (
        _REPO_ROOT / "rust" / "jasper-fanin" / "src" / "host_clock.rs"
    ).read_text(encoding="utf-8")
    # The exit-neutralize block ends the thread body; every reverse-signal clear
    # follows the actuator neutralize (so they run on graceful + caught-panic exit).
    exit_start = host_clock_text.index('neutralize_for_exit("shutdown")')
    exit_tail = host_clock_text[exit_start:]
    assert "signals.ladder_l0.store(false, Ordering::Relaxed)" in exit_tail, (
        "servo-thread exit must clear ladder_l0 so a dead thread cannot leave the "
        "decay tick reading a stale l0=true"
    )
    assert "signals.commanded_milli_ppm.store(0, Ordering::Relaxed)" in exit_tail, (
        "servo-thread exit must clear commanded_milli_ppm"
    )
    # The three compliance-revalidation reverse signals must clear on exit too, so a
    # stopped servo cannot leave a stale L2 / probe FAIL that revokes a good proof.
    assert "signals.ladder_l2.store(false, Ordering::Relaxed)" in exit_tail, (
        "servo-thread exit must clear ladder_l2 so a dead thread cannot leave the "
        "compliance revalidation reading a stale l2=true (→ spurious revoke)"
    )
    assert "signals.probe_result_code.store(" in exit_tail, (
        "servo-thread exit must clear probe_result_code (→ ProbeResult::None) so a "
        "dead thread cannot leave a stale probe FAIL that revokes a good proof"
    )
    assert "ProbeResult::None" in exit_tail, (
        "the probe_result_code clear must store the None verdict, not a stale value"
    )
    assert "signals.probe_response_ratio_milli.store(" in exit_tail, (
        "servo-thread exit must clear probe_response_ratio_milli alongside the verdict"
    )


def test_input_resampler_recovery_restarts_capture_pcm():
    text = _mixer_rs_text()
    recovery_start = text.index("fn recover_resampler_input_xrun(")
    recovery_end = text.index("fn read_into_resampler_and_render(", recovery_start)
    recovery_body = text[recovery_start:recovery_end]

    assert ".try_recover(error, true)" in recovery_body
    # `input.pcm` is now `Option<PCM>` (None only on the USB DIRECT lane, which
    # uses recover_direct_xrun instead); the aloop resampler lane binds it and
    # still restarts the capture PCM if a post-recover try_recover left it
    # PREPARED. Assert the state-check + restart on the bound handle.
    assert "pcm.state() != State::Running" in recovery_body
    assert ".start()" in recovery_body
