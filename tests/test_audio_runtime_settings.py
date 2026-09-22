# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jasper import audio_runtime_settings as audio_settings
from jasper.audio_hardware.dac import (
    APPLE_USB_C_DONGLE_ID,
    latency_floor_for,
)
from jasper.audio_runtime_overrides import (
    DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH,
    RuntimeOverrideEntry,
)
from jasper.audio_runtime_settings import (
    AUDIO_ROUTE_PROFILE_KEY,
    DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES,
    DEFAULT_OUTPUTD_PERIOD_FRAMES,
    FANIN_USB_DIRECT_PERIOD_KEY,
    OUTPUTD_DAC_BUFFER_KEY,
    OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER,
    OUTPUTD_PERIOD_KEY,
    PACKAGED_OUTPUTD_DEFAULT_SOURCE,
    ROUTE_USB_LOW_LATENCY_48K,
    outputd_dac_buffer_pair_error,
    outputd_env_buffer_pair_error,
)
from jasper.audio_runtime_plan import build_audio_runtime_plan
from jasper.camilla_config_contract import DEFAULT_TARGET_LEVEL


ROOT = Path(__file__).resolve().parents[1]


def test_plan_uses_dac_profile_floor_as_intended_source():
    plan = build_audio_runtime_plan(
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
        outputd_env={
            "JASPER_OUTPUTD_PERIOD_FRAMES": "128",
            "JASPER_OUTPUTD_DAC_BUFFER_FRAMES": "256",
        },
    )

    assert plan.setting("JASPER_OUTPUTD_PERIOD_FRAMES").value == 128
    assert plan.setting("JASPER_OUTPUTD_DAC_BUFFER_FRAMES").value == 256
    assert plan.setting(OUTPUTD_PERIOD_KEY).source_kind == "device_profile"
    assert plan.warnings == ()


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

    setting = audio_settings.resolve_profile_floor_int(
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

    policy_setting = audio_settings.resolve_profile_floor_int(
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


def test_invalid_lab_override_is_ignored_with_warning():
    plan = build_audio_runtime_plan(
        overrides={"JASPER_CAMILLA_TARGET_LEVEL": "bad"},
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )

    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").value == DEFAULT_TARGET_LEVEL
    assert any(
        "audio_runtime_overrides" in warning and "invalid" in warning
        for warning in plan.warnings
    )


def test_stale_generated_floor_warns_against_device_profile():
    plan = build_audio_runtime_plan(
        outputd_env={OUTPUTD_PERIOD_KEY: "1024"},
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )

    setting = plan.setting(OUTPUTD_PERIOD_KEY)
    assert setting.value == 128
    assert setting.source_kind == "device_profile"
    assert setting.warnings


def test_python_outputd_buffer_contract_matches_rust_validator():
    """Pins the Python-side error message and multiplier the doctor/CLI show
    the operator; Rust's own `validate_buffer` is pinned by its own test
    suite, not scraped here (issue #3461)."""
    assert OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER == 2
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

    assert (
        outputd_dac_buffer_pair_error(
            period_frames=DEFAULT_OUTPUTD_PERIOD_FRAMES,
            dac_buffer_frames=DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES,
        )
        is None
    )
    assert latency_floor_for("hifiberry_dac8x_studio") is None
    # And the composite is deliberately NO LONGER floor-less. Asserted here, at
    # the test whose premise it was, so a revert of item 1c cannot quietly
    # restore the old exemplar and leave this docstring lying.
    assert latency_floor_for("dual_apple_usb_c_dac_4ch") is not None
    assert outputd_env_buffer_pair_error(base_env={}, outputd_env={}) is None


def _override(
    value: str, *, reason: str = "r", created_at: str = ""
) -> RuntimeOverrideEntry:
    return RuntimeOverrideEntry(
        key=OUTPUTD_DAC_BUFFER_KEY,
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
        base_env={OUTPUTD_DAC_BUFFER_KEY: "1536"},
        outputd_env={},
        base_label=_BASE_LABEL,
        outputd_label=_OUTPUTD_LABEL,
    )
    assert operator is not None
    assert f"{OUTPUTD_DAC_BUFFER_KEY}=1536 comes from {_BASE_LABEL}" in operator
    assert (
        f"{OUTPUTD_PERIOD_KEY}=1024 comes from {PACKAGED_OUTPUTD_DEFAULT_SOURCE}"
        in operator
    )

    generated = outputd_env_buffer_pair_error(
        base_env={},
        outputd_env={OUTPUTD_DAC_BUFFER_KEY: "1536"},
        base_label=_BASE_LABEL,
        outputd_label=_OUTPUTD_LABEL,
    )
    assert generated is not None
    assert f"{OUTPUTD_DAC_BUFFER_KEY}=1536 comes from {_OUTPUTD_LABEL}" in generated


def test_buffer_pair_refusal_is_the_bare_rust_mirror_without_labels():
    """Unlabelled callers keep the byte-identical daemon message."""

    assert outputd_env_buffer_pair_error(
        base_env={},
        outputd_env={OUTPUTD_DAC_BUFFER_KEY: "1536"},
    ) == (
        "JASPER_OUTPUTD_DAC_BUFFER_FRAMES=1536 must be >= "
        "2 x JASPER_OUTPUTD_PERIOD_FRAMES=1024 (minimum ALSA jitter margin)"
    )
    # One label is not enough: a half-labelled provenance would name one layer
    # and guess the other.
    assert ";" not in (
        outputd_env_buffer_pair_error(
            base_env={},
            outputd_env={OUTPUTD_DAC_BUFFER_KEY: "1536"},
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
        outputd_env={OUTPUTD_DAC_BUFFER_KEY: "1536"},
        base_label=_BASE_LABEL,
        outputd_label=_OUTPUTD_LABEL,
        override_entries={
            OUTPUTD_DAC_BUFFER_KEY: _override(
                "1536",
                reason="latency-tuning-outputd-dac-buffer-1536-verified-floor",
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
        outputd_env={OUTPUTD_DAC_BUFFER_KEY: "1536"},
        base_label=_BASE_LABEL,
        outputd_label=_OUTPUTD_LABEL,
        override_entries={OUTPUTD_DAC_BUFFER_KEY: _override("1536")},
        override_label="/run/test/overrides.json",
    )
    assert relocated is not None
    assert "/run/test/overrides.json" in relocated
    assert DEFAULT_AUDIO_RUNTIME_OVERRIDES_PATH not in relocated
    assert "reason=latency-tuning-outputd-dac-buffer-1536-verified-floor" in detail
    # The remediation must be runnable AS PRINTED — the actual key, never a
    # `<key>` placeholder the operator has to translate.
    assert f"jasper-audio-config overrides-clear {OUTPUTD_DAC_BUFFER_KEY}" in detail
    assert "<key>" not in detail


@pytest.mark.parametrize(
    "why, base_env, outputd_env, entry",
    [
        pytest.param(
            "the store disagrees with the value on disk",
            {},
            {OUTPUTD_DAC_BUFFER_KEY: "1536"},
            _override("2048"),
            id="store-value-mismatch",
        ),
        pytest.param(
            "jasper.env owns the line, not the store",
            {OUTPUTD_DAC_BUFFER_KEY: "1536"},
            {},
            _override("1536"),
            id="operator-layer-owns-it",
        ),
    ],
)
def test_buffer_pair_refusal_does_not_misattribute_to_the_override_store(
    why: str,
    base_env: dict[str, str],
    outputd_env: dict[str, str],
    entry: RuntimeOverrideEntry,
):
    """A wrong origin is worse than a missing one — it sends the fix elsewhere."""

    detail = outputd_env_buffer_pair_error(
        base_env=base_env,
        outputd_env=outputd_env,
        base_label=_BASE_LABEL,
        outputd_label=_OUTPUTD_LABEL,
        override_entries={OUTPUTD_DAC_BUFFER_KEY: entry},
    )
    assert detail is not None, why
    assert "override store" not in detail, why
    assert "overrides-clear" not in detail, why


def test_bad_operator_value_is_ignored_and_warned():
    plan = build_audio_runtime_plan(
        base_env={"JASPER_CAMILLA_TARGET_LEVEL": "rough-test"},
        outputd_env={"JASPER_CAMILLA_TARGET_LEVEL": "1536"},
        profile_id=APPLE_USB_C_DONGLE_ID,
        route_mode="solo",
    )

    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").value == DEFAULT_TARGET_LEVEL
    assert any(
        "rough-test" in warning and "ignored" in warning for warning in plan.warnings
    )


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
        item for item in plan.settings if item.key == FANIN_USB_DIRECT_PERIOD_KEY
    )
    assert setting.value == expected
    bound_warning = any("must be 32..1024" in warning for warning in setting.warnings)
    assert bound_warning is invalid


def test_packaged_outputd_defaults_match_the_rust_daemon():
    """The two outputd frame defaults have ONE owner: the daemon that runs them.

    Nothing writes them into an env file — jasper-outputd.service deliberately
    carries no `Environment=` for either key, because systemd applies env in
    file order and a literal there would beat /etc/jasper/jasper.env, the
    documented operator seam. So the bottom layer this module models IS
    `Config::from_env`'s fallback, and this compares the two rather than letting
    a third copy drift.
    """
    config_rs = (ROOT / "rust" / "jasper-outputd" / "src" / "config.rs").read_text(
        encoding="utf-8"
    )

    def _rust_const(name: str) -> int:
        match = re.search(rf"pub const {name}: u32 = ([\d_]+);", config_rs)
        assert match is not None, name
        return int(match.group(1).replace("_", ""))

    assert _rust_const("DEFAULT_PERIOD_FRAMES") == DEFAULT_OUTPUTD_PERIOD_FRAMES
    assert _rust_const("DEFAULT_DAC_BUFFER_FRAMES") == DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES
    # ...and the daemon really does resolve the keys against them, so an absent
    # key lands on the pair above rather than on a hard failure.
    for key, const in (
        ("JASPER_OUTPUTD_PERIOD_FRAMES", "DEFAULT_PERIOD_FRAMES"),
        ("JASPER_OUTPUTD_DAC_BUFFER_FRAMES", "DEFAULT_DAC_BUFFER_FRAMES"),
    ):
        assert re.search(
            rf'env_u32_positive_or_bail\(\s*"{key}",\s*{const},?\s*\)', config_rs
        ), key
