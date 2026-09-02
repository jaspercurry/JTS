# ADR-0223: A moved reference queue is what K absorbs, not a staleness signal

- **Date:** 2026-09-02
- **Status:** Accepted

## Context

`K = commissioned SYS_DELAY + commissioned queue median`, boot applies
`K - live median`, and both medians come from the same reader. Every outputd
restart re-opens the chip-reference PCM at a different, then stable, fill, so
the live median moves by design. Four jts.local commissioning runs, 2026-09-02:
the median ranged 186-266 while K held within 3 frames (248, 245, 247, 248) and
the chirp-chosen SYS_DELAY moved to match. Arming applied SYS_DELAY -21 with
`AEC_AECCONVERGED=1` and a coherent clock check — yet `runtime_sys_delay`
bounded that delay against the artifact's recorded `sys_delay` by
`MIN_EDGE_MARGIN` and demoted a passing box to `disclosed_stale` with "run
sudo jasper-aec-commission", permanently.

## Decision

Boot has two alignment-validity checks and no third: `K - live median` inside
the chip's declared `CHIP_AEC_SYS_DELAY_MIN..MAX` (refuse, never clamp), and
the identity comparison, with `queue_window_is_stable` still the precondition
for reading a median. The `MIN_EDGE_MARGIN` bound against the commissioned
SYS_DELAY is deleted; `MIN_EDGE_MARGIN` stays as commissioning's own chirp-time
causal margin, and the artifact keeps `sys_delay` for the boot journal. This
supersedes only ADR-0190's premise that "the drift budget depends on that
recorded value" — there is no boot-side drift budget now. Its schema-v2
re-measurement mandate survives on schema grounds: `artifact_from_dict` still
requires `sys_delay` and the current `ARTIFACT_SCHEMA`.

## Consequences

`disclosed_stale` again means something a household can act on. Given up: an
early warning if K itself stopped describing the box — that shows as a
non-converging chip, which the existing runtime observables (converged flag,
clock/SRO check) already report, one level closer to the ear.
