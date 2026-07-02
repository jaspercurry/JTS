# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-audio-config`` operational diagnostics."""

from __future__ import annotations

import argparse
import json

from jasper.audio_runtime_plan import (
    AUDIO_RUNTIME_OVERRIDE_KEYS,
    DEFAULT_BASE_ENV_PATH,
    DEFAULT_FANIN_ENV_PATH,
    DEFAULT_GROUPING_ENV_PATH,
    DEFAULT_OUTPUTD_ENV_PATH,
    OUTPUTD_LATENCY_KEYS,
    build_audio_runtime_plan,
    build_audio_runtime_plan_from_system,
    outputd_latency_floor_actions,
    route_owned_env_actions,
    resolve_audio_route_profile,
)
from jasper.audio_runtime_overrides import (
    clear_runtime_override,
    load_runtime_overrides,
    runtime_overrides_path,
    set_runtime_override,
)
from jasper.env_load import read_env_file_state


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


def _route_action_target(key: str) -> str:
    if key.startswith("JASPER_FANIN_"):
        return "fanin"
    if key.startswith("JASPER_USBSINK_"):
        return "usbsink"
    return "base"


def _cmd_route_actions(args: argparse.Namespace) -> int:
    base = read_env_file_state(args.base_env)
    route = resolve_audio_route_profile(base.values)
    print(f"summary route {route.route_id}")
    for action in route_owned_env_actions(route):
        target = _route_action_target(action.key)
        if action.action == "set":
            print(f"{target} set {action.key} {action.value}")
        else:
            print(f"{target} unset {action.key}")
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

    route_actions = sub.add_parser(
        "route-actions",
        help="emit shell-readable fanin/usbsink env actions for the audio route",
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
