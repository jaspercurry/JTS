# ADR-0255: Every product measures through the wired microphone

- **Date:** 2026-09-08
- **Status:** Accepted. Supersedes (partial)
  [ADR-0222](0222-the-relay-is-deleted-the-wired-microphone-is-the-only-capture-path.md):
  the browser-upload carve-out its Consequences left for room correction.

## Context

ADR-0222 deleted the phone relay and ruled the wired microphone on jts.local
the only capture path, but its Consequences left v1 room correction on "their
browser-upload path, which was already the default". That carve-out is live
code. The room product still captures through the browser's `getUserMedia`:
`jasper/correction/session.py:48,270,1135` run the `browser_audio` preflight,
`jasper/web/correction_handlers.py:188,310` run it and serve its report,
`jasper/web/correction_room_flow.py:29-34` documents the secure-context split
it forces, and `deploy/assets/shared/js/measurement-audio.js` plus
`deploy/assets/correction/js/main.js` hold the browser side. Its analysis
metrics default their low edge to 50 Hz because of the iPhone microphone's
high-pass (`jasper/audio_measurement/analysis.py:281`,
`jasper/correction/session.py:1905-1908`).

The speaker-tuning product already captures through the wired kernel
(`jasper/audio_measurement/wired_capture.py`, behind
`jasper/web/correction_crossover_v2_wired.py`): the Pi records its own
excitation, driven from the jts.local pages one prompted position at a time.
The bass-extension program's unbuilt waves are still written around the relay:
`docs/HANDOFF-bass-extension-plan.md` §0 reuses "the calibrated phone-mic
relay", `docs/bass-extension-waves/wave-4-commissioning-backend.md:362,389`
starts a relay session and pulls a relay capture, `bass-commissioning-ux.md:230-234`
renders a relay QR, and `wave-7-hardware-validation.md:24-26` pulls "the
capture phone mid-rung". Two capture shapes for three products is the coupling
ADR-0222 deleted 40,000 lines to remove.

## Decision

Owner ruling, 2026-09-08:

1. All measurement — speaker tuning, room correction, bass extension, and
   anything future — captures through the wired microphone plugged into the
   Pi, driven from the jts.local browser as a position-ready walk: the page
   prompts a position, the operator moves the microphone, the Pi plays and
   records.
2. No browser-microphone or relay capture path will exist again, for any
   product. ADR-0188 §1 (wired-first) stands; ADR-0222's carve-out for room
   correction closes.
3. One household microphone record (identity, calibration, vendor curve)
   serves every product. A product keeps no microphone facts of its own.
4. The browser path in the room product is scheduled for deletion in the next
   wave. This ADR deletes nothing.

## Consequences

- Deletes later, in the wave that moves room correction onto the wired
  session: `jasper/correction/browser_audio.py` and its plumbing in
  `session.py`, `correction_handlers.py` and `correction_room_flow.py`;
  `deploy/assets/shared/js/measurement-audio.js`; most of
  `deploy/assets/correction/js/main.js` (device pick, AudioWorklet meter,
  upload); the built-in-mic mismatch gate in `jasper/web/correction_capture.py`,
  which exists only because a phone could be the device. ADR-0222's rules of
  deletion apply: no shims, no flags, no "browser removed" comments.
- The 50 Hz analysis floor loses its reason. It was the iPhone high-pass, not
  room physics; the deletion wave re-derives the readout floor from the wired
  microphone's calibration or drops it to the design floor, and
  `room_boundary.py`'s "What is NOT owned here" note follows.
- The room product's secure-context split (`/sound/room/` on HTTP, capture
  over HTTPS with a local CA) exists only for `getUserMedia`; it goes with the
  path.
- The bass plan's transport text in waves 4, 6 and 7 is stale as of this ADR
  and is rewritten when those waves are picked up (ADR-0257 §2), not now.
- Docs that describe room correction's browser capture as current
  (`docs/room-correction-information-design.md`,
  `docs/tuning-operator-runbook.md`, the two design docs) are stale from this
  date and are corrected in the deletion wave, where the replacement can be
  described instead of a hole.
- Gives up: measuring a room from a device with no cable to the Pi. Moving-mic
  capture has no transport (ADR-0222); it stays a new design, never a revival.
