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
    *, stash: str | None, stash_file_exists: bool, bonded_active: bool,
) -> str:
    """The unwind-ladder decision. PURE.

    Returns one of:
      - ``"none"``     — nothing to restore (the common solo reconcile;
                         CamillaDSP is already on a solo config and no
                         stash is pending). MUST be a no-op upstream.
      - ``"stash"``    — apply the stashed prior solo config.
      - ``"re_emit"``  — stash missing/gone: re-emit a solo config from
                         the saved profile and apply that.
    """
    if not stash and not bonded_active:
        return "none"
    if stash and stash_file_exists:
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
    from jasper.sound.camilla_yaml import (
        BASE_CONFIG_PATH,
        emit_sound_config,
        extract_room_peqs_from_config,
        is_base_config,
        is_jts_generated_config,
    )
    from jasper.sound.profile import load_profile
    from jasper.sound.settings import load_sound_settings, output_trim_db

    cam = camilla_factory()
    current = await cam.get_config_file_path(best_effort=True)

    def _prepare() -> dict[str, int]:
        profile = load_profile()
        settings = load_sound_settings()
        if not current or is_base_config(current):
            peqs = []
        elif is_jts_generated_config(current, config_dir=CONFIG_DIR):
            peqs = extract_room_peqs_from_config(current)
        else:
            raise RuntimeError(
                "CamillaDSP is running a custom config that JTS cannot "
                f"safely preserve ({current}). Reset to {BASE_CONFIG_PATH} "
                "or apply room correction before forming a bond."
            )
        emit_sound_config(
            profile,
            room_peqs=peqs,
            out_path=BONDED_CONFIG_PATH,
            profile_id=f"grouping-{cfg.bond_id or 'bond'}",
            output_trim_db=output_trim_db(profile, settings),
            **member_camilla_kwargs(cfg),
        )
        return {"room_peq_count": len(peqs)}

    await apply_dsp_config(
        source=REGEN_SOURCE,
        candidate_path=BONDED_CONFIG_PATH,
        prepare=_prepare,
        load_config=lambda p: cam.set_config_file_path(p, best_effort=False),
        get_current_config_path=lambda: cam.get_config_file_path(
            best_effort=True,
        ),
    )
    # Stash the prior SOLO path for the unwind — but never the bonded
    # path itself (a re-reconcile while bonded must not poison the
    # stash, or disband would "restore" the bond).
    if current and current != BONDED_CONFIG_PATH:
        _write_stash(current)
    logger.info(
        "event=multiroom.camilla_apply result=bonded path=%s prior=%s",
        BONDED_CONFIG_PATH, current or "(none)",
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
        stash_file_exists=bool(stash and Path(stash).exists()),
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
    logger.info(
        "event=multiroom.camilla_apply result=solo_restored path=%s via=%s",
        candidate, action,
    )
    return candidate


# ---------- sync wrappers for the oneshot reconciler ----------

def apply_bonded_leader_config_sync(cfg: GroupingConfig) -> str:
    return asyncio.run(apply_bonded_leader_config(cfg))


def restore_solo_config_sync() -> str | None:
    return asyncio.run(restore_solo_config())
