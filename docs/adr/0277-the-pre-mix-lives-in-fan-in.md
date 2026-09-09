# ADR-0277: The pre-mix lives in fan-in, because CamillaDSP captures one device

- **Date:** 2026-09-08
- **Status:** Accepted
- **Context:** A smart speaker plays music AND voice prompts. They need
  different mix policy but the same speaker-protection path: music should duck
  while speech plays, while TTS and cues still need crossover, correction, gain
  ceilings and active-speaker protection. The Linux-on-one-Pi version of that
  pattern has a hard constraint — CamillaDSP supports exactly one ALSA capture
  device per process — so something upstream has to present it a single stream.
  This rationale lived only as prose at the top of
  [docs/audio-paths.md](../audio-paths.md); it is a decision, not a description,
  and the doc rewrite lifted it here.
- **Decision:** `jasper-fanin` is the pre-mix. Renderer and program lanes are
  ducked there, TTS is mixed *after* the duck, CamillaDSP receives one stream
  for crossover/protection, and `jasper-outputd` writes the final sink. No
  ALSA `multi` aggregation sits in the hot path.
- **Consequences:** Single Apple, dual Apple and DAC8x profiles get identical
  TTS semantics, and assistant loudness has exactly one owner — the pre-DSP
  mix boundary — rather than one per output profile. The passive bonded
  multiroom member is the deliberate exception: its local TTS enters outputd
  *after* CamillaDSP so replies do not ride the shared sync buffer, which is
  why that path treats downstream attenuation as zero instead of reusing
  fan-in's `- downstream_db` algebra. Given up: a renderer cannot reach
  CamillaDSP without traversing fan-in, so fan-in is on the critical path for
  every source; that is the cost of one capture device.
