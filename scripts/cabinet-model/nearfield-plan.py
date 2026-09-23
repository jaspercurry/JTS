#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0
"""Stage a v5 plan for woofer near-field takes (a stopgap until #5684 ships a near-field program).

Runs ON THE SPEAKER with its venv. It plays the rear/pair program (each woofer solo, then both,
tweeter silent) through the applied tune with its rear stage and bass boost cleared, so the takes
see the raw woofers, at "close" (front woofer) and "behind" (rear woofer) poses. Those kinds keep
the round out of the EQ page's rear-pair level match. Take each spacing as its own run, so every
take records its true distance. Placement is confirmed from the CLI:

    scp scripts/cabinet-model/nearfield-plan.py pi@<speaker>:/tmp/
    ssh pi@<speaker> 'sudo /opt/jasper/.venv/bin/python /tmp/nearfield-plan.py --level-db -32 --woofer front > /tmp/nf.json'
    ssh pi@<speaker> 'sudo /opt/jasper/.venv/bin/jasper-round run --plan /tmp/nf.json'   # prints the run id
    ssh pi@<speaker> 'sudo /opt/jasper/.venv/bin/jasper-round placed --run <id>'          # once per take
    ssh pi@<speaker> 'sudo /opt/jasper/.venv/bin/jasper-round stop --run <id>'

The pair check refuses near-field takes (the far woofer is ~30 dB down at the mic) and asks
again, so one `placed` can give two recordings. Each stays in the session as a numbered attempt;
nearfield-analyze.py reads them. Level: the mic peaks ~33 dB above the 1 m level, so pick the
fader from one measured take, never from the seat anchor (on jts3, with the bass boost on, -32 dB
gave an 82.5 dB peak at 15 mm, under the unchanged 85 dB stop).
"""
from __future__ import annotations

import argparse
import json
from dataclasses import replace

from jasper.active_speaker.angle_capture import LevelPolicy, request_for_program
from jasper.active_speaker.candidate_bank import publish_authored_candidate
from jasper.active_speaker.crossover_v2.prescription_document import (
    DOCUMENT_KIND, judge_prescription_document, saved_base,
)
from jasper.active_speaker.measurement_programs import POSE_KIND_BEHIND, POSE_KIND_CLOSE, ProgramPose, run_program
from jasper.active_speaker.movers import MOVER_CONFIRMED

DETAIL = "Capsule on the dust-cap axis, pointed straight in, tip level with a ruler laid across the rubber surround."


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--level-db", type=float, required=True, help="run fader in dB")
    ap.add_argument("--gap-m", type=float, default=0.0146, help="stated mic distance from the dust-cap apex")
    ap.add_argument("--woofer", choices=("front", "rear", "both"), default="front")
    args = ap.parse_args()
    poses = {
        "front": ProgramPose(0, 0, kind=POSE_KIND_CLOSE, distance_m=args.gap_m,
                             headline="Microphone at the FRONT woofer", detail=DETAIL),
        "rear": ProgramPose(0, 0, kind=POSE_KIND_BEHIND, distance_m=args.gap_m,
                            headline="Microphone at the REAR woofer", detail=DETAIL),
    }
    chosen = tuple(poses[w] for w in (("front", "rear") if args.woofer == "both" else (args.woofer,)))
    program = replace(run_program("rear", "rear/pair_behind"), size="custom", layout="", poses=chosen,
                      mover=MOVER_CONFIRMED)
    base, base_profile = saved_base()
    raw = judge_prescription_document(
        {"kind": DOCUMENT_KIND, "schema": 1, "base": "saved", "sections": {"rear_calibration": {}, "bass": {}},
         "rationale": "Near-field takes of the raw woofers: no rear stage, no bass boost."},
        base=base, base_profile=base_profile)
    request = request_for_program(program, candidates=(publish_authored_candidate(raw).fingerprint,),
                                  level=LevelPolicy(level_db=args.level_db), level_source="operator",
                                  mover=MOVER_CONFIRMED)
    print(json.dumps(request.to_dict(), indent=1, sort_keys=True))


if __name__ == "__main__":
    main()
