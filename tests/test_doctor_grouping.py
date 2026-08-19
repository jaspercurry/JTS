# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor grouping domain."""

from types import SimpleNamespace

from jasper.cli import doctor


from .doctor_test_support import (
    _grouping_cfg,
    _registered_check_names,
)

# ----------------------------------------------------------------- grouping


def _patch_grouping(monkeypatch, cfg, is_active_stdout=None, *, unit_states=None):
    """Stub the grouping check's IO. Prefer `unit_states` (a unit→state
    mapping; unlisted units default to "inactive") — the plan carries the
    dumb-follower park/restore intents, so positional stdout lines are
    brittle against the unit list growing."""
    import jasper.multiroom.config as mr_config

    monkeypatch.setattr(mr_config, "load_config", lambda *a, **k: cfg)

    def fake_run(argv, *a, **kw):
        class FakeRun:
            stdout = ""

        if unit_states is not None and list(argv[:2]) == ["systemctl", "is-active"]:
            FakeRun.stdout = (
                "\n".join(unit_states.get(u, "inactive") for u in argv[2:]) + "\n"
            )
        elif is_active_stdout is not None:
            FakeRun.stdout = is_active_stdout
        return FakeRun()

    monkeypatch.setattr(doctor.grouping, "_run", fake_run)
    # No producer-feed stubbing: check_grouping injects leader_tap_path=""
    # unconditionally (no music producer exists yet — HANDOFF-multiroom.md
    # §2, Increments 3–5), so a bonded leader honestly derives degraded.


def test_check_grouping_off_is_ok(monkeypatch):
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=False), "")
    r = doctor.check_grouping()
    assert r.status == "ok"
    assert "single-speaker" in r.detail


def test_check_snapcast_off_skips(monkeypatch):
    """Grouping off → snapcast deliberately not installed; skip (ok)."""
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=False), "")
    monkeypatch.setattr("shutil.which", lambda name: None)
    r = doctor.check_grouping_snapcast_installed()
    assert r.status == "ok"
    assert "grouping off" in r.detail


def test_check_snapcast_present_is_ok(monkeypatch):
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=True, role="leader"), "")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    r = doctor.check_grouping_snapcast_installed()
    assert r.status == "ok"
    assert "present" in r.detail


def test_check_snapcast_missing_fails_with_remediation(monkeypatch):
    """The JTS5 root state (2026-06-23): grouping enabled but the snapcast
    binaries are still absent. Surface it as a FAIL carrying the install command,
    so a failed or skipped provisioning step cannot stay invisible."""
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=True, role="leader"), "")
    monkeypatch.setattr("shutil.which", lambda name: None)
    r = doctor.check_grouping_snapcast_installed()
    assert r.status == "fail"
    assert "snapserver" in r.detail and "snapclient" in r.detail
    assert "apt install" in r.detail


# --- snapclient version drift — a live probe (bounded), never a record;
# mirrors check_grouping_snapcast_installed's grouping-off skip.


def test_check_snapcast_version_registered():
    assert "check_grouping_snapcast_version" in _registered_check_names()


def test_check_snapcast_version_off_skips(monkeypatch):
    """Mirrors check_grouping_snapcast_installed's grouping-off skip — a
    warn about a disabled subsystem's version is not actionable."""
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=False), "")
    r = doctor.check_grouping_snapcast_version()
    assert r.status == "ok"
    assert "grouping off" in r.detail


def test_check_snapcast_version_not_installed_skips(monkeypatch):
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=True, role="leader"), "")
    monkeypatch.setattr("shutil.which", lambda name: None)
    r = doctor.check_grouping_snapcast_version()
    assert r.status == "ok"
    assert "not installed" in r.detail


def test_check_snapcast_version_unparseable_skips(monkeypatch):
    """Output with no version-shaped token skips (ok) — never manufacture a
    warning from a fact the probe could not determine."""
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=True, role="leader"), "")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        doctor.grouping, "_run",
        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="not a version\n", stderr=""),
    )
    r = doctor.check_grouping_snapcast_version()
    assert r.status == "ok"
    assert "could not determine" in r.detail


def test_check_snapcast_version_match_is_ok(monkeypatch):
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=True, role="leader"), "")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        doctor.grouping, "_run",
        lambda argv, **kw: SimpleNamespace(
            returncode=0, stdout="snapclient v0.31.0\n", stderr="",
        ),
    )
    r = doctor.check_grouping_snapcast_version()
    assert r.status == "ok"
    assert "0.31.0" in r.detail


def test_check_snapcast_version_mismatch_warns(monkeypatch):
    """The drift case: an installed version that differs from
    VALIDATED_SNAPCAST_VERSION warns (visibility), it does not fail — no
    apt pin exists to enforce it."""
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=True, role="leader"), "")
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        doctor.grouping, "_run",
        lambda argv, **kw: SimpleNamespace(
            returncode=0, stdout="snapclient v0.32.1\n", stderr="",
        ),
    )
    r = doctor.check_grouping_snapcast_version()
    assert r.status == "warn"
    assert "0.32.1" in r.detail and "0.31.0" in r.detail


def test_check_household_credential_solo_is_ok(monkeypatch):
    # Solo short-circuits before reading the secret file (a lone speaker needs
    # no household credential).
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=False), "")
    r = doctor.check_grouping_household_credential()
    assert r.status == "ok"
    assert "solo" in r.detail


def test_check_household_credential_bonded_paired_ok(monkeypatch, tmp_path):
    import jasper.control.household_credential as hc

    secret = tmp_path / "household_secret"
    secret.write_text("s\n")
    monkeypatch.setattr(hc, "SECRET_FILE", str(secret))
    _patch_grouping(
        monkeypatch,
        _grouping_cfg(enabled=True, role="leader", channel="left", bond_id="x"),
        "",
    )
    r = doctor.check_grouping_household_credential()
    assert r.status == "ok"
    assert "present" in r.detail


def test_check_household_credential_bonded_unpaired_warns(monkeypatch, tmp_path):
    """RECOVERY drift: bonded but the secret was lost -> /grouping/set is
    fail-safe-open; the doctor is the only place that loss is visible."""
    import jasper.control.household_credential as hc

    monkeypatch.setattr(hc, "SECRET_FILE", str(tmp_path / "absent"))
    _patch_grouping(
        monkeypatch,
        _grouping_cfg(
            enabled=True,
            role="follower",
            channel="right",
            bond_id="x",
            leader_addr="192.168.1.50",
        ),
        "",
    )
    r = doctor.check_grouping_household_credential()
    assert r.status == "warn"
    assert "household credential is missing" in r.detail
    assert "/rooms" in r.detail


# --- camilla#2 endpoint-crossover unit (Stage B B1, INERT) ----------------


def test_crossover_unit_check_registered():
    assert "check_crossover_unit_installed" in _registered_check_names()


def test_crossover_unit_solo_is_ok(monkeypatch):
    # Not an active member -> skip before touching topology or systemd.
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=False), "")
    r = doctor.check_crossover_unit_installed()
    assert r.status == "ok"
    assert "not an active bond leader" in r.detail


def test_crossover_unit_follower_is_ok(monkeypatch):
    # An ACTIVE follower is not the leader half of the pair; camilla#2 is the
    # leader's instance, so a follower skips.
    _patch_grouping(
        monkeypatch,
        _grouping_cfg(
            enabled=True,
            role="follower",
            channel="right",
            bond_id="x",
            leader_addr="192.168.1.50",
        ),
        "",
    )
    r = doctor.check_crossover_unit_installed()
    assert r.status == "ok"
    assert "not an active bond leader" in r.detail


def test_crossover_unit_passive_leader_is_ok(monkeypatch, tmp_path):
    # A bonded LEADER whose output topology has NO roleful/protected outputs
    # is a passive leader — it runs no per-driver crossover, so camilla#2 is
    # n/a and the check skips with ok.
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(_topology([]), path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    _patch_grouping(
        monkeypatch,
        _grouping_cfg(enabled=True, role="leader", channel="left", bond_id="x"),
        "",
    )
    r = doctor.check_crossover_unit_installed()
    assert r.status == "ok"
    assert "passive leader" in r.detail


def _active_leader_topology(monkeypatch, tmp_path):
    """Set up an ACTIVE-LEADER context: a roleful/protected output topology
    plus a bonded-leader grouping config. Leaves `doctor.grouping._run`
    patchable by the caller for the systemd probes."""
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _active_topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(_active_topology("mono", "active_2_way"), path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    import jasper.multiroom.config as mr_config

    cfg = _grouping_cfg(enabled=True, role="leader", channel="left", bond_id="x")
    monkeypatch.setattr(mr_config, "load_config", lambda *a, **k: cfg)


def test_crossover_unit_active_leader_warns_when_missing(monkeypatch, tmp_path):
    # Active leader but the unit is not installed (systemctl cat nonzero) ->
    # a real gap the reconciler PR would have nothing to arm.
    _active_leader_topology(monkeypatch, tmp_path)

    def fake_run(argv, *a, **kw):
        class R:
            returncode = 1
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(doctor.grouping, "_run", fake_run)
    r = doctor.check_crossover_unit_installed()
    assert r.status == "warn"
    assert "jasper-camilla-crossover.service is not installed" in r.detail


def test_crossover_unit_active_leader_ok_when_installed(monkeypatch, tmp_path):
    # Active leader, unit installed (systemctl cat ok) and parseable
    # (systemd-analyze verify ok) -> ok, and the message says INERT.
    _active_leader_topology(monkeypatch, tmp_path)

    def fake_run(argv, *a, **kw):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(doctor.grouping, "_run", fake_run)
    # Force the "systemd-analyze present" branch deterministically.
    monkeypatch.setattr(
        doctor.grouping.shutil, "which", lambda _name: "/usr/bin/systemd-analyze"
    )
    r = doctor.check_crossover_unit_installed()
    assert r.status == "ok"
    assert "INERT" in r.detail


def test_crossover_unit_active_leader_ok_without_systemd_analyze(monkeypatch, tmp_path):
    # Active leader, unit installed, but systemd-analyze unavailable (dev box)
    # -> ok with an explicit "parse unchecked" note; never a false warn.
    _active_leader_topology(monkeypatch, tmp_path)

    def fake_run(argv, *a, **kw):
        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return R()

    monkeypatch.setattr(doctor.grouping, "_run", fake_run)
    monkeypatch.setattr(doctor.grouping.shutil, "which", lambda _name: None)
    r = doctor.check_crossover_unit_installed()
    assert r.status == "ok"
    assert "parse unchecked" in r.detail


# --- local-vs-wireless sub coexistence (B3) -------------------------------


def _set_local_sub_topology(monkeypatch, tmp_path, *, with_sub: bool):
    """Persist a topology to a tmp path and point the loader at it. A
    subwoofer topology carries routing.subwoofer_group_ids; the no-sub case
    points at a missing file (loads as an empty draft → no subwoofer groups)."""
    topology_path = tmp_path / "output_topology.json"
    if with_sub:
        from jasper.output_topology import save_output_topology
        from tests.test_active_speaker_runtime_contract import _subwoofer_topology

        save_output_topology(_subwoofer_topology(), path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))


def test_check_local_vs_wireless_sub_registered():
    assert "check_grouping_local_vs_wireless_sub" in _registered_check_names()


def test_local_vs_wireless_sub_neither_is_ok(monkeypatch, tmp_path):
    _set_local_sub_topology(monkeypatch, tmp_path, with_sub=False)
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=False), "")
    r = doctor.check_grouping_local_vs_wireless_sub()
    assert r.status == "ok"
    assert "no local or wireless sub" in r.detail


def test_local_vs_wireless_sub_local_only_is_ok(monkeypatch, tmp_path):
    _set_local_sub_topology(monkeypatch, tmp_path, with_sub=True)
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=False), "")
    r = doctor.check_grouping_local_vs_wireless_sub()
    assert r.status == "ok"
    assert "local sub only" in r.detail


def test_local_vs_wireless_sub_wireless_follower_only_is_ok(monkeypatch, tmp_path):
    _set_local_sub_topology(monkeypatch, tmp_path, with_sub=False)
    _patch_grouping(
        monkeypatch,
        _grouping_cfg(
            enabled=True,
            role="follower",
            channel="sub",
            bond_id="lr",
            leader_addr="192.168.1.50",
        ),
        "",
    )
    r = doctor.check_grouping_local_vs_wireless_sub()
    assert r.status == "ok"
    assert "wireless sub only" in r.detail


def test_local_vs_wireless_sub_wireless_leader_only_is_ok(monkeypatch, tmp_path):
    from jasper.multiroom.config import BondMember

    _set_local_sub_topology(monkeypatch, tmp_path, with_sub=False)
    _patch_grouping(
        monkeypatch,
        _grouping_cfg(
            enabled=True,
            role="leader",
            channel="left",
            bond_id="lr",
            roster=(BondMember(addr="192.168.1.60", name="Sub", channel="sub"),),
        ),
        "",
    )
    r = doctor.check_grouping_local_vs_wireless_sub()
    assert r.status == "ok"
    assert "wireless sub only" in r.detail


def test_local_vs_wireless_sub_both_follower_warns(monkeypatch, tmp_path):
    # LOCAL sub topology AND this speaker is itself a wireless sub follower:
    # two bass producers at one speaker — fail LOUD.
    _set_local_sub_topology(monkeypatch, tmp_path, with_sub=True)
    _patch_grouping(
        monkeypatch,
        _grouping_cfg(
            enabled=True,
            role="follower",
            channel="sub",
            bond_id="lr",
            leader_addr="192.168.1.50",
        ),
        "",
    )
    r = doctor.check_grouping_local_vs_wireless_sub()
    assert r.status == "warn"
    assert "LOCAL sub" in r.detail
    assert "wireless sub follower" in r.detail


def test_local_vs_wireless_sub_both_leader_warns(monkeypatch, tmp_path):
    # LOCAL sub topology AND this leader's bond roster has a sub member.
    from jasper.multiroom.config import BondMember

    _set_local_sub_topology(monkeypatch, tmp_path, with_sub=True)
    _patch_grouping(
        monkeypatch,
        _grouping_cfg(
            enabled=True,
            role="leader",
            channel="left",
            bond_id="lr",
            roster=(BondMember(addr="192.168.1.60", name="Sub", channel="sub"),),
        ),
        "",
    )
    r = doctor.check_grouping_local_vs_wireless_sub()
    assert r.status == "warn"
    assert "LOCAL sub" in r.detail
    assert "leads a bond with a wireless sub member" in r.detail


def test_check_grouping_invalid_config_warns(monkeypatch):
    cfg = _grouping_cfg(
        enabled=True,
        role="leader",
        channel="left",
        error="JASPER_GROUPING_BOND_ID is empty (grouping is on)",
    )
    _patch_grouping(monkeypatch, cfg, "")
    r = doctor.check_grouping()
    assert r.status == "warn"
    assert "BOND_ID" in r.detail


def test_check_grouping_leader_reads_degraded_when_config_not_piped(
    monkeypatch, tmp_path
):
    # Increment 5 honest state: even with both snap units active, if the
    # leader's ACTIVE CamillaDSP config does not write the snapserver
    # pipe (the bond apply did not land), the bond is silent — degraded,
    # with the specific operator explanation. Pin the statefile to a
    # missing path so a dev machine with real CamillaDSP state can't
    # flip the result.
    monkeypatch.setenv(
        "JASPER_CAMILLA_STATEFILE",
        str(tmp_path / "no-statefile.yml"),
    )
    cfg = _grouping_cfg(
        enabled=True,
        role="leader",
        channel="left",
        bond_id="living-room",
    )
    _patch_grouping(
        monkeypatch,
        cfg,
        unit_states={
            "jasper-snapserver.service": "active",
            "jasper-snapclient.service": "active",
        },
    )
    r = doctor.check_grouping()
    assert r.status == "warn"
    assert "does not write the snapserver pipe" in r.detail
    assert "jasper-grouping-reconcile" in r.detail


def test_check_grouping_follower_unreachable_leader_warns(monkeypatch):
    cfg = _grouping_cfg(
        enabled=True,
        role="follower",
        channel="right",
        bond_id="living-room",
        leader_addr="192.168.1.50",
    )
    # snapclient failed => degraded, leader unreachable.
    _patch_grouping(
        monkeypatch,
        cfg,
        unit_states={
            "jasper-snapclient.service": "failed",
        },
    )
    r = doctor.check_grouping()
    assert r.status == "warn"
    assert "follower not connected" in r.detail
    assert "192.168.1.50" in r.detail


def test_check_grouping_connected_follower_is_ok(monkeypatch):
    """A follower has no producer concept: with its snapclient active it
    reads ok — the missing leader-side producer can never push a FOLLOWER
    to degraded."""
    cfg = _grouping_cfg(
        enabled=True,
        role="follower",
        channel="right",
        bond_id="living-room",
        leader_addr="192.168.1.50",
    )
    # Connected follower (snapclient active) => ok; the parked source-resource
    # stack reads inactive by default, which can never degrade health
    # (only desired=start units can).
    _patch_grouping(
        monkeypatch,
        cfg,
        unit_states={
            "jasper-snapclient.service": "active",
        },
    )
    r = doctor.check_grouping()
    assert r.status == "ok"
    assert "follower connected" in r.detail


def test_check_grouping_pair_lock_registered():
    assert "check_grouping_pair_lock" in _registered_check_names()


def test_check_grouping_pair_lock_warns_when_clock_lock_unobservable(monkeypatch):
    cfg = _grouping_cfg(
        enabled=True,
        role="follower",
        channel="right",
        bond_id="living-room",
        leader_addr="192.168.1.50",
    )
    _patch_grouping(
        monkeypatch,
        cfg,
        unit_states={
            "jasper-snapclient.service": "active",
        },
    )
    monkeypatch.setattr(
        doctor.grouping,
        "_read_outputd_status",
        lambda: {
            "dac_content": {"enabled": True, "serving_fifo": True},
        },
    )

    r = doctor.check_grouping_pair_lock()

    assert r.status == "warn"
    assert "clock lock is unobservable" in r.detail


def test_check_grouping_pair_lock_warns_when_fifo_not_serving(monkeypatch):
    cfg = _grouping_cfg(
        enabled=True,
        role="follower",
        channel="right",
        bond_id="living-room",
        leader_addr="192.168.1.50",
    )
    _patch_grouping(
        monkeypatch,
        cfg,
        unit_states={
            "jasper-snapclient.service": "active",
        },
    )
    monkeypatch.setattr(
        doctor.grouping,
        "_read_outputd_status",
        lambda: {
            "dac_content": {"enabled": True, "serving_fifo": False},
        },
    )

    r = doctor.check_grouping_pair_lock()

    assert r.status == "warn"
    assert "not serving FIFO bytes" in r.detail
