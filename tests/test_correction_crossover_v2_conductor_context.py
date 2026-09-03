# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pins the W6 hardware-run bug: ``resolve_conductor_context`` must resolve
the active playback device through
:func:`jasper.active_speaker.playback_route.resolve_active_playback_device`,
never a nonexistent ``topology.playback_device`` attribute.

Before this fix, ``OutputTopology`` (a frozen dataclass with no
``playback_device`` field) always made
``getattr(topology, "playback_device", None)`` resolve to ``None``, so every
call to ``resolve_conductor_context`` — the shared context builder behind
both ``POST /crossover/v2/session`` and ``POST /crossover/v2/verify`` —
refused unconditionally with "the active output device is not declared".
This was 100% reproducible on real hardware and had zero test coverage: every
existing endpoint test short-circuits before reaching this code (an empty or
inactive ``status`` refuses earlier), so the dead seam shipped silently.

These tests build a REAL, verified :class:`~jasper.output_topology.OutputTopology`
(mirroring the fixture builder in ``tests/test_active_speaker_playback_route.py``)
rather than a mock with a hand-set ``playback_device`` attribute — a mock
would not have caught the original bug, since the mock would happily answer
whatever attribute the test author set on it.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from jasper.active_speaker import commission_wiring, crossover_v2_flow, design_draft
from jasper.active_speaker import driver_safety as driver_safety_mod
from jasper.active_speaker.crossover_v2.refusal_copy import (
    REASON_MEASUREMENT_TARGETS_MISSING,
    REASON_REGISTRY,
    REASON_SPEAKER_SHAPE_UNSUPPORTED,
)
from jasper.active_speaker import excitation_safety_plan as excitation_safety_plan_mod
from jasper.active_speaker.tone_plan import load_active_speaker_preset
from jasper.audio_hardware.dac import HIFIBERRY_DAC8X
from jasper.audio_measurement.excitation_admission import FrequencyBand
from jasper.output_topology import (
    ACTIVE_PLAYBACK_DEVICE_ENV,
    OUTPUT_TOPOLOGY_KIND,
    OutputTopology,
)
from jasper.web import correction_crossover_v2 as v2host
from tests.crossover_v2_fixtures import fake_measurement_mic

_TWO_WAY_GROUP = [{
    "id": "mono",
    "label": "Mono",
    "kind": "mono",
    "mode": "active_2_way",
    "channels": [
        {"role": "woofer", "physical_output_index": 0, "identity_verified": True},
        {
            "role": "tweeter",
            "physical_output_index": 1,
            "identity_verified": True,
            "startup_muted": True,
            "protection_required": True,
            "protection_status": "present",
        },
    ],
}]


def _topology(device_id: str, count: int, *, card_id: str | None = None) -> OutputTopology:
    """Mirror the fixture builder in tests/test_active_speaker_playback_route.py."""

    hardware: dict[str, Any] = {
        "device_id": device_id,
        "device_label": "Test device",
        "physical_output_count": count,
    }
    if card_id:
        hardware["card_id"] = card_id
    return OutputTopology.from_mapping({
        "artifact_schema_version": 1,
        "kind": OUTPUT_TOPOLOGY_KIND,
        "topology_id": "t",
        "name": "n",
        "status": "draft",
        "hardware": hardware,
        "speaker_groups": _TWO_WAY_GROUP,
        "routing": {"mono_group_id": "mono"},
    })


def _status() -> dict[str, Any]:
    return {
        "active": True,
        "setup": {"status": "ready"},
        "targets": {
            "drivers": [
                {"role": "woofer", "target_fingerprint": "fp-woofer"},
                {"role": "tweeter", "target_fingerprint": "fp-tweeter"},
            ],
        },
    }


# The REAL derivation functions, stashed at import time BEFORE the autouse
# stub below replaces the module attributes — the W6.5 context-caps test
# restores them so the true resolver runs against a real profile.
_REAL_RESOLVE_PRESET = commission_wiring.resolve_capture_preset
_REAL_RESOLVE_CEILINGS = excitation_safety_plan_mod.resolve_driver_excitation_ceilings
_REAL_SWEEP_DURATION_LIMIT = excitation_safety_plan_mod.effective_sweep_duration_limit_s
_REAL_DERIVE_SESSION_VOLUME = crossover_v2_flow.derive_session_volume_db


@pytest.fixture(autouse=True)
def _stub_non_topology_inputs(monkeypatch):
    """Stub every conductor-context input EXCEPT topology/playback-device
    resolution — the bug under test. Real preset, driver-safety, and volume
    derivation shapes are exercised elsewhere (tests/test_crossover_v2_conductor.py);
    stubbing them here keeps this module focused on the one seam that shipped
    broken and untested.
    """
    # The preparers' source gate (#2662 W2b S3) asks the relay-configured
    # question before any bundle opens; this suite models the fleet default.
    monkeypatch.setenv("JASPER_CAPTURE_RELAY_BASE", "https://relay.test")
    preset = load_active_speaker_preset()  # bundled 2-way default, real object
    monkeypatch.setattr(commission_wiring, "resolve_capture_preset", lambda topo: preset)
    # Same rule as every stub here: this module tests the topology/playback-
    # device seam, not the driver-safety contract. The profile does have to
    # carry the confirmed per-role protection, because R15's session open
    # resolves it (``confirmed_protection_sections``) before any capture — a
    # bare ``{}`` refuses the session for a reason unrelated to this test. The
    # resolver itself is covered in tests/test_active_speaker_branch_chain.py.
    monkeypatch.setattr(
        design_draft, "load_design_draft",
        lambda **kw: {"driver_safety_profile": {
            "targets": [
                {
                    "role": role,
                    "target_fingerprint": f"fp-{role}",
                    "required_protection_filters": [{
                        "kind": kind,
                        "cutoff_hz": cutoff,
                        "minimum_slope_db_per_octave": 24.0,
                    }],
                }
                for role, kind, cutoff in (
                    ("woofer", "lowpass", 6000.0), ("tweeter", "highpass", 300.0),
                )
            ],
        }},
    )
    # W6.11 landed a real crossover-preview ensure step at the top of
    # resolve_conductor_context (before resolve_capture_preset runs). It reads
    # real global state (load_output_topology, the on-disk design draft) that
    # this module deliberately does not stub — stubbing it here (like
    # resolve_capture_preset above) keeps this file focused on the ONE seam
    # under test; the ensure step itself is covered in
    # tests/test_correction_crossover_v2_endpoints.py.
    monkeypatch.setattr(v2host, "ensure_crossover_preview_ready", lambda: None)
    # Same rule as the other stubs: this module tests the topology/playback-
    # device seam, not the driver-safety contract. The session-open confirmation
    # gate (issue #1821) is covered against the REAL evaluator in
    # tests/test_crossover_v2_profile_not_confirmed.py; here the stub profile is
    # a bare ``{}``, so without this the gate would refuse every case.
    monkeypatch.setattr(
        driver_safety_mod,
        "evaluate_driver_safety_profile",
        lambda profile, topology: driver_safety_mod.DriverSafetyProfileEvaluation(
            "confirmed", True, "f" * 64, (),
        ),
    )
    monkeypatch.setattr(
        excitation_safety_plan_mod,
        "resolve_driver_excitation_ceilings",
        lambda safety_profile, fingerprint, **kw: (FrequencyBand(20.0, 20000.0), 90.0),
    )
    # Same rule, duration half: this module tests the topology/playback-device
    # seam, and the stub profile above carries no ``level_duration_limits`` for
    # the real reader to answer from. The REAL reader runs in the two probes
    # below that restore it, both of which supply a real confirmed profile.
    monkeypatch.setattr(
        excitation_safety_plan_mod,
        "effective_sweep_duration_limit_s",
        lambda safety_profile, fingerprint: 6.0,
    )
    monkeypatch.setattr(
        crossover_v2_flow,
        "derive_session_volume_db",
        lambda safety_profile, fps, **kw: -20.0,
    )
    monkeypatch.delenv(ACTIVE_PLAYBACK_DEVICE_ENV, raising=False)
    yield


def _patch_topology(monkeypatch, topology: OutputTopology) -> None:
    from jasper import output_topology as output_topology_mod

    monkeypatch.setattr(output_topology_mod, "load_output_topology", lambda *a, **k: topology)


# --------------------------------------------------------------------------- #
# resolve_conductor_context — unit tests
# --------------------------------------------------------------------------- #


def _passive_status(topology: OutputTopology) -> dict[str, Any]:
    """A subless passive box's status, from the PRODUCTION derivation.

    A summed target is a claim about two branches, so ``active`` is honestly
    False and there is no ``setup`` block — both are questions about an active
    crossover this speaker does not have.
    """
    from jasper.active_speaker import measurement

    summed = measurement.active_summed_targets(topology)
    return {
        "active": bool(summed),
        "targets": {
            "drivers": measurement.active_driver_targets(topology), "summed": summed,
        },
    }


@pytest.fixture
def _passive_topology(monkeypatch) -> OutputTopology:
    """The autouse stubs, re-pointed at a ``full_range_passive`` box."""
    from tests.active_speaker_fixtures import mono_output_topology

    topology = mono_output_topology(mode="full_range_passive")
    _patch_topology(monkeypatch, topology)
    # The REAL resolver over the real topology: never the bundled 2-way preset
    # the autouse stub hands every other case here.
    monkeypatch.setattr(commission_wiring, "resolve_capture_preset", _REAL_RESOLVE_PRESET)
    monkeypatch.setenv(ACTIVE_PLAYBACK_DEVICE_ENV, "hw:Lab")
    return topology


def test_a_subless_passive_speaker_opens_a_session(monkeypatch, _passive_topology):
    """The gates that refused a passive box are answered by its shape."""
    # A passive box compiles no crossover preview; asking for one would refuse a
    # session it can serve.
    monkeypatch.setattr(
        v2host, "ensure_crossover_preview_ready",
        lambda: pytest.fail("a passive topology must not need a crossover preview"),
    )

    context = v2host.resolve_conductor_context(_passive_status(_passive_topology))

    assert context.preset.way_count == 1
    assert context.preset.crossover_regions == ()
    assert context.fc_hz is None
    assert [rb.role for rb in context.roles_bands] == ["full_range"]
    assert context.role_channels == {"full_range": 0}
    assert set(context.driver_caps_dbfs) == {"full_range"}


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        pytest.param("preset", REASON_SPEAKER_SHAPE_UNSUPPORTED, id="no_walk"),
        pytest.param(
            "targets", REASON_MEASUREMENT_TARGETS_MISSING, id="nothing_to_measure",
        ),
    ],
)
def test_the_session_refuses_by_a_registered_code(
    monkeypatch, _passive_topology, mutate, expected_code,
):
    status = _passive_status(_passive_topology)
    if mutate == "preset":
        from jasper.active_speaker.profile import ActiveSpeakerPreset
        from tests.test_active_speaker_profile import _three_way_preset

        monkeypatch.setattr(
            commission_wiring, "resolve_capture_preset",
            lambda topo: ActiveSpeakerPreset.from_mapping(_three_way_preset("mono")),
        )
    else:
        status["targets"] = {"drivers": [], "summed": []}

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.resolve_conductor_context(status)

    assert excinfo.value.code == expected_code
    assert expected_code in REASON_REGISTRY



def test_resolves_real_playback_device_from_a_verified_topology(monkeypatch):
    """A real, resolvable topology (DAC8x with an active outputd lane) must
    produce a non-empty playback_device — the exact case that was refusing
    unconditionally on hardware."""
    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)

    context = v2host.resolve_conductor_context(_status())

    assert context.playback_device
    assert isinstance(context.playback_device, str)
    assert context.topology is topo


def test_a_stale_baseline_topology_opens_the_session_and_says_so(
    monkeypatch, caplog,
):
    """Wave 7j: the topology-staleness block becomes a disclosure.

    A rotated `topology_config_fingerprint` used to make `setup.status`
    `blocked`, and this chokepoint refuses anything that is not `ready` — so
    renaming a speaker group, a display-only string that reaches no clamp and
    no emitted filter, refused the measure session outright. The session now
    opens and the fact is said once, here, rather than on every `/state` poll.

    A declared fact that DOES gate still gates, one step downstream: this test
    stops at `resolve_conductor_context`'s setup check, and the driver-safety
    evaluation below it is untouched.
    """
    from jasper.active_speaker._common import BASELINE_TOPOLOGY_CHANGED

    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)
    status = _status()
    status["setup"] = {
        "status": "ready",
        "issues": [{
            "severity": "warning",
            "code": BASELINE_TOPOLOGY_CHANGED,
            "message": "topology changed since the applied baseline",
        }],
    }

    with caplog.at_level(logging.WARNING):
        context = v2host.resolve_conductor_context(status)

    assert context.topology is topo
    assert "event=correction.crossover_v2_baseline_topology_stale" in caplog.text
    assert f"code={BASELINE_TOPOLOGY_CHANGED}" in caplog.text


def test_refuses_when_the_layout_has_no_resolvable_playback_route(monkeypatch):
    """An unrecognized DAC id resolves to MISSING_SOURCE (no playback route) —
    the ONE case that should still raise the typed refusal."""
    topo = _topology("generic_single_dac", 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)

    with pytest.raises(v2host.CrossoverV2Refused) as excinfo:
        v2host.resolve_conductor_context(_status())

    assert "active output device is not declared" in str(excinfo.value)


def test_explicit_env_playback_device_is_honored(monkeypatch):
    """The explicit-device escape hatch (JASPER_ACTIVE_SPEAKER_PLAYBACK_DEVICE,
    e.g. lab/CI hardware) must still resolve through the same seam."""
    topo = _topology("generic_single_dac", 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)
    monkeypatch.setenv(ACTIVE_PLAYBACK_DEVICE_ENV, "hw:Lab")

    context = v2host.resolve_conductor_context(_status())

    assert context.playback_device == "hw:Lab"


# --------------------------------------------------------------------------- #
# driver_class_by_role resolver seam (#1668 PR-D)
# --------------------------------------------------------------------------- #


def test_driver_class_by_role_resolver_default_pins_todays_empty_behavior(monkeypatch):
    """The REAL (unstubbed) _resolve_driver_class_by_role — a #1665 stub —
    must return {} today, so context.driver_class_by_role is empty and
    CrossoverV2Session's own ``.get(role, "unknown")`` read falls back to
    "unknown" for every role, exactly as it did before this resolver
    existed (test_crossover_v2_conductor.py's own
    test_happy_path_..._driver_class == "unknown" assertions)."""
    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)

    context = v2host.resolve_conductor_context(_status())

    assert context.driver_class_by_role == {}
    assert v2host._resolve_driver_class_by_role(context.preset) == {}


def test_driver_class_by_role_fake_resolver_injection_reaches_the_context(monkeypatch):
    """A fake resolver (simulating #1665's future component-entry-backed
    implementation) proves the WIRING is real, not dead code: whatever
    _resolve_driver_class_by_role returns lands unchanged on
    context.driver_class_by_role, which is the exact value both conductor
    construction sites (prepare_v2_session's .hydrate(...) and
    the verify-only prepare's CrossoverV2Session(...)) pass straight through as
    the ctor's driver_class_by_role= — and
    test_driver_class_by_role_ctor_param_threads_into_the_fit
    (tests/test_crossover_v2_conductor.py) independently proves THAT ctor
    param reaches compose_envelope's driver_class= argument. Together the
    two tests cover the full resolver -> context -> conductor ->
    compose_envelope chain without one end-to-end test spanning both
    modules' heavy session-open seams."""
    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)
    injected = {"tweeter": "compression_horn", "woofer": "unknown"}
    monkeypatch.setattr(v2host, "_resolve_driver_class_by_role", lambda preset: injected)

    context = v2host.resolve_conductor_context(_status())

    assert context.driver_class_by_role == injected
    assert context.driver_class_by_role is injected


def test_context_caps_equal_admission_caps_with_jts3_declaration(monkeypatch):
    """The W6.5 gate blocker probe: ``resolve_conductor_context`` must resolve
    caps on the proven-HP path with the declaration's sensitivities — the
    reviewer proved the derived ceiling was inert here (context caps
    {tweeter: -65} vs admission caps {tweeter: -35} as the derivation stood
    then, before the absolute hedge was retired 2026-08-20; composed CHECK
    pilot -65.01). Runs the REAL resolver against a REAL confirmed safety profile
    plus a declaration-shaped draft, and asserts the context caps EQUAL what
    admission resolves with the same inputs — one derivation, two consumers.
    """
    from jasper.active_speaker.driver_safety import build_driver_safety_profile
    from jasper.active_speaker.measurement import active_driver_targets

    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)

    def _driver(role, peak, filters, diameter, **extra):
        return {
            "target_id": f"mono:{role}",
            "role": role,
            "model": f"model-{role}",
            "hard_excitation_band_hz": [500, 20_000],
            "measurement_band_hz": [500, 10_000],
            # #2603: the tweeter declares its low limit once, and its hard
            # floor derives from it rather than sharing the woofer's 500 Hz.
            **({"recommended_highpass_hz": 1500} if role == "tweeter" else {}),
            "level_duration_limits": {
                "max_effective_peak_dbfs": peak,
                "max_sweep_duration_s": 6,
                "max_repeat_count": 3,
                "minimum_cooldown_s": 0,
            },
            "required_protection_filters": filters,
            "cabinet": {
                "enclosure_kind": "sealed",
                "radiator_count": 1,
                "effective_radiating_diameter_mm": diameter,
                **extra,
            },
        }

    # JTS3 shape: woofer cap -8; tweeter cap left at the -65 class-default seed.
    settings = {
        "drivers": [
            _driver(
                "woofer", -8,
                [{"kind": "lowpass", "cutoff_hz": 3000, "minimum_slope_db_per_octave": 24}],
                132, baffle_width_mm=210,
            ),
            _driver(
                "tweeter", -65,
                [{"kind": "highpass", "cutoff_hz": 5000, "minimum_slope_db_per_octave": 24}],
                25,
            ),
        ],
        "crossover_candidates": [],
    }
    profile = build_driver_safety_profile(
        topo,
        manual_settings=settings,
        driver_research=None,
        saved_at="2026-07-19T12:00:00Z",
    )
    targets = {t["role"]: t["target_fingerprint"] for t in active_driver_targets(topo)}
    status = {
        "active": True,
        "setup": {"status": "ready"},
        "targets": {
            "drivers": [
                {"role": role, "target_fingerprint": fingerprint}
                for role, fingerprint in targets.items()
            ],
        },
    }
    # The declaration section as it lives on JTS3's persisted design draft:
    # sensitivities under manual_settings.drivers (83.3 / 108.5).
    draft = {
        "driver_safety_profile": profile,
        "manual_settings": {
            "drivers": [
                {"role": "woofer", "sensitivity_db_2v83_1m": 83.3},
                {"role": "tweeter", "sensitivity_db_2v83_1m": 108.5},
            ],
            "crossover_candidates": [],
        },
    }
    monkeypatch.setattr(design_draft, "load_design_draft", lambda **kw: draft)
    # Restore the REAL derivation functions over the autouse stubs.
    monkeypatch.setattr(
        excitation_safety_plan_mod,
        "resolve_driver_excitation_ceilings",
        _REAL_RESOLVE_CEILINGS,
    )
    monkeypatch.setattr(
        excitation_safety_plan_mod,
        "effective_sweep_duration_limit_s",
        _REAL_SWEEP_DURATION_LIMIT,
    )
    monkeypatch.setattr(
        crossover_v2_flow, "derive_session_volume_db", _REAL_DERIVE_SESSION_VOLUME
    )

    context = v2host.resolve_conductor_context(status)

    # The derived {-8, -33.2}, not the pre-fix {-8, -65}. -33.2 is the
    # sensitivity arithmetic outright (-8 less the 25.2 dB delta); the
    # provisional -35 dBFS absolute hedge over it was retired 2026-08-20.
    assert context.driver_caps_dbfs == {
        "woofer": -8.0,
        "tweeter": pytest.approx(-33.2),
    }
    assert context.declared_sensitivities == {"woofer": 83.3, "tweeter": 108.5}
    assert context.session_volume_db == -20.0
    # Probe: context caps == admission caps, per role, from the same inputs.
    for role, fingerprint in targets.items():
        _band, admission_cap = _REAL_RESOLVE_CEILINGS(
            profile,
            fingerprint,
            program_admission=True,
            declared_sensitivities=context.declared_sensitivities,
        )
        assert context.driver_caps_dbfs[role] == pytest.approx(admission_cap)
    # Flat-linearization plan PR-4: the tweeter's confirmed measurement_band_hz
    # resolves onto the context — the field resolve_driver_excitation_ceilings
    # reads and validates internally but does not return.
    #
    # Its LOWER edge is 1500, not the 500 this fixture types, because #2603
    # clamps the analysis window up into the allowed band: a window cannot
    # honestly extend below the frequency the driver may not be excited under.
    # The upper edge is untouched — that half is still a declared fact.
    assert context.measurement_band_hz_by_role["tweeter"] == (1500.0, 10_000.0)
    # The DURATION half of the same one-derivation-two-consumers probe
    # (#2921): the MEASURE composer must be HANDED the very number the
    # admission gate will compare its sweeps against, never re-derive one.
    # This fixture declares 6 s for both drivers while the code-side table
    # gives the woofer 12 s and the tweeter 4 s, so the two roles resolve from
    # OPPOSITE halves of the min -- a restatement that dropped either half
    # would show up on one of them.
    from jasper.active_speaker.excitation_safety_plan import (
        effective_sweep_duration_limit_s,
    )

    assert context.driver_sweep_duration_limits_s == {
        "woofer": 6.0,
        "tweeter": 4.0,
    }
    for role, fingerprint in targets.items():
        assert context.driver_sweep_duration_limits_s[role] == (
            effective_sweep_duration_limit_s(profile, fingerprint)
        )


def test_a_role_with_no_resolvable_measurement_band_is_simply_absent(monkeypatch):
    """The autouse stub's design draft has no ``targets`` list at all
    (``{"driver_safety_profile": {}}``), so the REAL
    ``resolve_driver_measurement_band_hz`` this context resolves through
    cannot find a target record — this is a declared-metadata GAP, never a
    reason to refuse the whole measurement session (see
    ``resolve_conductor_context``'s own comment at the call site)."""
    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)

    context = v2host.resolve_conductor_context(_status())

    assert "tweeter" not in context.measurement_band_hz_by_role


def test_declared_driver_class_and_pad_reach_the_conductor_context(monkeypatch):
    """#1665 component entry, closing the seam #1668 PR-C left data-untested.

    PR-C's own conductor-level test
    (test_driver_class_by_role_ctor_param_threads_into_the_fit in
    test_crossover_v2_conductor.py) already proved "IF the ctor param is
    populated, it reaches compose_envelope." What it could NOT prove yet —
    because #1665 hadn't landed — is that a REAL design draft's declared
    driver_class actually reaches that ctor param. This test closes that
    half: runs the real resolver against a real confirmed safety profile
    plus a declaration-shaped draft (the same JTS3 shape as the sibling test
    above) and asserts driver_class_by_role is derived correctly. It also
    confirms declared_sensitivities now reads the PAD-FOLDED effective
    figure, not the naked one — the other half of #1665's resolver swap.
    """
    from jasper.active_speaker.driver_safety import build_driver_safety_profile
    from jasper.active_speaker.measurement import active_driver_targets

    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)

    def _driver(role, peak, filters, diameter, **extra):
        return {
            "target_id": f"mono:{role}",
            "role": role,
            "model": f"model-{role}",
            "hard_excitation_band_hz": [500, 20_000],
            "measurement_band_hz": [500, 10_000],
            # #2603: the tweeter declares its low limit once, and its hard
            # floor derives from it rather than sharing the woofer's 500 Hz.
            **({"recommended_highpass_hz": 1500} if role == "tweeter" else {}),
            "level_duration_limits": {
                "max_effective_peak_dbfs": peak,
                "max_sweep_duration_s": 6,
                "max_repeat_count": 3,
                "minimum_cooldown_s": 0,
            },
            "required_protection_filters": filters,
            "cabinet": {
                "enclosure_kind": "sealed",
                "radiator_count": 1,
                "effective_radiating_diameter_mm": diameter,
                **extra,
            },
        }

    settings = {
        "drivers": [
            _driver(
                "woofer", -8,
                [{"kind": "lowpass", "cutoff_hz": 3000, "minimum_slope_db_per_octave": 24}],
                132, baffle_width_mm=210,
            ),
            _driver(
                "tweeter", -65,
                [{"kind": "highpass", "cutoff_hz": 5000, "minimum_slope_db_per_octave": 24}],
                25,
            ),
        ],
        "crossover_candidates": [],
    }
    profile = build_driver_safety_profile(
        topo,
        manual_settings=settings,
        driver_research=None,
        saved_at="2026-07-19T12:00:00Z",
    )
    targets = {t["role"]: t["target_fingerprint"] for t in active_driver_targets(topo)}
    status = {
        "active": True,
        "setup": {"status": "ready"},
        "targets": {
            "drivers": [
                {"role": role, "target_fingerprint": fingerprint}
                for role, fingerprint in targets.items()
            ],
        },
    }
    draft = {
        "driver_safety_profile": profile,
        "manual_settings": {
            "drivers": [
                {"role": "woofer", "sensitivity_db_2v83_1m": 83.3},
                {
                    "role": "tweeter",
                    "sensitivity_db_2v83_1m": 108.5,
                    "driver_class": "compression_horn",
                    "pad": {"kind": "direct_db", "attenuation_db": -14.4},
                },
            ],
            "crossover_candidates": [],
        },
    }
    monkeypatch.setattr(design_draft, "load_design_draft", lambda **kw: draft)
    monkeypatch.setattr(
        excitation_safety_plan_mod,
        "resolve_driver_excitation_ceilings",
        _REAL_RESOLVE_CEILINGS,
    )
    monkeypatch.setattr(
        excitation_safety_plan_mod,
        "effective_sweep_duration_limit_s",
        _REAL_SWEEP_DURATION_LIMIT,
    )
    monkeypatch.setattr(
        crossover_v2_flow, "derive_session_volume_db", _REAL_DERIVE_SESSION_VOLUME
    )

    context = v2host.resolve_conductor_context(status)

    assert context.driver_class_by_role == {"tweeter": "compression_horn"}
    # 108.5 naked minus the declared -14.4 dB pad = 94.1 effective, not 108.5.
    assert context.declared_sensitivities == {
        "woofer": 83.3,
        "tweeter": pytest.approx(94.1),
    }


# --------------------------------------------------------------------------- #
# endpoint-level: the real resolver actually runs behind prepare_v2_session
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_v2_state(tmp_path, monkeypatch):

    v2host.set_state_path_for_tests(tmp_path / "v2_state.json")
    yield
    v2host.set_state_path_for_tests(None)
    v2host.set_volume_plan_for_tests(None)


def test_prepare_v2_session_runs_the_real_conductor_context_resolver(monkeypatch):
    """Existing endpoint tests in test_correction_crossover_v2_endpoints.py
    call prepare_v2_session with status={}, which refuses BEFORE reaching
    resolve_conductor_context — that's how the dead playback_device seam
    shipped with zero coverage. This drives prepare_v2_session with a full
    status against a real, resolvable topology so resolve_conductor_context
    actually runs; only the evidence-store bundle I/O (a different, already
    fail-soft seam) is stubbed."""
    topo = _topology(HIFIBERRY_DAC8X.id, 8, card_id="DAC8")
    _patch_topology(monkeypatch, topo)
    # The subject is the CONTEXT resolver, so the mic is named rather than
    # probed: no machine running this suite has one plugged in.
    monkeypatch.setattr(
        v2host, "_resolve_prepare_wired_mic", fake_measurement_mic,
    )
    monkeypatch.setattr(
        v2host, "open_v2_evidence_store", lambda topology: (object(), "sess-fake")
    )

    prepared = v2host.prepare_v2_session(
        {}, status=_status(), run_async=None, camilla_factory=None
    )

    assert prepared.label == v2host.V2_RELAY_KIND_SESSION
