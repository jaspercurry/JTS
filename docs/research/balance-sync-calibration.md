# Research: Multi-Speaker Balance and Sync Calibration

> **Status: research artifact.** Snapshot from 2026-06-13 while
> designing JTS stereo-pair balance/sync measurement. Preserved for
> traceability and source links. Current operational truth lives in
> [HANDOFF-multiroom.md](../HANDOFF-multiroom.md) and
> [dumb-endpoint-bringup.md](../dumb-endpoint-bringup.md).

## Bottom line

Balance and sync are separate calibration problems. Balance asks
"which speaker is louder at the listening position?" Sync asks "which
speaker arrives earlier at the listening position?" A steady tone can
help with level balance, but it is the wrong primitive for timing.
Timing should use an impulse-like marker, sweep, chirp, or
cross-correlation signal with a clear time anchor.

The most important architectural clarification is that "Snapcast vs.
CamillaDSP" is not a rough/fine split. There are three different timing
concepts:

| Concept | Owner | Corrects | Measurement source | Persists with |
|---|---|---|---|---|
| Snapcast sync loop | Snapcast client/server | Network transport jitter, client clock drift, synchronized playout | Snapcast's live protocol/time-sync state | The active playback session |
| Snapcast client latency | Snapcast client config / leader control plane | Fixed whole-client PCM/DAC/backend/output-path latency | Colocated speakers, electrical loopback, or otherwise endpoint-path-only baseline | The endpoint hardware path |
| CamillaDSP rendered-channel delay | Leader render graph | Listening-seat acoustic arrival offset between rendered channels | Phone/mic capture at the seat, impulse response, chirp, or cross-correlation | The room/pair calibration |

The implementation rule for JTS should be:

- If the offset remains when speakers are colocated, measured
  electrically, or otherwise isolated to a client/DAC/backend path, put
  it in Snapcast client latency.
- If the offset is caused by speaker placement relative to the listening
  seat, put it in leader-side CamillaDSP channel delay.
- If a seat measurement includes both, split it when there is a stable
  endpoint baseline. Without that baseline, start with an explicit
  room/pair calibration value and do not pretend it is a hardware-path
  number.

## Prior art and source notes

Snapcast's own README describes the core audio plane: the server sends
timestamped PCM chunks to clients, each client synchronizes time with
the server, and the client corrects remaining deviation by playing
slightly faster or slower through single-sample insertion/removal. The
same README says typical deviation is below 0.2 ms. That makes Snapcast
the distributed sync engine, not merely a packet pipe.

Source:
<https://raw.githubusercontent.com/badaix/snapcast/master/README.md>

Snapcast also exposes a static per-client latency knob. The JSON-RPC
API has `Client.SetLatency`, and the Debian `snapclient` man page
describes `--latency` as the "Latency of the PCM device." That wording
is a strong hint about intended ownership: this knob compensates the
client/output-device path, not arbitrary room geometry.

Sources:
<https://raw.githubusercontent.com/badaix/snapcast/master/doc/json_rpc_api/control.md>
<https://manpages.debian.org/unstable/snapclient/snapclient.1.en.html>

CamillaDSP exposes a `Delay` filter with units in milliseconds,
microseconds, millimeters, and samples, with optional subsample
precision. That makes it a good renderer-side primitive for static
channel delay inside a leader-baked stereo pair or 2.1 render graph.
It is not the distributed network sync engine.

Source:
<https://raw.githubusercontent.com/HEnquist/camilladsp/master/README.md>

Room EQ Wizard separates level and timing workflows. Its measurement
docs explain that an acoustic timing reference can remove variable
computer/output delays from relative measurements, and its All SPL
tools separately expose level alignment and impulse/time alignment
operations. That supports a JTS flow that measures level and timing as
two related but distinct passes.

Sources:
<https://www.roomeqwizard.com/help/help_en-GB/html/makingmeasurements.html>
<https://www.roomeqwizard.com/help/help_en-GB/html/graph_allspl.html>

## JTS product implications

The measurement surface should live on the leader. The leader owns the
pair/group, the render graph, the stored calibration record, and the
decision about which knob receives a measured correction. A satellite
or endpoint can expose health and local driver-DSP controls, but it
should not independently adapt its timing against another speaker.

For a web-based measurement flow:

- Reuse the phone/browser microphone model from balance/correction
  work, but record both speaker markers in one capture when possible.
  One shared recording cancels most phone/browser latency uncertainty
  because the feature of interest is the relative arrival delta.
- Use broadband chirps, exponential sweeps, MLS-like sequences, or
  short impulses/noise bursts. Do not use a steady sine wave as the
  sync primitive; it is ambiguous by full cycles.
- Run at least three measurements and report confidence. Reject or ask
  the user to retry when impulse peaks disagree, signal-to-noise is
  low, or cross-correlation has multiple plausible peaks.
- Separate level balance from delay. A single wizard can present both,
  but storage and apply paths should remain distinct.
- Apply compensation only after showing the measured result and the
  target knob: endpoint latency for a fixed client path, or rendered
  channel delay for the room/pair.
- Store compact summaries by default: timestamp, speaker IDs, playback
  role, signal type, measured level delta, measured time delta,
  confidence, and chosen compensation. Raw audio can be opt-in debug
  evidence rather than normal persistent state.

## Implementation hypothesis to verify

For the first product slice, JTS can use Snapcast's sync loop as the
transport timing authority, Snapcast client latency for fixed
endpoint/DAC/backend offsets, and leader-side CamillaDSP delay for
fine rendered-channel acoustic alignment at the listening seat.

The key validation test is not "can we set both knobs?" It is whether a
repeated measurement can distinguish endpoint-path delay from acoustic
seat delay well enough to avoid putting a persistent room-geometry
correction into a hardware-path setting. The first implementation
should therefore make the distinction visible in logs, state, and the
calibration record even if the UI initially presents a simplified
"align speakers" flow.

Last researched: 2026-06-13
