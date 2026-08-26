# ADR-0118: The AirPlay backend latency offset is derived from the live chain, never hand-set

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

AirPlay 2 inverts AP1's latency contract. The receiver advertises
`Audio-Latency: 0` and the **sender** authors the whole timeline: it picks the
latency, ships it as the PTP anchor, and delays its own on-screen video by the
same number to hold lip-sync. The receiver is informed, not consulted, and the
sender does not measure our hardware — it *assumes* sound emerges at the anchor
time. The entire burden of landing sound on that anchor is ours, and any
uncompensated downstream delay surfaces as audio-late lip-sync.

shairport reads `snd_pcm_delay()` on its own output handle to estimate that
delay. On JTS that handle is a private renderer lane, so the number it gets is
that lane's ring fill — not CamillaDSP's target buffer, jasper-fanin's output
queue, or jasper-outputd's DAC queue. Those are real, fixed, and invisible to
it. shairport's documented lever for exactly this is
`audio_backend_latency_offset_in_seconds`.

Each of those three terms is owned by a different file and changes for its own
reasons (a DAC swap, a buffer trim, a topology change). A hand-written offset
is a second copy of all three that nothing keeps honest, and the one on-Pi
chirp measurement that produced a magic number is not reproducible — the tap it
correlated against was deleted with the topology it belonged to.

## Decision

`deploy/shairport-sync.conf.template` carries the placeholder
`__AUDIO_BACKEND_LATENCY_OFFSET_SECONDS__` and nothing else.
`deploy/bin/jasper-apply-airplay-mode` renders it at every unit start as

```
-((target_level - chunksize + fanin_output_latency + outputd_dac_latency) / samplerate)
```

`target_level` and `chunksize` come from the **active** CamillaDSP config. The
fan-in and outputd terms **prefer the daemons' live STATUS
`snd_pcm_delay_frames`** and fall back to the configured buffer sizes only when
a daemon is unavailable or too old to publish the field. Any residual beyond
owned queue telemetry needs an in-daemon timestamp proof before it is chased —
it does not become a constant.

## Consequences

- A buffer or DAC change re-renders the offset at the next shairport restart,
  with no doc, template, or second constant to update.
- The offset is local: it never goes on the wire. In the AP2 path it folds into
  the PTP anchor unconditionally, so one static value shifts playout by exactly
  `added_latency / rate` for both AP2 stream types with no stream-type branch.
- An over-budget offset is not a crash. shairport warns and drops the offset;
  the realized result is bounded residual lag, never corruption.
- This is a **video/multi-room sync** correctness measure. It does not reduce
  packet-drop rates — that class of fault is a topology problem and was fixed
  by the fan-in cutover.
- A bonded leader plays its own channel through the Snapcast round trip, whose
  buffer is not in the derivation and is never recomputed on bond. That gap is
  disclosed rather than silently absorbed: `/state`, `/rooms.json`, the `/rooms`
  card, and a doctor check report the computed fit on an active bonded leader
  and stay silent (`applicable: false`) everywhere else.
- Rejected: `disable_synchronization=yes`, which removes the symptom by
  removing A/V and multi-room sync. Rejected: the deprecated per-source AirPlay
  latency settings.
