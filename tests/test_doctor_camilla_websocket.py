# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jasper.camilla import CamillaUnavailable
from jasper.cli.doctor import audio


@pytest.mark.asyncio
async def test_doctor_camilla_probe_uses_bounded_controller(monkeypatch):
    constructed: list[tuple[str, int]] = []

    class Controller:
        def __init__(self, host: str, port: int) -> None:
            constructed.append((host, port))

        async def get_volume_db(self):
            return -12.5

        async def get_clipped_samples(self):
            return 0

        async def close(self):
            pass

    monkeypatch.setattr(audio, "CamillaController", Controller)
    cfg = SimpleNamespace(camilla_host="127.0.0.1", camilla_port=1234)

    result = await audio.check_camilla_websocket(cfg)

    assert result.status == "ok"
    assert "volume=-12.5 dB clipped_samples=0" in result.detail
    assert constructed == [("127.0.0.1", 1234)]


@pytest.mark.asyncio
async def test_doctor_camilla_probe_reports_controller_timeout(monkeypatch):
    class Controller:
        def __init__(self, _host: str, _port: int) -> None:
            pass

        async def get_volume_db(self):
            raise CamillaUnavailable("operation exceeded 5.0s")

        async def close(self):
            pass

    monkeypatch.setattr(audio, "CamillaController", Controller)
    cfg = SimpleNamespace(camilla_host="127.0.0.1", camilla_port=1234)

    result = await audio.check_camilla_websocket(cfg)

    assert result.status == "fail"
    assert "operation exceeded 5.0s" in result.detail


@pytest.mark.asyncio
async def test_doctor_camilla_probe_keeps_clipped_samples_optional(monkeypatch):
    class Controller:
        def __init__(self, _host: str, _port: int) -> None:
            pass

        async def get_volume_db(self):
            return -18.0

        async def get_clipped_samples(self):
            raise CamillaUnavailable("status command unavailable")

        async def close(self):
            pass

    monkeypatch.setattr(audio, "CamillaController", Controller)
    cfg = SimpleNamespace(camilla_host="127.0.0.1", camilla_port=1234)

    result = await audio.check_camilla_websocket(cfg)

    assert result.status == "ok"
    assert "volume=-18.0 dB clipped_samples=?" in result.detail
