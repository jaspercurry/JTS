# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Device-bound entry prompts for the three shared tuning programs."""
from __future__ import annotations

from typing import Any, Mapping

from jasper.identity.reader import (
    CROSSOVER_PAGE_PATH,
    SPEAKER_SETUP_PAGE_PATH,
    read_identity,
    speaker_url,
)

HANDOFF_READY = "ready"
HANDOFF_NOT_READY = "not_ready"

#: Why the card holds the prompt back. No tuning flow is a prerequisite for
#: USING the speaker, so the handoff appears only once the executor chain has
#: produced a playing baseline — never earlier, and never as a gate on sound.
NO_APPLIED_BASELINE = "no_applied_baseline"

#: Installed console-script paths, not bare names: an SSH session gets no
#: ``EnvironmentFile=`` and /opt/jasper/.venv is not on the default PATH.
_BIN = "/opt/jasper/.venv/bin"
ORIENTATION_COMMAND = f"sudo {_BIN}/jasper-crossover-prescriber status"
PROGRAM_DOOR_COMMAND = (
    f"sudo {_BIN}/jasper-round run --help"
)


def build_tuning_handoff_binding(design_draft: Mapping[str, Any]) -> dict[str, Any]:
    """Who this prompt was minted for, and against which declarations.

    Identity and URLs only. **No credential of any kind belongs here** — not
    the control token, not a PSK, not the peer id: this payload is minted to
    be copied into a third-party chat session, so anything in it is disclosed
    by construction. Access is the human's to grant over SSH.
    """
    identity = read_identity()
    revision = design_draft.get("revision")
    return {
        "speaker_name": identity.name,
        "hostname": identity.hostname,
        "declaration_url": speaker_url(SPEAKER_SETUP_PAGE_PATH),
        "crossover_url": speaker_url(CROSSOVER_PAGE_PATH),
        "design_draft_revision": revision if isinstance(revision, int) else 0,
    }


PROGRAM_ENTRIES = (
    {"id": "speaker", "title": "Speaker", "description": "Fit the drivers and align their crossover."},
    {"id": "room", "title": "Room", "description": "Fit the listening area and keep the saved Speaker tune."},
    {"id": "bass", "title": "Bass", "description": "Add low bass that eases back as volume or bass demand rises. Keep Speaker and Room."},
)


def build_tuning_handoff_prompt(binding: Mapping[str, Any], program_id: str) -> str:
    entry = next(item for item in PROGRAM_ENTRIES if item["id"] == program_id)
    hostname = str(binding.get("hostname") or "")
    return "\n".join((
        f"Help me run the {entry['title']} tuning program on {binding.get('speaker_name') or hostname} ({hostname}).",
        entry["description"],
        f"Use existing SSH access to {hostname}; ask for a login only if access is missing.",
        f"Start with {ORIENTATION_COMMAND}.",
        f"Read the Entry contract and {entry['title']} section of /opt/jasper/docs/tuning-operator-runbook.md, then use its tool menu.",
        "Find retained evidence and the current saved tune before choosing new measurements. Use the shared capture, candidate bank, trial and save tools.",
        f"Inspect available measurement plans with {PROGRAM_DOOR_COMMAND}.",
        "Explain the next step briefly. I place the microphone and start each position batch. Measure the chosen change, show its limits, and get my choice before saving.",
        f"This copy names declaration revision {binding.get('design_draft_revision')}. Check the live identity and declarations at {binding.get('declaration_url') or ''} before playback.",
    ))


def build_tuning_handoff(
    *,
    commissioning_view: Mapping[str, Any],
    design_draft: Mapping[str, Any],
) -> dict[str, Any]:
    """See ADR-0312: declaration changes do not revoke an applied proof."""
    binding = build_tuning_handoff_binding(design_draft)
    applied = commissioning_view.get("applied_profile")
    ready = isinstance(applied, Mapping) and applied.get("stands") is True
    reason = None if ready else NO_APPLIED_BASELINE
    return {
        "status": HANDOFF_READY if ready else HANDOFF_NOT_READY,
        "reason": reason,
        "binding": binding,
        "programs": [{**entry, "prompt": build_tuning_handoff_prompt(binding, entry["id"]) if ready else ""}
                     for entry in PROGRAM_ENTRIES],
    }
