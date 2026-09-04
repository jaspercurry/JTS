# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor grouping domain."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from jasper.cli import doctor
from jasper.multiroom.config import BondMember
from jasper.multiroom.tts_route import VOICE_PARK_ENV
from jasper.tts_routing import (
    FANIN_TTS_SOCKET,
    OUTPUTD_TTS_SOCKET,
    VOICE_TTS_SOCKET_ENV,
)

from .doctor_test_support import _grouping_cfg, _registered_check_names

_LEADER = dict(enabled=True, role="leader", channel="left", bond_id="x")
_FOLLOWER = dict(
    enabled=True,
    role="follower",
    channel="right",
    bond_id="living-room",
    leader_addr="192.168.1.50",
)
_SUB_FOLLOWER = dict(
    enabled=True,
    role="follower",
    channel="sub",
    bond_id="lr",
    leader_addr="192.168.1.50",
)
_SUB_LEADER = dict(
    enabled=True,
    role="leader",
    channel="left",
    bond_id="lr",
    roster=(BondMember(addr="192.168.1.60", name="Sub", channel="sub"),),
)


def _patch_grouping(monkeypatch, cfg, is_active_stdout=None, *, unit_states=None):
    """Stub the grouping check's IO. Prefer `unit_states` (a unit→state
    mapping; unlisted units default to "inactive") — the plan carries the
    dumb-follower park/restore intents, so positional stdout lines are brittle
    against the unit list growing."""
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
    # unconditionally (no music producer exists yet), so a bonded leader
    # honestly derives degraded.


# ------------------------------------------------------------ snapcast install


@pytest.mark.parametrize(
    "cfg_kwargs, installed, status, must_name",
    [
        # Grouping off means snapcast is deliberately not installed.
        ({"enabled": False}, False, "ok", "grouping off"),
        (_LEADER, True, "ok", "present"),
        # The JTS5 root state (2026-06-23): grouping enabled, binaries still
        # absent because provisioning failed or was skipped. The FAIL carries
        # the install command so that cannot stay invisible.
        (_LEADER, False, "fail", "apt install"),
    ],
    ids=["grouping-off", "present", "missing"],
)
def test_check_grouping_snapcast_installed_verdicts(
    monkeypatch, cfg_kwargs, installed, status, must_name
):
    _patch_grouping(monkeypatch, _grouping_cfg(**cfg_kwargs), "")
    monkeypatch.setattr(
        "shutil.which", lambda name: f"/usr/bin/{name}" if installed else None
    )

    r = doctor.check_grouping_snapcast_installed()

    assert r.status == status
    assert must_name in r.detail


# ------------------------------------------------------------ snapcast version
#
# A live probe (bounded), never a record. Drift warns for visibility rather
# than failing: no apt pin exists to enforce VALIDATED_SNAPCAST_VERSION.


def _probe(returncode=0, stdout="", stderr="", raises=None):
    def run(argv, **kw):
        if raises is not None:
            raise raises
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return run


_LINKER_ERROR = (
    "snapclient: error while loading shared libraries: "
    "libboost_program_options.so.1.83.0: cannot open shared object file: "
    "No such file or directory"
)


@pytest.mark.parametrize(
    "cfg_kwargs, installed, run, status, must_name, must_not_name",
    [
        ({"enabled": False}, False, None, "ok", "grouping off", ""),
        (_LEADER, False, None, "ok", "not installed", ""),
        # A probe that cannot even run (timeout, vanished binary) skips rather
        # than manufacturing a verdict.
        (
            _LEADER,
            True,
            _probe(raises=subprocess.TimeoutExpired(cmd="snapclient", timeout=5)),
            "ok",
            "could not determine",
            "",
        ),
        # DSF-1: a non-zero exit must never reach the regex parse even though
        # the linker error itself carries a version-shaped token (a linked
        # library's version, not snapclient's own).
        (
            _LEADER,
            True,
            _probe(returncode=127, stderr=_LINKER_ERROR),
            "ok",
            "libboost",
            "differs from",
        ),
        (_LEADER, True, _probe(stdout="not a version\n"), "ok",
         "could not determine", ""),
        (_LEADER, True, _probe(stdout="snapclient v0.31.0\n"), "ok", "0.31.0", ""),
        # The concatenation order is load-bearing: snapclient's real version
        # rides stdout, but ALSA-lib can print its OWN version-shaped token to
        # stderr, so stdout must win the parse.
        (
            _LEADER,
            True,
            _probe(
                stdout="snapclient v0.31.0\n",
                stderr="ALSA lib pcm.c:1234:(some_fn) something 9.9.9\n",
            ),
            "ok",
            "0.31.0",
            "9.9.9",
        ),
        (_LEADER, True, _probe(stdout="snapclient v0.32.1\n"), "warn", "0.32.1", ""),
    ],
    ids=[
        "grouping-off",
        "not-installed",
        "probe-raises",
        "nonzero-exit",
        "unparseable",
        "match",
        "stdout-wins",
        "drift",
    ],
)
def test_check_grouping_snapcast_version_verdicts(
    monkeypatch, cfg_kwargs, installed, run, status, must_name, must_not_name
):
    _patch_grouping(monkeypatch, _grouping_cfg(**cfg_kwargs), "")
    monkeypatch.setattr(
        "shutil.which", lambda name: f"/usr/bin/{name}" if installed else None
    )
    if run is not None:
        monkeypatch.setattr(doctor.grouping, "_run", run)

    r = doctor.check_grouping_snapcast_version()

    assert r.status == status
    assert must_name in r.detail
    if must_not_name:
        assert must_not_name not in r.detail


# --------------------------------------------------------- household credential


@pytest.mark.parametrize(
    "cfg_kwargs, secret_present, status, must_name",
    [
        # Solo short-circuits before reading the secret file.
        ({"enabled": False}, False, "ok", "solo"),
        (_LEADER, True, "ok", "present"),
        # RECOVERY drift: bonded but the secret was lost, so /grouping/set is
        # fail-safe-open and the doctor is the only place that loss is visible.
        (_FOLLOWER, False, "warn", "household credential is missing"),
    ],
    ids=["solo", "bonded-paired", "bonded-unpaired"],
)
def test_check_grouping_household_credential_verdicts(
    monkeypatch, tmp_path, cfg_kwargs, secret_present, status, must_name
):
    import jasper.control.household_credential as hc

    secret = tmp_path / "household_secret"
    if secret_present:
        secret.write_text("s\n")
    monkeypatch.setattr(hc, "SECRET_FILE", str(secret))
    _patch_grouping(monkeypatch, _grouping_cfg(**cfg_kwargs), "")

    r = doctor.check_grouping_household_credential()

    assert r.status == status
    assert must_name in r.detail


# --- camilla#2 endpoint-crossover unit (Stage B B1, INERT) ----------------


def _active_leader_topology(monkeypatch, tmp_path):
    """An ACTIVE-LEADER context: a roleful/protected output topology plus a
    bonded-leader grouping config. Leaves `doctor.grouping._run` patchable."""
    import jasper.multiroom.config as mr_config
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _active_topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(_active_topology("mono", "active_2_way"), path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setattr(
        mr_config, "load_config", lambda *a, **k: _grouping_cfg(**_LEADER)
    )


@pytest.mark.parametrize(
    "cfg_kwargs, status, must_name",
    [
        # Not an active member: skip before touching topology or systemd.
        ({"enabled": False}, "ok", "not an active bond leader"),
        # An ACTIVE follower is not the leader half of the pair; camilla#2 is
        # the leader's instance.
        (_FOLLOWER, "ok", "not an active bond leader"),
    ],
    ids=["solo", "follower"],
)
def test_check_crossover_unit_skips_when_not_an_active_leader(
    monkeypatch, cfg_kwargs, status, must_name
):
    _patch_grouping(monkeypatch, _grouping_cfg(**cfg_kwargs), "")

    r = doctor.check_crossover_unit_installed()

    assert r.status == status
    assert must_name in r.detail


def test_check_crossover_unit_skips_for_a_passive_leader(monkeypatch, tmp_path):
    """A bonded LEADER whose topology has NO roleful/protected outputs runs no
    per-driver crossover, so camilla#2 is n/a."""
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(_topology([]), path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    _patch_grouping(monkeypatch, _grouping_cfg(**_LEADER), "")

    r = doctor.check_crossover_unit_installed()

    assert r.status == "ok"
    assert "passive leader" in r.detail


@pytest.mark.parametrize(
    "returncode, systemd_analyze, status, must_name",
    [
        # `systemctl cat` nonzero: the unit is not installed, so the reconciler
        # would have nothing to arm.
        (1, "/usr/bin/systemd-analyze", "warn",
         "jasper-camilla-crossover.service is not installed"),
        (0, "/usr/bin/systemd-analyze", "ok", "INERT"),
        # No systemd-analyze on a dev box: ok with an explicit note, never a
        # false warn.
        (0, None, "ok", "parse unchecked"),
    ],
    ids=["not-installed", "installed", "no-systemd-analyze"],
)
def test_check_crossover_unit_active_leader_verdicts(
    monkeypatch, tmp_path, returncode, systemd_analyze, status, must_name
):
    _active_leader_topology(monkeypatch, tmp_path)
    monkeypatch.setattr(
        doctor.grouping,
        "_run",
        lambda argv, *a, **kw: SimpleNamespace(
            returncode=returncode, stdout="", stderr=""
        ),
    )
    monkeypatch.setattr(doctor.grouping.shutil, "which", lambda _n: systemd_analyze)

    r = doctor.check_crossover_unit_installed()

    assert r.status == status
    assert must_name in r.detail


# --- local-vs-wireless sub coexistence (B3) -------------------------------


def _set_local_sub_topology(monkeypatch, tmp_path, *, with_sub: bool):
    """A subwoofer topology carries routing.subwoofer_group_ids; the no-sub
    case points at a missing file (an empty draft, so no subwoofer groups)."""
    topology_path = tmp_path / "output_topology.json"
    if with_sub:
        from jasper.output_topology import save_output_topology
        from tests.test_active_speaker_runtime_contract import _subwoofer_topology

        save_output_topology(_subwoofer_topology(), path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))


@pytest.mark.parametrize(
    "local_sub, cfg_kwargs, status, must_name",
    [
        (False, {"enabled": False}, "ok", "no local or wireless sub"),
        (True, {"enabled": False}, "ok", "local sub only"),
        (False, _SUB_FOLLOWER, "ok", "wireless sub only"),
        (False, _SUB_LEADER, "ok", "wireless sub only"),
        # Two bass producers at one speaker.
        (True, _SUB_FOLLOWER, "warn", "wireless sub follower"),
        (True, _SUB_LEADER, "warn", "leads a bond with a wireless sub member"),
    ],
    ids=["neither", "local-only", "wireless-follower", "wireless-leader",
         "both-follower", "both-leader"],
)
def test_check_grouping_local_vs_wireless_sub_verdicts(
    monkeypatch, tmp_path, local_sub, cfg_kwargs, status, must_name
):
    _set_local_sub_topology(monkeypatch, tmp_path, with_sub=local_sub)
    _patch_grouping(monkeypatch, _grouping_cfg(**cfg_kwargs), "")

    r = doctor.check_grouping_local_vs_wireless_sub()

    assert r.status == status
    assert must_name in r.detail
    if status == "warn":
        assert "LOCAL sub" in r.detail


# ------------------------------------------------------------- check_grouping


def test_check_grouping_off_is_ok(monkeypatch):
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=False), "")

    r = doctor.check_grouping()

    assert r.status == "ok"
    assert "single-speaker" in r.detail


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
    """Increment 5 honest state: with both snap units active but the leader's
    ACTIVE CamillaDSP config not writing the snapserver pipe (the bond apply
    did not land), the bond is silent."""
    # Pin the statefile to a missing path so a dev machine with real CamillaDSP
    # state cannot flip the result.
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(tmp_path / "absent.yml"))
    _patch_grouping(
        monkeypatch,
        _grouping_cfg(
            enabled=True, role="leader", channel="left", bond_id="living-room"
        ),
        unit_states={
            "jasper-snapserver.service": "active",
            "jasper-snapclient.service": "active",
        },
    )

    r = doctor.check_grouping()

    assert r.status == "warn"
    assert "does not write the snapserver pipe" in r.detail
    assert "jasper-grouping-reconcile" in r.detail


@pytest.mark.parametrize(
    "snapclient_state, status, must_name",
    [
        ("failed", "warn", "192.168.1.50"),
        # A follower has no producer concept: with snapclient active it reads
        # ok, and the missing leader-side producer can never degrade it. The
        # parked source-resource stack reads inactive, which cannot degrade
        # health either (only desired=start units can).
        ("active", "ok", "follower connected"),
    ],
    ids=["unreachable-leader", "connected"],
)
def test_check_grouping_follower_verdicts(
    monkeypatch, snapclient_state, status, must_name
):
    _patch_grouping(
        monkeypatch,
        _grouping_cfg(**_FOLLOWER),
        unit_states={"jasper-snapclient.service": snapclient_state},
    )

    r = doctor.check_grouping()

    assert r.status == status
    assert must_name in r.detail


@pytest.mark.parametrize(
    "dac_content, must_name",
    [
        ({"enabled": True, "serving_fifo": True}, "clock lock is unobservable"),
        ({"enabled": True, "serving_fifo": False}, "not serving FIFO bytes"),
    ],
    ids=["clock-unobservable", "fifo-not-serving"],
)
def test_check_grouping_pair_lock_warns(monkeypatch, dac_content, must_name):
    _patch_grouping(
        monkeypatch,
        _grouping_cfg(**_FOLLOWER),
        unit_states={"jasper-snapclient.service": "active"},
    )
    monkeypatch.setattr(
        doctor.grouping,
        "read_status_socket_or_none",
        lambda *a, **kw: {"dac_content": dac_content},
    )

    r = doctor.check_grouping_pair_lock()

    assert r.status == "warn"
    assert must_name in r.detail


def _solo_tts_lane(monkeypatch, voice_env_path):
    """Run the TTS-lane check on a solo box, with the systemctl surface
    limited to the unit's inline `Environment=` directive — all it can ever
    report, since it never reads `EnvironmentFile=` layers."""
    import jasper.multiroom.config as mr_config
    import jasper.multiroom.reconcile as mr_reconcile

    monkeypatch.setattr(mr_config, "load_config", lambda *a, **k: _grouping_cfg())
    monkeypatch.setattr(mr_reconcile, "VOICE_GROUPING_ENV_FILE", str(voice_env_path))
    monkeypatch.setattr(
        doctor.grouping,
        "_run",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=f"{VOICE_TTS_SOCKET_ENV}={FANIN_TTS_SOCKET}\n",
            stderr="",
        ),
    )
    return doctor.check_grouping_tts_lane()


@pytest.mark.parametrize(
    "grouping_env_text, must_name",
    [
        (f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n", OUTPUTD_TTS_SOCKET),
        (f"{VOICE_PARK_ENV}=1\n", VOICE_PARK_ENV),
        # Present-but-empty resolves to an empty socket path, which breaks
        # playout exactly like a wrong one — key ABSENCE is the only no-drift.
        (f"{VOICE_TTS_SOCKET_ENV}=\n", VOICE_TTS_SOCKET_ENV),
    ],
    ids=["stale-socket-override", "stale-park-flag", "present-but-empty-socket"],
)
def test_check_grouping_tts_lane_reads_the_environmentfile_authority(
    monkeypatch, tmp_path, grouping_env_text, must_name
):
    """`systemctl show -p Environment` reports inline `Environment=` directives
    only, so a solo box whose reconciler-owned grouping-voice.env still carries
    a bonded override read green through both solo guards (#2387)."""
    env_file = tmp_path / "grouping-voice.env"
    env_file.write_text(grouping_env_text)

    r = _solo_tts_lane(monkeypatch, env_file)

    assert r.status == "warn"
    assert must_name in r.detail


def test_check_grouping_tts_lane_warns_when_the_authority_is_unreadable(
    monkeypatch, tmp_path
):
    """An unreadable grouping-voice.env must not fail soft into the inline env:
    that reprints the #2387 false green. A path that raises OSError on read
    stands in for the deployed case (mode 600 root, doctor as jasper-control)
    without depending on the test user's privileges."""
    unreadable = tmp_path / "grouping-voice.env"
    unreadable.mkdir()

    r = _solo_tts_lane(monkeypatch, unreadable)

    assert r.status == "warn"
    assert str(unreadable) in r.detail


@pytest.mark.parametrize(
    "check_name",
    [
        "check_grouping_snapcast_version",
        "check_crossover_unit_installed",
        "check_grouping_local_vs_wireless_sub",
        "check_grouping_pair_lock",
    ],
)
def test_grouping_checks_are_registered(check_name):
    assert check_name in _registered_check_names()
