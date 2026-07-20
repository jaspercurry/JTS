# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The fail-closed activation seam: prove-before-unmute, restore on every exit."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from jasper.bass_extension.bench import activation
from jasper.bass_extension.bench.activation import (
    ActivationError,
    ActivationProof,
    snapshot_predecessor,
    temporary_bass_activation,
)

PREDECESSOR_YAML = "filters: {}\npipeline: []\n"
CANDIDATE_YAML = "filters:\n  baseline_limiter_woofer:\n    type: Limiter\npipeline: []\n"


class FakeController:
    def __init__(self, config_path: Path, *, reload_yaml: str | None = None) -> None:
        self.active_raw = PREDECESSOR_YAML
        self.config_path = config_path
        self._reload_yaml = reload_yaml if reload_yaml is not None else PREDECESSOR_YAML
        self.calls: list[str] = []
        self.patches: list[dict[str, Any]] = []

    async def get_active_config_raw(self) -> str:
        return self.active_raw

    async def get_config_file_path(self) -> str:
        return str(self.config_path)

    async def set_active_config_raw(self, raw: str) -> bool:
        self.calls.append("set_active_config_raw")
        self.active_raw = raw
        return True

    async def patch_config(self, patch: dict[str, Any]) -> bool:
        self.calls.append("patch_config")
        self.patches.append(patch)
        return True

    async def reload(self) -> bool:
        self.calls.append("reload")
        self.active_raw = self._reload_yaml
        return True


def _proof(clip: float = -1.0) -> ActivationProof:
    return ActivationProof(
        limiter_name="baseline_limiter_woofer",
        owner_channels=(2,),
        profile_summary={"runtime_block_required": True},
        expected_clip_limit_dbfs=clip,
    )


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "active.yml"
    path.write_text(PREDECESSOR_YAML, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _prove_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise the lifecycle, not the graph-proof primitives (owned + tested in
    # graph_safety). The proof returns the configured clip the caller expects.
    monkeypatch.setattr(
        activation,
        "_prove_active_graph",
        lambda config, proof: proof.expected_clip_limit_dbfs,
    )


async def _floor(calls: list[str]):
    async def to_floor() -> None:
        calls.append("to_floor")

    async def assert_at_floor() -> None:
        calls.append("assert_at_floor")

    return to_floor, assert_at_floor


async def test_discovery_activation_mutates_running_only_and_restores(config_file: Path) -> None:
    controller = FakeController(config_file)
    predecessor = await snapshot_predecessor(controller)
    floor_calls: list[str] = []
    to_floor, assert_at_floor = await _floor(floor_calls)

    unmuted = False
    async with temporary_bass_activation(
        controller,
        graph_raw_text=CANDIDATE_YAML,
        candidate_clip_limit_dbfs=None,
        proof=_proof(-1.0),
        predecessor=predecessor,
        to_floor=to_floor,
        assert_at_floor=assert_at_floor,
    ) as readback:
        unmuted = True  # the caller unmutes only inside the body
        assert readback.configured_clip_limit_dbfs == -1.0

    assert unmuted is True
    assert "set_active_config_raw" in controller.calls
    assert "patch_config" not in controller.calls  # discovery keeps the baseline
    assert "reload" in controller.calls  # restore via reload, never a file write
    assert "assert_at_floor" in floor_calls
    # The on-disk file is never written.
    assert config_file.read_text(encoding="utf-8") == PREDECESSOR_YAML
    assert controller.active_raw == PREDECESSOR_YAML  # restored


async def test_candidate_activation_patches_the_clip_limit(config_file: Path) -> None:
    controller = FakeController(config_file)
    predecessor = await snapshot_predecessor(controller)
    to_floor, assert_at_floor = await _floor([])

    async with temporary_bass_activation(
        controller,
        graph_raw_text=CANDIDATE_YAML,
        candidate_clip_limit_dbfs=-20.0,
        proof=_proof(-20.0),
        predecessor=predecessor,
        to_floor=to_floor,
        assert_at_floor=assert_at_floor,
    ):
        pass

    assert controller.patches == [
        {
            "filters": {
                "baseline_limiter_woofer": {
                    "parameters": {"clip_limit": -20.0, "soft_clip": True}
                }
            }
        }
    ]


async def test_readback_proof_failure_refuses_before_unmute_and_restores(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = FakeController(config_file)
    predecessor = await snapshot_predecessor(controller)
    to_floor, assert_at_floor = await _floor([])

    def _reject(config: Any, proof: Any) -> float:
        raise ActivationError("read-back mismatch")

    monkeypatch.setattr(activation, "_prove_active_graph", _reject)

    unmuted = False
    with pytest.raises(ActivationError):
        async with temporary_bass_activation(
            controller,
            graph_raw_text=CANDIDATE_YAML,
            candidate_clip_limit_dbfs=-20.0,
            proof=_proof(-20.0),
            predecessor=predecessor,
            to_floor=to_floor,
            assert_at_floor=assert_at_floor,
        ):
            unmuted = True

    assert unmuted is False  # no tone can play on an unproven graph
    assert "reload" in controller.calls  # restored anyway
    assert controller.active_raw == PREDECESSOR_YAML


async def test_body_exception_still_restores(config_file: Path) -> None:
    controller = FakeController(config_file)
    predecessor = await snapshot_predecessor(controller)
    to_floor, assert_at_floor = await _floor([])

    with pytest.raises(RuntimeError, match="boom"):
        async with temporary_bass_activation(
            controller,
            graph_raw_text=CANDIDATE_YAML,
            candidate_clip_limit_dbfs=None,
            proof=_proof(-1.0),
            predecessor=predecessor,
            to_floor=to_floor,
            assert_at_floor=assert_at_floor,
        ):
            raise RuntimeError("boom")

    assert "reload" in controller.calls
    assert controller.active_raw == PREDECESSOR_YAML


async def test_cancellation_still_restores(config_file: Path) -> None:
    controller = FakeController(config_file)
    predecessor = await snapshot_predecessor(controller)
    to_floor, assert_at_floor = await _floor([])
    entered = asyncio.Event()

    async def run() -> None:
        async with temporary_bass_activation(
            controller,
            graph_raw_text=CANDIDATE_YAML,
            candidate_clip_limit_dbfs=None,
            proof=_proof(-1.0),
            predecessor=predecessor,
            to_floor=to_floor,
            assert_at_floor=assert_at_floor,
        ):
            entered.set()
            await asyncio.sleep(3600)

    task = asyncio.ensure_future(run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "reload" in controller.calls  # restore ran despite the cancel
    assert controller.active_raw == PREDECESSOR_YAML


async def test_snapshot_refuses_when_running_diverges_from_file(config_file: Path) -> None:
    controller = FakeController(config_file)
    controller.active_raw = CANDIDATE_YAML  # running != file
    with pytest.raises(ActivationError):
        await snapshot_predecessor(controller)


async def test_restore_failure_is_typed_and_fail_closed(config_file: Path) -> None:
    # reload() reverts to a graph that is NOT the predecessor -> restore refuses.
    controller = FakeController(config_file, reload_yaml="filters:\n  x: {}\npipeline: []\n")
    predecessor = await snapshot_predecessor(controller)
    to_floor, assert_at_floor = await _floor([])

    with pytest.raises(ActivationError):
        async with temporary_bass_activation(
            controller,
            graph_raw_text=CANDIDATE_YAML,
            candidate_clip_limit_dbfs=None,
            proof=_proof(-1.0),
            predecessor=predecessor,
            to_floor=to_floor,
            assert_at_floor=assert_at_floor,
        ):
            pass
