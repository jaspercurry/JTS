# Handoff: USB composite gadget — management network + USB audio input

**Status: operational.** Canonical for the ConfigFS composite USB gadget
(`jts-usb-audio`) that carries two independent USB functions off the Pi's
single dwc2 controller: an always-on **management network** (`ncm.usb0`)
and the wizard-toggled **USB audio input** source (`uac2.usb0`, owned
operationally by [HANDOFF-usbsink.md](HANDOFF-usbsink.md)). This doc is the
single source of truth for gadget composition and the USB network; the
audio-source design (volume model, fan-in wiring, low-latency route) stays
in HANDOFF-usbsink.md and HANDOFF-usb-low-latency.md.

## Mission

A laptop plugged into the speaker's USB-C port (via the 8086 splitter's data
leg, or a USB-A→C cable on a bare Pi) gets a network link to the Pi so
`http://<JASPER_HOSTNAME>/` works even when the Pi has no Wi-Fi. When Wi-Fi
and USB are both up, the experience is invisible — same hostname, either
path. Multiple speakers keep distinct hostnames (mDNS) and distinct MACs
(derived from each Pi's CPU serial).

## Product decisions

1. **USB networking is always-on** after install (default enabled at boot).
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
6. **Hardware validation is deferred.** Every claim below that can only be
   confirmed on real hardware is called out and repeated in the checklist
   at the bottom. Nothing here has been run against a physical Pi yet.

## Unit topology

```
jasper-usbgadget.service            (NEW — the composite gadget owner)
  ├─ ExecCondition: jasper-usbgadget-wanted   (skip if no UDC, or nothing wanted)
  ├─ ExecStart:     jasper-usbgadget-up       (composes ncm.usb0 and/or uac2.usb0)
  └─ ExecStop:      jasper-usbgadget-down

jasper-usbnet-dhcp.service          (NEW — device-activated dnsmasq on usb0)
  BindsTo=sys-subsystem-net-devices-usb0.device

jasper-usbsink.service              (audio bridge — unchanged data plane)
  Requires=/PartOf=jasper-usbgadget.service   (repointed from the deleted init unit)
```

`jasper-usbsink-init.service` — the old audio-only gadget-owner oneshot —
is **deleted**. `jasper-usbgadget.service` is its replacement and does more:
it is the single owner of the ConfigFS descriptor for *both* functions, and
unlike the old init unit it is enabled and started **unconditionally** at
install time (it carries the always-on network, so it cannot be gated on
audio being enabled or on multiroom follower status).

### Function truth table

Computed once per `jasper-usbgadget-up` run and logged as a structured
`event=usb_gadget.compose network=<0|1> audio=<0|1> ...` line:

| `JASPER_USB_NETWORK` | audio intent (`jasper-usbsink.service` enabled) + local-source-allowed | functions composed |
|---|---|---|
| enabled (default) | yes | `ncm.usb0` + `uac2.usb0` |
| enabled | no / parked follower | `ncm.usb0` only |
| disabled | yes | `uac2.usb0` only (legacy, audio-only shape) |
| disabled | no | none — the unit's `ExecCondition` already skipped the whole unit |

The audio gate lives **inside** `jasper-usbgadget-up`, not on the unit
itself — `jasper-usbgadget.service` has no
`jasper-local-source-allowed` `ExecCondition`, because the network function
must keep serving even when this speaker is a parked multiroom follower.
`jasper-usbsink.service` keeps its own `ExecCondition` unchanged: it never
starts the audio daemon while parked, it only stops recomposing the gadget.

### Toggling audio from `/sources/`

Enable is a **three-step** order (`jasper.web.sources_setup._apply`, pinned by
`tests/test_sources_setup_usbsink.py`): (1) `enable` the bridge — record
household intent, but do **not** start it yet, or it would race a card that
does not exist; (2) restart `jasper-usbgadget.service` so it recomposes and
adds `uac2.usb0` (the gadget reads the now-enabled intent at recompose time and
the `UAC2Gadget` card appears); (3) `start` the bridge — its
`ExecStartPre=jasper-usbsink-wait-card` finds the card immediately. The order
matters: composing first would fail because is-enabled would still be false at
recompose time, and starting first would 30 s-timeout on the wait-card.

Disable: `disable --now jasper-usbsink.service`, then restart
`jasper-usbgadget.service` (recomposes **without** `uac2.usb0` — the network
function is untouched). A brief host-visible re-enumeration ("Playback
Inactive" flicker, momentary network blip) is expected on either transition and
is a hardware-checklist item (#5) — see below.

### Multiroom follower parking

Parking a bonded follower stops the audio daemon and units per the existing
local-source registry, and **restarts** (not stops) the gadget-owning unit
so it recomposes to drop `uac2.usb0` — the host stops seeing a USB audio
device from a follower, while the USB management network keeps working (it
must, since the household may need to reach the follower's management UI
directly). The park order is **stop-audio then recompose** so the recompose
reads "audio parked" and drops `uac2.usb0`.

Restoring a follower is the deliberate **mirror image**: **recompose the
gadget first** (it re-adds `uac2.usb0` iff USB audio is enabled, and the
`UAC2Gadget` card reappears), **then** restore the audio unit (its
`wait-card` ExecStartPre finds the card immediately). The reverse order (start
the bridge before the recompose) made `wait-card` poll a card that only exists
after the recompose that would come next — a guaranteed 30 s stall + failed
`jasper-grouping-reconcile` transient on every un-park. Both orders are pinned
in `jasper.multiroom.reconcile.plan` by
`tests/test_multiroom_reconcile.py::test_plan_follower_parks_usbsink_bridge_and_recomposes_gadget`
(park) and
`::test_plan_restore_recomposes_gadget_before_restarting_bridge` (restore).

### Edge cases the truth table preserves

- **Fresh install, pre-reboot** (dtoverlay not yet applied → no UDC under
  `/sys/class/udc`): `jasper-usbgadget-wanted` exits non-zero, the unit's
  `ExecCondition` skips cleanly — **not** a unit failure. `jasper-doctor`'s
  existing dtoverlay check tells the operator to reboot.
- **Kill switch flipped at runtime**: an operator restarts
  `jasper-usbgadget.service`; recompose honors the new value immediately.
- **`systemctl stop jasper-usbgadget`** (operator-initiated): `PartOf=`
  propagation stops the audio daemon too; both kernel modules unload; the
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
- **Testability**: the ConfigFS root, UDC class dir, CPU-serial file, and
  the audio-intent/gate probe commands are all env-overridable
  (`JASPER_CONFIGFS_ROOT`, `JASPER_UDC_CLASS_DIR`, `JASPER_CPUINFO_FILE`,
  `JASPER_USBGADGET_AUDIO_INTENT_CMD`, `JASPER_USBGADGET_AUDIO_GATE_CMD`),
  so `tests/test_usbgadget_script.py` drives the scripts hermetically
  against a temp dir, mirroring `tests/test_wifi_guardian_script.py`. The
  defaults are the real production paths — overriding them never changes
  runtime behavior on an actual Pi.

## Network design

NetworkManager is the box's **single** network owner for `usb0` — no
systemd-networkd, no dispatcher scripts.

- **NM keyfile**: [`deploy/usb-network/jts-usb.nmconnection`](../deploy/usb-network/jts-usb.nmconnection),
  installed to `/etc/NetworkManager/system-connections/jts-usb.nmconnection`
  (mode `0600`, root:root) by `install.sh`, followed by a best-effort
  `nmcli connection reload`. `type=ethernet`, `interface-name=usb0`, fixed
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

| OS | NCM support | Grade |
|---|---|---|
| **Windows 11** | In-box `UsbNcm.sys` (Microsoft's open-sourced reference: `microsoft/NCM-Driver-for-Windows`). Correctly sends the NCM-spec zero-length-packet on transfer boundaries. | **Verified.** One real caveat: the driver is present but not always auto-bound by class/subclass alone — some devices need an explicit compatible-ID nudge or a manual "Update Driver → Network adapters → Microsoft → UsbNcm Host Device" in Device Manager. No canonical minimum build number found; treat "Windows 11, any current build" as the verified floor. |
| **Windows 10** | Documented as **unsupported**. | **Verified, with nuance.** Microsoft's own Q&A material frames Windows 10's NCM host-driver ZLP handling as not spec-compliant, and community reports (TI E2E, BeagleBoard forum) show users hunting for third-party drivers. The failure mode is "binds incorrectly / ZLP handling broken" or "nothing binds," not literally "no driver file exists anywhere," but the practical guidance across the ecosystem is: don't rely on Windows 10 for NCM. |
| **macOS** | Native NCM class driver since OS X El Capitan (10.11); solid/reliable framing from Big Sur (11.0) onward — no driver install needed. | **Likely, not primary-sourced.** All evidence is secondary (forums, vendor FAQ pages) — no Apple-authored document naming the driver (commonly referred to as `AppleUSBNCM`) was found. Treat "native since 10.11, solid by 11.0+" as likely true. |
| **macOS + composite UAC2+NCM specifically** | Whether combining a UAC2 audio function and an NCM function on one composite descriptor has macOS-specific quirks. | **Unverified — no direct evidence found.** Searches turned up only generic USB-Ethernet-dongle complaints unrelated to composite gadgets, plus a suggestive-but-not-specific note that macOS is less forgiving of composite Linux gadgets generally (`g_multi.ko`). Do not assume a specific failure mode here; hardware checklist item #1 is the only way to close this. |
| **Linux** | NCM is a standard `usbnet`/`cdc_ncm` in-kernel driver, auto-binds by class/subclass. | Not separately re-verified in this pass (long-standing, uncontroversial upstream support); treated as a given. |

### dwc2 endpoint capacity — unverified, needs hardware measurement

Whether the BCM2712 (Pi 5) dwc2 controller has enough USB endpoints to
carry a composite gadget with **both** UAC2 (isochronous IN capture +
feedback) and NCM (bulk IN/OUT + interrupt IN) functions simultaneously is
**not verified by any source found**. No document gives a hard endpoint
count for BCM2712's dwc2 instance, nor an explicit statement that this
specific combination fits or doesn't. What is corroborated only
indirectly: the Pi 5's dwc2 is confirmed to be the sole USB
peripheral-mode controller (no XHCI-peripheral alternative), so whatever
budget exists is a singular constraint; and community reports describe
*other* multi-function composite conflicts (e.g. RNDIS + UAC1, "only one
will work," on some Pi setups), suggesting composite endpoint exhaustion
is a known real-world failure class on dwc2 gadgets generally — but no
source quantifies whether UAC2+NCM specifically fits. **Do not treat this
as settled.** Hardware checklist item #1 covers the only reliable way to
know: bind the composite gadget with both functions and check for
`-ENODEV`/`-EBUSY` from libcomposite at UDC-bind time, or read
`/sys/kernel/debug/usb/*/state` if available.

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
| No UDC (fresh install pre-reboot) | ~50 KB (dwc2 module only) | The dtoverlay is set but the OTG controller isn't peripheral yet, so the gadget's `ExecCondition` skips and nothing binds. `jasper-doctor`'s dtoverlay check tells the operator to reboot. |
| Network composed (bound), no host plugged in | ~1 MB | `libcomposite` + `usb_f_ncm`/`u_ether` kernel modules loaded, `ncm.usb0` composed and bound, `usb0` **exists** (carrier down), and the `MemoryMax=16M`-bounded `jasper-usbnet-dhcp` instance **is resident** (device-activated on `usb0`, which is present from bind). Typically far below the cap for a one-pool DHCP server. |
| Network composed, host plugged in, audio off | ~1 MB | Same residents as above; `usb0` now has carrier and the DHCP server hands out a lease. No new persistent cost over the no-host row. |
| Network + audio both on | ~1 MB (network) + the existing usbsink audio-daemon cost | See [HANDOFF-usbsink.md](HANDOFF-usbsink.md) "RAM budget" for the audio side — unchanged by this work. |

This replaces the historical "~50 KB always, 0 when the audio bridge is
off" framing in HANDOFF-usbsink.md's RAM table with a composite-aware one:
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
the same way but keeps tracking the **audio daemon's** lifecycle
(`PartOf=jasper-usbsink.service`), not the gadget's, since the gadget now
outlives the audio function. `jasper-usbgadget.service` is the first
gadget unit `install.sh` enables — deliberate, since it's the one carrying
the always-on network. The migration is idempotent and safe under
`install.sh --dry-run`.

**Restore-if-enabled after the migration.** The init-unit stop above runs
while the *pre-upgrade* graph is still in memory, where `jasper-usbsink` has
`PartOf=jasper-usbsink-init.service` — so stopping the init unit propagates a
stop to a running (possibly playing) audio bridge. `enable --now
jasper-usbgadget.service` is only a *start*, and `PartOf=` never propagates a
start, so without a fix an enabled USB-audio bridge would be left stopped until
the next reboot. `enable_usbgadget` (`deploy/lib/install/systemd-units.sh`)
therefore does a restore-if-enabled: `systemctl is-enabled --quiet
jasper-usbsink.service && systemctl start jasper-usbsink.service`. This runs
**unconditionally** (deliberately not gated on `SKIP_RESTART`) because the
migration's stop is itself unconditional — honoring `SKIP_RESTART` here would
leave the bridge down until reboot, the worse outcome. Pinned by
`tests/test_install_usbgadget_migration.py`; hardware-checklist item #10.

## Guard acceptance

The management-host guard (`jasper.http_security`) accepts `Host:`
headers for `10.12.194.1`, `10.12.194.1:8780`, and IPv4 link-local
(`169.254.0.0/16`, reachable before/without DHCP completing) alongside the
existing mDNS/LAN acceptance, with a public-IP rejection pinned as the
contrast case. See `tests/test_http_security.py`.

## Observability

- `jasper-doctor` (`jasper/cli/doctor/usbsink.py`) checks that gadget
  composition matches intent (network enabled ⇒ `ncm.usb0` present;
  audio intent ⇒ `uac2.usb0` present + daemon active; kill-switch + no
  audio ⇒ nothing loaded), that `usb0` exists with `10.12.194.1` when NCM
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

None of the following has been run against physical hardware as of this
writing. Each item names the specific claim above it verifies.

1. **Composite enumeration on macOS.** Plug a Mac into a configured
   speaker with USB audio enabled: confirm both an NCM network adapter
   and a UAC2 audio device appear, and that dwc2's endpoint capacity holds
   with both functions active simultaneously (isochronous + bulk +
   interrupt all in use at once). Confirms the "dwc2 endpoint capacity"
   claim above and the macOS composite-quirk unknown.
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
   route's telemetry (bridge xruns, resampler relocks, host-clock ladder
   state) — the composite gadget must not perturb the existing
   low-latency contract.
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
    while USB audio is enabled and a host is playing; confirm audio resumes
    after the deploy (the migration stops the old init unit, which under
    the pre-upgrade graph propagates a stop to `jasper-usbsink` —
    `enable_usbgadget`'s restore-if-enabled must bring the bridge back).

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

---

Last verified: 2026-07-04
