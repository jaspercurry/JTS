# USB composite gadget — hardware evidence and incidents (2026-07) — historical

> **Status: historical.** Frozen record of the hardware runs and the one
> host-wide incident behind the composite gadget's current shape. Every
> number describes the box, host OS, and build of its stated date. Current
> operational truth is [HANDOFF-usb-gadget.md](../HANDOFF-usb-gadget.md).

## dwc2 endpoint capacity — bind and capture proof (2026-07-15)

The BCM2712 (Pi 5) dwc2 controller has enough endpoints to **bind and
transfer** the tested composite: NCM plus UAC2 host playback plus a temporary
UAC2 host-capture endpoint. A bounded prototype bound that shape without
`-ENODEV`/`-EBUSY`; macOS fetched its BCD 2.90 descriptor, kept the existing
JTS output, published a one-channel Jasper input, and recorded real AEC-mic
samples into a 48 kHz CoreAudio WAV.

The first nonblocking prototype had continuity gaps. Its blocking successor
used ALSA's resampler behind a bounded drop-oldest queue; after a one-time
five-second host-clock warmup, a 720,960-frame / 15.02 s recording reported
zero CoreAudio overflows, zero digital-silent 20 ms blocks, and a 0.1 ms
longest zero run. The Pi reported zero writer errors, then restored the
then-current production descriptor (BCD 2.00 / `p_chmask=0`).

This closed the basic endpoint-allocation unknown for the Pi 5. It did not
certify simultaneous sustained Mac→Pi music + Pi→Mac mic + heavy NCM traffic:
the proof transferred the return stream while the opposite UAC2 direction was
enumerated but idle.

**Development caution that came out of the same work:** repeated
descriptor cycling is not benign on the tested macOS 26 build. After many
back-to-back production/lab swaps, CoreAudio once lost its entire device
graph, including built-in devices. Use the product switch for ordinary
operation rather than rapid descriptor cycling.

## Return-path soak (2026-07-17)

The in-process ALSA writer passed an uninterrupted Mac CoreAudio capture for
7,200.3 seconds: 345,595,200 host frames in 359,995 callbacks with zero host
status errors. The relay stayed on one PID with zero service restarts, writer
xruns, packet loss, sequence resets/reorders/discontinuities, or new
streaming-drop deltas. Bridge-emit→final-ALSA-write p95 normally stayed
24–35 ms and briefly reached 52.5 ms under deliberate CPU/memory pressure,
below the 80 ms evidence target. The run included 91.1 seconds of
simultaneous bidirectional UAC2 audio plus roughly 256 MiB of NCM traffic in
each direction; neither the overlap nor the pressure run produced an xrun or
stream-integrity fault.

**The run evidence-refuted the plan's `writer_splices <= 1 per 10 min` number
without refuting the design.** There were 22 bounded, insert-only 20 ms
corrections in 120 minutes (1.83 per 10 minutes); ordinary later cadence
approached one correction per 197 seconds. That is the expected result of the
plan's own ~100 ppm independent-clock premise: a 20 ms excursion at 100 ppm is
consumed in about 200 seconds, or roughly three corrections per 10 minutes.
Meeting the one-per-10-minute number would require running near an empty
ring, increasing the verified 40 ms buffer and latency, or adding an adaptive
resampler/DLL. **Judge this controller by bounded correction duty plus zero
xrun/loss/sequence failures, not by that internally inconsistent historical
number.** The original execution plan is preserved byte-for-byte in
[`PLAN-usb-mic-export-latency-fix.md`](../PLAN-usb-mic-export-latency-fix.md).

A separate 7,200.884-second Pi system soak recorded 241 samples with zero
tracked-service restarts, memory-pressure events, OOMs, or warning-level
journal entries. The sampler used for that run omitted `jasper-usbmic.service`
and `jasper-usbnet-dhcp.service`; manual service, cgroup, and journal evidence
showed both on one PID with zero restarts or failures throughout. The tracker
now includes both resident units.

## Capture-latency knob A/B (2026-07-16)

On build `1b1b36015`, PortAudio negotiated 80 ms with
`JASPER_AEC_CAPTURE_LATENCY` unset and 20 ms with `low` on the same XVF3800.
During real macOS CoreAudio pulls the corresponding 30-second USB-microphone
artifacts passed at p95 46.1 ms and 19.3 ms respectively, with zero run-delta
packet loss, streaming drops, writer splices, or xruns; 50/60-second host
captures reported zero callback errors. The artifact metric begins at bridge
emit and therefore does not directly include the 60 ms capture-buffer
reduction.

`low` stays an opt-in experiment, not the production default, until the
shared wake/voice soak and wake-rate parity gates pass — the same capture
stream feeds voice and wake.

## macOS total-audio wedge (2026-07-22 incident)

An ordinary, non-development failure captured on the Mac Studio. This is a
**whole composite-gadget/controller wedge**, not a stalled AEC bridge, mic
relay, Spotify client, browser, or speech application.

- macOS still enumerated the physical JTS USB device, but Core Audio reported
  zero audio devices. Spotify refused to start tracks, YouTube waited
  forever, and every microphone disappeared. The JTS NCM interface showed
  link locally but could not ping the device address.
- The Pi still reported the UDC as `configured`, `usb0` as `LOWER_UP`, and
  all JTS audio services as active. AEC and the USB-mic source kept producing
  fresh packets. **Healthy userspace surfaces cannot detect this failure by
  themselves** — which is why the gadget unit now takes controller snapshots
  around every unbind/bind.
- macOS logs show the first hard failure while stopping JTS's playback
  stream, exactly 30 seconds after a successful Wispr Flow dictation ended:
  endpoint `0x02` aborted, endpoint `0x84` returned `0xe00002ed`, then the
  control endpoint timed out with `0xe00002d6` while selecting alternate
  setting 0. Every later audio start failed with the same USB timeout.
- DWC2 debugfs simultaneously held a pending endpoint interrupt
  (`GINTSTS=0x04048038`, `DAINT=0x00000002`, `DIEPINT(1)=0x90`/`0x2090`) with
  queued requests unfinished. The Pi kernel journal had no matching fault.
- Core Audio attributed the JTS streams to Wispr Flow's audio-service PID.
  That identifies the client whose normal stream-stop exposed the fault; it
  does not prove Flow caused it.

Restarting **only** `jasper-usbgadget.service` over Wi-Fi re-enumerated JTS,
restored NCM ping, and brought Mac audio back without restarting the Mac or
the applications. This A/B recovery establishes JTS USB as causal for the
host-wide outage.

The exact kernel/function race is unproven. The mic-enabled descriptor is a
leading hypothesis because it adds endpoint `0x84`, which faulted immediately
after playback endpoint `0x02` stopped — and selecting a different microphone
in an app does not remove that endpoint, since `p_chmask=1` remains on the
same UAC2 function until the Mac-microphone switch is turned off. But the
first failed operation was the playback stream *stop*, so the evidence does
not justify blaming the mic feature alone. Turning the Mac microphone off in
`/wake/` when it is not needed is reasonable risk reduction and a useful A/B,
not a guaranteed fix.

Raspberry Pi OS offered `6.18.34-1+rpt1` while the incident box ran
`6.12.75-1+rpt1`. A source comparison of `rpi-6.12.y` and `rpi-6.18.y`
DWC2/UAC2 drivers found no endpoint-disable or audio stream-stop fix matching
this signature; the shared `u_audio.c` engine was unchanged. Do not treat
that kernel upgrade as an incident fix without a controlled soak/rollback
window.

## Zero 2 W USB data-role migration (2026-07-14 → 2026-07-15)

Pre-reboot on JTS4: the board identified as Raspberry Pi Zero 2 W; its config
forced `dwc2,dr_mode=peripheral`; no registered I²S/HAT overlay or output DAC
was observable because the shared port was not acting as a host. The
migration therefore resolved `host` and reported a pending reboot.

Post-reboot closed the loop: JTS4 resolved an active host role, detected its
Apple USB-C output DAC with a ready output-hardware artifact and ALSA outputd
backend, kept Bluetooth enabled, reported USB Audio Input intentionally
unavailable, and passed strict deploy health with 0 failures / 0 warnings.
This proves the Zero USB-output path; it does not claim positive UAC2/gadget
hardware validation.

## Legacy `10.12.194.1/24` migration

The original USB network reused Raspberry Pi OS's rescue-gadget device
address fleet-wide. The replacement — one derived /30 per speaker — and the
reason are [ADR-0105](../adr/0105-each-speaker-derives-its-own-usb-subnet.md).

Migration mechanics as built: on upgrade the installer first stages the
derived plan. If `usb0` is live on any different address, promotion is
explicitly deferred — neither installed projection nor either running
consumer is touched, and
`event=install.usb_network_migration_pending activation=next_boot
live_files=preserved` records the boundary. This holds even when the same
install path later recomposes the gadget for USB Audio Input; that recompose
returns on the complete legacy pair and cannot strand an install riding it.
At the next boot, before NetworkManager and the gadget start, the plan
service publishes both generated projections through the canonical durable
atomic-file writer under the owner lock, then removes the pending marker.
There is no filesystem primitive that atomically replaces two files:
catchable replacement failures restore the prior pair, and a process death
between replacements is repaired by the next successful plan run before the
gadget may expose `usb0`. During a full-profile install both projection
destinations join the enclosing unit-generation rollback snapshot.

The management-host guard keeps accepting the legacy `10.12.194.1` during
migration; that acceptance is the thing to delete once no box carries the old
generation.

## Relationship to Raspberry Pi OS's own rescue gadget

Raspberry Pi OS Trixie images (2025-10-20 or later) ship **`rpi-usb-gadget`**,
selectable in Raspberry Pi Imager ≥2.0 ("USB Gadget mode") or via cloud-init.
Verified against the upstream README (`github.com/raspberrypi/rpi-usb-gadget`,
`pios/trixie` branch), it is a genuinely different mechanism:

- It uses the legacy **`g_ether`** module, presenting as **CDC-ECM** on
  Linux/macOS and **RNDIS** on Windows — not ConfigFS, not NCM.
- It is **not** first-boot-only: `rpi-usb-gadget-ics.service` keeps running
  every boot, polling for a host-side Internet Connection Sharing gateway and
  switching between two NetworkManager profiles.
- Its documented IP is `10.12.194.1/28` in "SHARED" mode.

**Load-bearing, unverified-upstream fact: only one gadget can bind the single
dwc2 UDC at a time.** `g_ether` and JTS's ConfigFS/libcomposite gadget cannot
both be bound — whichever claims the UDC first wins. This is a well-known
class of conflict for single-UDC hardware, but `rpi-usb-gadget`'s README does
not discuss interacting with a pre-existing custom ConfigFS gadget, so the
specific contention scenario is not addressed upstream and remains untested
here.

In practice JTS's `install.sh` never installs or enables `rpi-usb-gadget`, so
on a speaker set up through JTS's own onboarding there is nothing to contend
with. Do not enable it on a JTS-managed speaker.

## OS support grading (2026-07-04 research pass, UAC2 rechecked 2026-07-16)

Audio support and management-network support are separate questions. The
NCM table is about the management link only.

| OS | NCM support | Grade |
|---|---|---|
| **Windows 11** | In-box `UsbNcm.sys` (Microsoft's open-sourced `microsoft/NCM-Driver-for-Windows`). Correctly sends the NCM-spec zero-length packet on transfer boundaries. | **Verified.** Caveat: present but not always auto-bound by class/subclass alone — some devices need an explicit compatible-ID nudge or a manual "Update Driver → Network adapters → Microsoft → UsbNcm Host Device". No canonical minimum build; treat "Windows 11, any current build" as the floor. |
| **Windows 10** | Documented as **unsupported**. | **Verified, with nuance.** Microsoft's own Q&A frames Windows 10's NCM host-driver ZLP handling as not spec-compliant; community reports show users hunting third-party drivers. The failure mode is "binds incorrectly / ZLP handling broken", not "no driver file exists". Do not rely on Windows 10 for NCM. |
| **macOS** | Native NCM class driver since OS X El Capitan (10.11); solid framing from Big Sur (11.0) onward. | **Likely, not primary-sourced.** All evidence is secondary; no Apple-authored document naming the driver was found. |
| **macOS + composite UAC2+NCM** | Whether one composite descriptor carrying both has macOS-specific quirks. | **Hardware-verified on Pi 5 + Mac Studio (2026-07-15).** Production NCM and host-playback UAC2 enumerate together; a bounded lab descriptor added a mono host-capture stream that macOS bound as a one-channel input and recorded real mic audio while NCM stayed composed. Proves the bounded lab path, not long-run product quality or a blanket macOS-version claim. |
| **Linux** | Standard `usbnet`/`cdc_ncm` in-kernel driver, auto-binds by class/subclass. | Treated as a given; not separately re-verified. |

For UAC2 audio specifically, Microsoft ships its in-box `usbaudio2.sys` class
driver from Windows 10 release 1703 onward and documents PCM plus
asynchronous input/output support, so the JTS descriptor fits the documented
Windows envelope without a vendor driver — but the exact composite descriptor
has not been tested on a Windows host.
