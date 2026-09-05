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

# The exact set crossover_v2_flow.CrossoverV2Session stamps onto this field;
# "" means linearization was never evaluated this attempt. Validated here so a
# typo in the single writer fails at construction rather than persisting.
_LINEARIZATION_OUTCOME_VALUES = frozenset({
    "",
    "fitted",
    LINEARIZATION_OUTCOME_SINGLE_BRANCH,
    "trim_rejected",
    "ineligible_mic_tier",
    "ineligible_repeats",
    "fit_failed",
})


#: The candidate keys ``from_mapping`` accepts as absent, each mapped to the
#: type whose empty value that absence claims. ONE list: the unknown-field
#: check, the reopen comparison's ``setdefault`` (without which a candidate
#: written before a field refuses as ``candidate_tampered``) and the test that
#: walks them all read it.
_OPTIONAL_FIELD_TYPES: Mapping[str, type] = {
    "linearization": dict,
    "linearization_outcome": str,
    "trim_decision": dict,
    "exclusion_evidence": dict,
    "blend_correction": list,
}


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

    All three fields travel together or not at all — never a partial claim.

    Sign convention: ``delay_us`` is a non-negative magnitude and ``delay_role``
    names the branch that receives the DSP ``Delay`` filter. ``polarity``
    always describes the region's *upper* driver relative to its lower
    (reference) driver: ``"keep"`` leaves the persisted polarity, ``"invert"``
    flips it.
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

    ``program_id`` and ``analysis`` are opaque identity/evidence; this module
    only fingerprints them alongside the proposal, so a stale reviewed candidate
    cannot silently apply with different semantics (the fingerprint feeds
    ``baseline_profile``'s ``expected_candidate_fingerprint`` staleness gate).

    ``linearization`` entries come in two shapes: a FITTED role
    (``linearization_fit.LinearizationFit.to_dict``) and a PRESCRIBED role
    (``filters``, ``prescribed_by``, ``mic_tier``, ``headroom_cost_db`` and
    deliberately no fit-quality fields, since a prescription measured nothing),
    so every reader must treat a fit-quality key as OPTIONAL rather than a shape
    guarantee. Only the compact fit result is persisted, never the underlying
    ``EnvelopeCurve``. ``linearization_outcome`` is the WHY behind the FITTED
    half only, stamped verbatim by
    ``crossover_v2.candidates.LinearizationState.outcome``: a candidate may read
    ``fit_failed`` while carrying prescribed filters, and the entry's own
    ``prescribed_by`` is what distinguishes them.

    ``trim_decision`` is WHICH trim pair ``role_attenuations_db`` came from,
    never those dB: ``{"strategy", "committed_side", "anchor_drift_db"}``. It
    exists because ``linearization_outcome`` cannot tell an anchored commit
    from a resolved one. Empty where no pair was committed, and where a trim
    pin displaced the one that was.

    ``exclusion_evidence`` is the exclusion reason of record for that fit. It
    deliberately duplicates the session's ``cloud_measure.json``, which bundle
    retention may prune, so the reason travels with the correction it justifies
    (widest measured case, a ten-position cloud: ~5.3 kB).

    ``blend_correction`` is a flat ``[{biquad_type, freq, q, gain}, ...]`` list
    emitted pre-split on the stereo bus because it describes the SUM, not a
    driver. It is also the round's INCUMBENT record: the next round reads it off
    the applied candidate to know what its summed measurement rode through.

    Every optional field above is frozen through the same exact-JSON-data walk,
    participates in the fingerprint when non-empty, and is omitted from the
    fingerprinted core when empty so a candidate from before the field existed
    keeps its fingerprint. ``from_mapping`` accepts the key's outright absence
    the same way.
    """

    program_id: str
    analysis: Mapping[str, Any]
    source_preset: ActiveSpeakerPreset
    role_attenuations_db: Mapping[str, float]
    alignment: MeasuredCrossoverAlignment = _NO_ALIGNMENT
    linearization: Mapping[str, Any] = field(default_factory=dict)
    linearization_outcome: str = ""
    trim_decision: Mapping[str, Any] = field(default_factory=dict)
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
            # Fail closed at construction, not at first apply, when the role
            # does not identify exactly one crossover region.
            _region_for_role(self.source_preset, self.alignment.delay_role)
        try:
            frozen_analysis = DspPredecessor({"analysis": self.analysis}).state[
                "analysis"
            ]
        except NullWalkError as exc:
            _refuse("analysis_invalid", f"analysis must be exact JSON data: {exc}")
        object.__setattr__(self, "analysis", frozen_analysis)
        # The mapping-shaped optional fields: one exact-JSON-data walk each,
        # refusing as ``<name>_invalid``.
        for name in (k for k, kind in _OPTIONAL_FIELD_TYPES.items() if kind is dict):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                _refuse(f"{name}_invalid", f"{name} must be a mapping")
            try:
                frozen = DspPredecessor({name: dict(value)}).state[name]
            except NullWalkError as exc:
                _refuse(f"{name}_invalid", f"{name} must be exact JSON data: {exc}")
            object.__setattr__(self, name, frozen)
        # A list, not a mapping, so the shape check differs from its neighbours
        # above; the exact-JSON-data walk and the freeze are the same.
        # Cuts-only is enforced at the emitter boundary
        # (``camilla_yaml._validated_blend_correction``), not re-checked here.
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

        Every optional field is OMITTED when empty rather than included as
        ``{}`` / ``""`` / ``[]``, so an empty candidate's ``_core()`` keeps the
        fingerprint code from before the field existed. Non-empty values stay
        in, and are therefore tamper-protected like every other field.
        ``to_dict()`` deliberately does NOT mirror this omission.
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
        if self.trim_decision:
            core["trim_decision"] = dict(self.trim_decision)
        if self.exclusion_evidence:
            core["exclusion_evidence"] = dict(self.exclusion_evidence)
        if self.blend_correction:
            core["blend_correction"] = [dict(f) for f in self.blend_correction]
        return core

    def to_dict(self) -> dict[str, Any]:
        """The full persisted shape — ALWAYS carries every optional key.

        Unlike ``_core()`` (the fingerprint input), this never omits a key even
        when empty, so a fresh write and a freshly-built ``raw`` dict agree
        byte-for-byte, which ``from_mapping``'s tamper check relies on. Era
        tolerance for older payloads lives in ``from_mapping``, on the read side.
        """
        return {
            **self._core(),
            "linearization": dict(self.linearization),
            "linearization_outcome": self.linearization_outcome,
            "trim_decision": dict(self.trim_decision),
            "exclusion_evidence": dict(self.exclusion_evidence),
            "blend_correction": [dict(f) for f in self.blend_correction],
            "fingerprint": self.fingerprint,
        }

    def driver_corrections(self) -> dict[str, dict[str, float | bool]]:
        """The compiler-ready ``{role: {gain_db, delay_ms, inverted}}`` mapping."""

        return driver_corrections(self)

    @classmethod
    def from_mapping(cls, raw: Any) -> "MeasuredCrossoverCandidate":
        """Strictly reopen one persisted candidate without re-deriving evidence.

        A ``candidate.json`` written before an install can be reopened after it,
        so each key in ``_OPTIONAL_FIELD_TYPES`` may be absent and means
        exactly what its explicit empty value means. Every other field stays
        strictly required.
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
        if not isinstance(raw, Mapping) or (
            set(raw) - set(_OPTIONAL_FIELD_TYPES) != required
        ):
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
        # Absent -> {} (era tolerance); present -> validated as usual.
        linearization_raw = raw.get("linearization", {})
        if not isinstance(linearization_raw, Mapping):
            _refuse(
                "linearization_malformed", "candidate linearization is malformed"
            )
        # Absent -> "" (era tolerance); present -> validated by __post_init__.
        linearization_outcome_raw = raw.get("linearization_outcome", "")
        if not isinstance(linearization_outcome_raw, str):
            _refuse(
                "linearization_outcome_malformed",
                "candidate linearization_outcome is malformed",
            )
        # Absent -> {} (era tolerance); present -> validated by __post_init__.
        trim_decision_raw = raw.get("trim_decision", {})
        if not isinstance(trim_decision_raw, Mapping):
            _refuse("trim_decision_malformed", "candidate trim_decision is malformed")
        # Absent -> {} (era tolerance); present -> validated by __post_init__.
        exclusion_evidence_raw = raw.get("exclusion_evidence", {})
        if not isinstance(exclusion_evidence_raw, Mapping):
            _refuse(
                "exclusion_evidence_malformed",
                "candidate exclusion_evidence is malformed",
            )
        # Absent -> [] (era tolerance); present -> validated by __post_init__,
        # and re-validated for cuts-only at the emitter boundary.
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
                trim_decision=dict(trim_decision_raw),
                exclusion_evidence=dict(exclusion_evidence_raw),
                blend_correction=list(blend_correction_raw),
            )
        except (TypeError, ActiveSpeakerConfigError) as exc:
            raise MeasuredCrossoverCandidateError(
                "candidate_malformed", str(exc)
            ) from exc
        # to_dict() always carries every optional key, while an older `raw`
        # claimed the empty value by never mentioning it — so compare against
        # that claim made explicit.
        raw_for_comparison = dict(raw)
        for key, empty in _OPTIONAL_FIELD_TYPES.items():
            raw_for_comparison.setdefault(key, empty())
        if candidate.to_dict() != raw_for_comparison:
            _refuse(
                "candidate_tampered",
                "persisted measured crossover candidate does not match its "
                "declared result",
            )
        return candidate


def effective_preset(candidate: MeasuredCrossoverCandidate) -> ActiveSpeakerPreset:
    """The preset with the candidate's alignment written into its region fields.

    Absent alignment returns ``candidate.source_preset`` unchanged. Present
    alignment writes ``delay_ms``/``delay_target_driver`` onto the region
    ``delay_role`` identifies, and flips that region's ``upper_polarity`` only
    when ``polarity == "invert"``.
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
        # The ONE µs→ms quantizer, shared with prove_static_delay_binding's
        # expected value: a second recipe (e.g. round(µs/1000, 6)) disagrees
        # with the proof on ~0.4% of the valid range and refuses the apply.
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
    """The exact compiler-ready refinement this candidate proposes."""

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

    ``emit_kwargs`` forwards any other emitter keyword unchanged.

    CONVENTION shared with ``baseline_profile.build_baseline_profile_candidate``:
    the emitter derives delay and inversion from ``corrections`` ONLY, never
    from a region's ``delay_ms``/``delay_target_driver``/polarity fields (the
    baseline mixer is emitted with ``apply_region_polarity=False``). An emitter
    change that starts reading those fields must revisit both call sites.

    ``candidate.linearization`` is reduced by the shared
    ``linearization_fit.linearization_filters_by_role``. Not shared with
    ``baseline_profile.recompose_applied_baseline_yaml``, which reads an
    already-reduced snapshot and re-validates it inline.
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

    Fail-closed, no I/O: raises :class:`MeasuredCrossoverCandidateError` on the
    first failing proof. Two proofs, both independent second checks at the
    candidate boundary: every tweeter output keeps its protective high-pass,
    and an aligned candidate binds exactly one ``Delay`` filter for
    ``delay_role``, on that role's channels, at the requested ``delay_us``.
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
