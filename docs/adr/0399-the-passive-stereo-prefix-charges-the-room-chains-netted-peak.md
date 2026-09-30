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
  2. **The peak.** `biquad.peaking_cascade_peak_db` is the maximum of
     `biquad.peaking_cascade_response_db`: the one stdlib function that reads a series cascade of
     Peaking biquads on the one biquad model. Across a span, its grid is
     `RESPONSE_GRID_POINTS_PER_OCTAVE` (48) log-spaced points plus each filter's own centre inside
     that span. The peak's span is the evaluable span, widened to hold every filter's centre below
     Nyquist. So every centre is sampled, and a lone boost reads its own gain. A chain with no
     boost is not evaluated. The
     [ADR-0370](0370-each-run-purpose-declares-what-it-plays-and-a-bass-run-plays-with-room-off.md)
     room-off rise (`seat_level_reference.rise_without_room_db`) reads the same function as its
     minimum across its band, bit for bit as before.
  3. **One home.** `headroom_charge_db`, `HEADROOM_MARGIN_DB`, `PEAK_EPS_DB` and
     `RESPONSE_GRID_POINTS_PER_OCTAVE` live in `jasper/platform/biquad.py`. The stereo prefix
     imports them without numpy and without `jasper.active_speaker`, and the active path imports
     them from there. `branch_chain`'s grid reads the same density, and its construction does not
     change. No active graph byte changes.
- **Consequences:**
  - A room whose netted peak is at or under unity emits no `room_headroom`, like a cuts-only
    room. A cuts-only config stays byte-identical. A room whose boosts a cut nets under unity
    loses the `room_headroom` it had, so its config changes.
  - Against the old sum, a boosted room's charge moves by its netted peak plus 1.0 dB, less the
    sum of its boosts. The netted peak is at most that sum, so a charge rises by at most 1.0 dB:
    by exactly 1.0 dB for a lone boost with no cut near it, and by 0.7 dB for +0.3 dB at 40 Hz
    with +0.3 dB at 300 Hz (q 4). A charge falls only where the boosts sum to more than the
    netted peak plus the margin, or where the netted peak is at or under unity. The stereo goldens
    move: `room_boost_headroom` from 3.0 to 2.9355 dB and `leader_bake_delays` from 1.0 to
    1.9998 dB.
  - A config keeps its old `room_headroom` until it is re-emitted. The next `/sound` save or live
    draft writes the new value; a move of that trim alone writes in place
    ([ADR-0219](0219-a-durable-save-that-moves-only-a-trim-writes-in-place.md)). A room whose
    charge falls then plays louder at the same fader, by its drop, and one whose charge rises
    plays quieter, by at most 1.0 dB.
  - On the grid, a charged room chain peaks at -1 dB. The grid reads the dense peak to within
    about 0.08 dB inside the active room layer's declared bounds (`room_limits`: 20-500 Hz, q 1-8,
    -10 to +6 dB, 8 filters a side), and to within about 0.025 dB inside the bounds of the retired
    passive strategy that made the rooms on disk (at most 500 Hz, q 0.7-10, at most +2 dB a filter
    and +3 dB in all). A property test holds both under 0.1 dB. The margin covers both.
  - For inputs far outside every producer's bounds, the grid can read more than the 1.0 dB margin
    under the true peak, so the charged chain can peak over unity: 1.6 dB under for #2850's
    near-Nyquist boost and cut pair, and 1.1 dB under for a ±20 dB q 50 pair 0.3 % apart. The old
    sum of boosts bounded every input. No producer reaches those inputs, so no guard is added
    ([AGENTS.md](../../AGENTS.md): no guards for hypotheticals).
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
