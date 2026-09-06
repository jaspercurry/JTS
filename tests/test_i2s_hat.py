# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from pathlib import Path

import pytest

from jasper.audio_hardware.dac import all_profiles
from jasper.audio_hardware.i2s_hat import (
    I2S_HAT_BLOCK_BEGIN,
    I2sHatCollision,
    read_i2s_hat_intent,
    render_i2s_hat_boot_config,
    selectable_i2s_hat_profiles,
    write_i2s_hat_intent,
)

I2S_PROFILES = tuple(p for p in all_profiles() if p.connection == "i2s")
I2S_PROFILE_IDS = tuple(p.id for p in I2S_PROFILES)
SELECTABLE_PROFILES = selectable_i2s_hat_profiles()


@pytest.mark.parametrize(
    "profile", SELECTABLE_PROFILES, ids=[p.id for p in SELECTABLE_PROFILES]
)
def test_i2s_hat_intent_round_trip(tmp_path: Path, profile) -> None:
    intent = tmp_path / "i2s_hat.env"

    assert read_i2s_hat_intent(intent) is None
    write_i2s_hat_intent(profile.id, intent)
    assert intent.read_text(encoding="utf-8") == f"JASPER_I2S_HAT_PROFILE={profile.id}\n"
    assert read_i2s_hat_intent(intent) == profile.id

    # Explicit "none" is a persisted, distinct state from the file never
    # having existed: it writes a marker, not an unlink (#i2s-hat-intent).
    write_i2s_hat_intent(None, intent)
    assert intent.is_file()
    assert intent.read_text(encoding="utf-8") == "JASPER_I2S_HAT_PROFILE=\n"
    assert read_i2s_hat_intent(intent) is None


def test_i2s_hat_intent_rejects_unsupported_profiles(tmp_path: Path) -> None:
    intent = tmp_path / "i2s_hat.env"
    non_i2s_id = next(p.id for p in all_profiles() if p.connection != "i2s")
    detectable_id = next(p.id for p in I2S_PROFILES if p.hat_products)

    intent.write_text("JASPER_I2S_HAT_PROFILE=other_hat\n", encoding="utf-8")
    with pytest.raises(ValueError):
        read_i2s_hat_intent(intent)

    for refused in (non_i2s_id, "not_a_real_profile", detectable_id):
        with pytest.raises(ValueError):
            write_i2s_hat_intent(refused, intent)

    # A detectable profile saved by an older build is void, not an error:
    # detection is its only source now (ADR-0234).
    intent.write_text(
        f"JASPER_I2S_HAT_PROFILE={detectable_id}\n", encoding="utf-8"
    )
    assert read_i2s_hat_intent(intent) is None


@pytest.mark.parametrize("profile", I2S_PROFILES, ids=I2S_PROFILE_IDS)
def test_i2s_hat_renderer_manages_only_global_overlay(profile) -> None:
    original = (
        "arm_64bit=1\n"
        "[cm5]\n"
        f"dtoverlay={profile.dtoverlay}\n"
        "[all]\n"
        "dtparam=audio=on\n"
    )

    enabled, enabled_changed, enabled_collision = render_i2s_hat_boot_config(
        original, profile.id
    )
    disabled, disabled_changed, disabled_collision = render_i2s_hat_boot_config(
        enabled, None
    )

    assert enabled.count(I2S_HAT_BLOCK_BEGIN) == 1
    assert enabled.count(f"dtoverlay={profile.dtoverlay}") == 2
    # Section-scoped ([cm5]) lines are out of the global/all overlay scan,
    # so they never collide with the managed block.
    assert enabled_changed is True
    assert enabled_collision is None
    assert "arm_64bit=1" in enabled and "dtparam=audio=on" in enabled
    assert f"[cm5]\ndtoverlay={profile.dtoverlay}" in disabled
    assert disabled.count(f"dtoverlay={profile.dtoverlay}") == 1
    assert I2S_HAT_BLOCK_BEGIN not in disabled
    assert disabled_changed is True
    assert disabled_collision is None


@pytest.mark.parametrize("profile", I2S_PROFILES, ids=I2S_PROFILE_IDS)
def test_i2s_hat_renderer_refuses_a_same_overlay_collision(profile) -> None:
    original = f"[all]\ndtoverlay={profile.dtoverlay}\ndtparam=audio=on\n"

    rendered, changed, collision = render_i2s_hat_boot_config(original, profile.id)

    # Two declarations of the same overlay is still two I2S machine drivers
    # as far as this renderer is concerned: refuse rather than compound the
    # hand-written line with a managed one.
    assert rendered == original
    assert changed is False
    assert collision == I2sHatCollision(
        managed_overlay=profile.dtoverlay,
        colliding_overlays=(profile.dtoverlay,),
    )

    # Clearing never refuses -- there is nothing to collide with when
    # removing JTS's own (nonexistent) block.
    cleared, cleared_changed, cleared_collision = render_i2s_hat_boot_config(
        original, None
    )
    assert I2S_HAT_BLOCK_BEGIN not in cleared
    assert f"dtoverlay={profile.dtoverlay}" in cleared
    assert cleared_changed is False
    assert cleared_collision is None


def test_i2s_hat_renderer_refuses_a_different_overlay_collision() -> None:
    mismatched, matching = I2S_PROFILES[0], I2S_PROFILES[1]
    original = f"[all]\ndtoverlay={mismatched.dtoverlay}\n"

    rendered, changed, collision = render_i2s_hat_boot_config(original, matching.id)

    assert rendered == original
    assert changed is False
    assert collision == I2sHatCollision(
        managed_overlay=matching.dtoverlay,
        colliding_overlays=(mismatched.dtoverlay,),
    )


def test_i2s_hat_renderer_rejects_non_i2s_and_unregistered_profiles() -> None:
    non_i2s_id = next(p.id for p in all_profiles() if p.connection != "i2s")

    with pytest.raises(ValueError):
        render_i2s_hat_boot_config("[all]\n", non_i2s_id)
    with pytest.raises(ValueError):
        render_i2s_hat_boot_config("[all]\n", "not_a_real_profile")
