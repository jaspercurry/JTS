# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-audio-config`` operational diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from jasper.audio_hardware.dac import (
    active_outputd_lane_channels_for,
    latency_floor_for,
)
from jasper.audio_runtime_plan import (
    AUDIO_RUNTIME_OVERRIDE_KEYS,
    DEFAULT_BASE_ENV_PATH,
    DEFAULT_CAMILLA2_STATEFILE_PATH,
    DEFAULT_CAMILLA_STATEFILE_PATH,
    DEFAULT_FANIN_ENV_PATH,
    DEFAULT_GROUPING_ENV_PATH,
    DEFAULT_OUTPUTD_ENV_PATH,
    OUTPUTD_LATENCY_KEYS,
    build_audio_runtime_plan,
    build_audio_runtime_plan_from_system,
    outputd_env_buffer_pair_error,
    output_endpoint_devices_from_statefiles,
    outputd_latency_floor_actions,
    route_owned_env_actions,
    resolve_audio_route_profile,
)
from jasper.transport_coherence import transport_coherence_report
from jasper.camilla_config_contract import (
    ACTIVE_OUTPUTD_PLAYBACK_DEVICE,
    outputd_capture_device_for_playback,
)
from jasper.audio_runtime_overrides import (
    clear_runtime_override,
    load_runtime_overrides,
    runtime_overrides_path,
    set_runtime_override,
)
from jasper.env_load import read_env_file_state
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    RING_ACTIVE_PLAYBACK_DEVICE,
    RING_SLOT_FRAMES,
    resolve_ring_wire,
)
from jasper.ring_assets import RING_CONF_D, render_ring_conf_wire

DEFAULT_OUTPUT_TOPOLOGY_PATH = "/var/lib/jasper/output_topology.json"
# Both transports of the ONE active lane: the snd-aloop active PCM and the
# ACTIVE RING. A graph naming either is an active-lane graph and must pass the
# same hardware/topology proof before its pairing is enforced.
_ACTIVE_ENDPOINT_DEVICES = frozenset(
    (ACTIVE_OUTPUTD_PLAYBACK_DEVICE, RING_ACTIVE_PLAYBACK_DEVICE)
)


def _cmd_explain(args: argparse.Namespace) -> int:
    plan = build_audio_runtime_plan_from_system(
        base_env_path=args.base_env,
        outputd_env_path=args.outputd_env,
        fanin_env_path=args.fanin_env,
        grouping_env_path=args.grouping_env,
        overrides_path=args.overrides,
        output_hardware_state_path=args.output_hardware_state,
    )
    if args.json:
        print(json.dumps(plan.to_dict(), indent=2, sort_keys=True))
        return 0 if not plan.errors else 1

    print("Audio runtime plan")
    print(f"  profile: {plan.profile_id} ({plan.profile_label})")
    print(f"  route: {plan.route_mode}")
    for setting in plan.settings:
        unit = f" {setting.unit}" if setting.unit else ""
        print(
            f"  {setting.key}={setting.value}{unit} "
            f"[{setting.source_kind}: {setting.source}]"
        )
        if setting.override_value is not None:
            print(f"    override: {setting.override_value}")
        if setting.operator_value is not None:
            print(f"    operator: {setting.operator_value}")
        if setting.generated_value is not None:
            print(f"    generated: {setting.generated_value}")
    # The settings above are POLICY; this is the OBSERVATION beside them (see
    # AudioRuntimePlan.camilla_emitted). Both output modes carry it.
    emitted = plan.camilla_emitted
    if emitted is None:
        print("  camilla_emitted: unread")
    else:
        print(
            f"  camilla_emitted: chunksize={emitted.chunksize} "
            f"target_level={emitted.target_level} "
            f"capture={emitted.capture_device} "
            f"playback={emitted.playback_device} "
            f"[{emitted.config_path}]"
        )
    if plan.errors:
        print("Errors:")
        for error in plan.errors:
            print(f"  - {error}")
    if plan.warnings:
        print("Warnings:")
        for warning in plan.warnings:
            print(f"  - {warning}")
    return 0 if not plan.errors else 1


def _cmd_outputd_floor_actions(args: argparse.Namespace) -> int:
    base = read_env_file_state(args.base_env)
    outputd = read_env_file_state(args.outputd_env)
    overrides = load_runtime_overrides(
        args.overrides,
        allowed_keys=AUDIO_RUNTIME_OVERRIDE_KEYS,
    )
    plan = build_audio_runtime_plan(
        base_env=base.values,
        outputd_env=outputd.values,
        overrides=overrides.values(),
        profile_id=args.profile_id,
        route_mode="solo",
        base_env_label=base.path,
        outputd_env_label=outputd.path,
        override_label=args.overrides,
        plan_warnings=overrides.warnings,
    )
    for key in OUTPUTD_LATENCY_KEYS:
        print(f"summary {key} {plan.setting(key).value}")
    for action in outputd_latency_floor_actions(
        profile_id=args.profile_id,
        base_env=base.values,
        outputd_env=outputd.values,
        overrides=overrides.values(),
    ):
        if action.action == "set":
            print(f"set {action.key} {action.value}")
        else:
            print(f"unset {action.key}")
    return 0


def _load_topology_for_ring_wire(path: str) -> tuple[object | None, str]:
    """``(topology, reason_token)`` for the ring-wire resolution, fail-safe.

    An ABSENT topology is not a failure: ``load_output_topology_strict`` returns
    an empty draft for it, because "no saved topology" means "not configured
    yet" — a ring-eligible shape in its own right, not an unknown one. Only a
    CORRUPT or unreadable file yields ``None``, which resolves the
    SHIPPED stereo ring wire — the geometry already on disk in the conf.d. That
    is the fail-safe direction for a RENDERER: an indeterminate topology must
    never move the conf.d off what the box is running, and the arm preflights
    (which do read the topology, strictly) are what refuse to arm on one.
    """
    from jasper.output_topology import OutputTopologyError, load_output_topology_strict

    try:
        return load_output_topology_strict(path), "loaded"
    except (OutputTopologyError, OSError, ValueError):
        return None, "topology_unreadable"


def _cmd_render_ring_conf_wire(args: argparse.Namespace) -> int:
    """Render the shm-ring conf.d wire from a DAC's DECLARED floor + the topology.

    The rule, and the whole of it: a per-box ring conf.d is rendered ONLY from a
    declared ``LatencyFloor`` whose ``outputd_period_frames`` equals
    ``RING_SLOT_FRAMES``. A profile that declares no floor (and an unrecognized
    id) leaves the SHIPPED conf.d untouched and therefore keeps whatever
    coupling that box has today — so this command is a no-op until floor data
    exists for a profile.

    The floor gates the render; the WIRE it renders comes from
    ``jasper.fanin_coupling.resolve_ring_wire`` — format plus a per-ring channel
    count, resolved from the saved output topology. Joining them here rather
    than in either owner is why this command re-reads both instead of taking
    values on the command line.

    Why the second condition: Ring A's slot size is fan-in's COMPILE-TIME
    ``RING_SLOT_FRAMES`` (``rust/jasper-ring/src/layout.rs``, no env override).
    Rendering any other period would make CamillaDSP's ioplug attach against a
    geometry fan-in never builds — a hard ``RING_ATTACH_FATAL`` that CRASHES
    shm_ring at arm instead of refusing it. So a non-matching floor is REFUSED
    here, with its own reason token so the reconcile journal names why. Such a
    DAC still gets the floor's outputd period/buffer geometry through
    ``outputd.env``, which is where most of the floor's value lives; teaching
    the ring slot to follow the floor across
    fan-in, the ioplug, the CamillaDSP emitter, and the negotiation model is
    issue #2147.

    The registry owns the floor and ``jasper.ring_assets`` owns the conf.d
    format; this command only joins them, which is why it re-reads the floor
    rather than taking a period on the command line.

    Emits ``key value`` lines for the shell caller (the ``outputd-floor-actions``
    idiom) and returns non-zero with a reason on stderr when the conf.d cannot
    be rendered.
    """
    conf_d = args.conf_d or RING_CONF_D
    floor = latency_floor_for(args.profile_id) if args.profile_id else None
    if floor is None:
        print("result skipped")
        print("reason no_declared_floor")
        print(f"conf {conf_d}")
        return 0
    if floor.outputd_period_frames != RING_SLOT_FRAMES:
        print("result skipped")
        print(f"reason ring_slot_fixed_{RING_SLOT_FRAMES}")
        print(f"period_frames {floor.outputd_period_frames}")
        print(f"conf {conf_d}")
        return 0
    topology, topology_reason = _load_topology_for_ring_wire(args.output_topology)
    # The floor gate above has already established that this box's declared
    # period is RING_SLOT_FRAMES, and the renderer refuses any other; the wire
    # carries the same axis, so it needs no second comparison here.
    wire = resolve_ring_wire(topology)
    try:
        outcome = render_ring_conf_wire(wire, conf_d=conf_d)
    except (OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"result {'rendered' if outcome.changed else 'unchanged'}")
    print(f"period_frames {outcome.period_frames}")
    if outcome.previous_period_frames is not None:
        print(f"previous_period_frames {outcome.previous_period_frames}")
    print(f"sample_format {outcome.sample_format}")
    print(f"ring_a_channels {outcome.ring_a_channels}")
    print(f"ring_b_channels {outcome.ring_b_channels}")
    print(f"ring_active_channels {outcome.ring_active_channels}")
    print(f"topology {topology_reason}")
    print(f"conf {outcome.conf_d}")
    return 0


def _cmd_renderer_lanes(args: argparse.Namespace) -> int:
    """Arm / disarm renderer-ingress ring lanes — the SINGLE writer of the map.

    Both ends of a lane flip are written together (fan-in's armed set and the
    renderer's ``--device``), into one file both units load last, so a half-flip
    is not representable. Neither end is restarted here: the values take effect
    on each unit's next start, and the operator restarts the pair (or deploys,
    which bounces both). Not restarting is the conservative half — it means
    there is no window in which one end has moved and the other has not.

    ``--arm`` / ``--disarm`` are applied to the CURRENTLY armed set read back
    from the file, so this is idempotent and composable; ``--set`` replaces the
    set outright. With no flags it REPORTS, which is what makes the file's own
    contents the intent record rather than needing a second file.

    Arming preflights the ring platform (ioplug + ``/dev/shm/jts-ring``) and
    refuses when it is absent, because a renderer whose ``jts_ring`` PCM cannot
    resolve is a SILENT source, and silence is the failure mode hardest to
    trace back to this command.
    """
    from jasper import renderer_lanes as rl
    from jasper.ring_assets import ring_asset_presence

    path = args.path or rl.RENDERER_LANES_ENV
    current = list(rl.read_armed_labels(path))

    if args.set is not None:
        desired = rl.parse_armed_labels(args.set)
    else:
        desired_list = list(current)
        for label in args.arm:
            if label not in desired_list:
                desired_list.append(label)
        for label in args.disarm:
            if label in desired_list:
                desired_list.remove(label)
        desired = tuple(desired_list)

    newly_armed = [label for label in desired if label not in current]
    if newly_armed:
        presence = ring_asset_presence()
        lane_conf = os.path.exists(rl.RENDERER_LANES_CONF_D)
        # Resolve the geometry THE BOX will actually run, from the same env-file
        # chain jasper-fanin loads — not from this command's defaults. Reading
        # our own flags would approve a box whose next daemon start uses
        # different numbers, which is precisely the class the Ring-A
        # `resolve_effective_fanin_ring_slots` precedent exists to prevent. The
        # CLI flags remain available as EXPLICIT overrides for an operator
        # modelling a box other than this one.
        eff_buffer, eff_period, provenance = rl.effective_lane_geometry()
        if args.input_buffer_frames is not None:
            eff_buffer = args.input_buffer_frames
            provenance += " buffer=cli-override"
        if args.period_frames is not None:
            eff_period = args.period_frames
            provenance += " period=cli-override"
        print(f"geometry {eff_buffer}/{eff_period} ({provenance})")
        for label in newly_armed:
            lane = rl.lane_by_label(label)
            # A unitless lane (correction — ephemeral aplay writers) has no
            # unit file to parse; the row itself names the one NON-root
            # writer identity to preflight (its root identities pass the
            # group predicate trivially).
            if lane is None:
                user = None
            elif lane.unit is None:
                user = lane.arm_preflight_user
            else:
                user = _renderer_unit_user(lane.unit)
            refusal = rl.arm_refusal_reason(
                label,
                assets_present=presence.all_present,
                missing_assets=presence.missing(),
                lane_conf_present=lane_conf,
                # UNCONDITIONAL on purpose. The old `if user else None`
                # short-circuit meant a renderer with no `User=` (root) never
                # reached the predicate at all, so the predicate's own
                # root-is-capable branch was dead from production and the arm
                # saw an indistinguishable "unknown". Same arm outcome either
                # way — `None` never refused and `True` never refuses — but the
                # value is now honest about WHY it does not refuse.
                user_in_ring_group=rl.renderer_user_in_ring_group(user),
                input_buffer_frames=eff_buffer,
                period_frames=eff_period,
            )
            if refusal is not None:
                print(f"refused {label}: {refusal}", file=sys.stderr)
                print("result refused")
                print(f"reason {refusal}")
                return 1

    if desired == tuple(current) and args.set is None and not args.arm and not args.disarm:
        # Pure report: never write on a read.
        print("result reported")
        print(f"armed {','.join(current)}")
        print(f"path {path}")
        for lane in rl.RENDERER_LANES:
            armed = lane.label in current
            print(f"lane {lane.label} {'ring' if armed else 'aloop'} "
                  f"{rl.device_for(lane, armed)} {rl.renderer_ring_path(lane.label)}")
        return 0

    try:
        outcome = rl.render_renderer_lanes_env(desired, path=path)
    except (OSError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    # A ring file left over from a PRIOR geometry is a create-or-ATTACH error,
    # not something either end recovers from: the renderer would fail its open
    # and the lane would be silent until someone ran `rm` by hand. Clear it on
    # EVERY transition — arm and disarm alike — so the lever can be pulled and
    # released, which is exactly the trap the format axis sprang on Ring A's
    # rollback. tmpfs transport state, recreated by whichever end opens next.
    for label in set(desired) ^ set(current):
        reason = rl.delete_stale_ring(label)
        if reason is not None:
            print(f"cleared_stale_ring {label} {reason}")

    print(f"result {'rendered' if outcome.changed else 'unchanged'}")
    print(f"armed {','.join(outcome.armed)}")
    print(f"previous {','.join(current)}")
    print(f"path {outcome.path}")
    # Unitless lanes (correction) contribute no unit here — their writers
    # are ephemeral spawns that read the map fresh per spawn, so the flip
    # needs only jasper-fanin's restart on that side.
    restart = " ".join(
        sorted({
            lane.unit
            for lane in rl.RENDERER_LANES
            if lane.label in set(desired) ^ set(current) and lane.unit is not None
        })
    )
    print(f"restart_required jasper-fanin.service {restart}".rstrip())
    # Arm-time advisories: registry data a NEWLY armed lane wants in front of
    # the operator (P6d: AirPlay's sync constants were derived on the aloop
    # transport). Informational only — refusals happened above; an advisory
    # never gates. Printed last so it is the freshest thing on the terminal.
    for label in newly_armed:
        lane = rl.lane_by_label(label)
        if lane is not None and lane.arm_advisory:
            print(f"advisory {label}: {lane.arm_advisory}")
    return 0


def _renderer_unit_user(unit: str) -> str | None:
    """The `User=` a renderer unit runs as, read from the installed unit file.

    Deliberately parses the unit rather than asking systemd: the arm can be run
    on a box whose renderer is stopped, and `systemctl show -p User` on an
    inactive unit is less reliable than the file that defines it.

    **`None` collapses three genuinely different cases**, and the caller must
    treat it as "assume root" rather than "no user":

    1. the main unit exists and sets no ``User=`` — systemd's default IS root,
       so `None` is the right answer and root is what runs;
    2. no unit file was found at either search path — the packaged unit may live
       somewhere this parse does not look;
    3. the ``User=`` is set somewhere a FILE parse cannot see — a drop-in under
       ``<unit>.d/``, or ``DynamicUser=``. This is not hypothetical here: JTS
       configures ``bluealsa-aplay.service`` through exactly such a drop-in, so
       a future ``User=`` added there would be invisible to this function.

    Only case 1 is a real answer; 2 and 3 are ignorance wearing the same value.
    The drop-in-aware net is the doctor's runtime `systemctl show -p User`
    (``jasper.cli.doctor.renderers._systemd_unit_user``), which resolves the full
    unit + drop-in merge and is what the PR #214 probe actually runs as. If this
    function's answer ever has to be trusted rather than merely advisory, use
    that instead.
    """
    for base in ("/etc/systemd/system", "/usr/lib/systemd/system"):
        try:
            text = Path(base, unit).read_text()
        except OSError:
            continue
        for line in text.splitlines():
            if line.strip().startswith("User="):
                return line.split("=", 1)[1].strip() or None
    return None


def _cmd_validate_outputd_env(args: argparse.Namespace) -> int:
    base = read_env_file_state(args.base_env)
    outputd = read_env_file_state(args.outputd_env)
    # Labels and the override store, not just values: this refusal is what the
    # audio-hardware reconciler logs as `outputd_env_invalid detail=...`, and
    # the operator reading it has to know WHICH layer holds the losing line.
    # The store matters because the latency-floor pass COPIES its values into
    # outputd.env, so a line that looks operator-written may be one that comes
    # straight back after every reconcile.
    #
    # READ PATH AND REPORTED LABEL ARE SEPARATE. The reconciler validates a
    # STAGED CANDIDATE (`mktemp` under /var/lib/jasper, removed on EXIT), so the
    # path this command reads is a temp file that no longer exists by the time an
    # operator reads the journal. Reporting it named a deleted file as the origin
    # — the wrong-attribution failure this provenance exists to prevent — so the
    # reconciler passes the REAL destination as --outputd-label and that is what
    # the message names.
    overrides = load_runtime_overrides(
        args.overrides,
        allowed_keys=AUDIO_RUNTIME_OVERRIDE_KEYS,
    )
    # A malformed or unreadable store degrades attribution — the refusal would
    # silently stop naming an origin it cannot parse. Say so rather than let the
    # message get quietly less useful.
    for warning in overrides.warnings:
        print(warning)
    detail = outputd_env_buffer_pair_error(
        base_env=base.values,
        outputd_env=outputd.values,
        base_label=base.path,
        outputd_label=args.outputd_label or outputd.path,
        override_entries={entry.key: entry for entry in overrides.entries},
        override_label=args.overrides,
    )
    if detail is not None:
        print(detail)
        return 1
    fanin = read_env_file_state(args.fanin_env)
    devices = output_endpoint_devices_from_statefiles(
        args.camilla_statefile,
        args.camilla2_statefile,
    )
    # A graph that targets the active lane but fails the hardware/topology
    # safety proof is intentionally demoted to the passive fail-closed route by
    # the output-hardware reconciler. Only enforce the active pairing when the
    # same canonical active-lane decision says that graph is legal for this DAC.
    #
    # MEMBERSHIP over every legal active endpoint, and the reason is that this
    # guard FAILS OPEN on a device it does not recognize: an unlisted endpoint
    # skips the decision entirely and is never demoted, so a graph targeting it
    # would sail through on a DAC that fails the hardware proof. Adding the
    # active ring is what keeps the demotion covering both transports of the one
    # active lane rather than only the snd-aloop one.
    if devices and devices.get("playback_device") in _ACTIVE_ENDPOINT_DEVICES:
        active_cap = active_outputd_lane_channels_for(
            str(base.values.get("JASPER_AUDIO_DAC_ID") or "")
        )
        from jasper.active_speaker.runtime_contract import outputd_active_lane_decision

        decision = (
            outputd_active_lane_decision(
                active_cap,
                statefile_path=args.camilla_statefile,
                crossover_statefile_path=args.camilla2_statefile,
                topology_path=args.output_topology,
            )
            if active_cap is not None
            else None
        )
        if decision is None or not decision.ok:
            devices = None
    merged_outputd = {**base.values, **outputd.values}
    report = transport_coherence_report(
        coupling=fanin.values.get(COUPLING_ENV_VAR),
        outputd_env=merged_outputd,
        camilla_devices=devices,
    )
    if report.errors:
        print("; ".join(report.errors))
        return 1
    # A note is a coherent-but-transient state (today: the ACTIVE-ring arm
    # waypoint), so this EXITS 0 — the reconciler must be allowed to derive the
    # marker that is the ladder's own next step. Printed on the ok path so the
    # caller's captured stdout carries it: `jasper-audio-hardware-reconcile`
    # logs it as event=audio_hardware_reconcile.outputd_env_note, which is the
    # journal line an operator standing mid-ladder actually reads.
    if report.notes:
        print("ok note=" + "; ".join(report.notes))
        return 0
    print("ok")
    return 0


def _cmd_outputd_capture_device(args: argparse.Namespace) -> int:
    capture_device = outputd_capture_device_for_playback(args.playback_device)
    if capture_device is None:
        print(
            f"no outputd capture endpoint is registered for "
            f"CamillaDSP playback={args.playback_device!r}"
        )
        return 1
    print(capture_device)
    return 0


def _cmd_route_actions(args: argparse.Namespace) -> int:
    base = read_env_file_state(args.base_env)
    route = resolve_audio_route_profile(base.values)
    print(f"summary route {route.route_id}")
    for action in route_owned_env_actions(route):
        if action.action == "set":
            print(f"fanin set {action.key} {action.value}")
        else:
            print(f"fanin unset {action.key}")
    return 0


def _cmd_overrides_list(args: argparse.Namespace) -> int:
    overrides = load_runtime_overrides(
        args.overrides,
        allowed_keys=AUDIO_RUNTIME_OVERRIDE_KEYS,
    )
    print(json.dumps(overrides.to_dict(), indent=2, sort_keys=True))
    return 0


def _cmd_overrides_set(args: argparse.Namespace) -> int:
    updated = set_runtime_override(
        key=args.key,
        value=args.value,
        reason=args.reason,
        path=args.overrides,
        ttl_seconds=args.ttl_seconds,
        expires_at=args.expires_at or "",
        allowed_keys=AUDIO_RUNTIME_OVERRIDE_KEYS,
    )
    print(json.dumps(updated.to_dict(), indent=2, sort_keys=True))
    return 0


def _cmd_overrides_clear(args: argparse.Namespace) -> int:
    updated = clear_runtime_override(
        args.key,
        path=args.overrides,
        allowed_keys=AUDIO_RUNTIME_OVERRIDE_KEYS,
    )
    print(json.dumps(updated.to_dict(), indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-audio-config",
        description="Explain resolved Jasper audio runtime settings",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    explain = sub.add_parser(
        "explain",
        help="show the planned audio knobs, provenance, and drift warnings",
    )
    explain.add_argument("--json", action="store_true")
    explain.add_argument("--base-env", default=DEFAULT_BASE_ENV_PATH)
    explain.add_argument("--outputd-env", default=DEFAULT_OUTPUTD_ENV_PATH)
    explain.add_argument("--fanin-env", default=DEFAULT_FANIN_ENV_PATH)
    explain.add_argument("--grouping-env", default=DEFAULT_GROUPING_ENV_PATH)
    explain.add_argument("--overrides", default=runtime_overrides_path())
    explain.add_argument("--output-hardware-state", default=None)
    explain.set_defaults(func=_cmd_explain)

    outputd_floor = sub.add_parser(
        "outputd-floor-actions",
        help=(
            "emit shell-readable outputd.env set/unset actions for the active "
            "DAC latency floor"
        ),
    )
    outputd_floor.add_argument("--profile-id", default="")
    outputd_floor.add_argument("--base-env", default=DEFAULT_BASE_ENV_PATH)
    outputd_floor.add_argument("--outputd-env", default=DEFAULT_OUTPUTD_ENV_PATH)
    outputd_floor.add_argument(
        "--overrides",
        default=runtime_overrides_path(),
    )
    outputd_floor.set_defaults(func=_cmd_outputd_floor_actions)

    render_ring_conf = sub.add_parser(
        "render-ring-conf-wire",
        help=(
            "render the shm-ring conf.d wire (slot period, format, per-ring "
            "channels) from the DAC profile's declared latency floor and the "
            "saved output topology (no declared floor leaves it untouched)"
        ),
    )
    render_ring_conf.add_argument("--profile-id", default="")
    render_ring_conf.add_argument(
        "--conf-d",
        default="",
        help="override the ring conf.d path (default: the ring_assets SSOT)",
    )
    render_ring_conf.add_argument(
        "--output-topology",
        default=DEFAULT_OUTPUT_TOPOLOGY_PATH,
        help="saved output topology the Ring B channel count is resolved from",
    )
    render_ring_conf.set_defaults(func=_cmd_render_ring_conf_wire)

    renderer_lanes = sub.add_parser(
        "renderer-lanes",
        help=(
            "report or change which renderer lanes ingress over an SHM ring "
            "(U3/P6); writes both ends of the flip into one file"
        ),
    )
    renderer_lanes.add_argument(
        "--arm",
        action="append",
        default=[],
        metavar="LABEL",
        help="arm this fan-in lane label for ring ingress (repeatable)",
    )
    renderer_lanes.add_argument(
        "--disarm",
        action="append",
        default=[],
        metavar="LABEL",
        help="return this lane to its snd-aloop substream (repeatable)",
    )
    renderer_lanes.add_argument(
        "--set",
        default=None,
        metavar="LABELS",
        help="replace the armed set outright (comma-separated; empty disarms all)",
    )
    renderer_lanes.add_argument(
        "--path",
        default="",
        help="override the lane-map env path (default: the renderer_lanes SSOT)",
    )
    # The arm preflights the DERIVED ring geometry so an inexpressible one is
    # refused where the operator is watching, rather than only as a fan-in park
    # at its next start. Defaults are the shipped fan-in geometry.
    # EXPLICIT overrides only. Unset means "resolve what this box will run" via
    # the fan-in env chain; passing one models a different box on purpose.
    renderer_lanes.add_argument("--input-buffer-frames", type=int, default=None)
    renderer_lanes.add_argument("--period-frames", type=int, default=None)
    renderer_lanes.set_defaults(func=_cmd_renderer_lanes)

    validate_outputd = sub.add_parser(
        "validate-outputd-env",
        help="validate reconciler-owned outputd.env before installing it",
    )
    validate_outputd.add_argument("--base-env", default=DEFAULT_BASE_ENV_PATH)
    validate_outputd.add_argument("--outputd-env", default=DEFAULT_OUTPUTD_ENV_PATH)
    validate_outputd.add_argument("--fanin-env", default=DEFAULT_FANIN_ENV_PATH)
    # The path to NAME in refusals when it differs from the path to READ. The
    # reconciler validates a staged candidate under a temp name that is deleted
    # on exit; unset means the two are the same file.
    validate_outputd.add_argument("--outputd-label", default="")
    # Same default as outputd-floor-actions: the store is read whether or not a
    # caller names it, because the floor pass writes store values into
    # outputd.env and this refusal has to be able to say so.
    validate_outputd.add_argument("--overrides", default=runtime_overrides_path())
    validate_outputd.add_argument(
        "--camilla-statefile", default=DEFAULT_CAMILLA_STATEFILE_PATH
    )
    validate_outputd.add_argument(
        "--camilla2-statefile", default=DEFAULT_CAMILLA2_STATEFILE_PATH
    )
    validate_outputd.add_argument(
        "--output-topology", default=DEFAULT_OUTPUT_TOPOLOGY_PATH
    )
    validate_outputd.set_defaults(func=_cmd_validate_outputd_env)

    capture_device = sub.add_parser(
        "outputd-capture-device",
        help="resolve outputd's paired capture PCM for a CamillaDSP playback PCM",
    )
    capture_device.add_argument("--playback-device", required=True)
    capture_device.set_defaults(func=_cmd_outputd_capture_device)

    route_actions = sub.add_parser(
        "route-actions",
        help="emit shell-readable fanin env actions for the audio route",
    )
    route_actions.add_argument("--base-env", default=DEFAULT_BASE_ENV_PATH)
    route_actions.set_defaults(func=_cmd_route_actions)

    overrides_list = sub.add_parser(
        "overrides-list",
        help="list active audio runtime lab overrides",
    )
    overrides_list.add_argument(
        "--overrides",
        default=runtime_overrides_path(),
    )
    overrides_list.set_defaults(func=_cmd_overrides_list)

    overrides_set = sub.add_parser(
        "overrides-set",
        help="set one temporary audio runtime lab override",
    )
    overrides_set.add_argument("key", choices=sorted(AUDIO_RUNTIME_OVERRIDE_KEYS))
    overrides_set.add_argument("value")
    overrides_set.add_argument("--reason", required=True)
    overrides_set.add_argument("--ttl-seconds", type=int, default=None)
    overrides_set.add_argument("--expires-at", default="")
    overrides_set.add_argument(
        "--overrides",
        default=runtime_overrides_path(),
    )
    overrides_set.set_defaults(func=_cmd_overrides_set)

    overrides_clear = sub.add_parser(
        "overrides-clear",
        help="clear one audio runtime lab override",
    )
    overrides_clear.add_argument("key", choices=sorted(AUDIO_RUNTIME_OVERRIDE_KEYS))
    overrides_clear.add_argument(
        "--overrides",
        default=runtime_overrides_path(),
    )
    overrides_clear.set_defaults(func=_cmd_overrides_clear)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
