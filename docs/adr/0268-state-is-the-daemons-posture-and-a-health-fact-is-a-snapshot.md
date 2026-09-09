# ADR-0268: `/state` is the daemon's posture, and a health fact is a snapshot

- **Date:** 2026-09-09
- **Status:** Accepted

## Context

[ADR-0233](0233-one-reader-per-fact-two-surfaces-one-doctor.md) rule 2 named
`/state` "jasper-control's in-process posture". A census at HEAD measured the
opposite: 28 top-level keys of which 21 have no programmatic reader, 6
duplicating `/system/snapshot`, 60 file reads per uncached build including the
`/var/lib/jasper-secrets` and `/var/lib/jasper-intsecrets` compartments, and no
nginx route — `/state` had become a second general-purpose file reader. Every
unread fact already has a doctor check reading it from its owning module. The
same census found the shape that works — a module that answers one question
with a status and a closed reason set — used by two readers and copied by none.

## Decision

1. **`/state` carries exactly thirteen keys** and nothing else:
   `schema_version`, `ts`, `voice`, `cues`, `fanin`, `outputd`,
   `source_selection`, `audio`, `audio_health`, `active_source`, `resilience`
   (the three in-process supervisor snapshots only), `measurement`, `debug`.
   That is what only the resident daemon knows: its own memory, its live
   CamillaDSP probe, and the daemon `STATUS` bodies it passes through. Every
   other fact is read by its consumer from the module that owns it (rule 1),
   never from this wire. `STATE_SCHEMA_VERSION` is 4. This supersedes ADR-0233
   rule 2's `/state` sentence; the rest of rule 2 stands.

2. **A health fact is a module in `jasper/` exposing `snapshot()`.** The
   snapshot carries a status, a code drawn from that fact's own closed set of
   at most eight, and a state that distinguishes *unobserved* from *healthy* —
   an unread fact must never render as a well one.
   `jasper/outputd_failure_reconcile_state.py` and
   `jasper/control/transport_park.py` are the models. Checks, the dashboard and
   healers are projections of a fact, never second derivations of it. This
   amends ADR-0233 rule 3: the closed reason vocabulary is the fact's, and the
   doctor's check inherits it rather than minting its own. There is no base
   class and no framework — a module, a `snapshot()`, and a constant set.

3. **The signal-path classifier in `jasper/control/audio_health.py` owns the
   "speaker is silent" verdict.** `SIGNAL_PATH_CODES` is that fact's closed
   set. The doctor's `speaker_silent` projects it instead of classifying
   silence a second time. (The projection lands in a later change; this
   records the ownership.)

## Consequences

Easier: `/state` answers in one pass over in-process memory and four sockets,
opens no secret compartment, and a consumer can pin all thirteen keys. A new
health fact has one obvious home and one vocabulary.

Harder: a fact that wants to reach an operator must be given a module and a
code set first; `/state` is no longer the cheap place to publish one.

Given up: `/state` as a whole-box `curl | jq` dump — `/system/snapshot` and
`jasper-doctor --json` are that. Rejected: a `HealthFact` base class or
registry, which buys nothing a module and a constant set do not.
