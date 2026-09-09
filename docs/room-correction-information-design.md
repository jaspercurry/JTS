# Room correction: product and architecture reference

This document explains the Room page and its separate acoustic purpose. For
speaker experiments, start with the [tuning runbook](tuning-operator-runbook.md).
The [measurement doctrine](measurement-loop-doctrine.md) owns layer and
authority rules; code owns current fields, defaults, and limits.

Room retains live browser microphone capture at `https://<speaker>/sound/room/`.
The Pi plays the stimulus and the local browser records and uploads the capture.
HTTPS and trust in the speaker's local CA are required for microphone access.
This is distinct from crossover's wired Pi microphone. The phone relay was
retired; there is no relay-first Room path or second relay handoff to configure.
See [ADR-0222](adr/0222-the-relay-is-deleted-the-wired-microphone-is-the-only-capture-path.md).

## Product goal

Reduce repeatable room deviation across the listening area, with disclosed
choices and a measured before/after result. Room uses reverberant measurements:
removing reflections would remove the phenomenon it is meant to correct.
Driver EQ, crossover alignment, and driver protection remain speaker concerns.
Preference EQ expresses taste in its own layer.

Room has a guided measurement and acceptance loop. That product policy does
not turn the separate crossover toolbox into a compulsory campaign. The
existing Room adviser may explain evidence or propose a bounded tweak; it does
not own the deterministic acceptance verdict.

## Product flow

1. Open Room, inspect the speaker-readiness disclosure, and review the displayed
   position count, target, and strategy.
2. Select the browser microphone and calibration as needed. Check level and
   measure at the guided listening positions, including the main-seat repeat.
3. Review the measured correction and apply it explicitly.
4. Return to the main seat for a fresh verification capture. Read the measured
   result and any request to confirm a regression before restoration.

`jasper.correction.session` owns the default six positions, supported counts,
and main-seat repeat. `jasper.correction.strategy` owns the flat target,
balanced strategy, and household choices. The page renders those values rather
than keeping a second policy table. The repeat is a trust check at the same seat,
not another distinct listening position.

## Screen and whole-page visibility contract

[jasper.correction.envelope](../jasper/correction/envelope.py) owns the ordered
sections, user-facing state, result copy, and next action. The browser renders
that contract and owns microphone mechanics. It does not recompute smoothing,
confidence, acceptance, or the flow from its own state table. An unknown envelope
version cannot justify an invented forward action.

Keep one primary forward action on a screen. Stop, cancel, and restore remain
available where relevant. Current correction and recovery state must stay clear
while a capture or result is being shown. Reports carry detailed confidence,
spatial spread, runtime, and filter evidence without becoming another primary
verdict panel.

`POST /upload-capture` acknowledges the mechanism. The browser refreshes the
envelope for result copy, actions, and curves after the session changes. The
server supplies display-smoothed curves and helped/hurt segments; drawing them
does not give the browser authority to calculate a second verdict.

## Readiness, limits, and failures

Room consumes the speaker-owned readiness decision and carries its identity
through measurement and application. It does not reconstruct speaker authority
from historical captures or treat a saved manual value as measured proof.

Unproven or stale speaker readiness is disclosed; it does not by itself prevent
Start. The [current start handler](../jasper/web/correction_handlers.py) records
that limited authority and continues. This is different from an active-session
conflict, unusable browser capture, mismatched required calibration, or a real
graph/protection failure. Those conditions can prevent the requested operation.
The old rule requiring a fixed number of automatic crossover repeats before
Room could start no longer describes the product.

Measurement quality changes what a result can claim. Noise, position spread,
limited microphone response, and a weak repeat can warrant another measurement;
they must not silently become new physical-protection gates. Calibration must
remain bound to the microphone actually used. Changing gain does not repair
acoustic signal-to-noise ratio when it raises noise and signal equally.

A requested restore and a completed restore are separate states. Keep capture
results beside any cleanup failure, show the pending recovery action, and never
present an intended rollback as success. These are the result contract; the
existence of this document is not evidence that every interruption path has
passed a hardware check.

## Capture and returning-user state

The browser obtains microphone permission, captures local audio, and sends it
to the Room handlers. The Pi owns stimulus, level, position sequence, analysis,
and product decisions. Device and calibration identity must remain visible
through measurement and verification; calibration does not synchronize clocks.

[household_mic.py](../jasper/audio_measurement/household_mic.py) retains the last
successfully established microphone/calibration for local setup. The former
work plan's `preferences.json` store for remembered position, target, and
strategy choices is not implemented. Do not describe it as a shipped file,
install requirement, or returning-user guarantee. Browser local storage is not
an authority for the applied DSP graph.

A fresh page or run must distinguish saved correction state from unfinished
measurement state. It must not invent continuation or overwrite a predecessor
merely because a new session object was created.

## Target, filter, headroom, phase, and latency policy

The normal surface offers the Room-owned named targets and household strategies.
Room v1 designs IIR parametric EQ. The default is balanced and cuts-only;
`safe` is also available. The expert `assertive` strategy exists in Python but
is excluded from household choices. Its presence in the registry does not mean
the browser offers it. Read the strategy owner for filter count, band, Q, cut,
and boost bounds rather than copying those constants here.

Room removes repeatable room deviation; Sound owns subjective preference.
Existing named warmth targets remain part of Room's vocabulary. That overlap is
not proof that their vocabularies have been unified. The separate
[regime proposal](room-correction-regime-plan.md) does not establish a shipped
per-room transition, residual upper tier, or spatially admitted LF boost path.

A deep position-dependent null is poor evidence for boost. Extra drive can cost
headroom without repairing the cancellation. The current designer excludes
boost near the bass-management crossover; a summed deficit there does not
identify a room mode. Positive filter gain, when a strategy allows it, needs
composed headroom accounting. A lower maximum output level is a different cost
from ordinary listening volume.

Room does not design FIR or phase correction. A room error that changes with
seat cannot be repaired across the room by a single inverse filter. Long FIR
filters can also add latency that conflicts with speaker duties. Imported FIR
metadata may be shown only when present in the applied artifact, with its actual
phase mode and group delay. Missing metadata is unknown. The existing latency
eligibility contract continues to govern low-latency paths.

## Proof and acceptance authority

[AcceptanceEvaluator](../jasper/correction/acceptance.py) owns the Room verdict.
It compares the established main-seat basis with a fresh verification capture,
using repeatability and band-level change. A measured regression calls for a
confirmatory capture before automatic revert. An applied graph alone is not
acoustic verification, and an insignificant change is not a proved improvement.

The result leads with the verdict, helped/hurt spread, confidence limits, current
correction state, and next action. The optional LLM can interpret the evidence;
it cannot replace or hide the verdict. The browser can format server-owned text;
it cannot substitute a different acceptance calculation.

Restoration must report the actual outcome. If rollback failed, do not claim
that the previous sound was restored. Retain the relevant evidence and expose
recovery. Detailed design and runtime facts remain in reports and session
artifacts rather than a parallel set of browser rules.

## Architecture and ownership

Room session and envelope policy stay under `jasper.correction`; HTTP admission
and capture upload stay in the Room web handlers. Shared sweep, calibration,
deconvolution, and quality calculations live under `jasper.audio_measurement`.
Leaf analysis receives narrow data and callbacks, not a web handler or a live
DSP controller. See [extensibility.md](extensibility.md).

Room's measurement graph removes Room and preference layers while preserving
the speaker graph. Apply and restoration use the shared DSP writer boundary.
A runtime filename is provenance, not immutable rollback content; restoring a
predecessor requires the correct graph snapshot and must respect a later legal
writer. Room does not repair crossover state or issue speaker authority.

Room's listening-area average and main-seat repeat remain different from
Active's same-pose repetitions. Shared numerical primitives do not imply shared
product sessions, screen tables, or a revived commissioning host. No new generic
wizard framework is required to keep those boundaries.

## Durable evidence and observability

Keep the established session/bundle records and their readers. A refactor must
preserve useful old data; a historical field does not prove a retired transport
still runs. Logs and result artifacts should identify capture, application, and
cleanup outcomes without exposing credentials or raw secrets.

Reports distinguish measured response, filter design, applied state, and verified
change. Keep omitted data and unavailable confidence visible. A UI result or a
hardware-free test proves only the boundary it exercised; acoustic performance
and physical recovery need evidence from the actual speaker.

## Language guide

Use direct action labels: allow the microphone, move it, measure, compare, apply,
or restore. Explain one concrete problem and the next useful action. Keep
internal transport and graph names in details unless they help the operator
resolve that problem. Avoid a copied list of error prose: the server owns it.
