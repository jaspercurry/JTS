# ADR-0196: The commissioning record's read path takes no lock, and says what it found

- **Date:** 2026-08-30
- **Status:** Accepted

## Context

On JTS3, with a passing measured tune live, `/state` reported
`acoustic_commissioning.reason = active_commissioning_receipt_malformed` and
the household copy "the commissioning proof on disk could not be read... re-run
commissioning to replace it." The named record,
`/var/lib/jasper/active_speaker_commissioning_run.json`, was valid JSON at
`lifecycle_state: "unconfigured"`.

The record was never parsed, because it was never opened.
`CommissioningRunStore.snapshot()` took the advisory lock before reading, and
`_locked()` opens the sibling lock file `"a+"` — read plus WRITE — and `chmod`s
it. On that box:

```
-rw-r----- 1 root jasper  659 active_speaker_commissioning_run.json
-rw-r----- 1 root root      0 .active_speaker_commissioning_run.json.lock
```

The lock was created root-owned by a root-run status poll — `jasper-doctor`
reaches this same reader — after which every non-root reader
(`jasper-control`, `jasper-correction-web`) got `PermissionError` on the open.
The reader caught `OSError` in the same arm as its strict-schema rejects and
answered MALFORMED. **The diagnostic manufactured the fault it then reported**,
27,548 WARNING lines in six hours, each one telling the household its evidence
was damaged.

Two separate defects: a lock on a path that does not need one, and a
classification that reports "we could not read it" as "it is not valid".

## Decision

1. **`snapshot()` takes no lock.** `_write` publishes through
   `atomic_write_text`/`os.replace`, so a reader sees the whole previous record
   or the whole new one — the exclusive lock bought a pure read nothing while
   making it need write access to a sibling file, and CREATE that file when
   absent. A missing record is answered by the read's own `FileNotFoundError`
   arm rather than by an `exists()` probe, so an untraversable directory raises
   instead of reading as "no run".
2. **Denials are classified from structured types and codes, never by sniffing
   an exception chain.** The evidence store's own
   `CommissioningEvidenceStoreErrorCode` decides: `MISSING` is ABSENT, its
   tamper and integrity codes are the content class (a substituted artifact is
   a record defect whose remedy is a fresh mint, whatever errno produced it),
   anything else with an OS fault beneath it is the machine class.
   `CommissioningRunConflict` is STALE, and a lock timeout is a read that could
   not be completed, not damaged bytes.
3. **A fifth class, `active_commissioning_receipt_unreadable`, for a record
   JTS could not open or read.** Its copy stays modest — a machine-level fault,
   not a verdict on the record — because the class covers permissions, IO
   errors and a path that is not a file. MALFORMED's copy moves to content
   language so the two do not describe the same thing with opposite remedies.
4. **The structured cause rides the answer, not just the log.** The reason
   names the class; `cause` names the fault (a store code, or an exception
   class with errno and path). A class name alone cannot say WHICH file, which
   is the sentence that would have ended this incident from `/state`.
5. **The disclosure logs on transition.** A denial is the steady state for most
   speakers and this reader is polled by several clients; the WARNING fires
   when the (reason, cause) pair changes and the repeats are debug.

6. **The locks are group-WRITABLE and only re-chmodded when they differ.**
   Taking an advisory lock opens the file for write, so `0o640` let a group
   member read a lock it could never take; and an unconditional `chmod` on a
   lock this process does not own raises `EPERM` even when the open succeeds.
   Existing boxes are healed at install the way the sibling
   `/var/lib/camilladsp/configs/.dsp_apply.lock` already is — the record itself
   stays group-read, published that way by its own atomic writer.

Nothing here refuses anything: all five denials remain disclosures (ruling
S10), naming what may not be CLAIMED.

## Consequences

Easier: the reported box works — a root-created lock can no longer deny a
reader, and a machine fault no longer reads as damaged evidence. The
`/state` payload now carries the store code or errno that caused the denial,
so the next incident is diagnosable without SSH.

Harder: a lock-free read gives up cross-FILE consistency for the two reads that
still lock (`lifecycle_transition`, `current_live_mutation` — each still takes
the lock and is unchanged here). A racing writer can therefore produce a
mismatched pair, which the receipt comparison answers as STALE — a denial, and
denials never gate.

Prior art: `dsp_apply._proof_failure` already draws this line for DSP candidate
proofs ("an unreadable candidate is an operator-actionable I/O or permissions
fault on a file whose bytes may be perfectly intact"). This ADR applies the
same rule to commissioning evidence.

Naming collision worth knowing: `commissioning_run._parse_json` raises
"commissioning run state is unreadable" for a JSON parse failure, which is the
opposite of what `..._receipt_unreadable` means here. That message is internal
and untouched.

Not done here, and named so nobody assumes it was: the two lock sites still
hand-roll what `atomic_io.advisory_file_lock` does (create-time mode, group
from parent, guarded chmod), which two siblings in this package already
consume. Converging them means rewriting the store's bespoke
deadline/thread-lock tests — the mode and the guard, which are what the
incident turned on, are fixed here; the shape is its own change.
