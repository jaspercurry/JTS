# ADR-0331: Applied tune reads use the saved artifact

- **Date:** 2026-09-21
- **Status:** Accepted

## Decision

The applied record names the selected candidate artifact and fingerprint.
Its runtime view must retain that reference. Read the exact artifact through
`candidate_bank.load_applied_candidate`, which verifies its contents without
searching or migrating the bank. A missing or invalid artifact is unavailable;
a page read does not reconstruct it from another record.

Carry `BankedCandidate` through configuration and apply-record preparation.
`prepare_applied_baseline_profile` builds metadata from resolved inputs;
`candidate_bank.bank_candidate` owns discovery and publication for new applies.
Saved timing is supplied by the caller that already read the applied record.
Legacy snapshot migration remains in the existing compatibility path, outside
EQ reads and record preparation.

`applied_tune` owns the loaded inputs and their configuration builder. The
input object lasts for one operation. Loading and compilation are separate so
missing inputs can refuse before an apply starts, while compilation failures
retain the apply transaction's structured failure record.

EQ status, draft preview, and save share the carrier's compilation and graph
proof. Validate the same prepared snapshot that the apply path receives.
Durable EQ saves prepare once inside the existing DSP writer lock, then use
the existing load, confirmation, rollback, and persistence path.

## Evidence

On jts3, the EQ data request took 1.36 seconds. Its dry-run configuration build
searched 241 candidate artifacts twice. The applied runtime projection also
dropped the saved artifact path, preventing direct readers from using it.
An exact read makes the work independent of the size of the tune history.
