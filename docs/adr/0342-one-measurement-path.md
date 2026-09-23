# ADR-0342: One measurement path; `jasper-measure` is retired

- **Date:** 2026-09-22
- **Status:** Accepted
- **Supersedes:** The ADR-0296 paragraph that let `jasper-measure --specs` host
  the executor loop in-process for one release.

## Context

The owner asked for one high-quality measurement path, not two (2026-09-22).
`jasper-measure` was a second front end on the executor: its own measurement
door and mux owner, its own spec loop (`plan_run.run_specs`), and its own
excitation composer. That composer set driver gains from a blind −12 dBFS
peak, where production solves them from the CHECK pilot
(`compose_plan_program`). It ran as root with no placement gate. No runbook
step, playbook, web page, script or LLM tool called it.

ADR-0296 kept it for one release as the no-daemon diagnostic path. Its removal
condition was that the daemon path can serve that diagnostic use.

## Decision

`jasper-round run|trial` is the only way to measure. It posts the plan, and
`plan_run.run_plan` walks it with preflight SPL prediction, candidate proving
and the placement gate. The process that hosts the executor is
`jasper-correction-web` (`pyproject.toml`), not the voice daemon ADR-0296
names.

The removal condition is met: `--poses` plus `--candidates` serves the
diagnostic use. Comparing configurations at one spot is
`jasper-round run --poses 0 --candidates base,A,B --mover confirmed --wait`,
and a candidate already carries its delay and polarity. Measuring without the
daemon is retired. `jasper-measure`, its spec loop and its mux owner are
deleted.

## Consequences

One executor owns gain, retries, placement and evidence, so a diagnostic take
is the same kind of take as a tuning take. What goes away is a batch of raw
polarity, delay or level-match overlays in one run: bank each variant as a
candidate and compare the candidates.
