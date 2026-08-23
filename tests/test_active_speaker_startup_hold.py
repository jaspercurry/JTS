# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

from jasper.active_speaker.startup_hold import (
    DEFAULT_STARTUP_HOLD_MARKER,
    STARTUP_HOLD_MARKER_ENV,
    hold_staged_startup,
    release_staged_startup_hold,
    staged_startup_hold_active,
    startup_hold_marker_path,
)


def test_hold_set_read_clear_roundtrip(tmp_path: Path) -> None:
    marker = tmp_path / "nested" / "staged-startup-hold"
    assert staged_startup_hold_active(marker) is False
    assert hold_staged_startup(marker) is True
    assert marker.exists()  # parent dir created on demand
    assert staged_startup_hold_active(marker) is True
    assert release_staged_startup_hold(marker) is True
    assert not marker.exists()
    assert staged_startup_hold_active(marker) is False


def test_release_is_idempotent(tmp_path: Path) -> None:
    marker = tmp_path / "staged-startup-hold"
    # Clearing an absent marker is a no-op success — a torn-down or never-set
    # session must still finish cleanly.
    assert release_staged_startup_hold(marker) is True
    assert not marker.exists()


def test_read_of_untraversable_path_reads_as_no_hold(tmp_path: Path) -> None:
    # A marker path routed THROUGH a regular file cannot resolve to a real
    # marker; the read must answer "no hold" (the safe direction — the selector
    # then restores the baseline, audio never silence) rather than propagate.
    not_a_dir = tmp_path / "blocker"
    not_a_dir.write_text("x", encoding="utf-8")
    trapped = not_a_dir / "child" / "marker"
    assert staged_startup_hold_active(trapped) is False


def test_path_resolution_prefers_arg_then_env_then_default(
    tmp_path: Path, monkeypatch
) -> None:
    explicit = tmp_path / "explicit"
    env = tmp_path / "env"
    monkeypatch.setenv(STARTUP_HOLD_MARKER_ENV, str(env))
    assert startup_hold_marker_path(explicit) == explicit
    assert startup_hold_marker_path() == env
    monkeypatch.delenv(STARTUP_HOLD_MARKER_ENV, raising=False)
    assert startup_hold_marker_path() == DEFAULT_STARTUP_HOLD_MARKER
