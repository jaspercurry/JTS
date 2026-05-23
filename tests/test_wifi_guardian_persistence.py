"""Tests for jasper.wifi_guardian_persistence.

Covers the storage layer for the WiFi profile guardian:
  - round-trip write/read of (SSID, PSK, key_mgmt)
  - atomic write doesn't leave .tmp leftovers
  - mode 0600 (PSK in the file)
  - failure modes: missing file, empty stash, wpa-eap rejected
  - PSK never appears in caplog records

The fsync-on-parent-dir step is best-effort and silently degraded on
filesystems that don't support it (tmpfs in some CI runners) — we test
the contents land correctly but don't try to assert on the directory
inode metadata.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from jasper.wifi_guardian_persistence import (
    DEFAULT_PATH,
    WifiStash,
    clear_stash,
    read_stash,
    write_stash,
)


def _path(tmp_path: Path) -> Path:
    return tmp_path / "wifi_guardian.env"


def test_default_path_constant():
    assert DEFAULT_PATH == "/var/lib/jasper/wifi_guardian.env"


def test_read_missing_returns_none(tmp_path):
    assert read_stash(_path(tmp_path)) is None


def test_round_trip_wpa_psk(tmp_path):
    p = _path(tmp_path)
    write_stash(p, "MyNetwork", "correcthorsebatterystaple", "wpa-psk")
    stash = read_stash(p)
    assert stash == WifiStash(
        ssid="MyNetwork", psk="correcthorsebatterystaple", key_mgmt="wpa-psk",
    )


def test_round_trip_open_network(tmp_path):
    """Open network: empty PSK, key_mgmt=none."""
    p = _path(tmp_path)
    write_stash(p, "GuestWifi", "", "none")
    stash = read_stash(p)
    assert stash == WifiStash(ssid="GuestWifi", psk="", key_mgmt="none")


def test_round_trip_psk_with_spaces(tmp_path):
    """WPA PSKs are 8-63 ASCII chars; spaces are legal. Make sure the
    parser doesn't trim them — would lock the user out of any network
    whose PSK was "correct horse battery staple" or similar."""
    p = _path(tmp_path)
    write_stash(p, "MyNet", "correct horse battery staple", "wpa-psk")
    stash = read_stash(p)
    assert stash is not None
    assert stash.psk == "correct horse battery staple"


def test_written_file_mode_is_0600(tmp_path):
    """PSK is in the file — must be root-only, even after a careless
    chmod on /var/lib/jasper."""
    p = _path(tmp_path)
    write_stash(p, "X", "y", "wpa-psk")
    mode = os.stat(p).st_mode & 0o777
    assert mode == 0o600


def test_no_temp_files_leak_on_success(tmp_path):
    p = _path(tmp_path)
    write_stash(p, "X", "y", "wpa-psk")
    leftover = [
        n for n in os.listdir(tmp_path) if n.startswith(".wifi_guardian.")
    ]
    assert leftover == []


def test_overwrite_replaces_contents(tmp_path):
    p = _path(tmp_path)
    write_stash(p, "Old", "oldpsk1", "wpa-psk")
    write_stash(p, "New", "newpsk2", "sae")
    stash = read_stash(p)
    assert stash == WifiStash(ssid="New", psk="newpsk2", key_mgmt="sae")


def test_write_empty_ssid_raises(tmp_path):
    with pytest.raises(ValueError, match="ssid"):
        write_stash(_path(tmp_path), "", "psk", "wpa-psk")


def test_write_wpa_eap_raises(tmp_path):
    """Enterprise auth is out of scope — defense-in-depth check at the
    write path so a buggy caller can't write a stash the guardian would
    refuse to act on."""
    with pytest.raises(ValueError, match="enterprise"):
        write_stash(_path(tmp_path), "EAP-Net", "secret", "wpa-eap")


def test_read_rejects_stash_with_wpa_eap(tmp_path, caplog):
    """Hand-edited file with key_mgmt=wpa-eap → read returns None
    (defensive). The guardian must not attempt to recreate enterprise
    networks."""
    p = _path(tmp_path)
    p.write_text(
        "JASPER_WIFI_SSID=EnterpriseNet\n"
        "JASPER_WIFI_PSK=secret\n"
        "JASPER_WIFI_KEY_MGMT=wpa-eap\n"
    )
    with caplog.at_level("INFO"):
        assert read_stash(p) is None


def test_read_missing_ssid_returns_none(tmp_path):
    """A stash with PSK but no SSID is unusable — we can't recreate a
    profile without knowing which network to connect to."""
    p = _path(tmp_path)
    p.write_text("JASPER_WIFI_PSK=secret\nJASPER_WIFI_KEY_MGMT=wpa-psk\n")
    assert read_stash(p) is None


def test_read_empty_ssid_returns_none(tmp_path):
    p = _path(tmp_path)
    p.write_text("JASPER_WIFI_SSID=\nJASPER_WIFI_PSK=p\n")
    assert read_stash(p) is None


def test_read_defaults_key_mgmt_to_none(tmp_path):
    """A stash with no key_mgmt entry resolves to ``none`` (open network).
    Preserves backward-compat for hand-edited stashes."""
    p = _path(tmp_path)
    p.write_text("JASPER_WIFI_SSID=X\nJASPER_WIFI_PSK=\n")
    stash = read_stash(p)
    assert stash is not None
    assert stash.key_mgmt == "none"


def test_read_ignores_comments_and_other_keys(tmp_path):
    p = _path(tmp_path)
    p.write_text(
        "# seeded by install.sh from active profile\n"
        "OTHER_KEY=99\n"
        "JASPER_WIFI_SSID=Home\n"
        "JASPER_WIFI_PSK=secret\n"
        "JASPER_WIFI_KEY_MGMT=wpa-psk\n"
    )
    stash = read_stash(p)
    assert stash == WifiStash(ssid="Home", psk="secret", key_mgmt="wpa-psk")


def test_read_handles_quoted_values(tmp_path):
    """The install.sh migration helper writes raw nmcli output; nmcli
    quotes values that contain whitespace. Make sure the parser strips
    a single layer of matching quotes."""
    p = _path(tmp_path)
    p.write_text(
        'JASPER_WIFI_SSID="My Home Wifi"\n'
        'JASPER_WIFI_PSK="correct horse"\n'
        'JASPER_WIFI_KEY_MGMT="wpa-psk"\n'
    )
    stash = read_stash(p)
    assert stash == WifiStash(
        ssid="My Home Wifi", psk="correct horse", key_mgmt="wpa-psk",
    )


def test_read_handles_pathlike(tmp_path):
    p = _path(tmp_path)
    write_stash(str(p), "X", "y", "wpa-psk")
    assert read_stash(str(p)) is not None
    assert read_stash(p) is not None


def test_clear_stash_removes_file(tmp_path):
    p = _path(tmp_path)
    write_stash(p, "X", "y", "wpa-psk")
    assert p.exists()
    clear_stash(p)
    assert not p.exists()


def test_clear_stash_missing_is_success(tmp_path):
    """rm -f semantics — clearing a missing stash is not an error."""
    clear_stash(_path(tmp_path))  # doesn't raise


def test_write_creates_parent_directory(tmp_path):
    """Match mic_mute_persistence's contract: nested paths Just Work."""
    p = tmp_path / "nested" / "deeper" / "wifi_guardian.env"
    write_stash(p, "X", "y", "wpa-psk")
    assert read_stash(p) is not None


def test_psk_never_appears_in_log_records(tmp_path, caplog):
    """The whole point of the redaction discipline. Write, read,
    re-write, clear — none of these should leak the PSK to logs."""
    p = _path(tmp_path)
    psk = "super-secret-psk-do-not-log-32xq"
    with caplog.at_level("DEBUG"):
        write_stash(p, "X", psk, "wpa-psk")
        read_stash(p)
        write_stash(p, "X", psk + "-changed", "wpa-psk")
        clear_stash(p)
    for record in caplog.records:
        assert psk not in record.getMessage()
        assert psk not in str(record.args or ())


def test_write_failure_is_raised_for_callers_to_handle(tmp_path, monkeypatch):
    """Unlike mic_mute_persistence (which swallows), this module's
    write_stash raises so the wizard's hook can log a warning AND
    surface the drift via doctor. The actual swallow happens one layer
    up — see `_stash_after_connect` in wifi_setup."""
    import jasper.wifi_guardian_persistence as mod

    def boom(*args, **kwargs):
        raise OSError("simulated permission denied")

    monkeypatch.setattr(mod.tempfile, "mkstemp", boom)
    with pytest.raises(OSError, match="simulated"):
        write_stash(_path(tmp_path), "X", "y", "wpa-psk")


def test_fsync_failure_does_not_block_write(tmp_path, monkeypatch, caplog):
    """Some filesystems (tmpfs in CI) don't support directory fsync.
    The file contents are already on disk from the per-FD fsync; the
    parent-dir fsync failure logs at DEBUG and the write succeeds."""
    import jasper.wifi_guardian_persistence as mod

    real_fsync = os.fsync
    seen_calls = []

    def selective_fsync(fd):
        # Fail only the directory fsync (the one on a dir FD, which we
        # detect by checking that the FD is a directory). Per-file
        # fsync (during contents write) still works normally.
        try:
            st = os.fstat(fd)
        except OSError:
            return real_fsync(fd)
        import stat
        if stat.S_ISDIR(st.st_mode):
            seen_calls.append("dir")
            raise OSError("simulated parent-dir fsync failure")
        return real_fsync(fd)

    monkeypatch.setattr(mod.os, "fsync", selective_fsync)
    with caplog.at_level("DEBUG"):
        write_stash(_path(tmp_path), "X", "y", "wpa-psk")
    # File was still written successfully.
    assert read_stash(_path(tmp_path)) is not None
    # The dir fsync was attempted and failed gracefully.
    assert "dir" in seen_calls
