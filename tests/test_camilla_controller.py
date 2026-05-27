from __future__ import annotations

import pytest

from jasper.camilla import CamillaController


class _FakeVolume:
    def __init__(self) -> None:
        self.values: list[float] = []

    def set_main_volume(self, value: float) -> None:
        self.values.append(float(value))


class _FakeClient:
    def __init__(self) -> None:
        self.volume = _FakeVolume()


def _controller(fake: _FakeClient) -> CamillaController:
    cam = CamillaController("127.0.0.1", 1234)

    async def call(fn):
        return fn(fake)

    cam._call = call  # type: ignore[method-assign]
    return cam


@pytest.mark.asyncio
async def test_set_volume_db_clamps_positive_gain_to_zero():
    fake = _FakeClient()
    cam = _controller(fake)

    assert await cam.set_volume_db(6.0)

    assert fake.volume.values == [0.0]


@pytest.mark.asyncio
async def test_set_volume_db_rejects_non_finite_best_effort():
    fake = _FakeClient()
    cam = _controller(fake)

    assert await cam.set_volume_db(float("nan"), best_effort=True) is False

    assert fake.volume.values == []


@pytest.mark.asyncio
async def test_set_volume_db_rejects_non_finite_strict():
    fake = _FakeClient()
    cam = _controller(fake)

    with pytest.raises(ValueError):
        await cam.set_volume_db(float("inf"))

    assert fake.volume.values == []
