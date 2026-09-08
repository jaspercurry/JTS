# Calibration Agent Corpus

These public acoustic notes support the CLI/Python bundle intake in
[`tools.build_intake`](../tools.py). It adds selected excerpts and source paths
to the redacted context exported by `jasper-calibration-agent`. The live Room
advisor builds a separate context from session data in
[`correction_advisor`](../correction_advisor.py); it does not read this corpus.

Use `jasper-calibration-agent --help` for supported inputs and modes. Its
prompt export contains the response contract. Reading a bundle or exporting a
prompt does not call a provider or change sound.

Start speaker work from the [runbook entry contract](../../../docs/tuning-operator-runbook.md#entry-contract).
The [doctrine](../../../docs/measurement-loop-doctrine.md) owns layers and
authority; the [methodology](../../../docs/tuning-methodology.md) gives optional
science guidance. The [master plan](../../../docs/tuning-master-plan.md) owns
scope. These notes supply acoustic context, not another implementation plan or
set of tool contracts. Private room notes and listening feedback do not belong
in this public corpus.

The source archives retain the original reports and their limits:

- [2026-05-25 acoustic and active-speaker research](../../../docs/research/2026-05-25-calibration-agent/README.md)
- [2026-05-27 browser capture, targets, FIR, and spatial research](../../../docs/research/2026-05-27-room-correction-research/README.md)

When updating a note, prefer primary technical sources. Label vendor behavior,
community experience, marketing claims, and uncertainty separately. Preserve
source links and useful tradeoffs; check current code before describing an
implemented capability. The [intake template](research-intake-template.md)
supports that review.
