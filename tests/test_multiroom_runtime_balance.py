import pytest

from jasper.multiroom.config import DEFAULT_CODEC, GroupingConfig
from jasper.multiroom.runtime_balance import (
    apply_local_trim,
    camilla_patch_for_trim,
    coerce_trim_db,
)


def _cfg(role: str = "follower", trim_db: float = -2.5) -> GroupingConfig:
    return GroupingConfig(
        enabled=True,
        role=role,
        channel="right" if role == "follower" else "left",
        bond_id="bond",
        leader_addr="jts.local" if role == "follower" else "",
        buffer_ms=400,
        codec=DEFAULT_CODEC,
        error=None,
        trim_db=trim_db,
    )


def test_camilla_patch_for_trim_updates_only_pair_balance_gain() -> None:
    assert camilla_patch_for_trim(-2.54) == {
        "filters": {
            "pair_balance_trim": {
                "parameters": {
                    "gain": -2.5,
                    "inverted": False,
                    "mute": False,
                }
            }
        }
    }


@pytest.mark.parametrize("value", [0.1, -24.1, float("nan")])
def test_coerce_trim_rejects_boosts_floor_and_nonfinite(value: float) -> None:
    with pytest.raises(ValueError):
        coerce_trim_db(value)


@pytest.mark.asyncio
async def test_apply_local_trim_active_endpoint_patches_camilla() -> None:
    calls = []

    class FakeCamilla:
        async def patch_config(self, patch, *, best_effort=False):
            calls.append((patch, best_effort))
            return True

    result = await apply_local_trim(
        -3.0,
        cfg=_cfg("leader", trim_db=-3.0),
        active_box_reader=lambda: True,
        camilla_factory=lambda _cfg: FakeCamilla(),
    )

    assert result.applied is True
    assert result.mode == "active_camilla"
    assert calls == [(camilla_patch_for_trim(-3.0), True)]


@pytest.mark.asyncio
async def test_apply_local_trim_passive_endpoint_calls_outputd() -> None:
    commands = []

    async def fake_outputd(command: str):
        commands.append(command)
        return {"ok": True, "trim_db": -4.0}

    result = await apply_local_trim(
        -4.0,
        cfg=_cfg("follower", trim_db=-4.0),
        active_box_reader=lambda: False,
        outputd_command=fake_outputd,
    )

    assert result.applied is True
    assert result.mode == "outputd"
    assert commands == ["SET_DAC_CONTENT_TRIM_DB -4.0"]
