# ADR-0185: Latency is monitored and adapted, never certified

- **Date:** 2026-08-27
- **Status:** Accepted

## Context

[ADR-0108](0108-a-latency-claim-is-earned-by-a-measured-artifact.md) made the
`usb_low_latency_48k` route's low-latency claim something a box had to *earn*:
a stored artifact carrying measured percentiles, bound to a route identity
hash, graded against a 40/42 ms budget into pass/warn/fail, with promotion
tiers, disclosure codes, and a red `jasper-doctor` check when the proof was
missing or aged.

That machinery answered a question nobody asks. The speaker already reports its
end-to-end latency continuously and already moves its own operating point to
hold it — so a stored grade could only ever say something about a window that
had already closed, and it usually said the wrong thing: a fresh install, a
touched config, or a knob tightened by a later PR turned the box amber or red
while it was measurably fine. Every one of those reds was a re-measurement
chore, never a defect. Latency does not have a threshold that means anything
here; it has a live number the owner can look at.

## Decision

**Latency is monitored and adapted. It is never certified.** There is no
budget, no artifact, no pass/fail grade, and no promotion tier anywhere in the
route-latency path.

- **Monitored.** `/system/audio/` sums the four live queues — USB input
  (`resampler.fill_frames`), mixing (`snd_pcm_delay_ms`), DSP
  (`camilla.buffer_level`) and DAC presentation — from the fan-in and outputd
  `STATUS` sockets on every poll, and names the host-clock ladder state. That
  surface is the answer to "how much delay does this box have right now".
- **Adapted.** The fan-in USB resampler's warm-up cushion and its decay toward
  the 576-frame floor, plus the host-clock servo
  ([ADR-0109](0109-the-combo-host-clock-servo-observes-resampler-correction.md)),
  move the operating point at runtime. Adaptation is the mechanism that keeps
  latency low; a certificate never was.
- **Measured, on demand.** `jasper-route-latency-harness` still plays real
  impulses through the route and prints the per-impulse percentiles plus the
  route-health counter deltas across the window. It is a diagnostic: it writes
  no verdict, persists no claim, and nothing downstream reads its output.
- **Still checked, because it is live:** `jasper-doctor`'s
  "usbsink low-latency contract" compares the *running* fan-in lane against the
  identity the route declares (direct source, negotiated geometry, resampler
  lock/target). That is an observation of the present, not a grade of the past,
  and it stays.

## Consequences

- Deleted: `jasper-route-latency-artifact` and its console entry, the route
  artifact writer/reader/assessor, `route_latency_gate_status` and its
  certified-percentile and disclosure vocabulary, the `USB_LOW_LATENCY_P95/P99`
  budgets and the `p95_budget_ms`/`p99_budget_ms`/`evidence_profile` fields they
  fed, `/state`'s `audio_graph.route.claim_status` and `audio_graph.artifact`,
  the audio-health `verification` / `technical.route_verification` blocks, and
  the "route latency evidence" doctor check. The harness loses
  `--invoke-artifact`, `--require-pass`, `--confirm-route-health-ok`,
  `--measurement-id` and `--duration-seconds`.
- Deliberately given up: a persistent, per-box record that this route once
  measured under 40 ms. If that question is ever asked again it is answered by
  running the harness, which takes five minutes and reports the *current* box.
- Artifacts already written to `/var/lib/jasper/audio-validation/` are left on
  disk. Nothing reads them; they age out as ordinary files. That directory is
  still the home of the chip-AEC readiness and hardware-validation artifacts,
  which are a different concern and untouched.
- Supersedes ADR-0108 in full. [ADR-0101](0101-proven-once-disclose-on-change.md)
  is unaffected as a rule, but route latency is no longer one of its subjects:
  there is no proof left to disclose staleness about.
