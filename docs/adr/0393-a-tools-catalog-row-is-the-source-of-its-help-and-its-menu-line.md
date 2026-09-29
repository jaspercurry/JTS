# ADR-0393: A tool's catalog row is the source of its help and its menu line

- **Date:** 2026-09-29
- **Status:** Accepted. Supersedes (partial) [ADR-0204](0204-per-tool-contracts-live-in-the-tool-the-operator-surface-is-tiered.md)
  §1 and §2: a tool's `--help` and its line in the runbook's menu render from its catalog row, and
  `jasper-round-views catalog` is the discovery verb.
- **Context:** ADR-0204 put each tool's contract in its `--help` and made the runbook's menu an index
  generated from the CLIs' own metadata. That metadata described a CLI, not a tool:
  - the menu gave all 22 `jasper-round-views` views one cell and one description, and no view had a
    description of its own;
  - which program a view serves lived in `VIEW_PURPOSES`, beside the artifact table;
  - nothing said what a tool answers, what takes it needs, or whether it decodes a recording.

  So an agent had no one place to find the tool that answers its question, while the owner's rule is
  that it never has to write its own analysis code. This is [#5928](https://github.com/jaspercurry/JTS/issues/5928) TB4.
- **Decision:**
  1. Every tool an agent can call has one catalog row: `CatalogRow` in
     `jasper/active_speaker/round_view_artifacts.py`, which grows the old `ViewArtifact`. `CATALOG` holds,
     keyed by the command that runs each:
     - the `jasper-round-views` views;
     - `jasper-crossover-prescriber judge --preview [--vary]`, `contract` and `status`;
     - `jasper-round list`, `show` and `presets`;
     - the laptop tools (`dsp-replay`, `dsp-levels` and `scripts/cabinet-model/*.py`; ADR-0353).
  2. A row states:
     - `question`: one line, what the tool answers;
     - `needs`: the pose, regime and take kind it reads;
     - `reads`: `record` (the round's banked documents), `recording` (it decodes a take's WAV) or
       `laptop` (it runs on files that stay on the laptop);
     - `programs`: the purposes of the rounds it reads, none for every program (`VIEW_PURPOSES` folds in);
     - `argv`: its arguments after the command;
     - `schema`, `artifact` and `answer_fields`: its answer's shape, the file it writes, and the fields
       its answer carries beside the ADR-0387 envelope.
  3. A row reads each fact from its one owner: a view's artifact and schema are its artifact row's, and
     another tool's schema is its `ANSWER_SCHEMAS` row. `ANSWER_SCHEMAS` stays an import-free table,
     so `jasper-round list|show` stay light (ADR-0387 §3).
  4. The rows make the runbook's menu (amends §2). The generator renders one line per row: the call, the
     question, the needs, what it reads and the programs. The CLI table beside it names only the verbs
     that no row covers.
  5. The rows make each view's `--help` (amends §1): the row's question, when not to use the view, one
     example from its `argv`, and its exit codes from `jasper/cli/_refusal.py`. Each outcome has one code:
     1 when the view read its input and cannot grade it (it refuses by name), 2 when it cannot read its
     input, malformed input included, and 3 when it cannot write its artifact.
  6. `jasper-round-views catalog [--program P]` is the discovery verb. It reads no round, writes nothing,
     and answers through `_refusal.answer` (ADR-0387) under `jts_tool_catalog/1`:
     - `parameters` is `{program}`, and `subject` is empty;
     - `tools` has one entry per row: `tool`, `question`, `needs`, `reads`, `programs` (every program
       named when the row names none), `argv` (the command's words, then the row's), `schema`,
       `artifact` and `answer_fields`;
     - with `--program P` it lists the rows whose programs meet the purposes of P's presets, so `rear`
       also lists the room views that read a `rear/seat` round.
- **Consequences:** An agent asks one verb what it can ask, and a new tool adds one row (#5928 TB9-TB11)
  that its help and the menu follow. Tests pin that every row is complete, every row's `argv` parses,
  every view has a row, and every field an answer carries is one its row names. §5 lands in #5928 TB4b;
  until then a view's `--help` takes only its program tag from the rows.

  `reads` states what a tool reads today. A view that still decodes a WAV says `recording` until #5928
  TB7b moves it to the record. `jasper-round-views catalog` loads the view stack, as every
  `jasper-round-views` verb does: the package imports its view families at start.

  Rejected:
  - Folding `ANSWER_SCHEMAS` into the rows: the rows live beside the artifact table, which loads NumPy
    and SciPy, so `jasper-round list|show` would load them for one string (ADR-0387's reason).
  - A second, light module for the rows: it would restate artifact names and schemas that other modules
    own.
  - A row for every verb: `run`, `trial`, `apply`, `judge`, `compose` and the other action verbs do not
    answer an analysis question, and `contract` says what a document may write.
  - A row for `scripts/fit-rear-branches.py`: #5928 TB10 replaces it with a view and deletes it.
  - `catalog <round>`: #5928 TB5, where it replaces `inventory`, `index.md`'s commands and
    `status.next_commands`.
