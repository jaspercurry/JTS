# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import types
from pathlib import Path

from jasper.accounts import (
    Account,
    Registry,
    SPOTIFY_CACHE_FILE_MODE,
    build_cache_handler,
    default_cache_path_for,
    maybe_migrate_legacy,
)


def _tmp_registry() -> str:
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)
    return path


def _install_fake_spotipy_cache_handler(monkeypatch):
    from jasper import accounts as accounts_mod

    class FakeCacheFileHandler:
        def __init__(self, cache_path=None, *args, **kwargs):
            self.cache_path = cache_path

    spotipy_mod = types.ModuleType("spotipy")
    cache_handler_mod = types.ModuleType("spotipy.cache_handler")
    cache_handler_mod.CacheFileHandler = FakeCacheFileHandler
    monkeypatch.setitem(sys.modules, "spotipy", spotipy_mod)
    monkeypatch.setitem(sys.modules, "spotipy.cache_handler", cache_handler_mod)
    monkeypatch.setattr(accounts_mod, "_CACHE_HANDLER_CLS", None)
    return accounts_mod


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
                "cache_path": "/var/lib/jasper-intsecrets/spotify/caches/jasper.json",
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
    assert p.startswith("/var/lib/jasper-intsecrets/spotify/caches/")
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


# ----- per-account playlist config (the web-UI map) -----


def test_playlists_field_round_trip():
    """Account.playlists round-trips through save/load."""
    path = _tmp_registry()
    try:
        r = Registry(path=path)
        r.add_or_update(Account(name="jasper"), make_default=True)
        r.add_playlist("jasper", "spotify:playlist:abc", "Discover Weekly")
        r.add_playlist("jasper", "spotify:playlist:def", "Daylist")
        r.save()

        r2 = Registry.load(path)
        a = r2.get("jasper")
        assert a is not None
        assert a.playlists == {
            "spotify:playlist:abc": "Discover Weekly",
            "spotify:playlist:def": "Daylist",
        }
    finally:
        for f in (path, path + ".tmp"):
            if os.path.exists(f):
                os.unlink(f)


def test_load_tolerates_missing_playlists_field():
    """Older registry JSON has no `playlists` key. Load must not choke
    — running installs upgraded in place shouldn't have to rewrite the
    JSON before they boot."""
    path = _tmp_registry()
    try:
        Path(path).write_text(json.dumps({
            "version": 1,
            "default": "jasper",
            "accounts": [{
                "name": "jasper",
                "cache_path": "/tmp/jasper.json",
            }],
        }))
        r = Registry.load(path)
        a = r.get("jasper")
        assert a is not None
        assert a.playlists == {}
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_load_tolerates_garbage_playlist_entries():
    """Defensive: hand-edited JSON with non-string keys/values is filtered
    out rather than crashing the registry load."""
    path = _tmp_registry()
    try:
        Path(path).write_text(json.dumps({
            "version": 1,
            "default": "jasper",
            "accounts": [{
                "name": "jasper",
                "cache_path": "/tmp/jasper.json",
                "playlists": {
                    "spotify:playlist:abc": "Good",
                    "spotify:playlist:bad": 123,    # non-string value
                    "no_uri": "Also bad",            # we keep this — validation
                                                      # is best-effort, not strict
                },
            }],
        }))
        r = Registry.load(path)
        a = r.get("jasper")
        assert a is not None
        assert "spotify:playlist:abc" in a.playlists
        assert "spotify:playlist:bad" not in a.playlists  # non-string filtered
    finally:
        if os.path.exists(path):
            os.unlink(path)


def test_remove_playlist_returns_false_for_unknown_account():
    r = Registry()
    r.add_or_update(Account(name="jasper"), make_default=True)
    assert r.remove_playlist("alice", "spotify:playlist:abc") is False
    assert r.remove_playlist("jasper", "spotify:playlist:not-there") is False


def test_add_or_update_preserves_existing_playlists():
    """A subsequent add_or_update with an empty playlists field shouldn't
    nuke previously-configured entries — the OAuth re-flow path passes
    a freshly-constructed Account whose default playlists field is {}."""
    r = Registry()
    r.add_or_update(Account(name="jasper"), make_default=True)
    r.add_playlist("jasper", "spotify:playlist:abc", "Discover Weekly")
    # Second add_or_update (e.g. on re-OAuth) with no playlists field set:
    r.add_or_update(Account(name="jasper", cache_path="/new/path.json"))
    a = r.get("jasper")
    assert a.cache_path == "/new/path.json"
    assert a.playlists == {"spotify:playlist:abc": "Discover Weekly"}


def test_build_cache_handler_writes_group_readable_cache(monkeypatch):
    """The Spotify token cache must stay group-readable (0640) so every
    jasper-intsecrets member that builds a Spotify router can read refreshed
    tokens. Pinned so a future edit can't silently drop the mode and re-break
    the readers."""
    _install_fake_spotipy_cache_handler(monkeypatch)
    with tempfile.TemporaryDirectory() as d:
        cache_path = os.path.join(d, "jasper.json")
        handler = build_cache_handler(cache_path)
        handler.save_token_to_cache({
            "access_token": "x", "refresh_token": "y",
            "token_type": "Bearer", "expires_at": 0, "scope": "",
        })
        assert os.path.isfile(cache_path)
        mode = stat.S_IMODE(os.stat(cache_path).st_mode)
        assert mode == 0o640 == SPOTIFY_CACHE_FILE_MODE, (
            f"spotify cache mode {oct(mode)} != 0640 (group read) — the "
            "non-root readers would log 'Couldn't read cache'"
        )


def test_build_cache_handler_replaces_readonly_existing_cache(monkeypatch, tmp_path):
    """Re-link must recover when the old cache is readable but not writable.

    This is the production failure shape from a migrated cache:
    ``root:jasper-intsecrets 0640``. jasper-web can read it through the group,
    but spotipy's stock in-place writer cannot truncate it. The JTS handler
    must publish a replacement through the group-writable cache directory.
    """
    _install_fake_spotipy_cache_handler(monkeypatch)
    cache_path = tmp_path / "Jasper.json"
    cache_path.write_text('{"refresh_token": "revoked"}')
    cache_path.chmod(0o440)

    handler = build_cache_handler(str(cache_path))
    handler.save_token_to_cache({
        "access_token": "fresh", "refresh_token": "fresh-refresh",
        "token_type": "Bearer", "expires_at": 1, "scope": "",
    })

    data = json.loads(cache_path.read_text())
    assert data["access_token"] == "fresh"
    assert stat.S_IMODE(cache_path.stat().st_mode) == SPOTIFY_CACHE_FILE_MODE


def test_build_cache_handler_save_failure_propagates(monkeypatch, tmp_path):
    """OAuth callbacks must not claim success if the token never hit disk."""
    accounts_mod = _install_fake_spotipy_cache_handler(monkeypatch)

    def boom(*args, **kwargs):
        raise OSError("disk denied")

    monkeypatch.setattr(accounts_mod, "atomic_write_text", boom)
    handler = build_cache_handler(str(tmp_path / "Jasper.json"))

    try:
        handler.save_token_to_cache({"access_token": "fresh"})
    except OSError as exc:
        assert "disk denied" in str(exc)
    else:
        raise AssertionError("token cache write failure was swallowed")
