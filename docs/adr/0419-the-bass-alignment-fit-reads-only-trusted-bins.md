# ADR-0419: The bass alignment fit reads only trusted bins

- **Date:** 2026-10-02
- **Status:** Accepted. Amends
  [ADR-0398](0398-the-bass-alignment-fit-reads-a-woofers-near-field-curve.md) §1: the fit read
  every bin of the trusted band, equally weighted.
- **Context:** Finding F2 of the
  [2026-10-02 measurement audit](../audits/2026-10-02-measurement-program.md): at one seat spot,
  `bass-alignment --take` gave 112.8 Hz/Q 1.23, 67.1 Hz/Q 0.38 and 108.3 Hz/Q 0.98 in three runs,
  while the near-field fits agree on 83.8–85.6 Hz, Q about 1.0. Two causes: the room, and bins
  under 50 Hz below the 20 dB SNR floor. The fit read no SNR. This is step A7 of
  [#6227](https://github.com/jaspercurry/JTS/issues/6227).
- **Decision:**
  1. The fit reads only qualified bins: a bin qualifies when its band's SNR clears the bass view's
     floor (`snr_trusted`, 20 dB). `--take` reads the take's banked bass reading, qualified as the
     bass view qualifies it: an intact capture whose band SNR clears the floor. The bass ladder
     stops at 200 Hz, so a take's fit reads no bin at or above it. The near-field fit reads the
     near-field view's band rows: a bin qualifies when its band is `trusted` in every take of the
     curve. That flag now calls `snr_trusted` too.
  2. The fit reads the curve smoothed at 1/3 octave, the smoothing of the bass table's shape fit
     (`smooth_bass_curve`), over each unbroken run of qualified bins. The model goes through the
     same smoothing, so a clean box comes back unbiased. The artifact's `measured_db` and
     `model_db` are both smoothed.
  3. A band left with fewer than three qualified bins keeps the `coverage_short` refusal; its
     detail states `qualified_bins`. A take that banked no bass reading has no qualified bin, so it
     refuses the same way.
- **Consequences:**
  - The view stays advisory. No DSP, level path or clamp changes; `volume_limit`, the graph
    doors, the clamp, the 85 dB stop and the driver caps do not change.
  - On jts3's loudest seat takes (runs 1–3 of #6113, three spots each) the fits went from
    67.1–116.4 Hz, Q 0.32–1.51 to 95.8–101.7 Hz, Q 1.03–1.88, and 3 of the 9 now refuse with Q on
    its bound. The low-SNR cause is gone; the room stays. The seat curve carries the room's +6.7 dB
    at 106 Hz, so a seat fit is not the box's alignment. The near-field fit stays the source.
  - On the #5684 near-field views, the front woofer reads 85.8–85.9 Hz, Q 1.06–1.08 (before
    84.1–87.3 Hz, Q 1.01–1.13). The rear woofer's 35–50 Hz band reads 18.9–19.9 dB SNR, so its fit
    starts at 50 Hz and moves from 82.8–83.6 Hz, Q 0.88–0.90 to 90.8–90.9 Hz, Q 1.12–1.13. Over
    40–300 Hz it reads 88.0–88.6 Hz: below 50 Hz that curve is not a clean 2nd-order high-pass.
  - Rejected: smoothing the curve alone, which put a clean box's corner up to 7% low and its Q up to
    0.1 low; weighting bins by SNR, a second SNR rule; and counting bins above the bass ladder as
    qualified, which on jts3 spread the seat fits wider (98–117 Hz).
