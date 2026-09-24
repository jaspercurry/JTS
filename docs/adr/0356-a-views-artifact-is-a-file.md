# ADR-0356: A view's artifact is a file

- **Date:** 2026-09-24
- **Status:** Accepted (supersedes (partial) ADR-0237: its `--out -` sentence)
- **Context:** ADR-0237 made `--out -` depth on demand: the artifact printed
  as the one stdout document. The round views refuse `-` at parse time
  (`jasper/cli/_report.output_path`), so an agent that follows ADR-0237 gets
  an argparse error. Every answer already names its artifact under `out`
  (ADR-0237, ADR-0344).
- **Decision:** A view always writes its artifact to a file: beside the round,
  or at the path `--out` names. `--out -` is refused. Depth on demand is
  reading the file the answer's `out` names. The rest of ADR-0237 stands.
- **Consequences:** Each view has one output path. A caller that wants the
  curves reads the file (`jq` on `out`), and the answer on stdout stays small.
  Rejected: restoring `--out -`, a second output path in every view for what
  one file read already gives.
