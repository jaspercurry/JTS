# ADR-0222: The relay is deleted; the wired microphone on jts.local is the only capture path

- **Date:** 2026-09-02
- **Status:** Accepted. Supersedes ADR-0188 sections 2 and 3.

## Context

ADR-0188 made the wired USB measurement microphone the canonical acoustic
measurement path and parked the phone-mic relay (`capture-page/`, `relay/`,
`jasper/capture_relay/`, the relay wizard provider) as a "marooned island"
kept for a future moving-mic room-correction capability, with two live
callers exempted (the stereo-sync wizard and v1 room correction).

The island was not small. The relay had been built to share as much as
possible with the wired flow, so its vocabulary, its process-wide capture
slot, its consent copy, its cue rows, its installer seeding and its secrets
key ran through the live measurement code rather than beside it. Counted at
`origin/main` 3c57948b6, the relay-only trees held about 40,000 lines
(capture page 7,900; Worker 1,100; Pi package 5,700; tests 12,400 of JS and
7,000 of Python; the e0 experiment 2,200), and the seams into
`correction_setup.py`, `correction_crossover_v2.py`, `sync_flow.py`,
`household_mic.py`, `level_match.py` and the correction JS carried several
hundred relay branches more. Every agent working the measurement surface
paid to understand both lanes.

## Decision

Owner ruling, 2026-09-02: the relay and phone capture are deleted whole.
Nothing replaces phone-mic capture. The wired microphone (USB mic, UMIK)
driven from the `jts.local` correction pages is the only capture path.

What stays: the wired measurement flow, calibration upload and vendor
auto-fetch, the LLM-driven measurement programs, the turntable and arm
(`experiments/usb-turntable`, `jasper-angle-capture`), and the neutral
capture vocabulary the wired flow imports (`jasper/capture_protocol.py`,
`crossover_v2/capture_source.py`).

Rules of the deletion: no compatibility shims, no feature flags, no
"relay removed" comments. Seams keep one owner and one shape (the capture
slot in `correction_setup.py` is wired-only and named for what it is; the
mic identity in `household_mic.py` has one payload shape). A reader of the
tree must not be able to tell the relay was there, except here and in git
history. ADR-0188 section 1 (wired-first) and section 4 (arm, bass
extension park, verb split) stand.

## Consequences

- Moving-mic room correction has no transport. If it is ever wanted it is
  a new design, not a revival; the old code is in git history before the
  deletion PRs.
- The stereo-sync wizard and v1 room correction lose their relay path and
  keep only their browser-upload path, which was already the default.
- `relay.jasper.tech` and `capture.jasper.tech` are torn down by the owner;
  the three `JASPER_CAPTURE_*` keys leave every box's `jasper.env`.
- Rejected: keeping the parked island. Parking left the coupling in place,
  and a park with no consumer is dead code the repository pays for on every
  request.
