# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""bt_roles persistence — pin the group-readable mode under the WS1 non-root drop."""
from __future__ import annotations

import os
import stat

from jasper.bluetooth.roles import RoleStore


def test_role_store_writes_group_readable_0640(tmp_path):
    # bt_roles.json lives in /var/lib/jasper (the shared group-jasper state tree).
    # After the WS1 non-root privilege drop it must be group-readable, not the
    # hand-rolled NamedTemporaryFile default 0600, so any non-root daemon in the
    # jasper group can read it — consistent with the rest of /var/lib/jasper.
    # Regression pin for the migration to jasper.atomic_io.atomic_write_text.
    p = tmp_path / "bt_roles.json"
    RoleStore(path=str(p)).set("AA:BB:CC:DD:EE:FF", "hid_dial")

    mode = stat.S_IMODE(os.stat(p).st_mode)
    assert mode == 0o640, f"expected 0o640 (group-readable), got {oct(mode)}"
    assert mode & stat.S_IRGRP, "bt_roles.json must be group-readable for non-root daemons"


def test_role_store_inherits_parent_group_on_write(tmp_path, monkeypatch):
    import jasper.bluetooth.roles as roles

    calls = []

    def fake_atomic_write_text(path, text, *, mode, group_from_parent=False):
        calls.append({
            "path": path,
            "text": text,
            "mode": mode,
            "group_from_parent": group_from_parent,
        })

    monkeypatch.setattr(roles, "atomic_write_text", fake_atomic_write_text)

    p = tmp_path / "bt_roles.json"
    roles.RoleStore(path=str(p)).set("AA:BB:CC:DD:EE:FF", "hid_dial")

    assert calls
    assert calls[0]["path"] == p
    assert calls[0]["mode"] == 0o640
    assert calls[0]["group_from_parent"] is True


def test_role_store_round_trips(tmp_path):
    p = tmp_path / "bt_roles.json"
    store = RoleStore(path=str(p))
    store.set("AA:BB:CC:DD:EE:FF", "hid_dial")
    store.set("11:22:33:44:55:66", "bt_source")

    # case-insensitive lookup; persisted across a fresh store instance
    reloaded = RoleStore(path=str(p))
    assert reloaded.get("aa:bb:cc:dd:ee:ff") == "hid_dial"
    assert reloaded.get("11:22:33:44:55:66") == "bt_source"

    store.remove("AA:BB:CC:DD:EE:FF")
    assert RoleStore(path=str(p)).get("AA:BB:CC:DD:EE:FF") is None


def test_role_store_write_failure_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    # Best-effort promise (the _write comment): a role-map persistence failure
    # must NOT crash the bluetooth handler — it logs a warning and moves on. Pin
    # it so the migration to atomic_write_text can't silently turn a write error
    # into an uncaught raise (the don't-crash-the-handler contract).
    import logging

    import jasper.bluetooth.roles as roles

    def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(roles, "atomic_write_text", boom)
    store = roles.RoleStore(path=str(tmp_path / "bt_roles.json"))

    with caplog.at_level(logging.WARNING):
        store.set("AA:BB:CC:DD:EE:FF", "hid_dial")  # must not raise

    assert any("bt_roles: write failed" in r.message for r in caplog.records)
