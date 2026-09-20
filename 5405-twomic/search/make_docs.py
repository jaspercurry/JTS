#!/usr/bin/env python3
"""Write one prescription document per search point, from doc-N1.json.

Only the cancellation branch is touched: its ``delay_ms``, its ``gain_db``
(never positive), and the corner of its ``ButterworthLowpass``. Everything
else -- the front branch, the bass branch, ``common_delay_ms`` 1.06, the
``devices`` section the composer owns -- is copied through untouched.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "docs" / "doc-N1.json"


def one(spec: dict) -> dict:
    document = json.loads(BASE.read_text())
    section = document["sections"]["rear_calibration"]
    cancellation = section["rear"]["cancellation"]
    lowpass = next(f for f in cancellation["filters"]
                   if f["parameters"].get("type") == "ButterworthLowpass")
    cancellation["delay_ms"] = float(spec["delay_ms"])
    if "gain_db" in spec:
        gain = float(spec["gain_db"])
        if gain > 0.0:
            raise SystemExit(f"{spec['tag']}: cancellation gain_db {gain} is positive")
        cancellation["gain_db"] = gain
    if "lp_freq" in spec:
        lowpass["parameters"]["freq"] = float(spec["lp_freq"])
    document["rationale"] = spec["rationale"]
    section["assumptions"] = [
        section["assumptions"][0],
        f"#5405 measured null search, point {spec['tag']}: cancellation delay "
        f"{cancellation['delay_ms']:+.3f} ms, gain {cancellation['gain_db']:+.2f} dB, "
        f"low-pass corner {lowpass['parameters']['freq']:.0f} Hz. "
        "A search point measured behind the cabinet, not a tune.",
    ]
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for spec in json.loads(args.spec.read_text()):
        path = args.out_dir / f"doc-{spec['tag']}.json"
        path.write_text(json.dumps(one(spec), indent=1, sort_keys=True) + "\n")
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
