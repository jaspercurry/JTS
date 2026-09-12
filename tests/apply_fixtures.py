# SPDX-FileCopyrightText: 2026 Jasper Curry
# SPDX-License-Identifier: Apache-2.0

"""Bank a candidate and a completed summed trial through the evidence writers."""
import asyncio
import json
from pathlib import Path

from jasper.active_speaker import bundles
from jasper.active_speaker.candidate_bank import find_banked_candidate, CandidateBankRefusal
from jasper.active_speaker.bundles import open_bundle
from jasper.active_speaker.commissioning_evidence_store import CommissioningEvidenceStore
from jasper.active_speaker.crossover_v2.contracts import POSITION_EVIDENCE_KIND
from jasper.active_speaker.crossover_v2.capture_dispatch import level_drift_verdict
from jasper.active_speaker.crossover_v2.record_store import BankedRecordStore
from jasper.active_speaker.crossover_v2.refusal_copy import TakeVerdict
from jasper.active_speaker.run_manifest import RunManifest
from jasper.audio_measurement.bundles import record_artifact


async def bank_trial_async(candidate, profile, topology, *, record_fields=None, wav_bytes=b"captured trial bytes"):
    root = bundles.sessions_dir()
    try:
        find_banked_candidate(candidate.fingerprint)
    except CandidateBankRefusal:
        candidate_path = root / f"authored-{candidate.fingerprint}" / "evidence/v1/artifacts/crossover_v2/authored/candidate.json"
        candidate_path.parent.mkdir(parents=True, exist_ok=True)
        candidate_path.write_text(json.dumps(candidate.to_dict()))
    info = open_bundle(topology, calibration_id="", sessions_dir=root)
    bundle = Path(info["bundle_dir"])
    wav = bundle / "capture.wav"
    wav.write_bytes(wav_bytes)
    identity = record_artifact(bundle, wav, kind="jts_capture_wav", sensitivity="audio",
                               recomputable=False, generated_by="test")
    store = BankedRecordStore(CommissioningEvidenceStore.open(bundle, expected_session_id=info["session_id"]), "trial")
    graph = str((profile.get("config") or {}).get("sha256") or "")[:16]
    record = {"kind": POSITION_EVIDENCE_KIND, "measure_kind": "candidate", "session_id": "trial",
              "candidate_id": candidate.fingerprint, "role": "summed",
              "graph_scope": "candidate", "graph_fingerprint": graph, "measurement_status": "captured",
              "incident": "", "level_db": -25., "wav_path": wav.name, "wav_sha256": identity["sha256"],
              "capture_integrity": {"checks": []}}
    manifest = RunManifest("trial", store)
    stop = {"index": 1, "repeat": 1, "pose": {"kind": "bearing", "distance_m": 1., "position_deg": 0}}
    fields = record_fields if isinstance(record_fields, list) else [record_fields or {}]
    manifest.planned = [{**stop, "index": index} for index in range(1, len(fields) + 1)]
    for index, values in enumerate(fields, 1):
        manifest.begin({**stop, "index": index}, attempt=1, pose_index=index)
        take = {**record, **values, "take_id": f"trial_{index:02}"}
        record_id = await store.bank(take)
        await manifest.append(take, record_id, TakeVerdict(True), complete=True, started_s=0., ended_s=1.,
                              level_observation=level_drift_verdict(**manifest.level_observation(take)).evidence)
    manifest.finalized = True
    await manifest.persist()
    return bundle, manifest


def bank_trial(candidate, profile, topology, *, record_fields=None, wav_bytes=b"captured trial bytes"):
    return asyncio.run(bank_trial_async(candidate, profile, topology, record_fields=record_fields, wav_bytes=wav_bytes))
