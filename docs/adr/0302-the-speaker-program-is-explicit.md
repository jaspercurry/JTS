# ADR-0302: The Speaker program is explicit

- **Date:** 2026-09-11
- **Status:** Accepted

## Context

The speaker flow mixed measurement, automatic fit publication, a pre-apply
cloud ritual, adoption, and rollback. This hid the point where the LLM made a
prescription and let an accepted measurement commit alignment and trims as a
side effect.

The owner ratified option (a) on issue #4942. The why and evidence are in the
issue's brief v2.1 §§2.9 and 3 D8–D10 and evidence comments 6, 7, and 11.

Three older records contain stale facts and remain immutable. ADR-0179 counts
five engine seams although four remain. ADR-0198 defers `RecordStore` protocol
slots that have since reduced to `bank`. ADR-0014 names a source-text pin that
no longer exists; the invariant needs a behavioral pin when this design lands.

## Decision

Speaker is a named measurement program. It measures each driver behind the
position gate at the mark and defaults to two repeats. Every capture uses the
same executor, assessor, evidence manifest, and retry owner as other programs.

The program reports measured alignment pairs, drift, driver response, blend
facts, and repeat spread. The LLM prescribes alignment, topology, driver
filters and trims, and blend through the one prescription document. A
measurement does not commit those choices.

The campaign begins with a back-to-back repeatability pair as ADR-0192
requires. The frozen consecutive-pair floor remains the comparison authority;
the program does not compute a new tolerance from the current pair.

Automatic inline candidate publication at measurement acceptance retires. The
pre-apply cloud group and its speculative close machinery retire. Automatic
adoption restore and delta-probe rollback verdicts become advice. The engine's
resource give-back and the apply transaction's structural rollback remain.

## Consequences

The boundary between observation and prescription is visible. The same run
machinery serves Speaker, Room, and Bass, while each program owns only its
question and analysis defaults.

The LLM must make the speaker decision explicitly. This adds one judgment step
but removes hidden commits, an unreachable cloud ritual, and rollback theatre.

## Supersedes / Amends

This ADR follows ADR-0192 for the repeat pair and ADR-0230 as the precedent for
retiring automatic restore. It does not weaken engine give-back, apply
transaction rollback, declared driver caps, or the boost-over-bound stop.
