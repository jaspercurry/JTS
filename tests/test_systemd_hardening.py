"""Drift guard: the Tier-A daemons keep their WS1 phase-1 hardening stanza.

A compromise of an always-on, network-facing `jasper-*` daemon is a full-root
device compromise today (they all run as root). Phase 1 of the privilege-
separation work (docs/HANDOFF-privilege-separation.md) hardens each so a root
RCE can no longer write the filesystem, load kernel modules, change kernel
tunables, or enter new namespaces — measured on hardware to drop
`systemd-analyze security` from 8.7-9.6 (EXPOSED/UNSAFE) to ~6.2-6.6 (MEDIUM).

This test pins that contract: an edit that removes `ProtectSystem=strict` or any
of the phase-1 directives from a Tier-A unit fails CI. It deliberately encodes
the per-unit nuances (the reason a uniform block would break things), so the
exceptions are explicit, not silent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Tier-A unit -> its file (jasper-web lives in deploy/, the rest in deploy/systemd/).
TIER_A = {
    "jasper-voice": ROOT / "deploy/systemd/jasper-voice.service",
    "jasper-control": ROOT / "deploy/systemd/jasper-control.service",
    "jasper-web": ROOT / "deploy/jasper-web.service",
    "jasper-mux": ROOT / "deploy/systemd/jasper-mux.service",
    "jasper-input": ROOT / "deploy/systemd/jasper-input.service",
}

# Directives every Tier-A unit must carry (key -> required value, or None = any value).
REQUIRED_ALL = {
    "ProtectSystem": "strict",
    "ProtectHome": None,
    "PrivateTmp": "true",
    "NoNewPrivileges": "true",
    "ProtectKernelTunables": "true",
    "ProtectKernelModules": "true",
    "ProtectControlGroups": "true",
    "RestrictNamespaces": "true",
    "RestrictSUIDSGID": "true",
    "LockPersonality": "true",
    "SystemCallArchitectures": "native",
    "RestrictAddressFamilies": None,
}

# ProtectKernelLogs is intentionally OMITTED on the two units that shell out to
# diagnostic/network tools reading the kernel log ring buffer (dmesg). Required
# everywhere else.
KERNEL_LOGS_EXEMPT = {"jasper-control", "jasper-web"}

# ProtectHome=tmpfs (hide /root) on the daemons that need no home dir.
TMPFS_HOME = {"jasper-voice", "jasper-mux"}


def _directives(path: Path) -> list[tuple[str, str]]:
    """All `Key=Value` directive lines in a unit file (comments/blank stripped)."""
    out: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("[") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        out.append((key.strip(), value.strip()))
    return out


@pytest.mark.parametrize("unit,path", sorted(TIER_A.items()))
def test_tier_a_unit_exists(unit, path):
    assert path.is_file(), f"{unit}: expected unit at {path}"


@pytest.mark.parametrize("unit,path", sorted(TIER_A.items()))
def test_tier_a_required_directives(unit, path):
    directives = _directives(path)
    keys = {k for k, _ in directives}
    pairs = set(directives)
    missing = []
    for key, want in REQUIRED_ALL.items():
        if want is None:
            if key not in keys:
                missing.append(f"{key}=<any>")
        elif (key, want) not in pairs:
            missing.append(f"{key}={want}")
    assert not missing, (
        f"{unit} ({path.name}) lost WS1 phase-1 hardening directive(s): "
        f"{missing}. See docs/HANDOFF-privilege-separation.md."
    )


@pytest.mark.parametrize("unit,path", sorted(TIER_A.items()))
def test_protect_kernel_logs_present_unless_exempt(unit, path):
    has = ("ProtectKernelLogs", "true") in set(_directives(path))
    if unit in KERNEL_LOGS_EXEMPT:
        assert not has, (
            f"{unit} is documented as ProtectKernelLogs-exempt (spawns dmesg-reading "
            "subprocesses); if that changed, update KERNEL_LOGS_EXEMPT + the doc."
        )
    else:
        assert has, f"{unit} must set ProtectKernelLogs=true."


@pytest.mark.parametrize("unit", sorted(TMPFS_HOME))
def test_tmpfs_home_where_no_home_needed(unit):
    assert ("ProtectHome", "tmpfs") in set(_directives(TIER_A[unit])), (
        f"{unit} should hide /root via ProtectHome=tmpfs (it needs no home dir)."
    )
