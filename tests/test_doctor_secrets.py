# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free behaviour tests for the secret-compartment posture check.

The check runs as root on the Pi (so it can't use os.access — that reads
everything) and reasons about each compartment member's / non-member's identity
vs the dir + each secret file's mode. These tests exercise the pure,
identity-parameterized core with tmp files and synthetic identities — no
systemctl, no real jasper users:

- the headline two-sided contract: a 0640 group-`<compartment>` secret that a
  member shares the group of PASSES; the same file widened to o+r (world) or to a
  group a NON-member holds FAILS over-exposure; a 0600 file a member can't read
  WARNs availability;
- FAIL (over-exposure / confidentiality) outranks WARN (under-availability);
- the dir contract: 2770 correct-group PASSES, missing setgid / wrong group WARNs,
  a world/non-member-traversable dir FAILS;
- absent compartment → ok (not configured); the decorated checks stay total
  (never raise) off the Pi.
"""
from __future__ import annotations

import grp
import os
from pathlib import Path

from jasper.cli.doctor import secret_compartments as sc


def _ident(uid: int, gids, user: str) -> sc._Identity:
    return sc._Identity(uid=uid, gids=frozenset(gids), user=user)


def _comp(directory: Path, *files: str, group: str = "jasper-secrets") -> sc.SecretCompartment:
    return sc.SecretCompartment(
        group=group,
        directory=str(directory),
        member_units=("jasper-voice", "jasper-web"),
        files=tuple(str(directory / f) for f in files),
    )


def _group_name(st: os.stat_result) -> str:
    """The real group name of a tmp path — used where a test needs the dir/file to
    BE the compartment group (the host's tmp group stands in for jasper-secrets)."""
    return grp.getgrgid(st.st_gid).gr_name


def _mk_dir(path: Path, mode: int) -> os.stat_result:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)
    return path.stat()


def _mk_file(path: Path, mode: int) -> os.stat_result:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("SECRET-VALUE-NEVER-IN-OUTPUT")
    os.chmod(path, mode)
    return path.stat()


# --------------------------------------------------------------------------- #
# _file_over_exposed_to / _file_unreadable_by — the two sides
# --------------------------------------------------------------------------- #
def test_file_over_exposed_world_bit(tmp_path: Path):
    st = _mk_file(tmp_path / "voice_keys.env", 0o644)  # o+r
    # No non-members at all, yet the world bit alone is over-exposure.
    assert sc._file_over_exposed_to(st, []) == ["world"]


def test_file_over_exposed_to_nonmember_sharing_broad_group(tmp_path: Path):
    """0640 but the file's group is one a NON-member holds (the 'regressed back to
    group jasper' shape) → that non-member can read → exposed."""
    st = _mk_file(tmp_path / "voice_keys.env", 0o640)
    nonmember = _ident(999_999, {st.st_gid}, "jasper-input")  # shares the file group
    assert "jasper-input" in sc._file_over_exposed_to(st, [nonmember])


def test_file_not_exposed_when_group_is_compartment_only(tmp_path: Path):
    """0640 group the non-member does NOT hold, no o-bits → not exposed."""
    st = _mk_file(tmp_path / "voice_keys.env", 0o640)
    nonmember = _ident(999_999, {777_777}, "jasper-input")
    assert sc._file_over_exposed_to(st, [nonmember]) == []


def test_file_unreadable_by_member_on_0600(tmp_path: Path):
    st = _mk_file(tmp_path / "voice_keys.env", 0o600)
    member = _ident(999_999, {st.st_gid}, "jasper-web")  # shares group but 0600
    assert sc._file_unreadable_by(st, [member]) == ["jasper-web"]


def test_file_readable_by_member_on_0640(tmp_path: Path):
    st = _mk_file(tmp_path / "voice_keys.env", 0o640)
    member = _ident(999_999, {st.st_gid}, "jasper-web")
    assert sc._file_unreadable_by(st, [member]) == []


# --------------------------------------------------------------------------- #
# _classify_compartment — aggregate verdict
# --------------------------------------------------------------------------- #
def test_absent_dir_is_ok_not_configured(tmp_path: Path):
    comp = _comp(tmp_path / "jasper-secrets", "voice_keys.env")
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, members=[], non_members=[]
    )
    assert result.status == "ok"
    assert "not present" in result.detail


def test_happy_path_passes(tmp_path: Path):
    d = tmp_path / "jasper-secrets"
    dir_st = _mk_dir(d, 0o2770)
    f_st = _mk_file(d / "voice_keys.env", 0o640)
    # The host's real tmp group stands in for the compartment group.
    comp = _comp(d, "voice_keys.env", "google_credentials.env", group=_group_name(dir_st))
    member = _ident(999_999, {dir_st.st_gid, f_st.st_gid}, "jasper-voice")
    nonmember = _ident(888_888, {777_777}, "jasper-input")
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, [member], [nonmember]
    )
    assert result.status == "ok", result.detail
    assert "1 secret(s) readable only by jasper-voice" in result.detail


def test_happy_path_deduplicates_shared_unix_user_members(tmp_path: Path):
    d = tmp_path / "jasper-secrets"
    dir_st = _mk_dir(d, 0o2770)
    f_st = _mk_file(d / "voice_keys.env", 0o640)
    comp = _comp(d, "voice_keys.env", group=_group_name(dir_st))
    members = [
        _ident(101, {dir_st.st_gid, f_st.st_gid}, "jasper-web"),
        _ident(102, {dir_st.st_gid, f_st.st_gid}, "jasper-voice"),
        _ident(101, {dir_st.st_gid, f_st.st_gid}, "jasper-web"),
    ]

    result = sc._classify_compartment(
        "secret compartment: jasper-secrets",
        comp,
        members,
        non_members=[],
    )

    assert result.status == "ok", result.detail
    assert "readable only by jasper-web, jasper-voice" in result.detail
    assert "jasper-web, jasper-voice, jasper-web" not in result.detail


def test_world_readable_file_fails_over_exposure(tmp_path: Path):
    d = tmp_path / "jasper-secrets"
    _mk_dir(d, 0o2770)
    _mk_file(d / "voice_keys.env", 0o644)  # o+r — the confidentiality regression
    comp = _comp(d, "voice_keys.env")
    member = _ident(999_999, {d.stat().st_gid}, "jasper-voice")
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, [member], []
    )
    assert result.status == "fail", result.detail
    assert "OVER-EXPOSED" in result.detail
    assert "voice_keys.env" in result.detail
    assert "0o644" in result.detail
    assert "world" in result.detail


def test_broad_group_file_fails_over_exposure(tmp_path: Path):
    """A secret whose group a NON-member holds (e.g. regressed to `jasper`) FAILs
    even at 0640 — privsep's one-sided 'readable' check would PASS this."""
    d = tmp_path / "jasper-secrets"
    _mk_dir(d, 0o2770)
    f_st = _mk_file(d / "voice_keys.env", 0o640)
    comp = _comp(d, "voice_keys.env")
    member = _ident(999_999, {f_st.st_gid}, "jasper-voice")
    nonmember = _ident(888_888, {f_st.st_gid}, "jasper-input")  # shares file group
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, [member], [nonmember]
    )
    assert result.status == "fail", result.detail
    assert "jasper-input" in result.detail


def test_unreadable_secret_warns_availability(tmp_path: Path):
    d = tmp_path / "jasper-secrets"
    _mk_dir(d, 0o2770)
    _mk_file(d / "voice_keys.env", 0o600)  # owner-only; member can't read
    comp = _comp(d, "voice_keys.env")
    member = _ident(999_999, {d.stat().st_gid}, "jasper-web")  # not owner
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, [member], []
    )
    assert result.status == "warn", result.detail
    assert "not readable by jasper-web" in result.detail
    assert "re-deploy" in result.detail


def test_fail_outranks_warn(tmp_path: Path):
    """An over-exposed file + an unreadable file → FAIL, with the warning counted."""
    d = tmp_path / "jasper-secrets"
    _mk_dir(d, 0o2770)
    _mk_file(d / "voice_keys.env", 0o644)  # over-exposed
    _mk_file(d / "google_credentials.env", 0o600)  # unreadable by member
    comp = _comp(d, "voice_keys.env", "google_credentials.env")
    member = _ident(999_999, {d.stat().st_gid}, "jasper-web")
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, [member], []
    )
    assert result.status == "fail", result.detail
    assert "availability warning" in result.detail


def test_dir_missing_setgid_warns(tmp_path: Path):
    d = tmp_path / "jasper-secrets"
    dir_st = _mk_dir(d, 0o0770)  # correct group, but setgid lost
    comp = _comp(d)  # no files
    member = _ident(999_999, {dir_st.st_gid}, "jasper-voice")
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, [member], []
    )
    assert result.status == "warn", result.detail
    assert "2770" in result.detail


def test_dir_world_traversable_fails(tmp_path: Path):
    d = tmp_path / "jasper-secrets"
    _mk_dir(d, 0o2775)  # o+rx — anyone can traverse + reach a secret
    comp = _comp(d)
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, members=[], non_members=[]
    )
    assert result.status == "fail", result.detail
    assert "gate is open" in result.detail
    assert "world" in result.detail


def test_dir_traversable_by_nonmember_fails(tmp_path: Path):
    d = tmp_path / "jasper-secrets"
    dir_st = _mk_dir(d, 0o2770)
    comp = _comp(d)
    nonmember = _ident(888_888, {dir_st.st_gid}, "jasper-input")  # shares dir group
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, members=[], non_members=[nonmember]
    )
    assert result.status == "fail", result.detail
    assert "jasper-input" in result.detail


def test_glob_files_classified(tmp_path: Path):
    """A glob (the google/tokens/*.json shape) flags only the over-exposed member."""
    d = tmp_path / "jasper-secrets"
    _mk_dir(d, 0o2770)
    tokens = d / "google" / "tokens"
    _mk_file(tokens / "ok.json", 0o640)
    _mk_file(tokens / "leaked.json", 0o644)
    comp = sc.SecretCompartment(
        group="jasper-secrets",
        directory=str(d),
        member_units=("jasper-voice",),
        files=(str(tokens / "*.json"),),
    )
    member = _ident(999_999, {d.stat().st_gid}, "jasper-voice")
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, [member], []
    )
    assert result.status == "fail", result.detail
    assert "leaked.json" in result.detail
    assert "ok.json" not in result.detail


def test_overflow_truncates(tmp_path: Path):
    d = tmp_path / "jasper-secrets"
    _mk_dir(d, 0o2770)
    names = []
    for i in range(9):
        fn = f"f{i}.env"
        _mk_file(d / fn, 0o644)
        names.append(fn)
    comp = _comp(d, *names)
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, members=[], non_members=[]
    )
    assert result.status == "fail"
    assert "more)" in result.detail


def test_reports_never_contain_the_secret_value(tmp_path: Path):
    """Strictly secret-free: the file body must never reach the detail string."""
    d = tmp_path / "jasper-secrets"
    _mk_dir(d, 0o2770)
    _mk_file(d / "voice_keys.env", 0o644)
    comp = _comp(d, "voice_keys.env")
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, members=[], non_members=[]
    )
    assert "SECRET-VALUE-NEVER-IN-OUTPUT" not in result.detail


# --------------------------------------------------------------------------- #
# Decorated checks — total off the Pi (systemctl unavailable / absent dirs)
# --------------------------------------------------------------------------- #
def test_decorated_checks_skip_without_systemctl(monkeypatch):
    monkeypatch.setattr(sc.privsep, "_unit_runtime_identity", lambda unit: None)
    for fn in (
        sc.check_jasper_secrets_compartment,
        sc.check_jasper_intsecrets_compartment,
    ):
        result = fn()
        assert result.status == "ok"
        assert "systemctl unavailable" in result.detail


def test_decorated_checks_skip_absent_compartment(monkeypatch):
    """systemctl 'available' but the real compartment dirs don't exist on the test
    host → ok 'not present' (the nothing-configured path), never a raise."""
    monkeypatch.setattr(sc, "_systemctl_available", lambda: True)
    monkeypatch.setattr(
        sc,
        "_resolve_unit",
        lambda unit: sc._Identity(uid=12345, gids=frozenset({54321}), user=unit),
    )
    for fn in (
        sc.check_jasper_secrets_compartment,
        sc.check_jasper_intsecrets_compartment,
    ):
        result = fn()
        assert result.status == "ok"
        assert "not present" in result.detail
