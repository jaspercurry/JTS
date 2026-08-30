# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Contract: the applied-speaker evidence seam (issue #1787, plan RC1 / D2).

The room layer could not reach the speaker layer's gated evidence at all —
``jasper/correction/`` imported none of the validity floor, the gated spec
curve, or the null registry. RC1 opens a one-way, read-only window and this
file pins its two halves:

  * **The payload extension is era-tolerant.** The floor and the gated curve
    ride *inside* ``exclusion_evidence``, which participates in the candidate
    fingerprint only when non-empty. That is what makes the extension safe with
    no schema bump: a candidate written before RC1 still validates and simply
    reports its evidence as absent.
  * **The reader refuses rather than guessing.** Missing / unresolvable /
    stale / pre-extension evidence each return a typed absent result with a
    machine reason. Nothing consumes this for correction yet (Tier B is RC4) —
    the seam exists, is gated, and is tested.

The staleness rule is deliberately NOT invented here: it reuses the identity
the apply path already records, ``source.measured_candidate_fingerprint``.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from jasper.active_speaker.measured_crossover_candidate import (
    MeasuredCrossoverCandidate,
)
from jasper.correction import applied_speaker_evidence as seam
from jasper.correction.applied_speaker_evidence import (
    AppliedSpeakerEvidence,
    AppliedSpeakerEvidenceAbsent,
    resolve_applied_speaker_evidence,
)

from tests.test_active_speaker_measured_crossover_candidate import _candidate

GATED_CURVE = {
    "freqs_hz": [100.0, 200.0, 400.0, 800.0],
    "magnitude_db": [-1.0, 0.5, 0.0, -0.5],
}


def _evidence_payload(**overrides) -> dict:
    payload = {
        "phase": "cloud_measure",
        "excluded_bands_hz": [[5000.0, 6000.0]],
        "n_positions": 6,
        "band_spread": [],
        "null_registry": {"intervals": []},
        "validity_floor_hz": 120.0,
        "gated_spec_curve": dict(GATED_CURVE),
    }
    payload.update(overrides)
    return payload


def _publish(root: Path, candidate: MeasuredCrossoverCandidate, *, session="s1") -> Path:
    path = (
        root / "bundle-a" / "evidence" / "v1" / "artifacts"
        / "crossover_v2" / session / "candidate.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate.to_dict()), encoding="utf-8")
    return path


def _profile(fingerprint: str, *, linearization: dict | None = None) -> dict:
    """An applied-profile view naming `fingerprint`.

    `linearization` non-empty by default: that is what marks an ACOUSTIC
    commission (the same gate the producer uses to decide whether cloud
    evidence rides the candidate). Pass `{}` for the electrical-only shape.
    """
    return {
        "linearization": {"woofer": {}} if linearization is None else linearization,
        "source": {"measured_candidate_fingerprint": fingerprint},
    }


def _state(tmp_path: Path, fingerprint: str | None) -> Path:
    """A baseline-profile state file whose applied profile names a candidate."""
    source = {"topology_id": "t1", "fingerprint": "src-fp"}
    if fingerprint is not None:
        source["measured_candidate_fingerprint"] = fingerprint
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"source": source}), encoding="utf-8")
    return path


@pytest.fixture()
def applied(monkeypatch):
    """Stub the applied-profile read so these tests exercise the SEAM only.

    The real `load_applied_baseline_profile_state` is covered by the
    active-speaker suite; reproducing a full valid baseline artifact here would
    test that module, not this one.
    """

    def _install(profile):
        monkeypatch.setattr(
            "jasper.active_speaker.baseline_profile.load_applied_baseline_profile_state",
            lambda path=None: profile,
        )

    return _install


# --- the payload extension --------------------------------------------------


def test_extension_rides_inside_exclusion_evidence_and_is_fingerprinted():
    """A non-empty exclusion_evidence participates in the fingerprint, so the
    new keys are tamper-evident for free — no schema bump needed."""
    with_evidence = _candidate(exclusion_evidence=_evidence_payload())
    without = _candidate()
    other_floor = _candidate(
        exclusion_evidence=_evidence_payload(validity_floor_hz=200.0)
    )

    assert with_evidence.fingerprint != without.fingerprint
    # Editing the floor changes the fingerprint -> cannot be silently rewritten.
    assert other_floor.fingerprint != with_evidence.fingerprint


def test_pre_extension_candidate_still_validates_and_keeps_its_fingerprint():
    """Era tolerance: the omit-when-empty rule means an old candidate's
    fingerprint is unchanged by this PR, so it still round-trips."""
    old_payload = {
        "phase": "cloud_measure",
        "excluded_bands_hz": [],
        "n_positions": 6,
        "band_spread": [],
        "null_registry": {},
    }
    old = _candidate(exclusion_evidence=old_payload)
    reopened = MeasuredCrossoverCandidate.from_mapping(json.loads(json.dumps(old.to_dict())))
    assert reopened.fingerprint == old.fingerprint
    assert "gated_spec_curve" not in reopened.exclusion_evidence


def test_empty_exclusion_evidence_candidate_is_unaffected():
    """A candidate whose fit consumed no cloud evidence keeps the exact
    pre-extension shape — this is what makes the extension additive."""
    plain = _candidate()
    assert plain.exclusion_evidence == {}
    reopened = MeasuredCrossoverCandidate.from_mapping(json.loads(json.dumps(plain.to_dict())))
    assert reopened.fingerprint == plain.fingerprint


# --- the reader -------------------------------------------------------------


def test_resolves_the_applied_candidates_evidence(tmp_path, applied):
    candidate = _candidate(exclusion_evidence=_evidence_payload())
    _publish(tmp_path, candidate)
    applied(_profile(candidate.fingerprint))

    result = resolve_applied_speaker_evidence(
        state_path=_state(tmp_path, candidate.fingerprint), sessions_dir=tmp_path
    )

    assert isinstance(result, AppliedSpeakerEvidence)
    assert result.candidate_fingerprint == candidate.fingerprint
    assert result.validity_floor_hz == 120.0
    assert result.gated_spec_freqs_hz == tuple(GATED_CURVE["freqs_hz"])
    assert result.gated_spec_magnitude_db == tuple(GATED_CURVE["magnitude_db"])
    assert result.has_gated_curve is True
    assert result.null_registry == {"intervals": []}
    assert result.to_dict()["available"] is True


def test_no_applied_profile_is_absent_not_an_error(tmp_path, applied):
    applied(None)
    result = resolve_applied_speaker_evidence(
        state_path=tmp_path / "missing.json", sessions_dir=tmp_path
    )
    assert isinstance(result, AppliedSpeakerEvidenceAbsent)
    assert result.reason == "no_applied_profile"


def test_applied_profile_without_a_candidate_fingerprint_is_unresolved(
    tmp_path, applied
):
    """Pre-candidate-era applied profiles record no identity to gate on, so
    there is no honest way to claim evidence belongs to what is playing."""
    applied({"source": {"topology_id": "t1"}})
    result = resolve_applied_speaker_evidence(
        state_path=_state(tmp_path, None), sessions_dir=tmp_path
    )
    assert isinstance(result, AppliedSpeakerEvidenceAbsent)
    assert result.reason == "candidate_unresolved"


def test_electrical_only_commission_is_not_reported_as_stale(tmp_path, applied):
    """An electrical-only commission must not masquerade as a stale candidate.

    `MeasuredElectricalCandidate` writes the SAME
    `source.measured_candidate_fingerprint` field as an acoustic candidate but
    publishes nothing under `crossover_v2/`, so a fingerprint-only reader falls
    through to `candidate_stale` — telling the household "this speaker was
    re-measured since" when the truth is "this speaker was never commissioned
    with acoustic cloud evidence at all." Empty `linearization` is the honest
    discriminator.

    The published acoustic candidate below is a decoy: without the check it is
    exactly what would produce the wrong `candidate_stale` verdict.
    """
    decoy = _candidate(exclusion_evidence=_evidence_payload())
    _publish(tmp_path, decoy)
    applied(_profile("electrical-candidate-fp", linearization={}))

    result = resolve_applied_speaker_evidence(
        state_path=_state(tmp_path, "electrical-candidate-fp"), sessions_dir=tmp_path
    )
    assert isinstance(result, AppliedSpeakerEvidenceAbsent)
    assert result.reason == "no_cloud_evidence"
    assert "without acoustic cloud evidence" in result.detail


def test_pruned_bundle_is_unresolved(tmp_path, applied):
    """Raw session bundles are retention-capped; a pruned one must refuse."""
    applied(_profile("deadbeef"))
    result = resolve_applied_speaker_evidence(
        state_path=_state(tmp_path, "deadbeef"), sessions_dir=tmp_path
    )
    assert isinstance(result, AppliedSpeakerEvidenceAbsent)
    assert result.reason == "candidate_unresolved"


def test_a_different_candidate_on_disk_is_stale_not_a_silent_substitute(
    tmp_path, applied
):
    """THE staleness rule, reused from the apply path's recorded fingerprint.

    A recommission in progress leaves a NEWER candidate published while an
    OLDER profile is still applied. Handing back the newer candidate's gated
    curve would attribute one speaker configuration's response to another —
    exactly the double-correction Tier B exists to prevent.
    """
    published = _candidate(exclusion_evidence=_evidence_payload())
    _publish(tmp_path, published)
    applied(_profile("a-different-fingerprint"))

    result = resolve_applied_speaker_evidence(
        state_path=_state(tmp_path, "a-different-fingerprint"), sessions_dir=tmp_path
    )
    assert isinstance(result, AppliedSpeakerEvidenceAbsent)
    assert result.reason == "candidate_stale"


def test_pre_extension_candidate_reports_era_absent(tmp_path, applied):
    """The applied candidate is current, but predates the payload extension."""
    old = _candidate(
        exclusion_evidence={
            "phase": "cloud_measure",
            "excluded_bands_hz": [],
            "n_positions": 6,
            "band_spread": [],
            "null_registry": {},
        }
    )
    _publish(tmp_path, old)
    applied(_profile(old.fingerprint))

    result = resolve_applied_speaker_evidence(
        state_path=_state(tmp_path, old.fingerprint), sessions_dir=tmp_path
    )
    assert isinstance(result, AppliedSpeakerEvidenceAbsent)
    assert result.reason == "evidence_absent"
    assert "predates" in result.detail


def test_candidate_with_no_cloud_evidence_reports_absent(tmp_path, applied):
    plain = _candidate()
    _publish(tmp_path, plain)
    applied(_profile(plain.fingerprint))

    result = resolve_applied_speaker_evidence(
        state_path=_state(tmp_path, plain.fingerprint), sessions_dir=tmp_path
    )
    assert isinstance(result, AppliedSpeakerEvidenceAbsent)
    assert result.reason == "evidence_absent"


def test_unverified_floor_is_none_never_zero(tmp_path, applied):
    """'unknown' is not 0 Hz — the plan's unknown-is-not-zero trap.

    A group that never established a floor carries None. The curve is still
    usable, so this resolves rather than refusing, but the floor stays None.
    """
    candidate = _candidate(
        exclusion_evidence=_evidence_payload(validity_floor_hz=None)
    )
    _publish(tmp_path, candidate)
    applied(_profile(candidate.fingerprint))

    result = resolve_applied_speaker_evidence(
        state_path=_state(tmp_path, candidate.fingerprint), sessions_dir=tmp_path
    )
    assert isinstance(result, AppliedSpeakerEvidence)
    assert result.validity_floor_hz is None
    assert result.has_gated_curve is True


def test_ragged_curve_is_rejected_rather_than_half_used(tmp_path, applied):
    """A mismatched freq/magnitude pair must not produce a partial curve."""
    candidate = _candidate(
        exclusion_evidence=_evidence_payload(
            gated_spec_curve={"freqs_hz": [100.0, 200.0], "magnitude_db": [0.0]},
        )
    )
    _publish(tmp_path, candidate)
    applied(_profile(candidate.fingerprint))

    result = resolve_applied_speaker_evidence(
        state_path=_state(tmp_path, candidate.fingerprint), sessions_dir=tmp_path
    )
    # Floor survives; the ragged curve does not.
    assert isinstance(result, AppliedSpeakerEvidence)
    assert result.validity_floor_hz == 120.0
    assert result.gated_spec_freqs_hz == ()
    assert result.has_gated_curve is False


def test_corrupt_candidate_artifact_does_not_raise(tmp_path, applied):
    path = (
        tmp_path / "bundle-a" / "evidence" / "v1" / "artifacts"
        / "crossover_v2" / "s1" / "candidate.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    applied(_profile("abc"))

    result = resolve_applied_speaker_evidence(
        state_path=_state(tmp_path, "abc"), sessions_dir=tmp_path
    )
    assert isinstance(result, AppliedSpeakerEvidenceAbsent)
    assert result.reason == "candidate_unresolved"


def test_artifact_scan_is_bounded(tmp_path):
    """A pathological bundle directory must not become an unbounded parse.

    The bound and the sort are pinned; WHICH subset survives the cap is not.
    Bundle ids are random (``uuid4().hex[:12]``), so path order carries no
    time information and no end of it is the meaningful one to keep.
    """
    session_root = tmp_path / "bundle-a" / "evidence" / "v1" / "artifacts" / "crossover_v2"
    total = seam.MAX_CANDIDATE_ARTIFACTS_SCANNED + 15
    for index in range(total):
        target = session_root / f"s{index:03d}" / "candidate.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")

    found = seam._candidate_artifact_paths(tmp_path)
    assert len(found) == seam.MAX_CANDIDATE_ARTIFACTS_SCANNED
    assert found == sorted(found)


#: The seam this file's last test pins as unconsumed.
_SEAM_MODULE = "jasper.correction.applied_speaker_evidence"


def _imported_modules(tree: ast.Module, *, package: str) -> set[str]:
    """Every module ``tree`` imports, absolute and relative alike, resolved.

    ``package`` is the dotted package the file lives in, which is what a
    relative import is resolved against — ``from . import x`` inside
    ``jasper/correction/`` names ``jasper.correction.x``.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - (node.level - 1)] or parts[:1])
                root = f"{base}.{node.module}" if node.module else base
            else:
                root = node.module or ""
            found.add(root)
            found.update(f"{root}.{alias.name}" for alias in node.names)
    return found


def test_nothing_consumes_the_seam_yet():
    """Pins this module's own claim that the seam is not yet wired.

    Tier B is RC4. When a consumer legitimately lands it must bring its own
    gating and disclosure (missing evidence disables Tier B — it must never
    fall back to correcting the raw in-room curve), so this assertion is meant
    to be updated deliberately at that point, not deleted quietly.

    **The detector matches that semantic: it finds IMPORTERS.** It used to grep
    raw file text for the substring ``applied_speaker_evidence``, which is a
    different question — on 2026-08-16 a prose cross-reference in
    ``crossover_v2_flow``'s docstring ("the same posture
    ``resolve_applied_speaker_evidence`` takes on the same read") failed this
    pin while the seam still had zero consumers. Naming a function in a comment
    is not wiring it, and the repair is the detector rather than a whitelist:
    "mentions minus known mentions" is a weaker pin than "importers", and it
    would have pushed the next author to rename accurate prose to dodge a grep.

    Parsed rather than pattern-matched, so an import written inside a string,
    a comment, or a docstring example cannot be mistaken for one the module
    executes — the same class of false positive, one level down. Relative
    imports are resolved and counted too: a sibling in ``jasper/correction/``
    writing ``from .applied_speaker_evidence import …`` is the most likely
    first consumer, and it is a consumer.

    The expected set is EMPTY. The seam's own module appeared in the substring
    era only because it contains its own name; nothing imports it, including
    itself.
    """
    repo = Path(__file__).resolve().parents[1]
    importers = []
    for path in sorted((repo / "jasper").rglob("*.py")):
        relative = path.relative_to(repo)
        package = ".".join(relative.parts[:-1])
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if _SEAM_MODULE in _imported_modules(tree, package=package):
            importers.append(str(relative))
    assert importers == [], (
        "a consumer of the applied-speaker evidence seam appeared: "
        f"{importers}. Confirm it discloses absent evidence rather than "
        "falling back to the raw in-room curve, then update this pin."
    )
