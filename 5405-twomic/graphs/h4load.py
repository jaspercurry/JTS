#!/usr/bin/env python3
"""Decode round H4 once into a pickle: every take of both mics as a complex transfer."""
from __future__ import annotations
import pickle, sys
from pathlib import Path

SP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SP))
import identlib as il  # noqa: E402

ROUND = SP / "round-8ae2ac84b867"
CACHE = SP / "graphs" / "h4-takes.pkl"
CALS = {"main_cal": SP / "calibration_mics/minidsp/minidsp_umik2/minidsp-minidsp_umik2-b7343c0c625b.txt",
        "side_cal": SP / "dayton-CMM31555.txt"}


def rows():
    if CACHE.exists():
        with CACHE.open("rb") as handle:
            return pickle.load(handle)
    held = il.take_transfers(ROUND, **CALS)
    with CACHE.open("wb") as handle:
        pickle.dump(held, handle)
    return held


if __name__ == "__main__":
    held = rows()
    print(f"{len(held)} take rows")
    seen = {}
    for row in held:
        key = (row["mic"], row["pose"], row["candidate"])
        seen.setdefault(key, []).append((row["attempt"], row["ok"], row.get("own_ok"),
                                         round(row["kept"], 4), row["take_id"][-8:]))
    for key in sorted(seen):
        if len(seen[key]) > 1 or not seen[key][0][1]:
            print("  MULTI/REFUSED", key, seen[key])
    print(f"{len(seen)} unique (mic, pose, tune) cells; "
          f"{sum(1 for v in seen.values() if len(v) > 1)} with more than one attempt")
