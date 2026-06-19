"""Bonded-leader CamillaDSP regeneration + solo restore — the grouping
reconciler's config-apply arm (Increment 5, HANDOFF-multiroom.md §2).

On bond form, the LEADER's one CamillaDSP must switch from the solo ALSA
loopback sink to the snapserver pipe (the shared-stream producer); on
disband it must switch back. This module owns both moves, reusing the
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
    """Controller from env — mirrors sound_setup._camilla (4 lines,
    duplicated rather than importing the web module into the oneshot)."""
    from jasper.camilla import CamillaController

    host = os.environ.get("JASPER_CAMILLA_HOST", "127.0.0.1")
    port = int(os.environ.get("JASPER_CAMILLA_PORT", "1234"))
    return CamillaController(host, port)


# ---------- prior-config stash ----------

def _is_pipe_config(path: str) -> bool:
    """True when the config AT ``path`` is pipe-shaped (writes the
    snapserver FIFO). Used by BOTH stash guards: never *write* a
    pipe-shaped path into the stash, never *restore* one from it.
    Unreadable resolves to False — the write guard then skips stashing
    (defensive) and the read guard's separate ``exists()`` check already
    rejects missing files."""
    from .reconcile import SNAPFIFO

    try:
        text = Path(path).read_text()
    except OSError:
        return False
    return playback_is_pipe(text, SNAPFIFO)


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
    /sound), the member policy supplying the pipe + rate_adjust
    transforms, and the validated ``apply_dsp_config`` engine driving
    CamillaDSP's glitch-free config swap.
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
        # kwargs (the pipe sink + rate_adjust off).
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
    from jasper.sound.camilla_yaml import (
        emit_sound_config,
        extract_room_peqs_from_config,
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
            # Deliberately the SOLO defaults — no member kwargs: this IS
            # the un-bonding.
            emit_sound_config(
                profile,
                room_peqs=peqs,
                out_path=SOLO_RESTORE_PATH,
                profile_id="grouping-solo-restore",
                output_trim_db=output_trim_db(profile, settings),
            )
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
    File sink writing ``fifo`` — the bonded-leader pipe. Scans the exact
    shape our emitters generate (a 2-space ``playback:`` line, 4-space
    fields)."""
    in_playback = False
    saw_file = False
    saw_fifo = False
    for line in text.splitlines():
        if line.rstrip() == "  playback:":
            in_playback = True
            continue
        if in_playback:
            if line.startswith("    "):
                field = line.strip()
                if field == "type: File":
                    saw_file = True
                elif field.startswith("filename:") and fifo in field:
                    saw_fifo = True
            else:
                in_playback = False
    return saw_file and saw_fifo


def active_leader_pipe_path() -> str:
    """``SNAPFIFO`` when the ACTIVE CamillaDSP config writes the
    snapserver pipe, else ``""``. The producer-liveness signal for
    runtime health (/state + jasper-doctor): daemon-adjacent truth —
    CamillaDSP's own statefile names the loaded config, and the config
    text says whether it writes the pipe — never a mirror of env
    intent (the retired ``SNAPFIFO_PRODUCER_WIRED`` lesson). Total:
    any read failure resolves to ``""`` (degraded — fail visible)."""
    import re

    # Statefile location mirrors jasper.cli.doctor.correction's
    # _active_camilla_config_path (kept in sync by the shared env knob;
    # not imported — the doctor package is the wrong layer to pull into
    # the /state hot path).
    statefile = Path(
        os.environ.get(
            "JASPER_CAMILLA_STATEFILE",
            "/var/lib/camilladsp/outputd-statefile.yml",
        )
    )
    try:
        text = statefile.read_text()
    except OSError:
        return ""
    match = re.search(r"^\s*config_path:\s*(.+?)\s*$", text, flags=re.MULTILINE)
    if not match:
        return ""
    config_path = match.group(1).strip().strip("'\"")
    if not config_path:
        return ""
    try:
        config_text = Path(config_path).read_text()
    except OSError:
        return ""
    from .reconcile import SNAPFIFO

    return SNAPFIFO if playback_is_pipe(config_text, SNAPFIFO) else ""


# ---------- sync wrappers for the oneshot reconciler ----------

def apply_bonded_leader_config_sync(cfg: GroupingConfig) -> str:
    return asyncio.run(apply_bonded_leader_config(cfg))


def restore_solo_config_sync() -> str | None:
    return asyncio.run(restore_solo_config())
