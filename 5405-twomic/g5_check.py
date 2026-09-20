#!/usr/bin/env python3
"""Independent re-check of the three emitted documents.

Both product readers, and the shared-zero requirement: every document of a
round must carry Nm's front chain, rear.bass and common_delay_ms, or the
measured take of Nm stops being the zero the model subtracts.
"""
from __future__ import annotations

import json

import g5lib as g

from jasper.active_speaker.crossover_v2.prescription_document import read_prescription_document
from jasper.active_speaker.rear_calibration import read_rear_calibration

nm = g.load_json(g.SP / "docs/doc-Nm.json")["sections"]["rear_calibration"]
for name in ("S1", "S2", "S3"):
    path = g.SP / f"cands5/gated-{name}.json"
    doc = json.loads(path.read_text())
    notes = []
    try:
        section = read_rear_calibration(doc["sections"]["rear_calibration"], sample_rate=48000)
        read_prescription_document(doc)
        verdict = "PASS"
    except Exception as exc:                       # noqa: BLE001
        verdict = f"FAIL {type(exc).__name__}: {exc}"
        section = doc["sections"]["rear_calibration"]
    for key in ("front", "common_delay_ms", "sample_rate_hz", "valid_band_hz",
                "phase_convention", "rear_muted"):
        if json.dumps(section.get(key), sort_keys=True) != json.dumps(nm.get(key),
                                                                     sort_keys=True):
            notes.append(f"{key} differs from Nm")
    if json.dumps(section["rear"]["bass"], sort_keys=True) != json.dumps(nm["rear"]["bass"],
                                                                        sort_keys=True):
        notes.append("rear.bass differs from Nm")
    if "devices" in doc.get("sections", {}) or "devices" in doc:
        notes.append("HAS a devices section")
    chain = section["rear"]["cancellation"]
    low = [f["parameters"]["freq"] for f in chain["filters"]
           if f["parameters"].get("type") not in ("LinkwitzRileyHighpass",)
           and f["parameters"]["freq"] < 100.0]
    if low:
        notes.append(f"filter centred below 100 Hz: {low}")
    print(f"  gated-{name}.json  {verdict}  keys {sorted(doc)}  "
          f"gain_db {chain['gain_db']} inverted {chain['inverted']} "
          f"delay {chain['delay_ms']:+.4f}  "
          + ("; ".join(notes) if notes else "shares Nm's front/bass/common delay"))
