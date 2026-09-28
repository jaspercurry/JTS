# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""CamillaDSP's main fader and ``main_mute``, as the volume coordinator drives them.

The one module that builds a :class:`~jasper.volume_owner.VolumeOwner`, and the
one caller of ``set_main_mute``.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .log_event import log_event
from .volume_curve import main_mute_for_db
from .volume_owner import VolumeOwner

if TYPE_CHECKING:
    from .camilla import CamillaController

logger = logging.getLogger(__name__)


async def write_main_mute(
    camilla: "CamillaController", muted: bool, *, context: str,
) -> bool:
    """Set ``main_mute`` best-effort and log the outcome; ``False`` when refused."""
    target = bool(muted)
    ok = await camilla.set_main_mute(target, best_effort=True)
    log_event(
        logger,
        "volume.main_mute",
        muted=str(target).lower(),
        context=context,
        result="accepted" if ok else "failed",
        level=logging.DEBUG if ok else logging.WARNING,
    )
    return ok


class CamillaCarrier:
    """The Camilla doors, and the fader owner that arbitrates their dB writes.

    ``volume_owner`` is the process's registered owner where the caller has
    one (one owner per process: ``volume_owner.install_volume_owner``);
    ``None`` builds one over this carrier's own fader doors.
    """

    def __init__(
        self,
        *,
        camilla: "CamillaController",
        volume_owner: VolumeOwner | None = None,
    ) -> None:
        self._camilla = camilla
        self.volume_owner = (
            volume_owner
            if volume_owner is not None
            else VolumeOwner(
                set_fader_db=self._write_fader_db,
                get_fader_db=self._read_fader_db,
            )
        )

    # Bound with best_effort=True: the owner's doors must report failure, not
    # raise it (``volume_latch.FADER_IO_ERRORS`` states that contract, and
    # ``CamillaUnavailable`` is deliberately not in it).
    async def _write_fader_db(self, db: float) -> bool:
        return await self._camilla.set_volume_db(db, best_effort=True)

    async def _read_fader_db(self) -> float | None:
        return await self._camilla.get_volume_db(best_effort=True)

    async def read_volume_and_mute(
        self,
    ) -> tuple[float | None, bool | None]:
        result = await self._camilla.get_volume_and_mute(best_effort=True)
        if result is not None:
            db, muted = result
            return float(db), bool(muted)
        return None, None

    async def write_main_mute(
        self, muted: bool, *, context: str,
    ) -> bool:
        return await write_main_mute(self._camilla, muted, context=context)

    async def write_db_with_mute(
        self, db: float, *, context: str,
    ) -> bool:
        """Land the household level, and the mute that goes with it.

        The dB half is the owner's — this is the coordinator declaring the
        HOUSEHOLD claim, and every fader write it makes goes through that one
        arbiter. The mute half stays here: ``main_mute`` is a separate flag
        with its own two writers, and folding it into a level claim would give
        the owner a second question to answer.
        """
        target_mute = main_mute_for_db(db)
        if target_mute:
            mute_ok = await self.write_main_mute(
                True, context=context,
            )
            _volume_ok = await self.volume_owner.declare_household_level_db(db)
            # Final content silence comes from main_mute. The dB floor is a
            # defense-in-depth fallback if the mute flag is later lost.
            return bool(mute_ok)

        volume_ok = await self.volume_owner.declare_household_level_db(db)
        if not volume_ok:
            return False
        return await self.write_main_mute(False, context=context)
