# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Test the WS1 group-writable heal for shared state —
`heal_shared_state_modes` in deploy/lib/install/env-migrations.sh.

The 2026-06-19 incident: a shared SQLite DB (usage.db) created 0644 became
unwritable by a non-owner same-group daemon after a StateDirectory re-chown,
producing "attempt to write a readonly database". UMask=0007 fixes NEW files;
this heal fixes the EXISTING ones on upgrade.

The critical safety property pinned here: it is an ALLOWLIST, so it widens the
shared DBs to group-writable but must NEVER touch wifi_guardian.env (mode 0600,
the WiFi PSK) — a blanket `chmod -R g+w` would have leaked the PSK to the group.

CI has no root and no `jasper` group, so `getent`/`chgrp` are stubbed; the real
`chmod` runs against tmp files so the resulting modes are asserted directly.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "deploy" / "lib" / "install" / "env-migrations.sh"

# getent stubbed to succeed so the `getent group jasper` guard passes; chgrp is a
# no-op (no such group on CI) — the file MODE is what we assert, and chmod runs
# for real.
_STUBS = r"""
getent() { return 0; }
chgrp() { :; }
"""


def _extract(name: str) -> str:
    out = subprocess.run(
        ["bash", "-c", rf"sed -n '/^{name}()/,/^}}/p' '{LIB}'"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"{name}()" in out, f"could not extract {name} from {LIB}"
    return out


def _run_heal(state_dir: Path) -> None:
    script = (
        "set -euo pipefail\n"
        + _STUBS
        + _extract("heal_shared_state_modes")
        + f'\nSTATE_DIR="{state_dir}"\nheal_shared_state_modes\n'
    )
    subprocess.run(["bash", "-c", script], check=True)


def _mk(p: Path, mode: int) -> Path:
    p.write_text("x", encoding="utf-8")
    p.chmod(mode)
    return p


def _mode(p: Path) -> int:
    return p.stat().st_mode & 0o777


def test_heal_makes_shared_dbs_group_writable(tmp_path):
    (tmp_path / "wake-events").mkdir()
    usage = _mk(tmp_path / "usage.db", 0o644)
    wal = _mk(tmp_path / "usage.db-wal", 0o644)
    timers = _mk(tmp_path / "timers.db", 0o644)
    wake = _mk(tmp_path / "wake-events" / "wake-events.sqlite3", 0o644)
    vol = _mk(tmp_path / "speaker_volume.json", 0o644)
    # Grouping config + its write-lock: a pre-UMask=0007 lock (0644, non-owner)
    # blocks /grouping/set and /rooms bonding (the 2026-06-23 sub bring-up).
    grouping = _mk(tmp_path / "grouping.env", 0o644)
    grouping_lock = _mk(tmp_path / ".grouping.env.lock", 0o644)

    _run_heal(tmp_path)

    assert _mode(usage) == 0o660
    assert _mode(wal) == 0o660  # SQLite sidecars healed too
    assert _mode(timers) == 0o660
    assert _mode(wake) == 0o660
    assert _mode(vol) == 0o660
    assert _mode(grouping) == 0o660
    assert _mode(grouping_lock) == 0o660  # the lock /grouping/set opens a+
    # The wake-events dir needs group rwx so a non-owner can create WAL files.
    assert _mode(tmp_path / "wake-events") & 0o070 == 0o070


def test_heal_never_touches_the_wifi_psk(tmp_path):
    """The PSK stash (wifi_guardian.env, 0600) must stay root-only — the heal is
    an allowlist precisely so a blanket chmod cannot leak it to the group."""
    psk = _mk(tmp_path / "wifi_guardian.env", 0o600)
    # A shared DB present too, so the heal actually does its work this run.
    usage = _mk(tmp_path / "usage.db", 0o644)

    _run_heal(tmp_path)

    assert _mode(usage) == 0o660  # healed
    assert _mode(psk) == 0o600  # PSK untouched — no group bits added


def test_heal_is_noop_on_fresh_install(tmp_path):
    """No shared files yet → the heal does nothing and does not fail."""
    _run_heal(tmp_path)  # empty dir; must not raise
    assert list(tmp_path.iterdir()) == []
