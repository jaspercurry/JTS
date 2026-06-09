# Dual Apple USB-C DAC Lab Runbook

> **Status: lab-derived topology evidence.** This is not a sound-emitting
> product path yet. The current product path still assumes one coherent
> physical output device for normal listening, but
> `jasper.output_topology` can now model this exact measured
> one-DAC-per-speaker pair as `dual_apple_usb_c_dac_4ch` when
> exactly two Apple child DACs are observed on the expected same USB
> controller/bus with four physical outputs total. Stored drift evidence is
> validation evidence for confidence and doctor/readiness reporting; it is not
> the only thing that makes the hardware profile exist. Missing or partial live
> hardware observation blocks the composite clock report.

This runbook validates the best-case experimental topology for two Apple
USB-C to 3.5 mm adapters on Raspberry Pi 5:

- one Apple DAC per speaker;
- each DAC's left/right channels stay inside that speaker's
  woofer/tweeter crossover;
- no ALSA `multi`, `dmix`, `plug`, or CamillaDSP aggregate device;
- one lab Rust process opens both pinned hardware PCMs directly.

The experiment answers whether this exact pair is usable as a measured
clock-domain setup. It does not promote arbitrary dual USB DACs, ALSA
aggregate devices, or CamillaDSP multi-device playback to supported JTS
topologies.

## Current Results

**Conclusion as of 2026-06-03:** proceed with this as a controlled JTS
output architecture for the one-DAC-per-speaker topology. The evidence
supports two serial-pinned Apple USB-C DACs when one Rust process owns
both hardware PCMs directly, primes silence, uses conservative startup,
and aborts on xrun, disconnect, or frame mismatch. This is not a generic
ALSA aggregate-device endorsement; the supported shape is one Apple DAC
per speaker, with each speaker's woofer/tweeter crossover kept inside a
single physical DAC.

### Hardware observed on `jts.local`

The successful 2026-06-03 runs used exactly two Apple `05ac:110a`
adapters. Both enumerated as full-speed USB Audio devices on the same
Pi 5 USB2 controller/bus:

| Serial | ALSA card | PCM | USB path | Controller | Notes |
|---|---:|---|---|---|---|
| `DWH53530FHL2FN3AC` | `2` / `A` | `hw:2,0` | `usb1/1-2`, bus `1`, devpath `2` | `xhci-hcd.0` | Playback only during the final tests |
| `DWH53530FLL2FN3A3` | `4` / `A_1` | `hw:4,0` | `usb1/1-1`, bus `1`, devpath `1` | `xhci-hcd.0` | Playback plus mono capture with the attached analog load |

`/proc/asound/card*/stream0` reported both playback endpoints as
`Endpoint: 0x02 (2 OUT) (SYNC)`, 48 kHz only, with `S24_3LE` and
`S16_LE` altsets. This is useful descriptor evidence, but it does not
prove the two DACs share an analog timing basis. Treat them as separate
clock domains until a common-ADC drift capture proves otherwise.

Operationally important finding: when two identical Apple adapters are
active, the product `CARD=A` style is ambiguous (`A` and `A_1`). The lab
binary avoids this by resolving the exact serials to direct `hw:CARD,0`
PCMs at run time. The normal JTS output path must remain parked during
this experiment.

The common-clock capture rig used a Focusrite Scarlett Solo 4th Gen on
ALSA card `5` / `hw:5,0`, recording `S32_LE`, 48 kHz, 4 channels. The
Scarlett capture inputs saw only channels 1 and 2 because each Apple
adapter was tapped through the available mono leg of the 3.5 mm to XLR
cabling. This proves one analog channel from each DAC; it does not prove
the unused right-channel wiring.

### Runs completed on 2026-06-03

All sound-emitting runs were performed only after the operator reported
dummy loads/no speakers on the DAC outputs. `jasper-outputd`,
`jasper-voice`, and `jasper-camilla` were stopped before the armed runs;
the lab binary also refuses to run while `jasper-outputd` or
`jasper-voice` are active.

| Evidence directory on `jts.local` | Mode | Duration | Level | Result |
|---|---:|---:|---:|---|
| `/home/pi/jts/logs/dual-apple-dac-lab-20260603T092813-0400-silence-30s` | `silence` | 30 s | `-60 dBFS` | Pass |
| `/home/pi/jts/logs/dual-apple-dac-lab-20260603T092904-0400-silence-600s` | `silence` | 600 s | `-60 dBFS` | Pass |
| `/home/pi/jts/logs/dual-apple-dac-lab-20260603T094046-0400-identity-16s` | `identity` | 16 s | `-60 dBFS` | Pass as output stability; capture identity inconclusive |
| `/home/pi/jts/logs/dual-apple-dac-lab-20260603T095211-0400-soundcheck-identity-20s` | `identity` | 20 s | `-60 dBFS` | Pass as output stability |
| `/home/pi/jts/logs/dual-apple-dac-lab-20260603T095313-0400-ticks-900s` | `ticks` | 900 s | `-60 dBFS` | Pass |
| `/home/pi/jts/logs/dual-apple-dac-lab-20260603T110827-0400-scarlett-identity-24s` | `identity` + Scarlett capture | 24 s | `-60 dBFS` | Pass for captured mono taps: Scarlett ch1 = DAC A ch0, ch2 = DAC B ch0 |
| `/home/pi/jts/logs/dual-apple-dac-lab-20260603T110945-0400-scarlett-ticks-900s` | `ticks` + Scarlett capture | 854 s before software abort | `-60 dBFS` | Analog drift pass through 840 s; lab aborted on too-strict ALSA delay-report guard |
| `/home/pi/jts/logs/dual-apple-dac-lab-20260603T112936-0400-scarlett-ticks-900s-delay128` | `ticks` + Scarlett capture | 900 s | `-60 dBFS` | Pass; completed with zero measured median inter-channel drift |
| `/home/pi/jts/logs/dual-apple-dac-lab-20260603T120839-0400-scarlett-ticks-900s-repeat-buffered` | `ticks` + Scarlett capture | 900 s | `-60 dBFS` | Primary pass; no capture overrun, zero measured inter-channel drift |

For the 900 s `ticks` run:

- `snd_pcm_link` succeeded.
- Final frames written were identical:
  `43,204,608` frames on both DACs.
- `delay_delta_frames` stayed at `0` for all reported telemetry.
- Both PCMs stayed `Running` until normal completion.
- No `dual_apple_dac.abort` event occurred.
- Post-run streams returned to `Status: Stop`.
- The captured kernel log window contained no USB/ALSA/xrun messages.
- `systemctl --failed` reported `0 loaded units listed`.

The Scarlett-backed 24 s identity run showed:

- Scarlett channel 1 captured DAC A channel 0.
- Scarlett channel 2 captured DAC B channel 0.
- Scarlett channels 3 and 4 were silent for this cabling.
- The captured levels were usable and did not clip during identity.

The first Scarlett-backed 900 s `ticks` run stopped at about 854 s
because the lab binary's default `--max-delay-delta-frames 2` guard saw
an ALSA `snd_pcm_delay()` report wobble of 48 frames. The analog capture
did not show corresponding drift: rolling common-clock windows held the
same `-7` frame inter-channel offset through 840 s. Treat that run as
evidence that the delay-report guard was too strict for this hardware,
not as evidence of analog clock drift.

For product `jasper-outputd` dual-Apple mode, the default
`JASPER_OUTPUTD_DUAL_MAX_DELAY_DELTA_FRAMES` guard is 48 frames (1 ms at
48 kHz). That keeps the runtime fail-closed behavior aligned with the
measured ALSA delay-counter wobble instead of the stricter lab default.

The second Scarlett-backed 900 s `ticks` run raised only the
delay-report guard to 128 frames while leaving xrun, suspend, disconnect,
serial, ownership, and frame-count guards in place. It completed
successfully but the Scarlett capture reported one input overrun of
about 498 ms, so it is supporting evidence rather than the primary
measurement bundle.

The primary Scarlett-backed 900 s repeat used the same output path and a
larger Scarlett capture buffer. It completed cleanly:

- `snd_pcm_link` succeeded.
- Final frames written were identical:
  `43,204,608` frames on both DACs.
- `delay_delta_frames` stayed at `0` for all reported telemetry.
- The direct inter-channel analysis measured 900 tick seconds.
- The median lag was `-7` frames in every 60 s window.
- Every analyzed second reported lag `-7` frames; overall lag min/max
  were both `-7`.
- Rolling median drift span was `0` frames; slope was `0 ppm`.
- Correlation quality was high, with median absolute peaks around
  `0.936`.
- Capture levels were balanced enough for this measurement:
  Scarlett ch1 peak `-13.6 dBFS`, RMS `-37.5 dBFS`; ch2 peak
  `-11.9 dBFS`, RMS `-36.7 dBFS`.
- `arecord` exited `0` and did not report an overrun.
- The post-run kernel filter was empty.
- `systemctl --failed` reported `0 loaded units listed`.

### What this proves and does not prove

Proved so far:

- The Pi can enumerate two Apple adapters as two ALSA playback cards
  once both have suitable analog loads.
- A single Rust lab process can open both direct hardware PCMs by serial,
  link them with ALSA, prime silence, start them together, and sustain
  15 minutes of low-level non-silence without software-visible drift,
  xrun, suspend, disconnect, or kernel error.
- A common-clock Scarlett capture of one analog channel from each DAC
  showed stable relative timing over two 900 s runs in the best-case
  topology; the clean buffered repeat had no capture overrun and no
  measured inter-channel drift.
- This topology avoids ALSA aggregate plugins (`multi`, `dmix`, `plug`)
  and avoids CamillaDSP multi-device output during the experiment.

Not proved yet:

- physical left/right channel identity at the speaker terminals;
- the unused right channel of either Apple DAC under the current capture
  cabling;
- repeatability across replug, reboot, and repeated startup/reload
  cycles;
- startup/reload safety through the normal JTS product stack;
- safe active-crossover routing for tweeters;
- compatibility with product volume semantics. The lab binary bypasses
JTS volume and writes direct `hw:` PCMs.

## Product Boundary Added 2026-06-03

The first product-facing slice is intentionally non-audible:

- `jasper.output_topology` recognizes
  `dual_apple_usb_c_dac_4ch` as a four-output composite device.
- The hardware payload must list two Apple child DACs with stable serials
  and two physical output indexes each.
- The clock-domain report accepts the exact two-child/four-output hardware
  shape only when the current reconciler observation also reports the matching
  same-bus dual-Apple profile. Matching
  `dual_apple_usb_c_dac_drift_measurement` evidence is retained as validation
  evidence; missing evidence is a warning, while failed evidence or missing/
  partial live observation blocks.
- The current acceptance contract is at least 900 s at 48 kHz, zero
  output xruns, `max_offset_delta_frames <= 1`, and
  `abs(drift_ppm) <= 1`.
- `jasper.active_speaker.readiness` may treat that constrained composite
  clock as satisfying the clock precondition, but topology/readiness still
  grants no playback authority by itself.

Sound-emitting product work now uses `jasper-outputd`'s dual direct-sink
mode: one Rust process opens both pinned DAC PCMs, consumes the protected
four-channel post-Camilla active lane, primes silence, tracks xruns/delay/
frame counts, and aborts both sinks on mismatch. This lab binary remains a
bench measurement tool, not the product playback owner.

The next meaningful validation is repeatability, not escalation to the
product stack: rerun the same Scarlett capture after a cold reboot and
after physically replugging the DACs, then swap the cabling to prove the
unused right channels.

## Safety Gate

Do not run any playback command unless all of these are true:

- power amps are disconnected, off, or connected only to dummy loads;
- no tweeters are connected;
- the analog outputs are connected only to dummy loads, a capture
  interface, or a high-Z measurement input;
- a physical stop path is known before starting the run;
- normal JTS audio owners are stopped before opening the DACs.

The lab binary refuses playback without `--arm-no-speakers` and caps
test output at `-30 dBFS`; the default is `-60 dBFS`.

## Build

This crate is intentionally not installed by `deploy/install.sh` and has
no systemd unit. Build it explicitly on the Pi after deploying the tree:

```sh
cd /home/pi/jts/rust/jasper-dual-dac-lab
cargo build --release --locked
```

The binary path is:

```sh
/home/pi/jts/rust/jasper-dual-dac-lab/target/release/jasper-dual-dac-lab
```

## Passive Probe

Probe before any playback:

```sh
bash scripts/pi-run-diagnostic.sh -- bash -lc '
set -euo pipefail
cd /home/pi/jts
./rust/jasper-dual-dac-lab/target/release/jasper-dual-dac-lab probe
lsusb -t
cat /proc/asound/cards
for f in /proc/asound/card*/stream*; do echo "== $f =="; sed -n "1,160p" "$f"; done
journalctl -k --since "-30 min" --no-pager | grep -Ei "usb|snd|xrun|apple|05ac|110a|error|disconnect" || true
'
```

Proceed only if the probe reports exactly two Apple `05ac:110a` ALSA
cards with `has_playback:true` and non-empty serials. If a second adapter
appears as USB/HID only, plug in a harmless 3.5 mm load/headphones/dummy
plug and repeat the passive probe.

The armed `run` command also enforces this shape itself: exactly two
Apple USB devices, exactly two Apple ALSA playback cards, and no Apple
USB device left in HID-only/no-ALSA state.

## Stop Normal Audio Owners

Before any armed run:

```sh
ssh pi@jts.local 'sudo systemctl stop jasper-voice jasper-outputd jasper-camilla'
```

`jasper-outputd` is the normal final-output owner. If it is still active,
the lab binary refuses to run before opening the direct hardware PCMs.
It also refuses to run while `jasper-voice` is active, so assistant TTS
cannot race the lab run. Stop `jasper-camilla` too so the product audio
graph is fully parked even though CamillaDSP does not directly own the
Apple hardware PCMs in the outputd topology.

## Silence Soak

Use the serials from `probe`:

```sh
ssh pi@jts.local '
cd /home/pi/jts/rust/jasper-dual-dac-lab
./target/release/jasper-dual-dac-lab run \
  --dac-a-serial DWH... \
  --dac-b-serial DWH... \
  --mode silence \
  --duration-sec 600 \
  --arm-no-speakers
'
```

Pass criteria:

- exits successfully;
- no `dual_apple_dac.abort` event;
- no ALSA xrun/suspend/disconnect;
- no kernel USB errors during or after the run;
- `delay_delta_frames` remains within the configured baseline window.

## Channel Identity

Run only into capture inputs:

```sh
ssh pi@jts.local '
cd /home/pi/jts/rust/jasper-dual-dac-lab
./target/release/jasper-dual-dac-lab run \
  --dac-a-serial DWH... \
  --dac-b-serial DWH... \
  --mode identity \
  --duration-sec 20 \
  --level-db -60 \
  --arm-no-speakers
'
```

The identity mode emits low-level pulses in this repeating order:

1. DAC A channel 0
2. DAC A channel 1
3. DAC B channel 0
4. DAC B channel 1

The captured waveform must prove the expected physical wiring before any
speaker connection is considered.

## Drift Soak

Use `ticks` mode with simultaneous analog capture:

```sh
ssh pi@jts.local '
cd /home/pi/jts/rust/jasper-dual-dac-lab
./target/release/jasper-dual-dac-lab run \
  --dac-a-serial DWH... \
  --dac-b-serial DWH... \
  --mode ticks \
  --duration-sec 3600 \
  --level-db -60 \
  --arm-no-speakers
'
```

Analyze the captured signals by cross-correlation. The important results
are:

- intra-DAC channel skew for DAC A;
- intra-DAC channel skew for DAC B;
- inter-DAC skew slope over the full run;
- startup skew repeatability across repeated short runs.

Green evidence means no xruns/errors and no meaningful drift over one
hour. Yellow evidence means inter-DAC drift exists but the one-DAC-per-
speaker crossover stays internally coherent. Red evidence means any xrun,
disconnect, non-repeatable channel identity, or drift large enough to move
stereo timing during a normal listening session.

## Evidence Bundle

Store each run under a timestamped directory with:

- lab binary stdout JSONL;
- `lsusb -t`;
- `/proc/asound/cards`;
- `/proc/asound/card*/stream*`;
- `journalctl -k` before and after;
- capture WAV;
- analysis output.

Do not allow sound-emitting JTS paths to consume this topology unless a
bundle proves the exact serials, physical port map, channel identity, and
safe startup behavior.

Last verified: 2026-06-08
