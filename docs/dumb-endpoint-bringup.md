# Dumb endpoint bring-up: Raspberry Pi Zero 2 W

> **Status: historical.** Snapshot from mid-2026, before the install-tier
> consolidation. It predates the removal of the separate "endpoint" /
> "satellite" install tier (#707/#716) — there are now exactly **two**
> install profiles, `full` and `streambox`. The legacy `endpoint` /
> `satellite` tokens are still accepted and map to `streambox`
> (`jasper/install_profile.py`, `_LEGACY_STREAMBOX_ALIASES`), so a field
> box auto-migrates on its next deploy. "Endpoint behaviour" (a box that
> just plays a bonded channel) is now the runtime multiroom **follower**
> role on any full/streambox box, not an install tier.
>
> **Do not trust the following specifics below — they describe the dead
> tier:** the `--satellite-only` install flag (it does not exist), any
> `JASPER_INSTALL_PROFILE=endpoint` / persisted "satellite-only" profile,
> and the precedence rule that "an actively bonded follower stays
> satellite-only." Follower role is chosen at runtime by the multiroom
> reconciler regardless of install profile. Read this doc for the Pi
> Zero 2 W bring-up narrative, not for current install-profile state.
> Current operational truth lives in `jasper/install_profile.py` (the
> two-profile model) and [`HANDOFF-multiroom.md`](HANDOFF-multiroom.md)
> (the follower role).

This is the operator runbook for bringing up a cheap JTS endpoint such
as `jts4`: a Raspberry Pi Zero 2 W that can be a synchronized satellite
for a stereo pair / wireless subwoofer / multi-room group, or a
standalone streambox for AirPlay / Spotify Connect / Bluetooth playback.

It is **not** the full JTS speaker bring-up. The architecture lives in
[`HANDOFF-multiroom.md`](HANDOFF-multiroom.md); this file owns the
practical hardware/software path, the endpoint install tier, and the
working build plan for a dumb endpoint.

## Current Truth

Today, a transport endpoint has three supported shapes:

- **Lab/spike target:** Raspberry Pi OS Lite on a Zero 2 W, Wi-Fi + SSH,
  `alsa-utils`, `snapclient`, `sox`, and a DAC that ALSA can see, such
  as an Apple USB-C to 3.5 mm adapter on the Zero's USB OTG/data port.
- **JTS endpoint install tier:** the same repo/package deployed with
  `JASPER_INSTALL_PROFILE=endpoint` or `satellite`. This is now the
  explicit satellite-only profile, not the default Zero experience. It
  installs `jasper-control`, Avahi identity/discovery, the grouping
  reconciler, and JTS-managed Snapcast units; it also applies the same
  memory/cgroup/zram tuning that makes `jts-audio.slice` enforceable.
  It never installs `jasper-voice`, CamillaDSP, renderers, web wizards,
  AEC, fan-in, outputd, or the Rust/AEC build toolchain.
- **JTS streambox install tier:** the same repo/package deployed with
  `JASPER_INSTALL_PROFILE=streambox`. This installs the local renderer
  and DSP graph (`shairport-sync`, Spotify Connect/librespot, Bluetooth,
  USB Audio Input, fan-in, outputd, CamillaDSP, `/sources`, `/sound`,
  `/spotify`, `/system`, `/rooms`, and correction/balance/sync web
  surfaces) while deliberately omitting wake word, mic/AEC, assistant
  providers, TTS/cues, CamillaGUI, and accessory firmware. This is the
  default Zero 2 W install when the box is not already an active bonded
  follower.
  USB Audio Input is intentionally visible on streambox installs,
  including Zero-class hardware, so powered USB splitter experiments can
  validate whether the OTG data role and the USB DAC can coexist. If
  the splitter path cannot keep both legs stable, remove that source
  from the Zero-class profile rather than quietly degrading DAC output.

Do **not** run a bare full install on the Zero 2 W. The full profile
builds AEC3 and installs the voice/assistant stack, which does not fit
the product role or memory envelope. Deploy the `streambox` profile —
the default Zero role — by adopting the box and running the normal
deploy with the profile set explicitly so the deploy log shows intent:

```sh
bash scripts/onboard.sh jts4.local --adopt
PI_HOST=jts4.local JASPER_INSTALL_PROFILE=streambox bash scripts/deploy-to-pi.sh
```

If `.local` resolution is flaky but the router shows an IP, keep the
speaker identity explicit:

```sh
bash scripts/onboard.sh 192.168.1.162 --adopt --speaker-hostname jts4.local
PI_HOST=192.168.1.162 JASPER_HOSTNAME=jts4.local JASPER_INSTALL_PROFILE=streambox \
  bash scripts/deploy-to-pi.sh
```

`onboard.sh --adopt` sets up SSH/laptop state; `deploy-to-pi.sh` runs
the install, reboots once if the cgroup/zram boot contract needs it,
and verifies the management surface. A box that is already an active bonded
follower parks its brain through the multiroom role plan and its sources
through the canonical source coordinator — there is no separate
satellite-only install profile to choose.

For manual deploys, the normal deploy path is still valid. On a fresh
Raspberry Pi Zero 2 W with no persisted profile marker, the installer
auto-resolves to `streambox`; passing the profile explicitly is useful
when you want the deploy log and shell history to show intent:

```sh
PI_HOST=jts4.local JASPER_INSTALL_PROFILE=streambox bash scripts/deploy-to-pi.sh
```

If a legacy Zero already has a persisted `endpoint` marker and is not an
active bonded follower, the installer now treats that as an old
satellite-only bring-up and upgrades it to `streambox` on the next
deploy. An actively bonded follower stays satellite-only. An explicit
`JASPER_INSTALL_PROFILE=endpoint` or `--satellite-only` also keeps the
minimal follower profile.

The brainy JTS speaker remains the leader.

The product multi-room path is still in progress. The Zero can be used
now with [`scripts/multiroom-spike.sh`](../scripts/multiroom-spike.sh)
to prove Snapcast, Wi-Fi, and DAC behavior before product audio is wired
end-to-end.

## Role And Topology Axes

The product shape is one JTS package with small install roles plus a
separate output-topology capability. The install role says who owns
sources and content policy; the topology says what hardware this box
physically drives. Keep those separate.

Compatibility note: the built satellite role still persists
`endpoint` in `/var/lib/jasper/install_profile`. `satellite` is accepted
as an alias and normalizes to that marker so existing JTS4-style installs
do not need migration.

| Role / profile | Status | Typical hardware | Purpose | Runs |
|---|---|---|---|---|
| `full` | built | Pi 5 class | Brain speaker: sources, voice, room/content DSP, grouping leader, full UI | everything |
| `satellite` | built today as persisted `endpoint` marker | Zero 2 W | Follower for a leader-owned synchronized group | `jasper-control`, Avahi, grouping reconcile, Snapclient, shared capability-gated `/`, endpoint-scoped `/system`, `/sources` |
| `streambox` | built; Zero 2 W validation pending | Zero 2 W / Pi class | Standalone AirPlay / Spotify Connect / Bluetooth output target with no mic/AI brain | renderers, local output path, `/spotify`, `/sources`, `/system`, `/sound`, `/rooms`, correction/balance/sync |

| Output topology | Status | Applies to | Meaning |
|---|---|---|---|
| `full_range` | built for satellite endpoint | satellite or streambox | One local full-range output; no local driver DSP required. |
| `active_crossover` | planned | satellite or streambox | The box owns local driver routing/protection for the DACs/amps it drives, so it installs CamillaDSP and exposes `/crossover`. |

In the friendly first-run product flow, a newly imaged Zero 2 W starts as
the streambox-capable endpoint: quiet, safe, visible at `http://<host>/`,
and able to run local sources and local content DSP without becoming a
voice/AI brain. If another speaker bonds it as a follower, grouping changes the
runtime role and waits on the canonical source coordinator to park local
renderers; the paired leader owns content DSP, and the shared UI hides or redirects
the controls that no longer apply while paired.

The satellite landing page and `/system/` page follow the same
capability contract as the full and streambox UI. The shared landing
page hides source, sound, assistant, integration, accessory, network,
speaker-name, and developer cards whose routes are not present on the
endpoint tier; `/system/` shows health, diagnostics, and power actions,
but hides local voice/audio restart and audio-conversion controls
because those services do not run on the endpoint tier.

The satellite-only install profile remains useful for lab validation and
for a deliberately tiny follower image, but it is no longer the default
way to bring a Zero online. In normal product operation, streambox is the
capability set and grouping is the runtime role switch: pair it and local
sources park; unpair it and the source/EQ/room-correction surfaces are
available again.

Active crossover is not a third role. It is an output-topology capability
that can be layered onto either satellite or streambox:

- Satellite + active crossover: no local content EQ; local CamillaDSP is
  for driver crossover/protection only, and `/sound` should point to the
  leader.
- Streambox + active crossover: local sources and local `/sound` are
  valid; `/crossover` remains the hardware-safety surface.

## Decision (2026-06-12) — one package, install tiers; not a parallel endpoint package

An earlier draft of this plan proposed a separate code/install universe
for endpoints: a `jts_endpoint` agent package, a stdlib-only
`jts_contracts` wire-vocabulary package, a parallel installer under
`deploy/endpoint/`, `jts-endpoint-*` systemd units, and import-guard
machinery defending the boundary. **That direction is rejected.** The
endpoint is a JTS speaker on a small install tier — same repo, same
package, same control plane — for three reasons:

1. **The control plane already exists.** The proposed endpoint agent's
   jobs (identity, role assignment, health reporting, leader-side
   registry/rollup) are `jasper-control`'s existing job: `/rooms`
   discovery, `POST /grouping/set`, the grouping runtime-health derive,
   the `pair channels` doctor probe, the bonded-follower volume
   forward, and channel swap. Every pair feature works *because*
   members run jasper-control (23 MB) — a snapclient-only endpoint is
   invisible to all of it, so the parallel-package plan had to rebuild
   each feature as bespoke "endpoint registry" work, plus version and
   test a second wire-contract surface forever.
2. **The repo's pattern for hardware variants is a profile, not a
   package.** Config-ownership pattern 3 (pure-data registry +
   reconciler deriving what runs) is how DACs, mics, and wake models
   already vary. The bonded "dumb follower" work builds exactly the
   role-profile machinery an endpoint needs; the Zero is that profile
   on a machine where the parked components were never installed.
3. **The real Zero blocker is install-time, not runtime.** What runs
   fits easily in 512 MB (jasper-control + snapclient ≈ 26 MB). What
   breaks is *building*: webrtc-AEC3 and two Rust daemons compiled on
   512 MB (hours, OOM-prone) plus ~1.5 GB of voice/wake dependencies
   that would never execute. The fix is an **install profile** that
   skips those entirely — not a second codebase.

The boundary the package walls defended is held instead by guard tests:
an import-cost test (the daemons the satellite tier runs must be
importable without the voice extras), install-plan tests (endpoint has no
brain builds; streambox has the renderer/DSP graph but no voice/wake/mic),
profile-capability tests, deploy-verification tests, and a full-profile
regression test (the default speaker plan stays byte-identical). A new
small role is a tier upgrade on the same install, not a migration.

What survives from the earlier draft unchanged: the product contract,
buffer policy, resiliency contract, observability contract, the lab
runbook, and the hardware-validation phase below — those were always
requirements on the *role*, not on a package.

## Software boundary

Think of the satellite role as a transport appliance with a JTS control
plane, not a second brainy speaker. The leader owns the microphone path,
wake/AI session, content DSP, grouping decisions, stream production, and
room-level setup. The endpoint owns booting, staying on the network,
answering the control plane (`/rooms` bond/swap/health, the pair-volume
forward), accepting the leader's synchronized stream, selecting its
assigned channel, and playing it to the local DAC safely.

There are two DSP classes:

- **Content DSP** lives on the leader: room correction, preference EQ,
  group crossover/LFE generation, per-seat correction, and group delay /
  trim policy.
- **Driver DSP** lives on the box driving the DAC/amps: active
  woofer/tweeter crossover, per-driver delay/gain, polarity, protection
  filters, and hard safety limits.

The current `endpoint` marker implements the satellite/full-range case:
no local driver DSP. A future `active_crossover` topology capability may
install CamillaDSP and expose `/crossover` on either satellite or
streambox, but it is still not a brain: no wake word, no AI session, and
no satellite-local content EQ.

The install roles:

| Role | Hardware | Installs | Runs |
|---|---|---|---|
| Full speaker | Pi 5 class | everything | role-derived: solo / leader / dumb follower (voice, AEC, renderers, mux parked while bonded) |
| Streambox | Zero 2 W class, pending validation | renderer/DSP profile: AirPlay / Spotify Connect / Bluetooth / USB Audio Input, mux, fan-in/outputd, CamillaDSP, shared capability-gated web, local `/sound` | local music target; no voice/AI brain; may expose local rooms/pair controls when product policy allows |
| Satellite endpoint | Zero 2 W class | core profile only: `jasper-control` + the multiroom plumbing + a managed `snapclient` unit + ALSA bits | `jasper-control` + `snapclient`; everything else was never installed |

The output topology:

| Topology | Satellite behavior | Streambox behavior |
|---|---|---|
| Full-range | `snapclient -> ALSA/DAC`; `/sound` points to the leader | local sources + local `/sound` -> outputd/CamillaDSP -> ALSA/DAC |
| Active crossover | `snapclient -> local driver DSP -> DACs/amps`; `/sound` points to the leader; `/crossover` is local | local sources + local `/sound` + local driver DSP -> DACs/amps; `/crossover` is local |

What is NEVER installed on a satellite endpoint role: wake word, mic/AEC,
LLM connections, TTS generation, renderers, local source selection, and
the Rust toolchain that builds them. Local CamillaDSP is allowed only
when the output topology requires driver crossover/protection; it must
not become room/content DSP. The grouping reconciler's role plan treats
absent units as no-ops, so one role engine serves both full speakers and
small endpoints.

**DSP split (see HANDOFF-multiroom §7.5):** content DSP (room
correction, sound prefs) is leader-side for satellites, baked into the
stream. Driver DSP (active crossover, driver protection) must live on
the box driving the DAC; the future active-crossover capability adds the
prebuilt camilladsp binary plus the local driver-DSP path. Crossover
biquads are trivial CPU on a Zero 2 W; the practical risk is dual-DAC
USB power on the OTG port, so that remains a hardware-validation item.
Until that increment exists, active-crossover hardware cannot be a
satellite endpoint: outputd's round-trip lane fail-closes on the
dual-Apple sink by design.

Per-endpoint level trim (the heterogeneous-pair calibration) uses
snapcast's per-client volume on this tier — a software multiply in
snapclient with an invertible taper, persisted in the leader's
server.json — rather than the outputd `dac_content` gain the full tier
will use. Same leader-side calibration flow, different apply knob.

Endpoint timing trim is a separate axis from level trim. Snapcast's
sync loop keeps endpoints on the leader's playout clock; Snapcast
client latency (`Client.SetLatency` / `snapclient --latency`) is for
fixed whole-endpoint PCM/DAC/backend latency; leader-side CamillaDSP
`Delay` is for rendered-channel acoustic arrival at the listening seat.
Do not let a Zero-class endpoint invent local dynamic timing policy for
a stereo pair or sub group. The leader measures the group and decides
which persistent knob owns the correction: endpoint-path baseline in
Snapcast latency, room/pair arrival delta in the leader render graph.
The source notes live in
[research/balance-sync-calibration.md](research/balance-sync-calibration.md).

## Product contract

The endpoint is "dumb" in product logic, not in operational quality. Its
fundamental competencies are:

- **Identity:** stable hostname, stable device identity, software version,
  role assignment, and DAC identity.
- **Connectivity:** join Wi-Fi, recover from leader/network loss, and avoid
  treating mDNS as the only truth when an IP or pinned peer identity is
  available.
- **Timed rendering:** consume the leader's synchronized stream, maintain
  the leader-defined latency budget, and play only its assigned role.
- **Audio safety:** select the intended DAC, start quiet, mute on transport
  starvation, and avoid surprise gain or hidden DSP.
- **Recoverability:** restart `snapclient` or `jasper-control` without
  human intervention, but fail silent rather than free-running.
- **Observability:** report enough state for the leader to say `healthy`,
  `degraded`, or `offline`.

That contract is intentionally smaller than a full JTS node. It is also
larger than "install `snapclient` and hope", because a product endpoint
must explain failures and recover predictably.

## Audio quality and buffer policy

Use one leader-defined timing policy for every strict playback set. Stereo
pairs and 2.1 groups should be treated as one synchronization object, not
as independent clients that adapt their own buffers.

Buffer guidance:

- Use fixed group latency for `left`/`right`/`sub` sets.
- If Wi-Fi is fragile, raise the latency budget for the whole group rather
  than letting each endpoint independently grow or shrink its playout
  buffer.
- Reserve aggressive rebuffer/resync for startup, join, format changes, or
  obvious network faults.
- Prefer slow clock correction and measured resync over continuous
  per-endpoint jitter improvisation.
- Surface buffer pressure as `degraded` before audible failure when
  possible.

For a wireless subwoofer, the delay must remain stable after calibration.
A few milliseconds may not sound like an echo, but around the crossover it
can move phase enough to change bass summation. For stereo pairs, relative
left/right drift can move the image. Treat both as stricter than ordinary
multi-room playback.

## Resiliency contract

The product endpoint should boot into a quiet, recoverable state:

- `snapclient` runs under a JTS-owned systemd unit, not the distro default
  service.
- The unit has bounded restart behavior and logs leader, stream, DAC, and
  ALSA failures.
- `jasper-control` (the same daemon every JTS member runs) owns policy
  and health reporting; systemd owns process lifetime.
- DAC disappearance means mute, mark degraded/offline, retry, and resume
  only after the intended DAC is present again.
- Leader disappearance means silence and reconnect, not autonomous local
  playback.
- Repeated restart storms should trip a visible degraded/offline state
  instead of hiding inside journald.

Wi-Fi posture is part of the product contract. Disable or tune Wi-Fi power
save only after measuring the actual Zero 2 W behavior, then document the
setting in the endpoint image recipe.

## Observability contract

At minimum, the leader should be able to see for each endpoint:

- identity, hostname/IP, software version, role, and assigned leader;
- `snapclient` service state and current stream;
- DAC identity and ALSA device path;
- audio format, buffer/latency setting, and channel role;
- reconnects, stream starvation, underruns/XRUNs, and last fault time;
- Wi-Fi RSSI/link snapshot when cheap to collect;
- endpoint uptime, restart count, and last boot reason when available.

Keep endpoint logs event-shaped and sparse. Useful events include
`endpoint.leader_unreachable`, `endpoint.snapclient_restarted`,
`endpoint.dac_missing`, `endpoint.dac_recovered`,
`endpoint.stream_starved`, and `endpoint.role_changed`.

## Install Boundary — one package, multiple roles

Endpoint code lives in the same `jasper` package and the same installer;
the boundary is an **install role**, not a package. The live installer
supports `full`, `streambox`, and the satellite role via the persisted
`endpoint` compatibility marker. Active crossover is a topology
capability layered on a role, not its own role.
Target shape:

- **pyproject extras split**: the base install carries what
  `jasper-control` + the multiroom plumbing import. The `[streambox]`
  extra carries local-renderer/DSP dependencies such as CamillaDSP,
  PortAudio/sounddevice, scipy/numpy, spotipy, dbus-next, and zeroconf.
  The heavy voice/wake/assistant dependencies (`onnxruntime`,
  `openwakeword`, `jasper_aec3`, voice SDKs) stay behind the `[full]`
  extra that only the full tier installs.
- **`install.sh` role gating** (e.g. `JASPER_INSTALL_PROFILE=endpoint`):
  the satellite endpoint path skips the shairport/nqptp source builds, the
  webrtc-AEC3 build, both Rust daemon builds, renderer/web/voice unit
  installation, and installs the core Python package, the managed
  snapclient unit, and the ALSA userland bits. `deploy/install.sh --dry-run`
  must show a brain-free plan under the endpoint profile. The
  `streambox` profile has its own dry-run text and tests: it installs
  renderers, fan-in/outputd, CamillaDSP, source web, sound web, and
  streambox nginx, while omitting voice/wake/mic/AEC. Streambox also
  installs profile-scoped `jasper-web` service/socket templates under the
  normal runtime unit names, so the shared landing page and local-audio
  wizards reuse the same code without binding full-brain wizard ports or
  sourcing assistant-only env files. A fresh Zero 2 W with no persisted
  marker defaults to `streambox`; a legacy unpaired Zero with a persisted
  `endpoint` marker is also upgraded to `streambox`. The satellite-only
  marker still wins when the box is already an active bonded follower, or
  when the operator explicitly requests `endpoint` / `satellite`.
- **Same deploy path**: `scripts/deploy-to-pi.sh` works when
  `JASPER_INSTALL_PROFILE=endpoint` or `JASPER_INSTALL_PROFILE=streambox`
  is set, or when the persisted profile marker already says one of
  those profiles. The earlier "never run deploy-to-pi.sh on the Zero"
  rule applied to the FULL install and is retired with this decision;
  the identity/direction guards apply unchanged.
- **Managed snapclient**: the same `jasper-snapclient.service` +
  reconciler-derived argv the full tier already uses (leader address,
  stream, the round-trip player on the full tier vs direct ALSA here —
  see "Open question: outputd on endpoints" below).
- **Audio-slice memory protection**: endpoint installs still install
  `jts-audio.slice`, because `snapclient` is the real-time audio path.
  The endpoint profile therefore runs the memory-resilience and cgroup
  migrations too: vm sysctls, MGLRU, zram sizing, SSH recovery bias, and
  memory-cgroup boot args. A reboot may be required after first install
  before `jasper-doctor` reports `cgroup memory` as healthy.

Three guard tests make the boundary loud without package walls — these
are **load-bearing**, not optional polish; the package-wall design was
rejected on the strength of them:

- **Import-cost guard**: the modules the endpoint tier runs
  (`jasper.control.server`, `jasper.multiroom.*`) import successfully
  with the brain-only dependencies blocked — a new top-level import of
  a heavy dependency fails CI, not the Zero. Mechanically: a subprocess
  import with a meta-path blocker that raises on the blocklist, since
  CI machines have everything installed. Starting blocklist (extend as
  found, never shrink silently): `onnxruntime`, `openwakeword`,
  `scipy`, `sounddevice` (imports the PortAudio C library at import
  time — the classic way this breaks), `jasper_aec3` (a compiled
  module that will not exist on the endpoint), and the voice SDKs
  (`google.genai`, `openai`, `websockets`), plus renderer/discovery
  extras such as `spotipy`, `zeroconf`, `evdev`, and `dbus_next`.
- **Install-plan guard**: the endpoint profile's `--dry-run` plan
  contains no cargo builds, no AEC3 build, no renderer source builds,
  and no voice/web units. The streambox profile's dry-run plan contains
  the local renderer/DSP graph and explicitly excludes voice/wake/mic/AEC.
  Extend the existing dry-run plan test surface
  (`tests/test_install_plan_covers_main.py` is the pattern).
- **Full-profile regression guard**: with the profile env unset, the
  `--dry-run` plan is byte-identical to today's — the endpoint tier
  must be impossible to detect from a normal speaker's install. This
  is its own test, not a clause of the previous one: it is the guard
  that lets endpoint work merge continuously without re-validating
  every full speaker.
- **Role-conversion guard**: a streambox-to-satellite conversion is
  allowed only with explicit operator intent. The install profile marker
  should refuse implicit tier changes; the conversion path must stop and
  disable local renderer services before writing grouping follower state.

**Open question: outputd on endpoints.** The full tier plays bonded
audio through `snapclient → FIFO → jasper-outputd` (which carries the
calibration-trim hook, serving-FIFO health, and silence-fill). The
endpoint tier starts with `snapclient → ALSA` directly — no Rust on the
Zero — accepting snapcast client volume as the trim knob and snapclient
restart policy as the resilience story. If endpoint-side outputd ever
earns its keep (uniform health/trim), it arrives as prebuilt artifacts,
never as an on-Zero cargo build.

**Validation gate: streambox role.** The streambox software profile is
built: AirPlay / Spotify Connect / Bluetooth / USB Audio Input, local
volume/source UI, `/sound`, correction/balance/sync surfaces, no voice,
no wake, no mic/AEC, no grouping leader. The remaining risk is not the
product model; it is whether shairport-sync, nqptp, librespot,
mux/output routing, CamillaDSP, USB gadget mode, a USB DAC on the same
Zero-class hardware, and 2.4 GHz Wi-Fi stay reliable inside 512 MB.
Treat it as a measured role, not as a partial full install. The USB
source is allowed on JTS4-style hardware for powered-splitter validation;
the hardware result decides whether it remains part of the Zero-class
streambox default. The coupling owner runs so that USB DIRECT can arm, but its
separate install-profile gate keeps fan-in↔Camilla on `loopback`: installed ring
assets are not evidence that P4 is safe on Zero-class hardware.

**Open question: active-crossover topology.** Any role that drives
woofer/tweeter amps needs local driver DSP. That topology should install
a prebuilt CamillaDSP binary and a small `/crossover` surface, then feed
the local program through the driver graph. On a satellite, Snapclient
feeds that local graph and room/content DSP remains on the leader. On a
streambox, local sources may also use `/sound` for content EQ before the
driver graph.

## Implementation rails

Hard-won specifics for whoever implements the phases — each of these is
a place the work will otherwise drift or wedge:

- **Tier is install-time; role is runtime. Never conflate them.** The
  grouping vocabulary stays exactly `{leader, follower}` — do NOT add
  an `endpoint` role to `grouping.env`, `validate_grouping`, or the
  bond fan-out. The normal Zero tier is `streambox`; when it becomes a
  bonded follower, grouping lands the role and synchronously hands it to the
  canonical source coordinator, which parks local source renderers; the
  shared UI hides/redirects leader-owned controls. The
  satellite-only `endpoint` tier is reserved for the deliberately tiny
  follower image, where stop/start intents for never-installed units are
  no-ops. If an endpoint-tier member is accidentally configured as
  `role=leader`, the reconciler must fail closed: clear snapcast args,
  stop both snap units, return nonzero, and let `jasper-doctor` warn
  that the member must be reassigned as a follower.
- **Persist the install profile on the Pi, but default unpaired Zeros to
  streambox.** `install.sh` re-runs on every deploy; if the profile lives
  only in the invoking shell's env, the next bare `deploy-to-pi.sh` could
  pick the wrong tier. First install writes the profile to a root-owned
  marker (e.g. `/var/lib/jasper/install_profile`). Later runs still
  refuse surprising full/streambox/endpoint changes, except for the
  intentional legacy migration: an unpaired Zero with a persisted
  `endpoint` marker upgrades to `streambox`; an active bonded follower or
  explicit `JASPER_INSTALL_PROFILE=endpoint` stays satellite-only.
- **Pairing is not a package conversion.** A streambox that joins a pair
  should not reinstall itself as `endpoint`; it should accept
  `role=follower` grouping state, park local AirPlay/Spotify/Bluetooth/
  USB input through the source coordinator, and expose pair volume/health rather
  than advertising independent sender targets in the same room. Unpairing
  removes that runtime state, so the streambox source and DSP surfaces
  return without another install.
- **Deploy verification must probe what the tier has.** Satellite-only
  endpoint installs include the shared capability-gated landing page plus
  endpoint-scoped nginx routes for `/system/` and `/sources/`; streambox
  installs include that same shared landing page plus `/spotify/`,
  `/sources/`, `/sound/`, and `/system/`. Deploy must probe the relevant
  pages through nginx with the real Host header and also check
  `jasper-control`'s always-on `:8780/healthz`. Do not "fix" a failing
  verification by skipping verification.
- **`/rooms` discovery needs the avahi advert.** Member discovery
  rides the `_jasper-control._tcp` service file that `install.sh`
  installs — the endpoint profile must keep avahi + that advert (and
  the identity reconciler that feeds the friendly name), or Phase 3
  silently finds nothing. "Skip the web stuff" must not catch it.
- **Doctor ships on the endpoint, tier-aware.** `jasper-doctor` is the
  operator story and must run there, but most checks probe components
  that were never installed. Reuse the parked-by-role skip idiom from
  the dumb-follower work, keyed off the persisted install profile —
  "not installed (endpoint tier)" is an `ok` detail, not a warn. Do
  not fork a second doctor. Keep endpoint-local checks for the things
  the endpoint actually owns: `snapclient` binary/service readiness,
  the ALSA playback device, memory/cgroup protection for `jts-audio.slice`,
  grouping reachability, and obvious misconfiguration such as a
  channel-specific bond still using the default stereo ALSA player.
- **One reconciler, one argv builder.** The managed snapclient unit and
  its argv come from the existing grouping reconciler
  (`snapclient_argv`), not an endpoint-specific service or wrapper.
  If the endpoint plays `snapclient → ALSA` direct (no outputd), that
  is a *player argument* derived from the tier, not a new code path
  beside the reconciler.
- **Endpoint web is profile-scoped, not forked.** Satellite-only serves
  the shared JTS landing page filtered by `system_capabilities`, with a
  deliberately small nginx route set for `/system` and `/sources` because
  its renderer units are intentionally absent. Streambox exposes the same
  shared landing page and the combined `jasper-web` bundle filtered by
  install role:
  `/spotify/`, `/airplay/`, `/sources/`, `/sound/`, `/speaker/`, `/wifi/`,
  and `/rooms/` are live; voice/wake/assistant-only cards are
  hidden by the shared `system_capabilities` payload and their nginx
  routes are absent. Pair-management is also capability-gated: streambox
  keeps `/rooms/`, while satellite-only hides the card and omits the route.
  `/sources` remains a capability surface: source rows
  whose renderer units are absent from the install profile stay disabled
  and say why. A satellite with `active_crossover` topology may later
  expose `/crossover`; streambox already exposes local `/sound`.
- **Work from `main`.** The superseded `codex/dumb-endpoint-plan`
  branch predates several multiroom increments; nothing on it should
  be cherry-picked except by re-reading this doc.

## Working build plan

This is the plan of record for the dumb endpoint. Keep it scoped to the
endpoint role; cross-speaker synchronized-playback architecture stays in
[`HANDOFF-multiroom.md`](HANDOFF-multiroom.md).

### Phase 0 — Lab endpoint

Goal: prove the Zero 2 W, Wi-Fi, Snapclient, and DAC path before product
code exists.

Done / current:

- Flash Raspberry Pi OS Lite.
- Install `alsa-utils`, `snapclient`, and `sox`.
- Disable the distro `snapclient.service`.
- SSH to the device by IP when `jts4.local` mDNS is flaky.
- 2026-06-12 hardware verification on `jts4.local`: endpoint profile
  deployed, `snapclient v0.31.0` installed, `jasper-control` answered
  `:8780/healthz`, `jasper-doctor` reported `0 failed` / `2 warnings`
  (expected low-RAM warning on the Zero 2 W and missing Wi-Fi guardian
  stash), and the Apple USB-C to 3.5 mm adapter enumerated as ALSA card
  `A` / USB `05ac:110a`. The Wi-Fi guardian gap from that first run is
  now covered by the endpoint install calling the same active-profile
  stash migration as the full install.

Exit criteria:

- DAC enumerates with `aplay -l`.
- Quiet local `speaker-test` works.
- The multi-room spike can start/stop transient Snapcast units without
  fighting the distro service.

### Phase 1 — Dumb-follower role profile (shared foundation)

Goal: build the role machinery on the FULL tier first — it is the same
engine the endpoint tier reuses, and it ships household value on its own
(a bonded Pi 5 follower frees ~370 MB and stops advertising sources it
cannot honestly play).

This phase is the dumb-follower increment tracked in
[`HANDOFF-multiroom.md`](HANDOFF-multiroom.md): grouping lands
role=follower, then the canonical source coordinator parks the renderer stack
(shairport-sync, nqptp, librespot, bluealsa-aplay, bt-agent, mux, usbsink) —
effective stop, never household-intent loss; `/sources/` keeps the desired
choice and the same coordinator restores allowed sources after unpair.
`jasper-aec-reconcile` (already the single
owner of voice/bridge unit state, with an existing park concept) gains
one new park condition: bonded-active-follower. Doctor, dashboard, and
`/state` surfaces become role-aware ("parked (bonded follower)" is
surfaced state, not a silent failure).

Exit criteria:

- A bonded Pi 5 follower runs only the basics; unbond restores
  everything; reboot-while-bonded re-parks within seconds.
- Doctor is clean on both roles; no supervisor resurrects a parked unit.

### Phase 2 — Small install tiers

Goal: make a Zero 2 W installable as either a JTS satellite member or a
standalone streambox without ever building or installing the brain. The
satellite endpoint path is hardware-verified on JTS4; the streambox
software path is implemented and still needs real Zero 2 W validation.

Build:

- pyproject extras split (`[full]` heavy deps out of the base install).
- `install.sh` endpoint profile (skip AEC3/Rust/renderer/full-web/voice
  builds and units; install core Python + managed snapclient + ALSA +
  the shared JTS landing page plus endpoint-scoped nginx routes for
  `/system` and `/sources`).
- `install.sh` streambox profile (install renderer/DSP stack, fan-in,
  outputd, CamillaDSP, local source web, `/sound`, streambox nginx, and
  the shared JTS landing page filtered by system capabilities; omit
  voice/wake/mic/AEC/assistant/CamillaGUI).
- `scripts/deploy-to-pi.sh` forwards the endpoint or streambox profile
  and verifies the installed nginx surface (`/`, `/system/data.json`,
  `/sources/state`, plus `/sound/` and `/spotify/` for streambox) and
  `jasper-control` directly at `:8780/healthz`.
- `scripts/onboard.sh <host> --adopt` followed by
  `JASPER_INSTALL_PROFILE=streambox bash scripts/deploy-to-pi.sh` is the
  preferred fresh-Zero command pair. Onboard handles the mDNS/IP split
  and laptop state; deploy installs, reboots once for staged cgroup/zram
  boot changes, and verifies the management surface.
- The endpoint install path seeds the Wi-Fi guardian stash from the
  active NetworkManager profile when possible, matching the full
  speaker recovery contract without requiring the `/wifi/` wizard.
- Reconciler derives the Snapclient player from the install tier:
  full speakers and streamboxes use the outputd FIFO lane; satellite-only
  endpoints use direct ALSA (`alsa:device=default` by default).
- Boundary guards: import-cost test, install-plan test, base-dependency
  test, deploy-verification test, streambox nginx/profile tests,
  endpoint-leader fail-closed test, endpoint systemd render validation,
  and full-profile dry-run regression.

Exit criteria:

- A fresh Zero defaults to the streambox profile when it is not already
  bonded as a satellite; an explicit endpoint/satellite-only install
  still completes in minutes, with no cargo, no AEC3 compile, and no
  voice/wake dependencies on disk.
- All three boundary guards are in CI: import-cost, install-plan, and
  the full-profile byte-identical regression guard.
- The profile is persisted on the device and a bare re-deploy cannot
  silently switch tiers.
- Hardware run proves the endpoint starts cleanly, reports through
  `_jasper-control._tcp`, and starts/stops JTS-managed `snapclient`
  from the grouping reconciler.
- Hardware run proves streambox starts cleanly, keeps source renderers
  stable, serves shared capability-gated UI, and has acceptable idle
  memory on the Zero 2 W.

### Phase 3 — Endpoint joins the pair like any speaker

Goal: zero bespoke leader integration — the endpoint appears in
`/rooms`, bonds, swaps, and reports health through the machinery brainy
members already use.

Build:

- Verify the bond flow end-to-end against an endpoint-tier member
  (`/rooms` discovery via the always-on `_jasper-control._tcp` advert,
  `POST /grouping/set`, the grouping reconciler bringing up the managed
  snapclient with the assigned channel).
- Endpoint-tier grouping runtime health: the derive reports snapclient
  state + stream binding truth (already member-local); the leader's
  `pair channels` doctor probe works unchanged.
- Per-endpoint trim via snapcast `Client.SetVolume` (leader-side knob,
  invertible taper) wired into the calibration flow.
- Volume forward: the endpoint's `/volume*` already forwards to the
  leader (jasper-control is the same daemon) — verify, don't build.

Exit criteria:

- A household can pair "JTS + jts4" from `/rooms` with no SSH and no
  endpoint-specific UI; health/degraded/offline reads correctly on
  `/rooms`, `/state`, and the doctor on both ends.

### Phase 4 — Audio-quality and resilience validation

Goal: prove the design on real hardware before treating it as product.

Measure:

- Wi-Fi fragility and recovery.
- DAC hotplug / wrong-DAC behavior.
- Long-run drift and underruns.
- Stereo pair electrical/acoustic alignment.
- Wireless sub crossover-region stability.
- Restart storms and leader reboot behavior.

Exit criteria:

- The endpoint fails silent when it cannot play correctly.
- The leader can surface useful operator guidance for every common failure.
- For strict groups, fragile Wi-Fi changes the whole group's latency policy
  or triggers a deliberate resync; endpoints do not independently drift their
  buffers.

## Hardware

Use a Raspberry Pi Zero 2 W, not a Raspberry Pi Pico 2 W. The Pico is a
microcontroller board and does not follow the Raspberry Pi OS Lite +
microSD + `snapclient` path.

Minimum hardware:

- Raspberry Pi Zero 2 W.
- microSD card with Raspberry Pi OS Lite.
- Power into the Zero's `PWR IN` micro-USB port.
- DAC on the other micro-USB `USB` / OTG data port, through an OTG
  adapter or powered hub.
- Analog load on the DAC output before probing audio.

For the Apple USB-C to 3.5 mm adapter, plug headphones, an amp input, or
another analog load into the 3.5 mm jack before checking ALSA. The
adapter may not expose its USB Audio interface until the analog side is
loaded.

A powered OTG hub is the least surprising setup when using the Apple
adapter. A passive micro-USB OTG to USB-C adapter may work, but power
budget and cable direction can be fussy.

## Flash

Use Raspberry Pi Imager:

1. Device: `Raspberry Pi Zero 2 W`.
2. OS: `Raspberry Pi OS Lite`.
3. Hostname: choose a stable speaker name, for example `jts4`.
4. Username: `pi`.
5. Enable SSH.
6. Use password authentication for the first bring-up.
7. Configure the same Wi-Fi network as the brainy JTS speaker and the
   laptop.

After boot, SSH by mDNS when it works:

```sh
ssh pi@jts4.local
```

If `.local` resolution is flaky, find the IP from the router, ARP table,
or a prior SSH probe and use the IP directly:

```sh
ssh pi@192.168.1.162
```

## Install endpoint packages

This section is the **manual lab fallback**. The preferred path for a
JTS-managed box is the streambox deploy:

```sh
bash scripts/onboard.sh jts4.local --adopt
PI_HOST=jts4.local JASPER_INSTALL_PROFILE=streambox bash scripts/deploy-to-pi.sh
```

The streambox profile also installs `nginx-light` for the local
management surface. The raw manual lab fallback only needs the Snapcast
client and basic audio tools:

```sh
sudo apt update
sudo apt install -y alsa-utils snapclient sox
```

`snapserver` is not required on a dumb endpoint. It runs on the leader in
the product design and in the spike harness.

The Debian/Raspberry Pi package enables and starts the stock
`snapclient.service` when installed. For this lab endpoint, leave the
binary installed but stop the distro service; the spike harness starts its
own transient `jts-spike-client-*` units and should not contend with a
default client trying to play to the DAC:

```sh
sudo systemctl disable --now snapclient.service
systemctl is-enabled snapclient.service 2>/dev/null || true
systemctl is-active snapclient.service 2>/dev/null || true
snapclient --version
sox --version
```

Expected state after this step: `snapclient.service` is `disabled` and
`inactive`, while `/usr/bin/snapclient` and `/usr/bin/sox` exist.

## Verify DAC enumeration

With the DAC connected to the Zero's USB OTG/data port and an analog load
plugged into the DAC:

```sh
aplay -l
```

Expected result: an ALSA playback card for the USB DAC. Note the card
number or card name.

If no DAC appears:

- Confirm the adapter is on the Zero's `USB` data port, not `PWR IN`.
- Add an analog load to the Apple dongle's 3.5 mm jack.
- Try a powered OTG hub.
- Replug the adapter and check `dmesg -T` for USB enumeration errors.

## Verify quiet local playback

Start quiet. This direct test bypasses all JTS safety clamps.

If the DAC is card `1`:

```sh
speaker-test -D plughw:1,0 -t sine -f 80 -c 2 -l 1
```

Use a low level on headphones or the amp. For a subwoofer path, an 80 Hz
sine is a useful first sanity check. For a full-range endpoint, use a
short low-level stereo test before connecting a power amp.

## Run the multi-room spike

From the laptop checkout, use the throwaway harness in
[`scripts/multiroom-spike.sh`](../scripts/multiroom-spike.sh). It runs
transient `jts-spike-*` systemd units and does not modify the product JTS
audio path.

For a Zero used as a sub endpoint:

```sh
bash scripts/multiroom-spike.sh --setup --sub jts4.local --apt-install
bash scripts/multiroom-spike.sh --sweep
bash scripts/multiroom-spike.sh --teardown
```

If mDNS is unreliable, use the IP:

```sh
bash scripts/multiroom-spike.sh --setup --sub 192.168.1.162 --apt-install
```

The spike bypasses CamillaDSP and the JTS volume/safety ceilings. Keep the
amp or headphone level conservative and tear down the spike when finished.

## What product support still needs

Installing `snapclient` is enough for the current lab endpoint. It is
not the complete product. The durable path (phases above) still needs:

- Live hardware validation of the streambox install profile on the real
  Zero 2 W: first-run build/install time, idle memory, source toggles,
  outputd/CamillaDSP stability, and 2.4 GHz reliability.
- A product role-conversion flow between streambox and satellite. Today
  the install profile guard intentionally requires explicit operator
  intent for tier changes.
- Product channel selection for `left`, `right`, `mono`, and `sub`
  beyond the current direct Snapclient player.
- A stable leader identity/address rule, with IP fallback for flaky
  mDNS and a path toward peer-id pinning before this becomes
  user-facing.
- Analog ceiling calibration for the endpoint amp/DAC path, because an
  endpoint has no local CamillaDSP safety floor — start-quiet discipline
  plus a documented amp ceiling are the substitute.
- Mute-on-underrun behavior and visible degraded state when the leader
  or network stream is unavailable.
- Per-endpoint level trim via snapcast client volume, fed by the
  phone-mic SPL calibration flow.
- For stereo pairs: acoustic sync confirmation, with fixed endpoint-path
  latency separated from listening-seat acoustic delay, and (full tier
  only) per-side correction baked by the leader.
- For 2.1: a single synchronized multi-channel stream, not a separate
  sub stream.
- Real hardware measurements for Wi-Fi fragility, DAC hotplug, long-run
  clock drift, underruns, and stereo/sub acoustic alignment (Phase 4).

Until those land, treat the Zero 2 W roles as measured lab/productization
paths, not as shippable wireless-speaker defaults.

Last verified: 2026-07-14 (current streambox source-role handoff, fresh-install
mux startup, and loopback-versus-USB-DIRECT profile boundary rechecked; the
historical bring-up narrative below remains preserved.)
