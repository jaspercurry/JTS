# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The floor-tone restore's ``main_mute`` writes, through the one writer."""

from __future__ import annotations

import logging

import pytest

from jasper.volume_carrier import CamillaCarrier
from jasper.audio_resources.volume_owner import install_volume_owner
from jasper.web import volume_floor_tone

from ._log_events import event_field_maps
from .volume_coordinator_fixtures import _FakeCamilla

HOUSEHOLD_DB = -18.0


class _SilentTone:
    """The runner seam, playing nothing: the subject is the fader."""

    error = None
    running = True

    def __init__(self, _wav_path, *, on_finish=None) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


@pytest.fixture
def audition(tmp_path, monkeypatch):
    """A session, its CamillaDSP, and the process owner the web binds over it."""
    monkeypatch.setenv("JASPER_SOUND_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("JASPER_VOLUME_FLOOR_TONE_DIR", str(tmp_path / "tones"))
    camilla = _FakeCamilla(db=HOUSEHOLD_DB)
    carrier = CamillaCarrier(camilla=camilla)
    install_volume_owner(carrier.volume_owner)
    session = volume_floor_tone.VolumeFloorToneSession()
    session._writer_lock_dir = tmp_path
    return session, camilla, carrier.volume_owner


async def _start(session, camilla) -> dict:
    return await session.start_or_update(
        {"volume_floor_db": -40.0},
        camilla_factory=lambda: camilla,
        runner_factory=_SilentTone,
    )


@pytest.mark.parametrize(
    ("muted", "context"),
    [(True, "floor_tone_restore_mute"), (False, "floor_tone_restore_unmute")],
)
async def test_a_restore_whose_mute_write_fails_still_releases_the_claim(
    audition, caplog, muted: bool, context: str,
):
    session, camilla, owner = audition
    caplog.set_level(logging.WARNING, logger="jasper")
    camilla.muted = muted
    await _start(session, camilla)
    assert owner.declared_level_db() < HOUSEHOLD_DB

    camilla.mute_accepted = False
    await session.stop(camilla_factory=lambda: camilla, reason="stop")

    assert owner.declared_level_db() == pytest.approx(HOUSEHOLD_DB)
    assert await camilla.get_volume_db() == pytest.approx(HOUSEHOLD_DB)
    assert event_field_maps(caplog, "volume.main_mute") == [
        {"muted": str(muted).lower(), "context": context, "result": "failed"},
    ]


async def test_a_start_whose_unmute_fails_fails_the_start(audition, caplog):
    session, camilla, _owner = audition
    caplog.set_level(logging.WARNING, logger="jasper")
    camilla.mute_accepted = False

    with pytest.raises(RuntimeError):
        await _start(session, camilla)

    context = "floor_tone_start_unmute"
    assert event_field_maps(caplog, "volume.main_mute", context=context) == [
        {"muted": "false", "context": context, "result": "failed"},
    ]
