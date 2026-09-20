#!/usr/bin/env python3
"""Three single-knob variants of the incumbent gated-S1.

S1 leaves a repeatable 0.8-1.4 dB dip in the FRONT response at 500 Hz, because
its 472 Hz low-pass lets the inverted rear branch play high enough to comb the
front there. Two variants close the corner (compensating the delay so the low
frequency timing is unchanged) and one backs off the 120 Hz lift, which is
aimed at the weak gated 160 Hz band.

Only the cancellation branch is touched, one knob per document.
"""
from __future__ import annotations

import json
from pathlib import Path

SP = Path(__file__).resolve().parent.parent
BASE = SP / "cands5" / "gated-S1.json"

PLAN = (
    ("S1-lp400", {"corner": 400.0, "delay_ms": 0.0132},
     "S1-lp400 (#5405 round H1): the incumbent gated-S1 with its cancellation low-pass closed "
     "472.25 -> 400 Hz and delay_ms +0.0993 -> +0.0132, because a 2nd-order Butterworth at 400 Hz "
     "carries 0.086 ms more group delay than at 472 Hz and the low-frequency timing must not move. "
     "Aimed at S1's repeatable 0.8-1.4 dB front dip at 500 Hz, which the wider corner causes."),
    ("S1-lp350", {"corner": 350.0, "delay_ms": -0.0672},
     "S1-lp350 (#5405 round H1): as S1-lp400 but the cancellation low-pass closed to 350 Hz, "
     "delay_ms +0.0993 -> -0.0672 for the 0.166 ms of extra group delay. The firmer of the two "
     "attempts to take the inverted rear branch out of the front's 500 Hz region."),
    ("S1-w3", {"peak120": 3.0},
     "S1-w3 (#5405 round H1): the incumbent gated-S1 with its 120 Hz Peaking reduced from "
     "+5.907 to +3.0 dB, leaving the low-pass corner and delay untouched. Aimed at the weak gated "
     "160 Hz band, where S1 reads +1 to +4 dB instead of nulling."),
)


def main() -> int:
    out = SP / "search" / "H1"
    out.mkdir(parents=True, exist_ok=True)
    base = json.loads(BASE.read_text())
    reference = json.loads(BASE.read_text())["sections"]["rear_calibration"]
    for tag, change, rationale in PLAN:
        document = json.loads(json.dumps(base))
        section = document["sections"]["rear_calibration"]
        cancellation = section["rear"]["cancellation"]
        if "corner" in change:
            lowpass = next(f for f in cancellation["filters"]
                           if f["parameters"].get("type") == "ButterworthLowpass")
            lowpass["parameters"]["freq"] = change["corner"]
            cancellation["delay_ms"] = change["delay_ms"]
        if "peak120" in change:
            peak = next(f for f in cancellation["filters"]
                        if f["parameters"].get("type") == "Peaking"
                        and abs(f["parameters"]["freq"] - 120.0) < 1e-6)
            peak["parameters"]["gain"] = change["peak120"]
        document["rationale"] = rationale

        # Everything outside the cancellation branch must be untouched.
        for key, value in reference.items():
            if key in ("rear", "assumptions"):
                continue
            if section[key] != value:
                raise SystemExit(f"{tag}: {key} changed")
        if section["rear"]["bass"] != reference["rear"]["bass"]:
            raise SystemExit(f"{tag}: bass branch changed")
        if cancellation["gain_db"] > 0.0:
            raise SystemExit(f"{tag}: branch gain_db is positive")
        if "devices" in document or "devices" in document["sections"]:
            raise SystemExit(f"{tag}: writes devices")

        path = out / f"doc-{tag}.json"
        path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
        corner = next(f["parameters"]["freq"] for f in cancellation["filters"]
                      if f["parameters"].get("type") == "ButterworthLowpass")
        peak = next(f["parameters"]["gain"] for f in cancellation["filters"]
                    if f["parameters"].get("type") == "Peaking"
                    and abs(f["parameters"]["freq"] - 120.0) < 1e-6)
        print(f"{path.name}: corner {corner:7.2f}  delay {cancellation['delay_ms']:+.4f}  "
              f"120Hz peak {peak:+.3f}  filters {len(cancellation['filters'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
