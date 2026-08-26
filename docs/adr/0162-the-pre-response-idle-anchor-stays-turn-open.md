# ADR-0162: The pre-response idle anchor stays turn-open; a new endpointer derives its own bound instead

- **Date:** 2026-08-26
- **Status:** Accepted

## Context

One protocol-agnostic watchdog guards every voice turn on every provider. It
reads `turn.last_activity_at()` and abandons the turn when the model has been
silent for `JASPER_IDLE_TIMEOUT_SEC` (20 s in production) with no audio yet.

`last_activity_at()` tracks *model* activity, and returns the turn-start time
until the model first speaks — which it cannot do until `end_input()`. So the
anchor is effectively **turn-open**: the user's entire input phase and the
model's first-response latency share one budget.

Two consequences fall out, and both are easy to misread as bugs:

- `HARD_RECORDING_CAP_SEC` (30 s) sits *above* the 20 s watchdog and
  therefore cannot fire on a stock box, for any endpointer. A very long
  single utterance loses its answer to the watchdog first, and because
  `_end_turn` cancels playback *before* calling `end_input()`, the user hears
  nothing rather than a short reply.
- A push-to-talk hold is exactly the case where input legitimately runs long,
  so it cannot rely on a constant that never fires.

The clean fix is to re-anchor the pre-response timer on end-of-input, which
would make the recording cap meaningful again for every endpointer. That is a
shared-machinery change across all three adapters, touching the one timer
every provider depends on.

## Decision

**The anchor stays turn-open. Push-to-talk derives its own bound from the
same `idle_timeout_sec` the watchdog uses** — `WakeLoop._ptt_input_cap_sec`,
reserving `PTT_MODEL_FIRST_RESPONSE_ALLOWANCE_SEC` for the model to start
speaking — and logs `event=manual_mic.hold_cap` when it fires. This is a
local bound, explicitly not a fix for the anchor.

Because `PTT_MIN_INPUT_CAP_SEC` is a constant while the watchdog is not, a
low enough `JASPER_IDLE_TIMEOUT_SEC` walks the watchdog down through the
floor. Two degraded bands are detected and warned once per daemon, each
naming the timeout that clears it in `needs_sec=`:

| `JASPER_IDLE_TIMEOUT_SEC` | what happens | event |
|---|---|---|
| ≥ 11 (incl. the shipped 20) | cap fires with the full allowance | — |
| 6–10 | cap fires first, but the model gets less than the allowance, so a **slow** first chunk loses the answer | `manual_mic.idle_timeout_too_low` |
| ≤ 5 | watchdog fires first; the cap is unreachable and **every** long hold loses its answer | `manual_mic.hold_cap_unreachable` |

The floor stands in both degraded bands: a usable button beats one that
closes mid-sentence, and the remedy is the operator's timeout, not a shorter
cap.

## Consequences

- The button gets a correct bound without a shared-timer change, and the
  degraded configurations announce themselves instead of silently eating
  answers.
- `HARD_RECORDING_CAP_SEC` remains dead on a stock box. Anyone reading it as
  the thing that bounds a long utterance is wrong, which is why this ADR
  exists.
- The cap only bounds a button whose frames keep arriving — it is evaluated
  per frame, and a manual source's `frames()` is an untimed queue read, so a
  transport drop mid-hold is reaped by the watchdog instead
  (journal: `HOLD TIMEOUT`). The cap is easy to mistake for the net that
  catches that case; it is not.
- Deliberately deferred: re-anchoring on end-of-input. When it happens it
  supersedes this ADR and the local push-to-talk derivation should collapse
  into it.
