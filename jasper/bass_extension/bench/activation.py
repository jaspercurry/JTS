# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Fail-closed temporary-graph-activation seam for the bench runner.

This is the single most hardware-safety-critical piece of the campaign. It
implements the mechanism fixed by
``docs/bass-extension-waves/limiter-bench-runner-activation.md``:

* **Invariant:** never write the on-disk CamillaDSP config file. Every
  activation mutates the *running* config only — ``set_active_config_raw`` for
  the structural bass block, ``patch_config`` for the per-candidate
  ``clip_limit`` — so the untouched predecessor file stays the recovery point
  and ``reload()`` restores it, including after a crash or cancel.
* Mutate only while proven at/below the safe floor; prove the read-back graph
  (via CamillaDSP's own re-serialized dialect) **before** unmute; restore via
  ``reload()`` on **every** exit and re-prove the predecessor.

The helper owns exactly ``activate -> prove -> (yield for measurement) ->
restore``. It performs no analysis, no bundle I/O, and no persistence, and it is
**not** ``apply_bass_extension`` — it calls no profile writer. It raises a typed
:class:`ActivationError` on any proof or restore failure and never proceeds
silently.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import yaml

from jasper.active_speaker.camilla_yaml import (
    BASS_EXTENSION_LT_FILTER,
    BASS_EXTENSION_SUBSONIC_FILTER,
)
from jasper.active_speaker.graph_safety import (
    bass_extension_block_valid,
    filter_param_matches,
    view_from_camilla_dict,
)
from jasper.audio_measurement.evidence_identity import json_fingerprint


class ActivationError(RuntimeError):
    """A temporary-graph activation, read-back proof, or restore failed."""


@dataclass(frozen=True, slots=True)
class PredecessorSnapshot:
    """The exact live graph captured before any mutation, the restore anchor."""

    active_config_raw: str
    config_file_path: str
    graph_fingerprint: str


@dataclass(frozen=True, slots=True)
class ActivationProof:
    """Everything the read-back proof needs, before any tone plays.

    ``profile_summary`` is the ``bass_extension_block_valid`` summary for the
    target being activated (runtime block required, owner channels, natural
    LT/subsonic params). ``expected_clip_limit_dbfs`` is the baseline for a
    discovery/reference activation and the candidate for a candidate activation.
    """

    limiter_name: str
    owner_channels: tuple[int, ...]
    profile_summary: Mapping[str, Any]
    expected_clip_limit_dbfs: float


@dataclass(frozen=True, slots=True)
class ActivationReadback:
    """The proven active graph handed to the caller for measurement."""

    active_config_raw: str
    graph_fingerprint: str
    configured_clip_limit_dbfs: float


def _parse_running_config(raw: object) -> dict[str, Any]:
    if type(raw) is not str or not raw.strip():
        raise ActivationError("active config read-back was empty")
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise ActivationError(f"active config did not parse: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ActivationError("active config is not a mapping")
    return parsed


def _graph_fingerprint(config: Mapping[str, Any]) -> str:
    return json_fingerprint(config, field_name="active graph")


def _read_configured_clip_limit(
    config: Mapping[str, Any], limiter_name: str
) -> float:
    filters = config.get("filters")
    spec = filters.get(limiter_name) if isinstance(filters, Mapping) else None
    params = spec.get("parameters") if isinstance(spec, Mapping) else None
    value = params.get("clip_limit") if isinstance(params, Mapping) else None
    if type(value) is int:
        value = float(value)
    if type(value) is not float:
        raise ActivationError(
            f"limiter {limiter_name!r} has no numeric clip_limit in the read-back"
        )
    return value


def _prove_active_graph(
    config: Mapping[str, Any],
    proof: ActivationProof,
) -> float:
    """Prove the read-back graph before unmute; return the configured clip limit.

    Raises :class:`ActivationError` on any mismatch so no tone can play.
    """

    view = view_from_camilla_dict(config)
    if not bass_extension_block_valid(view, proof.profile_summary).valid:
        raise ActivationError("read-back bass-extension block failed its safety proof")

    if not filter_param_matches(
        view,
        proof.limiter_name,
        filter_type="Limiter",
        params={"clip_limit": proof.expected_clip_limit_dbfs, "soft_clip": True},
    ):
        raise ActivationError(
            f"read-back limiter {proof.limiter_name!r} does not match the expected "
            "clip_limit / soft_clip"
        )

    owner = frozenset(proof.owner_channels)
    owner_steps = [step for step in view.pipeline_steps if step.channels == owner]
    if len(owner_steps) != 1:
        raise ActivationError("read-back graph has no single bass-owner pipeline step")
    names = list(owner_steps[0].names)
    required = (
        BASS_EXTENSION_LT_FILTER,
        BASS_EXTENSION_SUBSONIC_FILTER,
        proof.limiter_name,
    )
    if not all(name in names for name in required):
        raise ActivationError("read-back owner chain is missing a required filter")
    if not (
        names.index(BASS_EXTENSION_LT_FILTER)
        < names.index(BASS_EXTENSION_SUBSONIC_FILTER)
        < names.index(proof.limiter_name)
    ):
        raise ActivationError("read-back owner chain is out of order")

    return _read_configured_clip_limit(config, proof.limiter_name)


async def snapshot_predecessor(controller: Any) -> PredecessorSnapshot:
    """Capture the exact predecessor graph and confirm ``reload`` can restore it.

    The running config must fingerprint-match the on-disk file so that a
    ``reload()`` reproduces exactly this predecessor. A mismatch means the live
    graph was already mutated (a prior run did not restore) — fail closed rather
    than mutate a graph whose restore point diverges.
    """

    raw = await controller.get_active_config_raw()
    parsed = _parse_running_config(raw)
    path = await controller.get_config_file_path()
    if type(path) is not str or not path.strip():
        raise ActivationError("could not read the CamillaDSP config file path")
    try:
        file_text = _read_text(path)
    except OSError as exc:
        raise ActivationError(f"could not read the config file {path!r}: {exc}") from exc
    file_parsed = _parse_running_config(file_text)
    running_fp = _graph_fingerprint(parsed)
    if running_fp != _graph_fingerprint(file_parsed):
        raise ActivationError(
            "running config does not match the on-disk file; refusing to mutate "
            "because reload() could not reproduce the exact predecessor"
        )
    return PredecessorSnapshot(
        active_config_raw=raw,
        config_file_path=path,
        graph_fingerprint=running_fp,
    )


def _read_text(path: str) -> str:
    from pathlib import Path

    return Path(path).read_text(encoding="utf-8")


async def _restore_predecessor(
    controller: Any,
    predecessor: PredecessorSnapshot,
    to_floor: Callable[[], Awaitable[None]],
) -> None:
    """Fade to floor, reload the untouched file, and re-prove the predecessor."""

    await to_floor()
    await controller.reload()
    raw = await controller.get_active_config_raw()
    parsed = _parse_running_config(raw)
    if _graph_fingerprint(parsed) != predecessor.graph_fingerprint:
        raise ActivationError(
            "restore via reload() did not reproduce the exact predecessor graph"
        )


@asynccontextmanager
async def temporary_bass_activation(
    controller: Any,
    *,
    graph_raw_text: str,
    candidate_clip_limit_dbfs: float | None,
    proof: ActivationProof,
    predecessor: PredecessorSnapshot,
    to_floor: Callable[[], Awaitable[None]],
    assert_at_floor: Callable[[], Awaitable[None]],
) -> AsyncIterator[ActivationReadback]:
    """Activate a candidate on the *running* graph, prove it, yield, restore.

    The caller enters ``measurement_window()`` and snapshots the predecessor
    first, then uses this context manager. On enter: fade to floor, prove
    at-floor, apply the structural graph (and any candidate ``clip_limit``) to
    the running config only, and prove the read-back before yielding — the
    caller unmutes and measures only inside the ``with`` body. On **every** exit
    (normal, error, or cancel) the predecessor is restored via ``reload()`` and
    re-proven.
    """

    await to_floor()
    await assert_at_floor()

    # Mutate the RUNNING config only — never the on-disk file.
    await controller.set_active_config_raw(graph_raw_text)
    if candidate_clip_limit_dbfs is not None:
        await controller.patch_config(
            {
                "filters": {
                    proof.limiter_name: {
                        "parameters": {
                            "clip_limit": candidate_clip_limit_dbfs,
                            "soft_clip": True,
                        }
                    }
                }
            }
        )

    try:
        raw = await controller.get_active_config_raw()
        config = _parse_running_config(raw)
        configured = _prove_active_graph(config, proof)
        readback = ActivationReadback(
            active_config_raw=raw,
            graph_fingerprint=_graph_fingerprint(config),
            configured_clip_limit_dbfs=configured,
        )
        yield readback
    finally:
        # Restore on EVERY exit, including cancellation. Shield so a cancel of
        # the surrounding task cannot abandon the live graph in the mutated
        # state; if we are cancelled, still await the shielded restore to
        # completion before re-raising. reload() is idempotent, so restoring an
        # already-restored graph is safe.
        restore = asyncio.ensure_future(
            _restore_predecessor(controller, predecessor, to_floor)
        )
        try:
            await asyncio.shield(restore)
        except asyncio.CancelledError:
            await restore
            raise
