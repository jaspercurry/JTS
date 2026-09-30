# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Device-bound entry prompts for the shared tuning programs, built from the program and preset rows."""
from __future__ import annotations

import json
from typing import Any, Collection, Mapping

from jasper.active_speaker.commissioning_coordinator import VIEW_STATUS_NOT_REQUIRED
from jasper.active_speaker.design_inputs import declared_by_target
from jasper.active_speaker.measurement_programs import (
    PROGRAM_ENTRIES, RUNNABLE_PROGRAMS, available_presets, offered_here, preset,
)
from jasper.active_speaker.tuning_docs import reading_order
from jasper.identity.reader import (
    CROSSOVER_PAGE_PATH,
    SPEAKER_SETUP_PAGE_PATH,
    read_identity,
    speaker_url,
)

HANDOFF_READY = "ready"
HANDOFF_NOT_READY = "not_ready"

NO_APPLIED_BASELINE = "no_applied_baseline"

#: Installed console-script paths, not bare names: an SSH session gets no
#: ``EnvironmentFile=`` and /opt/jasper/.venv is not on the default PATH.
_BIN = "/opt/jasper/.venv/bin"
ORIENTATION_COMMAND = f"sudo {_BIN}/jasper-crossover-prescriber status"


def pointer_commands(program_id: str) -> tuple[str, str, str]:
    """Where tuning stands, what the agent can ask, and what a document may write (#5928 TB6)."""
    return (ORIENTATION_COMMAND,
            f"sudo {_BIN}/jasper-round-views catalog --program {program_id}",
            f"sudo {_BIN}/jasper-crossover-prescriber contract --section {program_id}")


def _declared_components(design_draft: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each declared driver output, by the id ``jasper-round run --driver`` takes: its role, its
    output, its role's passband as ``status`` reports it, its diameter (ADR-0384) and its
    sensitivity after its pad."""
    from jasper.active_speaker.crossover_v2.driver_prescription import driver_passbands_from_safety_profile  # lazy: keeps jasper.web numpy-free (tests/test_correction_substream_ssot.py)

    profile = design_draft.get("driver_safety_profile") or {}
    passbands = driver_passbands_from_safety_profile(profile)
    diameters = declared_by_target(design_draft, "radiating_diameter_mm")
    components = []
    for target in profile.get("targets") or ():
        driver = target["target_id"].removeprefix(f"{target['speaker_group_id']}:")
        band = passbands.get(target["role"])
        components.append({
            "driver": driver, "role": target["role"], "physical_output_index": target.get("physical_output_index"),
            "passband_hz": list(band) if band else None, "radiating_diameter_mm": diameters.get(driver),
            "effective_sensitivity_db_2v83_1m": target.get("effective_sensitivity_db_2v83_1m"),
        })
    return components


def _one_driver_presets(programs: Collection[str], drivers: Collection[str]) -> list[str]:
    """The presets whose every pose plays one driver alone, among those this speaker runs."""
    return [row.preset for row in map(preset, available_presets())
            if all(pose.driver for pose in row.poses) and offered_here(row, programs=programs, targets=drivers)]


def build_tuning_handoff_binding(
    design_draft: Mapping[str, Any], commissioning_view: Mapping[str, Any],
) -> dict[str, Any]:
    """Which speaker, declarations, applied tune and round this prompt names.

    **No credential of any kind belongs here** — not the control token, not a
    PSK, not the peer id. Anything here is disclosed to a third-party chat.
    """
    from jasper.active_speaker.crossover_v2.round_inputs import banked_round_of, recent_round_sessions  # lazy: keeps jasper.web numpy-free (tests/test_correction_substream_ssot.py)

    identity = read_identity()
    revision = design_draft.get("revision")
    applied = commissioning_view.get("applied_profile")
    applied = applied if isinstance(applied, Mapping) else {}
    has_applied = applied.get("exists") is True
    rounds = recent_round_sessions(limit=1)
    return {
        "speaker_name": identity.name,
        "hostname": identity.hostname,
        "declaration_url": speaker_url(SPEAKER_SETUP_PAGE_PATH),
        "crossover_url": speaker_url(CROSSOVER_PAGE_PATH),
        "design_draft_revision": revision if isinstance(revision, int) else 0,
        "components": _declared_components(design_draft),
        "one_driver_presets": _one_driver_presets(commissioning_view.get("programs") or (),
                                                  commissioning_view.get("near_field_drivers") or ()),
        "applied_candidate_fingerprint": applied.get("candidate_fingerprint") if has_applied else None,
        "applied_record": applied.get("record") if has_applied else None,
        "applied_at": applied.get("applied_at") if has_applied else None,
        "latest_round_dir": str(banked_round_of(rounds[0]) or rounds[0]) if rounds else None,
    }


def _program_entry(program_id: str) -> dict[str, Any]:
    entry = next((item for item in PROGRAM_ENTRIES if item["id"] == program_id), None)
    if entry is None:
        raise ValueError(f"unknown tuning program: {program_id}")
    return entry


def build_tuning_handoff_prompt(binding: Mapping[str, Any], program_id: str) -> str:
    entry = _program_entry(program_id)
    hostname = str(binding.get("hostname") or "")
    documents = "\n".join(f"{i}. {item['path']}" for i, item in enumerate(reading_order(), 1))
    applied = (
        "Applied tune:\n"
        f"candidate fingerprint: {binding.get('applied_candidate_fingerprint')}\n"
        f"record id: {binding.get('applied_record')}\n"
        f"applied at: {binding.get('applied_at')}"
        if binding.get("applied_candidate_fingerprint") or binding.get("applied_record")
        else "no baseline applied"
    )
    latest_round = binding.get("latest_round_dir")
    components = binding.get("components") or ()
    presets = binding.get("one_driver_presets") or ()
    status, catalog, contract = pointer_commands(program_id)
    return "\n".join((
        "Read these documents in this order:",
        documents,
        "",
        "This speaker:",
        f"name: {binding.get('speaker_name') or hostname}",
        f"hostname: {hostname}",
        f"declaration: {binding.get('declaration_url') or ''}",
        f"crossover: {binding.get('crossover_url') or ''}",
        f"declaration revision: {binding.get('design_draft_revision')}",
        *(("declared components:", *map(json.dumps, components)) if components else ()),
        *(("one-driver presets:", *(f"{name}: {preset(name).use_when}" for name in presets)) if presets else ()),
        "",
        applied,
        (f"Latest round directory: {latest_round}" if latest_round
         else f"Latest round directory: find it with {ORIENTATION_COMMAND}"),
        "",
        f"Run the tuning programs in order: {' → '.join(RUNNABLE_PROGRAMS)} (skip rear if there is no rear driver).",
        "Re-run room after any upstream change.",
        f"Program: {entry['title']}",
        entry["description"],
        f"Run: sudo {_BIN}/jasper-round run --program {program_id}",
        "",
        f"Use existing SSH access to {hostname}; ask for a login only if access is missing.",
        f"Where tuning stands: {status}",
        f"What you can ask: {catalog}",
        f"What a document may write: {contract}",
        "Create the measurement session, then give me its returned link. Explain the next step briefly; I place the microphone and start each batch. Measure the change, show its limits, and get my choice before saving.",
    ))


def build_tuning_handoff(
    *,
    commissioning_view: Mapping[str, Any],
    design_draft: Mapping[str, Any],
    program_id: str = "speaker",
) -> dict[str, Any]:
    """See ADR-0312: declaration changes do not revoke an applied proof."""
    _program_entry(program_id)
    binding = build_tuning_handoff_binding(design_draft, commissioning_view)
    applied = commissioning_view.get("applied_profile")
    ready = (commissioning_view.get("status") == VIEW_STATUS_NOT_REQUIRED
             or isinstance(applied, Mapping) and applied.get("stands") is True)
    reason = None if ready else NO_APPLIED_BASELINE
    return {
        "status": HANDOFF_READY if ready else HANDOFF_NOT_READY,
        "reason": reason,
        "binding": binding,
        "driver_spacing_mm": commissioning_view.get("driver_spacing_mm"),
        "programs": [_program_entry(name) for name in commissioning_view["programs"]],
        "program": program_id,
        "prompt": build_tuning_handoff_prompt(binding, program_id) if ready else "",
    }
