# FIR Room Correction

> **Status: distilled from 2026-05-25 deep-research intake.** This
> file defines what FIR should mean in JTS. It is not a promise that
> every rung has been implemented.

## Operational Summary

FIR filters are not automatically "better PEQ." They expand the
design space: arbitrary magnitude curves, convolution export,
linear-phase filters, crossover/time alignment, and bounded
excess-phase / group-delay work. They also add latency, windowing
complexity, and pre-ringing risk. JTS should approach FIR as a staged
capability ladder, not as a magic room fixer.

## FIR Modes

| Mode | Use | Risk | JTS default |
|---|---|---|---|
| Minimum-phase FIR | High-resolution magnitude correction with phase tied to magnitude | Same acoustic limits as PEQ; can still overfit | First FIR generation rung. |
| Linear-phase FIR | Constant group delay, crossover/driver experiments, expert exports | Latency and pre-ringing | Expert/off by default. |
| Mixed-phase FIR | Selective excess-phase / group-delay correction | Requires excellent measurement and windowing | Later guarded rung only. |

Minimum-phase FIR is often the safest bridge from today's PEQ. It
can use the same conservative target and boost rules while exercising
CamillaDSP convolution, bundle storage, and export/import paths.

## Windowing

FIR generation is inseparable from impulse-response windowing. A raw
room impulse response includes direct sound, early reflections, late
decay, noise, and measurement artifacts. Inverting all of it is a
trap.

Frequency-dependent windowing is the strongest prior-art pattern:
use longer windows at low frequencies where the room behaves modally,
and shorter windows at high frequencies where direct sound should
dominate interpretation. DRC-FIR and Acourate-style workflows both
lean heavily on this idea.

Concrete research-pass starting point, not yet validated on JTS:

- roughly 15/15 cycles below 300 Hz;
- transition toward 5/5 cycles by 1 kHz;
- roughly 3/3 cycles above 4 kHz;
- persist pre/post window settings in the bundle;
- treat static full-range gates as a failure mode because they either
  throw away low-frequency resolution or include too much high-frequency
  room energy.

The FDW goal is not "invert the room." It is psychoacoustic restraint:
use enough low-frequency time support to see modal behavior, and
enough high-frequency truncation to avoid correcting late reflections.

## Latency And Tap Budget

CamillaDSP can run convolution on Raspberry Pi-class hardware, and
the research reports agree that Pi 5 stereo FIR is not likely to be
CPU-bound for sensible tap counts. The practical limits are latency,
buffering, and memory/process pressure on the 1 GB target.

Initial JTS policy:

- support FIR import/export before automatic generation;
- start with short minimum-phase FIRs for magnitude correction;
- surface latency profile in the bundle and UI;
- pause renderers/voice during expensive filter generation if needed;
- keep generated FIR taps and CamillaDSP YAML together in the bundle.

Operational math to keep visible:

- CamillaDSP convolution latency and CPU depend on chunk size, FFT
  segmentation, sample rate, and filter length. JTS still needs a
  first-party Pi 5 benchmark before promising user-facing FIR limits.
- At 48 kHz, chunk sizes of 1024 / 2048 / 4096 samples correspond to
  about 21.3 / 42.7 / 85.3 ms of chunk duration.
- A 16,384-tap FIR spans about 341 ms at 48 kHz.
- A 65,536-tap FIR has about 0.73 Hz bin spacing at 48 kHz and, if
  linear phase, about 682 ms of group delay.
- Long linear-phase FIR may be fine for music-only playback, but it
  is a poor default for interactive voice/TTS unless the full routing
  and latency story is explicit.

## Staged Ladder

1. **Artifact readiness:** raw captures, impulse responses, window
   settings, smoothing variants, phase/group-delay curves, spatial
   averages, target curves, and quality flags.
2. **FIR runtime substrate:** CamillaDSP `Conv` filter import/export,
   bundle storage, headroom accounting, and latency reporting.
3. **Minimum-phase FIR magnitude correction:** same conservative
   target discipline as current PEQ, with strict headroom and boost
   limits.
4. **FDW FIR:** frequency-dependent-windowed correction, still biased
   toward broad and low-frequency changes.
5. **Mixed-phase / excess-phase correction:** opt-in, guarded by
   measurement quality, pre-ringing audit, and latency profile.
6. **Agent explanation:** LLM can explain trade-offs and recommend a
   bounded strategy; deterministic code designs and validates filters.

## Safety Rails

- No aggressive correction of narrow nulls.
- No full-range flattening by default.
- Require calibrated mic metadata before trusting small high-frequency
  claims.
- Require multi-position data before claiming to correct a room or
  couch area.
- Prefer cuts; cap boosts tightly, especially above the transition
  region.
- Refuse high-Q boosts in linear-phase or mixed-phase modes.
- Audit pre-impulse energy / pre-ringing risk for non-minimum-phase
  filters. First candidate metric from the reports: pre-impulse energy
  at least roughly 20 dB below post-impulse energy in the relevant
  window, with fallback to minimum phase or a warning when it fails.
  Also account for backward masking around the first 10-20 ms rather
  than treating every tiny pre-echo sample as equally audible.
- Always reserve headroom before enabling a generated FIR.
- Surface latency, headroom, max boost, and pre-ringing risk in the
  UI.
- Always keep an easy reset-to-flat path.

## Deterministic / LLM Boundary

Deterministic code owns sweep deconvolution, windowing, phase
decomposition, FIR design, null detection, pre-ringing audit,
headroom calculation, CamillaDSP YAML, and bundle writes.

The LLM may explain what a filter class does, compare strategies,
ask clarifying questions, and request a bounded strategy such as
"try minimum-phase FIR under the safe defaults." It must not emit raw
taps or unconstrained DSP syntax.

## Sources

- [CamillaDSP](https://www.camilladsp.com/)
- [REW](https://roomeqwizard.com/)
- [REW — Why Can't I Fix All my Acoustic Problems with EQ?](https://www.roomeqwizard.com/help/help_en-GB/html/iseqtheanswer.html)
- [DRC-FIR](https://drc-fir.sourceforge.net/doc/drc.html)
- [rePhase](https://rephase.org/)
- [Dirac — On Room Correction and Equalization](https://www.dirac.com/wp-content/uploads/2021/09/On-equalization-filters.pdf)
- Deep-research intake reports, 2026-05-25.

## Open Questions

- Should JTS depend on DRC-FIR, implement a smaller internal
  minimum-phase FIR path first, or support both?
- What tap-count and chunk-size defaults are best on the 1 GB Pi 5
  target under real renderer load?
- What exact pre-ringing metric is conservative enough for automated
  user-facing mixed-phase correction?
- Is CamillaFIR a concrete dependency/workflow for JTS or only a
  loose community reference? Reports conflicted; verify before relying
  on it.

Last verified: 2026-05-25
