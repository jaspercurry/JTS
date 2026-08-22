# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""One vocabulary per question, and no second declaration of one.

``jasper.audio_measurement.quality_model`` owns both halves of the
capture-quality model: the THRESHOLDS (already shared) and, since 2026-08-22,
the WORDS the verdicts crossing them are spelled in — :data:`Severity`
("how bad is this one finding?"), :data:`ReportLevel` ("how does the whole
report roll up?"), and :data:`TrustLevel` ("how much do I trust this
number?").

Before that, every surface reporting one declared its own. Copies that agree
are still copies, and each one is a chance to disagree — one had already taken
it: the crossover-v2 feature classifier wrote ``med`` where
``correction.confidence``, ``correction.spatial``, and
``correction.acoustic_quality`` all wrote ``medium``, so a banked lab artifact
and a room-correction report answered one question in two spellings.

These tests pin the words themselves, so a seventh surface cannot re-open the
question by declaring a seventh ``Literal``. That qualifier is the honest
bound, not modesty: the scan below detects a ``Literal[...]`` declaration, so a
copy of the same words in another SHAPE — a ``frozenset``, a rank ``dict``, a
bare ``-> str`` returning them, a hand-mirrored JS array — is invisible to it.
One such copy is already known and deliberately out of scope
(``active_speaker.crossover_preview._CONFIDENCE_RANK``, these three words plus
``unknown``, mirrored by hand in ``deploy/assets/sound-profile/js/main.js``);
unifying it is a design call, not a rename. What each test catches:

* the alias test — a surface that stops speaking the shared vocabulary;
* the redeclaration scan — a surface that re-declares it as a ``Literal``
  instead of importing it, which the alias test alone cannot see
  (``typing.Literal`` is cached, so an identical re-declaration IS the same
  object);
* the legacy-spelling scan — a NEW writer of the retired ``med``;
* the tolerant-read test — a banked artifact from before the rename, which is
  on disk forever and must still read;
* the refusal test — the one rank that is deliberately NOT this vocabulary,
  pinned so nobody "unifies" it by mistake;
* the synthetic-key test — a copy key readable as a verdict value it is not.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, get_args

from jasper.active_speaker.crossover_v2 import feature_classification
from jasper.audio_measurement import quality, snr_policy
from jasper.audio_measurement.quality_model import (
    TRUST_UNAVAILABLE,
    ReportLevel,
    Severity,
    TrustLevel,
)
from jasper.correction import (
    acceptance,
    acoustic_quality,
    browser_audio,
    confidence,
    envelope,
    runtime_integrity,
    spatial,
)

_REPO = Path(__file__).resolve().parent.parent
_JASPER = _REPO / "jasper"

_SEVERITY_WORDS = frozenset(get_args(Severity))
_REPORT_WORDS = frozenset(get_args(ReportLevel))
_TRUST_WORDS = frozenset(get_args(TrustLevel))

#: The canonical sets, by the name a redeclaration would be re-declaring.
_CANONICAL_SETS = {
    "Severity": _SEVERITY_WORDS,
    "ReportLevel": _REPORT_WORDS,
    "TrustLevel": _TRUST_WORDS,
}

#: The retired middle-rank spelling. The classifier wrote it until 2026-08-22.
_LEGACY_TRUST_WORD = "med"

#: Where the legacy spelling is still allowed to appear as a string constant:
#: exactly the reader that maps it, and nowhere else. A WRITER of it anywhere
#: in the tree is the regression this scan exists for.
_LEGACY_SPELLING_ALLOWLIST = {
    "jasper/active_speaker/crossover_v2/feature_classification.py",
}


def test_every_surface_speaks_the_shared_vocabulary() -> None:
    """Each named alias resolves to the canonical word set, not a lookalike.

    Aliases are compared by their MEMBERS rather than by identity on purpose:
    identity is worthless here (``typing.Literal`` caches, so a hand-rolled
    duplicate is the same object), and members are what actually ships in an
    artifact. The redeclaration scan below is what catches the duplicate.
    """
    assert get_args(quality.Severity) == get_args(Severity)
    assert get_args(runtime_integrity.Severity) == get_args(Severity)
    assert get_args(browser_audio.Severity) == get_args(Severity)
    assert get_args(confidence.ConfidenceLevel) == get_args(TrustLevel)
    assert get_args(spatial.SpatialConfidence) == get_args(TrustLevel)


def test_the_no_evidence_slot_is_not_a_trust_level() -> None:
    """"We did not measure this" is not a low reading.

    A consumer that cannot tell them apart treats a missing noise floor as a
    bad one — which is why ``unavailable`` sits beside :data:`TrustLevel`
    rather than inside it.
    """
    assert TRUST_UNAVAILABLE not in _TRUST_WORDS


def test_the_no_evidence_slot_keeps_its_published_spelling() -> None:
    """``TRUST_UNAVAILABLE``'s VALUE is a wire contract, not an internal name.

    Python callers import the constant, so renaming the symbol is free — but
    the STRING is written into `acoustic_quality.json` and read back by
    readers that cannot import anything: `deploy/assets/correction/js/main.js`
    hand-compares `acoustic.snr_level !== 'unavailable'` and renders
    `repeatability.level || 'unavailable'`. Change the value and those readers
    silently start treating "not measured" as a real trust rank. Pinned here
    because no Python type can reach across that boundary.
    """
    assert TRUST_UNAVAILABLE == "unavailable"


def _literal_word_sets(tree: ast.AST) -> list[frozenset[str]]:
    """Every ``Literal["a", "b", ...]`` in one module, as its member set.

    AST-based, so a docstring or comment quoting the words does not count —
    only an actual type declaration does.
    """
    out: list[frozenset[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Subscript):
            continue
        target = node.value
        name = (
            target.attr if isinstance(target, ast.Attribute)
            else target.id if isinstance(target, ast.Name)
            else None
        )
        if name != "Literal":
            continue
        index = node.slice
        elements = index.elts if isinstance(index, ast.Tuple) else [index]
        words = {
            element.value for element in elements
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
        if words:
            out.append(frozenset(words))
    return out


def test_no_module_redeclares_a_canonical_word_set() -> None:
    """Only quality_model may DECLARE these words; everyone else imports them.

    This is the guard the alias test cannot be: a module that writes
    ``Severity = Literal["info", "warn", "fail"]`` of its own passes every
    member comparison in this file and is still a second source of truth —
    the exact shape that let ``med`` diverge in the first place.
    """
    owner = "jasper/audio_measurement/quality_model.py"
    offenders: dict[str, list[str]] = {}
    for path in sorted(_JASPER.rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if rel == owner:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        for words in _literal_word_sets(tree):
            for canonical_name, canonical in _CANONICAL_SETS.items():
                if words == canonical:
                    offenders.setdefault(rel, []).append(canonical_name)
    assert offenders == {}, (
        "these modules re-declare a vocabulary quality_model already owns; "
        f"import the alias instead: {offenders}"
    )


def test_no_module_writes_the_retired_middle_rank_spelling() -> None:
    """``med`` survives only in the reader that maps it forward.

    A subject scan, not a diff scan: a NEW writer of the old spelling in a
    module this PR never touched is exactly what a diff-scoped sweep misses.

    Deliberately unscoped, and therefore capable of catching a bare ``"med"``
    that means something else entirely. That is the accepted cost of total
    coverage — the fix for a genuine unrelated use is one line in
    :data:`_LEGACY_SPELLING_ALLOWLIST`, and the failure message says so.
    """
    offenders = set()
    for path in sorted(_JASPER.rglob("*.py")):
        rel = path.relative_to(_REPO).as_posix()
        if rel in _LEGACY_SPELLING_ALLOWLIST:
            continue
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value == _LEGACY_TRUST_WORD
            ):
                offenders.add(rel)
    assert offenders == set(), (
        f"{_LEGACY_TRUST_WORD!r} is the retired spelling of 'medium' "
        f"(quality_model.TrustLevel); these modules still write it: "
        f"{sorted(offenders)}. If one of them means something unrelated by "
        f"that string, add it to _LEGACY_SPELLING_ALLOWLIST with a note."
    )


def test_a_banked_med_reads_back_as_medium() -> None:
    """The tolerant read at the artifact parse boundary.

    Artifacts banked before 2026-08-22 carry ``med`` and are on disk forever,
    so the READER normalises and only the reader does. Deleting the mapping
    must fail here, not surface as a bar that silently stops recognising a
    trust rank it used to.
    """
    banked = {
        "hz": 1037.0,
        "classification": feature_classification.DEFECT_BOOSTABLE,
        "confidence": "med",
    }
    verdict = feature_classification.read_feature_verdicts([banked])[0]
    assert verdict.confidence == "medium"
    assert verdict.confidence in _TRUST_WORDS
    # Round-trip: what the reader produces reads back unchanged, so a packet
    # republishing a normalised row is not re-normalised into something else.
    again = feature_classification.read_feature_verdicts([verdict.to_dict()])[0]
    assert again == verdict


def test_an_unknown_confidence_is_kept_verbatim() -> None:
    """Tolerant in ONE direction. An unrecognised string is evidence about
    who wrote the artifact; repairing it would erase that."""
    banked = {
        "hz": 1037.0,
        "classification": feature_classification.DEFECT_BOOSTABLE,
        "confidence": "extremely-confident",
    }
    verdict = feature_classification.read_feature_verdicts([banked])[0]
    assert verdict.confidence == "extremely-confident"


def _report_words(node: Any, severities: set[str], levels: set[str]) -> None:
    """Collect every ``severity`` / ``*level`` word an artifact actually
    published, walking the nested report rather than sampling its top."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "severity" and isinstance(value, str):
                severities.add(value)
            elif (key == "level" or key.endswith("_level")) and isinstance(value, str):
                levels.add(value)
            _report_words(value, severities, levels)
    elif isinstance(node, list):
        for item in node:
            _report_words(item, severities, levels)


def test_the_acoustic_quality_report_publishes_only_shared_words() -> None:
    """The one surface that builds plain dicts, checked on what it EMITS.

    ``acoustic_quality`` has no dataclass for a reader to type-check, so its
    conformance has to be measured on a real report. Both scales it publishes
    are covered: per-finding ``severity`` and every ``*level`` roll-up.
    """
    report = acoustic_quality.build_acoustic_quality_report(
        session_id="vocabulary-contract",
        capture_quality=[
            # No noise floor recorded -> the `unavailable` trust slot and the
            # `info` severity, neither of which a happy-path capture reaches.
            {"capture_kind": "position", "position_index": 0},
            # A real but poor SNR -> the `low` trust rank and a `warn`.
            {
                "capture_kind": "position",
                "position_index": 1,
                "estimated_snr_db": 4.0,
            },
            # Between the two thresholds -> the `medium` rank, the word this
            # whole ticket exists for.
            {
                "capture_kind": "position",
                "position_index": 2,
                "estimated_snr_db": 22.0,
            },
        ],
    )
    severities: set[str] = set()
    levels: set[str] = set()
    _report_words(report, severities, levels)

    assert severities, "fixture published no severities — it proves nothing"
    assert levels, "fixture published no levels — it proves nothing"
    assert severities <= _SEVERITY_WORDS, severities - _SEVERITY_WORDS
    assert levels <= _REPORT_WORDS | _TRUST_WORDS | {TRUST_UNAVAILABLE}, (
        levels - (_REPORT_WORDS | _TRUST_WORDS | {TRUST_UNAVAILABLE})
    )
    # The fixture actually reached the interesting ranks, so a future edit
    # that stops producing them fails here rather than passing vacuously.
    assert "medium" in levels
    assert TRUST_UNAVAILABLE in levels
    assert "info" in severities


def test_the_browser_audio_report_publishes_only_shared_words() -> None:
    """The other typed surface, checked on a report that trips both scales."""
    report = browser_audio.assess_browser_audio_path(
        input_device={
            "sample_rate": 44100,
            "channel_count": 2,
            "echo_cancellation": True,
        },
        expected_sample_rate=48000,
        has_mic_calibration=False,
    )
    assert report.level in _REPORT_WORDS
    assert {issue.severity for issue in report.issues} <= _SEVERITY_WORDS


def test_the_snr_refusal_rank_is_deliberately_a_different_vocabulary() -> None:
    """``snr_policy``'s rank must NOT be folded into :data:`TrustLevel`.

    It looks like a trust rank — the magnitude class even reads the same two
    thresholds — and is not one. It REFUSES a decision, ships a
    ``shortfall_db`` saying how many dB would clear it, and is scoped per
    decision class, so one capture is legitimately magnitude-``ok`` and
    alignment-``insufficient`` at once. Two trust labels contradicting each
    other on one number would be a bug; two refusals about two different
    decisions is the split policy working. Pinned so the resemblance never
    gets "fixed".
    """
    rank_words = set(snr_policy._VERDICT_RANK)
    assert rank_words.isdisjoint(_TRUST_WORDS), (
        "the SNR refusal rank started sharing words with the trust vocabulary "
        f"— overlap: {sorted(rank_words & _TRUST_WORDS)}"
    )
    assert "reduced" in rank_words and "insufficient" in rank_words


def test_the_verdict_headline_map_holds_only_real_verdict_values() -> None:
    """No synthetic key among the real ones.

    The quality-gated surface copy (#2058 B1) used to sit in this map under
    ``"surface_quality_gated"`` — a string that reads exactly like a
    ``Verdict`` member and is not one: the verdict stays the literal
    ``"surface"`` and the copy is selected by ``quality_gated`` being True.
    It is a module constant now, so it cannot be mistaken for a member of a
    map it is not in.
    """
    real = {verdict.value for verdict in acceptance.Verdict}
    assert set(envelope._VERDICT_HEADLINE) <= real, (
        "synthetic keys in _VERDICT_HEADLINE: "
        f"{sorted(set(envelope._VERDICT_HEADLINE) - real)}"
    )
