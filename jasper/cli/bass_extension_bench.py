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

This CLI is the campaign's composition root and nothing else: it emits no graph
(the one composer does), derives no threshold, never wires the pure evidence
producer into a runtime path, never persists a profile, and calls no
``apply_bass_extension`` writer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.bass_extension.bench.activation import ActivationError
from jasper.bass_extension.bench.analysis import MeasurementPolicy
from jasper.bass_extension.bench.context import (
    BUNDLE_KIND,
    LIMITER_DOMAIN_MAX_DBFS,
    LIMITER_DOMAIN_MIN_DBFS,
)
from jasper.bass_extension.bench.executor import LIVE_PASS_FAILURES
from jasper.bass_extension.bench.manifest import (
    STIMULUS_ROLES,
    CampaignManifest,
    ManifestRefusal,
    author_campaign_manifest,
)
from jasper.bass_extension.bench.plan import target_plans
from jasper.bass_extension.bench.render import RenderError, resolve_render_binary
from jasper.bass_extension.bench.runner import (
    BenchAborted,
    BenchRefused,
    Stop,
    TargetPlan,
)
from jasper.bass_extension.targets import MARGINS

#: The operator authorized no measurement/transparency bounds to analyze with.
REFUSE_MEASUREMENT_POLICY = "bench_measurement_policy_missing"
#: This process registered no fader owner, so no level can be claimed.
REFUSE_NO_VOLUME_OWNER = "bench_no_volume_owner"
#: CamillaDSP's boot selector names no installed graph to compose a rung onto.
REFUSE_SELECTED_GRAPH = "bench_selected_graph_unavailable"
#: One target's live pass ended on a proof, derivation, render or cross-check.
REFUSE_LIVE_PASS = "bench_live_pass_failed"
#: The runner named a target this campaign composed no executor for.
REFUSE_TARGET_NOT_PLANNED = "bench_target_not_planned"


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


def _compose(
    manifest: CampaignManifest, target_ids: Sequence[str]
) -> tuple[Mapping[str, Any], tuple[TargetPlan, ...]]:
    topology, applied, selected = _box_state()
    return applied, target_plans(
        topology,
        applied,
        target_ids=target_ids,
        current_config_path=selected,
        margin_policy_name=manifest.margin_policy_name,
    )


def _measurement_policy(inputs: Mapping[str, object]) -> MeasurementPolicy:
    """The operator's measurement + transparency bounds, or a refusal.

    The two bounds ``MarginPolicy`` does not carry and
    :mod:`~jasper.bass_extension.bench.analysis` refuses to invent: the SNR
    floor a take must clear and the paired-transparency RMS bound. The frozen
    protocol binds them into the bundle by fingerprint
    (``transparency_policy_fingerprint``), so they are operator-authorized
    inputs like every other, never a default.
    """

    raw = inputs.get("measurement_policy")
    values: dict[str, float] = {}
    for name in ("min_snr_db", "max_tracking_rms_db"):
        value = raw.get(name) if isinstance(raw, Mapping) else None
        if type(value) is int:
            value = float(value)
        if type(value) is not float or not math.isfinite(value) or value <= 0.0:
            raise BenchRefused(
                REFUSE_MEASUREMENT_POLICY,
                f"measurement_policy.{name} must be a positive number the "
                "operator authorized",
            )
        values[name] = value
    return MeasurementPolicy(**values)


def _commanded_level_db(manifest: CampaignManifest) -> float:
    """The ONE fader level the whole campaign commands.

    Every request's ``requested_commanded_main_volume_db`` opens the same
    session volume, and the play seam refuses any request that disagrees with
    it — so a campaign naming two levels is refused here, before any device.
    """

    # lazy: wired_play is the hardware seam; only its refusal code is wanted.
    from jasper.active_speaker.volume_latch import fader_matches
    from jasper.bass_extension.bench.wired_play import REFUSE_COMMANDED_VOLUME

    levels = [
        float(request.requested_commanded_main_volume_db)
        for by_role in manifest.requests.values()
        for request in by_role.values()
    ]
    if not levels or not all(fader_matches(level, levels[0]) for level in levels):
        raise BenchRefused(
            REFUSE_COMMANDED_VOLUME,
            "the campaign manifest commands more than one main-volume level; "
            "one campaign opens one session volume",
        )
    return levels[0]


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
        applied, plans = _compose(manifest, target_ids)
    except BenchRefused as refusal:
        return _refused(refusal)
    _print_target_plans(plans)

    if args.dry_run or not args.live:
        print("dry run: no device opened, no graph mutated, no bundle written")
        return 0

    return _run_live(args, inputs, manifest, applied, plans)


@dataclass(frozen=True)
class _CampaignExecutor:
    """The campaign's ``RoleExecutor``: one ``BenchRoleExecutor`` per target.

    ``BenchRoleExecutor`` binds ONE target's plan and manifest slice, while the
    runner drives every target through the single injected executor — so this
    routes each call to the one that target belongs to.

    It is also where a live pass's typed failures (``executor
    .LIVE_PASS_FAILURES`` — a proof, a derivation, a render, a cross-check)
    become the bench's own refusal: the runner then ends THAT target through
    the refused arm with its partial artifacts preserved and runs the next one,
    instead of the exception escaping as a traceback that abandons them.
    """

    by_target: Mapping[str, Any]

    def _for(self, target: TargetPlan) -> Any:
        executor = self.by_target.get(target.target_id)
        if executor is None:
            raise BenchRefused(
                REFUSE_TARGET_NOT_PLANNED,
                f"no executor was composed for target {target.target_id!r}",
            )
        return executor

    async def _call(
        self, target: TargetPlan, method: str, *args: Any, **kwargs: Any
    ) -> Any:
        try:
            return await getattr(self._for(target), method)(
                *args, target=target, **kwargs
            )
        except LIVE_PASS_FAILURES as exc:
            raise BenchRefused(
                REFUSE_LIVE_PASS, f"{target.target_id}: {type(exc).__name__}: {exc}"
            ) from exc

    async def run_discovery(self, *, target: TargetPlan, **kwargs: Any) -> Any:
        return await self._call(target, "run_discovery", **kwargs)

    async def finish_discovery(
        self, captures: Any, *, target: TargetPlan, **kwargs: Any
    ) -> Any:
        return await self._call(target, "finish_discovery", captures, **kwargs)

    async def run_reference_sweep(self, *, target: TargetPlan, **kwargs: Any) -> Any:
        return await self._call(target, "run_reference_sweep", **kwargs)

    async def run_candidate(self, *, target: TargetPlan, **kwargs: Any) -> Any:
        return await self._call(target, "run_candidate", **kwargs)

    async def finish_candidate(
        self, capture: Any, *, target: TargetPlan, **kwargs: Any
    ) -> Any:
        return await self._call(target, "finish_candidate", capture, **kwargs)


def _run_live(
    args: argparse.Namespace,
    inputs: Mapping[str, object],
    manifest: CampaignManifest,
    applied: Mapping[str, Any],
    plans: Sequence[TargetPlan],
) -> int:
    """Run the on-device campaign under operator supervision, Stop-able by Ctrl-C.

    Everything the campaign measures with is already composed (the plans above)
    or resolved here from its own owner: the render binary (R5), the conductor
    context, the wired mic, the session volume claim, and the CamillaDSP
    controller. Every refusal — the bench's own, the box's, the mic's — leaves
    by the typed door with an exit code, never as a traceback.
    """

    # lazy: the box's and the mic's refusal vocabulary, both on-device modules.
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
        return asyncio.run(
            _campaign(args, inputs, manifest, applied, plans, binary, stop)
        )
    except BenchRefused as refusal:
        return _refused(refusal)
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
    inputs: Mapping[str, object],
    manifest: CampaignManifest,
    applied: Mapping[str, Any],
    plans: Sequence[TargetPlan],
    binary: Any,
    stop: Any,
) -> int:
    # lazy: every import below is an on-device collaborator (the controller,
    # the fader owner, the mux/fan-in sockets, the wired mic). The dry run must
    # reach none of them, and this is the only function that may.
    from jasper.active_speaker.crossover_v2.conductor_context import (
        conductor_status,
        resolve_conductor_context,
    )
    from jasper.active_speaker.crossover_v2.volume_claim import (
        MeasurementVolumeClaim,
        OwnerVolumeDoor,
    )
    from jasper.active_speaker.session_volume_plan import (
        DEFAULT_SESSION_VOLUME_STATE_PATH,
        SessionVolumePlan,
    )
    from jasper.active_speaker.volume_latch import fader_matches
    from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR
    from jasper.audio_measurement.evidence_identity import json_fingerprint
    from jasper.audio_measurement.wired_capture import require_wired_mic
    from jasper.bass_extension.bench.activation import snapshot_predecessor
    from jasper.bass_extension.bench.bundle import RETAINED_FACT_NAMES
    from jasper.bass_extension.bench.executor import (
        BenchRoleExecutor,
        estimate_campaign_render_count,
    )
    from jasper.bass_extension.bench.plan import (
        bench_role_targets,
        campaign_measured_context,
    )
    from jasper.bass_extension.bench.runner import BenchDeps, run_campaign
    from jasper.bass_extension.bench.sink import BundleSink
    from jasper.bass_extension.bench.wired_play import (
        REFUSE_COMMANDED_VOLUME,
        REFUSE_CONTROLLER,
        AdmissionContext,
        BenchWindow,
        ClaimFloorControl,
        WiredPlayAndCapture,
        bass_owner_role,
        controller_fader_reader,
    )
    from jasper.camilla import primary_controller
    from jasper.env_load import load_env_files
    from jasper.fanin.status import read_fanin_status
    from jasper.platform.uds import mux_socket_command
    from jasper.volume_coordinator import install_env_canonical_target_provider
    from jasper.volume_owner import volume_owner

    policy = _measurement_policy(inputs)
    level_db = _commanded_level_db(manifest)
    # Installed only once the operator's inputs are whole, and before anything
    # reads the box: a process that swaps the live graph needs BOTH the
    # canonical main-volume target every duck release lands on and THIS
    # process's fader owner, which is what the session claim is taken through.
    # Without them there is no SESSION_MEASUREMENT rank to claim with and the
    # campaign refuses at its first floor.
    load_env_files()
    install_env_canonical_target_provider()
    context = resolve_conductor_context(conductor_status())
    if not fader_matches(level_db, context.session_volume_db):
        raise BenchRefused(
            REFUSE_COMMANDED_VOLUME,
            f"the campaign commands {level_db:.2f} dB but this speaker's "
            f"session measurement volume is "
            f"{float(context.session_volume_db):.2f} dB",
        )
    admission = AdmissionContext(
        topology=context.topology,
        safety_profile=context.safety_profile,
        role_targets=bench_role_targets(
            context.role_targets,
            context.safety_profile,
            owner_role=bass_owner_role(context.preset, plans[0].owner_channels),
        ),
        session_volume_db=context.session_volume_db,
        declared_sensitivities=context.declared_sensitivities,
        preset=context.preset,
    )

    controller = primary_controller()
    # Read once, here: the bundle's natural_graph_fingerprint must be the graph
    # every rung's activation restores to, and this is the same snapshot the
    # activation seam re-proves at each window.
    predecessor = await snapshot_predecessor(controller)
    mic = require_wired_mic()
    owner = volume_owner()
    if owner is None:
        raise BenchRefused(
            REFUSE_NO_VOLUME_OWNER,
            "this process registered no fader owner; a bench that minted its "
            "own would be the second authority the owner exists to delete",
        )
    claim = MeasurementVolumeClaim(owner)
    read_fader = controller_fader_reader(controller)
    volume_plan = SessionVolumePlan(state_path=DEFAULT_SESSION_VOLUME_STATE_PATH)
    floor = ClaimFloorControl(owner, read_fader=read_fader, level_db=level_db)
    margin = MARGINS[manifest.margin_policy_name]
    sink = BundleSink(
        args.bundle_dir,
        bundle_id="bench-"
        + json_fingerprint(manifest.to_dict(), field_name="campaign manifest")[:12],
    )

    async def _mux_status() -> Mapping[str, Any]:
        # A socket-level failure already lands on the seam's controller
        # refusal; mux answering with an error or non-JSON is the same
        # unreadable status and is spelled the same way, never a traceback.
        try:
            return await mux_socket_command("STATUS")
        except (RuntimeError, ValueError) as exc:
            raise BenchRefused(REFUSE_CONTROLLER, f"mux_status: {exc}") from exc

    async def _fanin_status() -> Mapping[str, Any]:
        return await asyncio.to_thread(read_fanin_status) or {}

    play = WiredPlayAndCapture(
        sink=sink,
        controller=controller,
        mic=mic,
        plan=volume_plan,
        floor=floor,
        admission=admission,
        margin=margin,
        policy=policy,
        config_dir=DEFAULT_CAMILLA_CONFIG_DIR,
        read_mux_status=_mux_status,
        read_fanin_status=_fanin_status,
    )
    deps = BenchDeps(
        open_window=BenchWindow(
            plan=volume_plan,
            claim=claim,
            door=OwnerVolumeDoor(owner, read_fader=read_fader, claim=claim),
            floor=floor,
            level_db=level_db,
        ),
        controller=controller,
        floor=floor,
        executor=_CampaignExecutor(
            {
                plan.target_id: BenchRoleExecutor(
                    target=plan,
                    requests=manifest.requests[plan.target_id],
                    play_and_capture=play,
                    binary=binary,
                    margin=margin,
                    renders_outstanding=estimate_campaign_render_count(manifest),
                )
                for plan in plans
            }
        ),
        stop=stop,
    )

    sink.write_json(
        "tap-implementation-identity.json",
        binary.identity_artifact(),
        kind="jts_bass_extension_bench_tap_implementation_identity",
    )
    emitted = await run_campaign(
        deps,
        manifest=manifest,
        measured_context=campaign_measured_context(
            applied,
            plans,
            manifest=manifest,
            camilladsp_build_id=binary.camilladsp_build_id,
            tap_implementation_id=binary.tap_implementation_id,
            transparency_policy_fingerprint=json_fingerprint(
                {
                    "min_snr_db": policy.min_snr_db,
                    "max_tracking_rms_db": policy.max_tracking_rms_db,
                },
                field_name="transparency policy",
            ),
            natural_graph_fingerprint=predecessor.graph_fingerprint,
        ),
        targets=plans,
        retained_facts={
            name: sink.write_json(
                f"retained/{name}.json",
                {
                    "fact": name,
                    "replaced_by": "this campaign's fresh per-target evidence",
                    "target_ids": [plan.target_id for plan in plans],
                },
                kind="jts_bass_extension_bench_retained_fact",
            )
            for name in RETAINED_FACT_NAMES
        },
        sink=sink,
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
