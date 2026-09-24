# ADR-0359: The bass boost plays at every volume and gives way only near clip

- **Date:** 2026-09-24
- **Status:** Accepted. Supersedes ADR-0352 §2–§4, §1's delta-zero rule, and
  §5's fader part and plain-shelf closed form.

## Context

[Issue #5710](https://github.com/jaspercurry/JTS/issues/5710): under
ADR-0352 the native `Loudness` filter on `Aux1` tied the boost to the volume
knob, not the music. Full boost ended 20 dB under the reference, which is
capped at 0 dB. On jts3's tune B, the owner's listening level lost about 8 dB
at 30 Hz, and 100% lost all of it. The taper protected headroom by proxy.
On jts3 the real limits are far away. The drive is limited by the DAC
(2.1 Vrms full scale). A bridged TPA3255 on 36 V clips at about −0.5 dBFS.
At full digital level the E150HE-44 moves about 6 mm (Xmax 14.7 mm) and takes
about 85 W (rated 200 W). The issue has the sources.

## Decision

1. **One structure, full boost.** Each boost lane plays one native biquad,
   chosen by `boost_biquad` in `jasper/bass_extension/dynamic.py`. For a
   `linkwitz_transform` it is a `LinkwitzTransform` with the descriptor's own
   numbers. An ADR-0352 plain section plays its full-boost Loudness shelf
   (CamillaDSP `loudness.rs`): `Lowshelf`, 70 Hz, gain `low_boost_db`, at
   slope 12, which JTS emits as `q` = `SHELF_Q` like every shelf. The block
   is expand → boost biquad → `form_delta`
   (copy − dry; the detector is the boosted front copy) → delta high-pass and
   detector low-pass → Compressor → reduce. It has no `Loudness`, `Volume`,
   `Aux1`, control channel, shape stage or mixer gain. That is 3 mixers and at
   most 3 filters, all named with the `bass_ext_dynamic` prefix.
2. **The compressor is the limit.** ADR-0335 is unchanged: one gain per
   cardioid pair, detector on the front lane. `compressor_threshold_dbfs` is
   the detector level, in dBFS at the front woofer output, where the boost
   starts to give way. Calibrate it under the lower of the owner limiter
   (−1 dBFS) and the amp's clip point, less the rear lane's excess over the
   front and the envelope and 10:1 slope margin. For jts3 B at 36 V that is
   about −15 dBFS bridged (2 chips): a time-domain run of the block puts B's
   rear lane (+5 dB over the front at 30 Hz) at the limiter only from
   near-full-scale 25–30 Hz content at 100%, and at 78% a loud 30 Hz note
   keeps about 14 of its 19.5 dB. Single-ended (1 chip) starts near −21 dBFS.
   The detector corner covers the band where the boosted lane runs hot; B's
   114 Hz cut lets it keep 100 Hz.
3. **Descriptor.** A new section requires `linkwitz_transform` and
   `delta_highpass_hz` and has no `low_boost_db` or `reference_level_db`; a
   prescription document refuses the old form as `bass_descriptor_malformed`.
   A stored ADR-0352 section still loads. Its `reference_level_db` is ignored,
   and its normalized payload keeps its bytes, so banked fingerprints do not
   move. There are no new bounds and no new refusal codes. The delta-zero
   rule of ADR-0352 §1 goes with the `LowshelfFO` it served.
4. **Model.** `expected_boost_db(descriptor, freqs)` is |1 + HP·(T − 1)| and
   takes no fader. `dynamic_bass_gain_reserve_db` is the peak of
   1 + |HP·(T − 1)| for every section: 1/48-octave grid, plus 0.01 dB. That
   bound holds for any compressor gain. The plain shelf's closed form goes
   too, because it ignored the high-pass. The seat-level lift is the full
   reserve at every fader.

Proof, jts3 B (107 Hz / Q 0.44 → 30 Hz / Q 0.707, high-pass 22 Hz): the model
is within 0.0006 dB of the analog 1 + HP·(T − 1) from 5 Hz to 20 kHz. The
boost is +17.9 dB at 20 Hz, +19.4 at 25, +19.5 at 30, +17.9 at 40, +13.3 at
63, +8.6 at 100 and +5.0 at 160. The reserve is 19.6 dB. Tests pin that the
emitted fragment plays the model.

## Consequences

- **Safety case.** The ceiling stays where it was. The owner limiter caps
  each woofer lane at −1 dBFS, with or without the boost. `volume_limit`
  0.0, the `set_volume_db` clamp and the SPL stop are unchanged. Under the
  taper, Main plus boost never passed 0 dB, so the static graph bounded the
  deep-bass drive. Now deep bass can reach the limiter from any volume: on
  B, a full-scale 30 Hz input reached −9.9 dBFS and can now reach −1 dBFS.
  Handling that is the compressor's job. On jts3, a lane at −1 dBFS moves the
  cone about 5 mm and puts about 70 W into the coil. Every speaker with a
  bass section gets this after its next deploy, at its stored threshold. If
  some speaker's amp can push its woofer past its limits at −1 dBFS, the fix
  is a lower threshold at calibration, not a new guard.
- Old tunes lose their taper. They play full boost at every volume, limited
  by their stored threshold. B's −18 dBFS compresses loud notes early, which
  is safe. A new-form copy with the calibrated threshold replaces it.
- The detector reads T·dry without the delta high-pass. Below that corner it
  over-reads, so infrasonic content is compressed early.
- Old bass-round evidence stays valid (ADR-0101). It was measured with the
  taper: a level whose takes played the `Loudness` filter lists
  `volume_taper` in its `compression_includes`. The bass replay attributes
  stages only for a graph with this block; an older graph still replays
  without `--bass-descriptor`, and `dsp-levels` refuses a replay manifest
  rendered before this ADR.
- A seat anchor levelled under the taper, above reference − 20 dB, carried
  only part of the boost, so predicted rung SPL under-reads the bass until
  `jasper-seat-level` runs once after the deploy (jts3's 2026-09-17 anchor:
  about 3 dB for B). The SPL stop still ends a take that runs over.
- Older code refuses a new-form section and parks the speaker muted. Before a
  downgrade past this ADR, `jasper-round apply` an ADR-0352 tune; the same
  command recovers a speaker that parked after one.
- `Aux1` has no reader. Its plumbing is deleted separately (#5710 slice 2).
  ADR-0311's level-dependent boost schedule was a declaration; now there is
  none.
- Rejected: keeping the taper as a headroom proxy; an excursion or thermal
  model (on jts3 the DAC-limited drive stays near 6 mm and 85 W); a rear-lane
  detector (the rear excess goes into the threshold instead); a size rule
  for the shape (#5704 item 1: there is no shelf to under-read).
