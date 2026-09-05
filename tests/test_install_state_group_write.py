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

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "deploy" / "lib" / "install" / "env-migrations.sh"

# getent stubbed to succeed so the `getent group jasper` guard passes; chgrp is a
# no-op (no such group on CI) — the file MODE is what we assert, and chmod runs
# for real.
_STUBS = r"""
getent() { printf 'jasper:x:%s:\n' "$(id -g)"; }
chgrp() { :; }
"""

# Same stubs plus a `getent passwd jasper-web` that resolves to the test user,
# so the owner-moving `w:` specs actually chown (a non-root process may chown a
# file it already owns to itself, which is enough to exercise the path).
_STUBS_WITH_WEB_USER = r"""
getent() {
    if [ "$1" = "passwd" ]; then
        printf 'jasper-web:x:%s:%s:::\n' "$(id -u)" "$(id -g)"
    else
        printf 'jasper:x:%s:\n' "$(id -g)"
    fi
}
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


def _run_heal(state_dir: Path, *, stubs: str = _STUBS) -> None:
    script = (
        "set -euo pipefail\n"
        + stubs
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
    audio_incidents = _mk(tmp_path / "audio_health_incidents.json", 0o644)
    topology = _mk(tmp_path / "output_topology.json", 0o640)
    # The two Layer-A stores the ROOT crossover-accept seam writes and /sound/
    # (jasper-web) reads. Boxes that accepted a measured crossover before the
    # writers passed group_from_parent=True still carry root:root, which renders
    # the design page empty; the heal repairs what is already on disk.
    design_draft = _mk(tmp_path / "active_speaker_design_draft.json", 0o600)
    crossover_preview = _mk(
        tmp_path / "active_speaker_crossover_preview.json", 0o600,
    )
    # Grouping config + its write-lock: a pre-UMask=0007 lock (0644, non-owner)
    # blocks /grouping/set and /rooms bonding (the 2026-06-23 sub bring-up).
    grouping = _mk(tmp_path / "grouping.env", 0o644)
    grouping_lock = _mk(tmp_path / ".grouping.env.lock", 0o644)
    source_intent = _mk(tmp_path / "source_intent.env", 0o644)
    source_update_lock = _mk(tmp_path / ".source_intent.env.lock", 0o644)
    source_request_lock = _mk(tmp_path / "source_intent.env.request.lock", 0o644)
    source_reconcile_lock = _mk(
        tmp_path / "source_intent.env.reconcile.lock", 0o644,
    )

    _run_heal(tmp_path)

    assert _mode(usage) == 0o660
    assert _mode(wal) == 0o660  # SQLite sidecars healed too
    assert _mode(timers) == 0o660
    assert _mode(wake) == 0o660
    assert _mode(vol) == 0o660
    assert _mode(audio_incidents) == 0o660
    assert _mode(topology) == 0o640
    assert _mode(design_draft) == 0o640
    assert _mode(crossover_preview) == 0o640
    assert _mode(grouping) == 0o660
    assert _mode(grouping_lock) == 0o660  # the lock /grouping/set opens a+
    assert _mode(source_intent) == 0o660
    assert _mode(source_update_lock) == 0o660
    assert _mode(source_request_lock) == 0o660
    assert _mode(source_reconcile_lock) == 0o660
    # The wake-events dir needs group rwx so a non-owner can create WAL files.
    assert _mode(tmp_path / "wake-events") & 0o070 == 0o070


def test_heal_repairs_state_the_de_rooted_wizard_units_left_behind(tmp_path):
    """jasper-{bluetooth,correction}-web dropped from root to jasper-web, so the
    state their root incarnation created must move with them: readable for the
    files a writer atomically replaces, and OWNED for the SQLite ledger it
    modifies in place ("attempt to write a readonly database" otherwise)."""
    roles = _mk(tmp_path / "bt_roles.json", 0o600)
    measurements = _mk(tmp_path / "active_speaker_measurements.json", 0o600)
    tuning_db = _mk(tmp_path / "usage-tuning.db", 0o600)
    # A capture tree the root /sound/room/ arms made with a bare mkdir under
    # UMask=0077 — 0700, which the dropped writer cannot even traverse.
    captures = tmp_path / "active_speaker_captures"
    captures.mkdir(mode=0o700)

    _run_heal(tmp_path, stubs=_STUBS_WITH_WEB_USER)

    # RoleStore.set() loads before it writes: an unreadable map republishes an
    # empty one and forgets every paired device's handler.
    assert _mode(roles) == 0o640
    assert _mode(measurements) == 0o640
    assert _mode(tuning_db) == 0o644
    assert tuning_db.stat().st_uid == os.getuid()  # the stubbed jasper-web uid
    # setgid so anything measured into it keeps inheriting group `jasper`.
    assert captures.stat().st_mode & 0o7777 == 0o2770


def test_heal_leaves_owner_alone_when_the_web_user_does_not_exist(tmp_path):
    """Fresh install, before create_jasper_service_users: no uid to move to, so
    the pass must degrade to a group/mode heal instead of failing the install."""
    tuning_db = _mk(tmp_path / "usage-tuning.db", 0o600)

    _run_heal(tmp_path)  # stubs resolve no `jasper-web` passwd entry

    assert _mode(tuning_db) == 0o644


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


def test_heal_refuses_all_source_intent_symlinks_without_mutating_target(tmp_path):
    target = _mk(tmp_path / "sensitive-target", 0o600)
    names = (
        "source_intent.env",
        ".source_intent.env.lock",
        "source_intent.env.request.lock",
        "source_intent.env.reconcile.lock",
    )
    for name in names:
        case_dir = tmp_path / name.replace(".", "_")
        case_dir.mkdir()
        (case_dir / name).symlink_to(target)
        script = (
            "set -euo pipefail\n"
            + _STUBS
            + _extract("heal_shared_state_modes")
            + f'\nSTATE_DIR="{case_dir}"\nheal_shared_state_modes\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True,
        )
        assert proc.returncode != 0, name
        assert "refusing unsafe shared-state path" in proc.stderr
        assert target.read_text(encoding="utf-8") == "x"
        assert _mode(target) == 0o600


def test_heal_refuses_source_intent_fifo_without_blocking(tmp_path):
    os.mkfifo(tmp_path / "source_intent.env")
    script = (
        "set -euo pipefail\n"
        + _STUBS
        + _extract("heal_shared_state_modes")
        + f'\nSTATE_DIR="{tmp_path}"\nheal_shared_state_modes\n'
    )

    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=3,
    )

    assert proc.returncode != 0
    assert "unexpected shared-state file type" in proc.stderr


def test_heal_widens_active_run_locks_and_regroups_their_records(tmp_path):
    """The ~3 s crossover_level_run_unavailable storm and its twin (ADR-0196).

    A root-run poll created these advisory locks root:root 0640 -- a lock no
    service account could then TAKE, because taking one opens the file for
    write. The heal widens only the LOCKS to 0660 (the `l` kind derives the
    ".<record>.lock" name, so install does not respell it) while the records
    stay group-READ, published that way by their own atomic writers.

    Also owns what a source-text drift guard in test_install_plan_covers_main
    used to assert about the Layer-A SSOT: existing root:root
    active_speaker_baseline_profile.json must become readable by /state. That
    heal moved off its own hand-rolled path-following chgrp/chmod (redirectable
    through a symlink or hardlink under a group-writable dir) onto this
    allowlist, so the guarantee is pinned here, by running it.
    """
    level_lock = _mk(
        tmp_path / ".active_speaker_crossover_level_run.json.lock", 0o640,
    )
    repeat_lock = _mk(
        tmp_path / ".active_speaker_repeat_admission.json.lock", 0o640,
    )
    commissioning_lock = _mk(
        tmp_path / ".active_speaker_commissioning_run.json.lock", 0o640,
    )
    live_exec_lock = _mk(
        tmp_path / ".active_speaker_commissioning_run.json.live-execution.lock",
        0o640,
    )
    record = _mk(tmp_path / "active_speaker_commissioning_run.json", 0o600)
    live_mutation = _mk(
        tmp_path / ".active_speaker_commissioning_run.json.live-mutation.json",
        0o600,
    )
    # The Layer-A SSOT, folded off its own hand-rolled path-following heal.
    baseline = _mk(tmp_path / "active_speaker_baseline_profile.json", 0o600)

    _run_heal(tmp_path)

    assert _mode(level_lock) == 0o660
    assert _mode(repeat_lock) == 0o660
    assert _mode(commissioning_lock) == 0o660
    assert _mode(live_exec_lock) == 0o660
    assert _mode(record) == 0o640
    assert _mode(live_mutation) == 0o640
    assert _mode(baseline) == 0o640


def test_heal_refuses_a_symlinked_run_lock_without_mutating_target(tmp_path):
    """Blocker 1: the run-record lock heal must not follow a symlink.

    A group member can pre-create the lock NAME as a symlink onto a root file
    under group-writable /var/lib/jasper; a path-following chgrp/chmod would
    then redirect the mutation. O_NOFOLLOW on the `l`-derived path pins the
    inode, so a symlink aborts the install without touching its target.
    """
    target = _mk(tmp_path / "root-owned-secret", 0o600)
    (tmp_path / ".active_speaker_crossover_level_run.json.lock").symlink_to(target)
    script = (
        "set -euo pipefail\n"
        + _STUBS
        + _extract("heal_shared_state_modes")
        + f'\nSTATE_DIR="{tmp_path}"\nheal_shared_state_modes\n'
    )

    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert proc.returncode != 0
    assert "refusing unsafe shared-state path" in proc.stderr
    assert _mode(target) == 0o600


def test_heal_refuses_a_hardlinked_run_lock_without_mutating_target(tmp_path):
    """The hardlink variant O_NOFOLLOW cannot catch.

    A hardlink onto a root file is not a symlink -- O_NOFOLLOW opens it and
    fstat sees a plain regular file -- so the heal would fchown/fchmod the
    aliased target. The st_nlink check refuses any name whose inode has more
    than one link, regardless of the fs.protected_hardlinks sysctl.
    """
    target = _mk(tmp_path / "root-owned-secret", 0o600)
    os.link(target, tmp_path / ".active_speaker_crossover_level_run.json.lock")
    script = (
        "set -euo pipefail\n"
        + _STUBS
        + _extract("heal_shared_state_modes")
        + f'\nSTATE_DIR="{tmp_path}"\nheal_shared_state_modes\n'
    )

    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert proc.returncode != 0
    assert "refusing hardlinked shared-state path" in proc.stderr
    assert _mode(target) == 0o600
