# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import json

from jasper import home_assistant
from jasper.control import ha_probe_child
from jasper.control.ha_status_cache import HomeAssistantStatusCache


def test_ha_status_cache_unconfigured_does_not_spawn(tmp_path, monkeypatch):
    env_file = tmp_path / "home_assistant.env"
    env_file.write_text("")
    called = False

    def fake_run(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("unconfigured HA should not spawn a probe")

    monkeypatch.setattr("jasper.control.ha_status_cache.subprocess.run", fake_run)
    cache = HomeAssistantStatusCache(env_file_path=str(env_file))

    assert cache.snapshot() == {
        "configured": False,
        "connected": False,
        "url": "",
        "instance_name": None,
        "version": None,
        "error": None,
    }
    assert called is False


def test_ha_status_cache_refreshes_via_child_json(tmp_path, monkeypatch):
    env_file = tmp_path / "home_assistant.env"
    env_file.write_text(
        "JASPER_HA_URL=http://homeassistant.local:8123\n"
        "JASPER_HA_TOKEN=test-token\n"
    )
    payload = {
        "configured": True,
        "connected": True,
        "url": "http://homeassistant.local:8123",
        "instance_name": "Brooklyn House",
        "version": "2026.6.1",
        "error": None,
    }

    class Proc:
        returncode = 0
        stdout = json.dumps(payload)

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Proc()

    monkeypatch.setattr("jasper.control.ha_status_cache.subprocess.run", fake_run)
    cache = HomeAssistantStatusCache(env_file_path=str(env_file), ttl_sec=60)
    key = (
        "http://homeassistant.local:8123",
        hashlib.sha256(b"test-token").hexdigest(),
        True,
    )

    cache._refresh(key)

    assert cache.snapshot() == payload
    assert calls[0][0][:3] == [
        cache._python,
        "-m",
        "jasper.control.ha_probe_child",
    ]
    assert calls[0][0][3] == str(env_file)
    assert calls[0][1]["stderr"] is not None


def test_ha_probe_child_emits_status_json(monkeypatch, capsys):
    async def fake_probe_status_from_env(*, env_file_path, force):
        assert env_file_path == "/tmp/ha.env"
        assert force is True
        return {
            "configured": True,
            "connected": False,
            "url": "http://ha.local:8123",
            "instance_name": None,
            "version": None,
            "error": "offline",
        }

    monkeypatch.setattr(
        home_assistant,
        "probe_status_from_env",
        fake_probe_status_from_env,
    )

    assert ha_probe_child.main(["/tmp/ha.env"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "configured": True,
        "connected": False,
        "url": "http://ha.local:8123",
        "instance_name": None,
        "version": None,
        "error": "offline",
    }
