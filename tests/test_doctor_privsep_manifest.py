# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The privsep read manifest must mirror the systemd units it reasons about.

``jasper.cli.doctor.privsep.MANIFEST`` hardcodes each non-root daemon's runtime
identity (User / Group / SupplementaryGroups) and the files it reads. The
readability check resolves identity from the *live* unit at runtime, but the
manifest's declared identity is what documents the assumption — and a unit edit
(or a newly-added non-root daemon) that silently desyncs it would make the check
reason about the wrong identity. This test pins the manifest against the
committed unit files, mirroring ``test_env_load_mirrors_unit.py``:

1. every spec's (user, group, supplementary_groups) equals its unit file's
   directives;
2. every ``User=jasper-*`` unit in deploy/ is either in the manifest or in the
   explicit out-of-scope set (so a genuinely new non-root daemon can't be added
   without a conscious scope decision);
3. the manifest's read paths stay within the group-``jasper`` trees the check is
   scoped to (the secret compartments are deliberately excluded).
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.cli.doctor.privsep import MANIFEST, OUT_OF_SCOPE_NONROOT_UNITS

ROOT = Path(__file__).resolve().parents[1]

# The five Tier-A daemons WS1 dropped to non-root. Removing one from the
# manifest should be a conscious edit, not a silent drop.
_EXPECTED_MANIFEST_UNITS = frozenset(
    {"jasper-control", "jasper-web", "jasper-mux", "jasper-voice", "jasper-input"}
)

# The check only reasons about the single `jasper` group dimension, so every
# declared read must live under a group-`jasper` state tree.
_ALLOWED_PATH_PREFIXES = ("/var/lib/jasper/", "/var/lib/camilladsp/")


def _unit_identity(unit_file: Path) -> tuple[str, str, frozenset[str]]:
    """(User, Group, {SupplementaryGroups}) from a unit file. User/Group take the
    last assignment (systemd's last-wins); SupplementaryGroups accumulate across
    every line (systemd unions them)."""
    user = ""
    group = ""
    supp: set[str] = set()
    for raw in unit_file.read_text().splitlines():
        line = raw.strip()
        if m := re.match(r"^User=(.*)$", line):
            user = m.group(1).strip()
        elif m := re.match(r"^Group=(.*)$", line):
            group = m.group(1).strip()
        elif m := re.match(r"^SupplementaryGroups=(.*)$", line):
            supp.update(m.group(1).split())
    return user, group, frozenset(supp)


def test_manifest_covers_exactly_the_tier_a_daemons():
    assert {s.unit for s in MANIFEST} == _EXPECTED_MANIFEST_UNITS


def test_each_spec_identity_mirrors_its_unit_file():
    for spec in MANIFEST:
        unit_file = ROOT / spec.unit_file
        assert unit_file.is_file(), f"{spec.unit}: unit file {spec.unit_file} missing"
        user, group, supp = _unit_identity(unit_file)
        assert spec.user == user, (
            f"{spec.unit}: manifest User={spec.user!r} but {spec.unit_file} has "
            f"User={user!r} — update privsep.MANIFEST to match the unit."
        )
        assert spec.group == group, (
            f"{spec.unit}: manifest Group={spec.group!r} but unit has Group={group!r}"
        )
        assert frozenset(spec.supplementary_groups) == supp, (
            f"{spec.unit}: manifest SupplementaryGroups="
            f"{sorted(spec.supplementary_groups)} but unit has {sorted(supp)} — "
            "update privsep.MANIFEST to match the unit."
        )


def test_every_nonroot_jasper_unit_is_classified():
    """Enumerate every deploy unit declaring User=jasper-*; each must be in the
    manifest or the explicit out-of-scope set. Catches a new non-root daemon
    added without a scope decision."""
    manifest_units = {s.unit for s in MANIFEST}
    nonroot: dict[str, str] = {}
    for unit_file in sorted(ROOT.glob("deploy/**/*.service")):
        user, _, _ = _unit_identity(unit_file)
        if user.startswith("jasper-"):
            nonroot.setdefault(unit_file.stem, user)
    assert nonroot, "no User=jasper-* units found — parser regression?"
    unclassified = {
        unit
        for unit in nonroot
        if unit not in manifest_units and unit not in OUT_OF_SCOPE_NONROOT_UNITS
    }
    assert not unclassified, (
        f"non-root jasper unit(s) {sorted(unclassified)} are neither in "
        "privsep.MANIFEST nor OUT_OF_SCOPE_NONROOT_UNITS. A new non-root daemon "
        "must be classified: add its read-set to the manifest, or document it as "
        "out-of-scope (e.g. a reconciler) in OUT_OF_SCOPE_NONROOT_UNITS."
    )


def test_manifest_paths_stay_in_group_jasper_trees():
    for spec in MANIFEST:
        for path in spec.paths:
            assert path.startswith(_ALLOWED_PATH_PREFIXES), (
                f"{spec.unit}: path {path} is outside the group-`jasper` trees "
                f"{_ALLOWED_PATH_PREFIXES} this check is scoped to (secret "
                "compartments are deliberately excluded)."
            )


def test_out_of_scope_units_are_real_and_nonroot():
    """The out-of-scope allowlist must not rot: each entry must be a real deploy
    unit that actually runs as a non-root jasper user."""
    for unit in OUT_OF_SCOPE_NONROOT_UNITS:
        matches = list(ROOT.glob(f"deploy/**/{unit}.service"))
        assert matches, f"out-of-scope unit {unit} has no deploy unit file"
        user, _, _ = _unit_identity(matches[0])
        assert user.startswith("jasper-"), (
            f"out-of-scope unit {unit} is not non-root (User={user!r}); remove it "
            "from OUT_OF_SCOPE_NONROOT_UNITS."
        )
