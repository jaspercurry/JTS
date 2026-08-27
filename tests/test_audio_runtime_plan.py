# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from jasper import audio_runtime_plan as audio_plan
from jasper.audio_hardware.dac import (
    APPLE_USB_C_DONGLE_ID,
    HIFIBERRY_DAC8X_ID,
    HIFIBERRY_DAC8X_STUDIO_ID,
    latency_floor_for,
)
from jasper.audio_runtime_overrides import (
    DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
    RuntimeOverrideEntry,
)
from jasper.audio_runtime_plan import (
    AUDIO_RUNTIME_OVERRIDE_KEYS,
    AUDIO_ROUTE_PROFILE_KEY,
    FANIN_INPUT_RESAMPLER_KEY,
    FANIN_INPUT_RESAMPLER_LANE_KEY,
    FANIN_USB_DIRECT_PERIOD_KEY,
    OUTPUTD_CONTENT_BRIDGE_KEY,
    ROUTE_BITPERFECT_DECLARED,
    ROUTE_CORRECTED_48K,
    ROUTE_USB_LOW_LATENCY_48K,
    apply_capture_precedence,
    DEFAULT_FANIN_INPUT_BUFFER_FRAMES,
    DEFAULT_FANIN_OUTPUT_BUFFER_FRAMES,
    DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES,
    DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES,
    DEFAULT_OUTPUTD_PERIOD_FRAMES,
    DEFAULT_USB_LOW_LATENCY_OUTPUTD_CONTENT_BUFFER_FRAMES,
    OUTPUTD_CONTENT_BUFFER_KEY,
    OUTPUTD_DAC_BUFFER_KEY,
    OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER,
    OUTPUTD_PERIOD_KEY,
    build_audio_runtime_plan,
    coupling_supported_for_route,
    correction_latency_eligibility,
    correction_latency_eligibility_for_config,
    fanin_coupling_action,
    fanin_coupling_capture_kwargs,
    outputd_content_buffer_pair_error,
    outputd_dac_buffer_pair_error,
    outputd_env_buffer_pair_error,
    outputd_latency_floor_actions,
    output_endpoint_evidence_from_statefiles,
    resolve_audio_route_profile,
    route_owned_env_actions,
    transport_coherence_errors,
    transport_topology_for_coupling,
)
from jasper.camilla_config_contract import (
    ACTIVE_OUTPUTD_PLAYBACK_DEVICE,
    DEFAULT_OUTPUTD_CAPTURE_DEVICE,
    DEFAULT_PLAYBACK_DEVICE,
)
from jasper.cli.audio_config import main as audio_config_main
from jasper.env_load import EnvFileState
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_LOOPBACK,
    COUPLING_SHM_RING,
    RING_CAMILLA_CHUNKSIZE,
    RING_CAMILLA_QUEUELIMIT,
    RING_CAMILLA_TARGET_LEVEL,
    VALID_COUPLINGS,
)


ROOT = Path(__file__).resolve().parents[1]


def test_plan_uses_dac_profile_floor_as_intended_source():
    plan = build_audio_runtime_plan(
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
        outputd_env={
            "JASPER_CAMILLA_CHUNKSIZE": "256",
            "JASPER_CAMILLA_TARGET_LEVEL": "1536",
            "JASPER_OUTPUTD_PERIOD_FRAMES": "128",
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES": "256",
        },
    )

    assert plan.setting("JASPER_CAMILLA_CHUNKSIZE").value == 256
    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").value == 1536
    assert plan.setting("JASPER_OUTPUTD_PERIOD_FRAMES").value == 128
    assert plan.setting("JASPER_OUTPUTD_DAC_BUFFER_FRAMES").value == 256
    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").source_kind == "device_profile"
    assert plan.warnings == ()


def test_shm_ring_plan_uses_effective_ring_camilla_geometry():
    plan = build_audio_runtime_plan(
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
        fanin_env={COUPLING_ENV_VAR: COUPLING_SHM_RING},
        outputd_env={
            "JASPER_CAMILLA_CHUNKSIZE": "256",
            "JASPER_CAMILLA_TARGET_LEVEL": "1536",
        },
    )

    chunksize = plan.setting("JASPER_CAMILLA_CHUNKSIZE")
    target = plan.setting("JASPER_CAMILLA_TARGET_LEVEL")
    assert chunksize.value == RING_CAMILLA_CHUNKSIZE
    assert chunksize.source_kind == "route_policy"
    assert "shm_ring" in chunksize.source
    assert any("under shm_ring" in warning for warning in chunksize.warnings)
    assert target.value == RING_CAMILLA_TARGET_LEVEL
    assert target.source_kind == "route_policy"
    assert "shm_ring" in target.source
    assert any("under shm_ring" in warning for warning in target.warnings)


def test_operator_env_wins_but_duplicate_generated_home_warns():
    plan = build_audio_runtime_plan(
        base_env={"JASPER_CAMILLA_CHUNKSIZE": "512"},
        outputd_env={"JASPER_CAMILLA_CHUNKSIZE": "256"},
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )

    setting = plan.setting("JASPER_CAMILLA_CHUNKSIZE")
    assert setting.value == 512
    assert setting.source_kind == "operator_env"
    assert any("one knob has two homes" in warning for warning in plan.warnings)


def test_lab_override_wins_over_operator_and_profile_floor():
    plan = build_audio_runtime_plan(
        base_env={"JASPER_CAMILLA_CHUNKSIZE": "512"},
        outputd_env={"JASPER_CAMILLA_CHUNKSIZE": "256"},
        overrides={"JASPER_CAMILLA_CHUNKSIZE": "384"},
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
        override_label="/var/lib/jasper/audio_runtime_overrides.json",
    )

    setting = plan.setting("JASPER_CAMILLA_CHUNKSIZE")
    assert setting.value == 384
    assert setting.source_kind == "lab_override"
    assert setting.override_value == "384"
    assert any("lab override" in warning for warning in plan.warnings)


def test_profile_floor_wrapper_preserves_layer_precedence_and_warnings():
    key = "JASPER_CAMILLA_TARGET_LEVEL"
    base_label = "/etc/jasper/jasper.env"
    override_label = "/var/lib/jasper/audio_runtime_overrides.json"
    generated_label = "/var/lib/jasper/outputd.env"

    setting = audio_plan._resolve_profile_floor_int(
        key=key,
        default=1024,
        floor_value=1536,
        base_env={key: "512"},
        override_env={key: "384"},
        generated_env={key: "256"},
        base_label=base_label,
        override_label=override_label,
        generated_label=generated_label,
        profile_id=APPLE_USB_C_DONGLE_ID,
    )

    assert setting.value == 384
    assert setting.source_kind == "lab_override"
    assert setting.override_value == "384"
    assert setting.operator_value == "512"
    assert setting.generated_value == "256"
    assert setting.warnings == (
        f"{key} is set in both {base_label} and {generated_label}; "
        "one knob has two homes",
        f"{key} lab override in {override_label} is active; it intentionally "
        "wins over env/profile values",
    )

    policy_setting = audio_plan._resolve_profile_floor_int(
        key=key,
        default=1024,
        floor_value=1536,
        base_env={},
        override_env={},
        generated_env={key: "1024"},
        base_label=base_label,
        override_label=override_label,
        generated_label=generated_label,
        profile_id=APPLE_USB_C_DONGLE_ID,
    )
    assert policy_setting.value == 1536
    assert policy_setting.source_kind == "device_profile"
    assert policy_setting.warnings == (
        f"{key} in {generated_label} is 1024, but the "
        f"{APPLE_USB_C_DONGLE_ID} profile floor is 1536; rerun "
        "audio hardware reconcile",
    )


def test_content_buffer_wrapper_preserves_layer_precedence_and_warnings():
    key = OUTPUTD_CONTENT_BUFFER_KEY
    base_label = "/etc/jasper/jasper.env"
    override_label = "/var/lib/jasper/audio_runtime_overrides.json"
    generated_label = "/var/lib/jasper/outputd.env"
    route = resolve_audio_route_profile(
        {AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K}
    )

    setting = audio_plan._resolve_outputd_content_buffer_int(
        route=route,
        base_env={key: "2048"},
        override_env={key: "768"},
        generated_env={key: "1024"},
        base_label=base_label,
        override_label=override_label,
        generated_label=generated_label,
    )

    assert setting.value == 768
    assert setting.source_kind == "lab_override"
    assert setting.override_value == "768"
    assert setting.operator_value == "2048"
    assert setting.generated_value == "1024"
    assert setting.warnings == (
        f"{key} is set in both {base_label} and {generated_label}; "
        "one knob has two homes",
        f"{key} lab override in {override_label} is active; it intentionally "
        "wins over env/route values",
    )

    policy_setting = audio_plan._resolve_outputd_content_buffer_int(
        route=route,
        base_env={},
        override_env={},
        generated_env={key: "1024"},
        base_label=base_label,
        override_label=override_label,
        generated_label=generated_label,
    )
    assert policy_setting.value == 1536
    assert policy_setting.source_kind == "route_policy"
    assert policy_setting.warnings == (
        f"{key} in {generated_label} is 1024, but the "
        f"{ROUTE_USB_LOW_LATENCY_48K} route policy is 1536; rerun "
        "audio hardware reconcile",
    )


def test_invalid_lab_override_is_ignored_with_warning():
    plan = build_audio_runtime_plan(
        overrides={"JASPER_CAMILLA_TARGET_LEVEL": "bad"},
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )

    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").value == 1536
    assert any("audio_runtime_overrides" in warning and "invalid" in warning for warning in plan.warnings)


def test_stale_generated_floor_warns_against_device_profile():
    plan = build_audio_runtime_plan(
        outputd_env={
            "JASPER_CAMILLA_TARGET_LEVEL": "1024",
        },
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )

    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").value == 1536
    assert any(
        "profile floor is 1536" in warning or "profile floor for" in warning
        for warning in plan.warnings
    )


def test_outputd_latency_floor_actions_set_profile_floor_when_no_operator_env():
    actions = outputd_latency_floor_actions(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={},
        outputd_env={},
    )

    assert [(a.action, a.key, a.value) for a in actions] == [
        ("set", "JASPER_CAMILLA_CHUNKSIZE", "256"),
        ("set", "JASPER_CAMILLA_TARGET_LEVEL", "1536"),
        ("set", "JASPER_OUTPUTD_PERIOD_FRAMES", "128"),
        ("unset", "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES", ""),
        ("set", "JASPER_OUTPUTD_DAC_BUFFER_FRAMES", "256"),
    ]


def test_outputd_latency_floor_actions_set_usb_route_content_buffer():
    actions = outputd_latency_floor_actions(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        outputd_env={},
    )

    by_key = {action.key: action for action in actions}
    assert by_key["JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES"].action == "set"
    assert by_key["JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES"].value == "1536"


def test_usb_low_latency_without_dac_floor_keeps_outputd_pair_coherent():
    # Non-ring (loopback/direct) box with no DAC period floor: period stays 1024,
    # so the route's 1536 content buffer is incoherent (< 2 x period) and the
    # coherence guard repairs it to the packaged default with a "suppressing"
    # warning. (The shm_ring bridge takes a different path — the policy is inert
    # and dropped upstream — covered by test_usb_low_latency_shm_ring_bridge_*.)
    plan = build_audio_runtime_plan(
        profile_id="",
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        outputd_env={},
    )

    period = plan.setting(OUTPUTD_PERIOD_KEY).value
    content_buffer = plan.setting(OUTPUTD_CONTENT_BUFFER_KEY).value
    assert period == DEFAULT_OUTPUTD_PERIOD_FRAMES
    assert content_buffer == DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES
    assert content_buffer >= OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER * period
    assert any("suppressing the low-latency route buffer" in w for w in plan.warnings)

    actions = outputd_latency_floor_actions(
        profile_id="",
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        outputd_env={},
    )
    by_key = {action.key: action for action in actions}
    assert by_key[OUTPUTD_PERIOD_KEY].action == "unset"
    assert by_key[OUTPUTD_CONTENT_BUFFER_KEY].action == "unset"


def test_usb_low_latency_with_dac_floor_keeps_shipped_pair():
    plan = build_audio_runtime_plan(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        outputd_env={
            OUTPUTD_PERIOD_KEY: "128",
            OUTPUTD_CONTENT_BUFFER_KEY: "1536",
        },
    )

    assert plan.setting(OUTPUTD_PERIOD_KEY).value == 128
    assert (
        plan.setting(OUTPUTD_CONTENT_BUFFER_KEY).value
        == DEFAULT_USB_LOW_LATENCY_OUTPUTD_CONTENT_BUFFER_FRAMES
    )
    assert not any("suppressing the low-latency route buffer" in w for w in plan.warnings)


def test_usb_low_latency_shm_ring_bridge_drops_inert_content_buffer_policy():
    """Under the shm_ring content bridge (Ring B), the outputd content buffer is
    architecturally inert (outputd never opens the content ALSA PCM, so
    configure_pcm — the only consumer — is skipped). The USB low-latency route
    MUST NOT emit its 1536 policy there: it was one-knob-two-truths drift against
    the honest /state ring capacity. The setting falls back to the packaged
    default instead of the route policy."""
    plan = build_audio_runtime_plan(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        outputd_env={
            OUTPUTD_PERIOD_KEY: "128",
            OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring",
        },
    )

    setting = plan.setting(OUTPUTD_CONTENT_BUFFER_KEY)
    assert setting.value != DEFAULT_USB_LOW_LATENCY_OUTPUTD_CONTENT_BUFFER_FRAMES
    assert setting.value == DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES
    assert setting.source_kind != "route_policy"
    # Negative control: ONLY a genuine shm_ring is inert. Any other literal —
    # a typo, or the REMOVED `rate_match` — resolves `direct`, which DOES open
    # the content PCM, so the policy still applies. Exercised through both so
    # the suppression cannot quietly widen to "anything non-empty".
    for stale_bridge in ("rate_match", "not-a-bridge"):
        stale_plan = build_audio_runtime_plan(
            profile_id=APPLE_USB_C_DONGLE_ID,
            base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
            outputd_env={
                OUTPUTD_PERIOD_KEY: "128",
                OUTPUTD_CONTENT_BRIDGE_KEY: stale_bridge,
            },
        )
        assert (
            stale_plan.setting(OUTPUTD_CONTENT_BUFFER_KEY).value
            == DEFAULT_USB_LOW_LATENCY_OUTPUTD_CONTENT_BUFFER_FRAMES
        ), stale_bridge


def test_outputd_floor_actions_shm_ring_bridge_unsets_content_buffer():
    """Writer-side proof: the reconciler stops EMITTING
    JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536 under the shm_ring bridge — the
    action becomes `unset`, so outputd uses its (inert but non-misleading)
    compile-time default. This path only ever sees outputd.env, which is why the
    suppression keys on the outputd bridge and not the (invisible-here) coupling."""
    shm_ring_actions = outputd_latency_floor_actions(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        outputd_env={
            OUTPUTD_PERIOD_KEY: "128",
            OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring",
        },
    )
    by_key = {a.key: a for a in shm_ring_actions}
    assert by_key[OUTPUTD_CONTENT_BUFFER_KEY].action == "unset"

    # Non-ring (loopback/direct) USB low-latency box still emits the 1536 policy.
    loopback_actions = outputd_latency_floor_actions(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        outputd_env={OUTPUTD_PERIOD_KEY: "128"},
    )
    loopback_by_key = {a.key: a for a in loopback_actions}
    assert loopback_by_key[OUTPUTD_CONTENT_BUFFER_KEY].action == "set"
    assert loopback_by_key[OUTPUTD_CONTENT_BUFFER_KEY].value == "1536"


def test_usb_low_latency_route_hash_reflects_shm_ring_content_buffer_drop():
    """The suppressed content-buffer policy is part of the route identity, so the
    route_config_hash for a shm_ring USB-low-latency box differs from the loopback
    one — the intended, minimal-blast-radius consequence (no schema bump; only
    affected configs' hashes move). A stale route-latency artifact taken before
    this change is invalidated (config_mismatch), which is correct: it must be
    re-certified after this ships."""
    shm_ring_plan = build_audio_runtime_plan(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        fanin_env={COUPLING_ENV_VAR: COUPLING_SHM_RING},
        outputd_env={
            OUTPUTD_PERIOD_KEY: "128",
            OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring",
        },
        route_mode="solo",
    )
    loopback_plan = build_audio_runtime_plan(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        outputd_env={OUTPUTD_PERIOD_KEY: "128"},
        route_mode="solo",
    )
    assert shm_ring_plan.route_config_hash != loopback_plan.route_config_hash
    assert (
        shm_ring_plan.route_latency_identity()["outputd_config"][
            OUTPUTD_CONTENT_BUFFER_KEY
        ]
        == DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES
    )


def test_python_outputd_buffer_contract_matches_rust_validator():
    config_rs = (ROOT / "rust" / "jasper-outputd" / "src" / "config.rs").read_text(
        encoding="utf-8"
    )

    assert "fn validate_buffer(" in config_rs
    assert "period_frames.saturating_mul(2)" in config_rs
    assert "minimum ALSA jitter margin" in config_rs
    assert OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER == 2
    assert outputd_content_buffer_pair_error(
        period_frames=1024,
        content_buffer_frames=1536,
    ) == (
        "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536 must be >= "
        "2 x JASPER_OUTPUTD_PERIOD_FRAMES=1024 (minimum ALSA jitter margin)"
    )
    assert outputd_dac_buffer_pair_error(
        period_frames=1024,
        dac_buffer_frames=1024,
    ) == (
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=1024 must be >= "
        "2 x JASPER_OUTPUTD_PERIOD_FRAMES=1024 (minimum ALSA jitter margin)"
    )
    assert outputd_env_buffer_pair_error(
        base_env={},
        outputd_env={
            OUTPUTD_CONTENT_BUFFER_KEY: "1536",
        },
    ) == (
        "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536 must be >= "
        "2 x JASPER_OUTPUTD_PERIOD_FRAMES=1024 (minimum ALSA jitter margin)"
    )
    assert outputd_env_buffer_pair_error(
        base_env={},
        outputd_env={
            OUTPUTD_CONTENT_BUFFER_KEY: "4096",
            OUTPUTD_DAC_BUFFER_KEY: "1024",
        },
    ) == (
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=1024 must be >= "
        "2 x JASPER_OUTPUTD_PERIOD_FRAMES=1024 (minimum ALSA jitter margin)"
    )


def test_packaged_outputd_buffer_defaults_are_mutually_coherent():
    """The packaged defaults must satisfy outputd's own buffer/period rule.

    Two things rest on this. `_pair_provenance` carries no "both halves are
    packaged defaults" branch because that pair cannot fail; and a floor-less
    box runs exactly these numbers, so an incoherent pair here would refuse
    every candidate that box can compute. Issue #2489 was misdiagnosed as this
    defect — it is worth a test rather than a re-derivation.

    THE FLOOR-LESS EXEMPLAR MOVED (P8b item 1c). This clause used to name
    `dual_apple_usb_c_dac_4ch`, which declared no `LatencyFloor`. It declares
    one now — the floor it inherits from its children, without which the ring
    conf.d never renders — so `hifiberry_dac8x_studio` is the surviving
    floor-less registered profile and carries the clause instead. The assertion
    is kept (rather than dropped) because the SUBJECT is unchanged: a profile
    with no floor still falls back to exactly these packaged numbers, and the
    test would stop meaning anything if the last floor-less profile quietly
    gained one.
    """

    assert outputd_content_buffer_pair_error(
        period_frames=DEFAULT_OUTPUTD_PERIOD_FRAMES,
        content_buffer_frames=DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES,
    ) is None
    assert outputd_dac_buffer_pair_error(
        period_frames=DEFAULT_OUTPUTD_PERIOD_FRAMES,
        dac_buffer_frames=DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES,
    ) is None
    assert latency_floor_for("hifiberry_dac8x_studio") is None
    # And the composite is deliberately NO LONGER floor-less. Asserted here, at
    # the test whose premise it was, so a revert of item 1c cannot quietly
    # restore the old exemplar and leave this docstring lying.
    assert latency_floor_for("dual_apple_usb_c_dac_4ch") is not None
    assert outputd_env_buffer_pair_error(base_env={}, outputd_env={}) is None


def _override(value: str, *, reason: str = "r", created_at: str = "") -> RuntimeOverrideEntry:
    return RuntimeOverrideEntry(
        key=OUTPUTD_CONTENT_BUFFER_KEY,
        value=value,
        reason=reason,
        created_at=created_at,
    )


_BASE_LABEL = "/etc/jasper/jasper.env"
_OUTPUTD_LABEL = "/var/lib/jasper/outputd.env"


def test_buffer_pair_refusal_names_the_layer_that_holds_the_losing_value():
    """The Rust mirror names the keys; an operator needs the FILE.

    The reconciler unsets a key from outputd.env whenever jasper.env owns it,
    so a refusal that names only the key sends the reader to the file where the
    key is absent.
    """

    operator = outputd_env_buffer_pair_error(
        base_env={OUTPUTD_CONTENT_BUFFER_KEY: "1536"},
        outputd_env={},
        base_label=_BASE_LABEL,
        outputd_label=_OUTPUTD_LABEL,
    )
    assert operator is not None
    assert f"{OUTPUTD_CONTENT_BUFFER_KEY}=1536 comes from {_BASE_LABEL}" in operator
    assert f"{OUTPUTD_PERIOD_KEY}=1024 comes from packaged systemd/outputd default" in operator

    generated = outputd_env_buffer_pair_error(
        base_env={},
        outputd_env={OUTPUTD_CONTENT_BUFFER_KEY: "1536"},
        base_label=_BASE_LABEL,
        outputd_label=_OUTPUTD_LABEL,
    )
    assert generated is not None
    assert f"{OUTPUTD_CONTENT_BUFFER_KEY}=1536 comes from {_OUTPUTD_LABEL}" in generated

    # The DAC-buffer half of the pair gets the same treatment.
    dac = outputd_env_buffer_pair_error(
        base_env={OUTPUTD_DAC_BUFFER_KEY: "1024"},
        outputd_env={},
        base_label=_BASE_LABEL,
        outputd_label=_OUTPUTD_LABEL,
    )
    assert dac is not None
    assert f"{OUTPUTD_DAC_BUFFER_KEY}=1024 comes from {_BASE_LABEL}" in dac


def test_buffer_pair_refusal_is_the_bare_rust_mirror_without_labels():
    """Unlabelled callers keep the byte-identical daemon message."""

    assert outputd_env_buffer_pair_error(
        base_env={},
        outputd_env={OUTPUTD_CONTENT_BUFFER_KEY: "1536"},
    ) == (
        "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES=1536 must be >= "
        "2 x JASPER_OUTPUTD_PERIOD_FRAMES=1024 (minimum ALSA jitter margin)"
    )
    # One label is not enough: a half-labelled provenance would name one layer
    # and guess the other.
    assert ";" not in (
        outputd_env_buffer_pair_error(
            base_env={},
            outputd_env={OUTPUTD_CONTENT_BUFFER_KEY: "1536"},
            outputd_label=_OUTPUTD_LABEL,
        )
        or ""
    )


def test_buffer_pair_refusal_quotes_the_override_store_that_wrote_the_line():
    """jts.local's live #2489 shape: the store, not a hand-edited file.

    The latency-floor pass COPIES a store value into outputd.env, so naming
    only the file sends an operator to delete a line the next reconcile writes
    straight back. The store's own created_at/reason are the self-explanation
    that resolves the case on sight.
    """

    detail = outputd_env_buffer_pair_error(
        base_env={},
        outputd_env={OUTPUTD_CONTENT_BUFFER_KEY: "1536"},
        base_label=_BASE_LABEL,
        outputd_label=_OUTPUTD_LABEL,
        override_entries={
            OUTPUTD_CONTENT_BUFFER_KEY: _override(
                "1536",
                reason="latency-tuning-outputd-content-buffer-1536-verified-floor",
                created_at="2026-07-02T00:00:00Z",
            )
        },
    )
    assert detail is not None
    assert "written there from the override store" in detail
    assert DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH in detail
    assert "created_at=2026-07-02T00:00:00Z" in detail
    # The store path named must be the one the caller READ, not the production
    # constant — naming a file that was never consulted is the wrong-origin
    # failure this provenance exists to prevent.
    relocated = outputd_env_buffer_pair_error(
        base_env={},
        outputd_env={OUTPUTD_CONTENT_BUFFER_KEY: "1536"},
        base_label=_BASE_LABEL,
        outputd_label=_OUTPUTD_LABEL,
        override_entries={OUTPUTD_CONTENT_BUFFER_KEY: _override("1536")},
        override_label="/run/test/overrides.json",
    )
    assert relocated is not None
    assert "/run/test/overrides.json" in relocated
    assert DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH not in relocated
    assert (
        "reason=latency-tuning-outputd-content-buffer-1536-verified-floor" in detail
    )
    # The remediation must be runnable AS PRINTED — the actual key, never a
    # `<key>` placeholder the operator has to translate.
    assert f"jasper-audio-config overrides-clear {OUTPUTD_CONTENT_BUFFER_KEY}" in detail
    assert "<key>" not in detail


@pytest.mark.parametrize(
    "why, base_env, outputd_env, entry",
    [
        pytest.param(
            "the store disagrees with the value on disk",
            {},
            {OUTPUTD_CONTENT_BUFFER_KEY: "1536"},
            _override("2048"),
            id="store-value-mismatch",
        ),
        pytest.param(
            "jasper.env owns the line, not the store",
            {OUTPUTD_CONTENT_BUFFER_KEY: "1536"},
            {},
            _override("1536"),
            id="operator-layer-owns-it",
        ),
    ],
)
def test_buffer_pair_refusal_does_not_misattribute_to_the_override_store(
    why: str, base_env: dict[str, str], outputd_env: dict[str, str],
    entry: RuntimeOverrideEntry,
):
    """A wrong origin is worse than a missing one — it sends the fix elsewhere."""

    detail = outputd_env_buffer_pair_error(
        base_env=base_env,
        outputd_env=outputd_env,
        base_label=_BASE_LABEL,
        outputd_label=_OUTPUTD_LABEL,
        override_entries={OUTPUTD_CONTENT_BUFFER_KEY: entry},
    )
    assert detail is not None, why
    assert "override store" not in detail, why
    assert "overrides-clear" not in detail, why


def test_validate_outputd_env_cli_reads_the_override_store(tmp_path, capsys):
    """The CLI is the caller the reconciler runs — pin the wiring, not just the API.

    Every provenance assertion above goes through `outputd_env_buffer_pair_error`
    directly. That leaves the one thing an operator actually sees unpinned: a CLI
    that stopped passing the store would keep every library test green while the
    journal line lost the only field that explains the value.
    """

    base_env = tmp_path / "jasper.env"
    base_env.write_text("", encoding="utf-8")
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text(
        f"{OUTPUTD_CONTENT_BUFFER_KEY}=1536\n", encoding="utf-8"
    )
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text("", encoding="utf-8")
    store = tmp_path / "audio_runtime_overrides.json"
    store.write_text(
        json.dumps({
            "kind": "jts_audio_runtime_overrides",
            "schema_version": 1,
            "overrides": {
                OUTPUTD_CONTENT_BUFFER_KEY: {
                    "value": "1536",
                    "created_at": "2026-07-02T00:00:00Z",
                    "reason": "latency-tuning-outputd-content-buffer-1536-verified-floor",
                }
            },
        }),
        encoding="utf-8",
    )

    from jasper.cli.audio_config import main as audio_config_main

    result = audio_config_main([
        "validate-outputd-env",
        "--base-env", str(base_env),
        "--outputd-env", str(outputd_env),
        "--fanin-env", str(fanin_env),
        "--overrides", str(store),
    ])

    assert result == 1
    printed = capsys.readouterr().out
    assert "minimum ALSA jitter margin" in printed
    assert str(store) in printed
    assert "created_at=2026-07-02T00:00:00Z" in printed
    assert (
        "reason=latency-tuning-outputd-content-buffer-1536-verified-floor" in printed
    )


def test_coherence_repair_sees_the_override_and_declines_by_scope():
    """Ordering fact, pinned: the repair runs AFTER the store wins.

    `_coherent_outputd_content_buffer_setting` wraps the already-resolved
    setting, so it SEES a lab override — the store is not a structural bypass.
    It declines because its scope is JTS's own route policy: an operator value
    wins by design, and the candidate refusal is the correct catch. A refactor
    that made the repair rewrite a lab override would silently discard operator
    intent, so the decline is pinned rather than assumed.
    """

    plan = build_audio_runtime_plan(
        profile_id="",
        base_env={},
        outputd_env={},
        overrides={OUTPUTD_CONTENT_BUFFER_KEY: "1536"},
        route_mode="solo",
    )
    setting = plan.setting(OUTPUTD_CONTENT_BUFFER_KEY)
    assert setting.value == 1536
    assert setting.source_kind == "lab_override"
    assert plan.setting(OUTPUTD_PERIOD_KEY).value == DEFAULT_OUTPUTD_PERIOD_FRAMES
    # The repair saw it: it attached the incoherence warning rather than
    # rewriting the value.
    assert any("minimum ALSA jitter margin" in w for w in setting.warnings)

    # ...and the floor pass then COPIES that value into outputd.env, which is
    # how a store value reaches the file the refusal reports.
    actions = outputd_latency_floor_actions(
        profile_id="",
        base_env={},
        outputd_env={},
        overrides={OUTPUTD_CONTENT_BUFFER_KEY: "1536"},
    )
    assert ("set", OUTPUTD_CONTENT_BUFFER_KEY, "1536") in [
        (a.action, a.key, a.value) for a in actions
    ]


def test_outputd_latency_floor_actions_unset_when_operator_env_owns_key():
    actions = outputd_latency_floor_actions(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={"JASPER_CAMILLA_CHUNKSIZE": "512"},
        outputd_env={"JASPER_CAMILLA_CHUNKSIZE": "256"},
    )

    by_key = {action.key: action for action in actions}
    assert by_key["JASPER_CAMILLA_CHUNKSIZE"].action == "unset"
    assert by_key["JASPER_CAMILLA_TARGET_LEVEL"].action == "set"


def test_outputd_latency_floor_actions_unset_when_profile_has_no_floor():
    # A floorless profile drops every generated floor key so the packaged
    # defaults apply. Asserted, not assumed, that this profile is floorless —
    # a later floor declaration must fail here rather than quietly making the
    # {"unset"} expectation unreachable (what an R7a DAC8x floor did when this
    # test named `hifiberry_dac8x`, and what jts4's measured floor then did
    # when it named `innomaker_hifi_amp_pro`).
    assert latency_floor_for(HIFIBERRY_DAC8X_STUDIO_ID) is None
    actions = outputd_latency_floor_actions(
        profile_id=HIFIBERRY_DAC8X_STUDIO_ID,
        base_env={},
        outputd_env={
            "JASPER_CAMILLA_CHUNKSIZE": "256",
            "JASPER_CAMILLA_TARGET_LEVEL": "1536",
        },
    )

    assert {action.action for action in actions} == {"unset"}


def test_outputd_latency_floor_actions_set_the_dac8x_soak_floor():
    # The R7a hardware-validated floor reaches outputd.env through the same
    # writer-side policy the Apple dongle uses: Camilla 256/1536 and outputd
    # period 128 / dac_buffer 256, with the content buffer left at the packaged
    # default (4096) — the 128/4096 content pair the jts3 soak ran.
    actions = outputd_latency_floor_actions(
        profile_id=HIFIBERRY_DAC8X_ID,
        base_env={},
        outputd_env={},
    )

    by_key = {action.key: action for action in actions}
    assert by_key["JASPER_CAMILLA_CHUNKSIZE"].action == "set"
    assert by_key["JASPER_CAMILLA_CHUNKSIZE"].value == "256"
    assert by_key["JASPER_CAMILLA_TARGET_LEVEL"].value == "1536"
    assert by_key[OUTPUTD_PERIOD_KEY].value == "128"
    assert by_key[OUTPUTD_DAC_BUFFER_KEY].value == "256"
    # The packaged content-buffer default is emitted as an unset (the writer
    # never pins a value equal to the shipped default), so outputd runs the
    # soak-proven 128-period / 4096-buffer content pair.
    assert by_key[OUTPUTD_CONTENT_BUFFER_KEY].action == "unset"
    assert DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES == 4096


def test_outputd_latency_floor_actions_use_lab_override():
    actions = outputd_latency_floor_actions(
        profile_id=APPLE_USB_C_DONGLE_ID,
        base_env={"JASPER_CAMILLA_CHUNKSIZE": "512"},
        outputd_env={},
        overrides={"JASPER_CAMILLA_CHUNKSIZE": "384"},
    )

    by_key = {action.key: action for action in actions}
    assert by_key["JASPER_CAMILLA_CHUNKSIZE"].action == "set"
    assert by_key["JASPER_CAMILLA_CHUNKSIZE"].value == "384"


def test_bad_operator_value_is_ignored_and_warned():
    plan = build_audio_runtime_plan(
        base_env={"JASPER_CAMILLA_TARGET_LEVEL": "rough-test"},
        outputd_env={"JASPER_CAMILLA_TARGET_LEVEL": "1536"},
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )

    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").value == 1536
    assert any("rough-test" in warning and "ignored" in warning for warning in plan.warnings)


def test_fanin_env_is_the_owned_home_for_fanin_buffer_tuning():
    plan = build_audio_runtime_plan(
        base_env={"JASPER_FANIN_OUTPUT_BUFFER_FRAMES": "2048"},
        fanin_env={"JASPER_FANIN_OUTPUT_BUFFER_FRAMES": "1024"},
        route_mode="solo",
    )

    setting = plan.setting("JASPER_FANIN_OUTPUT_BUFFER_FRAMES")
    assert setting.value == 1024
    assert setting.source_kind == "generated_env"
    assert any("reconciler-owned home" in warning for warning in plan.warnings)


def test_audio_route_profile_defaults_to_corrected_safe_path():
    plan = build_audio_runtime_plan(route_mode="solo")

    assert plan.route_profile.route_id == ROUTE_CORRECTED_48K
    assert plan.route_profile.low_latency_claim is False
    assert plan.route_profile.camilla_required is True
    assert plan.route_profile.outputd_final_reference_required is True
    assert plan.route_config_hash
    assert plan.to_dict()["route_profile"]["route_id"] == ROUTE_CORRECTED_48K


def test_system_plan_warns_and_uses_process_route_when_base_env_unreadable(
    monkeypatch,
    tmp_path: Path,
):
    """jasper-control must not silently downgrade /state on EACCES.

    systemd injects /etc/jasper/jasper.env before dropping privileges. If the
    later fresh file read fails, the plan may use the process copy for the base
    route keys, but it must still surface the unreadable source-of-truth file.
    """

    def fake_read(path: str) -> EnvFileState:
        if path == "base.env":
            return EnvFileState(
                path,
                {},
                "unreadable",
                "PermissionError: [Errno 13] Permission denied",
            )
        return EnvFileState(path, {}, "missing")

    monkeypatch.setattr(audio_plan, "read_env_file_state", fake_read)
    monkeypatch.setenv(AUDIO_ROUTE_PROFILE_KEY, ROUTE_USB_LOW_LATENCY_48K)

    plan = audio_plan.build_audio_runtime_plan_from_system(
        base_env_path="base.env",
        outputd_env_path="outputd.env",
        fanin_env_path="fanin.env",
        grouping_env_path=str(tmp_path / "grouping.env"),
        overrides_path=str(tmp_path / "overrides.json"),
        output_hardware_state_path=str(tmp_path / "output_hardware.json"),
    )

    assert plan.route_profile.route_id == ROUTE_USB_LOW_LATENCY_48K
    assert any(
        "unreadable audio runtime base env file base.env" in warning
        for warning in plan.warnings
    )


def test_invalid_audio_route_profile_falls_back_with_warning():
    profile = resolve_audio_route_profile({AUDIO_ROUTE_PROFILE_KEY: "fastish"})

    assert profile.route_id == ROUTE_CORRECTED_48K
    assert any("invalid" in warning for warning in profile.warnings)


def test_usb_low_latency_route_requires_direct_fanin_resampler_and_reference():
    profile = resolve_audio_route_profile(
        {AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K}
    )
    actions = route_owned_env_actions(profile)
    by_key = {action.key: action for action in actions}

    assert profile.low_latency_claim is True
    assert profile.fanin_usb_direct_required is True
    assert profile.fanin_input_resampler_required is True
    assert profile.camilla_required is True
    assert profile.outputd_final_reference_required is True
    # Pinned to the certified, measured-basis budget (2026-07-11 promotion cert:
    # p95 37.93 / p99 38.29 ms — see the docstring on USB_LOW_LATENCY_P95_BUDGET_MS
    # in audio_runtime_plan.py). A silent loosening or tightening of these
    # constants should fail this test.
    assert profile.p95_budget_ms == 40.0
    assert profile.p99_budget_ms == 42.0
    assert by_key[FANIN_INPUT_RESAMPLER_KEY].value == "enabled"
    assert by_key[FANIN_INPUT_RESAMPLER_LANE_KEY].value == "usbsink"
    assert all(not key.startswith("JASPER_USBSINK_") for key in by_key)
    assert (
        by_key["JASPER_FANIN_INPUT_RESAMPLER_WARMUP_CUSHION_FRAMES"].value
        == "1536"
    )


def test_usb_low_latency_route_identity_carries_direct_capture_and_resampler():
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )
    identity = plan.route_latency_identity()

    assert identity["dac_profile_id"] == APPLE_USB_C_DONGLE_ID
    assert identity["route_config_hash"] == plan.route_config_hash
    assert identity["fanin_resampler_config"] == {
        "enabled": True,
        "lane": "usbsink",
        "target_frames": 512,
        "max_adjust_ppm": 500,
        "warmup_cushion_frames": 1536,
        "ring_frames": 4096,
    }
    assert identity["fanin_direct_config"] == {
        "lane": "usbsink",
        "source": "direct",
        "device": "hw:UAC2Gadget",
        "period_frames": 256,
        "min_buffer_frames": 768,
        "buffer_period_aligned": True,
    }
    assert identity["outputd_config"]["JASPER_OUTPUTD_PERIOD_FRAMES"] == 128
    assert (
        identity["outputd_config"]["JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES"]
        == DEFAULT_USB_LOW_LATENCY_OUTPUTD_CONTENT_BUFFER_FRAMES
    )
    assert identity["uac2_gadget_attrs"]["c_sync"] == "async"


def test_usb_low_latency_route_rejects_any_non_direct_bridge_literal():
    """The route policy compares the RAW outputd bridge literal, not the
    fail-safe resolver — so a partial flip without a matching shm_ring coupling
    is refused whatever the value spells.

    `rate_match` is the load-bearing case: outputd DELETED that bridge and now
    fail-safes every one of its spellings to `direct`. If this policy were
    "simplified" onto `resolve_outputd_content_bridge`, a box still carrying a
    stale spelling would launder into `direct` and certify a green
    `usb_low_latency_48k` claim it must not get. Each spelling plus a typo is
    exercised so that regression fails here.
    """
    for bridge in (
        "rate_match",
        "ratematch",
        "rate-matched",
        "rate_matched",
        "pipewire",
    ):
        plan = build_audio_runtime_plan(
            base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
            outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: bridge},
            route_mode="solo",
        )

        assert any(
            "requires JASPER_OUTPUTD_CONTENT_BRIDGE=direct" in error
            for error in plan.errors
        ), f"{bridge} must not certify: {plan.errors}"

    # Positive control: the two COHERENT transports still certify, so the
    # assertion above is not passing because everything errors.
    ok = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: "direct"},
        route_mode="solo",
    )
    assert not any(
        "requires JASPER_OUTPUTD_CONTENT_BRIDGE=direct" in error
        for error in ok.errors
    ), ok.errors


def test_route_config_hash_includes_fanin_direct_period():
    base_env = {AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K}
    default_plan = build_audio_runtime_plan(base_env=base_env, route_mode="solo")

    changed_plan = build_audio_runtime_plan(
        base_env=base_env,
        fanin_env={FANIN_USB_DIRECT_PERIOD_KEY: "128"},
        route_mode="solo",
    )

    assert changed_plan.route_latency_identity()["fanin_direct_config"][
        "period_frames"
    ] == 128
    assert changed_plan.route_config_hash != default_plan.route_config_hash


@pytest.mark.parametrize(
    ("raw", "expected", "invalid"),
    (("31", 256, True), ("32", 32, False), ("1024", 1024, False), ("1025", 256, True)),
)
def test_usb_direct_period_matches_rust_bounds(raw, expected, invalid):
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        fanin_env={FANIN_USB_DIRECT_PERIOD_KEY: raw},
        route_mode="solo",
    )

    setting = next(
        item for item in plan.settings
        if item.key == FANIN_USB_DIRECT_PERIOD_KEY
    )
    assert setting.value == expected
    bound_warning = any("must be 32..1024" in warning for warning in setting.warnings)
    assert bound_warning is invalid


def test_route_config_hash_includes_active_camilla_config_hash(tmp_path):
    config = tmp_path / "camilla.yml"
    base_env = {AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K}
    config.write_text("filters: {}\n", encoding="utf-8")
    first = build_audio_runtime_plan(
        base_env=base_env,
        route_mode="solo",
        correction_config_path=str(config),
    )

    config.write_text("filters:\n  peq:\n    type: Biquad\n", encoding="utf-8")
    second = build_audio_runtime_plan(
        base_env=base_env,
        route_mode="solo",
        correction_config_path=str(config),
    )

    assert first.camilla_config_hash != second.camilla_config_hash
    assert first.route_config_hash != second.route_config_hash


def test_non_low_latency_route_clears_fanin_resampler_knobs_only():
    actions = route_owned_env_actions(ROUTE_CORRECTED_48K)
    by_key = {action.key: action for action in actions}

    assert by_key[FANIN_INPUT_RESAMPLER_KEY].action == "unset"
    assert all(not key.startswith("JASPER_USBSINK_") for key in by_key)


def test_bitperfect_route_is_declared_but_inactive_and_aec_degraded():
    profile = resolve_audio_route_profile(
        {AUDIO_ROUTE_PROFILE_KEY: ROUTE_BITPERFECT_DECLARED}
    )

    assert profile.active is False
    assert profile.bitperfect is True
    assert profile.camilla_required is False
    assert profile.aec_reference_mode == "degraded_until_final_reference_proven"
    assert "inactive" in profile.blocking_reason


def test_fanin_coupling_capture_kwargs_are_the_ring_whatever_the_env_says(
    monkeypatch,
):
    # ADR-0100: the ring is the only transport, so no env value and no persisted
    # file can make this answer anything else — a `{}` here would emit a graph
    # capturing a lane nothing writes.
    def _boom(*a, **k):
        raise AssertionError("the capture kwargs must not depend on a coupling token")

    monkeypatch.setattr("jasper.fanin.ring_health.read_persisted_coupling", _boom)
    monkeypatch.setenv("JASPER_FANIN_CAMILLA_COUPLING", "loopback")

    for kwargs in (
        fanin_coupling_capture_kwargs(None),
        fanin_coupling_capture_kwargs(None, env={}),
        fanin_coupling_capture_kwargs("loopback"),
        fanin_coupling_capture_kwargs(COUPLING_SHM_RING),
    ):
        assert kwargs["capture_device"] == "jts_ring_capture"


def test_capture_precedence_applies_shm_ring_when_no_stronger_topology():
    base = {"enable_rate_adjust": True, "playback_pipe_path": None}
    coupling = fanin_coupling_capture_kwargs("shm_ring")

    merged = apply_capture_precedence(
        base,
        coupling,
        member_kwargs=base,
    )

    assert merged["capture_device"] == "jts_ring_capture"
    assert merged["playback_device"] == "jts_ring_playback"
    assert merged["enable_rate_adjust"] is False
    assert base == {"enable_rate_adjust": True, "playback_pipe_path": None}


def test_capture_precedence_grouped_sink_keeps_the_capture_half_only():
    """A grouped pipe SINK owns playback — and ONLY playback.

    Dropping the capture half too was a silent-bond hazard, not a no-op: this is
    the path a bonded leader's /sound or /correction save re-emits camilla#1
    through (graph_carrier -> apply_capture_precedence), and camilla#1 is the
    producer of the WHOLE bond's audio. Under an armed ring, keeping the
    emitter's `plug:jasper_capture` default there points the LIVE config at the
    snd-aloop tap fan-in no longer writes — every member silent, every daemon
    healthy, and the on-disk bake still reading correctly.
    """
    coupling = {
        "capture_device": "jts_ring_capture",
        "capture_format": "S32LE",
        "playback_device": "jts_ring_playback",
        "playback_format": "S32LE",
        "enable_rate_adjust": False,
    }
    grouped = {"playback_pipe_path": "/run/snapfifo", "enable_rate_adjust": False}

    grouped_result = apply_capture_precedence(
        grouped,
        coupling,
        member_kwargs=grouped,
    )
    assert grouped_result["playback_pipe_path"] == "/run/snapfifo"
    assert grouped_result["capture_device"] == "jts_ring_capture"
    assert grouped_result["capture_format"] == "S32LE"
    # The PLAYBACK half must never cross: Ring B is a different sink from the
    # SNAPFIFO pipe, and naming it here strands the bond.
    assert "playback_device" not in grouped_result
    assert "playback_format" not in grouped_result


def test_capture_half_is_one_owner_shared_by_both_sink_owning_callers():
    """`capture_half` is the single answer to "which keys are the capture half".

    Two callers emit against a sink they already own — this precedence branch and
    the active leader's program bake — and a second hand-rolled key list is
    exactly how they would drift apart.
    """
    from jasper.fanin_coupling import CAPTURE_HALF_KEYS, capture_half

    full = fanin_coupling_capture_kwargs("shm_ring")
    assert set(CAPTURE_HALF_KEYS) == {"capture_device", "capture_format"}
    assert set(capture_half(full)) == set(CAPTURE_HALF_KEYS)
    # No coercion: the resolver's values cross unchanged, so a future type change
    # surfaces instead of being silently stringified.
    assert capture_half(full) == {k: full[k] for k in CAPTURE_HALF_KEYS}
    assert capture_half({}) == {}


def test_fanin_coupling_is_transition_owned_not_lab_overrideable():
    plan = build_audio_runtime_plan(
        overrides={COUPLING_ENV_VAR: COUPLING_SHM_RING},
        route_mode="solo",
        override_label="/var/lib/jasper/audio_runtime_overrides.json",
    )

    setting = plan.setting(COUPLING_ENV_VAR)

    assert COUPLING_ENV_VAR not in AUDIO_RUNTIME_OVERRIDE_KEYS
    assert setting.value == COUPLING_LOOPBACK
    assert setting.source_kind == "packaged_default"
    assert setting.override_value is None
    assert any(
        "is ignored" in warning
        and "jasper-fanin-coupling-reconcile" in warning
        for warning in plan.warnings
    )


def test_plan_recognizes_shm_ring_coupling_without_false_warning():
    # The Ring A transport: the plan reuses fanin_coupling's SSOT
    # (VALID_COUPLINGS), so setting JASPER_FANIN_CAMILLA_COUPLING=shm_ring
    # surfaces value=shm_ring and does NOT emit the spurious
    # "is not recognized; resolved to loopback" warning it used to when the plan
    # kept an independent {loopback, transport_pipe} set. This is the drift the
    # Ring A Rust half flagged: resolve_coupling recognized shm_ring while the
    # plan warned it did not.
    plan = build_audio_runtime_plan(
        route_mode="solo",
        fanin_env={COUPLING_ENV_VAR: COUPLING_SHM_RING},
    )

    setting = plan.setting(COUPLING_ENV_VAR)
    assert setting.value == COUPLING_SHM_RING
    assert not any(
        "is not recognized" in warning for warning in plan.warnings
    ), plan.warnings


def test_plan_valid_couplings_is_fanin_coupling_ssot():
    # SSOT identity: the plan does not keep an independent set that can drift
    # from the resolver's recognized tokens. Both include shm_ring.
    from jasper import audio_runtime_plan

    assert audio_runtime_plan._VALID_COUPLINGS is VALID_COUPLINGS
    assert set(VALID_COUPLINGS) == {COUPLING_SHM_RING}


# --------------------------------------------------------------------------
# T-5 — the narrowed shm_ring gate, DERIVED rather than parametrized.
#
# The gate's second condition is `dac_content_lane_armed`, and this block never
# invents that boolean: every cell computes it from a real `GroupingConfig`
# through `jasper.multiroom.reconcile.dac_content_lane_armed`, which is itself
# read out of the function that WRITES the lane. Parametrizing it as a free
# boolean would pin combinations the real derivation cannot produce (the
# `invalid_grouping x armed=True` cell most of all) and would assert nothing
# about what the derivation actually answers.
# --------------------------------------------------------------------------


def _grouping_cfg(**kw):
    from jasper.multiroom.config import GroupingConfig

    base = dict(
        enabled=False, role="", channel="stereo", bond_id="",
        leader_addr="", buffer_ms=400, codec="flac", error=None,
    )
    base.update(kw)
    return GroupingConfig(**base)


_SOLO = _grouping_cfg()
_VALID_LEADER = _grouping_cfg(
    enabled=True, role="leader", channel="left", bond_id="b"
)
_VALID_FOLLOWER = _grouping_cfg(
    enabled=True, role="follower", channel="right", bond_id="b",
    leader_addr="jts.local",
)
_INVALID = _grouping_cfg(
    enabled=True, role="", channel="left", bond_id="",
    error="JASPER_GROUPING_BOND_ID is empty",
)

#: (label, cfg, active_endpoint, flat_output_allowed, expected_armed).
#:
#: REACHABLE CELLS ONLY. Two constraints bound the input space, and
#: `test_t5_cells_are_reachable` re-derives both rather than trusting this
#: comment:
#:
#:   1. `active_endpoint` is `is_active_member(cfg) and box_is_active`, so a
#:      solo or invalid config can NEVER be an active endpoint.
#:   2. `topology_allows_flat_dac_graph` is true only for a saved NORMAL
#:      full-range layout, and an ACTIVE box is roleful, so
#:      `active_endpoint=True` forces `flat_output_allowed=False`.
#:
#: `flat_output_allowed=False` on a PASSIVE box is not exotic: an unsaved
#: layout classifies CONTRACT_UNCONFIGURED, which the writer treats exactly
#: like a roleful one — the lane stays cleared because nothing has declared a
#: speaker to send a flat program to.
_T5_CELLS = [
    ("solo, flat-capable",            _SOLO,           False, True,  False),
    ("solo, no flat graph",           _SOLO,           False, False, False),
    ("passive leader (DUMB member)",  _VALID_LEADER,   False, True,  True),
    ("passive leader, no flat graph", _VALID_LEADER,   False, False, False),
    ("ACTIVE-speaker leader",         _VALID_LEADER,   True,  False, False),
    ("passive follower (DUMB member)", _VALID_FOLLOWER, False, True,  True),
    ("passive follower, no flat graph", _VALID_FOLLOWER, False, False, False),
    ("ACTIVE follower",               _VALID_FOLLOWER, True,  False, False),
    ("invalid grouping, flat-capable", _INVALID,       False, True,  False),
    ("invalid grouping, no flat graph", _INVALID,      False, False, False),
]


def _t5_armed(cfg, active_endpoint: bool, flat_output_allowed: bool) -> bool:
    from jasper.multiroom.reconcile import dac_content_lane_armed

    return dac_content_lane_armed(
        cfg,
        active_endpoint=active_endpoint,
        flat_output_allowed=flat_output_allowed,
    )


def test_t5_predicate_is_read_out_of_the_writer_not_restated():
    """The whole design rests on gate and writer being ONE rule. Pin it as an
    identity against `outputd_grouping_env` itself, so a future fourth input
    (the third arrived post-seal with the output runtime contract) cannot make
    the predicate answer for a rule the writer no longer applies."""
    from jasper.multiroom.reconcile import (
        OUTPUTD_DAC_CONTENT_FIFO_ENV,
        outputd_grouping_env,
    )

    for label, cfg, endpoint, flat, _expected in _T5_CELLS:
        written = outputd_grouping_env(
            cfg, active_endpoint=endpoint, flat_output_allowed=flat
        )
        assert _t5_armed(cfg, endpoint, flat) is bool(
            written[OUTPUTD_DAC_CONTENT_FIFO_ENV]
        ), label


def test_t5_cells_are_reachable():
    """Guard the table itself: every row must be a state the system can be in.

    Without this the table is just another free-boolean matrix wearing a
    `GroupingConfig` costume — the exact failure the derived form exists to
    avoid.
    """
    from jasper.active_speaker import runtime_contract as rc
    from jasper.multiroom.config import is_active_member

    for label, cfg, endpoint, flat, _expected in _T5_CELLS:
        # (1) an active endpoint is an active MEMBER on an active box.
        if endpoint:
            assert is_active_member(cfg) is True, label
            # (2) an active box is roleful, and no roleful contract is
            #     flat-permitted (re-derived below).
            assert flat is False, label

    # Sweep EVERY classification the contract module declares through the
    # permission function, rather than hand-picking two: this is what pins
    # "an ACTIVE topology can never be flat_output_allowed" against a future
    # classification being added to the accept-set.
    permitted = set()
    for name in dir(rc):
        if not name.startswith("CONTRACT_"):
            continue
        classification = getattr(rc, name)
        contract = rc.OutputContract(
            classification=classification,
            topology_configured=True,
            main_layout="stereo",
        )
        if rc.topology_allows_flat_dac_graph(contract):
            permitted.add(name)
    assert permitted == {
        "CONTRACT_NORMAL_STEREO_FULL_RANGE",
        "CONTRACT_NORMAL_MONO_FULL_RANGE",
    }, sorted(permitted)
    assert not any(name.startswith("CONTRACT_ACTIVE_") for name in permitted)
    # An unsaved layout is not flat-permitted either — that is the cell where a
    # PASSIVE box legitimately carries flat_output_allowed=False.
    assert "CONTRACT_UNCONFIGURED" not in permitted


def test_t5_shm_ring_gate_verdict_per_cell():
    """The narrowed rule, cell by cell: `shm_ring` is blocked exactly where the
    dac_content lane is armed.

    Two flips from the pre-narrowing gate are load-bearing and named here:

    - an ACTIVE endpoint (leader or follower) is now ALLOWED. That is what the
      hazard PR exists for — a bonded ring-armed active leader — and it is safe
      because the same writer clears that box's lane.
    - `invalid_grouping` is now ALLOWED. It falls past both writer branches into
      the off-path return, and `active = enabled and error is None` means no
      bond forms at all, so the box is definitively solo. It is DETERMINATE,
      unlike `unknown` (a transient read failure), which is why relaxing it is
      not a relaxation on an indeterminate state.
    """
    from jasper.audio_runtime_plan import route_mode_from_grouping_config

    for label, cfg, endpoint, flat, expected_armed in _T5_CELLS:
        armed = _t5_armed(cfg, endpoint, flat)
        assert armed is expected_armed, label

        route_mode = route_mode_from_grouping_config(cfg)
        support = coupling_supported_for_route(
            COUPLING_SHM_RING, route_mode, dac_content_lane_armed=armed
        )
        assert support.supported is (not expected_armed), label
        assert support.coupling == COUPLING_SHM_RING, label
        if expected_armed:
            assert support.reason == "fanin_shm_ring_unsupported_with_dac_content_lane"
            assert "dac_content" in support.detail
            # The old detail promised a ring-v2 date; the block is now about a
            # lane, and the operator action is disarm-or-ungroup.
            assert "ring v2" not in support.detail

    # loopback is never blocked, whatever the lane says.
    for label, cfg, endpoint, flat, _expected in _T5_CELLS:
        support = coupling_supported_for_route(
            COUPLING_LOOPBACK,
            route_mode_from_grouping_config(cfg),
            dac_content_lane_armed=_t5_armed(cfg, endpoint, flat),
        )
        assert support.supported is True, label


def test_t5_d1_asks_the_matrix_instead_of_hand_rolling_the_rule():
    """D1 (multiroom's "ring-armed box cannot bond") and D2 (this matrix) encoded
    DIFFERENT rules and coincided only because D1 was stricter: D1 compared
    `read_persisted_coupling()` to `COUPLING_SHM_RING` and stopped there, so a
    narrowing here could not reach it. Pin that the hand-rolled comparison is
    gone and the matrix is what D1 consults — the structural half of the claim;
    the behavioural half is `tests/test_multiroom_reconcile.py`'s bond gate.
    """
    import inspect

    from jasper.multiroom import reconcile as mr

    source = inspect.getsource(mr.main)
    assert "coupling_supported_for_route(" in source
    assert "read_persisted_coupling() == COUPLING_SHM_RING" not in source
    assert "COUPLING_SHM_RING" not in source, (
        "D1 must not name a coupling token directly any more — the support "
        "matrix owns which couplings are blocked for which shape"
    )


def test_shm_ring_route_policy_allows_solo_and_unknown():
    # solo = grouping off; unknown = a transient indeterminate grouping-config read
    # that must NOT refuse a legitimate solo arm (fail-safe direction). Neither
    # consults the lane, so an armed lane cannot block them either.
    for mode in ("solo", "unknown"):
        for armed in (True, False):
            support = coupling_supported_for_route(
                COUPLING_SHM_RING, mode, dac_content_lane_armed=armed
            )
            assert support.supported is True, (mode, armed)


def test_fanin_coupling_action_blocks_shm_ring_for_armed_dac_content_lane():
    action, support = fanin_coupling_action(
        COUPLING_SHM_RING, "active_follower", dac_content_lane_armed=True
    )

    assert action is None
    assert support.supported is False
    assert support.reason == "fanin_shm_ring_unsupported_with_dac_content_lane"


def test_fanin_coupling_action_sets_supported_coupling():
    action, support = fanin_coupling_action(
        COUPLING_SHM_RING, "solo", dac_content_lane_armed=False
    )

    assert support.supported is True
    assert action is not None
    assert (action.action, action.key, action.value) == (
        "set",
        "JASPER_FANIN_CAMILLA_COUPLING",
        COUPLING_SHM_RING,
    )


def test_fanin_coupling_action_admits_a_bonded_active_endpoint():
    """The narrowing at the reconciler's own entry point: a bonded box whose
    dac_content lane is cleared (every ACTIVE endpoint) arms the ring instead of
    being force-reverted to loopback on the next boot/deploy pass."""
    action, support = fanin_coupling_action(
        COUPLING_SHM_RING, "active_leader", dac_content_lane_armed=False
    )

    assert support.supported is True
    assert action is not None
    assert action.value == COUPLING_SHM_RING


def test_transport_topology_removed_transport_pipe_falls_back_to_loopback():
    # The removed transport_pipe coupling fails safe to the loopback topology.
    loopback = transport_topology_for_coupling("loopback").to_dict()
    removed = transport_topology_for_coupling("transport_pipe").to_dict()

    assert loopback["name"] == "loopback"
    assert loopback["outputd_content_source"] == "alsa"
    assert loopback["fanin_to_camilla"]["transport"] == "alsa_loopback"
    assert removed["name"] == "loopback"
    assert removed["outputd_content_source"] == "alsa"
    assert removed["fanin_to_camilla"]["transport"] == "alsa_loopback"


def test_loopback_topology_derives_outputd_reader_from_camilla_writer():
    # Re-pointed at the PASSIVE pair, which is the only registered pairing left.
    # The property under test is the derivation — outputd's reader follows
    # CamillaDSP's writer rather than being chosen independently — and that is
    # unchanged by which pair carries it.
    topology = transport_topology_for_coupling(
        COUPLING_LOOPBACK,
        camilla_playback_device=DEFAULT_PLAYBACK_DEVICE,
    )

    assert (
        topology.camilla_to_outputd["outputd_capture_pcm"]
        == DEFAULT_OUTPUTD_CAPTURE_DEVICE
    )


def test_the_retired_aloop_active_lane_has_no_registered_capture_pairing(capsys):
    """The ONE negative guard for the retired snd-aloop ACTIVE pair.

    It replaces three tests that pinned that pairing as live: a
    derives-the-reader case, a coherence-accepts case, and a CLI-resolves case.
    Those asserted a pairing that no longer exists — #2534 deleted the PCMs and
    the ACTIVE ring is now the one legal ACTIVE endpoint, so no box has an
    outputd capture half for this device either. Absence is the assertion, and
    it is deliberately made ONCE: three separate re-points of a dead pairing
    would be three places to keep agreeing about nothing.
    """
    assert audio_config_main(
        [
            "outputd-capture-device",
            "--playback-device",
            ACTIVE_OUTPUTD_PLAYBACK_DEVICE,
        ]
    ) != 0
    assert "no outputd capture endpoint is registered" in capsys.readouterr().out


def test_audio_config_rejects_unregistered_outputd_playback(capsys):
    assert audio_config_main(
        ["outputd-capture-device", "--playback-device", "future_unknown_lane"]
    ) == 1
    assert "no outputd capture endpoint" in capsys.readouterr().out


def test_output_endpoint_evidence_preserves_missing_statefile_reason(tmp_path):
    missing = tmp_path / "missing-statefile.yml"

    evidence = output_endpoint_evidence_from_statefiles(missing)

    assert evidence.devices is None
    assert evidence.errors
    assert str(missing) in evidence.errors[0]


def test_output_endpoint_evidence_marks_non_output_graph_unknown(tmp_path):
    config = tmp_path / "program-bake.yml"
    config.write_text(
        "devices:\n"
        "  capture:\n"
        "    type: Alsa\n"
        "    device: plug:jasper_capture\n"
        "  playback:\n"
        "    type: File\n"
        "    device: /run/jasper-grouping/program.pipe\n",
        encoding="utf-8",
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {config}\n", encoding="utf-8")

    evidence = output_endpoint_evidence_from_statefiles(statefile)

    assert evidence.devices is not None
    assert evidence.endpoint_recognized is False


def test_runtime_plan_to_dict_exposes_topology_and_correction_latency_gate():
    plan = build_audio_runtime_plan(
        fanin_env={COUPLING_ENV_VAR: COUPLING_LOOPBACK},
        route_mode="solo",
    )
    payload = plan.to_dict()

    assert payload["transport_topology"]["name"] == COUPLING_LOOPBACK
    assert payload["correction_latency_eligibility"]["eligible"] is True
    assert (
        payload["correction_latency_eligibility"]["minimum_phase_or_iir"]
        is True
    )


def test_correction_latency_gate_blocks_unmeasured_or_high_delay_fir():
    iir = correction_latency_eligibility()
    minimum = correction_latency_eligibility(fir_mode="minimum_phase")
    unknown = correction_latency_eligibility(fir_mode="unknown")
    high = correction_latency_eligibility(
        fir_mode="linear_phase",
        measured_group_delay_ms=21.333,
    )
    measured_small = correction_latency_eligibility(
        fir_mode="mixed_phase",
        measured_group_delay_ms=4.0,
    )

    assert iir.eligible and iir.minimum_phase_or_iir
    assert minimum.eligible and minimum.minimum_phase_or_iir
    assert unknown.eligible is False
    assert unknown.blocking_reason == "fir_group_delay_unmeasured"
    assert high.eligible is False
    assert high.measured_group_delay_frames == 1024
    assert high.blocking_reason == "fir_group_delay_exceeds_low_latency_budget"
    assert measured_small.eligible is True
    assert measured_small.minimum_phase_or_iir is False


def test_correction_latency_gate_reads_active_fir_metadata(tmp_path):
    fir_dir = tmp_path / "fir"
    fir_dir.mkdir()
    (fir_dir / "linear.json").write_text(json.dumps({
        "mode": "linear_phase",
        "filter_group_delay_ms": 21.333,
    }))
    config = tmp_path / "correction.yml"
    config.write_text(
        "filters:\n"
        "  room_fir:\n"
        "    type: Conv\n"
        "    parameters:\n"
        "      filename: fir/linear.wav\n",
        encoding="utf-8",
    )

    verdict = correction_latency_eligibility_for_config(str(config))

    assert verdict.eligible is False
    assert verdict.measured_group_delay_frames == 1024
    assert verdict.blocking_reason == "fir_group_delay_exceeds_low_latency_budget"


def test_low_latency_route_plan_errors_on_high_latency_fir(tmp_path):
    fir_dir = tmp_path / "fir"
    fir_dir.mkdir()
    (fir_dir / "linear.json").write_text(json.dumps({
        "mode": "linear_phase",
        "filter_group_delay_ms": 21.333,
    }))
    config = tmp_path / "correction.yml"
    config.write_text(
        "filters:\n"
        "  room_fir:\n"
        "    type: Conv\n"
        "    parameters:\n"
        "      filename: fir/linear.wav\n",
        encoding="utf-8",
    )

    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        route_mode="solo",
        correction_config_path=str(config),
    )

    assert any("correction latency" in error for error in plan.errors)
    assert plan.to_dict()["correction_latency_eligibility"]["eligible"] is False


def test_correction_latency_gate_allows_peq_and_minimum_phase(tmp_path):
    peq = tmp_path / "peq.yml"
    peq.write_text("filters:\n  room_peq_1:\n    type: Biquad\n", encoding="utf-8")
    fir_dir = tmp_path / "fir"
    fir_dir.mkdir()
    (fir_dir / "minimum.json").write_text(json.dumps({
        "mode": "minimum_phase",
        "filter_group_delay_ms": 0.0,
    }))
    minimum = tmp_path / "minimum.yml"
    minimum.write_text(
        "filters:\n"
        "  room_fir:\n"
        "    type: Conv\n"
        "    parameters:\n"
        "      filename: fir/minimum.wav\n",
        encoding="utf-8",
    )

    assert correction_latency_eligibility_for_config(str(peq)).eligible is True
    verdict = correction_latency_eligibility_for_config(str(minimum))
    assert verdict.eligible is True
    assert verdict.minimum_phase_or_iir is True


def test_packaged_systemd_defaults_match_plan_constants():
    outputd_unit = (ROOT / "deploy/systemd/jasper-outputd.service").read_text()
    fanin_unit = (ROOT / "deploy/systemd/jasper-fanin.service").read_text()

    assert _env_int(outputd_unit, "JASPER_OUTPUTD_PERIOD_FRAMES") == (
        DEFAULT_OUTPUTD_PERIOD_FRAMES
    )
    assert _env_int(outputd_unit, "JASPER_OUTPUTD_CONTENT_BUFFER_FRAMES") == (
        DEFAULT_OUTPUTD_CONTENT_BUFFER_FRAMES
    )
    assert _env_int(outputd_unit, "JASPER_OUTPUTD_DAC_BUFFER_FRAMES") == (
        DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES
    )
    assert _env_int(fanin_unit, "JASPER_FANIN_INPUT_BUFFER_FRAMES") == (
        DEFAULT_FANIN_INPUT_BUFFER_FRAMES
    )
    assert _env_int(fanin_unit, "JASPER_FANIN_OUTPUT_BUFFER_FRAMES") == (
        DEFAULT_FANIN_OUTPUT_BUFFER_FRAMES
    )


def _env_int(text: str, key: str) -> int:
    match = re.search(rf'Environment="{re.escape(key)}=(\d+)"', text)
    assert match is not None, key
    return int(match.group(1))


# --- shm_ring route policy + transport topology (P2) -------------------------


def test_usb_low_latency_accepts_coherent_shm_ring_pair():
    # The coherent ring pair (Ring A + Ring B) must NOT error the plan — else a
    # ring-armed box's shipped low-latency claim goes permanently red (gap 8).
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        fanin_env={COUPLING_ENV_VAR: COUPLING_SHM_RING},
        outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring"},
        route_mode="solo",
    )
    assert plan.route_policy_errors == ()


def test_coherent_shm_ring_preserves_camilla_device_mismatch_errors(tmp_path):
    config = tmp_path / "camilla.yml"
    config.write_text(
        "devices:\n"
        "  capture:\n"
        "    type: Alsa\n"
        "    device: wrong_ring_capture\n"
        "  playback:\n"
        "    type: Alsa\n"
        "    device: wrong_ring_playback\n",
        encoding="utf-8",
    )

    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        fanin_env={COUPLING_ENV_VAR: COUPLING_SHM_RING},
        outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring"},
        route_mode="solo",
        correction_config_path=str(config),
    )

    assert len(plan.route_policy_errors) == 2
    assert any("wrong_ring_capture" in error for error in plan.route_policy_errors)
    assert any("wrong_ring_playback" in error for error in plan.route_policy_errors)


def test_usb_low_latency_rejects_partial_ring_flip_fanin_only():
    # shm_ring fan-in + direct outputd = partial flip -> rejected.
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        fanin_env={COUPLING_ENV_VAR: COUPLING_SHM_RING},
        outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: "direct"},
        route_mode="solo",
    )
    assert plan.route_policy_errors
    assert any("partial flip" in e or "shm_ring" in e for e in plan.route_policy_errors)


def test_usb_low_latency_rejects_partial_ring_flip_outputd_only():
    # loopback fan-in + shm_ring outputd = partial flip -> rejected.
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        fanin_env={COUPLING_ENV_VAR: COUPLING_LOOPBACK},
        outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring"},
        route_mode="solo",
    )
    assert plan.route_policy_errors


def test_usb_low_latency_still_accepts_default_loopback_direct():
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        route_mode="solo",
    )
    assert plan.route_policy_errors == ()


def test_transport_topology_for_shm_ring_names_both_ring_devices():
    topo = transport_topology_for_coupling(
        COUPLING_SHM_RING, fanin_env={}, outputd_env={}
    ).to_dict()
    assert topo["name"] == COUPLING_SHM_RING
    assert topo["fanin_to_camilla"]["transport"] == "shm_ring"
    assert topo["fanin_to_camilla"]["camilla_capture_device"] == "jts_ring_capture"
    assert topo["camilla_to_outputd"]["transport"] == "shm_ring"
    assert topo["camilla_to_outputd"]["camilla_playback_device"] == "jts_ring_playback"
    assert topo["camilla"]["chunksize"] == RING_CAMILLA_CHUNKSIZE
    assert topo["camilla"]["target_level"] == RING_CAMILLA_TARGET_LEVEL
    assert topo["camilla"]["queuelimit"] == RING_CAMILLA_QUEUELIMIT
    assert topo["camilla"]["enable_rate_adjust"] is False
    assert topo["outputd_content_source"] == "shm_ring"


# --- D5 (wide-output-path program): shm_ring format-coherence axis -----------


def test_transport_coherence_shm_ring_format_axis_is_quiet_with_no_outputd_evidence():
    """RENAMED (was ..._accepts_the_narrow_ring_on_a_wide_box) and RE-POINTED.

    Discovered while fixing this file's ring-default-flip fallout: the old
    docstring claimed this proved "the shm_ring coupling forces the emitted
    lane to RING_WIRE_FORMAT [narrow], so a coherent ring pair produces NO
    format-axis error [against the box-wide S32_LE default]." That was never
    actually exercised by the body below — ``outputd_env`` declares no
    ``JASPER_OUTPUTD_CONTENT_FORMAT`` at all, so the missing-evidence doctrine
    ("an end this function cannot see is not a contradiction",
    ``transport_coherence_report``'s own docstring) is what silences the format
    axis here, on every box, narrow-pinned or not — true both before and after
    ``resolve_ring_wire_format``'s default flipped WIDE (PR #2601). This is
    now the SAME scenario as
    ``test_shm_ring_format_axis_is_quiet_when_outputd_declares_nothing`` above;
    kept as its own case rather than merged, since nothing here is actually
    broken — flagged as a near-duplicate rather than restructured, per the
    surgical-changes rule. The genuine "an armed ring tolerates a differing
    box-wide default" case this docstring meant to describe is now pinned by
    ``test_shm_ring_format_axis_is_quiet_when_the_declaration_agrees`` above
    (an explicit operator narrow pin against a wide box's own declaration).
    """
    from jasper.camilla_config_contract import DEFAULT_PLAYBACK_FORMAT
    from jasper.fanin_coupling import RING_WIRE_FORMAT

    assert DEFAULT_PLAYBACK_FORMAT != RING_WIRE_FORMAT
    errors = transport_coherence_errors(
        coupling=COUPLING_SHM_RING,
        outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring"},
        camilla_devices={
            "capture_device": "jts_ring_capture",
            "playback_device": "jts_ring_playback",
        },
    )
    assert errors == ()


def test_transport_coherence_shm_ring_flags_a_ring_end_that_declares_another_wire(
    monkeypatch,
):
    """The axis can still fail (the doctor-can-fail / mutation rule).

    Its EVIDENCE changed: it compares the resolved wire against outputd's own
    declared format rather than against a second derivation of the same
    coupling kwargs, which could not disagree with the first. The failing case
    is now reachable from real per-box state, and the wire-side half of the
    mutation is pinned here — move the resolver and the axis follows it.
    """
    import jasper.fanin_coupling as coupling

    monkeypatch.setattr(
        coupling,
        "resolve_ring_wire",
        lambda topology=None: coupling.RingWire(
            sample_format="S32_LE",
            ring_a_channels=2,
            ring_b_channels=2,
            period_frames=coupling.RING_SLOT_FRAMES,
        ),
    )
    errors = transport_coherence_errors(
        coupling=COUPLING_SHM_RING,
        outputd_env={
            OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring",
            "JASPER_OUTPUTD_CONTENT_FORMAT": "S16_LE",
        },
        camilla_devices={
            "capture_device": "jts_ring_capture",
            "playback_device": "jts_ring_playback",
        },
    )
    assert len(errors) == 1
    assert "S16_LE" in errors[0] and "S32_LE" in errors[0]


def test_removed_adaptive_buffer_keys_are_read_by_nothing():
    """The deleted adaptive-shrink keys must be INERT, not merely unused here.

    `JASPER_FANIN_ADAPTIVE_BUFFER` / `_ADAPTIVE_SHRUNK_FRAMES` gated the
    mux-driven fan-in output-buffer shrink, which was removed. A migrating box
    can still carry either line in `/var/lib/jasper/fanin.env`, and the
    legacy-cleanup contract for them is *ignored*, not *honoured* and not
    *fatal* — unlike the removed `rate_match` bridge value, nothing reads these
    keys at all, so there is no reader left to warn from and a stale line is a
    no-op.

    That is only true while no source file names them. This is the guard for
    that: a resurrected read (in Python or in either Rust daemon) would
    silently give a stale operator value authority over the shared summing
    daemon's output buffer again.
    """
    removed = ("JASPER_FANIN_ADAPTIVE_BUFFER", "JASPER_FANIN_ADAPTIVE_SHRUNK_FRAMES")
    roots = (ROOT / "jasper", ROOT / "rust", ROOT / "deploy")
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix in (".woff2", ".woff", ".ttf", ".png", ".ico", ".pyc", ".so"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):  # pragma: no cover - binary/unreadable
                continue
            for key in removed:
                if key in text:
                    offenders.append(f"{path.relative_to(ROOT).as_posix()}: {key}")

    assert offenders == [], (
        "the adaptive output-buffer keys were deleted; nothing may read them "
        f"again without restoring the reconciler that owned them: {offenders}"
    )


# --- shm_ring transport WIRE: resolved, and compared against per-end evidence --


def test_shm_ring_transport_reports_the_resolved_wire_not_a_literal(monkeypatch):
    """/state names the geometry the ring is built to.

    Both channel counts and the format come from the one resolver every
    declaring end reads. Patching it must move the reported transport; a literal
    would keep reporting stereo/S16 for a ring built otherwise, which is exactly
    the kind of observability that confirms a shear instead of catching it.
    """
    import jasper.fanin_coupling as fc

    monkeypatch.setattr(
        fc,
        "resolve_ring_wire",
        lambda topology=None: fc.RingWire(
            sample_format="S32_LE",
            ring_a_channels=2,
            ring_b_channels=6,
            period_frames=fc.RING_SLOT_FRAMES,
        ),
    )
    topo = transport_topology_for_coupling(COUPLING_SHM_RING).to_dict()
    assert topo["fanin_to_camilla"]["format"] == "S32_LE"
    assert topo["fanin_to_camilla"]["channels"] == 2
    assert topo["camilla_to_outputd"]["format"] == "S32_LE"
    assert topo["camilla_to_outputd"]["channels"] == 6


def test_shm_ring_format_axis_fails_on_a_sheared_outputd_declaration(monkeypatch):
    """The format axis compares the wire against OUTPUTD'S OWN declaration.

    It used to compare two derivations of one constant, so it could not fail:
    both sides moved together by construction. outputd's
    JASPER_OUTPUTD_CONTENT_FORMAT is a per-box env value that decides which
    sample_format its attach demands, so a box where it drifted is a real,
    detectable shear.

    The resolved wire is PINNED via monkeypatch rather than left to
    resolve_ring_wire()'s ambient file-fresh read: since the resolver's default
    flipped WIDE (PR #2601), an unpinned box's resolved format now equals
    S32_LE too, which would make outputd's OWN 'S32_LE' declaration agree rather
    than shear — the opposite of what this test demonstrates. Pinning the
    resolved wire narrow (S16_LE, the ioplug's own compiled-in default) keeps
    this test's shape — and its exact original assertions — unchanged, matching
    test_shm_ring_transport_reports_the_resolved_wire_not_a_literal's existing
    pattern above.
    """
    import jasper.fanin_coupling as fc

    monkeypatch.setattr(
        fc,
        "resolve_ring_wire",
        lambda topology=None: fc.RingWire(
            sample_format="S16_LE",
            ring_a_channels=2,
            ring_b_channels=2,
            period_frames=fc.RING_SLOT_FRAMES,
        ),
    )
    errors = transport_coherence_errors(
        coupling=COUPLING_SHM_RING,
        outputd_env={
            OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring",
            "JASPER_OUTPUTD_CONTENT_FORMAT": "S32_LE",
        },
        camilla_devices={
            "capture_device": "jts_ring_capture",
            "playback_device": "jts_ring_playback",
        },
    )
    assert len(errors) == 1
    assert "JASPER_OUTPUTD_CONTENT_FORMAT='S32_LE'" in errors[0]
    assert "S16_LE" in errors[0]


def test_shm_ring_format_axis_is_quiet_when_the_declaration_agrees(monkeypatch):
    """Sibling of the shear test above: same pinned wire, an outputd declaration
    that agrees with it instead of shearing."""
    import jasper.fanin_coupling as fc

    monkeypatch.setattr(
        fc,
        "resolve_ring_wire",
        lambda topology=None: fc.RingWire(
            sample_format="S16_LE",
            ring_a_channels=2,
            ring_b_channels=2,
            period_frames=fc.RING_SLOT_FRAMES,
        ),
    )
    assert (
        transport_coherence_errors(
            coupling=COUPLING_SHM_RING,
            outputd_env={
                OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring",
                "JASPER_OUTPUTD_CONTENT_FORMAT": "S16_LE",
            },
            camilla_devices={
                "capture_device": "jts_ring_capture",
                "playback_device": "jts_ring_playback",
            },
        )
        == ()
    )


def test_shm_ring_format_axis_is_quiet_when_outputd_declares_nothing():
    # An end this function cannot see is not a contradiction — the module's
    # missing-evidence doctrine, same as an absent Camilla config.
    assert (
        transport_coherence_errors(
            coupling=COUPLING_SHM_RING,
            outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring"},
            camilla_devices={
                "capture_device": "jts_ring_capture",
                "playback_device": "jts_ring_playback",
            },
        )
        == ()
    )


def test_shm_ring_channel_axis_fails_on_a_sheared_camilla_config(monkeypatch):
    # The ring header's channel count is compared field-by-field at attach, so a
    # Camilla config declaring another width fails the ioplug open.
    #
    # The resolved wire is PINNED (same technique and reason as the format-axis
    # tests above) so the FORMAT axis stays quiet here — this test's subject is
    # the channel axis alone, and an unpinned resolve_ring_wire() would now
    # ALSO shear on format (its default flipped WIDE in PR #2601), reporting two
    # errors instead of the one this test means to isolate.
    import jasper.fanin_coupling as fc

    monkeypatch.setattr(
        fc,
        "resolve_ring_wire",
        lambda topology=None: fc.RingWire(
            sample_format="S16_LE",
            ring_a_channels=2,
            ring_b_channels=2,
            period_frames=fc.RING_SLOT_FRAMES,
        ),
    )
    errors = transport_coherence_errors(
        coupling=COUPLING_SHM_RING,
        outputd_env={
            OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring",
            "JASPER_OUTPUTD_CONTENT_FORMAT": "S16_LE",
        },
        camilla_devices={
            "capture_device": "jts_ring_capture",
            "playback_device": "jts_ring_playback",
            "capture_channels": 2,
            "playback_channels": 6,
        },
    )
    assert len(errors) == 1
    assert "playback channels=2" in errors[0]
    assert "declares 6" in errors[0]


def test_shm_ring_channel_axis_is_quiet_when_camilla_agrees(monkeypatch):
    # Same pinned-wire reasoning as the sheared-config test above: keep the
    # format axis quiet so this proves the channel axis alone is quiet too.
    import jasper.fanin_coupling as fc

    monkeypatch.setattr(
        fc,
        "resolve_ring_wire",
        lambda topology=None: fc.RingWire(
            sample_format="S16_LE",
            ring_a_channels=2,
            ring_b_channels=2,
            period_frames=fc.RING_SLOT_FRAMES,
        ),
    )
    assert (
        transport_coherence_errors(
            coupling=COUPLING_SHM_RING,
            outputd_env={
                OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring",
                "JASPER_OUTPUTD_CONTENT_FORMAT": "S16_LE",
            },
            camilla_devices={
                "capture_device": "jts_ring_capture",
                "playback_device": "jts_ring_playback",
                "capture_channels": 2,
                "playback_channels": 2,
            },
        )
        == ()
    )


@pytest.mark.parametrize(
    "marker,expected_shape",
    [
        ("", "shm_ring"),         # the full-range stereo Ring B shape
        ("1", "shm_ring_active"), # the roleful ACTIVE-ring shape
    ],
)
@pytest.mark.parametrize(
    "bridge_lines,reported",
    [
        # ABSENT resolves to outputd's own default, `direct` — the shape a box
        # takes when the ring keys were swept but the coupling line was not.
        ({}, "direct"),
        # ...and stated explicitly, which is what the loopback branch of
        # `_outputd_actions` leaves behind mid-flip.
        ({"JASPER_OUTPUTD_CONTENT_BRIDGE": "direct"}, "direct"),
    ],
)
def test_a_ring_plan_over_a_direct_bridge_is_a_contradiction(
    marker, expected_shape, bridge_lines, reported
):
    """Ring A and the post-DSP ring are ONE coupling and must move together.

    The half-flipped box this catches: fan-in is told to write Ring A while
    outputd is still on its ALSA content lane, so CamillaDSP's capture side and
    outputd's read side are on different transports and nothing crosses. It is
    an ERROR under BOTH ring shapes, which is why the marker is parametrized —
    the guard sits outside the shape branch, and a test pinned to one shape would
    pass while the other silently lost its guard.

    Kept distinct from the ring-path waypoint deliberately: this is not a
    projection lagging its source. The bridge is a transport CHOICE with its own
    writer, no derivation makes it converge, and no ladder rung creates this
    state — so it refuses rather than notes.
    """
    outputd_env = {
        "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT": marker,
        "JASPER_OUTPUTD_SHM_RING_PATH": (
            "/dev/shm/jts-ring/active-content.ring"
            if marker
            else "/dev/shm/jts-ring/content.ring"
        ),
        **bridge_lines,
    }

    report = audio_plan.transport_coherence_report(
        coupling="shm_ring", outputd_env=outputd_env
    )

    assert report.notes == (), report
    bridge_errors = [e for e in report.errors if "must move together" in e]
    assert len(bridge_errors) == 1, report.errors
    assert f"JASPER_OUTPUTD_CONTENT_BRIDGE={reported}" in bridge_errors[0]
    # The message names the plan the box is actually on, so an operator reading
    # it knows which half to move.
    assert audio_plan.transport_topology_for_coupling(
        "shm_ring", outputd_env=outputd_env
    ).name == expected_shape
