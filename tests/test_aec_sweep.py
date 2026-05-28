from __future__ import annotations

import json

import pytest

from jasper.aec_sweep import (
    Aec3SweepConfigError,
    DEFAULT_AEC3_SWEEP_VARIANTS,
    aec3_sweep_config_payload,
    config_metadata,
    load_aec3_sweep_config,
    validate_aec3_sweep_config_payload,
    variant_metadata,
    write_aec3_sweep_config,
)


def test_missing_runtime_config_uses_built_in_defaults(tmp_path) -> None:
    path = tmp_path / "missing.json"

    config = load_aec3_sweep_config(path)

    assert config.source == "default"
    assert config.path == str(path)
    assert config.error is None
    assert config.variants == DEFAULT_AEC3_SWEEP_VARIANTS
    assert len(config.config_hash) == 12


def test_valid_runtime_config_overrides_labels_and_knobs(tmp_path) -> None:
    payload = aec3_sweep_config_payload()
    payload["variants"][0]["label"] = "AEC3 test A"
    payload["variants"][0]["env_overrides"] = {
        "JASPER_AEC_CONSERVATIVE_HF": False,
        "JASPER_AEC_DND_HOLD_DURATION": 125,
        "JASPER_AEC_MAX_DEC_LF": 0.015,
        "JASPER_AEC_NS_LEVEL": "high",
    }
    path = tmp_path / "aec3_sweep_variants.json"
    path.write_text(json.dumps(payload))

    config = load_aec3_sweep_config(path, strict=True)

    assert config.source == "file"
    assert config.variants[0].leg == "aec3_variant_1"
    assert config.variants[0].label == "AEC3 test A"
    assert config.variants[0].env_overrides == {
        "JASPER_AEC_CONSERVATIVE_HF": "0",
        "JASPER_AEC_DND_HOLD_DURATION": "125",
        "JASPER_AEC_MAX_DEC_LF": "0.015",
        "JASPER_AEC_NS_LEVEL": "high",
    }


def test_runtime_config_rejects_unknown_knobs() -> None:
    payload = aec3_sweep_config_payload()
    payload["variants"][0]["env_overrides"]["JASPER_AEC_NOT_REAL"] = "1"

    with pytest.raises(Aec3SweepConfigError, match="unknown AEC3 sweep knob"):
        validate_aec3_sweep_config_payload(payload)


def test_runtime_config_requires_stable_three_slots() -> None:
    payload = aec3_sweep_config_payload()
    payload["variants"] = payload["variants"][:2]

    with pytest.raises(Aec3SweepConfigError, match="exactly 3 variants"):
        validate_aec3_sweep_config_payload(payload)

    payload = aec3_sweep_config_payload()
    payload["variants"][0]["leg"] = "aec3_other"
    with pytest.raises(Aec3SweepConfigError, match="variant 1 leg"):
        validate_aec3_sweep_config_payload(payload)


def test_invalid_runtime_config_falls_back_unless_strict(tmp_path) -> None:
    path = tmp_path / "aec3_sweep_variants.json"
    path.write_text("{not json")

    config = load_aec3_sweep_config(path)

    assert config.source == "default"
    assert config.error
    assert config.variants == DEFAULT_AEC3_SWEEP_VARIANTS
    with pytest.raises(Aec3SweepConfigError):
        load_aec3_sweep_config(path, strict=True)


def test_write_runtime_config_is_atomic_and_metadata_ready(tmp_path) -> None:
    path = tmp_path / "aec3_sweep_variants.json"
    payload = aec3_sweep_config_payload()
    payload["variants"][1]["label"] = "AEC3 test B"

    config = write_aec3_sweep_config(payload, path)

    assert config.source == "file"
    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    assert variant_metadata(config.variants)[1]["label"] == "AEC3 test B"
    assert config_metadata(config)["hash"] == config.config_hash
