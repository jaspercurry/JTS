# HANDOFF - Source capabilities and provider boundaries

**Part of the JTS extensibility model** — this doc owns the *Sources*
contract. The cross-cutting lens (the host-mediated-indirection invariant,
the five extension contracts, the decision tree) lives in
[extensibility.md](extensibility.md).

This is the execution plan for making JTS easier to extend with new
music sources and providers without turning source integration into a
framework project. It is not yet the shipped implementation. Current
runtime truth still lives in:

- [audio-paths.md](audio-paths.md) for audio lanes, fan-in, and the
  canonical "adding a music source" checklist.
- [HANDOFF-volume.md](HANDOFF-volume.md) for the shipped
  `VolumeCoordinator` behavior.
- [HANDOFF-voice-music-control.md](HANDOFF-voice-music-control.md)
  for voice transport and Spotify routing.

The goal of this document is to define the boundary we should extract
next: **source capabilities**, starting with volume, then transport and
metadata.

## Current Shape

JTS already has the right system ownership:

- `jasper-fanin` is the hot audio gate. It knows labels and PCM lanes,
  not product policy.
- `jasper-mux` owns audible source policy: latest-source-wins, manual
  selection, preemption, and guarded source handoff before opening a
  fan-in lane.
- `VolumeCoordinator` owns the single user-facing `listening_level`,
  source handoff safety, degraded push guards, duck-aware Camilla
  writes, and persistence.
- `VolumeObserver` observes source-side volume surfaces and feeds
  confirmed user/source changes into the coordinator.
- `jasper/tools/transport.py` owns the voice transport command surface.
- Provider-specific code such as `spotify_router` owns account/catalog
  routing for that provider.

The missing boundary is that source capability details are still spread
across these modules. `jasper/music_sources.py` declares static source
facts and `VolumeMode`, and `jasper/local_sources/registry.py` declares
runtime lifecycle resources for built-in local sources. Source-specific
volume I/O, transport support, metadata, health, and provider prerequisites
still live as local branches in several callers.

That is acceptable for four built-in sources. It becomes awkward when
future sources arrive: Apple Music, local library playback, internet
radio, Music Assistant-backed streaming, Plex, Snapcast, or other
provider/renderers with different control surfaces.

## Vocabulary

Use these terms consistently:

| Term | Meaning |
|---|---|
| Provider | A catalog/account ecosystem: Spotify, Apple Music, Plex, a local library, radio. |
| Source / renderer | An audio path into JTS: Spotify Connect, AirPlay, Bluetooth, USB sink, future native player. |
| Lane | The ALSA/fan-in input a source writes to. |
| Volume carrier | The attenuator that carries JTS `listening_level` for the active source. |
| Capability | A declared source behavior: volume write/observe, transport, metadata, health. |

Provider and source are not the same thing. Apple Music over AirPlay is
an AirPlay source. Apple Music through a future native Pi player would
be a different source. Spotify voice search is provider/catalog logic;
Spotify Connect volume is renderer/source logic.

## Design Principles

1. **Default new sources to safe local control.** A new source starts as
   `CAMILLA_MASTER` unless it proves it has a reliable, observable,
   user-facing volume surface that JTS can write.
2. **Capabilities do I/O; coordinators own policy.** A volume adapter
   may set Spotify or AVRCP volume. It must not decide degraded handoff
   policy, duck behavior, persistence, or source arbitration.
3. **Source capability beats provider special case.** Do not add
   `if provider == apple_music` to volume code. Ask what the active
   source can do.
4. **Make failure visible.** If a source cannot satisfy a capability,
   expose that as health/diagnostic state. Do not silently redefine the
   product contract forever.
5. **Keep the Pi budget real.** Capability extraction should not add
   resident daemons, high-frequency polling, or per-tick network calls.
   Source health should be cached, derived, or event-driven where
   possible.
6. **Do not hide audio safety behind plugins.** Camilla ceilings,
   positive-gain clamps, source handoff guards, and duck/restore
   invariants remain centralized and testable.

## Target Capability Shape

This is the shape to extract incrementally, not a mandate to implement a
large plugin API in one PR.

### Static Source Spec

`jasper/music_sources.py` remains the static registry:

```python
@dataclass(frozen=True)
class MusicSourceSpec:
    id: Source
    fanin_label: str
    renderer_active_key: str
    wizard_key: str
    volume_mode: VolumeMode
    display_name: str
```

Near-term additions may include capability keys or adapter factory
identifiers, but keep static facts import-cheap. Do not import Spotipy,
DBus clients, HTTP clients, or heavy provider modules from the registry.

### Volume Capability

First extraction target. Suggested minimal contract:

```python
from typing import Literal

@dataclass(frozen=True)
class VolumeWriteResult:
    ok: bool
    reason: Literal[
        "ok",
        "unsupported",
        "missing_router",
        "no_active_device",
        "no_active_transport",
        "write_failed",
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
```

The coordinator should consume results, not provider internals:

- `ok=True` means the source's own volume surface accepted
  `listening_level`.
- `ok=False` means the coordinator should apply the existing
  degraded-safe Camilla guard when the source is becoming audible.
- `reason` is a stable machine value for tests, `/state`, and
  `event=` logs. Put operator prose in `detail`, not in `reason`.
- `observe_level()` means "the source reports its own user-facing
  volume from a cheap local/cached surface." It does not grant adapters
  permission to add network calls inside a poll loop, and it does not
  automatically mean JTS should honor the observation; AirPlay remains
  diagnostic-only until hardware evidence says otherwise. The
  observer/daemon owns cadence.

Current adapters map naturally:

| Source | Adapter behavior |
|---|---|
| Spotify Connect | `PUSH`; set via Spotify Web API; observe via `/run/librespot/state.json`; health includes router/accounts/device visibility. |
| Bluetooth | `PUSH`; set/observe AVRCP `MediaTransport1.Volume`; health includes active transport path. |
| AirPlay | `CAMILLA_MASTER`; no source write; sender volume observation remains diagnostic. |
| USB sink | `CAMILLA_MASTER`; host volume is observed one-way by the USB sink bridge, not written by JTS. |
| Idle | `CAMILLA_MASTER`; Camilla only. |

### Transport Capability

Second extraction target. Keep it separate from volume.

```python
class SourceTransportAdapter(Protocol):
    source: Source

    async def dispatch(self, action: TransportAction) -> TransportResult: ...
    def health(self) -> dict[str, object]: ...
```

Current mapping:

| Source | Transport |
|---|---|
| Spotify Connect | Spotify Web API against the resolved account/device. |
| AirPlay | Generic MPRIS/DACP when available; Spotify-over-AirPlay title-match fallback is provider-assisted. |
| Bluetooth | BlueZ AVRCP through `org.bluez.MediaPlayer1` when the active phone/player exposes a player object; otherwise return a concrete unavailable result. |
| USB sink | Host-owned; unsupported. |

### Metadata Capability

Metadata should answer "what is playing?" and support transport routing.
It should not decide source priority.

Examples:

- AirPlay: MPRIS title/artist/client name.
- Spotify Connect: librespot state plus Spotify Web API when needed.
- Bluetooth: best-effort BlueALSA/device metadata if available.
- USB sink: probably none unless a future host bridge provides it.

### Health / Diagnostics

Each capability should expose cheap, stable status fields that `/state`
can include without network churn:

- source id and display name
- active/inactive
- volume mode
- volume write availability
- last write result and reason
- observed source volume age
- push guard active / guard dB / guard reason when available
- transport supported/unsupported/degraded
- provider prerequisites such as "Spotify credentials missing" or
  "no authorized account"

Do not store secrets, refresh tokens, raw API keys, SSIDs, or device
metadata that is not already safe for `/state`.

## Current Status - 2026-05-28

Implemented in the current workstream:

- Spotify/BT degraded push guards clear after a later successful push
  dispatch or confirmed same-level active-source observation.
- Bluetooth transport and source preemption now use the shared BlueZ
  AVRCP helper (`org.bluez.MediaPlayer1`) when the active A2DP source
  exposes a player object. Extraction into a source capability adapter
  is still future work.
- `jasper-mux` loads the wizard-owned Spotify credential env file so
  guarded Spotify handoff has the same Web API inputs as voice/control.
- Spotify credential/account/default changes restart `jasper-voice`,
  `jasper-control`, and `jasper-mux`; playlist-only edits still restart
  voice only.
- `/state.audio.volume_policy` exposes the active carrier, volume mode,
  `listening_level`, Camilla dB, push guard state, last source push,
  last clear event, and mux `last_handoff`.
- `jasper.volume_diagnostics` records the last push/guard/clear facts
  in volatile `/run` state. It is not a daemon and it performs no
  network, DBus, Camilla, or Spotify calls from `/state`.

Outstanding before declaring the bug closed:

- Deploy to the Pi with `bash scripts/deploy-to-pi.sh`.
- Reproduce the user flow on hardware: AirPlay at the current phone
  volume, then Spotify Connect at 100%.
- Confirm `curl -s http://jts.local:8780/state | jq .audio.volume_policy`
  shows Spotify in `volume_mode="push"` with `carrier="source"` and
  `push_guard_active=false` after Spotify push or confirmed observation.
- If Spotify push fails, confirm the state instead shows
  `carrier="camilla_guard"` / `push_guard_active=true` and the speaker is
  quieter than intended, never louder.

Not yet started on purpose:

- Extracting `jasper/volume_adapters.py`.
- Centralizing Spotify router construction.
- Transport/metadata/source-template abstractions.
- Dashboard rendering for `/state.audio.volume_policy`.

Those are follow-up PRs. Do not start them until hardware validation
confirms this slice behaves correctly on the real audio path.

## Phased Execution Plan

### Phase 0 - Close The Current Bug

Finish the Spotify quiet-at-100% fix and validate on hardware:

- Spotify Connect at 100% after AirPlay should land with Camilla at
  `0.0 dB` once Spotify volume push or confirmed source observation
  succeeds. The 0% case is the intentional exception: Camilla keeps
  `main_mute=true` plus the calibrated floor (default −50 dB) so
  push-mode zero mutes content.
- Failed Spotify push should stay `degraded_safe`, never louder.
- AirPlay should remain Camilla-master.

No source-capability refactor should land before this behavior is live
and understood.

### Phase 1 - Add Read-Only Volume Diagnostics

Before moving code, make the state observable:

- Add derived `/state.audio.volume_policy`.
- Include effective source, `VolumeMode`, `listening_level`,
  `main_volume_db`, push guard active, guard dB, guard reason if known,
  last handoff result, and last push/clear event if already available.
- Prefer derived state plus structured logs. A tiny volatile `/run`
  diagnostics snapshot is acceptable for last push/clear details; do
  not introduce a resident diagnostics daemon or network-backed state.

Acceptance criteria:

- The "Spotify app says 100% but speaker is quiet" state is visible
  from `/state` without SSH.
- No new polling or network calls are added to `/state`.
- Tests cover degraded guard and normal push-mode visibility.

Initial 2026-05-28 slice: `jasper.volume_diagnostics` writes a
volatile `/run/jasper/volume_policy.json` snapshot on push/guard/clear
events, and `/state.audio.volume_policy` exposes the derived carrier
and guard state. This is observability only; it does not extract
adapters or change source policy.

### Phase 2 - Extract Volume Adapters

Create a small volume adapter module, preferably
`jasper/volume_adapters.py` so existing volume docs routing catches it.

Before moving Spotify into that adapter, centralize Spotify router
construction into one helper used by voice, control, mux, and the
adapter. This is a prerequisite for the Spotify slice, not a broad
provider framework.

Move source-specific I/O out of `VolumeCoordinator`:

- Spotify volume write and router health
- Bluetooth AVRCP write
- AirPlay/USB/idle Camilla-master no-op adapters
- source observation readers only when they belong naturally with the
  volume surface

Keep in `VolumeCoordinator`:

- `listening_level`
- persistence
- source handoff safety
- degraded-safe guard
- duck-aware Camilla writes
- source transition policy
- echo/cross-process suppression

Acceptance criteria:

- Adding a push-volume source does not require editing coordinator
  policy, only registering an adapter and tests.
- Existing source handoff tests still pass.
- No adapter can write positive Camilla gain or bypass the
  coordinator's safety guard.

### Phase 3 - Centralize Provider Runtime Construction

After the Spotify helper exists, use the same boring pattern for future
provider runtimes only when a real provider needs it. Spotify router
construction should no longer be duplicated across voice, control, mux,
and source adapters.

Target:

- one function/module owns env paths, account registry paths,
  redirect URI resolution, `BuildResult.clients`, statuses, empty
  reasons, and rebuild cooldowns.
- daemon units still declare which env files they need, but runtime code
  should not duplicate the same "build Spotify router" recipe.

This is where future Apple Music or other provider runtimes should look
for the pattern: a provider runtime owns provider auth/catalog details;
source adapters own renderer capability details.

### Phase 4 - Extract Transport Capabilities

Only after volume is cleaner, extract transport.

Move `jasper/tools/transport.py` toward source transport adapters:

- AirPlay adapter
- Spotify Connect adapter
- Bluetooth AVRCP adapter, with explicit unavailable results when no
  BlueZ player object exists
- unsupported adapter for USB with explicit user messages
- provider-assisted fallback for Spotify-over-AirPlay remains a special
  routing helper, not generic AirPlay capability

Acceptance criteria:

- Adding a source with no transport support requires declaring an
  unsupported result, not branching through the voice tool.
- Adding a source with real transport support has a single adapter test
  and one source registry entry.

### Phase 5 - Source Integration Template

Once volume and transport adapters exist, update the "Adding a new music
source" checklist in [audio-paths.md](audio-paths.md) into a concrete
template:

- lane
- active-state probe
- volume adapter
- transport adapter
- metadata adapter
- health fields
- mux preemption
- wizard/on-off behavior
- doctor checks
- tests
- docs

This should become the contributor-friendly path for Apple Music or any
other future source.

Do not start Phases 4-5 just to complete the abstraction. Wait until a
real second source/provider forces the transport, metadata, or template
shape.

## Contributor Checklist

When proposing a new source, answer these before coding:

1. How does audio enter JTS: AirPlay, Spotify Connect, Bluetooth, USB
   gadget, native player, or something else?
2. Which fan-in lane does it use?
3. How do we know it is active?
4. Who carries `listening_level`: source slider or Camilla?
5. If source slider, can JTS set it reliably?
6. Can JTS observe source-side volume reliably?
7. What happens when volume write fails?
8. Can JTS pause/resume/next/previous?
9. What metadata is available for "what is playing?"
10. What health should `/state` and `jasper-doctor` expose?
11. What is the idle RAM/CPU cost when the source is disabled?
12. What test proves it cannot create a loud transient?

If the answer to a control question is "no," that is fine. Declare the
capability unsupported and return a clear user-facing result.

## Anti-Patterns

- A provider plugin that owns fan-in, mux, volume, transport, and
  metadata all at once.
- A new source that writes directly to `jasper_out` and bypasses
  CamillaDSP unless it is explicitly assistant-owned audio like TTS.
- A new push-volume source without an observation or health story.
- A source-specific `if provider == ...` branch in `VolumeCoordinator`.
- A source-specific `if provider == ...` branch in mux policy unless it
  is genuinely source arbitration, not provider routing.
- Network calls inside a 1 Hz loop unless rate-limited and proven cheap.
- Silent fallback from broken push volume to permanent Camilla-master
  semantics without surfacing degraded health.

## Review Checklist For This Workstream

- Audio safety: failed writes are quieter than intended, never louder.
- Observability: degraded capability states are visible in logs and
  `/state`.
- Modularity: provider auth/catalog code is separate from source volume
  and transport capability.
- Resource budget: disabled sources cost zero resident RAM; enabled
  sources fit the Pi 5 budget.
- Tests: source capability contracts have hardware-free unit coverage;
  fan-in, mux, and coordinator invariants remain covered.
- Docs: update this doc for the capability model, `audio-paths.md` for
  source-addition checklist changes, `HANDOFF-volume.md` for volume
  behavior, and `HANDOFF-voice-music-control.md` for transport behavior.

Last verified: 2026-06-25
