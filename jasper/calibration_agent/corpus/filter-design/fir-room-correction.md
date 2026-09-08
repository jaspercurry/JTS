# FIR Room Correction

This note preserves FIR tradeoffs from the 2026-05-25 research. The current
[Room contract](../../../../docs/room-correction-information-design.md#target-filter-headroom-phase-and-latency-policy)
and [regime decision D6](../../../../docs/room-correction-regime-plan.md#d6--phasefir-room-correction)
own product scope. Room does not design FIR. Use
`jasper-correction-bundle --help` for existing import and inspection tools.

## Operational Summary

FIR filters are not automatically "better PEQ." They expand the
design space: arbitrary magnitude curves, convolution export,
linear-phase filters, crossover/time alignment, and bounded
excess-phase / group-delay work. They also add latency, windowing
complexity, and pre-ringing risk.

## FIR Modes

| Mode | Use | Risk |
|---|---|---|
| Minimum-phase FIR | Magnitude correction with phase tied to magnitude | Same acoustic limits as PEQ; can still overfit |
| Linear-phase FIR | Constant group delay, crossover/driver experiments | Latency and pre-ringing |
| Mixed-phase FIR | Selective excess-phase / group-delay correction | Requires suitable measurement and windowing |

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

## Evidence And Filter Limits

- Read the [room-correction limits](../concepts/room-correction-limits.md)
  before interpreting an inverse filter as a useful correction.
- Small high-frequency claims need suitable mic calibration; listening-area
  claims need spatial coverage. State what was measured.
- Inspect pre-impulse energy / pre-ringing risk for non-minimum-phase
  filters. First candidate metric from the reports: pre-impulse energy
  at least roughly 20 dB below post-impulse energy in the relevant
  window, with fallback to minimum phase or a warning when it fails.
  Also account for backward masking around the first 10-20 ms rather
  than treating every tiny pre-echo sample as equally audible.
- Reserve headroom before enabling a filter through its supported apply path.
- Surface latency, headroom, max boost, and pre-ringing risk in the
  UI.
- Preserve the prior tune and the supported restoration path.

Research thresholds above are suggestions for evaluation, not new execution
gates. Code owns supported filters, headroom, and latency limits. The
[doctrine](../../../../docs/measurement-loop-doctrine.md#2-the-authority-model)
owns the boundary between LLM judgment and execution.

## Sources

- [CamillaDSP](https://www.camilladsp.com/)
- [REW](https://roomeqwizard.com/)
- [REW — Why Can't I Fix All my Acoustic Problems with EQ?](https://www.roomeqwizard.com/help/help_en-GB/html/iseqtheanswer.html)
- [DRC-FIR](https://drc-fir.sourceforge.net/doc/drc.html)
- [rePhase](https://rephase.org/)
- [Dirac — On Room Correction and Equalization](https://www.dirac.com/wp-content/uploads/2021/09/On-equalization-filters.pdf)
- [2026-05-25 research archive](../../../../docs/research/2026-05-25-calibration-agent/README.md)
- [2026-05-27 FIR synthesis](../../../../docs/research/2026-05-27-room-correction-research/synthesis/fir-phase-room-correction.md)
