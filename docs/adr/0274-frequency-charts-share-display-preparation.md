# ADR-0274: Frequency charts share display preparation

- **Date:** 2026-09-09
- **Status:** Accepted.
- **Context:** The saved and live browser charts shared a drawing engine.
  Image export used the same measurements but had separate display rules.
  For saved JTS3 run `3964f898c03f`, it drew points below the 357 Hz trusted
  floor without the shading shown by the browser.
- **Decision:** [frequency_display.py](../../jasper/active_speaker/frequency_display.py)
  owns reference subtraction, valid-band clipping, and trust intervals for
  saved views, live measurements, predictions, and image export. The shared
  view carries these prepared display values beside the original numerical
  curves. A missing reference yields missing display values, never a zero
  reference. Invalid points break lines; untrusted points remain visible
  with shading. Run and curve exclusions are combined, and the displayed
  curves' trust regions are shaded without stacking their opacity.
- **Boundary:** Canvas and Matplotlib retain their own drawing and layout
  code. This preserves browser controls and PNG/SVG/PDF export without a
  browser dependency in the offline tools. The live chart's existing point
  limit still applies before display preparation. Measurement DSP and
  stored recordings remain the owners of the evidence.
