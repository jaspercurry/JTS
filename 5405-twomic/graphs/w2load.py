#!/usr/bin/env python3
"""Decode the two wall rounds once: every take of both mics as a complex transfer.

``main`` is the UMIK-2 on the front arm at 0.81 m, pose 0 only. ``side`` is the
Dayton FIXED AT THE LISTENING SEAT, ~2 m away and ~20 deg off axis -- in FRONT
of the speaker, not behind it as in the mid-room rounds.
"""
from __future__ import annotations
import pickle, sys
from pathlib import Path

SP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SP))
import identlib as il  # noqa: E402

ROUNDS = {"wall1c": "round-0d0abbb03574", "wall2": "round-dfe333aea6d3"}
CACHE = SP / "graphs" / "w2-takes.pkl"
CALS = {"main_cal": SP / "calibration_mics/minidsp/minidsp_umik2/minidsp-minidsp_umik2-b7343c0c625b.txt",
        "side_cal": SP / "dayton-CMM31555.txt"}


def rows():
    held = pickle.loads(CACHE.read_bytes()) if CACHE.exists() else {}
    for tag, name in ROUNDS.items():
        if tag in held:
            continue
        held[tag] = il.take_transfers(SP / name, **CALS)
        CACHE.write_bytes(pickle.dumps(held))
    return held


if __name__ == "__main__":
    held = rows()
    for tag, take_rows in held.items():
        print(f"\n=== {tag} ({ROUNDS[tag]}): {len(take_rows)} take rows")
        seen: dict = {}
        for row in take_rows:
            seen.setdefault((row["mic"], row["pose"], row["candidate"]), []).append(row)
        for key in sorted(seen):
            marks = " ".join(f"{r['take_id'][-8:]}/a{r['attempt']}/"
                             f"{'ok' if r['ok'] else 'REFUSED'}" for r in seen[key])
            print(f"  {key[0]:5s} {key[2]} n={len(seen[key])}  {marks}")
