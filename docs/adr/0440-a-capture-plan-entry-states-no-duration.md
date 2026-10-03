# ADR-0440: A capture-plan entry states no duration

- **Date:** 2026-10-03
- **Status:** Accepted. Amends [ADR-0417](0417-the-courtesy-prelude-announces-each-run-once.md)
  §2's last sentence ("The capture plan budgets the prelude on that entry") and
  [ADR-0434](0434-sweeps-per-take-are-a-preset-field.md) §4's clause that the count reaches "the
  capture plan's sizing (`build_inline_session_spec`)".
- **Context:** `CapturePlanEntry.duration_ms` bounded nothing at runtime. The wired recorder sizes
  each take's window from the program it plays (`WiredStimulusCapture.around`), and the position
  gate and the join read only an entry's screen. Its value came from a program composed per entry
  that was never played. This is follow-up 7 on [#6226](https://github.com/jaspercurry/JTS/issues/6226);
  the owner approved the edit to `capture_protocol.py`.
- **Decision:** a capture-plan entry states no duration. The field goes, with the per-entry
  compositions that sized it, `CAPTURE_ENTRY_MARGIN_MS`, and the arguments only they used.
- **Consequences:** every played program and its recorder window stay the same. The plan wire loses
  `entries[].duration_ms`; no other process reads it. The sweep spec's own `duration_ms`, read only
  by its validation, now sits at its 30 s floor. A plan whose take cannot compose now meets that at
  the take, not while preparing.
