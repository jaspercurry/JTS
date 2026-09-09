# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: one owner for the room band edge (issue #1787, RC1).

``jasper.audio_measurement.room_boundary`` owns the edge, so moving it is "a
one-file, test-visible change, never a scattered literal edit":

  1. **The drift guard.** None of the routed files re-declares a band-edge
     literal. This is the test that fails when someone adds another copy
     of ``350.0``.
  2. **Co-ownership.** The clamp bounds and the gated spec's lower edge are the
     SSOT's values, and the documented relation between them holds.
  3. **The deliberate non-mover.** The SNR band tables (both owned by
     ``audio_measurement.snr_policy``) still carry their static 350 Hz edge and
     still satisfy the cross-instrument pins — routing them is a trap, not an
     omission.

The last requirement is the same invariant one layer out: the truth layer
imports no front end. `jasper/audio_measurement` importing neither consumer
package is what makes it a valid home for the SSOT; adding
`jasper/active_speaker/crossover_v2` and its own front end gives the layer's
membership — both packages, all of each — a pin instead of a claim.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from jasper.active_speaker import flat_spec
from jasper.audio_measurement import analysis, peq, room_boundary, snr_policy

REPO_ROOT = Path(__file__).resolve().parents[1]

# The files RC1 routed through the SSOT. A file appearing here is a promise
# that its band edges come from jasper.audio_measurement.room_boundary.
#
# KNOWN LIMITATION — this is a per-file allowlist, so it is blind to band-edge
# literals in modules that do not appear in it, including modules that do not
# exist yet. RC4's Tier B correction is the concrete near-term risk: a new
# module that hard-codes 250/350/500 for the Tier A/Tier B handoff would pass
# this guard simply by not being listed. Whoever adds a module that reasons
# about the boundary adds it here in the same PR. A whole-package sweep was
# considered and rejected: these packages are full of unrelated frequencies
# (crossover corners, analysis bands, display vocabulary) and a blanket scan
# would be mostly false positives, which is how guards get disabled.
ROUTED_FILES: tuple[str, ...] = (
    "jasper/audio_measurement/peq.py",
    "jasper/audio_measurement/analysis.py",
    "jasper/active_speaker/flat_spec.py",
)

# The values that belong to the SSOT, matched by VALUE rather than by spelling.
# `350`, `350.0`, `350.`, and `3.5e2` are all the same re-declaration, and an
# earlier spelling-based version of this guard let three of those four through
# (mutation-verified). The scan parses each numeric literal and compares.
SSOT_VALUES: tuple[float, ...] = (250.0, 350.0, 500.0)


def _numeric_literals(path: Path) -> list[tuple[int, float]]:
    """Every numeric literal in the file's CODE, by line.

    Parsed from the AST rather than scanned as text, which buys two things the
    text version got wrong: prose is excluded for free (comments never reach
    the AST, and docstrings are `str` constants, so a routing comment may
    freely say "350 Hz"), and the match is by VALUE — `350`, `350.0`, `350.`,
    and `3.5e2` are all caught, where a spelling-based regex caught only one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[tuple[int, float]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            if isinstance(node.value, bool):
                continue
            out.append((node.lineno, float(node.value)))
    return out


def _imported_module(path: Path, node: ast.ImportFrom) -> str:
    """The absolute dotted name an ``ImportFrom`` names, relative ones included.

    ``from ..active_speaker import x`` reaches exactly as far as
    ``import jasper.active_speaker`` does, so a scan that skips
    ``node.level > 0`` is half a guard.
    """
    if node.level == 0:
        return node.module or ""
    package = path.relative_to(REPO_ROOT).with_suffix("").parts[:-1]
    base = package[: len(package) - node.level + 1]
    return ".".join([*base, *([node.module] if node.module else [])])


#: The imports a row still admits, and why each one is not yet removable.
#: Keyed by package, then by the importing file; an entry is a promise that the
#: symbol genuinely belongs to the front end it is reached in, not a parking
#: space. Removing one is the work; adding one owes the row's own argument.
BOUNDARY_ALLOWLIST: dict[str, dict[str, frozenset[str]]] = {
    "jasper/cli": {
        # `crossover_v2_status_block` is the web ADAPTER over the engine's
        # status projection — the loaded state, volume plan, review decision
        # and republish admission it supplies are the host's. The `GRADE_*`
        # vocabulary is declared beside the producer that selects it.
        "jasper/cli/doctor/correction.py": frozenset({
            "jasper.web.correction_crossover_v2",
            "jasper.web.correction_crossover_v2_status",
        }),
    },
}


def _upward_imports(
    package: str, forbidden: tuple[tuple[str, ...], ...]
) -> tuple[list[str], set[tuple[str, str]]]:
    """Sites in ``package`` importing under a ``forbidden`` prefix, plus the
    allowlist entries those sites used.

    ``ast.walk`` rather than ``tree.body``: a deferred (function-body) import
    reaches exactly as far as a module-scope one, and a relocation that leaves
    one behind is the failure this scan exists to catch.
    """
    allowed = BOUNDARY_ALLOWLIST.get(package, {})
    offenders: list[str] = []
    used: set[tuple[str, str]] = set()
    for path in sorted((REPO_ROOT / package).rglob("*.py")):
        rel = str(path.relative_to(REPO_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = _imported_module(path, node)
                names = [module]
                # A from-list inside a restricted module may name ordinary symbols.
                if tuple(module.split(".")[:2]) not in forbidden:
                    names.extend(f"{module}.{alias.name}" for alias in node.names)
            for name in names:
                if tuple(name.split(".")[:2]) not in forbidden:
                    continue
                if name in allowed.get(rel, frozenset()):
                    used.add((rel, name))
                    continue
                offenders.append(f"{rel}:{node.lineno}: imports {name}")
    return offenders, used


@pytest.mark.parametrize(
    "source,offender_count,uses_allowlist",
    [
        ("import jasper.active_speaker", 1, False),
        ("def probe():\n    import jasper.active_speaker", 1, False),
        ("from jasper.active_speaker import flat_spec", 1, False),
        ("from ..active_speaker import flat_spec", 1, False),
        ("from jasper import active_speaker as engine", 1, False),
        ("from .. import active_speaker", 1, False),
        ("import jasper.active_speaker.runtime_contract as contract", 0, True),
        ("from jasper.active_speaker.runtime_contract import DriverRuntimeContract", 0, True),
        ("from ..active_speaker.runtime_contract import DriverRuntimeContract, Caps", 0, True),
        ("from jasper.active_speaker.runtime_contract import *", 0, True),
        ("from jasper import audio_measurement", 0, False),
        ("from ..audio_measurement import active_speaker", 0, False),
        ("from jasper import __version__", 0, False),
    ],
)
def test_upward_import_forms(tmp_path, monkeypatch, source, offender_count, uses_allowlist):
    """The scanner's own form coverage, on a synthetic package.

    A real row would tie this to whichever allowlist entry happens to exist;
    the forms it must recognise (plain, deferred, relative, star, aliased) are
    the subject, so the fixture declares its own one-entry allowlist.
    """
    relative = "jasper/probe/leaf.py"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text(source, encoding="utf-8")
    monkeypatch.setattr(f"{__name__}.REPO_ROOT", tmp_path)
    monkeypatch.setitem(
        BOUNDARY_ALLOWLIST,
        "jasper/probe",
        {relative: frozenset({"jasper.active_speaker.runtime_contract"})},
    )

    offenders, used = _upward_imports("jasper/probe", (("jasper", "active_speaker"),))

    assert len(offenders) == offender_count
    assert used == (
        {(relative, "jasper.active_speaker.runtime_contract")} if uses_allowlist else set()
    )


@pytest.mark.parametrize("relative", ROUTED_FILES)
def test_routed_files_do_not_redeclare_band_edge_literals(relative: str):
    """The drift guard (plan D3 requirement 1).

    Before RC1 the "350 Hz cap" was ten independent literals. If this fails,
    a band edge was hard-coded again somewhere that already promised to read
    the SSOT — import the constant from
    ``jasper.audio_measurement.room_boundary`` instead. If the number genuinely
    is NOT the seam (a mic-physics floor, a tolerance, an unrelated
    frequency), give it a name and a comment saying so.
    """
    path = REPO_ROOT / relative
    source_lines = path.read_text(encoding="utf-8").splitlines()
    offenders = [
        f"{relative}:{number}: {value!r} in {source_lines[number - 1].strip()}"
        for number, value in _numeric_literals(path)
        if value in SSOT_VALUES
    ]
    assert not offenders, (
        "band-edge literal re-declared outside the boundary SSOT:\n"
        + "\n".join(offenders)
    )


def test_clamp_bounds_and_spec_edge_are_the_ssots_values():
    """Co-ownership (plan D3 requirement 2).

    The gated spec's lower edge and the room ceiling's clamp floor are held
    side by side in one module, so moving either is one file's decision.
    """
    assert room_boundary.GATED_SPEC_LOWER_EDGE_HZ == 250.0
    assert room_boundary.ROOM_BOUNDARY_DEFAULT_HZ == 350.0
    assert room_boundary.ROOM_BOUNDARY_MIN_HZ == 250.0
    assert room_boundary.ROOM_BOUNDARY_MAX_HZ == 500.0

    # flat_spec consumes the edge rather than re-declaring it.
    assert flat_spec.SPEC_BANDS[0][0] == room_boundary.GATED_SPEC_LOWER_EDGE_HZ
    assert flat_spec.REFERENCE_BAND_HZ[0] == room_boundary.GATED_SPEC_LOWER_EDGE_HZ


#: Package edges the tuning layers may not have, and why each one matters.
PACKAGE_BOUNDARIES: tuple[tuple[str, tuple[tuple[str, ...], ...], str], ...] = (
    (
        "jasper/audio_measurement",
        (("jasper", "active_speaker"), ("jasper", "cli")),
        "that is what makes it a valid home for the boundary SSOT",
    ),
    (
        "jasper/active_speaker",
        (("jasper", "web"), ("jasper", "cli")),
        "web/cli are front ends; the engine must not reach up into either",
    ),
    (
        "jasper/cli",
        (("jasper", "web"),),
        "the CLI is a front end beside the wizard, not a client of one",
    ),
)


@pytest.mark.parametrize(
    "package,forbidden,why",
    PACKAGE_BOUNDARIES,
    ids=[row[0] for row in PACKAGE_BOUNDARIES],
)
def test_package_boundary_holds(package, forbidden, why):
    """The invariant the SSOT's placement rests on (room_boundary's docstring).

    `audio_measurement` earns the home by being imported by its consumers
    while importing none of them — which is what lets `analysis.py` (itself a
    routed site) read the boundary with no new cross-package edge.

    If a row fails, the placement argument is no longer true: either move the
    offending import out, or re-argue where the SSOT belongs. Do not just
    delete this test.
    """
    offenders, used = _upward_imports(package, forbidden)
    assert not offenders, (
        f"{package} must not import "
        + " or ".join(".".join(prefix) for prefix in forbidden)
        + f" — {why}:\n"
        + "\n".join(offenders)
    )
    # An allowlist entry nobody imports pre-authorizes the very edge this row
    # exists to catch, so the row stays pinned at exactly the state it was left.
    stale = {
        (rel, name)
        for rel, names in BOUNDARY_ALLOWLIST.get(package, {}).items()
        for name in names
    } - used
    assert not stale, (
        f"{package}: allowlist entries no longer imported — delete them:\n"
        + "\n".join(f"{rel}: {name}" for rel, name in sorted(stale))
    )


def test_crossover_v2_imports_no_web_front_end():
    """The other half of the truth layer's membership.

    The truth layer is `jasper/audio_measurement` plus
    `jasper/active_speaker/crossover_v2`, and "truth layer" means the front ends
    import it and it imports no front end. The test above pins that direction
    for the first package; this pins it for the second, whose front end is the
    crossover wizard under `jasper/web`.

    It is also the property the analyze registry's home rests on: the one
    decoder + calibration + mic-tier + capture-report assembly lives in
    `jasper/web/correction_crossover_v2.py`, so a unit that needs it must have
    it lifted rather than reach up for it.
    """
    offenders, _used = _upward_imports(
        "jasper/active_speaker/crossover_v2", (("jasper", "web"),)
    )
    assert not offenders, (
        "jasper/active_speaker/crossover_v2 must not import jasper.web — the "
        "dependency runs the other way, and that is what makes it part of the "
        "truth layer rather than part of the wizard:\n" + "\n".join(offenders)
    )


def test_the_documented_relation_between_the_two_edges_holds():
    """The room ceiling may never be clamped below the gated spec's edge.

    Stated once in room_boundary's docstring: below that edge the gated layer
    has no measured authority to hand off to, so a room ceiling underneath it
    would leave a band neither layer owns.
    """
    assert room_boundary.ROOM_BOUNDARY_MIN_HZ >= room_boundary.GATED_SPEC_LOWER_EDGE_HZ
    assert room_boundary.ROOM_BOUNDARY_MAX_HZ > room_boundary.ROOM_BOUNDARY_MIN_HZ
    assert (
        room_boundary.ROOM_BOUNDARY_MIN_HZ
        <= room_boundary.ROOM_BOUNDARY_DEFAULT_HZ
        <= room_boundary.ROOM_BOUNDARY_MAX_HZ
    )


def test_every_routed_site_resolves_to_the_ssot_today():
    """Behaviour-identical: RC1 moved ownership, not values."""
    default = room_boundary.ROOM_BOUNDARY_DEFAULT_HZ

    assert inspect.signature(peq.design_peq).parameters["f_high"].default == default
    for fn in (
        analysis.deviation_metrics,
        analysis.before_after_fill_segments,
        analysis.before_after_delta,
    ):
        assert inspect.signature(fn).parameters["f_high"].default == default


# ---------------------------------------------------------------------------
# The deliberate non-mover.
# ---------------------------------------------------------------------------


def test_snr_band_tables_keep_their_static_edge():
    """TRAP GUARD (plan: "the one that must NOT move").

    These edges look like the boundary and must NOT be routed through it.
    They are capture-quality vocabulary shared with the gated instrument;
    tying them to a per-room ceiling would make banded SNR non-comparable
    across sessions and across instruments. If a future change "completes the
    routing" helpfully, this fails.
    """
    assert snr_policy.SNR_BANDS_HZ == (
        ("sub_bass", 20.0, 80.0),
        ("bass", 80.0, 160.0),
        ("upper_bass", 160.0, 350.0),
        ("transition", 350.0, 1000.0),
    )
    # The existing cross-instrument pins still hold.
    assert snr_policy.CROSSOVER_SNR_BANDS_HZ[:4] == snr_policy.SNR_BANDS_HZ
