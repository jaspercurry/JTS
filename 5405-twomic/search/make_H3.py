#!/usr/bin/env python3
"""H3: sweep the cancellation delay on the lp400 base.

H1 showed the 500 Hz FRONT dip tracks the cancellation branch delay at about
3.4 dB/ms and not the low-pass corner, so the corner stays at 400 Hz here and
only the delay moves. The last variant also backs the 120 Hz lift off to
+4.5 dB: with the -10 dB cap a 100 Hz band at -9 costs almost nothing, while
H1's w3 bought 5-6 dB at gated 160 Hz, and broad suppression over 100-350 Hz
is what the owner wants.
"""
from __future__ import annotations

import json
from pathlib import Path

SP = Path(__file__).resolve().parent.parent
BASE = SP / "search" / "H1" / "doc-S1-lp400.json"

PLAN = (
    ("d-012", -0.12, None,
     "H3 d-0.12 (#5405): the lp400 tune with its cancellation delay moved +0.0132 -> -0.12 ms. "
     "H1 found the 500 Hz front dip tracks this delay at about 3.4 dB/ms while the low-pass "
     "corner barely matters, so this is the first step of the sweep that prices the front fix."),
    ("d-025", -0.25, None,
     "H3 d-0.25 (#5405): the lp400 tune with its cancellation delay moved +0.0132 -> -0.25 ms, "
     "near where H1's 3.4 dB/ms relation predicts the 500 Hz front dip closes to flat."),
    ("d-025-w45", -0.25, 4.5,
     "H3 d-0.25-w4.5 (#5405): as d-0.25 with the 120 Hz Peaking reduced +5.907 -> +4.5 dB, a half "
     "step toward H1's w3. w3 bought 5-6 dB at gated 160 Hz but cost 6-7 dB at 100 Hz; under the "
     "-10 dB cap a 100 Hz band at -9 costs almost nothing, so this trades depth at 100 Hz for "
     "broader suppression across 100-350 Hz."),
)


def main() -> int:
    out = SP / "search" / "H3"
    out.mkdir(parents=True, exist_ok=True)
    base = json.loads(BASE.read_text())
    reference = json.loads(BASE.read_text())["sections"]["rear_calibration"]
    for tag, delay_ms, peak120, rationale in PLAN:
        document = json.loads(json.dumps(base))
        section = document["sections"]["rear_calibration"]
        cancellation = section["rear"]["cancellation"]
        cancellation["delay_ms"] = delay_ms
        if peak120 is not None:
            peak = next(f for f in cancellation["filters"]
                        if f["parameters"].get("type") == "Peaking"
                        and abs(f["parameters"]["freq"] - 120.0) < 1e-6)
            peak["parameters"]["gain"] = peak120
        document["rationale"] = rationale

        for key, value in reference.items():
            if key in ("rear", "assumptions"):
                continue
            if section[key] != value:
                raise SystemExit(f"{tag}: {key} changed")
        if section["rear"]["bass"] != reference["rear"]["bass"]:
            raise SystemExit(f"{tag}: bass branch changed")
        if cancellation["gain_db"] > 0.0:
            raise SystemExit(f"{tag}: branch gain_db is positive")
        if not -1.06 <= cancellation["delay_ms"] <= 3.0:
            raise SystemExit(f"{tag}: delay {cancellation['delay_ms']} out of range")
        if "devices" in document or "devices" in document["sections"]:
            raise SystemExit(f"{tag}: writes devices")

        path = out / f"doc-{tag}.json"
        path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
        corner = next(f["parameters"]["freq"] for f in cancellation["filters"]
                      if f["parameters"].get("type") == "ButterworthLowpass")
        peak = next(f["parameters"]["gain"] for f in cancellation["filters"]
                    if f["parameters"].get("type") == "Peaking"
                    and abs(f["parameters"]["freq"] - 120.0) < 1e-6)
        print(f"{path.name}: corner {corner:.0f}  delay {cancellation['delay_ms']:+.4f}  "
              f"120Hz {peak:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
