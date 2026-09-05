# ADR-0188: Wired-first measurement; relay parked

- **Date:** 2026-08-28
- **Status:** Accepted. Sections 2 and 3 are superseded by
  [ADR-0222](0222-the-relay-is-deleted-the-wired-microphone-is-the-only-capture-path.md).

## Context

The tuning-refactor program has carried two acoustic-capture lanes side by
side: a USB measurement microphone plugged directly into the Pi (the wired
source, `correction_crossover_v2_wired.py`), and the phone-mic relay
(`capture-page/`, `relay/`, `jasper/capture_relay/`, plus the relay wizard
provider `correction_crossover_v2_relay.py`). S11's own act 6 scope names the
relay as the instrument — *"One speaker (jts3), one phone through the capture
relay, one region"* (`REFACTOR-TUNING-2026-08.md:1287`) — and act 4, the
#2202 scoping hour, was run the same way. Meanwhile the wired source cannot
yet be driven solo from the browser: on a hand-walked round the position
gate still holds every capture (there is no capture page to tap), and
nothing shipped posts the release except the `jasper-arm-walk` CLI
(`tuning-operator-runbook.md:311-333`, tracked as
[#2881](https://github.com/jaspercurry/JTS/issues/2881)). The live engine
(`crossover_v2_flow.py`, `correction_crossover_v2.py`) also imports the
relay's protocol types (`CaptureBeginDeferred`, `CaptureBeginRefused`)
directly, and the setup wizard still defaults a fresh-install Room to the
relay when configured (`correction_setup.py:4454`). Two lanes, neither fully
self-serve, and the plan's own sanctioned hardware acts split across both.
The owner picked one lane as canonical and set terms for the other.

## Decision

Owner ruling, 2026-08-28, in four parts.

**1. Wired-first.** A USB measurement microphone plugged into the Pi is *the*
acoustic-measurement path — the single source of acoustic measurements. AEC
mics are never acoustic-measurement instruments. The human must be able to
complete a full gated measurement round at the `jts.local` web interface
alone, with nobody driving a CLI on the side — which means the missing
position-release UI gets built. That is the #2881 gap, and closing it is now
required for the wired lane to stand on its own rather than a nice-to-have.

**2. Relay parked, "marooned on purpose."** The phone-mic relay path
(`capture-page/`, `relay/`, `jasper/capture_relay/`, and the relay wizard
provider) is **not deleted.** It is set aside as a modular island:

- The live measurement path must not import from `jasper/capture_relay`.
  The shared protocol vocabulary it currently pulls in directly
  (`CaptureBeginDeferred`, `CaptureBeginRefused`) moves to a neutral home —
  a separate PR does this; this ADR only records that it is now required.
- The wizard's fresh-install default flips from relay to wired — a separate
  PR.
- Relay's reason to exist going forward is **moving-mic room correction**, a
  future capability, not today's crossover measurement.
- Untouched by this ruling: the stereo-sync wizard (`jasper/web/sync_flow.py`)
  and v1 room correction (`jasper/correction/household_mic.py`) keep calling
  into `jasper.capture_relay` exactly as they do today.

**3. S11 acts 4 and 6 are deferred with the park.** Both were scoped against
the relay (`REFACTOR-TUNING-2026-08.md:1287`): act 4, the #2202 one-hour
scoping, and act 6, the commissioning-producer proof. Neither is cancelled or
failed — both leave the active sanctioned-act list and return when the relay
lane revives for moving-mic work. Acts 1, 2, 3, and 5 are unchanged by this
ruling.

**4. Scope notes riding with this ruling.**

- The arm/turntable (the USB turntable with the attached mic arm that orbits
  the speaker) stays live and supported. The owner uses it for remote smoke
  tests; an LLM may drive it via `jasper-arm-walk`. It stays invisible in the
  user-facing web UI, compartmentalized to CLI/SSH — most users will never
  own one, and this ruling does not change that.
- [ADR-0018](0018-bass-extension-stays-parked.md)'s `bass_extension` park
  stands, unaffected.
- The engine's three verbs split by interface, by design: `measure` gets the
  web interface; `analyze` and `recommend` are LLM-over-SSH surfaces, with no
  web UI planned for either.

## Consequences

- The wired lane's remaining gap (#2881) becomes load-bearing rather than a
  nice-to-have: until the position-release UI ships, a human cannot yet run
  a full hand-walked round at the web interface without `jasper-arm-walk` or
  another CLI posting the release. That gap is now the one thing standing
  between "wired is the measurement path" as a ruling and as a delivered
  experience.
- `jasper/capture_relay` keeps its current shape and its two live callers
  (the stereo-sync wizard, v1 room correction) instead of shrinking toward
  deletion. The deletion mandate gains an explicit, named exception here, the
  same way ADR-0018 exempted `bass_extension`.
- The engine importing relay session types directly is now a named debt
  instead of an invisible coupling — tracked for the follow-up PR that moves
  the shared vocabulary to a neutral home before the relay import can be
  removed.
- S11's sanctioned-act list runs at four live items (1, 2, 3, 5) until the
  relay revives; acts 4 and 6 do not need re-sanctioning when that happens —
  they resume where they were already scoped.
- Rejected: keeping both lanes as co-equal, chosen-per-session paths. That
  would leave the position-release gap perpetually non-urgent, and would
  leave the engine's relay import with no forcing function to unwind —
  the ambiguity was costing the program more than a single ranked lane does.
