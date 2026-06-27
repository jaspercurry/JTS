# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper-doctor's env loading, provider-aware key
check, and ALSA mic-card lookup. Hardware-side checks (sounddevice,
systemctl, arecord, etc) are exercised on the Pi via
``jasper-doctor`` itself; this file pins the pure-python helpers."""
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from jasper.audio_profile_state import MicProbe
from jasper import wake_models
from jasper.cli import doctor
from jasper.config import Config
from jasper.correction import bundles
from jasper.output_hardware import (
    APPLE_USB_C_DONGLE_DEVICE_ID,
    DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
    OutputCardFact,
    OutputHardwareState,
    classify_output_cards,
    write_state as write_output_hardware_state,
)
from jasper.voice.catalog import PROVIDERS, provider_ids_manifest_text

from .correction_bundle_fixtures import write_golden_correction_bundle


def _registered_check_names() -> set[str]:
    """Function names of every check registered to run via ``run_async``.

    The doctor's run order/membership moved from a hand-ordered literal
    list inside ``run_async`` into the ordered registry
    (``doctor.registered_checks()``). These tests used to source-grep
    ``run_async`` to prove a check was wired in; the registry is now the
    source of truth, so we assert against it instead.
    """
    return {c.func.__name__ for c in doctor.registered_checks()}


# ---------------------------------------------------------------- env loading


def test_parse_env_file_basic(tmp_path: Path):
    p = tmp_path / "jasper.env"
    p.write_text(
        "# comment line\n"
        "\n"
        "GEMINI_API_KEY=AIzaSyABC\n"
        "JASPER_VOICE_PROVIDER=openai\n"
        'OPENAI_API_KEY="sk-quoted"\n'
        "EMPTY=\n"
        "  WHITESPACE_KEY  =  trimmed  \n"
    )
    out = doctor._parse_env_file(str(p))
    assert out["GEMINI_API_KEY"] == "AIzaSyABC"
    assert out["JASPER_VOICE_PROVIDER"] == "openai"
    assert out["OPENAI_API_KEY"] == "sk-quoted"
    assert out["EMPTY"] == ""
    assert out["WHITESPACE_KEY"] == "trimmed"


def test_parse_env_file_missing_returns_empty(tmp_path: Path):
    out = doctor._parse_env_file(str(tmp_path / "does-not-exist"))
    assert out == {}


def test_read_env_file_state_reports_loaded_and_missing(tmp_path: Path):
    from jasper.env_load import read_env_file_state

    p = tmp_path / "jasper.env"
    p.write_text("JASPER_HOSTNAME=jts.local\n")

    loaded = read_env_file_state(str(p))
    assert loaded.status == "loaded"
    assert loaded.loaded
    assert loaded.values["JASPER_HOSTNAME"] == "jts.local"

    missing = read_env_file_state(str(tmp_path / "missing.env"))
    assert missing.status == "missing"
    assert missing.values == {}


def test_read_env_file_state_reports_unreadable(monkeypatch, tmp_path: Path):
    import jasper.env_load as env_load
    from jasper.env_load import read_env_file_state

    p = tmp_path / "jasper.env"
    p.write_text("JASPER_HOSTNAME=jts.local\n")

    def boom(self):
        raise PermissionError("blocked")

    monkeypatch.setattr(env_load.Path, "read_text", boom)

    state = read_env_file_state(str(p))
    assert state.status == "unreadable"
    assert state.values == {}
    assert "PermissionError" in state.error


def test_load_env_files_wizard_overrides_operator(monkeypatch, tmp_path: Path):
    """`/var/lib/jasper/voice_provider.env` (wizard) must override
    `/etc/jasper/jasper.env` (operator) — same precedence as the
    systemd unit's `EnvironmentFile=` ordering. Verified via the
    explicit-paths form of `load_env_files` so test fixtures don't
    have to monkeypatch a module-level constant."""
    from jasper.env_load import load_env_files
    operator = tmp_path / "jasper.env"
    operator.write_text(
        "GEMINI_API_KEY=op-key\n"
        "JASPER_VOICE_PROVIDER=gemini\n"
    )
    wizard = tmp_path / "voice_provider.env"
    wizard.write_text(
        "OPENAI_API_KEY=wiz-key\n"
        "JASPER_VOICE_PROVIDER=openai\n"
    )
    for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "JASPER_VOICE_PROVIDER"):
        monkeypatch.delenv(var, raising=False)

    load_env_files((str(operator), str(wizard)))

    assert os_environ_get("GEMINI_API_KEY") == "op-key"
    assert os_environ_get("OPENAI_API_KEY") == "wiz-key"
    assert os_environ_get("JASPER_VOICE_PROVIDER") == "openai"


def test_default_env_files_include_spotify_credentials_in_systemd_order():
    from jasper.env_load import ENV_FILES

    spotify_creds = "/var/lib/jasper-intsecrets/spotify_credentials.env"
    assert "/etc/jasper/jasper.env" in ENV_FILES
    assert spotify_creds in ENV_FILES
    assert ENV_FILES.index("/etc/jasper/jasper.env") < ENV_FILES.index(
        spotify_creds,
    )
    assert ENV_FILES.index(spotify_creds) < ENV_FILES.index(
        "/var/lib/jasper/voice_provider.env",
    )


def test_load_env_files_shell_wins_over_files(monkeypatch, tmp_path: Path):
    """A var already in the calling shell must NOT be overwritten by
    the env files. Lets an operator probe with `FOO=bar jasper-doctor`."""
    from jasper.env_load import load_env_files
    operator = tmp_path / "jasper.env"
    operator.write_text("JASPER_VOICE_PROVIDER=gemini\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "openai")

    load_env_files((str(operator),))
    assert os_environ_get("JASPER_VOICE_PROVIDER") == "openai"


def test_check_service_runtime_state_fails_on_failed_unit(monkeypatch):
    class FakeRun:
        stdout = (
            "Id=librespot.service\n"
            "LoadState=loaded\n"
            "ActiveState=failed\n"
            "SubState=failed\n"
            "Result=exit-code\n"
            "NRestarts=5\n"
        )

    monkeypatch.setattr(doctor._shared, "_run", lambda *a, **kw: FakeRun())

    r = doctor.check_service_runtime_state()

    assert r.status == "fail"
    assert "librespot.service state=failed/failed" in r.detail
    assert "NRestarts=5" in r.detail


def test_check_service_runtime_state_warns_on_restart_count(monkeypatch):
    class FakeRun:
        stdout = (
            "Id=jasper-voice.service\n"
            "LoadState=loaded\n"
            "ActiveState=active\n"
            "SubState=running\n"
            "Result=success\n"
            "NRestarts=2\n"
        )

    monkeypatch.setattr(doctor._shared, "_run", lambda *a, **kw: FakeRun())

    r = doctor.check_service_runtime_state()

    assert r.status == "warn"
    assert "jasper-voice.service NRestarts=2" in r.detail


# -------------------------------------------------- active WiFi connection


def _nmcli_active_run(stdout: str):
    """Build a fake `_run` returning ``stdout`` for any nmcli invocation.

    Records the argv it was called with so tests can assert the field
    order requested from nmcli."""
    calls: list[list[str]] = []

    def fake_run(argv, *a, **kw):
        calls.append(list(argv))

        class FakeRun:
            returncode = 0
            stdout = ""

        FakeRun.stdout = stdout
        return FakeRun()

    fake_run.calls = calls  # type: ignore[attr-defined]
    return fake_run


def test_active_wifi_connection_simple(monkeypatch):
    """Plain SSID with no colon resolves to (name, device)."""
    # nmcli -t -f TYPE,DEVICE,NAME connection show --active
    stdout = "802-11-wireless:wlan0:HomeWiFi\n"
    monkeypatch.setattr(doctor.network, "_run", _nmcli_active_run(stdout))

    name, device = doctor.network._active_wifi_connection("nmcli")

    assert name == "HomeWiFi"
    assert device == "wlan0"


def test_active_wifi_connection_handles_colon_in_ssid(monkeypatch):
    """An SSID containing a literal colon must still be matched.

    Real-world SSIDs like ``Home:2.4G`` / ``AT&T:5G`` appear in
    nmcli -t output with the colon escaped as ``\\:``. With the old
    NAME-first field order (``NAME,TYPE,DEVICE``) this row mis-parsed —
    the first ``\\:`` was treated as a field boundary, TYPE landed on
    ``2.4G`` (not a wifi type), and the active connection was silently
    missed, returning (None, None) for a valid profile. This test pins
    the colon-safe TYPE,DEVICE,NAME order + unescape and FAILS on the
    old order."""
    # As emitted by `nmcli -t -f TYPE,DEVICE,NAME connection show --active`:
    # the NAME field's literal colon is backslash-escaped.
    stdout = "802-11-wireless:wlan0:Home\\:2.4G\n"
    fake_run = _nmcli_active_run(stdout)
    monkeypatch.setattr(doctor.network, "_run", fake_run)

    name, device = doctor.network._active_wifi_connection("nmcli")

    assert name == "Home:2.4G", "colon-containing SSID must be unescaped, not dropped"
    assert device == "wlan0"
    # The variable-content NAME field must be requested last so fixed-format
    # TYPE/DEVICE tokens parse unambiguously.
    assert "TYPE,DEVICE,NAME" in fake_run.calls[0]


def test_active_wifi_connection_no_wifi_row(monkeypatch):
    """Only a non-wifi (ethernet) active connection → (None, None)."""
    stdout = "802-3-ethernet:eth0:Wired connection 1\n"
    monkeypatch.setattr(doctor.network, "_run", _nmcli_active_run(stdout))

    assert doctor.network._active_wifi_connection("nmcli") == (None, None)


def test_active_wifi_connection_nonzero_returncode(monkeypatch):
    """nmcli failure → (None, None), not a crash."""

    def fake_run(argv, *a, **kw):
        class FakeRun:
            returncode = 1
            stdout = ""

        return FakeRun()

    monkeypatch.setattr(doctor.network, "_run", fake_run)
    assert doctor.network._active_wifi_connection("nmcli") == (None, None)


# ----------------------------------------------------------------- grouping


def _grouping_cfg(**kw):
    from jasper.multiroom.config import (
        DEFAULT_BUFFER_MS,
        DEFAULT_CODEC,
        GroupingConfig,
    )

    defaults = dict(
        enabled=False, role="", channel="stereo", bond_id="", leader_addr="",
        buffer_ms=DEFAULT_BUFFER_MS, codec=DEFAULT_CODEC, error=None,
    )
    defaults.update(kw)
    return GroupingConfig(**defaults)


def _patch_grouping(monkeypatch, cfg, is_active_stdout=None, *,
                    unit_states=None):
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
            FakeRun.stdout = "\n".join(
                unit_states.get(u, "inactive") for u in argv[2:]
            ) + "\n"
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
    _patch_grouping(monkeypatch, _grouping_cfg(
        enabled=True, role="leader", channel="left", bond_id="x"), "")
    r = doctor.check_grouping_household_credential()
    assert r.status == "ok"
    assert "present" in r.detail


def test_check_household_credential_bonded_unpaired_warns(monkeypatch, tmp_path):
    """RECOVERY drift: bonded but the secret was lost -> /grouping/set is
    fail-safe-open; the doctor is the only place that loss is visible."""
    import jasper.control.household_credential as hc

    monkeypatch.setattr(hc, "SECRET_FILE", str(tmp_path / "absent"))
    _patch_grouping(monkeypatch, _grouping_cfg(
        enabled=True, role="follower", channel="right",
        bond_id="x", leader_addr="192.168.1.50"), "")
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
    _patch_grouping(monkeypatch, _grouping_cfg(
        enabled=True, role="follower", channel="right",
        bond_id="x", leader_addr="192.168.1.50"), "")
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
    _patch_grouping(monkeypatch, _grouping_cfg(
        enabled=True, role="leader", channel="left", bond_id="x"), "")
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

    cfg = _grouping_cfg(
        enabled=True, role="leader", channel="left", bond_id="x")
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
    monkeypatch.setattr(doctor.grouping.shutil, "which", lambda _name: "/usr/bin/systemd-analyze")
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
    _patch_grouping(monkeypatch, _grouping_cfg(
        enabled=True, role="follower", channel="sub",
        bond_id="lr", leader_addr="192.168.1.50"), "")
    r = doctor.check_grouping_local_vs_wireless_sub()
    assert r.status == "ok"
    assert "wireless sub only" in r.detail


def test_local_vs_wireless_sub_wireless_leader_only_is_ok(monkeypatch, tmp_path):
    from jasper.multiroom.config import BondMember

    _set_local_sub_topology(monkeypatch, tmp_path, with_sub=False)
    _patch_grouping(monkeypatch, _grouping_cfg(
        enabled=True, role="leader", channel="left", bond_id="lr",
        roster=(BondMember(addr="192.168.1.60", name="Sub", channel="sub"),)), "")
    r = doctor.check_grouping_local_vs_wireless_sub()
    assert r.status == "ok"
    assert "wireless sub only" in r.detail


def test_local_vs_wireless_sub_both_follower_warns(monkeypatch, tmp_path):
    # LOCAL sub topology AND this speaker is itself a wireless sub follower:
    # two bass producers at one speaker — fail LOUD.
    _set_local_sub_topology(monkeypatch, tmp_path, with_sub=True)
    _patch_grouping(monkeypatch, _grouping_cfg(
        enabled=True, role="follower", channel="sub",
        bond_id="lr", leader_addr="192.168.1.50"), "")
    r = doctor.check_grouping_local_vs_wireless_sub()
    assert r.status == "warn"
    assert "LOCAL sub" in r.detail
    assert "wireless sub follower" in r.detail


def test_local_vs_wireless_sub_both_leader_warns(monkeypatch, tmp_path):
    # LOCAL sub topology AND this leader's bond roster has a sub member.
    from jasper.multiroom.config import BondMember

    _set_local_sub_topology(monkeypatch, tmp_path, with_sub=True)
    _patch_grouping(monkeypatch, _grouping_cfg(
        enabled=True, role="leader", channel="left", bond_id="lr",
        roster=(BondMember(addr="192.168.1.60", name="Sub", channel="sub"),)), "")
    r = doctor.check_grouping_local_vs_wireless_sub()
    assert r.status == "warn"
    assert "LOCAL sub" in r.detail
    assert "leads a bond with a wireless sub member" in r.detail


def test_check_grouping_invalid_config_warns(monkeypatch):
    cfg = _grouping_cfg(
        enabled=True, role="leader", channel="left",
        error="JASPER_GROUPING_BOND_ID is empty (grouping is on)",
    )
    _patch_grouping(monkeypatch, cfg, "")
    r = doctor.check_grouping()
    assert r.status == "warn"
    assert "BOND_ID" in r.detail


def test_check_grouping_leader_reads_degraded_when_config_not_piped(monkeypatch, tmp_path):
    # Increment 5 honest state: even with both snap units active, if the
    # leader's ACTIVE CamillaDSP config does not write the snapserver
    # pipe (the bond apply did not land), the bond is silent — degraded,
    # with the specific operator explanation. Pin the statefile to a
    # missing path so a dev machine with real CamillaDSP state can't
    # flip the result.
    monkeypatch.setenv(
        "JASPER_CAMILLA_STATEFILE", str(tmp_path / "no-statefile.yml"),
    )
    cfg = _grouping_cfg(
        enabled=True, role="leader", channel="left", bond_id="living-room",
    )
    _patch_grouping(monkeypatch, cfg, unit_states={
        "jasper-snapserver.service": "active",
        "jasper-snapclient.service": "active",
    })
    r = doctor.check_grouping()
    assert r.status == "warn"
    assert "does not write the snapserver pipe" in r.detail
    assert "jasper-grouping-reconcile" in r.detail


def test_check_grouping_follower_unreachable_leader_warns(monkeypatch):
    cfg = _grouping_cfg(
        enabled=True, role="follower", channel="right",
        bond_id="living-room", leader_addr="192.168.1.50",
    )
    # snapclient failed => degraded, leader unreachable.
    _patch_grouping(monkeypatch, cfg, unit_states={
        "jasper-snapclient.service": "failed",
    })
    r = doctor.check_grouping()
    assert r.status == "warn"
    assert "follower not connected" in r.detail
    assert "192.168.1.50" in r.detail


def test_check_grouping_connected_follower_is_ok(monkeypatch):
    """A follower has no producer concept: with its snapclient active it
    reads ok — the missing leader-side producer can never push a FOLLOWER
    to degraded."""
    cfg = _grouping_cfg(
        enabled=True, role="follower", channel="right",
        bond_id="living-room", leader_addr="192.168.1.50",
    )
    # Connected follower (snapclient active) => ok; the parked source-resource
    # stack reads inactive by default, which can never degrade health
    # (only desired=start units can).
    _patch_grouping(monkeypatch, cfg, unit_states={
        "jasper-snapclient.service": "active",
    })
    r = doctor.check_grouping()
    assert r.status == "ok"
    assert "follower connected" in r.detail


def test_check_grouping_pair_lock_registered():
    assert "check_grouping_pair_lock" in _registered_check_names()


def test_check_grouping_pair_lock_warns_when_clock_lock_unobservable(monkeypatch):
    cfg = _grouping_cfg(
        enabled=True, role="follower", channel="right",
        bond_id="living-room", leader_addr="192.168.1.50",
    )
    _patch_grouping(monkeypatch, cfg, unit_states={
        "jasper-snapclient.service": "active",
    })
    monkeypatch.setattr(doctor.grouping, "_read_outputd_status", lambda: {
        "dac_content": {"enabled": True, "serving_fifo": True},
    })

    r = doctor.check_grouping_pair_lock()

    assert r.status == "warn"
    assert "clock lock is unobservable" in r.detail


def test_check_grouping_pair_lock_warns_when_fifo_not_serving(monkeypatch):
    cfg = _grouping_cfg(
        enabled=True, role="follower", channel="right",
        bond_id="living-room", leader_addr="192.168.1.50",
    )
    _patch_grouping(monkeypatch, cfg, unit_states={
        "jasper-snapclient.service": "active",
    })
    monkeypatch.setattr(doctor.grouping, "_read_outputd_status", lambda: {
        "dac_content": {"enabled": True, "serving_fifo": False},
    })

    r = doctor.check_grouping_pair_lock()

    assert r.status == "warn"
    assert "not serving FIFO bytes" in r.detail


def test_apple_dongle_check_skips_for_non_apple_output_dac(monkeypatch):
    def fail_probe(*_args, **_kwargs):
        raise AssertionError("Apple USB probe should not run")

    monkeypatch.delenv("JASPER_AUDIO_DAC_ID", raising=False)
    monkeypatch.setattr(
        doctor._shared,
        "_shared_parse_env_file",
        lambda _path: {"JASPER_AUDIO_DAC_ID": "hifiberry_dac8x"},
    )
    monkeypatch.setattr(doctor.audio, "_run", fail_probe)

    result = doctor.check_apple_dongle_audio()

    assert result.status == "ok"
    assert "active output DAC is hifiberry_dac8x" in result.detail


def test_apple_dongle_check_matches_usb_id_case_insensitively(monkeypatch):
    calls = []

    monkeypatch.delenv("JASPER_AUDIO_DAC_ID", raising=False)
    monkeypatch.setattr(
        doctor._shared,
        "_shared_parse_env_file",
        lambda _path: {"JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle"},
    )

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        if cmd == ["lsusb"]:
            return SimpleNamespace(
                returncode=0,
                stdout="Bus 001 Device 002: ID 05AC:110A Apple\n",
                stderr="",
            )
        if cmd == ["aplay", "-l"]:
            return SimpleNamespace(
                returncode=0,
                stdout="card 2: Apple [Apple USB-C to 3.5mm Headphone Jack]\n",
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(doctor.audio, "_run", fake_run)

    result = doctor.check_apple_dongle_audio()

    assert result.status == "ok"
    assert calls == [["lsusb"], ["aplay", "-l"]]


def test_dongle_headphone_gain_check_skips_for_non_apple_output_dac(monkeypatch):
    def fail_probe(*_args, **_kwargs):
        raise AssertionError("Apple mixer probe should not run")

    monkeypatch.delenv("JASPER_AUDIO_DAC_ID", raising=False)
    monkeypatch.setattr(
        doctor._shared,
        "_shared_parse_env_file",
        lambda _path: {"JASPER_AUDIO_DAC_ID": "hifiberry_dac8x"},
    )
    monkeypatch.setattr(doctor.audio, "_run", fail_probe)

    result = doctor.check_dongle_headphone_at_max()

    assert result.status == "ok"
    assert "active output DAC is hifiberry_dac8x" in result.detail


def test_dongle_headphone_gain_check_uses_reconciled_card(monkeypatch):
    calls = []

    monkeypatch.delenv("JASPER_AUDIO_DAC_ID", raising=False)
    monkeypatch.delenv("JASPER_AUDIO_DAC_CARD", raising=False)
    monkeypatch.setattr(
        doctor._shared,
        "_shared_parse_env_file",
        lambda _path: {
            "JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle",
            "JASPER_AUDIO_DAC_CARD": "Apple2",
        },
    )

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout="Front Left: Playback 100 [100%] [0.00dB] [on]\n",
            stderr="",
        )

    monkeypatch.setattr(doctor.audio, "_run", fake_run)

    result = doctor.check_dongle_headphone_at_max()

    assert result.status == "ok"
    assert calls == [["amixer", "-c", "Apple2", "sget", "Headphone"]]


def test_dual_apple_dongle_check_requires_two_audio_cards(monkeypatch):
    state = OutputHardwareState(
        profile_id=DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
        profile_label="Dual Apple USB-C DAC 4-channel pair",
        status="partial",
        physical_output_count=4,
        apple_dac_count=1,
        child_devices=(
            OutputCardFact(
                card_id="A",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            ),
        ),
    )

    def fake_run(cmd, *args, **kwargs):
        if cmd == ["lsusb"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "Bus 001 Device 002: ID 05ac:110a Apple USB-C\n"
                    "Bus 001 Device 003: ID 05ac:110a Apple USB-C\n"
                ),
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(doctor.audio, "_load_output_hardware_state", lambda: state)
    monkeypatch.setattr(doctor.audio, "_run", fake_run)

    result = doctor.check_apple_dongle_audio()

    assert result.status == "warn"
    assert "only 1 Apple audio card(s)" in result.detail


def test_active_speaker_hardware_mismatch_is_separate_from_basic_output_health(
    monkeypatch,
    tmp_path,
):
    from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, save_output_topology
    from jasper.output_topology import OutputTopology

    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple_pair",
        "name": "Dual Apple active pair",
        "status": "draft",
        "hardware": {
            "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
            "device_label": "Dual Apple USB-C DAC 4-channel pair",
            "physical_output_count": 4,
            "child_devices": [
                {
                    "child_id": "left_dac",
                    "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                    "device_label": "Apple USB-C audio adapter",
                    "serial": "DWH53530FHL2FN3AC",
                    "physical_output_indexes": [0, 1],
                },
                {
                    "child_id": "right_dac",
                    "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                    "device_label": "Apple USB-C audio adapter",
                    "serial": "DWH53530FLL2FN3A3",
                    "physical_output_indexes": [2, 3],
                },
            ],
        },
        "speaker_groups": [
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "active_2_way",
                "channels": [
                    {"role": "woofer", "physical_output_index": 0},
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "present",
                    },
                ],
            },
            {
                "id": "right",
                "label": "Right speaker",
                "kind": "right",
                "mode": "active_2_way",
                "channels": [
                    {"role": "woofer", "physical_output_index": 2},
                    {
                        "role": "tweeter",
                        "physical_output_index": 3,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "present",
                    },
                ],
            },
        ],
        "routing": {
            "main_left_group_id": "left",
            "main_right_group_id": "right",
        },
    })
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    state = OutputHardwareState(
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        profile_label="Apple USB-C audio adapter",
        status="ready",
        physical_output_count=2,
        selected_card_id="A",
        selected_pcm="hw:CARD=A,DEV=0",
        apple_dac_count=1,
        child_devices=(
            OutputCardFact(
                card_id="A",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                serial="DWH53530FHL2FN3AC",
            ),
        ),
    )
    monkeypatch.setattr(doctor.audio, "_load_output_hardware_state", lambda: state)

    output = doctor.check_output_hardware_state()
    active = doctor.check_active_speaker_output_hardware_match()

    assert output.status == "ok"
    assert "profile=apple_usb_c_dongle status=ready outputs=2" in output.detail
    assert active.status == "fail"
    assert f"saved={DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID}" in active.detail
    assert f"current={APPLE_USB_C_DONGLE_DEVICE_ID} status=ready" in active.detail
    assert "active speaker actions are blocked" in active.detail
    assert "Basic output hardware is reported separately" in active.detail


def test_active_speaker_hardware_match_checks_dual_apple_child_serials(
    monkeypatch,
    tmp_path,
):
    from jasper.output_topology import OUTPUT_TOPOLOGY_KIND, OutputTopology
    from jasper.output_topology import save_output_topology

    topology = OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "dual_apple_pair",
        "name": "Dual Apple active pair",
        "status": "draft",
        "hardware": {
            "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
            "device_label": "Dual Apple USB-C DAC 4-channel pair",
            "physical_output_count": 4,
            "child_devices": [
                {
                    "child_id": "left_dac",
                    "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                    "device_label": "Apple USB-C audio adapter",
                    "serial": "DWH53530FHL2FN3AC",
                    "physical_output_indexes": [0, 1],
                },
                {
                    "child_id": "right_dac",
                    "device_id": APPLE_USB_C_DONGLE_DEVICE_ID,
                    "device_label": "Apple USB-C audio adapter",
                    "serial": "DWH53530FLL2FN3A3",
                    "physical_output_indexes": [2, 3],
                },
            ],
            "clock_domain_evidence": {
                "evidence_kind": "dual_apple_usb_c_dac_drift_measurement",
                "measurement_id": "doctor-serial-contract",
                "status": "passed",
                "duration_seconds": 900,
                "sample_rate_hz": 48000,
                "offset_frames": -7,
                "max_offset_delta_frames": 0,
                "drift_ppm": 0,
                "xrun_count": 0,
                "dac_serials": [
                    "DWH53530FHL2FN3AC",
                    "DWH53530FLL2FN3A3",
                ],
            },
        },
        "speaker_groups": [
            {
                "id": "left",
                "label": "Left speaker",
                "kind": "left",
                "mode": "active_2_way",
                "channels": [
                    {"role": "woofer", "physical_output_index": 0},
                    {
                        "role": "tweeter",
                        "physical_output_index": 1,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "present",
                    },
                ],
            },
            {
                "id": "right",
                "label": "Right speaker",
                "kind": "right",
                "mode": "active_2_way",
                "channels": [
                    {"role": "woofer", "physical_output_index": 2},
                    {
                        "role": "tweeter",
                        "physical_output_index": 3,
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "present",
                    },
                ],
            },
        ],
        "routing": {
            "main_left_group_id": "left",
            "main_right_group_id": "right",
        },
    })
    topology_path = tmp_path / "output_topology.json"
    hardware_path = tmp_path / "output_hardware.json"
    save_output_topology(topology, path=topology_path)
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_OUTPUT_HARDWARE_STATE_PATH", str(hardware_path))
    write_output_hardware_state(
        classify_output_cards([
            OutputCardFact(
                card_id="A",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                serial="WRONGLEFTSERIAL",
                usb_path="usb1/1-2",
                busnum="1",
                controller="xhci-hcd.0",
                endpoint_sync="SYNC",
            ),
            OutputCardFact(
                card_id="A_1",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                serial="WRONGRIGHTSERIAL",
                usb_path="usb1/1-1",
                busnum="1",
                controller="xhci-hcd.0",
                endpoint_sync="SYNC",
            ),
        ]),
        path=hardware_path,
    )

    output = doctor.check_output_hardware_state()
    active = doctor.check_active_speaker_output_hardware_match()

    assert output.status == "ok"
    assert (
        f"profile={DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID} status=ready outputs=4"
        in output.detail
    )
    assert active.status == "fail"
    assert f"saved={DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID}" in active.detail
    assert f"current={DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID} status=ready" in active.detail
    assert "current-hardware clock blockers=dual_apple_observed_serial_mismatch" in active.detail
    assert "active speaker actions are blocked" in active.detail
    assert "Basic output hardware is reported separately" in active.detail


def test_dual_apple_headphone_gain_checks_every_card(monkeypatch):
    state = OutputHardwareState(
        profile_id=DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
        profile_label="Dual Apple USB-C DAC 4-channel pair",
        status="ready",
        physical_output_count=4,
        apple_dac_count=2,
        child_devices=(
            OutputCardFact(
                card_id="A",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            ),
            OutputCardFact(
                card_id="A_1",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
            ),
        ),
    )
    commands: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        commands.append(cmd)
        if cmd[:3] == ["amixer", "-c", "A"]:
            return SimpleNamespace(
                returncode=0,
                stdout="Front Left: Playback 120 [100%] [0.00dB] [on]\n",
                stderr="",
            )
        if cmd[:3] == ["amixer", "-c", "A_1"]:
            return SimpleNamespace(
                returncode=0,
                stdout="Front Left: Playback 90 [75%] [-10.00dB] [on]\n",
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(doctor.audio, "_load_output_hardware_state", lambda: state)
    monkeypatch.setattr(doctor.audio, "_run", fake_run)

    result = doctor.check_dongle_headphone_at_max()

    assert result.status == "warn"
    assert "A_1:75%" in result.detail
    assert ["amixer", "-c", "A", "sget", "Headphone"] in commands
    assert ["amixer", "-c", "A_1", "sget", "Headphone"] in commands


def test_check_bluetooth_pairing_policy_ok(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["systemctl", "show", "bt-agent.service"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "ActiveState=active\n"
                    "SubState=running\n"
                    "ExecStart={ path=/opt/jasper/.venv/bin/jasper-bluetooth-agent ; }\n"
                ),
                stderr="",
            )
        if cmd == ["bluetoothctl", "show"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "\tPowered: yes\n"
                    "\tDiscoverable: no\n"
                    "\tPairable: no\n"
                ),
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(doctor.renderers, "_run", fake_run)

    r = doctor.check_bluetooth_pairing_policy()

    assert r.status == "ok"
    assert "no-code agent active" in r.detail
    assert "closed" in r.detail


def test_check_bluetooth_pairing_policy_fails_old_agent(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        assert cmd[:3] == ["systemctl", "show", "bt-agent.service"]
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "ActiveState=active\n"
                "SubState=running\n"
                "ExecStart={ path=/usr/bin/bt-agent ; }\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(doctor.renderers, "_run", fake_run)

    r = doctor.check_bluetooth_pairing_policy()

    assert r.status == "fail"
    assert "not the JTS no-code agent" in r.detail


def test_check_bluetooth_pairing_policy_warns_pairable_outside_window(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["systemctl", "show", "bt-agent.service"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "ActiveState=active\n"
                    "SubState=running\n"
                    "ExecStart={ path=/opt/jasper/.venv/bin/jasper-bluetooth-agent ; }\n"
                ),
                stderr="",
            )
        if cmd == ["bluetoothctl", "show"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "\tPowered: yes\n"
                    "\tDiscoverable: no\n"
                    "\tPairable: yes\n"
                ),
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(doctor.renderers, "_run", fake_run)

    r = doctor.check_bluetooth_pairing_policy()

    assert r.status == "warn"
    assert "Pairable=yes outside an open pairing window" in r.detail


def test_check_bluetooth_pairing_policy_warns_when_pairing_window_open(monkeypatch):
    def fake_run(cmd, *args, **kwargs):
        if cmd[:3] == ["systemctl", "show", "bt-agent.service"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "ActiveState=active\n"
                    "SubState=running\n"
                    "ExecStart={ path=/opt/jasper/.venv/bin/jasper-bluetooth-agent ; }\n"
                ),
                stderr="",
            )
        if cmd == ["bluetoothctl", "show"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "\tPowered: yes\n"
                    "\tDiscoverable: yes\n"
                    "\tPairable: yes\n"
                ),
                stderr="",
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(doctor.renderers, "_run", fake_run)

    r = doctor.check_bluetooth_pairing_policy()

    assert r.status == "warn"
    assert "pairing window open" in r.detail


def test_subprocess_env_with_fresh_files_overrides_stale_daemon_env(tmp_path: Path):
    """Long-lived daemons launching subprocesses need fresh wizard-file
    truth, not the process env captured when the daemon started."""
    from jasper.env_load import subprocess_env_with_fresh_files
    operator = tmp_path / "jasper.env"
    operator.write_text("JASPER_VOICE_PROVIDER=gemini\n")
    wizard = tmp_path / "voice_provider.env"
    wizard.write_text(
        "JASPER_VOICE_PROVIDER=openai\n"
        "OPENAI_API_KEY=sk-fresh\n"
    )

    env = subprocess_env_with_fresh_files(
        base={"PATH": "/bin", "JASPER_VOICE_PROVIDER": "gemini"},
        paths=(str(operator), str(wizard)),
    )

    assert env["PATH"] == "/bin"
    assert env["JASPER_VOICE_PROVIDER"] == "openai"
    assert env["OPENAI_API_KEY"] == "sk-fresh"


def os_environ_get(name: str) -> str | None:
    import os
    return os.environ.get(name)


# -------------------------------------------------- openWakeWord assets


def _install_fake_openwakeword_package(
    monkeypatch,
    tmp_path: Path,
    files: dict[str, bytes],
    required_assets: tuple[SimpleNamespace, ...],
    package_assets: tuple[SimpleNamespace, ...] | None = None,
) -> None:
    pkg = tmp_path / "openwakeword"
    models = pkg / "resources" / "models"
    models.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    for filename, payload in files.items():
        (models / filename).write_bytes(payload)
    monkeypatch.setitem(
        sys.modules,
        "openwakeword",
        SimpleNamespace(__file__=str(pkg / "__init__.py")),
    )
    monkeypatch.setattr(
        wake_models,
        "required_openwakeword_assets",
        lambda: required_assets,
    )
    if package_assets is not None:
        monkeypatch.setattr(
            wake_models,
            "openwakeword_assets",
            lambda: package_assets,
        )


def _fake_asset(filename: str, payload: bytes = b"model") -> SimpleNamespace:
    return SimpleNamespace(
        filename=filename,
        download_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _required_fake_openwakeword_assets() -> tuple[SimpleNamespace, ...]:
    return (
        _fake_asset("embedding_model.onnx"),
        _fake_asset("melspectrogram.onnx"),
        _fake_asset("silero_vad.onnx"),
    )


def _fake_openwakeword_assets() -> tuple[SimpleNamespace, ...]:
    return _required_fake_openwakeword_assets() + (
        SimpleNamespace(
            key="hey_jarvis",
            filename="hey_jarvis_v0.1.onnx",
            download_sha256=hashlib.sha256(b"model").hexdigest(),
        ),
        SimpleNamespace(
            key="alexa",
            filename="alexa_v0.1.onnx",
            download_sha256=hashlib.sha256(b"model").hexdigest(),
        ),
    )


def test_openwakeword_doctor_fails_when_silero_asset_missing(monkeypatch, tmp_path: Path):
    required_assets = _required_fake_openwakeword_assets()
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "hey_jarvis_v0.1.onnx": b"model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model="hey_jarvis"))

    assert r.status == "fail"
    assert "silero_vad.onnx" in r.detail


def test_openwakeword_doctor_allows_missing_inactive_bundled_model(
    monkeypatch,
    tmp_path: Path,
):
    required_assets = _required_fake_openwakeword_assets()
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "silero_vad.onnx": b"model",
            "hey_jarvis_v0.1.onnx": b"model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model="hey_jarvis"))

    assert r.status == "ok"
    assert "hey_jarvis_v0.1.onnx" in r.detail


def test_openwakeword_doctor_fails_when_active_bundled_model_missing(
    monkeypatch,
    tmp_path: Path,
):
    required_assets = _required_fake_openwakeword_assets()
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "silero_vad.onnx": b"model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model="hey_jarvis"))

    assert r.status == "fail"
    assert "active wake model" in r.detail
    assert "deploy/install.sh" in r.detail


def test_openwakeword_doctor_missing_custom_model_points_at_path(
    monkeypatch,
    tmp_path: Path,
):
    required_assets = _required_fake_openwakeword_assets()
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "silero_vad.onnx": b"model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )
    missing = tmp_path / "custom-missing.onnx"

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model=str(missing)))

    assert r.status == "fail"
    assert f"active wake model path missing: {missing}" in r.detail
    assert "registered model in /wake/" in r.detail


def test_openwakeword_doctor_fails_when_required_asset_hash_mismatches(
    monkeypatch,
    tmp_path: Path,
):
    required_assets = _required_fake_openwakeword_assets()
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "silero_vad.onnx": b"wrong-model",
            "hey_jarvis_v0.1.onnx": b"model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model="hey_jarvis"))

    assert r.status == "fail"
    assert "hash mismatch" in r.detail
    assert "silero_vad.onnx" in r.detail
    assert "deploy/install.sh" in r.detail


def test_openwakeword_doctor_fails_when_active_external_model_hash_mismatches(
    monkeypatch,
    tmp_path: Path,
):
    required_assets = _required_fake_openwakeword_assets()
    active_model = tmp_path / "jarvis_v2.onnx"
    active_model.write_bytes(b"wrong-model")
    monkeypatch.setattr(
        wake_models,
        "by_model",
        lambda model: SimpleNamespace(
            download_sha256=hashlib.sha256(b"model").hexdigest(),
        ) if model == str(active_model) else None,
    )
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "silero_vad.onnx": b"model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model=str(active_model)))

    assert r.status == "fail"
    assert "active wake model hash mismatch" in r.detail
    assert "jarvis_v2.onnx" in r.detail


def test_openwakeword_doctor_fails_when_active_bundled_model_hash_mismatches(
    monkeypatch,
    tmp_path: Path,
):
    required_assets = _required_fake_openwakeword_assets()
    _install_fake_openwakeword_package(
        monkeypatch,
        tmp_path,
        {
            "embedding_model.onnx": b"model",
            "melspectrogram.onnx": b"model",
            "silero_vad.onnx": b"model",
            "hey_jarvis_v0.1.onnx": b"wrong-model",
        },
        required_assets,
        _fake_openwakeword_assets(),
    )

    r = doctor.check_openwakeword_model(SimpleNamespace(wake_model="hey_jarvis"))

    assert r.status == "fail"
    assert "active wake model hash mismatch" in r.detail
    assert "hey_jarvis_v0.1.onnx" in r.detail


# -------------------------------------------------- provider-aware key check


def _fresh_cfg(monkeypatch, **vars_) -> Config:
    """Build a Config with only the requested env vars set.

    Defaults JASPER_VOICE_PROVIDER=gemini so callers that only care
    about a single provider's key can omit it. Pass the var explicitly
    to override (e.g. testing the openai or grok path).
    """
    drop = [
        "GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
        "JASPER_VOICE_PROVIDER", "JASPER_GEMINI_MODEL",
        "SPOTIFY_CLIENT_ID",
    ]
    for v in drop:
        monkeypatch.delenv(v, raising=False)
    defaults = {"JASPER_VOICE_PROVIDER": "gemini"}
    for k, v in {**defaults, **vars_}.items():
        monkeypatch.setenv(k, v)
    return Config.from_env()


def test_provider_key_gemini_ok(monkeypatch):
    cfg = _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaSyABCDEF12345")
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"
    assert r.name == "GEMINI_API_KEY"


def test_provider_key_openai_ok(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="sk-realkey1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"
    assert r.name == "OPENAI_API_KEY"


def test_provider_key_grok_ok(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="grok",
        XAI_API_KEY="xai-realkey1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"
    assert r.name == "XAI_API_KEY"


def test_provider_key_warns_on_wrong_prefix(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="WRONGPREFIX-1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "warn"


def test_provider_key_other_providers_keys_unchecked(monkeypatch):
    """Active=openai. GEMINI_API_KEY is intentionally unset; the doctor
    must NOT flag that as a problem — gemini is dormant."""
    cfg = _fresh_cfg(
        monkeypatch,
        JASPER_VOICE_PROVIDER="openai",
        OPENAI_API_KEY="sk-active1234",
    )
    r = doctor.check_provider_key(cfg)
    assert r.status == "ok"


def test_provider_key_accepts_each_catalog_provider():
    for provider in PROVIDERS:
        key = provider.key_prefix_hint.rstrip(".") + "test-key"
        cfg = SimpleNamespace(
            voice_provider=provider.id,
            **{f"{provider.id.replace('-', '_')}_api_key": key},
        )

        r = doctor.check_provider_key(cfg)  # type: ignore[arg-type]

        assert r.status == "ok"
        assert r.name == provider.key_env


def test_voice_provider_ids_manifest_ok(monkeypatch, tmp_path: Path):
    manifest = tmp_path / "voice_provider_ids"
    manifest.write_text(provider_ids_manifest_text())
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_IDS_FILE", str(manifest))

    r = doctor.check_voice_provider_ids_manifest()

    assert r.status == "ok"


def test_voice_provider_ids_manifest_missing_fails(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(
        "JASPER_VOICE_PROVIDER_IDS_FILE",
        str(tmp_path / "missing_provider_ids"),
    )

    r = doctor.check_voice_provider_ids_manifest()

    assert r.status == "fail"
    assert "missing" in r.detail


def test_voice_provider_ids_manifest_stale_fails(monkeypatch, tmp_path: Path):
    manifest = tmp_path / "voice_provider_ids"
    manifest.write_text("gemini\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_IDS_FILE", str(manifest))

    r = doctor.check_voice_provider_ids_manifest()

    assert r.status == "fail"
    assert "stale" in r.detail


def test_voice_provider_ids_manifest_reordered_warns(monkeypatch, tmp_path: Path):
    manifest = tmp_path / "voice_provider_ids"
    manifest.write_text("openai\ngrok\ngemini\n")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER_IDS_FILE", str(manifest))

    r = doctor.check_voice_provider_ids_manifest()

    assert r.status == "warn"


# ------------------------------------------------------ Spotify Connect check


def test_spotify_connect_device_consumes_build_result(monkeypatch, tmp_path: Path):
    """build_clients returns BuildResult, not a bare clients dict.

    The dashboard runs `jasper-doctor --json` through jasper-control; a
    shape mismatch here used to crash before JSON rendering, which made
    /system/diagnostics report "doctor output not JSON".
    """
    accounts_path = tmp_path / "accounts.json"
    accounts_path.write_text(
        '{"accounts": [{"name": "jasper", "cache_path": "/tmp/cache"}], '
        '"default": "jasper"}'
    )
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        SPOTIFY_CLIENT_ID="a" * 32,
        JASPER_SPOTIFY_ACCOUNTS_PATH=str(accounts_path),
        JASPER_SPEAKER_NAME="JTS",
    )

    from jasper.spotify_router import ACCOUNT_OK, AccountStatus, BuildResult

    fake_client = SimpleNamespace(
        sp=SimpleNamespace(devices=lambda: {"devices": [{"name": "Kitchen JTS"}]}),
    )

    def fake_build_clients(_registry, *, client_id, redirect_uri):  # noqa: ARG001
        return BuildResult(
            clients={"jasper": fake_client},
            statuses=[AccountStatus(name="jasper", state=ACCOUNT_OK)],
            default_name="jasper",
        )

    with patch("jasper.spotify_router.build_clients", side_effect=fake_build_clients):
        result = doctor.check_spotify_connect_device(cfg)

    assert result.status == "ok"
    assert "jasper" in result.detail


def test_json_mode_reports_unhandled_check_exception(monkeypatch, capsys):
    """Machine-readable mode should stay machine-readable even if a
    diagnostic check raises unexpectedly."""
    monkeypatch.setattr(doctor, "_load_env_files", lambda: None)
    monkeypatch.setattr(Config, "from_env", staticmethod(lambda: object()))

    async def boom(_cfg):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(doctor, "run_async", boom)
    monkeypatch.setattr(sys, "argv", ["jasper-doctor", "--json"])

    try:
        doctor.main()
    except SystemExit as e:
        assert e.code == 1
    else:  # pragma: no cover - defensive, main() should always exit.
        raise AssertionError("main() did not exit")

    payload = json.loads(capsys.readouterr().out)
    assert payload["fails"] == 1
    assert payload["results"][0]["name"] == "jasper-doctor"
    assert "synthetic failure" in payload["error"]


def test_json_mode_endpoint_tier_does_not_require_voice_provider(
    monkeypatch,
    capsys,
):
    """Endpoint doctor must not build full voice Config before filtering.

    A freshly imaged dumb endpoint has no JASPER_VOICE_PROVIDER by design;
    it should still report endpoint health instead of failing at Config
    construction.
    """
    monkeypatch.setattr(doctor, "_load_env_files", lambda: None)
    monkeypatch.setattr(doctor, "read_install_profile", lambda: "endpoint")
    monkeypatch.setattr(
        Config,
        "from_env",
        staticmethod(lambda: (_ for _ in ()).throw(
            AssertionError("endpoint doctor must not construct full Config")
        )),
    )
    monkeypatch.setenv("JASPER_USAGE_DB", "/tmp/jasper-endpoint-usage.db")

    async def fake_run_async(cfg):
        assert cfg.usage_db == "/tmp/jasper-endpoint-usage.db"
        return [doctor.CheckResult("endpoint smoke", "ok", "minimal cfg")]

    monkeypatch.setattr(doctor, "run_async", fake_run_async)
    monkeypatch.setattr(sys, "argv", ["jasper-doctor", "--json"])

    try:
        doctor.main()
    except SystemExit as e:
        assert e.code == 0
    else:  # pragma: no cover - defensive, main() should always exit.
        raise AssertionError("main() did not exit")

    payload = json.loads(capsys.readouterr().out)
    assert payload["fails"] == 0
    assert payload["results"] == [
        {
            "name": "endpoint smoke",
            "status": "ok",
            "detail": "minimal cfg",
        }
    ]


def test_doctor_check_exception_becomes_fail_result():
    def explode():
        raise RuntimeError("synthetic check failure")

    result = doctor._run_doctor_check(("explosive check", explode))

    assert result.name == "explosive check"
    assert result.status == "fail"
    assert "RuntimeError: synthetic check failure" in result.detail


def test_doctor_check_exception_redacts_secret_like_values():
    def explode():
        raise RuntimeError(
            "refresh_token=super-secret-refresh "
            "Bearer super-secret-access-token "
            "sk-super-secret-openai-key"
        )

    result = doctor._run_doctor_check(("sensitive check", explode))

    assert "super-secret-refresh" not in result.detail
    assert "super-secret-access-token" not in result.detail
    assert "sk-super-secret-openai-key" not in result.detail
    assert "refresh_token=<redacted>" in result.detail
    assert "Bearer <redacted>" in result.detail
    assert "sk-s...-key" in result.detail


def test_async_doctor_check_exception_becomes_fail_result():
    async def explode():
        raise RuntimeError("synthetic async failure")

    result = asyncio.run(
        doctor._run_async_doctor_check("async check", explode),
    )

    assert result.name == "async check"
    assert result.status == "fail"
    assert "RuntimeError: synthetic async failure" in result.detail


def test_legacy_endpoint_token_doctor_behaves_as_streambox(monkeypatch):
    """A persisted/legacy 'endpoint' token normalizes to streambox, so the
    doctor applies the streambox skip behaviour (voice/brain groups skipped,
    local audio kept)."""
    from jasper.cli.doctor._registry import RegisteredCheck

    ran: list[str] = []

    def env_check():
        ran.append("env")
        return doctor.CheckResult("env file", "ok", "ran")

    def voice_check(_cfg):
        ran.append("voice")
        return doctor.CheckResult("provider key", "fail", "should not run")

    def web_check():
        ran.append("web")
        return doctor.CheckResult("management surface", "ok", "ran")

    monkeypatch.setattr(doctor, "read_install_profile", lambda: "endpoint")
    monkeypatch.setattr(doctor, "registered_checks", lambda: [
        RegisteredCheck(order=0, group="env", func=env_check),
        RegisteredCheck(
            order=1, group="voice", func=voice_check,
            needs_cfg=True, label="provider key",
        ),
        RegisteredCheck(order=1.5, group="web", func=web_check),
    ])

    results = asyncio.run(doctor.run_async(object()))

    assert ran == ["env", "web"]
    assert [(r.name, r.status, r.detail) for r in results] == [
        ("env file", "ok", "ran"),
        ("provider key", "ok", "not installed (streambox profile)"),
        ("management surface", "ok", "ran"),
    ]


def test_streambox_doctor_config_does_not_require_voice_provider(monkeypatch):
    monkeypatch.delenv("JASPER_VOICE_PROVIDER", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.setenv("JASPER_HOSTNAME", "jts4.local")
    monkeypatch.setenv("JASPER_SPEAKER_NAME", "JTS4")

    cfg = doctor._doctor_config_from_env("streambox")

    assert cfg.usage_db
    assert cfg.camilla_host == "127.0.0.1"
    assert cfg.camilla_port == 1234
    assert cfg.spotify_enabled is False
    assert cfg.spotify_device_name == "JTS4"
    assert cfg.spotify_setup_url == "http://jts4.local/spotify"


def test_streambox_doctor_skips_voice_brain_but_keeps_local_audio_checks():
    by_name = {entry.func.__name__: entry for entry in doctor.registered_checks()}

    assert (
        doctor._doctor_skip_reason(by_name["check_provider_key"], "streambox")
        == "not installed (streambox profile)"
    )
    assert doctor._doctor_skip_reason(
        by_name["check_openwakeword_model"], "streambox",
    )
    assert doctor._doctor_skip_reason(
        by_name["check_aec_bridge_running"], "streambox",
    )
    assert doctor._doctor_skip_reason(by_name["check_mic_capture"], "streambox")
    assert doctor._doctor_skip_reason(by_name["check_tts_open"], "streambox")
    assert not doctor._doctor_skip_reason(
        by_name["check_camilla_websocket"], "streambox",
    )
    assert not doctor._doctor_skip_reason(
        by_name["check_librespot_running"], "streambox",
    )
    assert not doctor._doctor_skip_reason(
        by_name["check_correction_web_service"], "streambox",
    )


def test_streambox_profile_doctor_keeps_local_audio_groups(monkeypatch):
    from jasper.cli.doctor._registry import RegisteredCheck

    ran: list[str] = []

    def voice_check(_cfg):
        ran.append("voice")
        return doctor.CheckResult("provider key", "fail", "should not run")

    def check_mic_capture(_cfg):
        ran.append("mic")
        return doctor.CheckResult("mic capture", "fail", "should not run")

    def renderer_check(_cfg):
        ran.append("renderers")
        return doctor.CheckResult("librespot.service", "ok", "ran")

    def correction_check():
        ran.append("correction")
        return doctor.CheckResult("room correction service", "ok", "ran")

    monkeypatch.setattr(doctor, "read_install_profile", lambda: "streambox")
    monkeypatch.setattr(doctor, "registered_checks", lambda: [
        RegisteredCheck(
            order=0, group="voice", func=voice_check,
            needs_cfg=True, label="provider key",
        ),
        RegisteredCheck(
            order=1, group="audio", func=check_mic_capture,
            needs_cfg=True, label="mic capture",
        ),
        RegisteredCheck(
            order=2, group="renderers", func=renderer_check,
            needs_cfg=True, label="librespot.service",
        ),
        RegisteredCheck(order=3, group="correction", func=correction_check),
    ])

    results = asyncio.run(doctor.run_async(SimpleNamespace()))

    assert ran == ["renderers", "correction"]
    assert [(r.name, r.status, r.detail) for r in results] == [
        ("provider key", "ok", "not installed (streambox profile)"),
        ("mic capture", "ok", "not installed (streambox profile)"),
        ("librespot.service", "ok", "ran"),
        ("room correction service", "ok", "ran"),
    ]


def test_run_async_parallelizes_blocking_checks_but_preserves_order(
    monkeypatch,
):
    from jasper.cli.doctor._registry import RegisteredCheck

    def make_check(name: str, delay: float):
        def check():
            time.sleep(delay)
            return doctor.CheckResult(name, "ok", "ran")
        return check

    monkeypatch.setattr(doctor, "read_install_profile", lambda: "full")
    monkeypatch.setenv("JASPER_DOCTOR_MAX_CONCURRENCY", "3")
    monkeypatch.setattr(doctor, "registered_checks", lambda: [
        RegisteredCheck(order=0, group="test", func=make_check("a", 0.15)),
        RegisteredCheck(order=1, group="test", func=make_check("b", 0.15)),
        RegisteredCheck(order=2, group="test", func=make_check("c", 0.15)),
        RegisteredCheck(order=3, group="test", func=make_check("d", 0.15)),
        RegisteredCheck(order=4, group="test", func=make_check("e", 0.15)),
        RegisteredCheck(order=5, group="test", func=make_check("f", 0.15)),
    ])

    started = time.perf_counter()
    results = asyncio.run(doctor.run_async(SimpleNamespace()))
    elapsed = time.perf_counter() - started

    assert [r.name for r in results] == ["a", "b", "c", "d", "e", "f"]
    assert elapsed < 0.65, (
        f"doctor run took {elapsed:.3f}s; expected bounded parallelism, "
        "not six sequential 150ms checks"
    )


def test_run_async_serializes_checks_in_same_exclusive_group(monkeypatch):
    from jasper.cli.doctor._registry import RegisteredCheck

    active = 0
    max_active = 0
    lock = threading.Lock()

    def exclusive(name: str):
        def check():
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.05)
            with lock:
                active -= 1
            return doctor.CheckResult(name, "ok", "ran")
        return check

    def ordinary():
        time.sleep(0.05)
        return doctor.CheckResult("c", "ok", "ran")

    monkeypatch.setattr(doctor, "read_install_profile", lambda: "full")
    monkeypatch.setenv("JASPER_DOCTOR_MAX_CONCURRENCY", "3")
    monkeypatch.setattr(doctor, "registered_checks", lambda: [
        RegisteredCheck(
            order=0,
            group="test",
            func=exclusive("a"),
            exclusive_group="audio-probe",
        ),
        RegisteredCheck(
            order=1,
            group="test",
            func=exclusive("b"),
            exclusive_group="audio-probe",
        ),
        RegisteredCheck(order=2, group="test", func=ordinary),
    ])

    results = asyncio.run(doctor.run_async(SimpleNamespace()))

    assert [r.name for r in results] == ["a", "b", "c"]
    assert max_active == 1


# ------------------------------------------------ ALSA shorthand mic lookup


def test_extract_card_name_returns_none_for_shorthand():
    assert doctor._extract_card_name("hw:7,1") is None
    assert doctor._extract_card_name("plughw:0,0") is None


def test_extract_card_name_named_card_passthrough():
    assert doctor._extract_card_name("Array") == "Array"
    assert doctor._extract_card_name("plughw:CARD=Loopback") == "Loopback"


def test_check_arecord_l_card_device_match():
    """Mock arecord -l output for a 6-card system that includes the
    LoopbackAEC bridge target (card 7, device 1)."""
    fake_output = (
        "card 0: dongle [USB Audio], device 0: USB Audio [USB Audio]\n"
        "card 1: Array [XVF3800 Voice Capture], device 0: USB Audio\n"
        "card 6: Loopback [Loopback], device 0: Loopback PCM\n"
        "card 6: Loopback [Loopback], device 1: Loopback PCM\n"
        "card 7: LoopbackAEC [Loopback], device 0: Loopback PCM\n"
        "card 7: LoopbackAEC [Loopback], device 1: Loopback PCM\n"
    )
    with patch.object(
        doctor.audio, "_run",
        return_value=type("FakeProc", (), {"stdout": fake_output, "returncode": 0})(),
    ), patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"):
        assert doctor._check_arecord_l_card_device(7, 1) is True
        assert doctor._check_arecord_l_card_device(7, 0) is True
        assert doctor._check_arecord_l_card_device(99, 0) is False


def test_check_arecord_l_does_not_match_wrong_card():
    """`device 1:` paired with card 6 must NOT satisfy a query for
    card 7 device 1 — both numbers must come from the same line."""
    fake_output = (
        "card 6: Loopback [Loopback], device 1: Loopback PCM\n"
        "card 7: LoopbackAEC [Loopback], device 0: Loopback PCM\n"
    )
    with patch.object(
        doctor.audio, "_run",
        return_value=type("FakeProc", (), {"stdout": fake_output, "returncode": 0})(),
    ), patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"):
        assert doctor._check_arecord_l_card_device(7, 1) is False


def test_check_mic_card_routes_shorthand_through_arecord_l(monkeypatch):
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )
    fake_output = (
        "card 7: LoopbackAEC [Loopback], device 1: Loopback PCM\n"
    )
    with patch.object(
        doctor.audio, "_run",
        return_value=type("FakeProc", (), {"stdout": fake_output, "returncode": 0})(),
    ), patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"):
        r = doctor.check_mic_card_matches_config(cfg)
    assert r.status == "ok"
    assert "card 7 device 1 present" in r.detail


def test_check_mic_capture_falls_back_to_daemon_active(monkeypatch):
    """When PortAudio refuses to open the mic AND jasper-voice is
    running, the check returns ok with a 'daemon holds device' note
    instead of a spurious fail. This is the snd-aloop / AEC bridge
    case where the daemon owns the capture handle exclusively."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )

    class FakeSD:
        def rec(self, *a, **kw):
            raise ValueError("No input device matching 'hw:7,1'")

    fake_sd = FakeSD()

    def fake_import(*args, **kwargs):
        if args and args[0] == "sounddevice":
            return fake_sd
        return __import__(*args, **kwargs)

    # Use a sd-stub by monkeypatching the import inside the function.
    # Easier: patch a wrapper. Instead, patch _jasper_voice_active and
    # mock sd.rec via injecting into sys.modules.
    import sys
    sys.modules["sounddevice"] = fake_sd
    try:
        with patch.object(doctor.audio, "_jasper_voice_active", return_value=True):
            r = doctor.check_mic_capture(cfg)
        assert r.status == "ok"
        assert "skipped" in r.detail
        assert "jasper-voice holds" in r.detail
    finally:
        del sys.modules["sounddevice"]


def test_check_mic_capture_fails_hard_when_daemon_inactive(monkeypatch):
    """If jasper-voice ISN'T running and the open still fails, the
    fail is real — the device is missing or misconfigured."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )

    class FakeSD:
        def rec(self, *a, **kw):
            raise ValueError("No input device matching 'hw:7,1'")

    import sys
    sys.modules["sounddevice"] = FakeSD()
    try:
        with patch.object(doctor.audio, "_jasper_voice_active", return_value=False):
            r = doctor.check_mic_capture(cfg)
        assert r.status == "fail"
    finally:
        del sys.modules["sounddevice"]


def test_check_mic_card_shorthand_failure_actionable(monkeypatch):
    """When the shorthand points at a card/device that's missing, the
    failure detail must mention the AEC bridge — that's the most
    common cause (bridge disabled but JASPER_MIC_DEVICE still set)."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaSyTest",
        JASPER_MIC_DEVICE="hw:7,1",
    )
    fake_output = "card 0: dongle [USB Audio], device 0: USB Audio\n"
    with patch.object(
        doctor.audio, "_run",
        return_value=type("FakeProc", (), {"stdout": fake_output, "returncode": 0})(),
    ), patch.object(doctor.shutil, "which", return_value="/usr/bin/arecord"):
        r = doctor.check_mic_card_matches_config(cfg)
    assert r.status == "fail"
    assert "AEC bridge" in r.detail


# --------------------------------------------- AEC bridge output assessment


def _rms_log_line(ref: int, mic: int, aec: int, attn_db: float) -> str:
    """Synthesize one bridge `rms over` log line in the journal `--output=cat`
    format the parser sees. Helper for the _assess_aec_bridge_output tests
    below."""
    return (
        f"2026-05-16 17:00:00,000 aec-bridge INFO "
        f"rms over 5.0s: ref={ref} mic={mic} aec={aec} → "
        f"attenuation={attn_db:.1f} dB (frames=1 ref_q=0 mic_q=0 "
        f"ref_clip=0.00% out_clip=0.00%)"
    )


def test_assess_aec_output_empty_journal_is_ok():
    """No rms lines = bridge probably just restarted in the assessment
    window. Not a failure, just nothing to evaluate."""
    r = doctor._assess_aec_bridge_output("")
    assert r.status == "ok"
    assert "no recent rms windows" in r.detail.lower()


def test_assess_aec_output_idle_returns_ok():
    """Mic and ref both quiet — speaker has been idle, no music has
    played. Doctor must NOT flag this as a degradation."""
    lines = [_rms_log_line(ref=0, mic=200, aec=30, attn_db=-16.5) for _ in range(10)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "no music activity" in r.detail.lower()


def test_assess_aec_output_silent_ref_with_no_healthy_window_fails():
    """The PR #75 dsnoop rate-lock signature: mic shows music acoustically
    throughout, ref delivers silence throughout, ZERO windows prove the
    ref chain ever worked in this period. The check MUST fail — this is
    the regression we exist to catch."""
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "fail"
    assert "reference path is delivering silence" in r.detail
    assert "Lessons learned" in r.detail  # actionable doc link


def test_assess_aec_output_silent_ref_downgrades_when_loopback_closed():
    """Same mic-loud + ref-silent shape as the rate-lock fail, but the
    music chain isn't active (no renderer writing the loopback). In
    that case ref MUST be silent — snd-aloop produces zeros without a
    producer — and the mic-loud bursts are TTS or voice (both bypass
    the loopback). Downgrade to OK with the diagnosis so a pure-voice
    session doesn't show as a degraded AEC bridge."""
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(8)]
    r = doctor._assess_aec_bridge_output(
        "\n".join(lines), music_chain_active=False,
    )
    assert r.status == "ok"
    assert "loopback playback is closed" in r.detail
    assert "jasper_out bypasses the loopback" in r.detail
    # Counterpart: when music chain IS active, same input still fails —
    # the guard only relaxes the FAIL when we have positive evidence
    # the loopback is idle, not on uncertainty.
    r_active = doctor._assess_aec_bridge_output(
        "\n".join(lines), music_chain_active=True,
    )
    assert r_active.status == "fail"


def test_assess_aec_output_silent_ref_with_healthy_window_is_ok():
    """The 2026-05-16 false-positive: TTS / wake cues / loud ambient
    push silent_ref over threshold, but at least one window in the
    assessment period has ref signal (proving the chain works). The
    check must NOT fail — silent-ref windows have benign explanations
    when the ref path is demonstrably alive."""
    lines = [
        # 5 mic-loud + ref-silent windows from non-reference loud sound.
        _rms_log_line(ref=0, mic=2200, aec=2100, attn_db=-0.4),
        _rms_log_line(ref=0, mic=2400, aec=2300, attn_db=-0.4),
        _rms_log_line(ref=0, mic=2600, aec=2500, attn_db=-0.3),
        _rms_log_line(ref=0, mic=2100, aec=2050, attn_db=-0.2),
        _rms_log_line(ref=0, mic=2300, aec=2250, attn_db=-0.2),
        # 2 windows where music played and ref captured it correctly
        _rms_log_line(ref=800, mic=2400, aec=200, attn_db=-21.6),
        _rms_log_line(ref=1100, mic=2800, aec=180, attn_db=-23.8),
    ]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "likely TTS or ambient" in r.detail
    assert "ref path proven healthy" in r.detail


def test_assess_aec_output_healthy_aec_work_is_ok():
    """Music playing through the loopback, ref strong, attenuation
    meaningful — the bridge is doing its job. ok with a summary."""
    lines = [_rms_log_line(ref=1200, mic=2400, aec=150, attn_db=-24.1) for _ in range(8)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "real AEC work" in r.detail


def test_assess_aec_output_drift_warnings_warn():
    """High count of `drained N stale ref frames (drift)` warnings
    indicates ref/mic clock skew or rate mismatch. Warn, don't fail."""
    drift_line = (
        "2026-05-16 17:00:00,000 aec-bridge WARNING "
        "drained 7 stale ref frames (drift)"
    )
    # Threshold is 30 in 5 min; 40 is comfortably over.
    journal = "\n".join([drift_line] * 40)
    r = doctor._assess_aec_bridge_output(journal)
    assert r.status == "warn"
    assert "ref-drift warnings" in r.detail


def test_assess_aec_output_single_healthy_window_suffices():
    """Boundary: exactly one healthy_ref window flips the silent-ref
    pattern from fail to ok. Documents the design choice — if the ref
    chain proved itself once in the window, we trust it."""
    lines = [_rms_log_line(ref=0, mic=2500, aec=2400, attn_db=-0.4) for _ in range(7)]
    lines.append(_rms_log_line(ref=300, mic=400, aec=80, attn_db=-14.0))
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"


def test_assess_aec_output_silent_ref_below_alarm_surfaces_in_summary():
    """When silent_ref_count is 1-4 (non-zero but below the fail
    threshold of 5), the OK summary appends a `silent-ref=N` note so
    intermittent ref glitches are visible before they tip into a real
    outage. Per PR #124 upstream — preserved in the refactor."""
    lines = [_rms_log_line(ref=1200, mic=2400, aec=150, attn_db=-24.1) for _ in range(6)]
    # 3 mic-loud + ref-silent windows: above 0 but below the 5-count alarm.
    lines += [_rms_log_line(ref=0, mic=2200, aec=2100, attn_db=-0.4) for _ in range(3)]
    r = doctor._assess_aec_bridge_output("\n".join(lines))
    assert r.status == "ok"
    assert "silent-ref=3" in r.detail
    assert "below alarm" in r.detail


def test_loopback_playback_active_reads_proc_status(tmp_path):
    """Helper must report True for any non-closed subdev and False when
    every subdev is closed. Verifies the first-line strip-and-compare
    against the actual /proc/asound status file format (single word
    `closed` vs `state: RUNNING\\n…`)."""
    fake_root = tmp_path / "asound" / "Loopback" / "pcm0p"
    fake_root.mkdir(parents=True)
    sub_paths = []
    for sub in range(4):
        d = fake_root / f"sub{sub}"
        d.mkdir()
        status = d / "status"
        status.write_text("closed\n")
        sub_paths.append(str(status))

    with patch("glob.glob", return_value=sub_paths):
        # All closed → inactive.
        assert doctor._loopback_playback_active() is False
        # Flip sub2 to RUNNING → active.
        (fake_root / "sub2" / "status").write_text(
            "state: RUNNING\nowner_pid   : 12345\n"
        )
        assert doctor._loopback_playback_active() is True

    # No status files at all (e.g., snd-aloop not loaded) → inactive,
    # never raises.
    with patch("glob.glob", return_value=[]):
        assert doctor._loopback_playback_active() is False


# ----------------------------------------- DTLN-aec engine health assessment


def _dtln_loaded_line(size: int = 256) -> str:
    """Synthesize the bridge's successful-load log line in journal
    `--output=cat` format. Matches jasper/cli/aec_bridge.py:~675."""
    return (
        f"2026-05-23 12:47:29,197 aec-bridge INFO "
        f"DTLN-aec engine enabled: size={size}, udp out=127.0.0.1:9878"
    )


def _dtln_failed_line(reason: str = "No such file or directory") -> str:
    """Synthesize the bridge's failed-load log line."""
    return (
        f"2026-05-23 12:47:29,197 aec-bridge WARNING "
        f"JASPER_AEC_DTLN_ENABLED set but DTLN couldn't load: {reason}. "
        f"Continuing with AEC3 only."
    )


def test_assess_dtln_engine_loaded_returns_ok():
    """Happy path: bridge logged a successful engine-init line.
    Doctor reports the engine size for the operator to confirm."""
    r = doctor._assess_dtln_engine(_dtln_loaded_line(size=256))
    assert r.status == "ok"
    assert "loaded" in r.detail.lower()
    assert "size=256" in r.detail


def test_assess_dtln_engine_load_failed_returns_fail():
    """The regression we exist to catch: JASPER_AEC_DTLN_ENABLED=1
    but the engine couldn't load (e.g. /var/lib/jasper/dtln/*.onnx
    missing because install.sh's download failed and the manual SCP
    step didn't happen). Without this check, the operator would
    spend a week analyzing 'DTLN never fires' data without realizing
    the engine never ran."""
    r = doctor._assess_dtln_engine(_dtln_failed_line(
        reason="DTLN ONNX models missing in /var/lib/jasper/dtln"
    ))
    assert r.status == "fail"
    assert "couldn't load" in r.detail
    assert "/var/lib/jasper/dtln" in r.detail   # actionable path
    assert "jasper-aec-bridge" in r.detail       # actionable next step


def test_assess_dtln_engine_no_marker_warns():
    """Bridge running but no engine-init marker in the journal
    window — probably means the bridge hasn't restarted since the
    env var was set. Warn with the actionable fix command."""
    r = doctor._assess_dtln_engine("some unrelated log lines\nbridge boot\n")
    assert r.status == "warn"
    assert "systemctl restart jasper-aec-bridge" in r.detail


def test_assess_dtln_engine_picks_most_recent_marker():
    """If the journal window straddles a bridge restart that fixed
    an earlier failure, the LATER successful-load line wins. Reverse
    iteration in _assess_dtln_engine ensures we evaluate newest-first."""
    journal = "\n".join([
        _dtln_failed_line(reason="onnxruntime import failed"),
        "(... operator fixed the venv ...)",
        _dtln_loaded_line(size=256),
    ])
    r = doctor._assess_dtln_engine(journal)
    assert r.status == "ok"


def test_check_dtln_skips_when_env_disabled(monkeypatch):
    """When JASPER_AEC_DTLN_ENABLED is unset (legacy dual-stream
    config), the whole check should skip cleanly without running
    journalctl. This is the common case for non-triple-stream
    installs and must not flap."""
    monkeypatch.delenv("JASPER_AEC_DTLN_ENABLED", raising=False)
    r = doctor.check_aec_bridge_dtln_engine()
    assert r.status == "ok"
    assert "skipped" in r.detail.lower()


def _install_fake_dtln_registry(monkeypatch, tmp_path: Path):
    from jasper.aec_engines import dtln_models

    expected = hashlib.sha256(b"model").hexdigest()

    class _FakeEntry:
        def __init__(self, size: int):
            self.size = size

        def files(self, base_dir=tmp_path):
            base = Path(base_dir)
            return [
                (
                    base / f"dtln_aec_{self.size}_1.onnx",
                    "https://example.invalid/1",
                    expected,
                ),
                (
                    base / f"dtln_aec_{self.size}_2.onnx",
                    "https://example.invalid/2",
                    expected,
                ),
            ]

    entries = {
        128: _FakeEntry(128),
        256: _FakeEntry(256),
    }
    monkeypatch.setattr(dtln_models, "DEFAULT_SIZE", 256)
    monkeypatch.setattr(dtln_models, "REGISTRY", tuple(entries.values()))
    monkeypatch.setattr(dtln_models, "by_size", lambda size: entries.get(size))
    monkeypatch.setenv("JASPER_DTLN_MODEL_DIR", str(tmp_path))


def test_check_dtln_fails_when_enabled_model_file_missing(monkeypatch, tmp_path: Path):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    (tmp_path / "dtln_aec_256_1.onnx").write_bytes(b"model")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "model files are missing" in r.detail
    assert "dtln_aec_256_2.onnx" in r.detail
    assert "deploy/install.sh" in r.detail


def test_check_dtln_fails_when_enabled_model_hash_mismatches(
    monkeypatch,
    tmp_path: Path,
):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    (tmp_path / "dtln_aec_256_1.onnx").write_bytes(b"model")
    (tmp_path / "dtln_aec_256_2.onnx").write_bytes(b"wrong-model")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "hashes do not match" in r.detail
    assert "dtln_aec_256_2.onnx" in r.detail
    assert "deploy/install.sh" in r.detail


def test_check_dtln_uses_configured_model_size(monkeypatch, tmp_path: Path):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_DTLN_SIZE", "128")
    monkeypatch.setattr(
        doctor.aec,
        "_run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="inactive"),
    )
    (tmp_path / "dtln_aec_128_1.onnx").write_bytes(b"model")
    (tmp_path / "dtln_aec_128_2.onnx").write_bytes(b"model")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "ok"
    assert "bridge not running" in r.detail


def test_check_dtln_fails_when_configured_model_size_is_invalid(
    monkeypatch,
    tmp_path: Path,
):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_DTLN_SIZE", "large")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "JASPER_AEC_DTLN_SIZE" in r.detail
    assert "not an integer" in r.detail


def test_check_dtln_fails_when_configured_model_size_is_not_registered(
    monkeypatch,
    tmp_path: Path,
):
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    monkeypatch.setenv("JASPER_AEC_DTLN_SIZE", "512")

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "JASPER_AEC_DTLN_SIZE=512" in r.detail
    assert "not registered" in r.detail
    assert "128" in r.detail
    assert "256" in r.detail


# ---------------------------------------------------- peering doctor checks


def test_check_peering_mode_no_file_returns_ok_default(monkeypatch, tmp_path):
    """When /var/lib/jasper/peering.env doesn't exist, peering is off
    by design — the default. Doctor should return ok with a hint."""
    fake = tmp_path / "peering.env"  # does not exist
    with patch("jasper.cli.doctor.peering.Path", side_effect=lambda p: fake if "peering.env" in p else Path(p)):
        r = doctor.check_peering_mode()
    assert r.status == "ok"
    assert "off" in r.detail.lower()


def test_check_peering_mode_off_explicit(tmp_path, monkeypatch):
    """Explicit JASPER_PEERING=off — same ok status, slightly different
    message (operator made the choice deliberately)."""
    env = tmp_path / "peering.env"
    env.write_text("JASPER_PEERING=off\n")
    monkeypatch.setattr("jasper.cli.doctor.peering.Path", lambda p: env if "peering.env" in p else Path(p))
    r = doctor.check_peering_mode()
    assert r.status == "ok"
    assert "off" in r.detail.lower()


def test_check_peering_mode_on(tmp_path, monkeypatch):
    env = tmp_path / "peering.env"
    env.write_text("JASPER_PEERING=on\nJASPER_PEER_ROOM=kitchen\n")
    monkeypatch.setattr("jasper.cli.doctor.peering.Path", lambda p: env if "peering.env" in p else Path(p))
    r = doctor.check_peering_mode()
    assert r.status == "ok"
    assert "on" in r.detail.lower()


def test_check_peering_mode_garbage_warns(tmp_path, monkeypatch):
    """A malformed value warns the user — silent failure here would let
    a typo (JASPER_PEERING=onn) leave the user thinking peering is on
    when it actually resolved to off."""
    env = tmp_path / "peering.env"
    env.write_text("JASPER_PEERING=banana\n")
    monkeypatch.setattr("jasper.cli.doctor.peering.Path", lambda p: env if "peering.env" in p else Path(p))
    r = doctor.check_peering_mode()
    assert r.status == "warn"
    assert "banana" in r.detail


def test_check_peering_discovery_no_peers(monkeypatch):
    """avahi-browse returns no peers — single-device mode (ok)."""
    fake_output = "+ eth0 IPv4 SomeOtherService _foo._tcp local\n"
    monkeypatch.setattr("jasper.cli.doctor.shutil.which", lambda p: "/usr/bin/avahi-browse")
    monkeypatch.setattr(
        "jasper.cli.doctor.peering._run",
        lambda *a, **kw: type("P", (), {"returncode": 0, "stdout": fake_output})(),
    )
    r = doctor.check_peering_discovery()
    assert r.status == "ok"
    assert "0 sibling" in r.detail


def test_check_peering_discovery_sees_siblings(monkeypatch, tmp_path):
    """avahi-browse returns two siblings — count them, exclude self."""
    fake_output = (
        '+ eth0 IPv4 JTSpeer_alice _jasper-peer._udp local\n'
        '= eth0 IPv4 JTSpeer_alice _jasper-peer._udp local\n'
        '  hostname = [alice.local]\n'
        '  txt = ["peer_id=alice-uuid" "room=kitchen" "primary=1" "proto=1"]\n'
        '+ eth0 IPv4 JTSpeer_bob _jasper-peer._udp local\n'
        '= eth0 IPv4 JTSpeer_bob _jasper-peer._udp local\n'
        '  hostname = [bob.local]\n'
        '  txt = ["peer_id=bob-uuid" "room=bedroom" "primary=0" "proto=1"]\n'
    )
    monkeypatch.setattr("jasper.cli.doctor.shutil.which", lambda p: "/usr/bin/avahi-browse")
    monkeypatch.setattr(
        "jasper.cli.doctor.peering._run",
        lambda *a, **kw: type("P", (), {"returncode": 0, "stdout": fake_output})(),
    )
    # Pretend we're alice — filter ourselves out.
    monkeypatch.setattr("jasper.cli.doctor.peering._local_peer_id", lambda: "alice-uuid")
    r = doctor.check_peering_discovery()
    assert r.status == "ok"
    assert "1 sibling" in r.detail
    assert "bob-uuid" in r.detail


def test_check_peering_discovery_no_avahi_browse_warns(monkeypatch):
    """Without avahi-browse we can't verify discovery — warn but
    don't fail (it's an optional dep)."""
    monkeypatch.setattr("jasper.cli.doctor.shutil.which", lambda p: None)
    r = doctor.check_peering_discovery()
    assert r.status == "warn"


# -------------------------------------------------- check_citibike


def _citibike_cfg(monkeypatch, *, stations: str = "", ebike_only: str = "") -> Config:
    """Fresh Config with only the citibike + voice-provider env vars set.

    Drops every JASPER_CITIBIKE_* from the calling shell so the test
    picks up only the values we pass, then sets a minimal voice
    provider config so `Config.from_env()` doesn't trip the
    JASPER_VOICE_PROVIDER-not-set RuntimeError."""
    for var in (
        "JASPER_CITIBIKE_STATIONS", "JASPER_CITIBIKE_EBIKE_ONLY",
        "GEMINI_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
        "JASPER_VOICE_PROVIDER",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-stub")
    if stations:
        monkeypatch.setenv("JASPER_CITIBIKE_STATIONS", stations)
    if ebike_only:
        monkeypatch.setenv("JASPER_CITIBIKE_EBIKE_ONLY", ebike_only)
    return Config.from_env()


def test_check_citibike_skips_when_not_configured(monkeypatch):
    cfg = _citibike_cfg(monkeypatch)  # no stations saved
    r = doctor.check_citibike(cfg)
    assert r.status == "ok"
    assert "not configured" in r.detail


def test_check_citibike_ok_when_all_saved_ids_resolve(monkeypatch):
    """Saved stations all present in GBFS → ok with the count."""
    import jasper.citibike as citibike_mod

    info = {"data": {"stations": [
        {"station_id": "abc"}, {"station_id": "def"},
    ]}}
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: info)
    cfg = _citibike_cfg(
        monkeypatch, stations="abc|9 Av,def|Atlantic",
    )
    r = doctor.check_citibike(cfg)
    assert r.status == "ok"
    assert "2 saved station" in r.detail
    assert "e-bike-only mode" not in r.detail


def test_check_citibike_ok_renders_ebike_only_suffix(monkeypatch):
    import jasper.citibike as citibike_mod
    info = {"data": {"stations": [{"station_id": "abc"}]}}
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: info)
    cfg = _citibike_cfg(
        monkeypatch, stations="abc|9 Av", ebike_only="1",
    )
    r = doctor.check_citibike(cfg)
    assert r.status == "ok"
    assert "e-bike-only mode" in r.detail


def test_check_citibike_warns_when_some_saved_ids_missing(monkeypatch):
    """One saved station retired by Lyft → warn naming the affected
    station, but don't fail (the OK ones still work)."""
    import jasper.citibike as citibike_mod
    info = {"data": {"stations": [{"station_id": "abc"}]}}  # def is gone
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: info)
    cfg = _citibike_cfg(
        monkeypatch, stations="abc|9 Av,def|Gone Station",
    )
    r = doctor.check_citibike(cfg)
    assert r.status == "warn"
    assert "Gone Station" in r.detail
    assert "1/2" in r.detail


def test_check_citibike_fails_when_gbfs_unreachable(monkeypatch):
    import jasper.citibike as citibike_mod

    def _raise(url, ttl, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(citibike_mod, "fetch_feed", _raise)
    cfg = _citibike_cfg(monkeypatch, stations="abc|9 Av")
    r = doctor.check_citibike(cfg)
    assert r.status == "fail"
    assert "GBFS unreachable" in r.detail


def test_check_citibike_caps_missing_list_at_three_with_suffix(monkeypatch):
    """When > 3 stations are missing, the detail names the first 3 and
    appends a '+N more' suffix so the line stays scannable."""
    import jasper.citibike as citibike_mod
    info = {"data": {"stations": []}}  # everything retired
    monkeypatch.setattr(citibike_mod, "fetch_feed", lambda url, ttl, **kw: info)
    cfg = _citibike_cfg(
        monkeypatch,
        stations="a|A,b|B,c|C,d|D,e|E",
    )
    r = doctor.check_citibike(cfg)
    assert r.status == "warn"
    assert "+2 more" in r.detail


# ---- shairport-sync.conf output_device check ---------------------------

def _patch_asound_conf(
    monkeypatch,
    conf_text: str,
    tmp_path: Path,
    *,
    stale_topology_env: bool = False,
):
    target = tmp_path / "asound.conf"
    target.write_text(conf_text)
    stale = tmp_path / "audio_topology.env"
    if stale_topology_env:
        stale.write_text("JASPER_AUDIO_TOPOLOGY=dmix\n")
    real_path_cls = doctor.Path

    def fake_path(arg):
        if arg == "/etc/asound.conf":
            return target
        if arg == "/var/lib/jasper/audio_topology.env":
            return stale
        return real_path_cls(arg)

    monkeypatch.setattr(doctor.audio, "Path", fake_path)


_FANIN_ASOUND = """
pcm.librespot_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,0"
        rate 48000
        channels 2
        format S16_LE
    }
}
pcm.shairport_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,1"
        rate 48000
        channels 2
        format S16_LE
    }
}
pcm.bluealsa_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,2"
        rate 48000
        channels 2
        format S16_LE
    }
}
pcm.usbsink_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,3"
        rate 48000
        channels 2
        format S16_LE
    }
}
pcm.correction_substream {
    type plug
    slave {
        pcm "hw:Loopback,0,4"
        rate 48000
        channels 2
        format S16_LE
    }
}
pcm.jasper_capture {
    type dsnoop
    slave {
        pcm "hw:Loopback,1,7"
        rate 48000
        channels 2
        format S16_LE
    }
}
pcm.jasper_ref {
    type plug
    slave.pcm "jasper_capture"
}
"""


def test_fanin_asound_wiring_ok(monkeypatch, tmp_path):
    _patch_asound_conf(monkeypatch, _FANIN_ASOUND, tmp_path)
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "ok"
    assert "substream 7" in r.detail


def test_fanin_asound_wiring_fails_on_legacy_capture(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND.replace('pcm "hw:Loopback,1,7"', 'pcm "hw:Loopback,1,0"'),
        tmp_path,
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert "substream 0" in r.detail
    assert "EBUSY" in r.detail


def test_fanin_asound_wiring_fails_without_jasper_ref(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND.replace(
            'pcm.jasper_ref {\n    type plug\n    slave.pcm "jasper_capture"\n}\n',
            "",
        ),
        tmp_path,
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert "pcm.jasper_ref missing" in r.detail


def test_fanin_asound_wiring_fails_when_capture_shape_unpinned(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND.replace(
            '        pcm "hw:Loopback,1,7"\n        rate 48000\n',
            '        pcm "hw:Loopback,1,7"\n',
        ),
        tmp_path,
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert "48 kHz stereo S16_LE" in r.detail


class _FakeSocket:
    def __init__(self, payload: bytes = b"", error: OSError | None = None):
        self._chunks = [payload, b""]
        self._error = error

    def settimeout(self, timeout):
        pass

    def connect(self, path):
        if self._error is not None:
            raise self._error

    def sendall(self, data):
        pass

    def recv(self, size):
        return self._chunks.pop(0)

    def close(self):
        pass


def _patch_fanin_systemctl(monkeypatch, *, enabled="enabled", active="active"):
    def fake_run(cmd, *args, **kwargs):
        stdout = ""
        if cmd[:2] == ["systemctl", "is-enabled"]:
            stdout = enabled + "\n"
        elif cmd[:2] == ["systemctl", "is-active"]:
            stdout = active + "\n"
        return type("P", (), {"stdout": stdout, "stderr": "", "returncode": 0})()

    monkeypatch.setattr(doctor.audio, "_run", fake_run)


def _fanin_status_payload(
    *,
    input_buffer_frames: int = 4096,
    output_buffer_frames: int = 3072,
    progress_age_ms: int = 2,
) -> bytes:
    return json.dumps({
        "input_buffer_frames": input_buffer_frames,
        "output": {
            "pcm": doctor._FANIN_EXPECTED_OUTPUT_PCM,
            "buffer_frames": output_buffer_frames,
            "frames_written": 1234,
            "xrun_count": 0,
        },
        "inputs": [
            {"label": label, "pcm": pcm, "xrun_count": 0}
            for label, pcm in doctor._FANIN_EXPECTED_INPUTS
        ],
        "tts": {
            "enabled": True,
            "pending_frames": 0,
            "max_pending_frames": 96000,
            "budget_frames": 96000,
            "dropped_commands": 0,
            "dropped_audio_frames": 0,
            "flush_requests": 0,
            "flushed_frames": 0,
            "assistant_loudness": {
                "content_short_lufs": -31.2,
                "content_anchor_lufs": -30.8,
                "decision_seen": False,
                "calibrated": False,
                "profile_confidence": 0.0,
                "baseline_lufs": None,
                "target_lufs": None,
                "source_lufs": None,
                "source_peak_dbfs": None,
                "requested_gain_db": None,
                "peak_cap_gain_db": None,
                "final_gain_db": None,
            },
        },
        "watchdog": {"last_progress_age_ms": progress_age_ms},
    }).encode()


def _outputd_status_payload(
    *,
    backend: str = "alsa",
    sink_mode: str = "single_alsa",
    content_pcm: str = doctor._OUTPUTD_EXPECTED_CONTENT_PCM,
    dac_pcm: str = doctor._OUTPUTD_EXPECTED_DAC_PCM,
    content_buffer_frames: int = 4096,
    dac_buffer_frames: int = 3072,
    period_frames: int = 1024,
    progress_age_ms: int = 2,
    dual_apple_status: dict | None = None,
) -> bytes:
    payload = {
        "backend": backend,
        "sink_mode": sink_mode,
        "content": {
            "pcm": content_pcm,
            "period_frames": period_frames,
            "buffer_frames": content_buffer_frames,
            "frames_read": 1234,
            "empty_periods": 2,
            "partial_periods": 1,
            "eagain_count": 1,
            "xrun_count": 0,
        },
        "dac": {
            "pcm": dac_pcm,
            "sample_rate": 48000,
            "period_frames": period_frames,
            "buffer_frames": dac_buffer_frames,
            "frames_written": 2048,
            "xrun_count": 0,
        },
        "mix": {"reference_sequence": 1, "clipped_samples": 0},
        "reference_outputs": {
            "speaker_reference_source": "outputd_final_electrical",
            "speaker_reference_is_fallback": False,
            "speaker_reference_active": False,
            "speaker_reference_sample_rate": 48000,
            "speaker_reference_channels": 2,
            "chip_ref_pcm": None,
            "chip_ref_sample_rate": 16000,
            "chip_ref_period_frames": 320,
            "chip_ref_buffer_frames": 1280,
            "udp_target": None,
        },
        "content_bridge": {
            "mode": "direct",
            "enabled": False,
            "locked": False,
            "ring_frames": 16384,
            "target_fill_frames": 4096,
            "fill_frames": 0,
            "min_fill_frames": 0,
            "max_fill_frames": 0,
            "ratio_ppm": 0.0,
            "input_frames": 0,
            "output_frames": 0,
            "silence_frames": 0,
            "underrun_frames": 0,
            "overrun_frames": 0,
            "resync_count": 0,
            "reset_count": 0,
            "ratio_clamp_count": 0,
            "lock_count": 0,
            "unlock_count": 0,
        },
        "tts": {
            "pending_frames": 0,
            "budget_frames": 96000,
            "max_pending_frames": 4096,
            "over_budget": False,
            "over_budget_periods": 0,
            "over_budget_ms": 0,
            "over_budget_streak_ms": 0,
            "dropped_commands": 0,
            "dropped_audio_frames": 0,
        },
        "assistant_loudness": {
            "content_short_lufs": -31.2,
            "content_anchor_lufs": -30.8,
            "decision_seen": False,
            "calibrated": False,
            "profile_confidence": 0.0,
            "baseline_lufs": None,
            "target_lufs": None,
            "source_lufs": None,
            "source_peak_dbfs": None,
            "requested_gain_db": None,
            "peak_cap_gain_db": None,
            "final_gain_db": None,
        },
        "watchdog": {"last_progress_age_ms": progress_age_ms},
    }
    if sink_mode == "dual_apple":
        payload["dual_apple"] = dual_apple_status or {
            "dac_a_pcm": "hw:CARD=A,DEV=0",
            "dac_b_pcm": "hw:CARD=A_1,DEV=0",
            "linked": True,
            "delay_delta_frames": 0,
            "delay_delta_baseline_frames": 0,
            "delay_delta_error_frames": 0,
            "max_delay_delta_frames": 2,
        }
    return json.dumps(payload).encode()


def _patch_fanin_status_socket(monkeypatch, payload: bytes):
    monkeypatch.setattr(
        doctor.socket,
        "socket",
        lambda *a, **kw: _FakeSocket(payload=payload),
    )


def test_check_fanin_service_ok_with_expected_status(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, _fanin_status_payload())
    r = doctor.check_fanin_service()
    assert r.status == "ok"
    assert "input_buffer_frames=4096" in r.detail
    assert "output_buffer_frames=3072" in r.detail
    assert "tts_enabled=true" in r.detail
    assert "assistant_loudness_decision=False" in r.detail


def test_check_fanin_service_reports_pre_dsp_tts_loudness(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    payload = json.loads(_fanin_status_payload().decode())
    payload["tts"] = {
        "enabled": True,
        "pending_frames": 0,
        "assistant_loudness": {
            "content_short_lufs": -31.2,
            "content_anchor_lufs": -30.8,
            "decision_seen": True,
            "calibrated": True,
            "profile_confidence": 1.0,
            "baseline_lufs": -38.0,
            "target_lufs": -36.5,
            "source_lufs": -25.0,
            "source_peak_dbfs": -8.0,
            "requested_gain_db": -11.5,
            "peak_cap_gain_db": 5.0,
            "final_gain_db": -11.5,
        },
    }
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_fanin_service()

    assert r.status == "ok"
    assert "tts_enabled=true" in r.detail
    assert "assistant_loudness_decision=True" in r.detail
    assert "assistant_final_gain_db=-11.5" in r.detail


def test_check_fanin_service_warns_on_malformed_pre_dsp_tts_loudness(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    payload = json.loads(_fanin_status_payload().decode())
    payload["tts"] = {
        "enabled": True,
        "pending_frames": 0,
        "assistant_loudness": {
            "decision_seen": True,
            "calibrated": False,
            "final_gain_db": None,
        },
    }
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_fanin_service()

    assert r.status == "warn"
    assert "decision_seen=true" in r.detail


def test_check_fanin_service_fails_on_invalid_status_json(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, b"not-json")
    r = doctor.check_fanin_service()
    assert r.status == "fail"
    assert "invalid JSON" in r.detail


def test_check_fanin_service_fails_when_status_socket_unreachable(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    monkeypatch.setattr(
        doctor.socket,
        "socket",
        lambda *a, **kw: _FakeSocket(error=OSError("connection refused")),
    )
    r = doctor.check_fanin_service()
    assert r.status == "fail"
    assert "UDS probe" in r.detail


def test_check_fanin_service_fails_on_small_runtime_buffers(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _fanin_status_payload(input_buffer_frames=2048),
    )
    r = doctor.check_fanin_service()
    assert r.status == "fail"
    assert "input_buffer_frames=2048" in r.detail

    _patch_fanin_status_socket(
        monkeypatch,
        _fanin_status_payload(output_buffer_frames=2048),
    )
    r = doctor.check_fanin_service()
    assert r.status == "fail"
    assert "output_buffer_frames=2048" in r.detail


def test_outputd_service_fails_when_disabled(monkeypatch):
    _patch_fanin_systemctl(monkeypatch, enabled="disabled")
    r = doctor.check_outputd_service()
    assert r.status == "fail"
    assert "expected enabled" in r.detail


def test_outputd_service_ok_with_expected_status(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, _outputd_status_payload())
    r = doctor.check_outputd_service()
    assert r.status == "ok"
    assert "backend=alsa" in r.detail
    assert "content_buffer_frames=4096" in r.detail
    assert "dac_buffer_frames=3072" in r.detail
    assert "content_empty_periods=2" in r.detail
    assert "content_eagain_count=1" in r.detail
    assert "content_bridge=direct" in r.detail
    assert "speaker_reference_source=outputd_final_electrical" in r.detail


def test_outputd_service_ok_with_single_alsa_active_lane(monkeypatch, tmp_path):
    env_path = tmp_path / "outputd.env"
    env_path.write_text("JASPER_OUTPUTD_ACTIVE_CHANNELS=2\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(env_path))
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_status_payload(
            content_pcm=doctor._OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM,
            dac_pcm=doctor._OUTPUTD_EXPECTED_DAC_PCM,
        ),
    )

    r = doctor.check_outputd_service()

    assert r.status == "ok"
    assert "active_channels=2" in r.detail


def test_outputd_service_fails_when_active_env_has_legacy_content_pcm(
    monkeypatch,
    tmp_path,
):
    env_path = tmp_path / "outputd.env"
    env_path.write_text("JASPER_OUTPUTD_ACTIVE_CHANNELS=2\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUTD_ENV_FILE", str(env_path))
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, _outputd_status_payload())

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert "outputd_active_content_capture" in r.detail
    assert "active_channels=2" in r.detail


def test_outputd_service_ok_when_loudness_is_owned_by_fanin(monkeypatch):
    payload = json.loads(_outputd_status_payload().decode())
    payload.pop("assistant_loudness", None)
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_outputd_service()

    assert r.status == "ok"
    assert "assistant_loudness=fan-in-owned" in r.detail


def test_outputd_service_fails_when_dual_apple_status_missing(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    payload = json.loads(
        _outputd_status_payload(
            sink_mode="dual_apple",
            content_pcm=doctor._OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM,
            dac_pcm=doctor._OUTPUTD_EXPECTED_DUAL_DAC_PCM,
        ).decode()
    )
    payload.pop("dual_apple", None)
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_outputd_service()
    assert r.status == "fail"
    assert "STATUS missing dual_apple runtime health" in r.detail


def test_outputd_service_warns_when_dual_apple_pcm_link_missing(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_status_payload(
            sink_mode="dual_apple",
            content_pcm=doctor._OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM,
            dac_pcm=doctor._OUTPUTD_EXPECTED_DUAL_DAC_PCM,
            dual_apple_status={
                "dac_a_pcm": "hw:CARD=A,DEV=0",
                "dac_b_pcm": "hw:CARD=A_1,DEV=0",
                "linked": False,
                "delay_delta_frames": 0,
                "delay_delta_baseline_frames": 0,
                "delay_delta_error_frames": 0,
                "max_delay_delta_frames": 2,
            },
        ),
    )
    r = doctor.check_outputd_service()
    assert r.status == "warn"
    assert "not ALSA-linked" in r.detail


def test_outputd_service_ok_with_dual_apple_status(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_status_payload(
            sink_mode="dual_apple",
            content_pcm=doctor._OUTPUTD_EXPECTED_ACTIVE_CONTENT_PCM,
            dac_pcm=doctor._OUTPUTD_EXPECTED_DUAL_DAC_PCM,
        ),
    )
    r = doctor.check_outputd_service()
    assert r.status == "ok"
    assert "backend=alsa" in r.detail
    assert "dual_a_pcm=hw:CARD=A,DEV=0" in r.detail
    assert "dual_linked=True" in r.detail


def test_outputd_service_fails_on_fake_backend(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_status_payload(backend="fake"),
    )
    r = doctor.check_outputd_service()
    assert r.status == "fail"
    assert "backend='fake'" in r.detail


def test_outputd_service_fails_on_small_runtime_buffers(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_status_payload(dac_buffer_frames=1024),
    )
    r = doctor.check_outputd_service()
    assert r.status == "fail"
    assert "dac.buffer_frames=1024" in r.detail


def test_outputd_service_fails_when_reference_contract_missing(monkeypatch):
    payload = json.loads(_outputd_status_payload().decode())
    payload["reference_outputs"] = {}
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_outputd_service()

    assert r.status == "fail"
    assert "speaker_reference_source" in r.detail


def test_outputd_service_warns_on_content_bridge_anomalies(monkeypatch):
    payload = json.loads(_outputd_status_payload().decode())
    payload["content_bridge"].update({
        "mode": "rate_match",
        "enabled": True,
        "locked": True,
        "fill_frames": 4096,
        "underrun_frames": 1024,
        "overrun_frames": 0,
        "resync_count": 1,
        "ratio_clamp_count": 0,
    })
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(monkeypatch, json.dumps(payload).encode())

    r = doctor.check_outputd_service()

    assert r.status == "warn"
    assert "rate-match content bridge reported anomalies" in r.detail
    assert "underrun_frames=1024" in r.detail
    assert "resync_count=1" in r.detail
    assert "bridge_fill_frames=4096" in r.detail



def _outputd_aec_clock_payload(
    *,
    chip_ref_pcm: str | None = "plughw:CARD=Array,DEV=0",
    aec_clock: dict | None = None,
) -> bytes:
    """An outputd STATUS payload whose reference_outputs carries a chip-ref
    and (optionally) an aec_clock block — the surface check_aec_clock_drift
    reads."""
    payload = json.loads(_outputd_status_payload().decode())
    payload["reference_outputs"]["chip_ref_pcm"] = chip_ref_pcm
    if aec_clock is not None:
        payload["reference_outputs"]["aec_clock"] = aec_clock
    return json.dumps(payload).encode()


def _aec_clock_block(
    *, verdict: str, status: str, ppm, observe: bool = False
) -> dict:
    return {
        "chip_ref_sro_ppm": ppm,
        "sro_estimator_status": status,
        "verdict": verdict,
        "verdict_reason": f"{verdict}/{status}",
        "observe": observe,
        "latency": {
            "dac_presentation_ms": 21.3,
            "playback_queue_ms": 64.0,
            "chip_ref_queue_ms": 80.0,
        },
    }


def test_aec_clock_drift_ok_when_coherent(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(
            aec_clock=_aec_clock_block(
                verdict="coherent", status="locked", ppm=1.2
            )
        ),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "verdict=coherent" in r.detail
    assert "chip_ref_sro_ppm=1.2" in r.detail
    assert "observe=False" in r.detail
    assert "playback_queue_ms=64.0" in r.detail


def test_aec_clock_drift_surfaces_observe_mode(monkeypatch):
    """Chip-ref observe mode (writer armed purely to MEASURE drift on the
    software-AEC3 path) is healthy and surfaced in the detail so an operator
    can tell why the chip-ref writer is running."""
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(
            aec_clock=_aec_clock_block(
                verdict="compensable", status="locked", ppm=42.0, observe=True
            )
        ),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "observe=True" in r.detail


def test_aec_clock_drift_ok_when_compensable(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(
            aec_clock=_aec_clock_block(
                verdict="compensable", status="locked", ppm=42.0
            )
        ),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "verdict=compensable" in r.detail
    assert "chip_ref_sro_ppm=42.0" in r.detail


def test_aec_clock_drift_warns_when_untrusted(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(
            aec_clock=_aec_clock_block(
                verdict="fallback", status="untrusted", ppm=None
            )
        ),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "warn"
    assert "cannot be trusted" in r.detail
    assert "sro_estimator_status=untrusted" in r.detail


def test_aec_clock_drift_ok_while_observing(monkeypatch):
    """The initial lock window (status=observing, which maps to a fallback
    verdict) is healthy, not a warning — warning there would cry wolf on every
    boot before the estimator has enough samples."""
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(
            aec_clock=_aec_clock_block(
                verdict="fallback", status="observing", ppm=None
            )
        ),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "sro_estimator_status=observing" in r.detail


def test_aec_clock_drift_skips_when_chip_ref_not_configured(monkeypatch):
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(chip_ref_pcm=None),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "skipped" in r.detail
    assert "chip reference not configured" in r.detail


def test_aec_clock_drift_skips_on_pre_layer0_build(monkeypatch):
    """A chip-ref is present but the outputd build has no aec_clock block."""
    _patch_fanin_systemctl(monkeypatch)
    _patch_fanin_status_socket(
        monkeypatch,
        _outputd_aec_clock_payload(aec_clock=None),
    )
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "skipped" in r.detail
    assert "predates aec_clock" in r.detail


def test_aec_clock_drift_skips_when_outputd_disabled(monkeypatch):
    _patch_fanin_systemctl(monkeypatch, enabled="disabled")
    r = doctor.check_aec_clock_drift()
    assert r.status == "ok"
    assert "skipped" in r.detail
    assert "not enabled" in r.detail


def test_audio_path_no_swap_includes_fanin_and_outputd():
    assert "jasper-fanin" in doctor._AUDIO_PATH_UNITS
    assert "jasper-outputd" in doctor._AUDIO_PATH_UNITS


def test_fanin_asound_wiring_fails_on_bare_renderer_lane(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND.replace(
            'slave {\n        pcm "hw:Loopback,0,1"\n        rate 48000\n        channels 2\n        format S16_LE\n    }',
            'slave.pcm "hw:Loopback,0,1"',
        ),
        tmp_path,
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert "shairport_substream" in r.detail


def test_fanin_asound_wiring_fails_on_legacy_renderer_dmix(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND + "\npcm.jasper_renderer_mix {\n    type dmix\n}\n",
        tmp_path,
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "fail"
    assert "legacy renderer dmix" in r.detail


def test_fanin_asound_wiring_warns_on_stale_topology_env(monkeypatch, tmp_path):
    _patch_asound_conf(
        monkeypatch,
        _FANIN_ASOUND,
        tmp_path,
        stale_topology_env=True,
    )
    r = doctor.check_fanin_asound_wiring()
    assert r.status == "warn"
    assert "stale" in r.detail


def _patch_shairport_conf(monkeypatch, conf_text: str, tmp_path: Path):
    """Have the doctor read a synthetic shairport-sync.conf instead of
    /etc/shairport-sync.conf. The function takes no args and hardcodes
    the path, so we substitute the `Path` constructor at the module
    level via a thin shim."""
    target = tmp_path / "shairport-sync.conf"
    target.write_text(conf_text)
    real_path_cls = doctor.Path

    def fake_path(arg):
        if arg == "/etc/shairport-sync.conf":
            return target
        return real_path_cls(arg)

    monkeypatch.setattr(doctor.renderers, "Path", fake_path)


def test_shairport_check_substream_is_ok(monkeypatch, tmp_path):
    """Canonical fan-in wiring: AirPlay targets its private lane."""
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "shairport_substream";\n};\n',
        tmp_path,
    )
    r = doctor.check_shairport_sync_loopback_plughw()
    assert r.status == "ok"
    assert "shairport_substream" in r.detail


def test_shairport_check_jasper_renderer_in_fails(monkeypatch, tmp_path):
    """The retired renderer-dmix device is now a hard drift signal."""
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "jasper_renderer_in";\n};\n',
        tmp_path,
    )
    r = doctor.check_shairport_sync_loopback_plughw()
    assert r.status == "fail"
    assert "retired dmix" in r.detail


def test_shairport_check_legacy_plughw_warns_with_redeploy_hint(
    monkeypatch, tmp_path,
):
    """Pre-PR-#214 wiring: output_device still points at the bare
    loopback. Doctor warns and tells the user to redeploy. This is
    the legacy-but-functional path, not a hard failure."""
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "plughw:Loopback,0,0";\n};\n',
        tmp_path,
    )
    r = doctor.check_shairport_sync_loopback_plughw()
    assert r.status == "warn"
    assert "plughw:Loopback" in r.detail
    assert "redeploy" in r.detail.lower() or "deploy-to-pi" in r.detail


def test_shairport_check_raw_hw_loopback_fails(monkeypatch, tmp_path):
    """Raw `hw:Loopback,0,0` bypasses plug entirely. shairport requests
    44.1 kHz and snd-aloop is locked at 48 kHz → silent rejection.
    This is the hard-fail case."""
    _patch_shairport_conf(
        monkeypatch,
        'alsa = {\n    output_device = "hw:Loopback,0,0";\n};\n',
        tmp_path,
    )
    r = doctor.check_shairport_sync_loopback_plughw()
    assert r.status == "fail"


def test_shairport_check_missing_output_device_warns(monkeypatch, tmp_path):
    """A conf without an output_device line at all means shairport is
    using its own default — almost certainly wrong on this host."""
    _patch_shairport_conf(
        monkeypatch, 'alsa = {\n    output_rate = 44100;\n};\n', tmp_path,
    )
    r = doctor.check_shairport_sync_loopback_plughw()
    assert r.status == "warn"
    assert "no `output_device`" in r.detail


def test_shairport_check_comments_ignored(monkeypatch, tmp_path):
    """// comments referencing plughw:Loopback (e.g. PR-history notes
    in the template) must not bait the check into reporting `ok` when
    the active line says something else."""
    conf = (
        "alsa = {\n"
        '    // Pre-2026-05-22 this was plughw:Loopback,0,0 directly\n'
        '    output_device = "shairport_substream";\n'
        "};\n"
    )
    _patch_shairport_conf(monkeypatch, conf, tmp_path)
    r = doctor.check_shairport_sync_loopback_plughw()
    assert r.status == "ok"


# ---- renderer ALSA device resolvable (PR #223 — the bug-class catch) ---

# These tests mock the parse helpers + the systemd-user lookup + the
# probe subprocess. They don't actually shell out — we're testing the
# orchestration, not aplay. The integration angle (does aplay actually
# open the device?) only meaningfully runs on the Pi via `jasper-doctor`.

def test_renderer_resolvable_all_ok(monkeypatch):
    """Happy path: every renderer has a discoverable device and the
    probe succeeds for each."""
    monkeypatch.setattr(doctor.renderers, "_renderer_device_shairport",
                        lambda: "shairport_substream")
    monkeypatch.setattr(doctor.renderers, "_renderer_device_librespot",
                        lambda: "librespot_substream")
    monkeypatch.setattr(doctor.renderers, "_renderer_device_bluealsa",
                        lambda: "bluealsa_substream")
    monkeypatch.setattr(doctor.renderers, "_systemd_user_for",
                        lambda unit: {
                            "shairport-sync.service": "shairport-sync",
                            "librespot.service": "pi",
                            "bluealsa-aplay.service": None,  # root
                        }[unit])
    monkeypatch.setattr(doctor.renderers, "_probe_open_as_user",
                        lambda dev, user: (True, ""))
    r = doctor.check_renderer_device_resolvable()
    assert r.status == "ok"
    assert "shairport-sync(shairport-sync)→shairport_substream" in r.detail
    assert "librespot(pi)→librespot_substream" in r.detail
    assert "bluealsa-aplay(root)→bluealsa_substream" in r.detail


def test_renderer_resolvable_accepts_busy_private_fanin_lane(monkeypatch):
    """An active renderer already owns its private lane, so a second
    aplay probe can return EBUSY. That is not an Unknown-PCM failure."""
    monkeypatch.setattr(doctor.renderers, "_renderer_device_shairport",
                        lambda: "shairport_substream")
    monkeypatch.setattr(doctor.renderers, "_renderer_device_librespot", lambda: None)
    monkeypatch.setattr(doctor.renderers, "_renderer_device_bluealsa", lambda: None)
    monkeypatch.setattr(doctor.renderers, "_systemd_user_for",
                        lambda unit: "shairport-sync")
    monkeypatch.setattr(doctor.renderers, "_probe_open_as_user",
                        lambda dev, user: (False, "Device or resource busy"))
    monkeypatch.setattr(doctor.renderers, "_fanin_lane_busy_owner_matches",
                        lambda dev, unit: (True, "busy/owned pid=123"))
    r = doctor.check_renderer_device_resolvable()
    assert r.status == "ok"
    assert "busy/owned" in r.detail


def test_renderer_resolvable_rejects_busy_lane_owned_by_wrong_unit(monkeypatch):
    """EBUSY is okay only when /proc shows the expected renderer owns
    the private fan-in lane."""
    monkeypatch.setattr(doctor.renderers, "_renderer_device_shairport",
                        lambda: "shairport_substream")
    monkeypatch.setattr(doctor.renderers, "_renderer_device_librespot", lambda: None)
    monkeypatch.setattr(doctor.renderers, "_renderer_device_bluealsa", lambda: None)
    monkeypatch.setattr(doctor.renderers, "_systemd_user_for",
                        lambda unit: "shairport-sync")
    monkeypatch.setattr(doctor.renderers, "_probe_open_as_user",
                        lambda dev, user: (False, "Device or resource busy"))
    monkeypatch.setattr(
        doctor.renderers,
        "_fanin_lane_busy_owner_matches",
        lambda dev, unit: (False, "busy but owner pid=999 cgroup='other.service'"),
    )
    r = doctor.check_renderer_device_resolvable()
    assert r.status == "fail"
    assert "other.service" in r.detail


def test_renderer_resolvable_catches_pr214_regression(monkeypatch):
    """The exact bug PR #223 fixes: configs look right, services look
    active, but shairport-sync's runtime user can't open the device.
    Pre-#223 the doctor missed this entirely. This test pins that the
    new check would have caught it."""
    monkeypatch.setattr(doctor.renderers, "_renderer_device_shairport",
                        lambda: "shairport_substream")
    monkeypatch.setattr(doctor.renderers, "_renderer_device_librespot",
                        lambda: "librespot_substream")
    monkeypatch.setattr(doctor.renderers, "_renderer_device_bluealsa",
                        lambda: "bluealsa_substream")
    monkeypatch.setattr(doctor.renderers, "_systemd_user_for",
                        lambda unit: {
                            "shairport-sync.service": "shairport-sync",
                            "librespot.service": "pi",
                            "bluealsa-aplay.service": None,
                        }[unit])

    # Simulate the bug: as shairport-sync user, the open fails with
    # the canonical "Unknown PCM" pattern. Root + pi (somehow) succeed
    # — only shairport-sync fails. Doctor must still fail-the-check.
    def fake_probe(dev, user):
        if user == "shairport-sync":
            return (False, 'ALSA lib pcm.c:2722: Unknown PCM shairport_substream')
        return (True, "")
    monkeypatch.setattr(doctor.renderers, "_probe_open_as_user", fake_probe)

    r = doctor.check_renderer_device_resolvable()
    assert r.status == "fail"
    assert "shairport-sync" in r.detail
    assert "Unknown PCM" in r.detail
    # The actionable hint should mention the fix path.
    assert "/etc/asound.conf" in r.detail


def test_renderer_resolvable_fail_includes_user_in_detail(monkeypatch):
    """Failure details must name the failing user — that's the key
    diagnostic for any "device works as root, fails as non-root" bug
    of which the PR #214 regression is the canonical example."""
    monkeypatch.setattr(doctor.renderers, "_renderer_device_shairport",
                        lambda: "weird-device")
    monkeypatch.setattr(doctor.renderers, "_renderer_device_librespot", lambda: None)
    monkeypatch.setattr(doctor.renderers, "_renderer_device_bluealsa", lambda: None)
    monkeypatch.setattr(doctor.renderers, "_systemd_user_for",
                        lambda unit: "shairport-sync")
    monkeypatch.setattr(doctor.renderers, "_probe_open_as_user",
                        lambda d, u: (False, "open failed"))
    r = doctor.check_renderer_device_resolvable()
    assert r.status == "fail"
    assert "(shairport-sync)" in r.detail


def test_renderer_resolvable_skips_missing_renderers(monkeypatch):
    """A stripped image without all renderers installed should
    `ok` for what works, `warn` only if nothing was probeable."""
    monkeypatch.setattr(doctor.renderers, "_renderer_device_shairport",
                        lambda: "shairport_substream")
    monkeypatch.setattr(doctor.renderers, "_renderer_device_librespot", lambda: None)
    monkeypatch.setattr(doctor.renderers, "_renderer_device_bluealsa", lambda: None)
    monkeypatch.setattr(doctor.renderers, "_systemd_user_for",
                        lambda unit: "shairport-sync")
    monkeypatch.setattr(doctor.renderers, "_probe_open_as_user",
                        lambda d, u: (True, ""))
    r = doctor.check_renderer_device_resolvable()
    assert r.status == "ok"
    assert "shairport-sync" in r.detail
    # Skipped renderers should be mentioned (informational).
    assert "skipped" in r.detail.lower()


def test_renderer_resolvable_no_renderers_at_all_is_warn(monkeypatch):
    """If literally nothing is configured, no audio path exists —
    surface as warn, not fail (could be a doctor-only image)."""
    monkeypatch.setattr(doctor.renderers, "_renderer_device_shairport", lambda: None)
    monkeypatch.setattr(doctor.renderers, "_renderer_device_librespot", lambda: None)
    monkeypatch.setattr(doctor.renderers, "_renderer_device_bluealsa", lambda: None)
    r = doctor.check_renderer_device_resolvable()
    assert r.status == "warn"


def test_renderer_resolvable_expands_systemd_env_vars(monkeypatch):
    """Operator overrides can still use `${VAR}` device indirection.
    The doctor's check must resolve those env vars via `systemctl show
    -p Environment` before probing, otherwise it false-positives with
    'Unknown PCM ${JASPER_LIBRESPOT_DEVICE}'."""
    monkeypatch.setattr(doctor.renderers, "_renderer_device_shairport",
                        lambda: "shairport_substream")  # already literal
    monkeypatch.setattr(doctor.renderers, "_renderer_device_librespot",
                        lambda: "${JASPER_LIBRESPOT_DEVICE}")
    monkeypatch.setattr(doctor.renderers, "_renderer_device_bluealsa",
                        lambda: "${JASPER_BLUEALSA_DEVICE}")
    monkeypatch.setattr(doctor.renderers, "_systemd_user_for",
                        lambda unit: {
                            "shairport-sync.service": "shairport-sync",
                            "librespot.service": "pi",
                            "bluealsa-aplay.service": None,
                        }[unit])

    # Mock _resolve_systemd_env_vars to simulate systemd returning
    # operator-supplied fan-in lane names.
    def fake_resolve(device, unit):
        env = {
            "librespot.service": {
                "JASPER_LIBRESPOT_DEVICE": "librespot_substream",
            },
            "bluealsa-aplay.service": {
                "JASPER_BLUEALSA_DEVICE": "bluealsa_substream",
            },
        }.get(unit, {})
        import re
        return re.sub(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda m: env.get(m.group(1), m.group(0)),
            device,
        )
    monkeypatch.setattr(doctor.renderers, "_resolve_systemd_env_vars", fake_resolve)

    # Probe sees the RESOLVED device — record what it gets called with.
    received: list[str] = []

    def fake_probe(device, user):
        received.append(device)
        return (True, "")
    monkeypatch.setattr(doctor.renderers, "_probe_open_as_user", fake_probe)

    r = doctor.check_renderer_device_resolvable()
    assert r.status == "ok"
    # Probe must have been called with the RESOLVED value, not the
    # literal ${VAR} string.
    assert "librespot_substream" in received
    assert "bluealsa_substream" in received
    assert "${JASPER_LIBRESPOT_DEVICE}" not in received
    assert "${JASPER_BLUEALSA_DEVICE}" not in received
    # Detail should show both literal and resolved when they differ,
    # so the operator can see env-var resolution at a glance.
    assert "from ${JASPER_LIBRESPOT_DEVICE}" in r.detail
    assert "from ${JASPER_BLUEALSA_DEVICE}" in r.detail
    # And the shairport literal (no `${`) is shown unchanged.
    assert "(shairport-sync)→shairport_substream" in r.detail
    assert "(from " not in r.detail.split("shairport-sync(")[1].split(";")[0]


def test_resolve_systemd_env_vars_no_op_when_no_placeholder():
    """Strings without ${VAR} pass through unchanged — avoids the
    subprocess call entirely."""
    assert doctor._resolve_systemd_env_vars(
        "librespot_substream", "librespot.service"
    ) == "librespot_substream"
    assert doctor._resolve_systemd_env_vars(
        "hw:Loopback,0,0", "any.service"
    ) == "hw:Loopback,0,0"


def test_resolve_systemd_env_vars_returns_original_on_failure(monkeypatch):
    """If systemctl is unavailable / errors, return the original
    string unchanged. The caller's aplay probe will then fail with
    a clear 'Unknown PCM ${VAR}' message — explicit failure beats
    silent wrong-value substitution."""
    import subprocess as sp

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("systemctl missing")
    monkeypatch.setattr(sp, "run", fake_run)
    # The function should swallow the error and return the input.
    assert doctor._resolve_systemd_env_vars(
        "${JASPER_LIBRESPOT_DEVICE}", "librespot.service"
    ) == "${JASPER_LIBRESPOT_DEVICE}"


# ---- renderer device parsers ----------------------------------------

def test_parse_shairport_device_from_conf(tmp_path, monkeypatch):
    """shairport-sync.conf uses libconfig syntax. Parser must handle
    double quotes, leading whitespace, and ignore // comments."""
    conf = tmp_path / "shairport-sync.conf"
    conf.write_text(
        "alsa = {\n"
        '    // Pre-2026-05-23 this was plughw:Loopback,0,0\n'
        '    output_device = "shairport_substream";\n'
        "};\n"
    )
    real_path_cls = doctor.Path

    def fake_path(arg):
        if arg == "/etc/shairport-sync.conf":
            return conf
        return real_path_cls(arg)

    monkeypatch.setattr(doctor.renderers, "Path", fake_path)
    assert doctor._renderer_device_shairport() == "shairport_substream"


def test_parse_librespot_device_from_systemd_unit(tmp_path, monkeypatch):
    """librespot.service has a multi-line ExecStart= with backslash
    continuations. Parser must handle line joining and grab --device."""
    unit = tmp_path / "librespot.service"
    unit.write_text(
        "[Service]\n"
        "ExecStart=/usr/bin/librespot \\\n"
        "    --name JTS \\\n"
        "    --backend alsa \\\n"
        "    --device librespot_substream \\\n"
        "    --format S24_3\n"
    )
    real_path_cls = doctor.Path

    def fake_path(arg):
        if arg == "/etc/systemd/system/librespot.service":
            return unit
        return real_path_cls(arg)

    monkeypatch.setattr(doctor.renderers, "Path", fake_path)
    assert doctor._renderer_device_librespot() == "librespot_substream"


def test_parse_bluealsa_device_from_dropin(tmp_path, monkeypatch):
    """bluealsa-aplay's device is configured via a drop-in's --pcm= flag."""
    dropin_dir = tmp_path / "bluealsa-aplay.service.d"
    dropin_dir.mkdir()
    dropin = dropin_dir / "jts-output.conf"
    dropin.write_text(
        "[Service]\n"
        "ExecStart=\n"
        "ExecStart=/usr/bin/bluealsa-aplay -S --pcm=bluealsa_substream\n"
    )
    real_path_cls = doctor.Path

    def fake_path(arg):
        if arg == "/etc/systemd/system/bluealsa-aplay.service.d/jts-output.conf":
            return dropin
        # The other candidate (override.conf) should not exist for this test.
        if arg == "/etc/systemd/system/bluealsa-aplay.service.d/override.conf":
            return tmp_path / "does-not-exist"
        return real_path_cls(arg)

    monkeypatch.setattr(doctor.renderers, "Path", fake_path)
    assert doctor._renderer_device_bluealsa() == "bluealsa_substream"


# ---------------------------------------------------- check_wifi_regdom

def _patch_doctor_iw_reg_get(monkeypatch, stdout: str, returncode: int = 0):
    def fake_run(cmd, timeout=5.0):
        assert cmd == ["iw", "reg", "get"]
        return subprocess.CompletedProcess(
            cmd,
            returncode,
            stdout=stdout,
            stderr="boom" if returncode else "",
        )

    monkeypatch.setattr(doctor.network, "_run", fake_run)


def test_check_wifi_regdom_ok_when_global_country_valid_and_phy_unlabeled(
    monkeypatch,
):
    _patch_doctor_iw_reg_get(
        monkeypatch,
        """global
country US: DFS-FCC
\t(2400 - 2472 @ 40), (N/A, 30), (N/A)

phy#0
country 99: DFS-UNSET
\t(2402 - 2482 @ 40), (6, 20), (N/A)
""",
    )
    r = doctor.check_wifi_regdom()
    assert r.status == "ok"
    assert "global country=US" in r.detail
    assert "phy0 country=99" in r.detail
    assert "not actionable by itself" in r.detail


def test_check_wifi_regdom_warns_when_global_country_unset(monkeypatch):
    _patch_doctor_iw_reg_get(
        monkeypatch,
        """global
country 00: DFS-UNSET

phy#0
country 99: DFS-UNSET
""",
    )
    r = doctor.check_wifi_regdom()
    assert r.status == "warn"
    assert "global regdom is '00'" in r.detail
    assert "do_wifi_country <CC>" in r.detail


def test_check_wifi_regdom_ok_with_valid_global_and_no_phy(monkeypatch):
    _patch_doctor_iw_reg_get(
        monkeypatch,
        """global
country DE: DFS-ETSI
""",
    )
    r = doctor.check_wifi_regdom()
    assert r.status == "ok"
    assert "global country=DE" in r.detail
    assert "no per-phy regdom reported" in r.detail


# ---------------------------------------------------- check_wifi_guardian
#
# The check has four happy/warn paths to cover (matches the design
# doc §3.7 (F)):
#   - ok: stash present, active SSID matches
#   - ok: no stash and no active WiFi (Ethernet-only Pi)
#   - warn: WiFi up, no stash -> wizard never saved
#   - warn: stash present, active WiFi on a different SSID -> drift
#   - warn: stash present, no active WiFi -> last guardian failed
# Skip path:
#   - ok with detail "skipped" when nmcli isn't on PATH

def _mock_nmcli_proc(stdout: str = "", returncode: int = 0):
    """Synthesize a CompletedProcess for `_run` to return."""
    import subprocess
    return subprocess.CompletedProcess(
        args=["nmcli"], returncode=returncode,
        stdout=stdout, stderr="",
    )


def _patch_doctor_nmcli(monkeypatch, response_stack):
    """Patch shutil.which to return a path and doctor._run to return
    the next CompletedProcess in response_stack for each call.

    Each entry can be either a string (treated as stdout, rc=0) or
    a CompletedProcess. The check makes 0-2 _run() calls depending
    on the path; over-long stacks are fine, under-long stacks fail
    the call with returncode=1.
    """
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: "/usr/bin/nmcli" if name == "nmcli" else None,
    )
    responses = iter(response_stack)

    def fake_run(cmd, timeout=5.0):
        try:
            r = next(responses)
        except StopIteration:
            return _mock_nmcli_proc(returncode=1)
        if isinstance(r, str):
            return _mock_nmcli_proc(stdout=r)
        return r

    monkeypatch.setattr(doctor.network, "_run", fake_run)


def test_check_wifi_guardian_ok_when_stash_matches_active(
    monkeypatch, tmp_path,
):
    stash = tmp_path / "wifi_guardian.env"
    stash.write_text(
        "JASPER_WIFI_SSID=Home\nJASPER_WIFI_PSK=p\nJASPER_WIFI_KEY_MGMT=wpa-psk\n",
    )
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(stash))
    _patch_doctor_nmcli(monkeypatch, [
        # connection show --active (TYPE,DEVICE,NAME)
        "802-11-wireless:wlan0:Home\n",
        # connection show Home (ssid lookup)
        "802-11-wireless.ssid:Home\n",
    ])
    r = doctor.check_wifi_guardian()
    assert r.status == "ok"
    assert "matches" in r.detail.lower() or "home" in r.detail.lower()


def test_check_wifi_guardian_ok_ethernet_only(monkeypatch, tmp_path):
    """No stash and no active WiFi → ethernet-only or never-configured
    Pi. Don't warn — there's nothing to recover and nothing to drift."""
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(tmp_path / "missing.env"))
    _patch_doctor_nmcli(monkeypatch, [
        # connection show --active → no wifi line (TYPE,DEVICE,NAME)
        "802-3-ethernet:eth0:Wired connection 1\n",
    ])
    r = doctor.check_wifi_guardian()
    assert r.status == "ok"


def test_check_wifi_guardian_warns_when_stash_missing_but_active(
    monkeypatch, tmp_path,
):
    """WiFi works but the stash hasn't been seeded — operator brought
    up wifi via raspi-config or installed before our migration shipped.
    Warn so the dashboard / system check surfaces the recovery gap."""
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(tmp_path / "missing.env"))
    _patch_doctor_nmcli(monkeypatch, [
        "802-11-wireless:wlan0:Home\n",
        "802-11-wireless.ssid:Home\n",
    ])
    r = doctor.check_wifi_guardian()
    assert r.status == "warn"
    assert "stash" in r.detail.lower()
    assert "/wifi/" in r.detail  # actionable: tells operator where to go


def test_check_wifi_guardian_warns_on_ssid_drift(monkeypatch, tmp_path):
    """Stash says Home, NM is on Cafe — operator switched via SSH and
    didn't re-save in the wizard. Warn so the next dirty shutdown
    doesn't recreate the wrong network."""
    stash = tmp_path / "wifi_guardian.env"
    stash.write_text(
        "JASPER_WIFI_SSID=Home\nJASPER_WIFI_PSK=p\nJASPER_WIFI_KEY_MGMT=wpa-psk\n",
    )
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(stash))
    _patch_doctor_nmcli(monkeypatch, [
        "802-11-wireless:wlan0:Cafe\n",
        "802-11-wireless.ssid:Cafe\n",
    ])
    r = doctor.check_wifi_guardian()
    assert r.status == "warn"
    assert "Home" in r.detail and "Cafe" in r.detail


def test_check_wifi_guardian_matches_colon_ssid(monkeypatch, tmp_path):
    """A profile NAME with a literal colon (e.g. "Home:5G") must be
    matched, not silently treated as "no active WiFi".

    Regression for the same colon-parse bug as C10-1: the guardian check
    used to run its own NAME-first nmcli probe, which mis-split an escaped
    "\\:" and reported a bogus "no recovery stash"/"no WiFi" state for a
    valid profile. It now reuses the colon-safe _active_wifi_connection.
    The SSID value lookup is forced to fail so the check falls back to the
    (unescaped) profile name, pinning the helper's output end-to-end."""
    stash = tmp_path / "wifi_guardian.env"
    stash.write_text(
        "JASPER_WIFI_SSID=Home:5G\nJASPER_WIFI_PSK=p\nJASPER_WIFI_KEY_MGMT=wpa-psk\n",
    )
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(stash))
    _patch_doctor_nmcli(monkeypatch, [
        # active connection: NAME "Home:5G" arrives colon-escaped from nmcli -t
        "802-11-wireless:wlan0:Home\\:5G\n",
        # ssid value lookup fails → fall back to the unescaped profile name
        _mock_nmcli_proc(returncode=1),
    ])
    r = doctor.check_wifi_guardian()
    assert r.status == "ok"
    assert "Home:5G" in r.detail


def test_check_wifi_guardian_warns_when_active_wifi_missing(
    monkeypatch, tmp_path,
):
    """Stash is configured but no WiFi is currently up. Either the
    guardian's last run failed, or NM was unable to bring up the
    network. Either way the operator should investigate."""
    stash = tmp_path / "wifi_guardian.env"
    stash.write_text(
        "JASPER_WIFI_SSID=Home\nJASPER_WIFI_PSK=p\nJASPER_WIFI_KEY_MGMT=wpa-psk\n",
    )
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(stash))
    _patch_doctor_nmcli(monkeypatch, [
        "",  # no active wifi
    ])
    r = doctor.check_wifi_guardian()
    assert r.status == "warn"
    assert "Home" in r.detail
    assert "guardian" in r.detail.lower()


def test_check_wifi_guardian_skipped_without_nmcli(monkeypatch):
    """Pis without NetworkManager (or running this check in CI) →
    skip cleanly. The guardian itself is no-op on those machines."""
    monkeypatch.setattr(
        doctor.shutil, "which",
        lambda name: None if name == "nmcli" else f"/usr/bin/{name}",
    )
    r = doctor.check_wifi_guardian()
    assert r.status == "ok"
    assert "skipped" in r.detail


def test_check_wifi_guardian_registered_in_sync_checks():
    """Make sure the check is actually registered to run (not just
    defined). Mirrors the spirit of the `check_wifi_regdom` registration
    this check sits next to."""
    assert "check_wifi_guardian" in _registered_check_names()


def test_check_wifi_link_local_ipv6_ok(monkeypatch):
    # nmcli -t -f TYPE,DEVICE,NAME connection show --active
    _patch_doctor_nmcli(monkeypatch, [
        "802-11-wireless:wlan0:Home\n",
        "link-local\n",
        "2: wlan0    inet6 fe80::1/64 scope link\n",
    ])
    r = doctor.check_wifi_link_local_ipv6()
    assert r.status == "ok"
    assert "link-local IPv6" in r.detail


def test_check_wifi_link_local_ipv6_warns_when_profile_ignores_ipv6(monkeypatch):
    # Profile NAME carries a literal colon (e.g. "Home:5G"); it arrives
    # escaped as "\:" in nmcli -t output and must be unescaped, not dropped.
    _patch_doctor_nmcli(monkeypatch, [
        "802-11-wireless:wlan0:Home\\:5G\n",
        "ignore\n",
    ])
    r = doctor.check_wifi_link_local_ipv6()
    assert r.status == "warn"
    assert "ipv6.method=ignore" in r.detail
    assert "Apple clients" in r.detail
    # Profile resolved with its colon intact (shlex.quote leaves a colon
    # name unquoted — colons need no shell escaping).
    assert "active WiFi profile 'Home:5G'" in r.detail
    assert "nmcli connection modify Home:5G ipv6.method link-local" in r.detail


def test_check_wifi_link_local_ipv6_warns_when_link_local_missing(monkeypatch):
    _patch_doctor_nmcli(monkeypatch, [
        "802-11-wireless:wlan0:Home\n",
        "auto\n",
        "",
    ])
    r = doctor.check_wifi_link_local_ipv6()
    assert r.status == "warn"
    assert "no link-local IPv6" in r.detail


def test_check_wifi_link_local_ipv6_registered_in_sync_checks():
    assert "check_wifi_link_local_ipv6" in _registered_check_names()


def test_check_avahi_jasper_control_ok_on_partial_timeout(monkeypatch):
    """Resolved avahi-browse can hang on stale sibling records after seeing
    the local service. That is still evidence that jasper-control is
    advertised; it should not crash the whole doctor run."""
    monkeypatch.setattr(
        doctor.network.shutil,
        "which",
        lambda name: "/usr/bin/avahi-browse" if name == "avahi-browse" else None,
    )

    def fake_run(cmd, timeout=5.0):
        raise subprocess.TimeoutExpired(
            cmd,
            timeout,
            output=(
                "+ wlan0 IPv4 JTS jasper-control on jts5 "
                "_jasper-control._tcp local\n"
            ),
        )

    monkeypatch.setattr(doctor.network, "_run", fake_run)

    r = doctor.check_avahi_jasper_control()

    assert r.status == "ok"
    assert "stale peer" in r.detail


def test_check_avahi_jasper_control_fails_on_timeout_without_service(
    monkeypatch,
):
    monkeypatch.setattr(
        doctor.network.shutil,
        "which",
        lambda name: "/usr/bin/avahi-browse" if name == "avahi-browse" else None,
    )

    def fake_run(cmd, timeout=5.0):
        raise subprocess.TimeoutExpired(cmd, timeout, output="")

    monkeypatch.setattr(doctor.network, "_run", fake_run)

    r = doctor.check_avahi_jasper_control()

    assert r.status == "fail"
    assert "timed out" in r.detail


def test_check_correction_web_service_ok_when_socket_active(monkeypatch):
    def fake_run(cmd, timeout=5.0):
        unit = cmd[-1]
        out = "active\n" if unit.endswith(".socket") else "inactive\n"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(doctor.correction, "_run", fake_run)
    r = doctor.check_correction_web_service()
    assert r.status == "ok"
    assert "socket active" in r.detail


def test_check_correction_https_assets_registered_in_sync_checks():
    assert "check_correction_https_assets" in _registered_check_names()


def _web_root_with_app_css(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.css").write_text("/* x */", encoding="utf-8")
    return tmp_path


def test_check_correction_https_assets_ok_on_200(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(_web_root_with_app_css(tmp_path)))
    monkeypatch.setattr(doctor.correction, "_probe_https_status", lambda *a, **k: (200, ""))
    r = doctor.check_correction_https_assets()
    assert r.status == "ok"
    assert "200" in r.detail


def test_check_correction_https_assets_warns_on_http_downgrade(monkeypatch, tmp_path):
    # The bug signature: an HTTPS asset downgrade to http:// → browsers
    # mixed-content-block it.
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(_web_root_with_app_css(tmp_path)))
    monkeypatch.setattr(
        doctor.correction, "_probe_https_status",
        lambda *a, **k: (308, "http://jts.local/assets/app.css"),
    )
    r = doctor.check_correction_https_assets()
    assert r.status == "warn"
    assert "mixed-content" in r.detail.lower()
    assert "443" in r.detail


def test_check_correction_https_assets_skips_without_web_root(monkeypatch, tmp_path):
    # Dev checkout: no /usr/share/jasper-web/assets/app.css → skip, never probes.
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))

    def _boom(*a, **k):
        raise AssertionError("must not probe when the web root is absent")

    monkeypatch.setattr(doctor.correction, "_probe_https_status", _boom)
    r = doctor.check_correction_https_assets()
    assert r.status == "ok"
    assert "skip" in r.detail.lower()


def test_check_correction_https_assets_skips_when_443_unreachable(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(_web_root_with_app_css(tmp_path)))

    def _refused(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(doctor.correction, "_probe_https_status", _refused)
    r = doctor.check_correction_https_assets()
    assert r.status == "ok"
    assert "not reachable" in r.detail.lower()


def test_check_correction_state_dirs_warns_on_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_CORRECTION_ROOT", str(tmp_path / "missing"))
    r = doctor.check_correction_state_dirs()
    assert r.status == "warn"
    assert "missing" in r.detail


def test_check_correction_current_config_reports_missing_config(
    monkeypatch, tmp_path,
):
    statefile = tmp_path / "statefile.yml"
    missing = tmp_path / "does-not-exist.yml"
    statefile.write_text(f"config_path: {missing}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    r = doctor.check_correction_current_config()
    assert r.status == "fail"
    assert "missing config" in r.detail


def test_check_correction_current_config_reports_flat_base(
    monkeypatch, tmp_path,
):
    statefile = tmp_path / "statefile.yml"
    base = tmp_path / "v1.yml"
    base.write_text("# base\n")
    statefile.write_text(f"config_path: {base}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    r = doctor.check_correction_current_config()
    assert r.status == "warn"
    assert "custom/non-JTS" in r.detail


def test_check_correction_current_config_reports_jts_sound_config(
    monkeypatch, tmp_path,
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    generated = config_dir / "sound_current.yml"
    generated.write_text(
        "# Source: jasper.sound.camilla_yaml.emit_sound_config\n"
        "filters:\n"
        "  flat:\n"
        "    type: Gain\n"
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {generated}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_correction_current_config()

    assert r.status == "ok"
    assert "JTS sound preference" in r.detail
    assert "no room correction" in r.detail.lower()


def test_check_correction_current_config_reports_active_speaker_baseline(
    monkeypatch, tmp_path,
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    generated = config_dir / "active_speaker_baseline.yml"
    generated.write_text(
        "# Source: jasper.active_speaker.camilla_yaml."
        "emit_active_speaker_baseline_config\n"
        "filters:\n"
        "  active_baseline_headroom:\n"
        "    type: Gain\n",
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {generated}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_correction_current_config()

    assert r.status == "ok"
    assert "JTS active-speaker baseline" in r.detail
    assert "managed by the active-speaker" in r.detail


def test_check_correction_current_config_reports_active_leader_program_bake(
    monkeypatch, tmp_path,
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    generated = config_dir / "grouping_active_leader_bake.yml"
    generated.write_text(
        "# Source: jasper.active_speaker.camilla_yaml."
        "emit_active_speaker_program_bake_config\n"
        "devices:\n"
        "  playback:\n"
        "    type: File\n",
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {generated}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_correction_current_config()

    assert r.status == "ok"
    assert "JTS active-leader program bake" in r.detail
    assert "managed by the active-speaker" in r.detail


def test_check_correction_current_config_reports_generated_correction(
    monkeypatch, tmp_path,
):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    generated = config_dir / "correction_abc_1700000000.yml"
    generated.write_text("filters:\n  room_peq_1:\n    type: Biquad\n")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {generated}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_correction_current_config()

    assert r.status == "ok"
    assert "session=abc" in r.detail
    assert "peqs=1" in r.detail


def test_check_camilla_volume_limit_ok(monkeypatch, tmp_path):
    config = tmp_path / "v1.yml"
    config.write_text("devices:\n  samplerate: 48000\n  volume_limit: 0.0\n")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_camilla_volume_limit()

    assert r.status == "ok"
    assert "volume_limit=0.0" in r.detail


def test_check_camilla_volume_limit_fails_when_missing(monkeypatch, tmp_path):
    config = tmp_path / "v1.yml"
    config.write_text("devices:\n  samplerate: 48000\n")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_camilla_volume_limit()

    assert r.status == "fail"
    assert "omits devices.volume_limit" in r.detail


def test_check_camilla_volume_limit_fails_when_positive(monkeypatch, tmp_path):
    config = tmp_path / "v1.yml"
    config.write_text("devices:\n  samplerate: 48000\n  volume_limit: 6.0\n")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n")
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_camilla_volume_limit()

    assert r.status == "fail"
    assert "expected <=" in r.detail


def test_check_camilla_volume_limit_registered_in_sync_checks():
    assert "check_camilla_volume_limit" in _registered_check_names()


def test_active_speaker_runtime_graph_registered_in_sync_checks():
    assert "check_active_speaker_runtime_graph" in _registered_check_names()


def test_active_speaker_output_hardware_match_registered_in_sync_checks():
    assert "check_active_speaker_output_hardware_match" in _registered_check_names()


def test_active_speaker_runtime_graph_ok_without_topology(monkeypatch, tmp_path):
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import _flat_yaml, _topology

    topology_path = tmp_path / "output_topology.json"
    save_output_topology(_topology([]), path=topology_path)
    config = tmp_path / "outputd-cutover.yml"
    config.write_text(_flat_yaml(), encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "ok"
    assert "no roleful/protected outputs" in r.detail


def test_active_speaker_runtime_graph_fails_corrupt_saved_topology(
    monkeypatch,
    tmp_path,
):
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "fail"
    assert "saved output topology is unavailable or invalid" in r.detail
    assert "not valid JSON" in r.detail


def test_active_speaker_runtime_graph_fails_flat_graph_on_tweeter_topology(
    monkeypatch,
    tmp_path,
):
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import (
        _active_topology,
        _flat_yaml,
    )

    topology = _active_topology("mono", "active_2_way")
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    config = tmp_path / "outputd-cutover.yml"
    config.write_text(_flat_yaml(), encoding="utf-8")
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "fail"
    assert "DAC output 2" in r.detail
    assert "flat full-range graph" in r.detail


def test_active_speaker_runtime_graph_accepts_staged_active_startup(
    monkeypatch,
    tmp_path,
):
    from jasper.output_topology import save_output_topology
    from tests.test_active_speaker_runtime_contract import (
        _active_topology,
        _active_yaml,
        _staged_metadata,
    )

    topology = _active_topology("mono", "active_2_way")
    topology_path = tmp_path / "output_topology.json"
    save_output_topology(topology, path=topology_path)
    config = tmp_path / "active_speaker_staged_startup.yml"
    config.write_text(_active_yaml("mono", 2, frozenset()), encoding="utf-8")
    metadata = tmp_path / "active_speaker_staged_config.json"
    metadata.write_text(
        json.dumps(_staged_metadata(topology, config)),
        encoding="utf-8",
    )
    statefile = tmp_path / "statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH", str(metadata))

    r = doctor.check_active_speaker_runtime_graph()

    assert r.status == "ok"
    assert "all_muted_active_startup" in r.detail


def test_check_sound_profile_reports_default_when_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(tmp_path / "missing.json"))

    r = doctor.check_sound_profile()

    assert r.status == "ok"
    assert "default Flat" in r.detail


def test_check_sound_profile_warns_when_saved_profile_not_active(
    monkeypatch, tmp_path,
):
    profile = tmp_path / "sound_profile.json"
    profile.write_text(json.dumps({
        "enabled": True,
        "curve_id": "harman",
        "simple_eq": {"bass_db": 1.0, "mid_db": 0.0, "treble_db": 0.0},
    }))
    statefile = tmp_path / "statefile.yml"
    base = tmp_path / "v1.yml"
    base.write_text("# base\n")
    statefile.write_text(f"config_path: {base}\n")
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile))
    monkeypatch.setenv("JASPER_CAMILLA_STATEFILE", str(statefile))

    r = doctor.check_sound_profile()

    assert r.status == "warn"
    assert "curve=harman" in r.detail
    assert "not reflected" in r.detail


def test_check_sound_profile_fails_on_corrupt_json(monkeypatch, tmp_path):
    profile = tmp_path / "sound_profile.json"
    profile.write_text("{not json")
    monkeypatch.setenv("JASPER_SOUND_PROFILE_PATH", str(profile))

    r = doctor.check_sound_profile()

    assert r.status == "fail"
    assert "could not read" in r.detail


def test_check_dsp_apply_state_reports_success(monkeypatch, tmp_path):
    state = tmp_path / "dsp_apply_state.json"
    state.write_text(json.dumps({
        "op_id": "abcdef123456",
        "source": "sound",
        "phase": "done",
        "result": "success",
        "candidate_config_path": "/var/lib/camilladsp/configs/sound_current.yml",
    }))
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(state))

    r = doctor.check_dsp_apply_state()

    assert r.status == "ok"
    assert "source=sound" in r.detail
    assert "result=success" in r.detail


def test_check_dsp_apply_state_fails_on_rollback_failure(monkeypatch, tmp_path):
    state = tmp_path / "dsp_apply_state.json"
    state.write_text(json.dumps({
        "op_id": "abcdef123456",
        "source": "correction",
        "phase": "load",
        "result": "load_failed_rollback_failed",
        "rollback_attempted": True,
        "rollback_succeeded": False,
    }))
    monkeypatch.setenv("JASPER_DSP_APPLY_STATE_PATH", str(state))

    r = doctor.check_dsp_apply_state()

    assert r.status == "fail"
    assert "rollback_failed" in r.detail


def test_check_correction_latest_bundle_warns_without_calibration(
    monkeypatch, tmp_path,
):
    sessions = tmp_path / "sessions"
    bundle = sessions / "abc"
    bundle.mkdir(parents=True)
    bundles.write_json_artifact(
        bundle,
        "info.json",
        {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": "abc",
            "state": "ready",
            "started_at": 1000,
            "capture_quality": [],
        },
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="tests.test_doctor",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    bundles.write_json_artifact(
        bundle,
        "result.json",
        {"bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION},
        kind="analysis_result",
        sensitivity="private_metadata",
        recomputable=True,
        generated_by="tests.test_doctor",
        dependencies=["info.json"],
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(sessions))

    r = doctor.check_correction_latest_bundle()

    assert r.status == "warn"
    assert "no calibrated mic" in r.detail


def test_check_correction_latest_bundle_warns_when_failed(
    monkeypatch, tmp_path,
):
    sessions = tmp_path / "sessions"
    bundle = sessions / "failed"
    bundle.mkdir(parents=True)
    bundles.write_json_artifact(
        bundle,
        "info.json",
        {
            "bundle_schema_version": bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
            "session_id": "failed",
            "state": "failed",
            "started_at": 1000,
            "error": "analysis failed: capture clipped",
            "capture_quality": [],
        },
        kind="session_metadata",
        sensitivity="private_metadata",
        recomputable=False,
        generated_by="tests.test_doctor",
        schema_version=bundles.CURRENT_BUNDLE_SCHEMA_VERSION,
    )
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(sessions))

    r = doctor.check_correction_latest_bundle()

    assert r.status == "warn"
    assert "capture clipped" in r.detail


def test_check_correction_latest_bundle_reports_bundle_collection(
    monkeypatch, tmp_path,
):
    sessions = tmp_path / "sessions"
    write_golden_correction_bundle(sessions, "old", started_at=1000)
    write_golden_correction_bundle(sessions, "new", started_at=2000)
    monkeypatch.setenv("JASPER_CORRECTION_SESSIONS_DIR", str(sessions))

    r = doctor.check_correction_latest_bundle()

    assert r.status == "ok"
    assert "session=new" in r.detail
    assert "bundles=2" in r.detail
    assert "storage=" in r.detail
    assert "private_raw=8/" in r.detail
    assert "evidence=complete(" in r.detail
    assert "old raw recordings present (8 files)" in r.detail


def test_correction_doctor_checks_registered():
    names = _registered_check_names()
    assert "check_correction_web_service" in names
    assert "check_correction_state_dirs" in names
    assert "check_correction_current_config" in names
    assert "check_sound_profile" in names
    assert "check_dsp_apply_state" in names
    assert "check_correction_latest_bundle" in names


def test_web_design_assets_warns_when_manifest_missing(
    monkeypatch, tmp_path: Path,
):
    """No manifest = unverifiable tree — warn, never guess from a stale
    built-in list (which could pass a partially-deployed tree as green)."""
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    (assets / "app.css").write_text("/* css */")
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))
    r = doctor.check_web_design_assets()
    assert r.status == "warn"
    assert ".install-manifest" in r.detail
    assert "redeploy" in r.detail


def _manifest_fixture(tmp_path: Path, entries: list[str]) -> Path:
    """Lay down app.css plus a manifest listing `entries`."""
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    (assets / "app.css").write_text("/* css */")
    (assets / ".install-manifest").write_text("\n".join(entries) + "\n")
    return assets


def test_web_design_assets_verifies_every_manifest_entry(
    monkeypatch, tmp_path: Path,
):
    """With the installer-written manifest present, the check covers the
    full installed tree — no hand list involved."""
    assets = _manifest_fixture(
        tmp_path, ["wifi/wifi.css", "wifi/js/main.js", "shared/js/escape.js"]
    )
    for rel in ("wifi/wifi.css", "wifi/js/main.js", "shared/js/escape.js"):
        target = assets / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// asset")
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))
    r = doctor.check_web_design_assets()
    assert r.status == "ok"
    assert "4 assets verified" in r.detail  # app.css + 3 manifest entries
    assert ".install-manifest" in r.detail


def test_web_design_assets_warns_on_missing_manifest_entry(
    monkeypatch, tmp_path: Path,
):
    assets = _manifest_fixture(tmp_path, ["wake/js/main.js", "wake/wake.css"])
    (assets / "wake").mkdir(parents=True)
    (assets / "wake" / "wake.css").write_text("/* css */")
    # wake/js/main.js deliberately absent — the page would load blank.
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))
    r = doctor.check_web_design_assets()
    assert r.status == "warn"
    assert "wake/js/main.js" in r.detail


def test_web_design_assets_ignores_malformed_manifest_lines(
    monkeypatch, tmp_path: Path,
):
    """One bad byte in the manifest must not distort the check."""
    assets = _manifest_fixture(
        tmp_path,
        ["", "# comment", "/etc/passwd", "a/../../escape", "voice/js/main.js"],
    )
    (assets / "voice" / "js").mkdir(parents=True)
    (assets / "voice" / "js" / "main.js").write_text("// module")
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))
    r = doctor.check_web_design_assets()
    assert r.status == "ok", r.detail
    assert "2 assets verified" in r.detail  # app.css + the one sane entry


def test_web_design_assets_caps_the_missing_list(monkeypatch, tmp_path: Path):
    """A wiped asset tree warns with a bounded list, not journal spam."""
    _manifest_fixture(
        tmp_path, [f"page{i}/js/main.js" for i in range(20)]
    )
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))
    r = doctor.check_web_design_assets()
    assert r.status == "warn"
    assert "(+8 more)" in r.detail
    assert r.detail.count("js/main.js") == 12


def test_web_design_assets_warns_when_stylesheet_missing(
    monkeypatch, tmp_path: Path,
):
    """app.css is pinned explicitly even if a manifest omits it — it is
    the design system itself."""
    assets = tmp_path / "assets"
    assets.mkdir(parents=True)
    (assets / ".install-manifest").write_text("voice/js/main.js\n")
    (assets / "voice" / "js").mkdir(parents=True)
    (assets / "voice" / "js" / "main.js").write_text("// module")
    # No app.css written — the design system can't load.
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path))
    r = doctor.check_web_design_assets()
    assert r.status == "warn"
    assert "assets/app.css" in r.detail


def test_web_design_assets_skips_when_not_installed(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("JASPER_WEB_SHARE_DIR", str(tmp_path / "nope"))
    r = doctor.check_web_design_assets()
    assert r.status == "ok"
    assert "not installed" in r.detail


def test_web_design_assets_check_registered():
    assert "check_web_design_assets" in _registered_check_names()


# ---------------------------------------------------------------------------
# _assess_wake_legs — configured intent vs runtime-armed legs
# (the runtime cross-check added with /state.voice.wake_legs)
# ---------------------------------------------------------------------------


def test_assess_wake_legs_skips_when_aec_disabled():
    r = doctor._assess_wake_legs(
        "disabled", raw=True, dtln=False, armed_runtime=None,
    )
    assert r.status == "ok"
    assert "n/a" in r.detail


def test_assess_wake_legs_reports_intent_when_daemon_unreachable():
    """armed_runtime=None (jasper-control down) → fall back to configured
    intent, never a false 'leg skipped' warning."""
    r = doctor._assess_wake_legs(
        "auto", raw=True, dtln=False, armed_runtime=None,
    )
    assert r.status == "ok"
    assert "configured" in r.detail
    assert "aec3" in r.detail and "raw" in r.detail
    assert "/wake/" in r.detail  # not the stale /system


def test_assess_wake_legs_ok_when_runtime_matches_config():
    r = doctor._assess_wake_legs(
        "auto", raw=True, dtln=True, armed_runtime={"on", "off", "dtln"},
    )
    assert r.status == "ok"
    assert "3 leg(s) armed" in r.detail


def test_assess_wake_legs_warns_when_configured_leg_not_armed():
    """The whole point: raw is configured on, but the daemon only opened
    the primary leg (a startup skip). Surface it instead of claiming
    'armed' off stale config. raw maps to the chip-direct "off" token."""
    r = doctor._assess_wake_legs(
        "auto", raw=True, dtln=False, armed_runtime={"on"},
    )
    assert r.status == "warn"
    assert "off" in r.detail               # the missing leg (raw -> off)
    assert "wake.leg_skipped" in r.detail  # actionable hint


def test_assess_wake_legs_dtln_skip_warns():
    """DTLN configured but not armed (model OOM / bridge not emitting on
    :9878) → warn naming dtln."""
    r = doctor._assess_wake_legs(
        "auto", raw=True, dtln=True, armed_runtime={"on", "off"},
    )
    assert r.status == "warn"
    assert "dtln" in r.detail


def test_assess_wake_legs_chip_aec_does_not_false_warn_on_cleared_raw():
    """Chip-AEC mutual exclusion: the reconciler clears raw/DTLN *device*
    vars when chip is on but preserves their booleans as wizard intent. So
    raw=True can coexist with chip_aec=True, and the armed set is the two
    chip beams + on — with NO 'off' leg. The doctor must expect the chip
    set (not 'off'), or it would false-warn 'off not running' on every
    chip-AEC install. This is the regression this fix prevents."""
    r = doctor._assess_wake_legs(
        "auto", raw=True, dtln=False,
        armed_runtime={"on", "chip_aec_150", "chip_aec_210"},
        chip_aec=True,
    )
    assert r.status == "ok", r.detail
    assert "3 leg(s) armed" in r.detail
    assert "chip_aec_150" in r.detail


def test_assess_wake_legs_chip_aec_warns_when_beams_not_armed():
    """Chip-AEC configured on but the beams aren't armed (chip not on the
    6-ch firmware, or bridge down) → warn naming the missing beams, with a
    6-ch-firmware hint."""
    r = doctor._assess_wake_legs(
        "auto", raw=True, dtln=False, armed_runtime={"on"}, chip_aec=True,
    )
    assert r.status == "warn"
    assert "chip_aec_150" in r.detail and "chip_aec_210" in r.detail
    assert "6-ch firmware" in r.detail


def test_assess_wake_legs_chip_aec_intent_when_daemon_unreachable():
    """Daemon down + chip configured → report chip intent, never a false
    leg-skip warning."""
    r = doctor._assess_wake_legs(
        "auto", raw=True, dtln=False, armed_runtime=None, chip_aec=True,
    )
    assert r.status == "ok"
    assert "chip_aec_150" in r.detail
    # raw is on but mutual exclusion means it isn't part of the chip config.
    assert "raw" not in r.detail


# ---------------------------------------------------------------------------
# Audio profile runtime truth — shared classifier used by /aec and doctor
# ---------------------------------------------------------------------------


def test_audio_profile_doctor_check_reports_active_chip_profile(monkeypatch):
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    settings = {
        "JASPER_WAKE_LEG_RAW": True,
        "JASPER_WAKE_LEG_DTLN": False,
        "JASPER_WAKE_LEG_CHIP_AEC": True,
    }
    monkeypatch.setattr(
        doctor.aec,
        "_wake_leg_setting",
        lambda key, default: settings.get(key, default),
    )

    status = doctor._audio_profile_status_for_doctor(
        bridge_active=True,
        env={
            "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AEC_CHIP_AEC_ENABLED": "1",
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "udp:9887",
            "JASPER_MIC_DEVICE_CHIP_AEC_210": "udp:9888",
        },
        mic_probe=MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
    )
    result = doctor._assess_audio_profile(status)

    assert result.status == "ok"
    assert "requested=xvf_chip_aec" in result.detail
    assert "active=xvf_chip_aec" in result.detail
    assert "Chip AEC 150 beam via :9876" in result.detail


def test_aec_bridge_running_reports_chip_forwarding(monkeypatch):
    def fake_run(cmd, **kwargs):
        if cmd == ["systemctl", "is-active", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if cmd == ["systemctl", "is-enabled", "jasper-aec-bridge.service"]:
            return SimpleNamespace(returncode=0, stdout="enabled\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(doctor.aec, "_parked_as_bonded_follower", lambda: False)
    monkeypatch.setattr(doctor.aec, "_run", fake_run)
    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda *, bridge_active=None: {
            "audio_profile": {"active": "xvf_chip_aec"},
            "microphone": {"processing_mode": "Chip-AEC"},
            "chip_aec_gate": {"status": "approved", "source": "static"},
        },
    )

    result = doctor.aec.check_aec_bridge_running()

    assert result.status == "ok"
    assert "chip-AEC beam forwarding" in result.detail
    assert "WebRTC AEC3 bypassed" in result.detail
    assert "gate=approved/static" in result.detail
    assert "software AEC enabled" not in result.detail


def test_audio_profile_doctor_check_warns_when_runtime_env_pending(monkeypatch):
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    settings = {
        "JASPER_WAKE_LEG_RAW": True,
        "JASPER_WAKE_LEG_DTLN": False,
        "JASPER_WAKE_LEG_CHIP_AEC": True,
    }
    monkeypatch.setattr(
        doctor.aec,
        "_wake_leg_setting",
        lambda key, default: settings.get(key, default),
    )

    status = doctor._audio_profile_status_for_doctor(
        bridge_active=True,
        env={
            "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "Array",
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
            "JASPER_MIC_DEVICE_CHIP_AEC_150": "",
            "JASPER_MIC_DEVICE_CHIP_AEC_210": "",
        },
        mic_probe=MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
    )
    result = doctor._assess_audio_profile(status)

    assert result.status == "warn"
    assert "active=xvf_software_aec3" in result.detail
    assert "not applied" in result.detail


def test_audio_profile_doctor_check_names_stale_saved_aec_card(monkeypatch):
    monkeypatch.setattr(doctor.aec, "_aec_mode_setting", lambda: "auto")
    settings = {
        "JASPER_WAKE_LEG_RAW": False,
        "JASPER_WAKE_LEG_DTLN": False,
        "JASPER_WAKE_LEG_CHIP_AEC": True,
    }
    monkeypatch.setattr(
        doctor.aec,
        "_wake_leg_setting",
        lambda key, default: settings.get(key, default),
    )

    status = doctor._audio_profile_status_for_doctor(
        bridge_active=False,
        env={
            "JASPER_AUDIO_DAC_ID": "hifiberry_dac8x",
            "JASPER_MIC_DEVICE": "udp:9876",
            "JASPER_AEC_MIC_DEVICE": "L16K6Ch",
            "JASPER_AEC_CHIP_AEC_ENABLED": "0",
        },
        mic_probe=MicProbe(
            xvf_present=True,
            capture_channels=6,
            recommended_channels=6,
            alsa_card_name="Array",
            variant_id="xvf3800_legacy_square_6ch",
            geometry="square",
            chip_beam_plan="xvf_square_fixed_150_210",
        ),
    )
    result = doctor._assess_audio_profile(status)

    assert result.status == "warn"
    assert "Configured AEC mic L16K6Ch" in result.detail
    assert "detected XVF card Array" in result.detail


def test_audio_validation_advisory_ok_when_chip_aec_not_requested():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "missing",
            "status": "unknown",
            "artifact_path": "/var/lib/jasper/audio-validation",
            "reason": "artifact not found",
        },
        requested_profile="xvf_software_aec3",
    )

    assert result.status == "ok"
    assert "advisory" in result.detail


def test_audio_validation_warns_when_chip_aec_requested_and_missing():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "missing",
            "status": "unknown",
            "artifact_path": "/var/lib/jasper/audio-validation",
            "reason": "artifact not found",
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert "sudo jasper-audio-validate --stdout" in result.detail
    assert "advisory" in result.detail


def test_audio_validation_suggests_hardware_runner_when_ready_for_passive_evidence():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_hardware_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert (
        "sudo jasper-audio-hw-validate --duration-seconds 10 --stdout"
        in result.detail
    )
    assert "advisory" in result.detail


def test_audio_validation_suggests_hardware_runner_for_drift_delay_recommendation():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_drift_delay_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert (
        "sudo jasper-audio-hw-validate --duration-seconds 10 --stdout"
        in result.detail
    )


def test_audio_validation_ok_for_known_supported_passive_chip_aec_validation():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_drift_delay_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
            "hardware": {
                "mic_id": "xvf3800",
                "dac_id": "hifiberry_dac8x",
            },
            "check_statuses": {
                "runtime_profile": "pass",
                "mic_detected": "pass",
                "runtime_env": "pass",
                "service_state": "pass",
                "dac_reference": "pass",
                "wake_legs": "pass",
                "bridge_counters": "warn",
                "outputd_reference_health": "pass",
                "bridge_counter_window": "pass",
                "chip_profile_readback": "pass",
                "chip_convergence": "pass",
                "measured_drift_delay": "not_run",
            },
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "ok"
    assert "known-supported xvf_chip_aec path" in result.detail
    assert "optional acoustic drift/delay probe" in result.detail


def test_audio_validation_still_warns_for_unknown_dac_drift_delay_recommendation():
    result = doctor._assess_audio_validation_summary(
        {
            "state": "current",
            "status": "warn",
            "recommendation": "run_drift_delay_validation",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
            "hardware": {
                "mic_id": "xvf3800",
                "dac_id": "apple_usb_c_dongle",
            },
            "check_statuses": {
                "runtime_profile": "pass",
                "mic_detected": "pass",
                "runtime_env": "pass",
                "service_state": "pass",
                "dac_reference": "pass",
                "wake_legs": "pass",
                "outputd_reference_health": "pass",
                "bridge_counter_window": "pass",
                "chip_profile_readback": "pass",
                "chip_convergence": "pass",
                "measured_drift_delay": "not_run",
            },
        },
        requested_profile="xvf_chip_aec",
    )

    assert result.status == "warn"
    assert (
        "sudo jasper-audio-hw-validate --duration-seconds 10 --stdout"
        in result.detail
    )


def test_audio_validation_readiness_filters_current_hardware(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        doctor.aec,
        "_audio_profile_status_for_doctor",
        lambda: {"audio_profile": {"requested": "xvf_chip_aec"}},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_shared_parse_env_file",
        lambda _path: {"JASPER_AUDIO_DAC_ID": "apple_usb_c_dongle"},
    )
    monkeypatch.setattr(
        doctor.aec,
        "_audio_validation_filter_kwargs",
        lambda **kwargs: {
            "requested_profile": kwargs["requested_profile"],
            "mic_id": "xvf3800",
            "dac_id": "apple_usb_c_dongle",
        },
    )

    def fake_summary(**kwargs):
        captured.update(kwargs)
        return {
            "state": "current",
            "status": "pass",
            "artifact_path": "/var/lib/jasper/audio-validation/latest.json",
        }

    monkeypatch.setattr(doctor.aec, "_audio_validation_summary", fake_summary)

    result = doctor.check_audio_validation_readiness()

    assert result.status == "ok"
    assert captured == {
        "requested_profile": "xvf_chip_aec",
        "mic_id": "xvf3800",
        "dac_id": "apple_usb_c_dongle",
    }


def test_pricing_ok_when_active_model_priced(monkeypatch):
    """The active model (gemini default) is in the bundled rates → ok."""
    cfg = _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaABCDEF12345")
    assert doctor.check_pricing(cfg).status == "ok"


def test_pricing_warns_when_active_model_unpriced(monkeypatch):
    """An active model with no bundled/override rate → warn (cost reads $0,
    the spend cap can't bound it)."""
    cfg = _fresh_cfg(
        monkeypatch,
        GEMINI_API_KEY="AIzaABCDEF12345",
        JASPER_GEMINI_MODEL="gemini-9.9-does-not-exist",
    )
    assert doctor.check_pricing(cfg).status == "warn"


# ---------------------------------------------------------------------------
# check_spend_cap — disabled cap renders as "disabled", not "$0.00 remaining"


def test_check_spend_cap_reports_disabled_not_zero_remaining(tmp_path: Path, monkeypatch):
    """With JASPER_DAILY_SPEND_CAP_USD=0 the cap is disabled (see
    jasper.usage.SpendCap.disabled); doctor must say so instead of the
    misleading "$0.0000 remaining of $0.00"."""
    from jasper.cli.doctor.voice import check_spend_cap

    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.setenv("JASPER_USAGE_DB", str(tmp_path / "usage.sqlite3"))
    monkeypatch.setenv("JASPER_DAILY_SPEND_CAP_USD", "0")
    cfg = Config.from_env()
    result = check_spend_cap(cfg)
    assert result.status == "ok"
    assert "disabled" in result.detail
    assert "remaining" not in result.detail
# DTLN engine — bridge stats snapshot surface (journal-independent)
# ---------------------------------------------------------------------------


def _dtln_stats(enabled: bool, loaded: bool, error=None, age_sec: float = 1.0):
    import time as _time
    return {
        "schema_version": 1,
        "updated_epoch_sec": _time.time() - age_sec,
        "leg_engines": {
            "dtln": {"enabled": enabled, "loaded": loaded, "error": error},
        },
    }


def test_assess_dtln_stats_loaded_returns_ok():
    import time as _time
    r = doctor.aec._assess_dtln_engine_from_stats(
        _dtln_stats(enabled=True, loaded=True), _time.time(),
    )
    assert r is not None and r.status == "ok"
    assert "stats snapshot" in r.detail


def test_assess_dtln_stats_load_failure_returns_fail_with_detail():
    import time as _time
    r = doctor.aec._assess_dtln_engine_from_stats(
        _dtln_stats(enabled=True, loaded=False, error="onnx missing"),
        _time.time(),
    )
    assert r is not None and r.status == "fail"
    assert "onnx missing" in r.detail
    assert ":9878" in r.detail  # names the unfed leg voice listens on


def test_assess_dtln_stats_bridge_started_without_leg_warns():
    import time as _time
    r = doctor.aec._assess_dtln_engine_from_stats(
        _dtln_stats(enabled=False, loaded=False), _time.time(),
    )
    assert r is not None and r.status == "warn"
    assert "systemctl restart jasper-aec-bridge" in r.detail
    # A hand-set JASPER_AEC_DTLN_ENABLED=1 under the chip-AEC profile is
    # NOT a stale-restart problem — the chip profile never loads DTLN.
    # The message must point at checking the active input profile, not
    # only at restarting the bridge.
    assert "input profile" in r.detail
    assert "xvf_chip_aec" in r.detail


def test_assess_dtln_stats_stale_or_legacy_falls_back():
    import time as _time
    now = _time.time()
    # Stale snapshot (dead/old bridge process) → journal fallback.
    assert doctor.aec._assess_dtln_engine_from_stats(
        _dtln_stats(enabled=True, loaded=True, age_sec=120.0), now,
    ) is None
    # Pre-leg_engines bridge build → journal fallback.
    assert doctor.aec._assess_dtln_engine_from_stats(
        {"updated_epoch_sec": now}, now,
    ) is None


def test_check_dtln_prefers_stats_snapshot_over_journal(
    monkeypatch, tmp_path: Path,
):
    """End-to-end: with a fresh stats snapshot reporting a load
    failure, the check fails from the snapshot and never shells out
    to journalctl (whose 10-min window would miss an old failure)."""
    _install_fake_dtln_registry(monkeypatch, tmp_path)
    monkeypatch.setenv("JASPER_AEC_DTLN_ENABLED", "1")
    (tmp_path / "dtln_aec_256_1.onnx").write_bytes(b"model")
    (tmp_path / "dtln_aec_256_2.onnx").write_bytes(b"model")
    stats_path = tmp_path / "aec_bridge_stats.json"
    stats_path.write_text(json.dumps(
        _dtln_stats(enabled=True, loaded=False, error="no onnxruntime"),
    ))
    monkeypatch.setenv("JASPER_AEC_BRIDGE_STATS_PATH", str(stats_path))

    def _fake_run(cmd, **kwargs):
        if cmd[0] == "systemctl":
            return SimpleNamespace(stdout="active", stderr="", returncode=0)
        raise AssertionError(f"unexpected subprocess: {cmd}")

    monkeypatch.setattr(doctor.aec, "_run", _fake_run)

    r = doctor.check_aec_bridge_dtln_engine()

    assert r.status == "fail"
    assert "no onnxruntime" in r.detail


# ---------------------------------------------------------------------------
# check_fanin_tts_drops — dropped TTS audio at the pending budget means the
# user heard garbled/"fast-forward" replies (the 2026-06-11 JTS3 incident).
# ---------------------------------------------------------------------------


def _fanin_payload_with_tts(tts: dict) -> bytes:
    payload = json.loads(_fanin_status_payload().decode())
    payload["tts"] = tts
    return json.dumps(payload).encode()


def test_check_fanin_tts_drops_ok_when_counters_zero(monkeypatch):
    _patch_fanin_status_socket(monkeypatch, _fanin_payload_with_tts({
        "enabled": True,
        "pending_frames": 0,
        "budget_frames": 96000,
        "dropped_commands": 0,
        "dropped_audio_frames": 0,
    }))
    r = doctor.check_fanin_tts_drops()
    assert r.status == "ok"
    assert "none since fan-in start" in r.detail


def test_check_fanin_tts_drops_warns_with_seconds_and_hint(monkeypatch):
    # 82 dropped commands / 523200 frames ≈ 10.9 s at 48 kHz — the real
    # incident's order of magnitude.
    _patch_fanin_status_socket(monkeypatch, _fanin_payload_with_tts({
        "enabled": True,
        "pending_frames": 89216,
        "budget_frames": 96000,
        "dropped_commands": 82,
        "dropped_audio_frames": 523200,
    }))
    r = doctor.check_fanin_tts_drops()
    assert r.status == "warn"
    assert "82 audio command(s)" in r.detail
    assert "~10.9s" in r.detail
    assert "tts_command_dropped" in r.detail  # journalctl breadcrumb


def test_check_fanin_tts_drops_ok_when_lane_disabled(monkeypatch):
    _patch_fanin_status_socket(
        monkeypatch, _fanin_payload_with_tts({"enabled": False}),
    )
    r = doctor.check_fanin_tts_drops()
    assert r.status == "ok"
    assert "disabled" in r.detail


def test_check_fanin_tts_drops_ok_when_status_unreachable(monkeypatch):
    # Reachability is the 'jasper-fanin service' check's job; this check
    # must not double-report a down daemon.
    monkeypatch.setattr(
        doctor.socket,
        "socket",
        lambda *a, **kw: _FakeSocket(error=OSError("connection refused")),
    )
    r = doctor.check_fanin_tts_drops()
    assert r.status == "ok"
    assert "jasper-fanin service" in r.detail


def test_renderer_checks_read_parked_on_bonded_follower(monkeypatch):
    """The dumb-follower profile deliberately stops the renderer stack —
    every liveness check for a parked unit must read ok/'parked', never
    fail against intended state. Driven through the real shared
    predicate with only the grouping config patched."""
    import jasper.multiroom.config as mr_config
    from jasper.cli.doctor import renderers as rdoc

    monkeypatch.setattr(
        mr_config, "load_config",
        lambda *a, **k: _grouping_cfg(
            enabled=True, role="follower", channel="right",
            bond_id="b", leader_addr="jts.local",
        ),
    )
    checks = [
        lambda: rdoc.check_librespot_running(None),
        rdoc.check_shairport_sync_ap2,
        rdoc.check_nqptp_running,
        rdoc.check_jasper_mux,
        rdoc.check_bluealsa,
    ]
    for check in checks:
        r = check()
        assert r.status == "ok", r
        assert "parked (bonded follower)" in r.detail


def test_renderer_checks_probe_normally_when_solo(monkeypatch):
    """The parked skip must vanish on a solo speaker — a dead librespot
    is a real failure there. (Fail-open contract of the predicate.)"""
    import jasper.multiroom.config as mr_config
    from jasper.cli.doctor import renderers as rdoc

    monkeypatch.setattr(
        mr_config, "load_config",
        lambda *a, **k: _grouping_cfg(enabled=False),
    )
    monkeypatch.setattr(rdoc.os.path, "isfile", lambda p: False)
    r = rdoc.check_librespot_running(None)
    assert r.status == "fail"  # binary missing probes through, no skip


def test_voice_aec_checks_read_parked_on_bonded_follower(monkeypatch):
    """PR-B: voice + the AEC stack park on a bonded follower — the
    bridge/mic liveness checks must read parked, never fail against
    intended state."""
    import jasper.multiroom.config as mr_config
    from jasper.cli.doctor import aec as adoc
    from jasper.cli.doctor import audio as audoc

    monkeypatch.setattr(
        mr_config, "load_config",
        lambda *a, **k: _grouping_cfg(
            enabled=True, role="follower", channel="right",
            bond_id="b", leader_addr="jts.local",
        ),
    )
    from jasper.cli.doctor import renderers as rdoc

    checks = [
        adoc.check_aec_bridge_running,
        adoc.check_aec_bridge_output_health,
        adoc.check_aec_bridge_dtln_engine,
        adoc.check_audio_profile_runtime,
        lambda: audoc.check_mic_card_matches_config(None),
        lambda: audoc.check_mic_capture(None),
        # Caught LIVE by the first on-pair doctor run after PR-B
        # deployed: these three probed parked units and read fail/warn
        # against intended state.
        rdoc.check_bluetooth_pairing_policy,
        lambda: rdoc.check_spotify_connect_device(None),
    ]
    for check in checks:
        r = check()
        assert r.status == "ok", r
        assert "parked (bonded follower)" in r.detail


# ----- check_wifi_recover_timer (Wi-Fi flap recovery timer health) -----


def test_check_wifi_recover_timer_enabled_ok(monkeypatch):
    monkeypatch.setattr(
        doctor.network.shutil, "which", lambda _x: "/usr/bin/systemctl"
    )
    monkeypatch.setattr(
        doctor.network, "_run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="enabled\n", stderr=""),
    )
    r = doctor.check_wifi_recover_timer()
    assert r.status == "ok"
    assert "enabled" in r.detail


def test_check_wifi_recover_timer_disabled_warns(monkeypatch):
    monkeypatch.setattr(
        doctor.network.shutil, "which", lambda _x: "/usr/bin/systemctl"
    )
    monkeypatch.setattr(
        doctor.network, "_run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="disabled\n", stderr=""),
    )
    r = doctor.check_wifi_recover_timer()
    assert r.status == "warn"
    assert "enable --now jasper-wifi-recover.timer" in r.detail


def test_check_wifi_recover_timer_not_installed_skips(monkeypatch):
    """A dev box with systemctl but no JTS units: skip, don't warn."""
    monkeypatch.setattr(
        doctor.network.shutil, "which", lambda _x: "/usr/bin/systemctl"
    )
    monkeypatch.setattr(
        doctor.network, "_run",
        lambda *a, **k: SimpleNamespace(
            returncode=1, stdout="",
            stderr="Failed to get unit file state ...: No such file or directory\n",
        ),
    )
    r = doctor.check_wifi_recover_timer()
    assert r.status == "ok"
    assert "not installed" in r.detail


def test_check_wifi_recover_timer_no_systemctl_skips(monkeypatch):
    monkeypatch.setattr(doctor.network.shutil, "which", lambda _x: None)
    r = doctor.check_wifi_recover_timer()
    assert r.status == "ok"
    assert "no systemctl" in r.detail


# ---- check_dac_usb_sync_mode (Stage 6 clock-coherence advisory) -------------


def _sync_mode_state(*syncs):
    """An OutputHardwareState with one Apple playback child per sync tag."""
    return OutputHardwareState(
        profile_id=APPLE_USB_C_DONGLE_DEVICE_ID,
        profile_label="Apple USB-C audio adapter",
        status="ready",
        physical_output_count=2,
        apple_dac_count=len(syncs),
        child_devices=tuple(
            OutputCardFact(
                card_id=f"A{i or ''}",
                device_id=APPLE_USB_C_DONGLE_DEVICE_ID,
                endpoint_sync=tag,
                has_playback=True,
            )
            for i, tag in enumerate(syncs)
        ),
    )


def test_dac_sync_mode_skips_when_no_xvf_mic(monkeypatch):
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: False)
    # Must short-circuit before reading output state when chip-AEC is moot.
    monkeypatch.setattr(
        doctor.audio, "_output_hardware_state_or_none",
        lambda: (_ for _ in ()).throw(AssertionError("must not probe")),
    )
    result = doctor.check_dac_usb_sync_mode()
    assert result.status == "ok"
    assert "no XVF3800 mic present" in result.detail


def test_dac_sync_mode_ok_for_sync_apple_dongle(monkeypatch):
    # Mirrors the real jts capture: Apple dongle reports (SYNC).
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: True)
    monkeypatch.setattr(
        doctor.audio, "_output_hardware_state_or_none",
        lambda: _sync_mode_state("SYNC"),
    )
    result = doctor.check_dac_usb_sync_mode()
    assert result.status == "ok"
    assert "synchronous USB playback endpoint" in result.detail
    # Advisory clock-coherence wording, not an enable/disable gate.
    assert "clock-coherence observation only" in result.detail
    assert "binding chip-AEC gate" in result.detail


def test_dac_sync_mode_ok_for_adaptive_endpoint(monkeypatch):
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: True)
    monkeypatch.setattr(
        doctor.audio, "_output_hardware_state_or_none",
        lambda: _sync_mode_state("ADAPTIVE"),
    )
    result = doctor.check_dac_usb_sync_mode()
    assert result.status == "ok"
    assert "synchronous USB playback endpoint" in result.detail


def test_dac_sync_mode_warns_fail_closed_for_async(monkeypatch):
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: True)
    monkeypatch.setattr(
        doctor.audio, "_output_hardware_state_or_none",
        lambda: _sync_mode_state("ASYNC"),
    )
    result = doctor.check_dac_usb_sync_mode()
    assert result.status == "warn"
    assert "async USB playback endpoint" in result.detail
    # Reframed as advisory: the binding gate is DAC qual + outputd SRO verdict.
    assert "outputd SRO verdict" in result.detail


def test_dac_sync_mode_na_for_i2s_dac(monkeypatch):
    # HiFiBerry/I2S HAT: known DAC profile, no USB endpoint sync tag.
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: True)
    state = OutputHardwareState(
        profile_id="hifiberry_dac8x",
        profile_label="HiFiBerry DAC8x",
        status="ready",
        physical_output_count=8,
        child_devices=(
            OutputCardFact(
                card_id="DAC8x",
                device_id="hifiberry_dac8x",
                endpoint_sync=None,
                has_playback=True,
            ),
        ),
    )
    monkeypatch.setattr(
        doctor.audio, "_output_hardware_state_or_none", lambda: state
    )
    result = doctor.check_dac_usb_sync_mode()
    assert result.status == "ok"
    assert "I2S clock slave" in result.detail


def test_dac_sync_mode_warns_when_state_unavailable(monkeypatch):
    monkeypatch.setattr(doctor.audio.xvf3800, "is_present", lambda: True)
    monkeypatch.setattr(
        doctor.audio, "_output_hardware_state_or_none", lambda: None
    )
    result = doctor.check_dac_usb_sync_mode()
    assert result.status == "warn"
    assert "output hardware state unavailable" in result.detail
