# Wake-telemetry build record (2026-05) — historical

> **Status: historical.** Frozen record of how the wake-event telemetry
> subsystem was scoped and built in May 2026: the evidence that motivated it,
> the four-PR sequence, the two-leg architecture it started from, and the
> integration bugs worth remembering. Kept because the motivating measurement
> cost a recording session and because the capture-ring bug recurs by design.
> **Nothing here is current operational truth** — the live spine is
> [HANDOFF-wake-telemetry.md](../HANDOFF-wake-telemetry.md), and the decisions
> are ADR-0132, ADR-0133 and ADR-0134.

## Why the subsystem was built

As of 2026-05-21, the 2026-05-20 wake-rate sweep showed 14 of 20 Jarvis
utterances stayed at 0.001 confidence across **all 23** AEC configurations —
AEC tuning had reached the end of its useful range against the then-current
`jarvis_v2` openWakeWord model. Synthetic phone-track testing
(`scripts/wake-rate-test.sh`) was a poor proxy for the real distribution of
user attempts and brutal to iterate on. The subsystem replaced that synthetic
feedback loop with production telemetry on real attempts.

The expected gain from OR-gating two legs was roughly +15 percentage points
over the better single leg, from test-1 aligned A/B data.

## The two-leg starting architecture (PR #191, 2026-05-22)

```
                chip ch 1 (ASR beam, BF+NS+AGC+HPF,
                chip AEC disabled via SHF_BYPASS=1)
                               │
                               ▼
                      jasper-aec-bridge
                      ┌────────────────────┐
                      │   chip-direct mic  │ ── udp:127.0.0.1:9877 ─┐
                      │   (pre-AEC)        │                         │
                      │         ▼          │                         │
                      │   WebRTC AEC3      │                         │
                      │         ▼          │                         │
                      │   AEC ON output    │ ── udp:127.0.0.1:9876 ─┤
                      └────────────────────┘                         ▼
                                                    jasper-voice WakeLoop
                                                    (two UdpMicCapture,
                                                     one model per leg,
                                                     OR-gated)
```

Both streams carried 16 kHz mono int16, 1280-sample (80 ms) packets. The
chip-direct stream was what AEC3 received as its near-end input, before any
AEC3 processing. The leg set later grew to DTLN and the two XVF fixed ASR
beams; the per-leg column shape in the DB still carries the irregular
`peak_score_aec_on` / `peak_score_aec_off` / `peak_score_dtln_aec` names from
this era, which is why `jasper/voice_daemon.py:_LEG_DB` lists columns
explicitly instead of deriving them from the leg token.

## The four-PR sequence

Each PR was independently shippable; PR 2 depended on PR 1's UDP stream, PR 3
on PR 2's per-frame scores, PR 4 on PR 3's DB.

- **PR 1 — bridge emits a second UDP stream.** `jasper-aec-bridge` gained a
  second non-blocking socket on `JASPER_AEC_UDP_PORT_RAW` (default 9877)
  carrying the chip-direct mic. Pure plumbing; jasper-voice kept consuming
  only 9876. Safe to deploy alone — nothing listened on 9877 yet.
- **PR 2 — wake loop ingests both streams, OR-gate fires.** Two
  `WakeWordDetector` instances, every frame scored on both, fire on either,
  one shared refractory. The accepted trade was OR-gating without per-config
  false-positive measurement first (see ADR-0132); the spend cap and the
  sustained-speech VAD gate carried the risk, and PR 3's
  `ts_speech_detected`-null query was the post-deploy signal.
- **PR 3 — SQLite + capture + funnel hooks.** `jasper/wake_events.py`, the
  funnel hook calls in the daemon, the WAV capture, the retention sweep, and
  the `mkdir`/`chown` in the installer.
- **PR 4 — `/wake-review/` web wizard. Never built.** The plan was a
  socket-activated wizard at `http://jts.local/wake-review/` with a sortable
  manifest, per-leg audio players, score sparklines and a label dropdown.
  Post-hoc review moved to the laptop-side analysis scripts against a fetched
  corpus instead; ADR-0134 records why no on-device review surface exists.

## Design shape that did not survive contact

- **Near-miss capture.** The original capture trigger was "a real fire **or**
  either leg crossing 0.10 in any 80 ms frame within a 6 s window, with a 5 s
  refractory between captures". The near-miss half was never implemented — the
  shipped store only records real fires, and `trigger_kind` only ever takes a
  `fire_*` value. The rationale for the 0.10 floor (lower captured mostly pure
  music, and the 0.001 utterances are invisible at any floor) is preserved in
  ADR-0132.
- **A 1 GiB audio ring** sized for 5-7 weeks at 30-50 events/day. Superseded by
  ADR-0133: 128 MiB, with the gold corpus as the durable store.
- **A `schema_version` table.** Deferred at design time and never needed; the
  store migrates additively via an `ALTER TABLE`-on-`open()` column list.
- **Per-wake-model score columns** (`peak_score_jasper_v1_aec_off` and
  friends) and a `fired_legs` encoding that identified the model as well as the
  leg (`"jasper_v1@aec_on,jarvis_v2@dtln"`). Sketched for the custom-model
  track; the shape was never fixed and the training program went a different
  way — see HANDOFF-wake-training-experiment.md.

## Integration bugs worth remembering

- **The secondary leg's capture-ring append is a separate concern from its
  scoring**, and is easy to forget. The primary loop appends each frame to its
  capture ring after the pre-roll append; each secondary leg loop has to do the
  same in the same gating position (past the mic-mute / measurement-active
  checks, before the acquisition checks). It shipped without that in the
  integration branch and the result was `audio_off_path` NULL on every event
  even though dual-stream wake was firing correctly.
- **`music_renderer` shipped in `CREATE TABLE` but was omitted from the
  migration column list.** A DB created before that column existed (upgraded,
  not reset) lacked it, the INSERT naming it failed, and the fail-soft handler
  swallowed the loss — silent telemetry loss on exactly the Pis with the most
  history. Every column now goes in both places.
