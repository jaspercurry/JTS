# ADR-0349: jasper-heal is deleted

- **Date:** 2026-09-23
- **Status:** Accepted. Supersedes
  [ADR-0271](0271-jasper-heal-is-a-control-supervisor-that-observes-before-it-acts.md).
  Supersedes (partial)
  [ADR-0283](0283-camilladsp-starts-only-on-a-graph-proved-against-its-own-topology.md) §2,
  the heal `stopped` case; §§1 and 3 stand.
- **Context:** ADR-0271 shipped heal observe-only, with a removal rule:
  "delete this module if that fortnight never puts a would_act line before a
  real incident". A read-only journal count since 2026-09-09 found one
  `heal.would_act` line against 43 `heal.start` on jts.local, and none against
  18 on jts4: heal would have acted once across two speakers in two weeks, and
  the owner ruled the rule met (#5643, decision HEAL-FORTNIGHT). jts3 was not
  checked. Heal never called an actuator. It published
  `/state.resilience.heal` and logged `event=heal.*`; no page rendered either,
  and the block's one reader was heal's own `heal recency` doctor row, which
  reported whether it was still ticking.
- **Decision:** Heal is deleted: `jasper/control/heal_supervisor.py`, its
  start in jasper-control, the `/state.resilience.heal` block and the
  `heal recency` doctor row. Nothing replaces the `/state` block. Each case it
  watched keeps a reader that does not need it. `stopped`, a CamillaDSP start
  the topology gate skipped (systemd counts a condition skip as a success, so
  `Restart=` never retries it), is the core doctor check
  `camilla statefile topology`: it reads the gate's `/run` record itself and
  fails on a refusal. `silent` is the `speaker silence` row, whose codes
  include the four heal watched. `deaf` is the `Wake recency` row, on the same
  24 h threshold.
- **Consequences:** `/state` moves to schema 6, and `resilience` is back to
  the three in-process supervisors ADR-0270 allows. jasper-control runs one coroutine
  fewer on its shared loop, without the `systemctl show` and voice STATUS
  read heal made every 10 minutes. A gate refusal now reaches two of the three
  surfaces ADR-0283's Consequences list: the journal line and the doctor row.
  ADR-0271's arming path closes with heal, so wiring a self-restart onto the
  audio path would take a new ADR. Rejected: keeping heal for the `stopped`
  case alone, because the doctor row already fails on that posture and heal
  added only a line nobody read.
