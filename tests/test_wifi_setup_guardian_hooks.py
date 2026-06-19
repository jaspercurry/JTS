"""Tests for the WiFi profile guardian hooks in jasper.web.wifi_setup.

The wizard's connect_new / connect_saved / forget code paths each call
into the guardian stash helpers. We mock _run_nmcli to control the
returncode + stdout, then assert the stash file ends up in the right
state.

PSK redaction is also verified here: even though the wizard's
``_run_nmcli_secret`` is the canonical PSK-on-the-wire scrubber, the
guardian hook is the only thing that PERSISTS the PSK to disk. We
double-check it doesn't accidentally log the PSK from the hook layer
either.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable
from unittest.mock import patch

import pytest

from jasper import wifi_guardian_persistence


def _mock_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["nmcli"], returncode=returncode,
        stdout=stdout, stderr=stderr,
    )


def _scripted_nmcli(steps: Iterable[subprocess.CompletedProcess]):
    """Build a side_effect that returns the next CompletedProcess on
    each call. Tests script the exact sequence of nmcli responses they
    want the wizard's call-sites to see."""
    steps_iter = iter(steps)

    def side_effect(cmd, *args, **kwargs):
        try:
            return next(steps_iter)
        except StopIteration:
            # Default: succeed with empty stdout. Catches "the call
            # site issued more nmclis than the test scripted" without
            # forcing every test to enumerate every probe.
            return _mock_proc()

    return side_effect


def _stash_path(tmp_path: Path) -> Path:
    return tmp_path / "wifi_guardian.env"


@pytest.fixture
def stash_path(tmp_path, monkeypatch):
    """Point the wizard's stash path at tmp_path. The wizard reads the
    env var at module-import time, so we have to monkeypatch the
    module-level constant directly too."""
    p = _stash_path(tmp_path)
    monkeypatch.setenv("JASPER_WIFI_STASH_FILE", str(p))
    import jasper.web.wifi_setup as wifi_setup
    monkeypatch.setattr(wifi_setup, "_STASH_PATH", str(p))
    return p


# ====== connect_new hook ======


def test_connect_new_success_writes_stash(stash_path, monkeypatch):
    """The canonical happy path: user pastes PSK into the wizard,
    nmcli connect returns 0 → stash gets the SSID + PSK + key_mgmt."""
    import jasper.web.wifi_setup as wifi_setup

    side_effect = _scripted_nmcli([
        _mock_proc(),  # _current_wifi: NAME,UUID,TYPE,DEVICE (empty)
        _mock_proc(),  # _profile_exists: NAME (empty)
        _mock_proc(returncode=0),  # the actual connect
        _mock_proc(returncode=0),  # _harden_wifi_profile
        # _resolve_key_mgmt → return wpa-psk
        _mock_proc(returncode=0,
                   stdout="802-11-wireless-security.key-mgmt:wpa-psk\n"),
    ])
    with patch.object(wifi_setup, "_run_nmcli", side_effect=side_effect), \
         patch.object(wifi_setup, "_run_nmcli_secret", side_effect=side_effect):
        ok, msg = wifi_setup.connect_new("Home", "myhomepsk")

    assert ok is True
    assert "Home" in msg
    stash = wifi_guardian_persistence.read_stash(stash_path)
    assert stash is not None
    assert stash.ssid == "Home"
    assert stash.psk == "myhomepsk"
    assert stash.key_mgmt == "wpa-psk"


def test_connect_new_success_hardens_nm_profile(stash_path, monkeypatch):
    """A successful wizard connect should persist NM settings that survive
    router flaps: retry forever and keep Wi-Fi power-save disabled."""
    import jasper.web.wifi_setup as wifi_setup

    calls: list[list[str]] = []

    def nmcli_side_effect(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[:4] == ["nmcli", "-t", "-f", "802-11-wireless-security.key-mgmt"]:
            return _mock_proc(
                returncode=0,
                stdout="802-11-wireless-security.key-mgmt:wpa-psk\n",
            )
        return _mock_proc(returncode=0)

    with patch.object(wifi_setup, "_run_nmcli", side_effect=nmcli_side_effect), \
         patch.object(
             wifi_setup, "_run_nmcli_secret", side_effect=nmcli_side_effect,
         ):
        ok, _ = wifi_setup.connect_new("Home", "myhomepsk")

    assert ok is True
    assert [
        "nmcli", "connection", "modify", "Home",
        "connection.autoconnect", "yes",
        "connection.autoconnect-retries", "0",
        "802-11-wireless.powersave", "2",
    ] in calls


def test_connect_new_hardening_nonzero_does_not_block_connect(stash_path):
    """Contract (AGENTS.md): a profile-hardening failure MUST NOT turn a
    successful user connect into a failed one. nmcli returning non-zero on
    the `connection modify` hardening call is logged and swallowed."""
    import jasper.web.wifi_setup as wifi_setup

    def nmcli_side_effect(cmd, *args, **kwargs):
        if cmd[:3] == ["nmcli", "connection", "modify"]:
            return _mock_proc(returncode=1, stderr="Error: hardening failed")
        if cmd[:4] == ["nmcli", "-t", "-f", "802-11-wireless-security.key-mgmt"]:
            return _mock_proc(
                returncode=0,
                stdout="802-11-wireless-security.key-mgmt:wpa-psk\n",
            )
        return _mock_proc(returncode=0)

    with patch.object(wifi_setup, "_run_nmcli", side_effect=nmcli_side_effect), \
         patch.object(wifi_setup, "_run_nmcli_secret", side_effect=nmcli_side_effect):
        ok, _ = wifi_setup.connect_new("Home", "myhomepsk")

    assert ok is True


def test_connect_new_hardening_oserror_does_not_block_connect(stash_path):
    """Even an exception from the hardening call (e.g. nmcli not on PATH)
    must not fail the connect — _harden_wifi_profile swallows OSError."""
    import jasper.web.wifi_setup as wifi_setup

    def nmcli_side_effect(cmd, *args, **kwargs):
        if cmd[:3] == ["nmcli", "connection", "modify"]:
            raise FileNotFoundError("nmcli not found")
        if cmd[:4] == ["nmcli", "-t", "-f", "802-11-wireless-security.key-mgmt"]:
            return _mock_proc(
                returncode=0,
                stdout="802-11-wireless-security.key-mgmt:wpa-psk\n",
            )
        return _mock_proc(returncode=0)

    with patch.object(wifi_setup, "_run_nmcli", side_effect=nmcli_side_effect), \
         patch.object(wifi_setup, "_run_nmcli_secret", side_effect=nmcli_side_effect):
        ok, _ = wifi_setup.connect_new("Home", "myhomepsk")

    assert ok is True


def test_connect_saved_hardening_failure_does_not_block_connect(stash_path):
    """Same best-effort contract on the saved-profile activation path."""
    import jasper.web.wifi_setup as wifi_setup

    def nmcli_side_effect(cmd, *args, **kwargs):
        if cmd[:3] == ["nmcli", "connection", "modify"]:
            return _mock_proc(returncode=1, stderr="Error: hardening failed")
        return _mock_proc(returncode=0)

    with patch.object(wifi_setup, "_run_nmcli", side_effect=nmcli_side_effect), \
         patch.object(wifi_setup, "_run_nmcli_secret", side_effect=nmcli_side_effect):
        ok, _ = wifi_setup.connect_saved("Home")

    assert ok is True


def test_connect_new_open_network_writes_stash(stash_path, monkeypatch):
    """Open network: no password arg → stash gets empty PSK +
    key_mgmt=none."""
    import jasper.web.wifi_setup as wifi_setup

    side_effect = _scripted_nmcli([
        _mock_proc(),  # _current_wifi
        _mock_proc(),  # _profile_exists
        _mock_proc(returncode=0),  # connect
        _mock_proc(returncode=0),  # _harden_wifi_profile
        _mock_proc(returncode=0,
                   stdout="802-11-wireless-security.key-mgmt:\n"),
    ])
    with patch.object(wifi_setup, "_run_nmcli", side_effect=side_effect), \
         patch.object(wifi_setup, "_run_nmcli_secret", side_effect=side_effect):
        ok, _ = wifi_setup.connect_new("GuestNet", None)

    assert ok is True
    stash = wifi_guardian_persistence.read_stash(stash_path)
    assert stash is not None
    assert stash.psk == ""
    # No key-mgmt reported by nmcli → defaults to "none".
    assert stash.key_mgmt == "none"


def test_connect_new_retries_hidden_on_ssid_lookup_failure(
    stash_path, monkeypatch,
):
    """Manual join should work for hidden SSIDs and scan-suppressed
    radios: if nmcli can't find the SSID in the scan cache, retry with
    `hidden yes` before rollback/cleanup."""
    import jasper.web.wifi_setup as wifi_setup

    calls: list[list[str]] = []

    def nmcli_side_effect(cmd, *args, **kwargs):
        calls.append(list(cmd))
        if cmd[-2:] == ["hidden", "yes"]:
            return _mock_proc(returncode=0)
        if "connect" in cmd:
            return _mock_proc(
                returncode=10,
                stderr="Error: No network with SSID 'HiddenHome' found\n",
            )
        if cmd[:4] == ["nmcli", "-t", "-f", "802-11-wireless-security.key-mgmt"]:
            return _mock_proc(
                returncode=0,
                stdout="802-11-wireless-security.key-mgmt:wpa-psk\n",
            )
        return _mock_proc()

    with patch.object(wifi_setup, "_run_nmcli", side_effect=nmcli_side_effect), \
         patch.object(
             wifi_setup, "_run_nmcli_secret", side_effect=nmcli_side_effect,
         ):
        ok, msg = wifi_setup.connect_new("HiddenHome", "myhomepsk")

    assert ok is True
    assert "HiddenHome" in msg
    assert any(call[-2:] == ["hidden", "yes"] for call in calls)
    stash = wifi_guardian_persistence.read_stash(stash_path)
    assert stash is not None
    assert stash.ssid == "HiddenHome"
    assert stash.psk == "myhomepsk"


def test_connect_new_explicit_hidden_uses_hidden_yes(stash_path, monkeypatch):
    """The manual form's Hidden checkbox should go straight to
    `hidden yes` without depending on the retry heuristic."""
    import jasper.web.wifi_setup as wifi_setup

    calls: list[list[str]] = []

    def nmcli_side_effect(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return _mock_proc(
            returncode=0,
            stdout="802-11-wireless-security.key-mgmt:wpa-psk\n",
        )

    with patch.object(wifi_setup, "_run_nmcli", side_effect=nmcli_side_effect), \
         patch.object(
             wifi_setup, "_run_nmcli_secret", side_effect=nmcli_side_effect,
         ):
        ok, _ = wifi_setup.connect_new("HiddenHome", "p", hidden=True)

    assert ok is True
    connect_calls = [call for call in calls if "connect" in call]
    assert connect_calls
    assert connect_calls[0][-2:] == ["hidden", "yes"]


def test_connect_new_failure_does_not_write_stash(stash_path):
    """nmcli returns non-zero → connect failed → stash unchanged. The
    user is in some rollback state; we mustn't promise recovery for a
    network we couldn't establish."""
    import jasper.web.wifi_setup as wifi_setup

    side_effect = _scripted_nmcli([
        _mock_proc(),  # _current_wifi
        _mock_proc(),  # _profile_exists
        _mock_proc(returncode=4,
                   stderr="Error: Connection activation failed: (4) ...\n"),
    ])
    with patch.object(wifi_setup, "_run_nmcli", side_effect=side_effect), \
         patch.object(wifi_setup, "_run_nmcli_secret", side_effect=side_effect):
        ok, _ = wifi_setup.connect_new("Home", "wrongpsk")

    assert ok is False
    assert not stash_path.exists()


def test_connect_new_stash_failure_does_not_block_connect(
    stash_path, monkeypatch, caplog,
):
    """If the stash write itself blows up (full disk, permission flip),
    the user's WiFi is up and we must not flip the connect to False.
    Log a warning so doctor surfaces the drift."""
    import jasper.web.wifi_setup as wifi_setup

    side_effect = _scripted_nmcli([
        _mock_proc(),  # _current_wifi
        _mock_proc(),  # _profile_exists
        _mock_proc(returncode=0),  # connect
        _mock_proc(returncode=0),  # _harden_wifi_profile
        _mock_proc(returncode=0,
                   stdout="802-11-wireless-security.key-mgmt:wpa-psk\n"),
    ])

    def boom(*args, **kwargs):
        raise OSError("simulated full disk")

    with patch.object(wifi_setup, "_run_nmcli", side_effect=side_effect), \
         patch.object(wifi_setup, "_run_nmcli_secret", side_effect=side_effect), \
         patch.object(
             wifi_guardian_persistence, "write_stash", side_effect=boom,
         ):
        with caplog.at_level("WARNING"):
            ok, _ = wifi_setup.connect_new("Home", "p")

    assert ok is True  # MUST stay True — stash failure can't block.
    assert any(
        "stash_write_failed" in r.getMessage() for r in caplog.records
    )


def test_connect_new_enterprise_skips_stash(stash_path, monkeypatch, caplog):
    """If nmcli reports the just-connected network as wpa-eap (operator
    bypassed the wizard's filter somehow), skip the stash write — the
    guardian wouldn't recreate it anyway."""
    import jasper.web.wifi_setup as wifi_setup

    side_effect = _scripted_nmcli([
        _mock_proc(),  # _current_wifi
        _mock_proc(),  # _profile_exists
        _mock_proc(returncode=0),  # connect
        _mock_proc(returncode=0),  # _harden_wifi_profile
        _mock_proc(returncode=0,
                   stdout="802-11-wireless-security.key-mgmt:wpa-eap\n"),
    ])
    with patch.object(wifi_setup, "_run_nmcli", side_effect=side_effect), \
         patch.object(wifi_setup, "_run_nmcli_secret", side_effect=side_effect):
        with caplog.at_level("INFO"):
            ok, _ = wifi_setup.connect_new("EnterpriseNet", "ignored")

    assert ok is True
    assert not stash_path.exists()
    assert any(
        "stash_skip" in r.getMessage() and "enterprise" in r.getMessage()
        for r in caplog.records
    )


def test_connect_new_psk_never_in_log_records(stash_path, caplog):
    """The wizard's connect hook persists the PSK to disk; verify the
    PSK does NOT then appear in any log line emitted by the hook
    itself."""
    import jasper.web.wifi_setup as wifi_setup
    psk = "super-secret-pin-xx99"

    side_effect = _scripted_nmcli([
        _mock_proc(),
        _mock_proc(),
        _mock_proc(returncode=0),
        _mock_proc(returncode=0),  # _harden_wifi_profile
        _mock_proc(returncode=0,
                   stdout="802-11-wireless-security.key-mgmt:wpa-psk\n"),
    ])
    with patch.object(wifi_setup, "_run_nmcli", side_effect=side_effect), \
         patch.object(wifi_setup, "_run_nmcli_secret", side_effect=side_effect):
        with caplog.at_level("DEBUG"):
            wifi_setup.connect_new("Home", psk)

    for record in caplog.records:
        assert psk not in record.getMessage()


# ====== connect_saved hook ======


def test_connect_saved_refreshes_stash_from_nmcli_secrets(stash_path):
    """User clicks a Saved-network row in the wizard. The PSK lives in
    NM's keyfile, not on the wire. The hook reads it via `nmcli -s` and
    refreshes the stash. SSID may differ from profile NAME (netplan-
    seeded profiles)."""
    import jasper.web.wifi_setup as wifi_setup

    side_effect = _scripted_nmcli([
        _mock_proc(returncode=0),  # connection up
        _mock_proc(returncode=0),  # _harden_wifi_profile
        # _read_profile_secrets: SSID + PSK + key_mgmt
        _mock_proc(returncode=0, stdout=(
            "802-11-wireless.ssid:HomeRealSSID\n"
            "802-11-wireless-security.psk:nmstoredpsk\n"
            "802-11-wireless-security.key-mgmt:wpa-psk\n"
        )),
    ])
    with patch.object(wifi_setup, "_run_nmcli", side_effect=side_effect):
        ok, _ = wifi_setup.connect_saved("netplan-wlan0-Home")

    assert ok is True
    stash = wifi_guardian_persistence.read_stash(stash_path)
    assert stash is not None
    # The stash carries the SSID, not the NM profile name.
    assert stash.ssid == "HomeRealSSID"
    assert stash.psk == "nmstoredpsk"
    assert stash.key_mgmt == "wpa-psk"


def test_connect_saved_failure_does_not_touch_stash(stash_path):
    """`nmcli connection up` failed → don't refresh the stash. Mirrors
    connect_new's failure path."""
    import jasper.web.wifi_setup as wifi_setup

    # Pre-write a stash so we can assert it's UNTOUCHED.
    wifi_guardian_persistence.write_stash(
        stash_path, "PriorNet", "priorpsk", "wpa-psk",
    )

    side_effect = _scripted_nmcli([
        _mock_proc(returncode=4, stderr="Error: failed\n"),
    ])
    with patch.object(wifi_setup, "_run_nmcli", side_effect=side_effect):
        ok, _ = wifi_setup.connect_saved("SomeProfile")

    assert ok is False
    stash = wifi_guardian_persistence.read_stash(stash_path)
    assert stash is not None
    assert stash.ssid == "PriorNet"


# ====== forget hook ======


def test_forget_clears_stash_when_matches_stashed_ssid(stash_path):
    """User forgets `Home`. Stash also points at `Home`. → clear stash
    so the next-boot guardian doesn't recreate the network the operator
    just told us to forget."""
    import jasper.web.wifi_setup as wifi_setup

    wifi_guardian_persistence.write_stash(stash_path, "Home", "p", "wpa-psk")
    assert stash_path.exists()

    side_effect = _scripted_nmcli([
        # _ssid_for_profile: returns "Home"
        _mock_proc(returncode=0, stdout="802-11-wireless.ssid:Home\n"),
        # delete
        _mock_proc(returncode=0),
    ])
    with patch.object(wifi_setup, "_run_nmcli", side_effect=side_effect):
        ok, _ = wifi_setup.forget("Home")

    assert ok is True
    assert not stash_path.exists()


def test_forget_preserves_stash_when_different_ssid(stash_path):
    """User forgets `GuestNet` while stash points at `Home`. → leave
    the stash alone. Forgetting one network must not invalidate
    recovery for another."""
    import jasper.web.wifi_setup as wifi_setup

    wifi_guardian_persistence.write_stash(
        stash_path, "Home", "homepsk", "wpa-psk",
    )

    side_effect = _scripted_nmcli([
        _mock_proc(returncode=0, stdout="802-11-wireless.ssid:GuestNet\n"),
        _mock_proc(returncode=0),  # delete
    ])
    with patch.object(wifi_setup, "_run_nmcli", side_effect=side_effect):
        ok, _ = wifi_setup.forget("GuestNet")

    assert ok is True
    stash = wifi_guardian_persistence.read_stash(stash_path)
    assert stash is not None
    assert stash.ssid == "Home"


def test_forget_does_not_clear_stash_on_failed_delete(stash_path):
    """nmcli delete failed → don't touch the stash. The profile is
    still there; recovery intent is still valid."""
    import jasper.web.wifi_setup as wifi_setup

    wifi_guardian_persistence.write_stash(stash_path, "Home", "p", "wpa-psk")

    side_effect = _scripted_nmcli([
        _mock_proc(returncode=0, stdout="802-11-wireless.ssid:Home\n"),
        _mock_proc(returncode=1, stderr="Error: delete failed\n"),
    ])
    with patch.object(wifi_setup, "_run_nmcli", side_effect=side_effect):
        ok, _ = wifi_setup.forget("Home")

    assert ok is False
    # Stash is still intact.
    stash = wifi_guardian_persistence.read_stash(stash_path)
    assert stash is not None
    assert stash.ssid == "Home"


def test_forget_no_stash_to_clear_is_silent(stash_path):
    """User forgets a profile but there's no stash to begin with —
    no error, no log spam, the forget succeeds anyway."""
    import jasper.web.wifi_setup as wifi_setup

    side_effect = _scripted_nmcli([
        _mock_proc(returncode=0, stdout="802-11-wireless.ssid:Home\n"),
        _mock_proc(returncode=0),
    ])
    with patch.object(wifi_setup, "_run_nmcli", side_effect=side_effect):
        ok, _ = wifi_setup.forget("Home")
    assert ok is True
