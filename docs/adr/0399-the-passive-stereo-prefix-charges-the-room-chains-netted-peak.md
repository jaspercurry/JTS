# ADR-0399: The passive stereo prefix charges the room chain's netted peak

- **Date:** 2026-09-30
- **Status:** Accepted. Supersedes
  [ADR-0121](0121-preference-boosts-boost-room-boosts-are-compensated.md)'s positive-sum
  `room_headroom` rule on the passive stereo path.
  [ADR-0385](0385-the-program-charge-is-the-emitted-graphs-peak-with-one-margin.md) already
  superseded it on the active path, so no path keeps it.
- **Context:** `build_stereo_prefix` charged `room_headroom` as the sum of the room chain's
  positive gains. That sum is an upper bound, not the peak: a room cut did not net against a room
  boost, and a lone boost paid no margin. The active path charges the emitted graph's netted peak
  plus one 1.0 dB margin (ADR-0385, [#5909](https://github.com/jaspercurry/JTS/issues/5909)
  H1-H3), and the owner asked for one ledger. The stereo emitter re-runs on every `/sound`
  live-draft move, where the web process must not load numpy
  ([ADR-0226](0226-constrained-hardware-doctrine-push-dont-pull-no-spawns-one-interpreter.md)).
- **Decision:**
  1. **The charge.** `room_headroom` is `biquad.headroom_charge_db` of the louder room chain's
     netted peak: that peak plus `HEADROOM_MARGIN_DB` (1.0 dB) when it is over `PEAK_EPS_DB`
     (1e-3 dB), else no filter. The active charge (`program_headroom.charge_db`) calls the same
     function. Room cuts net against room boosts. Preference EQ still rides at unity (ADR-0121).
  2. **The peak.** `biquad.peaking_cascade_peak_db` reads a room chain as a series cascade of
     Peaking biquads on the one biquad model, with the standard library only. The grid is 48
     points per octave across the evaluable span, plus each filter's own centre, so a lone boost
     reads its gain exactly. A chain with no boost is not evaluated.
  3. **One home.** `headroom_charge_db`, `HEADROOM_MARGIN_DB` and `PEAK_EPS_DB` move to
     `jasper/platform/biquad.py`. The stereo prefix imports them without numpy and without
     `jasper.active_speaker`, and the active path imports them from there. No active graph byte
     changes.
- **Consequences:**
  - A room that never leaves unity emits no `room_headroom`: a cuts-only room stays
    byte-identical, and so does a boost that a wider cut nets under unity.
  - A one-boost room charges 1.0 dB more than before. A multi-boost room with overlaps or cuts
    charges less. The stereo goldens move: `room_boost_headroom` from 3.0 to 2.9355 dB and
    `leader_bake_delays` from 1.0 to 1.9998 dB.
  - A config keeps its old `room_headroom` until it is re-emitted. The next `/sound` save or live
    draft writes the new value; a move of that trim alone writes in place
    ([ADR-0219](0219-a-durable-save-that-moves-only-a-trim-writes-in-place.md)). A multi-boost room
    can then play louder at the same fader, by up to its drop, and a one-boost room plays 1.0 dB
    quieter.
  - On the grid, a charged room chain peaks at -1 dB. Inside the declared room bounds (20-500 Hz,
    q 1-8, -10 to +6 dB, 8 filters a side) the grid reads the dense peak to within about 0.05 dB,
    and a property test holds it under 0.1 dB. The margin covers that.
  - Cost: about 2 ms per boosted 10-band chain on a laptop (Apple M1 Max), and nothing for a
    cuts-only room. The Pi Zero 2 W time is measured after the deploy. A cache follows only if one
    live-draft move measures over about 250 ms (ADR-0385).
  - These do not change: `devices.volume_limit` stays 0.0, no patch writes `devices`,
    `set_volume_db` still clamps, and the SPL stop stays. `jasper/multiroom/` is not edited: the
    passive leader bake gets the new charge through `emit_sound_config`, and the active leader's
    bake carries no room PEQs.
  - Rejected:
    - Calling `program_headroom.charge_db` on a stereo graph. It needs numpy on every live-draft
      move.
    - A stdlib copy of the active evaluation grid. A room chain has no shelves, mixer sums or
      all-passes, so its centres on a 48-per-octave background are enough.
    - Keeping the positive sum. It over-charges a netted room and gives a lone boost no margin.
