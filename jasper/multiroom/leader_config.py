# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Bonded-leader CamillaDSP regeneration + solo restore — the grouping
reconciler's config-apply arm (Increment 5).

On bond form, the LEADER's one CamillaDSP must switch from the solo Ring B
sink to the snapserver pipe (the shared-stream producer); on disband it
must switch back. This module owns both moves, reusing the
SAME machinery as the wizards so the three apply paths cannot drift:

  - the saved sound profile + settings (``load_profile`` /
    ``load_sound_settings`` / ``output_trim_db``) — the persisted truth,
    no wizard involvement;
  - room-PEQ preservation from the ACTIVE config
    (``extract_room_peqs_from_config``), with the same custom-config
    refusal as ``/sound`` (a hand-rolled config is never silently
    rewritten);
  - the ONE member policy (``member_camilla_kwargs``) for the
    pipe/rate_adjust transforms;
  - the shared validated apply engine (``apply_dsp_config``) and the
    glitch-free ``set_config_file_path`` swap.

UNWIND LADDER (restore_solo_config): the prior solo config path is
stashed at bond-apply time (persistent — a bond survives reboots, so
``/run`` would lose it); restore prefers the stash, falls back to
re-emitting a solo config from the saved profile when the stash is
missing or its file is gone, and is a NO-OP when there is nothing to
restore (the common solo reconcile must not churn CamillaDSP). The
stash is cleared only after a successful restore, so a failed attempt
(CamillaDSP down) retries on the next reconcile.

All entry points are fail-LOUD (they raise; the reconciler catches,
logs ``event=multiroom.reconcile.camilla_*``, and continues managing
units — the doctor's ``leader pipe`` check + grouping runtime health
surface the unapplied state).
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from .. import atomic_io
from ..camilla_config_contract import (
    devices_playback_is_pipe,
    parse_camilla_devices_config,
    read_camilla_devices_config,
)
from ..log_event import log_event
from .config import GroupingConfig
from .member_config import member_camilla_kwargs

logger = logging.getLogger(__name__)

# The wizard config dir (sound_setup's DEFAULT_CONFIG_DIR). The bonded /
# restore configs live HERE, with names registered in the sound module's
# _JTS_GENERATED_RE — so a /sound or /correction apply while bonded
# recognises them as JTS-generated, preserves their room PEQs, and
# regenerates THROUGH the member policy instead of refusing (or worse,
# silently rewriting a "custom" config).
CONFIG_DIR = "/var/lib/camilladsp/configs"
BONDED_CONFIG_PATH = CONFIG_DIR + "/grouping_leader.yml"
SOLO_RESTORE_PATH = CONFIG_DIR + "/grouping_solo_restore.yml"

# Persistent prior-config stash (NOT /run: a bond survives reboots, and
# the unwind may happen many boots after the bond formed). Cleared only
# on successful restore. Carries a config PATH, never a secret.
PRIOR_STASH = "/var/lib/jasper/grouping-prior-camilla.txt"

REGEN_SOURCE = "grouping-reconcile"


def _camilla():
    """Return camilla#1 without coupling this oneshot to a web module."""
    from jasper.camilla import primary_controller

    return primary_controller()


# ---------- prior-config stash ----------

def _is_pipe_config(path: str) -> bool:
    """True when the config AT ``path`` is pipe-shaped (writes the
    snapserver FIFO). Used by BOTH stash guards: never *write* a
    pipe-shaped path into the stash, never *restore* one from it.
    Unreadable resolves to False — the write guard then skips stashing
    (defensive) and the read guard's separate ``exists()`` check already
    rejects missing files."""
    from .reconcile import SNAPFIFO

    return devices_playback_is_pipe(read_camilla_devices_config(path) or {}, SNAPFIFO)


def read_stash(path: str = PRIOR_STASH) -> str | None:
    """The stashed prior solo config path, or None (no stash / unreadable)."""
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return None
    return text or None


def _write_stash(value: str, path: str = PRIOR_STASH) -> None:
    atomic_io.atomic_write_text(path, value + "\n", mode=0o644)


def _clear_stash(path: str = PRIOR_STASH) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def restore_action(
    *, stash: str | None, stash_usable: bool, bonded_active: bool,
) -> str:
    """The unwind-ladder decision. PURE.

    ``stash_usable`` means the stashed path exists AND its content is a
    genuinely SOLO config (``not _is_pipe_config``). The content check is
    load-bearing: a /sound or /correction save WHILE BONDED regenerates
    that wizard's own config file (sound_current.yml / correction_*.yml)
    PIPE-shaped — the feature that keeps a bonded save from un-bonding
    camilla — so a path that was solo when stashed can be pipe-shaped by
    unwind time (or a later bonded reconcile can stash the wizard file
    itself). Restoring a pipe config after disband would point camilla
    at a FIFO whose creator (snapserver) is stopped and whose directory
    is reaped — a restart-flapping core audio unit. NEVER restore a
    pipe-shaped stash; fall through to re-emit.

    Returns one of:
      - ``"none"``     — nothing to restore (the common solo reconcile;
                         CamillaDSP is already on a solo config and no
                         stash is pending). MUST be a no-op upstream.
      - ``"stash"``    — apply the stashed prior config (verified solo).
      - ``"re_emit"``  — stash missing/gone/pipe-shaped: re-emit a solo
                         config from the saved profile and apply that.
    """
    if not stash and not bonded_active:
        return "none"
    if stash and stash_usable:
        return "stash"
    return "re_emit"


# ---------- the two apply flows ----------

async def apply_bonded_leader_config(
    cfg: GroupingConfig, *, camilla_factory=_camilla,
) -> str:
    """Regenerate + apply the bonded-leader pipe config. Returns the
    applied path; raises on failure (caller logs and continues).

    Reuses the /sound prepare shape: saved profile + settings, room PEQs
    preserved from the active config (custom configs refused, same as
    /sound), the member policy supplying the pipe sink, and the validated
    ``apply_dsp_config`` engine driving CamillaDSP's glitch-free config swap.
    """
    from jasper.dsp_apply import apply_dsp_config
    from jasper.sound.camilla_yaml import BASE_CONFIG_PATH
    from jasper.sound.graph_carrier import carrier_for_loaded_config
    from jasper.sound.profile import load_profile
    from jasper.sound.settings import load_sound_settings, output_trim_db

    cam = camilla_factory()
    current = await cam.get_config_file_path(best_effort=True)

    def _prepare() -> dict[str, int]:
        profile = load_profile()
        settings = load_sound_settings()
        # Same graph-carrier dispatch as /sound, so the three apply paths
        # cannot drift: preserve a JTS-generated config's room PEQs, refuse a
        # roleful/custom config (fail closed — never silently rewrite it into
        # the pipe), and treat a missing/flat current as the base (no PEQs).
        # The leader is the one caller that overrides the carrier's default
        # disk read of the member policy, injecting its already-resolved cfg
        # kwargs (the pipe sink).
        carrier = carrier_for_loaded_config(
            current or str(BASE_CONFIG_PATH), config_dir=CONFIG_DIR
        )
        result = carrier.reemit(
            profile,
            out_path=BONDED_CONFIG_PATH,
            profile_id=f"grouping-{cfg.bond_id or 'bond'}",
            output_trim_db=output_trim_db(profile, settings),
            member_kwargs=member_camilla_kwargs(cfg),
        )
        return {"room_peq_count": result.room_peq_count}

    await apply_dsp_config(
        source=REGEN_SOURCE,
        candidate_path=BONDED_CONFIG_PATH,
        prepare=_prepare,
        load_config=lambda p: cam.set_config_file_path(p, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(
            best_effort=True,
        ),
    )
    # Stash the prior path for the unwind — but ONLY a genuinely SOLO
    # config: never the bonded path itself, and never a pipe-shaped
    # wizard config (a /sound save while bonded regenerates
    # sound_current.yml PIPE-shaped; stashing it would make disband
    # "restore" a config that writes a FIFO nobody creates — see
    # restore_action). A pipe-shaped current leaves any existing stash
    # alone: it may still name the true pre-bond solo config.
    if (
        current
        and current != BONDED_CONFIG_PATH
        and not _is_pipe_config(current)
    ):
        _write_stash(current)
    log_event(
        logger,
        "multiroom.camilla_apply",
        result="bonded",
        path=BONDED_CONFIG_PATH,
        prior=current or "(none)",
    )
    return BONDED_CONFIG_PATH


async def restore_solo_config(*, camilla_factory=_camilla) -> str | None:
    """Unwind CamillaDSP to a solo config per the restore ladder.

    Returns the applied path, or None when there was nothing to do.
    Raises on a failed apply (stash kept — the next reconcile retries).
    """
    from jasper.dsp_apply import apply_dsp_config
    from jasper.output_topology import load_output_topology_strict
    from jasper.sound.camilla_yaml import (
        FLAT_GRAPH_WIDTH,
        FlatChannelPlan,
        emit_sound_config,
        extract_room_peqs_from_config,
        flat_graph_channel_plan,
        is_jts_generated_config,
    )
    from jasper.sound.profile import load_profile
    from jasper.sound.settings import load_sound_settings, output_trim_db

    stash = read_stash()
    cam = camilla_factory()
    current = await cam.get_config_file_path(best_effort=True)
    action = restore_action(
        stash=stash,
        stash_usable=bool(
            stash and Path(stash).exists() and not _is_pipe_config(stash)
        ),
        bonded_active=(current == BONDED_CONFIG_PATH),
    )
    if action == "none":
        return None

    if action == "stash":
        candidate = stash
        prepare = None
    else:  # re_emit — stash missing or its file is gone
        candidate = SOLO_RESTORE_PATH

        def prepare() -> dict[str, int]:
            profile = load_profile()
            settings = load_sound_settings()
            peqs = (
                extract_room_peqs_from_config(current)
                if current
                and is_jts_generated_config(current, config_dir=CONFIG_DIR)
                else []
            )
            # Deliberately NOT routed through the graph carrier (unlike the
            # /sound apply paths and apply_bonded_leader_config): un-bonding
            # must always succeed, so this stays lenient and never refuses — a
            # carrier refusal here would strand the speaker on the bonded pipe
            # config. It is safe to stay lenient because an active speaker
            # cannot form a bond in the first place (apply_bonded_leader_config
            # refuses an active `current` via the carrier), so `current` here
            # is never a roleful graph. Deliberately the SOLO defaults — no
            # member kwargs: this IS the un-bonding.
            #
            # The channel plan IS one of those solo defaults (#2179): lenient
            # is not blind. Un-bonding hands the box its own DAC back, so the
            # outputs the topology declines must be muted and a mono cabinet's
            # one output must carry the folded program, exactly as every other
            # flat writer emits them.
            def emit(plan: FlatChannelPlan) -> None:
                emit_sound_config(
                    profile,
                    room_peqs=peqs,
                    out_path=SOLO_RESTORE_PATH,
                    profile_id="grouping-solo-restore",
                    output_trim_db=output_trim_db(profile, settings),
                    muted_outputs=plan.muted_outputs,
                    mono_fold_output=plan.mono_fold_output,
                )

            # Best effort: the never-refuse constraint above, spelled as code.
            # The plan adds two ways to raise and NEITHER may strand the
            # speaker on the bonded pipe config, so the fallback is the empty
            # plan — byte-identical to passing neither argument (the emitter's
            # solo-impact contract), i.e. this path's pre-plan call verbatim.
            #
            # `ValueError` is exactly the width of those two:
            # `load_output_topology_strict` normalises every unreadable or
            # malformed topology into `OutputTopologyError` (a ValueError), and
            # `emit_sound_config` refuses an inconsistent plan at its API
            # boundary with a plain one. A write failure is deliberately NOT
            # caught — the fallback writes the same file, so an un-bond cannot
            # survive one either way.
            #
            # The topology is loaded HERE rather than left to the plan's own
            # soft failure so a CORRUPT one is disclosed instead of silently
            # degrading. A MISSING one is not a failure — nothing is declared,
            # so nothing is undeclared — and yields the empty plan quietly.
            try:
                emit(flat_graph_channel_plan(
                    load_output_topology_strict(), width=FLAT_GRAPH_WIDTH
                ))
            except ValueError as exc:
                log_event(
                    logger,
                    "multiroom.camilla_apply",
                    result="solo_restore_unplanned",
                    path=SOLO_RESTORE_PATH,
                    error=type(exc).__name__,
                )
                emit(FlatChannelPlan())
            return {"room_peq_count": len(peqs)}

    await apply_dsp_config(
        source=REGEN_SOURCE,
        candidate_path=candidate,
        prepare=prepare,
        load_config=lambda p: cam.set_config_file_path(p, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(
            best_effort=True,
        ),
    )
    _clear_stash()
    log_event(
        logger,
        "multiroom.camilla_apply",
        result="solo_restored",
        path=candidate,
        via=action,
    )
    return candidate


# ---------- producer liveness (for runtime health + the doctor) ----------

def playback_is_pipe(text: str, fifo: str) -> bool:
    """True when a CamillaDSP config's ``devices.playback`` block is a
    File sink writing ``fifo`` — the bonded-leader pipe. The text-taking
    face of :func:`devices_playback_is_pipe`, for the one caller that holds
    config text rather than a path."""
    return devices_playback_is_pipe(parse_camilla_devices_config(text), fifo)


def active_leader_pipe_path() -> str:
    """``SNAPFIFO`` when the ACTIVE CamillaDSP config writes the
    snapserver pipe, else ``""``. The producer-liveness signal for
    runtime health (/state + jasper-doctor): daemon-adjacent truth —
    CamillaDSP's own statefile names the loaded config, and that config's
    own devices block says whether it writes the pipe — never a mirror of env
    intent (the retired ``SNAPFIFO_PRODUCER_WIRED`` lesson). Total:
    any read failure resolves to ``""`` (degraded — fail visible)."""
    # The statefile's one reader — the ``JASPER_CAMILLA_STATEFILE`` override,
    # the shipped default, and the ``config_path`` parse all live in
    # active_speaker.environment. Lazy like every other import in this module,
    # and free for all three daemons that reach this function: jasper-control's
    # `_get_state` already imports that module for `active_speaker_parked`;
    # jasper-web builds every role-eligible wizard at startup, and `/sound`
    # pulls it under the same role set that gates `/rooms`; the doctor's
    # correction module imports it outright.
    from jasper.active_speaker.environment import read_camilla_statefile_config_path

    from .reconcile import SNAPFIFO

    devices = read_camilla_devices_config(read_camilla_statefile_config_path()) or {}
    return SNAPFIFO if devices_playback_is_pipe(devices, SNAPFIFO) else ""


# ---------- sync wrappers for the oneshot reconciler ----------

def apply_bonded_leader_config_sync(cfg: GroupingConfig) -> str:
    return asyncio.run(apply_bonded_leader_config(cfg))


def restore_solo_config_sync() -> str | None:
    return asyncio.run(restore_solo_config())
