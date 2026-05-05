from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from jasper.accounts import (
    Account,
    Registry,
    default_cache_path_for,
    maybe_migrate_legacy,
)


def _tmp_registry() -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)
    return path


def test_account_matches_client_name_smart_quote():
    a = Account(name="jasper", client_name_patterns=["Jasper's iPhone"])
    # smart-quote variant should still match
    assert a.matches_client_name("Jasper’s iPhone") is True
    # case-insensitive
    assert a.matches_client_name("jasper's iphone") is True
    # substring match
    assert a.matches_client_name("Jasper's iPhone (work)") is True
    # no match
    assert a.matches_client_name("Brittany's iPhone") is False
    # empty client name
    assert a.matches_client_name("") is False


def test_account_no_patterns_never_matches():
    a = Account(name="x", client_name_patterns=[])
    assert a.matches_client_name("anything") is False


def test_account_multiple_patterns():
    a = Account(
        name="jasper",
        client_name_patterns=["Jasper's iPhone", "Jasper's Mac Studio"],
    )
    assert a.matches_client_name("Jasper's iPhone") is True
    assert a.matches_client_name("Jasper's Mac Studio") is True
    assert a.matches_client_name("Brittany's iPhone") is False


def test_registry_load_missing_returns_empty():
    r = Registry.load("/nonexistent/path.json")
    assert r.accounts == []
    assert r.default_name == ""


def test_registry_round_trip():
    path = _tmp_registry()
    try:
        r = Registry(path=path)
        r.add_or_update(
            Account(name="jasper", client_name_patterns=["Jasper's iPhone"]),
            make_default=True,
        )
        r.add_or_update(
            Account(name="brittany", client_name_patterns=["Brittany's iPhone"])
        )
        r.save()

        r2 = Registry.load(path)
        assert len(r2.accounts) == 2
        assert r2.default_name == "jasper"
        assert r2.get("brittany").client_name_patterns == ["Brittany's iPhone"]
    finally:
        for f in (path, path + ".tmp"):
            if os.path.exists(f):
                os.unlink(f)


def test_registry_match_client_name():
    r = Registry()
    r.add_or_update(
        Account(name="jasper", client_name_patterns=["Jasper's iPhone"]),
        make_default=True,
    )
    r.add_or_update(
        Account(name="brittany", client_name_patterns=["Brittany's iPhone"])
    )
    matched = r.match_client_name("Brittany’s iPhone")
    assert matched is not None and matched.name == "brittany"


def test_registry_remove_updates_default():
    r = Registry()
    r.add_or_update(Account(name="a"), make_default=True)
    r.add_or_update(Account(name="b"))
    assert r.default_name == "a"
    r.remove("a")
    assert r.default_name == "b"
    r.remove("b")
    assert r.default_name == ""


def test_default_cache_path_sanitises_name():
    p = default_cache_path_for("alice/../etc/passwd")
    assert "/etc/passwd" not in p
    assert p.endswith("alice_.._etc_passwd.json")


def test_legacy_migration_wraps_existing_cache():
    legacy_fd, legacy = tempfile.mkstemp(suffix=".cache")
    os.close(legacy_fd)
    Path(legacy).write_text('{"access_token": "xyz"}')
    reg_path = _tmp_registry()
    new_cache_dir = tempfile.mkdtemp()
    try:
        # Patch the cache dir so the migration writes somewhere we can clean up.
        from jasper import accounts as accounts_mod
        original_dir = accounts_mod.DEFAULT_CACHE_DIR
        accounts_mod.DEFAULT_CACHE_DIR = new_cache_dir
        try:
            r = Registry(path=reg_path)
            assert maybe_migrate_legacy(r, legacy_cache=legacy) is True
            assert len(r.accounts) == 1
            assert r.accounts[0].name == "default"
            assert r.default_name == "default"
            # Cache file copied
            assert Path(r.accounts[0].cache_path).read_text() == '{"access_token": "xyz"}'
            # Calling again is a no-op (registry not empty)
            assert maybe_migrate_legacy(r, legacy_cache=legacy) is False
        finally:
            accounts_mod.DEFAULT_CACHE_DIR = original_dir
    finally:
        for f in (legacy, reg_path, reg_path + ".tmp"):
            if os.path.exists(f):
                os.unlink(f)
        import shutil
        shutil.rmtree(new_cache_dir, ignore_errors=True)


def test_legacy_migration_skipped_when_no_legacy_cache():
    reg_path = _tmp_registry()
    try:
        r = Registry(path=reg_path)
        assert maybe_migrate_legacy(r, legacy_cache="/nonexistent") is False
        assert r.accounts == []
    finally:
        if os.path.exists(reg_path):
            os.unlink(reg_path)
