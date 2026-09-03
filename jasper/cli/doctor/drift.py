# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor check — installed-settings drift.

One table-driven check for the settings ``install.sh`` writes and the
kernel or systemd then reports back. A table row names a setting, where
its expected value comes from, and where the live value is read; the
check names every row whose live value disagrees and stays advisory
(``warn``) — drift means an install did not land or something edited it
afterwards, not that the box is broken right now.

A row whose setting does not exist on this box is not drift and is not
counted: a profile that never installs a unit (a streambox omits the
voice/AEC stack), a kernel that does not expose a knob, and a host with
no systemd all skip.

Rationale for the values lives with their owners: ``jasper/_oom_adj.py``
(the OOM ladder, shared with install.sh).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..._oom_adj import EXPECTED as _EXPECTED_OOM_ADJ
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import CheckResult

_LABEL = "installed settings"

# Machine-stable code naming the one branch that turns this check non-ok
# (AGENTS.md: tests pin status + reason, never detail prose).
REASON_SETTINGS_DRIFTED = "settings_drifted"

_INSTALL_FIX = "re-run install.sh"
_RESTART_FIX = "`systemctl restart <unit>` to apply now"
_SYSCTL_FIX = "`sudo sysctl --system`"
_TMPFILES_FIX = "`sudo systemd-tmpfiles --create /etc/tmpfiles.d/jts-mglru.conf`"


@dataclass(frozen=True)
class DriftItem:
    """One setting whose live value disagrees with what was installed."""

    item: str
    got: str
    want: str
    fix: str


# systemd directives install.sh writes into the unit files, keyed by the
# property name `systemctl show` reports. Without the reboot ladder a box is
# back to Tier 5's "PID 1 alive but userspace dead" gap.
#
# jasper-camilla is the one unit off that ladder: restart-limit exhaustion
# runs a forensics/recovery oneshot instead, because its observed failure
# class is ALSA ownership churn where holder evidence matters and the Pi
# should stay reachable.
#
# StartLimitAction/OnFailure stay hand-copied rather than read from each
# unit's installed file (`systemctl show -p FragmentPath` + a parse, mirroring
# the sysctl/MGLRU rows): FragmentPath resolves to the unit file on
# `/etc/systemd/system` install.sh actually writes, so the read is mechanically
# a single memoized batch property plus small local file reads — but that path
# cannot be exercised or verified without a real box (no Pi in this
# environment), and the values it would source (the reboot-escalation ladder)
# are resilience-tier. Revisit with hardware access rather than guessing.
_UNIT_DIRECTIVES: dict[str, dict[str, str]] = {
    "OOMScoreAdjust": {u: str(v) for u, v in _EXPECTED_OOM_ADJ.items()},
    "StartLimitAction": {
        "jasper-outputd": "reboot",
        "jasper-camilla": "none",
        "jasper-aec-bridge": "reboot",
        "jasper-voice": "reboot",
        "jasper-control": "reboot",
    },
    "OnFailure": {"jasper-camilla": "jasper-camilla-recover.service"},
}

# systemd reports OnFailure as a space-separated unit LIST, so its row is
# a membership test rather than equality.
_LIST_VALUED_DIRECTIVES = frozenset({"OnFailure"})

_JTS_SYSCTL_CONF = Path("/etc/sysctl.d/99-jts-vm.conf")
_PROC_SYS_VM = Path("/proc/sys/vm")
_MGLRU_MIN_TTL = Path("/sys/kernel/mm/lru_gen/min_ttl_ms")
_MGLRU_TMPFILES_CONF = Path("/etc/tmpfiles.d/jts-mglru.conf")


def _mglru_installed_ms(text: str) -> str | None:
    """The ``min_ttl_ms`` argument off the conf's ``w-`` line, or None when
    the conf carries no such line — read fresh rather than hardcoded so a
    future retune of the knob needs one edit (the conf), not two."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) >= 2 and parts[0] in ("w", "w-"):
            return parts[-1]
    return None


def _directive_value(directive: str, raw: str) -> str | None:
    """The live directive value in the table's shape, or None when systemd
    reported something this row cannot compare (so it is not drift)."""
    text = (raw or "").strip()
    if directive == "OOMScoreAdjust":
        # systemd reports 0 — its own default — when the directive is absent.
        try:
            return str(int(text or "0"))
        except ValueError:
            return None
    if directive == "StartLimitAction":
        return text.lower() or "none"
    return text or "none"


def _drift_installed_units(units: list[str]) -> set[str] | None:
    """Subset of ``units`` (bare drift-table names, no ``.service`` suffix)
    whose unit file is actually installed.

    "Installed" means ``LoadState`` is neither ``not-found`` (no unit
    file) nor ``masked`` (symlinked to /dev/null) — i.e. an effective
    unit file exists to carry a directive. A unit that exists but is
    broken (``error`` / ``bad-setting``) is intentionally KEPT so its
    drift still surfaces rather than being silently hidden.

    Returns ``None`` when the per-run unit-state evidence is unavailable
    (systemctl absent, or a dev host) — not "everything is installed":
    treating that as installed would read every unit's directive as its
    systemd default and fabricate drift on all of them. Callers fall
    through to their existing "skipped" path.

    Why: drift checks verify a PROPERTY of a unit. A unit a profile never
    installs — e.g. the voice/AEC stack on a streambox — has no property
    to drift, and ``systemctl show`` reports its directives as defaults,
    which would read as false drift. Callers filter their expected set to
    this set so the check stays correct on every install profile without
    hard-coding which units each tier runs.
    """
    installed: set[str] = set()
    for u in units:
        state = evidence.unit_state(f"{u}.service")
        if state is None:
            return None
        if state.get("load_state") not in ("not-found", "masked"):
            installed.add(u)
    return installed


def _systemd_drift() -> tuple[list[DriftItem], int, list[str]]:
    """Unit-file directive drift, plus the OOM ladder as the kernel holds it.

    Both shapes are named because they need different remedies: a unit-file
    value that disagrees survives the next restart, while a live
    ``/proc/<pid>/oom_score_adj`` that disagrees under a correct unit file is
    a process started before that file landed.
    """
    table_units = sorted({u for row in _UNIT_DIRECTIVES.values() for u in row})
    installed = _drift_installed_units(table_units)
    if installed is None:
        return [], 0, ["systemctl unavailable"]

    drift: list[DriftItem] = []
    notes: list[str] = []
    checked = 0
    # Units whose OOMScoreAdjust unit-file value is right, so their live
    # value is worth reading. ssh is exempt: OpenSSH's privileged listener
    # keeps itself at -1000 whatever the unit says, while accepted sessions
    # inherit the unit's moderate value.
    live_want: dict[str, str] = {}
    for directive, expected in _UNIT_DIRECTIVES.items():
        units = [u for u in expected if u in installed]
        if not units:
            continue
        values = evidence.unit_property(
            directive, tuple(f"{u}.service" for u in units),
        )
        if values is None:
            notes.append(f"{directive} unreadable")
            continue
        for unit, raw in zip(units, values):
            got = _directive_value(directive, raw)
            if got is None:
                notes.append(f"{unit} {directive} unparseable")
                continue
            want = expected[unit]
            checked += 1
            if directive in _LIST_VALUED_DIRECTIVES:
                matched = want in got.split()
            else:
                matched = got == want
            if not matched:
                drift.append(
                    DriftItem(f"{unit} {directive}", got, want, _INSTALL_FIX)
                )
            elif directive == "OOMScoreAdjust" and unit != "ssh":
                live_want[unit] = want

    if live_want:
        stopped = 0
        for unit in live_want:
            state = evidence.unit_state(f"{unit}.service")
            pid = state.get("main_pid", 0) if state else 0
            if not pid:
                stopped += 1
                continue
            try:
                got = Path(f"/proc/{pid}/oom_score_adj").read_text().strip()
            except OSError:
                continue
            checked += 1
            if got != live_want[unit]:
                drift.append(
                    DriftItem(
                        f"{unit} oom_score_adj (live)",
                        got,
                        live_want[unit],
                        _RESTART_FIX,
                    )
                )
        if stopped:
            notes.append(f"{stopped} unit(s) not running")
    return drift, checked, notes


def _sysctl_drift() -> tuple[list[DriftItem], int, list[str]]:
    """vm.* knobs: the installed conf is the expectation, /proc/sys the live
    value.

    Expected values are read from the conf rather than hardcoded because
    install.sh computes ``vm.min_free_kbytes`` per-Pi (2% of RAM), so the
    right target is hardware-specific. A value still wearing its
    ``__TEMPLATE__`` placeholder means install.sh's sed step failed and the
    kernel silently kept its own default.
    """
    try:
        text = _JTS_SYSCTL_CONF.read_text()
    except OSError:
        return (
            [DriftItem(str(_JTS_SYSCTL_CONF), "missing", "installed", _INSTALL_FIX)],
            0,
            [],
        )
    drift: list[DriftItem] = []
    checked = 0
    rows = 0
    for line in text.splitlines():
        key, sep, want = line.strip().partition("=")
        key, want = key.strip(), want.strip()
        if not sep or key.startswith("#") or not key.startswith("vm."):
            continue
        rows += 1
        if want.startswith("__") and want.endswith("__"):
            drift.append(
                DriftItem(key, "unsubstituted placeholder", "a value", _INSTALL_FIX)
            )
            continue
        try:
            got = (_PROC_SYS_VM / key[len("vm."):]).read_text().strip()
        except OSError:
            continue  # this kernel does not expose the knob
        checked += 1
        if got != want:
            drift.append(DriftItem(key, got, want, _SYSCTL_FIX))
    if not rows:
        return (
            [DriftItem(str(_JTS_SYSCTL_CONF), "empty", "vm.* settings", _INSTALL_FIX)],
            0,
            [],
        )
    return drift, checked, []


def _mglru_drift() -> tuple[list[DriftItem], int, list[str]]:
    """MGLRU's ``min_ttl_ms``, installed via /etc/tmpfiles.d/jts-mglru.conf.

    Only the kernel default 0 is drift: the tmpfiles ``w-`` line silently
    no-ops on kernels without MGLRU (< 6.1), and any other non-zero value is
    an operator override this check must not fight. The expected value is
    read from the installed conf (the sysctl row's own pattern), not a
    hardcoded constant, so the two can't quietly diverge.
    """
    if not _MGLRU_MIN_TTL.exists():
        return [], 0, ["kernel lacks MGLRU"]
    try:
        got = _MGLRU_MIN_TTL.read_text().strip()
    except OSError:
        # Not drift: re-running tmpfiles cannot fix a knob we could not
        # read, so this must not carry that remedy.
        return [], 0, [f"{_MGLRU_MIN_TTL} unreadable"]
    if got != "0":
        return [], 1, []
    try:
        conf_text = _MGLRU_TMPFILES_CONF.read_text()
    except OSError:
        return [], 0, [f"{_MGLRU_TMPFILES_CONF} unreadable"]
    installed = _mglru_installed_ms(conf_text)
    if installed is None:
        return [], 0, [f"{_MGLRU_TMPFILES_CONF} has no min_ttl_ms line"]
    return (
        [DriftItem("vm.lru_gen.min_ttl_ms", got, installed, _TMPFILES_FIX)],
        1,
        [],
    )


def _classify_drift(
    drift: list[DriftItem], checked: int, notes: list[str],
) -> CheckResult:
    suffix = f"; skipped: {', '.join(notes)}" if notes else ""
    if drift:
        named = ", ".join(f"{d.item}={d.got} (want {d.want})" for d in drift)
        fixes = " / ".join(dict.fromkeys(d.fix for d in drift))
        return CheckResult(
            _LABEL, "warn",
            f"{len(drift)} installed setting(s) drifted: {named}. "
            f"Fix: {fixes}{suffix}",
            reason=REASON_SETTINGS_DRIFTED,
        )
    return CheckResult(
        _LABEL, "ok", f"{checked} installed setting(s) match{suffix}",
    )


@doctor_check(order=37, group="install")
def check_installed_settings_drift() -> CheckResult:
    """Name every installed setting whose live value has drifted."""
    drift: list[DriftItem] = []
    notes: list[str] = []
    checked = 0
    for source in (_systemd_drift, _sysctl_drift, _mglru_drift):
        items, count, source_notes = source()
        drift.extend(items)
        checked += count
        notes.extend(source_notes)
    return _classify_drift(drift, checked, notes)
