# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""outputd's runtime lane, as the reconcile pass writes it into the
outputd.env stage: the content and DAC-edge formats, the backend, sink and
PCM keys for a single, composite or parked DAC, and the active-lane pair.

Each step takes the pass: it reads the role policy's verdict and the graph
gate (Pass.active_graph_status) from it, and writes through
Pass.set_env_file_var.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jasper.platform.atomic_io import EnvKeyAction as EnvAction
from jasper.platform.env_file import read_env_file

if TYPE_CHECKING:
    from jasper.audio_hardware.reconcile import Pass

# The ACTIVE RING's playback PCM — the ONE legal active endpoint. This module
# never CHOOSES it; the active-lane decision reports which endpoint the live
# graph targets, and this literal is only how the answer is recognized. Mirrors
# jasper.dsp_control.fanin_coupling.RING_ACTIVE_PLAYBACK_DEVICE and the conf.d block name;
# pinned equal by tests/test_ring_active_endpoint.py.
RING_ACTIVE_OUTPUTD_PLAYBACK_DEVICE = "jts_ring_active_playback"


def active_lane_channels_for_dac(run: Pass, dac_id: str) -> tuple[int | None, bool]:
    """A recognized single DAC's active-lane channel CAP, and whether the
    registry ITSELF answered "this DAC declares no active lane".

    THREE-VALUED: ``(n, False)`` the cap, ``(None, True)`` the registry's
    own "no active lane", and ``(None, False)`` no answer at all — the probe
    itself failed. The last two need different remedies, so they must not
    collapse."""
    if not dac_id:
        return None, False
    try:
        # lazy: patch target — the tests replace it on the source
        # module, which only a per-call import sees.
        from jasper.audio_hardware.dac import (
            active_outputd_lane_channels_for,
            is_known_profile_id,
        )

        width = active_outputd_lane_channels_for(dac_id)
        known = is_known_profile_id(dac_id)
    # noqa reason: three-valued by design — a probe that failed for any reason
    # answers "no answer", which the caller reports as the transient it is.
    except Exception:  # noqa: BLE001
        run.mark_degraded()
        return None, False
    if width:
        return int(width), False
    return None, bool(known)


def final_edge_format_for_dac(run: Pass, dac_id: str) -> tuple[str, str]:
    """A recognized DAC's declared final-edge ALSA format AND its outputd
    sink kind, from ONE profile lookup (ADR-0235 R1).

    BOTH or NEITHER: outputd's ``env_str`` defaults only on an UNSET key,
    so an empty JASPER_OUTPUTD_SINK fails its config parse and parks it at
    exit 78. Resolved BY ID off whichever profile the caller armed, never
    through a composite's children — outputd's paired composite sink has no
    packed-24 child write path, which is why the dual-Apple composite
    declares S16_LE though both its children declare S24_3LE.
    """
    if not dac_id:
        return "", ""
    try:
        # lazy: patch target — the tests replace it on the source
        # module, which only a per-call import sees.
        from jasper.audio_hardware.dac import by_id, final_edge_format_for

        fmt = final_edge_format_for(dac_id)
        profile = by_id(dac_id)
    # noqa reason: any failure preserves the previous edge format; writing a
    # guess would silently narrow a wide DAC edge.
    except Exception:  # noqa: BLE001
        run.mark_degraded()
        return "", ""
    if fmt and profile is not None:
        return fmt, profile.outputd_sink
    return "", ""


def dac_format_actions_for_recognized(
    run: Pass, dac_id: str
) -> tuple[list[EnvAction], str]:
    """A recognized DAC's declared edge format AND sink as env actions, plus
    the format the file will state — or NO actions when the registry probe
    is unavailable, since one lookup answers both and they degrade together.

    Empty is a MEANINGFUL value on the format key (outputd reads it as
    S16_LE), so writing it on a lost probe would silently NARROW a wide
    edge with no error anywhere. Preserving the previous value is the loud
    option: on a same-pass id change the stale value parks outputd at exit
    78 rather than converting audio wrongly.
    """
    dac_format, dac_sink = final_edge_format_for_dac(run, dac_id)
    if not dac_format:
        preserved = read_env_file(run.outputd_env_target).get(
            "JASPER_OUTPUTD_DAC_FORMAT", ""
        )
        run.log(
            "dac_format_skip",
            reason="registry_probe_unavailable",
            dac_id=dac_id,
            preserved=preserved or "absent",
            outputd_env=run.outputd_env_file,
        )
        return [], preserved
    return [
        ("JASPER_OUTPUTD_DAC_FORMAT", dac_format),
        ("JASPER_OUTPUTD_SINK", dac_sink),
    ], dac_format


def set_outputd_active_lane_pair(run: Pass, lane: str, endpoint_device: str) -> bool:
    """THE SINGLE WRITER of the active-lane PAIR.

    JASPER_OUTPUTD_ACTIVE_LANE and JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT are
    ONE FACT with two consumers: outputd bails at startup on the incoherent
    pair (marker set, lane clear), because that can only mean this writer
    is broken. So every path that states one states the other, here, from
    one decision. Positive equality against the named device, never "not
    the ALSA lane": an unrecognized endpoint must resolve to NO marker,
    which a negative test would invert into a spurious arm. Returns whether
    either key changed.
    """
    ring_endpoint = (
        "1"
        if lane == "1" and endpoint_device == RING_ACTIVE_OUTPUTD_PLAYBACK_DEVICE
        else ""
    )
    return run.set_env_file_var(
        run.outputd_env_target,
        [
            ("JASPER_OUTPUTD_ACTIVE_LANE", lane),
            ("JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT", ring_endpoint),
        ],
    )


def apply_audio_runtime_env(run: Pass) -> bool:
    target = run.outputd_env_target
    # The CONTENT lane's width is a function of the fan-in coupling, never
    # of the DAC, so unlike the edge format it is emitted once ahead of the
    # per-hardware branches and is always definitive. An empty answer means
    # leave the key alone rather than write a guess — a fallback here would
    # be a second spelling of DEFAULT_PLAYBACK_FORMAT.
    try:
        # lazy: patch target — the tests replace it on the source
        # module, which only a per-call import sees.
        from jasper.dsp_control.fanin_coupling import content_lane_format_for_coupling

        content_format = content_lane_format_for_coupling()
    # noqa reason: any failure leaves the key alone rather than narrowing the
    # content lane; the pass is marked degraded below.
    except Exception:  # noqa: BLE001
        content_format = ""
    prelude: list[EnvAction] = []
    if content_format:
        prelude.append(("JASPER_OUTPUTD_CONTENT_FORMAT", content_format))
    else:
        run.mark_degraded()
        run.log(
            "content_format_skip",
            reason="coupling_probe_unavailable",
            outputd_env=run.outputd_env_file,
        )
    changed = run.set_env_file_var(target, prelude)
    composite = run.observed.kind == "composite"
    if composite and run.output_dac_recognized:
        changed = _apply_composite_runtime_env(run, content_format) or changed
    elif run.output_dac_recognized:
        changed = _apply_single_runtime_env(run, content_format) or changed
    else:
        changed = _apply_parked_runtime_env(run, content_format) or changed
    return changed


def _apply_composite_runtime_env(run: Pass, content_format: str) -> bool:
    target = run.outputd_env_target
    run.outputd_active_mode = False
    run.outputd_active_channels = ""
    actions: list[EnvAction] = [
        ("JASPER_OUTPUTD_BACKEND", "alsa"),
        ("JASPER_OUTPUTD_DAC_PCM", run.output_dac_id),
        ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", run.dual_apple_dac_a_pcm),
        ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", run.dual_apple_dac_b_pcm),
    ]
    format_actions, dac_format = dac_format_actions_for_recognized(
        run, run.output_dac_id
    )
    actions += format_actions
    # Composite width is fixed at 4 (two stereo children); clear the
    # single-sink width knob so a stale value cannot reach outputd, which
    # rejects != 4 on this sink.
    actions.append(("JASPER_OUTPUTD_ACTIVE_CHANNELS", ""))
    changed = run.set_env_file_var(target, actions)
    # Deliberately narrower than the single-DAC branch: an ALOOP composite
    # keeps its unconditional clear. `active_lane` is inert on a composite
    # at runtime, so writing =1 there would change no behaviour but WOULD
    # churn outputd.env and /state on boxes this has no business touching.
    dual_apple_endpoint = run.dual_apple_active_endpoint_device
    if dual_apple_endpoint == RING_ACTIVE_OUTPUTD_PLAYBACK_DEVICE:
        changed = set_outputd_active_lane_pair(run, "1", dual_apple_endpoint) or changed
    else:
        changed = set_outputd_active_lane_pair(run, "", "") or changed
    run.log(
        "runtime_env",
        mode="dual_apple",
        content_format=content_format or "unset",
        dac_format=dac_format or "unset",
        outputd_env=run.outputd_env_file,
        changed=int(changed),
    )
    return changed


def _apply_single_runtime_env(run: Pass, content_format: str) -> bool:
    """A coherent single DAC runs the active lane ONLY when it declares one
    AND a legal active-speaker graph whose playback width fits within that
    cap is the live CamillaDSP config. We DRIVE WHAT WE USE: the gate
    returns the config's ACTUAL width W, emitted as
    JASPER_OUTPUTD_ACTIVE_CHANNELS so outputd opens the DAC at the first width the driver accepts at or above W and pads the rest with silence (`/state` `dac.channels` reports it).
    Fail-closed: without a confirmed in-cap active graph the DAC stays
    ordinary stereo."""
    target = run.outputd_env_target
    changed = False
    active_mode = False
    active_channels = ""
    active_endpoint_device = ""
    graph_status = ""
    active_lane_cap, declares_no_lane = active_lane_channels_for_dac(
        run, run.output_dac_id
    )
    # Three outcomes, three remedies. All resolve passive (fail-closed);
    # they differ only in what an operator reading the journal should do.
    if declares_no_lane:
        # The registry answered: this DAC declares no active outputd lane,
        # so the width gate never ran. Fixed only by choosing a different
        # layout at /sound/speaker/. Same literal as that save-guard's
        # refusal reason.
        graph_status = "dac_no_active_lane"
    elif active_lane_cap is not None:
        ok, payload = run.active_graph_status(active_lane_cap)
        if ok:
            active_mode = True
            active_channels, active_endpoint_device = payload
        else:
            graph_status = payload
    else:
        # No answer at all on a RECOGNIZED DAC: the lane-cap probe itself
        # failed. Transient — the next pass converges — so this must NOT be
        # reported as the permanent dac_no_active_lane.
        graph_status = "lane_probe_failed"
    actions: list[EnvAction] = [
        ("JASPER_OUTPUTD_BACKEND", "alsa"),
        ("JASPER_OUTPUTD_DAC_PCM", "outputd_dac"),
        ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", ""),
        ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", ""),
    ]
    format_actions, dac_format = dac_format_actions_for_recognized(
        run, run.output_dac_id
    )
    actions += format_actions
    if active_mode:
        run.outputd_active_mode = True
        run.outputd_active_channels = active_channels
        actions.append(("JASPER_OUTPUTD_ACTIVE_CHANNELS", active_channels))
        changed = run.set_env_file_var(target, actions)
        # An active 2-way speaker is ALSO 2-channel, so outputd's bare
        # content_channels==2 check would wrongly permit its post-crossover
        # TTS mixer / content bridge here. Mark the lane explicitly so
        # those stereo-only features fail closed (full-range-to-tweeter
        # safety). The endpoint travels with it, from the same decision.
        changed = (
            set_outputd_active_lane_pair(run, "1", active_endpoint_device)
            or changed
        )
        run.log(
            "runtime_env",
            mode="single_alsa_active",
            active_channels=active_channels,
            active_lane_cap=active_lane_cap,
            active_endpoint=active_endpoint_device or "unset",
            content_format=content_format or "unset",
            dac_format=dac_format or "unset",
            outputd_env=run.outputd_env_file,
            changed=int(changed),
        )
        return changed
    run.outputd_active_mode = False
    run.outputd_active_channels = ""
    # Clear the width knob so outputd defaults to stereo, and the lane PAIR
    # so a stale =1 cannot keep the stereo-only features fenced off on an
    # ordinary passive DAC.
    actions.append(("JASPER_OUTPUTD_ACTIVE_CHANNELS", ""))
    changed = run.set_env_file_var(target, actions)
    changed = set_outputd_active_lane_pair(run, "", "") or changed
    run.log(
        "runtime_env",
        mode="single_alsa",
        content_format=content_format or "unset",
        dac_format=dac_format or "unset",
        outputd_env=run.outputd_env_file,
        changed=int(changed),
        active_graph=graph_status or "none",
    )
    return changed


def _apply_parked_runtime_env(run: Pass, content_format: str) -> bool:
    run.outputd_active_mode = False
    run.outputd_active_channels = ""
    changed = run.set_env_file_var(
        run.outputd_env_target,
        [
            ("JASPER_OUTPUTD_BACKEND", "fake"),
            ("JASPER_OUTPUTD_SINK", "single_alsa"),
            ("JASPER_OUTPUTD_DAC_PCM", "outputd_dac"),
            ("JASPER_OUTPUTD_DUAL_DAC_A_PCM", ""),
            ("JASPER_OUTPUTD_DUAL_DAC_B_PCM", ""),
            # Unrecognized/parked: no profile to query, so clear rather than
            # query. Explicit empty, not omitted: this reconciler-owned file
            # always states a definitive value for every conditional key, so
            # a hot-swap to an unrecognized card cannot leave a stale format.
            ("JASPER_OUTPUTD_DAC_FORMAT", ""),
            ("JASPER_OUTPUTD_ACTIVE_CHANNELS", ""),
        ],
    )
    changed = set_outputd_active_lane_pair(run, "", "") or changed
    run.log(
        "runtime_env",
        mode="parked",
        content_format=content_format or "unset",
        dac_format="unset",
        outputd_env=run.outputd_env_file,
        changed=int(changed),
    )
    return changed
