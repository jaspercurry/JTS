# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Device-bound entry prompts for the shared tuning programs."""
from __future__ import annotations

from typing import Any, Mapping

from jasper.active_speaker.commissioning_coordinator import VIEW_STATUS_NOT_REQUIRED
from jasper.active_speaker.measurement_programs import RUNNABLE_PROGRAMS, PROGRAM_ENTRIES
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
PROGRAM_DOOR_COMMAND = (
    f"sudo {_BIN}/jasper-round run --help"
)


def build_tuning_handoff_binding(
    design_draft: Mapping[str, Any], commissioning_view: Mapping[str, Any],
) -> dict[str, Any]:
    """Which speaker, declarations, applied tune and round this prompt names.

    **No credential of any kind belongs here** — not the control token, not a
    PSK, not the peer id. Anything here is disclosed to a third-party chat.
    """
    from jasper.active_speaker.crossover_v2.round_inputs import recent_round_sessions  # lazy: keeps jasper.web numpy-free (tests/test_correction_substream_ssot.py)

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
        "applied_candidate_fingerprint": applied.get("candidate_fingerprint") if has_applied else None,
        "applied_record": applied.get("record") if has_applied else None,
        "applied_at": applied.get("applied_at") if has_applied else None,
        "latest_round_dir": str(rounds[0]) if rounds else None,
    }


#: Extra operator guidance. Rear composes the rear-muted reference and
#: variants as ordinary candidates, then trials them (playbook, Rear
#: section); bass's default layout pins the arm.
_PROGRAM_PROMPT_LINES = {
    "rear": (
        "First compose the rear-muted copy and the variants as ordinary "
        f"candidates (playbook, Rear), then: sudo {_BIN}/jasper-round trial "
        "<fingerprint> --candidates base,<muted>,<variant> (the trial picks "
        "the rear positions).",
        'Read packet["rear"] as the playbook says; the arm reaches 45 degrees, '
        "a person any bearing.",
    ),
    "bass": (
        f"Without the arm: sudo {_BIN}/jasper-round run --program bass --poses bass/nearfield "
        "--mover human (microphone 3 cm from the woofer).",
    ),
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
        f"Prescription contract: sudo {_BIN}/jasper-crossover-prescriber contract --round <dir> --section {program_id}",
        *_PROGRAM_PROMPT_LINES.get(program_id, ()),
        "",
        f"Use existing SSH access to {hostname}; ask for a login only if access is missing.",
        f"Orient with {ORIENTATION_COMMAND}.",
        f"Inspect available measurement plans with {PROGRAM_DOOR_COMMAND}.",
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
