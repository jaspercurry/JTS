#!/usr/bin/env python3
"""Score rear documents against a ``twomic_pair.py`` out-dir.

Prints, per document and per microphone position, the predicted 100-350 Hz
change of rear-on against the SAME document rear-muted -- ungated and
early-energy -- plus the 350 Hz-5 kHz forward guard, the product's headroom
charge, and whether the product's own reader accepts the document.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from jasper.active_speaker.camilla_yaml import rear_branch_sum_headroom_db
from jasper.active_speaker.rear_calibration import RearCalibrationError

from rearpred import FRONT_GUARD_BAND_HZ, NULL_BAND_HZ, load_positions, score_position, section_of


def validated(path: Path, *, unmute: bool) -> tuple[dict | None, str]:
    """``(section, verdict)`` from the product's own reader.

    ``unmute`` answers a seed: ``fit-rear-branches.py`` writes
    ``rear_muted: true`` ("SEED, NOT A TUNE"), and a muted section predicts
    itself, so scoring one as written gives 0.00 dB everywhere. The fitter's
    own report reads it the same way (``unmuted = {**document,
    "rear_muted": False}``). A document that is muted ON PURPOSE -- doc-Nm, the
    measured zero -- must be scored WITHOUT this flag.
    """
    try:
        section = section_of(json.loads(path.read_text()))
    except (RearCalibrationError, KeyError, ValueError, TypeError) as exc:
        return None, f"FAIL {type(exc).__name__}: {exc}"
    return ({**section, "rear_muted": False} if unmute else section), "PASS"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--third-octaves", action="store_true")
    parser.add_argument("--unmute", action="store_true",
                        help="score a rear_muted seed as if its rear were on")
    parser.add_argument("docs", type=Path, nargs="+")
    args = parser.parse_args()

    positions = load_positions(args.out_dir)
    keys = sorted(positions, key=lambda key: (positions[key]["mic"] != "side", key))
    # The pose key is the ARM bearing; the side mic sits at the mirror of it.
    def label(key: str) -> str:
        row = positions[key]
        degrees = float(row["pose"].split("az")[1].split("_")[0])
        return ("rear" if row["mic"] == "side" else "front") + f"{-degrees if row['mic'] == 'side' else degrees:+03.0f}"

    print(f"predicted change, dB: rear on against the SAME document rear-muted")
    print(f"  null band {NULL_BAND_HZ[0]:g}-{NULL_BAND_HZ[1]:g} Hz (energy mean on a log grid); "
          f"early = the product's 0-10 ms EARLY_WINDOW_MS on the band-limited impulse")
    print(f"  guard = {FRONT_GUARD_BAND_HZ[0]:g} Hz-{FRONT_GUARD_BAND_HZ[1]/1000:g} kHz, "
          f"front poses only (must stay within 0.4 dB)")
    print("  document      reader  charge" + "".join(f"{label(key):>9s}" for key in keys))
    for path in args.docs:
        section, verdict = validated(path, unmute=args.unmute)
        name = path.stem if path.stem != "doc" else path.parent.name
        if section is None:
            print(f"  {name:13s} {verdict}")
            continue
        charge = rear_branch_sum_headroom_db(section)
        rows = {key: score_position(section, positions[key], early=True) for key in keys}
        for which, field in (("ungated", "null_band_db"), ("early", "early_db")):
            first = which == "ungated"
            print(f"  {name if first else '':13s} {verdict if first else '':6s} "
                  f"{charge if first else 0.0:6.2f} "
                  + "".join(f"{rows[key][field]:+9.2f}" for key in keys) + f"  <- {which}")
        guard = [f"{label(key)} {rows[key]['guard_band_db']:+.2f}" for key in keys
                 if positions[key]["mic"] == "main"]
        print(f"  {'':27s} front guard: {', '.join(guard)} dB")
        if args.third_octaves:
            rear0 = next(key for key in keys if positions[key]["mic"] == "side"
                         and positions[key]["pose"].startswith("az+0"))
            print("    rear 0 deg per third octave: " + " ".join(
                f"{row['band_hz'][0]:.0f}:{row['change_db']:+.1f}"
                for row in rows[rear0]["third_octaves"]))
    print("  rearNN = side mic (Dayton, 0.61 m BEHIND); frontNN = main mic (UMIK-2, 0.61 m in "
          "FRONT). The pose key is the ARM bearing, so the rear angle is its mirror.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
