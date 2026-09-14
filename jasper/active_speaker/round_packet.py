# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Collect a level sequence in one bundle and finish its analysis packet."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from jasper.atomic_io import atomic_write_json

from .crossover_v2.refusal_copy import CrossoverV2Refused
from .run_manifest import RUN_MANIFEST_KIND, RunManifest

PACKET_FILENAME = "round_packet.json"


class RoundPacket:
    def __init__(self, manifest: RunManifest, schedule: Mapping[str, Any]) -> None:
        self.manifest, self.schedule = manifest, schedule
        self.runs: dict[str, Mapping[str, Any]] = {}
        self.finalized = False

    def to_dict(self) -> dict[str, Any]:
        runs = list(self.runs.values())
        sets: dict[str, dict[str, Any]] = {}
        for run in runs:
            for group in run["sets"]:
                merged = sets.setdefault(group["set_id"], {**group, "takes": []})
                merged["takes"].extend({**take, "run_id": run["run_id"]} for take in group["takes"])
        first = runs[0] if runs else self.manifest.to_dict()
        return {**first, "run_id": self.manifest.run_id, "sets": list(sets.values()),
                "schedule": self.schedule,
                "runs": [{key: run[key] for key in ("run_id", "level", "status", "reason", "request_fingerprint")}
                         for run in runs],
                "level": {"session": first["level"].get("session")},
                "finalized": self.finalized,
                "status": "complete" if self.finalized and runs and all(run["status"] == "complete" for run in runs)
                          and not self.manifest.reason else "partial",
                "reason": self.manifest.reason or next((run["reason"] for run in runs if run["reason"]), ""),
                "honoured": {**first["honoured"], **{
                    key: sum(run["honoured"][key] for run in runs)
                    for key in ("mic_moves", "stops_planned", "takes_measured", "takes_refused")}},
                "attempts": sum(run["attempts"] for run in runs),
                "wall_s": [value for run in runs for value in run["wall_s"]],
                "not_measured": [{**take, "run_id": run["run_id"]} for run in runs for take in run["not_measured"]]}

    async def bank(self, record: Mapping[str, Any]) -> str:
        if record.get("kind") == RUN_MANIFEST_KIND:
            self.runs[record["run_id"]] = record
            record = self.to_dict()
        return await self.manifest.records.bank(record)

    async def finish(self) -> None:
        self.finalized = True
        self.manifest.path = await self.manifest.records.bank(self.to_dict())


def finish_bass_packet(round_dir: Path, manifest_path: Path, *, join_levels: Callable[..., Path]) -> Path | None:
    manifest = json.loads(manifest_path.read_text())
    if "runs" not in manifest:
        return None
    candidates = sorted({row["capture_basis"]["candidate_id"] for row in manifest["sets"] if not row["base"]})
    try:
        table_path = join_levels([round_dir], candidates=[Path(candidate) for candidate in candidates])
        table = json.loads(table_path.read_text())
    except (CrossoverV2Refused, OSError, ValueError, KeyError) as exc:
        table = {"status": "unavailable", "code": getattr(exc, "code", "bass_fit_inputs_missing"),
                 "error_type": type(exc).__name__}
    destination = manifest_path.parent / PACKET_FILENAME
    atomic_write_json(destination, {"schema": "jts_round_packet/1", "run_id": manifest["run_id"],
                                   "status": manifest["status"], "runs": manifest["runs"],
                                   "manifest": str(manifest_path), "bass_table": table})
    return destination
