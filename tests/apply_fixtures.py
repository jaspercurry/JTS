# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bank a candidate and a completed summed trial through the evidence writers."""
import asyncio
import json
from pathlib import Path

from jasper.active_speaker import bundles
from jasper.active_speaker.bundles import open_bundle
from jasper.active_speaker.commissioning_evidence_store import CommissioningEvidenceStore
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
from jasper.active_speaker.crossover_v2.refusal_copy import TakeVerdict
from jasper.active_speaker.run_manifest import RunManifest
from jasper.audio_measurement.bundles import record_artifact


async def bank_trial_async(candidate, profile, topology, *, record_fields=None):
    root = bundles.sessions_dir()
    candidate_path = root / f"authored-{candidate.fingerprint}" / "evidence/v1/artifacts/crossover_v2/authored/candidate.json"
    candidate_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_path.write_text(json.dumps(candidate.to_dict()))
    info = open_bundle(topology, calibration_id="", sessions_dir=root)
    bundle = Path(info["bundle_dir"])
    wav = bundle / "capture.wav"
    wav.write_bytes(b"captured trial bytes")
    identity = record_artifact(bundle, wav, kind="jts_capture_wav", sensitivity="audio",
                               recomputable=False, generated_by="test")
    store = BankedRecordStore(CommissioningEvidenceStore.open(bundle, expected_session_id=info["session_id"]), "trial")
    graph = str((profile.get("config") or {}).get("sha256") or "")[:16]
    record = {"kind": POSITION_EVIDENCE_KIND, "measure_kind": "candidate", "session_id": "trial",
              "take_id": "trial_01", "candidate_id": candidate.fingerprint, "role": "summed",
              "graph_scope": "candidate", "graph_fingerprint": graph, "measurement_status": "captured",
              "incident": "", "level_db": -25., "wav_path": wav.name, "wav_sha256": identity["sha256"],
              **(record_fields or {})}
    manifest = RunManifest("trial", store)
    stop = {"index": 1, "repeat": 1, "pose": {"kind": "bearing", "distance_m": 1., "position_deg": 0}}
    manifest.planned = [stop]
    manifest.begin(stop, attempt=1, pose_index=1)

    record_id = await store.bank(record)
    await manifest.append(record, record_id, TakeVerdict(True), complete=True, started_s=0., ended_s=1.)
    manifest.finalized = True
    await manifest.persist()
    return bundle, manifest


def bank_trial(candidate, profile, topology, *, record_fields=None):
    return asyncio.run(bank_trial_async(candidate, profile, topology, record_fields=record_fields))
