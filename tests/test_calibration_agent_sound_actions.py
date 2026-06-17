from __future__ import annotations

from pathlib import Path

from jasper.calibration_agent import actions, response, sound_actions, tools
from jasper.camilla_config_contract import PeqFilter
from jasper.sound.camilla_yaml import emit_sound_config
from jasper.sound.profile import SimpleEq, SoundProfile, load_profile, save_profile

from .correction_bundle_fixtures import write_golden_correction_bundle


class FakeCamilla:
    def __init__(self, current_path: str) -> None:
        self.current_path = current_path
        self.loaded_path: str | None = None

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        return self.loaded_path or self.current_path

    async def set_config_file_path(self, path: str, *, best_effort: bool = False) -> bool:
        self.loaded_path = path
        return True


def _context(tmp_path: Path) -> dict:
    bundle = tools.load_measurement_bundle(
        bundle_dir=write_golden_correction_bundle(tmp_path),
    )
    return tools.build_intake(bundle)["advisor_context"]


def _advisor_response() -> dict:
    return {
        "artifact_schema_version": response.RESPONSE_SCHEMA_VERSION,
        "kind": "jts_advisor_response",
        "action_plan": [{
            "type": response.ACTION_AUDITION,
            "rationale": "Audition a small bass preference lift.",
            "profile": {
                "enabled": True,
                "curve_id": "harman",
                "simple_eq": {
                    "sub_bass_db": 0.5,
                    "bass_db": 1.0,
                    "mid_db": 0.0,
                    "presence_db": 0.0,
                    "treble_db": -0.5,
                },
                "parametric_bands": [],
            },
        }],
    }


def test_sound_audition_executor_loads_ephemeral_sound_config(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.setenv(
        "JASPER_DSP_APPLY_STATE_PATH",
        str(tmp_path / "dsp_apply_state.json"),
    )
    monkeypatch.setenv(
        "JASPER_SOUND_SETTINGS_PATH",
        str(tmp_path / "sound_settings.json"),
    )
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(
        emit_sound_config(
            SoundProfile(enabled=False),
            room_peqs=[PeqFilter(freq=80.0, q=4.0, gain=-3.0)],
        )
    )
    profile_path = tmp_path / "sound_profile.json"
    save_profile(
        SoundProfile(curve_id="flat", simple_eq=SimpleEq(mid_db=0.0)),
        profile_path,
    )
    fake = FakeCamilla(str(current))
    validation = response.validate_advisor_response(
        _advisor_response(),
        advisor_context=_context(tmp_path),
    )

    run = actions.run_validated_action_plan(
        validation,
        audition_executor=sound_actions.build_sound_audition_executor(
            profile_path=profile_path,
            library_path=tmp_path / "sound_profiles.json",
            config_dir=config_dir,
            camilla_factory=lambda: fake,
        ),
    )

    assert run["accepted"] is True
    assert run["status"] == "complete"
    assert run["side_effects"] == ["ephemeral_audio_state"]
    result = run["action_results"][0]["executor_result"]
    assert result["audition_mode"] == "advisor"
    assert result["active_config_name"] == "sound_audition.yml"
    assert result["preserved_room_peqs"] == 1
    assert result["profile"]["curve_id"] == "harman"
    assert Path(fake.loaded_path).name == "sound_audition.yml"
    assert load_profile(profile_path).curve_id == "flat"
