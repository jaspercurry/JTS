# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The one rule that decides whether a live EQ edit fades the speaker.

A pipeline replace ducks the fader for ~0.85 s; a parameter write does not.
:func:`jasper.sound.live_edit.plan_live_edit` picks between them by comparing
the two graphs, so what these pin is that a value — and even a biquad's own
``type`` string, which lives under ``parameters`` — may move freely, and
anything structural (sections, devices, pipeline, the filter set, or a
filter's OUTER kind) falls back to the ducked swap.
"""

from __future__ import annotations

import pathlib

import pytest

from jasper.sound.live_edit import plan_live_edit
from tests.sound_camilla_fixtures import FakeCamilla

RUNNING = """
devices:
  samplerate: 48000
  chunksize: 1024
  volume_limit: 0.0
filters:
  sound_preamp:
    type: Gain
    description: null
    parameters: {gain: 0.0, inverted: false, mute: false}
  sound_advanced_1:
    type: Biquad
    description: null
    parameters: {type: Peaking, freq: 1000.0, q: 1.0, gain: 2.0}
  sound_advanced_2:
    type: Biquad
    description: null
    parameters: {type: Peaking, freq: 4000.0, q: 2.0, gain: -1.0}
pipeline:
  - type: Filter
    channels: [0, 1]
    names: [sound_preamp, sound_advanced_1, sound_advanced_2]
"""


@pytest.mark.parametrize(
    "swap_in, swap_out",
    [
        pytest.param("gain: 2.0", "gain: 5.5", id="gain_moved"),
        pytest.param("freq: 1000.0", "freq: 120.0", id="frequency_dragged"),
        pytest.param("q: 1.0", "q: 6.25", id="q_narrowed"),
        pytest.param("gain: 2.0", "gain: 0.0", id="gain_dragged_through_zero"),
        pytest.param("gain: -1.0", "gain: 3.0", id="gain_crossed_sign"),
    ],
)
def test_a_number_moving_is_written_as_a_parameter(swap_in, swap_out):
    """Every continuous EQ control is a number, and numbers write in place."""
    plan = plan_live_edit(RUNNING, RUNNING.replace(swap_in, swap_out))

    assert plan.method == "parameters"
    assert plan.reason == ""


def test_two_bands_moving_together_is_still_a_parameter_write():
    wanted = RUNNING.replace("gain: 2.0", "gain: 4.0").replace("gain: -1.0", "gain: -6.0")

    assert plan_live_edit(RUNNING, wanted).method == "parameters"


def test_a_biquad_retype_is_a_parameter_write():
    """A biquad's ``type`` lives under ``parameters``; only the KIND ducks."""
    wanted = RUNNING.replace("type: Peaking", "type: Highshelf", 1)

    assert plan_live_edit(RUNNING, wanted).method == "parameters"


def test_a_retype_to_a_gainless_shape_is_still_a_parameter_write():
    """A Highpass has no ``gain`` — the parameter key set may differ too."""
    wanted = RUNNING.replace(
        "parameters: {type: Peaking, freq: 1000.0, q: 1.0, gain: 2.0}",
        "parameters: {type: Highpass, freq: 80.0, q: 0.7}",
    )

    assert plan_live_edit(RUNNING, wanted).method == "parameters"


def test_the_filters_outer_kind_changing_is_a_ducked_swap():
    """``Biquad`` to ``Gain`` rebuilds the filter group; a retyped biquad does not."""
    wanted = RUNNING.replace(
        "  sound_advanced_1:\n"
        "    type: Biquad\n"
        "    description: null\n"
        "    parameters: {type: Peaking, freq: 1000.0, q: 1.0, gain: 2.0}\n",
        "  sound_advanced_1:\n"
        "    type: Gain\n"
        "    description: null\n"
        "    parameters: {gain: 0.0}\n",
    )

    plan = plan_live_edit(RUNNING, wanted)

    assert plan.method == "swap"
    assert plan.reason == "filter_kind_differs"


@pytest.mark.parametrize(
    "wanted, reason",
    [
        pytest.param(
            RUNNING.replace(
                "names: [sound_preamp, sound_advanced_1, sound_advanced_2]",
                "names: [sound_preamp, sound_advanced_1]",
            ),
            "pipeline_differs",
            id="band_dropped_from_the_chain",
        ),
        pytest.param(
            RUNNING.replace("samplerate: 48000", "samplerate: 44100"),
            "devices_differs",
            id="device_touched",
        ),
        pytest.param("[unclosed", "graph_unparseable", id="unparseable"),
        pytest.param("just a string", "graph_not_a_mapping", id="not_a_mapping"),
        pytest.param(None, "graph_unreadable", id="unreadable"),
    ],
)
def test_anything_structural_falls_back_to_the_ducked_swap(wanted, reason):
    plan = plan_live_edit(RUNNING, wanted)

    assert plan.method == "swap"
    assert plan.reason == reason


def test_a_filter_the_running_graph_does_not_have_is_a_swap():
    wanted = RUNNING.replace(
        "pipeline:",
        "  sound_advanced_3:\n"
        "    type: Biquad\n"
        "    description: null\n"
        "    parameters: {type: Peaking, freq: 60.0, q: 1.0, gain: 3.0}\n"
        "pipeline:",
    )

    plan = plan_live_edit(RUNNING, wanted)

    assert plan.method == "swap"
    assert plan.reason == "filter_set_differs"


def test_an_identical_graph_is_not_written_at_all():
    """A redraw that changed nothing must not cost a swap, or anything."""
    plan = plan_live_edit(RUNNING, RUNNING)

    assert plan.method == "unchanged"
    assert plan.duck is False


@pytest.mark.parametrize(
    "method, expected_duck",
    [("swap", True), ("parameters", False), ("unchanged", False)],
)
def test_only_a_swap_ducks(method, expected_duck):
    from jasper.sound.live_edit import LiveEditPlan

    assert LiveEditPlan(method).duck is expected_duck


# --------------------------------------------------------------------------
# The DURABLE save takes the same decision, so an A/B does not fade.
# --------------------------------------------------------------------------


class _RecordingCamilla(FakeCamilla):
    """A ``FakeCamilla`` already running ``running`` before the first write.

    ``confirm_path`` is what it reports once a write has landed. Handing back
    a path other than the candidate is how ``apply_dsp_config``'s confirm step
    is made to fail, which is the honest way to reach its rollback.
    """

    def __init__(self, current_path: str, running: str, confirm_path=None) -> None:
        super().__init__(current_path)
        self.running = running
        self._confirm_path = confirm_path

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        if self._confirm_path is not None and (self.loaded_path or self.ducks):
            return self._confirm_path
        return await super().get_config_file_path(best_effort=best_effort)


def _carrier_emitting(wanted: str):
    from jasper.sound.graph_carrier import ReemitResult

    class _Carrier:
        kind = "base_flat"
        can_host_eq = True

        def reemit(self, profile, *, out_path=None, **kwargs):
            if out_path is not None:
                pathlib.Path(out_path).write_text(wanted, encoding="utf-8")
            return ReemitResult(yaml=wanted, room_peq_count=0)

    return _Carrier()


def _running_at(tmp_path, running: str = RUNNING) -> pathlib.Path:
    config_dir = tmp_path / "configs"
    config_dir.mkdir(exist_ok=True)
    current = config_dir / "sound_current.yml"
    current.write_text(running, encoding="utf-8")
    return current


async def _save(tmp_path, monkeypatch, cam, wanted: str, **kwargs):
    """Drive the durable save the Saved and Off tabs reach through ``/apply``."""
    from jasper.sound.profile import SoundProfile
    from jasper.sound.runtime import load_profile_config

    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH", str(tmp_path / "dsp_apply_state.json"),
    )
    monkeypatch.setattr(
        "jasper.sound.graph_carrier.carrier_for_loaded_config",
        lambda *a, **k: _carrier_emitting(wanted),
    )
    return await load_profile_config(
        SoundProfile(),
        profile_path=tmp_path / "sound_profile.json",
        config_dir=(tmp_path / "configs"),
        camilla_factory=lambda: cam,
        source="sound_apply",
        persist_profile=False,
        **kwargs,
    )


@pytest.mark.parametrize(
    "wanted, quiet",
    [
        pytest.param(RUNNING, True, id="unchanged"),
        pytest.param(
            RUNNING.replace("gain: 2.0", "gain: 5.5"), True, id="a_number_moved",
        ),
        pytest.param(
            RUNNING.replace("type: Peaking", "type: Highshelf", 1),
            True,
            id="a_band_retyped",
        ),
        pytest.param(
            RUNNING.replace("channels: [0, 1]", "channels: [0]"),
            False,
            id="the_pipeline_changed",
        ),
    ],
)
async def test_a_durable_save_ducks_only_when_the_live_rule_says_swap(
    tmp_path, monkeypatch, wanted, quiet,
):
    """Save and Off are an A/B control, so they duck on the same rule as a drag.

    The Saved tab rewrites the file CamillaDSP already runs; when that graph
    updates in place there is no gain step to hide, and ducking it fades an
    audition the listener is making on purpose. Asserted on the WRITE VEHICLE,
    which is what carries the bracket: the quiet route is
    ``set_active_config_raw(duck=False)`` -- the write ADR-0211 verified takes
    CamillaDSP's in-place parameter path -- and the ducked route stays the file
    loader, whose bracket is unconditional.
    """
    current = _running_at(tmp_path)
    cam = _RecordingCamilla(str(current), RUNNING)

    await _save(tmp_path, monkeypatch, cam, wanted)

    assert cam.ducks == ([False] if quiet else [])
    assert cam.set_calls == ([] if quiet else [str(current)])


@pytest.mark.parametrize(
    "trim", ["sound_preamp", "room_headroom", "active_baseline_headroom"],
)
async def test_a_save_that_moves_a_broadband_trim_is_written_in_place(
    tmp_path, monkeypatch, trim,
):
    """A profile A/B moves the trim, and that A/B is the comparison being made.

    With match loudness on, each profile carries its own compensating trim, so
    ducking a moved one fades the audition. The step this accepts is bounded by
    the trim's own range (ADR-0219).
    """
    running = RUNNING.replace("sound_preamp", trim)
    current = _running_at(tmp_path, running)
    cam = _RecordingCamilla(str(current), running)

    await _save(
        tmp_path, monkeypatch, cam, running.replace("gain: 0.0", "gain: -12.0"),
    )

    assert cam.ducks == [False]
    assert cam.set_calls == []


async def test_a_save_onto_a_different_file_keeps_its_duck(tmp_path, monkeypatch):
    """Only a rewrite of the RUNNING file can be quiet.

    Loading a different file is a real swap however similar its contents, so
    the audition preview -- a second path -- keeps its bracket.
    """
    current = _running_at(tmp_path)
    cam = _RecordingCamilla(str(current), RUNNING)

    await _save(tmp_path, monkeypatch, cam, RUNNING, audition=True)

    assert cam.ducks == []
    assert len(cam.set_calls) == 1


async def test_a_controller_without_the_raw_api_keeps_its_duck(tmp_path, monkeypatch):
    """The statefile transport cannot be asked the question, so it is not.

    With CamillaDSP down the reconcile runs on a controller that reports no
    running graph to compare against. It must fall back to the file loader
    rather than raise.
    """

    class _FileOnlyCamilla(_RecordingCamilla):
        def __getattribute__(self, name):
            if name == "get_active_config_raw":
                raise AttributeError(name)
            return super().__getattribute__(name)

    current = _running_at(tmp_path)
    cam = _FileOnlyCamilla(str(current), RUNNING)

    await _save(tmp_path, monkeypatch, cam, RUNNING)

    assert cam.ducks == []
    assert cam.set_calls == [str(current)]


async def test_a_rollback_never_reuses_the_quiet_write(tmp_path, monkeypatch):
    """``apply_dsp_config`` reuses ``load_config`` to roll back; that loads a file.

    An in-place rollback puts the candidate's pre-``prepare`` bytes back on
    disk and then reloads that path (#3511). Handing it the quiet vehicle a
    second time would re-send the graph that just failed and undo exactly the
    restore that ran. Driven through a real confirm failure rather than by
    calling the loader twice by hand, so the pin survives a change to how
    rollback is reached.
    """
    from jasper.dsp_apply import DspApplyError

    current = _running_at(tmp_path)
    cam = _RecordingCamilla(
        str(current), RUNNING, confirm_path=str(tmp_path / "elsewhere.yml"),
    )

    with pytest.raises(DspApplyError):
        await _save(
            tmp_path, monkeypatch, cam, RUNNING.replace("gain: 2.0", "gain: 9.0"),
        )

    # One quiet candidate load, then the rollback down the file loader.
    assert cam.ducks == [False]
    assert cam.set_calls == [str(current)]
