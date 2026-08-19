# The graveyard — hypotheses this speaker has already executed

**Pack file draft (2026-08-19), destined for `docs/crossover-agent/`.** The
file a fresh prescriber LLM reads so it does not spend a night re-proposing
what measurement already killed. Every entry: the hypothesis, who proposed
it, the pre-registered test, the verdict, and what survived. Attribution
discipline applies throughout — measured-on-this-speaker vs modeled vs
literature is stated per number. Evidence trail:
`captures/wired-night-2026-08-19/run-log.md` (§ refs below), the banked
armrun (`captures/xover-armrun-2026-08-18/`), and the PR dispositions on
#2726–#2736.

## Dead: killed by pre-registered measurement

1. **"The in-window features are not common-mode across seats."**
   (Round-16 driver hypothesis.) Killed by the detrended per-seat table:
   5/5 seats testify on all three in-window features, spreads 0.11–0.46 dB
   (§8.1). Survivor: per-seat agreement is necessary but NOT sufficient —
   see graves 2–5.
2. **"Size cuts to the detrended excursion."** (Conductor rule, round 18.)
   Falsified at 9.1σ: predicted improvement, measured on-axis loss, even
   with gains cut by a third from round 17. Mechanism later resolved by
   grave 5 + the Q-clamp finding (below). Survivor: prediction-before-play
   caught it in one round.
3. **"Cutting the 1271–2302 Hz region is a directivity trade — on-axis
   wants that energy, off-axis doesn't."** (Driver read after round 18.)
   Killed by the RAW target-relative table: every seat sits +3.7 to
   +4.2 dB above target there, on-axis included — no above/below split
   exists (§8.1 addendum). Survivor: the mark-vs-pool anti-correlation is
   real (see Standing Facts) but its mechanism is lobe re-aiming by the
   DELAY lever, not level trades by cuts.
4. **"The cut's skirt deepened the on-axis dip."** (First skirt
   hypothesis.) Refuted in the per-config frame — the dip got SHALLOWER at
   every seat (§8 raw before/after). Reinstated in a corrected form only
   under the frozen frame: the per-config framing that "refuted" it was
   itself the flattering frame (grave 5). Lesson: a hypothesis can be
   wrongly killed by a biased instrument; re-run kills after instrument
   corrections.
5. **"The metric hid the improvement — frozen-reference re-grading will
   show the cuts actually helped."** (Conductor hypothesis after the raw
   tables.) Killed decisively in the OTHER direction: freezing the
   reference made every round WORSE (r17 +8.2σ, r18 +15.2σ), proving the
   shipped per-config re-referencing had been FLATTERING the cuts — a cut
   lowers its own reference by 0.4–0.75 dB and partially forgives itself
   (§8.9; acceptance-tested reproduction in §10.6, 16/16). Survivors, both
   load-bearing: (a) grade every EQ delta against a FROZEN baseline
   reference; (b) the earlier "best off-axis of the night" claims were
   re-referencing artifacts and were retracted.
6. **"The night's EQ failures mean magnitude EQ is structurally the wrong
   tool near the crossover (non-minimum-phase summation zone)."**
   (The 2026-08-19 external research report's central diagnosis.) Killed
   by the controls-verified excess-group-delay classification: ALL NINE
   measured features are minimum-phase, largest excursion ≤8% of what a
   genuine cancellation produces through the identical pipeline;
   angle-invariant; gate-stable (§9). Survivor: the failures re-attribute
   to SELECTIVITY — the Q ≤ 2.0 clamp made every filter ~3× wider than its
   target (natural Q 3.9–6.6 in-window, measured), with only 28–43% of
   nominal depth landing on the feature (measured, r16). The clamp was
   raised for cuts to Q ≤ 8.0 (#2730) — the codebase's own pre-existing
   cut ceiling, restored not invented.
7. **"The model is fine; the armrun graded the wrong output."** (Conductor
   reframe during planning.) Half right, then killed where it mattered:
   the armrun HAD ranked delay arms by a delay-invariant number
   (predicted_ripple_db is polarity-bimodal, delay-flat — measured), but
   grading the delay-sensitive output made the anti-correlation STRONGER:
   Spearman ρ = −1.000 (n=6 arms, honestly "one clean monotone trend
   observed once"). Survivor: the forward model gets NO ranking vote for
   the delay lever until a calibration rung passes ρ ≥ +0.6; and the
   armrun's "model's favourite arm" folklore is void (it was the polarity
   odd-one-out).
8. **"The tweeter level anomaly (A1, 10.85 dB) means driver hardware or
   declarations need checking."** Killed by three independent instruments
   agreeing to 0.05 dB that the declaration and the −14.4 dB pad are
   CORRECT (declared spread minus pad = +10.80; measured raw trim −10.85;
   attempt-01 electrical plan spread 25.227 vs declared 25.2). The refusal
   that surfaced it was a CODE defect: the giveback earned in the core
   band (2–8 kHz) was spent against a verdict graded on the crossover span
   (1.6–3.3 kHz) — fixed in #2733 (band-matched giveback; identity exact
   to machine precision). **Standing instruction: do NOT touch the pad.**
9. **"The current config is what informed measurement produces."** The
   live baseline shipped with the tweeter +2.5 dB hot (inside the ±3.0
   realized-level tolerance) via grave 8's defect, and the owner heard it
   ("a little bright" — banked as perceptual corroboration). Survivors:
   the flatness views are structurally lenient to smooth TILT (the
   re-referencing absorbs the mean; banded tolerances absorb gentle
   slope); a tilt view is a named follow-up; the ±3.0 tolerance width is
   an owner decision with numbers owed.

## Dead process hypotheses (the meta-graveyard)

- **"A green sentinel means the tree it describes."** Three separate
  incidents: a lane certifying a tree a delegate had edited mid-run;
  `mapfile` silently absent under zsh running the whole suite instead of
  a bundle; two agents' `pkill -f` killing each other's lanes. Survivors:
  freeze-and-recheck (`git write-tree` before and after), trust only the
  printed sentinel, kill by captured PID only.
- **"A correct number is a correct claim."** Three attribution errors of
  one class in one PR cycle (2.99 = arithmetic stated as measurement;
  9.21 = another lens's corpus restated as one's own; 2.688 = a fixture
  artifact labeled as banked reality). Survivor rule: **state the artifact
  a number came from, or mark it inference** — value-checks pass when the
  number is right; none asks "right about what?".
- **"The decision rule's constants transfer between grading frames."**
  The frozen frame is 1.57× noisier than the shipped frame (the freeze
  stops absorbing session level drift — that is its job), so a threshold
  registered in shipped-σ was ~1.29σ frozen, quietly doubling false-KEEP
  odds. Survivor: register thresholds in the DECIDING frame's own measured
  σ (§10.7).
- **"The deciding instrument exists because the protocol names it."**
  Round 19 was nearly graded on a frozen-reference tool that did not
  exist on disk (two banked tools: one sign-reversed, one a no-op).
  Survivor: COMPUTE THE DECIDING VIEW BEFORE PLAYING ANYTHING; instruments
  are believed only after acceptance tests reproduce banked verdicts
  (16/16, §10.6).

## Standing facts a fresh prescriber must hold (measured unless noted)

- Baseline `389bd7a55148`: shipped on-axis 0.851 ± 0.028; frozen-frame
  anchor 0.8589 ± 0.0443 (thresholds live in §10.7).
- In-window features (detrended, 5/5 seats): −1.56 @ 1037 (nat. Q 6.6),
  +0.81 @ 1406 (Q 5.1), +0.67 @ 2057 (Q 3.9). Out-of-window common-mode
  bank: +0.83 @ 4149 · −1.46 @ 4582 · +1.13 @ 5396 · −0.70 @ 6245 ·
  −2.00 @ 8530 · +1.01 @ 9509 (no blend route reaches them). ≥3.6 kHz
  sign-agree/size-split class = beaming, barred from shared EQ.
- The 1037 dip is sub-detectable to boost even perfectly routed
  (−0.033..−0.046 vs 2σ = 0.057) — the boost case rests on the
  out-of-region bank, not the dip.
- Mark-vs-pool anti-correlate under the delay lever (ρ = −0.66, banked
  armrun): delay re-aims the lobe. Vertical measurement is BLOCKING for
  delay-lever ranking claims. (Modeled mitigation only: c-t-c synthesis,
  labeled modeled.)
- Timing: raw capture starts scatter ±14–26 ms; after alignment the chain
  is stable to sd ≤7.33 µs (quantization-floored upper bound). USB drops
  one packet silently in ~0.5% of captures — the slip guard (#2731)
  rejects ≥2-sample slips; a ~1-sample slip (20.8 µs = the whole 2 kHz
  phase budget) still passes; closing it needs in-program pilot signal.
- A2: the turntable's MOTION obeys the command and the manual (+ = right,
  owner-eyeballed); the offset READBACK negates sign — never trust
  readback sign, verify by |magnitude| + command history.
- Anchor delay quantization: ±20.833 µs lattice (#2710) ≈ 12.4° at Fc —
  at/over the summation phase budget; the reconstruction gap in #2735's
  banked-curve rung traces to exactly this (unbanked anchor, one-sample
  spread).
- Current alignment commits +24.06 µs on the tweeter (measured); an
  external model claims the horn's acoustic center implies ~175–292 µs
  (modeled, unverified) — the reverse-null test adjudicates; measured
  wins until it does.
- Fc-move hypothesis for this horn (modeled, external, 2026-08-19):
  1.6 → 2.5–2.8 kHz with claimed nonmonotonicity at 2.0 ("don't stop
  halfway"); binding ceiling = woofer breakup (unmeasured). A candidate
  for the offline search, gated by #2736's floor and the breakup check —
  never a ruling.

`Last drafted: 2026-08-19 (session context; productization into
docs/crossover-agent/ with runtime wiring is the named next-session PR).`
