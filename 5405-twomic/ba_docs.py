"""Write the BA documents. Every one clears the room layer (or replaces it)."""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

from ba_lib import SP

AGG = json.load(open(SP / "cands7" / "agg-1.json"))["sections"]["rear_calibration"]
OUT = SP / "search" / "BA"


def doc(rear, rationale, room=None):
    """A prescription on the saved base. ``room`` null CLEARS the saved room layer."""
    return {"base": "saved", "kind": "jts_prescription", "schema": 1,
            "rationale": rationale,
            "sections": {"rear_calibration": rear, "room": room}}


def c0_section():
    section = copy.deepcopy(AGG)
    section["rear_muted"] = True
    section["front"] = {"delay_ms": 0.0, "filters": [], "gain_db": 0.0,
                        "inverted": False, "muted": False}
    section["assumptions"] = [
        "C0: agg-1's stage with the rear woofer muted and an EMPTY front chain -- "
        "driver linearization only, no cardioid shaping, no room layer.",
        "The rear branches are carried verbatim but sum to zero while rear_muted is "
        "true, so the stage charges no headroom (rear_branch_sum_headroom_db = 0).",
    ]
    return section


def a0_section():
    section = copy.deepcopy(AGG)
    section["assumptions"] = [*AGG["assumptions"][:1],
                              "A0: the applied agg-1 rear stage verbatim, room layer cleared."]
    return section


def b0_section():
    section = json.load(open(OUT / "b0-section.json"))
    fit = json.load(open(OUT / "b0-fit.json"))
    section["assumptions"] = [
        "B0: a rear-muted copy of agg-1 whose FRONT chain carries four measured "
        "Peaking filters plus agg-1's own headroom cut in gain_db, so the front "
        "response matches A0 while the rear woofer is silent.",
        "Target = measured front (agg-1 minus Nm) at the main mic, pose 0, mean of "
        "rounds e5f73ee228bc and 433113c88326, 1/3-octave smoothed, untrimmed.",
        f"Fit residual over 40 Hz-5 kHz: worst {fit['worst_db']:.2f} dB, "
        f"rms {fit['rms_db']:.2f} dB; at third-octave centres 50 Hz-5 kHz worst 0.69 dB.",
    ]
    return section


def common_eq(section, filters, tag):
    """Append the room filters to EVERY woofer chain of the stage.

    The product's own room layer sits pre-split (camilla_yaml.py:1999-2006,
    before the split_active mixer the rear stage is spliced after,
    camilla_yaml.py:652-659), so it hits both woofers alike. The prescriber
    will not take a room section without a measured room median from a `room`
    round (round_inputs.py:318-324, room_prescription.py:207+), so the same
    transfer goes on front + rear.bass + rear.cancellation instead: a COMMON
    EQ scales the branch sum without touching the front-to-back ratio, which
    is what ADR-0327 and agg-1's own 190 Hz filter already do. The tweeter is
    not in the chain, and it carries nothing below 500 Hz.
    """
    import copy as _copy
    out = _copy.deepcopy(section)
    out["front"]["filters"] = out["front"]["filters"] + _copy.deepcopy(filters)
    if not out["rear_muted"]:
        for branch in ("bass", "cancellation"):
            out["rear"][branch]["filters"] = (out["rear"][branch]["filters"]
                                              + _copy.deepcopy(filters))
    out["assumptions"] = [
        f"{tag}: the room correction fitted on its own base in round ba1, carried as a "
        "COMMON EQ on every woofer chain of the stage (front"
        + ("" if out["rear_muted"] else " + rear.bass + rear.cancellation") + ").",
        "Equivalent to the product's pre-split room layer inside 40-500 Hz; the room "
        "SECTION itself needs a measured room median this rear round does not bank.",
        *section.get("assumptions", [])[:1],
    ]
    return out


def write(tag, section, rationale, room=None):
    path = OUT / f"doc-{tag}.json"
    path.write_text(json.dumps(doc(section, rationale, room), indent=1) + "\n")
    print(path)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    if "ba1" in sys.argv:
        write("C0", c0_section(),
              "C0 (#5405 before/after): driver linearization only -- agg-1's rear stage with "
              "the rear woofer muted and no front filters, and the saved room layer cleared. "
              "The naive 'cardioid off'. Measurement only.")
        write("B0", b0_section(),
              "B0 (#5405 before/after): the FAIR 'cardioid off' -- rear woofer muted, four "
              "measured Peaking filters and agg-1's headroom cut on the front chain so the "
              "front response matches A0, room layer cleared. Measurement only.")
        write("A0", a0_section(),
              "A0 (#5405 before/after): the applied agg-1 cardioid stage with the saved room "
              "layer cleared, so the pair A0/B0 differs only in whether the rear woofer plays. "
              "Measurement only.")
    if "ba2" in sys.argv:
        room = json.load(open(OUT / "room-fits.json"))
        write("A1", common_eq(a0_section(), room["A0"]["filters"], "A1"),
              "A1 (#5405 before/after): A0 plus the room correction fitted on A0's own "
              f"mean-of-3-poses front response in round ba1 ({room['A0']['n_filters']} Peaking "
              f"filters, 40-500 Hz, largest boost {room['A0']['largest_boost_db']:+.2f} dB). "
              "Measurement only.")
        write("B1", common_eq(b0_section(), room["B0"]["filters"], "B1"),
              "B1 (#5405 before/after): B0 plus the room correction fitted on B0's own "
              f"mean-of-3-poses front response in round ba1 ({room['B0']['n_filters']} Peaking "
              f"filters, 40-500 Hz, largest boost {room['B0']['largest_boost_db']:+.2f} dB), "
              "same recipe and same limits as A1's. Measurement only.")


if __name__ == "__main__":
    main()
