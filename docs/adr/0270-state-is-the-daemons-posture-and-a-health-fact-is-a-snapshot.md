# ADR-0270: `/state` is the daemon's posture, and a health fact is a snapshot

- **Date:** 2026-09-09
- **Status:** Accepted

## Context

[ADR-0233](0233-one-reader-per-fact-two-surfaces-one-doctor.md) rule 2 named
`/state` "jasper-control's in-process posture". A census at HEAD measured the
opposite: 28 top-level keys, 6 duplicating `/system/snapshot` and 21 with no
reader anywhere but a human `curl | jq` (each already has a doctor check
reading the same fact from its owning module), and 60 file reads per
uncached build including the `/var/lib/jasper-secrets` and
`/var/lib/jasper-intsecrets` compartments, with no nginx route — `/state`
had become a second general-purpose file reader. The same census found the
shape that works — a module answering one question with a status and a
closed reason set — used by two readers and copied by none.

## Decision

1. **`/state` carries exactly thirteen keys** and nothing else:
   `schema_version`, `ts`, `voice`, `cues`, `fanin`, `outputd`,
   `source_selection`, `audio`, `audio_health`, `active_source`, `resilience`
   (the three in-process supervisor snapshots only), `measurement`, `debug`.
   The test is what only the resident daemon knows, not "has a reader" —
   `voice`, `cues` and `resilience` are *also* read straight from their
   owning modules by the doctor, duplicated here only for the dashboard;
   the rest have no reader beyond this wire, and every deleted key is read
   by its consumer from the module that owns it instead (rule 1). Deleting
   `voice.model` ended `/state`'s own call into `merged_env_files()` (and so
   the secret compartments), not the function itself — the doctor's voice
   check, `speech_stimulus.py`, `research/state.py` and `aec_commission.py`
   still call it directly. `STATE_SCHEMA_VERSION` is 4; this supersedes
   ADR-0233 rule 2's `/state` sentence, the rest stands.

2. **A health fact is a module in `jasper/` exposing `snapshot()`** carrying a
   status, a code from that fact's own closed set (at most eight), and a
   state distinguishing *unobserved* from *healthy* — an unread fact must
   never render as a well one. `jasper/outputd_failure_reconcile_state.py`
   and `jasper/control/transport_park.py` are the models; checks, the
   dashboard and healers project the fact, never re-derive it. Amends
   ADR-0233 rule 3: the reason vocabulary is the fact's own, and the
   doctor's check inherits it. No base class, no framework — a module, a
   `snapshot()`, a constant set.

3. **`jasper/control/audio_health.py`'s signal-path classifier owns the
   "speaker is silent" verdict**, via its closed `SIGNAL_PATH_CODES`; the
   doctor's `speaker_silent` projects it, never reclassifies it.

## Consequences

Easier: `/state` answers in one pass over in-process memory and four sockets,
opens no secret compartment, and a consumer can pin all thirteen keys.

Harder: a fact that wants to reach an operator needs a module and a code
set first; `/state` is no longer the cheap place to publish one.

Given up: `/state` as a whole-box `curl | jq` dump — `/system/snapshot` and
`jasper-doctor --json` are that now. Rejected: a `HealthFact` base class or
registry, buying nothing a module and a constant set do not.
