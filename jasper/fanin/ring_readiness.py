# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Ring readiness: what this box can be PROVED to declare about the ring.

Everything here READS — the ring platform assets, both geometry axes, the wire
each declaring end states, the loaded CamillaDSP graph, the ACTIVE-ring
endpoint's staging. Nothing here writes env, moves a daemon, or decides an
order; that is :mod:`jasper.fanin.coupling_reconcile`, which composes these
answers into the ordered convergence and is the single writer of the keys they
read. The dependency is one-way: this module imports nothing from that one.

The ``*_ready`` / ``*_proof`` / ``*_converged`` gates answer ``(ok, detail)``
and fail CLOSED, so a box whose ring cannot be proved is left exactly as
it was found and the move is declined (never a fallback — ADR-0100); each
gate's own docstring owns why.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from jasper import fanin_coupling, ring_assets
from jasper.camilla_config_contract import parse_camilla_devices_config
from jasper.env_file import read_value
from jasper.env_load import BASE_ENV_PATH, FANIN_ENV_PATH, OUTPUTD_ENV_PATH
from jasper.fanin_coupling import (
    OUTPUTD_CONTENT_BRIDGE_ENV_VAR,
    OUTPUTD_CONTENT_FORMAT_ENV_VAR,
    OUTPUTD_DEFAULT_CONTENT_FORMAT,
    RING_A_CHANNELS,
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_CAPTURE_DEVICE,
    RING_PCM_DEVICES,
    RING_SLOTS_ENV_VAR,
    RING_WIRE_FORMAT_ENV_VAR,
    RING_WIRE_FORMAT_WIDE,
)


# A ring readiness gate returns (ok, detail) and fails CLOSED.
RingGate = Callable[[], tuple[bool, str]]


@dataclass(frozen=True)
class _EnvSnapshot:
    path: Path
    text: str
    existed: bool


@dataclass(frozen=True)
class FaninRingSlotsResolution:
    """Effective Ring-A slot resolution for the fan-in systemd env chain."""

    value: int | None
    source: str
    raw: str | None
    error: str = ""


def _read_snapshot(path: str | Path) -> _EnvSnapshot:
    env_path = Path(path)
    try:
        return _EnvSnapshot(env_path, env_path.read_text(encoding="utf-8"), True)
    except OSError:
        return _EnvSnapshot(env_path, "", False)


# outputd's own declarations, from the reconciler-owned outputd.env. The FORMAT
# key is written by jasper-audio-hardware-reconcile, not by this reconciler,
# which is why the width gate compares it only on a reconciled box. The CHANNELS
# key is the DacProfile's active-lane width.
#
# BOTH keys DECLARE A DEFAULT when absent rather than being indeterminate, and
# the defaults are the daemon's own (``config.rs``: an unset CONTENT_FORMAT
# resolves ``SampleFormat::S16Le``, an unset ACTIVE_CHANNELS resolves 2 on a
# single-ALSA sink). Reading an absent key as "unknown" would refuse the arm for
# a wire the daemon has in fact declared.
_OUTPUTD_ACTIVE_CHANNELS_ENV_VAR = "JASPER_OUTPUTD_ACTIVE_CHANNELS"
_OUTPUTD_DEFAULT_CONTENT_CHANNELS = 2


# Which ring a declaration is held to, on the CHANNELS axis. Named rather than
# spelled as bare strings at each construction site: a typo'd literal would
# silently fall through to whichever branch the comparison ends with.
RING_A = "A"
RING_B = "B"
RING_ACTIVE = "ACTIVE"


@dataclass(frozen=True)
class RingWireDeclaration:
    """One declaring end's statement of the ring wire, and where it was read.

    ``sample_format`` / ``channels`` are ``None`` for an axis this end does not
    declare — not a wildcard, but "this end is silent here", which the comparison
    reports rather than passes.

    ``ring`` is :data:`RING_A`, :data:`RING_B` or :data:`RING_ACTIVE` and selects
    which channel count this end is held to. The three are separate axes: Ring A
    is always the stereo program fan-in mixes, Ring B follows the box's output
    topology, and the ACTIVE ring carries a roleful box's post-crossover
    per-driver width.

    ``channels_excused`` marks an end that STRUCTURALLY states no channel count,
    a different fact from one that tried and could not — only the latter is an
    indeterminate declaration the gate refuses. A per-axis flag rather than a
    reuse of ``note``: the outputd end's note excuses its FORMAT before arming
    and must not also excuse its channels, which ARE compared on an unarmed box.
    """

    end: str
    source: str
    ring: str
    sample_format: str | None = None
    channels: int | None = None
    note: str = ""
    channels_excused: bool = False


@dataclass(frozen=True)
class LoadedCamillaGraph:
    """ONE snapshot of the CamillaDSP graph the durable statefile points at.

    A snapshot object rather than three field reads: the width gate compares a
    lane's device, format and channels together, and one file read per field
    gives three answers that need not come from one revision of it. ``devices``
    is :func:`~jasper.camilla_config_contract.parse_camilla_devices_config`'s
    subset over that single read.

    ``note`` is empty when the graph WAS read, and otherwise says why not. Never
    an exception: a box with no statefile yet is the ordinary fresh-install
    state.

    ``text`` is the file's raw text, carried so a caller inspecting something the
    ``devices:`` subset does not model (the per-output MUTE proof) works off the
    SAME read. Empty whenever ``note`` is set.
    """

    path: str
    devices: Mapping[str, Any]
    note: str = ""
    text: str = ""


def read_loaded_camilla_graph(config_path: str | None = None) -> LoadedCamillaGraph:
    """Read the loaded CamillaDSP graph once, for the callers that compare it.

    Statefile -> ``config_path`` -> the config's ``devices:`` subset, through the
    same public reader (``read_camilla_statefile_config_path``) every other
    surface uses, so this adds no copy of the statefile scan and honours
    ``JASPER_CAMILLA_STATEFILE``.

    ``config_path`` OVERRIDES the statefile read, because the statefile is the
    WEAKER of the two answers to "which graph is loaded": it is a durable pointer
    with several writers, so it can move while the running daemon still holds the
    previous graph. A caller holding the DAEMON's own answer
    (``reconcile_current_dsp``'s ``current_config_path``) passes it here.
    """
    from jasper.active_speaker.environment import (  # lazy: import cost, tests pin that importing this module stays off the active-speaker tree (ADR-0226)
        read_camilla_statefile_config_path,
    )

    config_path = config_path or read_camilla_statefile_config_path()
    if not config_path:
        return LoadedCamillaGraph(
            path="",
            devices={},
            note="no CamillaDSP statefile config_path to read",
        )
    try:
        text = Path(config_path).read_text(encoding="utf-8")
    except OSError as exc:
        return LoadedCamillaGraph(
            path=config_path,
            devices={},
            note=f"{config_path} is unreadable ({exc.strerror or type(exc).__name__})",
        )
    devices = parse_camilla_devices_config(text)
    if not devices:
        return LoadedCamillaGraph(
            path=config_path,
            devices={},
            note=f"{config_path} declares no parseable devices block",
        )
    return LoadedCamillaGraph(path=config_path, devices=devices, text=text)


def load_topology_for_wire():
    """The saved output topology for a wire resolution, or ``None``.

    Fail-SOFT on every error: ``resolve_ring_wire(None)`` answers the shipped
    stereo geometry, and refusing to arm on an unreadable topology is
    :func:`ring_topology_ready`'s decision, not this helper's.
    """
    from jasper.output_topology import (  # lazy: import cost — jasper.audio_runtime_plan defers this module for the same reason (ADR-0226)
        OutputTopologyError,
        load_output_topology_strict,
    )

    try:
        return load_output_topology_strict()
    except (OutputTopologyError, OSError, ValueError):
        return None


def saved_topology_reader() -> Callable[[], Any]:
    """A :func:`load_topology_for_wire` that touches the disk at most once.

    One pass hands the same reader to every consumer, so the axes that need the
    saved topology share a single read and the axes that do not read nothing.
    """
    return cache(lambda: load_topology_for_wire())


def _effective_env_value(
    later_text: str, key: str, *, later_path: str
) -> tuple[str | None, str]:
    """What a two-file ``EnvironmentFile=`` chain resolves for ``key``, and from where.

    Every audio daemon this module gates lists ``/etc/jasper/jasper.env`` as its
    FIRST ``EnvironmentFile=`` and its own ``/var/lib/jasper/<daemon>.env`` as a
    LATER one, so the later file wins and the earlier is the fallback.
    ``outputd_latency_floor_actions`` relies on this: it REMOVES the generated key
    from the later file so the earlier one is the only declaration left.

    ``later_text`` is the caller's already-read snapshot of the later file;
    ``jasper.env`` is read here. Returns the RAW string and its source path,
    applying no emptiness or parse policy — each caller's vocabulary for
    "declared but empty" differs. ``source`` is meaningful only when the value is
    not ``None``.
    """
    raw = read_value(later_text, key)
    if raw is not None:
        return raw, later_path
    return read_value(_read_snapshot(BASE_ENV_PATH).text, key), BASE_ENV_PATH


def resolve_effective_fanin_wire_format(fanin_text: str) -> tuple[str, str]:
    """fan-in's declared Ring-A wire format, and which file declared it.

    Same ``jasper.env`` -> ``fanin.env`` chain systemd gives ``jasper-fanin``:
    looking only at ``fanin.env`` would report the default while an operator's
    value in the earlier system env still controls the next daemon start.

    THE UNSET CASE GOES THROUGH THE RESOLVER'S OWN NORMALIZER, never a default
    restated here, so this end stays honest across a change to that default.

    An unrecognized token is returned VERBATIM rather than raised on: this
    function reports what an end declares, and
    :func:`ring_edge_width_ready`'s comparison against the resolved wire turns a
    bad token into a refusal. Raising here would throw mid-arm from a reader
    whose job is to describe.

    THIS END IS THE RESOLVER'S INPUT: it agrees by construction on the live path,
    and stays a declaration only because this reader takes the caller's fanin.env
    TEXT while the resolver reads the FILE. The independent witnesses are the
    conf.d, outputd's env and the loaded graph.
    """
    raw, source = _effective_env_value(
        fanin_text, RING_WIRE_FORMAT_ENV_VAR, later_path=FANIN_ENV_PATH
    )
    if raw is None or not raw.strip():
        return fanin_coupling.resolve_ring_wire_format(None), "default"
    return raw.strip(), source


def graph_wire_declarations(
    graph: LoadedCamillaGraph,
) -> tuple[RingWireDeclaration, ...]:
    """What the LOADED CamillaDSP graph declares, for each lane that IS a ring.

    The graph is a declaring end only for a lane whose device is one of the three
    ring PCMs (:data:`~jasper.fanin_coupling.RING_PCM_DEVICES`): a lane on the
    dsnoop capture or the ALSA active lane declares a width for a transport that
    is not the ring. So this returns ZERO declarations on an unarmed box and one
    or two on an armed one.

    Both lanes are inspected: the ring reaches a graph from either side (Ring A
    is CamillaDSP's capture, Ring B and the ACTIVE ring its playback).
    """
    declarations: list[RingWireDeclaration] = []
    for lane in ("capture", "playback"):
        device = graph.devices.get(f"{lane}_device")
        if not isinstance(device, str) or device not in RING_PCM_DEVICES:
            continue
        if device == RING_CAPTURE_DEVICE:
            ring = RING_A
        elif device == RING_ACTIVE_PLAYBACK_DEVICE:
            ring = RING_ACTIVE
        else:
            ring = RING_B
        raw_format = graph.devices.get(f"{lane}_format")
        raw_channels = graph.devices.get(f"{lane}_channels")
        declarations.append(
            RingWireDeclaration(
                end=f"loaded CamillaDSP graph ({lane} {device})",
                source=graph.path,
                ring=ring,
                sample_format=raw_format if isinstance(raw_format, str) else None,
                channels=raw_channels if isinstance(raw_channels, int) else None,
            )
        )
    return tuple(declarations)


def ring_wire_declarations(
    *,
    fanin_text: str,
    outputd_text: str,
    graph: LoadedCamillaGraph | None = None,
) -> tuple[RingWireDeclaration, ...]:
    """What each of the ring's declaring ends says the wire is.

    The outputd FORMAT axis is compared only once the coupling reconciler has
    written ``outputd.env`` at all (its bridge key is present), because
    ``JASPER_OUTPUTD_CONTENT_FORMAT`` is written by
    ``jasper-audio-hardware-reconcile``, not by this reconciler: on a box neither
    pass has touched, the value is whatever the LAST hardware reconcile rendered,
    and comparing it would refuse every box mid-convergence.

    ``graph`` adds the loaded CamillaDSP graph's own ring lanes
    (:func:`graph_wire_declarations`). Omitting it is legal (an env-only
    comparison); the caller is what refuses to CLAIM the graph agreed when it was
    not passed.
    """
    # An ABSENT conf.d is ``ring_assets_ready``'s refusal to own. A conf.d that
    # is PRESENT but declares no readable wire is a torn file no other gate
    # inspects, so that one stays this gate's to refuse.
    conf_present = ring_assets.ring_asset_presence().conf_present
    conf_absent_note = (
        ""
        if conf_present
        else f"{ring_assets.RING_CONF_D} absent — ring_assets_ready owns that refusal"
    )
    fanin_format, fanin_source = resolve_effective_fanin_wire_format(fanin_text)
    outputd_channels_raw = read_value(outputd_text, _OUTPUTD_ACTIVE_CHANNELS_ENV_VAR)
    try:
        outputd_channels = (
            int(outputd_channels_raw.strip())
            if outputd_channels_raw and outputd_channels_raw.strip()
            else _OUTPUTD_DEFAULT_CONTENT_CHANNELS
        )
    except ValueError:
        outputd_channels = None
    reconciled = bool(
        (read_value(outputd_text, OUTPUTD_CONTENT_BRIDGE_ENV_VAR) or "").strip()
    )
    outputd_format_raw = read_value(outputd_text, OUTPUTD_CONTENT_FORMAT_ENV_VAR)
    outputd_format = (
        outputd_format_raw.strip()
        if outputd_format_raw and outputd_format_raw.strip()
        else OUTPUTD_DEFAULT_CONTENT_FORMAT
    )
    return (
        RingWireDeclaration(
            end="fan-in (Ring A writer)",
            source=fanin_source,
            ring=RING_A,
            sample_format=fanin_format,
            # fan-in's mixer is stereo and NOT configurable
            # (``mixer.rs``'s ``CHANNELS: u32 = 2``). Comparing it catches a
            # resolver answering a Ring A width the writer cannot produce.
            channels=RING_A_CHANNELS,
        ),
        RingWireDeclaration(
            end=f"conf.d {ring_assets.RING_A_CONF_PCM}",
            source=ring_assets.RING_CONF_D,
            ring=RING_A,
            sample_format=ring_assets.ring_conf_format(ring_assets.RING_A_CONF_PCM) if conf_present else None,
            channels=ring_assets.ring_conf_channels(ring_assets.RING_A_CONF_PCM) if conf_present else None,
            note=conf_absent_note,
            # An ABSENT conf.d states nothing on either axis and the asset
            # gate owns that refusal; a PRESENT one that cannot be parsed is a
            # torn file whose channels line this gate must refuse.
            channels_excused=not conf_present,
        ),
        RingWireDeclaration(
            end=f"conf.d {ring_assets.RING_B_CONF_PCM}",
            source=ring_assets.RING_CONF_D,
            ring=RING_B,
            sample_format=ring_assets.ring_conf_format(ring_assets.RING_B_CONF_PCM) if conf_present else None,
            channels=ring_assets.ring_conf_channels(ring_assets.RING_B_CONF_PCM) if conf_present else None,
            note=conf_absent_note,
            channels_excused=not conf_present,
        ),
        RingWireDeclaration(
            end="CamillaDSP emitted stanzas",
            source="capture_kwargs_for_coupling(shm_ring)",
            ring=RING_B,
            sample_format=fanin_coupling.content_lane_format_for_coupling(),
            note="counterfactual: what arming would emit",
            # The coupling's kwargs carry a format and no channel count — this
            # end genuinely has nothing to say on that axis, ever.
            channels_excused=True,
        ),
        RingWireDeclaration(
            end="outputd (Ring B reader)",
            source=str(OUTPUTD_ENV_PATH),
            ring=RING_B,
            sample_format=outputd_format if reconciled else None,
            channels=outputd_channels,
            note=(
                ""
                if reconciled
                else (
                    f"{OUTPUTD_CONTENT_FORMAT_ENV_VAR} still declares "
                    "whatever the hardware reconciler last rendered until it "
                    "re-emits on arm, so the format axis is not compared "
                    "before arming"
                )
            ),
        ),
        *(graph_wire_declarations(graph) if graph is not None else ()),
    )


def resolve_wire_for_gate(topology: Any = None) -> tuple[Any | None, str]:
    """``(wire, "")`` — or ``(None, why)`` when the box declares an illegal wire.

    ``resolve_ring_wire`` FAILS LOUD on a ``JASPER_FANIN_RING_WIRE_FORMAT`` value
    neither language recognizes, which is right for an emitter and wrong for a
    GATE: the arm has already written the ring env by the time the preflights
    run, and an uncaught exception would skip the snapshot restore that makes a
    refused arm non-destructive.

    So every gate that needs the wire resolves it through here, and a gate added
    later gets the behaviour by using it.
    """
    try:
        return fanin_coupling.resolve_ring_wire(topology), ""
    except ValueError as exc:
        return None, (
            f"{exc} — refusing to arm on a wire this box cannot declare; "
            "fails closed and leaves the box exactly as it was found — "
            "never a fallback (ADR-0100)"
        )


def _wire_channels_for_ring(ring: str, wire: Any) -> int | None:
    """Which of the resolved wire's three channel fields ``ring`` is held to.

    Keyed on the ring TOKEN an end carries rather than on a PCM name, because
    two of this gate's ends (fan-in's env, outputd's env) name no device at all.
    ``None`` only for :data:`RING_ACTIVE` on a box whose wire resolves no active
    width — the caller treats that as unproven, never as a wildcard.
    """
    if ring == RING_A:
        return int(wire.ring_a_channels)
    if ring == RING_B:
        return int(wire.ring_b_channels)
    active = wire.ring_active_channels
    return None if active is None else int(active)


def ring_edge_width_ready(
    *,
    fanin_text: str | None = None,
    outputd_text: str | None = None,
    graph: LoadedCamillaGraph | None = None,
) -> tuple[bool, str]:
    """The shm_ring PREFLIGHT gate: do ALL the declaring ends state one wire?

    THE INVARIANT. For each ring, ``(sample_format, channels)`` is resolved once
    per box by ``jasper.fanin_coupling.resolve_ring_wire``, and every end that
    declares a geometry must declare exactly that. Any end that cannot ⇒ refuse
    to arm: the gate fails closed and leaves the box exactly as it was found —
    never a fallback (ADR-0100) — naming the end and the value it declared.
    **Equality only, never a ranking**: no width-comparison primitive exists
    in-repo for ALSA format strings, and ``S24_3LE`` — live on the DAC edge —
    breaks any ordering by byte count.

    THE ENDS, and what each contributes:

    - **fan-in** — ``JASPER_FANIN_RING_WIRE_FORMAT`` off the daemon's own env
      chain, plus its compile-time stereo mixer width. Its FORMAT axis is the
      resolver's own input, so it agrees by construction on the live path;
    - **the conf.d** — both stereo PCM blocks, PER BLOCK, because Ring A and
      Ring B may legitimately differ on channels and only the file says what the
      ioplug will attach with. The ACTIVE conf.d block is deliberately NOT one of
      this gate's ends — ``active_ring_endpoint_proof`` proves it on its own
      path, with its own remedy;
    - **CamillaDSP's emitted stanzas** — the counterfactual "what would arming
      emit", which catches ``capture_kwargs_for_coupling`` ever stopping forcing
      the ring's own format (the emit would then fall back to the box-wide
      program-lane default and mis-transcode every sample);
    - **outputd** — its declared content format (once reconciled) and its
      active-lane channel width;
    - **the LOADED CamillaDSP graph** — the config the statefile points at, for
      each lane whose device IS a ring PCM. The counterfactual stanza end answers
      what arming WOULD emit for the STEREO ring, not what is on this box's disk,
      and on the ACTIVE-ring ladder it is not even the same ring; without this
      end a shear between the resolver and the re-emitted graph would pass
      unreported.

    NOT INSPECTED IS NOT REFUSED. A box that has not armed yet loads a non-ring
    graph and a fresh box has no statefile, so an absent graph end costs the
    message its claim, never the arm its verdict.

    ORDERING. Runs after topology eligibility: on a box that resolves no ring
    width the wire question is not well-posed and a mismatch report would name
    the wrong defect.

    ``fanin_text`` / ``outputd_text`` / ``graph`` default to reading their
    sources, so the gate stays callable with no arguments; the arm path passes
    the snapshots it has already written. Each source is read ONCE per call.
    """
    if fanin_text is None:
        fanin_text = _read_snapshot(FANIN_ENV_PATH).text
    if outputd_text is None:
        outputd_text = _read_snapshot(OUTPUTD_ENV_PATH).text
    if graph is None:
        graph = read_loaded_camilla_graph()

    wire, wire_problem = resolve_wire_for_gate(load_topology_for_wire())
    if wire is None:
        return False, wire_problem
    declarations = ring_wire_declarations(
        fanin_text=fanin_text,
        outputd_text=outputd_text,
        graph=graph,
    )

    problems: list[str] = []
    for decl in declarations:
        if decl.sample_format is not None and decl.sample_format != (
            wire.sample_format
        ):
            problems.append(
                f"{decl.end} declares format {decl.sample_format} "
                f"(from {decl.source})"
            )
        elif decl.sample_format is None and not decl.note:
            problems.append(
                f"{decl.end} declares no format at all (from {decl.source}) — "
                "an indeterminate end cannot be proven to match"
            )
        want_channels = _wire_channels_for_ring(decl.ring, wire)
        if want_channels is None:
            # Reachable only on the ACTIVE ring: the wire resolves no active
            # width (a non-roleful box, or a roleful one whose sink cannot carry
            # one). ``None`` there means "this box has no active ring", never
            # "any width matches", so an end declaring a width against it is
            # unproven rather than agreed.
            if decl.channels is not None:
                problems.append(
                    f"{decl.end} declares {decl.channels} channels, but this "
                    f"box's wire resolves NO active-ring width at all (from "
                    f"{decl.source}) — there is nothing to prove that against"
                )
        elif decl.channels is not None and decl.channels != want_channels:
            problems.append(
                f"{decl.end} declares {decl.channels} channels, expected "
                f"{want_channels} (from {decl.source})"
            )
        elif decl.channels is None and not decl.channels_excused:
            # SYMMETRY WITH THE FORMAT AXIS: an end that meant to state a
            # channel count and could not is indeterminate. The shapes that
            # reach here are a PRESENT conf.d whose block declares ``channels``
            # twice with different values, or an outputd key that will not parse
            # as an int.
            problems.append(
                f"{decl.end} declares no channel count at all (from "
                f"{decl.source}) — an indeterminate end cannot be proven to match"
            )
    if problems:
        return False, (
            f"the ring wire resolves to {wire.sample_format} / Ring A "
            f"{wire.ring_a_channels}ch / Ring B {wire.ring_b_channels}ch, but "
            "these ends disagree: "
            + "; ".join(problems)
            + ". Every declaring end must state the SAME wire or the gate "
            "fails closed and leaves the box exactly as it was found — "
            "never a fallback (ADR-0100) — until they agree"
        )
    # The COUNT and the NAMES come from the declarations actually compared, so
    # the message cannot outlive an end being dropped from the list.
    inspected = ", ".join(decl.end for decl in declarations)
    graph_inspected = any(
        decl.end.startswith("loaded CamillaDSP graph") for decl in declarations
    )
    graph_clause = (
        ""
        if graph_inspected
        else (
            "; the loaded CamillaDSP graph was NOT one of them ("
            + (graph.note or "it names no ring PCM on either lane")
            + ") — it becomes a declaring end once the arm's first rung has "
            "re-emitted it against the ring"
        )
    )
    return True, (
        f"{len(declarations)} declaring ends state one ring wire "
        f"({wire.sample_format}, Ring A {wire.ring_a_channels}ch, Ring B "
        f"{wire.ring_b_channels}ch): {inspected}{graph_clause}"
    )


def ring_wire_caps_ready() -> tuple[bool, str]:
    """The shm_ring PREFLIGHT gate: can the INSTALLED ioplug open this wire?

    A RECORD COMPARE, never an open-probe: on an armed box the ioplug's SPSC
    guard EBUSYs a probe, and probing a live ring is the disturbance the doctor's
    armed-skip exists to avoid. The installer records the sha and capability set
    of the ``.so`` it installed (``deploy/lib/install/ring-platform.sh``) and
    this compares that record against the resolved wire's needs — see
    :func:`jasper.ring_assets.ring_ioplug_wire_supported`.

    THE WALK IT CLOSES. The ioplug build degrades to a WARN, so a failed rebuild
    leaves the PREVIOUS ``.so`` installed beside freshly-installed Rust daemons.
    If the resolved wire renders a conf.d ``format`` / ``channels`` key that old
    plugin does not parse, it refuses the device at ``open()`` with ``-EINVAL``
    and CamillaDSP cannot start against the ring — a crash loop on an
    ALREADY-armed box. Such a box is REFUSED here (its content lane parks,
    ADR-0178) rather than arming into a CamillaDSP that cannot open the ring;
    ``jasper-doctor``'s ``ring ioplug provenance`` check reports the state with
    the redeploy remedy first.

    The short-circuit — no record read at all — survives for one shape: a box an
    operator has pinned to ``S16_LE``, the ioplug's own compiled-in conf.d
    default.

    An unparseable declaration is refused here rather than raised — see
    :func:`resolve_wire_for_gate` for why a gate must not throw mid-arm.
    """
    wire, wire_problem = resolve_wire_for_gate(load_topology_for_wire())
    if wire is None:
        return False, wire_problem
    support = ring_assets.ring_ioplug_wire_supported(wire)
    return support.ok, support.detail


def ring_assets_ready() -> tuple[bool, str]:
    """The shm_ring PREFLIGHT gate: are the ring-platform assets present?

    Fail-SAFE: if the ioplug ``.so`` / conf.d / ``/dev/shm/jts-ring`` are not all
    present, arming would install a CamillaDSP config whose ring devices cannot
    resolve — CamillaDSP would crash-loop on its statefile and the fan-in
    ``StartLimitAction=reboot`` could compound it. So the reconciler leaves the
    box exactly as it was found, never a fallback (ADR-0100).

    Presence-only; the doctor owns the deep open-probe, and
    ``jasper.ring_assets`` is the SSOT shared with ``check_ring_platform_assets``.
    """
    presence = ring_assets.ring_asset_presence()
    if presence.all_present:
        return True, "ring platform assets present (ioplug .so + conf.d + shm dir)"
    return False, "ring platform assets incomplete: " + "; ".join(presence.missing())


def active_ring_endpoint_proof() -> tuple[bool, str]:
    """Is this box's ACTIVE-ring endpoint actually staged? Two independent facts.

    A roleful topology having an active ring WIDTH says only that a ring could
    exist for it. Arming needs the endpoint to be STAGED, and that is two
    separate things, owned by two different writers:

    1. **The marker** — ``JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT`` in
       ``outputd.env``, written by ``jasper-audio-hardware-reconcile`` from the
       accepted active-lane decision. It is what tells outputd to expect the
       active ring, and outputd's own allowlist bails if the ring path and this
       marker disagree. Arming ahead of it would flip the coupling into a daemon
       that refuses the pairing — an exit-78 park, not a working ring.
    2. **The rendered conf.d block** — ``pcm.jts_ring_active_playback`` declaring
       this box's resolved active width. The ioplug attaches with what the block
       says; a block still on the shipped default while the graph declares a
       different width is a guaranteed attach failure.

    Both are checked: they have different failure modes and different remedies,
    so one collapsed reason would send an operator to the wrong fix. Fail-CLOSED
    on anything indeterminate — an unreadable conf.d declares nothing.
    """
    from jasper.active_speaker.runtime_contract import (  # lazy: import cost, tests pin that importing this module stays off the active-speaker tree (ADR-0226)
        active_ring_channels_for_topology,
    )

    if not fanin_coupling.ring_active_endpoint_armed():
        return False, (
            "outputd's active-ring endpoint marker "
            "(JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT in outputd.env) is not set — "
            "run `sudo systemctl start jasper-audio-hardware-reconcile` first so "
            "the endpoint pair is written from the active-lane decision, then "
            "re-arm"
        )
    topology = load_topology_for_wire()
    width = (
        active_ring_channels_for_topology(topology) if topology is not None else None
    )
    if width is None:
        return False, (
            "the saved topology resolves no active-ring width, so there is no "
            "width the conf.d block could be proved against"
        )
    declared = ring_assets.ring_conf_channels(ring_assets.RING_ACTIVE_CONF_PCM)
    if declared is None:
        return False, (
            f"the ring conf.d declares no readable channels for "
            f"pcm.{ring_assets.RING_ACTIVE_CONF_PCM} (absent, torn, or unreadable) — redeploy "
            "to reinstall it, then re-run jasper-audio-hardware-reconcile to "
            "render the per-box wire"
        )
    if declared != width:
        return False, (
            f"pcm.{ring_assets.RING_ACTIVE_CONF_PCM} declares channels={declared} but this "
            f"box's active ring resolves to {width} — the ioplug attaches with "
            "what the block says, so this would fail the attach. Run `sudo "
            "systemctl start jasper-audio-hardware-reconcile` to render the "
            "conf.d wire, then re-arm"
        )
    return True, (
        f"active-ring endpoint staged (marker set, pcm.{ring_assets.RING_ACTIVE_CONF_PCM} "
        f"declares channels={declared})"
    )


def _anchor_is_all_muted(graph: LoadedCamillaGraph) -> tuple[bool, str]:
    """Prove EVERY output of ``graph`` ends in a wired hard mute. (ok, why-not).

    MEASURED, not inferred from a FILENAME: a graph at the published anchor path
    with one output unmuted, or one at full scale, would pass a filename check
    while failing the claim "an all-muted anchor hosts no EQ". This acceptance is
    the last thing between a graph at the ring endpoint and the drivers.

    THE WHOLE PROOF. Asks
    :func:`~jasper.active_speaker.graph_safety.output_terminally_muted` for every
    output the graph declares: (1) the repo's one mute idiom, an
    ``as_out{i}_commission_mute`` ``Gain`` at :data:`STARTUP_MUTE_GAIN_DB` with
    ``mute: true`` AND wired to channel ``i``; (2) the mute is TERMINAL — last
    name in its own ``Filter`` step, no later step touching the channel; (3) no
    ``bypassed`` step anywhere.

    Fact 2 is why this calls the primitive rather than composing fact 1 itself:
    fact 1 alone is satisfied by a graph appending a ``+240 dB`` ``Gain`` after
    the mute, appending that gain INTO the mute step's own ``names`` list, or
    appending a ``Dither`` step, which generates signal into a muted channel.

    The width comes from the graph's own ``playback_channels``, which the caller
    has already held to the topology-derived active-ring width.

    THE BYPASSED PRE-SCAN IS FOR THE MESSAGE, NOT THE VERDICT: the primitive
    already refuses a bypassed graph but returns a bare ``bool``.

    Fails closed on every shape it cannot read: unparseable YAML, a non-mapping
    document, a missing or non-positive channel count.
    """
    from jasper.active_speaker.camilla_yaml import (  # lazy: import cost, tests pin that importing this module stays off the active-speaker tree (ADR-0226)
        STARTUP_MUTE_GAIN_DB,
        output_commission_mute_name,
    )
    from jasper.active_speaker.graph_safety import (  # lazy: import cost, tests pin that importing this module stays off the active-speaker tree (ADR-0226)
        output_terminally_muted,
        view_from_yaml_dict,
    )

    try:
        payload = yaml.safe_load(graph.text)
    except yaml.YAMLError as exc:
        return False, f"its YAML does not parse ({type(exc).__name__})"
    if not isinstance(payload, dict):
        return False, "its YAML is not a mapping"

    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, list):
        return False, "it declares no readable pipeline"
    if any(
        isinstance(step, dict) and step.get("bypassed") is True for step in pipeline
    ):
        return False, (
            "its pipeline carries a bypassed step — CamillaDSP skips one "
            "entirely, so a mute behind it is not a mute"
        )

    width = graph.devices.get("playback_channels")
    if not isinstance(width, int) or isinstance(width, bool) or width < 1:
        return False, f"it declares no usable playback channel count ({width!r})"

    view = view_from_yaml_dict(payload)
    unmuted = [
        index
        for index in range(width)
        if not output_terminally_muted(
            payload,
            view,
            index,
            mute_name=output_commission_mute_name(index),
            mute_gain_db=STARTUP_MUTE_GAIN_DB,
        )
    ]
    if unmuted:
        return False, (
            "these outputs are not held at the startup mute floor "
            f"({STARTUP_MUTE_GAIN_DB:g} dB, muted, wired and TERMINAL — nothing "
            "after the mute touches the channel): "
            + ", ".join(str(index) for index in unmuted)
        )
    return True, ""


def _staged_anchor_identity(graph: LoadedCamillaGraph) -> tuple[bool, str]:
    """Is this loaded graph THE artifact the box published as its staged anchor?

    Identity only — nothing about where the graph plays or what it declares.
    Split out because two callers need this exact fact at two different moments:
    :func:`ring_endpoint_anchor_converged` asks it of a box ALREADY at the ring
    endpoint, and :func:`ring_roleful_unattended_ready` asks it of a box that is
    NOT yet armed. Endpoint and wire are what an arm CHANGES, so they cannot be
    part of the identity question without making it unanswerable before the arm.

    Fail-CLOSED on every unreadable or self-contradicting record shape.
    """
    from jasper.active_speaker.staging import load_staged_startup_config  # lazy: import cost, tests pin that importing this module stays off the active-speaker tree (ADR-0226)

    staged = load_staged_startup_config()
    # ``isinstance`` rather than ``(… or {}).get(…)``: that shape raises
    # AttributeError on a record whose ``config`` is a truthy NON-mapping, and an
    # exception escaping the reconciler's ordered arm would skip the snapshot
    # restore that makes a refused arm non-destructive.
    status = staged.get("status")
    config_record = staged.get("config")
    anchor_path = (
        config_record.get("path") if isinstance(config_record, Mapping) else None
    )
    if not anchor_path:
        # The record's SHAPE, not merely its absence: "publishes no anchor"
        # while the record says ``status='staged'`` sends a debugging operator to
        # re-stage when the real defect is a corrupt record.
        malformed = config_record is not None and not isinstance(
            config_record, Mapping
        )
        return False, (
            "this box publishes no usable staged startup anchor "
            + (
                "(record present but malformed: config is "
                f"{type(config_record).__name__}, not a mapping)"
                if malformed
                else f"(staged status={status!r})"
            )
            + ", so the loaded graph cannot be proved to BE one"
        )
    if status != "staged":
        # A ``blocked`` / ``unreadable`` record still carries a path, and
        # accepting it would treat a run the stager REFUSED as a published
        # anchor.
        return False, (
            f"the staged startup anchor record reports status={status!r}, not "
            "'staged' — a record the stager did not accept is not a published "
            "anchor"
        )
    if os.path.realpath(graph.path) != os.path.realpath(str(anchor_path)):
        return False, (
            f"the loaded graph is {graph.path}, which is not this box's "
            f"published startup anchor ({anchor_path})"
        )
    return True, ""


def graph_at_active_ring_endpoint(
    graph: LoadedCamillaGraph,
) -> tuple[bool, str]:
    """Is THIS graph already at the ACTIVE ring endpoint, at this box's wire?

    Two axes, and deliberately only two: the ENDPOINT pair (capture is Ring A
    and playback is the ACTIVE ring — both lanes, because a graph that plays the
    ring while capturing the snd-aloop tap captures a device nobody writes) and
    the WIRE (every ring lane states the box's resolved format AND channel
    width).

    TWO CALLERS. :func:`ring_endpoint_anchor_converged` asks this between its
    anchor-identity and all-muted axes; :mod:`jasper.fanin.converge` asks it
    alone, because its question is narrower — "has the transport move already
    happened to whatever graph is loaded". A COMMISSIONED box rides an applied
    baseline, so anchor identity is false there forever and all-muted is false by
    design, and asking the anchor predicate whole would re-emit its graph at
    every boot, deploy and hotplug.

    Fail-CLOSED on anything indeterminate: a wire this box cannot resolve, a lane
    that declares no format or no channel count.
    """
    capture = graph.devices.get("capture_device")
    playback = graph.devices.get("playback_device")
    if capture != RING_CAPTURE_DEVICE or playback != RING_ACTIVE_PLAYBACK_DEVICE:
        return False, (
            f"the loaded graph captures {capture!r} and plays {playback!r}, "
            f"not the ring endpoint pair (capture {RING_CAPTURE_DEVICE!r} -> "
            f"playback {RING_ACTIVE_PLAYBACK_DEVICE!r})"
        )

    wire, wire_problem = resolve_wire_for_gate(load_topology_for_wire())
    if wire is None:
        return False, wire_problem
    problems: list[str] = []
    for decl in graph_wire_declarations(graph):
        if decl.sample_format != wire.sample_format:
            problems.append(
                f"{decl.end} declares format {decl.sample_format}, expected "
                f"{wire.sample_format}"
            )
        want_channels = _wire_channels_for_ring(decl.ring, wire)
        if want_channels is None:
            problems.append(
                f"{decl.end} declares {decl.channels} channels, but this box's "
                "wire resolves NO active-ring width to prove that against"
            )
        elif decl.channels != want_channels:
            problems.append(
                f"{decl.end} declares {decl.channels} channels, expected "
                f"{want_channels}"
            )
    if problems:
        return False, (
            "the loaded graph is at the ring endpoint but does not state this "
            f"box's ring wire: {'; '.join(problems)}"
        )
    return True, (
        f"at the ACTIVE ring endpoint, stating the box's ring wire "
        f"({wire.sample_format})"
    )


def ring_endpoint_anchor_converged(
    *, loaded_config_path: str | None = None
) -> tuple[bool, str]:
    """Is the loaded graph ALREADY this box's staged anchor at the ring endpoint?

    THE STATE THIS ANSWERS FOR. A mid-commission roleful box boots from the
    all-muted staged startup anchor, not from an applied baseline.
    ``reconcile_current_dsp`` resolves that graph to a carrier with
    ``is_baseline=False``, which correctly refuses to host EQ and reports
    ``skipped``; the arm's camilla step reads every ``skipped`` as "the ring
    config was NOT loaded", so an anchor-riding box would otherwise never pass
    the arm. This proves directly that nothing is left to re-emit.

    Each axis is proved from the artifact on disk, through the owner that already
    answers for it:

    1. **Identity** — the loaded graph's path is the path the box PUBLISHED as
       its staged anchor, on a record whose own ``status`` is ``staged``. The
       loaded path comes from the DAEMON (``loaded_config_path``) and falls back
       to the statefile only when the caller has no daemon answer. A
       commissioning load lives at its own fixed path and therefore fails here:
       a per-driver commissioning graph is a transient with a driver armed at
       level, and must never be read as a converged arm.
    2+3. **Endpoint and wire** — :func:`graph_at_active_ring_endpoint`. The
       CHANNELS axis rides along because this predicate is also consulted on the
       CONFIRM path, where ``ring_edge_width_ready`` does not run; without it an
       armed anchor box whose width later sheared would be reported converged
       forever.
    4. **All-muted** — every output the graph declares ends in a wired hard mute
       at :data:`STARTUP_MUTE_GAIN_DB` (:func:`_anchor_is_all_muted`).

    FAIL-CLOSED on anything indeterminate: an unreadable config, no published
    anchor, a record that is not ``staged``, a lane that declares no format or no
    channel count, an unparseable or unmuted graph.

    NOT a gate in :func:`jasper.fanin.converge._ring_gates` and not a preflight —
    it is the camilla step's own acceptance criterion, consulted only after
    ``reconcile_current_dsp`` has already declined.
    """
    graph = read_loaded_camilla_graph(loaded_config_path)
    if graph.note:
        return False, f"cannot read the loaded CamillaDSP graph ({graph.note})"

    is_anchor, identity_problem = _staged_anchor_identity(graph)
    if not is_anchor:
        return False, identity_problem

    from jasper.active_speaker.camilla_yaml import STARTUP_MUTE_GAIN_DB  # lazy: import cost, tests pin that importing this module stays off the active-speaker tree (ADR-0226)

    at_endpoint, endpoint_detail = graph_at_active_ring_endpoint(graph)
    if not at_endpoint:
        return False, endpoint_detail

    muted, mute_problem = _anchor_is_all_muted(graph)
    if not muted:
        return False, (
            "the graph at the published anchor path is at the ring endpoint but "
            f"is not the all-muted anchor: {mute_problem}"
        )
    return True, (
        f"the loaded graph IS this box's staged startup anchor ({graph.path}), "
        f"{endpoint_detail}, with every output held at the "
        f"{STARTUP_MUTE_GAIN_DB:g} dB startup mute floor; an all-muted anchor "
        "hosts no EQ, so there is nothing left for the camilla step to re-emit"
    )


def composite_ring_wire_ready(topology: Any) -> tuple[bool, str]:
    """May THIS composite sink ride the ACTIVE ring at the wire the box declares?

    **Only at the WIDE wire.** Named on its own so the rule is greppable, but
    wired into exactly ONE call site — :func:`ring_topology_ready`'s ACTIVE arm —
    because both arming paths reach the ring through that gate.

    THE REGRESSION THIS REFUSES is invisible on every other axis: moving a
    composite onto a NARROW ring, changing nothing else, narrows the
    POST-crossover per-driver program from 32 to 16 bits. The rule is that the
    post-crossover hop carries the i32 program spine's width and quantizes ONCE,
    at the DAC edge; a 16-bit hop quantizes early and then again after outputd's
    per-driver gain, trim and protection have scaled it.

    ``ring_edge_width_ready`` cannot catch it: that gate proves every declaring
    end states the SAME wire, and a narrow composite arm is perfectly
    self-consistent. Coherence is not width.

    Since the resolver defaults WIDE, the only shape that reaches this refusal is
    a box an operator has PINNED to ``S16_LE``. Not a policy override of that
    declaration — the key's writer set stays EMPTY, which is what makes it a real
    rollback lever — but a refusal of the unsafe COMBINATION, naming the remedy.

    THE REMEDY NAMES ALL THREE RUNGS. A roleful graph's capture and playback
    formats are baked when it is EMITTED, and the hardware reconciler re-renders
    only the conf.d and outputd's env, so setting the key and re-running the
    reconciler alone leaves the BOOT GRAPH narrow and the next arm refuses again
    from ``ring_edge_width_ready``. The ``sudo /opt/jasper/.venv/bin/…`` spelling
    matches the doctor's own rollback ladder
    (``jasper/cli/doctor/audio_runtime_ring.py``); only that spelling pastes into
    a shell and works.

    Non-composite topologies pass untouched.
    """
    from jasper.active_speaker.runtime_contract import topology_sink_is_composite  # lazy: import cost, tests pin that importing this module stays off the active-speaker tree (ADR-0226)

    if topology is None or not topology_sink_is_composite(topology):
        return True, "not a composite sink; the wide-wire rule does not apply"
    try:
        declared = fanin_coupling.read_declared_ring_wire_format()
    except ValueError as exc:
        # A wire token neither language recognizes. fan-in parks at exit 78 on
        # the same value, so refusing here is the same verdict, earlier.
        return False, (
            f"this box declares an unusable ring wire ({exc}), so the composite "
            "wide-wire rule cannot be proved — fix the token, then re-arm"
        )
    if declared != RING_WIRE_FORMAT_WIDE:
        return False, (
            f"a composite sink may ride the ACTIVE ring only at the WIDE wire, "
            f"but this box declares {RING_WIRE_FORMAT_ENV_VAR}={declared}. The "
            f"WIDE wire ({RING_WIRE_FORMAT_WIDE}) is what carries the "
            f"post-crossover per-driver program at full width, so arming the "
            f"ring at this narrow pin would quantize every driver's signal "
            f"from 32 to 16 bits — a width REGRESSION disguised as a "
            f"transport change, which the every-end wire gate cannot see "
            f"(a narrow arm is perfectly self-consistent). An undeclared box "
            f"resolves the wide wire, so this is a deliberate pin: remove "
            f"{RING_WIRE_FORMAT_ENV_VAR} (or set it to {RING_WIRE_FORMAT_WIDE}) "
            f"in /var/lib/jasper/fanin.env, then re-run the WHOLE three-step arm "
            f"ladder in order: `sudo /opt/jasper/.venv/bin/jasper-active-speaker "
            f"baseline-reemit --endpoint ring && sudo systemctl start "
            f"jasper-audio-hardware-reconcile && sudo /opt/jasper/.venv/bin/"
            f"jasper-fanin-coupling-reconcile shm_ring`. The boot graph's own "
            f"capture and playback formats are baked when it is EMITTED, so "
            f"re-running the hardware reconciler alone leaves a stale narrow "
            f"boot graph and the next arm refuses again. The gate fails "
            f"closed: the box is left exactly as it was found, never a "
            f"fallback."
        )
    return True, (
        f"composite sink declares the wide ring wire ({RING_WIRE_FORMAT_WIDE}), "
        "so the arm does not narrow the per-driver program"
    )


def ring_roleful_unattended_ready() -> tuple[bool, str]:
    """May an unattended pass MOVE a ROLEFUL box's graph? Fail-closed, two arms.

    It refuses by DEFAULT and admits exactly two proven graph shapes. Both arms
    are about the GRAPH's provenance — never about the box's topology SHAPE,
    which :func:`ring_topology_ready` owns one gate later:

    1. **A hardware-fingerprint-matched applied baseline** —
       :func:`~jasper.active_speaker.baseline_profile.applied_baseline_hardware_match`,
       the same predicate the emitter fails closed on. What a converging pass
       then moves is the graph a human already approved for THIS hardware, with
       driver values byte-preserved. A real DAC swap fails the fingerprint and
       lands in the default refusal.
    2. **The all-muted staged anchor** — the loaded graph IS this box's published
       anchor (:func:`_staged_anchor_identity`) AND every output it declares ends
       in a wired terminal mute (:func:`_anchor_is_all_muted`). It emits silence,
       so it cannot be a hearing event on any hardware.

    SCOPE, held deliberately narrow: arm 1 is the fingerprint compare ONLY.
    Applied-record DIVERGENCE (``applied_profile_displacement``) is another
    gate's question.

    Everything else refuses, fail-CLOSED: an unreadable topology, no applied
    record and no anchor, a stale fingerprint, an anchor that is not terminally
    muted. The refusal names the runnable arm so a refused box has a way out.

    A CORRUPT applied record is caught HERE rather than left to the caller:
    ``load_applied_baseline_profile_state`` returns ``None`` for the shapes its
    own loader catches, but a non-UTF-8 byte (the SD-card / power-cut truncation)
    raises ``UnicodeDecodeError`` straight past it, and the caller's ``except``
    would turn that into a refusal carrying no remediation.
    """
    from jasper.active_speaker.baseline_profile import (  # lazy: import cost, pulls scipy via bass_extension (ADR-0226)
        applied_baseline_hardware_match,
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.runtime_contract import classify_output_contract  # lazy: import cost, tests pin that importing this module stays off the active-speaker tree (ADR-0226)
    from jasper.output_topology import (  # lazy: import cost — jasper.audio_runtime_plan defers this module for the same reason (ADR-0226)
        OutputTopologyError,
        load_output_topology_strict,
    )

    try:
        topology = load_output_topology_strict()
        contract = classify_output_contract(topology)
    except (OutputTopologyError, OSError, ValueError) as exc:
        return False, (
            f"topology unreadable ({exc}); an unattended pass cannot prove this "
            "box is not roleful, so it leaves the graph alone (fail-closed)"
        )
    if not contract.requires_roleful_graph:
        return True, "topology is not roleful"

    # Arm 1 — an applied baseline that still matches this hardware.
    stale_detail = ""
    try:
        applied = load_applied_baseline_profile_state()
    except (OSError, ValueError) as exc:
        applied = None
        stale_detail = (
            f"the applied active-speaker record could not be read "
            f"({type(exc).__name__})"
        )
    if applied is not None:
        _snapshot, hardware_issues = applied_baseline_hardware_match(
            topology, applied_profile=applied
        )
        if not hardware_issues:
            return True, (
                "roleful, and this box's applied active-speaker profile still "
                "matches the hardware (topology identity and fingerprint both "
                "current), so a converging pass moves the graph a human already "
                "approved for these drivers"
            )
        stale_detail = "; ".join(
            str(issue.get("code", "")) for issue in hardware_issues
        )

    # Arm 2 — the all-muted staged anchor.
    graph = read_loaded_camilla_graph()
    anchor_detail = graph.note or ""
    if not graph.note:
        is_anchor, identity_problem = _staged_anchor_identity(graph)
        if is_anchor:
            muted, mute_problem = _anchor_is_all_muted(graph)
            if muted:
                return True, (
                    "roleful, but the loaded graph IS this box's published "
                    "all-muted startup anchor, which emits silence on any "
                    "hardware"
                )
            anchor_detail = mute_problem
        else:
            anchor_detail = identity_problem

    return False, (
        "roleful topology, and neither proven arm holds: the applied baseline "
        f"({stale_detail or 'no applied active-speaker profile on this box'}) "
        f"and the all-muted staged anchor ({anchor_detail}). Leaving the graph "
        "where it is; re-apply the speaker profile at /sound/setup/, then run "
        "`jasper-fanin-coupling-reconcile shm_ring`."
    )


def ring_topology_ready(*, strict_unreadable: bool = False) -> tuple[bool, str]:
    """The shm_ring PREFLIGHT gate for topology eligibility.

    TWO admitting arms, because there are two rings:

    - **the STEREO arm** — Ring A/Ring B carry a full-range stereo program on a
      single coherent ALSA sink, so this is legal only for an explicit valid
      passive-stereo output contract. An unconfigured speaker stays silent. It
      consults ``topology_supports_shm_ring``, the single stereo-ring-eligibility
      predicate, so arming a non-eligible box refuses with a crisp reason here
      instead of failing later at outputd's Rust full-range-stereo rejection (a
      confusing daemon-level rollback);
    - **the ACTIVE arm** — a ROLEFUL topology is admitted iff it resolves an
      active-ring width AND :func:`active_ring_endpoint_proof` holds. A ROLEFUL
      COMPOSITE resolves a width (4) and reaches this arm, so it carries one
      extra condition the single-sink shapes do not:
      :func:`composite_ring_wire_ready`, the wide-wire rule. Explicit-mono still
      resolves no active width, and a PASSIVE composite is not roleful at all,
      so both stay refused.

    **Why an arm here and NOT a widening of ``topology_supports_shm_ring``.**
    That predicate has two other consumers which would silently change meaning:
    the unattended ``--auto`` pass would AUTO-ARM every roleful box with the
    marker absent, so outputd would refuse the pairing and park the speaker, and
    ``jasper.sound.camilla_yaml``'s flat-cutover defusal gate protects exactly
    the boxes the widening would re-expose.

    Unreadable-topology policy is caller-selectable:

    - ``strict_unreadable=True``: fail-CLOSED, for a caller deciding whether to
      MOVE a graph. An arm decision taken on an unreadable topology would
      arm→rollback on every boot/deploy the file is transiently corrupt, and
      outputd's own guard is not a sufficient backstop by itself — it fails open
      on that same error (the topology read failure clears the active-lane
      marker, so the stereo predicate then admits the ring).
    - ``strict_unreadable=False``: fail-OPEN, kept for callers that only want the
      topology's OPINION rather than an arm decision.
    """
    from jasper.active_speaker.runtime_contract import (  # lazy: import cost, tests pin that importing this module stays off the active-speaker tree (ADR-0226)
        CONTRACT_UNCONFIGURED,
        active_ring_channels_for_topology,
        classify_output_contract,
        topology_supports_shm_ring,
    )
    from jasper.output_topology import (  # lazy: import cost — jasper.audio_runtime_plan defers this module for the same reason (ADR-0226)
        OutputTopologyError,
        load_output_topology_strict,
    )

    try:
        topology = load_output_topology_strict()
    except (OutputTopologyError, OSError, ValueError) as exc:
        if strict_unreadable:
            # An unreadable topology is NOT proven eligible — fail closed and
            # leave the box exactly as it was found rather than arm a ring we
            # cannot prove is eligible.
            return False, (
                f"topology unreadable ({exc}); fail-closed (box left exactly "
                "as it was found) rather than arm a ring it cannot prove is "
                "eligible"
            )
        return True, f"topology unreadable ({exc}); deferring to outputd's own guard"
    if topology_supports_shm_ring(topology):
        return True, (
            "topology is ring-eligible (declared passive full-range single sink)"
        )
    if classify_output_contract(topology).classification == CONTRACT_UNCONFIGURED:
        return False, (
            "no speaker layout is configured; save a passive stereo layout "
            "before arming the full-range shm_ring coupling"
        )
    if active_ring_channels_for_topology(topology) is not None:
        # Asked BEFORE the endpoint proof: it is a property of the box's own
        # declaration rather than of what the reconciler has staged, so its
        # remedy is actionable whether or not the endpoint is up.
        wire_ok, wire_detail = composite_ring_wire_ready(topology)
        if not wire_ok:
            return False, wire_detail
        proved, detail = active_ring_endpoint_proof()
        if proved:
            return True, f"topology is ACTIVE-ring eligible (roleful); {detail}"
        return False, (
            f"topology resolves an active-ring width, but the endpoint is not "
            f"staged: {detail}"
        )
    # Neither ring fits: a roleful box reaching here resolved no ACTIVE-ring
    # width (an explicit mono, or an indeterminate driven width), and a composite
    # reaches here only when it is PASSIVE. These shapes PARK under their own
    # name (ADR-0178, jasper.control.transport_park) rather than falling back to
    # a second coupling.
    return False, (
        "saved output topology is not ring-eligible (the STEREO shm_ring is a "
        "full-range single-sink coupling; roleful/protected/subwoofer "
        "topologies need a per-driver crossover it cannot carry — those ride "
        "the ACTIVE ring instead, which this box did not qualify for either; a "
        "PASSIVE composite dual-DAC is neither, so it has no ring at all; and "
        "explicit-mono is excluded by policy, not a ring-v2 timing gap). "
        "This box parks under its own name (ADR-0178) rather than falling "
        "back to a second coupling. If this box is actually a plain stereo "
        "single-sink speaker carrying a stale roleful/subwoofer topology, run "
        "`jasper-output-topology-reset` to clear it to an unconfigured state, "
        "save an explicit passive stereo layout, then re-arm."
    )


def resolve_effective_fanin_ring_slots(fanin_text: str) -> FaninRingSlotsResolution:
    """Resolve Ring-A slots from the same env-file order ``jasper-fanin`` uses.

    ``jasper-fanin.service`` reads ``/etc/jasper/jasper.env`` first and
    ``/var/lib/jasper/fanin.env`` last, so the reconciler and doctor must model the
    same chain (:func:`_effective_env_value`). Looking only at ``fanin.env`` can
    report the new default while an old ``JASPER_FANIN_RING_SLOTS=8`` in the
    earlier system env still controls the next daemon start.
    """
    raw, source = _effective_env_value(
        fanin_text, RING_SLOTS_ENV_VAR, later_path=FANIN_ENV_PATH
    )
    if raw is None:
        source = "default"
    try:
        return FaninRingSlotsResolution(
            value=fanin_coupling.resolve_ring_slots(raw),
            source=source,
            raw=raw,
        )
    except ValueError as e:
        return FaninRingSlotsResolution(
            value=None,
            source=source,
            raw=raw,
            error=str(e),
        )
