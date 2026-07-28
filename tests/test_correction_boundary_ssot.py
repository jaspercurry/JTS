# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: one owner for the room-correction band edge (issue #1787, RC1).

Pins ``docs/room-correction-regime-plan.md`` D3's concrete requirement — that
moving the boundary is "a one-file, test-visible change, never a scattered
literal edit":

  1. **The drift guard.** None of the routed files re-declares a band-edge
     literal. This is the test that fails when someone adds an eleventh copy
     of ``350.0``.
  2. **Co-ownership.** The clamp bounds and the gated spec's lower edge are the
     SSOT's values, and the documented relation between them holds.
  3. **The #1797 fix.** A ``safe`` session grades, scores, and DISCLOSES over
     its own band, not ``balanced``'s.
  4. **The deliberate non-mover.** The SNR band tables still carry their static
     350 Hz edge and still satisfy the cross-instrument pins — routing them is
     a trap, not an omission.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from jasper.active_speaker import flat_spec
from jasper.audio_measurement import analysis, room_boundary, snr_policy
from jasper.correction import (
    acceptance,
    acoustic_quality,
    confidence,
    evidence,
    peq,
    status,
    strategy,
)
from jasper.correction.session import MeasurementSession, SessionConfig

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
# considered and rejected for RC1: `jasper/correction/**` is full of unrelated
# frequencies (crossover corners, analysis bands, display vocabulary) and a
# blanket scan would be mostly false positives, which is how guards get
# disabled.
ROUTED_FILES: tuple[str, ...] = (
    "jasper/correction/peq.py",
    "jasper/correction/strategy.py",
    "jasper/correction/session.py",
    "jasper/correction/acceptance.py",
    "jasper/correction/confidence.py",
    "jasper/correction/envelope.py",
    "jasper/correction/evidence.py",
    "jasper/audio_measurement/analysis.py",
    "jasper/active_speaker/flat_spec.py",
)

# The values that belong to the SSOT, matched by VALUE rather than by spelling.
# `350`, `350.0`, `350.`, and `3.5e2` are all the same re-declaration, and an
# earlier spelling-based version of this guard let three of those four through
# (mutation-verified). The scan parses each numeric literal and compares.
SSOT_VALUES: tuple[float, ...] = (250.0, 350.0, 500.0)

# Module-level constants inside routed files that are STATIC BAND VOCABULARY,
# not the correction boundary — the same category as the SNR tables, and
# exempt for the same reason: they label fixed reporting bands that must stay
# comparable across sessions even after the room ceiling goes per-room. They
# are exempted BY NAME so the exemption is a decision on the record rather
# than a hole in the guard; a new literal anywhere else in these files still
# fails. (POSITION_ANALYSIS_BANDS's own edges are 300/500 Hz — it does not
# even track the 350 Hz boundary, which is the clearest evidence it is a
# different vocabulary.)
STATIC_BAND_VOCABULARY: dict[str, frozenset[str]] = {
    "jasper/correction/confidence.py": frozenset({"POSITION_ANALYSIS_BANDS"}),
}


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


def _exempt_line_numbers(path: Path, names: frozenset[str]) -> set[int]:
    """Line numbers spanned by the named module-level assignments."""
    if not names:
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    spans: set[int] = set()
    found: set[str] = set()
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id in names:
                found.add(target.id)
                spans.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    missing = names - found
    assert not missing, (
        f"{path.name} no longer defines exempted constant(s) {sorted(missing)} — "
        "drop the stale exemption from STATIC_BAND_VOCABULARY"
    )
    return spans


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
    exempt = _exempt_line_numbers(path, STATIC_BAND_VOCABULARY.get(relative, frozenset()))
    source_lines = path.read_text(encoding="utf-8").splitlines()
    offenders = [
        f"{relative}:{number}: {value!r} in {source_lines[number - 1].strip()}"
        for number, value in _numeric_literals(path)
        if number not in exempt and value in SSOT_VALUES
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


def test_audio_measurement_imports_neither_consumer_package():
    """The invariant the SSOT's placement rests on (room_boundary's docstring).

    `correction` and `active_speaker` import each other, so neither is "below"
    the other. `audio_measurement` earns the home by being imported by both
    while importing neither — which is what lets `analysis.py` (itself a routed
    site) read the boundary with no new cross-package edge.

    If this fails, the placement argument is no longer true: either move the
    offending import out of `audio_measurement`, or re-argue where the SSOT
    belongs. Do not just delete this test.
    """
    root = REPO_ROOT / "jasper" / "audio_measurement"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            for name in names:
                if name.split(".")[:2] in (
                    ["jasper", "correction"],
                    ["jasper", "active_speaker"],
                ):
                    rel = path.relative_to(REPO_ROOT)
                    offenders.append(f"{rel}:{node.lineno}: imports {name}")
    assert not offenders, (
        "jasper/audio_measurement must import neither jasper.correction nor "
        "jasper.active_speaker — that is what makes it a valid home for the "
        "boundary SSOT:\n" + "\n".join(offenders)
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


@pytest.mark.parametrize(
    ("estimate", "expected"),
    [
        (None, 350.0),        # no estimate -> the disclosed fallback
        (300.0, 300.0),       # inside the clamp -> used as-is
        (100.0, 250.0),       # below -> clamped to the floor
        (900.0, 500.0),       # above -> clamped to the ceiling
        (float("nan"), 350.0),  # unknown is not a bound
        (float("inf"), 350.0),
        (0.0, 350.0),
        (-5.0, 350.0),
    ],
)
def test_room_boundary_hz_clamps_or_falls_back(estimate, expected):
    """A non-finite/non-positive estimate falls back rather than clamping.

    "Unknown" must not silently become a confident 250 or 500 — that would
    look like a per-room answer the estimator never produced.
    """
    assert room_boundary.room_boundary_hz(estimate) == expected


def test_every_routed_site_resolves_to_the_ssot_today():
    """Behaviour-identical: RC1 moved ownership, not values."""
    default = room_boundary.ROOM_BOUNDARY_DEFAULT_HZ

    assert inspect.signature(peq.design_peq).parameters["f_high"].default == default
    assert (
        inspect.signature(acceptance.evaluate_acceptance).parameters["f_high"].default
        == default
    )
    for fn in (
        analysis.deviation_metrics,
        analysis.before_after_fill_segments,
        analysis.before_after_delta,
    ):
        assert inspect.signature(fn).parameters["f_high"].default == default

    assert confidence.DEFAULT_BAND_HZ == (20.0, default)
    assert evidence.REPEATABILITY_BAND_HZ == (50.0, default)

    assert strategy.CORRECTION_STRATEGIES["safe"].f_high_hz == (
        room_boundary.ROOM_BOUNDARY_MIN_HZ
    )
    assert strategy.CORRECTION_STRATEGIES["balanced"].f_high_hz == default
    assert strategy.CORRECTION_STRATEGIES["assertive"].f_high_hz == (
        room_boundary.ROOM_BOUNDARY_MAX_HZ
    )


def test_repeatability_reclamp_follows_the_ssot_not_a_literal():
    """The re-clamp is one of the two sites that would have capped silently.

    ``min(<ceiling>, peq_f_high)`` must read the SSOT, so a raised ceiling
    widens repeatability instead of being quietly truncated at 350 Hz.
    """
    source = inspect.getsource(acoustic_quality.repeatability_from_arrays)
    assert "ROOM_BOUNDARY_DEFAULT_HZ" in source
    assert "min(350" not in source.replace(" ", "")


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
    assert acoustic_quality.SNR_BANDS_HZ == (
        ("sub_bass", 20.0, 80.0),
        ("bass", 80.0, 160.0),
        ("upper_bass", 160.0, 350.0),
        ("transition", 350.0, 1000.0),
    )
    # The existing cross-instrument pins still hold.
    assert snr_policy.CROSSOVER_SNR_BANDS_HZ[:4] == acoustic_quality.SNR_BANDS_HZ

    text = (REPO_ROOT / "jasper/correction/acoustic_quality.py").read_text()
    band_block = text.split("SNR_BANDS_HZ", 1)[0]
    assert "TRAP" in band_block, (
        "the SNR table's do-not-route rationale must stay next to the table"
    )


# ---------------------------------------------------------------------------
# Issue #1797 — a safe session grades and discloses over its OWN band.
# ---------------------------------------------------------------------------


def test_session_config_carries_no_band_shadow_copy():
    """The defect was a frozen copy nothing ever wrote. It must stay gone."""
    fields = SessionConfig().__dataclass_fields__
    assert "peq_f_high" not in fields
    assert "peq_f_low" not in fields


@pytest.mark.parametrize(
    ("strategy_choice", "expected_band"),
    [
        ("safe", (25.0, 250.0)),
        ("balanced", (20.0, 350.0)),
        ("assertive", (20.0, 500.0)),
    ],
)
def test_session_band_follows_the_selected_strategy(strategy_choice, expected_band):
    """#1797: the graded/scored/disclosed band is the band actually corrected.

    Before the fix every one of these read ``balanced``'s 350 Hz, so a ``safe``
    session corrected 25-250 Hz while grading ACCEPT/REVERT over 50-350,
    scoring confidence over 20-350, and disclosing ``[20, 350]``.
    """
    session = MeasurementSession(strategy_choice=strategy_choice)

    assert session.correction_band_hz == expected_band
    # The band the designer actually fits.
    fitted = strategy.CORRECTION_STRATEGIES[strategy_choice]
    assert (fitted.f_low_hz, fitted.f_high_hz) == expected_band

    # The disclosed config payload agrees, including the strategy id itself.
    payload = status.session_config_payload(session)
    assert (payload["peq_f_low"], payload["peq_f_high"]) == expected_band
    assert payload["correction_strategy"] == strategy_choice


def test_safe_session_no_longer_grades_over_the_balanced_band():
    """The headline regression: corrected-narrow-stated-wide is impossible."""
    safe = MeasurementSession(strategy_choice="safe")
    balanced = MeasurementSession(strategy_choice="balanced")

    assert safe.correction_band_hz[1] == 250.0
    assert balanced.correction_band_hz[1] == 350.0
    assert safe.correction_band_hz != balanced.correction_band_hz
