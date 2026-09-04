# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import fcntl
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml as yaml_lib

import jasper.active_speaker.declaration_vocabulary as vocabulary_mod
import jasper.active_speaker.staging as staging_mod
from jasper.active_speaker import (
    STAGED_STARTUP_CONFIG_KIND,
    ActiveSpeakerPreset,
    build_crossover_preview,
    emit_active_speaker_commissioning_config,
    load_active_speaker_preset,
    load_staged_startup_config,
    stage_protected_startup_config,
)
from jasper.active_speaker.crossover_preview import (
    DEFAULT_FILTER_TYPE,
    DEFAULT_SLOPE_DB_PER_OCTAVE,
)
from jasper.active_speaker.design_draft import (
    DRIVER_RESEARCH_KIND,
    ActiveSpeakerDesignDraftError,
    build_design_draft,
    normalise_manual_settings,
)
from jasper.active_speaker.profile import (
    SUPPORTED_CROSSOVER_TYPES,
    SUPPORTED_LR_ORDERS,
    ActiveSpeakerConfigError,
)
from jasper.active_speaker.declaration_vocabulary import (
    declared_filter_type_compiles,
    declared_slope_db_per_octave_compiles,
    supported_declaration_filter_types,
    supported_declaration_slopes_db_per_octave,
)
from jasper.active_speaker.path_safety import _startup_muted_by_candidate
from jasper.fanin_coupling import RING_ACTIVE_PLAYBACK_DEVICE
from jasper.output_hardware import DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID
from jasper.output_topology import OutputTopology
from tests.active_speaker_fixtures import (
    mono_output_topology,
    valid_camilla_config as _valid_config,
)

# Canonical preset fixtures (stereo 2-way: tweeters on physical outputs 1 and 3).
from tests.test_active_speaker_profile import _two_way_preset


def _topology(*, protection_status: str = "present") -> OutputTopology:
    return mono_output_topology(protection_status=protection_status)


def _dual_apple_topology(*, protection_status: str = "present") -> OutputTopology:
    raw = _topology(protection_status=protection_status).to_dict()
    raw["hardware"] = {
        "device_id": DUAL_APPLE_USB_C_DAC_4CH_DEVICE_ID,
        "device_label": "Dual Apple USB-C DAC 4-channel pair",
        "physical_output_count": 4,
        "child_devices": [
            {
                "child_id": "apple_dac_1",
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "physical_output_indexes": [0, 1],
            },
            {
                "child_id": "apple_dac_2",
                "device_id": "apple_usb_c_dongle",
                "device_label": "Apple USB-C audio adapter",
                "physical_output_indexes": [2, 3],
            },
        ],
    }
    return OutputTopology.from_mapping(raw)


def _three_way_topology() -> OutputTopology:
    return mono_output_topology(
        mode="active_3_way", protection_status="present",
        topology_id="bench_mono_3way",
    )


def _topology_with_subwoofer() -> OutputTopology:
    return mono_output_topology(
        with_subwoofer=True, protection_status="present",
        topology_id="bench_mono_with_sub", sub_label="Bench subwoofer",
    )


def _stereo_topology(*, way_count: int) -> OutputTopology:
    """The mono fixture's stereo twin: one group per side, lanes contiguous.

    Not `mono_output_topology`, which owns exactly one group — the split mixer,
    the routing keys and the lane count all differ, so this is a second
    topology rather than a variant of that one.
    """

    roles = ["woofer", "mid", "tweeter"] if way_count == 3 else ["woofer", "tweeter"]
    raw = _topology().to_dict()
    raw["topology_id"] = f"bench_stereo_{way_count}way"
    raw["speaker_groups"] = [
        {
            "id": side,
            "label": f"{side.capitalize()} speaker",
            "kind": side,
            "mode": f"active_{way_count}_way",
            "channels": [
                {
                    "role": role,
                    "physical_output_index": index * way_count + offset,
                    "identity_verified": True,
                }
                | (
                    {
                        "startup_muted": True,
                        "protection_required": True,
                        "protection_status": "present",
                    }
                    if role == "tweeter"
                    else {}
                )
                for offset, role in enumerate(roles)
            ],
        }
        for index, side in enumerate(("left", "right"))
    ]
    raw["routing"] = {
        "main_left_group_id": "left",
        "main_right_group_id": "right",
        "mono_group_id": None,
        "subwoofer_group_ids": [],
    }
    return OutputTopology.from_mapping(raw)


def _driver_research(
    *,
    frequency_hz: float = 2500,
    way_count: int = 2,
    with_subwoofer: bool = False,
) -> dict:
    drivers = [
        {
            "role": "woofer",
            "manufacturer": "Dayton Audio",
            "model": "Epique E150HE-44",
            "usable_frequency_range_hz": [45, 5000],
            "recommended_lowpass_hz": frequency_hz,
            "sources": ["https://example.test/woofer"],
        },
        {
            "role": "tweeter",
            "manufacturer": "Eminence",
            "model": "F110M-8",
            # #2603: one owner. This used to declare the tweeter's minimum
            # recommended crossover AS the crossover frequency, with a separate
            # lower do_not_test_below_hz doing the actual refusing. Those are
            # now the same number, so the fixture states the real shape -- a
            # driver whose minimum is BELOW where the design crosses it -- and
            # do_not_test_below_hz is RETIRED rather than derived
            # (driver_protection.py's ruling block says why deriving it would
            # have made #2491's load gate unreachable).
            "recommended_highpass_hz": 1500,
            "sources": ["https://example.test/tweeter"],
        },
    ]
    candidates = [
        {
            "between_roles": ["woofer", "tweeter"],
            "frequency_hz": frequency_hz,
            "filter_type": "Linkwitz-Riley",
            "slope_db_per_octave": 24,
            "confidence": "medium",
        }
    ]
    if way_count == 3:
        drivers.insert(1, {
            "role": "mid",
            "manufacturer": "Example",
            "model": "Mid driver",
            "usable_frequency_range_hz": [250, 5000],
            "recommended_highpass_hz": 450,
            "recommended_lowpass_hz": frequency_hz,
            "sources": ["https://example.test/mid"],
        })
        candidates = [
            {
                "between_roles": ["woofer", "mid"],
                "frequency_hz": 450,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
            },
            {
                "between_roles": ["mid", "tweeter"],
                "frequency_hz": frequency_hz,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
            },
        ]
    if with_subwoofer:
        drivers.append({
            "role": "subwoofer",
            "manufacturer": "Example",
            "model": "Sub driver",
            "usable_frequency_range_hz": [20, 200],
            "recommended_lowpass_hz": 80,
            "sources": ["https://example.test/sub"],
        })
    return {
        "artifact_schema_version": 1,
        "kind": DRIVER_RESEARCH_KIND,
        "drivers": drivers,
        "crossover_candidates": candidates,
    }


def _crossover_preview(
    topology: OutputTopology,
    *,
    frequency_hz: float = 2500,
    way_count: int = 2,
    with_subwoofer: bool = False,
) -> dict:
    return build_crossover_preview(
        build_design_draft(
            topology,
            driver_research=_driver_research(
                frequency_hz=frequency_hz,
                way_count=way_count,
                with_subwoofer=with_subwoofer,
            ),
            created_at="2026-06-10T12:00:00Z",
        ),
        created_at="2026-06-10T12:30:00Z",
    )


def test_default_active_speaker_preset_is_epique_f110m_safe_bringup() -> None:
    preset = load_active_speaker_preset()

    assert preset.preset_id == "epique-e150he44-eminence-f110m8-safe-v1"
    assert preset.name == "Dayton Epique E150HE-44 + Eminence F110M-8 safe bring-up"
    assert preset.crossover_regions[0].fc_hz == 2500
    assert preset.safety.max_commissioning_level_db_spl == 85


def test_stage_protected_startup_config_writes_muted_candidate(
    tmp_path: Path,
) -> None:
    out = tmp_path / "active_staged.yml"
    meta = tmp_path / "active_staged.json"

    payload = stage_protected_startup_config(
        _topology(),
        config_path=out,
        metadata_path=meta,
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    text = out.read_text(encoding="utf-8")
    loaded = load_staged_startup_config(metadata_path=meta)

    assert payload["kind"] == STAGED_STARTUP_CONFIG_KIND
    assert payload["status"] == "staged"
    assert payload["preset"]["preset_id"] == "epique-e150he44-eminence-f110m8-safe-v1"
    # Stage 2: the DAC8x declares an active outputd lane, so staging resolves to
    # that lane (not a direct-DAC route) — staging never silently defaults to
    # hw:<card>,0 on outputd-owned hardware. #2285 P2 retired the snd-aloop
    # ACTIVE endpoint, so the lane is reached over the ACTIVE RING; the SOURCE
    # is deliberately unchanged, because it names the lane ROLE and not the
    # transport, and re-pointing it would drop a genuinely invariant property.
    assert payload["config"]["playback_device"] == RING_ACTIVE_PLAYBACK_DEVICE
    assert payload["config"]["playback_device_source"] == "outputd_active_lane"
    assert payload["config"]["playback_channels"] == 2
    assert payload["config"]["validation"]["status"] == "valid"
    assert payload["config"]["tweeter_protective_highpass_hz"] == 5000
    assert payload["load"]["load_allowed"] is False
    assert payload["load"]["load_gate"] == "startup_load_preflight_required"
    assert payload["issues"] == []
    assert "preset_id=epique-e150he44-eminence-f110m8-safe-v1" in text
    assert "split_active_2way" in text
    assert "as_tweeter_protective_hp" in text
    assert "freq: 5000.0000" in text
    assert "mute: true" in text
    assert loaded["status"] == "staged"


def test_stage_protected_startup_config_uses_crossover_preview_frequency(
    tmp_path: Path,
) -> None:
    out = tmp_path / "active_staged.yml"
    preview = _crossover_preview(_topology(), frequency_hz=3200)

    payload = stage_protected_startup_config(
        _topology(),
        crossover_preview=preview,
        config_path=out,
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    text = out.read_text(encoding="utf-8")

    assert payload["status"] == "staged"
    assert payload["preset"]["source"]["mode"] == "crossover_preview"
    assert payload["preset"]["preset_id"] == "preview-bench_mono-2way"
    assert payload["config"]["tweeter_protective_highpass_hz"] == 6400
    assert "freq: 3200.0000" in text
    assert "freq: 6400.0000" in text


# --- The vocabulary /sound/ may OFFER ----------------------------------------
#
# The editor used to offer a wider vocabulary than this module builds
# ("Butterworth" in the filter picker, any multiple of 6 dB/octave in a free
# slope field), and the mismatch surfaced only as the
# ``crossover_preview_filter_unsupported`` blocker several screens later. The
# offer is derived now, so these pin the DERIVATION rather than today's
# membership: widening the supported sets must move the offer with them.


def test_offered_filter_types_are_one_per_supported_target_type() -> None:
    offered = supported_declaration_filter_types()

    assert len(offered) == len(SUPPORTED_CROSSOVER_TYPES)
    assert {
        vocabulary_mod._normalise_filter_type(spelling) for spelling in offered
    } == SUPPORTED_CROSSOVER_TYPES


def test_offered_slopes_are_one_per_supported_order_ascending() -> None:
    offered = supported_declaration_slopes_db_per_octave()

    assert list(offered) == sorted(offered)
    assert {
        vocabulary_mod._slope_to_lr_order(slope) for slope in offered
    } == SUPPORTED_LR_ORDERS


def test_a_supported_filter_with_no_declared_spelling_is_loud(monkeypatch) -> None:
    """Silently omitting it would narrow the offer back to what it was.

    That silence is exactly the defect the derived offer exists to end, so the
    honest failure for "the compiler grew a filter Sound cannot name" is a
    raise, not a shorter list.
    """
    monkeypatch.setattr(
        vocabulary_mod,
        "SUPPORTED_CROSSOVER_TYPES",
        SUPPORTED_CROSSOVER_TYPES | {"Bessel"},
    )

    with pytest.raises(ActiveSpeakerConfigError, match="Bessel"):
        supported_declaration_filter_types()


def test_everything_offered_compiles_and_so_do_household_spellings() -> None:
    for spelling in supported_declaration_filter_types():
        assert declared_filter_type_compiles(spelling)
    # Wider than the offer on purpose: a declaration written by hand or by the
    # research assistant may spell the same filter differently.
    assert declared_filter_type_compiles("LR")
    assert declared_filter_type_compiles("linkwitz riley")
    assert not declared_filter_type_compiles("Butterworth")
    assert not declared_filter_type_compiles("")
    assert not declared_filter_type_compiles(None)

    for slope in supported_declaration_slopes_db_per_octave():
        assert declared_slope_db_per_octave_compiles(slope)
    assert not declared_slope_db_per_octave_compiles(18)
    assert not declared_slope_db_per_octave_compiles(6)
    assert not declared_slope_db_per_octave_compiles(0)
    assert not declared_slope_db_per_octave_compiles("twenty-four")


def test_preview_defaults_are_members_of_the_offer() -> None:
    """The /sound/ editor pre-selects these.

    A default outside the offer would render a picker with nothing selected and
    then compile as something the household never saw.
    """
    assert DEFAULT_FILTER_TYPE in supported_declaration_filter_types()
    assert DEFAULT_SLOPE_DB_PER_OCTAVE in supported_declaration_slopes_db_per_octave()


def test_an_alias_for_an_unsupported_filter_is_not_a_filter(monkeypatch) -> None:
    """``"lr"`` is an alias, not a second owner of what compiles."""
    monkeypatch.setattr(vocabulary_mod, "SUPPORTED_CROSSOVER_TYPES", {"Bessel"})

    assert not declared_filter_type_compiles("LR")
    assert not declared_filter_type_compiles("Linkwitz-Riley")


def _preview_with_filter(
    topology: OutputTopology, *, filter_type: object, slope: object
) -> dict:
    """A preview whose crossovers declare ``filter_type`` / ``slope``.

    Patched onto a valid preview on purpose: since ticket 1.7 the design draft
    REFUSES a vocabulary the compiler cannot build, so the only way to hand
    ``compile_preset_from_crossover_preview`` a bad one is to write it past the
    door that now stops it.
    """

    preview = _crossover_preview(topology, frequency_hz=2500, way_count=2)
    for group in preview["groups"]:
        for crossover in group["crossovers"]:
            for entry in crossover["filters"]:
                entry["filter_type"] = filter_type
                entry["slope_db_per_octave"] = slope
    return preview


def test_every_entry_accepted_crossover_vocabulary_compiles() -> None:
    """The entry gate accepts only what this compiler builds.

    ``crossover_preview_filter_unsupported`` is the blocker the design draft's
    entry-time refusal exists to make unreachable from /sound/: anything the
    wizard can now save must clear it.
    """
    topology = _topology()
    for filter_type in supported_declaration_filter_types():
        for slope in supported_declaration_slopes_db_per_octave():
            preview = _preview_with_filter(
                topology, filter_type=filter_type, slope=slope
            )

            preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
                topology, preview
            )

            codes = {issue["code"] for issue in issues}
            assert "crossover_preview_filter_unsupported" not in codes, issues
            assert preset is not None, issues
            assert preset.crossover_regions[0].order == slope / 6


def test_the_late_filter_blocker_is_now_unreachable_from_the_draft() -> None:
    """Same value, two gates — and the entry gate is the one that fires first.

    The compile-time blocker stays as defence in depth for a draft written
    before the entry gate existed; what changed is that it is no longer the
    FIRST place a household hears about a filter JTS cannot build.
    """
    topology = _topology()
    for filter_type, slope in (("Butterworth", 24), ("Linkwitz-Riley", 18)):
        preview = _preview_with_filter(topology, filter_type=filter_type, slope=slope)

        _preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
            topology, preview
        )

        blocker = next(
            issue
            for issue in issues
            if issue["code"] == "crossover_preview_filter_unsupported"
        )
        # The blocker names the same sets the entry gate offers, read from the
        # accessors rather than spelled in prose that 4.1 would leave stale.
        for offered in (
            *supported_declaration_filter_types(),
            *(f"{s:g}" for s in supported_declaration_slopes_db_per_octave()),
        ):
            assert offered in blocker["message"], (offered, blocker)
        with pytest.raises(ActiveSpeakerDesignDraftError):
            normalise_manual_settings(
                {
                    "crossover_candidates": [
                        {
                            "between_roles": ["woofer", "tweeter"],
                            "frequency_hz": 2500,
                            "filter_type": filter_type,
                            "slope_db_per_octave": slope,
                        }
                    ]
                }
            )


def test_compile_preset_from_crossover_preview_sets_polarity_and_delay() -> None:
    topology = _stereo_topology(way_count=2)
    preview = _crossover_preview(topology, frequency_hz=2500, way_count=2)
    for group in preview["groups"]:
        crossover = group["crossovers"][0]
        crossover["lower_polarity"] = "non-inverted"
        crossover["upper_polarity"] = "inverted"
        crossover["delay_ms"] = 0.3
        crossover["delay_target_role"] = "woofer"

    preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
        topology, preview
    )

    assert preset is not None, issues
    region = preset.crossover_regions[0]
    assert region.lower_polarity == "non-inverted"
    assert region.upper_polarity == "inverted"
    assert region.delay_ms == 0.3
    assert region.delay_target_driver == "woofer"


def test_compile_preset_from_crossover_preview_omits_polarity_and_delay_by_default() -> None:
    # Legacy-shaped preview (no persisted working-crossover values): the
    # region stays byte-identical to the pre-Slice-0 schema defaults.
    topology = _topology()
    preview = _crossover_preview(topology, frequency_hz=2500, way_count=2)

    preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
        topology, preview
    )

    assert preset is not None, issues
    region = preset.crossover_regions[0]
    assert region.lower_polarity == "non-inverted"
    assert region.upper_polarity == "non-inverted"
    assert region.delay_ms is None
    assert region.delay_target_driver is None


def test_legacy_manual_role_rows_keep_stereo_preview_additive_but_not_confirmed() -> None:
    topology = _stereo_topology(way_count=2)
    legacy = _driver_research(frequency_hz=2500, way_count=2)
    draft = build_design_draft(
        topology,
        manual_settings={
            "drivers": legacy["drivers"],
            "crossover_candidates": legacy["crossover_candidates"],
        },
        created_at="2026-06-10T12:00:00Z",
    )
    preview = build_crossover_preview(
        draft,
        created_at="2026-06-10T12:30:00Z",
    )
    preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
        topology,
        preview,
    )

    assert draft["status"] == "ready_for_review"
    assert draft["summary"]["missing_driver_info_target_ids"] == []
    assert draft["driver_safety_profile"]["status"] == "incomplete"
    assert {
        target["target_values_binding"]
        for target in draft["driver_safety_profile"]["targets"]
    } == {"missing"}
    assert preview["status"] == "ready_for_protected_staging"
    assert preset is not None, issues


def test_compile_preset_from_crossover_preview_stereo_polarity_mismatch_blocks() -> None:
    topology = _stereo_topology(way_count=2)
    preview = _crossover_preview(topology, frequency_hz=2500, way_count=2)
    left_group = next(g for g in preview["groups"] if g["kind"] == "left")
    left_group["crossovers"][0]["lower_polarity"] = "inverted"

    preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
        topology, preview
    )

    assert preset is None
    assert "crossover_preview_stereo_values_differ" in {
        issue["code"] for issue in issues
    }


def test_compile_preset_from_crossover_preview_stereo_delay_mismatch_blocks() -> None:
    topology = _stereo_topology(way_count=2)
    preview = _crossover_preview(topology, frequency_hz=2500, way_count=2)
    right_group = next(g for g in preview["groups"] if g["kind"] == "right")
    right_group["crossovers"][0]["delay_ms"] = 0.5
    right_group["crossovers"][0]["delay_target_role"] = "woofer"

    preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
        topology, preview
    )

    assert preset is None
    assert "crossover_preview_stereo_values_differ" in {
        issue["code"] for issue in issues
    }


# --- Manual /sound/ form entry path, end to end ------------------------------
#
# The tests above hand-mutate an already-built preview dict, which never
# exercises _normalise_candidate's validation or crossover_preview's
# between_roles realignment. These two start from a manual_settings candidate
# shaped exactly like jasper/active_speaker/design_draft.py's manualSettingsPayload
# (deploy/assets/sound-profile/js/main.js) sends, and follow it through the
# real chain: build_design_draft -> build_crossover_preview ->
# compile_preset_from_crossover_preview.


def test_compile_preset_from_crossover_preview_manual_settings_end_to_end_sets_polarity_and_delay() -> None:
    topology = _topology()
    draft = build_design_draft(
        topology,
        driver_research=_driver_research(frequency_hz=2500, way_count=2),
        manual_settings={
            "drivers": [],
            "crossover_candidates": [{
                "between_roles": ["woofer", "tweeter"],
                "frequency_hz": 2500,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
                "lower_polarity": "non-inverted",
                "upper_polarity": "inverted",
                "delay_ms": 0.15,
                "delay_target_role": "tweeter",
            }],
        },
        created_at="2026-07-11T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-07-11T12:00:05Z")

    preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
        topology, preview
    )

    assert preset is not None, issues
    region = preset.crossover_regions[0]
    assert region.lower_polarity == "non-inverted"
    assert region.upper_polarity == "inverted"
    assert region.delay_ms == 0.15
    assert region.delay_target_driver == "tweeter"


def test_compile_preset_from_crossover_preview_manual_settings_reversed_between_roles_realigns_end_to_end() -> None:
    # The candidate declares its pair as [tweeter, woofer] -- reversed from
    # this topology's own (lower_role, upper_role)=(woofer, tweeter). The same
    # PHYSICAL role (tweeter) must end up inverted/delayed regardless of which
    # order the candidate (or a reversed research import) listed the pair in.
    topology = _topology()
    draft = build_design_draft(
        topology,
        driver_research=_driver_research(frequency_hz=2500, way_count=2),
        manual_settings={
            "drivers": [],
            "crossover_candidates": [{
                "between_roles": ["tweeter", "woofer"],
                "frequency_hz": 2500,
                "filter_type": "Linkwitz-Riley",
                "slope_db_per_octave": 24,
                "confidence": "medium",
                # Describes the candidate's OWN between_roles[0]=tweeter.
                "lower_polarity": "inverted",
                # Describes the candidate's OWN between_roles[1]=woofer.
                "upper_polarity": "non-inverted",
                "delay_ms": 0.15,
                "delay_target_role": "tweeter",
            }],
        },
        created_at="2026-07-11T12:00:00Z",
    )
    preview = build_crossover_preview(draft, created_at="2026-07-11T12:00:05Z")

    preset, issues, _gates = staging_mod.compile_preset_from_crossover_preview(
        topology, preview
    )

    assert preset is not None, issues
    region = preset.crossover_regions[0]
    assert region.lower_driver == "woofer"
    assert region.upper_driver == "tweeter"
    # Realigned to THIS function's (lower=woofer, upper=tweeter) convention:
    # tweeter is the physical role the candidate inverted/delayed, so it must
    # land on upper_polarity/delay here, not lower_polarity.
    assert region.lower_polarity == "non-inverted"
    assert region.upper_polarity == "inverted"
    assert region.delay_ms == 0.15
    assert region.delay_target_driver == "tweeter"


def test_stage_protected_startup_config_blocks_unready_crossover_preview(
    tmp_path: Path,
) -> None:
    preview = _crossover_preview(_topology())
    preview["status"] = "stale"
    preview["permissions"]["may_prepare_protected_startup_config"] = False

    payload = stage_protected_startup_config(
        _topology(),
        crossover_preview=preview,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )

    assert payload["status"] == "blocked"
    assert "crossover_preview_not_ready" in {
        issue["code"] for issue in payload["issues"]
    }


def test_stage_protected_startup_config_arms_subwoofer_muted(
    tmp_path: Path,
) -> None:
    # B2: a routed local subwoofer now STAGES — the sub output is wired into the
    # protected startup graph MUTED, exactly like the woofer/tweeter, rather than
    # blocking. The mains pick up the complementary bass-management high-pass.
    topology = _topology_with_subwoofer()
    preview = _crossover_preview(topology, with_subwoofer=True)
    out = tmp_path / "active_staged.yml"

    payload = stage_protected_startup_config(
        topology,
        crossover_preview=preview,
        config_path=out,
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    subwoofer_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "subwoofer_startup_staging_scope"
    )

    assert payload["status"] == "staged"
    assert out.exists() is True
    assert subwoofer_gate["passed"] is True
    assert payload["issues"] == []

    text = out.read_text(encoding="utf-8")
    parsed = yaml_lib.safe_load(text)
    # output_count grows by the sub output: 2 mains + 1 sub = 3.
    assert parsed["devices"]["playback"]["channels"] == 3
    sub_steps = [
        step for step in parsed["pipeline"]
        if step.get("type") == "Filter" and step.get("channels") == [2]
    ]
    # The sub output (channel 2): its protective lane (band-limit LP + excursion
    # limiter) runs FIRST, then the per-output commission mute. So the sub is
    # band-limited + excursion-limited even when the mute is later lifted to ramp.
    assert sub_steps[0]["names"] == ["as_sub_lowpass", "as_sub_startup_limiter"]
    assert sub_steps[-1]["names"] == ["as_out2_commission_mute"]
    assert parsed["filters"]["as_sub_lowpass"]["parameters"]["type"] == (
        "LinkwitzRileyLowpass"
    )
    assert parsed["filters"]["as_sub_startup_limiter"]["type"] == "Limiter"
    # The sub starts MUTED at boot: its per-output commission mute is a hard mute
    # (all_commission_mutes_engaged is asserted by the fully-muted gate too).
    assert parsed["filters"]["as_out2_commission_mute"]["parameters"]["mute"] is True
    # The mains' lowest driver (woofer, output 0) carries the bass-management HP.
    woofer_step = next(
        step for step in parsed["pipeline"]
        if step.get("type") == "Filter" and step.get("channels") == [0]
    )
    assert "as_woofer_bass_mgmt_hp" in woofer_step["names"]


def test_stage_protected_startup_config_blocks_misrouted_subwoofer(
    tmp_path: Path,
) -> None:
    # Fail-closed: a sub pinned to a NON-contiguous output (not the next channel
    # after the mains) can never be armed safely — staging must block, never stage a
    # mains-only graph that silently drops the sub.
    raw = _topology().to_dict()
    raw["topology_id"] = "bench_mono_bad_sub"
    raw["speaker_groups"].append({
        "id": "sub",
        "label": "Bench subwoofer",
        "kind": "subwoofer",
        "mode": "subwoofer",
        # Mains occupy 0+1; a safe sub is on output 2. Output 5 is misrouted.
        "channels": [
            {"role": "subwoofer", "physical_output_index": 5, "identity_verified": True}
        ],
    })
    raw["routing"]["subwoofer_group_ids"] = ["sub"]
    topology = OutputTopology.from_mapping(raw)
    preview = _crossover_preview(topology, with_subwoofer=True)
    out = tmp_path / "active_staged.yml"

    payload = stage_protected_startup_config(
        topology,
        crossover_preview=preview,
        config_path=out,
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    subwoofer_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "subwoofer_startup_staging_scope"
    )

    assert payload["status"] == "blocked"
    assert out.exists() is False
    assert subwoofer_gate["passed"] is False
    assert "active_subwoofer_output_not_contiguous" in {
        issue["code"] for issue in payload["issues"]
    }


def test_stage_protected_startup_config_supports_active_three_way_preview(
    tmp_path: Path,
) -> None:
    topology = _three_way_topology()
    preview = _crossover_preview(topology, frequency_hz=2800, way_count=3)
    out = tmp_path / "active_staged.yml"

    payload = stage_protected_startup_config(
        topology,
        crossover_preview=preview,
        config_path=out,
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    text = out.read_text(encoding="utf-8")

    assert payload["status"] == "staged"
    assert payload["preset"]["way_count"] == 3
    assert payload["config"]["playback_channels"] == 3
    assert payload["config"]["tweeter_protective_highpass_hz"] == 5600
    assert "split_active_3way" in text
    assert "freq: 450.0000" in text
    assert "freq: 2800.0000" in text
    assert "freq: 5600.0000" in text


def test_stage_protected_startup_config_supports_stereo_three_way_on_dac8x(
    tmp_path: Path,
) -> None:
    topology = _stereo_topology(way_count=3)
    preview = _crossover_preview(topology, frequency_hz=2800, way_count=3)

    payload = stage_protected_startup_config(
        topology,
        crossover_preview=preview,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
    )

    assert payload["status"] == "staged"
    # Stage 2: a stereo 3-way (6 lanes) fits within the DAC8x active outputd
    # lane (width 8), so staging resolves to it rather than a direct-DAC route.
    # The lane is reached over the ACTIVE RING since #2285 P2; the SOURCE is
    # invariant across that change and is asserted unchanged on purpose.
    assert payload["config"]["playback_device"] == RING_ACTIVE_PLAYBACK_DEVICE
    assert payload["config"]["playback_device_source"] == "outputd_active_lane"
    assert payload["config"]["playback_channels"] == 6
    capacity_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "active_playback_route_capacity"
    )
    assert capacity_gate["passed"] is True


def test_stage_protected_startup_config_preview_honors_saved_role_mapping(
    tmp_path: Path,
) -> None:
    raw = _topology().to_dict()
    raw["speaker_groups"][0]["channels"][0]["physical_output_index"] = 1
    raw["speaker_groups"][0]["channels"][1]["physical_output_index"] = 0
    topology = OutputTopology.from_mapping(raw)
    preview = _crossover_preview(topology)

    payload = stage_protected_startup_config(
        topology,
        crossover_preview=preview,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    role_order_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "active_output_role_order"
    )

    assert payload["status"] == "staged"
    assert role_order_gate["passed"] is True
    assert role_order_gate["message"] == (
        "Preview-derived DSP will follow the saved output role mapping"
    )


def test_stage_protected_startup_config_uses_outputd_active_lane_for_dual_apple(
    tmp_path: Path,
) -> None:
    out = tmp_path / "active_staged.yml"
    meta = tmp_path / "active_staged.json"

    payload = stage_protected_startup_config(
        _dual_apple_topology(),
        config_path=out,
        metadata_path=meta,
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )

    assert payload["status"] == "staged"
    # The lane is reached over the ACTIVE RING since #2285 P2. The SOURCE is
    # invariant and stays asserted; the emitted YAML names the same device, so
    # the text assertion re-points with the payload rather than being dropped.
    assert payload["config"]["playback_device"] == RING_ACTIVE_PLAYBACK_DEVICE
    assert payload["config"]["playback_device_source"] == "outputd_active_lane"
    assert f'device: "{RING_ACTIVE_PLAYBACK_DEVICE}"' in out.read_text(
        encoding="utf-8"
    )


def test_stage_protected_startup_config_blocks_missing_tweeter_protection(
    tmp_path: Path,
) -> None:
    out = tmp_path / "active_staged.yml"
    meta = tmp_path / "active_staged.json"

    payload = stage_protected_startup_config(
        _topology(protection_status="required_missing"),
        config_path=out,
        metadata_path=meta,
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )

    assert payload["status"] == "blocked"
    assert out.exists() is False
    assert "tweeter_protection_required" in {
        issue["code"] for issue in payload["issues"]
    }
    assert payload["config"]["validation"]["status"] == "skipped"


def test_stage_protected_startup_config_allows_software_guard_request_no_load_candidate(
    tmp_path: Path,
) -> None:
    out = tmp_path / "active_staged.yml"
    meta = tmp_path / "active_staged.json"

    payload = stage_protected_startup_config(
        _topology(protection_status="software_guard_requested"),
        config_path=out,
        metadata_path=meta,
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    text = out.read_text(encoding="utf-8")
    loaded = load_staged_startup_config(metadata_path=meta)
    codes = {issue["code"]: issue["severity"] for issue in payload["issues"]}
    guard_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "software_tweeter_guard_evidence"
    )

    assert payload["status"] == "staged"
    assert payload["load"]["load_allowed"] is False
    assert payload["software_guard"]["passed"] is True
    assert payload["software_guard"]["no_load"] is True
    assert payload["software_guard"]["no_playback"] is True
    assert all(payload["software_guard"]["checks"].values())
    assert guard_gate["passed"] is True
    assert codes == {"software_tweeter_guard_requested": "warning"}
    assert "as_tweeter_protective_hp" in text
    # Single-audio-path commissioning isolates per *physical output*: the tweeter
    # (mono 2-way output index 1) is muted by its per-output commission mute, and
    # the per-role startup mute is gone. Protective HP + limiter still wrap it.
    assert "as_out1_commission_mute" in text
    assert "as_tweeter_startup_mute" not in text
    assert "as_tweeter_startup_limiter" in text
    assert loaded["status"] == "staged"
    assert loaded["software_guard"]["passed"] is True


def test_stage_protected_startup_config_blocks_incomplete_software_guard_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    original_emit = staging_mod.emit_active_speaker_commissioning_config

    def corrupt_tweeter_mute(*args, **kwargs):
        # Simulate an audible tweeter: unmute its per-output commission mute. The
        # tweeter is mono 2-way output index 1, so flip as_out1_commission_mute.
        text = original_emit(*args, **kwargs)
        text = text.replace(
            (
                "  as_out1_commission_mute:\n"
                "    type: Gain\n"
                "    parameters: { gain: -120.0000, inverted: false, mute: true }"
            ),
            (
                "  as_out1_commission_mute:\n"
                "    type: Gain\n"
                "    parameters: { gain: -120.0000, inverted: false, mute: false }"
            ),
        )
        out_path = kwargs.get("out_path")
        if out_path is not None:
            Path(out_path).write_text(text, encoding="utf-8")
        return text

    monkeypatch.setattr(
        staging_mod,
        "emit_active_speaker_commissioning_config",
        corrupt_tweeter_mute,
    )
    payload = stage_protected_startup_config(
        _topology(protection_status="software_guard_requested"),
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )

    assert payload["status"] == "blocked"
    assert payload["software_guard"]["passed"] is False
    assert payload["software_guard"]["checks"]["startup_muted"] is False
    assert "software_tweeter_guard_incomplete" in {
        issue["code"] for issue in payload["issues"]
    }


def test_stage_protected_startup_config_blocks_noncontiguous_outputs(
    tmp_path: Path,
) -> None:
    raw = _topology().to_dict()
    raw["speaker_groups"][0]["channels"][1]["physical_output_index"] = 3
    topology = OutputTopology.from_mapping(raw)

    payload = stage_protected_startup_config(
        topology,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )

    assert payload["status"] == "blocked"
    assert "active_outputs_must_be_contiguous" in {
        issue["code"] for issue in payload["issues"]
    }


def test_stage_protected_startup_config_blocks_swapped_role_outputs(
    tmp_path: Path,
) -> None:
    raw = _topology().to_dict()
    raw["speaker_groups"][0]["channels"][0]["physical_output_index"] = 1
    raw["speaker_groups"][0]["channels"][1]["physical_output_index"] = 0
    topology = OutputTopology.from_mapping(raw)

    payload = stage_protected_startup_config(
        topology,
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    role_order_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "active_output_role_order"
    )

    assert payload["status"] == "blocked"
    assert "active_outputs_must_match_role_order" in {
        issue["code"] for issue in payload["issues"]
    }
    assert role_order_gate["passed"] is False
    assert "woofer on DAC output 1" in role_order_gate["message"]


def test_stage_protected_startup_config_boot_candidate_is_fully_muted(
    tmp_path: Path,
) -> None:
    # Crash-recovery invariant: the staged boot config has EVERY active output
    # muted. A reboot partway through commissioning must land everything-muted,
    # never a driver unmuted at level. Per-driver unmute is a transient runtime
    # load, never the frozen boot config.
    out = tmp_path / "active_staged.yml"
    payload = stage_protected_startup_config(
        _topology(),
        config_path=out,
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    muted_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "staged_candidate_fully_muted"
    )

    assert payload["status"] == "staged"
    assert muted_gate["passed"] is True
    parsed = yaml_lib.safe_load(out.read_text(encoding="utf-8"))
    commission_mutes = {
        name: spec
        for name, spec in parsed["filters"].items()
        if name.endswith("_commission_mute")
    }
    assert commission_mutes  # the production graph carries a per-output mute mask
    assert all(
        spec["parameters"]["mute"] is True for spec in commission_mutes.values()
    )


def test_stage_protected_startup_config_blocks_unmuted_boot_candidate(
    monkeypatch,
    tmp_path: Path,
) -> None:
    # Crash-recovery guard, blocking direction: if the staged boot config is NOT
    # fully muted, staging must fail closed. Use a physically-protected topology
    # so the software guard is not computed and only the fully-muted gate fires —
    # isolating this guard from the software-guard path.
    original_emit = staging_mod.emit_active_speaker_commissioning_config

    def unmute_one_output(*args, **kwargs):
        text = original_emit(*args, **kwargs).replace(
            (
                "  as_out0_commission_mute:\n"
                "    type: Gain\n"
                "    parameters: { gain: -120.0000, inverted: false, mute: true }"
            ),
            (
                "  as_out0_commission_mute:\n"
                "    type: Gain\n"
                "    parameters: { gain: -120.0000, inverted: false, mute: false }"
            ),
        )
        out_path = kwargs.get("out_path")
        if out_path is not None:
            Path(out_path).write_text(text, encoding="utf-8")
        return text

    monkeypatch.setattr(
        staging_mod,
        "emit_active_speaker_commissioning_config",
        unmute_one_output,
    )
    payload = stage_protected_startup_config(
        _topology(protection_status="present"),
        config_path=tmp_path / "active_staged.yml",
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )
    muted_gate = next(
        gate for gate in payload["required_gates"]
        if gate["id"] == "staged_candidate_fully_muted"
    )
    codes = {issue["code"] for issue in payload["issues"]}

    assert payload["status"] == "blocked"
    assert muted_gate["passed"] is False
    assert "staged_config_not_fully_muted" in codes
    # Physical protection: the software guard never ran, so this is the gate that
    # caught the unmuted output.
    assert "software_tweeter_guard_incomplete" not in codes


def test_software_guard_evidence_passes_for_muted_tweeter_outputs() -> None:
    # Software guard now proves the tweeter is muted via its per-output commission
    # mute, not a per-role startup mute. A fully-muted commissioning config passes.
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("stereo"))
    yaml = emit_active_speaker_commissioning_config(
        preset,
        playback_device="hw:CARD=DAC8x,DEV=0",
        audible_outputs=frozenset(),
    )
    evidence = staging_mod._software_guard_evidence(yaml, preset=preset)

    assert evidence["passed"] is True
    assert evidence["checks"]["startup_muted"] is True
    assert evidence["checks"]["tweeter_pipeline_guarded"] is True
    # Stereo 2-way tweeters live on physical outputs 1 and 3.
    assert evidence["tweeter_channels"] == [1, 3]


def test_software_guard_evidence_blocks_audible_tweeter_output() -> None:
    # Unmute one tweeter output (index 1): a single audible tweeter must fail the
    # software guard's startup_muted check, even though output 3 stays muted.
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("stereo"))
    yaml = emit_active_speaker_commissioning_config(
        preset,
        playback_device="hw:CARD=DAC8x,DEV=0",
        audible_outputs={1},
    )
    evidence = staging_mod._software_guard_evidence(yaml, preset=preset)

    assert evidence["checks"]["startup_muted"] is False
    assert evidence["passed"] is False


def test_physical_protection_staged_config_reads_as_muted_via_fallback(
    tmp_path: Path,
) -> None:
    # A physically-protected candidate carries no software_guard block, so
    # path_safety._startup_muted_by_candidate falls back to scanning the staged
    # YAML. The single-audio-path commissioning config mutes via per-output
    # `as_out{idx}_commission_mute`; the fallback must read that as "startup
    # muted" or a physically-protected speaker's startup-load preflight would
    # wrongly report the boot config as unmuted.
    out = tmp_path / "active_staged.yml"
    payload = stage_protected_startup_config(
        _topology(protection_status="present"),
        config_path=out,
        metadata_path=tmp_path / "active_staged.json",
        validate=_valid_config,
        created_at="2026-06-03T12:00:00Z",
    )

    assert payload["status"] == "staged"
    assert payload["software_guard"] == {}  # physical protection: no software guard
    assert _startup_muted_by_candidate(payload) is True


def test_all_commission_mutes_engaged_requires_pipeline_wiring() -> None:
    # The always-on crash-recovery gate must verify each per-output mute is not
    # just DEFINED muted but actually WIRED into the pipeline on its channel.
    # A mute filter that is defined (-120 dB, muted) but whose pipeline step is
    # missing must fail closed — otherwise the gate trusts emitter lockstep.
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("stereo"))
    yaml = emit_active_speaker_commissioning_config(
        preset, playback_device="hw:CARD=DAC8x,DEV=0", audible_outputs=frozenset()
    )
    assert staging_mod._all_commission_mutes_engaged(yaml, preset=preset) is True
    # Drop output 0's commission-mute PIPELINE step; keep its filter definition.
    unwired = yaml.replace(
        "  - type: Filter\n    channels: [0]\n    names: [as_out0_commission_mute]",
        "",
    )
    assert unwired != yaml  # the step existed and was removed
    assert "as_out0_commission_mute:" in unwired  # definition still present + muted
    assert staging_mod._all_commission_mutes_engaged(unwired, preset=preset) is False


def test_software_guard_evidence_blocks_when_tweeter_protection_unwired() -> None:
    # Isolate tweeter_pipeline_guarded: remove the tweeter's per-role protective
    # HP + limiter pipeline step while leaving every mute intact. startup_muted
    # stays True, but the HP/limiter no longer wrap the tweeter channel, so the
    # structural guard (and therefore `passed`) must fail.
    preset = ActiveSpeakerPreset.from_mapping(_two_way_preset("stereo"))
    yaml = emit_active_speaker_commissioning_config(
        preset, playback_device="hw:CARD=DAC8x,DEV=0", audible_outputs=frozenset()
    )
    baseline = staging_mod._software_guard_evidence(yaml, preset=preset)
    assert baseline["checks"]["tweeter_pipeline_guarded"] is True
    # The tweeter per-role chain is the pipeline Filter on channels [1, 3] whose
    # names begin with as_tweeter_protective_hp (unique to the tweeter chain).
    stripped, n = re.subn(
        r" {2}- type: Filter\n {4}channels: \[1, 3\]\n"
        r" {4}names: \[as_tweeter_protective_hp[^\]]*\]\n",
        "",
        yaml,
    )
    assert n == 1  # exactly the tweeter HP/limiter chain step was removed
    evidence = staging_mod._software_guard_evidence(stripped, preset=preset)
    assert evidence["checks"]["tweeter_pipeline_guarded"] is False
    assert evidence["checks"]["startup_muted"] is True  # mutes untouched
    assert evidence["passed"] is False


# --------------------------------------------------------------------------
# #2518 — the staged startup anchor PAIR has one lock, taken by both writers.
#
# The pair is the all-muted startup graph plus the staged metadata that LOCATES
# it. Neither writer publishes both halves in one filesystem operation, so
# without mutual exclusion an interleaved run can leave one graph's metadata
# over another's bytes. #2285's convergence design would add unattended
# invocations of the CLI writer, turning a human-vs-human race into a
# human-vs-machine one — which is why it names this lock its prerequisite
# rather than a nicety.
#
# The CLI writer's half of the same promise — including the claim that BOTH
# writers take the SAME file — is pinned in tests/test_ring_active_endpoint.py.
# --------------------------------------------------------------------------


def _probe_anchor_lock(lock_path: Path) -> bool:
    """True when some open file description already holds ``lock_path``.

    A second descriptor on the same file is a faithful probe even inside the
    writer's own process — flock(2): "If a process uses open(2) ... to obtain
    more than one file descriptor for the same file, these file descriptors are
    treated independently by flock()."
    """
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def test_stage_protected_startup_config_holds_the_lock_across_both_halves(
    monkeypatch, tmp_path: Path
) -> None:
    """The lock spans the graph write AND the metadata write, not one of them.

    A hold covering only one half would leave exactly the window #2518
    describes: one writer's bytes on disk under the other's metadata. So the
    probe runs AT each write — the graph emit and the metadata publish — and
    both must find the pair's lock already held. The post-call probe is the
    positive control: it proves the probe can report `free`, so the two `True`s
    are evidence of a hold rather than of a probe that always says yes.
    """
    out = tmp_path / "active_staged.yml"
    meta = tmp_path / "active_staged.json"
    lock_path = staging_mod.staged_anchor_lock_path(out)
    held_at: dict[str, bool] = {}

    real_emit = staging_mod.emit_active_speaker_commissioning_config
    real_write_json = staging_mod.atomic_write_json

    def _emit(*args, **kwargs):
        held_at["graph"] = _probe_anchor_lock(lock_path)
        return real_emit(*args, **kwargs)

    def _write_json(path, payload, **kwargs):
        if Path(path) == meta:
            held_at["metadata"] = _probe_anchor_lock(lock_path)
        return real_write_json(path, payload, **kwargs)

    monkeypatch.setattr(
        staging_mod, "emit_active_speaker_commissioning_config", _emit
    )
    monkeypatch.setattr(staging_mod, "atomic_write_json", _write_json)

    payload = stage_protected_startup_config(
        _topology(),
        config_path=out,
        metadata_path=meta,
        validate=_valid_config,
    )

    assert payload["status"] == "staged"
    assert held_at == {"graph": True, "metadata": True}, held_at
    # Positive control: released on the way out, so the probe is discriminating.
    assert _probe_anchor_lock(lock_path) is False


def test_stage_protected_startup_config_refuses_a_held_anchor_and_writes_nothing(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    """A contending stage waits a BOUNDED time, then refuses without writing.

    Bounded because this call sits on a /sound/ web request, where an
    open-ended block is a hung page. Writing nothing because a refusal that
    published a blocked payload over the live metadata would BE the corruption
    the lock exists to prevent.
    """
    out = tmp_path / "active_staged.yml"
    meta = tmp_path / "active_staged.json"
    meta.write_text('{"status": "previous"}', encoding="utf-8")
    lock_path = staging_mod.staged_anchor_lock_path(out)
    monkeypatch.setattr(staging_mod, "STAGED_ANCHOR_LOCK_TIMEOUT_SEC", 0.2)

    holder = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(holder, b"pid 4242 the-other-writer\n")
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=staging_mod.logger.name):
            payload = stage_protected_startup_config(
                _topology(),
                config_path=out,
                metadata_path=meta,
                validate=_valid_config,
            )
        waited = time.monotonic() - started
    finally:
        os.close(holder)

    assert payload["status"] == "blocked"
    assert [issue["code"] for issue in payload["issues"]] == [
        "staged_config_anchor_lock_contended"
    ]
    # Bounded: the wait is the configured timeout, not the caller's patience.
    assert waited < 5.0, waited
    # Neither half moved.
    assert not out.exists()
    assert meta.read_text(encoding="utf-8") == '{"status": "previous"}'
    # Loud: one stable event naming the holder and the bound it exceeded.
    contended = [
        rec.getMessage()
        for rec in caplog.records
        if "staged_anchor_lock_contended" in rec.getMessage()
    ]
    assert len(contended) == 1, caplog.text
    assert "the-other-writer" in contended[0]
    assert "timeout_ms=200" in contended[0]


def test_staged_anchor_lock_is_released_when_its_holder_is_killed(
    tmp_path: Path,
) -> None:
    """flock is dropped by the kernel on process death — the reason it is flock.

    Both writers run in short-lived contexts that can be killed mid-write: a
    CLI invocation inside a deploy, a web request, either OOM-killed on a
    1 GB Pi. A lock that outlived its holder would strand the anchor and leave
    the box unable to stage a boot graph at all, which is strictly worse than
    the stale evidence #2518 bounds. The live-holder assertion is the positive
    control: it proves the acquisition below succeeds because the lock was
    RELEASED, not because it was never taken.
    """
    anchor = tmp_path / "anchor.yml"
    lock_path = staging_mod.staged_anchor_lock_path(anchor)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,os,sys,time\n"
            "fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "sys.stdout.write('locked\\n'); sys.stdout.flush()\n"
            "time.sleep(120)\n",
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        assert _probe_anchor_lock(lock_path) is True  # positive control
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=30)
        # The kernel drops the flock with the dying process's descriptor. Poll
        # briefly: the drop is not synchronous with `wait()` returning.
        deadline = time.monotonic() + 5.0
        while _probe_anchor_lock(lock_path) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _probe_anchor_lock(lock_path) is False
        # ...and a real writer can take it again.
        with staging_mod.staged_anchor_lock(anchor, source="test", timeout_sec=1.0):
            assert _probe_anchor_lock(lock_path) is True
    finally:
        if child.poll() is None:  # pragma: no cover - only after a failed assert
            child.kill()
            child.wait(timeout=30)
        if child.stdout is not None:
            child.stdout.close()


def test_staged_anchor_lock_fails_open_when_the_lock_file_cannot_be_opened(
    tmp_path: Path, caplog
) -> None:
    """An unopenable lock path must not brick staging — warn and proceed.

    Mirrors `jasper.fanin.coupling_reconcile._acquire_entry_lock`. The
    asymmetry decides it: proceeding unserialized costs at worst the stale
    evidence #2518 already bounds, while refusing costs the box its ability to
    stage a boot anchor at all.
    """
    out = tmp_path / "active_staged.yml"
    meta = tmp_path / "active_staged.json"
    # A directory at the lock path makes `os.open(..., O_RDWR|O_CREAT)` raise
    # EISDIR — the shape a provisioning fault takes.
    staging_mod.staged_anchor_lock_path(out).mkdir()

    with caplog.at_level(logging.WARNING, logger=staging_mod.logger.name):
        payload = stage_protected_startup_config(
            _topology(),
            config_path=out,
            metadata_path=meta,
            validate=_valid_config,
        )

    assert payload["status"] == "staged"
    assert out.exists() and meta.exists()
    assert any(
        "staged_anchor_lock_unavailable" in rec.getMessage()
        for rec in caplog.records
    ), caplog.text
