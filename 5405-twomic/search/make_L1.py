#!/usr/bin/env python3
"""Build L1's three rear-LEVEL candidates.

The null works at 85-125 Hz only. At 223-281 Hz there is no null even gated,
because the rear sits 7-13 dB under the front at the rear microphone, and N1's
300 Hz low-pass takes another 2-3 dB there. So these three raise the rear
LEVEL around 250 Hz rather than move its phase:

  T1  e-0.35 + Peaking 250 Hz +5 dB q 1.0, low-pass 300 -> 400 Hz,
      delay -0.35 -> -0.16 ms (the corner move removes 0.19 ms of filter delay)
  T2  T1 with the Peaking at +3 dB
  T3  ident-C + the same Peaking 250 Hz +5 dB q 1.0, corner and delay untouched

Every edit is on the CANCELLATION branch. The branch ``gain_db`` is not
touched (it must stay <= 0); the boost is a filter parameter.
"""
from __future__ import annotations

import json
from pathlib import Path

SP = Path(__file__).resolve().parent.parent
PEAK_250 = {"type": "Biquad", "parameters": {"type": "Peaking", "freq": 250.0, "q": 1.0}}


def peaking(gain_db: float) -> dict:
    filter_ = json.loads(json.dumps(PEAK_250))
    filter_["parameters"]["gain"] = float(gain_db)
    return filter_


def build(source: Path, *, gain_db: float, corner: float | None,
          delay_ms: float | None, tag: str, rationale: str) -> dict:
    document = json.loads(source.read_text())
    cancellation = document["sections"]["rear_calibration"]["rear"]["cancellation"]
    cancellation["filters"].append(peaking(gain_db))
    if corner is not None:
        lowpass = next(f for f in cancellation["filters"]
                       if f["parameters"].get("type") == "ButterworthLowpass")
        lowpass["parameters"]["freq"] = corner
    if delay_ms is not None:
        cancellation["delay_ms"] = delay_ms
    if cancellation["gain_db"] > 0.0:
        raise SystemExit(f"{tag}: branch gain_db is positive")
    document["rationale"] = rationale
    return document


PLAN = (
    ("T1", "search/D2/doc-e-035.json", 5.0, 400.0, -0.16,
     "T1 (#5405 rear-level round L1): e-0.35 with the cancellation branch lifted around 250 Hz "
     "-- Peaking 250 Hz +5 dB q 1.0 -- and its low-pass opened 300 -> 400 Hz, delay -0.35 -> "
     "-0.16 ms to give back the 0.19 ms of group delay the wider corner removes. The null holds "
     "only at 85-125 Hz; at 223-281 Hz there is no null even gated because the rear is 7-13 dB "
     "under the front at the rear mic, so this raises rear LEVEL there instead of moving phase."),
    ("T2", "search/D2/doc-e-035.json", 3.0, 400.0, -0.16,
     "T2 (#5405 rear-level round L1): T1 with the 250 Hz lift at +3 dB instead of +5 dB, to see "
     "how much of the 250 Hz shortfall is worth closing before the boost costs elsewhere."),
    ("T3", "cands3/ident-C.json", 5.0, None, None,
     "T3 (#5405 rear-level round L1): the incumbent ident-C with the same Peaking 250 Hz +5 dB "
     "q 1.0 added to its cancellation branch, low-pass corner and delay untouched -- the rear "
     "level fix applied on top of the best measured tune rather than on e-0.35."),
)


def main() -> int:
    out = SP / "search" / "L1"
    out.mkdir(parents=True, exist_ok=True)
    base = json.loads((SP / "docs" / "doc-N1.json").read_text())["sections"]["rear_calibration"]
    for tag, source, gain_db, corner, delay_ms, rationale in PLAN:
        document = build(SP / source, gain_db=gain_db, corner=corner, delay_ms=delay_ms,
                         tag=tag, rationale=rationale)
        section = document["sections"]["rear_calibration"]
        for key in ("common_delay_ms", "front", "rear_muted", "sample_rate_hz",
                    "valid_band_hz", "phase_convention"):
            if section.get(key) != base.get(key):
                raise SystemExit(f"{tag}: {key} differs from N1")
        if section["rear"]["bass"] != base["rear"]["bass"]:
            raise SystemExit(f"{tag}: bass branch differs from N1")
        if "devices" in document or "devices" in document.get("sections", {}):
            raise SystemExit(f"{tag}: document writes devices")
        path = out / f"doc-{tag}.json"
        path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
        cancellation = section["rear"]["cancellation"]
        lowpass = next(f["parameters"]["freq"] for f in cancellation["filters"]
                       if f["parameters"].get("type") == "ButterworthLowpass")
        print(f"{path.name}: from {Path(source).name}  delay {cancellation['delay_ms']:+.3f} "
              f"gain {cancellation['gain_db']:+.4f}  lowpass {lowpass:.0f}  "
              f"peak250 +{gain_db:.0f}  filters {len(cancellation['filters'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
