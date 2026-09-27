# ADR-0373: A room, bass or rear take banks its analysed curves on its record

- **Date:** 2026-09-27
- **Status:** Accepted
- **Context:** A take's analysis runs once, in the capture host, before the bank writes the take's
  record once. That record kept the analysis's impulses (ADR-0354), diagnostics and gating, but not
  its curves: `analysis_curve_records` reached only the run manifest's per-take rows
  ([#5010](https://github.com/jaspercurry/JTS/issues/5010)). So every reader of a room, bass or rear
  take decoded the recording again and re-ran the deconvolution the capture had already run: at each
  bank the frequency view decoded every take and the room view its selected takes, and
  `frequency --analyze-wavs` did the same. The measurements page, which reads records and decodes
  nothing, drew no curve for these takes.
- **Decision:**
  1. The capture host writes a room, bass or rear take's analysed curves onto its record as
     `curves`, before the bank writes it. These are the takes `gate_exemption` reads ungated as the
     seat (`SEAT_EXEMPT`), which is #5010's scope. The curves are `analysis_curve_records`, the shape
     the run manifest and every curve reader already parse. A take whose analysis failed banks
     `analysis_error` and no curves. Every other take banks none, and its curves stay in the run
     manifest's rows only.
  2. `analyzed_measurements` reads the curves, gating and calibration such a take banked. One
     predicate, `banks_curves`, decides both the writing and the reading, so a record that carries
     curves for another reason (a take the retired flow banked, whose verify analysis was gated)
     still decodes.
  3. These still decode their recording: the bass view (`bass_view` through
     `decoded_measurements`), because its band SNR and harmonics read the samples, which no record
     holds (#5010 part B); the gated overlay (`reference_gated_measurement`) beside each selected
     summed room take in the frequency view, at each bank and in `frequency --analyze-wavs`; and
     every take banked before this decision.
  4. A measurement whose takes banked no curves says so with `take_curves_not_banked`, on the
     measurements page and in `jasper-round-views frequency`, instead of drawing nothing.
- **Consequences:**
  - The frequency, room and rear views of a new room, bass or rear round read the banked curves, and
    the measurements page draws those takes.
  - The curves have one derivation: the capture's own analysis. The capture and a decode read such a
    take the same way, ungated and through the same calibration. On a synthetic room capture the
    curves the host banks equal the decode's, pinned in
    `tests/test_crossover_v2_round_frequency_view.py`.
  - A banked curve carries the calibration its capture applied. `--calibration-root` reaches only the
    decoded takes and the gated overlay, which labels the calibration it used.
  - A take banks before its verdict, so a refused room, bass or rear retake carries curves too. The
    frequency and rear views read every take of the bundle, as their decodes did; the measurements
    page, which drew none of these takes, now draws each one, refused retakes included.
  - A take record grows by about 7 KB per curve (121 bins of magnitude and phase), and more with its
    repeats.
  - Rejected: banking the curves on every analysed take. Readers written when curves rode only on
    accepted takes scan records (the room ceiling, the feature classifier's pose bank, the delay
    pair, the candidates ladder), so a speaker round's MEASURE, entry baseline, poses and refused
    retakes would change what they compute.
  - Rejected: a companion `analysis/<take-id>.json` beside the take, following ADR-0354's pattern
    for impulses, as #5010's triage proposed. The C5 packet lane chose the record: it adds the least
    new machinery (no artifact kind, no manifest entry, no join in every reader) and keeps one record
    per take, in the shape every reader already takes from `curves`.
  - Rejected: banking the curves when the round is banked. A live bundle would still decode.
