from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path

import pytest

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


def test_build_cache_handler_writes_group_readable_cache():
    """WS1 Phase 3b: the Spotify token cache must be group-`jasper`-readable
    (0640) so the now-non-root jasper-control (/transport router) and jasper-web
    (/spotify wizard) can read the token jasper-voice persists. spotipy's stock
    CacheFileHandler writes it 0600 owner-only (the dropped readers then log
    "Couldn't read cache" on every poll); build_cache_handler re-chmods 0640
    after every save. Pinned so a future edit can't silently drop the chmod and
    re-break the readers."""
    pytest.importorskip("spotipy")
    with tempfile.TemporaryDirectory() as d:
        cache_path = os.path.join(d, "jasper.json")
        handler = build_cache_handler(cache_path)
        # spotipy CacheFileHandler.save_token_to_cache json-dumps the dict; the
        # subclass chmods 0640 afterward regardless of the writer's umask.
        handler.save_token_to_cache({
            "access_token": "x", "refresh_token": "y",
            "token_type": "Bearer", "expires_at": 0, "scope": "",
        })
        assert os.path.isfile(cache_path)
        mode = stat.S_IMODE(os.stat(cache_path).st_mode)
        assert mode == 0o640 == SPOTIFY_CACHE_FILE_MODE, (
            f"spotify cache mode {oct(mode)} != 0640 (group jasper read) — the "
            "non-root readers would log 'Couldn't read cache'"
        )
