# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""v2 measured-crossover apply extension — trims + optional delay/polarity.

Wave 4 of the crossover-measurement v2 redesign
(``docs/historical/crossover-measurement-productization-design.md`` §5.8). This is a
**new, standalone candidate model** — it does not extend or reuse
:class:`jasper.active_speaker.measured_candidate.MeasuredElectricalCandidate`,
the null-walk/evidence-store candidate built for the v1 flow (§5.9 of the
design doc retires that flow's near-field pass and null-walk delay source).
Building on top of machinery slated for deletion would be wasted work; this
module instead defines the small, self-contained shape Wave 5's new
check→measure→review/apply→verify flow will construct once Wave 1's
single-capture analysis exists.

**The apply mechanism reuses everything, invents nothing new:**

- Delay and polarity are written into the *preset's* ``CrossoverRegion``
  fields (``delay_ms``/``delay_target_driver``, ``upper_polarity``) — the
  same persisted, first-class fields a manual ``/sound/`` entry uses (see
  ``test_derive_corrections_manual_tier_sets_polarity_and_delay_from_region``
  in ``tests/test_active_speaker_baseline_profile.py``).
- :func:`driver_corrections` derives the compiler-ready
  ``{role: {gain_db, delay_ms, inverted}}`` mapping from that preset via
  ``camilla_yaml._role_polarity`` — the exact shared reduction
  ``jasper.active_speaker.baseline_profile._derive_corrections`` already
  uses (the legacy ``MeasuredElectricalCandidate.driver_corrections``
  inlines its own equivalent region walk), so this module adds no new
  polarity-to-inversion translation.
- :func:`compile_candidate_config` calls
  ``emit_active_speaker_baseline_config`` directly — the one Layer-A emitter,
  unchanged. Polarity rides the per-driver Gain filter (``inverted=...``), not
  the split mixer (``emit_active_speaker_baseline_config`` always emits the
  mixer as a no-op inverter — see ``_emit_split_mixer``'s docstring — so this
  is the *only* inversion mechanism a baseline graph has; there is no risk of
  double inversion).
- :func:`prove_candidate_config` re-proves the compiled text with the exact
  primitives named in the design doc: ``graph_safety.unprotected_tweeter_outputs``
  and a new one-shot
  ``jasper.audio_measurement.delay_graph.prove_static_delay_binding`` (added
  alongside this module) for "exactly one requested Delay filter, on the
  right channels, at the right value."

Absent alignment (``delay_us``/``delay_role``/``polarity`` all ``None``) is
byte-for-byte today's trims-only apply: :func:`effective_preset` returns the
source preset unchanged and :func:`driver_corrections` emits an all-zero delay
with each role's *existing* region polarity — exactly what
``MeasuredElectricalCandidate`` and ``_derive_corrections`` already produce for
a plain trims candidate.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, NoReturn, Sequence

from jasper.audio_measurement.evidence_identity import (
    EvidenceIdentityError,
    json_fingerprint,
)
from jasper.audio_measurement.delay_graph import quantized_delay_ms
from jasper.audio_measurement.null_walk import (
    MAX_DSP_DELAY_US,
    DspPredecessor,
    NullWalkError,
)

from .camilla_yaml import (
    _channels_for_role,
    _driver_delay_name,
    _role_polarity,
    emit_active_speaker_baseline_config,
)
from .crossover_alignment import POLARITY_INVERT, POLARITY_KEEP
from .crossover_v2.contracts import LINEARIZATION_OUTCOME_SINGLE_BRANCH
from .graph_safety import unprotected_tweeter_outputs, view_from_emitted_text
from .level_trim import MAX_ATTENUATION_DB
from .profile import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    CrossoverRegion,
    required_driver_roles,
)

SCHEMA_VERSION = 1
CANDIDATE_KIND = "jts_measured_crossover_candidate_v2"

_POLARITY_VALUES = frozenset({POLARITY_KEEP, POLARITY_INVERT})

# Gauge fix (2026-07-24): the exact set crossover_v2_flow.CrossoverV2Session
# stamps onto this field, from the ``_LinearizationState`` its candidate build
# returned (#2291 Phase 2b; it was a ``_last_*`` conductor field before that) —
# "" means "linearization was never evaluated this
# attempt" (a pre-#1668 candidate, or a MEASURE verdict rejected before
# ``_build_candidate`` ran). Validated here so a typo in the single writer
# fails loudly at construction instead of silently persisting garbage.
_LINEARIZATION_OUTCOME_VALUES = frozenset({
    "",
    "fitted",
    LINEARIZATION_OUTCOME_SINGLE_BRANCH,
    "trim_rejected",
    "ineligible_mic_tier",
    "ineligible_repeats",
    "fit_failed",
})


class MeasuredCrossoverCandidateError(ValueError):
    """A measured crossover candidate value is malformed or unsafe."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail if detail is not None else code


def _refuse(code: str, detail: str) -> NoReturn:
    raise MeasuredCrossoverCandidateError(code, detail)


def _region_for_role(preset: ActiveSpeakerPreset, role: str) -> CrossoverRegion:
    """The single crossover region owning ``role`` (fail-closed if ambiguous)."""

    matches = [
        region
        for region in preset.crossover_regions
        if role in (region.lower_driver, region.upper_driver)
    ]
    if len(matches) != 1:
        _refuse(
            "delay_role_ambiguous",
            f"driver role {role!r} must identify exactly one crossover region",
        )
    return matches[0]


@dataclass(frozen=True)
class MeasuredCrossoverAlignment:
    """Optional measured delay/polarity refinement for one crossover region.

    All three fields travel together or not at all: a candidate always names
    which driver is delayed and by how much (never a partial claim). Absent
    alignment (the default) is exactly today's trims-only apply behavior.

    Sign convention (design doc §5.6 item 5 / §5.8): ``delay_us`` is always a
    non-negative magnitude; ``delay_role`` names which driver branch receives
    the DSP ``Delay`` filter (positive ``delay_us`` with ``delay_role`` set to
    the tweeter means the tweeter arrived earlier and gets delayed to match
    the woofer). ``polarity`` always describes the identified region's
    *upper* driver relative to its lower (reference) driver — ``"keep"``
    leaves the region's persisted polarity as-is, ``"invert"`` flips it —
    mirroring the existing near-field alignment proposal's convention
    (``crossover_alignment.propose_crossover_alignment`` and
    ``baseline_profile._derive_corrections``'s automatic-tier polarity flip
    both only ever act on the upper role).
    """

    delay_us: float | None = None
    delay_role: str | None = None
    polarity: str | None = None

    def __post_init__(self) -> None:
        present = (
            self.delay_us is not None,
            self.delay_role is not None,
            self.polarity is not None,
        )
        if any(present) and not all(present):
            _refuse(
                "alignment_partial",
                "delay_us, delay_role, and polarity must be supplied together "
                "or not at all",
            )
        if self.delay_us is None:
            return
        if (
            isinstance(self.delay_us, bool)
            or not isinstance(self.delay_us, (int, float))
            or not math.isfinite(float(self.delay_us))
        ):
            _refuse("delay_us_invalid", "delay_us must be a finite number")
        delay_us = float(self.delay_us)
        if not 0.0 <= delay_us <= MAX_DSP_DELAY_US:
            _refuse(
                "delay_us_out_of_range",
                f"delay_us must be between 0 and {MAX_DSP_DELAY_US:.0f}",
            )
        object.__setattr__(self, "delay_us", delay_us)
        if not isinstance(self.delay_role, str) or not self.delay_role.strip():
            _refuse("delay_role_invalid", "delay_role must be a non-empty string")
        if self.polarity not in _POLARITY_VALUES:
            _refuse(
                "polarity_invalid",
                f"polarity must be one of {sorted(_POLARITY_VALUES)}",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "delay_us": self.delay_us,
            "delay_role": self.delay_role,
            "polarity": self.polarity,
        }


_NO_ALIGNMENT = MeasuredCrossoverAlignment()


@dataclass(frozen=True)
class MeasuredCrossoverCandidate:
    """A v2 measured-crossover proposal: required trims + optional alignment.

    ``program_id`` and ``analysis`` are opaque identity/evidence carried from
    Wave 1's excitation program and capture analysis (design doc §5.3/§5.6);
    this module does not interpret them, only fingerprints them alongside the
    proposal so a stale reviewed candidate can never silently apply with
    different semantics (mirrors #1423/#1441's apply-freshness hardening —
    the fingerprint flows into ``baseline_profile``'s existing
    ``expected_candidate_fingerprint`` staleness gate via
    ``build_baseline_profile_candidate``'s ``measured_candidate`` seam).

    ``linearization`` (#1668 PR-C) is the per-role driver-linearization the
    emitted graph carries, keyed by driver role, and its entries come in TWO
    shapes since a per-driver prescription can reach a round (PR-B):

    * a FITTED role — the Layer-1a artifact, a compact dict (see
      ``jasper.active_speaker.linearization_fit.LinearizationFit.to_dict``:
      filters, fit_band_hz, target_level_db, residuals, an octave-band reason
      summary, mic_tier, driver_class, n_repeats);
    * a PRESCRIBED role — ``filters`` plus ``prescribed_by`` (model, operator,
      packet fingerprint) plus ``mic_tier`` plus ``headroom_cost_db``, and
      deliberately none of the fit-quality fields, because a prescription
      measured nothing and emitting those zeroed would bank a claim nothing
      made. The last two are the exceptions and neither is a fit-quality claim:
      ``mic_tier`` names the MICROPHONE rather than the correction and is
      carried forward from the entry it replaces, so
      ``_mic_trust_ceiling_hz`` can decide where the delta probe may grade at
      all; ``headroom_cost_db`` is what the EMITTED chain costs the household in
      maximum level, so a prescribed boost discloses its own spend rather than
      0.0 (#2759). The owner of the first split is ``crossover_v2.
      driver_prescription.driver_prescription_to_candidate_fields``; the charge
      is stamped by ``crossover_v2.planning.build_candidate``, where the
      crossover sections and the committed trim are in scope.

    Every reader here must therefore treat a fit-quality key as OPTIONAL rather
    than as a shape guarantee — which is what
    ``linearization_filters_by_role`` (reads only ``filters``),
    ``worst_headroom_cost_db`` (absent key is an honest 0.0 for an era-older
    entry that carries none) and the emitter's own ``_validated_linearization``
    already do. Like ``analysis``, the map is frozen through the SAME
    exact-JSON-data walk. A
    NON-empty value participates in the fingerprint — tampering with a
    persisted linearization result trips the same ``candidate_tampered``
    refusal as tampering with anything else in this candidate. The empty
    dict (the default, and what every non-reference-tier or under-repeated
    MEASURE produces — see the v2 session's linearization gate) is
    deliberately OMITTED from the fingerprinted core instead of
    participating as ``{}`` — see ``_core()``'s own docstring — so it is a
    fully valid, era-tolerant shape: "no linearization was fit," identical
    to every candidate this module produced before this field existed,
    fingerprint included. ``from_mapping`` accepts the key's outright
    absence the same way (see its own docstring). This module does NOT
    persist the underlying ``EnvelopeCurve`` — only the compact fit result.

    ``linearization_outcome`` (gauge fix, 2026-07-24) is the WHY behind the
    FITTED half of ``linearization`` above: one of "fitted" / "trim_rejected" /
    "ineligible_mic_tier" / "ineligible_repeats" / "fit_failed", or "" when
    linearization was never evaluated this attempt. This is the single
    writer's own verdict (``crossover_v2.candidates.LinearizationState.outcome``,
    stamped verbatim at candidate-build time) — this module never re-derives
    it. **It says nothing about a prescribed role, and cannot**: the fit engine
    is its one writer and never saw the document. So a candidate may
    legitimately read ``fit_failed`` while carrying prescribed filters, and the
    thing that tells a reader which is which is the ENTRY's own
    ``prescribed_by`` rather than this field. Era-tolerant exactly like ``linearization``: omitted from the
    fingerprint when empty, and accepted absent on ``from_mapping`` (every
    candidate persisted before this field existed implicitly claimed "").

    ``exclusion_evidence`` (flat-linearization plan PR-6b) is the **exclusion
    reason of record** for the fit above: when the Layer-1a fit consumed a
    spatial cloud's honesty verdict, this carries exactly what it consumed —
    the excluded frequency intervals handed to ``spatial_exclusion_limit``, the
    cross-position ``band_spread`` and ``n_positions`` behind
    ``position_stability_limit``, and the identified-null registry with its
    τ/r/classification per null. A reader with this record can answer "why was
    this band not corrected" and re-derive the envelope terms without the
    session's captures. It deliberately DUPLICATES data also written to the
    session's ``cloud_measure.json`` artifact: that file is session evidence
    and can be pruned by bundle retention, while this travels with the
    correction it justifies and is re-read at every apply. Same optional-field
    conventions as ``linearization`` — frozen through the exact-JSON-data walk,
    omitted from the fingerprint when empty, accepted absent on
    ``from_mapping``. Empty means "no cloud evidence entered this fit", which
    is what every candidate produced before PR-6b implicitly claimed.

    **What it costs, measured rather than hand-waved** (2026-07-27, the S0
    ten-position cloud — this program's reference corpus, and the widest real
    case it has): **5,294 bytes** of ``candidate.json``, of which the null
    registry is 3,307 (3 identified nulls plus 5 recorded refusals, each
    carrying its own evidence mapping), ``band_spread`` 1,596 (10 octave
    bands x 6 numbers), and the merged intervals 287 (7 intervals). It scales
    with what the honesty instruments actually found, not with capture length,
    so a clean room writes a fraction of that and nothing writes an unbounded
    amount. Stated here for the same reason the `/state` projection states its
    own: this is the largest thing PR-6b adds to a persisted artifact, and a
    reader deciding whether to keep it should see the number.

    ``blend_correction`` (design doc decision 10) is the crossover blend
    region's bounded, cuts-first shape correction — the flat list
    ``[{biquad_type, freq, q, gain}, ...]``
    ``crossover_v2.blend_correction.solve_blend_correction`` designed from the
    PREVIOUS round's summed evidence, emitted pre-split on the stereo bus.
    Flat rather than per-role because it describes the SUM, not a driver: see
    ``camilla_yaml._emit_baseline_pipeline`` for why that placement is what
    makes it common-mode. Same optional-field conventions as ``linearization``
    — frozen through the exact-JSON-data walk, omitted from the fingerprint
    when empty, accepted absent on ``from_mapping``. Empty means "this round
    applied no blend correction", which is what every candidate before decision
    10 implicitly claimed, and what the first round of any series claims
    honestly (there is no previous VERIFY to derive one from).

    It is also the round's own INCUMBENT record: the next round reads this
    field off the candidate that was actually applied to know what its summed
    measurement was taken through. That is why it is persisted on the candidate
    rather than only banked on the receipt — a restored graph or a hand-applied
    config must be able to report "no readable incumbent" rather than have one
    assumed for it (#2653's refuse-when-unreconcilable, applied here).
    """

    program_id: str
    analysis: Mapping[str, Any]
    source_preset: ActiveSpeakerPreset
    role_attenuations_db: Mapping[str, float]
    alignment: MeasuredCrossoverAlignment = _NO_ALIGNMENT
    linearization: Mapping[str, Any] = field(default_factory=dict)
    linearization_outcome: str = ""
    exclusion_evidence: Mapping[str, Any] = field(default_factory=dict)
    blend_correction: Sequence[Mapping[str, Any]] = ()
    fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.program_id, str) or not self.program_id.strip():
            _refuse("program_id_invalid", "program_id must be a non-empty string")
        if not isinstance(self.analysis, Mapping) or not self.analysis:
            _refuse("analysis_invalid", "analysis must be a non-empty mapping")
        if not isinstance(self.source_preset, ActiveSpeakerPreset):
            _refuse("source_preset_invalid", "source_preset must be ActiveSpeakerPreset")
        try:
            self.source_preset.validate()
        except ActiveSpeakerConfigError as exc:
            _refuse("source_preset_invalid", str(exc))
        if not isinstance(self.alignment, MeasuredCrossoverAlignment):
            _refuse(
                "alignment_invalid", "alignment must be MeasuredCrossoverAlignment"
            )
        roles = required_driver_roles(self.source_preset.way_count)
        if not isinstance(self.role_attenuations_db, Mapping) or set(
            self.role_attenuations_db
        ) != set(roles):
            _refuse(
                "role_attenuations_incomplete",
                "role_attenuations_db must cover exactly the preset's driver roles",
            )
        normalized_trims: dict[str, float] = {}
        for role in roles:
            value = self.role_attenuations_db[role]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) > 0.0
                or float(value) < MAX_ATTENUATION_DB
            ):
                _refuse(
                    "attenuation_out_of_range",
                    f"attenuation for {role!r} must be between "
                    f"{MAX_ATTENUATION_DB} and 0 dB",
                )
            normalized_trims[role] = float(value)
        object.__setattr__(self, "role_attenuations_db", normalized_trims)
        if self.alignment.delay_role is not None:
            if self.alignment.delay_role not in roles:
                _refuse(
                    "delay_role_unknown",
                    "delay_role must be one of the preset's declared driver roles",
                )
            # Fail closed now (construction time) if the role does not
            # identify exactly one crossover region, rather than deferring the
            # refusal to first apply.
            _region_for_role(self.source_preset, self.alignment.delay_role)
        try:
            frozen_analysis = DspPredecessor({"analysis": self.analysis}).state[
                "analysis"
            ]
        except NullWalkError as exc:
            _refuse("analysis_invalid", f"analysis must be exact JSON data: {exc}")
        object.__setattr__(self, "analysis", frozen_analysis)
        if not isinstance(self.linearization, Mapping):
            _refuse("linearization_invalid", "linearization must be a mapping")
        try:
            frozen_linearization = DspPredecessor(
                {"linearization": dict(self.linearization)}
            ).state["linearization"]
        except NullWalkError as exc:
            _refuse(
                "linearization_invalid", f"linearization must be exact JSON data: {exc}"
            )
        object.__setattr__(self, "linearization", frozen_linearization)
        if not isinstance(self.exclusion_evidence, Mapping):
            _refuse(
                "exclusion_evidence_invalid", "exclusion_evidence must be a mapping"
            )
        try:
            frozen_exclusion = DspPredecessor(
                {"exclusion_evidence": dict(self.exclusion_evidence)}
            ).state["exclusion_evidence"]
        except NullWalkError as exc:
            _refuse(
                "exclusion_evidence_invalid",
                f"exclusion_evidence must be exact JSON data: {exc}",
            )
        object.__setattr__(self, "exclusion_evidence", frozen_exclusion)
        # Decision 10's blend correction. A list, not a mapping, so the shape
        # check differs from its neighbours above; the exact-JSON-data walk and
        # the freeze are the same. Cuts-only is NOT re-checked here — the
        # emitter refuses a positive gain at the graph boundary
        # (``camilla_yaml._validated_blend_correction``), which is the boundary
        # that matters, and a second policy copy here would be a second thing
        # to keep in step with the solver.
        if (
            not isinstance(self.blend_correction, Sequence)
            or isinstance(self.blend_correction, (str, bytes, Mapping))
        ):
            _refuse("blend_correction_invalid", "blend_correction must be a list")
        try:
            frozen_blend = DspPredecessor(
                {"blend_correction": [dict(entry) for entry in self.blend_correction]}
            ).state["blend_correction"]
        except (NullWalkError, AttributeError, TypeError, ValueError) as exc:
            _refuse(
                "blend_correction_invalid",
                f"blend_correction must be exact JSON data: {exc}",
            )
        object.__setattr__(self, "blend_correction", frozen_blend)
        if self.linearization_outcome not in _LINEARIZATION_OUTCOME_VALUES:
            _refuse(
                "linearization_outcome_invalid",
                "linearization_outcome must be one of "
                f"{sorted(_LINEARIZATION_OUTCOME_VALUES)}",
            )
        try:
            fingerprint = json_fingerprint(self._core())
        except EvidenceIdentityError as exc:
            _refuse("candidate_invalid", str(exc))
        object.__setattr__(self, "fingerprint", fingerprint)

    def _core(self) -> dict[str, Any]:
        """The exact fingerprinted payload (see ``__post_init__``).

        ``linearization`` is deliberately OMITTED here when empty (the
        default, and what every non-eligible MEASURE produces) rather than
        included as ``{}`` — an empty-linearization candidate's ``_core()``
        is then byte-for-byte the shape this module produced before
        ``linearization`` existed (#1668 PR-C), so its fingerprint matches
        what pre-PR-C code already computed and persisted for the identical
        other fields. A NON-empty linearization stays in ``_core()`` and is
        therefore tamper-protected exactly like every other field —
        stripping or mutating real fit data changes the recomputed
        fingerprint, tripping ``from_mapping``'s ``candidate_tampered``
        refusal. ``to_dict()`` does NOT mirror this omission (see its own
        docstring) — the two intentionally disagree.

        ``linearization_outcome`` (gauge fix, 2026-07-24),
        ``exclusion_evidence`` (plan PR-6b) and ``blend_correction`` (decision
        10) follow the exact same omit-when-empty convention, for the same
        era-tolerance reason: an empty value means "not evaluated" / "no cloud
        evidence entered this fit" / "this round applied no blend correction,"
        identical to every candidate produced before those fields existed. A
        NON-empty ``exclusion_evidence`` is fingerprinted like everything else,
        so the recorded reason for refusing to correct a band cannot be edited
        out of a persisted candidate without tripping ``candidate_tampered``.
        The same protection is what makes ``blend_correction`` usable as the
        next round's incumbent: the filters a capture rode cannot be edited
        after the fact without the candidate refusing to reopen.
        """
        core: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "kind": CANDIDATE_KIND,
            "program_id": self.program_id,
            "analysis": self.analysis,
            "source_preset": self.source_preset.to_dict(),
            "role_attenuations_db": dict(self.role_attenuations_db),
            "alignment": self.alignment.to_dict(),
        }
        if self.linearization:
            core["linearization"] = dict(self.linearization)
        if self.linearization_outcome:
            core["linearization_outcome"] = self.linearization_outcome
        if self.exclusion_evidence:
            core["exclusion_evidence"] = dict(self.exclusion_evidence)
        if self.blend_correction:
            core["blend_correction"] = [dict(f) for f in self.blend_correction]
        return core

    def to_dict(self) -> dict[str, Any]:
        """The full persisted shape — ALWAYS carries ``linearization``,
        ``linearization_outcome`` and ``exclusion_evidence``.

        Unlike ``_core()`` (the fingerprint input), this never omits either
        key, even when empty — every freshly-serialized candidate has the
        current, full field set, so a fresh write and a freshly-built
        ``raw`` dict always agree byte-for-byte (what ``from_mapping``'s
        tamper check relies on). Only an ALREADY-PERSISTED, pre-PR-C /
        pre-gauge-fix payload is missing either key — that era-tolerance
        lives in ``from_mapping`` on the READ side, not here on the write
        side.
        """
        return {
            **self._core(),
            "linearization": dict(self.linearization),
            "linearization_outcome": self.linearization_outcome,
            "exclusion_evidence": dict(self.exclusion_evidence),
            "blend_correction": [dict(f) for f in self.blend_correction],
            "fingerprint": self.fingerprint,
        }

    def driver_corrections(self) -> dict[str, dict[str, float | bool]]:
        """The compiler-ready ``{role: {gain_db, delay_ms, inverted}}`` mapping.

        Same shape ``MeasuredElectricalCandidate.driver_corrections`` and
        ``baseline_profile._derive_corrections`` already produce, so
        ``emit_active_speaker_baseline_config`` (and anything downstream that
        consumes a ``corrections`` mapping) needs no new code path.
        """

        return driver_corrections(self)

    @classmethod
    def from_mapping(cls, raw: Any) -> "MeasuredCrossoverCandidate":
        """Strictly reopen one persisted candidate without re-deriving evidence.

        ``linearization`` (#1668 PR-C), ``linearization_outcome`` (gauge
        fix, 2026-07-24) and ``exclusion_evidence`` (plan PR-6b) are the
        OPTIONAL fields: every candidate
        persisted before those changes lacks the keys entirely, and
        ``jasper.web.correction_crossover_v2._reopen_candidate_artifact``
        can hand this method exactly that older ``candidate.json`` shape
        across a deploy straddle — a candidate published moments before an
        install, reopened moments after it by code that now expects the
        newer shape. Absent ``linearization`` means the same thing an
        explicit ``{}`` means: "no linearization was fit." Absent
        ``linearization_outcome`` means the same thing an explicit ``""``
        means: "not evaluated." Absent ``exclusion_evidence`` means the same
        thing an explicit ``{}`` means: "no cloud evidence entered this fit."
        Absent ``blend_correction`` (decision 10) means the same thing an
        explicit ``[]`` means: "this round applied no blend correction."
        Every other field stays strictly required,
        matching every prior era of this schema.
        """

        required = {
            "schema_version",
            "kind",
            "program_id",
            "analysis",
            "source_preset",
            "role_attenuations_db",
            "alignment",
            "fingerprint",
        }
        optional = {
            "linearization", "linearization_outcome", "exclusion_evidence",
            "blend_correction",
        }
        if not isinstance(raw, Mapping) or set(raw) - optional != required:
            _refuse(
                "candidate_malformed",
                "measured crossover candidate has unknown or missing fields",
            )
        if (
            raw.get("schema_version") != SCHEMA_VERSION
            or raw.get("kind") != CANDIDATE_KIND
        ):
            _refuse(
                "candidate_schema_unsupported",
                "measured crossover candidate schema/kind is unsupported",
            )
        alignment_raw = raw["alignment"]
        if not isinstance(alignment_raw, Mapping) or set(alignment_raw) != {
            "delay_us",
            "delay_role",
            "polarity",
        }:
            _refuse("alignment_malformed", "candidate alignment is malformed")
        attenuations_raw = raw["role_attenuations_db"]
        if not isinstance(attenuations_raw, Mapping):
            _refuse(
                "role_attenuations_malformed", "candidate attenuations are malformed"
            )
        # Absent -> {} (era tolerance, see the docstring above); present ->
        # validated exactly like before.
        linearization_raw = raw.get("linearization", {})
        if not isinstance(linearization_raw, Mapping):
            _refuse(
                "linearization_malformed", "candidate linearization is malformed"
            )
        # Absent -> "" (era tolerance, see the docstring above); present ->
        # validated by __post_init__ against _LINEARIZATION_OUTCOME_VALUES.
        linearization_outcome_raw = raw.get("linearization_outcome", "")
        if not isinstance(linearization_outcome_raw, str):
            _refuse(
                "linearization_outcome_malformed",
                "candidate linearization_outcome is malformed",
            )
        # Absent -> {} (era tolerance, see the docstring above); present ->
        # validated by __post_init__'s exact-JSON walk.
        exclusion_evidence_raw = raw.get("exclusion_evidence", {})
        if not isinstance(exclusion_evidence_raw, Mapping):
            _refuse(
                "exclusion_evidence_malformed",
                "candidate exclusion_evidence is malformed",
            )
        # Absent -> [] (era tolerance, see the docstring above); present ->
        # validated by __post_init__'s exact-JSON walk, and re-validated for
        # cuts-only at the emitter boundary.
        blend_correction_raw = raw.get("blend_correction", [])
        if (
            not isinstance(blend_correction_raw, Sequence)
            or isinstance(blend_correction_raw, (str, bytes, Mapping))
        ):
            _refuse(
                "blend_correction_malformed",
                "candidate blend_correction is malformed",
            )
        try:
            candidate = cls(
                program_id=str(raw["program_id"]),
                analysis=raw["analysis"],
                source_preset=ActiveSpeakerPreset.from_mapping(raw["source_preset"]),
                role_attenuations_db=dict(attenuations_raw),
                alignment=MeasuredCrossoverAlignment(
                    delay_us=alignment_raw["delay_us"],
                    delay_role=alignment_raw["delay_role"],
                    polarity=alignment_raw["polarity"],
                ),
                linearization=dict(linearization_raw),
                linearization_outcome=linearization_outcome_raw,
                exclusion_evidence=dict(exclusion_evidence_raw),
                blend_correction=list(blend_correction_raw),
            )
        except (TypeError, ActiveSpeakerConfigError) as exc:
            raise MeasuredCrossoverCandidateError(
                "candidate_malformed", str(exc)
            ) from exc
        # candidate.to_dict() always carries "linearization",
        # "linearization_outcome" and "exclusion_evidence" (forward-shape
        # consistency — see its own docstring); an older `raw` implicitly
        # claimed {} / "" / {} by never mentioning the field, so compare
        # against that same claim made explicit — otherwise a payload that
        # predates the field would spuriously fail its own honest round trip
        # and refuse as tampered.
        #
        # **Every optional field in `to_dict()` needs a line here.** Adding one
        # without it makes EVERY previously-persisted candidate refuse as
        # `candidate_tampered` the moment a deploy straddles the change — the
        # live `handle_v2_apply` → `_reopen_candidate_artifact` route tells a
        # household their correction was tampered with when the file is merely
        # older. That is not hypothetical: `exclusion_evidence` shipped without
        # its line and this is the fix. Each is pinned by its own era test.
        raw_for_comparison = dict(raw)
        raw_for_comparison.setdefault("linearization", {})
        raw_for_comparison.setdefault("linearization_outcome", "")
        raw_for_comparison.setdefault("exclusion_evidence", {})
        raw_for_comparison.setdefault("blend_correction", [])
        if candidate.to_dict() != raw_for_comparison:
            _refuse(
                "candidate_tampered",
                "persisted measured crossover candidate does not match its "
                "declared result",
            )
        return candidate


def effective_preset(candidate: MeasuredCrossoverCandidate) -> ActiveSpeakerPreset:
    """The preset with the candidate's alignment written into its region fields.

    Absent alignment returns ``candidate.source_preset`` unchanged (exactly
    today's trims-only behavior). Present alignment writes
    ``delay_ms``/``delay_target_driver`` onto the region identified by
    ``delay_role``, and — only when ``polarity == "invert"`` — flips that
    region's ``upper_polarity``. ``"keep"`` leaves the region's persisted
    polarity untouched, whatever it already was.
    """

    alignment = candidate.alignment
    if alignment.delay_role is None:
        return candidate.source_preset
    assert alignment.delay_us is not None  # __post_init__ enforces all-or-nothing
    region = _region_for_role(candidate.source_preset, alignment.delay_role)
    upper_polarity = region.upper_polarity
    if alignment.polarity == POLARITY_INVERT:
        upper_polarity = (
            "non-inverted" if region.upper_polarity == "inverted" else "inverted"
        )
    updated_region = dataclasses.replace(
        region,
        delay_target_driver=alignment.delay_role,
        # quantized_delay_ms is the ONE µs→ms quantizer, shared with
        # prove_static_delay_binding's expected value — a second recipe here
        # (e.g. round(µs/1000, 6)) disagrees with the proof on ~0.4% of the
        # valid range and turns into a spurious fail-closed apply refusal.
        delay_ms=quantized_delay_ms(alignment.delay_us),
        upper_polarity=upper_polarity,
    )
    updated_regions = tuple(
        updated_region if existing.id == region.id else existing
        for existing in candidate.source_preset.crossover_regions
    )
    updated = dataclasses.replace(
        candidate.source_preset, crossover_regions=updated_regions
    )
    try:
        updated.validate()
    except ActiveSpeakerConfigError as exc:
        _refuse("effective_preset_invalid", str(exc))
    return updated


def driver_corrections(
    candidate: MeasuredCrossoverCandidate,
) -> dict[str, dict[str, float | bool]]:
    """The exact compiler-ready refinement this candidate proposes.

    Reuses ``camilla_yaml._role_polarity`` — the same region-polarity
    reduction ``baseline_profile._derive_corrections`` uses (the legacy
    ``MeasuredElectricalCandidate.driver_corrections`` inlines its own
    equivalent region walk) — so this module adds no new
    polarity-to-inversion translation.
    """

    preset = effective_preset(candidate)
    polarity = _role_polarity(preset)
    roles = required_driver_roles(preset.way_count)
    delay_role = candidate.alignment.delay_role
    delay_ms = 0.0
    if delay_role is not None:
        assert candidate.alignment.delay_us is not None  # all-or-nothing invariant
        # Same single quantizer as effective_preset and the delay_graph proof.
        delay_ms = quantized_delay_ms(candidate.alignment.delay_us)
    return {
        role: {
            "gain_db": candidate.role_attenuations_db[role],
            "delay_ms": delay_ms if role == delay_role else 0.0,
            "inverted": polarity[role],
        }
        for role in roles
    }


def compile_candidate_config(
    candidate: MeasuredCrossoverCandidate,
    *,
    playback_device: str,
    **emit_kwargs: Any,
) -> str:
    """Compile the candidate's baseline YAML — the one Layer-A emission path.

    Delegates entirely to ``emit_active_speaker_baseline_config``; this
    function only supplies the preset (with alignment folded in), the
    derived ``corrections`` mapping, and the derived ``linearization`` filter
    list (#1668 PR-D). ``emit_kwargs`` forwards any other emitter keyword
    (``capture_device``, ``out_path``, ...) unchanged.

    CONVENTION (shared with ``baseline_profile.build_baseline_profile_candidate``,
    the production emit site): the emitter derives delay and inversion from
    ``corrections`` ONLY — it does not read a region's ``delay_ms`` /
    ``delay_target_driver`` / polarity fields today (the baseline mixer is
    emitted with ``apply_region_polarity=False``; the per-driver Gain is the
    sole inverter). That is why the production path can hand the emitter the
    preview-compiled *source* preset while this helper hands it
    ``effective_preset`` — same corrections, byte-identical graph. If a
    future emitter change starts reading region delay/polarity fields
    directly, both call sites must be revisited together or they diverge.

    ``candidate.linearization`` (empty for a plain trims candidate or a
    pre-PR-C persisted one) is reduced to the emitter's own filter-list shape
    by the shared helper,
    ``jasper.active_speaker.linearization_fit.linearization_filters_by_role``
    — the same reduction ``baseline_profile.build_baseline_profile_candidate``
    uses, so the two RICH-candidate call sites never drift. NOT shared with
    ``baseline_profile.recompose_applied_baseline_yaml``: that seam reads an
    already-reduced snapshot and deliberately re-validates it inline instead
    (see ``linearization_filters_by_role``'s own docstring for why calling
    it on an already-reduced mapping is a trap, not a valid consolidation).
    """

    from .linearization_fit import linearization_filters_by_role

    preset = effective_preset(candidate)
    corrections = driver_corrections(candidate)
    linearization = linearization_filters_by_role(candidate.linearization)
    return emit_active_speaker_baseline_config(
        preset,
        playback_device=playback_device,
        corrections=corrections,
        linearization=linearization,
        blend_correction=list(candidate.blend_correction),
        **emit_kwargs,
    )


def prove_candidate_config(candidate: MeasuredCrossoverCandidate, yaml_text: str) -> None:
    """Re-prove a compiled candidate graph before it is ever applied.

    Fail-closed, no I/O: raises :class:`MeasuredCrossoverCandidateError` on
    the first failing proof.

    1. **graph_safety** — every tweeter/compression-driver output keeps its
       protective high-pass (``unprotected_tweeter_outputs``). The emitter
       already asserts this internally
       (``camilla_yaml._assert_tweeter_outputs_protected``); this is a second,
       independent check at the candidate boundary, matching the task's
       "graph_safety protection proofs" step explicitly.
    2. **delay_graph** — when the candidate carries alignment, the compiled
       graph binds *exactly one* ``Delay`` filter for ``delay_role``, on that
       role's exact output channels, at the exact requested ``delay_us``
       (``jasper.audio_measurement.delay_graph.prove_static_delay_binding``).
    """

    import yaml as _yaml

    from jasper.audio_measurement.delay_graph import (
        DelayGraphProofError,
        prove_static_delay_binding,
    )

    preset = effective_preset(candidate)
    view = view_from_emitted_text(yaml_text)
    tweeter_channels = {
        output.index
        for output in preset.channel_map.outputs
        if output.driver_role == "tweeter"
    }
    unprotected = unprotected_tweeter_outputs(view, tweeter_channels=tweeter_channels)
    if unprotected:
        _refuse(
            "tweeter_unprotected",
            "compiled candidate graph left tweeter output(s) unprotected: "
            + ", ".join(str(index) for index in unprotected),
        )

    delay_role = candidate.alignment.delay_role
    if delay_role is None:
        return
    assert candidate.alignment.delay_us is not None
    try:
        parsed = _yaml.safe_load(yaml_text)
    except _yaml.YAMLError as exc:
        _refuse("candidate_config_unparseable", str(exc))
    channels = tuple(_channels_for_role(preset, delay_role))
    try:
        prove_static_delay_binding(
            parsed,
            delay_filter_name=_driver_delay_name(delay_role),
            channels=channels,
            delay_us=candidate.alignment.delay_us,
        )
    except DelayGraphProofError as exc:
        _refuse("delay_graph_proof_failed", f"{exc.code}: {exc}")


def build_and_prove_candidate_config(
    candidate: MeasuredCrossoverCandidate,
    *,
    playback_device: str,
    **emit_kwargs: Any,
) -> str:
    """Compile the candidate, prove it, and return the proven YAML text.

    Fails closed: a failed proof raises before returning anything, exactly
    like the existing safety refusals elsewhere in the active-speaker apply
    path — the caller never receives a graph this function could not prove.
    """

    yaml_text = compile_candidate_config(
        candidate, playback_device=playback_device, **emit_kwargs
    )
    prove_candidate_config(candidate, yaml_text)
    return yaml_text
