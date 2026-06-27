# Handoff: JTS audio-latency foundation

Canonical reference for JTS's local-audio-latency work: lowering latency on
the music path while keeping the speaker resilient and supporting flexible
output/mic hardware. Read this before touching the lean lane, the USB-input
bridge latency, or the snapcast bond buffer.

**Targets:** USB-audio-input lip-sync under ~60 ms (including CamillaDSP);
AirPlay (Apple TV → bonded pair) staying within the ~2 s presentation budget;
and, in general, *only* adding latency where a specific piece of hardware
genuinely requires it.

---

## The chain, and where latency lives

```
renderer → snd-aloop fan-in ring → jasper-fanin → (capture) → CamillaDSP
         → outputd_content_playback (snd-aloop) → jasper-outputd → DAC
```

- The **fan-in input ring** (~85 ms) is the WiFi-burst absorber — load-bearing
  for networked sources (AirPlay/Spotify), *not* needed by a wired USB source.
- **CamillaDSP** owns `chunksize` / `target_level` (config-baked in
  [`jasper/camilla_config_contract.py`](../jasper/camilla_config_contract.py)).
- **jasper-outputd** is the final-output owner: a blocking DAC write is the
  timing master; the content lane is read non-blocking (absent content → silence).
  Both AEC references are produced here (software AEC3 → 48 kHz UDP `:9891`;
  chip-AEC → 16 kHz USB-IN), so **nothing can delete `outputd`**.
- **DAC buffer** is env-tunable (`JASPER_OUTPUTD_DAC_BUFFER_FRAMES`); measured
  ~16 ms reliable on both the HiFiBerry DAC8x and the Apple dongle — *not* the
  bottleneck.

## The lean lane (Stage 4)

The lean lane is the low-latency music path for a **single, exclusive, wired**
source (USB audio input): the source writes a named pipe, CamillaDSP
**File-captures** it directly instead of draining the fan-in summed lane,
shedding one full snd-aloop round-trip.

**Key fact:** the lean lane only swaps CamillaDSP's **capture** device
(`plug:jasper_capture` → a File pipe). Playback stays
`outputd_content_playback`, so **`jasper-outputd` is unchanged** and both AEC
references keep working. A File capture has no clock, so it requires
`enable_rate_adjust: true` **and** an async resampler (rate-adjust "method 2").

**CamillaDSP schema gotcha:** the deployed runtime is **CamillaDSP v4.x**, whose
resampler is an *object* — `resampler: {type: AsyncSinc, profile: Balanced}` —
not the pre-v2 scalar `resampler_type: BalancedAsync` (the v4 parser rejects the
scalar). The shared emitter helpers live in
[`jasper/camilla_config_contract.py`](../jasper/camilla_config_contract.py)
(`file_capture_resampler_yaml`, `is_async_resampler`,
`DEFAULT_FILE_CAPTURE_RESAMPLER_TYPE`/`_PROFILE`, `DEFAULT_LEAN_CAPTURE_FIFO`);
the stereo ([`jasper/sound/camilla_yaml.py`](../jasper/sound/camilla_yaml.py))
and active-speaker
([`jasper/active_speaker/camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py))
emitters both use them — one definition, no copy-paste twin.

**FIFO format:** the lean pipe carries full **S32_LE @ 48 kHz stereo** (the
usbsink bridge's normal snd-aloop lane uses the high-16 S16 view; the FIFO must
*not* — CamillaDSP's File capture defaults to S32_LE). One owner of the path:
`DEFAULT_LEAN_CAPTURE_FIFO` (`/run/jasper-usbsink/lean.pipe`).

## What's shipped vs owed

| Stage | What | State |
|---|---|---|
| 0 | snapcast bond buffer routed via `--stream.buffer` (was an inert URL param; bonds silently ran the 1000 ms default) | shipped |
| 2 | USB-bridge latency knobs (`JASPER_USBSINK_{QUEUE_MAXBLOCKS,LATENCY,BLOCK_FRAMES}`) | shipped, on-device tuning owed |
| 4a | File-capture CamillaDSP emitter + fail-loud guards (stereo + active) | shipped, default-OFF |
| 4b-i | `decide_lean_route` pure routing policy ([`jasper/lean_lane.py`](../jasper/lean_lane.py)) | shipped, unwired |
| 4b-ii | usbsink FIFO-output mode (`JASPER_USBSINK_OUTPUT_MODE=fifo`) | shipped, default-OFF |
| 4b-iii | reconciler stages + loads the lean File-capture config | **owed** |
| 4b-iv | wire `decide_lean_route` into mux `_tick` (enter/leave-lean ladders, fail-loud → buffered) | **owed** |
| 5 | shairport-sync built `--with-pipe` (capable binary; runtime AirPlay pipe lane is future, #1318-gated) | shipped, dormant |
| 6 | `jasper-doctor` DAC USB sync-mode advisory (clock-coherence signal, *not* the chip-AEC gate) | shipped |

**Going live is soak-gated.** `JASPER_LEAN_LANE` is opt-IN
(`=enabled`), default-OFF, and is an *experiment knob* until 4b-iii/iv land and
a **24 h on-device zero-xrun soak** passes — then it graduates to a
prose-commented `.env.example` entry. Until then it is allowlisted in
`tests/test_env_vars_codified.py::_UNCODIFIED`.

## Optionality: chip-AEC AND software-AEC, each at the lean floor

Both AEC references come from `outputd`, so one "lean `outputd`" stage serves
both at the same latency floor. The per-AEC difference is *constraints, not
latency*: chip-AEC needs a USB-SOF-locked DAC plus a static
`AUDIO_MGR_SYS_DELAY` reference-delay re-pin; software AEC3 takes any DAC plus
Pi CPU. The chip's no-drift comes from the XVF USB-SOF PLL, not from snd-aloop
or `enable_rate_adjust` — so removing inter-stage rings is safe for it.

## Hard rules — do NOT re-architect

- Swap the engine/profile, **not** the topology. No PipeWire `module-echo-cancel`,
  no replacing snd-aloop with PipeWire fanout, no WirePlumber (multi-GB RAM
  runaways → OOM on the 1 GB Pi). Targeted single-knob OS fixes are fine *when
  measurement localizes the cause to that layer*.
- snd-aloop is FULL (8/8 substream pairs) — a new lane must be a pipe/socket,
  never a 9th pair.
- Keep the fan-in input ring for networked sources (the WiFi-burst absorber).
- Never saturate all Pi cores while measuring — the hardware watchdog reboots a
  fully-wedged userspace. Measure under realistic 2-of-4-core load.

## AirPlay bonded lip-sync (open)

Stage 0 is a strict latency win (1000 ms → configured) **and** a disambiguating
experiment. Whether shairport's local offset propagates to the bonded playout is
**decoupled** in theory (snapcast re-timestamps on its own monotonic clock); the
only way to settle it is to **measure bonded Apple-TV A/V** after Stage 0. Until
that measurement exists, do not treat the offset as the bonded fix.

---

Last verified: 2026-06-27 (lean-lane emitter + FIFO mode + decision policy
landed; resampler v4 object schema confirmed against the CamillaDSP v4.1.3
config reference; outputd-unchanged topology confirmed against
`camilla_config_contract.DEFAULT_PLAYBACK_DEVICE` + `rust/jasper-outputd`).
