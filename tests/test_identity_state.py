# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for jasper.identity_state — the read side of the identity
reconciler.

The writer is deploy/bin/jasper-identity-reconcile (covered by
tests/test_identity_reconcile_script.py); this file pins the consumer
contract: fresh reads, allowlist-shaped name derivation, the
mtime-keyed cache, and the /state snapshot statuses.
"""
from __future__ import annotations

import os

from jasper import identity_state


def _write_identity(path, *, os_host="jts3", avahi="jts3.local",
                    configured="jts3.local", collision="0", drift="0",
                    avahi_available="1"):
    path.write_text(
        "# test fixture\n"
        f"JASPER_IDENTITY_OS_HOSTNAME={os_host}\n"
        f"JASPER_IDENTITY_AVAHI_HOSTNAME={avahi}\n"
        f"JASPER_IDENTITY_CONFIGURED_HOSTNAME={configured}\n"
        f"JASPER_IDENTITY_AVAHI_AVAILABLE={avahi_available}\n"
        f"JASPER_IDENTITY_COLLISION={collision}\n"
        f"JASPER_IDENTITY_DRIFT={drift}\n"
        "JASPER_IDENTITY_CHECKED_AT=2026-06-11T12:00:00Z\n"
    )


def test_effective_hostnames_includes_bare_and_local_twins(tmp_path):
    f = tmp_path / "identity.env"
    _write_identity(f, avahi="jts3-2.local")
    names = identity_state.effective_hostnames(str(f))
    # Every recorded name appears bare AND with .local — a browser may
    # present either form.
    for expected in ("jts3", "jts3.local", "jts3-2", "jts3-2.local"):
        assert expected in names


def test_effective_hostnames_missing_file_is_empty(tmp_path):
    names = identity_state.effective_hostnames(str(tmp_path / "absent.env"))
    assert names == frozenset()


def test_effective_hostnames_cache_refreshes_on_rewrite(tmp_path):
    f = tmp_path / "identity.env"
    _write_identity(f, avahi="jts3.local")
    first = identity_state.effective_hostnames(str(f))
    assert "jts3-2.local" not in first
    # Reconciler rewrites after a collision rename; ensure a different
    # mtime/size so the cache key changes even on coarse-mtime
    # filesystems.
    _write_identity(f, avahi="jts3-2.local", collision="1", drift="1")
    os.utime(f, (os.path.getmtime(f) + 2, os.path.getmtime(f) + 2))
    second = identity_state.effective_hostnames(str(f))
    assert "jts3-2.local" in second


def test_snapshot_absent(tmp_path):
    snap = identity_state.snapshot(str(tmp_path / "absent.env"))
    assert snap["status"] == "absent"


def test_snapshot_ok(tmp_path):
    f = tmp_path / "identity.env"
    _write_identity(f)
    snap = identity_state.snapshot(str(f))
    assert snap["status"] == "ok"
    assert snap["os_hostname"] == "jts3"
    assert snap["avahi_hostname"] == "jts3.local"
    assert snap["configured_hostname"] == "jts3.local"
    assert snap["avahi_available"] is True
    assert snap["checked_at"] == "2026-06-11T12:00:00Z"


def test_snapshot_collision_wins_over_drift(tmp_path):
    f = tmp_path / "identity.env"
    _write_identity(f, avahi="jts3-2.local", collision="1", drift="1")
    snap = identity_state.snapshot(str(f))
    assert snap["status"] == "collision"


def test_snapshot_drift(tmp_path):
    f = tmp_path / "identity.env"
    _write_identity(f, configured="jts.local", drift="1")
    snap = identity_state.snapshot(str(f))
    assert snap["status"] == "drift"


def test_identity_path_env_override(monkeypatch, tmp_path):
    f = tmp_path / "custom.env"
    _write_identity(f)
    monkeypatch.setenv("JASPER_IDENTITY_FILE", str(f))
    assert identity_state.identity_path() == str(f)
    assert identity_state.snapshot()["status"] == "ok"


def test_state_resilience_wires_identity_snapshot():
    """Pin the /state wiring: jasper-control's resilience block must
    surface identity_state.snapshot() (the dashboard/doctor consumers
    key off /state.resilience.identity). Static source pin, same style
    as the control-client route-table guard."""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    server_src = (repo / "jasper" / "control" / "server.py").read_text()
    aggregate_src = (
        repo / "jasper" / "control" / "state_aggregate.py"
    ).read_text()
    assert 'from . import state_aggregate as _state_aggregate' in server_src
    assert '"/state": "_get_state"' in server_src
    assert "return await _state_aggregate._get_state(" in server_src
    assert '"identity": identity_state.snapshot()' in aggregate_src
