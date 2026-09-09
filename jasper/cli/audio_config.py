# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-audio-config`` operational diagnostics."""

from __future__ import annotations

import argparse
import json

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
