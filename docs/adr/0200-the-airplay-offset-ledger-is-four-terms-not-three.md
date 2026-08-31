# ADR-0200: The AirPlay offset ledger is four terms, not three

- **Date:** 2026-08-31
- **Status:** Accepted

## Context

ADR-0118 established that `audio_backend_latency_offset_in_seconds` must be
derived from the live chain, never hand-set — that principle stands. Its
formula section is stale: it predates the ring topology (ADR-0100) and
undercounted the topology JTS actually ships. A source-verified audit found
three defects in `derive_audio_backend_latency_offset`
(`deploy/bin/jasper-apply-airplay-mode`): the fan-in term was a hardcoded
1024-frame fossil left over from the pre-ring `JASPER_FANIN_OUTPUT_BUFFER_FRAMES`
(the real ring-topology term, Ring A's slot capacity, is 256 frames); the
CamillaDSP term computed `target_level - chunksize`, which silently drops to
0 whenever target_level equals chunksize — the shipped config's actual shape
(`deploy/camilladsp/outputd-cutover.yml`); and Ring B (CamillaDSP -> outputd,
introduced by the ring topology) was never counted at all. On the shipped
config this moved the derived offset from -0.0265 s to -0.0158 s.

## Decision

The ledger is four terms:

```
-( (ring_a_frames + camilla_frames + ring_b_frames + outputd_frames)
   / samplerate + bonded_extra_sec )
```

- `camilla_frames = max(target_level - chunksize, 0) + chunksize` — Camilla
  holds at least one full chunk, plus any extra fill `target_level` asks for
  above that baseline.
- `ring_a_frames` / `ring_b_frames` each prefer live daemon STATUS (fan-in's
  `output.snd_pcm_delay_frames` ALSA delay, correct on a loopback box; else
  `output.ring.occupancy * output.period_frames`, the ring's live fill;
  outputd's `shm_ring.occupancy * shm_ring.slot_frames` for Ring B), then a
  `jasper.ring_assets` parse of the shipped
  `deploy/alsa/conf.d/60-jts-ring.conf` (the production authority for that
  file's schema — never a bespoke re-parse), then a hardcoded default
  matching that same shipped file. Ring A counts the full slot capacity
  (proven pinned-full by STATUS `full_waits`/`published` telemetry); Ring B
  counts only the one-slot floor, since no equivalent live evidence exists
  for it.
- `outputd_frames` is unchanged from ADR-0118: live `dac.snd_pcm_delay_frames`,
  else the configured buffer size.

The full per-term reasoning lives beside the formula in
`deploy/shairport-sync.conf.template` and in
`deploy/bin/jasper-apply-airplay-mode`'s own comments, never restated a third
time here.

Empirical validation path: `scripts/jasper-pipe-probe latency LANE_PCM`
measures the actual renderer-to-post-DSP-tap latency for a lane end to end.
It does not replace this derivation (it has no notion of the AP2 PTP anchor
the offset compensates for), but it is the tool to reach for when a derived
total needs cross-checking against a real, on-box timing measurement rather
than trusted on arithmetic alone.

## Consequences

- A ring retune (a different `period_frames`/`n_slots` in the shipped conf)
  propagates through Ring A/B's parsed tier without a code change here.
- The derived offset moves for every box on the ring topology (the fleet
  default): less aggressive early-compensation, closer AirPlay lip-sync
  accuracy. No audio-path behavior changes — this is offset bookkeeping only.
- `jasper/multiroom/airplay_latency.py`'s `PIPELINE_FIXED_DELAY_SEC` (the
  bonded-leader tight-regime threshold) is a deliberately conservative,
  independent round-number bound over every term's own fallback default at
  a large chunksize/target_level shape — not a live re-derivation of this
  formula. It moved from 0.150 to 0.160 alongside this change so it stays
  the safe (over-, never under-) bound in the direction that matters.
- Rejected: a bash/awk re-parse of the ring conf.d schema as Ring A/B's
  fallback tier. `jasper.ring_assets` already owns that schema (brace-matched
  block bodies, torn-conf detection across the file's shared `period_frames`)
  for the doctor, the coupling reconciler, and the conf.d renderer; a second,
  independent parser in bash would drift from it silently and does not
  understand the torn-file invariant those three callers already depend on.
