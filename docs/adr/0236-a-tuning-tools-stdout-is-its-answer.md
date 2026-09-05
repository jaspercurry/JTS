# ADR-0236: A tuning tool's stdout is its answer

- **Date:** 2026-09-05
- **Status:** Accepted
- **Context:** The tuning tools are driven by an LLM at an SSH prompt on the
  speaker: it stages a measurement, banks the round, then asks the tools what
  it needs and proposes a config. Its context is the scarce resource. The
  round-grading views (#2769) put their human summary on stderr and left
  stdout empty unless `--out -` was passed, so a caller either got nothing
  machine-readable or got the whole artifact — curves and delay grids
  included. Failures were already the other way round: `_refusal.failed()`
  publishes `{"status","reason","detail"}` on stdout, ungated. So a tool's
  worst outcome was the only one a program could read.
- **Decision:** Every tuning tool answers on stdout.
  1. **Success (exit 0)** prints exactly ONE JSON document, the tool's ANSWER:
     the scalar summary fields it already computes for its human line, plus
     `"out"` and `"bytes"` when it wrote an artifact and `"next"` when it
     knows the next runnable command. No numeric array longer than 16 elements
     — curves and grids stay in the artifact; lists of records bounded by the
     run's own size (seat ids, banked rows) are fine. The one human line still
     goes to stderr. `--out -` is depth on demand: the artifact itself becomes
     that one document, and no answer is printed over it.
  2. **Failure (exit 1/2/3)** prints `failed()`/`refused()`'s document, never
     gated by a flag, with `status == STATUS_BY_CODE[code]`. Everything else
     the outcome carried — receipt fields, `refused_by`, `evidence`, banked
     row ids, where it stopped — goes under `detail`. A run that stopped early
     is a refusal with that detail, not a fourth status. There is no `status`
     key on a success document: `status` is how a failure is recognised.
  3. `--json` flags whose only job was gating that document are deleted.
  4. Exempt: `jasper-declare-geometry`, the human-only sudo config door that
     keeps `OWN_EXIT_VOCABULARY` and prints text; and argparse usage errors,
     which the parser prints and exits on before the tool runs at all.
- **Consequences:** An LLM can run a view, read six fields, and pass the
  artifact path to the next command without ever loading the curve — the
  context cost of asking is bounded by the answer, not by the evidence. The
  cost is that each verb now names its own answer fields, which is one more
  thing to keep true as a view grows; the fields are the ones its human line
  already reports, so the two drift together or not at all. Rejected: a
  `--json` flag (a caller that must ask for the answer gets the empty stream
  by default, which is what this replaces), and printing the whole artifact on
  stdout by default (the delay landscape is a 200-row grid; that is the
  context bill this exists to avoid).
