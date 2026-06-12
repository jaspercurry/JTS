# Dumb endpoint bring-up: Raspberry Pi Zero 2 W

This is the operator runbook for bringing up a cheap JTS multi-room
endpoint such as `jts4`: a Raspberry Pi Zero 2 W that will eventually
play one synchronized channel for a stereo pair, wireless subwoofer, or
multi-room group.

It is **not** the full JTS speaker bring-up. The architecture lives in
[`HANDOFF-multiroom.md`](HANDOFF-multiroom.md); this file owns the
practical hardware/software path, endpoint code/install boundary, and
working build plan for a dumb endpoint.

## Current truth

Today, a dumb endpoint is a lab/spike target:

- Raspberry Pi OS Lite on a Zero 2 W.
- Wi-Fi + SSH.
- `alsa-utils`, `snapclient`, and `sox`.
- A DAC that ALSA can see, such as an Apple USB-C to 3.5 mm adapter on
  the Zero's USB OTG/data port.

Do **not** run `scripts/onboard.sh`, `scripts/deploy-to-pi.sh`, or the
full JTS install on the Zero 2 W for this role. A dumb endpoint does not
run `jasper-voice`, CamillaDSP, renderers, web wizards, AEC, or the full
daemon stack. The brainy JTS speaker remains the leader.

The product multi-room path is still in progress. The Zero can be used
now with [`scripts/multiroom-spike.sh`](../scripts/multiroom-spike.sh)
to prove Snapcast, Wi-Fi, and DAC behavior before product audio is wired
end-to-end.

## Software boundary

Think of this role as a transport appliance, not a second JTS speaker.
The leader owns the microphone path, wake/AI session, DSP, grouping
decisions, stream production, and user-facing setup. The dumb endpoint
only owns booting, staying on the network, accepting the leader's
synchronized audio stream, selecting its assigned channel, and playing it
to the local DAC safely.

Recent multi-room work reinforces that split. The PR 625 TTS/outputd work
makes assistant speech member-local on brainy bonded JTS speakers and adds
leader-side self-healing for Snapcast group bindings. That is not a reason
to install `jasper-voice`, `jasper-outputd`, `jasper-control`, CamillaDSP,
or the web wizards on a Zero 2 W dumb endpoint. It means the leader and
brainy members keep getting smarter so the endpoint can stay boring.

The eventual endpoint image should stay small:

- Raspberry Pi OS Lite, SSH, Wi-Fi, Avahi/mDNS, and stable hostname setup.
- Network posture for long-lived playback: stable DHCP/hostname identity,
  mDNS as convenience not sole control-plane truth, and an explicit Wi-Fi
  power-save policy after measurements.
- ALSA tools and USB DAC support.
- `snapclient` pointed at the leader's Snapcast stream.
- A tiny JTS-owned service/config layer, or equivalent image recipe, that
  pins the leader address, assigned channel, codec/buffer settings, local
  DAC device, restart policy, and health reporting.
- Systemd watchdog/restart behavior and logs for "connected", "playing",
  "underrun", "wrong DAC", and "leader unreachable".

What should stay off the endpoint: wake word, microphone/AEC, LLM
connections, TTS generation, renderers, CamillaDSP, the management UI, and
full JTS deploy/onboard machinery.

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
- **Recoverability:** restart `snapclient` or the endpoint agent without
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
- The endpoint agent, if present, owns policy and health; systemd owns
  process lifetime.
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

## Repo and install boundary

Endpoint code belongs in this repository, but not in the full
`jasper-speaker` install. The brain and endpoint should be versioned,
reviewed, and tested together, while installing different runtime
packages on different machines.

Target shape:

```text
jasper/                 full JTS speaker/leader package
jts_contracts/          tiny stdlib-only shared wire contracts
jts_endpoint/           endpoint agent and endpoint-only helpers
deploy/endpoint/        endpoint image/install assets
deploy/systemd/         endpoint systemd units, named jts-endpoint-*
tests/test_endpoint_*   contract, dependency, and installer guards
```

The import rule is one-way:

- `jts_contracts` may import only the Python standard library.
- `jasper` may import `jts_contracts`.
- `jts_endpoint` may import `jts_contracts`.
- `jts_endpoint` must not import `jasper`.
- `jasper` must not import `jts_endpoint`.

That keeps shared protocol vocabulary in one place without making the
endpoint a second copy of the brain. The shared contract should hold only
versioned payloads and vocabulary: endpoint identity, role assignment,
health states, buffer policy, event names, status JSON, and compatibility
helpers. It should not open sockets, shell out, read ALSA, call systemd, or
know about Snapcast process management.

The endpoint install should have its own package metadata and install path,
for example `jts-endpoint` installed under `/opt/jts-endpoint`, with config
in `/etc/jts-endpoint` and state in `/var/lib/jts-endpoint`. The full
`deploy/install.sh` path for brainy speakers must not install or enable the
endpoint agent by default, and the endpoint installer must not install or
enable `jasper-voice`, `jasper-control`, `jasper-outputd`, CamillaDSP,
renderers, or web wizards.

Testing should make the boundary loud:

- Contract round-trip tests for every brain-to-endpoint and
  endpoint-to-brain payload.
- Compatibility tests for unknown enum values and older endpoint versions.
- Import-guard tests that fail if `jts_endpoint` imports `jasper` or if
  `jts_contracts` imports third-party packages.
- Packaging tests that fail if the endpoint package declares brain-only
  dependencies such as OpenAI SDKs, CamillaDSP, SciPy, ONNX Runtime, web
  assets, or voice/wake packages.
- Installer tests that prove the brain install excludes endpoint services
  and the endpoint install excludes brain services.
- Systemd unit tests for restart policy, watchdog policy, and distro
  `snapclient.service` conflicts.

The first build step should be contracts before behavior: add
`jts_contracts`, a skeletal `jts_endpoint` package, and the dependency/import
guards. Only after that should the endpoint agent start reading DAC,
Snapclient, Wi-Fi, and systemd state.

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

### Phase 1 — Shared contracts and guardrails

Goal: create the repo boundary before behavior grows.

Build:

- `jts_contracts/` with stdlib-only dataclasses/enums/helpers for endpoint
  roles, health states, buffer policy, status payloads, event names, and
  schema versions.
- Skeletal `jts_endpoint/` package with no hardware behavior yet.
- Endpoint-specific package metadata, separate from `jasper-speaker`.

Tests:

- Contract JSON round trips.
- Unknown/older-version compatibility.
- Import guard: `jts_contracts` imports stdlib only.
- Import guard: `jts_endpoint` does not import `jasper`.
- Dependency guard: endpoint package does not declare brain-only deps.

Exit criteria:

- A PR can change a shared brain/endpoint payload only by updating the
  contract tests.
- The endpoint package can be inspected without pulling in voice, DSP, web,
  or AI dependencies.

### Phase 2 — Endpoint installer and service skeleton

Goal: make the endpoint installable without installing the brain.

Build:

- `deploy/endpoint/` installer or image recipe.
- `/opt/jts-endpoint`, `/etc/jts-endpoint`, and `/var/lib/jts-endpoint`
  ownership.
- `jts-endpoint-agent.service` systemd unit, initially read-only / idle.
- JTS-owned `jts-endpoint-snapclient.service` template or wrapper.
- Guard that disables/conflicts with the distro `snapclient.service`.

Tests:

- Brain installer excludes endpoint services.
- Endpoint installer excludes brain services.
- Endpoint unit restart/watchdog policy is pinned.
- Package/install plan contains only endpoint dependencies.

Exit criteria:

- A fresh Zero can install the endpoint target without creating
  `jasper-voice`, `jasper-control`, `jasper-outputd`, CamillaDSP, renderers,
  or web wizard services.

### Phase 3 — Read-only endpoint agent

Goal: report facts without making policy decisions.

Build:

- Endpoint identity and software version reporting.
- DAC inventory from ALSA/USB.
- `snapclient` service state and current configured leader/stream.
- Wi-Fi RSSI/link snapshot when cheap to collect.
- Uptime, restart count, and recent fault/event reporting.
- Local status command for SSH diagnostics.

Tests:

- Parsers handle missing DAC, missing `snapclient`, malformed command
  output, and permission failures.
- Status payloads round-trip through `jts_contracts`.
- Logs use stable event names and avoid secrets.

Exit criteria:

- The leader, or an SSH operator, can classify the endpoint as `healthy`,
  `degraded`, or `offline` from read-only facts.

### Phase 4 — Managed Snapclient playback

Goal: let the endpoint play the leader stream under JTS-owned config.

Build:

- Endpoint config for leader address/identity, role, codec/buffer policy,
  DAC device, and channel selection.
- JTS-owned `snapclient` argv generation.
- Conservative restart/reconnect behavior.
- Mute/fail-silent behavior for leader loss, DAC loss, stream starvation,
  or repeated restarts.

Tests:

- Argv generation for `left`, `right`, `mono`, and `sub`.
- Buffer policy stays group-defined; endpoints do not independently invent
  latency.
- Missing DAC and missing leader produce `degraded`/`offline`, not a green
  status.
- Distro `snapclient.service` cannot contend with the managed unit.

Exit criteria:

- The endpoint reliably starts, stops, reconnects, and reports failures
  around a Snapcast stream without running any brain-side daemons.

### Phase 5 — Leader integration

Goal: make endpoints visible and controllable from the brain while keeping
policy on the leader.

Build:

- Leader-side endpoint registry/discovery, initially simple and explicit.
- Leader-side health rollup from endpoint status.
- Role assignment and buffer policy pushed from the leader.
- `/state` or doctor coverage showing endpoint `healthy` / `degraded` /
  `offline`.
- Manual operator path for reassigning an endpoint or replacing hardware.

Tests:

- Brain consumes endpoint status through `jts_contracts`, not ad hoc JSON.
- Unknown newer endpoint fields are ignored safely.
- Older endpoint versions fail soft with a compatibility warning.
- Leader policy changes update endpoint config without local endpoint
  autonomy.

Exit criteria:

- A brainy speaker can tell whether `jts4` is present, assigned, connected,
  playing the intended role, and safe enough to use.

### Phase 6 — Audio-quality and resilience validation

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

Installing `snapclient` is enough for the current lab endpoint. It is not
the complete product.

The durable dumb-endpoint product path still needs:

- A minimal endpoint onboarding flow or image recipe, separate from full
  JTS speaker onboarding.
- A managed `snapclient` service with channel selection for `left`,
  `right`, `mono`, and `sub`.
- A stable leader identity/address rule, with IP fallback for flaky mDNS
  and a path toward peer-id pinning before this becomes user-facing.
- Analog ceiling calibration for the endpoint amp/DAC path, because a
  dumb endpoint has no local CamillaDSP safety floor.
- Mute-on-underrun behavior and visible degraded state when the leader or
  network stream is unavailable.
- Leader-side stream production from the brainy speaker's music path.
- For stereo pairs: acoustic sync confirmation and per-side correction.
- For 2.1: a single synchronized multi-channel stream, not a separate sub
  stream.
- Doctor/state coverage that proves the endpoint is present, safe, and
  playing the intended assigned channel.
- Real hardware measurements for Wi-Fi fragility, DAC hotplug, long-run
  clock drift, underruns, and stereo/sub acoustic alignment.

Until those pieces land, treat the Zero 2 W as a measured lab endpoint,
not as a shippable wireless speaker.

Last verified: 2026-06-12
