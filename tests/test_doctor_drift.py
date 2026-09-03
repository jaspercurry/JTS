# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the jasper-doctor install-settings-drift domain.

check_installed_settings_drift is one table-driven check over three sources
(systemd unit directives, sysctl knobs, MGLRU) that compares what
deploy/install.sh installed against what the running kernel and systemd report.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jasper.cli import doctor
from jasper.cli.doctor import drift as doctor_drift

from .doctor_test_support import _make_systemctl_show_run


_OOM_WANT = doctor_drift._UNIT_DIRECTIVES["OOMScoreAdjust"]
_ACTION_WANT = doctor_drift._UNIT_DIRECTIVES["StartLimitAction"]
_ON_FAILURE_WANT = doctor_drift._UNIT_DIRECTIVES["OnFailure"]

# Every table unit running, one pid each, so the live half has something to
# read. Unit i gets pid 1000+i.
_PID_MAP = {unit: str(1000 + i) for i, unit in enumerate(sorted(_OOM_WANT))}
_LIVE_OK = {_PID_MAP[unit]: want for unit, want in _OOM_WANT.items()}


def _healthy_systemd_run(**overrides):
    """A systemctl double where every table row matches, then apply overrides.

    ``overrides`` accepts ``oom=``, ``actions=``, ``on_failure=``, ``pids=``
    and ``load_map=`` partial maps merged over the healthy baseline.
    """
    return _make_systemctl_show_run(
        {
            "OOMScoreAdjust": {**_OOM_WANT, **overrides.get("oom", {})},
            "StartLimitAction": {**_ACTION_WANT, **overrides.get("actions", {})},
            "OnFailure": {**_ON_FAILURE_WANT, **overrides.get("on_failure", {})},
            "MainPID": {**_PID_MAP, **overrides.get("pids", {})},
        },
        defaults={
            "OOMScoreAdjust": "0",
            "StartLimitAction": "none",
            "OnFailure": "",
            "MainPID": "0",
        },
        load_map=overrides.get("load_map"),
    )


def _systemd_drift(live=None, **overrides):
    """Run the systemd drift source against a doubled host."""
    live = _LIVE_OK if live is None else {**_LIVE_OK, **live}

    def fake_read(self):
        return live.get(str(self).split("/")[2], "0") + "\n"

    with patch.object(doctor._shared, "_run",
                      side_effect=_healthy_systemd_run(**overrides)), \
         patch("pathlib.Path.read_text", fake_read):
        return doctor_drift._systemd_drift()


def test_systemd_settings_match_when_host_is_healthy():
    items, checked, notes = _systemd_drift()
    assert items == []
    assert checked > len(_OOM_WANT)
    assert notes == []


@pytest.mark.parametrize(
    "overrides, item, got, want",
    [
        ({"oom": {"jasper-camilla": "0"}},
         "jasper-camilla OOMScoreAdjust", "0", _OOM_WANT["jasper-camilla"]),
        ({"actions": {"jasper-voice": "reboot-force"}},
         "jasper-voice StartLimitAction", "reboot-force", "reboot"),
        ({"actions": {"jasper-camilla": "reboot"}},
         "jasper-camilla StartLimitAction", "reboot", "none"),
        ({"on_failure": {"jasper-camilla": ""}},
         "jasper-camilla OnFailure", "none",
         _ON_FAILURE_WANT["jasper-camilla"]),
    ],
)
def test_each_drifted_unit_directive_is_named_individually(
    overrides, item, got, want,
):
    items, _, _ = _systemd_drift(**overrides)
    assert [(d.item, d.got, d.want) for d in items] == [(item, got, want)]


def test_on_failure_matches_as_list_membership():
    """systemd reports OnFailure as a space-separated unit list."""
    handler = _ON_FAILURE_WANT["jasper-camilla"]
    items, _, _ = _systemd_drift(
        on_failure={"jasper-camilla": f"other.service {handler}"},
    )
    assert items == []


def test_units_this_profile_does_not_install_are_not_drift():
    """A streambox omits the voice/AEC stack; absent units report systemd's
    own defaults, which must not read as drift — while a present, drifted
    unit still does."""
    absent = ("jasper-voice", "jasper-aec-bridge")
    items, _, _ = _systemd_drift(
        oom={"jasper-mux": "0", **{u: "0" for u in absent}},
        actions={u: "none" for u in absent},
        load_map={u: "not-found" for u in absent},
    )
    assert [d.item for d in items] == ["jasper-mux OOMScoreAdjust"]


def test_live_oom_drift_is_named_apart_from_unit_file_drift():
    """A correct unit file with a stale running process is a different
    finding with a different remedy, so both are named."""
    unit_ok_live_drifted = _systemd_drift(
        live={_PID_MAP["jasper-camilla"]: "0"},
    )[0]
    assert [(d.item, d.got) for d in unit_ok_live_drifted] == [
        ("jasper-camilla oom_score_adj (live)", "0"),
    ]

    both = _systemd_drift(
        oom={"jasper-camilla": "0"},
        live={_PID_MAP["jasper-camilla"]: "0"},
    )[0]
    # The unit file is wrong, so its live value is not re-reported.
    assert [d.item for d in both] == ["jasper-camilla OOMScoreAdjust"]
    assert unit_ok_live_drifted[0].fix != both[0].fix


def test_openssh_listener_self_protection_is_not_live_drift():
    """OpenSSH keeps its privileged listener at -1000 whatever the unit
    says; the unit-file value is authoritative for ssh."""
    items, _, _ = _systemd_drift(live={_PID_MAP["ssh"]: "-1000"})
    assert items == []


def test_stopped_units_are_reported_as_skipped_not_drift():
    healthy_checked = _systemd_drift()[1]
    items, checked, notes = _systemd_drift(pids={"jasper-voice": "0"})
    assert items == []
    assert checked == healthy_checked - 1
    assert notes


@pytest.mark.parametrize("stdout", [None, "", "\n\n\n\n"])
def test_systemd_drift_skips_when_systemctl_cannot_answer(stdout):
    """Absent systemctl, and a present one answering nothing (no D-Bus, not
    booted with systemd, non-zero exit), are both unknown — not "every unit
    is installed at its systemd default", which fabricates drift on all."""
    def fake_run(cmd, **kwargs):
        if stdout is None:
            raise FileNotFoundError("systemctl not found")
        result = MagicMock()
        result.stdout = stdout
        return result

    with patch.object(doctor._shared, "_run", side_effect=fake_run):
        items, checked, notes = doctor_drift._systemd_drift()
    assert (items, checked) == ([], 0)
    assert notes


def test_unparseable_directive_value_is_disclosed_not_swallowed():
    items, _, notes = _systemd_drift(oom={"jasper-camilla": "not-a-number"})
    assert items == []
    assert notes


def test_degraded_directive_read_is_skipped_never_a_silent_pass():
    """A malformed batch read (length mismatch → None) must not be read as
    'no drift' on that directive."""
    healthy_checked = _systemd_drift()[1]
    healthy = _healthy_systemd_run()

    def fake_run(cmd, **kwargs):
        if cmd[3] != "StartLimitAction":
            return healthy(cmd, **kwargs)
        units = [c.rsplit(".", 1)[0] for c in cmd[5:]]
        result = MagicMock()
        result.stdout = "\n\n".join(["reboot"] * (len(units) - 1)) + "\n"
        return result

    def fake_read(self):
        return _LIVE_OK.get(str(self).split("/")[2], "0") + "\n"

    with patch.object(doctor._shared, "_run", side_effect=fake_run), \
         patch("pathlib.Path.read_text", fake_read):
        items, checked, notes = doctor_drift._systemd_drift()
    # The unreadable directive contributes neither drift nor a match, and the
    # skip is disclosed rather than swallowed.
    assert items == []
    assert checked == healthy_checked - len(_ACTION_WANT)
    assert notes


_INSTALLED_SYSCTL_CONF = """\
# JTS sysctl conf as written by install.sh
vm.swappiness = 100
vm.min_free_kbytes = 20296
"""


def _sysctl_drift(tmp_path, monkeypatch, conf, live):
    conf_path = tmp_path / "99-jts-vm.conf"
    if conf is not None:
        conf_path.write_text(conf)
    proc_vm = tmp_path / "vm"
    proc_vm.mkdir()
    for key, value in live.items():
        (proc_vm / key).write_text(value + "\n")
    monkeypatch.setattr(doctor_drift, "_JTS_SYSCTL_CONF", conf_path)
    monkeypatch.setattr(doctor_drift, "_PROC_SYS_VM", proc_vm)
    return doctor_drift._sysctl_drift()


def test_sysctl_expectations_come_from_the_installed_conf(tmp_path, monkeypatch):
    """install.sh computes vm.min_free_kbytes per-Pi (2% of RAM), so the
    conf — not a hardcoded number — is the expectation."""
    items, checked, _ = _sysctl_drift(
        tmp_path, monkeypatch, _INSTALLED_SYSCTL_CONF,
        {"swappiness": "100", "min_free_kbytes": "20296"},
    )
    assert (items, checked) == ([], 2)


def test_sysctl_drift_names_the_diverged_knob(tmp_path, monkeypatch):
    items, _, _ = _sysctl_drift(
        tmp_path, monkeypatch, _INSTALLED_SYSCTL_CONF,
        {"swappiness": "60", "min_free_kbytes": "20296"},
    )
    assert [(d.item, d.got, d.want) for d in items] == [
        ("vm.swappiness", "60", "100"),
    ]


def test_knob_this_kernel_does_not_expose_is_not_drift(tmp_path, monkeypatch):
    items, checked, _ = _sysctl_drift(
        tmp_path, monkeypatch, _INSTALLED_SYSCTL_CONF, {"swappiness": "100"},
    )
    assert (items, checked) == ([], 1)


def test_unsubstituted_template_placeholder_is_drift(tmp_path, monkeypatch):
    """install.sh's sed step failed, so the kernel kept its own default."""
    items, _, _ = _sysctl_drift(
        tmp_path, monkeypatch,
        _INSTALLED_SYSCTL_CONF.replace("20296", "__VM_MIN_FREE_KBYTES__"),
        {"swappiness": "100", "min_free_kbytes": "16384"},
    )
    assert [d.item for d in items] == ["vm.min_free_kbytes"]


@pytest.mark.parametrize("conf", [None, "# nothing installed here\n"])
def test_missing_or_empty_sysctl_conf_is_drift(tmp_path, monkeypatch, conf):
    items, _, _ = _sysctl_drift(tmp_path, monkeypatch, conf, {})
    assert len(items) == 1


@pytest.mark.parametrize(
    "live, expect_drift", [("1000", False), ("250", False), ("0", True)],
)
def test_mglru_only_the_kernel_default_is_drift(
    tmp_path, monkeypatch, live, expect_drift,
):
    """A non-zero value that is not ours is an operator override, not drift."""
    knob = tmp_path / "min_ttl_ms"
    knob.write_text(live + "\n")
    monkeypatch.setattr(doctor_drift, "_MGLRU_MIN_TTL", knob)
    items, checked, notes = doctor_drift._mglru_drift()
    assert (bool(items), checked, notes) == (expect_drift, 1, [])


def test_mglru_absent_on_kernels_without_it(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor_drift, "_MGLRU_MIN_TTL", tmp_path / "absent")
    items, checked, notes = doctor_drift._mglru_drift()
    assert (items, checked) == ([], 0)
    assert notes


def test_mglru_read_failure_is_disclosed_not_reported_as_drift(
    tmp_path, monkeypatch,
):
    """Re-running tmpfiles cannot fix a knob we could not read, so a read
    failure must not carry that remedy."""
    knob = tmp_path / "min_ttl_ms"
    knob.write_text("1000\n")
    monkeypatch.setattr(doctor_drift, "_MGLRU_MIN_TTL", knob)

    def boom(self, *a, **kw):
        raise OSError("EACCES")

    monkeypatch.setattr(Path, "read_text", boom)
    items, checked, notes = doctor_drift._mglru_drift()
    assert (items, checked) == ([], 0)
    assert notes


def test_the_check_aggregates_every_source(monkeypatch):
    """The registered check must reach all three readers — a dropped source
    is a whole class of drift going unreported."""
    captured = {}

    def source(item, checked, note):
        return lambda: (
            [doctor_drift.DriftItem(item, "got", "want", "fix")], checked, [note],
        )

    monkeypatch.setattr(doctor_drift, "_systemd_drift", source("a", 1, "n1"))
    monkeypatch.setattr(doctor_drift, "_sysctl_drift", source("b", 2, "n2"))
    monkeypatch.setattr(doctor_drift, "_mglru_drift", source("c", 4, "n3"))
    monkeypatch.setattr(
        doctor_drift, "_classify_drift",
        lambda drift, checked, notes: captured.update(
            drift=drift, checked=checked, notes=notes,
        ),
    )

    doctor_drift.check_installed_settings_drift()

    assert [d.item for d in captured["drift"]] == ["a", "b", "c"]
    assert captured["checked"] == 7
    assert captured["notes"] == ["n1", "n2", "n3"]


def test_drift_verdict_is_warn_only_when_something_drifted():
    assert doctor_drift._classify_drift([], 12, []).status == "ok"
    drifted = doctor_drift._classify_drift(
        [doctor_drift.DriftItem("vm.swappiness", "60", "100", "fix")], 12, [],
    )
    assert drifted.status == "warn"
    assert drifted.reason == doctor_drift.REASON_SETTINGS_DRIFTED


def test_systemctl_show_property_parses_double_newline_separator():
    """`systemctl show -p X --value u1 u2` separates values with a blank
    line, not a single newline (verified on the Pi)."""
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.stdout = "1001\n\n1002\n\n1003\n"
        return result

    with patch.object(doctor._shared, "_run", side_effect=fake_run):
        result = doctor._systemctl_show_property(
            "MainPID", ["unit-a", "unit-b", "unit-c"],
        )
    assert result == ["1001", "1002", "1003"]


def test_systemctl_show_property_handles_single_unit():
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.stdout = "1234\n"
        return result

    with patch.object(doctor._shared, "_run", side_effect=fake_run):
        result = doctor._systemctl_show_property("MainPID", ["unit-a"])
    assert result == ["1234"]


def test_systemctl_show_property_handles_empty_values():
    def fake_run(cmd, **kwargs):
        result = MagicMock()
        result.stdout = "\n\n\n\n\n"
        return result

    with patch.object(doctor._shared, "_run", side_effect=fake_run):
        result = doctor._systemctl_show_property(
            "MainPID", ["unit-a", "unit-b", "unit-c"],
        )
    assert result is not None
