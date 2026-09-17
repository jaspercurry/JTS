# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Output-hardware, topology and active-speaker commissioning payloads.

:mod:`jasper.web.sound_setup` owns the HTTP surface and imports the builders
here.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

if TYPE_CHECKING:
    from jasper.active_speaker.crossover_declaration import CrossoverGeometry

from jasper.active_speaker import commissioning_coordinator, design_draft as design_draft_store
from jasper.active_speaker.installation import installation_view
from jasper.active_speaker.rear_calibration import RearCalibrationError, diagnostic_seed, read_rear_calibration
from jasper.active_speaker.tuning_handoff import PROGRAM_ENTRIES, build_tuning_handoff
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
from jasper.log_event import log_event
from jasper.platform import wire
from jasper.output_topology import (
    OutputHardware,
    OutputTopology,
    OutputTopologyError,
    channel_identity_report,
    clock_domain_report,
    composite_serial_repin_plan,
    declared_hardware_mismatch,
    load_output_topology,
    load_output_topology_snapshot,
    new_topology_draft,
    output_topology_mutation,
    repin_composite_child_serials,
    set_channel_identity_verified,
)
from jasper.output_hardware import (
    OutputHardwareState,
    detected_hardware_adoption_precondition,
    load_state as load_output_hardware_state,
    topology_hardware_from_state,
)
from jasper.active_speaker.commission_wiring import (
    commission_seams,
)

from jasper.active_speaker.web_commissioning import (
    _commission_tone_mux_command,
    ensure_missing_software_guards,
    request_missing_software_guards as _request_missing_software_guards,
)

from ._common import refusal_envelope
from .sound_profile_apply import _sound_state_write_lock

logger = logging.getLogger(__name__)


I2S_HAT_REBOOT_REQUIRED_PATH = "/run/jasper-output-hardware/i2s-hat-reboot-required"
I2S_HAT_RECONCILE_UNIT = "jasper-audio-hardware-reconcile.service"


class OutputTopologyRevisionConflict(ValueError):
    """Raised when a browser posts a topology based on stale saved state."""


class OutputHardwareRequestConflict(ValueError):
    """A detected-hardware action no longer names the state it was offered for.

    Raised by both hardware-mismatch actions — the full reset and the
    same-shape re-pin — when the saved topology or the reconciler-owned
    hardware observation moved between rendering the offer and clicking it.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(
            "Speaker setup or detected hardware changed. Review it and try again."
        )


class OutputTopologyCapabilityBlocked(ValueError):
    """Raised when a posted layout needs hardware this DAC does not have.

    A ``ValueError`` so the POST dispatcher's existing validation branch returns
    it as ``{"error": ...}`` with 400 — the shape the page already renders as a
    layout error while keeping the operator's unsaved draft on screen.
    """


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


def _i2s_hat_collision_warnings(
    profile_id: str | None, boot_config_path: str | Path, *, detected: bool
) -> list[str]:
    """Re-derive (read-only) whether applying ``profile_id`` would collide.

    ``jasper-audio-hardware-reconcile`` owns the actual write and is the
    one place a collision gets refused; this recomputes the same pure
    check against the live config.txt purely to surface it in the wizard.
    """
    if profile_id is None:
        return []
    try:
        content = Path(boot_config_path).read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        _, _, collision = render_i2s_hat_boot_config(content, profile_id)
    except ValueError:
        return []
    if collision is None:
        return []
    remedy = (
        "Remove the hand-written line, then deploy or reboot."
        if detected
        else "Remove the existing line, then try again."
    )
    return [
        f"A hand-written dtoverlay={overlay} line is already in config.txt; "
        f"the {collision.managed_overlay} boot line was not written. {remedy}"
        for overlay in collision.colliding_overlays
    ]


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
        "warnings": _i2s_hat_collision_warnings(
            resolved_id, boot_config_path, detected=detected is not None
        ),
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
                timeout=55.0,
            )
        except (OSError, RuntimeError) as exc:
            result = {"ok": False, "error": str(exc)}
        payload = _i2s_hat_payload()
    outcome = "applied" if result.get("ok") else "error"
    log_event(logger, "sound.i2s_hat", result=outcome, desired=profile_id or "auto")
    return payload, result


def _output_topology_payload() -> dict[str, Any]:
    snapshot = load_output_topology_snapshot()
    topology = snapshot.topology
    observed_hardware = load_output_hardware_state()
    repin = composite_serial_repin_plan(topology, observed_hardware)

    return {
        "output_topology": topology.to_dict(include_evaluation=True),
        "topology_revision": snapshot.revision,
        "output_hardware": (
            observed_hardware.to_dict() if observed_hardware is not None else None
        ),
        "hardware_adoption": detected_hardware_adoption_precondition(
            observed_hardware
        ),
        "hardware_mismatch": declared_hardware_mismatch(topology, observed_hardware),
        "hardware_repin": repin.to_dict() if repin is not None else None,
        "i2s_hat": _i2s_hat_payload(),
        "channel_identity": channel_identity_report(topology),
        "clock_domain": clock_domain_report(topology),
        "active_playback_route": _active_speaker_playback_route_payload(topology),
    }


def _refuse_undrivable_layout(topology: OutputTopology) -> None:
    """Refuse a layout this box's DAC can never drive, before anything is saved.

    A roleful (crossover / protected / subwoofer) layout on a DAC that declares
    no active outputd lane leaves CamillaDSP playing into the active loopback
    lane while outputd captures the passive one: structurally silent with every
    daemon reporting healthy, and unrepairable downstream.
    """

    from jasper.active_speaker.playback_route import (
        ActiveLaneCapabilityGap,
        active_lane_capability_gap,
    )

    gap = active_lane_capability_gap(topology)
    # An unrecognized DAC profile is not proof the layout is undrivable — see
    # active_lane_capability_gap's docstring — so it must not block the save.
    if not isinstance(gap, ActiveLaneCapabilityGap):
        return
    log_event(
        logger,
        "sound.output_topology_save",
        level=logging.WARNING,
        result="blocked",
        reason="dac_no_active_lane",
        device_id=gap.device_id,
        topology_id=topology.topology_id,
    )
    raise OutputTopologyCapabilityBlocked(
        f"{gap.device_label} does not support the active speaker lane. Active "
        "crossover and subwoofer layouts need an active-capable DAC; choose a "
        "passive speaker layout for this hardware (passive sends full-range "
        "audio to every output — only safe when the speaker has its own "
        "built-in passive crossover), or attach an active-capable DAC."
    )


def _refuse_duplicate_physical_outputs(topology: OutputTopology) -> None:
    """Refuse a save that puts two drivers on the same DAC channel.

    The channel selector never disables an already-used output (a 3+
    channel group could not otherwise swap two drivers without parking one
    on a spare channel first), so this is the only gate against two
    channels sharing a physical_output_index. Reuses evaluate_output_topology's
    existing duplicate_physical_output blocker instead of re-deriving it.
    """

    duplicates = [
        issue for issue in topology.evaluation()["blockers"]
        if issue["code"] == "duplicate_physical_output"
    ]
    if duplicates:
        raise OutputTopologyError(duplicates[0]["message"])


def _save_output_topology_payload(
    raw: dict[str, Any],
    *,
    require_revision: bool = False,
) -> dict[str, Any]:
    """Replace saved speaker intent only after audio is proven parked."""

    from jasper.active_speaker.runtime_convergence import park_and_commit_topology
    from jasper.output_topology_runtime import RECONCILE_UNIT, trigger_reconcile

    def verify_revision(revision: str) -> None:
        if not require_revision:
            return
        expected_revision = str(raw.get("topology_revision") or "")
        if not expected_revision or expected_revision != revision:
            raise OutputTopologyRevisionConflict(
                "speaker layout changed in another session; refresh hardware before saving"
            )

    # One domain-owned transaction covers stale validation, park, durable
    # commit, and the synchronous reconcile request. A competing writer cannot
    # pass validation on the same revision or resurrect pre-reset state.
    with output_topology_mutation() as mutation:
        snapshot = mutation.snapshot()
        verify_revision(snapshot.revision)
        raw_topology = raw.get("output_topology", raw)
        topology = OutputTopology.from_mapping(raw_topology)
        _refuse_undrivable_layout(topology)
        _refuse_duplicate_physical_outputs(topology)
        topology, guards_changed = _request_missing_software_guards(topology)
        safe_stop = _active_speaker_stop_payload()
        def commit_topology() -> OutputTopology:
            mutation.save(topology)
            return topology

        runtime = park_and_commit_topology(snapshot.topology, commit_topology)
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
        software_guards_requested=str(guards_changed),
        runtime_convergence_ok=runtime.convergence.ok,
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


def _verified_detected_hardware(
    raw: Mapping[str, Any], *, revision: str
) -> OutputHardwareState | None:
    """Validate the browser's topology and detected-hardware snapshot.

    The reconciler owns the observed hardware file, so callers re-run this
    check after parking, before they commit. Returns the reconciler's
    observation whatever its adoption verdict; each action decides what it can
    do with it.
    """

    expected_revision = raw.get("topology_revision")
    expected_identity = raw.get("detected_hardware_identity")
    if not isinstance(expected_revision, str) or not expected_revision:
        raise ValueError("topology_revision is required")
    if not isinstance(expected_identity, str) or not expected_identity:
        raise ValueError("detected_hardware_identity is required")
    if expected_revision != revision:
        raise OutputHardwareRequestConflict("topology_changed")
    observed = load_output_hardware_state()
    adoption = detected_hardware_adoption_precondition(observed)
    if expected_identity != adoption["identity"]:
        raise OutputHardwareRequestConflict("detected_hardware_changed")
    return observed


def _reset_request_hardware(
    raw: Mapping[str, Any], *, revision: str
) -> OutputHardware | None:
    """Return the detected hardware a reset may adopt, or ``None``."""

    observed = _verified_detected_hardware(raw, revision=revision)
    if observed is None:
        return None
    if not detected_hardware_adoption_precondition(observed)["allowed"]:
        return None
    return OutputHardware.from_mapping(topology_hardware_from_state(observed))


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
        _reset_request_hardware(raw, revision=snapshot.revision)
        safe_stop = _active_speaker_stop_payload()
        setup_reset: dict[str, Any]
        saved_revision: str

        def commit_unconfigured() -> OutputTopology:
            nonlocal saved_revision, setup_reset
            detected_hardware = _reset_request_hardware(
                raw, revision=snapshot.revision
            )
            if detected_hardware is not None:
                after = new_topology_draft(hardware=detected_hardware)
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
    the reset's wipe would throw all of it away. The two things a swap does
    invalidate (per-lane identity for the replaced unit, and a drift
    measurement of two crystals that never ran together) are cleared by
    ``repin_composite_child_serials``.
    """

    from jasper.active_speaker.runtime_convergence import park_and_commit_topology
    from jasper.output_topology_runtime import trigger_reconcile

    if not isinstance(raw, Mapping):
        raise ValueError("re-pin request must be an object")

    with output_topology_mutation() as mutation:
        snapshot = mutation.snapshot()
        observed = _verified_detected_hardware(raw, revision=snapshot.revision)
        plan = composite_serial_repin_plan(snapshot.topology, observed)
        if plan is None:
            raise OutputHardwareRequestConflict("repin_unavailable")
        safe_stop = _active_speaker_stop_payload()
        saved_revision = ""

        def commit_repin() -> OutputTopology:
            # Re-read the reconciler's observation after parking: a dongle can
            # leave between the offer and the commit, and its identity token is
            # what proves this re-pin still names attached hardware.
            nonlocal saved_revision
            current = _verified_detected_hardware(raw, revision=snapshot.revision)
            after = repin_composite_child_serials(snapshot.topology, current)
            saved_revision = mutation.save(after)
            return after

        runtime = park_and_commit_topology(
            snapshot.topology,
            commit_repin,
            # The graph selector proves a graph legal for the saved SHAPE, which
            # a re-pin does not change — so it would happily resume the approved
            # active runtime through DACs nobody has confirmed by ear yet. Stay
            # parked instead; the arm ladder's identity gates own the way back.
            stay_parked=True,
            parked_reason=(
                "parked after a DAC re-pin; confirm the re-pinned outputs and "
                "re-arm before audio resumes"
            ),
        )
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
                "Pinned the new DAC and kept your speaker setup. Confirm these "
                "outputs again: "
                + ", ".join(plan.reverify_output_labels)
                + ". Then re-run the drift measurement."
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
        reverify_outputs=len(plan.reverify_output_indexes),
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


def _active_speaker_playback_route_payload(
    topology: OutputTopology | None = None,
) -> dict[str, Any]:
    """Return the active-speaker runtime route capability for the saved topology."""

    from jasper.active_speaker.playback_route import active_playback_route_capability

    return active_playback_route_capability(
        topology or load_output_topology()
    ).to_dict()


def _active_speaker_channel_identity_payload() -> dict[str, Any]:
    """Return physical-channel identity evidence for the saved topology."""

    topology = load_output_topology()
    return {
        "channel_identity": channel_identity_report(topology),
        "clock_domain": clock_domain_report(topology),
    }


def _active_speaker_channel_identity_save_payload(
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Mark or clear a saved topology channel's physical identity evidence."""

    if not isinstance(raw, dict):
        raise ValueError("channel identity request must be an object")
    speaker_group_id = str(raw.get("speaker_group_id") or raw.get("group_id") or "")
    role = str(raw.get("role") or "")
    verified = raw.get("identity_verified")
    if not isinstance(verified, bool):
        raise ValueError("identity_verified must be a boolean")
    from jasper.active_speaker.runtime_contract import roleful_identity_confirmed
    from jasper.active_speaker.runtime_convergence import park_and_commit_topology

    with output_topology_mutation() as mutation:
        topology = mutation.snapshot().topology
        updated = set_channel_identity_verified(
            topology,
            speaker_group_id=speaker_group_id,
            role=role,
            identity_verified=verified,
            output_variant=str(raw.get("output_variant", "primary")),
        )

        # Un-confirming an ASSIGNED lane of a ROLEFUL topology declares doubt
        # about which driver hangs where — the hazard a DAC swap creates, self
        # declared. Gated on the confirmed -> unconfirmed EDGE: an already
        # unconfirmed box is already parked, and confirming never parks.
        park_needed = (
            roleful_identity_confirmed(topology)
            and not roleful_identity_confirmed(updated)
        )
        # ORDER IS THE SAFETY PROPERTY HERE. The DURABLE half — the cleared flag
        # that makes `roleful_identity_confirmed` refuse an approved graph on
        # every later pass — lands FIRST and unconditionally; the silence is
        # best-effort after it. `park_and_commit_topology` would invert that: it
        # parks BEFORE it commits, so a park failure would discard the declared
        # doubt and leave the lane verified on its approved graph across every
        # reboot — and a park most plausibly fails when the graph is already
        # unhealthy, exactly when the doubt matters most.
        #
        # (The re-pin endpoint does NOT share this shape and keeps its
        # commit-inside-park: a failed park there leaves the old serials pinned,
        # and the hardware mismatch keeps flagging.)
        mutation.save(updated)
        parked = False
        park_error: str | None = None
        if park_needed:
            try:
                park_and_commit_topology(
                    updated,
                    lambda: updated,
                    stay_parked=True,
                    parked_reason=(
                        "parked after an output was marked not confirmed; "
                        "confirm it again and re-arm before audio resumes"
                    ),
                )
                parked = True
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                park_error = f"{type(exc).__name__}: {exc}"
    report = channel_identity_report(updated)
    evaluation = updated.evaluation()
    log_event(
        logger,
        "sound.active_speaker_channel_identity",
        action="mark_verified" if verified else "clear_verified",
        topology_id=updated.topology_id,
        group_id=speaker_group_id,
        role=role,
        output_variant=str(raw.get("output_variant", "primary")),
        status=str(report.get("status")),
        verified="%d/%d"
        % (report.get("verified_channel_count"), report.get("assigned_channel_count")),
        blockers=len(evaluation.get("blockers") or []),
        park_needed=str(park_needed),
        parked=str(parked),
        park_error=park_error,
    )
    payload = _output_topology_payload()
    if park_needed:
        # Say which half actually landed: the doubt is recorded either way, but
        # only the immediate silence can fail, and the household must not be
        # left believing the speaker went quiet when it did not.
        payload["identity_park"] = {
            "parked": parked,
            "message": (
                "Marked not confirmed. The speaker is silent until you confirm "
                "it again and it re-arms."
                if parked
                else "Marked not confirmed, but JTS could not silence the "
                "speaker right now. It stays silent from the next restart. "
                "Open Status before playing anything loud."
            ),
        }
    return payload


def _active_speaker_environment_payload() -> dict[str, Any]:
    """Return read-only active-speaker readiness for the /sound/ advanced card."""

    from jasper.active_speaker.environment import probe_active_speaker_environment

    evidence_path = _active_speaker_path_safety_evidence_path()
    report = probe_active_speaker_environment(
        path_safety_evidence_path=evidence_path or None,
    )
    log_event(
        logger,
        "sound.active_speaker_environment",
        status=str(report.get("status")),
        load_gate=str(report.get("load_gate")),
        blockers=int(report.get("blocker_count") or 0),
        safe_playback=str(bool(report.get("safe_playback", {}).get("playback_allowed"))),
    )
    return report


def _active_speaker_path_safety_evidence_path() -> str | None:
    from jasper.active_speaker.path_safety import path_safety_evidence_path

    evidence_path = os.environ.get("JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE")
    if evidence_path and evidence_path.strip():
        return evidence_path.strip()
    default_path = path_safety_evidence_path()
    return str(default_path) if default_path.exists() else None


def _active_speaker_staged_config_payload() -> dict[str, Any]:
    """Return the latest protected startup config staging evidence."""

    from jasper.active_speaker.staging import load_staged_startup_config

    return load_staged_startup_config()


def _active_speaker_tone_backend_status(
    topology: Any | None = None,
) -> dict[str, Any]:
    """Return the explicit lab tone backend status."""

    from jasper.active_speaker.playback import tone_backend_status

    resolved_topology = topology or load_output_topology()
    status = tone_backend_status()
    return {
        **status,
        "default_pcm_source": "explicit_lab_pcm",
        "playback_device": status.get("test_pcm"),
        "channel_count": int(resolved_topology.hardware.physical_output_count or 0),
        "requires_protected_startup": True,
    }


def _active_speaker_safe_playback_payload() -> dict[str, Any]:
    """Return the current no-audio active-speaker safety session."""

    from jasper.active_speaker.safe_playback import load_safe_playback_state

    return load_safe_playback_state()


def _active_speaker_calibration_level_payload(
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return or update the backend-owned active-speaker test-volume state."""

    from jasper.active_speaker.calibration_level import (
        load_calibration_level_state,
        update_calibration_level_state,
    )
    from jasper.active_speaker.safe_playback import load_safe_playback_state

    if raw is None:
        return load_calibration_level_state()
    if not isinstance(raw, dict):
        raise ValueError("calibration level request must be an object")
    action = str(raw.get("action") or "set")
    level = raw.get("level_dbfs", raw.get("requested_level_dbfs"))
    # Bind the persisted level to the current commissioning run (the active
    # safe_playback session) so a previous session's test level cannot seed it.
    run_id = load_safe_playback_state().get("session_id")
    payload = update_calibration_level_state(
        action=action,
        requested_level_dbfs=level,
        observed_mic_dbfs=raw.get("observed_mic_dbfs"),
        mic_clipping=bool(raw.get("mic_clipping")),
        run_id=run_id,
    )
    log_event(
        logger,
        "sound.active_speaker_calibration_level",
        action=str(payload.get("last_action")),
        level_dbfs=str(payload.get("test_signal", {}).get("requested_level_dbfs")),
        prior_level_dbfs=str(payload.get("prior_level_dbfs")),
        delta_db=str(payload.get("applied_delta_db")),
        mic_status=str(payload.get("mic_meter", {}).get("status")),
        mic_recommendation=str(payload.get("mic_meter", {}).get("recommendation")),
        issues=len(payload.get("issues") or []),
    )
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


def _active_speaker_bringup_preflight_payload() -> dict[str, Any]:
    """Return guided-vs-manual active-speaker bring-up readiness."""

    from jasper.active_speaker.bringup import build_bringup_preflight

    topology = load_output_topology()
    environment_report = _active_speaker_environment_payload()
    safe_session = _active_speaker_safe_playback_payload()
    staged_config = _active_speaker_staged_config_payload()
    calibration_level = _active_speaker_calibration_level_payload()
    payload = build_bringup_preflight(
        topology,
        environment_report=environment_report,
        safe_session=safe_session,
        staged_config=staged_config,
        calibration_level=calibration_level,
        tone_backend=_active_speaker_tone_backend_status(topology),
    )
    log_event(
        logger,
        "sound.active_speaker_bringup_preflight",
        status=str(payload.get("status")),
        manual_available=str(bool(payload.get("manual_bringup_available"))),
        guided_available=str(bool(payload.get("guided_calibration_available"))),
        microphone=str(payload.get("microphone", {}).get("status")),
        guard=str(payload.get("software_guard", {}).get("status")),
    )
    return payload


def _active_speaker_startup_load_payload() -> dict[str, Any]:
    """Return startup load state plus current guarded preflight."""

    from jasper.active_speaker.startup_load import (
        build_startup_load_preflight,
        load_startup_load_state,
    )

    topology = load_output_topology()
    payload = {
        "state": load_startup_load_state(),
        "preflight": build_startup_load_preflight(
            topology,
            path_safety_evidence_path=_active_speaker_path_safety_evidence_path(),
        ),
    }
    log_event(
        logger,
        "sound.active_speaker_startup_load",
        status=str(payload["state"].get("status")),
        preflight=str(payload["preflight"].get("status")),
        rollback_available=str(bool(payload["state"].get("rollback_available"))),
    )
    return payload


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
    """Build the silent, target-bound research request and copyable prompt."""

    from jasper.active_speaker.driver_safety import build_driver_research_request
    from jasper.active_speaker.driver_safety_prompt import build_driver_research_prompt
    from jasper.active_speaker.design_draft import (
        normalise_manual_settings,
        normalise_operator_inputs,
    )

    if not isinstance(raw, dict):
        raise ValueError("driver research request must be an object")
    allowed = {"operator_inputs", "manual_settings"}
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(
            "driver research request has unknown fields: " + ", ".join(unknown)
        )
    topology = load_output_topology()
    operator_inputs = normalise_operator_inputs(raw.get("operator_inputs"))
    manual_settings = normalise_manual_settings(raw.get("manual_settings"))
    request = build_driver_research_request(
        topology,
        operator_inputs,
        manual_settings,
    )
    payload = {
        "request": request,
        "prompt": build_driver_research_prompt(request),
        "safety": {
            "no_audio": True,
            "loads_camilla": False,
            "applies_filters": False,
            "authorizes_playback": False,
            "research_is_advisory": True,
        },
    }
    log_event(
        logger,
        "sound.active_speaker_driver_research_request",
        topology_id=topology.topology_id,
        target_count=len(request.get("targets") or []),
        request_fingerprint=str(request.get("request_fingerprint")),
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
        "driver_research_request",
        "driver_research",
        "manual_settings",
        "operator_inputs",
        "expected_revision",
    }
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(
            "design draft request has unknown fields: " + ", ".join(unknown)
        )
    if "expected_revision" not in raw:
        raise ValueError("design draft request requires expected_revision")
    expected_revision = raw.get("expected_revision")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise ValueError("expected_revision must be a non-negative integer")
    topology, _guards_changed = ensure_missing_software_guards()
    payload = save_design_draft(
        topology,
        driver_research_request=raw.get("driver_research_request"),
        driver_research=raw.get("driver_research"),
        manual_settings=raw.get("manual_settings"),
        operator_inputs=raw.get("operator_inputs"),
        expected_revision=expected_revision,
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
        safety_profile_status=str(
            (payload.get("driver_safety_profile") or {}).get("status")
        ),
        # #2603: the EVALUATION, not just the stored status, so a box whose
        # profile went un-confirmed is findable in the journal. Carried on this
        # save event rather than a new one — no second grep contract.
        safety_profile_evaluation=str(
            (payload.get("driver_safety_profile_evaluation") or {}).get("status")
        ),
        safety_profile_reasons=",".join(
            str(reason)
            for reason in (
                (payload.get("driver_safety_profile_evaluation") or {}).get("reasons")
                or ()
            )
        ),
        issues=len(payload.get("issues") or []),
    )
    return installation_view(payload)


def apply_measured_crossover_geometry(
    *, expected_revision: int, between_roles: tuple[str, str],
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
    measured at a different corner. The compare-and-swap covers all three for
    the same reason: it defends "Sound still says what this review measured".
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
        "expected_revision": expected_revision,
        "driver_research_request": draft.get("driver_research_request"),
        "driver_research": draft.get("driver_research"),
        "manual_settings": manual_settings_for_crossover(draft, between_roles, selected),
        "operator_inputs": draft.get("operator_inputs"),
    }, durable=True)


def _active_speaker_crossover_preview_payload() -> dict[str, Any]:
    """Return the saved no-audio crossover preview, if any."""

    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft

    payload = load_crossover_preview(current_design_draft=load_design_draft())
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


def _active_speaker_crossover_preview_save_payload() -> dict[str, Any]:
    """Persist a no-audio crossover preview from the saved design draft."""

    from jasper.active_speaker.crossover_preview import save_crossover_preview
    from jasper.active_speaker.design_draft import build_design_draft, load_design_draft

    draft = load_design_draft()
    if draft.get("status") not in {"not_saved", "unreadable"}:
        saved_revision = draft.get("revision", 0)
        topology, _guards_changed = ensure_missing_software_guards()
        draft = build_design_draft(
            topology,
            driver_research_request=draft.get("driver_research_request"),
            driver_research=draft.get("driver_research"),
            manual_settings=draft.get("manual_settings"),
            operator_inputs=draft.get("operator_inputs"),
            created_at=draft.get("created_at"),
            updated_at=draft.get("updated_at"),
        )
        draft["revision"] = saved_revision
    payload = save_crossover_preview(draft)
    log_event(
        logger,
        "sound.active_speaker_crossover_preview_save",
        status=str(payload.get("status")),
        topology_id=str((payload.get("source") or {}).get("topology_id")),
        active_crossover_count=str(
            (payload.get("summary") or {}).get("active_crossover_count")
        ),
        blocker_count=str((payload.get("summary") or {}).get("blocker_count")),
    )
    return payload


def _active_speaker_restore_auto_source(*, reason: str) -> dict[str, Any]:
    """Best-effort return from setup-only routing to normal latest-source-wins."""

    try:
        payload = _commission_tone_mux_command(wire.MUX_AUTO)
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
    """Read-only commission-load + ramp + per-driver floor state for the card.

    Deliberately calls NO preflight (which would emit the candidate YAML) — a
    pure read. The arm/step that run the preflight are POST-only.
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
    recent = latest_banked_rounds(identity) if identity is not None else {}
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
        summed="%s/%s"
        % (
            summary.get("validated_summed_group_count"),
            summary.get("required_summed_group_count"),
        ),
    )
    return payload


def _active_speaker_baseline_profile_payload(
    *,
    write: bool = False,
    design_draft: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from jasper.active_speaker.baseline_profile import compile_commissioning_profile  # lazy: graph compilation imports NumPy

    from .correction_crossover_v2_status import rollback_candidate, v2state  # lazy: status imports commissioning state

    _, payload = compile_commissioning_profile(design_draft=design_draft, write=write)
    payload["previous_candidate_fingerprint"] = rollback_candidate(v2state.load_v2_state())
    log_event(
        logger,
        "sound.active_speaker_baseline_profile",
        action="compile" if write else "status",
        status=str(payload.get("status")),
        may_apply=str(bool((payload.get("permissions") or {}).get("may_apply"))),
        issue_count=len(payload.get("issues") or []),
        config=str((payload.get("config") or {}).get("basename")),
    )
    return {**payload, "tuning_programs": PROGRAM_ENTRIES}


async def _active_speaker_baseline_profile_apply_payload(
    *,
    expected_candidate_fingerprint: str,
    on_candidate_verified: Callable[[], Awaitable[None]] | None = None,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Apply the active-speaker baseline profile through DSP apply."""

    from .correction_crossover_v2_apply import apply_candidate  # lazy: graph compilation imports NumPy

    payload = await apply_candidate(
        camilla_factory=camilla_factory,
        expected_candidate_fingerprint=expected_candidate_fingerprint,
        on_candidate_verified=on_candidate_verified,
    )
    if payload.get("status") == "applied":
        payload["source_selection_restore"] = _active_speaker_restore_auto_source(
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
    expected_candidate_fingerprint: str,
    camilla_factory: Callable[[], Any],
) -> dict[str, Any]:
    """Backend-owned final handoff from commissioning to the active profile.

    The browser expresses one intent — make the checked crossover the normal
    active speaker profile — and the backend owns the whole
    compile/validate/load/confirm sequence, so the UI cannot wedge itself
    between "saved" and "applied".
    """

    from jasper.active_speaker.baseline_profile import reviewed_candidate_refusal  # lazy: baseline readers import wizard state

    reviewed = _active_speaker_baseline_profile_payload(write=False)
    refusal = reviewed_candidate_refusal(reviewed, expected_candidate_fingerprint)
    if refusal:
        return {**refusal, "commissioning_cleanup": {"status": "not_attempted"}}

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
        expected_candidate_fingerprint=expected_candidate_fingerprint,
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
    baseline (``--base saved``); never applies it. Mirrors
    ``jasper-crossover-prescriber compose`` in-process, never shelling out."""

    from jasper.active_speaker.baseline_profile import (  # lazy: graph compilation imports NumPy
        _rear_calibration_issues,
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.candidate_bank import (  # lazy: graph compilation imports NumPy
        BankedCandidate,
        CandidateBankRefusal,
        publish_authored_candidate,
    )
    from jasper.active_speaker.candidate_parts import candidate_from_applied_profile  # lazy: graph compilation imports NumPy
    from jasper.active_speaker.crossover_v2.prescription_document import (  # lazy: graph compilation imports NumPy
        PrescriptionDocumentRefused,
        judge_prescription_document,
    )
    from jasper.active_speaker.crossover_v2.round_inputs import CrossoverEvidencePacketError  # lazy: graph compilation imports NumPy
    from jasper.active_speaker.measured_crossover_candidate import MeasuredCrossoverCandidateError  # lazy: graph compilation imports NumPy
    from jasper.active_speaker.state_paths import baseline_profile_state_path  # lazy: graph compilation imports NumPy
    from jasper.output_topology import load_output_topology_strict  # lazy: graph compilation imports NumPy

    document = {
        "kind": "jts_prescription", "schema": 1, "base": "saved",
        "sections": {"rear_calibration": raw if isinstance(raw, dict) else {}},
        "rationale": "Bank a cardioid rear calibration edited in the wizard.",
    }
    try:
        saved = candidate_from_applied_profile(
            load_output_topology_strict(), load_applied_baseline_profile_state() or {},
        )
        base = BankedCandidate(saved, "", "", baseline_profile_state_path())
        candidate = judge_prescription_document(document, base=base)
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
        "issues": _rear_calibration_issues(published.candidate),
    }
