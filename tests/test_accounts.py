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


def test_registry_load_missing_returns_empty():
    r = Registry.load("/nonexistent/path.json")
    assert r.accounts == []
    assert r.default_name == ""


def test_registry_round_trip():
    path = _tmp_registry()
    try:
        r = Registry(path=path)
        r.add_or_update(Account(name="jasper"), make_default=True)
        r.add_or_update(Account(name="brittany"))
        r.save()

        r2 = Registry.load(path)
        assert len(r2.accounts) == 2
        assert r2.default_name == "jasper"
        assert r2.get("brittany") is not None
        assert r2.get("brittany").cache_path.endswith("brittany.json")
    finally:
        for f in (path, path + ".tmp"):
            if os.path.exists(f):
                os.unlink(f)


def test_registry_load_tolerates_legacy_pattern_field():
    """Older registry files may have client_name_patterns. The new schema
    ignores it instead of choking — handles in-place upgrade without
    requiring the deploy script to rewrite accounts.json first."""
    path = _tmp_registry()
    try:
        Path(path).write_text(json.dumps({
            "version": 1,
            "default": "jasper",
            "accounts": [{
                "name": "jasper",
                "client_name_patterns": ["Jasper's iPhone"],
                "cache_path": "/var/lib/jasper/spotify/caches/jasper.json",
            }],
        }))
        r = Registry.load(path)
        assert len(r.accounts) == 1
        assert r.accounts[0].name == "jasper"
        assert r.default_name == "jasper"
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_registry_remove_updates_default():
    r = Registry()
    r.add_or_update(Account(name="a"), make_default=True)
    r.add_or_update(Account(name="b"))
    assert r.default_name == "a"
    r.remove("a")
    assert r.default_name == "b"
    r.remove("b")
    assert r.default_name == ""


def test_default_cache_path_blocks_traversal():
    """Sanitization replaces non-alphanumeric/underscore/dash chars
    so a malicious account name can't escape the cache dir."""
    p = default_cache_path_for("alice/../etc/passwd")
    assert "/etc/passwd" not in p
    assert p.startswith("/var/lib/jasper/spotify/caches/")
    assert p.endswith(".json")


def test_legacy_migration_wraps_existing_cache():
    legacy_fd, legacy = tempfile.mkstemp(suffix=".cache")
    os.close(legacy_fd)
    Path(legacy).write_text('{"access_token": "xyz"}')
    reg_path = _tmp_registry()
    new_cache_dir = tempfile.mkdtemp()
    try:
        from jasper import accounts as accounts_mod
        original_dir = accounts_mod.DEFAULT_CACHE_DIR
        accounts_mod.DEFAULT_CACHE_DIR = new_cache_dir
        try:
            r = Registry(path=reg_path)
            assert maybe_migrate_legacy(r, legacy_cache=legacy) is True
            assert len(r.accounts) == 1
            assert r.accounts[0].name == "default"
            assert r.default_name == "default"
            assert Path(r.accounts[0].cache_path).read_text() == '{"access_token": "xyz"}'
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
