# ADR-0369: The gating-v2 and room-correction regime plans are archived

- **Date:** 2026-09-26
- **Status:** Accepted. Supersedes (partial)
  [ADR-0228](0228-rulings-carried-out-of-refactor-tuning-on-its-retirement.md)
  §11, which kept both plans standing.
- **Context:** ADR-0228 §11 kept `docs/gating-v2-plan.md` and
  `docs/room-correction-regime-plan.md` standing (owner, 2026-08-26). Since
  then no PR of gating-v2's ladder has landed, and most of the code it
  targeted is deleted: the cloud combine, the group floor, and the verify and
  retake paths (the last Gen A halves went in #5639).
  In the regime plan, ADR-0256 supersedes D1 and carries D2, D6 and D7. The
  only rule the plan alone still held was D5's, and D5 also says household
  room strategies stay cuts-only, while the room door admits boosts up to
  `ROOM_MAX_FILTER_BOOST_DB`. The 2026-09-22 toolbox review raised this
  (#5665 item 1), and the owner ruled on 2026-09-26: archive both plans.
- **Decision:**
  1. Both plans move to `docs/historical/`. Each file's status line names
     this ADR, and `docs/doc-map.toml` no longer lists them.
  2. D5's boost-admission evidence is now in the module docstring of
     `jasper/audio_measurement/room_limits.py`, beside `admit_boost` and the
     boost caps that enforce it: a dip that persists across positions, shaped
     like a room mode rather than a null, within bounded headroom. D5's
     cuts-only line does not carry over, because the shipped caps are the
     policy.
  3. Nothing from gating-v2 carries over. `gating.py` drops its pointer to
     the plan's D3 `detector` field.
- **Consequences:** Anyone reading the room or gating code now finds only the
  code's own docstrings and the ADRs, with no plan that contradicts the code.
  The plans remain in `docs/historical/` as a research record, so the
  D-numbers that ADR-0256 and the research notes cite still resolve. Resumed
  gating or residual-tier work starts from HEAD, not from either plan's
  ladder. The files are archived rather than deleted (ADR-0199's route)
  because the owner chose that. The status line is what stops a grep hit from
  reading as current.
