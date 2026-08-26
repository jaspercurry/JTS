# HANDOFF — DLNA/UPnP media input (`jasper-dlna`)

> **Status: design-only, not implemented.** There is no `jasper/dlna/`, no
> `gmediarender.service`, and no DLNA fan-in lane in the codebase. This page is
> the decision and the shape a build would take; the full 2026-05 design record
> — renderer research, component plan, systemd and install.sh sketches, RAM
> budget, decision records, upmpdcli evaluation — is
> [historical/dlna-design-2026-05.md](historical/dlna-design-2026-05.md).
> **Before implementing, re-read** [audio-paths.md](audio-paths.md) "Adding a
> new music source" and [HANDOFF-fan-in-daemon.md](HANDOFF-fan-in-daemon.md):
> they are the operational source of truth for the topology, and the archived
> plan is written against the topology as it stood in 2026-05.

## The decision

DLNA/UPnP is the phone-casting surface;
[ADR-0119](adr/0119-dlna-is-the-phone-casting-surface.md) records why, in short:
Google Cast requires a private key fused into genuine Chromecast silicon and
signed by Google's root CA, every commercial sender app enforces it through the
Cast SDK, and there is no hobbyist certification path. No open-source project
has solved phone-app-initiated Cast audio; the partial workarounds cover Chrome
tab mirroring, need keys pulled from a rooted Chromecast, or use a protocol
phones cannot discover. Matter Casting is worth watching for 2027+ and is not
actionable — no phone senders, no Pi-class reference receiver.

DLNA fills the same need with a standard protocol any controller app speaks.
Android users install BubbleUPnP or similar; Windows has native "Play To";
iPhones already have AirPlay. AirConnect (virtual AirPlay endpoints forwarding
to UPnP renderers) stays available later if iOS DLNA demand appears.

## Scope

**In scope.** SSDP advertisement as a UPnP Media Renderer named after the
speaker; playback of the common codecs via GStreamer; audio into a **dedicated
private snd-aloop fan-in lane** — the same per-source pattern AirPlay, Spotify
Connect, Bluetooth, and USB Audio use; latest-source-wins arbitration through
`jasper-mux`; an on/off toggle at `/sources/`; camilla-as-master volume; a
`/state` section; doctor checks; `event=dlna.*` logging; Tier 2 resilience.
Enabled by default, since it is network-only with no hardware dependency.

**Explicit non-goals.** Google Cast compatibility. Video rendering (the
renderer supports it; JTS does not expose it). Acting as a DLNA *server* — JTS
is a renderer, not a source. UPnP volume push-mode: the sender's slider is an
upstream trim and the canonical volume stays on CamillaDSP's `main_volume`
(same decision and rationale as AirPlay, per
[HANDOFF-volume.md](HANDOFF-volume.md)). Multi-room — DLNA is single-room, one
endpoint per physical speaker, and a virtual "group" renderer is a mistake UPnP
has no semantics for. A DLNA controller wizard — the controller lives on the
phone. Track metadata on `/system/` — deferred; it needs `LastChange`
subscription on `AVTransport`.

## The shape

One external C binary (`gmediarender` from gmrender-resurrect, in Debian Trixie
arm64) handles UPnP advertisement, protocol negotiation, decoding, and ALSA
output. A thin Python sidecar (`jasper-dlna`) observes its UPnP state and
publishes it where `jasper-mux`, `jasper-control`, and the sources wizard read
it. **No audio passes through the sidecar.**

```
Phone (BubbleUPnP / Windows "Play To" / any DLNA controller)
   │ UPnP/SSDP discovery + SOAP control + HTTP audio fetch
   ▼
gmediarender ──GStreamer → alsasink──▶ pcm.dlna_substream  ← new private lane
   ▼ (snd-aloop)
jasper-fanin (sums active lanes) → Ring A → CamillaDSP
   ▼
jasper-outputd → DAC → amp → speakers
```

Five properties are the design, and each exists to avoid a coupling:

- **Its own private lane, never a shared dmix.** gmrender does not release the
  ALSA device on UPnP `Pause` — only on `Stop` — and most phone apps send Pause.
  On a private lane that is a non-issue: fan-in simply mixes the silence it
  produces while paused, and no other renderer is blocked.
- **The sidecar is a pure observer**, subscribing to `AVTransport` and
  `RenderingControl` via GENA (`SUBSCRIBE`/`NOTIFY`) rather than polling. UPnP's
  native eventing exists for this: state changes arrive in <50 ms instead of
  ~500 ms average, steady-state CPU is near zero, and it avoids gmrender's
  documented thread-pool pathology under polling load. A 30 s watchdog poll
  stays as the safety net for silently dropped subscriptions — a known failure
  mode in some UPnP stacks.
- **Mux talks to the sidecar, not to UPnP.** Preemption is an HTTP POST to a
  localhost `/preempt` endpoint (the same shape as `_usbsink_set_preempt()`),
  and the sidecar translates it into the right action sequence for whatever
  renderer is running. Mux never learns SOAP, AVTransport, or OpenHome. This is
  what makes a later renderer swap cheap: the protocol knowledge is in one file.
- **Preempt is Pause → confirm `PAUSED_PLAYBACK` → disarm, not raw Stop.**
  `Stop` is the correct semantic signal but phone-side behaviour on an
  externally issued Stop is not standardised — some controllers auto-retry
  `Play`. Clearing the URI leaves auto-resume nothing to play; `Stop` is the
  fallback for renderers that reject an empty URI.
- **One declaration, not a scatter of tuples.** `Source.DLNA` plus one
  `MusicSourceSpec` in `jasper/music_sources.py`
  (`volume_mode=CAMILLA_MASTER`, a `fanin_label`, `renderer_active_key`,
  `wizard_key`) is the entire volume-bucket wiring — `VolumeCoordinator`
  already reads the spec, so there is no `_set_dlna` dispatcher to add. That one
  spec also feeds mux's allow-list, manual source selection, and the doctor
  topology checks.

**Room correction needs no per-renderer wiring.** The archived plan says to add
the renderer unit to a `DEFAULT_RENDERERS_TO_PAUSE` list in
`jasper/correction/coordinator.py`; **that list no longer exists.** Measurement
now silences every music lane at fan-in's diagnostic gate through `jasper-mux`,
so a new lane is quiesced by construction. What a new source does still need is
its `LocalSourceLifecycle` entry in `jasper/local_sources/registry.py`, which
names the systemd intent unit whose enabled state expresses on/off.

**The one integration most easily missed** is the source handoff. DLNA is
camilla-as-master, so taking over from a push-mode source (Spotify/Bluetooth,
where CamillaDSP sits at 0 dB and the source slider carries `listening_level`)
means `main_volume` must be raised to carry the level. Mux must call
`VolumeCoordinator.prepare_source_handoff(...)` before it moves the fan-in gate
and `finalize_source_handoff(...)` after — the same transaction that prevents a
loud Spotify → AirPlay transient. Skipping it lets DLNA blast on takeover.

## Touch list for a build

| Path | Change |
|---|---|
| `jasper/music_sources.py` | `Source.DLNA` + one `MusicSourceSpec` — the canonical declaration everything else derives from |
| `deploy/alsa/asoundrc.jasper` | `pcm.dlna_substream`, a `plug:` alias over a private `hw:Loopback,0,N` lane |
| `rust/jasper-fanin/src/config.rs` | the lane's capture side in `input_pcms` + `"dlna"` in `input_renderers`, positionally aligned |
| `jasper/source_state.py` | `dlna_playing()`, fail-soft like `usbsink_playing()` |
| `jasper/mux.py` | preempt via the sidecar `/preempt` POST; participate in `prepare`/`finalize_source_handoff` |
| `jasper/local_sources/registry.py` | a `LocalSourceLifecycle` entry naming the renderer's intent unit |
| `jasper/renderer.py` | `dlna_playing()` in `active_renderers()` |
| `jasper/web/sources_setup.py` | the `"dlna"` toggle |
| `jasper/control/server.py` | `renderers.dlna` in `/state` |
| `jasper/cli/doctor/` | a renderer check and a GStreamer-plugin check |
| `deploy/install.sh` | package install, system user, persisted UUID, unit installation |
| new: `jasper/dlna/`, `jasper/cli/dlna_main.py`, two systemd units, `pyproject.toml` entry point |

Estimated at ~725 lines of Python plus ~50 of unit config — about 60% of
`jasper-usbsink`, which is the closest existing analogue. Per-file estimates and
the full component sketch are in the
[design record](historical/dlna-design-2026-05.md#9-file-map).

## Renderer choice

`gmediarender` is the Phase 1 pick: a C binary with a GStreamer audio pipeline,
~8-15 MB, Pi-optimised, headless, ALSA output, packaged in Trixie. Total when
enabled is ~13-20 MB Pss including the sidecar; **0 MB when disabled**, since
both services stop.

**upmpdcli** (used by Volumio, moOde, and HiFiBerry's OpenHome path) is the
Phase 2 A/B candidate: first-class OpenHome, strong gapless via MPD, a
server-side playlist so the phone can sleep, and better high-rate FLAC handling
— at ~45-75 MB for two daemons. The preemption-proxy pattern is what makes that
swap a couple of days rather than a week: mux, `source_state`, and the volume
coordinator do not change. The comparison table and swap-cost breakdown are in
the [design record](historical/dlna-design-2026-05.md#13-upmpdcli-evaluation-phase-2).

## Open questions

- **SSDP port conflict.** Avahi uses mDNS (UDP 5353), SSDP uses UDP 1900. No
  known conflict with any JTS daemon; verify at implementation time.
- **Enabled by default?** The plan says yes — it is pure software and costs
  nothing idle. The conservative alternative matches USB Audio Input's
  disabled-by-default stance.
- **DSD is not supported** by the GStreamer plugin set (confirmed upstream).
  Not a concern for DLNA streaming, but it is a hard limit if it ever matters.

Security posture is the same threat model as AirPlay and Spotify Connect:
LAN-only, SSDP is unauthenticated multicast by protocol design, so bind the
renderer to one interface on a multi-NIC Pi and keep UDP 1900 / the UPnP HTTP
port inside the WAN boundary.

## Multi-room forward compatibility

Nothing here blocks it, and nothing here needs to change for it. Every source
already converges at `jasper-fanin`'s summed stream, which is the single stable
insertion point for Snapcast; DLNA is one more input lane and inherits whatever
the summed-output stage becomes. Do not attempt a virtual group renderer.

Last verified: 2026-08-26 (triage pass — re-confirmed that nothing described
here exists in the tree: no `jasper/dlna/`, no DLNA unit, no
`pcm.dlna_substream`. `MusicSourceSpec`, `prepare_source_handoff` /
`finalize_source_handoff`, and `_usbsink_set_preempt` were rechecked as the
integration points the plan assumes. **One stale claim was deleted:**
`jasper/correction/coordinator.py` has no `DEFAULT_RENDERERS_TO_PAUSE` — that
mechanism was replaced by fan-in's mux-driven diagnostic gate, and the surviving
per-source requirement is a `LocalSourceLifecycle` entry. The Cast-vs-DLNA
decision became ADR-0119; the implementation plan, renderer research, and
decision records moved to `docs/historical/dlna-design-2026-05.md`.)
