# ADR-0415: The VERIFY pilot-transfer gate is gone

- **Date:** 2026-10-02
- **Status:** Accepted. Supersedes
  [ADR-0182](0182-the-verify-pilot-transfer-ceiling-rests-on-one-clean-session.md).
- **Context:** ADR-0182 records the VERIFY gate G3. `capture_dispatch.assess` took a
  `pilot_transfer_prior` and refused a VERIFY attempt as `verify_level_shift` when the summed-pilot
  transfer had stepped more than 0.35 dB since the sitting's first usable attempt. So the gate
  compared VERIFY attempts within one sitting. The post-apply verify route and the prior's last
  caller were retired on 2026-09-22 (`7f5b9f6a63`, `795ec1aa12`). Since then nothing passes the gate a
  prior, so it never fires. The same retired call passed `measure_gate_window_ms`, which fed the
  sibling refusal `verify_inconclusive`. That refusal never fires for the same reason. This is the
  deletion pass on [#5925](https://github.com/jaspercurry/JTS/issues/5925) (batch N1).
- **Decision:** No take is refused as `verify_level_shift` or `verify_inconclusive`. The gate (the
  `pilot_transfer_prior` branch of `capture_dispatch.py`), its 0.35 dB ceiling, its helper and its
  `pilot_transfer_step_db` evidence key are deleted. So are the sibling's `measure_gate_window_ms`
  branch and the two registry rows.
- **Consequences:** The evidence that ADR-0182 quotes stays in that ADR: the 0.75 to 0.82 dB step of
  the 2026-07-22 session against the 0.05 dB step of the one clean session. A future recorder-drift
  check starts from it.
