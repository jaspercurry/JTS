# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bank a candidate through the evidence writers, and prepare its applied profile."""
import json
from pathlib import Path

from jasper.active_speaker import bundles
from jasper.active_speaker.candidate_bank import find_banked_candidate, CandidateBankRefusal
from tests.active_speaker_fixtures import empty_protection


def bank_candidate(candidate):
    root = bundles.sessions_dir()
    try:
        return find_banked_candidate(candidate.fingerprint)
    except CandidateBankRefusal:
        candidate_path = root / f"authored-{candidate.fingerprint}" / "evidence/v1/artifacts/crossover_v2/authored/candidate.json"
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(json.dumps(candidate.to_dict()))
        return find_banked_candidate(candidate.fingerprint)


def prepare_candidate(candidate, topology, config_path, *, design_draft=None):
    import hashlib
    from jasper.active_speaker.baseline_record import prepare_applied_baseline_profile
    from jasper.active_speaker.branch_chain import confirmed_protection_sections
    from jasper.active_speaker.measurement_emit import MeasurementGraphProfile, compile_tuning_graph
    from jasper.active_speaker.playback_route import resolve_active_playback_device
    from jasper.sound.settings import saved_sound_layers

    banked = bank_candidate(candidate)
    draft = design_draft or {}
    safety = draft.get("driver_safety_profile")
    declaration = MeasurementGraphProfile(candidate.source_preset, topology, {}, resolve_active_playback_device(topology)[0],
        confirmed_protection_sections(safety) if safety else empty_protection(candidate.source_preset))
    preference_filters, trim_db = saved_sound_layers()
    text = compile_tuning_graph(declaration, candidate, preference_filters=preference_filters, output_trim_db=trim_db)
    Path(config_path).write_text(text)
    return prepare_applied_baseline_profile(banked, declaration=declaration, design_draft=draft,
        config_path=config_path, config_sha256=hashlib.sha256(text.encode()).hexdigest())
