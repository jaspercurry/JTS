# ADR-0389: The jasper-round action verbs answer through the envelope

- **Date:** 2026-09-29
- **Status:** Accepted. Supersedes (partial) [ADR-0387](0387-the-tuning-clis-answer-through-one-envelope.md)
  §1's list of verbs, which now includes every `jasper-round` verb, and clears the debt its Consequences
  name for those verbs. Supersedes (partial) [ADR-0344](0344-every-round-view-answer-carries-one-envelope.md)
  §3 for the `jasper-round` action verbs.
- **Context:** ADR-0387 left `jasper-round`'s action verbs outside the envelope, as named debt: `run`,
  `trial`, `placed`, `stop`, `status`, `wait`, `apply` and `reset`. They answered `{verb, …}` or passed the
  wizard's payload through, including its top-level `status`. A `run|trial --dry-run` that would block
  printed a success-shaped document and exited 1. This is
  [#5928](https://github.com/jaspercurry/JTS/issues/5928) TB3b.
- **Decision:**
  1. Every `jasper-round` verb answers through `_refusal.answer`. Each answer has one `ANSWER_SCHEMAS` row:
     `jts_round_run/1`, `jts_round_preflight/1` (`run|trial --dry-run`), `jts_round_placement/1`,
     `jts_round_stop/1`, `jts_round_status/1`, `jts_round_wait/1`, `jts_round_apply/1` and
     `jts_round_reset/1`. `view` is the verb that was typed:
     - `trial` answers under `run`'s rows;
     - `run|trial --wait` answers `jts_round_wait/1`.

     `verb` is gone, and each verb's own fields stay top-level.
  2. `subject` names only what applies (ADR-0344 §2):
     - `run`, `trial` and their dry run: `candidate_ids` when the run plays candidates, otherwise nothing,
       because a staged run has no round yet;
     - `wait` and `run|trial --wait`: the `round_id` it banked;
     - `apply` and `reset`: the `candidate_id` they applied;
     - `placed`, `stop` and `status`: nothing.
  3. These verbs grade nothing, so ADR-0344 §3's analysis parameters do not fit them. Their `parameters`
     are the run's resolved plan and flag values:
     - `run`, `trial` and their dry run state the plan's `program`, `layout`, `mover`, `level_db` and
       `levels`. `levels` is the ladder's admitted levels, and null for a one-level run.
     - They state `repeats` (takes per pose and configuration) and `driver` (an output a stop plays alone) as
       the stops play them. Stops that differ give a sorted list of the distinct values. `driver` is null
       when no stop plays one output alone.
     - So a `--plan` or `--poses` run states what its stops play, whatever flags were typed.
     - `placed` states `pose`, and `reset` states `program` and `keep_timing`. The other verbs state none.
  4. A `run|trial --dry-run` that would block is a refusal record, exit 1 (ADR-0237 §2):
     - `reason` and `code` are the first blocking issue's code, and the record carries its `next_action`;
     - `detail` holds the refused run's `subject` and `parameters` beside the preflight report;
     - its stderr line is the issue's sentence.

     A run that is not a dry run refuses the same way on the arm facts that only the CLI can see.
  5. `jasper-round status` calls the wizard's capture state `state`, because `status` names a failure
     (ADR-0237). The wizard's own payload keeps `status`.
  6. No fact appears twice:
     - on `apply` and `reset`, `subject.candidate_id` replaces `candidate_fingerprint`;
     - on a staged run, `subject.candidate_ids` replaces `shape`;
     - on a dry run, `jts_round_preflight/1` replaces `dry_run: true`.
- **Consequences:** Every success answer of the three tuning CLIs carries `view`, `schema`, `subject` and
  `parameters`, and none has a top-level `status`. A run's parameters read the same whether the run was
  typed as flags, a `--poses` list or a `--plan` document. A reader must allow a parameter to be a list
  where the plan's stops differ. `run_manifest.json` and `provenance.json`'s per-view records keep their
  `status`, as ADR-0387 left them.

  Rejected:
  - `repeats` and `driver` from their flags alone: a `--plan` or `--poses` run stated null whatever its
    stops played, and a preset's own repeats went unstated.
  - Null where the stops differ: two different mixed plans would compare equal.
  - The whole preflight report as the refusal's stderr line: about 6 KB of JSON on the stream meant for
    one sentence.
