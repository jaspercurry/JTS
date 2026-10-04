# ADR-0443: A take whose frame ledger names lost frames is never levelled

- **Date:** 2026-10-03
- **Status:** Accepted. Supersedes in part
  [ADR-0442](0442-a-level-probe-reads-each-burst-it-heard-at-its-anchor.md)'s Consequences line on
  "a frame loss inside the top burst" and
  [ADR-0412](0412-adr-0411s-bound-is-relative-to-the-reading-it-keeps.md) §3's "lost capture frames"
  clause: both now hold only for a loss the frame ledger does not name. Amends
  [ADR-0422](0422-a-placement-gets-two-extra-takes-and-a-probe-at-its-ceiling-stops.md) §2 for a
  probe whose ledger names lost frames.
- **Context:** `capture_dispatch.assess` levelled a take from its reading before it judged the
  recording, and the frame-ledger refusals are part of that judgment (any lost or inserted frame
  fails, #2094). So a take whose ledger named lost frames could still be levelled: a level probe
  that read a burst, and any other take that levels itself and read outside its band. A reading is
  the loudest period of a sweep (ADR-0364), and lost frames can cut that period out. A jts3 seat
  probe with 100 to 200 ms of frames removed at 20% of its −36 dBFS burst solved 2.54 dB louder
  than the same take whole. The review of [#6281](https://github.com/jaspercurry/JTS/pull/6281)
  found it.
- **Decision:** a take whose frame ledger names lost or inserted frames (`FrameLedger.lost_at`) is
  never levelled. Its recording is judged, and the existing refusal decides: `capture_overrun` when
  the recorder counted an overrun, otherwise `drift_baselines_disagree`, with `retake_same` and no
  gain. This holds for a level probe and for every other take that levels itself.
- **Consequences:**
  - A take with a clean ledger does not change. ADR-0442's presence rule, ADR-0411's solve and
    bound, ADR-0422's stop at the ceiling and the output mute guard stay.
  - A probe whose ledger names lost frames is retaken the same. It no longer stops the run as
    `level_unreachable` (ADR-0422 §2) or asks for the microphone as `snr_floor`. A probe that read
    no burst already did this (ADR-0442 §3).
  - The retake is the speaker's charge and counts against the placement's two extra takes
    (ADR-0422 §1), as for any other take with a frame fault.
  - A stopped probe is not refused for its stop. The ledger counts overruns and the frames at each
    stage of the capture chain, never the program's length, and the recorder records its post-roll
    after the stop. All 8 stopped jts3 probes had clean ledgers.
  - A frame loss that the ledger does not name still moves a solve as ADR-0442 and ADR-0412 say.
  - Rejected:
    - Refusing only a probe. A take that levels itself reads its level the same way, and outside
      its band it was levelled from such a reading too.
    - Reading only the bursts the loss missed. The ledger counts lost frames but does not say where
      they were.
