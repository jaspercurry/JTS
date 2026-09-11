# ADR-0305: The measurement hold spans a whole run from the mover's join

- **Date:** 2026-09-11
- **Status:** Accepted

## Context

ADR-0285 described its no-cue exception as capped by a 120-second hold TTL.
The redesigned executor can run several poses, candidates, attempts, and level
windows for much longer while renewing the lease. The old stated bound no
longer describes the operator-visible deafness window.

The owner ratified option (a) on issue #4942. The why and evidence are in the
issue's brief v2.1 §§2.1 and 2.3 and evidence comments 2 and 11.


## Decision

One measurement hold spans the whole run, including all poses, candidates,
retries, and fixed-level windows. The hold starts when a mover joins, not when
the run is created, returned, viewed, or polled. Before join, the run owns no
microphone pause or volume resource.

The daemon renews the hold lease while the executor remains live. Release,
cancellation, terminal refusal, or lease expiry restores exactly the persisted
mic-mute state that existed before the hold. A daemon restart adopts a live
hold before the first microphone frame, as ADR-0285 requires.

Wake detection is off for this entire interval. There is no cue and no bounded
wait for a wake. A wake is dropped rather than queued. Run status and doctor
must expose the active hold and its lease clock so the extended interval is
visible.

The exception to the no-silent-deafness rule is bounded by active ownership and
lease renewal, not by one TTL duration. The executor must stop renewing when
it can no longer make progress and must release through ADR-0179's cleanup
shape.

## Consequences

Audio isolation does not open between poses or level windows, and wake audio
cannot contaminate a long experiment. Waiting to hand over the link costs no
hold time and does not silence the speaker.

A legitimate run may be deaf for tens of minutes. The operator initiated that
run, the state is observable, and loss of the executor lets the lease expire.

## Supersedes / Amends

This ADR amends ADR-0285's stated 120-second bound. It restates the rest of
ADR-0285's NN-6 exception: whole-hold wake suppression, no cue, no bounded
wait, exact state restoration, and restart adoption.
