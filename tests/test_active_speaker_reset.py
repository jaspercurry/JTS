# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import fcntl
import os
import threading
from pathlib import Path

import pytest

from jasper.active_speaker import staging as staging_mod
from jasper.active_speaker.reset import (
    clear_active_speaker_measurement_journey,
    clear_active_speaker_setup_state,
)

# The GRAPH half of the staged startup-anchor pair. Reset never deletes it, but
# it is what the pair's lock is KEYED on (`staged_anchor_lock_path` derives the
# lock from this path), so every test in this module has to redirect it or the
# reset's lock acquisition reaches for the real /var/lib/camilladsp/configs.
_STAGED_CONFIG_ENV = "JASPER_ACTIVE_SPEAKER_STAGED_CONFIG_PATH"


@pytest.fixture(autouse=True)
def _staged_graph_half(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "staged.yml"
    monkeypatch.setenv(_STAGED_CONFIG_ENV, str(path))
    return path


# The subset clear_active_speaker_measurement_journey() must remove.
_MEASUREMENT_JOURNEY_ENVS = {
    "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE": "preview.json",
    "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH": "staged.json",
    "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE": "path-safety.json",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE": "commission-load.json",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE": "commission-ramp.json",
    "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE": "measurements.json",
}

# The subset it MUST preserve: driver research/manual settings, the applied
# Layer-A anchor, and the loaded-candidate rollback pointer.
_MEASUREMENT_JOURNEY_KEPT_ENVS = {
    "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE": "design.json",
    "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE": "startup-load.json",
    "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE": "baseline.json",
}

_STATE_ENVS = {
    "JASPER_ACTIVE_SPEAKER_DESIGN_DRAFT_STATE": "design.json",
    "JASPER_ACTIVE_SPEAKER_CROSSOVER_PREVIEW_STATE": "preview.json",
    "JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH": "staged.json",
    "JASPER_ACTIVE_SPEAKER_PATH_SAFETY_EVIDENCE": "path-safety.json",
    "JASPER_ACTIVE_SPEAKER_STARTUP_LOAD_STATE": "startup-load.json",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_LOAD_STATE": "commission-load.json",
    "JASPER_ACTIVE_SPEAKER_COMMISSION_RAMP_STATE": "commission-ramp.json",
    "JASPER_ACTIVE_SPEAKER_MEASUREMENTS_STATE": "measurements.json",
    "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE": "baseline.json",
}


def _seed_state_paths(monkeypatch, tmp_path: Path) -> list[Path]:
    paths: list[Path] = []
    for env_name, filename in _STATE_ENVS.items():
        path = tmp_path / filename
        path.write_text('{"stale": true}\n', encoding="utf-8")
        monkeypatch.setenv(env_name, str(path))
        paths.append(path)
    return paths


def test_clear_active_speaker_setup_state_removes_reset_owned_artifacts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    paths = _seed_state_paths(monkeypatch, tmp_path)

    payload = clear_active_speaker_setup_state()

    assert payload["kind"] == "jts_active_speaker_setup_reset"
    assert payload["status"] == "cleared"
    assert len(payload["cleared"]) == len(paths)
    assert payload["errors"] == []
    assert all(not path.exists() for path in paths)

    second = clear_active_speaker_setup_state()

    assert second["status"] == "cleared"
    assert second["cleared"] == []
    assert len(second["missing"]) == len(paths)
    assert second["errors"] == []


def test_clear_active_speaker_measurement_journey_clears_only_journey_subset(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A scoped "start over" clears the measurement journey and nothing else.

    Preserves design_draft (driver research + manual settings + topology
    intent), baseline_profile (the applied Layer-A anchor other subsystems
    read as SSOT), and startup_load (the currently-loaded candidate's
    rollback pointer — see reset.py's module docstring for the JTS3
    hardware evidence this exclusion is based on).
    """

    journey_paths: list[Path] = []
    for env_name, filename in _MEASUREMENT_JOURNEY_ENVS.items():
        path = tmp_path / filename
        path.write_text('{"stale": true}\n', encoding="utf-8")
        monkeypatch.setenv(env_name, str(path))
        journey_paths.append(path)

    kept_paths: list[Path] = []
    for env_name, filename in _MEASUREMENT_JOURNEY_KEPT_ENVS.items():
        path = tmp_path / filename
        path.write_text('{"keep": true}\n', encoding="utf-8")
        monkeypatch.setenv(env_name, str(path))
        kept_paths.append(path)

    payload = clear_active_speaker_measurement_journey()

    assert payload["kind"] == "jts_active_speaker_measurement_journey_reset"
    assert payload["status"] == "cleared"
    assert len(payload["cleared"]) == len(journey_paths)
    assert payload["errors"] == []
    assert {entry["id"] for entry in payload["cleared"]} == {
        "crossover_preview",
        "staged_config",
        "path_safety",
        "commission_load",
        "commission_ramp",
        "measurements",
    }

    assert all(not path.exists() for path in journey_paths)
    assert all(path.exists() for path in kept_paths)
    for path in kept_paths:
        assert "keep" in path.read_text(encoding="utf-8")

    second = clear_active_speaker_measurement_journey()

    assert second["status"] == "cleared"
    assert second["cleared"] == []
    assert len(second["missing"]) == len(journey_paths)
    assert second["errors"] == []
    assert all(path.exists() for path in kept_paths)


# --- #2590: the staged anchor's metadata half is unlinked under the pair lock -


class _StagePublish:
    """A second thread standing in for a concurrent staging writer.

    It does what both real writers do (``stage_protected_startup_config`` and
    ``baseline-reemit``'s publish): take the pair's lock, publish BOTH halves
    under it, release. Same-process is enough to contend — ``flock`` is scoped
    to the open file description, and this opens its own.
    """

    def __init__(self, *, graph: Path, metadata: Path) -> None:
        self._graph = graph
        self._metadata = metadata
        self._lock_path = staging_mod.staged_anchor_lock_path(graph)
        self.holding = threading.Event()
        self.may_publish = threading.Event()
        self.done = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, b"pid 4242 the-staging-writer\n")
            self.holding.set()
            # Held across the reset's whole bounded wait, then the pair is
            # published — the interleaving a lock-free reset would have torn.
            self.may_publish.wait(timeout=10)
            self._graph.write_text("# staged graph\n", encoding="utf-8")
            self._metadata.write_text('{"status": "staged"}', encoding="utf-8")
        finally:
            os.close(fd)
            self.done.set()

    def __enter__(self) -> "_StagePublish":
        self._thread.start()
        assert self.holding.wait(timeout=10), "staging thread never took the lock"
        return self

    def __exit__(self, *_exc) -> None:
        self.may_publish.set()
        self._thread.join(timeout=10)
        assert self.done.is_set(), "staging thread never released the lock"


def test_reset_refuses_the_staged_anchor_while_a_stage_holds_the_pair_lock(
    monkeypatch,
    tmp_path: Path,
    _staged_graph_half: Path,
) -> None:
    """The lost-reset race is closed: a held pair blocks ONLY its own unlink.

    Interleaving, not just "a lock exists". A second thread takes the pair's
    lock and publishes both halves while still holding it; the reset runs in
    between. Three things have to be true at once for the race to be closed,
    and each is asserted:

      1. the staged metadata the concurrent stage published is STILL ON DISK —
         the reset did not unlink a half of a pair another writer owns, which
         is the lost-reset / torn-pair outcome #2590 names;
      2. the contention is reported as THAT artifact's error, through the
         existing ``errors``/``status="partial"`` contract — no new exception
         type and no new return shape;
      3. the other eight artifacts still cleared — the hold is scoped to the
         one unlink, so a contending stage cannot block unrelated deletions.
    """
    paths = _seed_state_paths(monkeypatch, tmp_path)
    metadata = tmp_path / _STATE_ENVS["JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH"]
    others = [path for path in paths if path != metadata]
    assert len(others) == 8, others
    # Bounded wait, so the refusal is the test's outcome rather than its runtime.
    monkeypatch.setattr(staging_mod, "STAGED_ANCHOR_LOCK_TIMEOUT_SEC", 0.2)

    with _StagePublish(graph=_staged_graph_half, metadata=metadata):
        payload = clear_active_speaker_setup_state()

    # (2) the existing refusal contract carried the contention.
    assert payload["status"] == "partial"
    assert [entry["id"] for entry in payload["errors"]] == ["staged_config"]
    assert "StagedAnchorLockContended" in payload["errors"][0]["error"]
    assert "the-staging-writer" in payload["errors"][0]["error"]
    assert "staged_config" not in {entry["id"] for entry in payload["cleared"]}

    # (1) the pair the concurrent stage published survived intact — AND the
    # reset never claimed to have removed a file that is sitting on disk. That
    # pairing is the lost-reset shape itself: unguarded, the unlink lands
    # BEFORE the stage's publish, so the operator is told "cleared" and the
    # metadata is back a moment later.
    assert metadata.exists()
    assert metadata.read_text(encoding="utf-8") == '{"status": "staged"}'
    assert _staged_graph_half.read_text(encoding="utf-8") == "# staged graph\n"

    # (3) the hold was scoped to one unlink.
    assert {entry["id"] for entry in payload["cleared"]} == {
        "design_draft",
        "crossover_preview",
        "path_safety",
        "startup_load",
        "commission_load",
        "commission_ramp",
        "measurements",
        "baseline_profile",
    }
    assert all(not path.exists() for path in others)

    # POSITIVE CONTROL. Without this, a guard that ALWAYS errored on
    # `staged_config` would satisfy every assertion above. The lock is free
    # now, so the same call must clear the artifact it just refused.
    second = clear_active_speaker_setup_state()
    assert second["status"] == "cleared"
    assert second["errors"] == []
    assert [entry["id"] for entry in second["cleared"]] == ["staged_config"]
    assert not metadata.exists()
    # Still by design: the graph half is left for forensics (module docstring).
    assert _staged_graph_half.exists()


def test_reset_takes_the_pair_lock_keyed_on_the_graph_half(
    monkeypatch,
    tmp_path: Path,
    _staged_graph_half: Path,
) -> None:
    """The reset locks the SAME file the two staging writers lock.

    A lock keyed on the metadata half would serialize nothing: both writers
    pass ``staged_config_path()``. Proved by holding the lock at the writers'
    own key and observing the reset refuse — a different key would let the
    unlink straight through.
    """
    _seed_state_paths(monkeypatch, tmp_path)
    monkeypatch.setattr(staging_mod, "STAGED_ANCHOR_LOCK_TIMEOUT_SEC", 0.2)
    writers_key = staging_mod.staged_anchor_lock_path(
        staging_mod.staged_config_path()
    )
    assert writers_key == staging_mod.staged_anchor_lock_path(_staged_graph_half)

    fd = os.open(writers_key, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        payload = clear_active_speaker_setup_state()
    finally:
        os.close(fd)

    assert [entry["id"] for entry in payload["errors"]] == ["staged_config"]


def test_measurement_journey_reset_also_locks_the_staged_anchor(
    monkeypatch,
    tmp_path: Path,
    _staged_graph_half: Path,
) -> None:
    """The scoped "start over" clears ``staged_config`` too, so it locks too.

    Both public entry points reach the same ``_clear_paths``; this pins that
    the in-flow reset — the one a household actually triggers mid-measurement,
    where a concurrent stage is most likely — is not the unguarded half.
    """
    for env_name, filename in _MEASUREMENT_JOURNEY_ENVS.items():
        path = tmp_path / filename
        path.write_text('{"stale": true}\n', encoding="utf-8")
        monkeypatch.setenv(env_name, str(path))
    monkeypatch.setattr(staging_mod, "STAGED_ANCHOR_LOCK_TIMEOUT_SEC", 0.2)

    lock_path = staging_mod.staged_anchor_lock_path(_staged_graph_half)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        payload = clear_active_speaker_measurement_journey()
    finally:
        os.close(fd)

    assert payload["status"] == "partial"
    assert [entry["id"] for entry in payload["errors"]] == ["staged_config"]
    # Scoped: the other five journey artifacts still cleared.
    assert len(payload["cleared"]) == 5
