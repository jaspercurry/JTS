# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Conductor W5a: the play transaction."""

from __future__ import annotations

import asyncio
import pytest
import yaml
from tests.crossover_v2_fixtures import (
    SESSION_VOLUME_DB,
    _dummy_program,
)


def test_bind_program_playback_seams_is_the_play_transaction_and_confirms_strictly(
    tmp_path,
):
    """What the binding still owns after wave 6b, and what it hands off.

    The graph seams moved to ``MeasurementSessionGraph``; the SetConfig
    transport claim they carried — load and restore ride
    ``set_active_config_raw``, never ``set_config_file_path``, so the statefile
    boot anchor stays put and a crash mid-session reboots onto the staged
    anchor — moved with them and is pinned in
    ``tests/test_crossover_v2_session_graph.py``. ``confirm_graph_is_live``
    moved with the binding to ``crossover_v2.composition``; its strictness is
    still pinned here.
    """
    from jasper.active_speaker.crossover_v2 import composition
    from jasper.active_speaker.crossover_v2.composition import (
        bind_program_playback_seams,
    )
    from jasper.audio_control.camilla import CamillaConfigRejected

    calls: list = []

    class _FakeCam:
        """Models the 2026-08-05 hardware probe of CamillaDSP 4.1.3.

        ``GetConfig`` returns a default-filled, value-normalized SUPERSET of
        what was submitted (extra null keys; a submitted ``0`` back as ``0.0``),
        and ``ReadConfig`` — ``normalize_config_raw`` — applies exactly the same
        transform without applying anything. Comparing submitted TEXT against
        the readback would refuse every load on this fake, which is the point.
        """

        live = "prior: graph\n"

        @staticmethod
        def _camilla_serde(text):
            parsed = yaml.safe_load(text) or {}
            filled = {"description": None, "bypassed": None, **parsed}
            return yaml.safe_dump(
                {k: (0.0 if v == 0 else v) for k, v in filled.items()}
            )

        async def get_config_file_path(self, *, best_effort):
            calls.append(("get_path", best_effort))
            return str(tmp_path / "entry.yml")

        async def set_active_config_raw(self, text, *, best_effort, duck=True):
            calls.append(("set_raw", text, best_effort))
            self.live = text
            return True

        async def get_active_config_raw(self, *, best_effort):
            calls.append(("get_raw", best_effort))
            return self._camilla_serde(self.live)

        async def normalize_config_raw(self, text, *, best_effort):
            # What a live, healthy CamillaDSP raises for a config it parsed and
            # refused — CamillaController._call already maps pycamilladsp's
            # ConfigValidationError onto this class.
            if "!!not-yaml" in text:
                raise CamillaConfigRejected("camilla rejected the config")
            return self._camilla_serde(text)

        async def set_config_file_path(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("must never repoint the persisted statefile")

    entry = tmp_path / "entry.yml"
    entry.write_text("prior: graph\n", encoding="utf-8")
    cam = _FakeCam()
    seams = bind_program_playback_seams(
        cam,
        bundle_dir=str(tmp_path),
        artifact=object(),
        config_dir=str(tmp_path),
        program=_dummy_program(),
        wav_path=str(tmp_path / "program.wav"),
        topology=object(),
        safety_profile={},
        role_targets={},
        session_volume_db=SESSION_VOLUME_DB,
        graph_yaml="program: graph\n",
    )
    # The count IS the claim, and wave 6b shrank it: the three graph seams
    # moved to ``MeasurementSessionGraph``, which installs one graph per session
    # instead of swapping one in and out per stimulus. What is left here is the
    # play transaction proper.
    assert set(seams) == {"play_wav", "readmit", "writer_lock"}

    from jasper.active_speaker.program_playback import ProgramPlaybackError

    # ``confirm_graph_is_live`` moved WITH the binding to ``composition`` —
    # the session graph calls it, and its strictness is the same three claims
    # it always made.
    #
    # Default-fill tolerance: the readback is a normalized SUPERSET of the
    # submitted text, and a load is still CONFIRMED.
    cam.live = "program: graph\n"
    asyncio.run(composition.confirm_graph_is_live(cam, "program: graph\n"))
    # A genuinely different graph is still rejected — the check is strict
    # equality of normalized fingerprints, not a subset comparison.
    cam.live = "different: graph\n"
    with pytest.raises(ProgramPlaybackError, match="load was not confirmed"):
        asyncio.run(
            composition.confirm_graph_is_live(cam, "program: graph\n")
        )
    # Comment-only differences are benign: camilla's serde drops them.
    cam.live = "program: graph\n"
    asyncio.run(composition.confirm_graph_is_live(cam, "# a note\nprogram: graph\n"))
    # A submitted config camilla itself refuses is a NAMED refusal, distinct
    # from a mismatch, so hardware triage can tell the two apart.
    with pytest.raises(ProgramPlaybackError, match="normalization failed"):
        asyncio.run(composition.confirm_graph_is_live(cam, "!!not-yaml\n"))
