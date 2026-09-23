# ADR-0343: The room cut floor is a disclosure, not a refusal

- **Date:** 2026-09-22
- **Status:** Accepted. Supersedes (partial) ADR-0256 §2: the spatial σ stays
  the room layer's confidence signal, but it no longer caps a prescribed cut.
- **Context:** ADR-0207 retired every cut-depth bound on the driver and blend
  doors: a cut only removes level, so its depth is the prescriber's to spend,
  and an envelope bounds the fitter, never a prescription. The room door came
  later (ADR-0256 §2) and added one back. It refused `filter_cut_too_deep`
  when a filter's gain went below the floor its bin's cross-position spread
  supports. It refused `taper_violated` when the composed cut fell more than
  `ROOM_COMPOSED_TOLERANCE_DB` below that floor, which tapers to zero at the
  ceiling. Neither refusal guards hearing or hardware (the 2026-09-22 audit,
  finding F13). Owner ruling, 2026-09-22: a note, not a refusal.
- **Decision:**
  1. Consistent with ADR-0207, the room door never refuses a cut for its
     depth. A side's composed response can fall more than the composed
     tolerance below the tapered, spread-derived floor
     (`room_limits.cut_floor_db`). At those bins, each filter that itself
     cuts by more than the tolerance carries `cut_beyond_spread_db` on its
     entry in the judged receipt: the worst fall past the floor, in dB. The
     document composes. The note never enters the candidate's room set.
  2. `filter_cut_too_deep` leaves the room door's refusal vocabulary.
  3. The taper keeps its boost half. `taper_violated` still refuses a
     composed boost more than the tolerance above the tapered boost cap. The
     per-filter and per-side boost caps and the spatial boost admission do
     not change: a boost spends headroom, and ADR-0207 §3 keeps boost bounds.
- **Consequences:** A prescriber can spend a cut that the seats do not agree
  on. The receipt says how far past the floor it goes, and the round's own
  measured verify is the net, as on the driver and blend doors. Unlike
  ADR-0207 §4, this retired bound leaves a disclosure, because the floor
  comes from measured evidence (the cross-position spread), not from a policy
  number. The floor, its taper and the tolerance stay in `room_limits.py` and
  `room_prescription.py`, and `judge --preview` still reports the per-bin
  `cut_margin_db`. A fall within the tolerance is not disclosed, for the same
  reason the tolerance exists: a bell's skirt at the ceiling spends no
  audible level. Rejected: a note only at each filter's centre frequency.
  With it, a wide cut still open at the ceiling, or two stacked cuts, would
  pass with no note.
