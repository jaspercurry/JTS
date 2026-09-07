# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pure-function tests for jasper.control.state_aggregate."""
from __future__ import annotations

from jasper.control.state_aggregate import (
    _active_speaker_level_match_provisional,
    active_speaker_output_safety_snapshot,
)


def test_active_speaker_output_safety_snapshot_uses_setup_status(
    monkeypatch,
) -> None:
    import jasper.control.state_aggregate as state_agg_mod

    def fake_setup(*, active_config_path=None, **_kwargs):
        assert active_config_path.endswith("active_speaker_staged_startup.yml")
        return {
            "active": True,
            "configured": False,
            "volume_allowed": False,
            "grouping_allowed": False,
            "reason": "active_speaker_commissioning_config_loaded",
            "active_config_path": active_config_path,
            "issues": [],
        }

    monkeypatch.setattr(
        state_agg_mod, "read_active_speaker_setup_status", fake_setup,
    )

    payload = active_speaker_output_safety_snapshot({
        "current": {
            "camilla": {
                "config_path": (
                    "/var/lib/camilladsp/configs/"
                    "active_speaker_staged_startup.yml"
                ),
            },
        },
    })

    assert payload["safety_muted"] is True
    assert payload["reason"] == "active_speaker_commissioning_config_loaded"
    assert payload["active_config_path"].endswith(
        "active_speaker_staged_startup.yml"
    )


def test_active_speaker_output_safety_snapshot_allows_setup_ready(
    monkeypatch,
) -> None:
    import jasper.control.state_aggregate as state_agg_mod

    def fake_setup(*, active_config_path=None, **_kwargs):
        return {
            "active": True,
            "configured": True,
            "volume_allowed": True,
            "grouping_allowed": True,
            "reason": None,
            "active_config_path": active_config_path,
            "issues": [],
        }

    monkeypatch.setattr(
        state_agg_mod, "read_active_speaker_setup_status", fake_setup,
    )

    payload = active_speaker_output_safety_snapshot({
        "current": {
            "camilla": {
                "config_path": (
                    "/var/lib/camilladsp/configs/"
                    "active_speaker_baseline.yml"
                ),
            },
        },
    })

    assert payload["safety_muted"] is False
    assert payload["reason"] is None


def test_level_match_provisional_none_when_no_applied_baseline() -> None:
    # C3b-3: the value is read from the readiness snapshot the caller already
    # computed, not from a second off-disk open. No applicable active baseline ->
    # None: a passive speaker (no baseline_profile), a non-dict setup, and an
    # active baseline whose candidate is not `applied` (e.g. superseded /
    # not-yet-applied) all return None.
    assert _active_speaker_level_match_provisional(None) is None
    assert _active_speaker_level_match_provisional({"baseline_profile": None}) is None
    assert _active_speaker_level_match_provisional({
        "baseline_profile": {"status": "ready_to_apply", "provisional": True},
    }) is None


def test_level_match_provisional_reads_applied_baseline() -> None:
    assert _active_speaker_level_match_provisional({
        "baseline_profile": {"status": "applied", "provisional": True},
    }) is True
    assert _active_speaker_level_match_provisional({
        "baseline_profile": {"status": "applied", "provisional": False},
    }) is False


def test_level_match_provisional_deduped_from_snapshot_setup(
    tmp_path, monkeypatch,
) -> None:
    # C3b-3 dedup pin: the snapshot's `level_match_provisional` is read from the
    # SAME readiness snapshot it already computed (the single source), not a
    # second disk read. Mutation-check: have `read_active_speaker_setup_status`
    # report an applied+provisional baseline and assert the snapshot surfaces it.
    # Reverting the dedup to a stale second disk read against an absent file
    # would yield None here (it would no longer track the snapshot).
    import jasper.control.state_aggregate as state_agg_mod

    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_BASELINE_PROFILE_STATE",
        str(tmp_path / "absent_baseline_profile.json"),  # nothing on disk
    )

    def fake_setup(**_kwargs):
        return {
            "active": True,
            "configured": True,
            "volume_allowed": True,
            "grouping_allowed": True,
            "reason": None,
            "baseline_profile": {"status": "applied", "provisional": True},
            "issues": [],
        }

    monkeypatch.setattr(
        state_agg_mod, "read_active_speaker_setup_status", fake_setup,
    )

    payload = active_speaker_output_safety_snapshot({
        "current": {"camilla": {"config_path": "/var/lib/camilladsp/configs/x.yml"}},
    })
    # Tracks the snapshot's baseline_profile, despite the on-disk file being absent.
    assert payload["level_match_provisional"] is True
