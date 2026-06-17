"""Stage 5 — per-driver floor-unmute gain ramp.

Three layers, all hardware-free:

  * the ``audible_gain_db`` threading through the commissioning emitter/prepare
    (the ramp variable) — and that it does NOT disturb the slice-2b-ii evidence
    gates, which check ``mute: off`` at the floor, not the gain;
  * ``build_stage5_ramp_gate`` — the pure safety gate (gain envelope + per-step
    bound, live mask + tweeter high-pass, 0 dB ceiling, the audible driver's
    limiter, woofer-before-tweeter, prior-step ACK);
  * ``ramp_audible_step`` / ``record_ramp_operator_ack`` / ``abort_ramp`` — the
    orchestration that ties the guarded load to the safe_playback floor
    tri-state, fail-closed.
"""

from __future__ import annotations

import asyncio

import pytest
import yaml

from jasper.active_speaker import (
    ActiveSpeakerPreset,
    abort_ramp,
    audible_outputs_for_role,
    build_stage5_ramp_gate,
    driver_commission_audible_evidence,
    emit_active_speaker_commissioning_config,
    load_commission_load_state,
    load_ramp_state,
    next_ramp_gain_db,
    prepare_driver_commissioning_config,
    ramp_audible_step,
    record_ramp_operator_ack,
)
from jasper.active_speaker.calibration_level import (
    AUDIBLE_RAMP_STEP_DB,
    MAX_TEST_LEVEL_DBFS,
    MIN_TEST_LEVEL_DBFS,
)
from jasper.active_speaker.camilla_yaml import STARTUP_MUTE_GAIN_DB
from jasper.active_speaker import ActiveSpeakerConfigError

from tests.test_active_speaker_commission_load import _load
from tests.test_active_speaker_startup_load import _topology, _valid_config
from tests.test_active_speaker_profile import _two_way_preset


def _two_way() -> ActiveSpeakerPreset:
    return ActiveSpeakerPreset.from_mapping(_two_way_preset())


def _emit(preset: ActiveSpeakerPreset, audible: set[int], gain: float) -> str:
    return emit_active_speaker_commissioning_config(
        preset,
        playback_device="hw:CARD=DAC8x,DEV=0",
        audible_outputs=audible,
        audible_gain_db=gain,
    )


# --- audible_gain_db threading -----------------------------------------------


def test_audible_output_carries_ramp_gain_muted_stay_at_floor():
    preset = _two_way()
    woofer = set(audible_outputs_for_role(preset, "woofer"))  # {0}
    config = yaml.safe_load(_emit(preset, woofer, gain=-80.0))
    filters = config["filters"]
    # The audible woofer output (0) carries the ramp gain, un-muted.
    assert filters["as_out0_commission_mute"]["parameters"]["gain"] == -80.0
    assert filters["as_out0_commission_mute"]["parameters"]["mute"] is False
    # The muted tweeter output (1) stays at the -120 mute floor regardless.
    assert filters["as_out1_commission_mute"]["parameters"]["gain"] == STARTUP_MUTE_GAIN_DB
    assert filters["as_out1_commission_mute"]["parameters"]["mute"] is True


def test_default_audible_gain_is_the_silent_floor():
    preset = _two_way()
    woofer = set(audible_outputs_for_role(preset, "woofer"))
    config = yaml.safe_load(_emit(preset, woofer, gain=STARTUP_MUTE_GAIN_DB))
    # Default (un-ramped) arms at the silent floor: un-muted but -120 dB.
    assert config["filters"]["as_out0_commission_mute"]["parameters"]["gain"] == -120.0
    assert config["filters"]["as_out0_commission_mute"]["parameters"]["mute"] is False


def test_evidence_gates_unaffected_by_audible_gain():
    # The slice-2b-ii gates check mute:off for the audible output, not the gain,
    # so a louder audible gain must NOT change their verdict.
    preset = _two_way()
    woofer = set(audible_outputs_for_role(preset, "woofer"))
    for gain in (STARTUP_MUTE_GAIN_DB, -80.0, -45.0):
        yaml_text = _emit(preset, woofer, gain=gain)
        off = driver_commission_audible_evidence(
            yaml_text, preset=preset, audible_outputs=woofer
        )
        assert off["passed"] is True


def test_emitter_rejects_out_of_bound_audible_gain():
    preset = _two_way()
    woofer = set(audible_outputs_for_role(preset, "woofer"))
    with pytest.raises(ActiveSpeakerConfigError):
        _emit(preset, woofer, gain=6.0)  # above the 0 dB ceiling
    with pytest.raises(ActiveSpeakerConfigError):
        _emit(preset, woofer, gain=-200.0)  # below the mute floor


def test_prepare_records_audible_gain_and_way_count():
    prepared = prepare_driver_commissioning_config(
        _topology(),
        speaker_group_id="mono",
        role="woofer",
        audible_gain_db=-80.0,
        run_config_check=False,
        config_path="/tmp/jts-test-commission.yml",
    )
    assert prepared["status"] == "prepared"
    assert prepared["target"]["audible_gain_db"] == -80.0
    assert prepared["way_count"] == 2


# --- next_ramp_gain_db -------------------------------------------------------


def test_next_ramp_gain_progression():
    # Silent/armed floor -> first audible step is exactly the audible floor.
    assert next_ramp_gain_db(STARTUP_MUTE_GAIN_DB) == MIN_TEST_LEVEL_DBFS
    assert next_ramp_gain_db(-200.0) == MIN_TEST_LEVEL_DBFS
    # Audible -> one bounded step, clamped to the ceiling.
    assert next_ramp_gain_db(MIN_TEST_LEVEL_DBFS) == MIN_TEST_LEVEL_DBFS + AUDIBLE_RAMP_STEP_DB
    assert next_ramp_gain_db(MAX_TEST_LEVEL_DBFS) == MAX_TEST_LEVEL_DBFS


# --- build_stage5_ramp_gate (pure) -------------------------------------------


def _gate(
    preset,
    *,
    role,
    running_gain,
    current_gain_db,
    next_gain_db,
    confirmed_roles=frozenset(),
    prior_step_cleared=False,
    present_roles=frozenset({"woofer", "tweeter"}),
    corrupt_hp=False,
    bad_ceiling=False,
):
    audible = set(audible_outputs_for_role(preset, role))
    ev = driver_commission_audible_evidence(
        _emit(preset, audible, gain=next_gain_db), preset=preset, audible_outputs=audible
    )
    running = yaml.safe_load(_emit(preset, audible, gain=running_gain))
    if corrupt_hp:
        running["filters"]["as_tweeter_protective_hp"]["parameters"]["freq"] = 200.0
    if bad_ceiling:
        running["devices"]["volume_limit"] = 6.0
    return build_stage5_ramp_gate(
        running_config_raw=yaml.safe_dump(running),
        role=role,
        present_roles=present_roles,
        audible_outputs=ev["audible_outputs"],
        muted_outputs=ev["muted_outputs"],
        tweeter_outputs=ev["tweeter_outputs"],
        protective_hp_hz=ev["protective_highpass_hz"],
        current_gain_db=current_gain_db,
        next_gain_db=next_gain_db,
        confirmed_roles=confirmed_roles,
        prior_step_cleared=prior_step_cleared,
    )


def test_gate_passes_woofer_floor_step():
    g = _gate(
        _two_way(),
        role="woofer",
        running_gain=STARTUP_MUTE_GAIN_DB,
        current_gain_db=STARTUP_MUTE_GAIN_DB,
        next_gain_db=MIN_TEST_LEVEL_DBFS,
    )
    assert g["passed"] is True
    assert g["checks"]["role_order_woofer_first"] is True
    assert g["checks"]["prior_step_acknowledged"] is True  # the floor step itself


def test_gate_rejects_gain_above_ceiling_envelope():
    g = _gate(
        _two_way(),
        role="woofer",
        running_gain=MAX_TEST_LEVEL_DBFS,
        current_gain_db=MAX_TEST_LEVEL_DBFS,
        next_gain_db=MAX_TEST_LEVEL_DBFS + 10,
    )
    assert g["checks"]["gain_within_envelope"] is False
    assert g["passed"] is False


def test_gate_rejects_oversized_step():
    g = _gate(
        _two_way(),
        role="woofer",
        running_gain=MIN_TEST_LEVEL_DBFS,
        current_gain_db=MIN_TEST_LEVEL_DBFS,
        next_gain_db=MIN_TEST_LEVEL_DBFS + AUDIBLE_RAMP_STEP_DB + 5,
        prior_step_cleared=True,
    )
    assert g["checks"]["gain_step_bounded"] is False
    assert g["passed"] is False


def test_gate_first_audible_step_must_be_exactly_the_floor():
    # From the silent floor, the only allowed next gain is the audible floor.
    g = _gate(
        _two_way(),
        role="woofer",
        running_gain=STARTUP_MUTE_GAIN_DB,
        current_gain_db=STARTUP_MUTE_GAIN_DB,
        next_gain_db=MIN_TEST_LEVEL_DBFS + AUDIBLE_RAMP_STEP_DB,
    )
    assert g["checks"]["gain_step_bounded"] is False
    assert g["passed"] is False


def test_gate_blocks_tweeter_until_woofer_confirmed():
    blocked = _gate(
        _two_way(),
        role="tweeter",
        running_gain=STARTUP_MUTE_GAIN_DB,
        current_gain_db=STARTUP_MUTE_GAIN_DB,
        next_gain_db=MIN_TEST_LEVEL_DBFS,
        confirmed_roles=frozenset(),
    )
    assert blocked["checks"]["role_order_woofer_first"] is False
    assert blocked["passed"] is False
    allowed = _gate(
        _two_way(),
        role="tweeter",
        running_gain=STARTUP_MUTE_GAIN_DB,
        current_gain_db=STARTUP_MUTE_GAIN_DB,
        next_gain_db=MIN_TEST_LEVEL_DBFS,
        confirmed_roles=frozenset({"woofer"}),
    )
    assert allowed["checks"]["role_order_woofer_first"] is True
    assert allowed["passed"] is True


def test_gate_requires_live_high_pass_before_tweeter():
    g = _gate(
        _two_way(),
        role="tweeter",
        running_gain=STARTUP_MUTE_GAIN_DB,
        current_gain_db=STARTUP_MUTE_GAIN_DB,
        next_gain_db=MIN_TEST_LEVEL_DBFS,
        confirmed_roles=frozenset({"woofer"}),
        corrupt_hp=True,  # the RUNNING graph's tweeter HP is wrong
    )
    assert g["checks"]["live_mask_and_highpass"] is False
    assert g["passed"] is False


def test_gate_requires_volume_ceiling():
    g = _gate(
        _two_way(),
        role="woofer",
        running_gain=STARTUP_MUTE_GAIN_DB,
        current_gain_db=STARTUP_MUTE_GAIN_DB,
        next_gain_db=MIN_TEST_LEVEL_DBFS,
        bad_ceiling=True,
    )
    assert g["checks"]["volume_ceiling_0db"] is False
    assert g["passed"] is False


def test_gate_louder_step_requires_prior_floor_confirmation():
    base = dict(
        role="woofer",
        running_gain=MIN_TEST_LEVEL_DBFS,
        current_gain_db=MIN_TEST_LEVEL_DBFS,
        next_gain_db=MIN_TEST_LEVEL_DBFS + AUDIBLE_RAMP_STEP_DB,
    )
    unconfirmed = _gate(_two_way(), prior_step_cleared=False, **base)
    assert unconfirmed["checks"]["prior_step_acknowledged"] is False
    assert unconfirmed["passed"] is False
    confirmed = _gate(_two_way(), prior_step_cleared=True, **base)
    assert confirmed["checks"]["prior_step_acknowledged"] is True
    assert confirmed["passed"] is True


# --- orchestration -----------------------------------------------------------

_READY_ENV = {
    "status": "ready",
    "load_gate": "ready",
    "ok_to_load_active_config": True,
    "camilla_config": {},
    "safe_playback": {},
    "issues": [],
}


def _ramp_step(tmp_path, monkeypatch, *, role, confirm_first=None):
    """Arm ``role`` at the silent floor, then take one audible ramp step."""
    # Arm at the silent floor via the guarded commission load (reuses the
    # commission-load test harness for the full staged/path-safety/statefile setup).
    _result, cam, staged, staged_path, statefile, state_path = _load(
        tmp_path, monkeypatch, role=role
    )
    assert load_commission_load_state(state_path=state_path)["status"] == "loaded"

    common = dict(
        speaker_group_id="mono",
        load_config=cam.apply_running_config,
        read_running_config=cam.read_running_config,
        get_current_config_path=cam.get_config_file_path,
        path_safety_evidence_path=tmp_path / "path_safety.json",
        staged_config=staged,
        statefile_path=statefile,
        config_path=tmp_path / "commission.yml",
        commission_load_state_path=state_path,
        ramp_state_path_override=tmp_path / "ramp.json",
        safe_playback_state_path=tmp_path / "safe.json",
        environment_report=_READY_ENV,
        validate=_valid_config,
    )
    if confirm_first:
        # Pre-seed the ramp's ordering memory (e.g. woofer already confirmed).
        from jasper.active_speaker.commission_ramp import _record_ramp_state, _ramp_base_state, ramp_state_path
        _record_ramp_state(
            {
                **_ramp_base_state(ramp_state_path(tmp_path / "ramp.json")),
                "confirmed_roles": list(confirm_first),
            },
            state_path=tmp_path / "ramp.json",
        )

    step = asyncio.run(ramp_audible_step(_topology(), role=role, **common))
    return step, cam, staged_path, state_path, common


def test_ramp_step_woofer_floor_happy_path(monkeypatch, tmp_path):
    step, cam, staged_path, state_path, _ = _ramp_step(
        tmp_path, monkeypatch, role="woofer"
    )
    assert step["status"] == "stepped"
    assert step["next_gain_db"] == MIN_TEST_LEVEL_DBFS
    # The driver is now audible at the floor; the per-driver tri-state is pending
    # an operator ACK (preserved, not a bool).
    assert step["safe_playback"]["floor_status"] == "floor_pending_operator"
    assert step["ramp"]["pending"]["role"] == "woofer"
    assert step["ramp"]["pending"]["gain_db"] == MIN_TEST_LEVEL_DBFS
    # The guarded load reached the audible floor on the running graph.
    assert load_commission_load_state(state_path=state_path)["target"][
        "audible_gain_db"
    ] == MIN_TEST_LEVEL_DBFS


def test_ramp_step_blocked_when_not_loaded(tmp_path, monkeypatch):
    # No commission load active -> the ramp refuses and loads nothing.
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE",
        str(tmp_path / "commission_load.json"),
    )

    class _Cam:
        loaded_paths: list = []

        async def read_running_config(self):
            return None

        async def get_config_file_path(self, **k):
            return None

        async def apply_running_config(self, path):
            self.loaded_paths.append(path)
            return True

    cam = _Cam()
    step = asyncio.run(
        ramp_audible_step(
            _topology(),
            speaker_group_id="mono",
            role="woofer",
            load_config=cam.apply_running_config,
            read_running_config=cam.read_running_config,
            get_current_config_path=cam.get_config_file_path,
            commission_load_state_path=tmp_path / "commission_load.json",
            ramp_state_path_override=tmp_path / "ramp.json",
            safe_playback_state_path=tmp_path / "safe.json",
        )
    )
    assert step["status"] == "blocked"
    assert {i["code"] for i in step["issues"]} == {"commission_not_loaded"}
    assert cam.loaded_paths == []


def test_ramp_tweeter_blocked_until_woofer_confirmed_loads_nothing(
    monkeypatch, tmp_path
):
    # Arming + stepping the tweeter before the woofer is confirmed must fail the
    # ordering gate and emit no new audible level.
    step, cam, staged_path, state_path, _ = _ramp_step(
        tmp_path, monkeypatch, role="tweeter"
    )
    assert step["status"] == "gate_blocked"
    assert step["gate"]["checks"]["role_order_woofer_first"] is False
    # Only the arm loaded; the ramp loaded nothing more.
    assert cam.loaded_paths == [str(tmp_path / "commission.yml")]


def test_ramp_step_then_pending_blocks_a_second_step(monkeypatch, tmp_path):
    step, cam, staged_path, state_path, common = _ramp_step(
        tmp_path, monkeypatch, role="woofer"
    )
    assert step["status"] == "stepped"
    loads_after_first = len(cam.loaded_paths)
    # A second step before acknowledging the first must be refused.
    again = asyncio.run(ramp_audible_step(_topology(), role="woofer", **common))
    assert again["status"] == "blocked"
    assert {i["code"] for i in again["issues"]} == {"ramp_step_awaiting_ack"}
    assert len(cam.loaded_paths) == loads_after_first  # nothing new loaded


def test_operator_ack_confirms_floor_and_records_role(monkeypatch, tmp_path):
    step, cam, staged_path, state_path, _ = _ramp_step(
        tmp_path, monkeypatch, role="woofer"
    )
    assert step["status"] == "stepped"
    ack = asyncio.run(
        record_ramp_operator_ack(
            outcome="heard_correct_driver",
            ramp_state_path_override=tmp_path / "ramp.json",
            safe_playback_state_path=tmp_path / "safe.json",
            commission_load_state_path=state_path,
        )
    )
    assert ack["status"] == "confirmed"
    assert ack["safe_playback"]["floor_status"] == "floor_confirmed"
    ramp = load_ramp_state(state_path=tmp_path / "ramp.json")
    assert ramp["confirmed_roles"] == ["woofer"]
    assert ramp["pending"] is None


def test_silent_floor_allows_a_louder_retry(monkeypatch, tmp_path):
    step, cam, staged_path, state_path, common = _ramp_step(
        tmp_path, monkeypatch, role="woofer"
    )
    assert step["status"] == "stepped"
    assert step["next_gain_db"] == MIN_TEST_LEVEL_DBFS
    # The -80 dBFS floor is genuinely inaudible -> "silent" -> retry louder.
    ack = asyncio.run(
        record_ramp_operator_ack(
            outcome="silent",
            ramp_state_path_override=tmp_path / "ramp.json",
            safe_playback_state_path=tmp_path / "safe.json",
            commission_load_state_path=state_path,
        )
    )
    assert ack["status"] == "retry"
    # The next step is permitted (silent-retry) and rises by one bounded step.
    again = asyncio.run(ramp_audible_step(_topology(), role="woofer", **common))
    assert again["status"] == "stepped"
    assert again["next_gain_db"] == MIN_TEST_LEVEL_DBFS + AUDIBLE_RAMP_STEP_DB


def test_operator_ack_too_loud_aborts_to_staged(monkeypatch, tmp_path):
    step, cam, staged_path, state_path, _ = _ramp_step(
        tmp_path, monkeypatch, role="woofer"
    )
    assert step["status"] == "stepped"
    ack = asyncio.run(
        record_ramp_operator_ack(
            outcome="too_loud",
            load_config=cam.apply_running_config,
            ramp_state_path_override=tmp_path / "ramp.json",
            safe_playback_state_path=tmp_path / "safe.json",
            commission_load_state_path=state_path,
            validate=_valid_config,
        )
    )
    assert ack["status"] == "aborted"
    # The running graph is rolled back to the all-muted staged config.
    assert cam.loaded_paths[-1] == staged_path
    assert load_ramp_state(state_path=tmp_path / "ramp.json")["pending"] is None


def test_abort_ramp_rolls_back_and_resets(monkeypatch, tmp_path):
    step, cam, staged_path, state_path, _ = _ramp_step(
        tmp_path, monkeypatch, role="woofer"
    )
    assert step["status"] == "stepped"
    out = asyncio.run(
        abort_ramp(
            load_config=cam.apply_running_config,
            commission_load_state_path=state_path,
            ramp_state_path_override=tmp_path / "ramp.json",
            validate=_valid_config,
        )
    )
    assert out["status"] == "aborted"
    assert cam.loaded_paths[-1] == staged_path
    ramp = load_ramp_state(state_path=tmp_path / "ramp.json")
    assert ramp["pending"] is None
    assert ramp["confirmed_roles"] == []


def test_ramp_running_graph_carries_the_new_audible_gain(monkeypatch, tmp_path):
    step, cam, staged_path, state_path, _ = _ramp_step(
        tmp_path, monkeypatch, role="woofer"
    )
    assert step["status"] == "stepped"
    # The graph CamillaDSP is running now has the woofer un-muted at the floor.
    running = yaml.safe_load(cam.running_raw)
    woofer_mute = running["filters"]["as_out0_commission_mute"]["parameters"]
    assert woofer_mute["mute"] is False
    assert woofer_mute["gain"] == MIN_TEST_LEVEL_DBFS
