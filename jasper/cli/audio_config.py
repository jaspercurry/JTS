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

from jasper.audio_runtime_plan import (
    AUDIO_RUNTIME_OVERRIDE_KEYS,
    build_audio_runtime_plan_from_system,
)
from jasper.camilla_config_contract import (
    outputd_capture_device_for_playback,
)
from jasper.audio_runtime_overrides import (
    clear_runtime_override,
    load_runtime_overrides,
    runtime_overrides_path,
    set_runtime_override,
)
from jasper.env_load import (
    BASE_ENV_PATH,
    FANIN_ENV_PATH,
    GROUPING_ENV_FILE,
    OUTPUTD_ENV_PATH,
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
    explain.add_argument("--base-env", default=BASE_ENV_PATH)
    explain.add_argument("--outputd-env", default=OUTPUTD_ENV_PATH)
    explain.add_argument("--fanin-env", default=FANIN_ENV_PATH)
    explain.add_argument("--grouping-env", default=GROUPING_ENV_FILE)
    explain.add_argument("--overrides", default=runtime_overrides_path())
    explain.add_argument("--output-hardware-state", default=None)
    explain.set_defaults(func=_cmd_explain)

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

    capture_device = sub.add_parser(
        "outputd-capture-device",
        help="resolve outputd's paired capture PCM for a CamillaDSP playback PCM",
    )
    capture_device.add_argument("--playback-device", required=True)
    capture_device.set_defaults(func=_cmd_outputd_capture_device)

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
