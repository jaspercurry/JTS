# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Resolve saved tune inputs separately from configuration and record building."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from jasper.camilla_config_contract import FilterSpec
from jasper import output_topology_store as output_topology

from . import baseline_profile, candidate_bank, design_draft, measurement_emit, runtime_contract
from .crossover_declaration import assert_crossover_honours_declared_floor
from .measured_crossover_candidate import candidate_on_declaration


@dataclass(frozen=True)
class AppliedTune:
    banked: candidate_bank.BankedCandidate
    declaration: measurement_emit.MeasurementGraphProfile
    draft: Mapping[str, Any]
    applied: Mapping[str, Any]


def load_applied_tune() -> AppliedTune:
    applied = baseline_profile.load_applied_baseline_profile_state() or {}
    banked = candidate_bank.load_applied_candidate(
        (applied.get("source") or {}).get("measured_candidate_fingerprint", ""), applied_profile=applied,
    )
    topology = output_topology.load_output_topology_strict()
    draft = design_draft.load_design_draft(topology=topology)
    declaration = measurement_emit.load_tuning_declaration(topology, design_draft=draft)
    measurement_emit.require_candidate_speaker_identity(banked.candidate, declaration.preset)
    bound = candidate_on_declaration(banked.candidate, declaration.preset)
    assert_crossover_honours_declared_floor(bound.source_preset)
    return AppliedTune(banked, declaration, draft, applied)


def compile_applied_tune(
    tune: AppliedTune, *, preference_filters: Sequence[FilterSpec], output_trim_db: float,
) -> tuple[str, dict[str, Any]]:
    text = measurement_emit.compile_tuning_graph(tune.declaration, candidate=tune.banked.candidate,
        preference_filters=preference_filters, output_trim_db=output_trim_db)
    prepared = baseline_profile.prepare_applied_baseline_profile(tune.banked, declaration=tune.declaration,
        design_draft=tune.draft, provenance=tune.applied)
    proof = runtime_contract.classify_bass_extension_graph(tune.declaration.topology, evidence_source="desired",
        graph_text=text, applied_baseline_state=prepared)
    if not proof.allowed or proof.classification != runtime_contract.GRAPH_APPROVED_ACTIVE_RUNTIME:
        raise ValueError(proof.classification)
    return text, prepared
