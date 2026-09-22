# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Measured crossover candidates and their graph proof.

Delay and polarity use the preset's CrossoverRegion fields. Baseline graphs
apply polarity through the per-driver Gain filter; the split mixer must stay
a no-op inverter so the two stages cannot cancel the intended inversion.
Absent alignment preserves the source preset's existing delay and polarity.
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
from jasper.active_speaker.delay_graph import quantized_delay_ms
from jasper.audio_measurement.null_walk import (
    MAX_DSP_DELAY_US,
    DspPredecessor,
    NullWalkError,
)
from jasper.audio_measurement.room_boundary import (
    CEILING_SOURCES,
    ROOM_BOUNDARY_MAX_HZ,
    ROOM_BOUNDARY_MIN_HZ,
    ROOM_FLOOR_HZ,
)
from jasper.audio_measurement.room_limits import (
    ROOM_MAX_FILTER_BOOST_DB,
    ROOM_MAX_FILTERS_PER_SIDE,
    ROOM_MAX_TOTAL_BOOST_DB,
    ROOM_PEQ_Q_MAX,
    ROOM_PEQ_Q_MIN,
)
from jasper.bass_extension.dynamic import validate_dynamic_bass_descriptor
from jasper.camilla_config_contract import DEFAULT_SAMPLE_RATE, PeqFilter, total_positive_boost_db
from jasper.json_fields import finite_float

from ._common import issue, require_sha256_hex
from .camilla_names import driver_delay_name as _driver_delay_name
from .camilla_yaml import (
    _channels_for_role,
    _rear_stage_channels,
    role_polarity,
    emit_active_speaker_baseline_config,
)
from .crossover_v2.contracts import LINEARIZATION_OUTCOME_SINGLE_BRANCH, POLARITY_INVERT, POLARITY_KEEP
from .crossover_v2.room_prescription import ROOM_MEDIAN_FIELD
from .graph_safety import unprotected_tweeter_outputs, view_from_yaml_dict
from .level_trim import MAX_ATTENUATION_DB
from .measurement_programs import PROGRAM_DOCUMENT_ORDER
from .profile import (
    SIDES_BY_LAYOUT,
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    CrossoverRegion,
    required_driver_roles,
    declared_role_delays,
)
from .rear_calibration import (
    MIN_CHAIN_GAIN_DB,
    RearCalibrationError,
    read_rear_calibration,
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


# Shared by the unknown-field check, from_mapping persisted-core filter, and optional-field coverage test.
_OPTIONAL_FIELD_TYPES = {field.name: field.type for row in PROGRAM_DOCUMENT_ORDER for field in row.candidate_fields}

_ROOM_CORRECTION_KEYS = frozenset({
    "sides",
    "ceiling_hz",
    "ceiling_source",
    "basis",
    "boost_db_total",
    "level_cost_db",
})
_ROOM_BASIS_KEYS = frozenset({"round_id", ROOM_MEDIAN_FIELD, "admitted_boosts_hz"})
_ROOM_FILTER_KEYS = frozenset({"freq", "q", "gain"})


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


def _validated_rear_calibration(
    raw: Mapping[str, Any], preset: ActiveSpeakerPreset
) -> dict[str, Any]:
    """This cabinet's electrical cardioid document, or a typed refusal.

    Runtime v1 (ADR-0318): one mono cabinet, electrical settings, two summed
    rear branches. An acoustic-target or FIR document still validates as a
    handoff — it just cannot be the section a runtime graph is compiled from.
    """
    if _rear_stage_channels(preset) is None:
        _refuse(
            "rear_calibration_topology_unsupported",
            "rear calibration needs a mono cabinet of one front woofer, "
            "one rear woofer and one tweeter",
        )
    try:
        document = read_rear_calibration(raw, sample_rate=DEFAULT_SAMPLE_RATE)
    except RearCalibrationError as exc:
        _refuse("rear_calibration_invalid", str(exc))
    if document["case"] != "electrical_dsp":
        _refuse(
            "rear_calibration_case_unsupported",
            "a runtime rear calibration carries electrical settings, not acoustic targets",
        )
    if document["rear"]["mode"] != "branches":
        _refuse(
            "rear_calibration_mode_unsupported",
            "a runtime rear calibration carries two summed rear branches, not a FIR",
        )
    return document


#: The cabinet's woofers are both silent — a tuning the owner should see, not a
#: refusal: a document may legitimately park the rear while the front is fitted.
REAR_CALIBRATION_TWEETER_ONLY = "rear_calibration_cabinet_tweeter_only"


def _chain_is_silent(chain: Mapping[str, Any]) -> bool:
    return bool(chain["muted"]) or float(chain["gain_db"]) <= MIN_CHAIN_GAIN_DB


def _rear_calibration_disclosure(document: Mapping[str, Any]) -> dict[str, str] | None:
    """A warning when the document leaves only the tweeter audible."""
    rear = document["rear"]
    rear_silent = bool(document["rear_muted"]) or all(
        _chain_is_silent(rear[branch]) for branch in ("bass", "cancellation")
    )
    if not rear_silent or not _chain_is_silent(document["front"]):
        return None
    return issue(
        "warning",
        REAR_CALIBRATION_TWEETER_ONLY,
        "this rear calibration silences both woofers; only the tweeter plays",
    )


def _validated_room_correction(
    raw: Mapping[str, Any], *, layout_sides: tuple[str, ...]
) -> Mapping[str, Any]:
    """The room PEQ set ``raw`` claims, refused whole if it breaks a room limit.

    Empty is the ordinary case. The room prescription door composes the set and
    checks it against the measured median; this is the independent second check
    at the persistence boundary, using only the limits and the layout.
    """

    if not raw:
        return {}
    _ROOM_INVALID = "room_correction_invalid"
    if len(layout_sides) > 1:
        _refuse(
            _ROOM_INVALID,
            "the emitter takes one room PEQ list, so a room set on a "
            f"{len(layout_sides)}-sided layout would emit only "
            f"{layout_sides[0]!r}; remove this bound when per-side room "
            "emission lands (ADR-0258)",
        )
    if set(raw) != _ROOM_CORRECTION_KEYS:
        _refuse(
            _ROOM_INVALID,
            f"room_correction keys must be exactly {sorted(_ROOM_CORRECTION_KEYS)}",
        )
    ceiling_hz = finite_float(raw["ceiling_hz"])
    if (
        ceiling_hz is None
        or not ROOM_BOUNDARY_MIN_HZ <= ceiling_hz <= ROOM_BOUNDARY_MAX_HZ
    ):
        _refuse(
            _ROOM_INVALID,
            "ceiling_hz must be within "
            f"{ROOM_BOUNDARY_MIN_HZ}..{ROOM_BOUNDARY_MAX_HZ} Hz",
        )
    if raw["ceiling_source"] not in CEILING_SOURCES:
        _refuse(
            _ROOM_INVALID,
            f"ceiling_source must be one of {sorted(CEILING_SOURCES)}",
        )
    basis = raw["basis"]
    if not isinstance(basis, Mapping) or set(basis) != _ROOM_BASIS_KEYS:
        _refuse(
            _ROOM_INVALID,
            f"basis keys must be exactly {sorted(_ROOM_BASIS_KEYS)}",
        )
    if not isinstance(basis["round_id"], str) or not basis["round_id"].strip():
        _refuse(_ROOM_INVALID, "basis.round_id must be a non-empty string")
    try:
        require_sha256_hex(
            basis[ROOM_MEDIAN_FIELD], f"basis.{ROOM_MEDIAN_FIELD}", ValueError
        )
    except ValueError as exc:
        _refuse(_ROOM_INVALID, str(exc))
    if not isinstance(basis["admitted_boosts_hz"], list):
        _refuse(_ROOM_INVALID, "basis.admitted_boosts_hz must be a list")
    admitted: list[float] = []
    for value in basis["admitted_boosts_hz"]:
        number = finite_float(value)
        if number is None:
            _refuse(
                _ROOM_INVALID,
                "basis.admitted_boosts_hz must be finite numbers",
            )
        admitted.append(number)
    sides = raw["sides"]
    if not isinstance(sides, Mapping) or set(sides) != set(layout_sides):
        _refuse(
            _ROOM_INVALID,
            f"sides must cover exactly {sorted(layout_sides)}",
        )
    side_boosts: list[float] = []
    for side in layout_sides:
        filters = sides[side]
        if not isinstance(filters, list) or len(filters) > ROOM_MAX_FILTERS_PER_SIDE:
            _refuse(
                _ROOM_INVALID,
                f"side {side!r} must be a list of at most "
                f"{ROOM_MAX_FILTERS_PER_SIDE} filters",
            )
        peqs: list[PeqFilter] = []
        for entry in filters:
            if not isinstance(entry, Mapping) or set(entry) != _ROOM_FILTER_KEYS:
                _refuse(
                    _ROOM_INVALID,
                    f"side {side!r} filter keys must be exactly "
                    f"{sorted(_ROOM_FILTER_KEYS)}",
                )
            freq = finite_float(entry["freq"])
            q = finite_float(entry["q"])
            gain = finite_float(entry["gain"])
            if freq is None or q is None or gain is None:
                _refuse(
                    _ROOM_INVALID,
                    f"side {side!r} filter values must be finite numbers",
                )
            if not ROOM_FLOOR_HZ <= freq <= ceiling_hz:
                _refuse(
                    _ROOM_INVALID,
                    f"side {side!r} filter freq must be within "
                    f"{ROOM_FLOOR_HZ}..{ceiling_hz} Hz",
                )
            if not ROOM_PEQ_Q_MIN <= q <= ROOM_PEQ_Q_MAX:
                _refuse(
                    _ROOM_INVALID,
                    f"side {side!r} filter q must be within "
                    f"{ROOM_PEQ_Q_MIN}..{ROOM_PEQ_Q_MAX}",
                )
            peqs.append(PeqFilter(freq=freq, q=q, gain=gain))
            if gain <= 0.0:
                continue
            if gain > ROOM_MAX_FILTER_BOOST_DB:
                _refuse(
                    _ROOM_INVALID,
                    f"side {side!r} boost must not exceed "
                    f"{ROOM_MAX_FILTER_BOOST_DB} dB",
                )
            if not any(math.isclose(freq, value) for value in admitted):
                _refuse(
                    _ROOM_INVALID,
                    f"side {side!r} boost at {freq} Hz is not an admitted boost",
                )
        boost = total_positive_boost_db(peqs)
        if boost > ROOM_MAX_TOTAL_BOOST_DB:
            _refuse(
                _ROOM_INVALID,
                f"side {side!r} total boost must not exceed "
                f"{ROOM_MAX_TOTAL_BOOST_DB} dB",
            )
        side_boosts.append(boost)
    boost_db_total = finite_float(raw["boost_db_total"])
    if boost_db_total is None or not math.isclose(
        boost_db_total, max(side_boosts), abs_tol=1e-9
    ):
        _refuse(
            _ROOM_INVALID,
            "boost_db_total must equal the largest per-side positive gain sum",
        )
    level_cost_db = finite_float(raw["level_cost_db"])
    if level_cost_db is None or not math.isclose(
        level_cost_db, boost_db_total, abs_tol=1e-9
    ):
        _refuse(_ROOM_INVALID, "level_cost_db must equal boost_db_total")
    return raw


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

    ``room_correction`` is the modal-band PEQ set: ``{"sides": {side: [{freq,
    q, gain}, ...]}, "ceiling_hz", "ceiling_source", "basis", "boost_db_total",
    "level_cost_db"}``, where ``basis`` is the round and ``room.json`` median-section
    digest the set was prescribed from. The room prescription door is its only
    writer; :func:`_validated_room_correction` re-checks it here against the
    room layer's limits. Per-side sets are DATA today — the emitter takes one
    list (:func:`candidate_room_peqs`) and per-side emission arrives later.

    ``rear_calibration`` is this cabinet's ``jts_rear_calibration`` electrical
    document (ADR-0318). Present, the baseline emitter compiles it into the
    cardioid stage and the rear woofer plays; absent, the rear output stays
    terminally muted. ``_validated_rear_calibration`` re-checks it here against
    the runtime's v1 scope.

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
    room_correction: Mapping[str, Any] = field(default_factory=dict)
    bass_extension: Mapping[str, Any] = field(default_factory=dict)
    rear_calibration: Mapping[str, Any] = field(default_factory=dict)
    fingerprint: str = field(init=False, repr=False)
    _persisted_core: DspPredecessor | None = field(
        default=None, init=False, repr=False, compare=False,
    )

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
        object.__setattr__(
            self,
            "room_correction",
            _validated_room_correction(
                self.room_correction,
                layout_sides=SIDES_BY_LAYOUT[self.source_preset.channel_map.layout],
            ),
        )
        if self.bass_extension:
            try:
                dynamic_bass = validate_dynamic_bass_descriptor(self.bass_extension)
            except ValueError as exc:
                _refuse(getattr(exc, "reason"), str(exc))
            object.__setattr__(self, "bass_extension", dynamic_bass)
        if self.rear_calibration:
            document = _validated_rear_calibration(
                self.rear_calibration, self.source_preset,
            )
            object.__setattr__(self, "rear_calibration", document)
            self._disclose(_rear_calibration_disclosure(document))
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

    def _disclose(self, note: dict[str, str] | None) -> None:
        """Append one warning to ``analysis["issues"]``, idempotently.

        Reopening a written candidate must not re-append it, or the fingerprint
        would move on every round trip and read as tampering.
        """
        if note is None:
            return
        existing = list(self.analysis.get("issues") or [])
        if any(
            isinstance(item, Mapping) and item.get("code") == note["code"]
            for item in existing
        ):
            return
        object.__setattr__(self, "analysis", DspPredecessor(
            {"analysis": {**self.analysis, "issues": [*existing, note]}}
        ).state["analysis"])

    def _core(self) -> dict[str, Any]:
        """Stored identity survives preset schema changes; new identities use today's schema."""
        if self._persisted_core is not None:
            return self._persisted_core.state
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
        if self.room_correction:
            core["room_correction"] = dict(self.room_correction)
        if self.bass_extension:
            core["bass_extension"] = dict(self.bass_extension)
        if self.rear_calibration:
            core["rear_calibration"] = dict(self.rear_calibration)
        return core

    def to_dict(self) -> dict[str, Any]:
        """Persist the fingerprinted core with explicit empty optional fields."""
        payload = self._core()
        for key, empty in _OPTIONAL_FIELD_TYPES.items():
            payload.setdefault(key, empty())
        return {**payload, "fingerprint": self.fingerprint}

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
        # Absent -> {} (era tolerance); present -> validated by __post_init__.
        room_correction_raw = raw.get("room_correction", {})
        if not isinstance(room_correction_raw, Mapping):
            _refuse(
                "room_correction_malformed", "candidate room_correction is malformed"
            )
        bass_extension_raw = raw.get("bass_extension", {})
        if not isinstance(bass_extension_raw, Mapping):
            _refuse(
                "bass_extension_malformed", "candidate bass_extension is malformed"
            )
        rear_calibration_raw = raw.get("rear_calibration", {})
        if not isinstance(rear_calibration_raw, Mapping):
            _refuse(
                "rear_calibration_malformed", "candidate rear_calibration is malformed"
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
                room_correction=dict(room_correction_raw),
                bass_extension=dict(bass_extension_raw),
                rear_calibration=dict(rear_calibration_raw),
            )
        except (TypeError, ActiveSpeakerConfigError) as exc:
            raise MeasuredCrossoverCandidateError(
                "candidate_malformed", str(exc)
            ) from exc
        core = {
            key: value for key, value in raw.items()
            if key != "fingerprint" and (key not in _OPTIONAL_FIELD_TYPES or value)
        }
        try:
            fingerprint = json_fingerprint(core)
        except EvidenceIdentityError as exc:
            _refuse("candidate_invalid", str(exc))
        if fingerprint != raw["fingerprint"]:
            _refuse(
                "candidate_tampered",
                "persisted measured crossover candidate does not match its "
                "declared result",
            )
        object.__setattr__(candidate, "_persisted_core", DspPredecessor(core))
        object.__setattr__(candidate, "fingerprint", fingerprint)
        return candidate


def room_peqs_from_correction(
    room_correction: Mapping[str, Any],
    preset: ActiveSpeakerPreset,
) -> tuple[PeqFilter, ...]:
    """Decode an accepted room correction into the emitter's mono PEQ list."""

    if not room_correction:
        return ()
    correction = _validated_room_correction(
        room_correction,
        layout_sides=SIDES_BY_LAYOUT[preset.channel_map.layout],
    )
    side = SIDES_BY_LAYOUT[preset.channel_map.layout][0]
    return tuple(
        PeqFilter(
            freq=float(entry["freq"]),
            q=float(entry["q"]),
            gain=float(entry["gain"]),
        )
        for entry in correction["sides"][side]
    )


def candidate_room_peqs(
    candidate: MeasuredCrossoverCandidate,
) -> tuple[PeqFilter, ...]:
    """The room PEQs of the layout's one declared side; ``()`` when absent.

    The emitter takes one list, which is why ``_validated_room_correction``
    refuses a room set on a multi-sided layout at all (ADR-0258).
    """

    return room_peqs_from_correction(
        candidate.room_correction,
        candidate.source_preset,
    )


def candidate_on_declaration(
    candidate: MeasuredCrossoverCandidate, preset: ActiveSpeakerPreset,
) -> MeasuredCrossoverCandidate:
    return dataclasses.replace(candidate, source_preset=dataclasses.replace(
        preset, crossover_regions=candidate.source_preset.crossover_regions,
    ))


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
    polarity = role_polarity(preset)
    roles = required_driver_roles(preset.way_count)
    delays = declared_role_delays(preset)
    return {
        role: {
            "gain_db": candidate.role_attenuations_db[role],
            "delay_ms": delays.get(role, 0.0),
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

    Delay and inversion come from ``corrections`` only; region polarity is
    excluded by ``apply_region_polarity=False``.
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
        bass_extension=candidate.bass_extension,
        rear_calibration=candidate.rear_calibration,
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

    from jasper.active_speaker.delay_graph import (
        DelayGraphProofError,
        prove_static_delay_binding,
    )

    preset = effective_preset(candidate)
    view = view_from_yaml_dict(_yaml.safe_load(yaml_text))
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
