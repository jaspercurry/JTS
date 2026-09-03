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
    DEFAULT_OUTPUTD_DAC_BUFFER_FRAMES,
    DEFAULT_OUTPUTD_PERIOD_FRAMES,
    OUTPUTD_DAC_BUFFER_KEY,
    OUTPUTD_MIN_BUFFER_PERIOD_MULTIPLIER,
    OUTPUTD_PERIOD_KEY,
    PACKAGED_OUTPUTD_DEFAULT_SOURCE,
    build_audio_runtime_plan,
    correction_latency_eligibility,
    correction_latency_eligibility_for_config,
    outputd_dac_buffer_pair_error,
    outputd_env_buffer_pair_error,
    outputd_latency_floor_actions,
    output_endpoint_evidence_from_statefiles,
    resolve_audio_route_profile,
    route_owned_env_actions,
    TRANSPORT_OFF_RING,
    TRANSPORT_SHM_RING,
    transport_coherence_errors,
    transport_topology_for_coupling,
)
from jasper.camilla_config_contract import (
    ACTIVE_OUTPUTD_PLAYBACK_DEVICE,
)
from jasper.cli.audio_config import main as audio_config_main
from jasper.env_load import EnvFileState
from jasper.fanin_coupling import (
    COUPLING_ENV_VAR,
    COUPLING_SHM_RING,
    coupling_capture_kwargs_from_env,
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


def test_shm_ring_plan_keeps_the_dac_floor_as_the_camilla_policy():
    """The coupling does not rewrite the POLICY settings.

    jts.local runs the Apple floor (256/1536) across the ring healthily, so a
    plan that answered the certified ring pair here would describe a geometry no
    ordinary graph on the box emits.
    """
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
    assert (chunksize.value, target.value) == (256, 1536)
    assert chunksize.source_kind == "device_profile"
    assert target.source_kind == "device_profile"
    assert chunksize.warnings == ()
    assert target.warnings == ()


def test_plan_reports_the_emitted_geometry_the_config_declares(tmp_path):
    """POLICY and EMITTED are two facts, and the plan reports both.

    The settings answer what an emitter's fallback would read; camilla_emitted
    is a read of the config the statefile names. They differ whenever a graph
    passes its geometry explicitly or the ring's capacity clamps a floor.
    """
    config = tmp_path / "sound_current.yml"
    config.write_text(
        "devices:\n"
        "  samplerate: 48000\n"
        "  chunksize: 256\n"
        "  target_level: 1536\n"
        "  capture:\n"
        "    type: Alsa\n"
        '    device: "jts_ring_capture"\n'
        "  playback:\n"
        "    type: Alsa\n"
        '    device: "jts_ring_playback"\n'
    )

    plan = build_audio_runtime_plan(
        route_mode="solo",
        fanin_env={COUPLING_ENV_VAR: COUPLING_SHM_RING},
        outputd_env={
            "JASPER_CAMILLA_CHUNKSIZE": "1024",
            "JASPER_CAMILLA_TARGET_LEVEL": "2048",
        },
        correction_config_path=str(config),
    )

    assert plan.setting("JASPER_CAMILLA_CHUNKSIZE").value == 1024
    assert plan.setting("JASPER_CAMILLA_TARGET_LEVEL").value == 2048
    emitted = plan.camilla_emitted
    assert emitted is not None
    assert emitted.to_dict() == {
        "config_path": str(config),
        "chunksize": 256,
        "target_level": 1536,
        "capture_device": "jts_ring_capture",
        "playback_device": "jts_ring_playback",
    }
    assert plan.to_dict()["camilla_emitted"] == emitted.to_dict()
    # No config to read is reported as no observation, never as a guess.
    assert (
        build_audio_runtime_plan(
            route_mode="solo",
            fanin_env={COUPLING_ENV_VAR: COUPLING_SHM_RING},
        ).camilla_emitted
        is None
    )


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
        ("set", "JASPER_OUTPUTD_DAC_BUFFER_FRAMES", "256"),
    ]


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
    assert (
        "reason=latency-tuning-outputd-dac-buffer-1536-verified-floor" in detail
    )
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
    why: str, base_env: dict[str, str], outputd_env: dict[str, str],
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
        f"{OUTPUTD_DAC_BUFFER_KEY}=1536\n", encoding="utf-8"
    )
    fanin_env = tmp_path / "fanin.env"
    fanin_env.write_text("", encoding="utf-8")
    store = tmp_path / "audio_runtime_overrides.json"
    store.write_text(
        json.dumps({
            "kind": "jts_audio_runtime_overrides",
            "schema_version": 1,
            "overrides": {
                OUTPUTD_DAC_BUFFER_KEY: {
                    "value": "1536",
                    "created_at": "2026-07-02T00:00:00Z",
                    "reason": "latency-tuning-outputd-dac-buffer-1536-verified-floor",
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
        "reason=latency-tuning-outputd-dac-buffer-1536-verified-floor" in printed
    )


def test_audio_config_import_does_not_load_runtime_contract():
    """`jasper.cli.audio_config` is spawned six times per boot reconcile pass;
    `runtime_contract` is only needed on the active-endpoint branch of
    `validate-outputd-env`, so it must stay a call-site import (ADR-0226).
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, jasper.cli.audio_config as m; "
            "print('jasper.active_speaker.runtime_contract' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"


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
    # period 128 / dac_buffer 256.
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


def test_the_system_plan_reads_both_of_outputds_env_layers(monkeypatch, tmp_path):
    """The planner sees a bonded member's marker, which lives in the SECOND file.

    Reading `outputd.env` alone left every consumer downstream of
    `plan.transport_topology` — the route policy, the coherence report,
    `/state`'s audio graph — resolving a central-ring shape for a box that has
    none. The merged env is CARRIED on the plan so those consumers read it once
    rather than merging the two files again.
    """
    outputd_env = tmp_path / "outputd.env"
    outputd_env.write_text("", encoding="utf-8")
    grouping_env = tmp_path / "grouping-outputd.env"
    grouping_env.write_text(
        "JASPER_OUTPUTD_DAC_CONTENT_LANE=1\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        "jasper.multiroom.reconcile.OUTPUTD_GROUPING_ENV_FILE", str(grouping_env)
    )

    plan = audio_plan.build_audio_runtime_plan_from_system(
        base_env_path=str(tmp_path / "base.env"),
        outputd_env_path=str(outputd_env),
        fanin_env_path=str(tmp_path / "fanin.env"),
        grouping_env_path=str(tmp_path / "grouping.env"),
        overrides_path=str(tmp_path / "overrides.json"),
        output_hardware_state_path=str(tmp_path / "output_hardware.json"),
    )

    assert plan.transport_topology.name == audio_plan.TRANSPORT_DAC_CONTENT_RING
    assert plan.outputd_env["JASPER_OUTPUTD_DAC_CONTENT_LANE"] == "1"


def test_a_setting_is_never_attributed_to_the_grouping_env_layer(tmp_path):
    """PROVENANCE. The settings resolve from `outputd.env` alone, so a value the
    grouping layer happened to carry can never be reported as coming from the
    file an operator would go and edit."""
    def period(**kw):
        return audio_plan.build_audio_runtime_plan(
            profile_id="hifiberry_dac8x",
            outputd_env={"JASPER_OUTPUTD_PERIOD_FRAMES": "128"},
            outputd_env_label="/var/lib/jasper/outputd.env",
            **kw,
        ).setting(audio_plan.OUTPUTD_PERIOD_KEY)

    alone = period()
    with_grouping = period(
        grouping_outputd_env={"JASPER_OUTPUTD_PERIOD_FRAMES": "999"}
    )

    assert alone == with_grouping
    assert with_grouping.value == 128
    assert "999" not in str(with_grouping)


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
    assert identity["uac2_gadget_attrs"]["c_sync"] == "async"


def test_usb_low_latency_route_rejects_any_non_ring_bridge_literal():
    """The route policy compares the RAW outputd bridge literal, so a box
    carrying a stale declaration is refused whatever the value spells.

    `direct` and the `rate_match` spellings are the load-bearing cases: outputd
    still PARSES them (that is how its park refusals name the retired route),
    and a policy that resolved rather than compared would launder one into a
    green `usb_low_latency_48k` claim it must not get.
    """
    for bridge in (
        "direct",
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
            "requires JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring" in error
            for error in plan.errors
        ), f"{bridge} must not certify: {plan.errors}"

    # Positive control: the one COHERENT transport still certifies, so the loop
    # above is not passing because everything errors. Both spellings of it —
    # the token the reconciler writes, and the ABSENCE that resolves to the
    # same running transport on a box it has not written yet. A control staged
    # on `direct` would sit inside the refused set above and pass vacuously.
    for outputd_env in ({OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring"}, {}):
        ok = build_audio_runtime_plan(
            base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
            outputd_env=outputd_env,
            route_mode="solo",
        )
        assert ok.route_policy_errors == (), (outputd_env, ok.route_policy_errors)


def test_usb_low_latency_route_reads_each_daemon_own_accept_set():
    """The claim rides the one transport, and each half is asked its own rule.

    A persisted `loopback` is a coupling jasper-fanin REFUSES (exit 78,
    `rust/jasper-fanin/src/config.rs`), so the box cannot be on the measured
    transport and the claim is refused. An ABSENT key is the ring — that is
    what fan-in serves — so a box the reconciler has not written yet keeps its
    shipped claim. Demanding the written token instead turned every such box
    permanently red.
    """
    def _errors(**fanin_env):
        return build_audio_runtime_plan(
            base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
            fanin_env=fanin_env,
            route_mode="solo",
        ).route_policy_errors

    assert _errors() == ()
    assert _errors(JASPER_FANIN_CAMILLA_COUPLING="shm_ring") == ()
    assert _errors(JASPER_FANIN_CAMILLA_COUPLING="") == ()
    assert len(_errors(JASPER_FANIN_CAMILLA_COUPLING="loopback")) == 1
    assert len(_errors(JASPER_FANIN_CAMILLA_COUPLING="transport_pipe")) == 1


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


def test_capture_precedence_applies_shm_ring_when_no_stronger_topology():
    base = {"playback_pipe_path": None}
    coupling = coupling_capture_kwargs_from_env()

    merged = apply_capture_precedence(
        base,
        coupling,
        member_kwargs=base,
    )

    assert merged["capture_device"] == "jts_ring_capture"
    assert merged["playback_device"] == "jts_ring_playback"
    # The coupling carries the DEVICE axis only, so it adds exactly its four
    # keys and touches nothing the caller brought.
    assert merged == {**base, **coupling}
    assert base == {"playback_pipe_path": None}


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
    }
    grouped = {"playback_pipe_path": "/run/snapfifo"}

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

    full = coupling_capture_kwargs_from_env()
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
    assert setting.value == ""
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


# --------------------------------------------------------------------------
# The dumb-member lane rule, DERIVED rather than parametrized. Every cell is a
# real `GroupingConfig` put through `member_lane_decision`, so the table cannot
# pin a combination the system cannot be in — the failure a free-boolean matrix
# has. `test_t5_cells_are_reachable` re-derives the two constraints that bound
# the table rather than trusting the comment on it.
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

#: (label, cfg, active_endpoint, flat_output_allowed, expected_marker_armed).
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


def _t5_decision(cfg, active_endpoint: bool, flat_output_allowed: bool):
    from jasper.multiroom.dac_content_ring import DAC_CONTENT_RING_PERIOD_FRAMES
    from jasper.multiroom.reconcile import member_lane_decision

    return member_lane_decision(
        cfg,
        active_endpoint=active_endpoint,
        flat_output_allowed=flat_output_allowed,
        outputd_period_frames=DAC_CONTENT_RING_PERIOD_FRAMES,
    )


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


def test_the_lane_arms_on_exactly_the_dumb_member_cells():
    """Exactly the two DUMB-member rows arm; the other eight clear.

    The WRITER is held to the same answer in the same loop: `outputd_grouping_env`
    must emit the marker on exactly the cells the rule admits, or the two have
    drifted apart.
    """
    from jasper.fanin_coupling import dac_content_lane_marker_armed
    from jasper.multiroom.dac_content_ring import DAC_CONTENT_RING_PERIOD_FRAMES
    from jasper.multiroom.reconcile import outputd_grouping_env

    for label, cfg, endpoint, flat, expected_armed in _T5_CELLS:
        assert _t5_decision(cfg, endpoint, flat).armed is expected_armed, label
        written = outputd_grouping_env(
            cfg,
            active_endpoint=endpoint,
            flat_output_allowed=flat,
            outputd_period_frames=DAC_CONTENT_RING_PERIOD_FRAMES,
        )
        assert dac_content_lane_marker_armed(written) is expected_armed, label


def test_each_unarmed_member_cell_names_which_condition_refused_it():
    """The reason token is what the reconciler's bond refusal and the doctor
    branch on, so each unarmed MEMBER cell must carry the right one — and a
    non-member (solo, invalid) carries none, because it was never refused."""
    from jasper.multiroom.reconcile import (
        LANE_REFUSED_ACTIVE_ENDPOINT,
        LANE_REFUSED_FLAT_OUTPUT_DENIED,
        LANE_REFUSED_PERIOD,
        member_lane_decision,
    )

    for label, cfg, endpoint, flat, expected_armed in _T5_CELLS:
        decision = _t5_decision(cfg, endpoint, flat)
        if expected_armed:
            assert decision.reason == "", label
        elif not (cfg.enabled and cfg.error is None):
            assert decision.reason == "", label
        elif endpoint:
            assert decision.reason == LANE_REFUSED_ACTIVE_ENDPOINT, label
        else:
            assert decision.reason == LANE_REFUSED_FLAT_OUTPUT_DENIED, label

    # The fourth condition, on a cell that would otherwise arm.
    refused = member_lane_decision(
        _VALID_FOLLOWER,
        active_endpoint=False,
        flat_output_allowed=True,
        outputd_period_frames=1024,
    )
    assert refused.armed is False
    assert refused.reason == LANE_REFUSED_PERIOD


@pytest.mark.parametrize(
    "coupling,outputd_env",
    [
        ("loopback", {}),
        ("transport_pipe", {}),
        (COUPLING_SHM_RING, {"JASPER_OUTPUTD_CONTENT_BRIDGE": "direct"}),
    ],
)
def test_an_off_ring_box_still_publishes_ring_a_and_no_post_dsp_hop(
    coupling, outputd_env
):
    """ADR-0100: the fan-in -> CamillaDSP hop does not branch.

    Either END being off the one transport — a token fan-in refuses, or a
    content bridge that is not the ring — makes the shape off_ring, and only the
    POST-DSP half goes away with it: fan-in serves Ring A or parks, so this
    layer publishes Ring A either way and names no second route for the hop
    outputd is not taking.
    """
    topology = transport_topology_for_coupling(
        coupling, outputd_env=outputd_env
    ).to_dict()

    assert topology["name"] == TRANSPORT_OFF_RING
    assert topology["outputd_content_source"] == "alsa"
    assert topology["camilla_to_outputd"] == {"transport": None}
    assert topology["fanin_to_camilla"]["transport"] == "shm_ring"
    assert topology["fanin_to_camilla"]["camilla_capture_device"] == "jts_ring_capture"


def test_an_unwritten_coupling_key_is_the_ring():
    """The defect this replaced: an unset key resolved to a route ADR-0100
    deleted, so a healthy box the reconciler had not written yet was described
    as running one. Undeclared IS the ring — on both ends."""
    for coupling in (None, "", "   "):
        assert transport_topology_for_coupling(coupling).name == TRANSPORT_SHM_RING


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
        fanin_env={COUPLING_ENV_VAR: "loopback"},
        route_mode="solo",
    )
    payload = plan.to_dict()

    assert payload["transport_topology"]["name"] == TRANSPORT_OFF_RING
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


def test_packaged_fanin_buffer_default_matches_the_plan_constant():
    fanin_unit = (ROOT / "deploy/systemd/jasper-fanin.service").read_text()

    assert _env_int(fanin_unit, "JASPER_FANIN_INPUT_BUFFER_FRAMES") == (
        DEFAULT_FANIN_INPUT_BUFFER_FRAMES
    )


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
            rf'env_u32\(\s*"{key}",\s*{const},?\s*\)', config_rs
        ), key


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
    # A retired fan-in token + shm_ring outputd = partial flip -> rejected.
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        fanin_env={COUPLING_ENV_VAR: "loopback"},
        outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: "shm_ring"},
        route_mode="solo",
    )
    assert plan.route_policy_errors


def test_usb_low_latency_accepts_the_one_transport():
    """A reconciled box — the ring named at both ends — certifies."""
    plan = build_audio_runtime_plan(
        base_env={AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        fanin_env={COUPLING_ENV_VAR: COUPLING_SHM_RING},
        outputd_env={OUTPUTD_CONTENT_BRIDGE_KEY: COUPLING_SHM_RING},
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
    # The transport shape names DEVICES, never a latency geometry: one answer
    # for every ring box cannot describe a per-box chunk/target. The plan
    # answers that axis twice instead (settings = policy, camilla_emitted =
    # what the loaded config declares).
    assert set(topo["camilla"]) == {"capture_resampler"}
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


@pytest.mark.parametrize("marker", ["", "1"], ids=["stereo", "active"])
@pytest.mark.parametrize(
    "bridge_lines,reported",
    [
        # STATED explicitly — the only way to be off the ring now. An ABSENT
        # key used to appear here too, back when absence resolved `direct`; it
        # resolves the RING now (outputd's own default), so that row moved to
        # the coherent control below rather than being deleted.
        ({"JASPER_OUTPUTD_CONTENT_BRIDGE": "direct"}, "direct"),
        # A retired lab spelling is equally off the ring, and equally a
        # contradiction under a ring plan.
        ({"JASPER_OUTPUTD_CONTENT_BRIDGE": "rate_match"}, "rate_match"),
    ],
)
def test_a_ring_plan_over_a_direct_bridge_is_a_contradiction(
    marker, bridge_lines, reported
):
    """Ring A and the post-DSP ring are ONE coupling and must move together.

    The half-flipped box this catches: fan-in is told to write Ring A while
    outputd is still on its ALSA content lane, so CamillaDSP's capture side and
    outputd's read side are on different transports and nothing crosses. It is
    an ERROR whatever the endpoint marker says, which is why the marker is
    parametrized — the guard reads the coupling and the bridge only, and a test
    pinned to one marker would pass while the other silently lost its guard.

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
    # ...and the shape says so too: one end off the transport puts the whole box
    # off it, so no ring endpoint pair is published for a hop outputd is not
    # taking.
    assert (
        audio_plan.transport_topology_for_coupling(
            "shm_ring", outputd_env=outputd_env
        ).name
        == TRANSPORT_OFF_RING
    )


_MARKER_ARMED_ENV = {"JASPER_OUTPUTD_DAC_CONTENT_LANE": "1"}


def test_a_bonded_member_resolves_its_own_shape_and_declares_no_ring_b():
    """A DUMB bonded member is a shape of its own, not the off-ring one.

    Its post-DSP slot is the bonded RETURN ring, which its snapclient writes and
    outputd reads; CamillaDSP is not in that path, so no camilla playback device
    is published — a consumer that compared one would be comparing against a
    lane camilla does not drive. `content.source` is what outputd actually
    publishes for such a box: `alsa`, because `shm_ring` is only published while
    the CENTRAL ring is attached.
    """
    from jasper.multiroom.dac_content_ring import (
        DAC_CONTENT_RING_FILE,
        DAC_CONTENT_RING_PCM,
    )

    topology = audio_plan.transport_topology_for_coupling(
        "shm_ring", outputd_env=_MARKER_ARMED_ENV
    )

    assert topology.name == audio_plan.TRANSPORT_DAC_CONTENT_RING
    assert topology.outputd_content_source == "alsa"
    assert "camilla_playback_device" not in topology.camilla_to_outputd
    assert topology.camilla_to_outputd["path"] == DAC_CONTENT_RING_FILE
    assert topology.camilla_to_outputd["outputd_capture_pcm"] == DAC_CONTENT_RING_PCM
    # Ring A is UNCHANGED on this box: fan-in serves it here like anywhere else.
    assert (
        topology.fanin_to_camilla["camilla_capture_device"]
        == audio_plan.transport_topology_for_coupling(
            "shm_ring", outputd_env={}
        ).fanin_to_camilla["camilla_capture_device"]
    )


def test_a_bonded_member_keeps_ring_a_checked_and_claims_no_post_dsp_pair():
    """The coherence report's branch for the bonded shape.

    An unhandled shape fell through to an unconditionally empty report, which
    silently stopped Ring A being checked for every bonded member — a graph
    still sourcing the snd-aloop tap reads a device nobody writes, which is
    digital silence with every daemon reading clean. So Ring A's comparison must
    still fire here, while the bridge and post-DSP ones must NOT: a marker-armed
    box declares no bridge by construction.
    """
    from jasper.fanin_coupling import RING_CAPTURE_DEVICE

    healthy = audio_plan.transport_coherence_report(
        coupling="shm_ring",
        outputd_env=_MARKER_ARMED_ENV,
        camilla_devices={
            "capture_device": RING_CAPTURE_DEVICE,
            "playback_device": "jts_ring_playback",
        },
    )
    assert healthy.errors == (), healthy
    assert healthy.notes == (), healthy

    # The SAME call with only the capture device changed: the one error is
    # produced by that difference and nothing else.
    stranded = audio_plan.transport_coherence_report(
        coupling="shm_ring",
        outputd_env=_MARKER_ARMED_ENV,
        camilla_devices={
            "capture_device": "jasper_content_capture",
            "playback_device": "jts_ring_playback",
        },
    )
    assert len(stranded.errors) == 1, stranded
    assert stranded.notes == ()


_CONTRADICTED_ENV = {
    "JASPER_OUTPUTD_CONTENT_BRIDGE": "shm_ring",
    "JASPER_OUTPUTD_DAC_CONTENT_LANE": "1",
}


def test_the_marker_beside_a_declared_bridge_is_a_coherence_error():
    """The pair outputd bails EX_CONFIG on must reach a caller that refuses.

    It also must NOT resolve the served shape: a topology saying "bonded member,
    all well" would send every consumer past a daemon that cannot start.
    """
    report = audio_plan.transport_coherence_report(
        coupling="shm_ring", outputd_env=_CONTRADICTED_ENV
    )

    assert len(report.errors) == 1, report
    assert report.notes == ()
    # THE EXACT SHAPE, not "anything but the served one": resolving the central
    # ring here would let /state and the doctor describe a daemon that cannot
    # start as the healthy Ring A + Ring B pair.
    assert (
        audio_plan.transport_topology_for_coupling(
            "shm_ring", outputd_env=_CONTRADICTED_ENV
        ).name
        == audio_plan.TRANSPORT_OFF_RING
    )


def test_the_low_latency_route_names_the_bond_not_a_bridge_to_set():
    """A bonded member has no CENTRAL ring, so the measured pair is unavailable.

    The refusal must not tell the operator to set a bridge key: writing one
    beside the marker is exactly what parks the daemon.
    """
    plan = audio_plan.build_audio_runtime_plan(
        base_env={audio_plan.AUDIO_ROUTE_PROFILE_KEY: ROUTE_USB_LOW_LATENCY_48K},
        fanin_env={"JASPER_FANIN_CAMILLA_COUPLING": "shm_ring"},
        grouping_outputd_env={"JASPER_OUTPUTD_DAC_CONTENT_LANE": "1"},
    )

    assert plan.route_policy_reason_codes == (
        audio_plan.ROUTE_POLICY_BONDED_MEMBER,
    ), plan.route_policy_errors
    # NOT the bridge refusal: that one would tell the operator to set a key
    # whose presence beside the marker is what parks the daemon.
    assert (
        audio_plan.ROUTE_POLICY_OUTPUTD_OFF_RING
        not in plan.route_policy_reason_codes
    )


@pytest.mark.parametrize("marker", ["", "1"], ids=["stereo", "active"])
def test_an_undeclared_bridge_under_a_ring_plan_is_coherent(marker):
    """THE CONTROL for the guard above, and the defect it was inverted on.

    An UNDECLARED `JASPER_OUTPUTD_CONTENT_BRIDGE` is the ring — that is what
    `Config::from_env` runs — so a ring plan over it is the ordinary converged
    box, not a half flip. Reading absence the other way reported this box's
    transport as split, which on the doctor's split-transport check called a
    demonstrably playing speaker SILENT.
    """
    report = audio_plan.transport_coherence_report(
        coupling="shm_ring",
        outputd_env={
            "JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT": marker,
            "JASPER_OUTPUTD_SHM_RING_PATH": (
                "/dev/shm/jts-ring/active-content.ring"
                if marker
                else "/dev/shm/jts-ring/content.ring"
            ),
        },
    )

    assert [e for e in report.errors if "must move together" in e] == [], report
