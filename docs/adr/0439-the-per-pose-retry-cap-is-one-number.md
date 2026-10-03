# ADR-0439: The per-pose retry cap is one number

- **Date:** 2026-10-03
- **Status:** Accepted. Supersedes in part
  [ADR-0422](0422-a-placement-gets-two-extra-takes-and-a-probe-at-its-ceiling-stops.md) §1: its
  sentence "A run's operator share (`retries_per_pose`) defaults to the same two and never raises
  the cap."
- **Context:** `AngleCaptureRequest.retries_per_pose` had no production setter. The door's
  `RunRequest.from_mapping` refuses the key, `resolve_plan` never passes it, and the CLI posts only
  the request keys, so it always held its default, two. Since ADR-0422 the per-placement cap
  already bounds every charge, so the share could never bind.
- **Decision:** a run request carries no per-pose retry share. The field goes, with every parameter
  that only carried it (`request_for_preset`, the executor, `SlotAttempts`, the inline capture
  plan, the door). `MAX_EXTRA_ATTEMPTS_PER_POSITION` in `crossover_v2/admission.py` is the one cap.
- **Consequences:** ADR-0422's rule does not change: a placement gets two extra takes, of every
  charge but a replay, and a repeated refusal still spends it (ADR-0428). The plan document and the
  run manifest's `asked` block no longer carry the key, so each walk's `request_fingerprint` moves
  once.
