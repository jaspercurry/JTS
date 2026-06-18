# AEC-DIAG-04 Timing Results

Date: 2026-06-18
Scope: timing-only experiment for the outputd/chip-ref/XVF3800 chip-AEC path.
Constraint: no permanent production changes. Reference content was held constant
within each stimulus family while changing only outputd period/DAC buffer and
volatile `AUDIO_MGR_SYS_DELAY`.

## Setup And Reversibility

The laptop worktree was first updated with latest remote `main`. The live Pi was
still on installed build `91725b97`, whose packaged `jasper-outputd` did not
expose the AEC-DIAG-02 timing fields or chip-ref tee. To avoid a permanent
deploy, the test built `jasper-outputd` from `origin/main` `e44ca151` under
`/tmp` on the Pi, copied the binary temporarily to
`/var/lib/jasper/aec-timing-outputd-e44ca151`, and used a `/run/systemd`
drop-in to point `jasper-outputd.service` at it during the experiment.

Cleanup completed after the run:

- `ExecStart` restored to `/opt/jasper/bin/jasper-outputd`.
- `/run/systemd/system/jasper-outputd.service.d/` was empty.
- `/var/lib/jasper/aec-timing-outputd-e44ca151` was removed.
- `jasper-outputd`, `jasper-aec-bridge`, `jasper-voice`, and
  `shairport-sync` were all active.
- `AUDIO_MGR_SYS_DELAY` was restored to `12`.

Note: the first attempt to execute the temporary outputd directly from `/tmp`
failed because `jasper-outputd.service` has `PrivateTmp=true`; systemd could not
see that path. The service was stopped before retrying from `/var/lib/jasper`.
There were three failed `203/EXEC` starts before the stop, below the
`StartLimitAction=reboot` threshold.

## Raw Artifact Locations

All local artifacts are under `logs/`; remote copies remain under `/tmp` on the
Pi for manual inspection.

| Purpose | Local artifact directory | Remote artifact directory |
|---|---|---|
| Chirp: `outputd_udp -> raw ch2`, 2 runs/profile | `logs/aec-timing-probe-20260618T164813Z/` | `/tmp/aec-timing-probe-20260618T164813Z` |
| Chirp: `chip_ref_tee -> raw ch2`, 2 runs/profile | `logs/aec-timing-probe-20260618T164903Z/` | `/tmp/aec-timing-probe-20260618T164903Z` |
| Chirp: `outputd_udp -> chip beam ch0` | `logs/aec-timing-probe-20260618T164948Z/` | `/tmp/aec-timing-probe-20260618T164948Z` |
| Chirp: `outputd_udp -> chip beam ch1` | `logs/aec-timing-probe-20260618T165025Z/` | `/tmp/aec-timing-probe-20260618T165025Z` |
| Broadband/speech-like baseline at `SYS_DELAY=12` | `logs/aec-stimulus-sweep-20260618T165408Z/` | `/tmp/aec-stimulus-sweep-20260618T165408Z` |
| `SYS_DELAY` sweep on `512/1024` | `logs/aec-stimulus-sweep-20260618T165619Z/` | `/tmp/aec-stimulus-sweep-20260618T165619Z` |

Each directory contains `results.json`, `results.csv`, `summary.md`, and WAV
captures. The stimulus sweep directories include outputd UDP ref, chip-ref tee,
raw mic ch2, and chip beams ch0/ch1 for every run.

## Full Test Matrix

Baseline matrix at `AUDIO_MGR_SYS_DELAY=12`:

| Outputd profile | DAC delay | Chip-ref delay | Chirp `outputd_udp -> raw ch2` | Noise conv | Noise ch0/ch1 vs raw | Speech conv | Speech ch0/ch1 vs raw | Xruns/drops/queue lag |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `1024/3072` | ~79.3-80.0 ms | ~56.6-57.3 ms | 77.56 ms mean, high | yes | -4.2 / -4.7 dB | yes | -16.0 / -15.0 dB | 0 / 0 / 0 |
| `1024/2048` | ~58.0-58.7 ms | ~55.6-56.3 ms | 59.25 ms mean, high | yes | -3.2 / -2.9 dB | yes | -8.9 / -8.7 dB | 0 / 0 / 0 |
| `512/1024` | ~37.0-37.3 ms | ~46.3-46.6 ms | 43.22 ms mean, high | yes | -4.3 / -4.7 dB | yes | -9.0 / -8.7 dB | 0 / 0 / 0 |

`Xruns/drops/queue lag` means max content xrun, DAC xrun, chip-ref write xrun,
chip-ref write error, chip-ref queue drop, and chip-ref `reference_sequence_lag`
were all zero in the JSON snapshots for that profile.

## Timing Tables

Chirp timing against raw mic ch2:

| Profile | Run 1 | Run 2 | Mean | Confidence |
|---|---:|---:|---:|---|
| `1024/3072` | 81.25 ms | 73.88 ms | 77.56 ms | high / high |
| `1024/2048` | 65.94 ms | 52.56 ms | 59.25 ms | high / high |
| `512/1024` | 44.56 ms | 41.88 ms | 43.22 ms | high / high |

Broadband/speech-like baseline at `SYS_DELAY=12`:

| Profile | Stimulus | `outputd_udp -> raw` | `chip_ref_tee -> raw` | Tee confidence | Ch0/ch1 vs raw | Speech-band ch0/ch1 vs raw |
|---|---|---:|---:|---|---:|---:|
| `1024/3072` | noise | 86.94 ms | 144.81 ms | low | -4.2 / -4.7 dB | -1.5 / -3.4 dB |
| `1024/3072` | speech-like | 74.69 ms | 4.75 ms | medium | -16.0 / -15.0 dB | -12.5 / -10.9 dB |
| `1024/2048` | noise | 56.00 ms | 220.50 ms | low | -3.2 / -2.9 dB | -3.2 / -3.4 dB |
| `1024/2048` | speech-like | 51.69 ms | 3.13 ms | medium | -8.9 / -8.7 dB | -7.6 / -8.1 dB |
| `512/1024` | noise | 43.63 ms | 218.81 ms | low | -4.3 / -4.7 dB | -1.7 / -2.7 dB |
| `512/1024` | speech-like | 39.06 ms | 1.44 ms | low | -9.0 / -8.7 dB | -6.0 / -7.8 dB |

The `chip_ref_tee` lag is not reliable under chirp/noise in this run: peaks
were mostly low-confidence and moved non-physically. Treat the tee WAVs as
content evidence and the outputd state `chip_ref_writer` counters as health
evidence, not as a proven chip-consumption timestamp.

## SYS_DELAY Sweep

Sweep profile: `512/1024`. Mechanism: volatile XVF write to
`AUDIO_MGR_SYS_DELAY`; original value `12` restored. The chip accepts only about
`-64..256` samples, so there was no safe way to sweep around the full measured
`outputd_udp -> raw` acoustic lag (about 35-47 ms during this sweep). The table
therefore covers the safe writable range in roughly 2 ms steps.

| SYS_DELAY | ms | Noise conv | Noise ch0/ch1 vs raw | Speech conv | Speech ch0/ch1 vs raw | Speech-band ch0/ch1 vs raw |
|---:|---:|---:|---:|---:|---:|---:|
| -64 | -4.00 | yes | -4.5 / -4.4 dB | yes | -13.1 / -10.7 dB | -11.9 / -12.0 dB |
| 0 | 0.00 | yes | -2.4 / -2.4 dB | yes | -3.3 / -4.0 dB | +1.5 / +0.3 dB |
| 32 | 2.00 | yes | -2.7 / -3.2 dB | yes | -5.1 / -6.8 dB | -0.5 / -2.8 dB |
| 64 | 4.00 | yes | -2.3 / -2.4 dB | yes | -3.6 / -4.2 dB | -1.5 / -1.9 dB |
| 96 | 6.00 | yes | -4.8 / -4.7 dB | yes | -7.8 / -9.8 dB | -5.6 / -8.4 dB |
| 128 | 8.00 | yes | -2.4 / -3.5 dB | yes | -6.0 / -6.2 dB | -1.7 / -2.6 dB |
| 160 | 10.00 | yes | -3.8 / -3.9 dB | yes | -4.3 / -4.7 dB | -4.4 / -5.0 dB |
| 192 | 12.00 | yes | -3.9 / -3.8 dB | yes | -6.7 / -6.5 dB | -1.8 / -1.8 dB |
| 224 | 14.00 | yes | -4.0 / -4.3 dB | yes | -5.5 / -6.2 dB | -4.4 / -4.9 dB |
| 256 | 16.00 | yes | -3.8 / -4.0 dB | yes | -4.9 / -4.7 dB | -2.3 / -2.7 dB |

Best observed points in this sweep:

| Stimulus | Full-band best | Speech-band best |
|---|---|---|
| noise | `SYS_DELAY=96` (-4.8 / -4.7 dB) | `SYS_DELAY=96` (-3.3 / -4.0 dB) |
| speech-like | `SYS_DELAY=-64` (-13.1 / -10.7 dB) | `SYS_DELAY=-64` (-11.9 / -12.0 dB) |

## Does Convergence Follow Timing?

Not in the binary latch sense. `AEC_AECCONVERGED` was already `1` before every
broadband/speech-like capture, stayed `1` throughout every poll, and remained
`1` after every run. That means the experiment did confirm a convergence latch
under far-end stimulus, but it did not observe latch onset or loss. Within the
safe writable `SYS_DELAY` range, convergence did not distinguish good from bad
timing.

Attenuation did vary with `SYS_DELAY`, but not as a single monotonic timing
curve:

- Noise was best around `SYS_DELAY=96` samples.
- Speech-like content was best at `SYS_DELAY=-64` samples.
- `SYS_DELAY=0` and `64` were consistently weaker.
- The default `12` samples was acceptable for `512/1024`, but not the best
  point seen in the sweep.

The stronger timing result is outputd transport latency: lowering DAC buffer
from `3072` to `2048` to `1024` reduced high-confidence `outputd_udp -> raw`
chirp lag from about `77.6 ms` to `59.3 ms` to `43.2 ms`, with no xrun/drop
regression.

## Best Low-Latency Candidate

Use `outputd period=512, dac_buffer=1024` as the current low-latency stable
candidate for the next diagnostic round.

Evidence:

- Lowest high-confidence chirp lag to raw mic ch2: `43.22 ms` mean.
- Broadband/speech-like runs had no content xruns, DAC xruns, chip-ref write
  xruns/errors, chip-ref queue drops, or chip-ref sequence lag.
- DAC delay stayed around `37 ms`; chip-ref ALSA delay around `45-47 ms`.
- Speech-like chip-beam attenuation was useful at default `SYS_DELAY=12`
  (`~9 dB` full-band; `~6-8 dB` speech-band), and could improve in the sweep.

Do not promote a non-default `SYS_DELAY` yet. `-64` was best for speech-like
content, while `96` was best for noise. That split suggests either
content-dependent chip behavior or an imperfect measurement harness, not a
single clean timing solution.

## Remaining Unknowns

- `AEC_AECCONVERGED` was latched before stimulus, so this run did not measure
  convergence onset time or latch loss.
- `chip_ref_tee -> raw` correlation is not trustworthy enough yet. The tee is
  writer-side content, not a timestamp of XVF USB-IN consumption, and several
  peaks were low-confidence or implausible.
- The stimulus sweep measured chip beams against raw mic as an attenuation
  proxy. It is not a formal ERLE measurement against a chip-internal error
  signal.
- Noise and speech-like stimuli preferred different `SYS_DELAY` values.
- Ch0/ch1 chirp captures at the default chirp gain clipped slightly; the
  quieter broadband/speech-like captures are better evidence for attenuation.
- Runs were short: 6-8 seconds per stimulus. That is enough to see the existing
  convergence latch and gross attenuation, not long-window drift.
- The live installed build still lacks the timing fields; they were observed
  through a temporary diagnostic outputd binary from `origin/main`.

## Recommendation

Proceed with `512/1024` as the low-latency outputd transport candidate, but do
one small timing-telemetry fix before content A/B/C: improve the chip-ref tee
probe so each run has an aligned capture window or an explicit sequence/time
anchor. The current outputd UDP timing and state health are good enough to keep
moving, but the chip-ref timing evidence is too ambiguous to use as the
decider for content changes.

After that telemetry fix, run content A/B/C at `512/1024` with:

- `SYS_DELAY=12` as the production/default baseline.
- `SYS_DELAY=-64` as a speech-like positive-control candidate.
- `SYS_DELAY=96` as a broadband-noise positive-control candidate.

Do not persist any of those chip-delay values from this run without a longer
far-end-only validation window and a cleaner chip-ref timing measurement.
