# USB gadget audio source (`jasper-usbsink`) — operational reference

Canonical for the USB Audio Input *data plane*: what captures the gadget, who
owns observed state, and how mux silences it. Neighbouring owners — do not
restate them here: [HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md) (ConfigFS
composition, the descriptor, the NCM network, the reverse `JTS Mic` relay) ·
[HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md) (the enable/disable
transaction) · [HANDOFF-volume.md](HANDOFF-volume.md) (cross-surface mute
policy) · [HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md) (the
`usb_low_latency_48k` route and its evidence gate) ·
[historical/usbsink-implementation-appendix.md](historical/usbsink-implementation-appendix.md)
(the superseded bridge/aloop-solo/`state.json` build record and the rejected
alternatives). The one-pipeline rule is
[ADR-0107](adr/0107-usb-gadget-audio-has-one-capture-pipeline.md).

## Current operational truth (2026-07-15)

USB Audio Input is an opt-in source controlled from `/sources/`; canonical
household intent lives in `/var/lib/jasper/source_intent.env`. The root source
coordinator owns the ordered transition: arm fan-in DIRECT capture before
advertising UAC2, and withdraw UAC2 before disarming capture. Grouped followers
park local USB audio while the composite gadget may keep its management-network
function. [HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md) is canonical
for that transaction; [HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md) is canonical
for ConfigFS composition and the NCM network.

Availability is hardware-resolved, not an environment choice and not a source
toggle side effect. A Zero / Zero 2 W has one shared OTG data port: a
configured registered I²S DAC leaves it available for peripheral/gadget mode;
otherwise JTS reserves it for USB output-DAC host mode and reports USB Audio
Input as `unavailable` while preserving the household's saved intent. Pi 4/5
products keep gadget mode because their separate USB host ports can carry the
output DAC. The resolver and full decision matrix are canonical in
[HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md#usb-data-role-policy).

There is one audio pipeline:

```text
host -> UAC2 gadget -> jasper-fanin USB DIRECT lane -> summed music -> CamillaDSP/outputd
```

That statement is specifically the **host-to-speaker source** data plane. The
optional `/wake/` “Use JTS as a computer microphone” switch adds the reverse
Pi-to-host direction to the same UAC2 function through the independent
`jasper-usbmic` relay; it does not change fan-in ownership. Descriptor and relay
truth are canonical in [HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md).

`jasper-fanin` owns capture, level/activity, mix-mute, resampling, host-clock,
xrun, and route-health telemetry. The Rust `jasper-usbsink-audio` crate/binary
and `/run/jasper-usbsink/state.json` were deleted. Install removes an orphaned
binary and Cargo cache on upgrade.

`jasper-usbsink.service` remains because source and gadget lifecycle need a
stable systemd unit. It is a hardened `Type=oneshot`, `RemainAfterExit=yes`
readiness marker with no resident process. Its two `ExecCondition` gates check
local-source permission and the composed `uac2.usb0` function;
`jasper-usbsink-wait-card 30` then bounds the kernel ALSA-card registration
race. `PartOf=jasper-usbgadget.service` invalidates and re-runs that proof when
the gadget restarts. Active (exited) means readiness passed, not that audio is
currently flowing.

Observed state has two live owners:

- fan-in `STATUS`, selected by the identity-bound `label="usbsink"` DIRECT
  entry, owns `playing`, `rms_dbfs`, `muted`, and direct/resampler counters;
- `/sys/class/udc/*/state == configured`, read through `jasper.usbgadget`, owns
  `host_connected`.

`/state.renderers.usbsink` projects those owners directly. It retains
`combo:true`, `preempted:false`, and `updated_at:null` as lightweight response
compatibility fields; there is no second bridge state behind them.
`/state.audio_graph.fanin.usbsink_input` is the detailed ingress block.
The false `/state.audio_graph.rust_bridge` block no longer exists.

Mux silences a losing USB source with fan-in `MUTE/UNMUTE usbsink`; capture and
pre-mute telemetry continue so a muted-but-streaming host remains observable.
Room correction does not write that policy mute. It acquires mux's owner-bound
`TEST_SELECT correction` diagnostic gate, which excludes every music lane
while keeping mux as the single fan-in policy owner, then releases only its own
lease.
The host-volume observer remains `jasper-usbsink-volume.service` /
`volume_bridge.py`; the host slider is inbound volume intent, not a second audio
data plane. Its unchanged startup mixer read is discovery, tagged
`observation_initial=true` and retried until the active-source gate accepts it;
it yields to a JTS temporary mute already established by another surface. A
real later host value change drops that tag and remains authoritative. The
cross-surface mute policy is canonical in
[HANDOFF-volume.md](HANDOFF-volume.md).

## Device name

A connected Mac names the speaker from the UAC2 **AudioStreaming interface
string**, which the kernel hardcodes (`"Playback Inactive"` / `"Playback
Active"`, plus the `Capture` pair for the optional microphone) in
`f_uac2.c`. macOS prefers it over the configfs-settable `iProduct`, and it is
the one gadget string configfs does not expose — so the compiled module is the
only lever. (Windows uses `iProduct` and already shows the product string.)

Both host labels derive from the Speaker Name: output uses the canonical name,
input appends ` Mic`. [`deploy/usbsink/uac2_name_patch.py`](../deploy/usbsink/uac2_name_patch.py)
finds the four null-terminated tokens **by content** (offset-independent, so it
survives kernel builds) and overwrites them in place, length-preserving and
null-padded, bounded to **14 characters** — the shortest `"Capture Active"`
slot — shortening the base so the ` Mic` suffix always survives. It is
all-or-nothing: if any stock string is missing or ambiguous, nothing is
published. The full name still drives `iProduct`, Bluetooth, and the rest.
[`deploy/usbsink/jasper-usbsink-name-patch`](../deploy/usbsink/jasper-usbsink-name-patch)
builds the patched copy into `/lib/modules/$(uname -r)/updates/`, which modprobe
searches before `kernel/`, runs `depmod`, and `rmmod`s a stale in-memory module
so the next gadget-up autoloads the override. A versioned marker (patch schema,
kernel version, speaker name, derived mic name, stock-module hash) makes the
steady-state boot a no-op, and `event=usbsink_name.*` reports what happened.
Until the index update lands, `jasper-doctor` reports `usbsink name` as a warn
rather than claiming a label the host is not showing.

## Operational checks

```sh
curl -s http://jts.local:8780/state | jq '{renderer:.renderers.usbsink, ingress:.audio_graph.fanin.usbsink_input}'
systemctl status jasper-usbsink.service jasper-usbgadget.service jasper-fanin.service
jasper-doctor
```

For the optional reverse `JTS Mic` direction, the active-only 120 ms doctor
gate and the identity-bound `jasper-usb-mic-latency-artifact` certification
workflow are canonical in
[HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md#toggling-and-choosing-the-computer-microphone-from-wake).
That tool measures relay-emit→final-ALSA-write; it does not claim the
host-to-speaker route latency, which is measured by the click/capture harness in
[HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md).

## RAM budget

The readiness marker has zero resident-process RAM. USB-specific incremental
userspace cost is the non-real-time volume observer; fan-in is already the
shared source mixer. A sustained DIRECT-capture failure makes USB unavailable
and is surfaced by combo health/doctor; there is intentionally no hidden aloop
fallback.
