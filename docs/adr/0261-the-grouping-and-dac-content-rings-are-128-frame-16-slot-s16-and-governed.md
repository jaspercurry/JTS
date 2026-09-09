# ADR-0261: the grouping and dac-content rings are 128-frame, 16-slot, S16, and governed

- **Date:** 2026-09-09
- **Status:** Accepted

## Context

`deploy/alsa/conf.d/62-jts-ring-grouping.conf` and
`63-jts-ring-dac-content.conf` each declare `period_frames 128`, `n_slots 16`,
`format S16_LE`, `channels 2`, `pace_nominal 1`. Both files sourced those
numbers from a design document and an evidence capture that are not in the
tree, so nothing at HEAD said why they hold or what breaks if they move. Every
constraint behind them does survive, in the ring platform's own constants and
in `c/jts-ring-ioplug/jts_ring_shm.h`; this ADR re-derives the four rules from
those, and the two conf.d files, `jasper/multiroom/grouping_ring.py`, and the
header now point here.

## Decision

**1. The slot is 128 frames because the slot IS the reader's period.** The
period is not free on either ring: `RING_SLOT_FRAMES = 128` is a compile-time
constant with no env override (`rust/jasper-ring/src/layout.rs`, re-declared
for Python as `jasper.fanin_coupling.RING_SLOT_FRAMES`) and is the slot every
ring on the box uses. On the dac-content ring the reader is `jasper-outputd`,
whose resolved `JASPER_OUTPUTD_PERIOD_FRAMES` must equal the slot or the
ioplug attach fails hard; every DAC profile that declares a latency floor runs
outputd at 128, and the floorless HiFiBerry DAC8x Studio (packaged default
1024) is the one profile that cannot arm the lane. On the grouping ring the
reader is the bonded endpoint's CamillaDSP at `RING_CAMILLA_CHUNKSIZE = 128`,
so one slot is one chunk.

**2. The depth is 16 slots because that is the ioplug's ceiling, and the
ceiling is what CamillaDSP's rate controller needs.** `JTS_RING_MAX_SLOTS = 16`
gives 16 x 128 = 2048 frames of buffer. CamillaDSP negotiates
`buffer = next_pow2(max(3*chunksize, 4*min_period))` and drives its rate
controller toward `target_level`; at 4 slots the 512-frame buffer sat below
both the negotiated 1024 and `target_level` 1536, so the controller chased an
unreachable target and drove the writer full into stall/underrun flapping.
2048 clears `target_level` with headroom. With the period pinned by rule 1,
depth is the only buffer axis left, and it is also what bounds how coarse the
writer's delay signal can get (delay = occupancy x period).

**3. The wire is S16_LE stereo at both ends because the snapcast stream is.**
`jasper.multiroom.reconcile.snapserver_argv` pins `sampleformat=48000:16:2`,
snapclient decodes to exactly that, and on the return leg outputd's
dac_content lane is S16 by contract (`rust/jasper-outputd/src/dac_content.rs`).
`format` and `channels` are SPELLED in each block rather than inherited from
the ioplug's compiled defaults: these PCMs are opened directly with no `plug`
wrapper in front, so the hw_params is single-valued and a disagreement is a
failed open, not a quiet conversion.

**4. `pace_nominal 1` is the floor under a stalled or dead reader, not a
clock.** Both rings' writer is snapclient, whose ALSA player expects the
DEVICE to pace it. With a live reader the loop is clocked (outputd owns the
DAC on the dac-content ring; CamillaDSP feeds one on the grouping ring) and
the governor is inert. With a reader that has stalled or died the ring
free-runs and the writer storms — a stalled reader was measured taking this
ring's writer to 763x nominal where a live DAC-clocked reader held it to 1.00x
with zero resyncs. The token bucket's 2500 ppm headroom is sized against a
DIFFERENCE of independent clocks, not one crystal's spec: this fleet's dongle
measures ~667 ppm and the two-crystal case took ~4x that (2667 ppm). The bound
a finite window may observe is
`HEADROOM_PPM + 1e6*(2*period)/(rate*T) + instrument`, whose first two terms
at a 128-frame period are 2500 + 89 = 2589 ppm over 60 s. **The instrument
term is load-bearing, not a rounding hedge: without it the bound is BELOW the
2667 ppm measurement it is supposed to cover.** The field governs PLAYBACK
only: a bind on the capture side would starve CamillaDSP on a DAC-vs-Pi clock
difference.

## Consequences

- The four numbers are now derivable at HEAD. A change to any of them has a
  named failure: a period that is not the reader's is a hard attach error, a
  shallower ring reopens the rate-controller flap, a widened wire is a failed
  open at one end, and a dropped `pace_nominal` restores the storm.
- **The pace figures (763x, 1.00x, ~667 ppm, 2667 ppm, 3111 ppm) were
  observed on 2026-08-20 hardware whose capture is not in this tree.** They
  are recorded here and in `c/jts-ring-ioplug/jts_ring_shm.h` as the sizing
  evidence; they are not re-measurable from the repo, and a retune of the
  headroom should re-measure rather than trust them. Both windows reconcile
  against the derived bound only with a one-time term counted, and neither
  reconciles without one:
  - the graded 2667 ppm window sits inside the interior bound once that
    instrument's own 533 ppm is added: 2589 + 533 = 3122 >= 2667;
  - the +3111 ppm interior-stalled window straddles a STARVATION EXIT, whose
    alias-clamp catch-up releases `1e6*(buffer - period)/(rate*T)` = 667 ppm
    over 60 s once, not as rate: 3111 - 667 = 2444, inside the 2589 interior
    bound.
- Rejected: **a deeper ring.** Depth would have to move
  `JTS_RING_MAX_SLOTS`, `MAX_N_SLOTS` (`rust/jasper-ring/src/layout.rs`) and
  `MAX_SHM_RING_SLOTS` (`rust/jasper-outputd/src/config.rs`) in lockstep, and
  it buys latency, not safety — 2048 frames already clears the one consumer
  bound that motivated the depth.
- Rejected: **an S32 wire.** The program is 16-bit at the source, so widening
  the ring converts nothing; widening one end alone fails the open.
- Rejected: **no governor**, on the strength of the 763x measurement: without
  it a dead reader turns a bonded speaker's ingress into a busy loop.
