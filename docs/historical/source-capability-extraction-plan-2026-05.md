# Source-capability extraction plan (2026-05) — historical

> **Status: historical.** The five-phase plan for extracting source capability
> adapters (volume, then transport, then metadata, then a source template) and
> the dated 2026-05-28 status of the Spotify quiet-at-100% workstream that
> motivated it. Phases 0 and 1 shipped — `/state.audio.volume_policy` and
> `jasper.volume_diagnostics` are live. **Phases 2–5 were never started, on
> purpose:** `jasper/volume_adapters.py` does not exist, and no second provider
> has yet forced the transport, metadata, or template shape. Kept because the
> phase ordering and its acceptance criteria are still the right plan *if* that
> second provider arrives.
>
> **Nothing here is current operational truth** — the live spine is
> [HANDOFF-source-capabilities.md](../HANDOFF-source-capabilities.md), and the
> decisions are ADR-0150 and ADR-0151. The `Protocol` and dataclass sketches
> below were never implemented; treat them as a proposal, not as an API.

## Proposed capability protocols (never implemented)

```python
@dataclass(frozen=True)
class VolumeWriteResult:
    ok: bool
    reason: Literal[
        "ok", "unsupported", "missing_router", "no_active_device",
        "no_active_transport", "write_failed",
    ] = "ok"
    detail: str = ""

@dataclass(frozen=True)
class ObservedVolume:
    level: int
    source_units: str
    raw_value: float | int

class SourceVolumeAdapter(Protocol):
    source: Source
    mode: VolumeMode

    async def set_level(self, level: int) -> VolumeWriteResult: ...
    async def observe_level(self) -> ObservedVolume | None: ...
    def health(self) -> dict[str, object]: ...

class SourceTransportAdapter(Protocol):
    source: Source

    async def dispatch(self, action: TransportAction) -> TransportResult: ...
    def health(self) -> dict[str, object]: ...
```

The coordinator was to consume results, not provider internals: `ok=True` means
the source's own surface accepted `listening_level`; `ok=False` means apply the
existing degraded-safe Camilla guard when the source is becoming audible;
`reason` is a stable machine value for tests, `/state`, and `event=` logs, with
operator prose confined to `detail`. `observe_level()` was explicitly *not* a
licence to add network calls inside a poll loop — the observer/daemon owns
cadence.

Proposed health fields per capability: source id and display name,
active/inactive, volume mode, volume write availability, last write result and
reason, observed source volume age, push guard active / guard dB / guard reason,
transport supported/unsupported/degraded, and provider prerequisites such as
"Spotify credentials missing". Never secrets, refresh tokens, raw API keys,
SSIDs, or device metadata not already safe for `/state`.

## Status snapshot — 2026-05-28

Implemented in that workstream:

- Spotify/BT degraded push guards clear after a later successful push dispatch
  or confirmed same-level active-source observation.
- Bluetooth transport and source preemption use the shared BlueZ AVRCP helper
  (`org.bluez.MediaPlayer1`) when the active A2DP source exposes a player
  object. Extraction into a capability adapter was left as future work.
- `jasper-mux` loads the wizard-owned Spotify credential env file so guarded
  Spotify handoff has the same Web API inputs as voice/control.
- Spotify credential/account/default changes restart `jasper-voice`,
  `jasper-control`, and `jasper-mux`; playlist-only edits restart voice only.
- `/state.audio.volume_policy` exposes the active carrier, volume mode,
  `listening_level`, Camilla dB, push guard state, last source push, last clear
  event, and mux `last_handoff`.
- `jasper.volume_diagnostics` records the last push/guard/clear facts in
  volatile `/run` state — not a daemon, and no network, D-Bus, Camilla, or
  Spotify calls from `/state`.

Outstanding at the time: deploy to the Pi; reproduce AirPlay-then-Spotify-at-100%
on hardware; confirm `/state.audio.volume_policy` shows Spotify in
`volume_mode="push"` with `carrier="source"` and `push_guard_active=false` after
a push or confirmed observation; and confirm a failed push instead shows
`carrier="camilla_guard"` / `push_guard_active=true` with the speaker quieter
than intended, never louder.

## Phase 0 — close the current bug

Finish the Spotify quiet-at-100% fix and validate on hardware. Spotify Connect
at 100% after AirPlay should land with Camilla at `0.0 dB` once the volume push
or a confirmed source observation succeeds; the 0% case is the intentional
exception, where Camilla keeps `main_mute=true` plus the calibrated floor
(default −50 dB) so push-mode zero mutes content. A failed push stays
`degraded_safe`, never louder. AirPlay remains Camilla-master. No
source-capability refactor lands before this behaviour is live and understood.

## Phase 1 — read-only volume diagnostics

Make the state observable before moving code: derived
`/state.audio.volume_policy` carrying effective source, `VolumeMode`,
`listening_level`, `main_volume_db`, push guard active/dB/reason, last handoff
result, and last push/clear event. Prefer derived state plus structured logs; a
tiny volatile `/run` snapshot is acceptable, a resident diagnostics daemon or
network-backed state is not.

Acceptance: the "Spotify app says 100% but speaker is quiet" state is visible
from `/state` without SSH; no new polling or network calls in `/state`; tests
cover degraded guard and normal push-mode visibility. **Shipped.**

## Phase 2 — extract volume adapters

Create `jasper/volume_adapters.py`. First centralize Spotify router
construction into one helper used by voice, control, mux, and the adapter — a
prerequisite for the Spotify slice, not a provider framework. Move
source-specific I/O out of `VolumeCoordinator` (Spotify volume write and router
health; Bluetooth AVRCP write; AirPlay/USB/idle no-op adapters; source
observation readers only where they belong with the volume surface).

Keep in `VolumeCoordinator`: `listening_level`, persistence, source handoff
safety, the degraded-safe guard, duck-aware Camilla writes, source transition
policy, echo/cross-process suppression.

Acceptance: adding a push-volume source requires registering an adapter and
tests, not editing coordinator policy; existing handoff tests still pass; no
adapter can write positive Camilla gain or bypass the safety guard.

## Phase 3 — centralize provider runtime construction

One function/module owns env paths, account registry paths, redirect URI
resolution, `BuildResult.clients`, statuses, empty reasons, and rebuild
cooldowns. Daemon units still declare which env files they need, but runtime
code stops duplicating the "build Spotify router" recipe. This is where a future
Apple Music runtime should look for the pattern: a provider runtime owns
auth/catalog details, source adapters own renderer capability details.

## Phase 4 — extract transport capabilities

Only after volume is cleaner. Move `jasper/tools/transport.py` toward per-source
transport adapters: AirPlay; Spotify Connect; Bluetooth AVRCP with explicit
unavailable results when no BlueZ player object exists; an unsupported adapter
for USB with explicit user messages. The Spotify-over-AirPlay fallback stays a
provider-assisted routing helper, not generic AirPlay capability.

Acceptance: adding a source with no transport support requires declaring an
unsupported result rather than branching through the voice tool; adding one with
real support needs a single adapter test and one registry entry.

## Phase 5 — source integration template

Turn the "adding a new music source" checklist in
[audio-paths.md](../audio-paths.md) into a concrete template: lane, active-state
probe, volume adapter, transport adapter, metadata adapter, health fields, mux
preemption, wizard/on-off behaviour, doctor checks, tests, docs.

Phases 4–5 were explicitly not to be started just to complete the abstraction —
they wait until a real second source/provider forces the shape. That has not
happened.
