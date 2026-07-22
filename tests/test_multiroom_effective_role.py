# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest

from jasper.multiroom.config import GroupingConfig
from jasper.multiroom.effective_role import (
    MAX_BOOT_ID_BYTES,
    effective_follower_leader_addr,
    effective_local_sources_park_reason,
    grouping_request_fingerprint,
    MAX_EFFECTIVE_ROLE_STATUS_BYTES,
    read_current_boot_id,
    read_effective_role_status,
    FOLLOWER_STATUS_FILE,
)


BOOT_A = "11111111-1111-4111-8111-111111111111"
BOOT_B = "22222222-2222-4222-8222-222222222222"


def _follower(*, leader_addr: str = "jts1.local") -> GroupingConfig:
    return GroupingConfig(
        enabled=True,
        role="follower",
        channel="right",
        bond_id="living-room",
        leader_addr=leader_addr,
        buffer_ms=400,
        codec="flac",
        error=None,
    )


def _solo() -> GroupingConfig:
    return GroupingConfig(
        enabled=False,
        role="",
        channel="stereo",
        bond_id="",
        leader_addr="",
        buffer_ms=400,
        codec="flac",
        error=None,
    )


def test_requested_follower_parks_when_effective_status_is_missing_or_stale():
    cfg = _follower()

    assert effective_local_sources_park_reason(cfg, status={}) == "bonded_follower"
    assert effective_follower_leader_addr(cfg, status={}) == "jts1.local"
    stale = {
        "requested_fingerprint": grouping_request_fingerprint(
            _follower(leader_addr="old.local"),
        ),
        "local_sources_allowed": True,
    }
    assert effective_local_sources_park_reason(cfg, status=stale) == "bonded_follower"
    assert effective_follower_leader_addr(cfg, status=stale) == "jts1.local"


def test_matching_refused_follower_status_exposes_effective_solo_role():
    cfg = _follower()
    refused = {
        "requested_fingerprint": grouping_request_fingerprint(cfg),
        "local_sources_allowed": True,
        "boot_id": BOOT_A,
    }

    boot_id_reader = lambda: BOOT_A
    assert (
        effective_local_sources_park_reason(
            cfg,
            status=refused,
            boot_id_reader=boot_id_reader,
        )
        is None
    )
    assert (
        effective_follower_leader_addr(
            cfg,
            status=refused,
            boot_id_reader=boot_id_reader,
        )
        is None
    )


def test_refused_follower_grant_is_valid_only_for_the_current_boot():
    cfg = _follower()
    grant = {
        "requested_fingerprint": grouping_request_fingerprint(cfg),
        "local_sources_allowed": True,
        "boot_id": BOOT_A,
    }

    for current in (BOOT_B, "", "not-a-boot-id"):
        assert (
            effective_local_sources_park_reason(
                cfg,
                status=grant,
                boot_id_reader=lambda current=current: current,
            )
            == "bonded_follower"
        )
    for persisted in ("", "not-a-boot-id", BOOT_B):
        stale = {**grant, "boot_id": persisted}
        assert (
            effective_local_sources_park_reason(
                cfg,
                status=stale,
                boot_id_reader=lambda: BOOT_A,
            )
            == "bonded_follower"
        )


@pytest.mark.parametrize("active_follower", [True, False])
def test_non_follower_request_stays_parked_until_landed_status_replaces_parked_role(
    active_follower,
):
    cfg = _solo()
    old_follower = _follower()
    prior = {
        "active_follower": active_follower,
        "requested_fingerprint": grouping_request_fingerprint(old_follower),
        "local_sources_allowed": False,
        "boot_id": BOOT_A,
    }
    assert (
        effective_local_sources_park_reason(
            cfg,
            status=prior,
            boot_id_reader=lambda: BOOT_A,
        )
        == "role_transition_in_progress"
    )

    deny = {
        "active_follower": False,
        "blocked_reason": "role_transition_in_progress",
        "requested_fingerprint": grouping_request_fingerprint(cfg),
        "local_sources_allowed": False,
        "boot_id": BOOT_A,
    }
    assert (
        effective_local_sources_park_reason(
            cfg,
            status=deny,
            boot_id_reader=lambda: BOOT_A,
        )
        == "role_transition_in_progress"
    )

    landed = {**deny, "blocked_reason": "", "local_sources_allowed": True}
    assert (
        effective_local_sources_park_reason(
            cfg,
            status=landed,
            boot_id_reader=lambda: BOOT_A,
        )
        is None
    )


def test_non_follower_request_never_depends_on_effective_status():
    cfg = _solo()

    assert effective_local_sources_park_reason(cfg, status={}) is None
    assert effective_follower_leader_addr(cfg, status={}) is None


def test_effective_status_reader_rejects_non_boolean_permission(tmp_path):
    path = tmp_path / "status.json"
    path.write_text(
        json.dumps(
            {
                "active_follower": True,
                "local_sources_allowed": "yes",
                "requested_fingerprint": "abc",
            }
        )
    )

    assert read_effective_role_status(str(path)) == {
        "active_follower": True,
        "active_leader": False,
        "blocked_reason": "",
        "requested_fingerprint": "abc",
        "local_sources_allowed": None,
        "boot_id": "",
    }


def test_current_boot_id_reader_is_bounded_safe_and_canonical(tmp_path):
    valid = tmp_path / "boot-id"
    valid.write_text(BOOT_A.upper() + "\n")
    assert read_current_boot_id(str(valid)) == BOOT_A

    malformed = tmp_path / "malformed"
    malformed.write_text("not-a-uuid\n")
    assert read_current_boot_id(str(malformed)) == ""

    oversized = tmp_path / "oversized-boot-id"
    oversized.write_bytes(b"a" * (MAX_BOOT_ID_BYTES + 1))
    assert read_current_boot_id(str(oversized)) == ""

    link = tmp_path / "boot-id-link"
    link.symlink_to(valid)
    assert read_current_boot_id(str(link)) == ""


def test_effective_status_reader_rejects_unsafe_or_oversized_paths(tmp_path):
    valid = tmp_path / "valid.json"
    valid.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(valid)
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (MAX_EFFECTIVE_ROLE_STATUS_BYTES + 1))
    fifo = tmp_path / "status.fifo"
    os.mkfifo(fifo)

    assert read_effective_role_status(str(link)) == {}
    assert read_effective_role_status(str(oversized)) == {}
    assert read_effective_role_status(str(fifo)) == {}


def test_effective_status_reader_rejects_excessive_json_nesting(tmp_path):
    path = tmp_path / "nested.json"
    path.write_text("[" * 1_100 + "]" * 1_100)

    assert read_effective_role_status(str(path)) == {}


def test_default_authorization_path_rejects_group_writable_parent(
    tmp_path,
    monkeypatch,
):
    import jasper.multiroom.effective_role as effective_role

    parent = tmp_path / "replaceable"
    parent.mkdir()
    parent.chmod(0o770)
    path = parent / "effective-role.json"
    path.write_text("{}")
    monkeypatch.setattr(effective_role, "FOLLOWER_STATUS_FILE", str(path))

    assert stat.S_IMODE(parent.stat().st_mode) & 0o020
    assert effective_role.read_effective_role_status() == {}


def test_default_authorization_path_rejects_untrusted_final_inode(
    tmp_path,
    monkeypatch,
):
    import jasper.multiroom.effective_role as effective_role

    parent = tmp_path / "trusted-parent"
    parent.mkdir()
    path = parent / "effective-role.json"
    path.write_text("{}")
    real_lstat = os.lstat

    def fake_lstat(candidate):
        inode = real_lstat(candidate)
        if os.fspath(candidate) == str(parent):
            return SimpleNamespace(st_mode=inode.st_mode, st_uid=0)
        return SimpleNamespace(
            st_mode=inode.st_mode | stat.S_IWGRP,
            st_uid=0,
        )

    monkeypatch.setattr(effective_role, "FOLLOWER_STATUS_FILE", str(path))
    monkeypatch.setattr(effective_role.os, "lstat", fake_lstat)

    assert effective_role.read_effective_role_status() == {}


def test_default_authorization_path_rejects_non_root_final_owner(
    tmp_path,
    monkeypatch,
):
    import jasper.multiroom.effective_role as effective_role

    parent = tmp_path / "trusted-parent"
    parent.mkdir()
    path = parent / "effective-role.json"
    path.write_text("{}")
    real_lstat = os.lstat

    def fake_lstat(candidate):
        inode = real_lstat(candidate)
        uid = 0 if os.fspath(candidate) == str(parent) else 1000
        return SimpleNamespace(st_mode=inode.st_mode, st_uid=uid)

    monkeypatch.setattr(effective_role, "FOLLOWER_STATUS_FILE", str(path))
    monkeypatch.setattr(effective_role.os, "lstat", fake_lstat)

    assert effective_role.read_effective_role_status() == {}


def test_effective_role_authorization_uses_dedicated_root_state_path():
    assert FOLLOWER_STATUS_FILE == "/var/lib/jasper-grouping/effective-role.json"
