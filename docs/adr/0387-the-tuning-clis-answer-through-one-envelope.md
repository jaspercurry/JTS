# ADR-0387: The tuning CLIs answer through one envelope

- **Date:** 2026-09-29
- **Status:** Accepted. Supersedes (partial) [ADR-0344](0344-every-round-view-answer-carries-one-envelope.md):
  §1's home and scope, and §4's `ANSWER_SCHEMAS` clause. It clears two items of ADR-0344's known debt: the
  `jasper-round list|show` rows and the integer versions.
- **Context:** An agent reads three tuning CLIs. ADR-0344 gave one envelope only to `jasper-round-views`,
  built in `jasper/cli/round_views/_common.answer`. The other two did not use it:
  - `jasper-round list|show` answered `{verb, …}`, and their rows carried `status`, which ADR-0237 reserves
    for failures;
  - `jasper-crossover-prescriber` printed bare documents, and `contract` printed the contracts themselves.

  The gate-sweep and distortion artifacts also kept an integer version beside `schema`. This is
  [#5928](https://github.com/jaspercurry/JTS/issues/5928) TB3.
- **Decision:**
  1. `jasper/cli/_refusal.envelope` builds every analysis answer of the three tuning CLIs, and
     `_refusal.answer` prints it with its one human line. This covers `jasper-round-views`, `jasper-round
     list|show` and every `jasper-crossover-prescriber` verb (`judge`, `judge --preview [--vary]`,
     `compose`, `contract`, `status`). ADR-0344 §2-§6 hold for all of them, so:
     - the prescriber's `subject` names the round, set and take that the verb resolved, not the flags that
       were typed;
     - `judge --preview` states the window and band that its engine recorded.
  2. `contract` alone prints its envelope in the contracts' own compact serialization, with the bodies under
     `sections`, so the answer stays the size of what it serves. With `--out` it answers with the file,
     `bytes` and `sha256` instead of the bodies.
  3. `ANSWER_SCHEMAS` moves to `jasper/active_speaker/answer_schemas.py`, keyed by command. It holds the
     schema of every answer that no artifact row names, and it imports nothing, so `jasper-round list|show`
     stay light enough for a Pi Zero (ADR-0226). A prescriber file that belongs to an answer carries that
     answer's `schema` (ADR-0344 §4): the `judge --preview --out` file and each `--vary` preview.
  4. No success answer of these tools has a top-level `status`. The `list|show` rows say `result`, which is
     the name the packet and `jasper-round wait` already use.
  5. The gate-sweep and distortion artifacts drop their integer versions, so `schema` alone names their shape:
     `jts_gate_sweep/2` and `jts_harmonic_distortion/4`.
- **Consequences:** An agent reads one answer shape from all three tools and can compare two answers by
  `schema` and `parameters`. A new verb takes the envelope by calling `answer`. The following remain for a
  follow-up:
  - `jasper-round`'s run and receipt verbs (`run`, `trial`, `placed`, `stop`, `status`, `wait`, `apply`,
    `reset`) still answer `{verb, …}` or pass the wizard's payload through, and `status` passes the wizard's
    top-level `status` through;
  - `run_manifest.json` and `provenance.json`'s per-view records keep their `status`.

  Rejected:
  - Keeping `ANSWER_SCHEMAS` in `round_view_artifacts`: `jasper-round list|show` then load SciPy and the
    evidence-packet stack for one string.
  - Moving it into `round_bank`: `round_view_artifacts → round_bank → round_bookkeeping →
    round_view_artifacts` would be an import cycle.
  - Indenting `contract` like every other answer: indentation and escaped dashes made it 1.34 to 1.92 times
    the size of what it serves.
