from __future__ import annotations

import sys
import types

import pytest

# camilladsp is a Pi-side runtime dep not installed locally; stub it so
# `import jasper.camilla` works in unit tests. Ducker only touches
# CamillaController via the public interface (set_volume_db /
# adjust_volume_db), and we pass a fake camilla into it anyway.
sys.modules.setdefault("camilladsp", types.ModuleType("camilladsp"))
sys.modules["camilladsp"].CamillaClient = object  # type: ignore[attr-defined]

from jasper.camilla import Ducker  # noqa: E402


class _FakeCamilla:
    def __init__(self, db: float = 0.0) -> None:
        self._db = db
        self.set_calls: list[float] = []

    async def get_volume_db(self) -> float:
        return self._db

    async def set_volume_db(self, db: float) -> None:
        self._db = db
        self.set_calls.append(db)

    async def adjust_volume_db(self, delta_db: float) -> float:
        target = self._db + delta_db
        await self.set_volume_db(target)
        return target


def _ducker(camilla: _FakeCamilla, *, duck_db: float = -25.0,
            target: float = 0.0) -> Ducker:
    async def provider() -> float:
        return target
    return Ducker(camilla, duck_db, target_db_provider=provider)


def _ducker_with_dynamic_target(
    camilla: _FakeCamilla, *, duck_db: float = -25.0,
    target_holder: list[float],
) -> Ducker:
    async def provider() -> float:
        return target_holder[0]
    return Ducker(camilla, duck_db, target_db_provider=provider)


@pytest.mark.asyncio
async def test_duck_lowers_camilla_by_duck_db():
    cam = _FakeCamilla(db=-15.0)
    d = _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    assert cam._db == -40.0
    assert cam.set_calls == [-40.0]


@pytest.mark.asyncio
async def test_restore_writes_target_db_absolutely():
    cam = _FakeCamilla(db=-15.0)
    d = _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    await d.restore()
    assert cam._db == -15.0
    # Two writes: duck (additive) then restore (absolute set).
    assert cam.set_calls == [-40.0, -15.0]


@pytest.mark.asyncio
async def test_restore_uses_current_target_not_pre_duck_value():
    """Regression for the dial-during-duck overshoot. If
    `listening_level` changes mid-session, restore lands at the new
    target — not at `pre_duck + duck_delta`. Reproduces the +25 dB
    bug from 2026-05-08: pre_duck=0, duck=-25 → camilla=-25,
    listening_level moves so target becomes -27. Old additive restore
    would have written 0; new absolute restore writes -27."""
    cam = _FakeCamilla(db=0.0)
    target = [0.0]
    d = _ducker_with_dynamic_target(cam, duck_db=-25.0, target_holder=target)
    await d.duck()
    assert cam._db == -25.0
    target[0] = -27.0
    await d.restore()
    assert cam._db == -27.0


@pytest.mark.asyncio
async def test_restore_after_external_camilla_write_still_uses_target():
    """Even if some other writer touched camilla during the duck
    (the bug case where _set_camilla wasn't gated), restore uses the
    target_db_provider's value — not whatever camilla currently shows."""
    cam = _FakeCamilla(db=-15.0)
    d = _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    # Simulate an interloping write (e.g. dial pre-gate) during duck.
    await cam.set_volume_db(0.0)
    await d.restore()
    assert cam._db == -15.0


@pytest.mark.asyncio
async def test_double_duck_is_no_op():
    cam = _FakeCamilla(db=-15.0)
    d = _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.duck()
    await d.duck()
    assert cam.set_calls == [-40.0]


@pytest.mark.asyncio
async def test_restore_without_duck_is_no_op():
    cam = _FakeCamilla(db=-15.0)
    d = _ducker(cam, duck_db=-25.0, target=-15.0)
    await d.restore()
    assert cam.set_calls == []
