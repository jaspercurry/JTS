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
from dataclasses import replace
from pathlib import Path

from jasper import accounts
from jasper.cli.doctor import privsep
from jasper.cli.doctor import secret_compartments as sc
from jasper.cli.doctor.secret_compartments import COMPARTMENTS
from tests.systemd_unit_helpers import value_for, values_for


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
def test_absent_dir_is_skipped_not_configured(tmp_path: Path):
    comp = _comp(tmp_path / "jasper-secrets", "voice_keys.env")
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, members=[], non_members=[]
    )
    assert result.status == "skipped"
    assert result.reason == sc.REASON_COMPARTMENT_ABSENT


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
    # Pure formatting behavior: repeated Unix-identity members dedupe by name.
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
    assert result.reason == sc.REASON_COMPARTMENT_OVER_EXPOSED


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
    assert result.reason == sc.REASON_COMPARTMENT_OVER_EXPOSED


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
    assert result.reason == sc.REASON_COMPARTMENT_UNDER_AVAILABLE


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
    assert result.reason == sc.REASON_COMPARTMENT_OVER_EXPOSED
    # Fail still discloses the co-occurring availability warning count.
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
    assert result.reason == sc.REASON_COMPARTMENT_UNDER_AVAILABLE


def test_dir_world_traversable_fails(tmp_path: Path):
    d = tmp_path / "jasper-secrets"
    _mk_dir(d, 0o2775)  # o+rx — anyone can traverse + reach a secret
    comp = _comp(d)
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, members=[], non_members=[]
    )
    assert result.status == "fail", result.detail
    assert result.reason == sc.REASON_COMPARTMENT_OVER_EXPOSED


def test_dir_traversable_by_nonmember_fails(tmp_path: Path):
    d = tmp_path / "jasper-secrets"
    dir_st = _mk_dir(d, 0o2770)
    comp = _comp(d)
    nonmember = _ident(888_888, {dir_st.st_gid}, "jasper-input")  # shares dir group
    result = sc._classify_compartment(
        "secret compartment: jasper-secrets", comp, members=[], non_members=[nonmember]
    )
    assert result.status == "fail", result.detail
    assert result.reason == sc.REASON_COMPARTMENT_OVER_EXPOSED


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
    assert result.reason == sc.REASON_COMPARTMENT_OVER_EXPOSED
    # Pure formatting behavior: only the over-exposed glob member is named.
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
    assert result.reason == sc.REASON_COMPARTMENT_OVER_EXPOSED
    # Pure formatting behavior: the shown-list caps with an overflow marker.
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
        assert result.status == "skipped"
        assert result.reason == sc.REASON_SYSTEMCTL_UNAVAILABLE


def test_decorated_checks_skip_absent_compartment(monkeypatch):
    """systemctl 'available' but the real compartment dirs don't exist on the test
    host → skipped 'not present' (the nothing-configured path), never a raise."""
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
        assert result.status == "skipped"
        assert result.reason == sc.REASON_COMPARTMENT_ABSENT


# --------------------------------------------------------------------------- #
# COMPARTMENTS manifest vs the committed systemd units
#
# COMPARTMENTS hardcodes which non-root daemons are MEMBERS of each Phase 4
# compartment — the availability side of the two-sided contract. Non-members
# are derived as the rest of privsep's Tier-A universe, so the confidentiality
# side depends on that membership being right too. These pin the manifest
# against the unit files so a unit edit cannot silently desync it.
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parents[1]

# The two Phase 4 compartments. Adding a third must be a conscious edit here.
_EXPECTED_COMPARTMENT_GROUPS = frozenset({"jasper-secrets", "jasper-intsecrets"})

# privsep already pins the five Tier-A non-root daemons (and their canonical
# non-root unit files) against the units; reuse it as the universe so the two
# checks can't disagree about which daemons exist.
_TIER_A_UNIT_FILE = {s.unit: s.unit_file for s in privsep.MANIFEST}


def _supp_groups(unit_file: Path) -> set[str]:
    """The union of every ``SupplementaryGroups=`` line in a unit file (systemd
    unions them). Mirrors ``test_doctor_privsep._unit_identity``."""
    return set(values_for(unit_file.read_text(), "SupplementaryGroups"))


def _user(unit_file: Path) -> str:
    return value_for(unit_file.read_text(), "User") or ""


def _effective_compartment_groups_by_user() -> dict[str, set[str]]:
    """Compartment groups a Unix user effectively carries.

    Install-time ``usermod -aG`` grants supplementary groups to the Unix user,
    so a second unit running as that same user can read the compartment even if
    its unit file does not repeat ``SupplementaryGroups=``. The manifest must
    model that effective identity, not only each unit's local directives.
    """

    out: dict[str, set[str]] = {}
    for rel in _TIER_A_UNIT_FILE.values():
        unit_file = ROOT / rel
        user = _user(unit_file)
        if not user or user == "root":
            continue
        out.setdefault(user, set()).update(
            _EXPECTED_COMPARTMENT_GROUPS & _supp_groups(unit_file)
        )
    return out


def _effective_supp_groups(unit_file: Path) -> set[str]:
    direct = _supp_groups(unit_file)
    inherited = _effective_compartment_groups_by_user().get(_user(unit_file), set())
    return direct | inherited


def test_compartments_cover_exactly_the_two_phase4_groups():
    assert {c.group for c in COMPARTMENTS} == _EXPECTED_COMPARTMENT_GROUPS


def _groupadd_names(script_text: str) -> set[str]:
    """Every group name a ``groupadd`` invocation creates in a shell script,
    parsed by structure (the command + its flags) rather than matched as a
    literal substring, so the check survives a flag reorder."""
    names: set[str] = set()
    for line in script_text.splitlines():
        tokens = line.strip().split()
        if not tokens or tokens[0] != "groupadd":
            continue
        args = [t for t in tokens[1:] if not t.startswith("-")]
        names.update(args)
    return names


def test_compartment_groups_are_created_by_install():
    sh = (ROOT / "deploy/lib/install/service-users.sh").read_text()
    created = _groupadd_names(sh)
    missing = _EXPECTED_COMPARTMENT_GROUPS - created
    assert not missing, (
        f"service-users.sh must create the compartment group(s) {sorted(missing)}"
    )


def test_members_mirror_the_units_supplementary_groups():
    """Each compartment's members == the Tier-A units whose effective identity
    carries that compartment group.

    The drift catch: revoke a group from a user/unit, grant one, or add a unit
    that shares a secret-bearing Unix user, and this fails until COMPARTMENTS
    matches.
    """
    for comp in COMPARTMENTS:
        from_units = {
            unit
            for unit, rel in _TIER_A_UNIT_FILE.items()
            if comp.group in _effective_supp_groups(ROOT / rel)
        }
        assert set(comp.member_units) == from_units, (
            f"{comp.group}: manifest members {sorted(comp.member_units)} but the "
            f"units with effective access are {sorted(from_units)} — update "
            "secret_compartments.COMPARTMENTS to match the unit files."
        )


def test_members_are_real_tier_a_units():
    for comp in COMPARTMENTS:
        for unit in comp.member_units:
            assert unit in _TIER_A_UNIT_FILE, (
                f"{comp.group} member {unit} is not a Tier-A non-root daemon"
            )


def test_no_unit_joins_a_compartment_group_without_membership():
    """Scan every deploy unit: any unit with effective access to a compartment
    group MUST be a declared member.

    Catches both direct ``SupplementaryGroups=`` edits and a new non-root daemon
    running as a secret-bearing Unix user without updating COMPARTMENTS (which
    would leave it out of the availability set AND mis-classified as a leak
    target).
    """
    members_by_group = {c.group: set(c.member_units) for c in COMPARTMENTS}
    offenders: list[str] = []
    for unit_file in sorted(ROOT.glob("deploy/**/*.service")):
        user = _user(unit_file)
        if not user or user == "root":
            continue
        supp = _effective_supp_groups(unit_file)
        for group in _EXPECTED_COMPARTMENT_GROUPS & supp:
            if unit_file.stem not in members_by_group[group]:
                offenders.append(f"{unit_file.stem} -> {group}")
    assert not offenders, (
        "unit(s) declare a compartment group but are not listed as members: "
        f"{sorted(offenders)}. Add them to secret_compartments.COMPARTMENTS "
        "(and confirm they should hold the secret)."
    )


def test_secret_files_live_under_their_compartment_dir(monkeypatch):
    for var in ("SPOTIFY_CACHE_PATH", "JASPER_SPOTIFY_ACCOUNTS_PATH"):
        monkeypatch.delenv(var, raising=False)
    for comp in COMPARTMENTS:
        prefix = comp.directory.rstrip("/") + "/"
        for path, _ in comp.resolved_files():
            assert path.startswith(prefix), (
                f"{comp.group}: secret file {path} is outside the compartment dir "
                f"{comp.directory}"
            )


def test_audited_paths_follow_the_spotify_env_overrides(tmp_path, monkeypatch):
    """The Spotify cache + registry paths are env-overridable (jasper.accounts),
    so the audit must stat the files the box actually uses; a resolver's output
    is one literal path, never a glob pattern."""
    cache = str(tmp_path / "cache[1].json")
    accounts_json = str(tmp_path / "accounts.json")
    monkeypatch.setenv("SPOTIFY_CACHE_PATH", cache)
    monkeypatch.setenv("JASPER_SPOTIFY_ACCOUNTS_PATH", accounts_json)
    d = tmp_path / "jasper-intsecrets"
    dir_st = _mk_dir(d, 0o2770)
    comp = replace(
        next(c for c in COMPARTMENTS if c.group == "jasper-intsecrets"),
        directory=str(d),
    )
    statted: list[str] = []
    globbed: list[str] = []

    def _stat(path: str) -> os.stat_result:
        statted.append(path)
        return dir_st

    def _glob(pattern: str) -> list[str]:
        globbed.append(pattern)
        return []

    sc._classify_compartment(
        "secret compartment: jasper-intsecrets", comp, [], [],
        stat_fn=_stat, glob_fn=_glob,
    )
    assert {cache, accounts_json} <= set(statted)
    assert set(statted).isdisjoint(
        {accounts.LEGACY_CACHE_PATH, accounts.DEFAULT_REGISTRY_PATH}
    )
    # A literal pattern still globs; a resolver's path never does.
    assert globbed == [f"{accounts.DEFAULT_CACHE_DIR}/*.json"]


def test_member_units_are_non_root():
    """A compartment member that runs as root would make the availability check
    vacuous (root reads everything). The Tier-A members are all non-root; the
    streambox jasper-web-as-root variant self-skips at runtime, but the canonical
    unit pinned here is the non-root one."""
    for comp in COMPARTMENTS:
        for unit in comp.member_units:
            user = _user(ROOT / _TIER_A_UNIT_FILE[unit])
            assert user.startswith("jasper-"), (
                f"{comp.group} member {unit} canonical unit is not non-root "
                f"(User={user!r})"
            )
