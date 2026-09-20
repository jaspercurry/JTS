#!/usr/bin/env python3
"""Write the cardioid target table ``scripts/fit-rear-branches.py`` reads.

Its columns are ``frequency_hz``, ``rear_front_ratio_mag_db``,
``rear_front_ratio_phase_dsp_deg``, ``rear_motion_zero`` -- the ACOUSTIC
rear/front ratio wanted AT THE MICROPHONE, not a response. The fitter turns it
into the electrical ratio with ``T * H_front / H_rear``, and what reaches the
microphone is ``H_front * (1 + T)``: so a perfect null is ``T = -1``, which is
0 dB at 180 degrees, and ``T = 0`` leaves that microphone hearing the front
alone.

The table here asks for the deepest null over 100-350 Hz and for the rear to
stay out of the way elsewhere, so the forward response is not wrecked. The
skirts are written as a low magnitude at 180 degrees rather than as
``rear_motion_zero``: a silent row's complex value is ``0j``, whose ANGLE is
zero, and the fitter interpolates unwrapped phase -- a zero row therefore drags
a phase ramp through its neighbours.
"""
from __future__ import annotations

import argparse
from pathlib import Path

#: ``(frequency Hz, |T| dB)`` at 180 degrees. -40 dB is 1% of the front, below
#: the fitter's own 25 dB suppression floor over 500-800 Hz.
ROWS = (
    (40.0, -30.0), (50.0, -30.0), (63.0, -30.0), (71.0, -26.0), (80.0, -18.0), (90.0, -8.0),
    (100.0, 0.0), (112.0, 0.0), (125.0, 0.0), (140.0, 0.0), (160.0, 0.0), (180.0, 0.0),
    (200.0, 0.0), (224.0, 0.0), (250.0, 0.0), (280.0, 0.0), (315.0, 0.0), (350.0, 0.0),
    (400.0, -12.0), (450.0, -25.0), (500.0, -40.0), (630.0, -40.0), (800.0, -40.0),
)
PHASE_DEG = 180.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    lines = ["frequency_hz,rear_front_ratio_mag_db,rear_front_ratio_phase_dsp_deg,rear_motion_zero"]
    lines += [f"{hz:g},{db:g},{PHASE_DEG:g},false" for hz, db in ROWS]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n")
    print(f"wrote {args.out} ({len(ROWS)} rows, {ROWS[0][0]:g}-{ROWS[-1][0]:g} Hz)")


if __name__ == "__main__":
    main()
