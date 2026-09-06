# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""``jasper-sound`` operational commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

from jasper.log_event import log_event
from jasper.sound.profile import PROFILE_PATH
from jasper.sound.runtime import DEFAULT_CONFIG_DIR, reconcile_current_dsp
from jasper.logging_setup import configure_logging

logger = logging.getLogger(__name__)


def _print_reconcile(payload: dict[str, Any]) -> None:
    status = payload.get("status")
    print(f"sound DSP reconcile: {status}")
    if payload.get("reason"):
        print(f"  reason: {payload['reason']}")
    if payload.get("carrier_kind"):
        print(f"  carrier: {payload['carrier_kind']}")
    if payload.get("current_config_path"):
        print(f"  current: {payload['current_config_path']}")
    if payload.get("candidate_config_path"):
        print(f"  candidate: {payload['candidate_config_path']}")
    if payload.get("active_config_path"):
        print(f"  active: {payload['active_config_path']}")
    if payload.get("apply"):
        print(f"  op_id: {payload['apply'].get('op_id')}")


def _cmd_reconcile_current_dsp(args: argparse.Namespace) -> int:
    try:
        payload = asyncio.run(
            reconcile_current_dsp(
                profile_path=args.profile,
                config_dir=args.config_dir,
                force=args.force,
            )
        )
    except Exception as exc:  # noqa: BLE001
        if not args.fail_open:
            raise
        payload = {
            "status": "failed",
            "reason": type(exc).__name__,
            "message": str(exc),
        }
        log_event(
            logger,
            "sound.reconcile_current_dsp",
            result="failed",
            reason=type(exc).__name__,
            message=str(exc),
            level=logging.WARNING,
        )

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        _print_reconcile(payload)
        if payload.get("status") == "failed":
            print(
                f"  warning: {payload.get('message') or payload.get('reason')}",
                file=sys.stderr,
            )
    if payload.get("status") == "failed" and not args.fail_open:
        return 1
    return 0


def _cmd_render_flat_cutover(args: argparse.Namespace) -> int:
    """Render both flat startup configs through the product emitter.

    The ONE entry every writer uses — deploy/install.sh, the root
    jasper-audio-hardware-reconcile, and jasper-output-topology-reset — so the
    file a box boots cannot depend on which of them ran last. Write-on-change,
    and the graph is width-matched to the saved output topology (a mono layout
    gets its unclaimed channel hard muted and both program channels folded onto
    the one it claims; see jasper.sound.camilla_yaml.flat_graph_channel_plan).
    """

    from jasper.camilla_config_contract import parse_camilla_devices_config
    from jasper.output_topology import OutputTopologyError
    from jasper.sound.camilla_yaml import render_flat_cutover_configs

    try:
        result = render_flat_cutover_configs(config_dir=args.config_dir)
    except OutputTopologyError as exc:
        # Fail LOUD and write nothing. The reconciler has no guard behind this
        # render, so silently emitting the unmuted golden graph for an
        # unparseable topology would overwrite a healthy width-matched one.
        # Keeping the previous file on disk is the fail-closed answer; the
        # operator sees the reason in the journal / install transcript.
        print(
            f"refusing to render flat cutover configs: {exc}",
            file=sys.stderr,
        )
        return 1
    if args.json:
        print(json.dumps(
            {
                "config_dir": str(result.config_dir),
                "changed": result.changed,
                "rendered": [
                    {"path": str(item.path), "changed": item.changed}
                    for item in result.rendered
                ],
            },
            indent=2,
            sort_keys=True,
        ))
        return 0
    for item in result.rendered:
        devices = parse_camilla_devices_config(item.text)
        print(
            f"rendered {item.path.name} "
            f"path={item.path} "
            f"chunksize={devices.get('chunksize')} "
            f"target_level={devices.get('target_level')} "
            f"changed={'yes' if item.changed else 'no'}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jasper-sound",
        description="Inspect and reconcile Jasper sound-profile DSP state",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    reconcile = sub.add_parser(
        "reconcile-current-dsp",
        help="re-render the current JTS-owned DSP graph from saved sound intent",
    )
    reconcile.add_argument(
        "--profile",
        default=PROFILE_PATH,
        help="saved sound profile JSON path (default: /var/lib/jasper/sound_profile.json)",
    )
    reconcile.add_argument(
        "--config-dir",
        type=Path,
        default=DEFAULT_CONFIG_DIR,
        help="CamillaDSP generated config directory",
    )
    reconcile.add_argument(
        "--force",
        action="store_true",
        help="re-render even when the reconciler would otherwise skip a no-op",
    )
    reconcile.add_argument(
        "--fail-open",
        action="store_true",
        help="log failures but exit 0 so deploy/startup can continue",
    )
    reconcile.add_argument("--json", action="store_true")
    reconcile.set_defaults(func=_cmd_reconcile_current_dsp)

    render = sub.add_parser(
        "render-flat-cutover",
        help=(
            "render outputd-cutover.yml + its ring sibling, width-matched to "
            "the saved output topology (write-on-change)"
        ),
    )
    render.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="CamillaDSP config directory (default: /etc/camilladsp)",
    )
    render.add_argument("--json", action="store_true")
    render.set_defaults(func=_cmd_render_flat_cutover)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging(fmt="%(levelname)s %(message)s")
    # Hydrate os.environ from the wizard-owned env files (same set the daemons
    # load, fanin.env wins last) so a reconcile run from the CLI / install.sh —
    # neither of which pre-sources those files — sees the persisted
    # chunksize / target-level keys the emit consults from the live env. The
    # coupling TOKEN itself does not depend on this hydration:
    # coupling_capture_kwargs_from_env() resolves the ring devices
    # unconditionally (ONE transport, ADR-0100) and reads its wire format
    # FILE-FRESH (read_declared_ring_wire_format, not os.environ), so even
    # an un-hydrated CLI run resolves the same ring kwargs. setdefault
    # semantics keep an explicit shell override winning.
    from jasper.env_load import load_env_files

    load_env_files()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
