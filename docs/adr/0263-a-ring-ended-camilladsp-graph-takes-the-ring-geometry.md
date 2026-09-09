# ADR-0263: A ring-ended CamillaDSP graph takes the ring geometry

- **Date:** 2026-09-09
- **Status:** Accepted
- **Context:** Since ADR-0100 every CamillaDSP graph on a box crosses the SHM
  ring on at least one end, and the ring's capacity is a compile-time constant
  of the fan-in writer and the ioplug (`RING_SLOT_FRAMES x n_slots`, 256
  frames on the stereo ring). The flat boot graph and the active ring graph
  passed the certified ring geometry (chunk 128 / target_level 128 /
  queuelimit 1 / rate_adjust off) explicitly, while every applied sound or
  room-correction graph re-emitted through a second owner: a per-DAC
  `CamillaFloor` (256/1536), operator env knobs, a lab-override escape hatch
  and a clamp to the ring's capacity. Against a 256-frame ring a target of
  1536 is unreachable and was harmless only because `resolve_enable_rate_adjust`
  forces rate adjust off on ring PCMs, and nothing asserted that pairing.
- **Decision:** A graph whose governing device is a ring PCM (the playback
  end when it is an ALSA device, else the capture end behind a clockless
  `File` sink) takes `RING_CAMILLA_GEOMETRY` whole for every field the caller
  leaves unset: chunksize, target_level and queuelimit, with rate adjust off.
  `CamillaFloor`, the per-DAC floor lookup, the lab override and the capacity
  clamp are deleted. The operator knobs `JASPER_CAMILLA_CHUNKSIZE` /
  `JASPER_CAMILLA_TARGET_LEVEL` govern only a non-ring ALSA sink, and a ring
  end that ignores a set knob logs `event=camilla_latency.operator_knob_ignored`.
  The doctor warns on a loaded ring-playback graph whose target_level exceeds
  the ring's capacity, as a stale graph to regenerate.
- **Consequences:** One owner for the buffering geometry of every shipped
  graph; the emitted YAML for a ring-ended sound or correction graph moves
  from 256/1536/4 to 128/128/1 (every filter, mixer and pipeline byte is
  unchanged, so the samples are identical and only latency moves). The
  starting point is the certified pair; if the Pi Zero 2 W shows CamillaDSP
  short-read storms at chunk 128, the numbers go in the PR that observes them
  and the static default is set from that data, not tuned blindly. Rejected:
  keeping a DAC floor as a second owner (the DAC does not bound a ring), and
  clamping rather than owning (a clamp hides the second owner instead of
  removing it).
