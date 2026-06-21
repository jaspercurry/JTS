# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import stat
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "deploy" / "bin" / "jasper-librespot-event"


def _load_event_module():
    loader = importlib.machinery.SourceFileLoader("jasper_librespot_event", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_librespot_event_state_is_readable_by_non_root_daemons(tmp_path, monkeypatch):
    """librespot runs as pi:audio, but jasper-mux/control run as jasper:*.

    The event hook must not leave /run/librespot/state.json at mkstemp's 0600
    default, or Spotify auto-source detection fails closed under privilege
    separation.
    """
    module = _load_event_module()
    state_path = tmp_path / "run" / "librespot" / "state.json"
    monkeypatch.setattr(module, "STATE", state_path)

    for key in module.RELEVANT_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLAYER_EVENT", "playing")
    monkeypatch.setenv("TRACK_ID", "spotify:track:example")
    monkeypatch.setenv("VOLUME", "32768")

    assert module.main() == 0

    data = json.loads(state_path.read_text())
    assert data["playing"] is True
    assert data["track_id"] == "spotify:track:example"
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o644


def test_librespot_event_replaces_existing_owner_only_state(tmp_path, monkeypatch):
    module = _load_event_module()
    state_path = tmp_path / "run" / "librespot" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"playing": True}))
    state_path.chmod(0o600)
    monkeypatch.setattr(module, "STATE", state_path)

    for key in module.RELEVANT_VARS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PLAYER_EVENT", "paused")

    assert module.main() == 0

    data = json.loads(state_path.read_text())
    assert data["playing"] is False
    assert data["paused"] is True
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o644
