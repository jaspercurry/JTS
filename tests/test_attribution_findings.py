# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""WO-1 schema contracts: the finding artifact, the registry, the identity.

Pins the acceptance items ``docs/historical/attribution-stage-plan.md`` §7 assigns to
WO-1 that are about *shape* rather than *storage* — the schema test, the
golden round-trip, the stable cross-store session identity, and the plan rules
the excluded-band promotion path is bound by. Storage, retention, and the
per-position evidence live in ``tests/test_attribution_persistence.py``.
"""

from __future__ import annotations

import json
import logging

import pytest

from jasper.attribution.closed_sets import (
    CONFIDENCE_TIERS,
    EVIDENCE_TIERS,
    FIX_CLASSES,
    PROBES,
)
from jasper.attribution.findings import (
    EVIDENCE_STORE_BUNDLE,
    EVIDENCE_STORE_CAPTURE_RING,
    FINDING_FIELD_DESCRIPTIONS,
    FINDING_SCHEMA,
    FINDING_SET_SCHEMA,
    EvidenceRef,
    Finding,
    FindingError,
    FindingSet,
)
from jasper.active_speaker.crossover_v2.intervention import (
    REALIZED_LEVEL_SUSPECT_REASON,
)
from jasper.attribution.mechanisms import (
    MECHANISM_BOUNDARY_SBIR,
    MECHANISM_HF_REFLECTION,
    MECHANISM_LEVEL_FRAME,
    MECHANISM_REGISTRY,
    MechanismError,
    mechanism_spec,
)
from jasper.attribution.promotion import (
    LEVEL_FRAME_HOUSEHOLD_COPY,
    PRODUCED_BY,
    REALIZED_LEVEL_HOUSEHOLD_COPY,
    promote_carve_outs,
    promote_level_frame_disagreement,
)
from jasper.attribution.session_identity import (
    ALIAS_CAPTURE_SESSION_ID,
    SESSION_IDENTITY_KEY,
    SESSION_IDENTITY_SCHEME,
    SessionIdentity,
    SessionIdentityError,
    read_session_identity,
    stamp_session_identity,
)

_SESSION = SessionIdentity(
    session_id="7f54494228cc",
    aliases={ALIAS_CAPTURE_SESSION_ID: "cap_Ktm3xQ2p"},
)
_CITE = EvidenceRef(
    session=_SESSION,
    store=EVIDENCE_STORE_BUNDLE,
    locator="evidence/v1/artifacts/crossover_v2/cap_Ktm3xQ2p/cloud_measure.json",
    sha256="a" * 64,
)


def _finding(**overrides: object) -> Finding:
    kwargs: dict[str, object] = {
        "mechanism": MECHANISM_HF_REFLECTION,
        "band_hz": (4200.0, 4600.0),
        "evidence": {"tau_us": 310.0, "r_time": 0.28, "depth_db": -6.4},
        "confidence": "unsure",
        "fix_class": "carve",
        "household_copy": (
            "A narrow range here cancels rather than plays quietly, and "
            "adding level cannot fill a cancellation."
        ),
        "probes_run": ("P2",),
        "probes_recommended": ("P4",),
        "cites": (_CITE,),
    }
    kwargs.update(overrides)
    return Finding(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# The schema test (§7 acceptance)
# --------------------------------------------------------------------------- #


def test_finding_carries_exactly_the_plan_s_fields() -> None:
    """§3.1's artifact is ``{mechanism, band, evidence, confidence, fix_class,
    household_copy, probes_run, probes_recommended}`` plus the citation §7's
    acceptance requires. Serialized keys are pinned so a field cannot be
    dropped or renamed without this failing."""

    payload = _finding().to_dict()
    assert set(payload) == {
        "schema",
        "mechanism",
        "band_hz",
        "evidence",
        "confidence",
        "fix_class",
        "household_copy",
        "probes_run",
        "probes_recommended",
        "cites",
    }
    assert payload["schema"] == FINDING_SCHEMA


def test_every_serialized_field_carries_an_inline_description() -> None:
    """§6: "JSON with a ``schema`` field and inline field descriptions (no
    code-reading needed to interpret — the v2-state friction)". A new field
    that ships without its description fails here."""

    payload = _finding().to_dict()
    described = set(FINDING_FIELD_DESCRIPTIONS)
    assert described == set(payload) - {"schema"}
    for name, text in FINDING_FIELD_DESCRIPTIONS.items():
        assert text.strip(), name

    inline = FindingSet(
        session=_SESSION, produced_by=PRODUCED_BY, findings=(_finding(),)
    ).to_dict()["field_descriptions"]
    assert set(inline) == {"set", "finding"}
    assert set(inline["finding"]) == described


def test_the_closed_vocabularies_are_the_plan_s(  # noqa: D103
) -> None:
    # §3.3's closed set, verbatim and in order.
    assert FIX_CLASSES == (
        "delay",
        "polarity",
        "eq",
        "refit",
        "carve",
        "physical",
        "document_as_physics",
        "measure_differently",
    )
    # §3.2's Q6 ruling: three words, never a bare number.
    assert CONFIDENCE_TIERS == ("confident", "likely", "unsure")
    # §5's probe table.
    assert PROBES == ("P1", "P2", "P3", "P4", "P5", "P6", "P7")
    # §4's preamble. Pinned by ENUMERATION like its three siblings, not by the
    # membership check below: `spec.corpus_evidence_tier in EVIDENCE_TIERS`
    # passes just as happily against a set that grew a member or had one
    # renamed, so it reads as coverage while seeing neither. This set was the
    # one of the four left unpinned, which is the same three-not-four omission
    # the module and package docstrings carried.
    assert EVIDENCE_TIERS == (
        "adjudicated",
        "corroborating",
        "model_derived",
        "refuted",
    )


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"mechanism": "M99"}, "unregistered mechanism"),
        ({"confidence": "0.8"}, "confidence must be one of"),
        ({"fix_class": "delay"}, "may not route"),
        ({"fix_class": "not_a_class"}, "may not route"),
        ({"band_hz": (4600.0, 4200.0)}, "f_lo_hz < f_hi_hz"),
        ({"band_hz": (float("nan"), 4200.0)}, "finite"),
        ({"probes_run": ("P9",)}, "outside the §5 table"),
        ({"cites": ()}, "must cite at least one"),
        ({"evidence": {"tau_us": float("inf")}}, "finite"),
    ],
)
def test_a_malformed_finding_is_refused(overrides: dict, fragment: str) -> None:
    with pytest.raises(FindingError, match=fragment):
        _finding(**overrides)


def test_an_unsure_finding_must_recommend_a_probe() -> None:
    """§3.2: "``unsure`` carries the single recommended disambiguating
    probe". An unsure finding with nothing to recommend is a dead end dressed
    as a diagnosis — the flow could not offer §3.4's
    refuse-and-recommend-the-probe outcome."""

    with pytest.raises(FindingError, match="must recommend at least one probe"):
        _finding(confidence="unsure", probes_recommended=())
    # The same finding at a higher tier needs no recommendation.
    assert _finding(confidence="likely", probes_recommended=()).probes_recommended == ()


@pytest.mark.parametrize(
    "copy, fragment",
    [
        ("The horn reflects at this frequency.", "hardware-noun-free"),
        ("The tweeter is 7 dB dark here.", "hardware-noun-free"),
        ("This desk bounce cancels the low end.", "hardware-noun-free"),
        # Listed separately from "woofer" on purpose: the word boundary that
        # keeps "important" from tripping "port" also keeps "\bwoofer\b" from
        # matching inside "subwoofer". A live noun — JTS ships a local-DAC
        # subwoofer output group — so this gap was reachable.
        ("The subwoofer arrives late here.", "hardware-noun-free"),
        ("Both subwoofers arrive late here.", "hardware-noun-free"),
        ("This range is position_invariant.", "not an internal slug"),
        ("M2 explains this range.", "must not name a mechanism id"),
        ("   ", "non-empty"),
    ],
)
def test_household_copy_stays_phenomenon_level(copy: str, fragment: str) -> None:
    """§3.1's two-vocabularies rule, inherited verbatim from the shipped
    ``_null_classification_copy`` prohibition: "No hardware noun appears here
    … naming one would be the device-taxonomy guess this program forbids in
    shipped copy". ``mechanism`` may name hardware; ``household_copy`` may
    not."""

    with pytest.raises(FindingError, match=fragment):
        _finding(household_copy=copy)


def test_hardware_noun_matching_respects_word_boundaries() -> None:
    """A substring check would reject "important" for containing "port" and
    "concern" for containing "cone". The guard must be a word match, or it
    becomes the reason nobody can write a household sentence."""

    assert _finding(
        household_copy=(
            "It is important to note this concerns a narrow range, and the "
            "reported result is unchanged."
        )
    )


# --------------------------------------------------------------------------- #
# The golden round-trip (§7 acceptance)
# --------------------------------------------------------------------------- #

GOLDEN_FINDING_SET: dict = {
    "schema": "jts_attribution_finding_set/1",
    "session": {
        "scheme": "jts-session-1",
        "session_id": "7f54494228cc",
        "aliases": {"capture_session_id": "cap_Ktm3xQ2p"},
    },
    "produced_by": PRODUCED_BY,
    "findings": [
        {
            "schema": "jts_attribution_finding/1",
            "mechanism": "M2",
            "band_hz": [4200.0, 4600.0],
            "evidence": {"tau_us": 310.0, "r_time": 0.28, "depth_db": -6.4},
            "confidence": "unsure",
            "fix_class": "carve",
            "household_copy": (
                "A narrow range here cancels rather than plays quietly, and "
                "adding level cannot fill a cancellation."
            ),
            "probes_run": ["P2"],
            "probes_recommended": ["P4"],
            "cites": [
                {
                    "session": {
                        "scheme": "jts-session-1",
                        "session_id": "7f54494228cc",
                        "aliases": {"capture_session_id": "cap_Ktm3xQ2p"},
                    },
                    "store": "commissioning_bundle",
                    "locator": (
                        "evidence/v1/artifacts/crossover_v2/cap_Ktm3xQ2p/"
                        "cloud_measure.json"
                    ),
                    "sha256": "a" * 64,
                }
            ],
        }
    ],
}


def test_one_golden_finding_round_trips_byte_for_byte() -> None:
    """§7 acceptance: "one golden finding round-trip". Both directions, and
    through canonical JSON — the exact serialization the evidence store
    persists and re-hashes, so a change that survives in Python but not on
    disk still fails."""

    built = FindingSet(
        session=_SESSION, produced_by=PRODUCED_BY, findings=(_finding(),)
    )
    payload = built.to_dict()
    payload.pop("field_descriptions")
    assert payload == GOLDEN_FINDING_SET
    # The golden's schema tags are the shipped constants, not a copy that
    # could drift past a version bump unnoticed.
    assert GOLDEN_FINDING_SET["schema"] == FINDING_SET_SCHEMA
    assert GOLDEN_FINDING_SET["findings"][0]["schema"] == FINDING_SCHEMA

    reopened = FindingSet.from_mapping(built.to_dict())
    assert reopened == built

    canonical = json.dumps(built.to_dict(), sort_keys=True, separators=(",", ":"))
    assert FindingSet.from_mapping(json.loads(canonical)) == built


def test_reopen_refuses_an_unknown_or_missing_field() -> None:
    """Strictness on read is what makes the round-trip a contract rather than
    a coincidence: a field this version does not understand must not be
    silently dropped on the next write."""

    payload = _finding().to_dict()
    with pytest.raises(FindingError):
        Finding.from_mapping({**payload, "surprise": 1})
    with pytest.raises(FindingError):
        Finding.from_mapping({k: v for k, v in payload.items() if k != "evidence"})
    with pytest.raises(FindingError):
        Finding.from_mapping({**payload, "schema": "jts_attribution_finding/99"})


# --------------------------------------------------------------------------- #
# The registry (§2 shape, §10 do-not)
# --------------------------------------------------------------------------- #


def test_no_mechanism_entry_without_a_citation_carrying_its_tier() -> None:
    """§10's first do-not, enforced structurally rather than by review:
    ``MechanismSpec`` cannot be constructed without both fields, so a
    speculative entry fails at import."""

    assert MECHANISM_REGISTRY, "the registry must not be empty"
    for mechanism_id, spec in MECHANISM_REGISTRY.items():
        assert spec.id == mechanism_id
        assert spec.corpus_citation.strip()
        assert spec.corpus_evidence_tier in EVIDENCE_TIERS
        assert set(spec.fix_classes) <= set(FIX_CLASSES)
        assert set(spec.discriminating_probes) <= set(PROBES)


def test_the_registry_is_a_declaration_map_not_a_plugin_registry() -> None:
    """§2: the shape is the shipped registry-of-declarations — a pure-data
    mapping from a stable id to a spec, read by an engine with zero per-entry
    knowledge — deliberately NOT the transit provider pattern. A declaration
    that grew behaviour would be the drift this pins."""

    for spec in MECHANISM_REGISTRY.values():
        for value in vars(spec).values():
            assert not callable(value), f"{spec.id} declaration grew a callable"
    with pytest.raises(MechanismError):
        mechanism_spec("M404")


def test_m5_declares_both_branches_of_its_conditional_routing() -> None:
    """§4 M5, library panel 2026-07-29: the split is a *detector*
    requirement. A position-variant interference null routes ``physical`` and
    never ``eq``; boundary **loading** — broad, minimum-phase-ish, tracking
    solid angle — is legitimately EQ-able. The registry declares both because
    the blanket ``physical`` the row shipped with was stronger than any
    school."""

    spec = mechanism_spec(MECHANISM_BOUNDARY_SBIR)
    assert set(spec.fix_classes) == {"physical", "eq"}


# --------------------------------------------------------------------------- #
# The stable cross-store session identity (§6, §7 acceptance)
# --------------------------------------------------------------------------- #


def test_the_identity_round_trips_and_survives_a_flat_carrier() -> None:
    assert SessionIdentity.from_mapping(_SESSION.to_dict()) == _SESSION
    assert _SESSION.token == "jts-session-1:7f54494228cc"
    # A token carries the identity, not its decoration.
    assert SessionIdentity.from_token(_SESSION.token).session_id == _SESSION.session_id


def test_one_key_name_carries_the_identity_through_every_store() -> None:
    """§6's requirement is "one identifier that survives every hop". The
    mechanism is one canonical key: a reader that finds
    ``jts_session_identity`` in a bundle artifact, a capture-ring sidecar, or
    a laptop archive manifest resolves the session without knowing which
    store it came from."""

    hops = [
        {"kind": "jts_crossover_v2_cloud_evidence", "phase": "cloud_measure"},
        {"phase": "measure", "wav_sha256": "b" * 64},
        {"schema": "jts_attribution_finding_set/1"},
    ]
    for payload in hops:
        stamp_session_identity(payload, _SESSION)
        assert SESSION_IDENTITY_KEY in payload
        assert read_session_identity(payload) == _SESSION


def test_an_identity_without_a_session_id_is_refused_by_name() -> None:
    """``from_mapping`` refused unknown fields but never REQUIRED the one
    field that matters, so a mapping missing it fell through to the charset
    check and reported a malformed identifier rather than a missing one.
    (``Finding.from_mapping`` checks presence explicitly — this was the
    inconsistent sibling.) Caught by the mypy lenient-baseline gate, which
    gives unbaselined modules full strictness."""

    for raw in ({}, {"scheme": SESSION_IDENTITY_SCHEME}, {"session_id": None},
                {"session_id": 12}):
        with pytest.raises(SessionIdentityError):
            SessionIdentity.from_mapping(raw)


def test_an_unstamped_payload_reads_as_legacy_not_as_an_error() -> None:
    """Every artifact written before this module existed carries no identity
    key — and that corpus is exactly what WO-0 had to read. Absence is
    history; malformation is a writer bug."""

    assert read_session_identity({"phase": "measure"}) is None
    assert read_session_identity(None) is None
    with pytest.raises(SessionIdentityError):
        read_session_identity({SESSION_IDENTITY_KEY: {"session_id": "x", "junk": 1}})


def test_a_token_cannot_be_confused_with_a_content_hash() -> None:
    """§6: "Content hashing stays the *verifier*; it must stop being the
    *index*." The scheme prefix is what keeps an identity self-identifying
    wherever it appears, so a bare hex digest can never be mistaken for one."""

    with pytest.raises(SessionIdentityError):
        SessionIdentity.from_token("a" * 64)
    with pytest.raises(SessionIdentityError):
        SessionIdentity(session_id="7f54494228cc", scheme="sha256")


# --------------------------------------------------------------------------- #
# The excluded-band promotion path (§3.1's embryo)
# --------------------------------------------------------------------------- #


def _carve_outs(**overrides: object) -> list[dict]:
    row = {
        "f_lo_hz": 4200.0,
        "f_hi_hz": 4600.0,
        "source": "identified_null",
        "f_center_hz": 4400.0,
        "n": 3,
        "tau_us": 310.0,
        "r_time": 0.28,
        "r_freq": 0.31,
        "depth_db": -6.4,
        "classification": "position_invariant",
        "reason": (
            "This range is a cancellation between two arrivals. Adding level "
            "cannot fill a cancellation, so it is left out of correction and "
            "out of grading. It sat at the same frequencies at every "
            "microphone position."
        ),
    }
    row.update(overrides)
    return [{"band_hz": [2000.0, 8000.0], "intervals": [row]}]


def test_the_embryo_becomes_a_finding_with_mechanism_and_fix_class() -> None:
    """§3.1: "The excluded-band tau records are the embryo: [attribution]
    promotes them from 'reason to refuse EQ' to findings with mechanism and
    fix class attached."""

    findings = promote_carve_outs(
        _carve_outs(), session=_SESSION, cites=(_CITE,)
    )
    assert len(findings) == 1
    finding = findings[0]
    assert finding.mechanism == MECHANISM_HF_REFLECTION
    assert finding.fix_class == "carve"
    assert finding.band_hz == (4200.0, 4600.0)
    assert finding.evidence["tau_us"] == 310.0
    assert finding.evidence["classification"] == "position_invariant"


def test_a_p2_only_finding_never_rises_above_unsure() -> None:
    """§5, unconditionally: "Any finding whose only support is P2 stays
    ``unsure`` with P4 as its recommended probe." The cloud is a P2
    instrument — the shipped ``interference_nulls`` docstring says a single
    session cannot separate "travels with the speaker" from "a path that did
    not change while measuring" — so no tau, however clean, earns a higher
    tier from this path."""

    for classification in ("position_invariant", "position_dependent"):
        findings = promote_carve_outs(
            _carve_outs(classification=classification),
            session=_SESSION,
            cites=(_CITE,),
        )
        assert [f.confidence for f in findings] == ["unsure"]
        assert [f.probes_run for f in findings] == [("P2",)]
        assert [f.probes_recommended for f in findings] == [("P4",)]


def test_promotion_never_routes_eq() -> None:
    """§3.3's hard rule, pinned: ``eq`` is never the routed class for a
    position-variant null or a source-fixed interference ripple. The warrant
    is the physics one (§11.3 X22) — energy added into a cancellation is
    itself cancelled, so the null is an interference zero, not a deficit the
    drive can fill — not the headphones-only psychoacoustic one."""

    for classification in ("position_invariant", "position_dependent"):
        findings = promote_carve_outs(
            _carve_outs(classification=classification),
            session=_SESSION,
            cites=(_CITE,),
        )
        assert findings
        assert all(f.fix_class != "eq" for f in findings)
    variant = promote_carve_outs(
        _carve_outs(classification="position_dependent"),
        session=_SESSION,
        cites=(_CITE,),
    )
    assert variant[0].mechanism == MECHANISM_BOUNDARY_SBIR
    assert variant[0].fix_class == "physical"


def test_an_unattributable_record_is_left_alone_not_guessed_at() -> None:
    """§10: "No speculative mechanisms." A position-screen carve has no tau
    and no classification by construction, and a null the shipped gate called
    ``insufficient_evidence`` has, by that gate's own verdict, nothing to
    attribute."""

    assert (
        promote_carve_outs(
            _carve_outs(source="position_screen", classification=""),
            session=_SESSION,
            cites=(_CITE,),
        )
        == ()
    )
    assert (
        promote_carve_outs(
            _carve_outs(classification="insufficient_evidence"),
            session=_SESSION,
            cites=(_CITE,),
        )
        == ()
    )
    assert promote_carve_outs(None, session=_SESSION, cites=(_CITE,)) == ()
    assert promote_carve_outs([], session=_SESSION, cites=(_CITE,)) == ()


@pytest.mark.parametrize(
    "bound",
    [
        {"f_lo_hz": None},
        {"f_hi_hz": None},
        {"f_lo_hz": "4200"},
        {"f_hi_hz": float("nan")},
        {"f_lo_hz": float("inf")},
        {"f_hi_hz": True},
    ],
)
def test_an_attributable_record_with_an_unusable_band_is_refused_loudly(
    bound: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """The chosen behaviour, pinned: **refused with an event**, not skipped.

    This record IS attributable — the shipped gate classified it — but its
    own frequency edges are missing or non-finite, which makes it malformed
    rather than "correctly not attributable". Those are different states and
    only the first deserves a log line, so a silent drop here would hide a
    genuine data defect behind the same silence that correctly covers a
    position-screen carve.

    ``True`` is in the table on purpose: ``isinstance(True, int)`` is true in
    Python, so a band edge of ``True`` would otherwise be accepted as 1.0 Hz.

    The refusal is not new — ``Finding``'s own validation already rejected
    these and the caller already logged. What the explicit narrowing adds is
    that the call site passes a real ``tuple[float, float]`` instead of two
    ``Any | None`` values that only happened to be numbers, and that the
    message now NAMES the bad bound instead of reporting a generic band
    error. The message assertion below is what pins that, and it is what
    fails if the narrowing is removed and the record falls through to the
    constructor again.
    """

    with caplog.at_level(logging.WARNING, logger="jasper.attribution.promotion"):
        findings = promote_carve_outs(
            _carve_outs(**bound), session=_SESSION, cites=(_CITE,)
        )

    assert findings == ()
    assert "attribution.carve_out_promotion_refused" in caplog.text
    assert "no usable band" in caplog.text
    # The offending values are quoted, so a reader does not have to re-derive
    # which edge was bad from the record.
    assert "f_lo_hz=" in caplog.text and "f_hi_hz=" in caplog.text


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "position_screen", "classification": ""},
        {"classification": "insufficient_evidence"},
    ],
)
def test_a_correctly_unattributable_record_is_skipped_silently(
    overrides: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """The other half of the same distinction. §10's "no speculative
    mechanisms" means these records SHOULD produce nothing, so producing a
    warning for them would train a reader to ignore the warning that matters."""

    with caplog.at_level(logging.WARNING, logger="jasper.attribution.promotion"):
        findings = promote_carve_outs(
            _carve_outs(**overrides), session=_SESSION, cites=(_CITE,)
        )

    assert findings == ()
    assert caplog.text == ""


def test_a_straddling_null_becomes_one_finding_not_two() -> None:
    """``carve_outs_by_band`` lists a null under every spec band it overlaps —
    correct for disclosure, since it removes bins from both — but a
    straddling null is one physical feature."""

    row = _carve_outs()[0]["intervals"][0]
    straddling = [
        {"band_hz": [250.0, 2000.0], "intervals": [row]},
        {"band_hz": [2000.0, 8000.0], "intervals": [row]},
    ]
    assert len(
        promote_carve_outs(straddling, session=_SESSION, cites=(_CITE,))
    ) == 1


def test_the_household_sentence_is_the_shipped_one_copied() -> None:
    """One owner per verdict (§3.1): the carve-out record's own ``reason`` is
    the shipped SSOT for what a household is told about an excluded band.
    Minting a second sentence here would put the hardware-noun prohibition in
    two places instead of one."""

    carve_outs = _carve_outs()
    shipped = carve_outs[0]["intervals"][0]["reason"]
    findings = promote_carve_outs(carve_outs, session=_SESSION, cites=(_CITE,))
    assert findings[0].household_copy == shipped


def test_a_finding_must_be_anchored_in_the_bundle_that_holds_its_evidence() -> None:
    """Q-C, made structural. A finding whose only citations were the capture
    ring or a laptop archive would have no lifetime binding at all — both
    prune on their own schedules — so it could silently outlive everything
    that justified it."""

    ring_only = EvidenceRef(
        session=_SESSION,
        store=EVIDENCE_STORE_CAPTURE_RING,
        locator="1785340835947079_cloud_measure_iPhone.wav",
    )
    with pytest.raises(FindingError):
        _finding(cites=(ring_only,))
    # A ring citation alongside a bundle one is fine: it is a cross-store
    # join, not a support claim.
    assert _finding(cites=(_CITE, ring_only)).cites == (_CITE, ring_only)


def test_a_set_may_not_hold_a_finding_from_another_session_s_bundle() -> None:
    other = SessionIdentity(session_id="9445639e508f")
    stray = _finding(
        cites=(
            EvidenceRef(
                session=other,
                store=EVIDENCE_STORE_BUNDLE,
                locator="evidence/v1/artifacts/crossover_v2/x/cloud_measure.json",
            ),
        )
    )
    with pytest.raises(FindingError):
        FindingSet(session=_SESSION, produced_by=PRODUCED_BY, findings=(stray,))


# --------------------------------------------------------------------------- #
# The level-frame promotion path (#1866 frame-gate ruling, 2026-07-30)
# --------------------------------------------------------------------------- #


def _level_frame_record(**overrides: object) -> dict:
    """A banked record in the shape
    ``crossover_v2.accountability.level_frame_record`` emits.

    Numbers are the conductor fixture's own, measured in
    ``tests/test_crossover_v2_conductor.py`` on a woofer carrying an extra
    -1.6 dB/octave of passband tilt — the synthetic stand-in for the
    2026-07-30 field session (3.2307 dB frame, realized -0.247, both recorded
    on #1870 and not replayable in-repo).

    This is the ESTIMATOR shape: the two estimates disagree and the realized
    check PASSES (-0.828 dB, inside 3.0). ``_realized_only_record`` below is
    its sibling for the other condition the record can now carry.
    """

    record = {
        "f_lo_hz": 150.0,
        "f_hi_hz": 5844.7,
        "level_match_axis": "design_axis_0deg",
        "estimator_worst_delta_db": 3.209,
        "estimator_tolerance_db": 3.0,
        "reason": "level_definitions_differ",
        "realized_difference_db": -0.828,
        "realized_tolerance_db": 3.0,
        "realized_level_w_db": 0.213,
        "realized_level_t_db": -0.615,
        "core_level_db_woofer": 3.276,
        "core_band_lo_hz_woofer": 150.0,
        "core_band_hi_hz_woofer": 1255.8,
        "radiating_band_lo_hz_woofer": 0.0,
        "radiating_band_hi_hz_woofer": 1282.3,
        "trim_band_average_db_woofer": -0.067,
        "estimator_delta_db_woofer": 0.0,
        "core_level_db_tweeter": 0.0,
        "core_band_lo_hz_tweeter": 2020.0,
        "core_band_hi_hz_tweeter": 5844.7,
        "radiating_band_lo_hz_tweeter": 1996.4,
        "radiating_band_hi_hz_tweeter": None,
        "trim_band_average_db_tweeter": 0.0,
        "estimator_delta_db_tweeter": 3.209,
    }
    record.update(overrides)
    return record


def _level_frame_finding(**overrides: object):
    return promote_level_frame_disagreement(
        _level_frame_record(**overrides), session=_SESSION, cites=(_CITE,)
    )


def _realized_only_record(**overrides: object) -> dict:
    """The record ``level_frame_record`` builds when ONLY the realized check
    fires — the shape the realized-level demotion (doctrine deviation (i))
    made reachable, and the one that used to be a refusal instead.

    Derived from the estimator fixture by making it true of the other
    condition rather than by inventing a second table: the estimator pair is
    dropped (they AGREED, so ``level_consistency`` contributes nothing and its
    two keys are absent), the reason is the realized sibling, and the realized
    difference is the ~9 dB the 2026-07-27 dark-tweeter profile measured — the
    case ``refusal_copy``'s deleted row was written for.
    """

    record = _level_frame_record()
    for key in ("estimator_worst_delta_db", "estimator_tolerance_db"):
        record.pop(key)
    for role in ("woofer", "tweeter"):
        record.pop(f"estimator_delta_db_{role}")
    record.update(
        reason=REALIZED_LEVEL_SUSPECT_REASON,
        realized_difference_db=-9.0,
        realized_level_w_db=0.213,
        realized_level_t_db=-8.787,
        polish_delta_db_woofer=0.0,
        polish_delta_db_tweeter=0.0,
    )
    record.update(overrides)
    return record


def _realized_only_finding(**overrides: object):
    return promote_level_frame_disagreement(
        _realized_only_record(**overrides), session=_SESSION, cites=(_CITE,)
    )


def test_a_realized_level_mismatch_is_promoted_as_its_own_condition() -> None:
    """**The blocker this test exists for.** The producer was widened to bank a
    realized-only record; its only production reader was not, so every such
    record rendered the ESTIMATOR finding — a household sentence that is false
    in all three of its claims here, routed to the wrong fix class.

    What must be true instead: the promoter reads the record's own ``reason``
    and nothing else, and a realized-only record gets ``eq`` — §4 M7's own
    split, ``eq`` "when a driver's level is genuinely low" against ``refit``
    "when the level error is upstream in the fit's own frame". The frame is not
    in dispute here; the pair that would ship is simply not level.
    """

    finding = _realized_only_finding()
    assert finding is not None
    assert finding.mechanism == MECHANISM_LEVEL_FRAME
    assert finding.fix_class == "eq"
    # …and the definition arm still routes the other way, so this is a branch
    # and not a flip. All three classes are declared on M7: `eq` for this arm,
    # `document_as_physics` for the definition gap S8 explained, and `refit`
    # kept because §4 M7 declares it and a genuine upstream frame error can
    # still occur — the definition gap just stopped being an instance of one.
    assert _level_frame_finding().fix_class == "document_as_physics"
    assert set(mechanism_spec(MECHANISM_LEVEL_FRAME).fix_classes) == {
        "eq", "refit", "document_as_physics",
    }


def test_the_realized_household_copy_is_true_and_still_recommends() -> None:
    """Doctrine §3: a defect outside §4's closed list "discloses **and
    recommends a next action**". The demotion removed the stop; it must not
    also have removed the recommendation.

    Three things are asserted, each one a clause of the estimator sentence that
    is FALSE about this record:

    * it does not claim two cross-checks disagreed — they agreed, which is why
      this reason won;
    * it does not reassure that "nothing was guessed at", which is exactly the
      thing that is wrong;
    * it does not send the household to re-run the room pass, which does not
      address a sensitivity figure or a pad.

    And the recommendation the deleted ``refusal_copy`` row carried — "Re-check
    the driver details — sensitivity and any resistor pad — in speaker setup,
    then measure again" — survives, minus the two things about it that stopped
    being true: the speaker is NOT left alone (the round proceeds), and the
    hardware nouns cannot pass ``Finding``'s own validator.
    """

    finding = _realized_only_finding()
    assert finding.household_copy == REALIZED_LEVEL_HOUSEHOLD_COPY
    assert finding.household_copy != LEVEL_FRAME_HOUSEHOLD_COPY
    lowered = REALIZED_LEVEL_HOUSEHOLD_COPY.lower()

    for false_clause in (
        "cross-check", "cross check", "nothing was guessed",
        "room pass", "left your speaker alone", "left alone",
    ):
        assert false_clause not in lowered, false_clause
    # The recommendation, in the two words that make it actionable.
    assert "sensitivity" in lowered
    assert "pad" in lowered
    assert "measure again" in lowered
    # It must not claim the tuning was applied — the candidate is a PROPOSAL at
    # the moment this is minted, same rule as the sibling copy.
    for banned in ("applied", "was kept", "your speaker now", "phone"):
        assert banned not in lowered, banned
    # …and it must not claim the pair WAS measured at those levels. The check
    # reads the fit's MODEL of the emission — the measured per-branch responses
    # through the modelled correction — not a capture of the applied tuning,
    # which is the delta probe's job after an apply that has not happened. The
    # deleted refusal's "would not have ended up at matching levels" is the
    # tense this instrument earns, and the copy keeps it.
    assert "would not end up" in lowered
    for overclaim in (
        "measured on", "we measured", "confirmed", "verified", "proved",
        "independent",
    ):
        assert overclaim not in lowered, overclaim
    # §3.1's contract holds on the new string too, enforced by the validator
    # rather than by this list: a round-trip reconstructs it unchanged.
    assert Finding.from_mapping(finding.to_dict()).household_copy == (
        REALIZED_LEVEL_HOUSEHOLD_COPY
    )
    # Short enough to read on a review screen (IA over copy) — the same bar
    # its sibling is held to, even though this one also carries a
    # recommendation and that one does not.
    assert len(REALIZED_LEVEL_HOUSEHOLD_COPY) < 260


def test_the_realized_finding_is_unsure_for_its_own_reason() -> None:
    """The tier is the same as the estimator arm's and the argument is not.

    ``realized_branch_level_match``'s own docstring says it is "One estimator,
    not a second opinion" — the same power-band average over the same halves
    that set the trim — so none of the estimator arm's rival-span artifacts
    apply. What is unsure is the CAUSE: the levels are read off the emission
    the fit MODELS, not off a post-apply capture, and nothing here separates a
    wrong sensitivity or pad value from an error in the fit's own frame.

    ``unsure`` obliges a recommended probe (§3.2), and this finding carries
    M7's two raisers rather than claiming a probe ran.
    """

    finding = _realized_only_finding()
    assert finding.confidence == "unsure"
    assert finding.probes_run == ()
    assert finding.probes_recommended == ("P5", "P7")


def test_the_realized_evidence_rides_and_the_estimator_keys_are_named() -> None:
    """Every non-band key is evidence, and the two instruments' keys are told
    apart by name rather than by a reader's memory.

    A realized-only record carries no estimator pair at all, so a bare
    ``worst_delta_db`` beside ``reason=realized_levels_disagree`` would be a
    number from the OTHER instrument wearing an unqualified name. The producer
    prefixes them; this pins that a reader of the finding sees the prefix.
    """

    evidence = _realized_only_finding().evidence
    assert evidence["reason"] == REALIZED_LEVEL_SUSPECT_REASON
    assert evidence["realized_difference_db"] == -9.0
    assert evidence["realized_tolerance_db"] == 3.0
    assert evidence["realized_level_w_db"] == 0.213
    assert evidence["realized_level_t_db"] == -8.787
    # The attribution that says how much of the mismatch the MEASURE ripple
    # polish could explain. Zero here, which is the reader's cue to look
    # elsewhere — the disclosure's whole job.
    assert evidence["polish_delta_db_tweeter"] == 0.0
    assert evidence["polish_delta_db_woofer"] == 0.0
    # No estimator keys on THIS fixture, which models the session that
    # produced no consistency verdict at all. A realized-only record whose
    # estimators ran and AGREED still carries them — prefixed — which is what
    # makes the prefix load-bearing rather than cosmetic; the producer's own
    # test covers that shape.
    assert not [key for key in evidence if key.startswith("estimator_")]
    # …and on the estimator record they are present, PREFIXED.
    estimator_evidence = _level_frame_finding().evidence
    assert estimator_evidence["estimator_worst_delta_db"] == 3.209
    assert estimator_evidence["estimator_tolerance_db"] == 3.0
    assert "worst_delta_db" not in estimator_evidence
    assert "tolerance_db" not in estimator_evidence


def test_the_estimator_reason_still_owns_the_copy_when_both_conditions_fire(
) -> None:
    """The producer writes ONE reason and the estimator condition wins it. This
    pins that the promoter follows that ordering rather than re-deriving which
    condition fired from the numbers.

    Why the estimator sentence is still TRUE here, and not merely the one the
    precedence happens to pick: two instruments that disagree about the frame
    make the realized read downstream of a suspect frame, so "this room pass is
    worth re-running" is the right first move and acting on a setup value is
    not. Re-deriving the realized verdict here to append a second sentence is
    what the promoter's own "re-decides nothing" rule forbids.
    """

    both = _level_frame_record(realized_difference_db=-9.0)
    finding = promote_level_frame_disagreement(
        both, session=_SESSION, cites=(_CITE,)
    )
    assert finding is not None
    assert finding.household_copy == LEVEL_FRAME_HOUSEHOLD_COPY
    assert finding.fix_class == "document_as_physics"
    # Nothing is lost to the precedence: the realized numbers still ride.
    assert finding.evidence["realized_difference_db"] == -9.0
    assert finding.evidence["estimator_worst_delta_db"] == 3.209


def test_a_banked_frame_disagreement_becomes_an_m7_finding() -> None:
    """The owner's 2026-07-30 ruling on #1866, as an artifact: a frame
    disagreement that the realized-level check's PASS allows to proceed is
    banked rather than refused, and what is banked is an M7-class finding
    carrying the numbers.

    ``document_as_physics``, and NOT ``refit``. This arm routed ``refit``
    while the two numbers were read as competing estimates of one frame, on
    the argument that a disagreement between them is upstream of every trim
    derived from them. Ruling S8 established they were never estimates of one
    quantity — the handover level and the passband average measure different
    things, and on a sloped horn they legitimately differ by many dB — so
    there is nothing to re-solve. The routing has to agree with the household
    sentence, which asks for nothing.
    """

    finding = _level_frame_finding()
    assert finding is not None
    assert finding.mechanism == MECHANISM_LEVEL_FRAME
    assert finding.fix_class == "document_as_physics"
    assert finding.band_hz == (150.0, 5844.7)


def test_the_banked_finding_carries_all_three_instruments() -> None:
    """The evidence conventions the ruling names — "both estimator levels +
    both bands" — plus the tiebreaker that decided the session.

    A reader of this finding is being asked to believe a session proceeded
    past a gate that would have stopped it, so all three level instruments
    ride: the fit's per-driver median, the trim solve's per-driver
    level-match term, and the realized check. Banking only the two that
    disagreed would record the argument and drop the evidence that settled
    it.
    """

    evidence = _level_frame_finding().evidence
    # Estimator 1 (the fit's median) and estimator 2 (the trim solve's
    # level-match term), per role.
    assert evidence["core_level_db_woofer"] == 3.276
    assert evidence["core_level_db_tweeter"] == 0.0
    assert evidence["trim_band_average_db_woofer"] == -0.067
    assert evidence["trim_band_average_db_tweeter"] == 0.0
    # Both bands, per role: the span the median was actually taken over, and
    # the radiating bound it was asked for (#1929's disclosure pair).
    assert evidence["core_band_lo_hz_woofer"] == 150.0
    assert evidence["core_band_hi_hz_woofer"] == 1255.8
    assert evidence["radiating_band_lo_hz_woofer"] == 0.0
    assert evidence["radiating_band_hi_hz_woofer"] == 1282.3
    assert evidence["core_band_lo_hz_tweeter"] == 2020.0
    assert evidence["core_band_hi_hz_tweeter"] == 5844.7
    assert evidence["radiating_band_lo_hz_tweeter"] == 1996.4
    # A high-pass branch radiates to infinity; ``None`` says so and survives
    # JSON, where ``inf`` does not.
    assert evidence["radiating_band_hi_hz_tweeter"] is None
    # WHICH axis every level above was read on. Without it the numbers are
    # unplaceable: on-axis, listening-window and power-response ratios differ
    # wherever beaming and horn directivity mismatch, and there is no single
    # correct level to assume (Toole). It rides unconditionally, so a reader of
    # ANY banked level finding can recover the frame.
    from jasper.active_speaker.crossover_v2.intervention import LEVEL_MATCH_AXIS

    assert evidence["level_match_axis"] == LEVEL_MATCH_AXIS
    # The disagreement and its tolerance, both PREFIXED with the instrument
    # they belong to, plus the realized check — which passes on this record and
    # so is not why it was banked.
    assert evidence["estimator_worst_delta_db"] == 3.209
    assert evidence["estimator_tolerance_db"] == 3.0
    assert evidence["realized_difference_db"] == -0.828
    assert evidence["realized_tolerance_db"] == 3.0


def test_the_banked_finding_says_what_the_session_did_about_it() -> None:
    """#2599, in the artifact the household's own findings file is minted from.

    A reader of this finding is being told the two per-driver estimators
    disagreed. Since #2609 that disagreement changes NOTHING about the tune —
    the pair is anchored on the raw measured trim either way — so what the
    record has to carry is the disagreement itself, per role, and the reason
    code that names it. There is no action and no dB cost to report, which is
    why the ``frame_exclusion_reason`` / ``anchor_delta_db_*`` pair this test
    used to assert is gone rather than left reading zero.
    """

    evidence = _level_frame_finding().evidence

    assert evidence["reason"] == "level_definitions_differ"
    assert evidence["estimator_delta_db_tweeter"] == 3.209
    assert evidence["estimator_delta_db_woofer"] == 0.0


def test_the_banked_finding_stays_unsure_and_claims_no_probe() -> None:
    """Why ``unsure`` is the honest tier and not a cautious one.

    THAT the two estimators disagreed is measured; that the disagreement is a
    real inter-driver level error is not, because the shipped gate's own
    residual produces this exact signature on a healthy speaker — a pair
    identical by construction reads 0.910 dB apart, and ordinary woofer
    passband tilt adds roughly 1.33 dB per dB/octave (both measured in
    ``tests/test_crossover_v2_conductor.py``). A single session cannot
    separate "the drivers really sit that far apart" from "these two
    estimators read different spans of a curve that is not flat in the same
    way over both".

    ``probes_run`` is empty for the same reason: the evidence is the flow's
    own instruments, none of which is a §5 primitive, and naming one would be
    the cheapest possible way to launder a model-derived number into
    probe-adjudicated standing.
    """

    finding = _level_frame_finding()
    assert finding.confidence == "unsure"
    assert finding.probes_run == ()
    assert finding.probes_recommended == ("P5", "P7")
    # §3.2's rule, which this finding must not be the exception to: an
    # ``unsure`` finding always carries the probe that would raise it.
    assert set(finding.probes_recommended) <= set(PROBES)


def test_the_banked_finding_s_household_copy_obeys_the_conventions() -> None:
    """§3.1's two-vocabularies contract, applied to the one sentence this
    path mints.

    It is minted rather than copied — unlike the carve-out path, the frame
    gate had no shipped sentence for a session that PROCEEDS. So the
    prohibition has to bite on a new string, and it does: ``Finding`` itself
    rejects a hardware noun, an internal slug, or a mechanism id, so a future
    edit reaching for "woofer" fails at construction rather than in front of a
    household. (Its realized-level sibling is held to the same bar, one test
    up, where the noun ban is what forced the deleted refusal's wording to be
    carried over rather than pasted.)
    """

    finding = _level_frame_finding()
    assert finding.household_copy == LEVEL_FRAME_HOUSEHOLD_COPY
    # The three checks, run against the shipped string rather than a fixture
    # of it — this is the copy that will render.
    assert Finding.from_mapping(finding.to_dict()).household_copy == (
        LEVEL_FRAME_HOUSEHOLD_COPY
    )
    # It says what happened and no more. In particular it must NOT claim the
    # tuning was applied: the candidate is a PROPOSAL at the moment this is
    # minted, and the apply is a separate household action (two-stage split).
    lowered = LEVEL_FRAME_HOUSEHOLD_COPY.lower()
    assert "applied" not in lowered
    assert "phone" not in lowered
    for banned in ("was kept", "we have applied", "your speaker now"):
        assert banned not in lowered
    # …and it must not claim the check was INDEPENDENT. It is
    # ``realized_branch_level_match``, whose own docstring says "One estimator,
    # not a second opinion" — the same power-band average over the same halves
    # that set the trim, re-read on the pair about to ship. It is independent
    # of the fit's median and it is non-vacuous, but "an independent check"
    # invites a household to read it as a second opinion that settled which
    # measurement was right, and nothing in the session did that. Simple copy
    # is allowed; false copy is not.
    for overclaim in ("independent", "confirmed", "verified", "proved"):
        assert overclaim not in lowered, overclaim
    # Short enough to read on a review screen (IA over copy).
    assert len(LEVEL_FRAME_HOUSEHOLD_COPY) < 260


def test_the_banked_finding_re_decides_nothing() -> None:
    """The flow owns the decision; this path owns the translation.

    There is no threshold here, no comparison of the disagreement against the
    tolerance, and no re-reading of the realized check — all three are the
    gate's, and duplicating any of them would be the second computation of one
    verdict §3.1 forbids. A record that says the estimators agreed, or that
    the realized check failed, is still promoted verbatim: it could only have
    arrived from a gate that decided to bank it, and second-guessing that here
    would make two owners for one ruling.

    ``reason`` is the ONE field it does read, and reading it is not
    re-deciding: the producer already wrote which condition fired, and choosing
    the sentence for a condition somebody else decided is exactly the
    translation this path is for. What it must never do is compare
    ``realized_difference_db`` against ``realized_tolerance_db`` itself, which
    is what the second half of this test pins.
    """

    contradictory = _level_frame_finding(
        estimator_worst_delta_db=0.1, realized_difference_db=99.0
    )
    assert contradictory is not None
    assert contradictory.evidence["estimator_worst_delta_db"] == 0.1
    assert contradictory.evidence["realized_difference_db"] == 99.0
    # A realized number far past its own tolerance does not move the copy or
    # the routing on an ESTIMATOR record: only ``reason`` does that. If the
    # promoter ever re-derived the realized verdict, this record — whose
    # realized error is 99 dB — would flip, and it must not.
    assert contradictory.household_copy == LEVEL_FRAME_HOUSEHOLD_COPY
    assert contradictory.fix_class == "document_as_physics"
    # …and symmetrically, a realized-only record whose realized error is INSIDE
    # tolerance still gets the realized treatment, because its reason says so.
    inside = _realized_only_finding(realized_difference_db=-0.1)
    assert inside is not None
    assert inside.household_copy == REALIZED_LEVEL_HOUSEHOLD_COPY
    assert inside.fix_class == "eq"


def test_a_malformed_banked_record_is_refused_loudly_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A findings failure must not cost a session the gate already decided
    may proceed (§3.4: findings are *optional* evidence artifacts). So the
    promoter returns ``None`` rather than raising — but never silently: the
    reason is named on its own event, because a diagnosis that vanished is
    indistinguishable from one that was never attempted.
    """

    caplog.set_level(logging.WARNING, logger="jasper.attribution.promotion")
    assert _level_frame_finding(f_hi_hz=None) is None
    assert "event=attribution.level_frame_promotion_refused" in caplog.text
    assert "no usable band" in caplog.text

    caplog.clear()
    # A non-finite evidence value is the other reachable shape: ``Finding``
    # refuses it, and the refusal is reported rather than swallowed.
    assert _level_frame_finding(disagreement_db=float("nan")) is None
    assert "event=attribution.level_frame_promotion_refused" in caplog.text

    # Not a mapping at all — the seam's own degenerate input.
    assert promote_level_frame_disagreement(
        None, session=_SESSION, cites=(_CITE,)
    ) is None


def test_m7_is_registered_with_no_detector_and_says_why() -> None:
    """M7 arrived ahead of WO-4's detectors, and the registry has to be
    honest about the two things that makes true.

    The ruling produced a second way to reach a mechanism: the shipped
    level-frame gate ALREADY computes the comparison M7 is about, so the
    finding needs no signature function. What it does NOT do is earn M7 a
    probe from §5's table — §4's own M7 probe ("per-driver passband comparison
    against a declared-sensitivity prior") is not a P1-P7 primitive, and
    inventing an id for it in the registry would be a plan change made
    silently in the wrong file.
    """

    spec = mechanism_spec(MECHANISM_LEVEL_FRAME)
    assert spec.title == "Inter-driver level-frame error"
    assert set(spec.fix_classes) == {"eq", "refit", "document_as_physics"}
    assert spec.corpus_evidence_tier == "adjudicated"
    # The tier is the plan's stated EXTENSION (a known intervention applied
    # and the feature responding), not a §5 probe, and the citation says so
    # rather than letting a reader assume a probe was run.
    assert "extension" in spec.corpus_citation
    assert set(spec.discriminating_probes) <= set(PROBES)
