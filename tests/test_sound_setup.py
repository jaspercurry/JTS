from __future__ import annotations

from pathlib import Path

from jasper.correction.camilla_yaml import emit_correction_config
from jasper.correction.peq import PEQ
from jasper.sound.profile import SimpleEq, SoundProfile, load_profile
from jasper.web import sound_setup


class FakeCamilla:
    def __init__(self, current_path: str, *, fail_set: bool = False) -> None:
        self.current_path = current_path
        self.loaded_path: str | None = None
        self.set_calls: list[str] = []
        self.fail_set = fail_set

    async def get_config_file_path(self, *, best_effort: bool = False) -> str:
        return self.current_path

    async def set_config_file_path(self, path: str, *, best_effort: bool = False) -> bool:
        self.set_calls.append(path)
        self.loaded_path = path
        if self.fail_set and not best_effort:
            raise RuntimeError("reload failed")
        return True


def test_index_html_exposes_simple_eq_language_only():
    html = sound_setup._index_html().decode()

    assert "Bass" in html
    assert "Mid" in html
    assert "Treble" in html
    assert "Natural" not in html
    assert "Warm" not in html
    assert "Clear" not in html
    assert "aria-label=\"Turn preference EQ on or off\"" in html
    assert "curve-description" in html


def test_state_payload_contains_stock_curves_and_preview():
    payload = sound_setup._state_payload(SoundProfile(curve_id="harman"))

    assert [curve["id"] for curve in payload["curves"]] == ["flat", "harman", "bk"]
    assert payload["profile"]["curve_id"] == "harman"
    assert payload["preview"]
    assert payload["headroom_db"] > 0


async def test_apply_profile_preserves_active_room_peqs(tmp_path: Path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(emit_correction_config([PEQ(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(current))
    profile_path = tmp_path / "sound_profile.json"

    payload = await sound_setup._apply_profile(
        SoundProfile(curve_id="bk", simple_eq=SimpleEq(treble_db=1.5)),
        profile_path=profile_path,
        config_dir=config_dir,
        camilla_factory=lambda: fake,
    )

    assert fake.loaded_path is not None
    generated = Path(fake.loaded_path).read_text()
    assert Path(fake.loaded_path).name == "sound_current.yml"
    assert "room_peq_1:" in generated
    assert "sound_curve_bk_bass:" in generated
    assert payload["preserved_room_peqs"] == 1
    assert load_profile(profile_path).curve_id == "bk"


async def test_apply_profile_rejects_unknown_active_config(tmp_path: Path):
    current = tmp_path / "custom.yml"
    current.write_text("# handmade\n")
    fake = FakeCamilla(str(current))

    try:
        await sound_setup._apply_profile(
            SoundProfile(simple_eq=SimpleEq(bass_db=1.0)),
            profile_path=tmp_path / "sound_profile.json",
            config_dir=tmp_path / "configs",
            camilla_factory=lambda: fake,
        )
    except RuntimeError as e:
        assert "custom config" in str(e)
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected unknown config rejection")


async def test_apply_profile_rolls_back_when_reload_fails(tmp_path: Path):
    config_dir = tmp_path / "configs"
    config_dir.mkdir()
    current = config_dir / "correction_abc_123.yml"
    current.write_text(emit_correction_config([PEQ(freq=80.0, q=4.0, gain=-3.0)]))
    fake = FakeCamilla(str(current), fail_set=True)

    try:
        await sound_setup._apply_profile(
            SoundProfile(simple_eq=SimpleEq(bass_db=1.0)),
            profile_path=tmp_path / "sound_profile.json",
            config_dir=config_dir,
            camilla_factory=lambda: fake,
        )
    except RuntimeError as e:
        assert "reload failed" in str(e)
    else:  # pragma: no cover - defensive assertion style
        raise AssertionError("expected reload failure")

    assert fake.set_calls[-1] == str(current)
    assert not (tmp_path / "sound_profile.json").exists()
