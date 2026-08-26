# Handoff: source capabilities and provider boundaries

> **Status: operational (contract), plan archived.** This doc owns the *Sources*
> contract: the vocabulary, who owns what today, and what a new source must
> answer before it is written. The cross-cutting lens — the
> host-mediated-indirection invariant, the five extension contracts, the
> decision tree — is [extensibility.md](extensibility.md). Decisions:
> [ADR-0150](adr/0150-source-adapters-hint-the-mux-reconciler-decides.md),
> [ADR-0151](adr/0151-a-new-source-is-camilla-master-until-it-proves-an-observable-volume-surface.md).
>
> The five-phase capability-adapter extraction plan is **archived and not
> shipped** — phases 0–1 landed, phases 2–5 were never started and wait on a
> real second provider:
> [historical/source-capability-extraction-plan-2026-05.md](historical/source-capability-extraction-plan-2026-05.md).
> Runtime truth lives in [audio-paths.md](audio-paths.md) (lanes, fan-in, the
> canonical "adding a music source" checklist),
> [HANDOFF-volume.md](HANDOFF-volume.md) (`VolumeCoordinator`), and
> [HANDOFF-voice-music-control.md](HANDOFF-voice-music-control.md) (transport
> and Spotify routing).

## Vocabulary

| Term | Meaning |
|---|---|
| Provider | A catalog/account ecosystem: Spotify, Apple Music, Plex, a local library, radio. |
| Source / renderer | An audio path into JTS: Spotify Connect, AirPlay, Bluetooth, USB sink, a future native player. |
| Lane | The ALSA/fan-in input a source writes to. |
| Volume carrier | The attenuator that carries JTS `listening_level` for the active source. |
| Capability | A declared source behavior: volume write/observe, transport, metadata, health. |

Provider and source are not the same thing. Apple Music over AirPlay is an
AirPlay source; Apple Music through a future native Pi player would be a
different source. Spotify voice search is provider/catalog logic; Spotify
Connect volume is renderer/source logic.

## Who owns what today

- **`jasper-fanin`** is the hot audio gate. It knows labels and PCM lanes, not
  product policy.
- **`jasper-mux`** owns audible source policy: source-neutral
  latest-start-wins, persistent manual selection, preemption, and guarded source
  handoff before opening a fan-in lane. Source-specific cleanup runs only after
  the guarded handoff succeeds — for AirPlay, receiver-owned `DropSession` with
  an observable MPRIS `Stop` compatibility fallback. No source adapter owns
  priority ([ADR-0150](adr/0150-source-adapters-hint-the-mux-reconciler-decides.md)).
- **`VolumeCoordinator`** owns the single user-facing `listening_level`, source
  handoff safety, degraded push guards, duck-aware Camilla writes, and
  persistence. **`VolumeObserver`** observes source-side volume surfaces and
  feeds confirmed user/source changes into it.
- **`jasper/tools/transport.py`** owns the voice transport command surface.
- **Provider-specific code** such as `spotify_router` owns account/catalog
  routing for that provider.

Static source facts live in `jasper/music_sources.py` — `MusicSourceSpec` with
`id`, `fanin_label`, `renderer_active_key`, `wizard_key`, `volume_mode`,
`display_name`, plus the `VolumeMode` enum (`CAMILLA_MASTER` / `PUSH`). Keep
that registry import-cheap: no Spotipy, D-Bus clients, HTTP clients, or heavy
provider modules. Runtime lifecycle resources for built-in local sources live in
`jasper/local_sources/registry.py`
([HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md)); that entry also
declares the small `health_units` subset whose failure affects playback, while
pairing/advertising helpers stay out of renderer availability.

Source capability details beyond those two registries — volume I/O, transport
support, metadata, provider prerequisites — are still local branches in their
callers. That is acceptable for four built-in sources and is the thing the
archived plan would extract if a fifth arrives.

## The shipped capability map

**Volume.** The mode assignment and the reasoning behind the safe default are
[ADR-0151](adr/0151-a-new-source-is-camilla-master-until-it-proves-an-observable-volume-surface.md).

| Source | Volume |
|---|---|
| Spotify Connect | `PUSH`; set via Spotify Web API, observe via `/run/librespot/state.json`; health includes router/accounts/device visibility. |
| Bluetooth | `PUSH`; set/observe AVRCP `MediaTransport1.Volume`; health includes the active transport path. |
| AirPlay | `CAMILLA_MASTER`; no source write, sender volume observation is diagnostic. |
| USB sink | `CAMILLA_MASTER`; host volume is observed one-way by the USB sink bridge, never written. |
| Idle | `CAMILLA_MASTER`; Camilla only. |

**Transport.**

| Source | Transport |
|---|---|
| Spotify Connect | Spotify Web API against the resolved account/device. |
| AirPlay | Generic MPRIS/DACP when available; the Spotify-over-AirPlay title-match fallback is provider-assisted, not generic AirPlay capability. |
| Bluetooth | BlueZ AVRCP through `org.bluez.MediaPlayer1` when the active phone/player exposes a player object; otherwise a concrete unavailable result. |
| USB sink | Host-owned; unsupported. |

**Metadata** answers "what is playing?" and supports transport routing; it never
decides source priority. AirPlay gives MPRIS title/artist/client name; Spotify
Connect gives librespot state plus the Web API when needed; Bluetooth gives
best-effort BlueALSA/device metadata; USB sink gives none unless a future host
bridge provides it.

**Health** is exposed through `/state.audio.volume_policy` (active carrier,
volume mode, `listening_level`, Camilla dB, push guard state, last source push,
last clear event, mux `last_handoff`), backed by `jasper.volume_diagnostics`
writing a volatile `/run` snapshot on push/guard/clear events. It is not a
daemon and performs no network, D-Bus, Camilla, or Spotify calls from `/state`.
Never put secrets, refresh tokens, raw API keys, SSIDs, or unsafe device
metadata there.

## Design principles

1. **Capabilities do I/O; coordinators own policy.** A volume adapter may set
   Spotify or AVRCP volume. It must not decide degraded handoff policy, duck
   behavior, persistence, or source arbitration.
2. **Source capability beats provider special case.** Do not add `if provider ==
   apple_music` to volume code — ask what the active source can do.
3. **Make failure visible.** A source that cannot satisfy a capability exposes
   that as health/diagnostic state rather than silently redefining the product
   contract.
4. **Keep the Pi budget real.** No resident daemons, high-frequency polling, or
   per-tick network calls; source health is cached, derived, or event-driven.
5. **Do not hide audio safety behind plugins.** Camilla ceilings, positive-gain
   clamps, source handoff guards, and duck/restore invariants stay centralized
   and testable.

## Contributor checklist

Answer these before writing a new source:

1. How does audio enter JTS: AirPlay, Spotify Connect, Bluetooth, USB gadget,
   native player, something else?
2. Which fan-in lane does it use?
3. How do we know it is active?
4. Who carries `listening_level`: the source slider or Camilla?
5. If the source slider — can JTS set it reliably?
6. Can JTS observe source-side volume reliably?
7. What happens when a volume write fails?
8. Can JTS pause/resume/next/previous?
9. What metadata is available for "what is playing?"
10. What health should `/state` and `jasper-doctor` expose?
11. What is the idle RAM/CPU cost when the source is disabled?
12. What test proves it cannot create a loud transient?

"No" is a fine answer to any control question — declare the capability
unsupported and return a clear user-facing result.

## Anti-patterns

- A provider plugin that owns fan-in, mux, volume, transport, and metadata all
  at once.
- A new source that writes directly to a DAC/dmix alias and bypasses CamillaDSP,
  unless it is explicitly assistant-owned audio like TTS.
- A new push-volume source without an observation or health story.
- A source-specific `if provider == ...` branch in `VolumeCoordinator`, or in
  mux policy unless it is genuinely source arbitration rather than provider
  routing.
- Network calls inside the fixed patrol/reconcile loop unless rate-limited and
  proven cheap.
- Silent fallback from broken push volume to permanent Camilla-master semantics
  without surfacing degraded health.

Last verified: 2026-08-26 (`MusicSourceSpec` fields, the `VolumeMode` enum, and
the four shipped mode assignments rechecked against `jasper/music_sources.py`;
`started_seq` and the 5.0 s `UNKNOWN_ACTIVE_HOLD_SEC` grace against
`jasper/mux.py`; `volume_policy` against `jasper/volume_diagnostics.py` and
`jasper/control/state_aggregate.py`; `jasper/volume_adapters.py` confirmed
absent, so the archived phases 2–5 remain unstarted). Prior 2026-07-22 (mux
policy ownership, post-handoff AirPlay receiver cleanup, fallback
observability, source-adapter boundaries).
