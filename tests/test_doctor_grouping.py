# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor grouping domain."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from jasper.cli import doctor
from jasper.cli.doctor import _evidence, grouping
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
def _fake_unit_states(
    active: dict[str, str] | None = None,
    *,
    load: dict[str, str] | None = None,
    default_active="inactive",
    default_load="loaded",
):
    """A ``_evidence.read_unit_states`` stand-in: ``active``/``load`` map a
    unit name to its ActiveState/LoadState word; any unit not named gets the
    matching default. Both the batched roster read and a per-unit fallback
    read route through this one fake, so it must answer for any units list."""
    active = active or {}
    load = load or {}

    def fake(units, *, timeout=2.0):
        return {
            u: {
                "unit": u,
                "load_state": load.get(u, default_load),
                "active_state": active.get(u, default_active),
                "sub_state": None,
                "unit_file_state": None,
                "result": None,
                "n_restarts": 0,
                "main_pid": 0,
                "tasks_current": None,
                "memory_current_bytes": None,
                "cpu_usage_nsec": None,
                "control_group": "",
            }
            for u in units
        }

    return fake


def _patch_grouping(monkeypatch, cfg, *, unit_states=None):
    """Stub the grouping check's IO. ``unit_states`` maps a unit name to its
    ``systemctl is-active`` word; unlisted units default to "inactive" — the
    plan carries the dumb-follower park/restore intents, so positional stdout
    lines are brittle against the unit list growing."""
    import jasper.multiroom.config as mr_config

    monkeypatch.setattr(mr_config, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(
        _evidence, "read_unit_states", _fake_unit_states(unit_states)
    )
    # No producer-feed stubbing: check_grouping injects leader_tap_path=""
    # unconditionally (no music producer exists yet), so a bonded leader
    # honestly derives degraded.


# ------------------------------------------------------------ snapcast install


@pytest.mark.parametrize(
    "cfg_kwargs, installed, status, reason",
    [
        # Grouping off means snapcast is deliberately not installed.
        ({"enabled": False}, False, "ok", grouping.REASON_GROUPING_OFF),
        (_LEADER, True, "ok", ""),
        # The JTS5 root state (2026-06-23): grouping enabled, binaries still
        # absent because provisioning failed or was skipped. The FAIL carries
        # the install command so that cannot stay invisible.
        (_LEADER, False, "fail", grouping.REASON_SNAPCAST_BINARY_MISSING),
    ],
    ids=["grouping-off", "present", "missing"],
)
def test_check_grouping_snapcast_installed_verdicts(
    monkeypatch, cfg_kwargs, installed, status, reason
):
    _patch_grouping(monkeypatch, _grouping_cfg(**cfg_kwargs))
    monkeypatch.setattr(
        "shutil.which", lambda name: f"/usr/bin/{name}" if installed else None
    )

    r = grouping.check_grouping_snapcast_installed()

    assert r.status == status
    assert r.reason == reason


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
    "cfg_kwargs, installed, run, status, reason",
    [
        ({"enabled": False}, False, None, "ok", grouping.REASON_GROUPING_OFF),
        (
            _LEADER, False, None, "skipped",
            grouping.REASON_SNAPCAST_NOT_INSTALLED,
        ),
        # A probe that cannot even run (timeout, vanished binary) skips rather
        # than manufacturing a verdict.
        (
            _LEADER,
            True,
            _probe(raises=subprocess.TimeoutExpired(cmd="snapclient", timeout=5)),
            "skipped",
            grouping.REASON_SNAPCAST_VERSION_UNKNOWN,
        ),
        # DSF-1: a non-zero exit must never reach the regex parse even though
        # the linker error itself carries a version-shaped token (a linked
        # library's version, not snapclient's own) — pinned here as
        # "version_unknown", never "version_mismatch".
        (
            _LEADER,
            True,
            _probe(returncode=127, stderr=_LINKER_ERROR),
            "skipped",
            grouping.REASON_SNAPCAST_VERSION_UNKNOWN,
        ),
        (
            _LEADER, True, _probe(stdout="not a version\n"), "skipped",
            grouping.REASON_SNAPCAST_VERSION_UNKNOWN,
        ),
        (
            _LEADER, True, _probe(stdout="snapclient v0.31.0\n"), "ok",
            grouping.REASON_SNAPCAST_VERSION_MATCH,
        ),
        (_LEADER, True, _probe(stdout="snapclient v0.32.1\n"), "warn",
         grouping.REASON_SNAPCAST_VERSION_MISMATCH),
    ],
    ids=[
        "grouping-off",
        "not-installed",
        "probe-raises",
        "nonzero-exit",
        "unparseable",
        "match",
        "drift",
    ],
)
def test_check_grouping_snapcast_version_verdicts(
    monkeypatch, cfg_kwargs, installed, run, status, reason
):
    _patch_grouping(monkeypatch, _grouping_cfg(**cfg_kwargs))
    monkeypatch.setattr(
        "shutil.which", lambda name: f"/usr/bin/{name}" if installed else None
    )
    if run is not None:
        monkeypatch.setattr(doctor.grouping, "_run", run)

    r = grouping.check_grouping_snapcast_version()

    assert r.status == status
    assert r.reason == reason


def test_check_grouping_snapcast_version_stdout_wins_over_stderr(monkeypatch):
    """The concatenation order is load-bearing: snapclient's real version rides
    stdout, but ALSA-lib can print its OWN version-shaped token to stderr, so
    stdout must win the parse. The extracted value is data the reason
    vocabulary can't carry, so this keeps a `.detail` check as the
    pure-formatting-helper exception."""
    _patch_grouping(monkeypatch, _grouping_cfg(**_LEADER))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        doctor.grouping,
        "_run",
        _probe(
            stdout="snapclient v0.31.0\n",
            stderr="ALSA lib pcm.c:1234:(some_fn) something 9.9.9\n",
        ),
    )

    r = grouping.check_grouping_snapcast_version()

    assert r.status == "ok"
    assert r.reason == grouping.REASON_SNAPCAST_VERSION_MATCH
    assert "9.9.9" not in r.detail


# --------------------------------------------------------- household credential


@pytest.mark.parametrize(
    "cfg_kwargs, secret_present, status, reason",
    [
        # Solo short-circuits before reading the secret file.
        ({"enabled": False}, False, "skipped", grouping.REASON_NOT_APPLICABLE),
        (_LEADER, True, "ok", ""),
        # RECOVERY drift: bonded but the secret was lost, so /grouping/set is
        # fail-safe-open and the doctor is the only place that loss is visible.
        (_FOLLOWER, False, "warn", grouping.REASON_HOUSEHOLD_CREDENTIAL_MISSING),
    ],
    ids=["solo", "bonded-paired", "bonded-unpaired"],
)
def test_check_grouping_household_credential_verdicts(
    monkeypatch, tmp_path, cfg_kwargs, secret_present, status, reason
):
    import jasper.control.household_credential as hc

    secret = tmp_path / "household_secret"
    if secret_present:
        secret.write_text("s\n")
    monkeypatch.setattr(hc, "SECRET_FILE", str(secret))
    _patch_grouping(monkeypatch, _grouping_cfg(**cfg_kwargs))

    r = grouping.check_grouping_household_credential()

    assert r.status == status
    assert r.reason == reason


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
    "cfg_kwargs",
    [{"enabled": False}, _FOLLOWER],
    ids=["solo", "follower"],
)
def test_check_crossover_unit_skips_when_not_an_active_leader(
    monkeypatch, cfg_kwargs
):
    """Not an active member, or an active follower (not the leader half of the
    pair): both skip before touching topology or systemd."""
    _patch_grouping(monkeypatch, _grouping_cfg(**cfg_kwargs))

    r = grouping.check_crossover_unit_installed()

    assert r.status == "skipped"
    assert r.reason == grouping.REASON_NOT_APPLICABLE


def test_check_crossover_unit_skips_for_a_passive_leader(monkeypatch, tmp_path):
    """A bonded LEADER whose topology has NO roleful/protected outputs runs no
    per-driver crossover, so camilla#2 is n/a."""
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(_topology([]), path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    _patch_grouping(monkeypatch, _grouping_cfg(**_LEADER))

    r = grouping.check_crossover_unit_installed()

    assert r.status == "skipped"
    assert r.reason == grouping.REASON_NOT_APPLICABLE


@pytest.mark.parametrize(
    "returncode, systemd_analyze, status, reason",
    [
        # `systemctl cat` nonzero: the unit is not installed, so the reconciler
        # would have nothing to arm.
        (1, "/usr/bin/systemd-analyze", "warn",
         grouping.REASON_CROSSOVER_UNIT_MISSING),
        (0, "/usr/bin/systemd-analyze", "ok", ""),
        # No systemd-analyze on a dev box: skipped with an explicit note,
        # never a false warn.
        (0, None, "skipped", grouping.REASON_CROSSOVER_UNIT_UNVERIFIED),
    ],
    ids=["not-installed", "installed", "no-systemd-analyze"],
)
def test_check_crossover_unit_active_leader_verdicts(
    monkeypatch, tmp_path, returncode, systemd_analyze, status, reason
):
    _active_leader_topology(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _evidence,
        "read_unit_states",
        _fake_unit_states(
            load={
                "jasper-camilla-crossover.service": (
                    "loaded" if returncode == 0 else "not-found"
                ),
            },
        ),
    )
    monkeypatch.setattr(
        doctor.grouping,
        "_run",
        lambda argv, *a, **kw: SimpleNamespace(
            returncode=returncode, stdout="", stderr=""
        ),
    )
    monkeypatch.setattr(doctor.grouping.shutil, "which", lambda _n: systemd_analyze)

    r = grouping.check_crossover_unit_installed()

    assert r.status == status
    assert r.reason == reason


# ------------------------------------------------------------- check_grouping


def test_check_grouping_off_is_ok(monkeypatch):
    _patch_grouping(monkeypatch, _grouping_cfg(enabled=False))

    r = grouping.check_grouping()

    assert r.status == "ok"
    assert r.reason == grouping.REASON_GROUPING_OFF


def test_check_grouping_invalid_config_warns(monkeypatch):
    cfg = _grouping_cfg(
        enabled=True,
        role="leader",
        channel="left",
        error="JASPER_GROUPING_BOND_ID is empty (grouping is on)",
    )
    _patch_grouping(monkeypatch, cfg)

    r = grouping.check_grouping()

    assert r.status == "warn"
    assert r.reason == grouping.REASON_CONFIG_INVALID


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

    r = grouping.check_grouping()

    assert r.status == "warn"
    assert r.reason == grouping.REASON_RUNTIME_DEGRADED


@pytest.mark.parametrize(
    "snapclient_state, status",
    [
        ("failed", "warn"),
        # A follower has no producer concept: with snapclient active it reads
        # ok, and the missing leader-side producer can never degrade it. The
        # parked source-resource stack reads inactive, which cannot degrade
        # health either (only desired=start units can).
        ("active", "ok"),
    ],
    ids=["unreachable-leader", "connected"],
)
def test_check_grouping_follower_verdicts(monkeypatch, snapclient_state, status):
    _patch_grouping(
        monkeypatch,
        _grouping_cfg(**_FOLLOWER),
        unit_states={"jasper-snapclient.service": snapclient_state},
    )

    r = grouping.check_grouping()

    assert r.status == status
    assert r.reason == (grouping.REASON_RUNTIME_DEGRADED if status == "warn" else "")


@pytest.mark.parametrize(
    "dac_content, reason",
    [
        (
            {"enabled": True, "serving_fifo": True},
            grouping.REASON_PAIR_LOCK_UNKNOWN,
        ),
        (
            {"enabled": True, "serving_fifo": False},
            grouping.REASON_PAIR_LOCK_DEGRADED,
        ),
    ],
    ids=["clock-unobservable", "fifo-not-serving"],
)
def test_check_grouping_pair_lock_warns(monkeypatch, dac_content, reason):
    _patch_grouping(
        monkeypatch,
        _grouping_cfg(**_FOLLOWER),
        unit_states={"jasper-snapclient.service": "active"},
    )
    monkeypatch.setattr(
        _evidence,
        "read_status_socket",
        lambda _path, *, timeout=2.0: {"dac_content": dac_content},
    )

    r = grouping.check_grouping_pair_lock()

    assert r.status == "warn"
    assert r.reason == reason


def _solo_tts_lane(monkeypatch, voice_env_path):
    """Run the TTS-lane check on a solo box, with the systemctl surface
    limited to the unit's inline `Environment=` directive — all it can ever
    report, since it never reads `EnvironmentFile=` layers."""
    import jasper.multiroom.config as mr_config
    import jasper.multiroom.reconcile as mr_reconcile

    monkeypatch.setattr(mr_config, "load_config", lambda *a, **k: _grouping_cfg())
    monkeypatch.setattr(mr_reconcile, "VOICE_GROUPING_ENV_FILE", str(voice_env_path))
    _evidence.evidence.seed(
        "prop:Environment:jasper-voice",
        [f"{VOICE_TTS_SOCKET_ENV}={FANIN_TTS_SOCKET}"],
    )
    return grouping.check_grouping_tts_lane()


@pytest.mark.parametrize(
    "grouping_env_text, reason",
    [
        (
            f"{VOICE_TTS_SOCKET_ENV}={OUTPUTD_TTS_SOCKET}\n",
            grouping.REASON_TTS_SOCKET_DRIFT,
        ),
        (f"{VOICE_PARK_ENV}=1\n", grouping.REASON_TTS_PARK_FLAG_DRIFT),
        # Present-but-empty resolves to an empty socket path, which breaks
        # playout exactly like a wrong one — key ABSENCE is the only no-drift.
        (f"{VOICE_TTS_SOCKET_ENV}=\n", grouping.REASON_TTS_SOCKET_DRIFT),
    ],
    ids=["stale-socket-override", "stale-park-flag", "present-but-empty-socket"],
)
def test_check_grouping_tts_lane_reads_the_environmentfile_authority(
    monkeypatch, tmp_path, grouping_env_text, reason
):
    """`systemctl show -p Environment` reports inline `Environment=` directives
    only, so a solo box whose reconciler-owned grouping-voice.env still carries
    a bonded override read green through both solo guards (#2387)."""
    env_file = tmp_path / "grouping-voice.env"
    env_file.write_text(grouping_env_text)

    r = _solo_tts_lane(monkeypatch, env_file)

    assert r.status == "warn"
    assert r.reason == reason


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
    assert r.reason == grouping.REASON_TTS_VOICE_ENV_UNRESOLVED


@pytest.mark.parametrize(
    "check_name",
    [
        "check_grouping_snapcast_version",
        "check_crossover_unit_installed",
        "check_grouping_pair_lock",
    ],
)
def test_grouping_checks_are_registered(check_name):
    assert check_name in _registered_check_names()
