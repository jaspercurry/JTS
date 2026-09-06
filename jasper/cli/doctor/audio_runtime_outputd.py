# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""jasper-doctor checks for jasper-outputd, the final output owner.

Import direction across the audio-runtime check modules runs one way —
``audio_runtime_camilla`` -> ``_fanin`` -> ``_outputd`` -> ``_ring``, so this
module may not import from ``audio_runtime_ring``.

Closed vocabulary for this module's `CheckResult.reason`: one snake_case
constant per distinct decision branch of the checks below, its value unique
across the doctor and prefixed by the check that emits it. `detail` stays the
human sentence (free to reword); `reason` is what tests and self-healing
consumers pin instead (ADR-0233 rule 3).

A branch that formed NO verdict — subsystem not installed, not applicable to
this box, or the evidence source unreachable so nothing was observed — is
`skipped` with a reason, never `ok`. An `ok` reason means an actual verdict a
consumer would branch on (a feature the box turned off, a floor that is
deliberately not renderable).
"""
from __future__ import annotations

import os

from ...route_latency.status_socket import OUTPUTD_STALE_MS, OUTPUTD_STATUS_SOCKET
from ._evidence import evidence
from ._registry import doctor_check
from ._shared import (
    REASON_SYSTEMCTL_UNAVAILABLE,
    CheckResult,
    _service_state_failure,
)
from .audio_runtime_fanin import _assistant_gain_fault

REASON_OUTPUTD_UNIT_MISSING = "outputd_unit_missing"
REASON_OUTPUTD_UNIT_NOT_ENABLED = "outputd_unit_not_enabled"
REASON_OUTPUTD_INACTIVE = "outputd_inactive"
REASON_OUTPUTD_STATUS_UNREACHABLE = "outputd_status_unreachable"
REASON_OUTPUTD_STATUS_MALFORMED = "outputd_status_malformed"
REASON_OUTPUTD_STATUS_MISSING_CONTENT = "outputd_status_missing_content"
REASON_OUTPUTD_STATUS_MISSING_DAC = "outputd_status_missing_dac"
REASON_OUTPUTD_STATUS_MISSING_WATCHDOG = "outputd_status_missing_watchdog"
REASON_OUTPUTD_BACKEND_NOT_ALSA = "outputd_backend_not_alsa"
REASON_OUTPUTD_SAMPLE_RATE_UNEXPECTED = "outputd_sample_rate_unexpected"
REASON_OUTPUTD_PERIOD_FRAMES_MISSING = "outputd_period_frames_missing"
REASON_OUTPUTD_PROGRESS_STALE = "outputd_progress_stale"
REASON_OUTPUTD_TTS_OVER_BUDGET = "outputd_tts_over_budget"
REASON_OUTPUTD_XRUN_RATE_SUSTAINED = "outputd_xrun_rate_sustained"
REASON_OUTPUTD_CONTENT_SOURCE_MISMATCH = "outputd_content_source_mismatch"
REASON_OUTPUTD_TRANSPORT_ROUTE_UNPAIRED = "outputd_transport_route_unpaired"
REASON_OUTPUTD_TRANSPORT_EVIDENCE_UNKNOWN = "outputd_transport_evidence_unknown"
REASON_OUTPUTD_DAC_PCM_MISMATCH = "outputd_dac_pcm_mismatch"
REASON_OUTPUTD_REFERENCE_CONTRACT_MISSING = "outputd_reference_contract_missing"
REASON_OUTPUTD_REFERENCE_SOURCE_UNEXPECTED = "outputd_reference_source_unexpected"
REASON_OUTPUTD_CONTENT_BUFFER_UNDERSIZED = "outputd_content_buffer_undersized"
REASON_OUTPUTD_DAC_BUFFER_UNDERSIZED = "outputd_dac_buffer_undersized"
REASON_OUTPUTD_RING_CONTRACT_MISSING = "outputd_ring_contract_missing"
REASON_OUTPUTD_RING_SLOTS_INVALID = "outputd_ring_slots_invalid"
REASON_OUTPUTD_RING_SLOT_FRAMES_MISMATCH = "outputd_ring_slot_frames_mismatch"
REASON_OUTPUTD_RING_CAPACITY_INCOHERENT = "outputd_ring_capacity_incoherent"
REASON_OUTPUTD_RING_FORMAT_SHEAR = "outputd_ring_format_shear"
REASON_OUTPUTD_RING_CHANNELS_SHEAR = "outputd_ring_channels_shear"
REASON_OUTPUTD_DUAL_APPLE_STATUS_MISSING = "outputd_dual_apple_status_missing"
REASON_OUTPUTD_DUAL_APPLE_PCM_MISSING = "outputd_dual_apple_pcm_missing"
REASON_OUTPUTD_DUAL_APPLE_PCMS_IDENTICAL = "outputd_dual_apple_pcms_identical"
REASON_OUTPUTD_DUAL_APPLE_DELAY_EXCEEDED = "outputd_dual_apple_delay_exceeded"
REASON_OUTPUTD_DUAL_APPLE_NOT_LINKED = "outputd_dual_apple_not_linked"
REASON_OUTPUTD_ASSISTANT_GAIN_NOT_NUMERIC = "outputd_assistant_gain_not_numeric"
REASON_OUTPUTD_ASSISTANT_GAIN_OFF_CONTRACT = "outputd_assistant_gain_off_contract"

REASON_AEC_CLOCK_OUTPUTD_NOT_ENABLED = "aec_clock_outputd_not_enabled"
REASON_AEC_CLOCK_OUTPUTD_INACTIVE = "aec_clock_outputd_inactive"
REASON_AEC_CLOCK_STATUS_UNAVAILABLE = "aec_clock_status_unavailable"
REASON_AEC_CLOCK_REFERENCE_OUTPUTS_MISSING = "aec_clock_reference_outputs_missing"
REASON_AEC_CLOCK_CHIP_REF_NOT_CONFIGURED = "aec_clock_chip_ref_not_configured"
REASON_AEC_CLOCK_BLOCK_ABSENT = "aec_clock_block_absent"
REASON_AEC_CLOCK_CHIP_REF_UNAVAILABLE = "aec_clock_chip_ref_unavailable"
REASON_AEC_CLOCK_UNTRUSTED = "aec_clock_untrusted"

_OUTPUTD_EXPECTED_DAC_PCM = "outputd_dac"

_OUTPUTD_EXPECTED_DUAL_DAC_PCM = "dual_apple_usb_c_dac_4ch"

def _outputd_reconciled_env() -> dict[str, str]:
    """outputd's env as its own unit layers it, read once per doctor run.

    :func:`jasper.env_load.outputd_reconciled_env` plus the
    ``JASPER_OUTPUTD_ENV_FILE`` operator seam; nothing else.
    """

    def read() -> dict[str, str]:
        from ...env_load import outputd_reconciled_env

        return outputd_reconciled_env(
            os.environ.get("JASPER_OUTPUTD_ENV_FILE") or None
        )

    return evidence.get("outputd_reconciled_env", read)


def _outputd_active_channels_from_env(env: dict[str, str]) -> int | None:
    raw = str(env.get("JASPER_OUTPUTD_ACTIVE_CHANNELS") or "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if 2 <= value <= 8 else None


# outputd STATUS publishes xrun_rate_per_hour (count / uptime-hours) and
# last_xrun_age_ms (ms since the most recent xrun, null when none). The WARN
# keys on BOTH: a high rate alone can be a long-ago burst diluting as uptime
# grows, and a recent single xrun alone is a normal transient.
_OUTPUTD_XRUN_RATE_WARN_PER_HOUR = 6.0
_OUTPUTD_XRUN_RECENT_AGE_MS = 300_000  # 5 minutes


def _outputd_xrun_rate_warning(
    content: dict[str, object],
    dac: dict[str, object],
) -> str | None:
    """Return a one-clause WARN reason when either outputd lane shows a
    sustained xrun rate with a recent xrun, else None.

    Both sections are checked independently; the worst qualifying lane wins.
    """

    def _f(value: object) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    worst: tuple[float, str] | None = None
    for label, section in (("content", content), ("dac", dac)):
        if not isinstance(section, dict):
            continue
        rate = _f(section.get("xrun_rate_per_hour"))
        age = _f(section.get("last_xrun_age_ms"))  # null → None → no recent xrun
        if rate is None or age is None:
            continue
        if rate >= _OUTPUTD_XRUN_RATE_WARN_PER_HOUR and age <= _OUTPUTD_XRUN_RECENT_AGE_MS:
            reason = (
                f"{label} xrun_rate_per_hour={rate:.1f} "
                f"(last_xrun_age_ms={int(age)})"
            )
            if worst is None or rate > worst[0]:
                worst = (rate, reason)
    return worst[1] if worst else None


def _outputd_dual_apple_health(
    data: dict[str, object],
    *,
    sink_mode: object,
    active_single_alsa: bool,
    active_channels: int | None,
) -> tuple[str, str, str | None] | CheckResult:
    dual_detail = ""
    dual_warning: str | None = None
    active_detail = (
        f", active_channels={active_channels}"
        if active_single_alsa else ""
    )
    if sink_mode == "dual_apple":
        dual = data.get("dual_apple")
        if not isinstance(dual, dict):
            return CheckResult(
                "jasper-outputd",
                "fail",
                "STATUS missing dual_apple runtime health for dual sink",
                reason=REASON_OUTPUTD_DUAL_APPLE_STATUS_MISSING,
            )
        dual_a_pcm = dual.get("dac_a_pcm")
        dual_b_pcm = dual.get("dac_b_pcm")
        if not isinstance(dual_a_pcm, str) or not dual_a_pcm:
            return CheckResult(
                "jasper-outputd",
                "fail",
                "dual_apple.dac_a_pcm is missing",
                reason=REASON_OUTPUTD_DUAL_APPLE_PCM_MISSING,
            )
        if not isinstance(dual_b_pcm, str) or not dual_b_pcm:
            return CheckResult(
                "jasper-outputd",
                "fail",
                "dual_apple.dac_b_pcm is missing",
                reason=REASON_OUTPUTD_DUAL_APPLE_PCM_MISSING,
            )
        if dual_a_pcm == dual_b_pcm:
            return CheckResult(
                "jasper-outputd",
                "fail",
                "dual_apple DAC A/B PCMs are identical",
                reason=REASON_OUTPUTD_DUAL_APPLE_PCMS_IDENTICAL,
            )
        dual_linked = bool(dual.get("linked", False))
        delay_delta = dual.get("delay_delta_frames")
        delay_error = dual.get("delay_delta_error_frames")
        max_delay = dual.get("max_delay_delta_frames")
        if (
            isinstance(delay_error, int)
            and isinstance(max_delay, int)
            and delay_error > max_delay
        ):
            return CheckResult(
                "jasper-outputd",
                "fail",
                "dual_apple delay delta exceeds runtime budget: "
                f"error={delay_error} max={max_delay}",
                reason=REASON_OUTPUTD_DUAL_APPLE_DELAY_EXCEEDED,
            )
        if not dual_linked:
            dual_warning = "dual Apple PCMs are not ALSA-linked"
        dual_detail = (
            f", dual_a_pcm={dual_a_pcm}, dual_b_pcm={dual_b_pcm}, "
            f"dual_linked={dual_linked}, "
            f"dual_delay_delta_frames={delay_delta}, "
            f"dual_delay_delta_error_frames={delay_error}, "
            f"dual_max_delay_delta_frames={max_delay}"
        )
    return active_detail, dual_detail, dual_warning


def _outputd_status_payload() -> dict[str, object] | CheckResult:
    """Load and validate the STATUS transport envelope."""
    status = evidence.outputd_status()
    if status.unreachable:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"active but STATUS probe at {OUTPUTD_STATUS_SOCKET} failed: "
            f"{status.error}. Without STATUS doctor cannot verify DAC "
            "ownership, buffers, xruns, or work-loop progress.",
            reason=REASON_OUTPUTD_STATUS_UNREACHABLE, speaker_silent=True,
        )
    if status.payload is None:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"active but STATUS is unusable: {status.error}",
            reason=REASON_OUTPUTD_STATUS_MALFORMED,
        )
    return status.payload


def _outputd_content_bridge_detail(data: dict[str, object]) -> str:
    """Report outputd's resolved content source (``direct`` or ``shm_ring``)."""
    bridge = data.get("content_bridge")
    if not isinstance(bridge, dict):
        return "content_bridge=missing"
    mode = bridge.get("mode")
    if not isinstance(mode, str) or not mode:
        return "content_bridge=missing"
    return f"content_bridge={mode}"


def _outputd_loudness_health(data: dict[str, object]) -> str | CheckResult:
    """Validate optional assistant-loudness telemetry and render its detail."""
    loudness = data.get("assistant_loudness")
    if not isinstance(loudness, dict):
        return "assistant_loudness=fan-in-owned"
    decision_seen = bool(loudness.get("decision_seen", False))
    calibrated = bool(loudness.get("calibrated", False))
    final_gain = loudness.get("final_gain_db")
    content_anchor = loudness.get("content_anchor_lufs")
    if decision_seen and not isinstance(final_gain, (int, float)):
        return CheckResult(
            "jasper-outputd",
            "warn",
            "active but assistant_loudness.decision_seen=true without "
            "numeric final_gain_db.",
            reason=REASON_OUTPUTD_ASSISTANT_GAIN_NOT_NUMERIC,
        )
    gain_fault = _assistant_gain_fault(loudness)
    if gain_fault is not None:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but assistant_loudness.{gain_fault}.",
            reason=REASON_OUTPUTD_ASSISTANT_GAIN_OFF_CONTRACT,
        )
    return (
        f"assistant_loudness_decision={decision_seen}, "
        f"assistant_loudness_calibrated={calibrated}, "
        f"assistant_final_gain_db={final_gain}, "
        f"content_anchor_lufs={content_anchor}"
    )


def _outputd_buffer_health(
    data: dict[str, object],
    content: dict[str, object],
    *,
    content_hop: str,
    content_buffer: object,
    dac_buffer: object,
    period_frames: int,
) -> str | CheckResult:
    """Validate the content hop's buffer geometry and return ring detail.

    ``content_hop`` is the resolved transport shape's name
    (:data:`jasper.fanin_coupling.TRANSPORT_SHAPES`), which is what decides
    both branches below AND which ring's width the observed channels are held
    to — the ACTIVE shape reads the post-crossover per-driver ring. Taking the
    resolved shape rather than re-reading markers keeps one env read per check.
    """
    from jasper.fanin_coupling import (
        RING_TRANSPORT_SHAPES,
        TRANSPORT_DAC_CONTENT_RING,
        TRANSPORT_SHM_RING_ACTIVE,
    )

    ring_detail = ""
    if content_hop in RING_TRANSPORT_SHAPES:
        # Under shm_ring neither outputd sink opens a content PCM, so
        # content.buffer_frames is a synthetic period-sized stand-in and the
        # generic ">= 2x period" ALSA jitter-margin floor does not apply (every
        # shm_ring box would structurally fail it). The TRUE geometry comes from
        # content.ring, where capacity_frames == n_slots x slot_frames.
        if not isinstance(content_buffer, int) or content_buffer < period_frames:
            return CheckResult(
                "jasper-outputd",
                "fail",
                f"shm_ring content.buffer_frames={content_buffer!r}; expected >= "
                f"one period ({period_frames})",
                reason=REASON_OUTPUTD_CONTENT_BUFFER_UNDERSIZED,
            )
        ring = content.get("ring")
        if not isinstance(ring, dict):
            return CheckResult(
                "jasper-outputd",
                "fail",
                "content.source='shm_ring' but STATUS missing content.ring geometry "
                "contract (n_slots/slot_frames/capacity_frames). Redeploy outputd.",
                reason=REASON_OUTPUTD_RING_CONTRACT_MISSING,
            )
        ring_slots = ring.get("slots")
        ring_slot_frames = ring.get("slot_frames")
        ring_capacity = ring.get("capacity_frames")
        if not isinstance(ring_slots, int) or ring_slots < 2:
            return CheckResult(
                "jasper-outputd",
                "fail",
                f"shm_ring content.ring.slots={ring_slots!r}; expected >= 2 "
                "(ping-pong minimum)",
                reason=REASON_OUTPUTD_RING_SLOTS_INVALID,
            )
        if not isinstance(ring_slot_frames, int) or ring_slot_frames != period_frames:
            return CheckResult(
                "jasper-outputd",
                "fail",
                f"shm_ring content.ring.slot_frames={ring_slot_frames!r}; expected "
                f"== dac.period_frames ({period_frames}) — the ring slot must match "
                "the DAC period.",
                reason=REASON_OUTPUTD_RING_SLOT_FRAMES_MISMATCH,
            )
        expected_capacity = ring_slots * ring_slot_frames
        if not isinstance(ring_capacity, int) or ring_capacity != expected_capacity:
            return CheckResult(
                "jasper-outputd",
                "fail",
                f"shm_ring content.ring.capacity_frames={ring_capacity!r}; expected "
                f"n_slots*slot_frames ({expected_capacity})",
                reason=REASON_OUTPUTD_RING_CAPACITY_INCOHERENT,
            )
        # A transient empty ring is normal (idle), so occupancy/attach are
        # surfaced in the detail without gating on them.
        shm_ring_block = data.get("shm_ring")
        ring_occupancy = (
            shm_ring_block.get("occupancy")
            if isinstance(shm_ring_block, dict) else None
        )
        ring_attached = (
            bool(shm_ring_block.get("attached", False))
            if isinstance(shm_ring_block, dict) else None
        )
        # THE WIRE, compared and not merely printed: outputd publishes the
        # format/channels it read back off the header it ATTACHED to, and the
        # resolver answers what this box's wire should be, so a disagreement is a
        # real shear. Only checked once ATTACHED — before that outputd reports
        # its own declaration, which proves nothing about a ring that does not
        # exist yet.
        if ring_attached and isinstance(shm_ring_block, dict):
            from jasper.fanin_coupling import resolve_ring_wire

            # TOPOLOGY-THREADED, like every reconciler gate that compares this
            # wire (``ring_edge_width_ready`` / ``ring_wire_caps_ready``): the
            # channel counts are PER-TOPOLOGY axes, so resolving with ``None``
            # would answer the shipped stereo declaration and FAIL a box whose
            # post-DSP ring legitimately carries a different width.
            wire = resolve_ring_wire(evidence.saved_topology_for_wire())
            # WHICH ring outputd attached decides which width it is held to: an
            # armed ACTIVE endpoint reads the post-crossover per-driver ring,
            # whose width is ``ring_active_channels``. An armed endpoint whose
            # active width does not resolve (``None``) has no honest expectation,
            # so the channels axis is skipped rather than compared to a fallback.
            active_endpoint = content_hop == TRANSPORT_SHM_RING_ACTIVE
            expected_channels = (
                wire.ring_active_channels if active_endpoint else wire.ring_b_channels
            )
            ring_label = "the active ring" if active_endpoint else "Ring B"
            observed_format = shm_ring_block.get("format")
            observed_channels = shm_ring_block.get("channels")
            if observed_format is not None and observed_format != wire.sample_format:
                return CheckResult(
                    "jasper-outputd",
                    "fail",
                    f"shm_ring.format={observed_format!r} but this box's ring wire "
                    f"resolves to {wire.sample_format} — outputd attached to "
                    f"{ring_label} geometry nobody declared. Run: sudo /opt/jasper/"
                    ".venv/bin/jasper-fanin-coupling-reconcile shm_ring (it clears "
                    "a wire-mismatched ring file before re-arming).",
                    reason=REASON_OUTPUTD_RING_FORMAT_SHEAR,
                )
            if (
                observed_channels is not None
                and expected_channels is not None
                and observed_channels != expected_channels
            ):
                return CheckResult(
                    "jasper-outputd",
                    "fail",
                    f"shm_ring.channels={observed_channels!r} but this box's "
                    f"{ring_label} resolves to {expected_channels} — outputd "
                    f"attached to {ring_label} geometry nobody declared. Run: sudo "
                    "/opt/jasper/.venv/bin/jasper-fanin-coupling-reconcile shm_ring "
                    "(it clears a wire-mismatched ring file before re-arming).",
                    reason=REASON_OUTPUTD_RING_CHANNELS_SHEAR,
                )
        ring_detail = (
            f", shm_ring_slots={ring_slots}, shm_ring_slot_frames={ring_slot_frames}"
            f", shm_ring_capacity_frames={ring_capacity}"
            f", shm_ring_occupancy={ring_occupancy}"
            f", shm_ring_attached={ring_attached}"
            f", shm_ring_wire="
            f"{shm_ring_block.get('format') if isinstance(shm_ring_block, dict) else None}"
            f"/{shm_ring_block.get('channels') if isinstance(shm_ring_block, dict) else None}ch"
        )
    elif content_hop != TRANSPORT_DAC_CONTENT_RING and (
        not isinstance(content_buffer, int) or content_buffer < period_frames * 2
    ):
        # The ALSA jitter floor applies to exactly the ALSA class. A bonded
        # member's content hop is the dac-content RETURN ring, whose
        # `content.buffer_frames` is the same period-sized synthetic every SHM
        # hop publishes (`rust/jasper-outputd/src/state.rs`), and outputd
        # publishes no `content.ring` block for it — that sub-block is the
        # CENTRAL ring's capacity contract.
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"content.buffer_frames={content_buffer!r}; expected >= "
            f"2 x period ({period_frames})",
            reason=REASON_OUTPUTD_CONTENT_BUFFER_UNDERSIZED,
        )
    if not isinstance(dac_buffer, int) or dac_buffer < period_frames * 2:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"dac.buffer_frames={dac_buffer!r}; expected >= "
            f"2 x period ({period_frames})",
            reason=REASON_OUTPUTD_DAC_BUFFER_UNDERSIZED,
        )

    return ring_detail



def _transport_route_remedy() -> str:
    """Return the remedy that actually clears a post-DSP route disconnect.

    Three branches, because naming the reconciler is only right for one of them:
    a roleful layout on a DAC with no active outputd lane can never be
    reconciled, and a DAC with no registered profile is neither proven futile nor
    proven reconcilable.
    """
    from jasper.active_speaker.playback_route import (
        ActiveLaneCapabilityGap,
        UnrecognizedDacProfile,
        active_lane_capability_gap,
    )
    gap = active_lane_capability_gap(evidence.output_topology())
    if isinstance(gap, ActiveLaneCapabilityGap):
        return (
            f". {gap.device_label} does not support the active speaker lane, so "
            "this cannot be reconciled: choose a passive speaker layout on this "
            "speaker's /sound/setup/ page (passive sends full-range audio to "
            "every output — only safe when the speaker has its own built-in "
            "passive crossover), or attach an active-capable DAC."
        )
    if isinstance(gap, UnrecognizedDacProfile):
        return (
            f". DAC profile {gap.device_id!r} is not recognized, so whether it "
            "supports the active speaker lane is unknown: "
            "jasper-audio-hardware-reconcile may not help here until a profile "
            "for this DAC exists."
        )
    return (
        ". Run jasper-audio-hardware-reconcile to restore the paired "
        "CamillaDSP playback/outputd capture lane, then re-run "
        "jasper-fanin-coupling-reconcile --auto only if the coupling check also "
        "reports Ring A/Ring B drift."
    )


def _outputd_transport_health(
    data: dict[str, object],
    content: dict[str, object],
    dac: dict[str, object],
    *,
    outputd_env: dict[str, str],
    sink_mode: object,
    active_channels: int | None,
    expected_dac_pcm: str,
) -> tuple[str, str, str, str] | CheckResult:
    """Validate outputd's live topology, endpoint coherence, PCMs, and references.

    OUTPUTD'S OWN ENV IS THE EXPECTATION, not ``JASPER_FANIN_CAMILLA_COUPLING``
    (which under ADR-0100 selects nothing). ``outputd_env`` is read through the
    unit's ``EnvironmentFile=`` layering (:func:`_outputd_reconciled_env`), so
    the question here is "is the running daemon on the env it was last given?".
    Whether that env is the RIGHT one for this box is
    :func:`check_content_transport_coherence`'s.
    """
    from jasper.fanin_coupling import OUTPUTD_CONTENT_BRIDGE_ENV_VAR
    from jasper.audio_runtime_plan import (
        DEFAULT_CAMILLA2_STATEFILE_PATH,
        DEFAULT_CAMILLA_STATEFILE_PATH,
        output_endpoint_evidence_from_statefiles,
    )
    from jasper.transport_coherence import (
        transport_coherence_report,
        transport_topology_for_coupling,
    )

    bridge = (
        str(outputd_env.get(OUTPUTD_CONTENT_BRIDGE_ENV_VAR) or "").strip()
        or "(unset, = the ring)"
    )
    # NO COUPLING HANDED IN: the planner reads outputd's own env, which is the
    # half this check is about. Its resolved SHAPE is the content hop's class —
    # the ALSA lane, the central ring, or a bonded member's return ring — and
    # everything below branches on that rather than on a second marker read.
    topology = transport_topology_for_coupling(
        outputd_env=outputd_env, read_saved_topology=evidence.saved_topology_for_wire
    )
    expected_content_source = topology.outputd_content_source
    actual_content_source = content.get("source")
    if actual_content_source != expected_content_source:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"content.source={actual_content_source!r}; expected "
            f"{expected_content_source!r} for {OUTPUTD_CONTENT_BRIDGE_ENV_VAR}="
            f"{bridge!r} in outputd's own env — the running daemon is on an older "
            "env than the file. Run jasper-fanin-coupling-reconcile --auto to "
            "restart outputd onto it.",
            reason=REASON_OUTPUTD_CONTENT_SOURCE_MISMATCH,
        )
    live_outputd_env = dict(outputd_env)
    endpoint_evidence = output_endpoint_evidence_from_statefiles(
        DEFAULT_CAMILLA_STATEFILE_PATH,
        DEFAULT_CAMILLA2_STATEFILE_PATH,
    )
    transport_evidence_warning = ""
    if (
        endpoint_evidence.devices is None
        or not endpoint_evidence.endpoint_recognized
    ):
        evidence_detail = "; ".join(endpoint_evidence.errors) or (
            "loaded graph does not target a registered output endpoint"
        )
        transport_evidence_warning = (
            "post-DSP transport coherence unknown: " + evidence_detail
        )
    else:
        transport_report = transport_coherence_report(
            outputd_env=live_outputd_env,
            camilla_devices=endpoint_evidence.devices,
            read_saved_topology=evidence.saved_topology_for_wire,
        )
        if transport_report.errors:
            return CheckResult(
                "jasper-outputd",
                "fail",
                "; ".join(transport_report.errors) + _transport_route_remedy(),
                reason=REASON_OUTPUTD_TRANSPORT_ROUTE_UNPAIRED,
            )
        # Notes are deliberately not elevated: both rungs are owned by
        # :func:`check_content_transport_coherence`, which FAILs on the same
        # states with a runnable remedy and reads PERSISTED evidence rather than
        # outputd's live STATUS (at the endpoint rung outputd has refused to
        # start, so this function returns its systemd failure long before
        # reaching here).
    local_pipe_detail = f"content_source={actual_content_source}"
    if dac.get("pcm") != expected_dac_pcm:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"dac.pcm={dac.get('pcm')!r}; expected {expected_dac_pcm!r} "
            f"for sink_mode={sink_mode!r}, active_channels={active_channels!r}",
            reason=REASON_OUTPUTD_DAC_PCM_MISMATCH,
        )
    reference_outputs = data.get("reference_outputs")
    if not isinstance(reference_outputs, dict):
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS missing reference_outputs speaker-reference contract",
            reason=REASON_OUTPUTD_REFERENCE_CONTRACT_MISSING,
        )
    speaker_reference_source = reference_outputs.get("speaker_reference_source")
    if speaker_reference_source != "outputd_final_electrical":
        return CheckResult(
            "jasper-outputd",
            "fail",
            "reference_outputs.speaker_reference_source="
            f"{speaker_reference_source!r}; expected 'outputd_final_electrical'",
            reason=REASON_OUTPUTD_REFERENCE_SOURCE_UNEXPECTED,
        )
    reference_detail = (
        "speaker_reference_source=outputd_final_electrical, "
        "speaker_reference_active="
        f"{bool(reference_outputs.get('speaker_reference_active', False))}, "
        "speaker_reference_channels="
        f"{reference_outputs.get('speaker_reference_channels')}, "
        f"speaker_reference_udp={reference_outputs.get('udp_target')!r}, "
        f"chip_ref_pcm={reference_outputs.get('chip_ref_pcm')!r}"
    )
    return (
        transport_evidence_warning,
        local_pipe_detail,
        reference_detail,
        topology.name,
    )



@doctor_check(core=True)
def check_outputd_service() -> CheckResult:
    """Validate the outputd final-output-owner daemon.

    outputd owns the physical DAC, so disabled/inactive is a real audio-path
    failure.
    """
    service_failure = _service_state_failure(
        "jasper-outputd",
        "jasper-outputd.service",
        missing=REASON_OUTPUTD_UNIT_MISSING,
        not_enabled=REASON_OUTPUTD_UNIT_NOT_ENABLED,
        inactive=REASON_OUTPUTD_INACTIVE,
    )
    if service_failure is not None:
        return service_failure
    status_payload = _outputd_status_payload()
    if isinstance(status_payload, CheckResult):
        return status_payload
    data = status_payload

    if data.get("backend") != "alsa":
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"active but backend={data.get('backend')!r}; expected 'alsa'",
            reason=REASON_OUTPUTD_BACKEND_NOT_ALSA, speaker_silent=True,
        )
    sink_mode = data.get("sink_mode") or "single_alsa"
    outputd_env = _outputd_reconciled_env()
    active_channels = _outputd_active_channels_from_env(outputd_env)
    active_single_alsa = sink_mode == "single_alsa" and active_channels is not None
    expected_dac_pcm = (
        _OUTPUTD_EXPECTED_DUAL_DAC_PCM
        if sink_mode == "dual_apple"
        else _OUTPUTD_EXPECTED_DAC_PCM
    )
    content = data.get("content", {})
    dac = data.get("dac", {})
    if not isinstance(content, dict):
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS missing content{}",
            reason=REASON_OUTPUTD_STATUS_MISSING_CONTENT,
        )
    if not isinstance(dac, dict):
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS missing dac{}",
            reason=REASON_OUTPUTD_STATUS_MISSING_DAC,
        )
    transport_health = _outputd_transport_health(
        data,
        content,
        dac,
        outputd_env=outputd_env,
        sink_mode=sink_mode,
        active_channels=active_channels,
        expected_dac_pcm=expected_dac_pcm,
    )
    if isinstance(transport_health, CheckResult):
        return transport_health
    (
        transport_evidence_warning,
        local_pipe_detail,
        reference_detail,
        content_hop,
    ) = transport_health
    dual_health = _outputd_dual_apple_health(
        data,
        sink_mode=sink_mode,
        active_single_alsa=active_single_alsa,
        active_channels=active_channels,
    )
    if isinstance(dual_health, CheckResult):
        return dual_health
    active_detail, dual_detail, dual_warning = dual_health
    sample_rate = dac.get("sample_rate")
    period_frames = dac.get("period_frames")
    content_buffer = content.get("buffer_frames")
    dac_buffer = dac.get("buffer_frames")
    if sample_rate != 48000:
        return CheckResult(
            "jasper-outputd",
            "fail",
            f"dac.sample_rate={sample_rate!r}; expected 48000",
            reason=REASON_OUTPUTD_SAMPLE_RATE_UNEXPECTED,
        )
    if not isinstance(period_frames, int) or period_frames <= 0:
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS missing positive dac.period_frames",
            reason=REASON_OUTPUTD_PERIOD_FRAMES_MISSING,
        )
    buffer_health = _outputd_buffer_health(
        data,
        content,
        content_hop=content_hop,
        content_buffer=content_buffer,
        dac_buffer=dac_buffer,
        period_frames=period_frames,
    )
    if isinstance(buffer_health, CheckResult):
        return buffer_health
    ring_detail = buffer_health
    watchdog = data.get("watchdog")
    progress_age = (
        watchdog.get("last_progress_age_ms", -1)
        if isinstance(watchdog, dict)
        else -1
    )
    if not isinstance(progress_age, (int, float)) or progress_age < 0:
        return CheckResult(
            "jasper-outputd",
            "fail",
            "STATUS response missing watchdog.last_progress_age_ms",
            reason=REASON_OUTPUTD_STATUS_MISSING_WATCHDOG,
        )
    content_xruns = int(content.get("xrun_count", 0) or 0)
    dac_xruns = int(dac.get("xrun_count", 0) or 0)
    xrun_warning = _outputd_xrun_rate_warning(content, dac)
    content_empty = int(content.get("empty_periods", 0) or 0)
    content_partial = int(content.get("partial_periods", 0) or 0)
    content_eagain = int(content.get("eagain_count", 0) or 0)
    frames = int(dac.get("frames_written", 0) or 0)
    bridge_detail = _outputd_content_bridge_detail(data)
    tts_raw = data.get("tts")
    tts = tts_raw if isinstance(tts_raw, dict) else {}
    tts_pending = int(tts.get("pending_frames", 0) or 0)
    tts_over_budget = bool(tts.get("over_budget", False))
    tts_over_budget_ms = int(tts.get("over_budget_ms", 0) or 0)
    tts_over_budget_streak_ms = int(
        tts.get("over_budget_streak_ms", 0) or 0
    )
    tts_max_pending = int(tts.get("max_pending_frames", 0) or 0)
    tts_dropped_commands = int(tts.get("dropped_commands", 0) or 0)
    tts_dropped_audio_frames = int(
        tts.get("dropped_audio_frames", 0) or 0
    )
    loudness_health = _outputd_loudness_health(data)
    if isinstance(loudness_health, CheckResult):
        return loudness_health
    loudness_detail = loudness_health
    if progress_age > OUTPUTD_STALE_MS:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but last_progress_age_ms={progress_age} "
            "(work loop may be wedged; watchdog should fire soon)",
            reason=REASON_OUTPUTD_PROGRESS_STALE,
        )
    if dual_warning is not None:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but {dual_warning}. {dual_detail.lstrip(', ')}",
            reason=REASON_OUTPUTD_DUAL_APPLE_NOT_LINKED,
        )
    if tts_over_budget or tts_pending > 48000 * 2:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but tts.pending_frames={tts_pending} (>2s). "
            f"over_budget_streak_ms={tts_over_budget_streak_ms}. "
            "TTS producer may be outrunning outputd playback.",
            reason=REASON_OUTPUTD_TTS_OVER_BUDGET,
        )
    if xrun_warning is not None:
        return CheckResult(
            "jasper-outputd",
            "warn",
            f"active but {xrun_warning}. xruns={content_xruns}/{dac_xruns}. "
            "A sustained, recent xrun rate means audible dropouts — check "
            "CPU contention (jasper-camilla RT scheduling), DAC buffer sizing "
            "(JASPER_OUTPUTD_DAC_BUFFER_FRAMES), and "
            "`journalctl -u jasper-outputd | grep xrun`.",
            reason=REASON_OUTPUTD_XRUN_RATE_SUSTAINED,
        )
    status = "warn" if transport_evidence_warning else "ok"
    evidence_reason = (
        REASON_OUTPUTD_TRANSPORT_EVIDENCE_UNKNOWN
        if transport_evidence_warning
        else ""
    )
    transport_detail = (
        f", {transport_evidence_warning}" if transport_evidence_warning else ""
    )
    return CheckResult(
        "jasper-outputd",
        status,
        f"active, backend=alsa, frames_written={frames}, "
        f"content_buffer_frames={content_buffer}, dac_buffer_frames={dac_buffer}, "
        f"xruns={content_xruns}/{dac_xruns}, "
        f"content_empty_periods={content_empty}, "
        f"content_partial_periods={content_partial}, "
        f"content_eagain_count={content_eagain}, "
        f"{local_pipe_detail}, "
        f"tts_pending_frames={tts_pending}, "
        f"tts_max_pending_frames={tts_max_pending}, "
        f"tts_over_budget_ms={tts_over_budget_ms}, "
        f"tts_dropped_commands={tts_dropped_commands}, "
        f"tts_dropped_audio_frames={tts_dropped_audio_frames}, "
        f"{bridge_detail}, "
        f"{reference_detail}, "
        f"{loudness_detail}, "
        f"progress_age_ms={progress_age}"
        f"{active_detail}"
        f"{dual_detail}"
        f"{ring_detail}"
        f"{transport_detail}",
        reason=evidence_reason,
    )

@doctor_check()
def check_aec_clock_drift() -> CheckResult:
    """Surface the passive chip-AEC clock-drift estimate (Layer 0).

    Reads ``reference_outputs.aec_clock`` from outputd STATUS — the observe-only
    SRO (sample-rate-offset) estimator's verdict, ppm, and latency budget. Purely
    diagnostic; no audio path depends on it.

      - skipped when outputd is disabled/inactive, STATUS is unreachable or
        invalid, the chip reference is not configured, or the aec_clock block
        is absent.
      - warn only when sro_estimator_status == "untrusted".
      - ok otherwise: coherent, compensable (a real steady offset, the expected
        state on independent-clock DACs like the HiFiBerry), and observing
        (still measuring) are all healthy.
    """
    label = "AEC clock drift"
    state = evidence.unit_state("jasper-outputd.service")
    if state is None:
        return CheckResult(
            label,
            "skipped",
            "systemctl unavailable — skipped (not Linux?)",
            reason=REASON_SYSTEMCTL_UNAVAILABLE,
        )
    if state.get("load_state") == "not-found" or state.get(
        "unit_file_state"
    ) not in ("enabled", "enabled-runtime"):
        return CheckResult(
            label,
            "skipped",
            "jasper-outputd not enabled",
            reason=REASON_AEC_CLOCK_OUTPUTD_NOT_ENABLED,
        )
    if state.get("active_state") != "active":
        return CheckResult(
            label,
            "skipped",
            "jasper-outputd not active",
            reason=REASON_AEC_CLOCK_OUTPUTD_INACTIVE,
        )

    read = evidence.outputd_status()
    data = read.payload
    if data is None:
        return CheckResult(
            label,
            "skipped",
            f"STATUS unusable: {read.error}",
            reason=REASON_AEC_CLOCK_STATUS_UNAVAILABLE,
        )

    reference_outputs = data.get("reference_outputs")
    if not isinstance(reference_outputs, dict):
        return CheckResult(
            label,
            "skipped",
            "STATUS missing reference_outputs",
            reason=REASON_AEC_CLOCK_REFERENCE_OUTPUTS_MISSING,
        )
    if reference_outputs.get("chip_ref_pcm") is None:
        return CheckResult(
            label,
            "skipped",
            "chip reference not configured",
            reason=REASON_AEC_CLOCK_CHIP_REF_NOT_CONFIGURED,
        )
    chip_ref_writer = reference_outputs.get("chip_ref_writer")
    if isinstance(chip_ref_writer, dict) and not bool(
        chip_ref_writer.get("active", chip_ref_writer.get("enabled", False))
    ):
        writer_status = str(chip_ref_writer.get("status") or "unknown")
        recovery = (
            "outputd is retrying the optional AEC reference device"
            if writer_status in {"connecting", "degraded"}
            else "the reference worker stopped; correct the device/config and "
            "restart jasper-outputd"
        )
        return CheckResult(
            label,
            "warn",
            "chip reference is desired but unavailable; speaker playback remains "
            f"active and {recovery} "
            f"(status={writer_status}, "
            f"open_errors={chip_ref_writer.get('open_error_count')}, "
            f"retries={chip_ref_writer.get('retry_count')})",
            reason=REASON_AEC_CLOCK_CHIP_REF_UNAVAILABLE,
        )
    aec_clock = reference_outputs.get("aec_clock")
    if not isinstance(aec_clock, dict):
        return CheckResult(
            label, "skipped", "outputd build predates aec_clock observation",
            reason=REASON_AEC_CLOCK_BLOCK_ABSENT,
        )

    verdict = aec_clock.get("verdict")
    status = aec_clock.get("sro_estimator_status")
    sro_ppm = aec_clock.get("chip_ref_sro_ppm")
    reason = aec_clock.get("verdict_reason")
    # Observe mode: the chip-ref writer was armed purely to MEASURE drift on the
    # software-AEC3 mic path, not for production chip-AEC.
    observe = aec_clock.get("observe")
    latency = aec_clock.get("latency") or {}
    dac_ms = latency.get("dac_presentation_ms")
    playback_ms = latency.get("playback_queue_ms")
    chip_ref_ms = latency.get("chip_ref_queue_ms")
    detail = (
        f"verdict={verdict}, sro_estimator_status={status}, "
        f"observe={observe}, chip_ref_sro_ppm={sro_ppm}, "
        f"dac_presentation_ms={dac_ms}, playback_queue_ms={playback_ms}, "
        f"chip_ref_queue_ms={chip_ref_ms}"
    )
    # "observing" (still measuring at startup) and "compensable" (expected on
    # independent-clock DACs) are both healthy.
    if status == "untrusted":
        return CheckResult(
            label,
            "warn",
            f"chip-AEC clock drift cannot be trusted: {reason}. {detail}",
            reason=REASON_AEC_CLOCK_UNTRUSTED,
        )
    return CheckResult(label, "ok", detail)
