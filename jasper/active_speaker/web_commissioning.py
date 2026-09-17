# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared startup anchors, software guards and commission status."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from jasper.active_speaker.commission_ramp import (
    load_ramp_state,
)
from jasper.active_speaker.commission_wiring import (
    read_current_config_path,
    write_commission_path_safety,
)
from jasper.active_speaker.safe_playback import (
    load_safe_playback_state,
)
from jasper.active_speaker.staging import (
    DEFAULT_CAMILLA_CONFIG_DIR as DEFAULT_CAMILLA_CONFIG_DIR,
    load_staged_startup_config,
    stage_protected_startup_config,
)
from jasper.active_speaker.commission_load import (
    load_commission_load_state,
)
from jasper.active_speaker.startup_load import (
    load_protected_startup_config,
    staged_topology_match_status,
)
from jasper.camilla import CamillaUnavailable
from jasper.dsp_apply import same_config_file
from jasper.log_event import log_event
from jasper.output_topology import (
    OutputTopology,
    load_output_topology,
    output_topology_mutation,
    set_channel_protection_status,
)

from ._common import blocker_issue as _issue

logger = logging.getLogger(__name__)

CamillaFactory = Callable[[], Any]

_EVIDENCE_READ_ERRORS = (
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
    json.JSONDecodeError,
)
_COMMISSION_OPERATION_ERRORS = (
    CamillaUnavailable,
    OSError,
    RuntimeError,
    ValueError,
    TypeError,
)


async def attempt_graph_restore(
    restore: Callable[[], Awaitable[Any]],
) -> tuple[bool, str | None]:
    """Run one graph restore and never raise: ``(took_effect, raise_message)``.

    The one verdict the swap transaction reaches, here and on
    ``program_playback``'s measurement path: it TOOK, it RAISED (message
    present), or CamillaDSP REJECTED it (``False``, no message). Both failures
    are returned rather than collapsed to a bool because they are different
    failures at the same call site — #2198 is what an absent distinction costs.
    Callers own the consequence, which is the half that legitimately differs:
    a restore inside a ``finally`` reports, one inside an ``except`` raises.
    """
    try:
        restored = await restore()
    except _COMMISSION_OPERATION_ERRORS as exc:
        return False, str(exc)
    return restored is True, None


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dict_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def request_missing_software_guards(
    topology: OutputTopology,
) -> tuple[OutputTopology, bool]:
    """Return intent with commissioning's required guard requests applied."""

    updated = topology
    changed = False
    for group in topology.speaker_groups:
        if not str(group.mode or "").startswith("active_"):
            continue
        for channel in group.channels:
            if not channel.protection_required:
                continue
            if channel.protection_status in {"present", "software_guard_requested"}:
                continue
            updated = set_channel_protection_status(
                updated,
                speaker_group_id=group.id,
                role=channel.role,
                protection_status="software_guard_requested",
            )
            changed = True
    return updated, changed


def ensure_missing_software_guards() -> tuple[OutputTopology, bool]:
    """Fresh-read and persist missing protection requests transactionally."""

    with output_topology_mutation() as mutation:
        topology = mutation.snapshot().topology
        updated, changed = request_missing_software_guards(topology)
        if changed:
            mutation.save(updated)
        return updated, changed


def _stage_startup_config(
    topology: OutputTopology,
    *,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if preset is None and crossover_preview is None:
        from jasper.active_speaker.crossover_preview import build_crossover_preview
        from jasper.active_speaker.design_draft import load_design_draft

        design_draft = load_design_draft()
        crossover_preview = build_crossover_preview(design_draft)
    return stage_protected_startup_config(
        topology,
        preset=preset,
        crossover_preview=crossover_preview,
    )


async def _load_startup_config(
    camilla_factory: CamillaFactory,
    *,
    path_safety_evidence_path: str | Path | None = None,
) -> dict[str, Any]:
    topology = load_output_topology()
    cam = camilla_factory()
    return await load_protected_startup_config(
        topology,
        load_config=lambda path: cam.set_config_file_path(path, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(best_effort=False),
        path_safety_evidence_path=path_safety_evidence_path
        or _path_safety_evidence_path(),
    )


def _path_safety_evidence_path() -> str | None:
    from jasper.active_speaker.path_safety import path_safety_evidence_path

    evidence_path = os.environ.get("JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE")
    if evidence_path and evidence_path.strip():
        return evidence_path.strip()
    default_path = path_safety_evidence_path()
    return str(default_path) if default_path.exists() else None


def commission_startup_anchor_not_staged_issue() -> dict[str, str]:
    return _issue(
        "commission_startup_anchor_not_staged",
        "could not stage the silent active-speaker setup before driver testing",
    )


def commission_startup_anchor_path_safety_blocked_issue() -> dict[str, str]:
    return _issue(
        "commission_startup_anchor_path_safety_blocked",
        "could not verify the silent active-speaker setup path before driver testing",
    )


def commission_startup_anchor_load_failed_issue() -> dict[str, str]:
    return _issue(
        "commission_startup_anchor_load_failed",
        "could not load the silent active-speaker setup before driver testing",
    )


def _blocked_startup_anchor(
    *,
    group: str,
    role: str,
    issue: dict[str, str],
    startup_setup: dict[str, Any],
    extra_issues: tuple[dict[str, str], ...] = (),
) -> dict[str, Any]:
    return {
        "status": "blocked",
        "startup_setup": startup_setup,
        "preflight": None,
        "load": {
            "status": "blocked",
            "last_action": "startup_anchor_blocked",
            "target": {"speaker_group_id": group, "role": role},
            "issues": [issue, *extra_issues],
        },
    }


async def _ensure_commission_startup_anchor(
    *,
    group: str,
    role: str,
    staged_config: dict[str, Any],
    current_config_path: str | None,
    camilla_factory: CamillaFactory,
    preset: Any = None,
    crossover_preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ensure commissioning has the silent startup graph as rollback anchor."""

    if preset is not None and crossover_preview is not None:
        raise ValueError(
            "commissioning startup anchor requires one resolved graph source"
        )
    staged_path = (staged_config.get("config") or {}).get("path")

    # A PATH MATCH IS NOT AN ANCHOR MATCH, and this second term is why. The
    # staged pair's metadata records the topology it was built for, so a box
    # whose saved topology has moved since — a DAC swap, a role edit — can hold
    # a staged graph at the very path this check is about to accept, describing
    # hardware the box no longer has. Reusing it would anchor a commissioning
    # rollback to a graph for the wrong speaker.
    #
    # A mismatch is NOT a refusal — it falls through to the re-stage below,
    # which rebuilds the pair against the topology the box actually has. The log
    # line is what makes the re-stage attributable rather than silent.
    topology = load_output_topology()
    staged_topology = staged_topology_match_status(topology, staged_config)
    paths_match = same_config_file(current_config_path, staged_path)
    if paths_match and bool(staged_topology.get("matched")):
        return {"status": "already_loaded", "staged_config_path": staged_path}
    if paths_match:
        log_event(
            logger,
            "active_speaker.web_commission_startup_anchor",
            action="startup_anchor",
            group=group,
            role=role,
            status="refresh_required",
            reason="staged_topology_mismatch",
        )

    topology, _guards_changed = ensure_missing_software_guards()
    stage = _stage_startup_config(
        topology,
        preset=preset,
        crossover_preview=crossover_preview,
    )
    if stage.get("status") != "staged":
        # #2184: forward the SPECIFIC stage failure(s) — ~8 distinct causes
        # (blocked preview, active_playback_device_required,
        # subwoofer_staging_unresolved, passive_main_output_unassigned,
        # software_tweeter_guard_incomplete, staged_config_generation_failed,
        # preset-bind issues, ...) each already mint their own code+message
        # via ``_issue`` inside ``stage_protected_startup_config``. Falling
        # back to the generic ``commission_startup_anchor_not_staged`` code
        # only when staging reported no issue at all keeps every existing
        # consumer's "did this stage?" branch working while the failure card
        # names the actual remedy instead of one generic sentence for all ~8.
        # A stage can report more than one blocker at once; every one of
        # them is forwarded (headline first) rather than only the first.
        stage_issues = _dict_items(stage.get("issues"))
        issue = (
            stage_issues[0] if stage_issues
            else commission_startup_anchor_not_staged_issue()
        )
        return _blocked_startup_anchor(
            group=group,
            role=role,
            issue=issue,
            extra_issues=tuple(stage_issues[1:]),
            startup_setup={"status": "blocked", "stage": stage},
        )

    staged = load_staged_startup_config()
    cam = camilla_factory()
    path, error = await read_current_config_path(cam)
    evidence_path = write_commission_path_safety(topology, staged, path, error)

    from jasper.active_speaker.path_safety import evaluate_path_safety_evidence

    try:
        report = evaluate_path_safety_evidence(
            json.loads(Path(evidence_path).read_text(encoding="utf-8"))
        )
    except _EVIDENCE_READ_ERRORS as exc:
        report = {"status": "blocked", "load_gate": "blocked", "error": str(exc)}
    if report.get("load_gate") != "ready":
        return _blocked_startup_anchor(
            group=group,
            role=role,
            issue=commission_startup_anchor_path_safety_blocked_issue(),
            startup_setup={"status": "blocked", "stage": stage, "path_safety": report},
        )

    startup_load = await _load_startup_config(
        camilla_factory,
        path_safety_evidence_path=evidence_path,
    )
    load_state = _dict_value(startup_load.get("load"))
    if load_state.get("status") != "loaded" or not load_state.get(
        "rollback_available"
    ):
        return _blocked_startup_anchor(
            group=group,
            role=role,
            issue=commission_startup_anchor_load_failed_issue(),
            startup_setup={
                "status": "blocked",
                "stage": stage,
                "path_safety": report,
                "startup_load": startup_load,
            },
        )

    return {
        "status": "loaded",
        "staged_config_path": _dict_value(stage.get("config")).get("path"),
        "path_safety_load_gate": report.get("load_gate"),
        "startup_load_status": load_state.get("status"),
        "rollback_available": bool(load_state.get("rollback_available")),
    }


def commission_status_payload() -> dict[str, Any]:
    """Return the active-speaker operator measurement state."""

    return {
        "commission_load": load_commission_load_state(),
        "ramp": load_ramp_state(),
        "safe_playback": load_safe_playback_state(),
    }
