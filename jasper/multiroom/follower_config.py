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

    snapclient --player alsa -> hw:Loopback,0,5   (snapclient writes)
    CamillaDSP captures           hw:Loopback,1,5  (rate-tracked, bit-perfect)
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
    from jasper.active_speaker.baseline_profile import (
        build_baseline_profile_candidate,
    )
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.active_speaker.runtime_contract import classify_camilla_graph
    from jasper.output_topology import load_output_topology

    from .reconcile import (
        GROUPING_LOOPBACK_CAPTURE,
        GROUPING_LOOPBACK_CAPTURE_FORMAT,
    )

    program_channel = program_channel_for(cfg.channel)

    topology = load_output_topology()
    design_draft = load_design_draft()
    crossover_preview = load_crossover_preview(current_design_draft=design_draft)
    measurements = load_measurement_state(topology)

    # Emit the driver-domain-only graph to the follower-specific path, capturing
    # the round-trip loopback. The solo baseline state/config are untouched.
    # ``validate`` is a test seam (mirrors apply_baseline_profile); production
    # leaves it None so build uses the real CamillaDSP --check.
    build_kwargs = {} if validate is None else {"validate": validate}
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
        **build_kwargs,
    )
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

    # Invariant 5 — re-prove the EMITTED graph against the saved topology before
    # loading it. The emitter and verifier are independent on purpose: the
    # carrier emits, classify_camilla_graph re-proves Layer A is present
    # (crossover HP + per-driver limiter <= 0 + non-positive gain + 0 dB
    # ceiling). A graph that cannot be re-proven NEVER reaches CamillaDSP.
    graph = classify_camilla_graph(
        config_path=FOLLOWER_CONFIG_PATH, topology=topology,
    )
    if not graph.allowed:
        codes = [i.get("code") for i in graph.issues if isinstance(i, dict)]
        raise ActiveFollowerError(
            "graph_unprovable",
            "active follower driver-domain graph failed re-proof "
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
    from jasper.dsp_apply import apply_dsp_config

    from .reconcile import GROUPING_LOOPBACK_CAPTURE

    cam = camilla_factory()
    current = await cam.get_config_file_path(best_effort=True)
    await apply_dsp_config(
        source=REGEN_SOURCE,
        candidate_path=FOLLOWER_CONFIG_PATH,
        load_config=lambda p: cam.set_config_file_path(p, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(
            best_effort=True,
        ),
    )
    # Stash the prior solo-active config for the unwind — but only a genuinely
    # different (solo) config, never the follower config itself. Paths passed
    # explicitly (module globals read at CALL time) so tests can redirect them;
    # a def-time default would pin the production path (the reconcile idiom).
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


async def restore_active_follower_solo(*, camilla_factory=_camilla) -> str | None:
    """Unwind an active follower's CamillaDSP back to its solo-active baseline.

    Returns the applied path, or None when there is nothing to restore (the
    common reconcile on a solo-active box — no follower stash, CamillaDSP not on
    the follower config). Restore order: the stashed prior config, else the
    durable solo active baseline YAML on disk. **Never a passive graph** — a
    flat config on an active sink is the full-range-to-tweeter hazard. Raises on
    a failed apply (stash kept; the next reconcile retries).
    """
    from jasper.active_speaker.baseline_profile import baseline_config_path
    from jasper.dsp_apply import apply_dsp_config

    cam = camilla_factory()
    current = await cam.get_config_file_path(best_effort=True)
    stash = read_stash(FOLLOWER_PRIOR_STASH)

    if not stash and current != FOLLOWER_CONFIG_PATH:
        # Solo-active box that was never an active follower — no churn.
        return None

    candidate: str | None = None
    via = ""
    if stash and stash != FOLLOWER_CONFIG_PATH and Path(stash).exists():
        candidate, via = stash, "stash"
    else:
        # Stash missing/gone: fall back to the durable solo active baseline YAML
        # (the commissioned config; it persists on disk). Still an ACTIVE graph.
        durable = baseline_config_path()
        if durable.exists():
            candidate, via = str(durable), "durable_baseline"

    if candidate is None:
        # Nothing safe-and-active to restore. Do NOT downgrade to a passive
        # config; leave CamillaDSP on its current (safe Layer-A) graph and let
        # the doctor surface it. Clear the stash so we stop retrying a dead path.
        log_event(
            logger,
            "multiroom.camilla_apply",
            result="active_follower_restore_unavailable",
            current=current or "(none)",
            level=logging.WARNING,
        )
        _clear_stash(FOLLOWER_PRIOR_STASH)
        return None

    await apply_dsp_config(
        source=RESTORE_SOURCE,
        candidate_path=candidate,
        load_config=lambda p: cam.set_config_file_path(p, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(
            best_effort=True,
        ),
    )
    _clear_stash(FOLLOWER_PRIOR_STASH)
    log_event(
        logger,
        "multiroom.camilla_apply",
        result="active_follower_solo_restored",
        path=candidate,
        via=via,
    )
    return candidate


# ---------- sync wrappers for the oneshot reconciler ----------

def precheck_active_follower_sync(cfg: GroupingConfig) -> str:
    return asyncio.run(precheck_active_follower(cfg))


def apply_prebuilt_follower_config_sync() -> str:
    return asyncio.run(apply_prebuilt_follower_config())


def restore_active_follower_solo_sync() -> str | None:
    return asyncio.run(restore_active_follower_solo())
