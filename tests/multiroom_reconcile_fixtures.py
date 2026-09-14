# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared test doubles for jasper.multiroom.reconcile's test suite.

Synthetic GroupingConfig builders and the main()-I/O / active-leader patch
helpers. Consumed by tests/test_multiroom_reconcile.py and by the
cross-module decision-trace pin in docs/adr and PLAN documents for the
reconcile.py split — both import these by name rather than redefining them.
"""

from __future__ import annotations

from jasper.multiroom import reconcile as reconcile_mod
from jasper.multiroom.config import DEFAULT_BUFFER_MS, DEFAULT_CODEC, GroupingConfig
from jasper.multiroom.dac_content_ring import DAC_CONTENT_RING_PERIOD_FRAMES


# ---------- config builders ----------


def _disabled() -> GroupingConfig:
    return GroupingConfig(
        enabled=False,
        role="",
        channel="stereo",
        bond_id="",
        leader_addr="",
        buffer_ms=DEFAULT_BUFFER_MS,
        codec=DEFAULT_CODEC,
        error=None,
    )


def _leader(
    *,
    channel="left",
    bond_id="living-room",
    buffer_ms=DEFAULT_BUFFER_MS,
    codec=DEFAULT_CODEC,
) -> GroupingConfig:
    return GroupingConfig(
        enabled=True,
        role="leader",
        channel=channel,
        bond_id=bond_id,
        leader_addr="",
        buffer_ms=buffer_ms,
        codec=codec,
        error=None,
    )


def _follower(
    *,
    channel="right",
    bond_id="living-room",
    leader_addr="192.168.1.50",
    buffer_ms=DEFAULT_BUFFER_MS,
    codec=DEFAULT_CODEC,
) -> GroupingConfig:
    return GroupingConfig(
        enabled=True,
        role="follower",
        channel=channel,
        bond_id=bond_id,
        leader_addr=leader_addr,
        buffer_ms=buffer_ms,
        codec=codec,
        error=None,
    )


def _invalid() -> GroupingConfig:
    """Enabled but carrying an error (the fail-LOUD state)."""
    return GroupingConfig(
        enabled=True,
        role="leader",
        channel="left",
        bond_id="",
        leader_addr="",
        buffer_ms=DEFAULT_BUFFER_MS,
        codec=DEFAULT_CODEC,
        error="JASPER_GROUPING_BOND_ID is empty (grouping is on)",
    )


# ---------- main(): assembles + writes args BEFORE applying the plan ----------
#
# main() is the I/O entrypoint; here we stub out the real systemctl calls
# (_apply) and the config load, and redirect the args file to a tmp path,
# so we can assert the args-write happens (and is ordered before _apply)
# without touching the host.


def _patch_main_io(monkeypatch, tmp_path, cfg):
    """Redirect ALL of main()'s side effects to a tmp dir + record order.

    Patches: the args + outputd-env files into tmp_path; the box's resolved
    outputd period to the ring's slot; load_config to the synthetic cfg;
    _apply + _restart_outputd
    to order-recording fakes; and the leader_config sync entrypoints to
    spies (main from-imports them at call time, so patching the
    leader_config MODULE attributes intercepts them)."""
    import jasper.multiroom.leader_config as leader_config_mod

    target = tmp_path / "snapcast-args.env"
    monkeypatch.setattr(reconcile_mod, "ARGS_DIR", str(tmp_path))
    monkeypatch.setattr(reconcile_mod, "ARGS_FILE", str(target))
    monkeypatch.setattr(
        reconcile_mod,
        "OUTPUTD_GROUPING_ENV_FILE",
        str(tmp_path / "grouping-outputd.env"),
    )
    monkeypatch.setattr(
        reconcile_mod,
        "VOICE_GROUPING_ENV_FILE",
        str(tmp_path / "grouping-voice.env"),
    )
    monkeypatch.setattr(
        reconcile_mod,
        "AIRPLAY_GROUPING_ENV_FILE",
        str(tmp_path / "grouping-airplay.env"),
    )
    # The box runs the return ring's slot, so the FOURTH arming gate passes and
    # the dumb-member branch is the one under test. The mismatching box has its
    # own test (test_a_period_that_cannot_carry_the_return_ring_stays_solo).
    monkeypatch.setattr(
        reconcile_mod,
        "box_outputd_period_frames",
        lambda: DAC_CONTENT_RING_PERIOD_FRAMES,
    )
    monkeypatch.setattr(
        reconcile_mod,
        "FOLLOWER_STATUS_FILE",
        str(tmp_path / "grouping-follower-status.json"),
    )
    # macOS has no Linux /proc boot_id. Give every reconciler main() test one
    # stable synthetic boot identity; dedicated effective-role tests exercise
    # missing/malformed/mismatched readers.
    monkeypatch.setattr(
        reconcile_mod,
        "read_current_boot_id",
        lambda: "11111111-1111-4111-8111-111111111111",
    )
    # Default to the PASSIVE path (these legacy main() tests assert dumb-member
    # behavior); the active-follower main() flow has its own tests that override
    # this. is_active_speaker_box reads the topology, so stub it for hermeticity.
    monkeypatch.setattr(
        reconcile_mod, "output_topology_state", lambda: (False, True)
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_systemctl_unit_state",
        lambda _query, _unit: False,
    )
    monkeypatch.setattr("jasper.multiroom.config.load_config", lambda *a, **k: cfg)
    # Snapcast provisioning (main() calls it for any enabled bond): default to a
    # present no-op so these tests never shell out to apt. The provisioning tests
    # override it. main() from-imports it, so patch the provision module attr.
    import jasper.multiroom.provision as provision_mod

    monkeypatch.setattr(
        provision_mod,
        "ensure_snapcast_installed",
        lambda **kw: {"state": "present", "detail": ""},
    )

    order: list[str] = []
    real_write = reconcile_mod._write_args_file

    def _spy_write(keys, *, path=str(target)):
        order.append("write")
        return real_write(keys, path=path)

    def _fake_apply(plan_):
        order.append("apply")
        # Assert the args file already exists when _apply runs.
        assert target.exists(), "args file must be written BEFORE _apply"
        return 0

    monkeypatch.setattr(reconcile_mod, "_write_args_file", _spy_write)
    monkeypatch.setattr(reconcile_mod, "_apply", _fake_apply)
    monkeypatch.setattr(
        reconcile_mod,
        "_restart_outputd",
        lambda: order.append("outputd_restart") or True,
    )

    def _fake_restart(unit, *, no_block=False, active_only=False):
        suffix = ":no_block" if no_block else ""
        verb = "try-restart" if active_only else "restart"
        order.append(f"{verb}:{unit}{suffix}")
        return True

    monkeypatch.setattr(reconcile_mod, "_restart_unit", _fake_restart)
    monkeypatch.setattr(
        reconcile_mod,
        "_converge_sources_after_role",
        lambda **_kwargs: (
            order.append(f"start-owner:{reconcile_mod.SOURCE_INTENT_RECONCILE_UNIT}")
            or True
        ),
    )
    monkeypatch.setattr(
        leader_config_mod,
        "apply_bonded_leader_config_sync",
        lambda cfg_: order.append("camilla_bonded") or "bonded.yml",
    )
    monkeypatch.setattr(
        leader_config_mod,
        "restore_solo_config_sync",
        lambda: order.append("camilla_restore_check") and None,
    )
    import jasper.multiroom.snapcast_rpc as snapcast_rpc_mod

    monkeypatch.setattr(
        snapcast_rpc_mod,
        "ensure_groups_on_stream",
        lambda want, **kw: (
            order.append("stream_binding")
            or {
                "reachable": True,
                "groups": 1,
                "fixed": 0,
                "failed": 0,
            }
        ),
    )
    return target, order


def _patch_active_leader(monkeypatch, order):
    """Stub the active-leader config arm + the camilla#2 unit lifecycle into the
    order recorder. main() from-imports these at call time, so patching the
    active_leader_config MODULE attributes (and the reconcile module helpers)
    intercepts them."""
    import jasper.multiroom.active_leader_config as alc_mod

    monkeypatch.setattr(
        alc_mod,
        "precheck_active_leader_sync",
        lambda cfg_: order.append("precheck") or ("bake.yml", "crossover.yml"),
    )
    monkeypatch.setattr(
        alc_mod,
        "apply_active_leader_bake_sync",
        lambda: order.append("bake") or "bake.yml",
    )
    monkeypatch.setattr(
        alc_mod,
        "seed_crossover_statefile",
        lambda *a, **k: order.append("seed") or "crossover-statefile.yml",
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_arm_crossover_unit",
        lambda: order.append("arm_camilla2") or True,
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_disable_crossover_unit",
        lambda: order.append("disable_camilla2") or True,
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_run_audio_hardware_reconcile",
        lambda *, reason: order.append(f"audio_hardware:{reason}") or True,
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_ensure_unit_active",
        lambda unit, *, reason: order.append(f"ensure:{unit}:{reason}") or True,
    )
    # Default: snapserver is up (the bake gate passes). The snapserver-down
    # incident test overrides this. camilla#2 defaults to inactive so the arm
    # path exercises the new positive handle-release barrier.
    monkeypatch.setattr(
        reconcile_mod,
        "_unit_is_active",
        lambda unit: unit == reconcile_mod.SNAPSERVER_UNIT,
    )
    monkeypatch.setattr(
        reconcile_mod,
        "_wait_for_active_content_pcm_release",
        lambda: (
            order.append("probe")
            or reconcile_mod._PcmHandleProbeResult(
                "released",
                "writer_lock_free",
                attempts=1,
                timeout_sec=0.8,
            )
        ),
    )
    return alc_mod
