# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pin the single-owner contract for commission-tone orchestration (L4-1).

PR #969 forked the commission-tone helpers between ``jasper/web/sound_setup.py``
and the new ``jasper/active_speaker/web_commissioning.py``; the two copies had
already begun to drift. The migration is finished by making
``web_commissioning`` the single owner and having ``/sound/`` import the helpers
rather than keep a hand-copied fork (the sibling ``/correction/`` surface already
routes through ``web_commissioning`` via ``correction_crossover_backend``).

These guards make a future re-fork fail CI: the shared helpers must be the *same
function objects* on both surfaces (i.e. imported, not re-defined), and the
driver-test signal plan must come out identical from either surface.
"""

from __future__ import annotations

import jasper.active_speaker.web_commissioning as web_commissioning
import jasper.web.correction_crossover_backend as correction_backend
import jasper.web.sound_setup as sound_setup

# The commission-tone helpers active-speaker now owns and both operator surfaces
# share. ``_stop_commission_tone_locked`` is intentionally NOT here: it is bound
# to each module's own ``_COMMISSION_TONE_SESSION`` global that the per-surface
# play orchestration owns, so it stays surface-local by design.
SHARED_COMMISSION_TONE_HELPERS = (
    "_combined_speech_stimulus_wav_path",
    "_commission_summed_stimulus_issue",
    "_commission_tone_issue",
    "_commission_tone_mux_command",
    "_commission_tone_payload",
    "_commission_tone_release_fanin_lane",
    "_commission_tone_select_fanin_lane",
    "_commission_tone_signal_plan",
    "_commission_tone_target_key",
    "_commission_tone_wav_path",
    "_config_paths_match",
    "_summed_playback_with_issue",
)


def test_sound_setup_shares_web_commissioning_tone_helpers():
    """/sound/ must import the helpers from the owner, not re-define them."""

    for name in SHARED_COMMISSION_TONE_HELPERS:
        owner_fn = getattr(web_commissioning, name)
        sound_fn = getattr(sound_setup, name)
        assert sound_fn is owner_fn, (
            f"{name} on /sound/ is not the web_commissioning owner object — "
            "the commission-tone helpers have re-forked (see L4-1)."
        )


def test_no_forked_commission_tone_helper_defs_in_sound_setup():
    """sound_setup must not carry its own copy of any shared helper (textual)."""

    src = sound_setup.__loader__.get_source(sound_setup.__name__)
    assert src is not None
    for name in SHARED_COMMISSION_TONE_HELPERS:
        assert f"def {name}(" not in src, (
            f"sound_setup re-defines {name}; it must import it from "
            "jasper.active_speaker.web_commissioning instead (L4-1)."
        )


def test_correction_routes_through_web_commissioning_owner():
    """/correction/ keeps consuming the same owner module (no parallel fork)."""

    assert correction_backend.web_commissioning is web_commissioning


def test_driver_signal_plan_identical_across_surfaces(monkeypatch):
    """The shared driver-test signal plan is byte-identical from either surface.

    Both surfaces resolve the plan through the same owner object, so they cannot
    diverge; this exercises it end-to-end with a stub preset to prove the shared
    helper yields one plan regardless of which surface calls it.
    """

    class _Channel:
        role = "woofer"
        driver_style = "sealed"

    class _Group:
        id = "mono"
        channels = (_Channel(),)

    class _Topology:
        speaker_groups = (_Group(),)

    class _Preset:
        preset_id = "preset-test"
        name = "Test preset"

    sentinel_plan = {
        "artifact_schema_version": 1,
        "kind": "jts_active_speaker_driver_test_signal_plan",
        "status": "ready",
        "role": "woofer",
        "frequency_hz": 120.0,
        "allowed_band": {"highpass_hz": 80.0, "lowpass_hz": 400.0},
    }

    def _fake_driver_test_signal_plan(preset, role, *, driver_style=None):
        return dict(sentinel_plan)

    # Patch the lazy import target the shared helper reaches for.
    import jasper.active_speaker as active_speaker_pkg

    monkeypatch.setattr(
        active_speaker_pkg,
        "driver_test_signal_plan",
        _fake_driver_test_signal_plan,
    )

    kwargs = dict(
        role="woofer",
        group_id="mono",
        topology=_Topology(),
        preset=_Preset(),
    )
    plan_from_sound = sound_setup._commission_tone_signal_plan(**kwargs)
    plan_from_web = web_commissioning._commission_tone_signal_plan(**kwargs)

    assert plan_from_sound == plan_from_web
    assert plan_from_sound["status"] == "ready"
    assert plan_from_sound["preset_source"] == "explicit_preset"
    assert plan_from_sound["preset_id"] == "preset-test"
