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
        self.config = self
        self.active_raw_values: list[str] = []
        self.queries: list[tuple[str, object]] = []

    def set_active_raw(self, value: str) -> None:
        self.active_raw_values.append(value)

    def query(self, command: str, *, arg=None):
        self.queries.append((command, arg))
        return None


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


@pytest.mark.asyncio
async def test_set_active_config_raw_uploads_without_file_path_reload():
    fake = _FakeClient()
    cam = _controller(fake)

    assert await cam.set_active_config_raw("---\nfilters: {}\n")

    assert fake.active_raw_values == ["---\nfilters: {}\n"]
    assert fake.queries == []


@pytest.mark.asyncio
async def test_set_active_config_raw_rejects_empty_config():
    fake = _FakeClient()
    cam = _controller(fake)

    assert await cam.set_active_config_raw("", best_effort=True) is False

    assert fake.active_raw_values == []


@pytest.mark.asyncio
async def test_patch_config_uses_camilla_query_escape_hatch():
    fake = _FakeClient()
    cam = _controller(fake)

    patch = {"filters": {"sound_simple_bass": {"parameters": {"gain": 1.5}}}}

    assert await cam.patch_config(patch)

    assert fake.queries == [("PatchConfig", patch)]
