# ADR-0214: A raised cushion target is a declared window, not a measurement

- **Date:** 2026-09-02
- **Status:** Accepted (amends ADR-0208's consequences)
- **Context:** ADR-0208 gave the cushion decay one published derivation of its
  own commanded drain (`demand_ppm`) and had `build_obs` subtract it, so the
  outer servo stopped chasing a descent. It disclosed a residual: a snap-back
  with the lane still LOCKED moves the held target to the acquisition ceiling in
  ONE tick. A rate term structurally cannot carry that: the inner
  `RateController` sees a `fill − held` error of up to `ceiling − floor` frames
  and pins its command at `−max_adjust_ppm` until the ring catches up. Through
  that window `demand_ppm` reads 0 (the machine is frozen, not stepping) and the
  fill compensation `fill + ceiling − held` reads 0 (the target is AT the
  ceiling), so the entire commanded refill reached the ladder as clock error.
  Reproduced by mutation: 60 such ticks integrate the railed observable to
  saturation and demote the ladder to `L2Fallback` — pitch neutral, host-clock
  authority lost for the session.

  The sole live trigger is a **ladder demotion out of L0**. A capture-generation
  re-probe is not one: every generation bump reopens the capture, which calls
  `LaneResampler::reset()` first, and that snaps to the ceiling with the ring
  already empty — no deficit, so no window.

- **Decision:** The decay machine owns both halves of "expected fill". It
  already publishes the rate at which it LOWERS the target (`demand_ppm`); it
  now also declares the window in which it has RAISED it and the inner loop is
  still railed getting back (`CushionDecay::refilling`). `jasper-host-clock`'s
  `Obs::locked` is renamed `Obs::steady` — "no declared fill ramp in progress" —
  and while it is false the ladder HOLDS: estimators keep their means, the L0
  servo does not integrate, the last pitch command stands, and the L1/L2
  evidence counters are discarded rather than resumed across the gap.

  The hold is scoped to the servo and the estimators. The probe's own gates
  (`settle_regime_ok`, the mid-measurement abort) stay on the lane's raw playing
  bool: a declared ramp is exactly the railed-but-legitimate regime those gates
  must keep admitting, and gating them on the window would restore the rail gate
  #1167 deleted after the 2026-07-05 AwaitLock deadlocks, delay the re-probe the
  demotion just asked for, and report a `lock_lost` that never happened. With
  that, the crate's raw-lock and playing fields are the same fact for every
  consumer that has ever existed, so they are collapsed to one.

  **Arming.** Only a `NotL0` snap-back that actually lowered-then-raised the
  target arms a window. An `Unlocked` snap-back CLEARS one: that is a session
  boundary, the re-lock seats the cursor afresh, and carrying a window across it
  would park the servo through a new session — including a bounded-prime
  fall-through lock that legitimately seats below the ceiling.

  **Exit.** `interval_periods` consecutive UNSATURATED inner commands, not a fill
  sample. The fill sawtooths by a whole render period while the rail closes the
  deficit at ~0.13 frames per period, so a single burst would shut the window
  early and it cannot re-open; and the target can descend to meet the fill, so a
  resumed descent and an open window legitimately coexist (`demand_ppm > 0` and
  `refilling` are NOT disjoint). The saturated command is the contamination
  itself and is immune to both.

  **Bound.** Refilling `D` frames at a railed command takes
  `D × 1e6 / (max_adjust_ppm × period_frames)` render periods — the sample rate
  cancels. The worst case this machine can create is the whole `ceiling − floor`
  deficit; `refill_cap_periods` is twice that, and the doubling stands in for the
  host term the formula omits: a host running `h` ppm slow nets `authority − h`,
  so at the shipped geometry the real refill takes ~83 s at `h = 0`, ~14 min at
  `h = 450`, and never arrives at `h = 500`. Past the cap the window
  force-clears, counts, and logs, and measurement resumes — the servo goes back
  to steering the host on a still-railed observable, which is what it did before
  this change. The cap exists so a beyond-authority host cannot park the servo
  indefinitely; it is NOT a hand-off to a demotion net. In the shipped
  `ObsMode::Correction` that net does not catch this host at all: the
  anti-windup's arming condition and `uncorrected`'s threshold are the same
  `|err| > probe_ppm/2` test, so the conditional integration pins
  `|feed_forward + trim| ≤ MAX_BIAS_PPM` and `saturated` never becomes true
  (measured: `err = −500` settles the trim at 982 with `l2_evidence_ticks = 0`).
  Only a railed feed-forward reaches L2 there. Tracked as issue #3609 — a
  pre-existing gap, not created or widened by this change.

- **Consequences:** No new compensator and no new knob; the observable is gated,
  not corrected. `/state` gains `resampler.decay.refilling` and
  `refill_force_clears`, and `host_clock.hold {active, ticks, reason}` — while a
  hold is active `fill_frames`, `fill_slope_ppm`, `fill_variance`,
  `correction_ppm` and `dll.*` all freeze, and that object is the only way a soak
  can tell a hold from a flat trace. One `event=fanin.decay_refill` line per
  edge carries duration and whether the cap forced it. The fill compensation
  stays one-sided by construction and is now documented as such: descents ride
  `demand_ppm`, raises ride `refilling`.

  A `DECAY_SNAP <label>` control verb forces the still-locked snap-back on
  demand. Without it the window has no lever — it needs a real ladder demotion —
  and the hold could never be proven on hardware.

  **Removal condition:** remove the window when the snap-back becomes a slew
  inside the demand budget, at which point `demand_ppm` carries the raise. That
  is not this change: the one-tick snap is the only place the repo declares the
  held cushion load-bearing, and a slew inside the budget would double the
  refill time.

  Not covered (unchanged, pre-existing): a bounded-prime FALL-THROUGH lock seats
  below the held target, so its ramp still rides through `Obs::steady` true;
  `PROBE_SETTLE_SECS` covers it as before.
