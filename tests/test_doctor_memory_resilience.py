# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Coverage for the memory-resilience doctor checks
(docs/HANDOFF-resilience.md).

They verify the configs installed by `migrate_memory_resilience` (in
deploy/lib/install/memory-resilience.sh, sourced by deploy/install.sh)
are actually applied at runtime. The check functions all read kernel
interfaces (/proc, /sys, /sys/fs/cgroup), so we mock those.

The bar: each check should (a) work on Linux where the paths
exist, (b) skip gracefully on dev hosts where they don't,
(c) emit useful detail when drift is found.
"""
from __future__ import annotations

import io
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jasper.cli import doctor
from jasper.cli.doctor import drift as doctor_drift
from jasper.cli.doctor import memory as doctor_memory
from jasper.conversation_history import (
    CAPTURE_ENABLED_ENV,
    DEFAULT_RETENTION_DAYS,
    DEFAULT_RETENTION_MAX_ROWS,
    ConversationStore,
    ConversationTurn,
    DB_PATH_ENV,
    RETENTION_DAYS_ENV,
    make_turn_id,
)


ROOT = Path(__file__).resolve().parents[1]


# --- check_ram -----------------------------------------------------------


def test_ram_warns_on_small_full_install():
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 426076,  # ~416 MB: too small for the full brain stack
    })), patch(
        "jasper.cli.doctor.memory.read_install_profile", return_value="full",
    ):
        r = doctor.check_ram()

    assert r.status == "warn"
    assert "recommend 2GB Pi 5" in r.detail


def test_ram_ok_on_small_streambox_board():
    # Streambox is the deliberately-light tier a Zero 2 W resolves to, so the
    # full-speaker "recommend 2GB Pi 5" board-size warn is a false positive
    # there. Live pressure is still covered SKU-agnostically by
    # check_memory_headroom.
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 426076,  # ~416 MB: a Zero 2 W board running streambox
    })), patch(
        "jasper.cli.doctor.memory.read_install_profile",
        return_value="streambox",
    ):
        r = doctor.check_ram()

    assert r.status == "ok"
    assert "streambox tier" in r.detail
    assert "recommend 2GB Pi 5" not in r.detail


def test_ram_warn_survives_install_profile_read_failure():
    # A marker-read glitch must NOT silently suppress the warn on a real
    # full speaker — _install_profile_is_streambox fails toward False.
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 426076,
    })), patch(
        "jasper.cli.doctor.memory.read_install_profile",
        side_effect=OSError("marker unreadable"),
    ):
        r = doctor.check_ram()

    assert r.status == "warn"
    assert "recommend 2GB Pi 5" in r.detail


# --- check_memory_headroom -----------------------------------------------


def _mock_meminfo(values: dict[str, int]):
    """Build a mock for `open('/proc/meminfo')` returning lines in
    the kernel's canonical "Field: NNN kB" format."""
    lines = [f"{k}: {v} kB\n" for k, v in values.items()]
    m = MagicMock()
    m.__enter__.return_value = io.StringIO("".join(lines))
    m.__exit__.return_value = None
    return m


def test_memory_headroom_healthy_on_1gb():
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 1014768,    # ~991 MB
        "MemAvailable": 300000,  # ~293 MB
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "ok"
    assert "MB available" in r.detail


def test_memory_headroom_warn_below_100mb_on_1gb():
    """1 GB Pi: warn threshold is max(100 MB, 10% × 991 MB ≈ 99 MB) = 100 MB."""
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 1014768,
        "MemAvailable": 80000,   # ~78 MB, below 100 MB warn threshold
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "warn"
    assert "tight" in r.detail


def test_memory_headroom_fail_below_30mb_on_1gb():
    """1 GB Pi: fail threshold is max(30 MB, 3% × 991 MB ≈ 30 MB) = 30 MB."""
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 1014768,
        "MemAvailable": 20000,  # ~19 MB, below 30 MB fail threshold
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "fail"
    assert "imminent" in r.detail


def test_memory_headroom_2gb_pi_uses_proportional_thresholds():
    """2 GB Pi: warn at max(100 MB, 10% × 2 GB = 200 MB) = 200 MB.
    78 MB on a 2 GB Pi IS dangerously tight (3.8% headroom) — the
    old check missed this because it gated on total_mb < 1500."""
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 2097152,    # 2 GB
        "MemAvailable": 80000,   # 78 MB — way below 200 MB warn
    })):
        r = doctor.check_memory_headroom()
    # Below 3% (60 MB) is fail; 78 MB is in warn territory.
    # 78 MB is > 60 MB so it's warn, not fail.
    assert r.status == "warn"


def test_memory_headroom_8gb_pi_uses_proportional_thresholds():
    """8 GB Pi: warn at 800 MB (10%), fail at 240 MB (3%). 500 MB
    available is tight relative to the box, so warn fires."""
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 8388608,    # 8 GB
        "MemAvailable": 500000,  # ~488 MB — below 800 MB warn threshold
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "warn"
    # And NOT below fail (240 MB)
    assert "imminent" not in r.detail


def test_memory_headroom_8gb_pi_with_healthy_available():
    """8 GB Pi with 2 GB available is healthy (25%)."""
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 8388608,
        "MemAvailable": 2097152,  # 2 GB available
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "ok"


def test_memory_headroom_16gb_pi_fail_threshold_scales():
    """16 GB Pi: fail threshold is 3% = 480 MB. 400 MB available
    on a 16 GB box means something is wrong — should fail, not just warn."""
    with patch("builtins.open", return_value=_mock_meminfo({
        "MemTotal": 16777216,
        "MemAvailable": 400000,  # ~390 MB — below fail threshold of 480 MB
    })):
        r = doctor.check_memory_headroom()
    assert r.status == "fail"


def test_memory_headroom_handles_meminfo_read_failure():
    with patch("builtins.open", side_effect=OSError("permission denied")):
        r = doctor.check_memory_headroom()
    assert r.status == "warn"


# --- check_zram_size_ratio -----------------------------------------------


def _zram_test_mocks(zram_bytes: int, rpi_swap_installed: bool = True):
    """Mock both Path.read_text() (for /sys/block/zram0/disksize) and
    Path.exists() (for /etc/rpi/swap.conf)."""
    def fake_read(self):
        s = str(self)
        if s == "/sys/block/zram0/disksize":
            return str(zram_bytes)
        raise FileNotFoundError(s)
    def fake_exists(self):
        s = str(self)
        if s == "/etc/rpi/swap.conf":
            return rpi_swap_installed
        return False
    return fake_read, fake_exists


def test_zram_size_warns_when_over_60pct_of_ram_with_rpi_swap():
    """rpi-swap installed + zram > 60% of RAM → actionable warn:
    reboot to apply the JTS drop-in."""
    fake_read, fake_exists = _zram_test_mocks(
        zram_bytes=1014767616,  # ~990 MB zram
        rpi_swap_installed=True,
    )
    with patch("pathlib.Path.read_text", fake_read), \
         patch("pathlib.Path.exists", fake_exists), \
         patch("builtins.open", return_value=_mock_meminfo({
             "MemTotal": 1014768,
         })):
        r = doctor.check_zram_size_ratio()
    assert r.status == "warn"
    assert "old default" in r.detail
    assert "reboot" in r.detail


def test_zram_size_skips_when_rpi_swap_not_installed():
    """Bookworm / non-Trixie / forked-onto-another-distro: rpi-swap
    isn't installed, so JTS's drop-in is inert — no actionable fix
    from the operator's side. Skip with ok rather than warn forever."""
    fake_read, fake_exists = _zram_test_mocks(
        zram_bytes=1014767616,  # ~990 MB zram, 99% of RAM
        rpi_swap_installed=False,
    )
    with patch("pathlib.Path.read_text", fake_read), \
         patch("pathlib.Path.exists", fake_exists), \
         patch("builtins.open", return_value=_mock_meminfo({
             "MemTotal": 1014768,
         })):
        r = doctor.check_zram_size_ratio()
    # NOT warn — no actionable resolution. Operator can't fix from
    # this side without changing distros.
    assert r.status == "ok"
    assert "rpi-swap not installed" in r.detail or "different zram package" in r.detail


def test_zram_size_ok_at_50pct():
    fake_read, fake_exists = _zram_test_mocks(
        zram_bytes=520 * 1024 * 1024,  # ~520 MB zram
        rpi_swap_installed=True,
    )
    with patch("pathlib.Path.read_text", fake_read), \
         patch("pathlib.Path.exists", fake_exists), \
         patch("builtins.open", return_value=_mock_meminfo({
             "MemTotal": 1014768,
         })):
        r = doctor.check_zram_size_ratio()
    assert r.status == "ok"


def test_zram_size_no_zram_device():
    """Dev host / older RPi OS — no /sys/block/zram0 — skip cleanly."""
    with patch("pathlib.Path.read_text",
               side_effect=FileNotFoundError):
        r = doctor.check_zram_size_ratio()
    assert r.status == "ok"
    assert "rpi-swap not active" in r.detail


# --- check_installed_settings_drift ---------------------------------------


def _make_systemctl_show_run(
    property_maps: dict[str, dict[str, str]],
    *,
    defaults: dict[str, str],
    load_map: dict[str, str] | None = None,
):
    """Double for the batched ``systemctl show --value`` wire format.

    Real systemctl separates per-unit values with a blank line (``\n\n``)
    when several units are requested with ``--value``.
    """

    def fake_run(cmd, **kwargs):
        prop = cmd[3]
        units = [c.rsplit(".", 1)[0] for c in cmd[5:]]
        if prop == "LoadState":
            values = [(load_map or {}).get(unit, "loaded") for unit in units]
        else:
            values_for_property = property_maps.get(prop, {})
            default = defaults.get(prop, "")
            values = [values_for_property.get(unit, default) for unit in units]
        result = MagicMock()
        result.stdout = "\n\n".join(values) + "\n" if values else "\n"
        return result

    return fake_run


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
    assert "vm.swappiness" in drifted.detail


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
    assert len(result) == 3


# --- Stage 2 audio-slice checks ------------------------------------------


def test_cgroup_memory_enabled_when_controller_listed():
    """memory cgroup is on → check passes."""
    fake_read = MagicMock(return_value="cpu io memory pids\n")
    fake_exists = MagicMock(return_value=True)
    with patch("pathlib.Path.exists", fake_exists), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_cgroup_memory_enabled()
    assert r.status == "ok"
    assert "controller enabled" in r.detail


def test_cgroup_memory_disabled_fails_loudly():
    """memory NOT in cgroup.controllers → audio-slice MemorySwapMax=0
    is a no-op. This is the exact silent-failure trap we want the
    doctor to surface, so it's FAIL (not warn) — the audio protection
    is gone."""
    fake_read = MagicMock(return_value="cpu io pids\n")  # no memory
    fake_exists = MagicMock(return_value=True)
    with patch("pathlib.Path.exists", fake_exists), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_cgroup_memory_enabled()
    assert r.status == "fail"
    assert "NOT enabled" in r.detail
    assert "Reboot" in r.detail


def test_cgroup_memory_skips_on_dev_host():
    """No /sys/fs/cgroup → not Linux, skip cleanly."""
    fake_exists = MagicMock(return_value=False)
    with patch("pathlib.Path.exists", fake_exists):
        r = doctor.check_cgroup_memory_enabled()
    assert r.status == "ok"
    assert "not Linux" in r.detail


def test_audio_path_no_swap_covers_every_protected_slice_unit():
    systemd_dir = ROOT / "deploy/systemd"
    expected: set[str] = set()
    for service in systemd_dir.glob("*.service"):
        directives = {
            line.strip()
            for line in service.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        if directives & {"Slice=jts-audio.slice", "Slice=jts-mic.slice"}:
            expected.add(service.stem)
    for drop_in in systemd_dir.glob("*.service.d/*.conf"):
        directives = {
            line.strip()
            for line in drop_in.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        if directives & {"Slice=jts-audio.slice", "Slice=jts-mic.slice"}:
            expected.add(drop_in.parent.name.removesuffix(".service.d"))

    assert set(doctor._AUDIO_PATH_UNITS) == expected
    assert len(doctor._AUDIO_PATH_UNITS) == len(set(doctor._AUDIO_PATH_UNITS))


def test_audio_path_no_swap_happy_path():
    """All audio-path daemons running with VmSwap=0 (or very low):
    happy path = ok."""
    def fake_read(self):
        # All audio daemons have VmSwap=0 (or tiny transient)
        return (
            "Name:\tfake\n"
            "VmRSS:\t100000 kB\n"
            "VmSwap:\t0 kB\n"
        )

    pids = [str(2001 + index) for index, _unit in enumerate(doctor._AUDIO_PATH_UNITS)]
    with patch.object(doctor_memory, "_systemctl_show_property", return_value=pids), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_audio_path_no_swap()
    assert r.status == "ok"
    assert "swap-free" in r.detail


def test_audio_path_no_swap_warns_on_42mb_swap():
    """Reproduce the 2026-05-24 failure-mode signature: aec-bridge
    with 42 MB of VmSwap. Should warn loudly with the daemon name
    + amount."""
    def fake_read(self):
        pid_str = str(self).split("/")[2]
        # jasper-aec-bridge (pid 2003) has 42 MB swapped (the
        # 2026-05-24 signature). Others are clean.
        if pid_str == "2003":
            return "Name:\tfoo\nVmRSS:\t100000 kB\nVmSwap:\t43056 kB\n"
        return "Name:\tfoo\nVmRSS:\t100000 kB\nVmSwap:\t0 kB\n"

    pids = [
        "2003" if unit == "jasper-aec-bridge" else str(3000 + index)
        for index, unit in enumerate(doctor._AUDIO_PATH_UNITS)
    ]
    with patch.object(doctor_memory, "_systemctl_show_property", return_value=pids), \
         patch("pathlib.Path.read_text", fake_read):
        r = doctor.check_audio_path_no_swap()
    assert r.status == "warn"
    assert "jasper-aec-bridge" in r.detail
    assert "43056" in r.detail
    assert "music may glitch" in r.detail


def test_audio_path_no_swap_dev_host():
    """No systemctl → all daemons "not running", check still passes
    cleanly (doesn't crash)."""
    with patch.object(doctor_memory, "_systemctl_show_property", return_value=None):
        r = doctor.check_audio_path_no_swap()
    assert r.status == "ok"
    assert "not running" in r.detail


# --- check_disk_space (disk-pressure observability) ----------------------


def _fake_statvfs(*, total_bytes: int, free_bytes: int, frsize: int = 4096):
    """Build an os.statvfs replacement returning a result with the given
    total/free byte figures. Mirrors the kernel's statvfs_result shape
    (f_blocks/f_bavail in f_frsize units) closely enough for the check."""
    from types import SimpleNamespace

    blocks = total_bytes // frsize
    avail = free_bytes // frsize

    def fake(path):
        return SimpleNamespace(f_blocks=blocks, f_bavail=avail, f_frsize=frsize)

    return fake


def test_disk_warn_percent_default():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JASPER_DISK_WARN_PERCENT", None)
        assert doctor.memory._disk_warn_percent() == 85


def test_disk_warn_percent_custom_value_honored():
    with patch.dict(os.environ, {"JASPER_DISK_WARN_PERCENT": "70"}):
        assert doctor.memory._disk_warn_percent() == 70


def test_disk_warn_percent_out_of_range_falls_back():
    """A warn >= the fixed 95% fail line, <= 0, or unparseable must snap
    back to the 85% default — a fat-fingered env line can't disable the
    warning or invert the warn/fail band."""
    for bad in ("0", "-5", "95", "99", "notanumber"):
        with patch.dict(os.environ, {"JASPER_DISK_WARN_PERCENT": bad}):
            assert doctor.memory._disk_warn_percent() == 85, bad


def test_disk_space_ok_when_plenty_free():
    fake = _fake_statvfs(
        total_bytes=64 * 1024**3,   # 64 GiB card
        free_bytes=40 * 1024**3,    # 40 GiB free → ~37% used
    )
    with patch.object(doctor.memory.os, "statvfs", fake):
        r = doctor.check_disk_space()
    assert r.status == "ok"
    assert "37% used" in r.detail
    assert "40.0 GiB free" in r.detail
    assert r.detail.startswith("/:")


def test_disk_space_warns_over_85_percent():
    fake = _fake_statvfs(
        total_bytes=32 * 1024**3,
        free_bytes=int(0.12 * 32 * 1024**3),  # 12% free → 88% used
    )
    with patch.object(doctor.memory.os, "statvfs", fake), \
         patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JASPER_DISK_WARN_PERCENT", None)
        r = doctor.check_disk_space()
    assert r.status == "warn"
    assert "88% used" in r.detail
    assert "85% warn threshold" in r.detail


def test_disk_space_fails_over_95_percent():
    fake = _fake_statvfs(
        total_bytes=16 * 1024**3,
        free_bytes=int(0.03 * 16 * 1024**3),  # 3% free → 97% used
    )
    with patch.object(doctor.memory.os, "statvfs", fake):
        r = doctor.check_disk_space()
    assert r.status == "fail"
    assert "97% used" in r.detail
    assert "corruption" in r.detail  # the SD-corruption rationale


def test_disk_space_fail_beats_a_high_custom_warn():
    """Even with the warn knob set above the fail line (which snaps back
    to 85), a 96%-full disk still FAILs — fail always takes precedence."""
    fake = _fake_statvfs(
        total_bytes=16 * 1024**3,
        free_bytes=int(0.04 * 16 * 1024**3),  # 96% used
    )
    with patch.object(doctor.memory.os, "statvfs", fake), \
         patch.dict(os.environ, {"JASPER_DISK_WARN_PERCENT": "99"}):
        r = doctor.check_disk_space()
    assert r.status == "fail"


def test_disk_space_skips_when_statvfs_unavailable():
    """Non-POSIX dev host (no os.statvfs) → skip cleanly as ok, same
    posture as the /proc and /sys checks."""
    # getattr(os, "statvfs", None) must return None.
    with patch.object(doctor.memory.os, "statvfs", None, create=True):
        # Ensure the attribute lookup yields None even though the real
        # module has it: patch sets it to None, getattr returns None.
        r = doctor.check_disk_space()
    assert r.status == "ok"
    assert "skipped" in r.detail


def test_disk_space_warns_on_statvfs_oserror():
    def boom(path):
        raise OSError("nope")

    with patch.object(doctor.memory.os, "statvfs", boom):
        r = doctor.check_disk_space()
    assert r.status == "warn"
    assert "couldn't statvfs" in r.detail


def test_disk_space_skips_zero_sized_fs():
    fake = _fake_statvfs(total_bytes=0, free_bytes=0)
    with patch.object(doctor.memory.os, "statvfs", fake):
        r = doctor.check_disk_space()
    assert r.status == "ok"
    assert "zero-sized" in r.detail


# --- _bounded_dir_size + storage checks ----------------------------------


def test_bounded_dir_size_sums_files(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"x" * 100)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.bin").write_bytes(b"y" * 250)
    total, truncated = doctor.memory._bounded_dir_size(tmp_path)
    assert total == 350
    assert truncated is False


def test_bounded_dir_size_caps_entries(tmp_path, monkeypatch):
    """The entry cap must stop a runaway walk and flag truncation rather
    than examining an unbounded number of dir entries on a 1 GB Pi."""
    for i in range(10):
        (tmp_path / f"f{i}.bin").write_bytes(b"z" * 10)
    monkeypatch.setattr(doctor.memory, "_STORAGE_WALK_MAX_ENTRIES", 3)
    total, truncated = doctor.memory._bounded_dir_size(tmp_path)
    assert truncated is True
    # We stopped early, so the total is a floor — strictly less than the
    # full 100 bytes had we walked everything.
    assert total < 100


def test_bounded_dir_size_caps_depth(tmp_path, monkeypatch):
    """Deeply nested dirs beyond the depth cap are not descended into;
    their contents are excluded and truncation is flagged."""
    deep = tmp_path
    for i in range(5):
        deep = deep / f"d{i}"
        deep.mkdir()
    (deep / "buried.bin").write_bytes(b"q" * 999)
    # Surface file that IS counted.
    (tmp_path / "top.bin").write_bytes(b"a" * 5)
    monkeypatch.setattr(doctor.memory, "_STORAGE_WALK_MAX_DEPTH", 2)
    total, truncated = doctor.memory._bounded_dir_size(tmp_path)
    assert truncated is True
    assert total == 5  # only the surface file, the buried one is past the cap


def test_bounded_dir_size_missing_dir_is_zero(tmp_path):
    total, truncated = doctor.memory._bounded_dir_size(tmp_path / "nope")
    assert total == 0
    assert truncated is False


def test_correction_storage_ok_below_threshold(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "small.wav").write_bytes(b"0" * 1024)
    with patch.dict(os.environ, {
        "JASPER_CORRECTION_SESSIONS_DIR": str(sessions),
    }):
        r = doctor.check_correction_storage()
    assert r.status == "ok"
    assert str(sessions) in r.detail


def test_correction_storage_warns_over_threshold(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "big.wav").write_bytes(b"0" * 4096)
    with patch.dict(os.environ, {
        "JASPER_CORRECTION_SESSIONS_DIR": str(sessions),
        "JASPER_CORRECTION_STORAGE_WARN_BYTES": "1024",  # 1 KiB threshold
    }):
        r = doctor.check_correction_storage()
    assert r.status == "warn"
    assert "warn threshold" in r.detail
    assert "JASPER_CORRECTION_STORAGE_WARN_BYTES" in r.detail


def test_correction_storage_absent_dir_is_ok(tmp_path):
    with patch.dict(os.environ, {
        "JASPER_CORRECTION_SESSIONS_DIR": str(tmp_path / "never_created"),
    }):
        r = doctor.check_correction_storage()
    assert r.status == "ok"
    assert "absent" in r.detail


def test_wake_events_storage_warns_over_threshold(tmp_path):
    wake = tmp_path / "wake-events"
    wake.mkdir()
    (wake / "clip.wav").write_bytes(b"0" * 8192)
    with patch.dict(os.environ, {
        "JASPER_WAKE_EVENTS_DIR": str(wake),
        "JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES": "2048",
    }):
        r = doctor.check_wake_events_storage()
    assert r.status == "warn"
    assert "JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES" in r.detail


def test_wake_events_storage_ok_below_default_threshold(tmp_path):
    """A healthy ring (well under the 1.3 GiB default) never warns."""
    wake = tmp_path / "wake-events"
    wake.mkdir()
    (wake / "clip.wav").write_bytes(b"0" * 1024)
    with patch.dict(os.environ, {
        "JASPER_WAKE_EVENTS_DIR": str(wake),
    }):
        os.environ.pop("JASPER_WAKE_EVENTS_STORAGE_WARN_BYTES", None)
        r = doctor.check_wake_events_storage()
    assert r.status == "ok"


def test_storage_warn_bytes_fallback_on_bad_value():
    assert doctor.memory._storage_warn_bytes("X_UNSET_KNOB_", 4242) == 4242
    with patch.dict(os.environ, {"X_BAD_KNOB_": "notint"}):
        assert doctor.memory._storage_warn_bytes("X_BAD_KNOB_", 99) == 99
    with patch.dict(os.environ, {"X_NEG_KNOB_": "-1"}):
        assert doctor.memory._storage_warn_bytes("X_NEG_KNOB_", 7) == 7


# --- /state.resilience.disk snapshot (state_aggregate) -------------------


def test_disk_snapshot_shape():
    from jasper.control import state_aggregate

    fake = _fake_statvfs(
        total_bytes=64 * 1024**3,
        free_bytes=16 * 1024**3,  # 25% free → 75% used
    )
    with patch.object(state_aggregate.os, "statvfs", fake):
        snap = state_aggregate._disk_snapshot("/")
    assert snap == {
        "path": "/",
        "percent_used": 75,
        "free_gib": 16.0,
        "total_gib": 64.0,
    }


def test_disk_snapshot_none_on_oserror():
    from jasper.control import state_aggregate

    def boom(path):
        raise OSError("denied")

    with patch.object(state_aggregate.os, "statvfs", boom):
        assert state_aggregate._disk_snapshot("/") is None


def test_disk_snapshot_none_when_statvfs_unavailable():
    from jasper.control import state_aggregate

    with patch.object(state_aggregate.os, "statvfs", None, create=True):
        assert state_aggregate._disk_snapshot("/") is None


def test_disk_snapshot_none_on_zero_total():
    from jasper.control import state_aggregate

    fake = _fake_statvfs(total_bytes=0, free_bytes=0)
    with patch.object(state_aggregate.os, "statvfs", fake):
        assert state_aggregate._disk_snapshot("/") is None


# --- /state.chat snapshot ------------------------------------------------


def test_conversation_history_state_reads_store_summary(monkeypatch, tmp_path):
    from jasper.control import state_aggregate

    db_path = tmp_path / "conversation_history.db"
    settings_path = tmp_path / "conversation_history.env"
    settings_path.write_text(
        "\n".join([
            f"{CAPTURE_ENABLED_ENV}=1",
            f"{DB_PATH_ENV}={db_path}",
            f"{RETENTION_DAYS_ENV}=30",
        ])
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings_path))
    store = ConversationStore(str(db_path))
    assert store.add(
        ConversationTurn(
            id=make_turn_id("2026-06-19T20:15:00Z", 1),
            ts_utc="2026-06-19T20:15:00Z",
            provider="gemini",
            user_text="hello",
            assistant_text="hi",
            tool_calls_json=None,
            data_json=None,
            session_id=1,
        ),
    )
    store.close()

    snap = state_aggregate._conversation_history_state()

    assert snap is not None
    assert snap["capture_enabled"] is True
    assert snap["turn_count"] == 1
    assert snap["last_write_age_seconds"] is not None
    # max_rows is absent from the env file, so it resolves to the code
    # default rather than disabling the row-count guard.
    assert snap["retention"] == {"days": 30, "max_rows": DEFAULT_RETENTION_MAX_ROWS}


def test_conversation_history_state_disabled_missing_db_is_not_unavailable(
    monkeypatch, tmp_path,
):
    from jasper.control import state_aggregate

    settings_path = tmp_path / "conversation_history.env"
    settings_path.write_text(f"{CAPTURE_ENABLED_ENV}=0\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings_path))

    # Neither retention var is set, so both bounds resolve to the code
    # defaults that keep the store bounded out of the box.
    assert state_aggregate._conversation_history_state() == {
        "capture_enabled": False,
        "turn_count": None,
        "last_write_age_seconds": None,
        "retention": {
            "days": DEFAULT_RETENTION_DAYS,
            "max_rows": DEFAULT_RETENTION_MAX_ROWS,
        },
    }


def test_conversation_history_state_enabled_missing_db_is_null(
    monkeypatch, tmp_path,
):
    from jasper.control import state_aggregate

    db_path = tmp_path / "missing.db"
    settings_path = tmp_path / "conversation_history.env"
    settings_path.write_text(
        f"{CAPTURE_ENABLED_ENV}=1\n{DB_PATH_ENV}={db_path}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JASPER_CONVERSATION_HISTORY_FILE", str(settings_path))

    assert state_aggregate._conversation_history_state() is None
    assert db_path.exists() is False
