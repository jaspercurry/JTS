# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""v2 measured-crossover apply extension — trims + optional delay/polarity.

Wave 4 of the crossover-measurement v2 redesign
(``docs/crossover-measurement-productization-design.md`` §5.8). This is a
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
from typing import Any, Mapping, NoReturn

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
from .graph_safety import unprotected_tweeter_outputs, view_from_emitted_text
from .profile import (
    ActiveSpeakerConfigError,
    ActiveSpeakerPreset,
    CrossoverRegion,
    required_driver_roles,
)

SCHEMA_VERSION = 1
CANDIDATE_KIND = "jts_measured_crossover_candidate_v2"

# Mirrors baseline_profile._MAX_ATTENUATION_DB / measured_candidate._MAX_ATTENUATION_DB
# (the shared -60 dB attenuation floor); duplicated locally rather than imported
# since neither module exports it and this module intentionally does not couple
# to either.
_MAX_ATTENUATION_DB = -60.0

_POLARITY_VALUES = frozenset({POLARITY_KEEP, POLARITY_INVERT})


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
    """

    program_id: str
    analysis: Mapping[str, Any]
    source_preset: ActiveSpeakerPreset
    role_attenuations_db: Mapping[str, float]
    alignment: MeasuredCrossoverAlignment = _NO_ALIGNMENT
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
                or float(value) < _MAX_ATTENUATION_DB
            ):
                _refuse(
                    "attenuation_out_of_range",
                    f"attenuation for {role!r} must be between "
                    f"{_MAX_ATTENUATION_DB} and 0 dB",
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
        try:
            fingerprint = json_fingerprint(self._core())
        except EvidenceIdentityError as exc:
            _refuse("candidate_invalid", str(exc))
        object.__setattr__(self, "fingerprint", fingerprint)

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": CANDIDATE_KIND,
            "program_id": self.program_id,
            "analysis": self.analysis,
            "source_preset": self.source_preset.to_dict(),
            "role_attenuations_db": dict(self.role_attenuations_db),
            "alignment": self.alignment.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._core(), "fingerprint": self.fingerprint}

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
        """Strictly reopen one persisted candidate without re-deriving evidence."""

        expected = {
            "schema_version",
            "kind",
            "program_id",
            "analysis",
            "source_preset",
            "role_attenuations_db",
            "alignment",
            "fingerprint",
        }
        if not isinstance(raw, Mapping) or set(raw) != expected:
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
            )
        except (TypeError, ActiveSpeakerConfigError) as exc:
            raise MeasuredCrossoverCandidateError(
                "candidate_malformed", str(exc)
            ) from exc
        if candidate.to_dict() != dict(raw):
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
    function only supplies the preset (with alignment folded in) and the
    derived ``corrections`` mapping. ``emit_kwargs`` forwards any other
    emitter keyword (``capture_device``, ``out_path``, ...) unchanged.

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
    """

    preset = effective_preset(candidate)
    corrections = driver_corrections(candidate)
    return emit_active_speaker_baseline_config(
        preset,
        playback_device=playback_device,
        corrections=corrections,
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
