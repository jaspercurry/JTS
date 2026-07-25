# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator CLI for the Bass Extension limiter-evidence bench runner.

Bench-only, operator-supervised. Given an operator-authored manifest of stimulus
requests (a JSON file — the operator's authorized inputs, never defaulted), it
authors the campaign manifest, prints the preflight plan, and — outside
``--dry-run`` — runs the frozen campaign and writes the replayable bundle. It
plays real audio at stress levels and temporarily mutates the live CamillaDSP
graph, so it is run by hand at the bench with the Stop control (Ctrl-C) ready.

``--dry-run`` authors + validates the manifest and prints the plan without
opening any device, socket, or CamillaDSP connection — the safe preflight the
operator runs before the supervised session, and the DEFAULT posture: live
execution additionally requires the explicit ``--live`` flag.

This CLI never wires the pure evidence producer into a runtime path, never
persists a profile, and calls no ``apply_bass_extension`` writer.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from typing import Sequence

from jasper.bass_extension.bench.context import (
    BUNDLE_KIND,
    LIMITER_DOMAIN_MAX_DBFS,
    LIMITER_DOMAIN_MIN_DBFS,
)
from jasper.bass_extension.bench.manifest import (
    STIMULUS_ROLES,
    CampaignManifest,
    ManifestRefusal,
    author_campaign_manifest,
)
from jasper.bass_extension.bench.render import RenderError, resolve_render_binary
from jasper.bass_extension.targets import MARGINS


def _load_inputs(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("manifest inputs file must be a JSON object")
    return raw


def _target_ids(inputs: dict[str, object]) -> tuple[str, ...]:
    requests = inputs.get("requests")
    if not isinstance(requests, dict) or not requests:
        raise SystemExit(
            "manifest inputs must include a non-empty 'requests' object keyed by "
            "target id (deepest target through natural)"
        )
    return tuple(str(target_id) for target_id in requests)


def _print_plan(inputs: dict[str, object], manifest, target_ids: Sequence[str]) -> None:
    print(f"campaign manifest: margin={manifest.margin_policy_name}")
    print(f"  targets ({len(target_ids)}): {', '.join(target_ids)}")
    print(f"  stimulus roles: {', '.join(STIMULUS_ROLES)}")
    print(
        f"  trusted limiter domain: [{LIMITER_DOMAIN_MIN_DBFS}, "
        f"{LIMITER_DOMAIN_MAX_DBFS}] dBFS"
    )
    print(f"  bundle kind: {BUNDLE_KIND}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        type=Path,
        help="path to the operator-authored manifest-inputs JSON file",
    )
    parser.add_argument(
        "--bundle-dir",
        type=Path,
        default=Path("bass-extension-bench-bundle"),
        help="directory to write the replayable evidence bundle into",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="author + validate the manifest and print the plan; open no device",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "run the actual on-device campaign — plays real audio at stress "
            "levels and temporarily mutates the live CamillaDSP graph. Requires "
            "every on-device collaborator; --dry-run (or omitting --live) is the "
            "safe preflight and remains the default posture."
        ),
    )
    args = parser.parse_args(argv)

    inputs = _load_inputs(args.manifest)
    if inputs.get("margin_policy_name") not in MARGINS:
        raise SystemExit(
            "margin_policy_name must be one of: " + ", ".join(sorted(MARGINS))
        )
    target_ids = _target_ids(inputs)

    try:
        manifest = author_campaign_manifest(inputs, target_ids=target_ids)
    except ManifestRefusal as refusal:
        print("REFUSED — the manifest is missing operator-authorized inputs:", file=sys.stderr)
        for path in refusal.missing_paths:
            print(f"  - {path}", file=sys.stderr)
        return 2

    _print_plan(inputs, manifest, target_ids)

    if args.dry_run or not args.live:
        print("dry run: no device opened, no graph mutated, no bundle written")
        return 0

    return _run_live(args, manifest, target_ids)


class _CamillaFloor:
    """The ``FloorControl`` the activation seam fades to before every mutation.

    ``SAFE_FLOOR_DB`` reuses ``jasper.volume_curve.DEFAULT_VOLUME_FLOOR_DB`` —
    the existing main-volume curve's own floor (also
    ``jasper.audio_measurement.ramp.MeasurementRamp.start_db``'s coarse-ramp
    starting point) — rather than inventing a new "safe" constant.
    """

    def __init__(self, controller: object) -> None:
        self._controller = controller

    async def to_floor(self) -> None:
        from jasper.volume_curve import DEFAULT_VOLUME_FLOOR_DB

        await self._controller.set_volume_db(DEFAULT_VOLUME_FLOOR_DB)  # type: ignore[attr-defined]

    async def assert_at_floor(self) -> None:
        from jasper.volume_curve import DEFAULT_VOLUME_FLOOR_DB

        reading = await self._controller.get_volume_and_mute()  # type: ignore[attr-defined]
        if reading is None:
            raise RuntimeError("could not confirm the safe floor: camilla unreachable")
        volume_db, _muted = reading
        if volume_db > DEFAULT_VOLUME_FLOOR_DB:
            raise RuntimeError(
                f"main volume {volume_db:.1f} dB is above the safe floor "
                f"{DEFAULT_VOLUME_FLOOR_DB:.1f} dB — refusing to mutate"
            )


def _run_live(args: argparse.Namespace, manifest: CampaignManifest, target_ids: Sequence[str]) -> int:
    """Run the on-device campaign under operator supervision, Stop-able by Ctrl-C.

    Real: binary resolution (R5), the CamillaDSP controller, the safe-floor
    adapter, and the Ctrl-C Stop control. The campaign orchestration
    (:func:`~jasper.bass_extension.bench.runner.run_campaign`), the
    activation seam, and the full derivation/render/cross-check pipeline
    (:mod:`~jasper.bass_extension.bench.executor`) are complete and
    hardware-free tested.

    What remains NOT bound here: constructing each target's
    :class:`~jasper.bass_extension.bench.runner.TargetPlan` (natural graph
    text, LT/subsonic params, owner channels) from the household's confirmed
    bass-extension profile, and the on-device
    :class:`~jasper.bass_extension.bench.executor.PlayAndCapture` collaborator
    (the phone capture-relay session composition) — assembling both
    correctly is an on-device integration exercise this hardware-free
    implementation session could not responsibly author untested. Both are
    named, narrow next steps, not open questions — tracked as
    https://github.com/jaspercurry/JTS/issues/1738 ("Bass extension: final
    bench-campaign binding"), N-7: the durable, in-repo reference for this
    unbound scope, alongside TargetPlan construction, the live
    get_playback_peak_all/get_clipped_samples/get_volume_and_mute polling
    wiring, on-device verification of resolve_render_binary's systemd
    parse, and the operator runbook deliverable.
    """

    try:
        binary = resolve_render_binary()
    except RenderError as exc:
        print(f"REFUSED — render binary could not be resolved: {exc}", file=sys.stderr)
        return 2
    print(
        f"resolved render binary: {binary.path} ({binary.version_output}, "
        f"sha256={binary.sha256[:12]}…)"
    )

    from jasper.bass_extension.bench.runner import Stop

    stop = Stop()

    def _handle_sigint(signum: int, frame: object) -> None:
        del signum, frame
        print(
            "\nStop requested — finishing the in-flight step, then restoring "
            "the predecessor graph and exiting…",
            file=sys.stderr,
        )
        stop.stop()

    signal.signal(signal.SIGINT, _handle_sigint)

    raise SystemExit(
        "live bench execution requires binding each target's TargetPlan from "
        "the household's confirmed bass-extension profile and the on-device "
        "PlayAndCapture collaborator (jasper.bass_extension.bench.executor) — "
        "neither is wired here yet. The manifest validated, the render binary "
        "resolved, and the plan is shown above; re-run with --dry-run for the "
        "full preflight. The campaign orchestration, activation seam, "
        "derivation/render/cross-check pipeline, and Ctrl-C Stop control are "
        "built and hardware-free tested."
    )


if __name__ == "__main__":
    raise SystemExit(main())
