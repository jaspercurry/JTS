from __future__ import annotations

import json
from pathlib import Path

import pytest

from jasper.correction.runtime_safety import (
    CorrectionRuntimeSafetyError,
    flat_measurement_config_path,
    reset_config_path,
)

from tests.test_active_speaker_runtime_contract import (
    _active_topology,
    _active_yaml,
    _flat_yaml,
    _full_range_mono,
    _full_range_stereo,
    _staged_metadata,
)


def test_flat_measurement_allows_full_range_stereo(tmp_path: Path) -> None:
    base = tmp_path / "outputd-cutover.yml"
    base.write_text(_flat_yaml(), encoding="utf-8")

    assert flat_measurement_config_path(
        base,
        topology=_full_range_stereo(),
    ) == base


def test_flat_measurement_rejects_active_topology(tmp_path: Path) -> None:
    base = tmp_path / "outputd-cutover.yml"
    base.write_text(_flat_yaml(), encoding="utf-8")

    with pytest.raises(CorrectionRuntimeSafetyError, match="flat sweep is unsafe"):
        flat_measurement_config_path(
            base,
            topology=_active_topology("mono", "active_2_way"),
        )


def test_flat_measurement_rejects_mono_topology_wider_than_graph(
    tmp_path: Path,
) -> None:
    base = tmp_path / "outputd-cutover.yml"
    base.write_text(_flat_yaml(), encoding="utf-8")

    with pytest.raises(CorrectionRuntimeSafetyError, match="exposes 2 output"):
        flat_measurement_config_path(base, topology=_full_range_mono())


def test_flat_measurement_rejects_corrupt_saved_topology(
    monkeypatch,
    tmp_path: Path,
) -> None:
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    base = tmp_path / "outputd-cutover.yml"
    base.write_text(_flat_yaml(), encoding="utf-8")

    with pytest.raises(CorrectionRuntimeSafetyError, match="not valid JSON"):
        flat_measurement_config_path(base)


def test_reset_selects_staged_active_startup_for_active_topology(
    monkeypatch,
    tmp_path: Path,
) -> None:
    topology = _active_topology("mono", "active_2_way")
    flat = tmp_path / "outputd-cutover.yml"
    flat.write_text(_flat_yaml(), encoding="utf-8")
    staged = tmp_path / "active_speaker_staged_startup.yml"
    staged.write_text(_active_yaml("mono", 2, frozenset()), encoding="utf-8")
    metadata = tmp_path / "active_speaker_staged_config.json"
    metadata.write_text(
        json.dumps(_staged_metadata(topology, staged)),
        encoding="utf-8",
    )
    statefile = tmp_path / "outputd-statefile.yml"
    statefile.write_text(f"config_path: {flat}\n", encoding="utf-8")
    monkeypatch.setenv("JASPER_ACTIVE_SPEAKER_STAGED_METADATA_PATH", str(metadata))

    assert reset_config_path(
        flat,
        statefile_path=statefile,
        topology=topology,
    ) == staged


def test_reset_rejects_corrupt_saved_topology(
    monkeypatch,
    tmp_path: Path,
) -> None:
    topology_path = tmp_path / "output_topology.json"
    topology_path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("JASPER_OUTPUT_TOPOLOGY_PATH", str(topology_path))
    base = tmp_path / "outputd-cutover.yml"
    base.write_text(_flat_yaml(), encoding="utf-8")

    with pytest.raises(CorrectionRuntimeSafetyError, match="not valid JSON"):
        reset_config_path(base)
