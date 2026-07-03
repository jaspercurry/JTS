# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Active *leader* CamillaDSP apply/restore arm — the grouping reconciler's
two-instance arm for an ACTIVE speaker that LEADS a bond (distributed-active
Stage B / Slice 5, docs/HANDOFF-distributed-active.md "The active leader
(gap 3)" + "Stage B — the ratified active-leader realization").

An active leader is **brains + an endpoint**: it bakes the program domain to the
wire AND runs its own per-driver crossover on the round-tripped stream, so it
needs TWO CamillaDSP instances — "one CamillaDSP drives one sink, and the leader
must feed both the wire (2 ch) and its own DACs (N ch)":

  - **camilla#1** (the always-on instance, ``:1234``) runs the PROGRAM-domain
    bake — Layer B room correction + Layer C preference EQ + headroom — to a
    ``File`` sink writing the snapserver pipe (``SNAPFIFO``), so the follower(s)
    receive a corrected stereo wire. ``enable_rate_adjust: false`` (a File sink
    has no output clock to steer; the one rate loop is downstream). This is
    :func:`jasper.active_speaker.emit_active_speaker_program_bake_config`
    (Slice-5 emit, PR #929).
  - **camilla#2** (the endpoint-crossover instance, ``:1235``,
    ``jasper-camilla-crossover.service`` — INERT infra from PR #930) runs the
    DRIVER domain — Layer A: the ``2->N`` split + per-driver crossover / delay /
    gain / soft-clip limiter (+ tweeter high-pass) — captured from the
    round-trip loopback (snapclient -> loopback -> camilla#2 [rate_adjust ON] ->
    DAC). This is **literally the follower endpoint config**
    (:func:`jasper.active_speaker.emit_active_speaker_driver_domain_config`, via
    ``build_baseline_profile_candidate(driver_domain=True, ...)``), so the
    leader's own drivers are protected by the SAME re-proven Layer-A graph a
    wireless follower uses.

This is the **music-only validated seam** (HANDOFF "Sequencing" step 1): no
``outputd-summer``, no leader TTS yet (Steps 2-3). camilla#2 keeps
``enable_rate_adjust`` ON — exactly the already-validated active-follower clock
seam — so a failure here has one candidate cause (the two-instance setup), not a
new clock topology.

Structure mirrors :mod:`jasper.multiroom.follower_config` (a fail-closed precheck
GATE + late applies + an unbond restore, all fail-LOUD; the reconciler catches,
logs ``event=multiroom.reconcile.camilla_*``, and keeps managing units). Two
differences from the follower:

  - The precheck builds + RE-PROVES **two** configs (camilla#1 bake + camilla#2
    driver-domain). Either failing refuses the bond (fail-safe to solo active —
    invariant 5).
  - camilla#1 is the always-on instance; camilla#2 is reconciler-armed
    (``systemctl enable --now`` the crossover unit, owned by the reconciler) on a
    statefile this module RE-SEEDS with the re-proven driver-domain config. That
    seam matters: the install seed is flat on a non-active box and the crossover
    guard repairs only a dead pipe (NOT a flat statefile), so the never-flat
    (never full-range to a tweeter) guarantee for an ARMED camilla#2 rests on
    THIS arm-time re-seed (deploy/install.sh ``ensure_crossover_camilla_statefile``
    flags exactly this hand-off). The reconciler owns camilla#2 unit lifecycle:
    disable before bake, then start from the reseeded statefile after the active
    content PCM release is proven.

The unbond restore re-uses :func:`jasper.multiroom.follower_config.restore_active_camilla_solo`
(the shared "restore camilla#1 to a RE-PROVEN active baseline, never passive"
ladder) — both arms restore the SAME always-on camilla#1 to the SAME re-proven
baseline; only the stashed-prior path and the own-config to exclude differ.
Disabling the camilla#2 UNIT on unbond is the reconciler's job (it owns systemctl).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil

from .. import atomic_io
from ..audio_runtime_plan import coupling_supported_for_route
from ..fanin.coupling_reconcile import read_persisted_coupling
from ..log_event import log_event
from . import follower_config
from .config import GroupingConfig
from .follower_config import program_channel_for

logger = logging.getLogger(__name__)

# camilla#1's program bake + camilla#2's driver-domain config + its (throwaway)
# compile state. Names registered in jasper.sound.camilla_yaml._JTS_GENERATED_RE
# so a /sound or /correction read while bonded recognises them as JTS-generated
# (never "custom"). DELIBERATELY leader-specific paths so neither clobbers the
# solo baseline profile state at baseline_profile.DEFAULT_STATE_PATH — that
# record must survive the bond so the unbond restore can re-apply the solo
# active baseline — and so neither collides with the active-FOLLOWER arm's files
# (a box is a leader xor a follower at a time, but separate files keep the two
# arms from ever fighting over one path).
CONFIG_DIR = "/var/lib/camilladsp/configs"
LEADER_BAKE_CONFIG_PATH = CONFIG_DIR + "/grouping_active_leader_bake.yml"
CROSSOVER_CONFIG_PATH = CONFIG_DIR + "/grouping_active_leader_crossover.yml"
CROSSOVER_STATE_PATH = "/var/lib/jasper/active_leader_crossover_profile.json"

# Persistent prior-config stash for camilla#1's solo-active baseline (NOT /run:
# a bond survives reboots, and the unwind may happen many boots after the bond
# formed). Cleared only on a successful restore. Carries a config PATH, never a
# secret. Separate from follower_config.FOLLOWER_PRIOR_STASH so the two arms
# never fight over one file.
LEADER_BAKE_PRIOR_STASH = "/var/lib/jasper/grouping-active-leader-prior-camilla.txt"

# camilla#2's OWN statefile (NOT camilla#1's outputd-statefile.yml). The unit
# loads its config from this file's ``config_path:`` field. The default mirrors
# deploy/systemd/jasper-camilla-crossover.service + the crossover guard +
# Config.camilla2_statefile; read via JASPER_CAMILLA2_STATEFILE at CALL time so
# the env override (and tests) are honoured.
_DEFAULT_CROSSOVER_STATEFILE = "/var/lib/camilladsp/crossover-statefile.yml"

REGEN_SOURCE = "grouping-active-leader-bake"
RESTORE_SOURCE = "grouping-active-leader-restore"
# Tags the shared restore ladder's structured ``result`` lines so the leader arm
# stays distinguishable from the follower arm in the journal.
RESTORE_LOG_KIND = "active_leader"


class ActiveLeaderError(RuntimeError):
    """An active leader could not be wired safely — fail closed (do not bond into
    a state that could send full-range to a tweeter). ``reason`` is a short
    stable token for the log + /state surface. Sibling of
    :class:`jasper.multiroom.follower_config.ActiveFollowerError`."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def crossover_statefile_path() -> str:
    """camilla#2's statefile path (``JASPER_CAMILLA2_STATEFILE``), read at CALL
    time so an env override / test redirect is honoured."""
    return os.environ.get("JASPER_CAMILLA2_STATEFILE", _DEFAULT_CROSSOVER_STATEFILE)


def _camilla():
    """camilla#1 controller from env — mirrors follower_config._camilla /
    leader_config._camilla (the oneshot does not import the web module)."""
    from jasper.camilla import CamillaController

    host = os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1")
    port = int(os.environ.get("JASPER_CAMILLA_PORT", "1234"))
    return CamillaController(host, port)


# ---------- the fail-closed GATE: build + re-prove BOTH instances ----------

async def precheck_active_leader(
    cfg: GroupingConfig, *, validate=None,
) -> tuple[str, str]:
    """Emit + RE-PROVE camilla#1's program bake AND camilla#2's driver-domain
    config — the fail-closed GATE, with no CamillaDSP I/O and no systemctl.

    Returns ``(bake_path, crossover_path)``; raises :class:`ActiveLeaderError` on
    any fail-closed condition (bad channel, box not commissioned, unreadable
    topology, or either emitted graph cannot be re-proven). The reconciler runs
    this BEFORE it tears down the solo path, so a leader that cannot be made safe
    never bonds — it falls back to solo active so the box keeps playing its own
    content (invariant 5 + self-recovery). The companion late steps
    (:func:`apply_active_leader_bake`, :func:`seed_crossover_statefile`, and the
    reconciler's ``systemctl enable --now`` of the crossover unit) run only after
    snapserver + snapclient are up.
    """
    from jasper.active_speaker import (
        ActiveSpeakerConfigError,
        emit_active_speaker_program_bake_config,
    )
    from jasper.active_speaker.baseline_profile import (
        build_baseline_profile_candidate,
    )
    from jasper.active_speaker.crossover_preview import load_crossover_preview
    from jasper.active_speaker.design_draft import load_design_draft
    from jasper.active_speaker.measurement import load_measurement_state
    from jasper.active_speaker.runtime_contract import classify_camilla_graph
    from jasper.output_topology import (
        OutputTopologyError,
        load_output_topology_strict,
    )
    from jasper.sound.profile import load_profile
    from jasper.sound.settings import load_sound_settings, output_trim_db

    from .reconcile import (
        GROUPING_LOOPBACK_CAPTURE,
        GROUPING_LOOPBACK_CAPTURE_FORMAT,
    )

    # program_channel_for is the SHARED single-box channel pick; re-raise its
    # follower-flavoured error as the leader error so this arm raises a single
    # consistent type (the ``reason`` token is role-neutral, preserved verbatim).
    try:
        program_channel = program_channel_for(cfg.channel)
    except follower_config.ActiveFollowerError as exc:
        raise ActiveLeaderError(exc.reason, str(exc)) from exc

    # Snapcast precondition (fail-closed). An active leader HOSTS the wire
    # (snapserver) AND plays its own channel through the round-trip (snapclient);
    # without those binaries there is no FIFO reader for camilla#1's bake, so the
    # bake cannot release the DAC — and arming camilla#2 onto the DAC would then
    # fight camilla#1 and exhaust its recovery budget (the 2026-06-23 JTS5
    # incident, on a box with no Snapcast installed).
    # Refuse the bond UP FRONT (the box stays solo-active) rather than commit to a
    # two-instance setup the hardware cannot support. The reconciler's step-5
    # gates (snapserver-actually-active + arm-only-if-bake-succeeded) are the
    # belt-and-suspenders runtime backstop.
    for binary in ("snapserver", "snapclient"):
        if shutil.which(binary) is None:
            raise ActiveLeaderError(
                "snapcast_unavailable",
                f"{binary} is not installed — an active leader cannot host the "
                "wireless pair; refusing to bond (no camilla#1/#2 DAC conflict)",
            )

    coupling_support = coupling_supported_for_route(
        read_persisted_coupling(), "active_leader"
    )
    if not coupling_support.supported:
        raise ActiveLeaderError(
            coupling_support.reason,
            coupling_support.detail
            + " Run `jasper-fanin-coupling-reconcile loopback` before bonding.",
        )

    # STRICT topology load (fail-closed). Both re-proofs below pass this topology
    # explicitly to classify_camilla_graph, so a fail-SOFT loader would hand them
    # an empty draft (requires_roleful_graph=False) on a corrupt topology.json —
    # and a flat full-range graph would then RE-PROVE allowed (the tweeter guard
    # is keyed on a roleful topology). The 2026-05-23 filesystem-loss class
    # corrupts topology.json too, so refuse to bond on an unreadable topology.
    try:
        topology = load_output_topology_strict()
    except OutputTopologyError as exc:
        raise ActiveLeaderError(
            "topology_unreadable",
            "active leader cannot re-prove its graphs — output topology is "
            f"missing/corrupt ({exc}); refusing to bond (no full-range emit)",
        ) from exc

    # 1. camilla#2 driver-domain (Layer A) — the leader's OWN drivers, captured
    #    from the round-trip loopback. Identical build to the active follower
    #    (build_baseline_profile_candidate(driver_domain=True, ...)) — the leader
    #    is its own receiver — only the config/state paths differ so the solo
    #    baseline + follower files are never clobbered.
    design_draft = load_design_draft()
    crossover_preview = load_crossover_preview(current_design_draft=design_draft)
    measurements = load_measurement_state(topology)
    # ``validate`` is a test seam (mirrors apply_baseline_profile); production
    # leaves it None so build uses the real CamillaDSP --check.
    build_kwargs = {} if validate is None else {"validate": validate}
    # The L0 emit gate inside emit_active_speaker_driver_domain_config raises
    # ActiveSpeakerConfigError (a ValueError) if the driver-domain graph would
    # ship an unprotected tweeter. Convert it to ActiveLeaderError (a
    # RuntimeError) so the reconciler's `except RuntimeError` fail-safe-to-solo
    # path catches it — a refused graph must fall back to solo, never crash the
    # reconciler oneshot. Mirrors the ActiveFollowerError re-raise below.
    try:
        candidate = build_baseline_profile_candidate(
            topology,
            design_draft=design_draft,
            crossover_preview=crossover_preview,
            measurements=measurements,
            write=True,
            state_path=CROSSOVER_STATE_PATH,
            config_path=CROSSOVER_CONFIG_PATH,
            capture_device=GROUPING_LOOPBACK_CAPTURE,
            capture_format=GROUPING_LOOPBACK_CAPTURE_FORMAT,
            driver_domain=True,
            program_channel=program_channel,
            driver_domain_pair_trim_db=max(0.0, -float(cfg.trim_db)),
            **build_kwargs,
        )
    except ActiveSpeakerConfigError as exc:
        raise ActiveLeaderError(
            "driver_domain_emit_refused",
            "active leader camilla#2 driver-domain graph refused at the emit "
            f"gate (no full-range emit to a tweeter): {exc}",
        ) from exc
    if not candidate.get("permissions", {}).get("may_apply"):
        codes = [
            i.get("code") for i in candidate.get("issues", [])
            if isinstance(i, dict)
        ]
        raise ActiveLeaderError(
            "baseline_not_ready",
            "active leader has no ready driver-domain baseline for camilla#2 "
            f"(status={candidate.get('status')}, issues={codes}); commission "
            "this speaker as an active speaker before leading a bond",
        )
    crossover_graph = classify_camilla_graph(
        config_path=CROSSOVER_CONFIG_PATH, topology=topology,
    )
    if not crossover_graph.allowed:
        codes = [i.get("code") for i in crossover_graph.issues if isinstance(i, dict)]
        raise ActiveLeaderError(
            "crossover_graph_unprovable",
            "active leader camilla#2 driver-domain graph failed re-proof "
            f"(classification={crossover_graph.classification}, issues={codes}); "
            "refusing to bond (no full-range emit)",
        )

    # 2. camilla#1 program bake (Layer B/C + headroom, File -> SNAPFIFO,
    #    enable_rate_adjust=False). The initial bond bake is emitted and re-proved
    #    here. Once this graph is loaded, /sound and /correction may re-emit its
    #    program domain through the graph carrier, but only while grouping state
    #    still resolves to the same pipe sink; roleful active graphs keep the
    #    eq_on_active_bonded_member fence.
    #    Inputs mirror the passive leader's apply_bonded_leader_config: the saved
    #    SoundProfile (Layer C preference EQ + headroom) and the output trim.
    #    room_peqs is empty: an active speaker does NOT embed Layer B room
    #    correction in its baseline today (it rides Layer C/preference + headroom
    #    only — see emit_active_speaker_baseline_config), so the faithful bake of
    #    today's solo-active program domain carries no room PEQs. When active room
    #    correction lands, source them here so the wire stays in sync with what
    #    the speaker applies solo. (On-device Step-1 gate confirms the followers
    #    hear the same correction the leader does solo.)
    profile = load_profile()
    settings = load_sound_settings()
    emit_active_speaker_program_bake_config(
        profile,
        room_peqs=[],
        output_trim_db=output_trim_db(profile, settings),
        out_path=LEADER_BAKE_CONFIG_PATH,
        profile_id=f"grouping-{cfg.bond_id or 'bond'}",
    )
    bake_graph = classify_camilla_graph(
        config_path=LEADER_BAKE_CONFIG_PATH, topology=topology,
    )
    if not bake_graph.allowed:
        codes = [i.get("code") for i in bake_graph.issues if isinstance(i, dict)]
        raise ActiveLeaderError(
            "bake_graph_unprovable",
            "active leader camilla#1 program-bake graph failed re-proof "
            f"(classification={bake_graph.classification}, issues={codes}); "
            "refusing to bond",
        )
    return LEADER_BAKE_CONFIG_PATH, CROSSOVER_CONFIG_PATH


# ---------- late applies (after snapserver + snapclient are up) ----------

async def apply_active_leader_bake(*, camilla_factory=_camilla) -> str:
    """Swap camilla#1 to the pre-checked program bake (the wire feed) + stash the
    prior solo-active config for the unwind. Call ONLY after
    :func:`precheck_active_leader` has built + re-proven it, and after snapserver
    is up (the pipe's reader exists — a FIFO write-open blocks until a reader
    exists, exactly like the passive leader's apply_bonded_leader_config).
    """
    from jasper.dsp_apply import apply_dsp_config

    cam = camilla_factory()
    current = await cam.get_config_file_path(best_effort=True)
    await apply_dsp_config(
        source=REGEN_SOURCE,
        candidate_path=LEADER_BAKE_CONFIG_PATH,
        load_config=lambda p: cam.set_config_file_path(p, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(
            best_effort=True,
        ),
    )
    # Stash the prior solo-active config for the unwind — but only a genuinely
    # different (solo) config, never the bake itself. (The shared restore ladder
    # re-proves every candidate, so even a stale/odd stash can never load a
    # passive graph onto the active sink.) Same on-disk shape the shared
    # follower_config.read_stash reads (a config PATH + trailing newline, mode
    # 0644 — matching the sibling FOLLOWER_PRIOR_STASH; a non-secret path).
    if current and current != LEADER_BAKE_CONFIG_PATH:
        atomic_io.atomic_write_text(
            LEADER_BAKE_PRIOR_STASH, current + "\n", mode=0o644,
        )
    log_event(
        logger,
        "multiroom.camilla_apply",
        result="active_leader_bake",
        path=LEADER_BAKE_CONFIG_PATH,
        prior=current or "(none)",
    )
    return LEADER_BAKE_CONFIG_PATH


def seed_crossover_statefile(
    *, config_path: str | None = None, statefile: str | None = None,
) -> str:
    """RE-SEED camilla#2's statefile to load the re-proven driver-domain config,
    then the reconciler ``systemctl enable --now``-s the crossover unit.

    This closes the seam B1 (PR #930) flagged: the install seed is flat on a
    non-active box and the crossover guard repairs ONLY a dead bonded pipe, NOT a
    flat statefile — so the never-flat (never full-range to a tweeter) guarantee
    for an ARMED camilla#2 rests on THIS arm-time re-seed pointing the statefile
    at the re-proven driver-domain (Layer-A-intact) graph. Reuses the canonical
    :func:`jasper.active_speaker.runtime_contract.write_camilla_statefile` (the
    same writer install.sh + the runtime contract use), which preserves any
    existing statefile fields, writes ``config_path`` + muted/unity slots, mode
    0644. Returns the statefile path written.

    Paths read from the module globals / env at CALL time (the reconcile idiom),
    overridable for tests.
    """
    from jasper.active_speaker.runtime_contract import write_camilla_statefile

    target_config = config_path or CROSSOVER_CONFIG_PATH
    target_statefile = statefile or crossover_statefile_path()
    write_camilla_statefile(target_statefile, target_config)
    log_event(
        logger,
        "multiroom.camilla_apply",
        result="active_leader_crossover_seeded",
        statefile=target_statefile,
        config=target_config,
    )
    return target_statefile


# ---------- unbond restore (camilla#1 config; the reconciler disables the unit) ----------

async def restore_active_leader_solo(*, camilla_factory=_camilla) -> str | None:
    """Unwind an active leader's camilla#1 back to its solo-active baseline.

    Thin wrapper over
    :func:`jasper.multiroom.follower_config.restore_active_camilla_solo` with the
    leader's stash + bake-config paths (read from the module globals HERE, at
    call time, and passed explicitly — never left to the primitive's def-time
    defaults — so a test redirect is honoured). Both arms restore the SAME
    always-on camilla#1 to the SAME re-proven active baseline (never passive — a
    flat config on an active sink is the full-range-to-tweeter hazard); only the
    stashed-prior path and the own-config to exclude differ.

    Disabling the camilla#2 UNIT (``systemctl disable --now``) is the
    reconciler's job — it owns every unit's lifecycle — and is gated there on the
    unit being enabled (the signal "this box WAS an active leader"), so the
    untouched active-FOLLOWER restore path stays byte-identical.
    """
    return await follower_config.restore_active_camilla_solo(
        camilla_factory=camilla_factory,
        stash_path=LEADER_BAKE_PRIOR_STASH,
        own_config_path=LEADER_BAKE_CONFIG_PATH,
        apply_source=RESTORE_SOURCE,
        log_kind=RESTORE_LOG_KIND,
    )


# ---------- sync wrappers for the oneshot reconciler ----------

def precheck_active_leader_sync(cfg: GroupingConfig) -> tuple[str, str]:
    return asyncio.run(precheck_active_leader(cfg))


def apply_active_leader_bake_sync() -> str:
    return asyncio.run(apply_active_leader_bake())


def restore_active_leader_solo_sync() -> str | None:
    return asyncio.run(restore_active_leader_solo())
