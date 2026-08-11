# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ring v2 R5b: the arm/confirm gates, and what recovery does when they fail.

Three failure walks the coupling reconciler could not previously get out of:

* an ioplug that cannot PARSE the wire the box resolves (a stale ``.so`` beside
  new daemons — the build degrades to a WARN, so this is the ordinary shape of a
  bad deploy, not an exotic one). CamillaDSP then cannot start against the ring;
* an ALREADY-armed box whose camilla confirm keeps failing. The CONFIRM path
  never reached ``_arm_ring`` or ``_fail_ring_arm``, so it logged and left the
  box exactly as broken as it found it;
* a recovery whose camilla step fails — which is the normal case when the reason
  for recovering is that CamillaDSP cannot start. The env moved to loopback while
  the statefile still named the ring config, so the next start came up on the
  ring again.

Plus the four-ends wire gate that decides the first of those before an arm.
"""

from __future__ import annotations

import pytest

from jasper.fanin.coupling_reconcile import (
    RING_CONFIRM_STRIKE_LIMIT,
    read_persisted_coupling,
    ring_edge_width_ready,
    ring_wire_caps_ready,
)
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
    OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
)

# Reuse the reconcile suite's hermetic env isolation, daemon recorder, and the
# helper that forces the non-wire preflights to pass. Redefining them here would
# be a second answer to "what does an armable box look like".
from tests.test_fanin_coupling_reconcile import (
    SHIPPED_RING_CONF_D,
    _recorder,
    _reconcile,
    _write,
    force_ring_gates_pass,
    isolate_base_jasper_env,
)


@pytest.fixture(autouse=True)
def _isolate_base_jasper_env(tmp_path, monkeypatch):
    """These tests resolve fan-in's wire format through the jasper.env ->
    fanin.env chain, so the developer host's /etc state must not reach them."""
    isolate_base_jasper_env(tmp_path, monkeypatch)


@pytest.fixture
def _ring_assets_present(monkeypatch):
    """Every non-wire ring preflight passes, so these tests exercise the gates
    they are about rather than the asset/geometry gates ahead of them."""
    force_ring_gates_pass(monkeypatch)


def _wide_wire(monkeypatch):
    """Resolve a wire that renders a conf.d field a pre-ring-v2 ioplug refuses."""
    import jasper.fanin_coupling as fc

    monkeypatch.setattr(
        fc,
        "resolve_ring_wire",
        lambda topology=None: fc.RingWire(
            sample_format="S32_LE",
            ring_a_channels=2,
            ring_b_channels=2,
            period_frames=fc.RING_SLOT_FRAMES,
        ),
    )


def _armed_env(tmp_path):
    """The env pair an ALREADY-armed box carries.

    outputd.env must hold the COMPLETE reconciler-owned ring key set, not just
    the bridge: a reconcile that would still move any of them counts as a
    coupling transition and takes ``_arm_ring``, not the CONFIRM path these
    tests are about. Deriving it from ``_outputd_actions`` keeps the fixture
    honest as that set grows.
    """
    import jasper.fanin.coupling_reconcile as cr

    fanin_env = _write(
        tmp_path / "fanin.env", f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n"
    )
    outputd_env = _write(
        tmp_path / "outputd.env",
        cr._apply_actions("", cr._outputd_actions(COUPLING_SHM_RING, ""))[0],
    )
    assert OUTPUTD_CONTENT_BRIDGE_ENV_VAR in outputd_env.read_text()
    return fanin_env, outputd_env


# --- the ioplug CAPABILITY gate ---------------------------------------------


def test_caps_gate_is_inert_on_the_shipped_wire():
    """The dormancy bar for this gate, stated as a behaviour.

    The shipped wire renders no conf.d field beyond the ioplug's own defaults,
    so the gate answers ok WITHOUT reading a provenance record or hashing a
    plugin. That short-circuit is why a fleet that has never written a record is
    completely unaffected by this rung.
    """
    ok, detail = ring_wire_caps_ready()
    assert ok is True
    assert "no conf.d field beyond" in detail


def test_caps_gate_refuses_a_wide_wire_with_no_record(monkeypatch, tmp_path):
    import jasper.ring_assets as ra

    _wide_wire(monkeypatch)
    monkeypatch.setattr(ra, "RING_IOPLUG_PROVENANCE", str(tmp_path / "absent"))
    ok, detail = ring_wire_caps_ready()
    assert ok is False
    assert "no provenance record" in detail


def test_arm_refuses_and_recovers_when_the_ioplug_cannot_parse_the_wire(
    tmp_path, monkeypatch, _ring_assets_present
):
    """The arm-side half: refuse BEFORE bouncing a daemon, recover to loopback."""
    import jasper.ring_assets as ra

    _wide_wire(monkeypatch)
    monkeypatch.setattr(ra, "RING_IOPLUG_PROVENANCE", str(tmp_path / "absent"))
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.ok is False
    assert result.recovered is True
    assert "camilla:shm_ring" not in calls
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK


def test_confirm_recovers_immediately_when_the_ioplug_cannot_parse_the_wire(
    tmp_path, monkeypatch, _ring_assets_present
):
    """THE DEGRADED-DEPLOY WALK, on an ALREADY-armed box.

    A stale ``.so`` beside new daemons leaves CamillaDSP unable to start against
    the ring. The CONFIRM path only re-ran the camilla reconcile — it never
    reached ``_arm_ring`` or ``_fail_ring_arm``, so nothing ever moved the box.
    Re-arming would meet the same ``-EINVAL``, so this recovers to loopback
    DIRECTLY rather than escalating to an arm that cannot succeed.
    """
    import jasper.ring_assets as ra

    _wide_wire(monkeypatch)
    monkeypatch.setattr(ra, "RING_IOPLUG_PROVENANCE", str(tmp_path / "absent"))
    fanin_env, outputd_env = _armed_env(tmp_path)
    calls, ro, rf, rc = _recorder()

    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )

    assert result.direction == "confirm"
    assert result.ok is False
    assert result.recovered is True
    assert "camilla:loopback" in calls
    assert "camilla:shm_ring" not in calls
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK


# --- repeated CONFIRM failure escalates to recovery -------------------------


def _strike_state(monkeypatch, tmp_path):
    path = tmp_path / "strikes.json"
    monkeypatch.setattr(
        "jasper.fanin.coupling_reconcile.RING_CONFIRM_STRIKE_STATE", str(path)
    )
    return path


def _confirm_once(tmp_path, *, camilla_ok):
    """One CONFIRM pass on an armed box.

    ``camilla_ok=False`` fails camilla for the RING coupling only, not for every
    coupling: the walk being modelled is "CamillaDSP cannot load the ring
    config", and a loopback config it also cannot load would be a different
    (and much worse) box. Keeping loopback loadable is what lets the escalation
    prove it actually recovered rather than just attempting to.
    """
    fanin_env, outputd_env = _armed_env(tmp_path)
    calls, ro, rf, rc = _recorder(
        camilla_fail_for=None if camilla_ok else COUPLING_SHM_RING
    )
    result = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )
    return result, calls, fanin_env


def test_a_single_confirm_failure_is_a_strike_not_a_recovery(
    tmp_path, monkeypatch, _ring_assets_present
):
    """One failure can be a transient — it must not disarm a working box."""
    strikes = _strike_state(monkeypatch, tmp_path)
    result, calls, fanin_env = _confirm_once(tmp_path, camilla_ok=False)
    assert result.ok is False
    assert result.recovered is False
    assert result.direction == "confirm"
    # Still armed: the box gets another chance before we take its ring away.
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING
    assert "camilla:loopback" not in calls
    assert strikes.exists()


def test_consecutive_confirm_failures_escalate_to_recovery(
    tmp_path, monkeypatch, _ring_assets_present
):
    """THE HOLE. An armed box whose CamillaDSP cannot load the ring config used
    to sit there logging ``confirm_failed`` with nothing that would ever move it.
    At the strike limit it goes back to loopback — the only state that plays
    audio."""
    _strike_state(monkeypatch, tmp_path)
    for _ in range(RING_CONFIRM_STRIKE_LIMIT - 1):
        _confirm_once(tmp_path, camilla_ok=False)
    result, calls, fanin_env = _confirm_once(tmp_path, camilla_ok=False)

    assert result.ok is False
    assert result.recovered is True
    assert "camilla:loopback" in calls
    assert read_persisted_coupling(fanin_env) == COUPLING_LOOPBACK
    assert "recovered the box to loopback" in result.detail


def test_a_successful_confirm_clears_accumulated_strikes(
    tmp_path, monkeypatch, _ring_assets_present
):
    """Strikes are CONSECUTIVE. Without the clear, a box that failed once months
    ago would be one transient away from being disarmed."""
    strikes = _strike_state(monkeypatch, tmp_path)
    _confirm_once(tmp_path, camilla_ok=False)
    assert strikes.exists()
    result, _calls, fanin_env = _confirm_once(tmp_path, camilla_ok=True)
    assert result.ok is True
    assert not strikes.exists()
    assert read_persisted_coupling(fanin_env) == COUPLING_SHM_RING


def test_a_successful_rearm_clears_accumulated_strikes(
    tmp_path, monkeypatch, _ring_assets_present
):
    """THE POLICY, DRIVEN END TO END: a fresh re-arm resets the strike count.

    Without the clear at the end of ``_arm_ring`` the two-strike policy silently
    degraded to ONE for any box that had failed a confirm before the arm: the
    operator re-arms, CamillaDSP loads the ring config, everything is healthy —
    and the very next transient confirm hits the limit and recovers the box to
    loopback, citing a failure the arm had already fixed.

    The walk is: confirm fails (strike 1) -> a real ARM lands -> one more
    transient confirm failure. That last one must be strike 1 of a NEW incident,
    which means it stays a strike and the box stays armed.
    """
    strikes = _strike_state(monkeypatch, tmp_path)

    _confirm_once(tmp_path, camilla_ok=False)
    assert strikes.exists(), "the pre-arm failure must have recorded a strike"

    # A real ARM: loopback env -> shm_ring, so the coupling MOVES and the
    # reconcile takes _arm_ring rather than the confirm path.
    fanin_env = _write(tmp_path / "fanin.env", "")
    outputd_env = _write(tmp_path / "outputd.env", "")
    calls, ro, rf, rc = _recorder()
    armed = _reconcile(
        COUPLING_SHM_RING,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )
    assert armed.ok is True and armed.direction == "arm", armed.detail
    assert "camilla:shm_ring" in calls
    assert not strikes.exists(), (
        "a completed arm must clear the record — CamillaDSP just loaded the ring "
        "config, which is the success the strike count is counting the absence of"
    )

    # One transient AFTER the arm is strike 1 of a new incident, not the limit.
    result, calls_after, fanin_after = _confirm_once(tmp_path, camilla_ok=False)
    assert result.recovered is False, (
        "a single post-arm transient must not recover the box — that is the "
        "one-strike degradation this clear prevents"
    )
    assert "camilla:loopback" not in calls_after
    assert read_persisted_coupling(fanin_after) == COUPLING_SHM_RING


def test_a_successful_disarm_clears_accumulated_strikes(
    tmp_path, monkeypatch, _ring_assets_present
):
    """Leaving the ring ends the incident the strikes were evidence of.

    Strikes describe an ARMED ring whose confirm keeps failing. Once the box is
    off the ring that evidence is spent, and carrying it forward would make the
    next arm inherit a head start toward being recovered.
    """
    strikes = _strike_state(monkeypatch, tmp_path)
    _confirm_once(tmp_path, camilla_ok=False)
    assert strikes.exists()

    fanin_env, outputd_env = _armed_env(tmp_path)
    calls, ro, rf, rc = _recorder()
    result = _reconcile(
        COUPLING_LOOPBACK,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )
    assert result.ok is True and result.direction == "disarm", result.detail
    assert "camilla:loopback" in calls
    assert not strikes.exists()


def test_a_partial_disarm_keeps_the_strike_record(
    tmp_path, monkeypatch, _ring_assets_present
):
    """The clear is gated on the disarm SUCCEEDING, and that boundary matters.

    A partial disarm can leave part of the box still on the ring, so the
    accumulated evidence is still about the live state. Clearing there would
    throw away the strikes on exactly the box whose disarm did not land.
    """
    strikes = _strike_state(monkeypatch, tmp_path)
    _confirm_once(tmp_path, camilla_ok=False)
    assert strikes.exists()

    fanin_env, outputd_env = _armed_env(tmp_path)
    _calls, ro, rf, rc = _recorder(fanin_ok=False)
    result = _reconcile(
        COUPLING_LOOPBACK,
        fanin_env=fanin_env,
        outputd_env=outputd_env,
        restart_outputd=ro,
        restart_fanin=rf,
        reconcile_camilla=rc,
        active_leader_check=lambda: False,
    )
    assert result.ok is False and result.direction == "disarm"
    assert strikes.exists(), "a partial disarm must not discard live evidence"


def test_strikes_outside_the_window_are_not_counted(tmp_path, monkeypatch):
    """Evidence ages out. The reconciler is event-driven — boot, deploy, a
    /sources/ toggle — so two failures a year apart are two incidents, not a
    pattern."""
    import json
    import time

    import jasper.fanin.coupling_reconcile as cr

    path = _strike_state(monkeypatch, tmp_path)
    path.write_text(
        json.dumps(
            {
                "count": 99,
                "first_ts": time.time() - cr.RING_CONFIRM_STRIKE_WINDOW_SEC - 1,
            }
        )
    )
    assert cr._read_ring_confirm_strikes(str(path)) == 0


# --- recovery re-seeds the statefile when camilla is unreachable ------------


def test_recovery_reseeds_the_statefile_when_the_camilla_reconcile_fails(
    tmp_path, monkeypatch
):
    """THE CONVERGENCE FIX.

    Recovery's camilla step goes over the daemon's websocket. When the reason we
    are recovering is that CamillaDSP cannot START, there is no websocket: the
    reconcile fails, the statefile still names the ring config, and the next
    start comes up on the same ring and fails the same way. The env said
    loopback; the graph never moved.
    """
    import jasper.fanin.coupling_reconcile as cr

    seeded: list[str] = []
    monkeypatch.setattr(
        cr,
        "_reseed_loopback_statefile",
        lambda reason: (seeded.append(reason) or (True, "re-seeded")),
    )
    _calls, ro, rf, rc = _recorder(camilla_ok=False)
    ok = cr._recover_to_loopback(
        rf, ro, rc, tmp_path / "fanin.env", tmp_path / "outputd.env", "t"
    )
    assert ok is False  # the camilla step genuinely failed
    assert seeded == ["t"], "a failed camilla step must re-seed the statefile"


def test_recovery_does_not_reseed_when_camilla_answered(tmp_path, monkeypatch):
    """The live re-point is authoritative when it lands; writing the statefile on
    top of a successful reconcile would be a second writer of one fact."""
    import jasper.fanin.coupling_reconcile as cr

    seeded: list[str] = []
    monkeypatch.setattr(
        cr,
        "_reseed_loopback_statefile",
        lambda reason: (seeded.append(reason) or (True, "re-seeded")),
    )
    _calls, ro, rf, rc = _recorder()
    ok = cr._recover_to_loopback(
        rf, ro, rc, tmp_path / "fanin.env", tmp_path / "outputd.env", "t"
    )
    assert ok is True
    assert seeded == []


def test_reseed_pins_the_coupling_to_loopback(monkeypatch):
    """It pins ``coupling=loopback`` rather than re-reading the persisted token.

    Recovery must not re-select the ring graph it is recovering FROM. Reading
    the persisted coupling here would do exactly that on the CONFIRM path, where
    the env still says ``shm_ring`` at the moment the decision is taken.
    """
    import jasper.fanin.coupling_reconcile as cr
    from jasper.active_speaker import runtime_contract as rc_mod
    from jasper.output_topology import OutputTopologyError

    seen: dict[str, object] = {}

    def _decide(topology=None, **kwargs):
        seen.update(kwargs)
        raise OutputTopologyError("saved topology is corrupt")

    monkeypatch.setattr(rc_mod, "safe_graph_for_current_topology", _decide)
    ok, detail = cr._reseed_loopback_statefile("t")
    assert seen["coupling"] == COUPLING_LOOPBACK
    assert ok is False
    assert "saved topology is corrupt" in detail


def test_reseed_swallows_realistic_failures_but_not_programming_errors(monkeypatch):
    """The catch is a CONCRETE set, and the boundary is deliberate.

    A corrupt topology or an unwritable statefile is an operational condition
    that must not blow up a recovery in progress — it is reported and the
    recovery continues to its daemon ops. A ``RuntimeError`` is not that: it is
    a bug in this code path, and swallowing it would hide the bug behind a
    recovery that said "re-seed failed" and carried on. Pinning both halves is
    what stops the catch from silently widening back to a blind except.
    """
    import jasper.fanin.coupling_reconcile as cr
    from jasper.active_speaker import runtime_contract as rc_mod

    def _raise(exc):
        def _decide(topology=None, **kwargs):
            raise exc

        return _decide

    monkeypatch.setattr(
        rc_mod, "safe_graph_for_current_topology", _raise(OSError("read-only fs"))
    )
    ok, detail = cr._reseed_loopback_statefile("t")
    assert ok is False
    assert "read-only fs" in detail

    monkeypatch.setattr(
        rc_mod, "safe_graph_for_current_topology", _raise(RuntimeError("bug"))
    )
    with pytest.raises(RuntimeError):
        cr._reseed_loopback_statefile("t")


# --- the slot migration declines when the WIRE is sheared -------------------


def _migrate(tmp_path, monkeypatch, *, fanin_text: str):
    """Run the slot migration against ``fanin_text`` on the SHIPPED conf.d.

    Returns (post_migration_text, records) where ``records`` is the log_event
    result tokens the migration emitted — the migration's only externally
    visible statement about what it decided.
    """
    import jasper.fanin.coupling_reconcile as cr
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    path = _write(tmp_path / "fanin.env", fanin_text)

    records: list[str] = []
    real_log_event = cr.log_event

    def _capture(logger, event, **kw):
        records.append(str(kw.get("result", "")))
        return real_log_event(logger, event, **kw)

    monkeypatch.setattr(cr, "log_event", _capture)
    snapshot = cr._read_snapshot(path)
    result = cr._migrate_stale_fanin_ring_slots(snapshot, "t")
    return result.text, records


def test_slot_migration_writes_the_coherent_value_when_only_slots_are_stale(
    tmp_path, monkeypatch
):
    """POSITIVE CONTROL for the decline below: the migration does fire.

    Without this, a decline test proves only that the function wrote nothing —
    which a broken migration that never writes would also satisfy.
    """
    from jasper.fanin_coupling import RING_SLOTS_ENV_VAR

    text, records = _migrate(
        tmp_path, monkeypatch, fanin_text=f"{RING_SLOTS_ENV_VAR}=8\n"
    )
    assert "stale_ring_slots_overridden" in records
    assert f"{RING_SLOTS_ENV_VAR}=2" in text


def test_slot_migration_declines_when_the_wire_format_is_sheared(
    tmp_path, monkeypatch
):
    """It does not converge an axis it does not own, and says so.

    Writing the slot count while fan-in and the conf.d disagree about the WIRE
    would make the geometry look repaired — the operator reads
    ``stale_ring_slots_overridden`` as progress — while the arm still cannot
    succeed. The wire gate is the one that refuses with the reason that actually
    describes the box, so the migration steps aside and leaves it to say so.
    """
    from jasper.fanin_coupling import RING_SLOTS_ENV_VAR, RING_WIRE_FORMAT_ENV_VAR

    text, records = _migrate(
        tmp_path,
        monkeypatch,
        # Both true at once: a stale slot count the migration WOULD write, and a
        # wire shear that must stop it.
        fanin_text=(
            f"{RING_SLOTS_ENV_VAR}=8\n{RING_WIRE_FORMAT_ENV_VAR}=S32_LE\n"
        ),
    )
    assert "stale_ring_slots_override_declined" in records
    assert "stale_ring_slots_overridden" not in records
    assert f"{RING_SLOTS_ENV_VAR}=8" in text, (
        "the declined migration must leave the stale value alone, not half-write it"
    )


def test_slot_migration_declines_on_a_sheared_channel_count(tmp_path, monkeypatch):
    """The channels axis declines the write for the same reason the format does."""
    from jasper.fanin_coupling import RING_SLOTS_ENV_VAR
    import jasper.ring_assets as ra

    # A conf.d whose Ring-A block declares a channel count fan-in's fixed-stereo
    # mixer cannot produce.
    conf = tmp_path / "sheared.conf"
    conf.write_text(
        "pcm.jts_ring_capture {\n    period_frames 128\n    n_slots 2\n"
        "    channels 4\n}\n"
        "pcm.jts_ring_playback {\n    period_frames 128\n    n_slots 2\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(conf))

    import jasper.fanin.coupling_reconcile as cr

    path = _write(tmp_path / "fanin.env", f"{RING_SLOTS_ENV_VAR}=8\n")
    records: list[str] = []
    real_log_event = cr.log_event
    monkeypatch.setattr(
        cr,
        "log_event",
        lambda logger, event, **kw: (
            records.append(str(kw.get("result", ""))) or real_log_event(
                logger, event, **kw
            )
        ),
    )
    out = cr._migrate_stale_fanin_ring_slots(cr._read_snapshot(path), "t")
    assert "stale_ring_slots_override_declined" in records
    assert f"{RING_SLOTS_ENV_VAR}=8" in out.text


# --- the sample_format axis at BOTH on-disk-header consumers ----------------


def _ring_file(path, *, sample_format, n_slots=2, period=128, channels=2):
    """Write a valid ring header (JRIN magic) with the given geometry."""
    import struct

    import jasper.ring_assets as ra

    hdr = bytearray(ra._RING_HEADER_BYTES)
    struct.pack_into("<I", hdr, ra._RING_OFF_MAGIC, 0x4A52_494E)
    struct.pack_into("<I", hdr, ra._RING_OFF_VERSION, 1)
    struct.pack_into("<I", hdr, ra._RING_OFF_RATE, 48000)
    struct.pack_into("<I", hdr, ra._RING_OFF_CHANNELS, channels)
    struct.pack_into("<I", hdr, ra._RING_OFF_SAMPLE_FORMAT, sample_format)
    struct.pack_into("<I", hdr, ra._RING_OFF_PERIOD_FRAMES, period)
    struct.pack_into("<I", hdr, ra._RING_OFF_N_SLOTS, n_slots)
    path.write_bytes(bytes(hdr) + b"\x00" * 256)
    return path


def _point_ring_files_at(monkeypatch, tmp_path):
    """Repoint both ring files into the tmpdir. Returns (ring_a, ring_b)."""
    import jasper.ring_assets as ra

    ring_a = tmp_path / "program.ring"
    ring_b = tmp_path / "content.ring"
    monkeypatch.setattr(ra, "RING_A_PROGRAM_FILE", str(ring_a))
    monkeypatch.setattr(ra, "RING_B_CONTENT_FILE", str(ring_b))
    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    return ring_a, ring_b


def test_stale_file_guard_deletes_a_format_mismatched_ring(tmp_path, monkeypatch):
    """WHAT MAKES THE WIRE ROLLBACK LEVER ONE-SHOT-ABLE, as a behaviour.

    While this guard was blind to ``sample_format``, forcing the wire narrow
    again left the WIDE file on disk: the writer rejected it at attach as a
    config-class fault and the box PARKED until someone ran ``rm`` by hand — so
    the lever worked once and then needed an operator. The file must be cleared
    here, on an axis where slots and period both still match.
    """
    import jasper.fanin.coupling_reconcile as cr
    import jasper.ring_assets as ra

    ring_a, ring_b = _point_ring_files_at(monkeypatch, tmp_path)
    # Slots and period MATCH the shipped conf.d; only the format is wide. A guard
    # that compared the old two axes would leave this file in place.
    _ring_file(ring_a, sample_format=ra.RING_SAMPLE_FORMAT_S32LE)
    _ring_file(ring_b, sample_format=ra.RING_SAMPLE_FORMAT_S16LE)

    cr._delete_stale_ring_files("t", "")

    assert not ring_a.exists(), "a format-stale ring file must be deleted"
    assert ring_b.exists(), "a coherent ring file must be left alone"


def test_confirm_self_heal_escalates_on_a_format_mismatched_ring(
    tmp_path, monkeypatch
):
    """The SECOND consumer of the same comparator must agree with the first.

    The CONFIRM-path predicate decides whether to escalate to the arm that runs
    the delete above. If it were blind to ``sample_format`` while the delete was
    not, a format-stale file would read as coherent, the escalation would never
    fire, and the file the arm is ready to remove would sit there — the two
    halves meaning different things by "coherent" is exactly the drift the
    shared comparator exists to prevent.
    """
    import jasper.fanin.coupling_reconcile as cr
    import jasper.ring_assets as ra

    ring_a, ring_b = _point_ring_files_at(monkeypatch, tmp_path)
    _ring_file(ring_b, sample_format=ra.RING_SAMPLE_FORMAT_S16LE)

    # Coherent on every axis -> stay lightweight (the positive control).
    _ring_file(ring_a, sample_format=ra.RING_SAMPLE_FORMAT_S16LE)
    needed, detail = cr._ring_confirm_needs_self_heal("")
    assert needed is False, detail

    # Same file, wide format -> escalate, naming the axis.
    _ring_file(ring_a, sample_format=ra.RING_SAMPLE_FORMAT_S32LE)
    needed, detail = cr._ring_confirm_needs_self_heal("")
    assert needed is True
    assert "sample_format" in detail
    assert "stale-file self-heal" in detail


# --- the four-ends wire gate, per end ---------------------------------------


def test_wire_gate_names_the_end_that_disagrees(monkeypatch):
    """A refusal must say WHICH end declared what. A bare "mismatch" leaves an
    operator with four files to read and no order to read them in."""
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(
        fanin_text="JASPER_FANIN_RING_WIRE_FORMAT=S32_LE\n", outputd_text=""
    )
    assert ok is False
    assert "fan-in (Ring A writer)" in detail
    assert "S32_LE" in detail


def test_wire_gate_compares_outputd_only_once_armed(monkeypatch):
    """THE PR-1 DEFECT, structurally prevented.

    ``JASPER_OUTPUTD_CONTENT_FORMAT`` is written by the audio-hardware
    reconciler, not by this one, and on a box still on loopback it correctly
    carries the LOOPBACK lane's format. Comparing it at preflight would refuse
    the arm on every box in the fleet — the exact shape of the defect this
    gate's history records. Same file, two verdicts, decided by whether the box
    is already armed.
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    loopback_outputd = "JASPER_OUTPUTD_CONTENT_FORMAT=S32_LE\n"

    ok_unarmed, _ = ring_edge_width_ready(fanin_text="", outputd_text=loopback_outputd)
    assert ok_unarmed is True, "a not-yet-armed box must not be refused for this"

    ok_armed, detail = ring_edge_width_ready(
        fanin_text=f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n",
        outputd_text=loopback_outputd,
    )
    assert ok_armed is False
    assert "outputd (Ring B reader)" in detail


def test_wire_gate_reads_an_absent_outputd_key_as_the_daemon_default(monkeypatch):
    """An unset ``JASPER_OUTPUTD_CONTENT_FORMAT`` DECLARES ``S16_LE`` — that is
    outputd's own documented fallback, not an unknown. Reading absence as
    indeterminate would refuse the arm on every armed box whose hardware
    reconciler has not written the key, for a wire the daemon would in fact have
    declared correctly."""
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(
        fanin_text=f"{COUPLING_ENV_VAR}={COUPLING_SHM_RING}\n", outputd_text=""
    )
    assert ok is True, detail


def test_wire_gate_refuses_an_outputd_channel_width_the_ring_does_not_carry(
    monkeypatch,
):
    """The channels axis has teeth independently of the format axis."""
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(
        fanin_text="", outputd_text="JASPER_OUTPUTD_ACTIVE_CHANNELS=6\n"
    )
    assert ok is False
    assert "6 channels" in detail
    assert "outputd (Ring B reader)" in detail


def test_wire_gate_defers_an_absent_conf_d_to_the_asset_gate(monkeypatch, tmp_path):
    """One missing file, one reason. ``ring_assets_ready`` owns the absent
    conf.d; a second refusal here would bury the one that names the fix."""
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(tmp_path / "nope.conf"))
    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, False, True)
    )
    ok, _ = ring_edge_width_ready(fanin_text="", outputd_text="")
    assert ok is True


def test_wire_gate_refuses_a_conf_d_that_is_present_but_unreadable(
    monkeypatch, tmp_path
):
    """A torn conf.d is nobody else's refusal to own, so it stays this gate's."""
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(tmp_path / "torn.conf"))
    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")
    assert ok is False
    assert "declares no format at all" in detail


def test_wire_gate_refuses_an_indeterminate_channel_count_like_an_indeterminate_format(
    monkeypatch, tmp_path
):
    """SYMMETRY. The two axes must treat "cannot be read" the same way.

    The reachable shape is a PRESENT conf.d whose block declares ``channels``
    twice with different values — ``ring_conf_channels`` answers None for
    exactly that torn file. The format axis already refused such a block; the
    channels axis passed it silently, so a box could arm on a channel count
    nothing had actually agreed. Note the format here is single and CORRECT, so
    the refusal can only be coming from the channels axis.
    """
    import jasper.ring_assets as ra

    torn = tmp_path / "torn.conf"
    torn.write_text(
        "pcm.jts_ring_capture {\n    period_frames 128\n    n_slots 2\n"
        "    format S16_LE\n    channels 2\n    channels 4\n}\n"
        "pcm.jts_ring_playback {\n    period_frames 128\n    n_slots 2\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(ra, "RING_CONF_D", str(torn))
    monkeypatch.setattr(
        ra, "ring_asset_presence", lambda **kw: ra.RingAssetPresence(True, True, True)
    )
    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")
    assert ok is False
    assert "declares no channel count at all" in detail
    assert "jts_ring_capture" in detail
    # The format axis is fine on this file — a refusal citing it would mean the
    # test proved the wrong branch.
    assert "declares no format at all" not in detail


def test_wire_gate_refuses_an_outputd_channel_count_that_will_not_parse(monkeypatch):
    """The other reachable indeterminate: a malformed outputd channels value.

    This is why the excuse is a PER-AXIS flag rather than a reuse of ``note``.
    The outputd end carries a note explaining why its FORMAT is not compared
    before arming — and if that note also excused its channels, a value that
    will not parse as an int would pass here on every unarmed box, which is
    every box about to arm.
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(
        fanin_text="", outputd_text="JASPER_OUTPUTD_ACTIVE_CHANNELS=stereo\n"
    )
    assert ok is False
    assert "outputd (Ring B reader)" in detail
    assert "declares no channel count at all" in detail


def test_wire_gate_does_not_invent_a_channels_refusal_for_ends_that_state_none(
    monkeypatch,
):
    """The CONTROL for the symmetry above: two ends legitimately say nothing.

    ``CamillaDSP emitted stanzas`` carries a format and no channel count — the
    coupling's kwargs simply have none — and an ABSENT conf.d states nothing on
    either axis while the asset gate owns that refusal. Neither may be reported
    as indeterminate, or the shipped fleet fails a gate it has always passed.
    """
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")
    assert ok is True, detail


def test_wire_gate_passes_on_the_shipped_wire(monkeypatch):
    """The dormancy bar for the wire gate: a fleet box declares one wire at every
    end, so nothing about this rung changes what it does."""
    import jasper.ring_assets as ra

    monkeypatch.setattr(ra, "RING_CONF_D", str(SHIPPED_RING_CONF_D))
    ok, detail = ring_edge_width_ready(fanin_text="", outputd_text="")
    assert ok is True, detail
    assert "all declaring ends state one ring wire" in detail
