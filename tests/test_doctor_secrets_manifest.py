"""The secret-compartment manifest must mirror the systemd units it reasons about.

``jasper.cli.doctor.secret_compartments.COMPARTMENTS`` hardcodes, per Phase 4
compartment, which non-root daemons are MEMBERS (must read the secrets) — the
availability side of the two-sided contract. The non-members are derived as the
rest of privsep's Tier-A universe, so the confidentiality side depends on the
membership being correct too. A unit edit that adds/removes a daemon from a
compartment group, or a genuinely new daemon joining one, would silently desync
this. This test pins the manifest against the committed unit files, mirroring
``test_doctor_privsep_manifest.py``:

1. the compartments cover exactly the two known Phase 4 groups, each created by
   service-users.sh;
2. each compartment's declared members equal the set of Tier-A units whose unit
   file declares that compartment group in ``SupplementaryGroups=`` (so a revoked
   or granted group can't drift the membership);
3. NO unit anywhere in deploy/ declares a compartment group without being listed as
   a member (catches a new non-root daemon joining a secret group);
4. every declared secret file lives under its compartment directory.
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.cli.doctor import privsep
from jasper.cli.doctor.secret_compartments import COMPARTMENTS

ROOT = Path(__file__).resolve().parents[1]

# The two Phase 4 compartments. Adding a third must be a conscious edit here.
_EXPECTED_COMPARTMENT_GROUPS = frozenset({"jasper-secrets", "jasper-intsecrets"})

# privsep already pins the five Tier-A non-root daemons (and their canonical
# non-root unit files) against the units; reuse it as the universe so the two
# checks can't disagree about which daemons exist.
_TIER_A_UNIT_FILE = {s.unit: s.unit_file for s in privsep.MANIFEST}


def _supp_groups(unit_file: Path) -> set[str]:
    """The union of every ``SupplementaryGroups=`` line in a unit file (systemd
    unions them). Mirrors ``test_doctor_privsep_manifest._unit_identity``."""
    supp: set[str] = set()
    for raw in unit_file.read_text().splitlines():
        line = raw.strip()
        if m := re.match(r"^SupplementaryGroups=(.*)$", line):
            supp.update(m.group(1).split())
    return supp


def _user(unit_file: Path) -> str:
    user = ""
    for raw in unit_file.read_text().splitlines():
        line = raw.strip()
        if m := re.match(r"^User=(.*)$", line):
            user = m.group(1).strip()
    return user


def test_compartments_cover_exactly_the_two_phase4_groups():
    assert {c.group for c in COMPARTMENTS} == _EXPECTED_COMPARTMENT_GROUPS


def test_compartment_groups_are_created_by_install():
    sh = (ROOT / "deploy/lib/install/service-users.sh").read_text()
    for group in _EXPECTED_COMPARTMENT_GROUPS:
        assert f"groupadd -r {group}" in sh, (
            f"service-users.sh must create the {group} compartment group"
        )


def test_members_mirror_the_units_supplementary_groups():
    """Each compartment's members == the Tier-A units whose unit file declares that
    compartment group. The drift catch: revoke a group from a unit, or grant one,
    and this fails until COMPARTMENTS matches."""
    for comp in COMPARTMENTS:
        from_units = {
            unit
            for unit, rel in _TIER_A_UNIT_FILE.items()
            if comp.group in _supp_groups(ROOT / rel)
        }
        assert set(comp.member_units) == from_units, (
            f"{comp.group}: manifest members {sorted(comp.member_units)} but the "
            f"units declaring the group are {sorted(from_units)} — update "
            "secret_compartments.COMPARTMENTS to match the unit files."
        )


def test_members_are_real_tier_a_units():
    for comp in COMPARTMENTS:
        for unit in comp.member_units:
            assert unit in _TIER_A_UNIT_FILE, (
                f"{comp.group} member {unit} is not a Tier-A non-root daemon"
            )


def test_no_unit_joins_a_compartment_group_without_membership():
    """Scan every deploy unit: any unit declaring a compartment group in its
    SupplementaryGroups MUST be a declared member. Catches a new non-root daemon
    added to a secret group without updating COMPARTMENTS (which would leave it out
    of the availability set AND mis-classified as a leak target)."""
    members_by_group = {c.group: set(c.member_units) for c in COMPARTMENTS}
    offenders: list[str] = []
    for unit_file in sorted(ROOT.glob("deploy/**/*.service")):
        supp = _supp_groups(unit_file)
        for group in _EXPECTED_COMPARTMENT_GROUPS & supp:
            if unit_file.stem not in members_by_group[group]:
                offenders.append(f"{unit_file.stem} -> {group}")
    assert not offenders, (
        "unit(s) declare a compartment group but are not listed as members: "
        f"{sorted(offenders)}. Add them to secret_compartments.COMPARTMENTS "
        "(and confirm they should hold the secret)."
    )


def test_secret_files_live_under_their_compartment_dir():
    for comp in COMPARTMENTS:
        prefix = comp.directory.rstrip("/") + "/"
        for path in comp.files:
            assert path.startswith(prefix), (
                f"{comp.group}: secret file {path} is outside the compartment dir "
                f"{comp.directory}"
            )


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
