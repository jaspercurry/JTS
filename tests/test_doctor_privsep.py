# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free behaviour tests for the privsep readability check core.

The check runs as root on the Pi (so it can't use os.access — that would read
everything) and reasons about a daemon's identity vs each file's mode. These
tests exercise the pure, identity-parameterized core with tmp files and
synthetic identities — no systemctl, no real jasper users:

- the headline contract: a non-root daemon's input at 0600-root-style (owner
  bits only, daemon owns neither uid nor group) FAILS; the same file at 0640
  group-`jasper` (daemon shares the group) PASSES;
- glob expansion flags only the unreadable member;
- absent inputs are not flagged (absent != the present-but-unreadable bug);
- the household_secret verdict: present-but-unreadable = gate fail-safe-OPENED,
  present-and-readable = gate enforced, absent = not paired.
"""
from __future__ import annotations

import os
from pathlib import Path

from jasper.cli.doctor import privsep


def _make(path: Path, mode: int) -> os.stat_result:
    path.write_text("x")
    os.chmod(path, mode)
    return path.stat()


# --------------------------------------------------------------------------- #
# _process_can_read — POSIX owner/group/other precedence
# --------------------------------------------------------------------------- #
def test_process_can_read_owner_group_other(tmp_path: Path):
    st = _make(tmp_path / "f", 0o640)
    gid = st.st_gid
    # Owner with read bit (owner path wins even when group lacks the bit).
    assert privsep._process_can_read(st, st.st_uid, frozenset())
    # Not owner, shares group, group-read set -> readable.
    assert privsep._process_can_read(st, 999_999, frozenset({gid}))
    # Not owner, does NOT share group, no other-read on 0640 -> NOT readable.
    assert not privsep._process_can_read(st, 999_999, frozenset({777_777}))
    # 0600: group member still can't read (no group bit).
    st600 = _make(tmp_path / "g", 0o600)
    assert not privsep._process_can_read(st600, 999_999, frozenset({st600.st_gid}))
    # 0644: other-read lets a stranger read.
    st644 = _make(tmp_path / "h", 0o644)
    assert privsep._process_can_read(st644, 999_999, frozenset({777_777}))


# --------------------------------------------------------------------------- #
# _classify_readable_inputs — the headline 0600-fails / 0640-passes contract
# --------------------------------------------------------------------------- #
def test_input_at_0640_group_jasper_passes(tmp_path: Path):
    f = tmp_path / "voice_provider.env"
    st = _make(f, 0o640)
    # Daemon shares the file's group but does not own it (the real scenario:
    # jasper-control reading a jasper-web-written, group-`jasper` file).
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-control",
        (str(f),),
        uid=999_999,
        gids=frozenset({st.st_gid}),
        user="jasper-control",
    )
    assert result.status == "ok", result.detail
    assert "1 input(s) readable" in result.detail


def test_input_at_0600_root_fails_naming_file_and_mode(tmp_path: Path):
    f = tmp_path / "voice_provider.env"
    _make(f, 0o600)
    # 0600 owned by someone the daemon is not, group not shared -> unreadable.
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-control",
        (str(f),),
        uid=999_999,
        gids=frozenset({777_777}),
        user="jasper-control",
    )
    assert result.status == "warn", result.detail
    assert str(f) in result.detail
    assert "0o600" in result.detail
    assert "jasper-control cannot read" in result.detail


def test_wrong_group_0640_fails(tmp_path: Path):
    """0640 but the daemon shares neither uid nor the file's group -> unreadable
    (the bt_roles.json 0640-but-wrong-group shape)."""
    f = tmp_path / "bt_roles.json"
    _make(f, 0o640)
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-web",
        (str(f),),
        uid=999_999,
        gids=frozenset({777_777}),
        user="jasper-web",
    )
    assert result.status == "warn"
    assert str(f) in result.detail


def test_glob_flags_only_unreadable_member(tmp_path: Path):
    configs = tmp_path / "configs"
    configs.mkdir()
    ok_cfg = configs / "sound_current.yml"
    bad_cfg = configs / "grouping_leader.yml"
    ok_st = _make(ok_cfg, 0o640)
    _make(bad_cfg, 0o600)
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-control",
        (str(configs / "*.yml"),),
        uid=999_999,
        gids=frozenset({ok_st.st_gid}),
        user="jasper-control",
    )
    assert result.status == "warn"
    assert str(bad_cfg) in result.detail
    assert str(ok_cfg) not in result.detail


def test_absent_inputs_are_not_flagged(tmp_path: Path):
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-control",
        (str(tmp_path / "does-not-exist.env"), str(tmp_path / "none.yml")),
        uid=999_999,
        gids=frozenset({777_777}),
        user="jasper-control",
    )
    assert result.status == "ok"
    assert "no declared inputs present yet" in result.detail


def test_mixed_present_and_absent_only_checks_present(tmp_path: Path):
    present = tmp_path / "transit.env"
    st = _make(present, 0o640)
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-web",
        (str(present), str(tmp_path / "absent.env")),
        uid=999_999,
        gids=frozenset({st.st_gid}),
        user="jasper-web",
    )
    assert result.status == "ok"
    assert "1 input(s) readable" in result.detail


# --------------------------------------------------------------------------- #
# household_secret verdict — the fail-safe-open observability case
# --------------------------------------------------------------------------- #
def test_household_secret_present_unreadable_warns_gate_open(tmp_path: Path):
    secret = tmp_path / "household_secret"
    st = _make(secret, 0o600)
    result = privsep._household_secret_verdict(
        st, uid=999_999, gids=frozenset({777_777}), user="jasper-control"
    )
    assert result.status == "warn"
    assert "fail-safe-OPENED" in result.detail


def test_household_secret_present_readable_ok_gate_enforced(tmp_path: Path):
    secret = tmp_path / "household_secret"
    st = _make(secret, 0o640)
    result = privsep._household_secret_verdict(
        st, uid=999_999, gids=frozenset({st.st_gid}), user="jasper-control"
    )
    assert result.status == "ok"
    assert "gate is enforced" in result.detail


def test_household_secret_absent_is_ok(tmp_path, monkeypatch):
    """Absent secret = not paired (ok). This path returns before any systemctl
    call, so it is fully hardware-free."""
    from jasper.control import household_credential

    monkeypatch.setattr(
        household_credential, "SECRET_FILE", str(tmp_path / "nope"), raising=True
    )
    result = privsep.check_household_secret_readable()
    assert result.status == "ok"
    assert "not paired" in result.detail


# --------------------------------------------------------------------------- #
# Integration: the decorated checks must be total (never crash) off the Pi.
# --------------------------------------------------------------------------- #
def test_decorated_checks_are_total_without_systemctl(monkeypatch):
    """With systemctl unavailable every per-daemon check returns a skip-ok,
    never raising — the doctor must stay total on a dev host."""
    monkeypatch.setattr(privsep, "_unit_runtime_identity", lambda unit: None)
    for fn in (
        privsep.check_control_readable_inputs,
        privsep.check_web_readable_inputs,
        privsep.check_mux_readable_inputs,
        privsep.check_voice_readable_inputs,
    ):
        result = fn()
        assert result.status == "ok"
        assert "skipped" in result.detail


def test_not_installed_unit_skips(monkeypatch):
    monkeypatch.setattr(
        privsep,
        "_unit_runtime_identity",
        lambda unit: {"LoadState": "not-found", "User": ""},
    )
    result = privsep.check_voice_readable_inputs()
    assert result.status == "ok"
    assert "not installed" in result.detail


def test_root_unit_skips(monkeypatch):
    """A unit running as root (e.g. streambox jasper-web) reads everything -> skip."""
    monkeypatch.setattr(
        privsep,
        "_unit_runtime_identity",
        lambda unit: {"LoadState": "loaded", "User": "root"},
    )
    result = privsep.check_web_readable_inputs()
    assert result.status == "ok"
    assert "runs as root" in result.detail


def test_classify_warn_overflow_truncates(tmp_path: Path):
    """Many unreadable files -> detail truncates with a (+N more) marker."""
    paths = []
    for i in range(9):
        f = tmp_path / f"f{i}.env"
        _make(f, 0o600)
        paths.append(str(f))
    result = privsep._classify_readable_inputs(
        "daemon reads: jasper-control",
        tuple(paths),
        uid=999_999,
        gids=frozenset({777_777}),
        user="jasper-control",
    )
    assert result.status == "warn"
    assert "(+3 more)" in result.detail
