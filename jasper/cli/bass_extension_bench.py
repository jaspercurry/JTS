# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Operator CLI for the Bass Extension limiter-evidence bench runner.

Bench-only, operator-supervised. Given an operator-authored manifest of stimulus
requests (a JSON file — the operator's authorized inputs, never defaulted), it
authors the campaign manifest, composes one
:class:`~jasper.bass_extension.bench.runner.TargetPlan` per named rung of the
APPLIED bass family, prints the preflight plan, and — outside ``--dry-run`` —
runs the frozen campaign and writes the replayable bundle. It plays real audio
at stress levels and temporarily mutates the live CamillaDSP graph, so it is run
by hand at the bench with the Stop control (Ctrl-C) ready.

``--dry-run`` authors + validates the manifest and composes every rung's graph
without opening any device, socket, or CamillaDSP connection — the safe
preflight the operator runs before the supervised session, and the DEFAULT
posture: live execution additionally requires the explicit ``--live`` flag.

This CLI is parse → compose → run → report and nothing else: the plans come
from :mod:`~jasper.bass_extension.bench.plan`, the collaborators from
:mod:`~jasper.bass_extension.bench.compose`. It emits no graph (the one
composer does), derives no threshold, never wires the pure evidence producer
into a runtime path, never persists a profile, and calls no
``apply_bass_extension`` writer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jasper.bass_extension.bench.activation import ActivationError
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
from jasper.bass_extension.bench.plan import CampaignPlan, target_plans
from jasper.bass_extension.bench.render import RenderError, resolve_render_binary
from jasper.bass_extension.bench.runner import (
    BenchAborted,
    BenchRefused,
    Stop,
    TargetPlan,
)
from jasper.bass_extension.targets import MARGINS
from jasper.env_load import load_env_files
from jasper.volume_coordinator import install_env_canonical_target_provider

#: CamillaDSP's boot selector names no installed graph to compose a rung onto.
REFUSE_SELECTED_GRAPH = "bench_selected_graph_unavailable"
#: The box moved off the graph these rungs were composed onto.
REFUSE_SELECTED_GRAPH_MISMATCH = "bench_selected_graph_mismatch"


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


def _refused(refusal: BenchRefused) -> int:
    print(f"REFUSED — {refusal.reason}: {refusal.detail}", file=sys.stderr)
    return 2


def _box_state() -> tuple[Any, Mapping[str, Any], Path]:
    """The topology, the applied baseline snapshot, and the selected graph file.

    File reads only — the dry run composes the same plans the live run
    activates, so the preflight proves the composition rather than promising it.
    """

    # lazy: the applied-baseline reader pulls the whole active-speaker profile
    # stack, which a manifest-only refusal never needs.
    from jasper.active_speaker.baseline_profile import (
        load_applied_baseline_profile_state,
    )
    from jasper.active_speaker.environment import read_camilla_statefile_config_path
    from jasper.output_topology import load_output_topology

    selected = read_camilla_statefile_config_path()
    if not selected:
        raise BenchRefused(
            REFUSE_SELECTED_GRAPH,
            "CamillaDSP's boot selector names no config path, so there is no "
            "installed graph to compose a rung onto",
        )
    return (
        load_output_topology(),
        load_applied_baseline_profile_state() or {},
        Path(selected),
    )


def _compose(manifest: CampaignManifest, target_ids: Sequence[str]) -> CampaignPlan:
    topology, applied, selected = _box_state()
    return target_plans(
        topology,
        applied,
        target_ids=target_ids,
        current_config_path=selected,
        margin_policy_name=manifest.margin_policy_name,
    )


def _print_plan(manifest: CampaignManifest, target_ids: Sequence[str]) -> None:
    print(f"campaign manifest: margin={manifest.margin_policy_name}")
    print(f"  targets ({len(target_ids)}): {', '.join(target_ids)}")
    print(f"  stimulus roles: {', '.join(STIMULUS_ROLES)}")
    print(
        f"  trusted limiter domain: [{LIMITER_DOMAIN_MIN_DBFS}, "
        f"{LIMITER_DOMAIN_MAX_DBFS}] dBFS"
    )
    print(f"  bundle kind: {BUNDLE_KIND}")


def _print_target_plans(plans: Sequence[TargetPlan]) -> None:
    print(f"composed {len(plans)} rung graph(s) from the applied family:")
    for plan in plans:
        summary = plan.profile_summary
        emitted = summary.get("natural") if isinstance(summary, Mapping) else None
        emitted = emitted if isinstance(emitted, Mapping) else {}
        print(
            f"  {plan.target_id}: fp={emitted.get('fp_hz')} Hz "
            f"qp={emitted.get('qp')} boost={plan.boost_headroom_db:g} dB, "
            f"limiter {plan.limiter_name} at "
            f"{plan.baseline_clip_limit_dbfs:g} dBFS, "
            f"owner channels {list(plan.owner_channels)}, "
            f"target {plan.target_fingerprint[:12]}…"
        )


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
    # First, before anything reads a path or composes a graph: `.env` files
    # decide where the applied profile, the statefile and the sound overlays
    # live, so a composition made before them could be built on other files
    # than the live context reads. `install_env_canonical_target_provider`
    # registers this process's VolumeOwner and the canonical target the duck
    # release lands on; without it there is no SESSION_MEASUREMENT rank to
    # claim the fader through. Both are what measure.py and jasper-null do.
    load_env_files()
    install_env_canonical_target_provider()

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

    _print_plan(manifest, target_ids)

    try:
        campaign = _compose(manifest, target_ids)
    except BenchRefused as refusal:
        return _refused(refusal)
    _print_target_plans(campaign.plans)

    if args.dry_run or not args.live:
        print("dry run: no device opened, no graph mutated, no bundle written")
        return 0

    return _run_live(args, manifest, campaign)


def _run_live(
    args: argparse.Namespace,
    manifest: CampaignManifest,
    campaign: CampaignPlan,
) -> int:
    """Run the on-device campaign under operator supervision, Stop-able by Ctrl-C.

    Everything the campaign measures with is already composed (the plans above)
    or resolved here from its own owner: the render binary (R5), the conductor
    context, and the CamillaDSP controller. Every refusal — the bench's own
    (a live pass's failures included: the campaign's executor translates them
    into it), the box's, the door's, the mic's — leaves by the typed door with
    an exit code, never as a traceback.
    """

    # lazy: the box's, the door's and the mic's refusal vocabulary, all
    # on-device modules a preflight must not import.
    from jasper.active_speaker.crossover_v2.door import MeasurementDoorRefused
    from jasper.active_speaker.crossover_v2.refusal_copy import CrossoverV2Refused
    from jasper.audio_measurement.wired_capture import WiredCaptureError

    try:
        binary = resolve_render_binary()
    except RenderError as exc:
        print(f"REFUSED — render binary could not be resolved: {exc}", file=sys.stderr)
        return 2
    print(
        f"resolved render binary: {binary.path} ({binary.version_output}, "
        f"sha256={binary.sha256[:12]}…)"
    )

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

    try:
        return asyncio.run(_campaign(args, manifest, campaign, binary, stop))
    except BenchRefused as refusal:
        return _refused(refusal)
    except MeasurementDoorRefused as refusal:
        print(f"REFUSED — {refusal.reason}: {refusal.detail}", file=sys.stderr)
        return 2
    except BenchAborted as exc:
        print(f"REFUSED — bench_stopped: {exc}", file=sys.stderr)
        return 2
    except ActivationError as exc:
        # The read-back proof and the restore proof are the campaign's, not one
        # target's: a graph that cannot be proved or put back ends the run.
        print(f"REFUSED — bench_activation_failed: {exc}", file=sys.stderr)
        return 2
    except CrossoverV2Refused as exc:
        print(f"REFUSED — bench_box_not_ready: {exc}", file=sys.stderr)
        return 2
    except WiredCaptureError as exc:
        print(f"REFUSED — bench_no_wired_mic: {exc}", file=sys.stderr)
        return 2


async def _campaign(
    args: argparse.Namespace,
    manifest: CampaignManifest,
    campaign: CampaignPlan,
    binary: Any,
    stop: Stop,
) -> int:
    """Resolve the live box, compose onto it, run, report."""

    # lazy: the on-device collaborators — the controller, the conductor's live
    # status, and the composition root that wires the fader owner and the mic.
    from jasper.active_speaker.crossover_v2.conductor_context import (
        conductor_status,
        resolve_conductor_context,
    )
    from jasper.bass_extension.bench.activation import snapshot_predecessor
    from jasper.bass_extension.bench.compose import compose_campaign
    from jasper.bass_extension.bench.runner import run_campaign
    from jasper.camilla import primary_controller

    controller = primary_controller()
    # The restore anchor, read once: every rung's activation restores to THIS
    # graph, and the bundle's natural_graph_fingerprint names it. A box that
    # has moved off the file the rungs were composed onto would be measured
    # against overlays it no longer runs, so it is refused here rather than
    # discovered by the program-layer proof mid-campaign.
    predecessor = await snapshot_predecessor(controller)
    if predecessor.config_file_path != str(campaign.selected_config_path):
        raise BenchRefused(
            REFUSE_SELECTED_GRAPH_MISMATCH,
            f"the rungs were composed onto {campaign.selected_config_path}, but "
            f"CamillaDSP is running {predecessor.config_file_path}",
        )
    composed = compose_campaign(
        manifest=manifest,
        campaign=campaign,
        context=resolve_conductor_context(conductor_status()),
        controller=controller,
        binary=binary,
        natural_graph_fingerprint=predecessor.graph_fingerprint,
        bundle_dir=args.bundle_dir,
        stop=stop,
    )
    emitted = await run_campaign(
        composed.deps,
        manifest=manifest,
        measured_context=composed.measured_context,
        targets=campaign.plans,
        retained_facts=composed.retained_facts,
        sink=composed.sink,
    )
    return _report(emitted, args.bundle_dir)


def _report(emitted: Mapping[str, Any], bundle_dir: Path) -> int:
    """One line per target, and the exit code the operator acts on."""

    unevaluated: list[str] = []
    for target in emitted.get("targets", ()):
        result = target.get("result", {})
        disposition = str(result.get("disposition"))
        print(f"  {target.get('target_id')}: {disposition}")
        if disposition != "evaluated":
            unevaluated.append(str(target.get("target_id")))
    print(f"bundle written: {bundle_dir}")
    if unevaluated:
        print(
            "REFUSED — bench_targets_unevaluated: "
            + ", ".join(unevaluated)
            + " (their partial artifacts are in the bundle)",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
