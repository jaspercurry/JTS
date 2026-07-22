# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active wireless follower CamillaDSP apply + solo restore — the grouping
reconciler's *active-follower* arm (distributed-active Slice 3,
docs/HANDOFF-distributed-active.md "The active follower (gap 2)").

A *dumb* (passive, single-DAC) follower plays the round-tripped stream through
outputd's ``dac_content`` lane, dropping its channel with a ``ChannelPick``;
its CamillaDSP stays out of the bonded path (jasper.multiroom.leader_config +
member_config own that). An *active* (multi-driver) follower cannot do that —
sending the full-range program to a tweeter would destroy it. So this module
relocates **Layer A** (the ``2->N`` split + per-driver crossover / limiter /
tweeter high-pass) onto the follower's own CamillaDSP, fed by the round-trip
loopback:

    snapclient --player alsa -> hw:Loopback,0,6   (snapclient writes)
    CamillaDSP captures           hw:Loopback,1,6  (rate-tracked, bit-perfect)
      -> driver-domain Layer A (channel-select -> split -> per-driver chain)
      -> outputd active sink -> DACs

The graph is ALWAYS the re-proven driver-domain baseline, so no capture
content — stream, silence, or garbage — can produce a full-range driver feed
(graph-resident protection, the active-crossover analogue of inv-1). The fixed
CamillaDSP pipeline latency is nulled by snapclient ``--latency`` so the active
follower stays sample-locked with a dumb follower.

This mirrors :mod:`jasper.multiroom.leader_config`'s structure (an apply flow
and a solo-restore flow, both fail-LOUD; the reconciler catches, logs
``event=multiroom.reconcile.camilla_*``, plays a cue, and keeps managing
units). The crucial difference from the leader/passive restore: an active
speaker's solo restore re-applies the **active baseline** (Layer A intact),
**never** a passive ``emit_sound_config`` — a flat graph on an active sink is
exactly the full-range-to-tweeter hazard this whole increment exists to
prevent.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .. import atomic_io
from ..log_event import log_event
from .config import GroupingConfig

logger = logging.getLogger(__name__)

# The follower driver-domain config + its (throwaway) compile state. The
# config name is registered in jasper.sound.camilla_yaml._JTS_GENERATED_RE so a
# /sound or /correction read while bonded recognises it as JTS-generated
# (never "custom"). The state path is DELIBERATELY follower-specific so the
# driver-domain compile never clobbers the solo baseline profile state at
# baseline_profile.DEFAULT_STATE_PATH — that record must survive the bond so
# the unbond restore can re-apply the solo active baseline.
CONFIG_DIR = "/var/lib/camilladsp/configs"
FOLLOWER_CONFIG_PATH = CONFIG_DIR + "/grouping_follower.yml"
FOLLOWER_STATE_PATH = "/var/lib/jasper/active_speaker_follower_profile.json"

# Persistent prior-config stash (NOT /run: a bond survives reboots, and the
# unwind may happen many boots after the bond formed). Cleared only on a
# successful restore. Carries a config PATH, never a secret. Separate from
# leader_config.PRIOR_STASH so the two arms never fight over one file.
FOLLOWER_PRIOR_STASH = "/var/lib/jasper/grouping-follower-prior-camilla.txt"

REGEN_SOURCE = "grouping-follower"
RESTORE_SOURCE = "grouping-follower-restore"

# cfg.channel -> the driver-domain program-channel pick. A single active
# 2-way follower has ONE set of drivers, so it plays one inter-speaker
# channel: a side (left/right) or a clip-safe mono sum. "stereo" (both
# channels) and "sub" (the wireless-sub member, gap 5) are NOT a single-box
# pick — fail closed rather than guess (HANDOFF-distributed-active.md gap 2).
_CHANNEL_TO_PROGRAM = {"left": "left", "right": "right", "mono": "mono"}


class ActiveFollowerError(RuntimeError):
    """An active follower could not be wired safely — fail closed (do not bond
    into a full-range state). ``reason`` is a short stable token for the cue +
    /state surface."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def program_channel_for(channel: str) -> str:
    """Map a bond ``channel`` to a driver-domain program-channel. PURE.

    Raises :class:`ActiveFollowerError` (fail closed) for ``stereo`` / ``sub`` /
    anything else — a single active 2-way follower cannot play a passthrough
    stereo pair, and the wireless sub is a separate design (gap 5)."""
    program = _CHANNEL_TO_PROGRAM.get(channel)
    if program is None:
        raise ActiveFollowerError(
            "channel_not_single_box_pick",
            f"active follower channel must be one of {sorted(_CHANNEL_TO_PROGRAM)}, "
            f"not {channel!r} (a single active speaker plays one inter-speaker "
            "channel; pick a side or mono in the pair wizard)",
        )
    return program


def _camilla():
    """Controller from env — mirrors leader_config._camilla (the oneshot does
    not import the web module)."""
    from jasper.camilla import CamillaController

    host = os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1")
    port = int(os.environ.get("JASPER_CAMILLA_PORT", "1234"))
    return CamillaController(host, port)


# ---------- prior-config stash ----------

def read_stash(path: str = FOLLOWER_PRIOR_STASH) -> str | None:
    """The stashed prior solo-active config path, or None."""
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return None
    return text or None


def _write_stash(value: str, path: str = FOLLOWER_PRIOR_STASH) -> None:
    atomic_io.atomic_write_text(path, value + "\n", mode=0o644)


def _clear_stash(path: str = FOLLOWER_PRIOR_STASH) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


# ---------- the two apply flows ----------

async def precheck_active_follower(
    cfg: GroupingConfig, *, validate=None,
) -> str:
    """Emit + RE-PROVE the active follower's driver-domain config — the
    fail-closed GATE, with no CamillaDSP I/O.

    Returns the written config path; raises :class:`ActiveFollowerError` on any
    fail-closed condition (bad channel, box not commissioned, or the emitted
    driver-only graph cannot be re-proven). The reconciler runs this BEFORE it
    tears down the solo path, so a follower that cannot be made safe never bonds
    — it falls back to solo active (invariant 5 + self-recovery). The companion
    :func:`apply_prebuilt_follower_config` does the actual CamillaDSP swap once
    snapclient is feeding the loopback.
    """
    from jasper.active_speaker import ActiveSpeakerConfigError
    from jasper.active_speaker.baseline_profile import (
        build_baseline_profile_candidate,
    )
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.active_speaker.runtime_contract import (
        GRAPH_DRIVER_DOMAIN_BASELINE,
        classify_camilla_graph,
    )
    from jasper.output_topology import (
        OutputTopologyError,
        load_output_topology_strict,
    )

    from .reconcile import (
        GROUPING_LOOPBACK_CAPTURE,
        GROUPING_LOOPBACK_CAPTURE_FORMAT,
    )

    program_channel = program_channel_for(cfg.channel)

    # STRICT topology load (fail-closed). The re-proof below passes this
    # topology explicitly to classify_camilla_graph, so a fail-SOFT loader
    # would hand it an empty draft (requires_roleful_graph=False) on a corrupt
    # topology.json — and a flat full-range graph then RE-PROVES as allowed
    # (the tweeter guard is keyed on a roleful topology). The 2026-05-23
    # filesystem-loss class corrupts topology.json too, so the soft loader goes
    # blind exactly when it matters. Refuse to bond on an unreadable topology.
    try:
        topology = load_output_topology_strict()
    except OutputTopologyError as exc:
        raise ActiveFollowerError(
            "topology_unreadable",
            "active follower cannot re-prove its graph — output topology is "
            f"missing/corrupt ({exc}); refusing to bond (no full-range emit)",
        ) from exc
    design_draft = load_design_draft()
    crossover_preview = load_crossover_preview(current_design_draft=design_draft)
    measurements = load_measurement_state(topology)

    # Emit the driver-domain-only graph to the follower-specific path, capturing
    # the round-trip loopback. The solo baseline state/config are untouched.
    # ``validate`` is a test seam (mirrors apply_baseline_profile); production
    # leaves it None so build uses the real CamillaDSP --check.
    build_kwargs = {} if validate is None else {"validate": validate}
    # The L0 emit gate inside emit_active_speaker_driver_domain_config raises
    # ActiveSpeakerConfigError (a ValueError) if the driver-domain graph would
    # ship an unprotected tweeter. Convert it to ActiveFollowerError (a
    # RuntimeError) so the reconciler's `except RuntimeError` fail-safe-to-solo
    # path catches it — a refused graph must fall back to solo, never crash the
    # reconciler oneshot. Mirrors the graph_unprovable re-prove refusal below.
    try:
        candidate = build_baseline_profile_candidate(
            topology,
            design_draft=design_draft,
            crossover_preview=crossover_preview,
            measurements=measurements,
            write=True,
            state_path=FOLLOWER_STATE_PATH,
            config_path=FOLLOWER_CONFIG_PATH,
            capture_device=GROUPING_LOOPBACK_CAPTURE,
            capture_format=GROUPING_LOOPBACK_CAPTURE_FORMAT,
            driver_domain=True,
            program_channel=program_channel,
            driver_domain_pair_trim_db=max(0.0, -float(cfg.trim_db)),
            **build_kwargs,
        )
    except ActiveSpeakerConfigError as exc:
        raise ActiveFollowerError(
            "driver_domain_emit_refused",
            "active follower driver-domain graph refused at the emit gate "
            f"(no full-range emit to a tweeter): {exc}",
        ) from exc
    if not candidate.get("permissions", {}).get("may_apply"):
        codes = [
            i.get("code") for i in candidate.get("issues", [])
            if isinstance(i, dict)
        ]
        raise ActiveFollowerError(
            "baseline_not_ready",
            "active follower has no ready driver-domain baseline to relocate "
            f"(status={candidate.get('status')}, issues={codes}); commission "
            "this speaker as an active speaker before bonding it",
        )

    # Re-prove the complete emitted Layer-A graph independently. The emit gates
    # cover tweeter HP and the bass-owner pair; this verifier also proves every
    # driver crossover/gain/limiter chain before the candidate can be loaded.
    graph = classify_camilla_graph(
        topology=topology,
        text=Path(FOLLOWER_CONFIG_PATH).read_text(encoding="utf-8"),
        config_path=FOLLOWER_CONFIG_PATH,
        bass_profile_summary=candidate.get("bass_extension_profile_summary"),
    )
    if (
        not graph.allowed
        or graph.classification != GRAPH_DRIVER_DOMAIN_BASELINE
    ):
        codes = [
            issue.get("code")
            for issue in graph.issues
            if isinstance(issue, dict)
        ]
        raise ActiveFollowerError(
            "graph_unprovable",
            "active follower driver-domain graph failed whole-graph re-proof "
            f"(classification={graph.classification}, issues={codes}); refusing "
            "to bond (no full-range emit)",
        )
    return FOLLOWER_CONFIG_PATH


async def apply_prebuilt_follower_config(*, camilla_factory=_camilla) -> str:
    """Swap CamillaDSP to the pre-checked driver-domain config + stash the prior
    solo-active config for the unwind. Call ONLY after
    :func:`precheck_active_follower` has built + re-proven it, and after
    snapclient is feeding the round-trip loopback (so CamillaDSP locks at once).
    """
    from jasper.active_speaker.runtime_contract import (
        GRAPH_DRIVER_DOMAIN_BASELINE,
    )
    from jasper.dsp_apply import apply_dsp_config, dsp_writer_lock

    from .reconcile import GROUPING_LOOPBACK_CAPTURE

    cam = camilla_factory()
    async with dsp_writer_lock(
        Path(FOLLOWER_CONFIG_PATH).parent,
        source=REGEN_SOURCE,
    ):
        current = await cam.get_config_file_path(best_effort=True)
        await apply_dsp_config(
            source=REGEN_SOURCE,
            candidate_path=FOLLOWER_CONFIG_PATH,
            load_config=lambda p: cam.set_config_file_path(p, best_effort=False),
            get_current_config_path=lambda: cam.get_config_file_path(
                best_effort=True,
            ),
            acquire_lock=False,
        )
        try:
            await _prove_live_bass_extension_graph(
                cam,
                expected_config_path=FOLLOWER_CONFIG_PATH,
                expected_classification=GRAPH_DRIVER_DOMAIN_BASELINE,
            )
        except RuntimeError as exc:
            if current and current != FOLLOWER_CONFIG_PATH:
                await apply_dsp_config(
                    source=f"{REGEN_SOURCE}-proof-rollback",
                    candidate_path=current,
                    load_config=lambda p: cam.set_config_file_path(
                        p, best_effort=False
                    ),
                    get_current_config_path=lambda: cam.get_config_file_path(
                        best_effort=True
                    ),
                    acquire_lock=False,
                )
            raise ActiveFollowerError(
                "graph_unprovable",
                "active follower driver-domain graph failed canonical live re-proof",
            ) from exc
        # Stash the prior solo-active config for the unwind — but only a
        # genuinely different (solo) config, never the follower config itself.
        # Paths passed explicitly (module globals read at CALL time) so tests
        # can redirect them; a def-time default would pin the production path.
        if current and current != FOLLOWER_CONFIG_PATH:
            _write_stash(current, path=FOLLOWER_PRIOR_STASH)
    log_event(
        logger,
        "multiroom.camilla_apply",
        result="active_follower",
        path=FOLLOWER_CONFIG_PATH,
        capture=GROUPING_LOOPBACK_CAPTURE,
        prior=current or "(none)",
    )
    return FOLLOWER_CONFIG_PATH


async def apply_active_follower_config(
    cfg: GroupingConfig, *, camilla_factory=_camilla, validate=None,
) -> str:
    """Combined precheck + apply (direct callers / tests). The reconciler uses
    the two phases separately (gate early, swap late)."""
    await precheck_active_follower(cfg, validate=validate)
    return await apply_prebuilt_follower_config(camilla_factory=camilla_factory)


async def restore_active_camilla_solo(
    *,
    camilla_factory=_camilla,
    stash_path: str = FOLLOWER_PRIOR_STASH,
    own_config_path: str = FOLLOWER_CONFIG_PATH,
    apply_source: str = RESTORE_SOURCE,
    log_kind: str = "active_follower",
) -> str | None:
    """Restore the always-on CamillaDSP (camilla#1) to a RE-PROVEN solo-active
    baseline — the shared unwind ladder behind BOTH the active *follower* and the
    active *leader* arms (jasper.multiroom.active_leader_config).

    Returns the applied path, or None when there is nothing to restore (the
    common reconcile on a solo-active box — no stash, CamillaDSP not on
    ``own_config_path``). Restore order: the stashed prior config, else the
    durable solo active baseline YAML on disk — and each candidate is
    RE-PROVEN with ``classify_camilla_graph`` against the saved topology before
    it is loaded. **Never a passive graph** — a flat config on an active sink is
    the full-range-to-tweeter hazard — and that promise is enforced AT LOAD, not
    by trusting the stash + on-disk integrity (a durable baseline could be
    corrupted/replaced — the 2026-05-23 filesystem-loss class). A candidate that
    cannot be re-proven is skipped; if none is provable, CamillaDSP is left on
    its current (safe) graph. Raises on a failed apply (stash kept; the next
    reconcile retries).

    Both arms restore the SAME always-on camilla#1 to the SAME re-proven active
    baseline; only the stashed-prior path (``stash_path``) and the own-config to
    exclude (``own_config_path`` — the follower's driver-domain graph, or the
    leader's program bake) differ. ``log_kind`` tags the structured ``result`` so
    the two arms stay distinguishable in the journal; ``apply_source`` labels the
    dsp-apply for the same reason.
    """
    from jasper.active_speaker.baseline_profile import baseline_config_path
    from jasper.active_speaker.runtime_contract import (
        GRAPH_APPROVED_ACTIVE_RUNTIME,
        safe_graph_for_current_topology,
    )
    from jasper.dsp_apply import apply_dsp_config, dsp_writer_lock
    from jasper.output_topology import (
        OutputTopologyError,
        load_output_topology_strict,
    )

    cam = camilla_factory()
    async with dsp_writer_lock(
        Path(own_config_path).parent,
        source=apply_source,
    ):
        current = await cam.get_config_file_path(best_effort=True)
        stash = read_stash(stash_path)

        if not stash and current != own_config_path:
            # Solo-active box that was never a bonded active endpoint — no churn.
            return None

        # STRICT topology load (fail-closed). The re-proof below keys "is this a
        # safe active graph" on the topology being roleful; a fail-SOFT empty
        # draft would let a flat full-range config pass re-proof (the tweeter
        # guard goes blind). On an unreadable topology, load nothing.
        try:
            topology = load_output_topology_strict()
        except OutputTopologyError as exc:
            log_event(
                logger,
                "multiroom.camilla_apply",
                result=f"{log_kind}_restore_topology_unreadable",
                error=str(exc),
                current=current or "(none)",
                level=logging.WARNING,
            )
            return None

        # Candidate order: the stashed prior solo config, then the durable solo
        # active baseline YAML on disk. Both must differ from our endpoint graph.
        options: list[tuple[str, str]] = []
        if stash and stash != own_config_path and Path(stash).exists():
            options.append((stash, "stash"))
        durable = baseline_config_path()
        if durable.exists() and str(durable) != own_config_path:
            options.append((str(durable), "durable_baseline"))

        candidate: str | None = None
        via = ""
        for cand, cand_via in options:
            decision = safe_graph_for_current_topology(
                topology,
                current_config_path=cand,
                consider_applied_baseline=False,
            )
            graph = decision.current_graph
            if (
                graph is not None
                and graph.allowed
                and decision.selected_config_path == cand
            ):
                candidate, via = cand, cand_via
                break
            if graph is None:
                codes = [
                    issue.get("code")
                    for issue in decision.issues
                    if isinstance(issue, dict)
                ]
                classification = "unavailable"
            else:
                codes = [
                    issue.get("code")
                    for issue in graph.issues
                    if isinstance(issue, dict)
                ]
                classification = graph.classification
            log_event(
                logger,
                "multiroom.camilla_apply",
                result=f"{log_kind}_restore_skip_unsafe",
                candidate=cand,
                via=cand_via,
                classification=classification,
                issues=codes,
                level=logging.WARNING,
            )

        if candidate is None:
            # Nothing safe-and-active to restore. Do NOT downgrade to a passive
            # graph. Clear the dead stash while ownership still excludes writers.
            log_event(
                logger,
                "multiroom.camilla_apply",
                result=f"{log_kind}_restore_unavailable",
                current=current or "(none)",
                level=logging.WARNING,
            )
            _clear_stash(stash_path)
            return None

        await apply_dsp_config(
            source=apply_source,
            candidate_path=candidate,
            load_config=lambda p: cam.set_config_file_path(p, best_effort=False),
            get_current_config_path=lambda: cam.get_config_file_path(
                best_effort=True,
            ),
            acquire_lock=False,
        )
        try:
            await _prove_live_bass_extension_graph(
                cam,
                expected_config_path=candidate,
                expected_classification=GRAPH_APPROVED_ACTIVE_RUNTIME,
            )
        except RuntimeError as exc:
            if current and current != candidate:
                await apply_dsp_config(
                    source=f"{apply_source}-proof-rollback",
                    candidate_path=current,
                    load_config=lambda p: cam.set_config_file_path(
                        p, best_effort=False
                    ),
                    get_current_config_path=lambda: cam.get_config_file_path(
                        best_effort=True
                    ),
                    acquire_lock=False,
                )
            raise ActiveFollowerError(
                "restore_graph_unprovable",
                "restored solo graph failed canonical live re-proof",
            ) from exc
        _clear_stash(stash_path)
    log_event(
        logger,
        "multiroom.camilla_apply",
        result=f"{log_kind}_solo_restored",
        path=candidate,
        via=via,
    )
    return candidate


async def _prove_live_bass_extension_graph(
    cam,
    *,
    expected_config_path: str | Path,
    expected_classification: str,
    statefile_path=None,
):
    """Canonical live graph/profile proof shared by both active bond roles."""

    from jasper.active_speaker.baseline_profile import baseline_profile_state_path
    from jasper.active_speaker.environment import DEFAULT_CAMILLA_STATEFILE
    from jasper.active_speaker.runtime_contract import (
        classify_active_bass_extension_graph,
    )
    from jasper.active_speaker.staging import staged_metadata_path
    from jasper.bass_extension import BASS_EXTENSION_APPLY_INTENT_PATH
    from jasper.bass_extension.profile import DEFAULT_PROFILE_PATH
    from jasper.output_topology import load_output_topology_strict

    proof = await classify_active_bass_extension_graph(
        load_output_topology_strict(),
        statefile_path=Path(statefile_path or DEFAULT_CAMILLA_STATEFILE),
        read_active_graph_text=lambda: cam.get_active_config_raw(best_effort=False),
        applied_baseline_path=baseline_profile_state_path(),
        profile_path=DEFAULT_PROFILE_PATH,
        intent_path=BASS_EXTENSION_APPLY_INTENT_PATH,
        staged_metadata_path=staged_metadata_path(),
    )
    if (
        not proof.allowed
        or proof.config_path != str(expected_config_path)
        or proof.classification != expected_classification
    ):
        code = proof.issues[0].get("code") if proof.issues else proof.classification
        raise RuntimeError(
            "live bass-extension graph proof failed: "
            f"{code} (path={proof.config_path!r}, "
            f"classification={proof.classification!r})"
        )
    return proof


async def restore_active_follower_solo(*, camilla_factory=_camilla) -> str | None:
    """Unwind an active *follower*'s CamillaDSP back to its solo-active baseline.

    Thin wrapper over :func:`restore_active_camilla_solo` with the follower's
    stash + config paths. The paths are read from the module globals HERE (at
    call time) and passed explicitly — never left to the primitive's def-time
    defaults — so a test that redirects ``FOLLOWER_PRIOR_STASH`` /
    ``FOLLOWER_CONFIG_PATH`` is honoured (the reconcile idiom). Kept as the named
    entry point the reconciler + the follower tests call.
    """
    return await restore_active_camilla_solo(
        camilla_factory=camilla_factory,
        stash_path=FOLLOWER_PRIOR_STASH,
        own_config_path=FOLLOWER_CONFIG_PATH,
    )


# ---------- sync wrappers for the oneshot reconciler ----------

def precheck_active_follower_sync(cfg: GroupingConfig) -> str:
    return asyncio.run(precheck_active_follower(cfg))


def apply_prebuilt_follower_config_sync() -> str:
    return asyncio.run(apply_prebuilt_follower_config())


def restore_active_follower_solo_sync() -> str | None:
    return asyncio.run(restore_active_follower_solo())
