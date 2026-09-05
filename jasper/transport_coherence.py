# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Resolve a box's transport shape from its env, and report the contradictions
across CamillaDSP and outputd that shape implies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from jasper.camilla_config_contract import (
    DEFAULT_SAMPLE_RATE,
    UNPAIRED_POST_DSP_PLAYBACK_DEVICES,
)
from jasper.fanin.ring_health import load_topology_for_wire, resolve_wire_for_gate
from jasper.fanin_coupling import (
    COUPLING_SHM_RING,
    DEFAULT_OUTPUTD_ACTIVE_RING_PATH,
    OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
    OUTPUTD_CONTENT_FORMAT_ENV_VAR,
    OUTPUTD_RING_PATH_ENV_VAR,
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_CAPTURE_DEVICE,
    RING_PATH_ENV_VAR,
    RING_PLAYBACK_DEVICE,
    RING_TRANSPORT_SHAPES,
    TRANSPORT_DAC_CONTENT_RING,
    TRANSPORT_OFF_RING,
    TRANSPORT_SHM_RING_ACTIVE,
    TransportTopology,
    coupling_value_removed,
    dac_content_marker_contradicted,
    dac_content_ring_served,
    outputd_content_is_central_ring,
    resolve_coupling,
    resolve_outputd_ring_path,
    resolve_ring_path,
    ring_active_endpoint_armed,
)


def transport_topology_for_coupling(
    coupling: str | None = None,
    *,
    fanin_env: Mapping[str, str] | None = None,
    outputd_env: Mapping[str, str] | None = None,
    read_saved_topology: Callable[[], Any] | None = None,
) -> TransportTopology:
    """Return the concrete transport topology this box's env implies.

    The four shapes and what each is: see
    :data:`jasper.fanin_coupling.TRANSPORT_SHAPES`' constants.
    They are told apart by the two markers in ``outputd_env`` — see those
    constants for why the observed playback device is not the discriminator.

    THE FAN-IN -> CAMILLADSP HOP DOES NOT BRANCH. Since ADR-0100 it is Ring A on
    every box: fan-in serves the ring for a ``shm_ring``, unset or empty token
    and PARKS on anything else.

    BOTH ENDS ANSWER FOR THEMSELVES, each through the predicate that owns its
    daemon's accept set: :func:`coupling_value_removed` for fan-in and
    :func:`outputd_bridge_is_ring` for outputd. Either one off the ring gives
    :data:`TRANSPORT_OFF_RING`. UNDECLARED IS THE RING on both axes, so a box
    the reconciler has not written yet resolves the ring rather than a route
    this repo deleted.

    ``read_saved_topology`` replaces the saved-topology read the ACTIVE width
    needs, so one pass's consumers can share a single memoized read.
    """

    fanin_values: Mapping[str, str] = fanin_env or {}
    outputd_values: Mapping[str, str] = outputd_env or {}
    # `outputd_content_is_central_ring`, not the bridge key alone: a marker
    # declared beside a bridge is the pair outputd refuses at startup, and
    # resolving it as the healthy central ring would let /state and the doctor
    # describe a daemon that cannot run.
    on_ring = not coupling_value_removed(coupling) and outputd_content_is_central_ring(
        outputd_values
    )
    # The MARKER, not the observed device, selects the post-DSP shape. On an
    # armed active endpoint the post-DSP hop is the ACTIVE ring: a different
    # device, a different file, and a per-driver width the topology decides,
    # where Ring B is a full-range stereo program.
    active_endpoint = ring_active_endpoint_armed(outputd_values)
    # SERVED, not merely armed: the marker beside a DECLARED bridge is the pair
    # outputd refuses at startup, so it is not this shape. It resolves off-ring
    # and `jasper.control.transport_park` names it under its own class.
    dac_content_lane = dac_content_ring_served(outputd_values)
    # Read the saved topology ONLY where an axis actually depends on it: the
    # ACTIVE ring's width, which only the ring arm publishes. Every other axis —
    # the format, Ring A's width, Ring B's — is topology-free, so the other
    # arms answer for the shipped geometry without touching the disk.
    #
    # Through the GATE resolver, never `resolve_ring_wire` directly. This layer
    # DESCRIBES a box for read-only surfaces (`jasper-audio-config explain`,
    # jasper-doctor); a wire token neither language parses would otherwise raise
    # through them and replace the whole verdict with a traceback. The two wire
    # axes go UNKNOWN instead, which every comparison in
    # :func:`transport_coherence_report` already treats as missing evidence, and
    # the bad declaration keeps its loud owners: fan-in parks at exit 78 and the
    # doctor's ring-wire check names the token.
    read = read_saved_topology or load_topology_for_wire
    wire, _ = resolve_wire_for_gate(read() if active_endpoint and on_ring else None)
    wire_format = wire.sample_format if wire is not None else None
    # Ring A (fan-in -> CamillaDSP, jts_ring_capture). Its wire comes from the
    # one resolver every declaring end reads, so /state reports the geometry the
    # ring is actually built to rather than a literal that can disagree with it.
    fanin_to_camilla: dict[str, Any] = {
        "transport": "shm_ring",
        "path": resolve_ring_path(fanin_values.get(RING_PATH_ENV_VAR)),
        "writer": "jasper-fanin",
        "camilla_capture_device": RING_CAPTURE_DEVICE,
        "format": wire_format,
        "channels": wire.ring_a_channels if wire is not None else None,
        "sample_rate": DEFAULT_SAMPLE_RATE,
    }
    if dac_content_lane:
        # Function-local: `jasper.multiroom.dac_content_ring` pulls the whole
        # multiroom package (and `jasper.camilla_emit`) into the import time of
        # this module's socket-activated read-only consumers.
        from jasper.multiroom.dac_content_ring import (
            DAC_CONTENT_RING_CHANNELS,
            DAC_CONTENT_RING_FILE,
            DAC_CONTENT_RING_FORMAT,
            DAC_CONTENT_RING_PCM,
        )

        return TransportTopology(
            name=TRANSPORT_DAC_CONTENT_RING,
            fanin_to_camilla=fanin_to_camilla,
            camilla_to_outputd={
                # `camilla_playback_device` is deliberately ABSENT: CamillaDSP
                # does not drive this hop, and a guessed one would let a
                # consumer compare an endpoint against a lane it never writes.
                "transport": "shm_ring",
                "path": DAC_CONTENT_RING_FILE,
                "outputd_capture_pcm": DAC_CONTENT_RING_PCM,
                "writer": "snapclient",
                "reader": "jasper-outputd",
                "format": DAC_CONTENT_RING_FORMAT,
                "channels": DAC_CONTENT_RING_CHANNELS,
                "sample_rate": DEFAULT_SAMPLE_RATE,
            },
            camilla={"capture_resampler": None},
            # outputd publishes `content.source` as `shm_ring` only while the
            # CENTRAL ring is attached (`rust/jasper-outputd/src/state.rs`).
            outputd_content_source="alsa",
        )
    if on_ring:
        # Ring B (CamillaDSP -> outputd, jts_ring_playback), or the ACTIVE ring
        # on an armed roleful box.
        post_dsp_path = resolve_outputd_ring_path(
            outputd_values.get(OUTPUTD_RING_PATH_ENV_VAR)
        )
        if active_endpoint:
            post_dsp_device = RING_ACTIVE_PLAYBACK_DEVICE
            post_dsp_channels: int | None = (
                wire.ring_active_channels if wire is not None else None
            )
        else:
            post_dsp_device = RING_PLAYBACK_DEVICE
            post_dsp_channels = wire.ring_b_channels if wire is not None else None
        return TransportTopology(
            name=TRANSPORT_SHM_RING_ACTIVE if active_endpoint else COUPLING_SHM_RING,
            fanin_to_camilla=fanin_to_camilla,
            camilla_to_outputd={
                "transport": "shm_ring",
                "path": post_dsp_path,
                "camilla_playback_device": post_dsp_device,
                "reader": "jasper-outputd",
                "format": wire_format,
                "channels": post_dsp_channels,
                "sample_rate": DEFAULT_SAMPLE_RATE,
            },
            # NO latency geometry here. A transport shape is one answer for
            # every ring box, and the graphs on those boxes carry different
            # chunk/target (a floor clamped to the ring's capacity, or the
            # certified pair on an end-to-end ring graph). The plan answers
            # that axis twice, per box: `settings` for policy and
            # `camilla_emitted` for what the loaded config declares.
            camilla={"capture_resampler": None},
            outputd_content_source="shm_ring",
        )
    return TransportTopology(
        name=TRANSPORT_OFF_RING,
        fanin_to_camilla=fanin_to_camilla,
        # No CamillaDSP -> outputd pair to report: outputd's content comes from
        # whatever its own bridge names. `alsa` below is outputd's own STATUS
        # token for "no ring attached" (state.rs), which the doctor compares
        # this shape against.
        camilla_to_outputd={"transport": None},
        camilla={"capture_resampler": None},
        outputd_content_source="alsa",
    )


@dataclass(frozen=True)
class TransportCoherenceReport:
    """One transport comparison's contradictions AND its non-error observations.

    ``errors`` are contradictions: a caller that reports them refuses, parks, or
    fails. ``notes`` are states that are coherent but not steady — the two rungs
    of the ACTIVE-ring arm ladder — so a caller PROCEEDS while still saying what
    the box is sitting in. The split exists because those two need opposite
    dispositions from the same comparison: collapsing them into ``errors``
    deadlocks the documented arm ladder at either rung — the graph rung on a
    loopback plan, and the endpoint rung on a plan already ``shm_ring``.

    Notes are not "soft errors". A note means the state is safe-by-construction
    at this instant and has a documented next step, not that a contradiction was
    downgraded.
    """

    errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def transport_coherence_report(
    *,
    coupling: str | None = None,
    outputd_env: Mapping[str, str] | None = None,
    camilla_devices: Mapping[str, Any] | None = None,
    read_saved_topology: Callable[[], Any] | None = None,
) -> TransportCoherenceReport:
    """Return contradictions across the complete Camilla/outputd transport.

    ``TransportTopology`` is the policy source. This function compares its two
    runtime consumers without re-deriving endpoint strings in reconcilers or
    doctor checks. Missing Camilla evidence is not itself an error; a concrete
    contradiction is.

    Both ring SHAPES take the same branch: :data:`COUPLING_SHM_RING` and
    :data:`TRANSPORT_SHM_RING_ACTIVE` differ in WHICH post-DSP endpoint they
    expect, and the endpoint comparison reads that from the resolved topology
    rather than re-deriving it. The active shape additionally requires outputd's
    own ring PATH to be the active ring's — the Python-side twin of outputd's
    startup allowlist, reported here at reconcile time instead of at a daemon
    bail.

    Returns a :class:`TransportCoherenceReport`: contradictions in ``errors``,
    coherent-but-transient states in ``notes``.
    """

    outputd_values: Mapping[str, str] = outputd_env or {}
    devices: Mapping[str, Any] = camilla_devices or {}
    playback_device = str(devices.get("playback_device") or "") or None
    capture_device = str(devices.get("capture_device") or "") or None
    topology = transport_topology_for_coupling(
        coupling,
        outputd_env=outputd_values,
        read_saved_topology=read_saved_topology,
    )
    errors: list[str] = []
    notes: list[str] = []
    normalized = topology.name
    # Only what a message PRINTS; every decision is the predicate's. Undeclared
    # IS the ring, the same rule route policy applies, so this report cannot
    # call a healthy box's pair split.
    bridge_label = (
        str(outputd_values.get(OUTPUTD_CONTENT_BRIDGE_ENV_VAR) or "").strip().lower()
        or "(unset, = the ring)"
    )

    def _compare_lane_channels(lane: Mapping[str, Any], hop: str) -> None:
        """Compare one hop's declared channel count against Camilla's loaded one."""
        expected_channels = lane.get("channels")
        observed = devices.get(f"{hop}_channels")
        if not isinstance(expected_channels, int) or not isinstance(observed, int):
            return
        if observed != expected_channels:
            errors.append(
                f"transport plan is shm_ring with "
                f"{hop} channels={expected_channels}, "
                f"but Camilla's loaded config declares {observed} — the ring "
                "header's channel count is compared field-by-field at attach, "
                "so the ioplug open fails"
            )

    if dac_content_marker_contradicted(outputd_values):
        # THE PAIR OUTPUTD REFUSES. Reported here as well as parked, because a
        # caller that refuses on errors must not proceed onto a box whose daemon
        # will bail EX_CONFIG the moment it restarts.
        errors.append(
            "the dac-content lane marker is armed while "
            f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR}={bridge_label} is declared beside "
            "it; outputd refuses that pair at startup and parks. Remove the "
            f"{OUTPUTD_CONTENT_BRIDGE_ENV_VAR} line from outputd.env and re-run "
            "jasper-grouping-reconcile"
        )
    elif normalized == TRANSPORT_OFF_RING:
        # OFF-RING. Reached when either end is off the one transport, so the
        # ring comparisons below have no ring to compare against.
        #
        # NO BRIDGE-VS-PLAN ERROR FOR AN UNDECLARED PAIR. Both terms answer
        # absence with the ring, so together they say nothing about a box the
        # reconciler has not written yet — that box is not on a second route, it
        # is on the ring with nothing written down. Only a coupling that
        # EXPLICITLY names the ring while outputd's bridge does not is a split,
        # and doctor's `check_content_transport_coherence` is its evidence-based owner
        # (it compares the LOADED GRAPH against the bridge). Reaching this shape
        # under such a coupling already means outputd is the end that is off, so
        # the bridge predicate is not re-run here.
        if resolve_coupling(coupling) == COUPLING_SHM_RING:
            errors.append(
                f"transport plan is shm_ring but {OUTPUTD_CONTENT_BRIDGE_ENV_VAR}="
                f"{bridge_label}; Ring A and the post-DSP ring must move together"
            )
        if playback_device == RING_ACTIVE_PLAYBACK_DEVICE:
            # BY NAME, and BEFORE the membership test below. The ACTIVE ring
            # under an off-ring plan is the arm ladder's own step-1 state, so a
            # note: an error here refuses the state the next rung consumes
            # (#2285).
            #
            # Name-only on purpose — `outputd_active_lane_decision` is the ONE
            # arm authority, and a second derivation here is the drift that
            # produced the defect.
            notes.append(
                f"Camilla playback={playback_device!r} while this box is off the "
                "ring is the ACTIVE-ring arm waypoint: the "
                "graph on disk names the active ring (and Ring A on its capture "
                "side — the coupling is end-to-end, so the re-emit moves both "
                "halves) while outputd is still attached to the ring its "
                "unconverged path key names. The running CamillaDSP may still be on the "
                "previously-loaded graph, so this box goes silent at the next "
                "CamillaDSP load and stays silent until the ladder finishes. "
                "Complete it with `systemctl start "
                "jasper-audio-hardware-reconcile` then "
                "`jasper-fanin-coupling-reconcile shm_ring`. There is no rollback "
                "direction: the ring is the one legal ACTIVE endpoint, and an "
                "off-ring roleful box has no content transport at all."
            )
        elif playback_device in UNPAIRED_POST_DSP_PLAYBACK_DEVICES:
            # MEMBERSHIP, not one `==`: the retired snd-aloop ACTIVE lane
            # (#2534) and the stereo ring under an off-ring plan are two
            # contradictions with no documented next step, and both must be
            # reported.
            errors.append(
                f"post-DSP route has no registered outputd capture for "
                f"Camilla playback={playback_device!r}"
            )
    elif normalized in RING_TRANSPORT_SHAPES or normalized == TRANSPORT_DAC_CONTENT_RING:
        # RING A, COMMON TO EVERY SHAPE THAT HAS ONE. Since ADR-0100 the fan-in
        # hop is the same ring on the two central-ring shapes and on a bonded
        # member, so its comparison is hoisted out of them: a graph still
        # sourcing the snd-aloop tap reads a device nobody is writing, which is
        # digital silence with every env and every daemon reading clean, and
        # invisible on the channels axis because Ring A and the tap are both
        # stereo.
        expected_capture = str(
            topology.fanin_to_camilla.get("camilla_capture_device") or ""
        )
        if capture_device and capture_device != expected_capture:
            errors.append(
                f"transport plan is shm_ring but Camilla capture={capture_device!r}; "
                f"expected {expected_capture!r}"
            )
        _compare_lane_channels(topology.fanin_to_camilla, "capture")

        # A DUMB BONDED MEMBER (:data:`TRANSPORT_DAC_CONTENT_RING`) contributes
        # only the Ring A pair above. CamillaDSP does not drive its post-DSP hop
        # — its snapclient does — so the endpoint, bridge and post-DSP-width
        # comparisons below have no subject, and an armed member declares no
        # bridge by construction.
        if normalized in RING_TRANSPORT_SHAPES:
            expected_playback = str(
                topology.camilla_to_outputd.get("camilla_playback_device") or ""
            )
            if normalized == TRANSPORT_SHM_RING_ACTIVE:
                # The armed active endpoint may read ONLY the active ring file,
                # and outputd enforces that pairing as a biconditional at its
                # own startup. This layer reports the pair; it does not gate.
                #
                # WHY A NOTE AND NOT AN ERROR. The two halves are not two facts.
                # The MARKER is the fact — jasper-audio-hardware-reconcile
                # writes it from the accepted active-lane decision — and the
                # PATH is its projection, with exactly one derivation and one
                # writer (jasper.fanin.coupling_reconcile's
                # `_outputd_ring_path_for`, applied by `_outputd_actions` on
                # every pass). A crossed pair is therefore always a projection
                # one pass behind its source, never a disagreement between two
                # independent observations — and refusing on it DEADLOCKED the
                # arm, because the marker cannot be written until the path moves
                # while the path is derived FROM the marker.
                #
                # Safe by construction rather than by permission: outputd
                # REFUSES the crossed pair, so the waypoint is silence and never
                # wrong audio; the pair's own writer converges it on its next
                # pass, which the marker's writer kicks
                # (`jasper-fanin-coupling-auto`); and the device / bridge /
                # format / channel comparisons in this same branch still return
                # ERRORS for a graph that is not actually on the active ring, so
                # this note never stands alone on a wrecked box.
                observed_ring_path = str(topology.camilla_to_outputd.get("path") or "")
                if observed_ring_path != DEFAULT_OUTPUTD_ACTIVE_RING_PATH:
                    notes.append(
                        f"{OUTPUTD_RING_PATH_ENV_VAR}={observed_ring_path!r} under an "
                        f"armed active endpoint is the FIRST-ARM waypoint: the "
                        f"endpoint marker has moved and its ring-path projection has "
                        f"not. An armed active endpoint may read only "
                        f"{DEFAULT_OUTPUTD_ACTIVE_RING_PATH!r}, so outputd refuses "
                        "the pair and this box is silent — never wrong audio — until "
                        "the path's single writer converges it. Complete it with "
                        "`jasper-fanin-coupling-reconcile shm_ring` (the audio-"
                        "hardware reconciler also starts "
                        "jasper-fanin-coupling-auto.service, which runs the same "
                        "pass)."
                    )
            if playback_device and playback_device != expected_playback:
                errors.append(
                    f"transport plan is shm_ring but Camilla playback={playback_device!r}; "
                    f"expected {expected_playback!r}"
                )
            # D5 belt-and-suspenders (wide-output-path program): an ARMED ring's
            # DECLARING ENDS must agree with the wire the resolver resolved.
            # jasper.fanin.coupling_reconcile's ring_edge_width_ready gate
            # refuses to ARM when the emitter's override path is broken; this is
            # the standing coherence check for a box already armed, and it asks
            # a different question — not "does the emitter still force the
            # ring's width" but "do the ends this function can actually observe
            # declare that width".
            #
            # That distinction is why the comparison is against per-end evidence
            # and not against another derivation of the same constant: outputd's
            # declared JASPER_OUTPUTD_CONTENT_FORMAT (the reader's own env — the
            # value that decides which sample_format its attach demands) and
            # CamillaDSP's observed channel counts from the config actually
            # loaded. An end that shears from the resolved wire fails the ring
            # attach; an end this function cannot see (key absent, no Camilla
            # evidence) is not itself an error, matching this module's
            # missing-evidence doctrine.
            #
            # Deliberately equality on every axis, never a "wider/narrower"
            # claim — see ring_edge_width_ready's docstring for why: no
            # width-ranking primitive exists in-repo, and a directional claim
            # would stop being reliably true once a third live format exists
            # (D9, S24_3LE).
            ring_format = str(topology.camilla_to_outputd.get("format") or "")
            outputd_format = str(
                outputd_values.get(OUTPUTD_CONTENT_FORMAT_ENV_VAR, "") or ""
            ).strip()
            if ring_format and outputd_format and outputd_format != ring_format:
                errors.append(
                    f"transport plan is shm_ring with wire format={ring_format!r}, "
                    f"but {OUTPUTD_CONTENT_FORMAT_ENV_VAR}={outputd_format!r}; outputd "
                    "attaches Ring B demanding its own declared format, so the ends "
                    "shear and the attach fails — see ring_edge_width_ready "
                    "(jasper.fanin.coupling_reconcile)"
                )
            _compare_lane_channels(topology.camilla_to_outputd, "playback")
    return TransportCoherenceReport(errors=tuple(errors), notes=tuple(notes))
