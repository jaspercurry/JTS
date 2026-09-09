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


async def test_state_publishes_wake_storage_and_turn_identity(monkeypatch, tmp_path):
    from jasper.control import state_aggregate as sa
    from tests._wake_loop import wake_loop_for_tests
    from jasper.wake_events import WakeEventStore

    store = WakeEventStore(tmp_path / "wake-events")
    store.open()
    wl = wake_loop_for_tests(wake_event_store=store)
    wl._anchor_turn_timeline()
    status = wl.session_status()
    async def no_status(*_args, **_kwargs):
        return None
    async def voice_status(*_args, **_kwargs):
        return status
    async def camilla_status(**_kwargs):
        return {key: None for key in (
            "main_volume_db", "playback_rms_dbfs", "playback_peak_dbfs",
            "clipped_samples", "active_config_path",
        )}
    monkeypatch.setattr(sa, "_camilla_status", camilla_status)
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(tmp_path / "vol.json"))
    monkeypatch.setenv("JASPER_LIBRESPOT_STATE", str(tmp_path / "spot.env"))
    try:
        state = await sa._get_state(
            camilla_host="127.0.0.1", camilla_port=1234, voice_socket_path="/unused",
            voice_socket_command=voice_status, mux_socket_command=no_status,
            local_status_json=no_status,
            airplay_playing_snapshot=lambda: None,
        )
        assert state["voice"]["wake_event_store"] == status["wake_event_store"]
        assert state["voice"]["turn_event_id"] == status["turn_event_id"]
    finally:
        await store.aclose()
