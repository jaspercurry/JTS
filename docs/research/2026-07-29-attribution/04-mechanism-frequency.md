# Mechanism frequency — first pass over the corpus

> WO-0 deliverable 3 of the attribution-stage plan, §4.
> Plan: [`docs/attribution-stage-plan.md`](../../attribution-stage-plan.md).
> For each seed mechanism M1–M6: which sessions in the corpus show it, at what
> magnitude, and with what strength of evidence. Read-only sweep, 2026-07-29.
> Session identifiers resolve through [`08-corpus-index.md`](08-corpus-index.md).

## Evidence-strength vocabulary (enforced throughout)

The plan's do-not #1 is "no mechanism entry without a corpus citation." This
pass adds a second discipline, from the adversarial review of the plan:

| tier | means |
|---|---|
| **adjudicated** | a discriminating probe was actually run — the speaker or the mic was physically moved, or the drive level was changed, and the feature responded (or didn't) |
| **corroborating** | consistent with the mechanism, but consistent with at least one other mechanism too. Within-session position stability is *always* this tier |
| **model-derived** | computed from the flow's own two-branch model, so it inherits whatever that model gets wrong (#1868) |
| **refuted** | a probe was run and the mechanism was ruled out for that feature |

**Why within-session position stability can never be more than corroborating**:
the shipped detector says so itself. `jasper/audio_measurement/
interference_nulls.py`, under "What `position_invariant` does and does not
claim": position-invariance within one session is equally consistent with an
origin that travels with the speaker **and** with a room path that did not
change while the session ran; a single session cannot separate the two, and
S0 separated them only by physically moving the speaker. The plan's §5 P2 row
("feature-frequency stability across cloud positions ⇒ source-fixed vs room")
over-claims and must be reworded before WO-4 encodes it.

**Instrument segregation.** Distortion and harmonic numbers are reported per
capture instrument, because a consumer-tier or phone mic contributes its own
AGC and harmonic floor. Corpus instrument census:

| instrument | tier | where |
|---|---|---|
| UMIK-2 s/n 810-8494, cal `minidsp-minidsp_umik2-b7343c0c625b` | reference | all 12 crossover-v2 bundles (every retained sidecar reads `UMIK-2 (2752:002b)`), the iLoud comparison, all S0 legs, the 2026-07-11 hardware bundles, ~half of `xover-e0` |
| Dayton iMM-6C s/n CMM31555 | consumer | ~30 captures in `xover-e0-2026-07-21` (per-capture tags in `phase0-forensics/REPORT.md` §1); 3 room-correction bundles |
| phone / browser | phone | `jts3-hardware-20260711-room-browser-retest`; the relay path generally |
| **unknown** | — | 14 of 41 room-correction bundles record only a `device_id_hash` with no model. **Excluded from any harmonic conclusion.** |

Caveat that binds every HF number regardless of tier: the two per-serial
calibrations disagree by ~4.7 dB above 8 kHz on the same horn (#1672), so at
most one is right up there.

---

## M1 — Inter-driver time misalignment at Fc

**Fix class `delay`. Frequency: the most common mechanism in the corpus —
present in every crossover session that reached MEASURE.** Phase reach across
the 12 jts3 bundles (counted by which `*_program.wav` each holds): 10 reached
CHECK, **7 reached MEASURE**, 5 reached cloud, 4 reached VERIFY, 4 produced a
candidate. One of the 7 MEASUREs is the #1838 artifact, leaving **6 valid
alignment estimates on jts3** in the 07-25 → 07-29 window, plus E0's 7 runs
(07-21) and the cdhorn-live-session's runs 3–7 (07-24).

| session | date | evidence | magnitude | tier |
|---|---|---|---|---|
| `cap_J2OLTNvzmApF0cEAgxrIZw` (bundle `7f54494228cc`) | 07-29 08:29 | coherent sum −5.93 dB vs phase-blind magnitude sum −0.88 dB at 1919 Hz | **5.0 dB is phase**, 0.9 dB is slope | model-derived |
| same | 07-29 | notch present at 4/4 examined cloud positions, −4.1 … −6.8 dB; 8-position mean −3.56 dB | notch is 0.5–3.2 dB **deeper at any single position** than the cloud mean shows | corroborating |
| `cap_6lv47oF_qkVtwk9Fg6Liaw` (`8c2d69a5bfbd`) | 07-29 12:00 | `delay_us 23.924`, `delay_role woofer`, `polarity invert`, `alignment_confidence 0.6747` | `predicted_ripple_db 7.668` | model-derived |
| `cap_lMo1I-yxZqZyQ6lr4nuZlA` (`e0d5385c09d0`) | 07-28 13:08 | `delay_us 22.771`, `anchor_delay_us −7.252`, `snap_delta_us 30.023`, `snap_found true`, `confidence 0.7056` | `predicted_ripple_db 9.596`; `flatness_improvement_db 2.700` | model-derived |
| E0, 7 completed runs, 3 placements | 07-21 | alignment confidence **never cleared its own 0.60 floor** (0.44–0.52); within-run repeat σ 0.16 samples; across-run disagreement ~306 µs at `P3-baddesk` | predicted in-band ripple 10.96–24.72 dB | corroborating |

**Magnitude of the lever (model-derived, not measured):** +125 µs relative
tweeter delay takes 1919 Hz from −5.93 to −1.36 dB and ripple from 7.73 to
3.28 dB, with the HF carved bands invariant to ≤0.01 dB — a falsifiable
prediction. It is λ/4 at Fc, outside #1649's ±λ/6 snap radius, so the shipped
selector structurally cannot reach it.

**What the corpus cannot answer.** No reverse-null (P1) has ever been run, so
M1 and M3 are **not separated by measurement anywhere in this corpus** — only
by the flow's own model, which is the thing #1868 says is untrustworthy at
exactly this frequency. The bench for it is #1870.

**One M1-shaped reading that is an instrument artifact, not a mechanism:** the
07-28 16:23 MEASURE (`cap_-Us10xORVNlFa`, bundle `9445639e508f`) reports
`alignment_status: delay_exceeds_search_window`, `delay_us −1732.044`,
`confidence 0.0`, `tweeter_repeat_epsilon_ppm 495.176`. That is the #1838
low-SNR run. Exclude it from any M1 statistic.

---

## M2 — Source-fixed reflection comb

**Fix class `document_as_physics`. Frequency: present on 16/16 positions of
every S0 leg and in every 07-27→07-29 crossover session that produced a
cloud.**

**This is the corpus's one adjudicated source-fixed mechanism** — and it was
adjudicated by *physically moving the speaker*, across three geometries, not
by within-session position stability:

| leg | geometry | n | τ | level re direct | tier |
|---|---|---:|---|---|---|
| S0 main | historical desk position, 10-position cloud (4 of them a hand-width low) | 10 | 310.4–328.7 µs, median 321.5, median pairwise shift 1.6 %, `geometry_locked` | −8.17 … −8.80 dB | **adjudicated** (leg 1 of 3) |
| S0 desk edge | speaker moved flush to the desk front edge | 3 | 319.2–321.4 µs | −8.85 … −9.04 dB | **adjudicated** |
| S0 ground plane | desk removed entirely, speaker on the floor | 3 | present, cepstrum 327.2 / 270.2 / 342.8 µs | −7.05, −9.96, −10.43 dB | **adjudicated** |

Two independent estimators agree on the reflection ratio to 0.03: time-domain
r = 0.373 (desk, n=6) vs frequency-domain r = 0.342 (from the 6.19 dB HF null
depth). Desk-edge leg: 0.356 vs 0.331.

The 2026-07-29 forensics on `cap_J2OLTNvzmApF0cEAgxrIZw` re-confirmed the same
family three ways on the *new* horn — solo-driver magnitude comb, IR secondary
arrival at 292 µs / −8.2 dB, cepstral quefrency 333 µs — giving τ ≈ 292–333 µs,
r ≈ 0.28, ~11.4 cm path. Carved rungs: 8.4 / 11.4 / 14.8 kHz. Uncarved and
therefore still EQ-eligible: n=1 at 4948 Hz (the observed 4–5.6 kHz deficit)
and n=0 at 1649 Hz, inside the crossover overlap (#1867).

**Null depth: 5.9–7.0 dB across all of the S0 desk leg, deepening to
10.4–12.7 dB on the floor.** A hard ceiling falls out of r: a reflection with
r = 0.373 can cut a null no deeper than 20·log₁₀((1+r)/(1−r)) = **6.81 dB**
anywhere.

**The reflector is still unnamed** (baffle edge vs horn mouth vs desk), though
the horn-swap comparison below narrows it. The electrical path is ruled out by
a loopback control (r ≈ 0.021, ≈0.3 % of echo energy — `RB-3-horn-swap-runbook.md`).
P4 (rotation solo) is the adjudicator; it is the second half of #1870.

**The horn swap is an unclaimed, pre-registered discriminator the corpus
already ran.** `flat-linearization-20260725/RB-3-horn-swap-runbook.md` commits
the prediction *before* the swap: "more lip roll-back should reduce the echo's
amplitude (r) without materially shifting its delay (τ), because delay tracks
mouth radius while amplitude tracks how abruptly the rim's curvature changes."
The owner mounted the new horn on 2026-07-27 (~3 h before the 12:59 fit).
Comparing across it: **τ essentially unchanged** (S0 old horn median 321.5 µs
→ 07-29 new horn 292–333 µs) while **r fell** (S0 0.373 → 07-29 ≈0.28; the
07-27 first-cloud session reported 0.175). That is the runbook's prediction
confirmed, and it points *at* the horn rim rather than away from it.

Three caveats that keep this from being a finished adjudication, and they
matter: RB-3 was never executed as its own graded pre/post session (the
runbook is still `Status: DRAFT`), so this is opportunistic cross-session
comparison — exactly the #1859 frame hazard; the 0.175 figure carries #1763's
uncalibrated-regime asterisk; and the two sides used different DSP states.
The runbook argues the last point is harmless because the comb is
DSP-independent — a loopback control measured the electrical graph's own
contribution at **r ≈ 0.021, ≈0.3 % of echo energy**. Even so, the clean
version is the runbook's own "optional but recommended" same-instrument
before/after, or P4.

---

## M3 — Unfitted-overlap slope error

**Fix class `eq` / re-fit. Frequency: present wherever M1 is, because nothing
in the corpus separates them.**

| session | evidence | magnitude | tier |
|---|---|---|---|
| `cap_J2OLTNvzmApF0cEAgxrIZw` | phase-blind magnitude sum at 1919 Hz | **0.9 dB** of the 5.9 dB dip | model-derived |
| synthetic reconstruction (#1817) | the fit flattens a curve measured *through* an LR4 against a **flat** target | attracts **+2.379 dB at 1570.6 Hz** (0.79·Fc); the crossover eats 2.27 dB of it, so the emitted branch peaks at +0.111 dB and charges 1.11 dB of headroom | adjudicated (synthetic, exact) |

**What the corpus cannot answer.** M3's own signature in the plan — "reverse-null
cannot be driven deep at any delay" — has never been measured. Everything
above is either model arithmetic on one session or a synthetic reconstruction.
**M3 is the least empirically grounded of the six seeds.**

**Do not confuse M3 with the level-frame family.** #1667's 1.7–6.3 dB trim
bias and the iLoud comparison's 7–11 dB tweeter deficit are *level* errors in
the overlap band, not acoustic-slope errors, and they route differently. See
the proposed M7 below.

---

## M4 — Frame mismatch (window vs power, and reference frame generally)

**Fix class `measure_differently` / `document_as_physics`. Frequency: every
multi-geometry or cross-session comparison in the corpus shows it. Largest
magnitudes in the whole corpus.**

| comparison | magnitude | tier |
|---|---|---|
| S0: 8–16 kHz max deviation of the **same speaker** across five frames — desk edge −6.94, desk cloud at tweeter height −8.00, all-10 cloud −8.94, low-4 −11.33, ground plane **−24.43 dB** | **17.5 dB spread**, all frames legitimate | adjudicated (mic and speaker physically moved) |
| #1859: 07-27 single desk point (0.65 m, design axis, 7 ms gate) vs 07-29 8-position in-room cloud, byte-identical DSP, same mic, same cal sign | 3.1 dB at 2.8–4 kHz rising monotonically to **7.66 dB at 12.3 kHz** | corroborating (a physical change over ~40 h is not excluded) |
| Fc notch, same session: 4 individual positions −4.1…−6.8 dB vs 8-position mean −3.56 dB | 0.5–3.2 dB — **spatial averaging partially fills the notch**, so the household's real seat is worse than the cloud curve claims | corroborating |
| #1857: full-range 250 Hz–8 kHz reference mean vs a woofer-anchored frame, inside a single verdict | **3.13 dB** of frame drag, enough to point the verdict at the wrong driver | adjudicated (recomputed from the persisted curve to the digit) |

**The plan's seed cell understates this.** M4's "Observed" column cites #1859's
3–7.7 dB. The corpus's real number is the S0 five-frame spread: **up to 17.5 dB
in the top two octaves on one speaker with one DSP state.** M4 is not a
nuisance term; it is the largest single source of disagreement in the archive.

**What the corpus cannot answer.** The matched-geometry A/B that #1859 names
(one desk-point gated capture + one cloud against the same unchanged state,
same day) has not been run, so the frame-vs-physical-change question is open
and the Q4 target ruling stays a working hypothesis, exactly as the plan says.

---

## M5 — Boundary / SBIR (desk bounce)

**Fix class `physical`. Frequency: measurable as a positive control; zero
attributed instances in the corpus.**

| session | evidence | tier |
|---|---|---|
| S0, mic dropped one hand-width, 1.8 kHz dip | Implied relative delay goes 275.7 → 253.3 µs (ratio 0.919). A desk bounce scales with mic height, so that ratio demands a mic **62–123 cm above the desk at 1 m**. Forward-checking a plausible 21.7 cm geometry predicts nulls at 2356 / 2870 / 3359 Hz for a 5 / 8 / 10 cm drop; it measured **1974 Hz** (19–70 % error) | **refuted** for this feature |
| S0 ground-plane leg | The floor captures carry their own arrival at 125–146 µs at −0.64 … −2.57 dB re direct (r = 0.74–0.93) — a deliberately created boundary bounce, and it made the ground plane the **worst** top-octave reference of the three legs | **adjudicated** (positive control) |
| iLoud comparison, 0.6–0.75 m desk geometry, 6 captures | **No reflection detected within 7 ms on any capture**; gated and in-room deltas agree within ~0.5 dB | adjudicated (negative) |
| E0, 14 MEASURE captures | 12 of 14 gate at `validity_floor_hz = 142.857` = 1000/7, i.e. the analysis window lands exactly on the ~10 ft room's 7 ms physical ceiling; the other 2 gate at 3.33 ms / 300 Hz, meaning a boundary reflection arrived **before** 7 ms in those captures | corroborating |

**The plan's seed cell for M5 does not have a corpus citation.** It reads
"Observed: Room-line corpus; predicted by geometry." No room-correction bundle
carries an SBIR attribution, and the room line has been dormant since
2026-07-16 (41 bundles, 25 of them empty shells). Under the plan's own do-not
#1, M5 must either cite the S0 ground-plane leg and the E0 validity-floor
evidence above, or be re-ranked.

**What the corpus cannot answer.** The one JTS3 geometry that would produce a
classic desk-bounce SBIR at a household listening seat has never been
deliberately swept; the ground-plane leg is the closest and it was built to be
a *reference*, not an SBIR case study.

---

## M6 — Nonlinearity (driver / port, level-dependent)

**Fix class `physical` / level policy. Frequency: one reference-tier negative
result; no harmonic decomposition anywhere in the corpus yet.**

### Reference tier (UMIK-2, laptop-side, iLoud comparison 2026-07-27)

| probe | result |
|---|---|
| Two-level invariance (**this is P3, already run once**) | Re-measured at **10 dB lower drive: identical within 0.12 dB**. Level-dependent behaviour is ruled out as a contributor to the 7–11 dB tonal defect at the levels tested |
| Total harmonic distortion | JTS3 median **0.14 %**, p90 0.27 %; iLoud reference median 0.07 %, p90 0.30 %. After the hand correction JTS3 improved to **0.07 %** |

Instrument bound: the UMIK-2's own harmonic floor is undocumented in this
corpus, so 0.07–0.14 % should be read as "at or near the instrument floor,"
not as a driver measurement. The number is *not* transferable across mics —
see #1672.

### Consumer tier (iMM-6C)

~30 captures exist in `captures/xover-e0-2026-07-21/`, individually tagged by
mic in `phase0-forensics/REPORT.md` §1. **No harmonic analysis has been run on
them, and any that is must be reported separately from the UMIK-2 results and
labelled instrument-bounded** — the iMM's 8–16 kHz reading of this horn is
already known to disagree with the UMIK's by ~4.7 dB (#1672).

### Phone / unknown

`jts3-hardware-20260711-room-browser-retest` and 14 of the 41
room-correction bundles either used a browser/phone path or record no mic
model at all. **Excluded from harmonic conclusions entirely.** A phone
capture carries AGC and its own harmonic floor; a Farina pass over one reads
mic nonlinearity as speaker nonlinearity.

### What the corpus cannot answer

**Everything except the one level-invariance result above.** No Farina
harmonic-IR extraction has been run on any capture. That is probe P6 and it is
the companion agent's pass over the retained ESS sweeps; M6 cannot be ranked
or given a fix class until it reports. Two constraints for that pass, stated
here so the registry entry inherits them: segregate strictly by instrument,
and treat the phone/unknown group as unusable for harmonic claims.

---

## Frequency roll-up

Denominators differ by mechanism because each needs different evidence: M1/M3
need a MEASURE (6 valid on jts3, +7 E0 runs, +5 cdhorn-live runs = **18
alignment estimates**); M2/M4 need multiple geometries; M5/M6 need a probe.

| mechanism | shows up in | strongest magnitude | best evidence tier |
|---|---|---|---|
| M1 alignment at Fc | **18 / 18** alignment estimates — every one either reports a delay+ripple or fails its own confidence floor | 5.0 dB at 1919 Hz | **model-derived only** — no P1 ever run |
| M2 source-fixed comb | 16 / 16 S0 positions across 3 geometries; every 07-27→07-29 cloud | 5.9–7.0 dB (desk) → 10.4–12.7 dB (floor); r-ceiling 6.81 dB | **adjudicated** (speaker relocated ×2 + loopback control); horn-swap prediction opportunistically confirmed |
| M3 overlap slope | 1 measured + 1 synthetic | 0.9 dB at Fc; +2.379 dB fit attraction at 1570.6 Hz | model-derived / synthetic — **least grounded seed** |
| M4 frame mismatch | every multi-frame comparison in the archive | **17.5 dB** (S0 five-frame, 8–16 kHz) | **adjudicated** |
| M5 boundary/SBIR | 1 positive control, 1 refutation, 2 negatives; 0 attributed instances | −24.43 dB HF penalty on the ground plane | **adjudicated** (control) + **refuted** (for the 1.8 kHz dip) |
| M6 nonlinearity | 1 reference-tier negative | ≤0.12 dB level dependence at −10 dB drive | adjudicated (UMIK-2 only); no harmonic pass yet |
| **M7 inter-driver level frame** (proposed) | every commissioned profile inspected — 07-25 and 07-27 carried the same defect shape | **7–11 dB** | **adjudicated** (independent hand correction closed it to ±0.9 dB) |
| **M8 vertical lobing at Fc** (proposed) | S0's 3 mic heights | dip depth 10.7 → 4.1 → 1.7 dB with mic height | **adjudicated** (old horn only — re-run owed) |

## What the seed table gets wrong on the evidence

Five corrections, in order of consequence.

1. **M5 has no corpus citation** ("Room-line corpus; predicted by geometry").
   The room line has never attributed an SBIR feature and has produced nothing
   since 2026-07-16. Re-cite it to the S0 ground-plane leg (positive control)
   and E0's validity-floor evidence, or re-rank it. As it stands it violates
   the plan's own do-not #1.

2. **M4 is understated by a factor of two** and mis-scoped. The cited number
   is #1859's 3–7.7 dB; the corpus's number is the S0 five-frame spread, up to
   **17.5 dB** at 8–16 kHz on one speaker with one DSP state. M4 is not just
   "window vs power" — it is the whole reference-frame family, and it is the
   corpus's largest disagreement.

3. **The plan is missing the corpus's single largest measured defect.**
   Nothing in M1–M6 covers **inter-driver level-frame error**: the two
   drivers' realized passband levels are never compared anywhere in the
   pipeline. That is the mechanism behind the 7–11 dB dark tweeter (iLoud
   `REPORT.md` §3), the 13.9 dB gap between per-driver fit targets
   (`FORENSICS-SYNTHESIS.md` row 1), the trim frame sitting at the bare
   datasheet sensitivity gap with a −14.4 dB L-pad in circuit (row 4), and
   #1667's 1.7–6.3 dB trim bias. It is **adjudicated** — an independent hand
   correction moved every band from 300 Hz to 16 kHz within ±0.9 dB of the
   reference — and it is the only mechanism in this corpus with a
   before/after listening verdict attached. Propose **M7 — inter-driver level
   frame**, signature "one driver's passband sits N dB off the other's against
   any common anchor; broad and monotonic, not an interference notch", probe
   "per-driver passband comparison against a declared-sensitivity prior (free,
   back-catalog)", fix class `eq` (level).

4. **Vertical lobing at Fc deserves its own entry (propose M8), and the corpus
   already holds its probe result.** S0 measured the Fc-region dip's depth
   against mic height: **10.7 dB** at tweeter height (n=6) → **4.1 dB** a
   hand-width low (n=4) → **1.7 dB** on the ground plane (n=3), while the
   8–16 kHz comb held 5.9–7.0 dB and then *deepened* to 10.4–12.7 dB. Leg-A
   Pearson correlation between the two features: r = −0.05 (n=13). That is a
   strong vertical-angle dependence and it is the dissertation's own lobing
   signature ("if the notch frequency moves >~10 % with vertical angle ⇒
   vertical lobing, measure/aim on design axis, do not EQ"). **M8 — vertical
   lobing at Fc**: signature "Fc-region dip depth and/or frequency tracks
   vertical mic offset; uncorrelated with the HF comb", probe P5
   (design-axis / vertical-offset capture), fix class `physical` /
   `measure_differently`. Lobing routes to `physical`; M1 routes to `delay`;
   M3 routes to `eq`. Folding all three into "the Fc dip" will produce a
   wrong fix class.
   **Caveat that must ride with this**: S0 (2026-07-25) predates the horn swap
   (~2026-07-27 10:00), so the vertical data is on the *old* horn. It is a
   reason to re-run P5 on the current hardware, not a finished answer.

5. **M1's and M2's signature wording over-claims.** M1's "per-position stable"
   and M2's "position-invariant comb", and the P2 row in §5
   ("stability across cloud positions ⇒ source-fixed vs room"), all assert a
   classification the shipped instrument's own docstring says a single session
   cannot make. Reword to "position-stable within session" and make P4
   (rotation solo) the named adjudicator for source-vs-room. M2 keeps its
   `document_as_physics` fix class on the strength of S0's three-geometry
   pass, not on position stability.

Two smaller notes:

6. **M2's "τ≈303 µs" is not the corpus figure.** The measured range is
   292–333 µs (07-29, new horn) and 310.4–328.7 µs with median 321.5 (S0,
   n=10, old horn). The registry entry should carry the range and cite S0 as
   the adjudicating evidence, since S0 is both stronger and earlier.

7. **M6's "To be seeded by WO-0 Farina pass" is right but incomplete.** One
   P3 two-level invariance result already exists and is negative (identical
   within 0.12 dB at −10 dB drive, UMIK-2). Record it now so the Farina pass
   is interpreting a delta, not starting cold.

8. **M2's probe list is missing a discriminator the corpus half-ran.** The
   plan lists P4 (rotation solo) and P2 (position-variance). A *geometry swap
   of the suspected radiator* — the RB-3 horn swap — is a stronger
   discriminator than either, it has a written pre-registered protocol
   (`captures/flat-linearization-20260725/RB-3-horn-swap-runbook.md`), and its
   prediction has already been opportunistically confirmed (τ held, r fell).
   It is not generalizable as a household probe, but it belongs in the
   mechanism's evidence chain and in #1870's bench, and the runbook's
   loopback control (electrical path r ≈ 0.021) should be recorded as the
   negative control it is.

---

*Compiled 2026-07-29 by WO-0 Agent A (read-only). Sources as cited per row:
`captures/flat-linearization-20260725/s0-analysis/REPORT.md`;
`captures/flat-linearization-20260725/phase0-forensics/REPORT.md`;
`captures/iloud-comparison-20260727/{REPORT,FORENSICS-SYNTHESIS}.md`;
`captures/xover-e0-2026-07-21/{RESULTS,MANIFEST}.md`; jts3's retained
sidecars and `active_speaker_crossover_v2_state.json`; and issues #1649,
#1654, #1667, #1672, #1763, #1817, #1838, #1857, #1859, #1867, #1868, #1869,
#1870.*
