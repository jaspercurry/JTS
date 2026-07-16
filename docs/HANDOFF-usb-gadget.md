# Handoff: USB composite gadget — management network + USB audio input

**Status: operational.** Canonical for the ConfigFS composite USB gadget
(`jts-usb-audio`) that carries two independent USB functions off the Pi's
dwc2 controller: a hardware-conditional **management network** (`ncm.usb0`)
and the wizard-toggled **USB audio input** source (`uac2.usb0`, owned
operationally by [HANDOFF-usbsink.md](HANDOFF-usbsink.md)). The same UAC2
function can optionally advertise a mono **computer microphone** direction controlled
from `/wake/`. This doc is the
single source of truth for gadget composition and the USB network; the
audio-source design (volume model, fan-in wiring, low-latency route) stays
in HANDOFF-usbsink.md and HANDOFF-usb-low-latency.md. Persisted USB Audio
Input intent and its ordered lifecycle transition are owned by
[HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md).

## Mission

A laptop plugged into the speaker's USB-C port (via the 8086 splitter's data
leg, or a USB-A→C cable on a bare Pi) gets a network link to the Pi so
`http://<JASPER_HOSTNAME>/` works even when the Pi has no Wi-Fi. When Wi-Fi
and USB are both up, the experience is invisible — same hostname, either
path. Multiple speakers keep distinct hostnames (mDNS) and distinct MACs
(derived from each Pi's CPU serial).

## Product decisions

1. **USB networking is on whenever gadget hardware is available** (default
   enabled at boot). On a Zero-class product whose one OTG data port is
   reserved for a USB output DAC, the network and USB Audio Input are both
   intentionally unavailable. They return together when a registered I²S DAC
   overlay leaves that port free.
   USB **audio** stays wizard-toggled and off by default, exactly as before
   this change. Kill switch: `JASPER_USB_NETWORK=disabled` in
   `/etc/jasper/jasper.env` (exact literal, case-insensitive; any other
   value logs a warning and stays enabled — mirrors
   `JASPER_SHAIRPORT_SUPERVISOR` / `JASPER_SYSTEM_SUPERVISOR`).
2. **Pi-side address: `10.12.194.1/24` on `usb0`** — deliberately the same
   number Raspberry Pi OS's own first-boot USB rescue gadget uses (see
   "Relationship to Raspberry Pi OS's own USB rescue gadget" below). No env
   override for the IP in v1 — see "Changing the IP" below if you need one.
3. **NCM only** (`ncm.usb0`). No RNDIS, no ECM. OS support is summarized in
   "OS support" below, with verified-vs-assumed called out explicitly.
4. **No IP forwarding / NAT / internet sharing.** The DHCP server pushes no
   router (option 3) and no DNS (option 6) to the host, so plugging JTS in
   can never hijack the laptop's default route. A future opt-in
   internet-sharing config is a clean seam, not something this ships.
5. **mDNS is the canonical UX.** `jts.local` (the configured
   `JASPER_HOSTNAME`) is expected to resolve over the USB link because
   Avahi already advertises on all multicast interfaces and this feature
   adds no interface restriction. The raw IP (`http://10.12.194.1/`) is the
   documented fallback, not the primary story.
6. **Port role is hardware-resolved, never selected by source intent.** Toggling
   USB Audio Input cannot switch a controller between host and peripheral.
7. **The computer microphone is explicit, subordinate, and off by default.** Its
   durable preference is `/var/lib/jasper/usb_mic.env`; it is eligible only
   while USB Audio Input is authorized/composed and an echo-cancelled AEC bridge
   profile is active. On uses UAC2 `p_chmask=1`, 48 kHz mono S16, microphone
   terminal type, and descriptor revision `0x0210`; Off uses `p_chmask=0` and
   revision `0x0200`. The distinct revision makes macOS discard the opposite
   cached shape.

## USB data-role policy

`jasper.audio_hardware.usb_port_role` is the one resolver. Its inputs are the
observed board model, the configured boot overlays registered by
`DacProfile`, the active UDC role, and the observed output profile (for
diagnostic detail). `jasper-audio-hardware-reconcile` publishes the result as
`usb_data_role` inside
`/run/jasper-output-hardware/output_hardware.json`; the source coordinator,
final source guard, fan-in coupling, Sources UI, and doctor consume it.

| Hardware | Configured output | Desired role | Gadget/network |
|---|---|---|---|
| Zero / Zero 2 W (one shared OTG port) | registered I²S overlay | peripheral | available after the role is active |
| Zero / Zero 2 W | USB DAC, unknown DAC, or no registered I²S overlay | host | unavailable; port is reserved for output |
| Pi 4 / Pi 5 (separate USB host ports) | USB or I²S DAC | peripheral | available; USB-A host ports carry the DAC |
| unknown board | unknown | unchanged | fail-closed unavailable |

The Zero default stays `host` when its USB DAC is temporarily absent. This is
the resilience invariant that lets unplug/replug self-recover; absence is
never treated as evidence of an I²S DAC. A role/configuration mismatch is
reported as `role_change_pending_reboot`; the installer never reboots on its
own. The installer owns a sentinel-delimited `[all]` role block and migrates
the legacy unconditional peripheral block.

The artifact deliberately exposes two related facts. `gadget_available` is
strict and authorizes USB Audio Input only when desired, configured, and active
roles are all peripheral. `management_transport_available` follows the
currently active known controller, so an existing NCM-only link may survive a
pending peripheral→host reboot long enough for deployment to finish. The
privileged gadget start boundary accepts only that management fact; its audio
guard still requires strict availability. Stable host and unknown hardware
fail closed, and reboot naturally removes the pending transport.

Pre-reboot JTS4 evidence on 2026-07-14: the board identified as Raspberry Pi
Zero 2 W; its config forced `dwc2,dr_mode=peripheral`; no registered I²S/HAT
overlay or output DAC was observable because the shared port was not acting as
a host. The migration therefore resolved `host` and reported a pending reboot.
Post-reboot evidence on 2026-07-15 closed that loop: JTS4 resolved an active
host role, detected its Apple USB-C output DAC with a ready output-hardware
artifact and ALSA outputd backend, kept Bluetooth enabled, reported USB Audio
Input intentionally unavailable, and passed strict deploy health with 0
failures / 0 warnings. This proves the Zero USB-output path; it does not claim
positive UAC2/gadget hardware validation.

## Unit topology

```
jasper-usbgadget.service            (NEW — the composite gadget owner)
  ├─ ExecCondition: jasper-usbgadget-wanted   (hardware + UDC + function gate)
  ├─ ExecStart:     jasper-usbgadget-up       (composes ncm.usb0 and/or uac2.usb0)
  └─ ExecStop:      jasper-usbgadget-down

jasper-usbnet-dhcp.service          (NEW — device-activated dnsmasq on usb0)
  BindsTo=sys-subsystem-net-devices-usb0.device

jasper-usbsink.service              (derived USB-audio Type=oneshot/RemainAfterExit readiness marker; no resident process — fan-in DIRECT-captures audio)
  Requires=/PartOf=jasper-usbgadget.service   (repointed from the deleted init unit)

jasper-usbmic.service               (optional Pi-to-host clean-mic relay)
  After/PartOf=jasper-usbgadget + jasper-aec-bridge
  ExecCondition: intent On + p_chmask=1 + bridge active
  Consumes dedicated localhost UDP :9894; voice remains on :9876
```

`jasper-usbmic` publishes `/run/jasper-usbmic/status.json` schema 3 with
separate source-packet, sink-write, and host `hw_ptr` progress timestamps plus
drop counts/rate. The schema also carries v2 bridge-emit age p50/p95/p99,
sequence-gap loss, and separate drop totals while the host clock is advancing
versus idle. ALSA's gadget PCM reports `RUNNING` as soon as `aplay` opens,
even when no host application is consuming it, so `RUNNING` is never treated as
host use by itself. The Wake page says Streaming only after `hw_ptr` actually
advances. A never-advanced or later-idle clock is normal Ready state; queue
drops while that host clock is idle are expected drop-oldest behavior. Missing
AEC packets, or sustained drops while the host clock is independently advancing,
are degraded relay health and produce a stable `event=usb_mic.audio_health`
transition plus a doctor warning. This distinction keeps idle hosts from raising
false alarms while ensuring an alive-but-stuck writer cannot claim Streaming.

`jasper-usbsink-init.service` — the old audio-only gadget-owner oneshot —
is **deleted**. `jasper-usbgadget.service` is its replacement and does more:
it is the single owner of the ConfigFS descriptor for *both* functions, and
unlike the old init unit it is enabled at install time. Its hardware/UDC
condition cleanly skips the unit when the controller belongs to output host
mode; when gadget-capable, it is not gated on audio intent or follower status.

### Function truth table

Computed once per `jasper-usbgadget-up` run and logged as a structured
`event=usb_gadget.compose network=<0|1> audio=<0|1> ...` line:

| `JASPER_USB_NETWORK` | USB audio authorized and lifecycle-ready | functions composed |
|---|---|---|
| enabled (default) | yes | `ncm.usb0` + `uac2.usb0` |
| enabled | no / parked follower | `ncm.usb0` only |
| disabled | yes | `uac2.usb0` only (legacy, audio-only shape) |
| disabled | no | none — the unit's `ExecCondition` already skipped the whole unit |

When `uac2.usb0` is present, `JASPER_USB_MIC=enabled` refines that single
function to bidirectional audio; it never composes UAC2 by itself. With USB
Audio Input off/parked/unready, the microphone preference remains saved but
`p_chmask` stays `0` and `jasper-usbmic` stays inactive.

The audio gate lives **inside** both `jasper-usbgadget-wanted` and
`jasper-usbgadget-up`, not on the unit itself —
`jasper-usbgadget.service` has no whole-unit `jasper-local-source-allowed`
`ExecCondition`, because the network function must keep serving (when hardware
permits it) even when USB
Audio is Off or this speaker is a parked multiroom follower. Both scripts call
the same source-aware `jasper-local-source-allowed --source usbsink` check and
then require `jasper-usbsink.service` to be enabled as the derived lifecycle
readiness mirror **and** fan-in STATUS to report that the direct USB lane is
armed. Canonical Off or follower parking always wins over stale enablement;
desired-On with a disabled mirror or unarmed data plane produces NCM-only
composition instead of advertising UAC2 without its consumer. The mirrors are
never treated as household intent. At boot the gadget orders after and wants
`jasper-fanin.service`, so a previously converged USB-On box can prove the lane
before composition; if it cannot, the coordinator later performs the normal
arm-then-recompose transition.
`jasper-usbsink.service` carries that same source-aware ExecCondition, so the
process-free audio readiness marker also skips while Off or parked.

### Toggling audio from `/sources/`

`/sources/` writes household intent; the shared source coordinator derives
unit enablement and performs the load-bearing stop/recompose/start order. It
recomposes only when the observed UAC2 card disagrees with the target, so an
unrelated toggle does not re-enumerate this gadget. The complete transition and
verification contract is canonical in
[HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md).

### Toggling the computer microphone from `/wake/`

`/wake/` writes only the independent `JASPER_USB_MIC=enabled|disabled` intent.
The control daemon hands the change to `jasper-usbmic-apply.service`, whose
350 ms grace is durable across a control-daemon exit and naturally debounces
rapid changes. The apply job restarts `jasper-aec-bridge.service` plus
`jasper-usbgadget.service`: the bridge adds or removes the dedicated `:9894`
duplicate, the gadget changes `p_chmask` and `bcdDevice`, and systemd
starts/stops the dependency-enabled `jasper-usbmic.service`. The grace lets a
request arriving over USB NCM finish before descriptor re-enumeration briefly
drops that link.

The relay's Python queue is bounded to two 20 ms periods and drops oldest audio
if the host is not consuming. It uses ALSA's blocking 16→48 kHz conversion
proven by the lab, with a 10 ms ALSA period that the Pi's four-period UAC2 ring
realizes as a 40 ms hardware buffer. The dedicated `:9894` packet now carries a
sequence and bridge-emit monotonic timestamp; status reports a bounded rolling
p50/p95/p99 from bridge emit to relay dequeue, plus sequence gaps and active-
host versus idle drop totals. `event=usb_mic.pipe_configured` records the real
pipe capacity and `event=usb_mic.pipe_baseline` records one startup occupancy
sample while this transitional pipe still exists.

Those measurements do **not** yet prove a 40–80 ms end-to-end bound because the
dequeue timestamp is before the opaque `aplay` pipe and gadget ring. A genuine
host recording on 2026-07-16 held about 50 ms in that pipe and a 30–40 ms gadget
ring sawtooth. The earlier 490 ms pipe reading was a frozen idle residual, not
steady-state latency—but frozen residuals varied with prior use and there is no
flush when host capture begins. The real defect is therefore history-dependent
latency and a possible stale leading burst. Replacing the pipe with an
occupancy-targeted writer and reset-on-host-resume remains the next measured
slice; the existing 15.02 s capture proves continuity, not latency.

The relay publishes fresh status under `/run/jasper-usbmic/status.json`;
“streaming” requires the gadget PCM hardware pointer and sink writer to advance,
while an idle host stays “ready.” The `/wake/` switch is the sole end-user
authority for this export: pausing the JTS voice assistant does not alter or
silence an explicitly enabled computer microphone. `/wake/`, `/aec`, logs, and
the usbsink doctor group expose desired, advertised, relay, and streaming state.

From this descriptor owner's perspective, an actual transition adds or removes
`uac2.usb0` while leaving the network function wanted. A brief host-visible
re-enumeration ("Playback Inactive" flicker, momentary network blip) is expected
when a recompose is necessary and remains hardware-checklist item #5.

### Multiroom follower parking

Parking a bonded follower makes grouping land the role and synchronously hand
it to the canonical source coordinator. That owner stops the audio units,
disarms fan-in, and **restarts** (not stops) the gadget-owning unit so it
recomposes to drop `uac2.usb0` — the host stops seeing a USB audio
device from a follower, while the USB management network keeps working (it
must, since the household may need to reach the follower's management UI
directly). Restoring recomposes the audio function only when persisted intent
wants it. Grouping owns the role transition; source-lifecycle owns the
desired/effective semantics and ordered restore. Grouping owns no source-unit,
accessory, or USB-coupling sequence; it waits for the source pass to finish.
See
[HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md) and
[HANDOFF-multiroom.md](HANDOFF-multiroom.md).

### Edge cases the truth table preserves

- **Fresh install or role change, pre-reboot** (desired peripheral role not yet active → no UDC under
  `/sys/class/udc`): `jasper-usbgadget-wanted` exits non-zero, the unit's
  `ExecCondition` skips cleanly — **not** a unit failure. `jasper-doctor`'s
  USB data-role check tells the operator to reboot.
- **Kill switch flipped at runtime**: an operator restarts
  `jasper-usbgadget.service`; recompose honors the new value immediately.
- **`systemctl stop jasper-usbgadget`** (operator-initiated): `PartOf=`
  propagation stops the audio readiness marker and volume observer too; both
  kernel modules unload; the
  host sees nothing. Starting again restores per the truth table. No
  wedged intermediate states — the down path stays best-effort but loud
  (logs every step, never silently leaves a half-torn-down descriptor).

## Gadget scripts

`deploy/usbsink/jasper-usbgadget-up` / `-down` / `-wanted` (installed to
`/usr/local/sbin/`). Idempotent: a bound descriptor is left alone; a
partial (unbound) one is torn down and rebuilt.

- **NCM function** (`functions/ncm.usb0`): `host_addr` / `dev_addr` are
  **deterministic**, derived by hashing the Pi's CPU serial (the same
  `/proc/cpuinfo` read used for the gadget serial number) with a
  locally-administered unicast prefix (`02:...`, per
  [the kernel's ConfigFS NCM ABI doc](https://www.kernel.org/doc/Documentation/ABI/testing/configfs-usb-gadget-ncm) —
  verified, see "OS support" §5 below). Two speakers derive two different
  MAC pairs; the same speaker derives the same pair across reboots. This
  determinism is load-bearing: a randomly-assigned MAC would make the host
  see a brand-new network adapter (new interface name, dropped
  routes/leases) on every boot.
- **UAC2 function**: byte-identical attribute block to the pre-composite
  gadget — this is a protection-list contract, see "Relationship to USB
  low-latency" below.
- **Product string**: changed from `"${SPEAKER_NAME} USB Audio"` to just
  `"${SPEAKER_NAME}"`, because the gadget is no longer audio-only and the
  NIC shouldn't carry an audio-flavored label. The host-visible *audio
  device* label is a separate string patched by
  `jasper-usbsink-name-patch` (see HANDOFF-usbsink.md "Device name") and is
  unaffected. **Hardware-verify**: confirm the audio label is unchanged
  after this product-string edit (checklist #4).
- **`bcdDevice`**: bumped `0x0100` → `0x0200` so a host that caches
  descriptors by VID:PID:bcdDevice re-reads the new (composite) function
  set rather than a stale cached one. **Hardware-verify**: confirm hosts
  actually re-enumerate on the bump (checklist #1).
- **Testability**: the ConfigFS root, UDC class dir, CPU-serial file, canonical
  hardware-transport probe, audio-permission probe,
  derived-lifecycle-readiness probe, and live fan-in data-readiness probe are
  env-overridable test seams
  (`JASPER_CONFIGFS_ROOT`, `JASPER_UDC_CLASS_DIR`, `JASPER_CPUINFO_FILE`,
  `JASPER_USBGADGET_HARDWARE_ALLOWED_CMD`,
  `JASPER_USBGADGET_AUDIO_ALLOWED_CMD`,
  `JASPER_USBGADGET_AUDIO_READY_CMD`,
  `JASPER_USBGADGET_AUDIO_DATA_READY_CMD`, and
  `JASPER_SPEAKER_NAME_READER`),
  so `tests/test_usbgadget_script.py` drives the scripts hermetically
  against a temp dir, mirroring `tests/test_wifi_guardian_script.py`. These are
  not production configuration: `jasper-usbgadget.service` strips every seam,
  Python/loader override, and speaker-name path before the root scripts run.
  The root scripts never source the management-writable speaker-name file:
  they pass its fixed path to `jasper.speaker_name`, which owns env quoting and
  the canonical 32-character printable-name policy before the result reaches
  ConfigFS, a module marker, or a journal field.

## Network design

NetworkManager is the box's **single** network owner for `usb0` — no
systemd-networkd, no dispatcher scripts.

- **NM keyfile**: [`deploy/usb-network/jts-usb.nmconnection`](../deploy/usb-network/jts-usb.nmconnection),
  installed to `/etc/NetworkManager/system-connections/jts-usb.nmconnection`
  (mode `0600`, root:root) by `install.sh`. Raspberry Pi OS deliberately marks
  all `DEVTYPE=gadget` interfaces unmanaged; JTS overrides that distribution
  default for `usb0` only with
  [`deploy/usb-network/90-jasper-usbnet.conf`](../deploy/usb-network/90-jasper-usbnet.conf).
  Its per-device `managed=1` has higher priority than the udev default and
  `ignore-carrier=yes` lets this static-address profile activate before a
  laptop is attached. Install reloads both files and performs one bounded
  activation for an already-present `usb0`; later gadget rebuilds use normal
  NetworkManager autoconnect, with no poller. The keyfile is `type=ethernet`,
  `interface-name=usb0`, fixed
  `uuid`, `autoconnect=true` with a low `autoconnect-priority` (so a real
  network connection is always preferred when both exist), IPv4
  `method=manual, address1=10.12.194.1/24, never-default=true`
  (no gateway is set — nothing to advertise as a route even if a future
  change forgot the dnsmasq option suppression), IPv6 `method=link-local`.
- **dnsmasq**: `install.sh` apt-installs **`dnsmasq-base`** — the binary
  only (verified current on Debian trixie, v2.91-1 per packages.debian.org;
  it ships no systemd service scaffolding of its own — see "OS support" §4)
  — **not** the full `dnsmasq` package, which would register a global
  system service we don't want. Conf file
  [`deploy/usb-network/usbnet-dnsmasq.conf`](../deploy/usb-network/usbnet-dnsmasq.conf)
  installs to `/etc/jasper/usbnet-dnsmasq.conf`:
  `interface=usb0`, `bind-dynamic` (tolerates `10.12.194.1` appearing on the
  interface after dnsmasq starts), `except-interface=lo`, `port=0` (DNS
  listener fully disabled — this instance does DHCP only),
  `dhcp-range=10.12.194.10,10.12.194.20,12h`, empty-valued `dhcp-option=3`
  (router) and `dhcp-option=6` (DNS) to explicitly suppress both, lease
  file under the unit's `RuntimeDirectory` (`/run/jasper-usbnet/`, tmpfs,
  cleared on stop), `log-facility=-` (journal).
- **`jasper-usbnet-dhcp.service`** is device-activated:
  `BindsTo=sys-subsystem-net-devices-usb0.device`, `WantedBy=` the same
  device unit. It exists only while `usb0` exists — zero cost whenever the
  gadget/NCM function is absent (kill-switched, or audio-only legacy
  shape). `MemoryMax=16M`-class bounds plus the hardening set
  `tests/test_systemd_hardening.py` expects of new units.
- **IPv6 link-local + Avahi** give a hostname-based fallback even if DHCP
  never completes — no extra code, just a consequence of Avahi already
  advertising on all multicast-capable interfaces and the NM profile
  bringing up link-local IPv6 unconditionally.
- **No sysctl, no nftables, no `ip_forward` anywhere.** The seam for a
  future opt-in "share this speaker's internet connection with the plugged
  laptop" config is left clean (a config-file-level addition, gated
  behind an explicit opt-in) — nothing toward it is built here.

### Changing the IP

There is no wizard or env knob for `10.12.194.1/24` in v1. To use a
different subnet, edit both
`deploy/usb-network/jts-usb.nmconnection` (`address1=`) and
`deploy/usb-network/usbnet-dnsmasq.conf` (`dhcp-range=`) to match, then
redeploy. Keep them in sync — a mismatch between the interface address and
the DHCP pool leaves the DHCP server handing out a range the interface
itself isn't on.

## Relationship to Raspberry Pi OS's own USB rescue gadget

Raspberry Pi OS Trixie images (2025-10-20 or later) ship
**`rpi-usb-gadget`**, selectable in Raspberry Pi Imager ≥2.0 ("USB Gadget
mode") or via cloud-init. **This is a genuinely different mechanism from
JTS's composite gadget**, verified against the upstream project's own
README (`github.com/raspberrypi/rpi-usb-gadget`, `pios/trixie` branch):

- It uses the legacy **`g_ether`** kernel module, presenting as **CDC-ECM**
  on Linux/macOS and **RNDIS** on Windows — not ConfigFS, not NCM. JTS's
  gadget and the RPi OS rescue gadget are different gadget classes bound
  through different code paths.
- It is **not** a first-boot-only feature — it is persistent
  (`rpi-usb-gadget-ics.service`, a watcher that keeps running every boot,
  polling for a host-side Internet Connection Sharing gateway and
  switching between two NetworkManager profiles).
- Its documented IP is **`10.12.194.1/28`** in its "SHARED" mode (host has
  no ICS), the same number JTS deliberately reuses — chosen specifically
  so a household member who used the rescue gadget to adopt a fresh Pi
  doesn't need to remember two different addresses.

**Load-bearing, unverified-upstream fact: only one gadget can bind the
single dwc2 UDC at a time.** The Pi 5's dwc2 controller has exactly one
USB Device Controller; `g_ether` (module-based) and JTS's ConfigFS/
libcomposite gadget cannot both be bound simultaneously — whichever
claims the UDC first wins, the other fails to bind. This is a well-known
class of conflict for single-UDC hardware (confirmed generically by the
kernel USB gadget docs and community reports of similar multi-function
conflicts), but `rpi-usb-gadget`'s own README does not discuss interacting
with a pre-existing custom ConfigFS gadget, so the **specific** contention
scenario is not addressed by upstream documentation.

**Why this doesn't matter for JTS in practice**: JTS's `install.sh` never
installs or enables `rpi-usb-gadget` — it is an independent Raspberry Pi
Imager/cloud-init opt-in a household could theoretically also select. On a
speaker set up via JTS's own onboarding path (Imager without that option,
or the plain rescue-gadget-then-`scripts/onboard.sh` flow BRINGUP.md/
QUICKSTART.md already teach), `rpi-usb-gadget` is never enabled, so there
is nothing to contend with. If a household enabled BOTH `rpi-usb-gadget`
(via Imager) and let JTS's `jasper-usbgadget.service` run, the two would
race for the UDC on every boot; do not enable `rpi-usb-gadget` on a
JTS-managed speaker. This exact interaction is untested — see checklist
item #7.

## OS support

Claims below are graded by the verification pass that produced this doc
(2026-07-04 research pass); grading follows through to the
hardware-validation checklist for anything that needs a physical device to
confirm.

Audio support and management-network support are separate questions. The
following UAC2 support claim was rechecked against Microsoft's current driver
documentation on 2026-07-16. The computer-microphone direction is standard
UAC2 mono 48 kHz S16 PCM. Microsoft
ships its in-box `usbaudio2.sys` class driver from Windows 10 release 1703
onward, and documents PCM plus asynchronous input/output support. The JTS
descriptor therefore fits the documented Windows audio envelope without a
vendor driver, but the exact composite descriptor has not yet been tested on a
Windows host. The NCM table below applies to the USB management link, not to
whether Windows can use JTS as an audio input. See
[Microsoft's USB Audio 2.0 driver documentation](https://learn.microsoft.com/en-us/windows-hardware/drivers/audio/usb-2-0-audio-drivers).

| OS | NCM support | Grade |
|---|---|---|
| **Windows 11** | In-box `UsbNcm.sys` (Microsoft's open-sourced reference: `microsoft/NCM-Driver-for-Windows`). Correctly sends the NCM-spec zero-length-packet on transfer boundaries. | **Verified.** One real caveat: the driver is present but not always auto-bound by class/subclass alone — some devices need an explicit compatible-ID nudge or a manual "Update Driver → Network adapters → Microsoft → UsbNcm Host Device" in Device Manager. No canonical minimum build number found; treat "Windows 11, any current build" as the verified floor. |
| **Windows 10** | Documented as **unsupported**. | **Verified, with nuance.** Microsoft's own Q&A material frames Windows 10's NCM host-driver ZLP handling as not spec-compliant, and community reports (TI E2E, BeagleBoard forum) show users hunting for third-party drivers. The failure mode is "binds incorrectly / ZLP handling broken" or "nothing binds," not literally "no driver file exists anywhere," but the practical guidance across the ecosystem is: don't rely on Windows 10 for NCM. |
| **macOS** | Native NCM class driver since OS X El Capitan (10.11); solid/reliable framing from Big Sur (11.0) onward — no driver install needed. | **Likely, not primary-sourced.** All evidence is secondary (forums, vendor FAQ pages) — no Apple-authored document naming the driver (commonly referred to as `AppleUSBNCM`) was found. Treat "native since 10.11, solid by 11.0+" as likely true. |
| **macOS + composite UAC2+NCM specifically** | Whether combining a UAC2 audio function and an NCM function on one composite descriptor has macOS-specific quirks. | **Hardware-verified on the current Pi 5 + Mac Studio path (2026-07-15).** Production NCM and host-playback UAC2 enumerate together. A bounded lab descriptor also added a mono host-capture stream; macOS bound it as a one-channel input manufactured by `Jasper Tech Speaker` and recorded real mic audio while NCM remained composed. After a one-time USB-clock warmup, a 15.02 s capture had no CoreAudio overflow or silent 20 ms block. This proves the bounded lab path—not long-run product quality or a blanket macOS-version claim. |
| **Linux** | NCM is a standard `usbnet`/`cdc_ncm` in-kernel driver, auto-binds by class/subclass. | Not separately re-verified in this pass (long-standing, uncontroversial upstream support); treated as a given. |

### dwc2 endpoint capacity — positive bind/capture proof; stress pending

The current BCM2712 (Pi 5) dwc2 controller has enough endpoints to **bind and
transfer** the tested composite: NCM plus UAC2 host playback and a temporary
UAC2 host-capture endpoint. A bounded prototype on 2026-07-15 bound that shape
without `-ENODEV`/`-EBUSY`; macOS fetched its BCD 2.90 descriptor, kept the
existing JTS output, published a one-channel Jasper input, and recorded real
AEC-mic samples into a 48 kHz CoreAudio WAV. The first nonblocking prototype had
continuity gaps. Its blocking successor used ALSA's resampler behind a bounded
drop-oldest queue; after a one-time five-second host-clock warmup, a
720,960-frame / 15.02 s recording reported zero CoreAudio overflows, zero
digital-silent 20 ms blocks, and a 0.1 ms longest zero run. The Pi reported zero
writer errors, then restored the then-current production descriptor (BCD 2.00 /
`p_chmask=0`); gadget, USB Audio Input, fan-in, AEC, and voice were all active
afterward. The superseded prototype scripts were removed when the shipped
`jasper-usbmic` route became the single maintained implementation.

This closes the basic endpoint-allocation unknown for this Pi 5. It does **not**
yet certify simultaneous sustained Mac→Pi music + Pi→Mac mic + heavy NCM
traffic: the proof transferred the return stream while the opposite UAC2
direction was enumerated but idle. That bidirectional traffic/soak case and
xrun/counter evidence remain hardware checklist items #1 and #3.

Repeated development-time descriptor cycling is also not benign on the tested
macOS 26 build: after many back-to-back production/lab swaps, CoreAudio once
lost its entire device graph (including built-in devices), and restarting its
daemon group did not recover it. Physical reconnect or a Mac restart remains
the recovery for that observed wedge. Avoid rapid repeated descriptor cycling;
use the product switch for ordinary operation.

## RAM contract

The load-bearing fact for this table: **`u_ether` registers the `usb0` netdev
at gadget-BIND time, not host-attach time.** So on a composed + bound NCM
gadget, `usb0` exists (and its `sys-subsystem-net-devices-usb0.device` unit is
active) regardless of whether a laptop is plugged in — which means
`jasper-usbnet-dhcp` is resident whenever the network function is composed,
waiting to serve a lease the moment a host attaches. The only truly zero cost
is when NCM is *not* composed (kill-switched, or no UDC pre-reboot). Carrier —
not interface existence — reflects the cable.

| State | Cost | Notes |
|---|---|---|
| Kill-switched (`JASPER_USB_NETWORK=disabled`) AND audio off | Same as the historical zero-RAM contract (~50 KB, the dwc2 kernel module only) | `jasper-usbgadget-wanted` exits non-zero (nothing wanted), the unit's `ExecCondition` skips, libcomposite never loads, `usb0` never appears, `jasper-usbnet-dhcp` never starts. |
| Gadget unavailable in stable host mode, or pending host→peripheral | ~0-50 KB | A Zero USB-output product intentionally stays host-only. When peripheral mode is desired but not active yet, no UDC is available, the gadget's `ExecCondition` skips, and nothing binds. `jasper-doctor` distinguishes the intentional state from a reboot requirement. |
| Pending peripheral→host while the current controller is still peripheral | ~1 MB until reboot | Strict audio availability is false and UAC2 is withdrawn, but `management_transport_available` keeps or restores NCM-only composition so an in-flight deploy over USB does not sever itself. Reboot activates host mode and removes the UDC/NCM residents. |
| Network composed (bound), no host plugged in | ~1 MB | `libcomposite` + `usb_f_ncm`/`u_ether` kernel modules loaded, `ncm.usb0` composed and bound, `usb0` **exists** (carrier down), and the `MemoryMax=16M`-bounded `jasper-usbnet-dhcp` instance **is resident** (device-activated on `usb0`, which is present from bind). Typically far below the cap for a one-pool DHCP server. |
| Network composed, host plugged in, audio off | ~1 MB | Same residents as above; `usb0` now has carrier and the DHCP server hands out a lease. No new persistent cost over the no-host row. |
| Network + audio both on | ~1 MB (network) + the bounded volume observer | The process-free readiness marker adds no resident process; fan-in is already part of the core audio graph. See [HANDOFF-usbsink.md](HANDOFF-usbsink.md) "RAM budget". |

This replaces the historical "~50 KB always, 0 when the audio bridge is
off" framing with a composite-aware one:
the network function's baseline cost (kernel modules **plus the resident
dnsmasq instance**) is now paid whenever `JASPER_USB_NETWORK` isn't explicitly
disabled and a UDC is present, which is the new default on a booted speaker.

## Naming debt

The ConfigFS gadget directory name stays `jts-usb-audio` even though the
gadget is no longer audio-only. This is **accepted debt**, not an
oversight: `jasper-doctor`'s `check_usbsink_low_latency_contract` reads
`/sys/kernel/config/usb_gadget/jts-usb-audio/functions/uac2.usb0/...`
verbatim, and renaming the directory would mean touching that pinned,
protection-listed contract for a purely cosmetic reason. Revisit only if
the low-latency contract itself is ever revisited for other reasons.

## Migration behavior

On upgrade, `install.sh` disables and stops `jasper-usbsink-init.service`
(if present from a prior install) before enabling
`jasper-usbgadget.service`. `jasper-usbsink.service`'s ordering directives
(`Requires=`/`After=`/`PartOf=`) are repointed from the deleted init unit
to `jasper-usbgadget.service`; `jasper-usbsink-volume.service` is repointed
the same way but keeps tracking the **audio readiness marker's** lifecycle
(`PartOf=jasper-usbsink.service`), not the gadget's, since the gadget now
outlives the audio function. `jasper-usbgadget.service` is the first
gadget unit `install.sh` enables — deliberate, since it's the one carrying
the default-on network when hardware permits. A pending host-role reboot keeps
NCM-only composition while the controller is still peripheral so a deploy over
that link can finish; strict USB audio availability remains false. The
migration is idempotent and safe under
`install.sh --dry-run`.

**Restore from canonical intent after the migration.** The init-unit stop above runs
while the *pre-upgrade* graph is still in memory, where `jasper-usbsink` has
`PartOf=jasper-usbsink-init.service` — so stopping the init unit propagates a
stop to a running (possibly playing) audio bridge. `enable --now
jasper-usbgadget.service` is only a *start*, and `PartOf=` never propagates a
start. `enable_usbgadget` (`deploy/lib/install/systemd-units.sh`) therefore
establishes one safe deployment baseline without interpreting intent: disable
and stop the derived USB-audio unit, keep/bring up NCM, and recompose an active
gadget only when old unit state or a present UAC2 card proves stale audio could
still be advertised. The later `reapply_source_intent` call is the single
canonical replay point; for On it performs fan-in DIRECT arm → UAC2 recompose →
readiness-marker start, while invalid intent fails closed. An already-converged
NCM-only deploy does not bounce the management link. Pinned by
`tests/test_install_usbgadget_migration.py`; hardware-checklist item #10.

## Guard acceptance

The management-host guard (`jasper.http_security`) accepts `Host:`
headers for `10.12.194.1`, `10.12.194.1:8780`, and IPv4 link-local
(`169.254.0.0/16`, reachable before/without DHCP completing) alongside the
existing mDNS/LAN acceptance, with a public-IP rejection pinned as the
contrast case. See `tests/test_http_security.py`.

## Observability

- `jasper-doctor` (`jasper/cli/doctor/usbsink.py`) checks that gadget
  composition matches the same gates as the scripts (network enabled ⇒
  `ncm.usb0` present; authorized audio + derived unit enabled + live fan-in
  DIRECT consumer ⇒ `uac2.usb0`; kill-switch + no ready audio ⇒ nothing
  loaded), that `usb0` exists with `10.12.194.1` when NCM
  is composed, that the `jts-usb` NetworkManager profile is active on
  `usb0`, that `jasper-usbnet-dhcp`'s unit state is coherent with `usb0`'s
  presence, and a loopback HTTP probe of
  `http://10.12.194.1/system/data.json` with `Host: <JASPER_HOSTNAME>`
  expecting 200 (mirrors the deploy-time `/system/data.json` verification
  and `check_management_surface` — this pins nginx bind + guard acceptance
  of the fallback URL without needing hardware).
- `/state` carries a compact `usb_network` block
  (`{enabled, iface_present, carrier, address}`), read fresh from
  `/sys/class/net/usb0/*` and the kill-switch env on every call — never
  cached. `carrier=false`/absent is normal (nothing plugged in), never an
  error state.
- Structured logs: `event=usb_gadget.compose|up|down|skip ...` from the
  gadget scripts (the wifi-guardian idiom).

## Hardware-validation checklist

Item 1 now has a positive enumeration/return-capture proof but retains its
simultaneous-traffic stress half. Items 2-8 and 10 otherwise remain open. The
carrierless lifecycle subset of item 9 was verified on JTS3 on
2026-07-15: `usb0` activated with `10.12.194.1` and DHCP active without a host
cable, then automatically reconverged after a full gadget destroy/recreate.
Each item names the specific claim above it verifies.

1. **Composite enumeration on macOS.** Plug a Mac into a configured
   speaker with USB audio enabled: confirm both an NCM network adapter
   and a UAC2 audio device appear, and that dwc2's endpoint capacity holds
   with both functions active simultaneously (isochronous + bulk +
   interrupt all in use at once). Confirms the "dwc2 endpoint capacity"
   claim above and the macOS composite-quirk unknown. **Partial pass
   2026-07-15:** NCM had carried the original management session and remained
   composed, production JTS output was present, and the lab-only return endpoint
   recorded real AEC-mic audio into CoreAudio. The bounded blocking bridge's
   post-warmup 15.02 s continuity run also passed (see the endpoint section
   above). Still run a long-lived bridge with sustained
   Mac→Pi playback and Pi→Mac capture together while exercising NCM before
   calling the traffic-stress half complete.
2. **`jts.local` resolution + fallback + no-hijack.** With the Pi's Wi-Fi
   off, confirm `jts.local` resolves from the plugged-in host over usb0,
   `http://10.12.194.1/` works as a fallback, DHCP hands out a lease, and
   the host keeps its own default route and internet access (i.e. the
   no-forwarding/no-router-push design actually holds on a real DHCP
   client, not just in the dnsmasq conf). **Also confirm dnsmasq itself
   starts and drops privileges cleanly**: `systemctl status
   jasper-usbnet-dhcp` shows `active (running)` (not a `Restart=on-failure`
   loop), the process runs as `nobody:nogroup` (`ps -o user= -C dnsmasq`),
   and the journal shows no "cannot change to user" die — the
   `CapabilityBoundingSet` must include `CAP_SETUID`/`CAP_SETGID` for the
   drop to a non-`dnsmasq` user (dnsmasq-base ships no such user) to succeed.
3. **Audio path unaffected.** With USB audio enabled and playing, hammer
   the web UI over usb0 and confirm no regression in the low-latency
   route's telemetry (fan-in direct-capture xruns, resampler relocks,
   fan-in host-clock ladder state) — the composite gadget must not perturb
   the existing low-latency contract.
4. **Host audio device label unchanged.** Confirm the product-string
   change (dropping "USB Audio" from the NIC-facing string) did not alter
   what the host shows as the *audio* device name — that label is owned
   by the separate name-patch mechanism and should be unaffected, but this
   has not been confirmed post-change. If it did change, revert the
   product-string edit.
5. **Toggle audio on/off from `/sources/` while plugged in.** Confirm the
   host re-enumerates cleanly on both directions (brief network blip is
   expected and acceptable) and that the network function survives both
   transitions.
6. **Multiroom follower parking.** Confirm a parked bonded follower keeps
   its network adapter visible to a plugged-in host while its audio
   device disappears, and that restoring the follower brings audio back
   correctly.
7. **No UDC contention with `rpi-usb-gadget`.** Confirm that a speaker
   set up via JTS's normal onboarding path never has `rpi-usb-gadget`
   enabled, and — if a household has separately opted into it via
   Raspberry Pi Imager — document what actually happens (confirmed
   failure to bind, or unexpected worse behavior) rather than assuming.
8. **Speaker rename.** Confirm a rename triggers a gadget restart that
   updates both the NIC-facing product string and the (separately owned)
   audio device label consistently.
9. **`usb0` + dnsmasq resident with nothing plugged in.** This is the
   load-bearing timing assumption of the RAM contract and the doctor's
   compose/bind failure check: confirm that on a booted speaker with the
   network composed but **no host cable attached**, `usb0` already exists
   (`ip link show usb0` succeeds, carrier down) and `jasper-usbnet-dhcp` is
   `active` — proving `u_ether` registers the netdev at gadget-bind time,
   not host-attach time. If instead `usb0` only appears when a host is
   plugged in, the doctor's "usb0 absent + UDC present = compose/bind
   failure" check and the "dnsmasq resident whenever composed" RAM row need
   to be softened back to a host-attach model.
10. **Live-upgrade over an enabled USB-audio session.** Deploy a new build
    while USB audio is enabled and a host is playing; confirm the installer
    first establishes the network-only baseline without losing management,
    then the canonical source replay rearms fan-in, recomposes UAC2, and restores
    the readiness marker so audio resumes.
11. **Windows UAC2 input.** On a current Windows 11 host (and Windows 10 1703+
    if available), confirm the in-box `usbaudio2.sys` driver binds without a
    vendor driver, `JTS Mic` appears as a mono 48 kHz input, and a sustained
    recording has advancing audio with no gaps or relay drops. Repeat with NCM
    both bound and unbound: Windows audio compatibility must not be conflated
    with the separate management-network driver caveats above.
12. **Return-path latency decomposition.** Add same-clock taps at selected
    chip-frame arrival, `:9894` receive, and ALSA enqueue/pointer progress; read
    the actual UDP receive buffer, pipe capacity, queue depths, and
    `snd_pcm_delay`. Pair that with a host capture to separate Pi-side transport
    from host/USB latency. Do not claim the visible two-period queue plus 40 ms
    UAC2 ring is the complete latency budget until the opaque pipe is removed or
    measured empty under steady-state and stall/recovery tests.

## Cable caveats

- **USB-C-to-USB-C cables hit an open kernel bug**
  ([raspberrypi/linux#6289](https://github.com/raspberrypi/linux/issues/6289))
  and don't work reliably for gadget mode on Pi 5. Use a USB-A-to-USB-C
  cable (the 8086 splitter's host-side leg is USB-A specifically to
  sidestep this).
- **Apple Silicon Macs have a known USB-PD interaction**
  ([raspberrypi/linux#6569](https://github.com/raspberrypi/linux/issues/6569))
  that can break gadget detection. Try a different host if detection
  fails on an Apple Silicon Mac.
- **8086 Consultancy USB-C/PWR Splitter**: the data leg carries the
  gadget link to the host; the power leg stays connected to the Pi's
  normal PSU. See [HANDOFF-usbsink.md](HANDOFF-usbsink.md) "Hardware
  setup" for the full topology diagram — unchanged by this work.
- **Pi 5 halts at boot (solid red LED) on splitter power without
  `usb_max_current_enable`.** The Pi 5 sizes its power budget from the USB-C
  **PD** negotiation, and the splitter (like USB-C power injectors generally)
  does not pass PD through — so the Pi 5 can't confirm a 5 A supply, runs
  power-restricted, and can stop at the *firmware* stage before the OS boots:
  a solid red LED, unreachable on the network, and **no journal at all**
  (nothing reached userspace, so `journalctl --list-boots` shows no entry for
  the attempt) — even with a fully capable PSU on the power leg. `install.sh`'s
  `reconcile_usb_data_role` writes `usb_max_current_enable=1` to
  `/boot/firmware/config.txt` (a second `[all]` step alongside the `dwc2`
  dtoverlay, checked independently so already-deployed gadget boxes backfill it
  on a re-run) to tell the firmware to allow full current without the PD
  handshake. No-op on a box powered by a normal PD supply (PD negotiates 5 A
  anyway); safe with a capable supply — the Pi's own undervoltage detection
  still guards a marginal one. Diagnosed + verified on jts.local 2026-07-06
  (red-LED halt → boots clean with the flag, `EXT5V=5.00 V`, `throttled=0x0`);
  pinned by `tests/test_install_helpers.py::test_reconcile_usb_data_role_keeps_pi5_peripheral_and_power_fix`.

---

Last verified: 2026-07-16 (the Windows UAC2 support envelope was rechecked
against Microsoft's current class-driver documentation; live `jts` probes
separated a genuine-recording ~50 ms pipe / 30–40 ms gadget-ring baseline from
history-dependent frozen idle residuals, so the prior continuity result is no
longer presented as a latency bound; JTS4 active-host Apple-DAC recovery and strict
deploy health verified; hardware-aware USB data-role matrix, pending-role RAM
contract, and conditional NCM availability rechecked;
the `/wake/` USB-microphone intent, `p_chmask`/BCD descriptor split, dedicated
`:9894` relay, dependency lifecycle, assistant-pause independence, and
status/doctor surfaces
were rechecked against the implementation and focused tests;
Pi 5 + Mac Studio composite enumeration and lab-only UAC2 return capture were
hardware-verified, closing basic dwc2 endpoint allocation while leaving
simultaneous bidirectional-audio/NCM stress open;
`jasper-usbsink.service` rechecked as the process-free readiness marker; USB
audio composition requires canonical
source-aware authorization, derived lifecycle readiness, and a live fan-in
DIRECT consumer, with canonical Off dominance and NCM preservation pinned;
NetworkManager's usb0 managed/carrierless policy and automatic activation
after gadget recreation were hardware-verified on JTS3;
the canonical speaker-name reader and install-baseline/source-replay boundary
were rechecked against the root scripts and installer; intent/transition
ownership rechecked against `jasper.source_intent`; this doc retains gadget composition and links to
HANDOFF-source-lifecycle.md for ordering/idempotence. Prior 2026-07-10 light
touch: corrected the two audio-data-plane mentions for the then-standby helper;
fan-in DIRECT-captures the gadget audio.)
