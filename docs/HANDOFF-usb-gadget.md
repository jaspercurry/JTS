# Handoff: USB composite gadget — management network + USB audio input

**Status: operational.** Canonical for the ConfigFS composite USB gadget
(`jts-usb-audio`) that carries two independent functions off the Pi's dwc2
controller: a hardware-conditional **management network** (`ncm.usb0`) and the
wizard-toggled **USB audio input** source (`uac2.usb0`). The same UAC2 function
can optionally advertise a mono **computer microphone** direction controlled
from `/wake/`.

This doc owns gadget composition and the USB network. Neighbours:
[HANDOFF-usbsink.md](HANDOFF-usbsink.md) (the audio source's volume model,
fan-in wiring, device name) · [HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md)
(the low-latency route) · [HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md)
(persisted USB Audio Input intent and its ordered transition) ·
[HANDOFF-aec.md](HANDOFF-aec.md#optional-computer-microphone-carrier-and-source-selection)
(the bridge side of the computer microphone) · [PRIVACY.md](../PRIVACY.md)
(mic-mute behaviour) ·
[historical/usb-gadget-hardware-evidence-2026-07.md](historical/usb-gadget-hardware-evidence-2026-07.md)
(soak runs, the macOS total-audio wedge, dwc2 endpoint proof, OS-support
grading, the legacy-subnet migration, and the rescue-gadget comparison).

## Mission

A laptop plugged into the speaker's USB data port — USB-C on Pi 4/5 (including
the 8086 splitter's data leg) or micro-USB OTG on Zero-class hardware — gets a
network link to the Pi so `http://<JASPER_HOSTNAME>/` works even when the Pi has
no Wi-Fi. When Wi-Fi and USB are both up the experience is invisible: same
hostname, either path. Multiple speakers keep distinct hostnames (mDNS),
distinct MACs (derived from each Pi's CPU serial), and independently derived,
collision-resistant USB IPv4 subnets, so several household speakers can be
attached to one computer at once —
[ADR-0105](adr/0105-each-speaker-derives-its-own-usb-subnet.md).

## Product decisions

1. **USB networking is on whenever gadget hardware is available** (default
   enabled at boot). On a Zero-class product whose one OTG data port is reserved
   for a USB output DAC, the network and USB Audio Input are both intentionally
   unavailable; they return together when a registered I²S DAC overlay frees the
   port. USB **audio** stays wizard-toggled and off by default. Kill switch:
   `JASPER_USB_NETWORK=disabled` in `/etc/jasper/jasper.env` (exact literal,
   case-insensitive; any other value logs a warning and stays enabled — mirrors
   `JASPER_SHAIRPORT_SUPERVISOR` / `JASPER_SYSTEM_SUPERVISOR`).
2. **Stable per-speaker IPv4 /30 on `usb0`**, derived by `jasper.usb_network`
   from the CPU serial under the versioned `cpu-serial-sha256-v1` plan. See
   ADR-0105 for the allocation space and why a fleet-wide address was unsafe.
3. **NCM only** (`ncm.usb0`). No RNDIS, no ECM. **No IP forwarding / NAT /
   internet sharing** — the DHCP server pushes no router (option 3) and no DNS
   (option 6), so plugging JTS in can never hijack the laptop's default route. A
   future opt-in internet-sharing config is a clean seam, not something this
   ships.
4. **mDNS is the canonical UX.** `jts.local` resolves over the USB link because
   Avahi already advertises on all multicast interfaces and this feature adds no
   interface restriction. The plan-derived raw device address is a diagnostic
   fallback shown by `/state.usb_network.desired_address` and doctor — not an
   address the user must remember.
5. **Port role is hardware-resolved, never selected by source intent.** Toggling
   USB Audio Input cannot switch a controller between host and peripheral.
6. **The computer microphone is explicit, subordinate, and off by default.** Its
   durable enablement and source preference are `JASPER_USB_MIC` and
   `JASPER_USB_MIC_LEG` in `/var/lib/jasper/usb_mic.env`; it is eligible only
   while USB Audio Input is authorized/composed and an echo-cancelled AEC bridge
   profile is active. On uses UAC2 `p_chmask=1`, 48 kHz mono S16, microphone
   terminal type, and descriptor revision `0x0210`; Off uses `p_chmask=0` and
   revision `0x0200`. **The distinct revision makes macOS discard the opposite
   cached shape.** Source selection never changes either descriptor.

## USB data-role policy

`jasper.audio_hardware.usb_port_role` is the one resolver. Its inputs are the
observed board model, the configured boot overlays registered by `DacProfile`,
the active UDC role, and the observed output profile.
`jasper-audio-hardware-reconcile` publishes the result as `usb_data_role` inside
`/run/jasper-output-hardware/output_hardware.json`; the source coordinator,
final source guard, the fan-in USB reconciler, Sources UI, and doctor consume it.

| Hardware | Configured output | Desired role | Gadget/network |
|---|---|---|---|
| Zero / Zero 2 W (one shared OTG port) | registered I²S overlay | peripheral | available after the role is active |
| Zero / Zero 2 W | USB DAC, unknown DAC, or no registered I²S overlay | host | unavailable; port reserved for output |
| Pi 4 / Pi 5 (separate USB host ports) | USB or I²S DAC | peripheral | available; USB-A host ports carry the DAC |
| unknown board | unknown | unchanged | fail-closed unavailable |

**The Zero default stays `host` when its USB DAC is temporarily absent.** That
is the resilience invariant that lets unplug/replug self-recover: absence is
never treated as evidence of an I²S DAC. A role/configuration mismatch is
reported as `role_change_pending_reboot`; the installer never reboots on its
own. The installer owns a sentinel-delimited `[all]` role block and migrates the
legacy unconditional peripheral block. The same root reconciler composes Sound
Setup's InnoMaker HAT choice with this USB-role block in one atomic boot render;
the output-side contract is in
[Speaker output reference](HANDOFF-speaker-output-reference.md#current-outputd-state).

The artifact deliberately exposes two related facts. `gadget_available` is
strict and authorizes USB Audio Input only when desired, configured, and active
roles are all peripheral. `management_transport_available` follows the currently
active known controller, so an existing NCM-only link may survive a pending
peripheral→host reboot long enough for a deployment to finish. The privileged
gadget start boundary accepts only that management fact; its audio guard still
requires strict availability. Stable host and unknown hardware fail closed, and
a reboot naturally removes the pending transport.

## Unit topology

```
jasper-usbgadget.service            (the composite gadget owner)
  ├─ ExecCondition: jasper-usbgadget-wanted   (hardware + UDC + function gate)
  ├─ ExecStart:     jasper-usbgadget-up       (composes ncm.usb0 and/or uac2.usb0)
  └─ ExecStop:      jasper-usbgadget-down

jasper-usbnet-dhcp.service          (device-activated dnsmasq on usb0)
  BindsTo=sys-subsystem-net-devices-usb0.device

jasper-usbsink.service              (derived USB-audio readiness marker;
                                     Type=oneshot/RemainAfterExit, no resident
                                     process — fan-in DIRECT-captures the audio)
  Requires=/PartOf=jasper-usbgadget.service

jasper-usbmic.service               (optional Pi-to-host clean-mic relay)
  After/PartOf=jasper-usbgadget + jasper-aec-bridge
  ExecCondition: intent On + p_chmask=1 + bridge active
  Consumes dedicated localhost UDP :9894; voice remains on :9876
```

`jasper-usbgadget.service` is the single owner of the ConfigFS descriptor for
*both* functions and is enabled at install time. Its hardware/UDC condition
cleanly skips the unit when the controller belongs to output host mode; when
gadget-capable it is **not** gated on audio intent or follower status.

### Function truth table

Computed once per `jasper-usbgadget-up` run and logged as
`event=usb_gadget.compose network=<0|1> audio=<0|1> ...`:

| `JASPER_USB_NETWORK` | USB audio authorized and lifecycle-ready | functions composed |
|---|---|---|
| enabled (default) | yes | `ncm.usb0` + `uac2.usb0` |
| enabled | no / parked follower | `ncm.usb0` only |
| disabled | yes | `uac2.usb0` only (legacy, audio-only shape) |
| disabled | no | none — the unit's `ExecCondition` already skipped it |

When `uac2.usb0` is present, `JASPER_USB_MIC=enabled` refines that single
function to bidirectional audio; it never composes UAC2 by itself. With USB
Audio Input off/parked/unready the microphone preference stays saved but
`p_chmask` stays `0` and `jasper-usbmic` stays inactive.

**The audio gate lives inside both `jasper-usbgadget-wanted` and
`jasper-usbgadget-up`, not on the unit** — the service has no whole-unit
`jasper-local-source-allowed` `ExecCondition`, because the network function must
keep serving even when USB Audio is Off or this speaker is a parked multiroom
follower. Both scripts call the same source-aware
`jasper-local-source-allowed --source usbsink` check, then require
`jasper-usbsink.service` to be enabled as the derived readiness mirror **and**
fan-in STATUS to report the direct USB lane armed. Canonical Off or follower
parking always wins over stale enablement; desired-On with a disabled mirror or
unarmed data plane produces NCM-only composition instead of advertising UAC2
without its consumer. The mirrors are never treated as household intent. At boot
the gadget orders after and wants `jasper-fanin.service`, so a previously
converged USB-On box can prove the lane before composition; if it cannot, the
coordinator later performs the normal arm-then-recompose transition.

### Edge cases the truth table preserves

- **Fresh install or role change, pre-reboot** (no UDC under `/sys/class/udc`):
  `jasper-usbgadget-wanted` exits non-zero, the `ExecCondition` skips cleanly —
  **not** a unit failure. Doctor's USB data-role check says to reboot.
- **Kill switch flipped at runtime:** an operator restarts the unit; recompose
  honours the new value immediately.
- **`systemctl stop jasper-usbgadget`:** `PartOf=` propagation stops the audio
  readiness marker and volume observer too, both kernel modules unload, the host
  sees nothing. Starting again restores per the truth table. No wedged
  intermediate states — the down path stays best-effort but loud, logging every
  step and never silently leaving a half-torn-down descriptor.

### Toggling audio from `/sources/`

`/sources/` writes household intent; the shared source coordinator derives unit
enablement and performs the load-bearing stop/recompose/start order. It
recomposes only when the observed UAC2 card disagrees with the target, so an
unrelated toggle does not re-enumerate this gadget. The complete transition and
verification contract is canonical in
[HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md).

### Toggling and choosing the computer microphone from `/wake/`

`/wake/` writes only the independent `JASPER_USB_MIC=enabled|disabled` intent.
The control daemon hands the change to `jasper-usbmic-apply.service`, whose
350 ms grace is durable across a control-daemon exit and naturally debounces
rapid changes — it lets a request arriving over USB NCM finish before descriptor
re-enumeration briefly drops that link. The apply job restarts
`jasper-aec-bridge.service` plus `jasper-usbgadget.service`: the bridge adds or
removes the dedicated `:9894` duplicate, the gadget changes `p_chmask` and
`bcdDevice`, and systemd starts/stops the dependency-enabled
`jasper-usbmic.service`. The POST returns 200 only after systemd accepts the
apply job; if scheduling fails it returns a structured 502 while reporting that
the durable intent was saved. Once accepted, a failed bridge/gadget apply is
retried three times with a two-second backoff (four attempts total); the hard
start limit prevents an unbounded recompose loop, and
`event=usb_mic.recompose_failed` plus doctor drift are the operator surface if
all attempts fail. An explicit later switch resets that retry budget.

The adjacent source selector writes `JASPER_USB_MIC_LEG`. The control endpoint
fresh-reads the reconciler-owned `ChipBeamPlan` (rather than trusting the
long-lived control process environment) and rejects a choice it does not
advertise; the bridge-owned processing, fallback, and voice/wake-isolation
contracts are canonical in [HANDOFF-aec.md](HANDOFF-aec.md#optional-computer-microphone-carrier-and-source-selection).
**This source change has a deliberately narrower restart path than the On/Off
toggle**: `/aec/usb-mic-leg` saves the preference, then asks the restart broker
to restart only `jasper-aec-bridge.service` with reason `usb_mic_leg`. Saving
the already-selected value is a no-op; a changed value first clears that unit's
systemd start counter so deliberate rapid changes cannot consume the
`StartLimitAction=reboot` crash-recovery budget. `jasper-usbmic.service` follows
through its existing `PartOf=`. The path does **not** invoke the apply unit,
restart the gadget, alter `p_chmask`/`bcdDevice`, re-enumerate the device, or
interrupt NCM.

**The relay writer.** `jasper-usbmic` publishes `/run/jasper-usbmic/status.json`
schema 4 (`USB_MIC_RELAY_SCHEMA_VERSION` in `jasper/usb_mic.py`) with separate
source-packet, ALSA-write, and host `hw_ptr` progress timestamps, drop
counts/rate, bridge-emit-to-ALSA-write p50/p95/p99, bounded sequence-gap loss,
host `appl_ptr - hw_ptr` fill, writer target/geometry, resets, xruns, counted
drift splices, and drop totals attributed to host-advancing versus idle **status
intervals** — that split is diagnostic attribution at the 500 ms sampling
boundary, not proof of the host-clock state at each individual drop.

Three contracts the surfaces above depend on:

- **`RUNNING` is never treated as host use.** ALSA's gadget PCM reports it as
  soon as its playback side is primed, even with nothing consuming. The Wake page
  says Streaming only after `hw_ptr` actually advances, and a backward pointer
  reset is explicitly not progress. A never-advanced or later-idle clock is
  normal Ready state; missing AEC packets, sustained drops while the host clock
  advances independently, or writer failure are degraded health with stable
  structured events plus a doctor warning.
- **The 30 ms occupancy target inside a 20–40 ms band is the lowest reliable
  posture.** A 20 ms target / 10–30 ms band was lower-latency but recorded an
  ordinary-load xrun after 15 minutes on real hardware. The bounded source queue
  (two 20 ms frames, drop-oldest), the exact-10 ms period split, the four-period
  `plughw` geometry, and the splice accounting are implementation of that target;
  the relay never blocks on a full or non-draining gadget, and repeated recovery
  failures exit to systemd instead of reopening forever.
- **The writer removes host-idle history before resume can expose it.** After
  `hw_ptr` freezes for 200 ms the relay latches one idle sanitization — close and
  reopen the PCM, clear queued room audio, preload only silence — and when that
  silence-only ring starts advancing it resets once more and primes 20 ms of
  silence followed by the freshest source frame. The first post-idle samples are
  silence, never minutes-old room audio.

During an active host recording, doctor compares the fresh p95 with the 120 ms
acceptance budget; it deliberately does not judge a frozen idle ring. For a
reviewable run record, keep the host capture open and run
`sudo /opt/jasper/.venv/bin/jasper-usb-mic-latency-artifact` (`--duration-seconds`,
`--host-os`, `--host-app`, `--output`; `--require-pass` makes a warning a nonzero
exit). The schema-1 artifact rejects any tick where the host is not pulling and
binds the window to build, descriptor revision, resolved export source,
negotiated capture and writer geometry, host/app identity, and counter deltas.
Certification requires ≥15 s of uninterrupted status, and percentile aggregation
begins only after both an 11-second warm-up **and** 512 exact source-age appends
— that counter proof, not wall time, is what guarantees the rolling window holds
only this run when status reads are delayed. Percentiles are conservative
nearest-rank aggregates of qualifying ticks, not raw per-frame samples; the three
`*_sha256` fields bind configuration, run identity, and content and **none is a
cryptographic operator signature**.

The measured scope is `source_age_scope=bridge_emit_to_alsa_write`. **This is
not physical mic→host end-to-end latency**: XVF/PortAudio capture time, current
gadget fill, USB transport, and the host audio stack are separate terms.

From the descriptor owner's perspective an actual transition adds or removes
`uac2.usb0` while leaving the network function wanted, and a brief host-visible
re-enumeration ("Playback Inactive" flicker, momentary network blip) is expected.
Changing only `JASPER_USB_MIC_LEG` is not such a transition and must not
recompose. The `/wake/` controls are the sole end-user authority for this export:
pausing the JTS voice assistant does not alter or silence an explicitly enabled
computer microphone.

### Multiroom follower parking

Parking a bonded follower makes grouping land the role and synchronously hand it
to the canonical source coordinator, which stops the audio units, disarms fan-in,
and **restarts** (not stops) the gadget-owning unit so it recomposes to drop
`uac2.usb0`. The host stops seeing a USB audio device from a follower while the
USB management network keeps working — it must, since the household may need to
reach the follower's management UI directly. Restoring recomposes the audio
function only when persisted intent wants it. Grouping owns the role transition
and no source-unit, accessory, or USB-coupling sequence; it waits for the source
pass to finish. See [HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md)
and [HANDOFF-multiroom.md](HANDOFF-multiroom.md).

## Gadget scripts

`deploy/usbsink/jasper-usbgadget-up` / `-down` / `-wanted`, installed to
`/usr/local/sbin/`. Idempotent: a bound descriptor is left alone; a partial
(unbound) one is torn down and rebuilt.

- **NCM function:** `host_addr` / `dev_addr` are **deterministic**, derived by
  hashing the Pi's CPU serial with a locally-administered unicast prefix
  (`02:...`). This determinism is load-bearing — a randomly assigned MAC would
  make the host see a brand-new network adapter, with a new interface name and
  dropped routes/leases, on every boot.
- **UAC2 function:** byte-identical attribute block to the pre-composite gadget.
  This is a protection-list contract; see "Naming debt".
- **Product string:** `"${SPEAKER_NAME}"`, not `"${SPEAKER_NAME} USB Audio"` —
  the NIC should not carry an audio-flavoured label. The host-visible *audio
  device* label is a separate string patched by `jasper-usbsink-name-patch`.
  `bcdDevice` is bumped so a host that caches descriptors by VID:PID:bcdDevice
  re-reads the new function set rather than a stale one.
- **Testability:** the ConfigFS root, UDC class dir, CPU-serial file, and the
  four probe commands are env-overridable seams, so
  `tests/test_usbgadget_script.py` drives the scripts hermetically against a temp
  dir. **These are not production configuration:** `jasper-usbgadget.service`
  strips every seam, Python/loader override, and speaker-name path before the
  root scripts run. The root scripts
  never source the management-writable speaker-name file — they pass its fixed
  path to `jasper.speaker_name`, which owns env quoting and the canonical
  32-character printable-name policy before the result reaches ConfigFS, a module
  marker, or a journal field.

## Network design

NetworkManager is the box's **single** network owner for `usb0` — no
systemd-networkd, no dispatcher scripts.

- **Address-plan owner:** [`jasper/usb_network.py`](../jasper/usb_network.py) is
  the only derivation, validation, rendering, and attestation surface. The
  installer reads `/proc/cpuinfo`, fails loudly if no stable non-zero hex CPU
  serial exists, and writes a validated plan to the root-owned
  `/var/lib/jasper-usb-network/plan.json`. **The raw serial is not persisted** —
  observability uses a 12-hex SHA-256 fingerprint. A boot-time
  `jasper-usb-network-plan.service` re-attests the artifact against the current
  Pi, then renders the NetworkManager and dnsmasq projections under one bounded
  owner lock. Confirmed interface absence, or an existing interface with no
  IPv4, permits promotion; an inspection error fails loudly rather than being
  mistaken for safe absence. A missing, corrupt, mismatched, or partially
  unwritable plan blocks `jasper-usbgadget.service` but does **not** block
  NetworkManager or Wi-Fi.
- **NM keyfile:** the owner generates
  `/etc/NetworkManager/system-connections/jts-usb.nmconnection` (mode `0600`,
  root:root). Raspberry Pi OS marks all `DEVTYPE=gadget` interfaces unmanaged;
  JTS overrides that for `usb0` only with
  [`deploy/usb-network/90-jasper-usbnet.conf`](../deploy/usb-network/90-jasper-usbnet.conf),
  whose per-device `managed=1` outranks the udev default and whose
  `ignore-carrier=yes` lets this static-address profile activate before a laptop
  is attached. The profile carries a low `autoconnect-priority` (a real network
  always wins when both exist) and IPv4 `method=manual, never-default=true` with
  **no gateway set** — nothing to advertise as a route even if a future change
  forgot the dnsmasq suppression.
- **dnsmasq:** `install.sh` apt-installs **`dnsmasq-base`** — the binary only,
  which ships no systemd service scaffolding — **not** the full `dnsmasq`
  package, which would register a global system service. The generated
  `/etc/jasper/usbnet-dnsmasq.conf` is DHCP-only for one host:
  `interface=usb0`, `bind-dynamic` (the derived address may appear after dnsmasq
  starts), `port=0` (DNS listener fully off), a one-address `dhcp-range`, and
  empty-valued `dhcp-option=3`/`dhcp-option=6` to explicitly suppress router and
  DNS. The lease file lives under the unit's `RuntimeDirectory`
  (`/run/jasper-usbnet/`, tmpfs).
- **`jasper-usbnet-dhcp.service`** is device-activated
  (`BindsTo=`/`WantedBy=sys-subsystem-net-devices-usb0.device`), so it exists
  only while `usb0` exists, under `MemoryMax=16M`-class bounds plus the hardening
  set `tests/test_systemd_hardening.py` expects.
- **IPv6 link-local + Avahi** give a hostname fallback even if DHCP never
  completes — no extra code, just Avahi advertising on all multicast interfaces.
- **No sysctl, no nftables, no `ip_forward` anywhere.** The seam for a future
  opt-in "share this speaker's internet connection" config is left clean;
  nothing toward it is built here.

Upgrades from the legacy fleet-wide `10.12.194.1/24` generation defer promotion
to the next boot when `usb0` is live on a different address, so an install
running over that link cannot strand itself. Mechanics are in the historical
appendix; `/state` and doctor show desired vs observed address plus
`migration_pending` throughout.

## Deploy baseline and source replay

`enable_usbgadget` (`deploy/lib/install/systemd-units.sh`) establishes one safe
deployment baseline **without interpreting intent**: disable and stop the derived
USB-audio unit, keep or bring up NCM, and recompose an active gadget only when
old unit state or a present UAC2 card proves stale audio could still be
advertised. `jasper-usbgadget.service` is the first gadget unit the installer
enables, deliberately, since it carries the default-on network. A pending
host-role reboot keeps NCM-only composition while the controller is still
peripheral so a deploy over that link can finish; strict USB audio availability
stays false. The later `reapply_source_intent` call is the single canonical
replay point: for On it performs fan-in DIRECT arm → UAC2 recompose → readiness
marker start, while invalid intent fails closed. An already-converged NCM-only
deploy does not bounce the management link. Pinned by
`tests/test_install_usbgadget_migration.py`.

## Controller forensics

The gadget unit runs the bounded `jasper-usbgadget-snapshot` helper before
unbind and after bind, recording UDC/configuration state, DWC2 state and
registers, endpoint queues, interrupt line, `usb0` counters, and USB-mic status
under `/var/lib/jasper/usb-gadget-incidents/` (latest 12 kept). Each capture is
wrapped in a two-second timeout and a best-effort systemd directive, so
diagnostics cannot prevent teardown or recovery. Structured
`event=usb_gadget.snapshot` lines carry the reason, UDC state, `GINTSTS`,
`DAINT`, and artifact path. The production unit strips every test-only path
override before invoking this root helper.

**These exist because UDC `configured` and healthy userspace audio do not prove
the controller still answers the host** — see the 2026-07-22 macOS total-audio
wedge in the historical appendix. Recovery for that shape is a gadget-only
restart run **over Wi-Fi**, because the USB management link is part of the wedge:

```bash
ssh pi@<wifi-ip> sudo systemctl restart jasper-usbgadget.service
```

### Opt-in rolling USB forensics

The **USB forensics** card under `/system/` is the bounded investigation mode for
failures that cannot be reconstructed from one restart snapshot. It is off by
default. Enabling it writes the sole persistent intent marker at
`/var/lib/jasper/usb_gadget_forensics.env`; the marker survives ordinary deploys
and reboots, and removing it is the entire disable operation. A tiny
always-enabled systemd path watcher starts the sampler only while that marker
exists, so the off state has no resident sampler or polling timer.

While enabled, `jasper-usbgadget-forensics.service` samples every 10 seconds,
recording only controller/UDC state, `GINTSTS`/`DAINT`, the DWC2 interrupt
counter, and `usb0` packet/error counters. **It never opens an audio device,
records audio, reads application data, or writes a steady-state journal line.**
The rolling timeline lives under `/run/jasper-usb-gadget-forensics/` (tmpfs),
hard-capped at 512 KiB across the current and previous segment; the service is
capped at 32 MiB. Disk writes happen only when evidence is frozen: **Capture
now** adds at most the latest 128 KiB to one ordinary incident artifact, and
**Capture & repair USB** queues a gadget-only restart whose existing pre-reset
and post-start hooks freeze the timeline on both sides of the transition.
Turning forensics off releases the RAM timeline but deliberately leaves the
bounded incident history.

The dashboard reads `/state.usb_gadget_forensics`: requested enablement, observed
sampler freshness, sample count, last fixed action, latest artifact path. A
requested-but-stale sampler is shown as needing attention rather than silently
reported healthy. The root sampler accepts only two fixed request filenames
(`capture` and `repair`); `jasper-control` never receives general root command
execution.

## RAM contract

The load-bearing fact: **`u_ether` registers the `usb0` netdev at gadget-BIND
time, not host-attach time.** So on a composed and bound NCM gadget `usb0` exists
(and its `.device` unit is active) whether or not a laptop is plugged in, which
means `jasper-usbnet-dhcp` is resident whenever the network function is composed,
waiting to serve a lease the moment a host attaches. The only truly zero cost is
when NCM is *not* composed. Carrier — not interface existence — reflects the
cable.

| State | Cost | Notes |
|---|---|---|
| Kill-switched AND audio off | ~50 KB (the dwc2 kernel module only) | `jasper-usbgadget-wanted` exits non-zero, the `ExecCondition` skips, libcomposite never loads, `usb0` never appears |
| Gadget unavailable in stable host mode, or pending host→peripheral | ~0–50 KB | A Zero USB-output product intentionally stays host-only; when peripheral is desired but not yet active there is no UDC. Doctor distinguishes the intentional state from a reboot requirement |
| Pending peripheral→host while the controller is still peripheral | ~1 MB until reboot | Strict audio availability is false and UAC2 is withdrawn, but `management_transport_available` keeps NCM-only composition so an in-flight deploy over USB does not sever itself |
| Network composed (bound), no host plugged in | ~1 MB | `libcomposite` + `usb_f_ncm`/`u_ether` loaded, `usb0` exists (carrier down), and the `MemoryMax=16M`-bounded dnsmasq **is resident** |
| Network composed, host plugged in, audio off | ~1 MB | Same residents; `usb0` now has carrier and DHCP hands out the lease |
| Network + audio both on | ~1 MB + the bounded volume observer | The readiness marker adds no resident process; fan-in is already core. See HANDOFF-usbsink.md "RAM budget" |

## Naming debt

The ConfigFS gadget directory stays `jts-usb-audio` even though the gadget is no
longer audio-only. **Accepted debt, not an oversight:** doctor's
`check_usbsink_low_latency_contract` reads
`/sys/kernel/config/usb_gadget/jts-usb-audio/functions/uac2.usb0/...` verbatim,
and renaming would mean touching that pinned, protection-listed contract for a
cosmetic reason. Revisit only if the low-latency contract is revisited anyway.

## Guard acceptance

The management-host guard (`jasper.http_security`) accepts `Host:` headers for
the plan-derived RFC 1918 address (with or without port), the legacy
`10.12.194.1` during migration, and IPv4 link-local (`169.254.0.0/16`, reachable
before or without DHCP completing) alongside the existing mDNS/LAN acceptance,
with a public-IP rejection pinned as the contrast case. See
`tests/test_http_security.py`.

## Observability

- `jasper-doctor` (`jasper/cli/doctor/usbsink.py`) checks that gadget
  composition matches the same gates as the scripts. Its network checks validate
  and identity-attest the persisted plan, compare both installed projections with
  the one renderer, verify `usb0` carries the desired /30 (or visibly report a
  pending legacy migration), and confirm the `jts-usb` profile is active with
  `jasper-usbnet-dhcp`'s unit state coherent with `usb0`'s presence. A loopback
  HTTP probe uses `usb0`'s observed address with `Host: <JASPER_HOSTNAME>`
  expecting 200 — pinning nginx bind plus guard acceptance of the fallback URL
  without needing hardware. The USB-microphone check reads the resolved hardware
  role first and skips cleanly before requiring wizard-owned intent when the
  topology cannot expose a gadget.
- `/state.usb_network` carries enabled/interface/carrier state, observed and
  desired address, plan version, identity fingerprint, and `migration_pending`.
  `observation_status` distinguishes `absent`, `no_address`, `observed`, `error`,
  with `observation_error` carrying the inspection failure. It reads live
  sysfs/interface state and the validated plan on **every call — never cached**;
  a missing or corrupt plan leaves desired fields null instead of fabricating an
  address, and `carrier=false`/absent is normal, never an error state.
- Events: `usb_network.plan_{staged,applied,deferred,failed}`,
  `install.usb_network_migration_pending`,
  `usb_gadget.{compose,up,down,skip,snapshot}`.

## Open hardware validation

What is proven, and against which run, is in the historical appendix. What is
still owed a hardware pass:

- **Composite under simultaneous traffic** — enumeration and return capture
  passed separately; still owed sustained Mac→Pi playback and Pi→Mac capture
  together while exercising NCM, plus the schema-4-vs-host-capture latency
  certification that separates gadget/USB/host terms.
- **The network claims, end to end, with Wi-Fi off** — `jts.local` and the
  derived address both reach this speaker over usb0, DHCP hands out one lease,
  the host keeps its own default route, and dnsmasq drops privileges cleanly
  (`nobody:nogroup`; its `CapabilityBoundingSet` must include
  `CAP_SETUID`/`CAP_SETGID`, since dnsmasq-base ships no user of its own). Then
  two speakers on one Mac with no overlapping /30, and one upgraded over its
  legacy address promoting at the next boot.
- **The RAM contract's timing assumption** — `usb0` and dnsmasq resident with
  nothing plugged in, which is also what doctor's "usb0 absent + UDC present =
  compose/bind failure" check depends on. The carrierless subset passed on JTS3
  on the prior address generation; re-confirm on the derived plan.
- **The transitions** — `/sources/` toggle while plugged in, multiroom follower
  parking, a live upgrade over an enabled USB-audio session, and a speaker
  rename updating both the NIC-facing product string and the separately owned
  audio device label. Also confirm the product-string edit did not change the
  host's *audio* device label; if it did, revert that edit.
- **The unmeasured hosts** — Windows UAC2 input (`usbaudio2.sys` binding with no
  vendor driver, with NCM both bound and unbound) and what actually happens when
  a household separately opts into `rpi-usb-gadget` via Imager.

## Cable caveats

- **USB-C-to-USB-C cables hit an open kernel bug**
  ([raspberrypi/linux#6289](https://github.com/raspberrypi/linux/issues/6289))
  and do not work reliably for gadget mode on Pi 5. Use USB-A-to-USB-C (the 8086
  splitter's host-side leg is USB-A specifically to sidestep this).
- **Apple Silicon Macs have a known USB-PD interaction**
  ([raspberrypi/linux#6569](https://github.com/raspberrypi/linux/issues/6569))
  that can break gadget detection. Try a different host if detection fails.
- **8086 Consultancy USB-C/PWR Splitter:** the data leg carries the gadget link;
  the power leg stays on the Pi's normal PSU. Topology diagram in
  [historical/usbsink-implementation-appendix.md](historical/usbsink-implementation-appendix.md)
  "Physical topology".
- **Pi 5 halts at boot (solid red LED) on splitter power without
  `usb_max_current_enable`.** The Pi 5 sizes its power budget from the USB-C
  **PD** negotiation and the splitter does not pass PD through, so the Pi cannot
  confirm a 5 A supply, runs power-restricted, and can stop at the *firmware*
  stage before the OS boots — solid red LED, unreachable, and **no journal at
  all**, even with a fully capable PSU on the power leg. `install.sh`'s
  `reconcile_usb_data_role` writes `usb_max_current_enable=1` to
  `/boot/firmware/config.txt` (a second `[all]` step alongside the `dwc2`
  dtoverlay, checked independently so deployed boxes backfill it on a re-run).
  No-op on a normal PD supply; undervoltage detection still guards a marginal
  one. Pinned in `tests/test_install_helpers.py`.

Last verified: 2026-08-25 (triage pass — unit names, script paths, the plan
module's derivation constants, the relay schema version, the latency-artifact
console script, and the forensics marker path rechecked against
`deploy/systemd/`, `deploy/usbsink/`, `jasper/usb_network.py`,
`jasper/usb_mic.py`, `jasper/control/usb_gadget_forensics.py`, and
`pyproject.toml`. The "Migration behavior" section describing `install.sh`
disabling `jasper-usbsink-init.service` was deleted as stale — no installer path
references that unit any more. Hardware evidence, the macOS wedge, OS-support
grading, and the legacy-subnet migration mechanics moved to
`docs/historical/usb-gadget-hardware-evidence-2026-07.md`.)
