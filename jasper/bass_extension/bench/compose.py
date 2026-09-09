# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The campaign's dependency wiring: plans and a held speaker in, deps out.

Everything :func:`~jasper.bass_extension.bench.runner.run_campaign` needs,
assembled in one place so the operator CLI is parse → compose → run → report.
Nothing here decides a number: the levels come from the manifest, the caps and
targets from ``resolve_conductor_context``, the measurement bounds from
:data:`~jasper.bass_extension.bench.analysis.WIRED_MEASUREMENT_POLICY`.

**Why not** :func:`~jasper.active_speaker.crossover_v2.door.measurement_door`.
That door installs a MEASUREMENT graph on open and restores it on give-back,
and the bench may hold no such second graph authority: its activation seam
mutates the *running* config per rung and restores by ``reload()``, and
``snapshot_predecessor`` proves the running graph against the on-disk file —
which a door-installed graph would fail by construction. So this module takes
the door's own primitives instead: the ``live_measurement_session`` interlock
against a concurrent measurement (a second session moving the fader mid-hold is
what it exists to prevent), :func:`~jasper.active_speaker.crossover_v2.door
.measurement_claim`, :func:`~jasper.active_speaker.crossover_v2.door
.volume_door`, and — through
:class:`~jasper.bass_extension.bench.wired_play.BenchWindow` — the same
``give_back`` orchestration.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jasper.active_speaker.crossover_v2.door import (
    REFUSE_SESSION_LIVE,
    MeasurementDoorRefused,
    measurement_claim,
    volume_door,
)
from jasper.active_speaker.session_volume_plan import (
    DEFAULT_SESSION_VOLUME_STATE_PATH,
    SessionVolumePlan,
    live_measurement_session,
)
from jasper.active_speaker.volume_latch import fader_matches
from jasper.active_speaker.web_commissioning import DEFAULT_CAMILLA_CONFIG_DIR
from jasper.audio_measurement.evidence_identity import (
    ArtifactIdentity,
    json_fingerprint,
)
from jasper.audio_measurement.wired_capture import require_wired_mic
from jasper.bass_extension.targets import MARGINS
from jasper.fanin.status import read_fanin_status
from jasper.platform.uds import mux_socket_command

from .analysis import WIRED_MEASUREMENT_POLICY
from .bundle import RETAINED_FACT_NAMES
from .executor import (
    LIVE_PASS_FAILURES,
    BenchRoleExecutor,
    estimate_campaign_render_count,
)
from .manifest import CampaignManifest
from .plan import CampaignPlan, bench_role_targets, campaign_measured_context
from .runner import BenchDeps, BenchRefused, Stop, TargetPlan
from .sink import BundleSink
from .wired_play import (
    REFUSE_COMMANDED_VOLUME,
    REFUSE_CONTROLLER,
    AdmissionContext,
    BenchWindow,
    ClaimFloorControl,
    WiredPlayAndCapture,
    bass_owner_role,
    controller_fader_reader,
)

#: One target's live pass ended on a proof, derivation, render or cross-check.
REFUSE_LIVE_PASS = "bench_live_pass_failed"
#: The runner named a target this campaign composed no executor for.
REFUSE_TARGET_NOT_PLANNED = "bench_target_not_planned"

#: What the interlock's refusal sentence says this campaign was about to do.
BENCH_ACTION = "running the bass-extension limiter-evidence bench"


@dataclass(frozen=True, slots=True)
class BenchCampaign:
    """The composed campaign: what ``run_campaign`` is called with."""

    deps: BenchDeps
    sink: BundleSink
    measured_context: Mapping[str, Any]
    retained_facts: Mapping[str, ArtifactIdentity]


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

    by_target: Mapping[str, BenchRoleExecutor]

    def _for(self, target: TargetPlan) -> BenchRoleExecutor:
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


def commanded_level_db(manifest: CampaignManifest, context: Any) -> float:
    """The one level this campaign opens the speaker at, or a refusal.

    Two agreements, both provable before a device is opened: every request
    commands the SAME level, and that level is the one this speaker's session
    volume is resolved at — the play seam proves each stimulus against both.
    """

    level_db = manifest.commanded_level_db()
    if level_db is None:
        raise BenchRefused(
            REFUSE_COMMANDED_VOLUME,
            "the campaign manifest commands more than one main-volume level; "
            "one campaign opens one session volume",
        )
    if not fader_matches(level_db, context.session_volume_db):
        raise BenchRefused(
            REFUSE_COMMANDED_VOLUME,
            f"the campaign commands {level_db:.2f} dB but this speaker's "
            f"session measurement volume is "
            f"{float(context.session_volume_db):.2f} dB",
        )
    return level_db


def compose_campaign(
    *,
    manifest: CampaignManifest,
    campaign: CampaignPlan,
    context: Any,
    controller: Any,
    binary: Any,
    natural_graph_fingerprint: str,
    bundle_dir: Path,
    stop: Stop,
) -> BenchCampaign:
    """Hold this speaker's session and wire every collaborator onto it.

    ``context`` is a ``resolve_conductor_context`` result and ``binary`` an
    R5-resolved :class:`~jasper.bass_extension.bench.render.BinaryIdentity`.
    Refuses — before the claim is taken — a manifest whose level this speaker
    is not open at, and before anything else a speaker another measurement
    session is already holding.
    """

    plans = campaign.plans
    level_db = commanded_level_db(manifest, context)
    busy = live_measurement_session(
        state_path=DEFAULT_SESSION_VOLUME_STATE_PATH, action=BENCH_ACTION
    )
    if busy is not None:
        raise MeasurementDoorRefused(REFUSE_SESSION_LIVE, busy)

    owner, claim = measurement_claim()
    read_fader = controller_fader_reader(controller)
    volume_plan = SessionVolumePlan(state_path=DEFAULT_SESSION_VOLUME_STATE_PATH)
    floor = ClaimFloorControl(owner, read_fader=read_fader, level_db=level_db)
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
    mic = require_wired_mic()
    margin = MARGINS[manifest.margin_policy_name]
    sink = BundleSink(
        bundle_dir,
        bundle_id="bench-"
        + json_fingerprint(manifest.to_dict(), field_name="campaign manifest")[:12],
    )

    async def read_mux() -> Mapping[str, Any]:
        # A socket-level failure already lands on the play seam's controller
        # refusal; mux answering with an error or non-JSON is the same
        # unreadable status and is spelled the same way, never a traceback.
        try:
            return await mux_socket_command("STATUS")
        except (RuntimeError, ValueError) as exc:
            raise BenchRefused(REFUSE_CONTROLLER, f"mux_status: {exc}") from exc

    async def read_fanin() -> Mapping[str, Any]:
        return await asyncio.to_thread(read_fanin_status) or {}

    play = WiredPlayAndCapture(
        sink=sink,
        controller=controller,
        mic=mic,
        plan=volume_plan,
        floor=floor,
        admission=admission,
        margin=margin,
        policy=WIRED_MEASUREMENT_POLICY,
        config_dir=DEFAULT_CAMILLA_CONFIG_DIR,
        read_mux_status=read_mux,
        read_fanin_status=read_fanin,
    )
    deps = BenchDeps(
        open_window=BenchWindow(
            plan=volume_plan,
            claim=claim,
            door=volume_door(owner, lambda: controller, claim=claim),
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
    return BenchCampaign(
        deps=deps,
        sink=sink,
        measured_context=campaign_measured_context(
            campaign,
            manifest=manifest,
            camilladsp_build_id=binary.camilladsp_build_id,
            tap_implementation_id=binary.tap_implementation_id,
            natural_graph_fingerprint=natural_graph_fingerprint,
        ),
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
    )
