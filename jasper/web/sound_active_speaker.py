# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Output-hardware, topology and active-speaker commissioning payloads.

:mod:`jasper.web.sound_setup` owns the HTTP surface and imports the builders
here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

if TYPE_CHECKING:
    from jasper.active_speaker.crossover_declaration import CrossoverGeometry
    from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidate

from jasper.active_speaker import commissioning_coordinator, design_draft as design_draft_store
from jasper.active_speaker.driver_safety import build_driver_research_context
from jasper.active_speaker.driver_safety_prompt import build_driver_research_prompt
from jasper.active_speaker.installation import installation_view
from jasper.active_speaker.playback_route import (
    ActiveLaneCapabilityGap, UnrecognizedDacProfile,
    active_lane_capability_gap, active_playback_route_capability,
)
from jasper.active_speaker.rear_calibration import RearCalibrationError, diagnostic_seed, read_rear_calibration
from jasper.active_speaker.state_paths import baseline_profile_state_path
from jasper.active_speaker.tuning_handoff import build_tuning_handoff
from jasper.active_speaker.measurement_programs import program_entries
from jasper.camilla_config_contract import DEFAULT_SAMPLE_RATE

from jasper.audio_hardware.config_txt import DEFAULT_BOOT_CONFIG_PATH
from jasper.audio_hardware.hat_eeprom import DEFAULT_HAT_DIR
from jasper.audio_hardware.i2s_hat import (
    DEFAULT_I2S_HAT_INTENT_PATH,
    detected_i2s_hat_profile,
    read_i2s_hat_intent,
    render_i2s_hat_boot_config,
    selectable_i2s_hat_profiles,
    write_i2s_hat_intent,
)
from jasper.active_speaker.audition import AUDITION_LAYER_REAR_COMPARE, audition_summary
from jasper.active_speaker.rear_compare import rear_compare_level
from ._common import bonded_follower_active
from jasper.log_event import log_event
from jasper.platform import wire
from jasper.platform.uds import mux_socket_command
from jasper.output_topology import (
    OutputHardware,
    OutputTopology,
    OutputTopologyError,
    clock_domain_report,
    composite_serial_repin_plan,
    declared_hardware_mismatch,
    load_output_topology,
    load_output_topology_snapshot,
    new_topology_draft,
    output_topology_mutation,
    repin_composite_child_serials,
)
from jasper.output_hardware import (
    detected_hardware_adoption_precondition,
    load_state as load_output_hardware_state,
    topology_hardware_from_state,
)
from jasper.output_topology_runtime import trigger_reconcile
from jasper.active_speaker.commission_wiring import (
    commission_seams,
)

from ._common import refusal_envelope
from .sound_profile_apply import _sound_state_write_lock

logger = logging.getLogger(__name__)


I2S_HAT_REBOOT_REQUIRED_PATH = "/run/jasper-output-hardware/i2s-hat-reboot-required"
I2S_HAT_RECONCILE_UNIT = "jasper-audio-hardware-reconcile.service"


class OutputHardwareRequestConflict(ValueError):
    """The attached hardware cannot be re-pinned."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(
            "No matching hardware to re-pin. Refresh the hardware view and review."
        )


class OutputTopologyCapabilityBlocked(ValueError):
    """Raised when a posted layout needs hardware this DAC does not have.

    A ValueError so the POST dispatcher's validation branch returns it as
    ``{"error": ...}`` with 400 — the shape the page renders as a layout error
    while keeping the operator's unsaved draft on screen.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _output_hardware_dict() -> dict[str, Any] | None:
    """Serializable form of the live output-hardware state, or ``None``.

    These payloads are emitted with plain ``json.dumps``, which cannot encode
    the frozen ``OutputHardwareState``; ``to_dict`` is the single conversion
    boundary. The page keeps this envelope key separate from the topology's own
    ``hardware`` block: topology hardware is the saved speaker contract, this is
    the currently observed attachment state (also mirrored by ``/state`` as
    ``audio.output_hardware``).
    """
    hardware = load_output_hardware_state()
    return hardware.to_dict() if hardware is not None else None


def _i2s_hat_payload(
    *,
    intent_path: str | Path = DEFAULT_I2S_HAT_INTENT_PATH,
    boot_config_path: str | Path = DEFAULT_BOOT_CONFIG_PATH,
    hat_dir: str | Path = DEFAULT_HAT_DIR,
) -> dict[str, Any]:
    profiles = selectable_i2s_hat_profiles()
    hardware = _output_hardware_dict() or {}
    topology = str(
        (hardware.get("usb_data_role") or {}).get("board_topology") or "unknown"
    )
    available = topology in {"shared_otg_port", "separate_host_ports"}
    reason = ""
    if not available:
        reason = "I²S HAT setup requires a recognized Raspberry Pi."
    intent_error = ""
    try:
        desired_profile_id = read_i2s_hat_intent(intent_path)
    except (OSError, UnicodeError, ValueError) as exc:
        desired_profile_id = None
        intent_error = str(exc)
    # A HAT that names itself in its EEPROM is reconciled without the operator
    # choosing anything, so the wizard reports it instead of offering it. On a
    # board the reconciler will not manage, it reports nothing.
    detected = detected_i2s_hat_profile(hat_dir) if available else None
    resolved_id = detected.id if detected is not None else desired_profile_id
    warnings = []
    if available and resolved_id is not None:
        try:
            _, _, collision = render_i2s_hat_boot_config(
                Path(boot_config_path).read_text(encoding="utf-8"), resolved_id
            )
        except (OSError, ValueError):
            collision = None
        if collision is not None:
            remedy = "deploy or reboot" if detected is not None else "save again"
            warnings = [
                f"A hand-written dtoverlay={overlay} line blocks the "
                f"{collision.managed_overlay} boot line. "
                f"Remove the hand-written line from config.txt, then {remedy}."
                for overlay in collision.colliding_overlays
            ]
    return {
        "visibility": "visible",
        "available": available,
        "shared_usb_data_port": topology == "shared_otg_port",
        "reason": reason,
        "intent_error": intent_error,
        "profiles": [{"id": p.id, "label": p.label} for p in profiles],
        "desired_profile_id": desired_profile_id,
        "detected_profile_id": detected.id if detected is not None else None,
        "detected_label": detected.label if detected is not None else "",
        "warnings": warnings,
        "restart_required": Path(I2S_HAT_REBOOT_REQUIRED_PATH).is_file(),
    }


def _save_i2s_hat_payload(
    profile_id: str | None,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    from jasper.control.restart_broker import manage_units

    with _sound_state_write_lock:
        status = _i2s_hat_payload()
        if not status["available"]:
            raise ValueError(status["reason"] or "I²S HAT setup is unavailable")
        write_i2s_hat_intent(profile_id)
        try:
            result = manage_units(
                I2S_HAT_RECONCILE_UNIT,
                verb="start",
                reason="sound i2s hat setting",
                no_block=False,
                # Over the unit's TimeoutStartSec=50s, under the 65s
                # proxy_read_timeout on /sound/speaker/ and /sound/output/.
                timeout=55.0,
            )
        except (OSError, RuntimeError) as exc:
            result = {"ok": False, "error": str(exc)}
        payload = _i2s_hat_payload()
    outcome = "applied" if result.get("ok") else "error"
    log_event(logger, "sound.i2s_hat", result=outcome, desired=profile_id or "auto")
    return payload, result


def _output_topology_payload() -> dict[str, Any]:
    topology = load_output_topology_snapshot().topology
    observed_hardware = load_output_hardware_state()
    repin = composite_serial_repin_plan(topology, observed_hardware)

    return {
        "output_topology": topology.to_dict(include_evaluation=True),
        "output_hardware": (
            observed_hardware.to_dict() if observed_hardware is not None else None
        ),
        "hardware_adoption": detected_hardware_adoption_precondition(
            observed_hardware
        ),
        "hardware_mismatch": declared_hardware_mismatch(topology, observed_hardware),
        "hardware_repin": repin.to_dict() if repin is not None else None,
        "i2s_hat": _i2s_hat_payload(),
        "clock_domain": clock_domain_report(topology),
    }


def _refuse_undrivable_layout(topology: OutputTopology) -> None:
    """Refuse layouts outside the DAC's declared active route capacity."""
    gap = active_lane_capability_gap(topology)
    if isinstance(gap, UnrecognizedDacProfile):
        return
    route = active_playback_route_capability(topology)
    if isinstance(gap, ActiveLaneCapabilityGap):
        reason = "dac_no_active_lane"
        message = (
            f"{gap.device_label} does not support the active speaker lane. Active "
            "crossover and subwoofer layouts need an active-capable DAC; choose a "
            "passive speaker layout for this hardware (passive sends full-range "
            "audio to every output — only safe when the speaker has its own "
            "built-in passive crossover), or attach an active-capable DAC."
        )
    else:
        blocker = next((issue for issue in route.issues if issue["severity"] == "blocker"), None)
        if blocker is None:
            return
        reason, message = blocker["code"], blocker["message"]
    log_event(
        logger, "sound.output_topology_save", level=logging.WARNING,
        result="blocked", reason=reason, device_id=topology.hardware.device_id,
        topology_id=topology.topology_id,
        required_active_output_count=route.required_active_output_count,
        transport_channel_count=route.transport_channel_count,
        subwoofer_supported=route.subwoofer_supported,
    )
    raise OutputTopologyCapabilityBlocked(reason, message)


def _refuse_duplicate_physical_outputs(topology: OutputTopology) -> None:
    """The channel selector never disables an already-used output (a 3+ channel
    group could not otherwise swap two drivers without parking one on a spare
    channel first), so this is the only gate against two channels sharing a
    physical_output_index. Unassigned channels are a supported stored state:
    the page saves a draft at every card (#2145)."""

    blockers = [
        issue for issue in topology.evaluation()["blockers"]
        if issue["code"] == "duplicate_physical_output"
    ]
    if blockers:
        raise OutputTopologyError(blockers[0]["message"])


def _save_output_topology_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Save speaker intent, parking audio when the layout changes."""

    from jasper.active_speaker.runtime_convergence import park_and_commit_topology
    from jasper.output_topology_runtime import RECONCILE_UNIT, trigger_reconcile

    with output_topology_mutation() as mutation:
        snapshot = mutation.snapshot()
        raw_topology = raw.get("output_topology", raw)
        topology = OutputTopology.from_mapping(raw_topology)
        _refuse_duplicate_physical_outputs(topology)
        _refuse_undrivable_layout(topology)
        safe_stop = _active_speaker_stop_payload()
        def commit_topology() -> OutputTopology:
            mutation.save(topology)
            return topology

        runtime = park_and_commit_topology(
            snapshot.topology, commit_topology, replacement=topology,
        )
        reconcile = trigger_reconcile(reason="output_topology_save")
        if not reconcile.get("ok"):
            log_event(
                logger,
                "sound.output_topology_save_reconcile",
                level=logging.WARNING,
                unit=RECONCILE_UNIT,
                error=reconcile.get("error"),
                converging=reconcile.get("converging"),
            )
    evaluation = topology.evaluation()
    log_event(
        logger,
        "sound.output_topology_save",
        topology_id=topology.topology_id,
        status=evaluation["status"],
        device_id=topology.hardware.device_id,
        groups=len(topology.speaker_groups),
        assigned_outputs=evaluation["assigned_output_count"],
        blockers=len(evaluation["blockers"]),
        warnings=len(evaluation["warnings"]),
        runtime_convergence_ok=runtime.convergence.ok,
        live_applied=runtime.convergence.live_applied,
        parked=runtime.parked.live_applied,
        reconcile_ok=reconcile.get("ok"),
        reconcile_converging=reconcile.get("converging"),
        safe_stop=str(safe_stop.get("status")),
    )
    needs_attention_save = {
        "status": "needs_attention",
        "message": (
            "Speaker layout was saved, but audio remains off. "
            "Open Status before continuing."
        ),
    }
    if not runtime.convergence.ok:
        save = needs_attention_save
    elif reconcile.get("converging"):
        # The reconciler is still running past trigger_reconcile's own wait
        # budget (#3094) -- not a failure, so it must not read as one.
        save = {
            "status": "converging",
            "message": (
                "Speaker layout was saved and is still applying. "
                "Check Status in a moment."
            ),
        }
    elif not reconcile.get("ok"):
        save = needs_attention_save
    else:
        save = {"status": "saved", "message": "Saved speaker layout."}
    return {
        **_output_topology_payload(),
        "runtime_convergence": runtime.convergence.to_dict(),
        "reconcile": reconcile,
        "save": save,
    }


def _reset_output_topology_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Clear speaker setup to a silent unconfigured topology.

    One operation serves both the contextual detected-hardware action (hidden
    until the reconciler can name usable hardware) and the lower recovery
    control (may still clear stale setup with nothing usable attached).
    Neither path loads a flat graph.
    """

    from jasper.active_speaker.reset import clear_active_speaker_setup_state
    from jasper.active_speaker.runtime_convergence import park_and_commit_topology
    from jasper.output_topology_runtime import trigger_reconcile

    if not isinstance(raw, Mapping):
        raise ValueError("reset request must be an object")

    with output_topology_mutation() as mutation:
        snapshot = mutation.snapshot()
        safe_stop = _active_speaker_stop_payload()
        setup_reset: dict[str, Any]
        saved_revision: str

        def commit_unconfigured() -> OutputTopology:
            nonlocal saved_revision, setup_reset
            observed = load_output_hardware_state()
            if observed is not None and detected_hardware_adoption_precondition(
                observed
            )["allowed"]:
                after = new_topology_draft(hardware=OutputHardware.from_mapping(
                    topology_hardware_from_state(observed)
                ))
            else:
                # Recovery remains possible without attached hardware. Preserve
                # the last known DAC description only as topology metadata;
                # zero groups means unconfigured and runtime stays parked.
                after = new_topology_draft(hardware=snapshot.topology.hardware)
            saved_revision = mutation.save(after)
            try:
                setup_reset = clear_active_speaker_setup_state()
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                # Cleanup follows the durable intent write. It may require
                # attention, but it must never roll back that write or restore
                # the prior audible graph.
                setup_reset = {
                    "status": "partial",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            return after

        runtime = park_and_commit_topology(
            snapshot.topology,
            commit_unconfigured,
        )
        reconcile = trigger_reconcile(reason="output_topology_reset")
    adoption = detected_hardware_adoption_precondition(load_output_hardware_state())
    needs_attention_reset = {
        "status": "needs_attention",
        "message": (
            "Speaker setup was reset and audio is off. JTS could not finish "
            "setup cleanup; open Status before continuing."
        ),
    }
    if setup_reset.get("status") == "partial" or not runtime.convergence.ok:
        reset_result = needs_attention_reset
    elif reconcile.get("converging"):
        # The reconciler is still running past trigger_reconcile's own wait
        # budget (#3094) -- not a failure, so it must not read as one.
        reset_result = {
            "status": "converging",
            "message": (
                "Speaker setup was reset and is still applying. "
                "Check Status in a moment."
            ),
        }
    elif not reconcile.get("ok"):
        reset_result = needs_attention_reset
    else:
        reset_result = {
            "status": "reset",
            "message": "Speaker setup was reset. Audio is off until you choose a speaker layout.",
        }
    log_event(
        logger,
        "sound.output_topology_reset",
        result=reset_result["status"],
        topology_revision=saved_revision,
        hardware_ready=str(bool(adoption["allowed"])),
        cleanup_status=str(setup_reset.get("status")),
        cleanup_error=setup_reset.get("error"),
        runtime_convergence_ok=runtime.convergence.ok,
        reconcile_ok=str(bool(reconcile.get("ok"))),
        reconcile_converging=str(bool(reconcile.get("converging"))),
        safe_stop=str(safe_stop.get("status")),
    )
    payload = _output_topology_payload()
    payload["reset"] = reset_result
    payload["saved"] = True
    payload["runtime_convergence"] = runtime.convergence.to_dict()
    payload["reconcile"] = reconcile
    return payload


def _repin_output_topology_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Re-pin a same-shape composite onto the DAC units now attached.

    The narrow counterpart to the reset above. A speaker group, its roles, its
    physical-output assignment, and the crossover/commissioning design are all
    keyed to physical output INDEX, never to a DAC serial, so a replacement
    unit of the same kind in the same USB port invalidates none of them — while
    the reset's wipe would throw all of it away. The applied baseline record
    and the pair's drift measurement must be renewed after the swap.
    """

    from jasper.active_speaker.runtime_convergence import park_and_commit_topology
    from jasper.output_topology_runtime import trigger_reconcile

    if not isinstance(raw, Mapping):
        raise ValueError("re-pin request must be an object")

    with output_topology_mutation() as mutation:
        snapshot = mutation.snapshot()
        observed = load_output_hardware_state()
        plan = composite_serial_repin_plan(snapshot.topology, observed)
        if plan is None:
            raise OutputHardwareRequestConflict("repin_unavailable")
        safe_stop = _active_speaker_stop_payload()
        saved_revision = ""

        def commit_repin() -> OutputTopology:
            nonlocal saved_revision
            current = load_output_hardware_state()
            after = repin_composite_child_serials(snapshot.topology, current)
            saved_revision = mutation.save(after)
            return after

        runtime = park_and_commit_topology(
            snapshot.topology,
            commit_repin,
            stay_parked=True,
            parked_reason=(
                "parked after a DAC re-pin; Apply the baseline to resume audio"
            ),
        )
        baseline_profile_state_path().unlink(missing_ok=True)
        reconcile = trigger_reconcile(reason="output_topology_repin")
    needs_attention_repin = {
        "status": "needs_attention",
        "message": (
            "The new DAC was pinned and your speaker setup was kept, but audio "
            "remains off. Open Status before continuing."
        ),
    }
    if not runtime.convergence.ok:
        repin_result = needs_attention_repin
    elif reconcile.get("converging"):
        # The reconciler is still running past trigger_reconcile's own wait
        # budget (#3094) -- not a failure, so it must not read as one.
        repin_result = {
            "status": "converging",
            "message": (
                "Pinned the new DAC and kept your speaker setup. Audio is "
                "still applying. Check Status in a moment."
            ),
        }
    elif not reconcile.get("ok"):
        repin_result = needs_attention_repin
    else:
        repin_result = {
            "status": "repinned",
            "message": (
                "Pinned the new DAC and kept your speaker setup. Re-run the "
                "drift measurement, then Apply the baseline to resume audio."
            ),
        }
    log_event(
        logger,
        "sound.output_topology_repin",
        result=repin_result["status"],
        topology_revision=saved_revision,
        device_id=snapshot.topology.hardware.device_id,
        replaced_children=plan.replaced_child_count,
        child_count=plan.child_count,
        runtime_convergence_ok=runtime.convergence.ok,
        reconcile_ok=str(bool(reconcile.get("ok"))),
        reconcile_converging=str(bool(reconcile.get("converging"))),
        safe_stop=str(safe_stop.get("status")),
    )
    payload = _output_topology_payload()
    payload["repin"] = repin_result
    payload["saved"] = True
    payload["runtime_convergence"] = runtime.convergence.to_dict()
    payload["reconcile"] = reconcile
    return payload


def _active_speaker_stop_payload() -> dict[str, Any]:
    """Stop the no-audio safety session."""

    from jasper.active_speaker.calibration_level import update_calibration_level_state
    from jasper.active_speaker.playback import stop_tone_playback
    from jasper.active_speaker.safe_playback import stop_safe_playback_session

    playback = stop_tone_playback(reason="operator_stop")
    state = dict(stop_safe_playback_session())
    try:
        state["calibration_level"] = update_calibration_level_state(
            action="stop", run_id=state.get("session_id")
        )
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "sound.active_speaker_calibration_level",
            level=logging.WARNING,
            action="stop_reset",
            result="error",
            error=type(e).__name__,
        )
        state["calibration_level"] = {
            "status": "reset_failed",
            "error": str(e),
        }
    log_event(
        logger,
        "sound.active_speaker_safe_playback",
        action="stop",
        status=str(state.get("status")),
        session_id=str(state.get("session_id")),
        playback_status=str(playback.get("status")),
        audio_emitted=str(bool(playback.get("audio_emitted"))),
        level_status=str(state.get("calibration_level", {}).get("status")),
    )
    return state


def _active_speaker_tuning_handoff_payload(program_id: str = "speaker") -> dict[str, Any]:
    payload = build_tuning_handoff(
        commissioning_view=commissioning_coordinator.load_commissioning_view(),
        design_draft=design_draft_store.load_design_draft(),
        program_id=program_id,
    )
    log_event(
        logger,
        "sound.active_speaker_tuning_handoff",
        status=str(payload["status"]),
        reason=str(payload["reason"]),
        design_draft_revision=str(payload["binding"]["design_draft_revision"]),
    )
    return payload


def _active_speaker_design_draft_payload() -> dict[str, Any]:
    """Return the saved active-speaker design draft, if any."""

    from jasper.active_speaker.design_draft import load_design_draft

    payload = load_design_draft(topology=load_output_topology())
    log_event(
        logger,
        "sound.active_speaker_design_draft",
        status=str(payload.get("status")),
        driver_count=str((payload.get("summary") or {}).get("driver_count")),
        candidate_count=str(
            (payload.get("summary") or {}).get("crossover_candidate_count")
        ),
    )
    return installation_view(payload)


def _active_speaker_driver_research_request_payload(
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Return prompt text for the unsaved models and build notes."""

    if not isinstance(raw, dict):
        raise ValueError("driver research request must be an object")
    allowed = {"operator_inputs"}
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(
            "driver research request has unknown fields: " + ", ".join(unknown)
        )
    topology = load_output_topology()
    operator_inputs = design_draft_store.normalise_operator_inputs(raw.get("operator_inputs"))
    request = build_driver_research_context(
        topology,
        operator_inputs,
        design_draft_store.load_design_draft(topology=topology).get("manual_settings"),
    )
    payload = {
        "prompt": build_driver_research_prompt(request),
    }
    log_event(
        logger,
        "sound.active_speaker_driver_research_request",
        topology_id=topology.topology_id,
        target_count=len(request.get("targets") or []),
    )
    return payload


def _active_speaker_design_draft_save_payload(
    raw: dict[str, Any], *, durable: bool = False
) -> dict[str, Any]:
    """Persist a design draft from current topology plus bounded research JSON.

    ``durable`` is a caller-only knob (never read from ``raw``, so an HTTP
    body can't set it): the crossover-accept seam
    (:func:`apply_measured_crossover_geometry`) opts in, ordinary wizard
    edits keep the cheaper default.
    """

    from jasper.active_speaker.design_draft import save_design_draft

    if not isinstance(raw, dict):
        raise ValueError("design draft request must be an object")
    allowed = {
        "driver_research",
        "manual_settings",
        "operator_inputs",
    }
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(
            "design draft request has unknown fields: " + ", ".join(unknown)
        )
    topology = load_output_topology()
    payload = save_design_draft(
        topology,
        driver_research=raw.get("driver_research"),
        manual_settings=raw.get("manual_settings"),
        operator_inputs=raw.get("operator_inputs"),
        durable=durable,
    )
    log_event(
        logger,
        "sound.active_speaker_design_draft_save",
        status=str(payload.get("status")),
        topology_id=topology.topology_id,
        driver_count=str((payload.get("summary") or {}).get("driver_count")),
        candidate_count=str(
            (payload.get("summary") or {}).get("crossover_candidate_count")
        ),
        manual_driver_count=str(
            (payload.get("summary") or {}).get("manual_driver_count")
        ),
        manual_candidate_count=str(
            (payload.get("summary") or {}).get("manual_crossover_candidate_count")
        ),
        safety_profile_issues=",".join(issue["code"] for issue in
                                     (payload.get("driver_safety_profile") or {}).get("issues", [])),
        issues=len(payload.get("issues") or []),
    )
    return installation_view(payload)


def apply_measured_crossover_geometry(
    *, between_roles: tuple[str, str],
    configured: "CrossoverGeometry", selected: "CrossoverGeometry",
) -> dict[str, Any]:
    """Write a measured crossover onto the Sound declaration. Durable: every
    write through this function is fsynced before it is visible.

    The declaration states a crossover as three fields (corner, filter type,
    slope) and all three go through this one writer, in one write, one fsync
    and one Undo leg: ``baseline_profile``'s
    ``measured_candidate_preset_mismatch`` guard compares the speaker identity,
    crossover regions included, and slope compiles into
    ``CrossoverRegion.order``, so a candidate measured at a
    different slope is as unreconcilable with the saved declaration as one
    measured at a different corner.
    """
    from jasper.active_speaker.crossover_declaration import (
        declared_crossover_geometry,
        manual_settings_for_crossover,
    )
    from jasper.active_speaker.design_draft import load_design_draft

    draft = load_design_draft(topology=load_output_topology())
    current = declared_crossover_geometry(draft, between_roles)
    if current is None or not current.matches(configured):
        raise ValueError("Sound changed since this measurement; review afresh")
    return _active_speaker_design_draft_save_payload({
        "driver_research": draft.get("driver_research"),
        "manual_settings": manual_settings_for_crossover(draft, between_roles, selected),
        "operator_inputs": draft.get("operator_inputs"),
    }, durable=True)


def _active_speaker_crossover_preview_payload() -> dict[str, Any]:
    """Compute the no-audio crossover preview from the current design draft."""

    from jasper.active_speaker.crossover_preview import current_crossover_preview

    payload = current_crossover_preview()
    log_event(
        logger,
        "sound.active_speaker_crossover_preview",
        status=str(payload.get("status")),
        active_crossover_count=str(
            (payload.get("summary") or {}).get("active_crossover_count")
        ),
        blocker_count=str((payload.get("summary") or {}).get("blocker_count")),
    )
    return payload


async def _active_speaker_restore_auto_source(*, reason: str) -> dict[str, Any]:
    """Best-effort return from setup-only routing to normal latest-source-wins."""

    try:
        payload = await mux_socket_command(wire.MUX_AUTO)
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError) as exc:
        log_event(
            logger,
            "sound.active_speaker_source_auto",
            level=logging.WARNING,
            action="restore",
            reason=reason,
            status="failed",
            error=exc,
        )
        return {
            "status": "failed",
            "reason": reason,
            "error": str(exc),
        }
    log_event(
        logger,
        "sound.active_speaker_source_auto",
        action="restore",
        reason=reason,
        status="ok",
        mode=str(payload.get("mode")),
        active_source=str(payload.get("active_source")),
        test_source=str(payload.get("test_source")),
    )
    return {
        "status": "ok",
        "reason": reason,
        "state": payload,
    }


def _active_speaker_confirmed_driver_roles(
    topology: OutputTopology,
    *,
    group: str,
) -> list[str]:
    from jasper.active_speaker.measurement import confirmed_driver_roles

    if not group:
        return []
    return confirmed_driver_roles(topology, speaker_group_id=group)


async def _active_speaker_commission_ramp_abort_payload(
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Hard Stop: roll back to the all-muted staged config and reset the ramp."""

    from jasper.active_speaker.commission_ramp import abort_ramp

    cam = camilla_factory()
    load_config, _, _ = commission_seams(cam)
    payload = await abort_ramp(load_config=load_config)
    log_event(
        logger,
        "sound.active_speaker_commission",
        action="ramp_abort",
        status=str(payload.get("status")),
    )
    return payload


async def _active_speaker_commission_state_payload(
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Read commission-load, ramp and floor state for the commissioning view.

    Skip preflight because it writes candidate YAML.
    """

    from jasper.active_speaker.commission_ramp import (
        effective_confirmed_roles,
        load_ramp_state,
    )
    from jasper.active_speaker.safe_playback import load_safe_playback_state
    from jasper.active_speaker.commission_load import (
        commission_load_runtime_status,
        commission_load_state_with_runtime_status,
        load_commission_load_state,
    )

    commission = load_commission_load_state()
    if commission.get("status") == "loaded":
        try:
            running_raw = await camilla_factory().get_active_config_raw(
                best_effort=False
            )
        except Exception:  # noqa: BLE001 - status must fail closed, not crash the page.
            running_raw = None
        commission = commission_load_state_with_runtime_status(
            commission,
            commission_load_runtime_status(commission, running_raw),
        )
    ramp = load_ramp_state()
    target = commission.get("target") or {}
    group = str(
        target.get("speaker_group_id") or ramp.get("speaker_group_id") or ""
    ).strip()
    durable_confirmed: list[str] = []
    if group:
        topology = load_output_topology()
        durable_confirmed = _active_speaker_confirmed_driver_roles(
            topology,
            group=group,
        )
    quiet = load_safe_playback_state().get("quiet_start") or {}
    stale = commission.get("status") == "stale"
    pending = None if stale else ramp.get("pending")
    floor_status = quiet.get("status")
    if stale and floor_status == "floor_pending_operator":
        floor_status = "floor_required"
    return {
        "kind": "jts_active_speaker_commission_state",
        "commission_load": {
            "status": commission.get("status"),
            "target": commission.get("target") or {},
            "rollback_available": bool(commission.get("rollback_available")),
            "runtime_status": commission.get("runtime_status") or {},
            "issues": commission.get("issues") or [],
        },
        "ramp": {
            "confirmed_roles": effective_confirmed_roles(
                ramp,
                speaker_group_id=group,
                confirmed_roles=durable_confirmed,
            ),
            "pending": pending,
        },
        "floor": {
            "status": floor_status,
            "floor_audio_confirmed": bool(
                quiet.get("floor_audio_confirmed") and not stale
            ),
            "last_level_dbfs": None if stale else quiet.get("last_level_dbfs"),
            "last_operator_result": (
                {}
                if stale or not isinstance(quiet.get("last_operator_result"), dict)
                else quiet.get("last_operator_result")
            ),
        },
    }


async def _active_speaker_commissioning_view_payload(
    *,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Return the backend-owned active-speaker setup view model.

    State-loading and composition live in the shared
    ``commissioning_coordinator.load_commissioning_view``, which the crossover
    envelope consumes too. Only the ``commission`` runtime relay is built here,
    because it needs the async CamillaDSP runtime probe this caller owns.
    """

    from jasper.active_speaker.commissioning_coordinator import (
        load_commissioning_view,
    )

    commission = await _active_speaker_commission_state_payload(
        camilla_factory=camilla_factory,
    )
    view = load_commissioning_view(commission=commission)
    from jasper.active_speaker.applied_identity import applied_identity  # lazy: view-only bank lookup
    from jasper.active_speaker.baseline_profile import load_applied_baseline_profile_state  # lazy: view-only state
    from jasper.active_speaker.crossover_v2.round_inputs import latest_banked_rounds  # lazy: view-only bank lookup
    from jasper.active_speaker.timing_status import timing_status_lines  # lazy: view-only formatting
    applied = load_applied_baseline_profile_state()
    identity = applied_identity(applied)
    recent = latest_banked_rounds(identity, programs=("speaker",)) if identity is not None else {}
    view["timing"] = timing_status_lines(applied, recent.get("speaker"))
    log_event(
        logger,
        "sound.active_speaker_commissioning_view",
        status=str(view.get("status")),
        next_action=str((view.get("next_action") or {}).get("id")),
    )
    return view


def _active_speaker_measurements_payload() -> dict[str, Any]:
    """Return active-speaker measurement evidence for the saved topology."""

    from jasper.active_speaker.measurement import load_measurement_state

    topology = load_output_topology()
    payload = load_measurement_state(topology)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    log_event(
        logger,
        "sound.active_speaker_measurements",
        status=str(payload.get("status")),
        drivers="%s/%s"
        % (summary.get("captured_driver_count"), summary.get("required_driver_count")),
    )
    return payload


def _active_speaker_baseline_profile_payload(
    *,
    write: bool = False,
    design_draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from jasper.active_speaker.baseline_profile import compile_commissioning_profile, load_applied_baseline_profile_state  # lazy: graph compilation imports NumPy

    topology = load_output_topology()
    _, payload = compile_commissioning_profile(applied_profile=load_applied_baseline_profile_state(), topology=topology, design_draft=design_draft, write=write)
    log_event(
        logger,
        "sound.active_speaker_baseline_profile",
        action="compile" if write else "status",
        status=str(payload.get("status")),
        may_apply=str(bool((payload.get("permissions") or {}).get("may_apply"))),
        issue_count=len(payload.get("issues") or []),
        config=str((payload.get("config") or {}).get("basename")),
    )
    return {**payload, "tuning_programs": program_entries(topology)}


async def _active_speaker_baseline_profile_apply_payload(
    *,
    candidate: "MeasuredCrossoverCandidate | None" = None,
    on_candidate_verified: Callable[[], Awaitable[None]] | None = None,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Apply the active-speaker baseline profile through DSP apply."""

    from .correction_crossover_v2_apply import apply_candidate  # lazy: graph compilation imports NumPy

    payload = await apply_candidate(
        candidate,
        camilla_factory=camilla_factory,
        on_candidate_verified=on_candidate_verified,
    )
    if payload.get("status") == "applied":
        payload["reconcile"] = await asyncio.to_thread(trigger_reconcile, reason="baseline_apply")
        if not payload["reconcile"].get("ok"):
            issue = {"code": "output_route_not_ready", "message": "The configuration is saved, but the audio output is not ready. Try Save to speaker again."}
            return {**payload, "status": "needs_attention", "issues": [*payload.get("issues", []), issue]}
        payload["source_selection_restore"] = await _active_speaker_restore_auto_source(
            reason="baseline_apply",
        )
    log_event(
        logger,
        "sound.active_speaker_baseline_profile",
        action="apply",
        status=str(payload.get("status")),
        apply_result=str((payload.get("apply") or {}).get("result")),
        issue_count=len(payload.get("issues") or []),
        source_restore=str((payload.get("source_selection_restore") or {}).get("status")),
    )
    return payload


def _active_speaker_output_safety_from_config_path(
    config_path: str | os.PathLike[str] | None,
) -> dict[str, Any]:
    """Classify whether an applied config is still the safety-muted startup graph."""

    from jasper.active_speaker.staging import DEFAULT_STAGED_CONFIG_NAME

    path = str(config_path or "")
    safety_muted = os.path.basename(path) == DEFAULT_STAGED_CONFIG_NAME
    return {
        "safety_muted": safety_muted,
        "reason": "active_speaker_staged_startup" if safety_muted else None,
        "active_config_path": path or None,
    }


async def _active_speaker_finish_commissioning_payload(
    *,
    candidate: "MeasuredCrossoverCandidate | None" = None,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Backend-owned final handoff from commissioning to the active profile.

    The browser expresses one intent — make the checked crossover the normal
    active speaker profile — and the backend owns the whole
    compile/validate/load/confirm sequence, so the UI cannot wedge itself
    between "saved" and "applied".
    """

    commissioning_cleanup: dict[str, Any] = {"status": "not_attempted"}

    async def cleanup_after_locked_proof() -> None:
        nonlocal commissioning_cleanup
        try:
            from jasper.active_speaker.commission_ramp import load_ramp_state
            from jasper.active_speaker.commission_load import load_commission_load_state

            ramp_state = load_ramp_state()
            commission_load = load_commission_load_state()
            cleanup_needed = isinstance(ramp_state.get("pending"), dict) or (
                commission_load.get("status") == "loaded"
            )
            if cleanup_needed:
                ramp_cleanup = await _active_speaker_commission_ramp_abort_payload(
                    camilla_factory=camilla_factory,
                )
            else:
                ramp_cleanup = {
                    "status": "idle",
                    "ramp": ramp_state,
                    "commission_load": commission_load,
                }
        except (OSError, RuntimeError, ValueError) as exc:
            ramp_cleanup = {"status": "error", "error": str(exc)}
        commissioning_cleanup = {
            "ramp": ramp_cleanup,
        }

    payload = await _active_speaker_baseline_profile_apply_payload(
        candidate=candidate,
        on_candidate_verified=cleanup_after_locked_proof,
        camilla_factory=camilla_factory,
    )
    payload["commissioning_cleanup"] = commissioning_cleanup
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    apply_state = payload.get("apply") if isinstance(payload.get("apply"), dict) else {}
    config = profile.get("config") if isinstance(profile.get("config"), dict) else {}
    active_config_path = apply_state.get("active_config_path") or config.get("path")
    payload["output_safety"] = _active_speaker_output_safety_from_config_path(
        active_config_path
        if isinstance(active_config_path, (str, os.PathLike))
        else None
    )
    log_event(
        logger,
        "sound.active_speaker_finish_commissioning",
        status=str(payload.get("status")),
        apply_result=str((payload.get("apply") or {}).get("result")
            if isinstance(payload.get("apply"), dict)
        else None),
        safety_muted=str((payload.get("output_safety") or {}).get("safety_muted")),
        issue_count=len(payload.get("issues") or []),
    )
    return payload


# --- rear calibration (cardioid) wizard panel -------------------------------
#
# ADR-0318: a `jts_rear_calibration` document is authored data, not a form.
# These three routes seed a diagnostic starting document, validate a pasted
# document against the reader alone, and bank a candidate that carries it as
# a `jts_prescription` document's `rear_calibration` section on the applied
# baseline. None of the three apply anything — the page's existing apply flow
# adopts a banked fingerprint.


def _active_speaker_rear_calibration_seed_payload() -> dict[str, Any]:
    """Return the explicitly untuned, muted rear-calibration diagnostic seed."""

    return {"ok": True, "calibration": diagnostic_seed(DEFAULT_SAMPLE_RATE)}


def _rear_calibration_summary(document: Mapping[str, Any]) -> str:
    """One line describing a validated document, for the panel's status text."""

    if document["case"] == "acoustic_targets":
        return "acoustic targets, pending electrical fitting"
    return ("muted" if document["rear_muted"] else "unmuted") + " electrical rear stage"


def _active_speaker_rear_calibration_validate_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate a pasted rear-calibration document; never applies or banks it."""

    if not isinstance(raw, dict):
        return refusal_envelope(code="rear_calibration_invalid", message="calibration request must be an object")
    try:
        document = read_rear_calibration(raw, sample_rate=DEFAULT_SAMPLE_RATE)
    except RearCalibrationError as exc:
        return refusal_envelope(code="rear_calibration_invalid", message=str(exc))
    return {"ok": True, "case": document["case"], "summary": _rear_calibration_summary(document)}


def _active_speaker_rear_calibration_bank_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Bank a candidate carrying a pasted rear-calibration document on the applied
    baseline, through the same ``--base saved`` composer
    ``jasper-crossover-prescriber compose`` uses; never applies it."""

    from jasper.active_speaker.baseline_profile import rear_calibration_issues  # lazy: graph compilation imports NumPy
    from jasper.active_speaker.candidate_bank import (  # lazy: graph compilation imports NumPy
        CandidateBankRefusal,
        publish_authored_candidate,
    )
    from jasper.active_speaker.crossover_v2.prescription_document import (  # lazy: graph compilation imports NumPy
        PrescriptionDocumentRefused,
        bank_section,
    )
    from jasper.active_speaker.crossover_v2.round_inputs import CrossoverEvidencePacketError  # lazy: graph compilation imports NumPy
    from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidateError  # lazy: graph compilation imports NumPy

    try:
        candidate = bank_section(
            "rear_calibration", raw,
            rationale="Bank a cardioid rear calibration edited in the wizard.",
        )
        published = publish_authored_candidate(candidate)
    except PrescriptionDocumentRefused as exc:
        return exc.to_dict()
    except (CandidateBankRefusal, MeasuredCrossoverCandidateError) as exc:
        return PrescriptionDocumentRefused(exc.code, None, exc.detail).to_dict()
    except (CrossoverEvidencePacketError, OSError, ValueError) as exc:
        # Mirrors jasper-crossover-prescriber's --base saved block: a corrupt or
        # unreadable on-disk topology/applied-profile file fails closed as a
        # typed refusal instead of an unhandled exception reaching the client.
        return PrescriptionDocumentRefused("evidence_unreadable", None, str(exc)).to_dict()
    log_event(
        logger,
        "sound.active_speaker_rear_calibration_bank",
        candidate_fingerprint=published.fingerprint,
    )
    return {
        "ok": True,
        "candidate_fingerprint": published.fingerprint,
        "issues": rear_calibration_issues(published.candidate),
    }


def _cardioid_compare_payload(*, cached_only: bool = False) -> dict[str, Any]:
    from jasper.active_speaker.baseline_profile import applied_layer_names, load_applied_baseline_profile_state  # lazy: numpy startup cost

    applied = load_applied_baseline_profile_state()
    layers = applied_layer_names(applied)
    rear = (applied or {}).get("recomposition_snapshot", {}).get("rear_calibration", {})
    topology = load_output_topology()
    reason = ("follower" if bonded_follower_active() else
              "no_rear_output" if not any(c.output_variant == "rear"
                  for g in topology.speaker_groups for c in g.channels) else
              "no_applied_profile" if not applied else
              "no_rear_layer" if not layers["rear"] else
              "rear_muted_in_tune" if rear.get("rear_muted") else "")
    session = audition_summary()
    if session and session["layer"] != AUDITION_LAYER_REAR_COMPARE:
        session = None
    return {"available": not reason, "reason": reason,
            "state": session["state"] if session else "normal",
            "tune": {"label": "Current tune", "layers": [k for k, v in layers.items() if v],
                     "applied_at": (applied or {}).get("applied_at")},
            "level_match": rear_compare_level(cached_only=cached_only),
            "expires_in_s": session["expires_in_s"] if session else None}
