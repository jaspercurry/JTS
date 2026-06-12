# Dumb endpoint bring-up: Raspberry Pi Zero 2 W

This is the operator runbook for bringing up a cheap JTS multi-room
endpoint such as `jts4`: a Raspberry Pi Zero 2 W that will eventually
play one synchronized channel for a stereo pair, wireless subwoofer, or
multi-room group.

It is **not** the full JTS speaker bring-up. The architecture lives in
[`HANDOFF-multiroom.md`](HANDOFF-multiroom.md); this file owns the
practical hardware/software path, the endpoint install tier, and the
working build plan for a dumb endpoint.

## Current truth

Today, a dumb endpoint is a lab/spike target:

- Raspberry Pi OS Lite on a Zero 2 W.
- Wi-Fi + SSH.
- `alsa-utils`, `snapclient`, and `sox`.
- A DAC that ALSA can see, such as an Apple USB-C to 3.5 mm adapter on
  the Zero's USB OTG/data port.

Until the endpoint install profile (Phase 2 below) lands, do **not**
run `scripts/onboard.sh`, `scripts/deploy-to-pi.sh`, or the full JTS
install on the Zero 2 W — the FULL install builds AEC3 and two Rust
daemons, which a 512 MB Zero cannot do comfortably. The decided product
path (see the decision section) makes the profile-gated deploy the
supported route; an endpoint never runs `jasper-voice`, CamillaDSP,
renderers, web wizards, or AEC regardless. The brainy JTS speaker
remains the leader.

The product multi-room path is still in progress. The Zero can be used
now with [`scripts/multiroom-spike.sh`](../scripts/multiroom-spike.sh)
to prove Snapcast, Wi-Fi, and DAC behavior before product audio is wired
end-to-end.

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

The boundary the package walls defended is held instead by three cheap
guards: an import-cost test (the daemons the endpoint tier runs must be
importable without the voice extras), an install-plan test (the endpoint
profile's plan contains no brain builds), and a full-profile regression
test (the default speaker plan stays byte-identical). A future "smart
endpoint" is a tier upgrade on the same install, not a migration.

What survives from the earlier draft unchanged: the product contract,
buffer policy, resiliency contract, observability contract, the lab
runbook, and the hardware-validation phase below — those were always
requirements on the *role*, not on a package.

## Software boundary

Think of this role as a transport appliance with a JTS control plane,
not a second brainy speaker. The leader owns the microphone path,
wake/AI session, DSP, grouping decisions, stream production, and
user-facing setup. The dumb endpoint owns booting, staying on the
network, answering the control plane (`/rooms` bond/swap/health, the
pair-volume forward), accepting the leader's synchronized stream,
selecting its assigned channel, and playing it to the local DAC safely.

The install tiers:

| Tier | Hardware | Installs | Runs |
|---|---|---|---|
| Full speaker | Pi 5 class | everything | role-derived: solo / leader / dumb follower (voice, AEC, renderers, mux parked while bonded) |
| Endpoint | Zero 2 W class | core profile only: `jasper-control` + the multiroom plumbing + a managed `snapclient` unit + ALSA bits | `jasper-control` + `snapclient`; everything else was never installed |

What is NEVER installed on the endpoint tier: wake word, mic/AEC, LLM
connections, TTS generation, renderers, CamillaDSP, jasper-outputd,
jasper-fanin, the web wizards, and the Rust toolchain that builds them.
The grouping reconciler's role plan treats absent units as no-ops, so
one role engine serves both tiers.

**DSP split (see HANDOFF-multiroom §7.5):** content DSP (room
correction, sound prefs) is leader-side, baked into the stream — an
endpoint never needs it locally. Driver DSP (active crossover, driver
protection) must live on the box driving the DAC; a future "crossover
endpoint" tier variant adds the prebuilt camilladsp binary + the
follower local driver-DSP path (crossover biquads are trivial CPU on a
Zero 2 W; the practical risk is dual-DAC USB power on the OTG port —
hardware-validation item). Until that increment exists, active-crossover
hardware cannot be an endpoint: outputd's round-trip lane fail-closes on
the dual-Apple sink by design.

Per-endpoint level trim (the heterogeneous-pair calibration) uses
snapcast's per-client volume on this tier — a software multiply in
snapclient with an invertible taper, persisted in the leader's
server.json — rather than the outputd `dac_content` gain the full tier
will use. Same leader-side calibration flow, different apply knob.

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

## Install boundary — one package, two profiles

Endpoint code lives in the same `jasper` package and the same installer;
the boundary is a **profile**, not a package. Target shape:

- **pyproject extras split**: the base install carries what
  `jasper-control` + the multiroom plumbing import; the heavy
  voice/wake/DSP dependencies (onnxruntime, openwakeword, scipy, the
  voice SDKs) move behind a `[voice]` extra that only the full tier
  installs.
- **`install.sh` profile gating** (e.g. `JASPER_INSTALL_PROFILE=endpoint`):
  the endpoint path skips the shairport/nqptp source builds, the
  webrtc-AEC3 build, both Rust daemon builds, renderer/web/voice unit
  installation, and installs the core Python package, the managed
  snapclient unit, and the ALSA bits. `deploy/install.sh --dry-run`
  must show a brain-free plan under the endpoint profile.
- **Same deploy path**: `scripts/deploy-to-pi.sh` against an endpoint
  works once the profile is honored end-to-end (the earlier "never run
  deploy-to-pi.sh on the Zero" rule applied to the FULL install and is
  retired with this decision; the identity/direction guards apply
  unchanged).
- **Managed snapclient**: the same `jasper-snapclient.service` +
  reconciler-derived argv the full tier already uses (leader address,
  stream, the round-trip player on the full tier vs direct ALSA here —
  see "Open question: outputd on endpoints" below).

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
  (`google.genai`, `openai`, `websockets`).
- **Install-plan guard**: the endpoint profile's `--dry-run` plan
  contains no cargo builds, no AEC3 build, no renderer source builds,
  and no voice/web units. Extend the existing dry-run plan test
  surface (`tests/test_install_plan_covers_main.py` is the pattern).
- **Full-profile regression guard**: with the profile env unset, the
  `--dry-run` plan is byte-identical to today's — the endpoint tier
  must be impossible to detect from a normal speaker's install. This
  is its own test, not a clause of the previous one: it is the guard
  that lets endpoint work merge continuously without re-validating
  every full speaker.

**Open question: outputd on endpoints.** The full tier plays bonded
audio through `snapclient → FIFO → jasper-outputd` (which carries the
calibration-trim hook, serving-FIFO health, and silence-fill). The
endpoint tier starts with `snapclient → ALSA` directly — no Rust on the
Zero — accepting snapcast client volume as the trim knob and snapclient
restart policy as the resilience story. If endpoint-side outputd ever
earns its keep (uniform health/trim), it arrives as prebuilt artifacts,
never as an on-Zero cargo build.

## Implementation rails

Hard-won specifics for whoever implements the phases — each of these is
a place the work will otherwise drift or wedge:

- **Tier is install-time; role is runtime. Never conflate them.** The
  grouping vocabulary stays exactly `{leader, follower}` — do NOT add
  an `endpoint` role to `grouping.env`, `validate_grouping`, or the
  bond fan-out. An endpoint is a *follower on the endpoint install
  tier*. The only runtime accommodation is the reconciler treating
  stop/start intents for never-installed units as no-ops.
- **Persist the install profile on the Pi, and never switch tiers
  implicitly.** `install.sh` re-runs on every deploy; if the profile
  lives only in the invoking shell's env, the next bare
  `deploy-to-pi.sh` full-installs onto the Zero (hours of compiles,
  OOM risk). First install writes the profile to a root-owned marker
  (e.g. `/var/lib/jasper/install_profile`); later runs read it and
  REFUSE a tier change without an explicit override flag — same
  posture as the deploy identity/downgrade guards.
- **Deploy verification must probe what the tier has.** The deploy's
  post-install check probes `/system/data.json` through nginx — the
  endpoint tier installs neither. Under the endpoint profile, verify
  against `jasper-control`'s `:8780/healthz` instead. Do not "fix" a
  failing verification by skipping verification.
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
  not fork a second doctor.
- **One reconciler, one argv builder.** The managed snapclient unit and
  its argv come from the existing grouping reconciler
  (`snapclient_argv`), not an endpoint-specific service or wrapper.
  If the endpoint plays `snapclient → ALSA` direct (no outputd), that
  is a *player argument* derived from the tier, not a new code path
  beside the reconciler.
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
[`HANDOFF-multiroom.md`](HANDOFF-multiroom.md): the grouping reconciler
parks the renderer stack (shairport-sync, nqptp, librespot,
bluealsa-aplay, bt-agent, mux, usbsink) on role=follower — stop, never
disable; `/sources/` keeps enable/disable as the household's intent and
restore is start-if-enabled. `jasper-aec-reconcile` (already the single
owner of voice/bridge unit state, with an existing park concept) gains
one new park condition: bonded-active-follower. Doctor, dashboard, and
`/state` surfaces become role-aware ("parked (bonded follower)" is
surfaced state, not a silent failure).

Exit criteria:

- A bonded Pi 5 follower runs only the basics; unbond restores
  everything; reboot-while-bonded re-parks within seconds.
- Doctor is clean on both roles; no supervisor resurrects a parked unit.

### Phase 2 — Endpoint install tier

Goal: make a Zero 2 W installable as a JTS member without ever building
or installing the brain.

Build:

- pyproject extras split (`[voice]` heavy deps out of the base install).
- `install.sh` endpoint profile (skip AEC3/Rust/renderer/web/voice
  builds and units; install core Python + managed snapclient + ALSA).
- Reconciler role plan tolerates absent units (stop-intent on a unit
  that was never installed is a no-op).
- The two boundary guards: import-cost test + install-plan test.

Exit criteria:

- A fresh Zero installs the endpoint profile in minutes, with no cargo,
  no AEC3 compile, and no voice/wake dependencies on disk.
- All three boundary guards are in CI: import-cost, install-plan, and
  the full-profile byte-identical regression guard.
- The profile is persisted on the device and a bare re-deploy cannot
  silently switch tiers.

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

The endpoint only needs the Snapcast client and basic audio tools:

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

- The dumb-follower role profile on the full tier (Phase 1 — the shared
  role engine).
- The endpoint install profile + extras split + boundary guard tests
  (Phase 2).
- Managed `snapclient` with channel selection for `left`, `right`,
  `mono`, and `sub` derived by the same reconciler argv builder the
  full tier uses.
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
- For stereo pairs: acoustic sync confirmation and (full tier only)
  per-side correction baked by the leader.
- For 2.1: a single synchronized multi-channel stream, not a separate
  sub stream.
- Real hardware measurements for Wi-Fi fragility, DAC hotplug, long-run
  clock drift, underruns, and stereo/sub acoustic alignment (Phase 4).

Until those land, treat the Zero 2 W as a measured lab endpoint, not as
a shippable wireless speaker.

Last verified: 2026-06-12
