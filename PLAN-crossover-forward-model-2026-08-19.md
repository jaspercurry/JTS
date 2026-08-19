# PLAN — the crossover forward model and its search

> **Status: plan, ratified by nobody yet.** Written 2026-08-19 against
> `origin/main` at `4b7e76db4`. Every "exists" claim names the symbol it was
> read from, at that commit. This closes gap 3 of
> [`docs/active-speaker-tuning-layers-design.md`](docs/active-speaker-tuning-layers-design.md)
> "Stage P2 — crossover tuning by measurement": *"The forward model and the
> optimizer — MISSING."*

---

## 0. The plan in one screen

Predict what the speaker will measure, for any candidate crossover, without
playing anything. Rank candidates. Hand the top few to the prescriber harness
that already exists. Let hardware overrule the ranking.

Five new modules in `jasper/active_speaker/crossover_v2/`:

| module | one job |
|---|---|
| `candidate_space.py` | what a candidate IS, and which ones are legal |
| `forward_model.py` | candidate + per-driver complex responses → predicted sum per angle |
| `objective.py` | score a predicted sum **in the units the hardware round grades in** |
| `search.py` | enumerate the discrete choices × scan the continuous ones → ranked shortlist |
| `crossover_prescription.py` | the strict gate that lets a ranked candidate enter the LLM loop |

Three things die: `fc_sweep.py`, `fc_selector.py`, and their call sites.

**One thing must be fixed before any of it plays a tone:** the routine apply
transaction does not check a chosen crossover frequency against the tweeter's
declared safe floor. See §9.

---

## 1. Headlines — six premises that are false at HEAD

Per the house rule, a premise found false is a headline. All six change what
gets built. The first is a safety finding and outranks the rest.

### H1 — SAFETY: the apply transaction does not check Fc against the tweeter's declared floor

Today nothing varies Fc, so nothing has needed to. This plan makes Fc a
searchable parameter, which turns a dormant gap into a live hazard.

A repo-wide grep of `baseline_profile.py`, `measured_crossover_candidate.py`
and the whole `crossover_v2/` package for `resolve_driver_low_limit`,
`declared_protection_highpass_floor_hz`, `protection_highpass_floor_satisfied`
and `recommended_highpass_hz` returns **zero matches**. The routine
apply/rollback transaction — `build_baseline_profile_candidate` →
`apply_baseline_profile` → `dsp_apply.apply_dsp_config` — never compares the
chosen corner to the declared minimum.

What *is* enforced on every apply is **structural presence only**:
`graph_safety.unprotected_tweeter_outputs`, asserted inside
`camilla_yaml.emit_active_speaker_baseline_config` and re-proved by
`measured_crossover_candidate.prove_candidate_config`. It answers "is there
*a* high-pass on this output", not "is its corner at or above this driver's
declared minimum".

The check that *does* compare against the floor —
`path_safety._tweeter_protection_floor_verdict`, using the shared predicate
`driver_protection.protection_highpass_floor_satisfied`, refusing with
`tweeter_crossover_below_declared_protection_floor` — runs at
**startup / commission-load**, not on a routine correction apply.

**Consequence, stated plainly:** a search that proposes a lower Fc cannot rely
on the apply path to refuse an unsafe one. The bound must be enforced by the
candidate admissibility filter *and* re-enforced at the apply boundary. §9
makes closing this a blocking prerequisite of the Fc lever, not a nice-to-have.

### H2 — The forward model is not missing. It is written, and it is chained to a build.

Gap 3 says the predictor does not exist. That is too strong; the physics is in
the tree twice:

- `program_analysis.py::predicted_branch_sum(W, T, trim_w_db, trim_t_db, sign,
  *, freqs_hz, residual_delay_us)` returns `W·g_w + sign·T·g_t·e^{−jωτ}` —
  public since #1668 PR-D, and what VERIFY already grades against.
- `fc_selector.py::predict_pose_sum_db(evaluation, curves)` computes
  `S_pose = Σ_role M_pose,role · operator_role`, with the operator built by
  `fc_sweep.py::branch_operators` — *"sign * (C_c / P) * K * 10**(trim/20),
  plus the alignment's polarity and residual delay on the tweeter"*.

So `H_sum(f,θ) = Σ_role M_role(f,θ)·C_role(f)·g_role·p_role·e^{−jωτ_role}` is
**already implemented, already using the biquad math CamillaDSP runs**
(`branch_operators`: *"Every factor is evaluated by the SAME function the
emitted graph is built from"*).

What is actually missing is narrower:

1. **One axis only.** `fc_sweep.candidate_sections`: *"Order, direction and role
   assignment are the preset's — only the corner moves, because R17 adjudicates
   WHERE to cross, never what shape to cross with (topology search is deferred,
   #1894)."* And `fc_candidate_set` is documented as deriving *"the bounded
   **LR4** Fc candidate set"* — order is hardcoded, not swept.
2. **Each candidate costs a build.** `branch_operators` takes `linearization`
   and `trims` from a per-candidate fit: `FC_CORNER_COMPUTE_COST_S = 16.0`s,
   `MAX_PROPOSED_FC_CANDIDATES = 5`. A budget for five corners, not a search.
3. **The objective is a different quantity from the grade** (H3).
4. **There is no search.**

**Consequence:** this is refactor-and-extend, not green-field. The physics gets
lifted out of the build-bound path, generalized from one axis to five, and
given an objective that matches the referee.

### H3 — There are TWO production "grades", and the plan must name which one it matches

This is the commensurability trap, and it is sharper than "smoothing and
pooling must match".

- **Round / prediction-gate currency:** `flat_spec.spec_convergence_residual(
  report).rms_db` — a bin-count-weighted RMS across the three spec bands. Used
  by `crossover_v2/verification.py::_pooled_residual` (line 656),
  `round_evidence.py::_post_residual` (908), and
  `accountability.assess_accountability` (501, 559-560).
- **Attempts-ledger currency:** `attempts_loop.AttemptRecord.grade_db`, which is
  populated from **`max_db_notch_excluded`**, computed in
  `program_analysis.py` (~6798) and wired in by
  `crossover_v2_flow.attempt_record_from_verify` (3240). **This number never
  touches `flat_spec.py`.**

`round_evidence.py`'s own `post_residual_db` docstring records a prior false
claim being corrected on exactly this point — *"the attempts ledger's
`grade_db` is read from `analysis.verify_tracking`'s `max_db_notch_excluded`…
never from here."*

**Decision, stated once:** this plan's objective targets
**`spec_convergence_residual`**, because that is the currency of the
prediction-accountability gate — the thing structurally closest to "grade a
candidate before it touches hardware". The plan does **not** predict
`max_db_notch_excluded`, and therefore does not predict the attempts loop's
own continue/stop behaviour. That is a stated limitation, not an oversight.

### H4 — The frozen reference is a ratified rule with no implementation, and freezing it *changes* the numbers

Rule 5 of the design doc requires grading against a frozen baseline reference,
and says the comparator is **MISSING**:

> *"`evaluate_flat_spec` computes `reference_db` from whichever curve it is
> handed and takes no frozen-reference argument — so the honest comparator is a
> rule this stage ratifies, not behaviour the shipped views have."*

Verified: `evaluate_flat_spec(freqs_hz, spec_smoothed_db, exclusion_mask=None,
*, smoothing_fraction=3, trusted_floor_hz=None)` has no reference parameter.
`reference_db` is `_power_mean_db` over `REFERENCE_BAND_HZ` of **the same curve
being graded**. Every before/after comparison currently evaluates each side
independently, so **each side gets its own reference**
(`verification._pooled_residual`, `accountability`'s `_rms` closure).

The mechanism, in the repo's own words. `flat_spec.py:496-506`:

> *"the reference is a power mean, so removing the sub-floor region moves the
> zero that every surviving deviation is stated against… On the S0 corpus that
> shift is **+1.0676 dB in the FLATTERING direction**"*

and `verification.py:706-718`:

> *"Narrowing the mask re-routes `evaluate_flat_spec`'s own centering: its
> reference level comes from `REFERENCE_BAND_HZ` intersected with what
> survives."*

**The trap, and it cuts both ways.** Freezing the reference on the *objective
only*, while the on-device grade keeps self-referencing, is worse than
freezing neither: the two would then disagree by construction, and cutting-
shaped candidates would be systematically under-credited relative to what the
device will score them at.

**Consequence:** PR-1 changes **both readers or neither**. It is the first PR
of this plan for that reason, and its test is that a broadband cut scores
*worse* under the frozen reference than under self-reference — the flattery,
pinned as a number rather than asserted.

*(Rider, so nobody conflates two things with the same name: the "+8.2σ /
+15.2σ against the frozen reference" figures quoted in
`blend_prescription.py:263` come from a campaign-side analysis with its own
frozen baseline curve. **No reusable "N σ against a frozen reference" function
exists in `jasper/`.** That is a different mechanism from `flat_spec`'s
reference level, and this plan builds the latter.)*

### H5 — The "model is anti-correlated with measurement" evidence indicts the wrong instrument

> **CORRECTED 2026-08-19, same day, by running the test this section proposed.**
> The instrument critique below is right. **The conclusion I drew from it was
> wrong**, and the correction is the more important half.
>
> I graded the banked arms with the delay-SENSITIVE output
> (`verify_priors.verify_measured`, both sides through one identical
> reduction; local grader validated against `evaluate_flat_spec` to 1.1e-16 dB).
> Result: against the campaign's pooled measured referee the model is
> **perfectly anti-correlated — Spearman ρ = −1.000 across 6 arms / 5 distinct
> delays** (and ρ = −0.986 on the product's own two numbers). Against the mark
> it is unstable (+0.66 to −1.00 depending on reference policy and subset), i.e.
> says nothing at this n.
>
> **So "the model has been anti-correlated with measurement" SURVIVES**, on
> better evidence than it originally had. What survives of this section is only
> the narrow claim that the run-log's *stated* evidence was invalid — see the
> polarity confound below, now measured: `predicted_ripple_db` is bimodal by
> polarity (invert 13.34–13.99, keep 4.24–4.63; between-group ≈ 9.1 dB,
> within-group ≤ 0.65 dB across a 200 µs delay range) and correlates with
> nothing measured (ρ = −0.43 / +0.09).
>
> Three consequences, all live: **rung 4's ρ ≥ +0.6 bar would fail today**, so
> the model gets no ranking vote for the delay lever; **the objective's currency
> is an open design question**, because the broadband residual is uncorrelated
> (ρ = +0.03) with the crossover-region feature the delay lever actually moves;
> and **R1 is confirmed in data** — the mark and the pool anti-correlate with
> each other (ρ = −0.66), which is a lobe being re-aimed.
>
> Full analysis, tool and data:
> `captures/xover-armrun-2026-08-18/analysis/README-delay-arm-regrade.md`.

This matters because it is the stated reason the model *"earns its sign
empirically or it does not get a vote."*

`captures/xover-armrun-2026-08-18/run-log.md` (~line 388) ranks four **delay**
arms by `predicted_ripple_db`:

| arm | `predicted_ripple_db` | measured dip worst |
|---|---|---|
| −250 | 13.66 | −2.50 |
| −350 | 13.77 | **−0.97 / −1.07** (best) |
| −450 | 13.34 | −2.13 |
| −550 | **4.63** (model's favourite) | −2.51 (worst) |

`predicted_ripple_db` is documented in code as deliberately delay-independent.
`program_analysis.py`, `CrossoverCandidate` docstring (~1477):

> *"`predicted_ripple_db` is measured on the INDEPENDENTLY ALIGNED
> (zero-residual) branch sum **at the committed POLARITY** … It asks a
> capture-quality question — how coherently can these two branches sum at all —
> which is **a property of the measurement and not of the delay selection** …
> Moving it onto the delay-carrying curve would let a candidate's own alignment
> lower its own disclosure number."*

and the module docstring, line 43: *"a capture-quality number **the delay must
not move**."*

**Four delay arms were ranked by a number engineered not to respond to delay.**
The delay-sensitive output — `predicted_sum`, *"what the emitted graph will do,
and what VERIFY's tracking comparison grades against"* — was never scored
against those arms.

I am **not** claiming the model is good. I am claiming the cited evidence does
not show it is bad, so "anti-correlated twice" is not an established fact about
this model. The honest position: **the forward model's delay axis has never
been graded against measurement**, and the banked armrun makes that a cheap,
decisive experiment (§11 rung 4).

Two riders so this is not over-read:

- The `−550` outlier (4.63 against ~13.5 for its neighbours) is **not explained
  by this argument.** A delay-insensitive metric should not swing 9 dB. The
  most likely mover is the committed polarity, which `predicted_ripple_db`
  *does* track. Rung 4 must explain it, not wave at it.
- A *different* anticorrelation (1000–1600 Hz, in W1) is recorded in
  `captures/first-principles-panel-20260731/measurement-verdict.md`. Separate
  finding, separate quantity, not addressed here.

### H6 — The per-angle per-driver replay set is in the armrun bank, not the wired night

The brief points the validation ladder at `captures/wired-night-2026-08-19/`.
That bank has **no lateral captures at all** — 4 `measure` (on-axis,
per-driver), 14 `verify` and 55 `cloud_verify` (both summed).

`captures/xover-armrun-2026-08-18/` has what the ladder needs, per arm:

| kind | per arm | what it is |
|---|---|---|
| `*_measure.wav` | 1 | interleaved per-driver capture, on-axis |
| `*_lateral.wav` | **6** | the same program replayed at six poses — per-driver, off-axis |
| `*_verify.wav` + `*_cloud_verify.wav` | 1 + 4 | the summed measured outcome |

across 7 arms (`control`, `a250`, `a350`, `a450`, `a550`, `a550b`,
`adopt350`) — 137 WAVs, 371 MB. A complete, free, offline calibration set that
**varies a parameter the model claims to predict**. The wired night cannot
supply that.

---

## 2. What is reused, with the citation for each

| what | symbol | why it is the right one |
|---|---|---|
| biquad coefficients | `jasper/sound/profile.py::_biquad_coeffs`, `_filter_response_complex` | RBJ cookbook, bilinear, `fs = RESPONSE_SAMPLE_RATE_HZ = 48000`; magnitude fixture-pinned against CamillaDSP, and against the browser twin `deploy/assets/sound-profile/js/eq-math.js` |
| biquad cascade | `branch_chain.py::chain_response(filters, freqs_hz)` | *"there is exactly one biquad evaluator in this codebase and this is a caller of it, not a second one"* |
| crossover response | `branch_chain.py::crossover_response_complex(freqs_hz, sections)` | the **digital** LR sections; the analog closed form was tried and under-read by up to **1.58 dB (LR2)** near Nyquist, in the loud direction |
| crossover parameterization | `branch_chain.py::CrossoverSection(fc_hz, order, highpass)`; `sections_by_role(regions)` | already the C-term vocabulary |
| evaluation grid | `branch_chain.py::_evaluation_grid`, `CHAIN_GRID_HZ` (1/48 oct, 20 Hz–20 kHz, 480 pts) | unions each filter's centre + adjacent-pair midpoints + band edges; cascade error **≤ 0.07 dB**, **≤ 0.21 dB** at the fit's `_PEAKING_Q_MAX = 8` |
| the summation | `program_analysis.py::predicted_branch_sum` | public; already VERIFY's reference |
| the delay convention | `program_analysis.py::summed_model_residual_delay_us` | *"it has ONE owner"* — see the landmine, §5 |
| per-driver complex data | `program_analysis.py::DriverResponse.complex_tf` | direct-arrival windowed + adaptively reflection-gated |
| per-angle sum | `fc_selector.py::predict_pose_sum_db` | lifted into `forward_model.py` (§10) |
| the operator | `fc_sweep.py::branch_operators` | lifted and decoupled from the build (§10) |
| Fc safety bounds | `fc_sweep.py::fc_candidate_set`, `resolve_fc_search_band` | lifted into `candidate_space.py` (§10) |
| the declared floor | `driver_protection.py::resolve_driver_low_limit`, `protection_highpass_floor_satisfied`, `declared_protection_highpass_floor_hz` | the tweeter wall — H1 |
| beaming ceiling | `branch_chain.py::beaming_onset_hz(diameter_mm, ka=2.0)` | #1675's ka prior |
| smoothing | `jasper/audio_measurement/analysis.py::smooth_fractional_octave(freqs, magnitude_db, fraction)` | the one smoother; power-domain, not dB-mean (a confusion that has shipped wrong before) |
| decimation | `spatial_combine.py::decimate_curve_to_analysis_grid` | block-average **then** smooth, never the reverse |
| the grade | `flat_spec.py::evaluate_flat_spec`, `spec_convergence_residual` | the referee, once PR-1 gives it a frozen reference |
| on-axis / off-axis views | `flat_spec_views.py::role_split_flatness(report, positions, *, primary_role)`, `log_pooled_residual` | already pools per role without averaging roles together |
| grading a **predicted** curve | `crossover_v2_flow.py::spec_report_for_predicted_sum` (4878) | already does decimate → smooth(3) → `evaluate_flat_spec`; relocated in PR-3 (§10) |
| material-improvement bar | `attempt_grading.py::PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB = 0.5` | the canonical bar, already re-exported not redefined |
| prescription pattern | `blend_prescription.py`, `alignment_prescription.py` | two existing instances of the gate shape |
| evidence document | `evidence_packet.py::build_crossover_evidence_packet` | *"One round's banked evidence, gathered into one document a reader can answer"* |
| staging | `prescription_spool.py` | `stage_prescription` / `take_staged_prescription` / `withdraw_staged_prescription` |
| apply + rollback | `baseline_profile.py::apply_baseline_profile`, `restore_applied_baseline_profile`; `dsp_apply.py::apply_dsp_config`, `_rollback` | §9 |
| the Fc declaration writer | `sound_setup.py::apply_measured_crossover_frequency` (2425) | §9 — exists, fsynced, and its Undo leg is already tested |
| CLI | `jasper/cli/crossover_prescriber.py` (`jasper-crossover-prescriber`) | verbs `packet` / `propose` / `stage`; one more joins them |

### Genuinely new

Four things only: a candidate with more than one axis; a **build-free**
operator (same formula, linearization held fixed); an objective in the grade's
currency; and the search.

---

## 3. Module map

All five land in `jasper/active_speaker/crossover_v2/`, beside the two
prescription gates they mirror.

**Boundary rules, both AST-pinned — do not violate:**

- `jasper/audio_measurement/**` may import neither `jasper.correction` nor
  `jasper.active_speaker` (`tests/test_correction_boundary_ssot.py::
  test_audio_measurement_imports_neither_consumer_package`). The forward model
  therefore lives on the `active_speaker` side and reaches *down* into the
  kernel. This is also why `priors.role_transfers` hands the kernel a
  `freqs -> complex` **callable** rather than a `CrossoverSection`.
- `crossover_v2/*` may import nothing from `jasper.web` and nothing from
  `crossover_v2_flow` (`tests/test_crossover_v2_journey.py::
  test_no_domain_module_imports_the_host_or_the_legacy_flow`). The same test
  asserts the intra-package graph is a **DAG**.

The five modules form a chain, so the DAG holds trivially:

```
candidate_space.py        (leaf: pure data + legality)
        ↓
forward_model.py          (physics; branch_chain + the kernel)
        ↓
objective.py              (grade; flat_spec + flat_spec_views)
        ↓
search.py                 (enumerate × scan → shortlist)
        ↓
crossover_prescription.py (the strict gate)
```

### 3.1 `candidate_space.py` — what a candidate is, and which are legal

Pure data and legality. No physics, no scoring, no I/O.

```python
@dataclass(frozen=True)
class XoverCandidate:
    sections_by_role: Mapping[str, tuple[CrossoverSection, ...]]
    polarity_by_role: Mapping[str, int]        # +1 / -1
    delay_us_by_role: Mapping[str, float]      # signed, analysis frame
    gain_db_by_role: Mapping[str, float]       # <= 0, the emitted trim

@dataclass(frozen=True)
class CandidateBounds:
    fc_band_hz: tuple[float, float] | None     # None => no proposal is legal
    fc_lo_role: str | None                     # who declared each edge
    fc_hi_role: str | None
    declared_floor_hz_by_role: Mapping[str, float]   # the H1 wall
    beaming_ceiling_hz: float | None           # guidance, not a fence
    legal_orders: frozenset[int]               # SUPPORTED_LR_ORDERS
    delay_window_us: tuple[float, float]
    delay_step_us: float                       # what the chain can realize (R2)
    gain_db_range: tuple[float, float]
    undeclared_roles: tuple[str, ...]
```

**Contract:** `search.py` calls `enumerate_discrete()` and
`refusal_for(candidate) -> str | None` — a slug, **never a clamp**.
`crossover_prescription.py` re-checks the same bounds at the gate. One
expression of each wall, two readers.

### 3.2 `forward_model.py` — the physics

```python
@dataclass(frozen=True)
class DriverPlant:
    role: str
    angle_deg: float
    freqs_hz: np.ndarray
    plant: np.ndarray             # M / P — complex, candidate-independent
    band_hz: tuple[float, float]  # driven band; outside it there was no stimulus
    trusted: np.ndarray           # bool: where P was well-conditioned

def driver_plants(analysis, *, protection_by_role) -> tuple[DriverPlant, ...]
def branch_operator(candidate, role, freqs_hz, *, linearization) -> np.ndarray
def predict_sum(candidate, plants, *, linearization) -> Mapping[float, np.ndarray]
```

`branch_operator` is `branch_operators`' formula with the crossover made a
parameter:

```
op_role(f) = p_role · C_role(f; candidate) · K_role(f) · 10^(g_role/20) · e^{−j2πf·τ_role·1e−6}
```

`predict_sum` is `predict_pose_sum_db`'s multiply-add, per angle:

```
S(f,θ) = Σ_role  plant_role(f,θ) · op_role(f)      (zero outside the driven band)
```

**Why `plant = M/P` is separated:** dividing out the emitted protection is
per-driver-per-angle and **candidate-independent**. Doing it once instead of
per candidate is the whole reason the search can be offline. It also puts the
ill-conditioning rule (`|P| < −12 dB` ⇒ refuse) in one place, evaluated once
per capture.

Takes arrays and a candidate; returns arrays. Fixture-testable, no session.

### 3.3 `objective.py` — the score, in the referee's units

Also the new home of the predicted-curve reduction currently living in
`crossover_v2_flow.spec_report_for_predicted_sum` (§10 D3). See §5.

### 3.4 `search.py` — enumerate × scan

See §7.

### 3.5 `crossover_prescription.py` — the strict gate

The third instance of `blend_prescription.py`'s five-function shape. See §8.

---

## 4. The objective — exact definition

```
score(candidate) = w_on  · residual_on(candidate)          # primary view
                 + w_off · residual_off(candidate)         # secondary view
                 + λ_head · headroom_charge_db(candidate)
                 + λ_sim  · sim_to_real_penalty(candidate)
                                                            (lower is better)
```

### How each piece is computed — by calling, never by reimplementing

1. `forward_model.predict_sum(candidate, plants)` → complex `S(f,θ)`.
2. `20·log10|S|` → magnitude dB, per angle.
3. **Reduce exactly as the prediction path already does** —
   `decimate_curve_to_analysis_grid(...)` then
   `smooth_fractional_octave(grid, curve_db, fraction=3)`. This two-step is
   lifted verbatim from `crossover_v2_flow.spec_report_for_predicted_sum`
   (4878), which becomes a caller of the new home rather than a second copy.
4. `flat_spec.evaluate_flat_spec(freqs, smoothed_db, mask, smoothing_fraction=3,
   trusted_floor_hz=<the session's own>, reference_db=<frozen>)` → `FlatSpecReport`.
5. `flat_spec.spec_convergence_residual(report).rms_db` → the scalar (H3).
6. **Views** — `flat_spec_views.role_split_flatness(report, positions, *,
   primary_role=POSITION_ROLE_ONAX)`. It re-grades each position pinned to the
   report's own `trusted_floor_hz` and published `excluded_intervals`, pools
   **within each role only**, and never averages roles together. `residual_on`
   is the primary role's pooled residual; `residual_off` is `offax`'s.

### The three smoothings, because they are three different numbers

A recurring source of confusion, so stated once:

| number | constant | what it smooths |
|---|---|---|
| **1/3 oct** | `spatial_combine.DEFAULT_SPEC_FRACTION = 3` | the pooled **spec** curve — what the grade is computed on |
| **1/6 oct** | `spatial_combine.DEFAULT_DIAG_FRACTION = 6` | the per-position **diagnostic** curve `flat_spec_views` re-grades |
| **1/12 oct** | `positions.curve_grid.fractional_octave` in the banked packet | the **published** position curve grid |

The brief's "1/12-octave" is the third. **The objective uses 1/3**, because
that is what the grade uses. A per-position residual reads systematically
higher than the pooled one for the smoothing difference alone — `flat_spec_views`
carries `PositionFlatness.smoothing_fraction` precisely so the two are never
silently conflated, and the objective must not conflate them either.

### The band, read rather than hardcoded

`GATED_SPEC_LOWER_EDGE_HZ = 250.0`;
`SPEC_BANDS = ((250, 2000, 1.5), (2000, 8000, 2.0), (8000, 16000, 2.5))`;
`REFERENCE_BAND_HZ = (250.0, 8000.0)`; `BEST_EFFORT_ABOVE_HZ = 16000.0`.

**357.14 Hz is not a constant.** It is one capture's `graded_lo_hz`:
`evaluate_flat_spec` raises every band's lower edge to
`max(f_lo, trusted_floor_hz)`, and `trusted_floor_hz` is `2.5/T` for that
capture's own gate window (`gating.f_trusted_floor_hz`). The objective takes
the session's own floor and never hardcodes 357.

### `headroom_charge_db`

`branch_chain.branch_headroom_db(filters, sections=..., trim_db=...)` — already
*"the number a household is told the correction costs, the number the emitter
attenuates by, and the number the runtime contract proves — one function,
three readers."* A candidate that buys flatness with headroom pays here.

### `sim_to_real_penalty`

Zero-weight until rung 4 calibrates it. This is the term that encodes "the
model has not earned its sign yet" (§11, §13).

### Deliberately not a term

The ka/beaming prior. `score_candidate`'s docstring already makes the argument:
it bounds what may be *proposed*, and charging it again in the score *"could
recommend a change on the strength of a prior rather than a measurement."*
Same reasoning, same decision.

### The weights

`w_on`, `w_off`, `λ_head` are a **calibration output, not a guess** — rung 4
fits them against the banked armrun. Until then the shortlist reports the
components separately and the total is advisory.

### The landmine: the delay term is a RESIDUAL

`DriverResponse.complex_tf` and the sum-prediction pair `W`/`T` come from
`_driver_response` and `_aligned_branch_tf`, which perform **identical**
transforms: `argmax(|full_ir|)` → `direct_arrival_window(..., IR_PRE_MS,
IR_POST_MS)` → `apply_arrival_window` → `gate_impulse_response` →
`_complex_tf`. Each branch is referenced to **its own** direct peak, so the
physical inter-driver gap is already out of the measured pair.

Phasing such a pair by the full applied delay counts that gap twice.
`summed_model_residual_delay_us` calls this *"the fix-2 failure mode"* and says
what it does: *"injects a deep comb into the predicted sum on good measurements
and fails VERIFY."*

**Rule:** `forward_model` never accepts an applied delay. It accepts
`residual_delay_us = summed_model_residual_delay_us(anchor_delay_us,
applied_delay_us)`, importing that function rather than restating the
arithmetic. A test asserts the model at the bare anchor is exactly the
zero-residual sum.

---

## 5. Candidate bounds — where every number comes from

| axis | bound | source symbol |
|---|---|---|
| **Fc lower** | HF driver's confirmed minimum recommended crossover; **at** the floor is legal (owner ruling 2026-08-17) | `driver_protection.resolve_driver_low_limit` / `declared_protection_highpass_floor_hz`, off `recommended_highpass_hz` |
| **Fc upper** | lower driver's declared hard ceiling | `roles_bands`, confirmed by `resolve_driver_excitation_ceilings` |
| **Fc both** | intersection of every participating role's declared search band | `resolve_fc_search_band` → `candidate_space` |
| **Fc guidance** | ka beaming ceiling | `branch_chain.beaming_onset_hz(radiating_diameter_mm, ka=2.0)` |
| **order** | `{2, 4, 8}` | `profile.py:74::SUPPORTED_LR_ORDERS`, enforced by `CrossoverRegion` validation (`profile.py:400`) |
| **filter type** | LinkwitzRiley only | `profile.py:73::SUPPORTED_CROSSOVER_TYPES = {"LinkwitzRiley"}` |
| **delay** | declared geometry ± half a period at Fc | `null_walk.geometry_seed_us`, `alignment_walk.driver_delay_walk_spec`, `declared_geometry_plus_minus_half_period`; lobe bound via `program_analysis.half_period_us` |
| **delay step** | what the chain can realize (R2) | `quantized_delay_ms` — the one µs→ms quantizer, shared with `prove_static_delay_binding` |
| **gain** | ≤ 0 dB, seeded from declared sensitivities and in-line pad | `driver_pad.effective_sensitivity_db`, `declared_effective_driver_sensitivities` |

`resolve_fc_search_band` already implements the fail-closed direction: *"the
cost of honouring a stale one is a proposal not made, while the cost of
ignoring it is a driver asked to cross below what its declaration permits."*

**"Filter type" is not a real search axis**, and the plan says so rather than
implying generality it will not have. `SUPPORTED_CROSSOVER_TYPES` is a
one-element set, and the design-draft compiler refuses anything else outright
(`staging._normalise_filter_type` returns `None` → region refused as
`crossover_preview_filter_unsupported`). There is **no representable slot** in
the schema for Butterworth or a 3rd-order slope. So the outer enumeration is
**order × polarity**, not a filter-type catalogue.

---

## 6. The search

### Shape

**Outer: exhaustive enumeration.** order ∈ {2, 4, 8} × tweeter polarity ∈
{+1, −1} = **6 branches** for a two-way. A `for` loop, not a search problem.

**Inner: bounded grid scan** over `(Fc, τ, g_tweeter)` — three parameters per
branch. `g_woofer` is fixed by the level solve; only the relative trim matters.

### Why a grid, and why it is the local idiom rather than a shortcut

**Prior art in the repo first.** `scipy.optimize` is used exactly twice
(`bass_extension/adapters/{sealed,ported}.py`, `least_squares`, fitting
continuous physical box parameters to a measured curve). `minimize`,
`curve_fit`, `brentq` and golden-section appear **nowhere** in `jasper/`.

What *does* exist is one idiom, implemented independently **three times** for
three different variables:

| where | variable | grid | regularization |
|---|---|---|---|
| `program_analysis.solve_ripple_optimal_trim` | trim | `seed ± RIPPLE_TRIM_SEARCH_WINDOW_DB (10.0)` at `RIPPLE_TRIM_SEARCH_STEP_DB (0.1)` | among candidates within `RIPPLE_TRIM_FLAT_MINIMUM_EPSILON_DB (0.25)` of the global minimum, pick the one **closest to the seed** |
| `program_analysis._select_alignment_pair` | polarity × delay | `ALIGNMENT_FLATNESS_STEP_US (10.0)` over `ALIGNMENT_FLATNESS_SPAN_PERIODS (1.0)` period at Fc | same, `ALIGNMENT_FLAT_MINIMUM_EPSILON_DB (0.25)`, seed's polarity preferred |
| `fc_sweep.fc_candidate_set` | Fc | geometric — *"a crossover argument is a per-octave one"* | — |

**Decision: adopt that idiom.** Coarse-to-fine bounded grid, and — this is the
part that matters — **flat-minimum-regularized selection: among all candidates
within ε of the best score, take the one closest to the incumbent.** Not
decoration: the objective's bowl is shallow near its minimum, and bare `argmin`
on a shallow bowl chases measurement noise. The existing constants
(ε = 0.25 dB) are the right starting value because they were chosen against
this same ripple-shaped objective.

Two passes per branch: coarse (Fc geometric, τ at ~⅛ period at Fc, g at 0.5 dB),
then fine at ×4 resolution around the coarse winner.

**Why not an optimizer:**

- **The objective is not smooth.** It contains `flat_spec`'s banded tolerances
  and worst-deviation terms. A local method on a non-smooth, multi-modal
  landscape is exactly where a local optimum passes for an answer — and the
  armrun's four-arm bracket is direct evidence the delay landscape has
  structure a local search would miss.
- **It is affordable.** One evaluation is a few hundred complex multiplies ×
  ~6 angles plus one `evaluate_flat_spec`. A 6-branch coarse+fine sweep is
  seconds on a laptop, under a minute on the Pi. Zero speaker time either way,
  so there is no budget pressure to be clever.
- **It produces a landscape, not a point** — which is what a bracket-then-
  measure method needs, and what the run-log says worked: *"an offline
  landscape chose the arms worth measuring, and measurement then overruled its
  ranking completely."*
- **No convergence criteria, seeds or restarts for a reviewer to reason about.**

If a future axis makes the space genuinely large, the grid can be replaced
behind `search.py`'s signature. A seam, not a promise.

### Output

```python
@dataclass(frozen=True)
class RankedCandidate:
    candidate: XoverCandidate
    score: ObjectiveScore                    # total + every component
    predicted_by_view: Mapping[str, float]   # in the grading view's own units
    delta_vs_incumbent_by_view: Mapping[str, float]

@dataclass(frozen=True)
class Shortlist:
    ranked: tuple[RankedCandidate, ...]      # top N, default 5
    incumbent: RankedCandidate               # always evaluated, always present
    landscape: Mapping[str, Any]             # coarse-grid summary for the reader
    bounds: CandidateBounds                  # what the search could consider
    refusals: tuple[tuple[str, str], ...]    # (what, why) — never a silent drop
```

**The incumbent is always evaluated and ties go to it.** That is `select_fc`'s
§9.8 discipline, kept verbatim: *"the configured candidate is always in the
evaluated set, an alternative must beat it by `margin_db`, and keeping
configured is an honest verdict."* The margin is
`attempt_grading.PREDICTED_SPEC_MATERIAL_IMPROVEMENT_DB = 0.5` dB — the
canonical bar for "a model-graded improvement is material" — rather than
`fc_selector`'s own `MIN_RECOMMENDATION_MARGIN_DB = 1.0`, because this score is
in the spec-residual currency that bar is defined against (H3).

---

## 7. How the shortlist enters the LLM loop

The harness contract is established twice already; the third instance mirrors
it rather than inventing a shape.

**The document.** `evidence_packet.build_crossover_evidence_packet` gains a
`crossover_shortlist` block: ranked candidates, predicted deltas per view,
bounds with each edge's owning role, and refusals. It is a `dict`, it is
covered by `_fingerprint`, and it follows the packet's two rules — absence has
two flavours (`source_absent` vs `field_null`), and anything not computable is
disclosed in `not_evaluated` rather than omitted.

**The response format.** A **new** `kind`, not a widening of the blend
contract. `blend_prescription`'s `prohibited_keys` explicitly forbids
`delay_us` and `role_attenuations_db`; that gate is correctly scoped to EQ
shape and must stay that way. `crossover_prescription.py` gets its own
`prescription_response_format()` with its own bounds (Fc band, legal orders,
polarity, delay window and step, gain range) and its own closed
`PRESCRIPTION_REFUSAL_REASONS` frozenset.

**The gate**, mirroring `read_blend_prescription`:

| function | job |
|---|---|
| `read_prescription_bytes` | reuse the existing one — size cap **before** parse |
| `read_crossover_prescription(raw, *, packet_fingerprint, bounds)` | **the one gate** |
| `crossover_prescription_route(p)` | which writer it lands in (§9) |
| `crossover_prescription_to_candidate_fields(p)` | the apply surface |
| `crossover_prescription_from_mapping(raw)` | durable read-back, shape only, **not** re-bounded |

**What the validator owns and the model never does:** bounds, safety, packet
freshness, statistics, and whether a win is real. Fail-closed, never clamped:
*"A proposal outside a bound is not pulled to the boundary and run… a silently
different shape is worse than none."*

**Staging and rollback** reuse `prescription_spool` unchanged, including the
round-ordinal check.

**The CLI** gains one verb beside `packet` / `propose` / `stage`:

```
jasper-crossover-prescriber shortlist <session_dir> [--state STATE] [--out OUT]
```

It stays inside the tool's boundary: no model client, no API key, no network.
*"Who calls the model is not this tool's business."*

---

## 8. The apply path — item 4, answered with evidence

**Verdict: more exists than the brief assumes. Branch gains, delay and
polarity apply today. Fc has a complete, tested apply-and-rollback path that is
merely unreachable. Only slope/order has no route at all — and the safety check
of H1 is missing from all of them.**

### 8.1 What applies today

`MeasuredCrossoverCandidate` (`measured_crossover_candidate.py:216`) carries
`role_attenuations_db` (**branch gains**) and `alignment:
MeasuredCrossoverAlignment` (`delay_us`, `delay_role`, `polarity` — all-or-none).

The seam is `measured_crossover_candidate.py:662::effective_preset(candidate)`
— *"The preset with the candidate's alignment written into its region fields."*
It `dataclasses.replace`s `delay_target_driver`, `delay_ms` and
`upper_polarity` onto the matching `CrossoverRegion`, then `validate()`s.
Called at three sites (719, 777, 815) on the compile/apply paths.

The transaction underneath is a **whole-file swap and reload**, never a live
per-parameter poke: `apply_dsp_config` (`dsp_apply.py:787`) validates, then
`CamillaController.set_config_file_path` does `config.set_file_path(path)` +
`general.reload()`.

**Rollback, two mechanisms, both tested:**

- **In-transaction:** `dsp_apply.py:756::_rollback` reloads
  `state.prior_config_path` on any load/confirm/persist failure, before
  `apply_dsp_config` returns.
- **Operator Undo:** `baseline_profile.py:3783::restore_applied_baseline_profile`
  reloads *that exact file* — path **and sha256** identity-checked — through the
  same atomic transaction. Reached by `handle_v2_restore`
  (`correction_crossover_v2.py:8517`). Five preconditions guard it
  (`ANCHOR_NOT_APPLIED`, `ANCHOR_NO_PRE_APPLY_PROFILE`,
  `ANCHOR_TOPOLOGY_CHANGED`, `ANCHOR_RUNNING_CONFIG_DIVERGED`,
  `ANCHOR_STASH_NOT_DISPLACED`), all added after two real field incidents
  (2026-08-15, jts3 cycle 4) recorded in `round_anchor.py`.

**The rollback unit is one whole previously-applied YAML, identity-checked by
path + sha256** — never a diff, never a per-parameter revert.

### 8.2 Fc already has a route, and its Undo leg is already tested

This is the finding that changes the plan. `handle_v2_apply`
(`correction_crossover_v2.py:7833-7920`) checks
`fc_selection.verdict == "recommend_alternative"` and then calls
`sound_setup.py:2425::apply_measured_crossover_frequency(expected_revision,
between_roles, configured_hz, selected_hz)` — *"Write a measured Fc onto the
Sound declaration. Durable: every write through this function is fsynced"* —
then regenerates `crossover_preview` and recomposes through
`build_baseline_profile_candidate` / `apply_baseline_profile`.

Undo has a **second leg** for exactly this: `handle_v2_restore` calls
`_restore_sound_declaration(...)`, inverting the write through the same
function, and reports it as a **separate** `sound_declaration` status beside
the DSP-graph status — because *"they are restored by different owners through
different writers and can genuinely disagree."*

Already pinned by tests:
`test_undo_puts_the_declared_crossover_back_through_the_sound_writer` and
`test_undo_declaration_restore_writes_durably`
(`tests/test_correction_crossover_v2_endpoints.py`), plus the full apply→Undo
round trip in `test_apply_stashes_pre_apply_profile_and_restore_reverts_through_real_seams`.

**So the Fc work is re-pointing an existing route, not building one.** The only
reason it is dead is that `fc_selection` is `None` on every shipped session.

**This also settles a design question the brief raised.** I had considered
adding an Fc/order override field to the candidate, applied by
`effective_preset`. **That would be a second way to change Fc, which the owner's
directive forbids.** It would also trip
`baseline_profile.py:2169`'s staleness guard
(`measured_candidate_preset_mismatch`), which requires the candidate's
`source_preset` to equal the preset compiled fresh from the saved declaration.
The declaration route is the one the system already has, and it is the one to
use.

### 8.3 What is genuinely missing

**Slope / order has no route.** `apply_measured_crossover_frequency` carries a
frequency and nothing else. But the *schema* already represents slope: the
design draft's `manual_settings.crossover_candidates[]` carries
`filter_type` and `slope_db_per_octave`
(`design_draft.py`, allowlisted by `driver_safety._MANUAL_CANDIDATE_FIELDS`),
and `staging.py:810-811` compiles them via `_normalise_filter_type` /
`_slope_to_lr_order`.

**The elegant fix is to widen the existing writer, not add a parallel one:**
generalize `apply_measured_crossover_frequency` into
`apply_measured_crossover_geometry(expected_revision, between_roles,
configured, selected)` where `configured`/`selected` carry frequency **and**
slope, and widen `_restore_sound_declaration` symmetrically. Same writer, same
fsync, same Undo leg, existing tests extended rather than duplicated. One
system, one job.

### 8.4 BLOCKING SAFETY PREREQUISITE (H1)

**The Fc lever must not ship before this is closed.**

The declaration route regenerates `crossover_preview`, which *discloses* a
corner below the declared protection floor (`crossover_preview.py:339`) but
does not block. The hard gate (`path_safety._tweeter_protection_floor_verdict`)
runs at **startup / commission-load**, not on a routine apply. So a lowered Fc
can reach the emitted graph and only be refused at the next boot.

Required, and both are needed:

1. **Admissibility.** `candidate_space` refuses any Fc below
   `resolve_driver_low_limit`'s value for the HF role, using
   `protection_highpass_floor_satisfied` — the *shared predicate*, not a
   reimplementation. This is what `fc_sweep.fc_candidate_set` already does via
   `FC_REJECT_BELOW_DECLARED_FLOOR`, and it moves with the rest of that
   function (§9 D1).
2. **Boundary re-check.** The apply path re-enforces the same predicate before
   the write, so a hand-edited declaration or a stale spooled prescription
   cannot slip past. A gate that only runs in the proposer is a gate the
   proposer can be bypassed around.

A test asserts the apply refuses a below-floor corner with a named reason.
This is the one place in the plan where duplication is deliberate: the proposer
and the boundary check the same rule, because they defend against different
failures.

### 8.5 What this does not answer

`#2710` — per-role integer-sample alignment quantization at **±20.833 µs** on a
48 kHz chain — is raised from caveat to blocker for P2 by the design doc,
because it sits at the same order as the entire 20 µs timing budget. The apply
path can carry a delay; whether that delay *means* anything at that resolution
is #2710's question. §13 R2.

---

## 9. Deletions — one obvious system per job

Every mechanism this plan supersedes gets a named disposition. No "kept just in
case."

### D1. `fc_sweep.py` + `fc_selector.py` — **DELETED**, in the PR that supersedes them (PR-4)

**Status at HEAD:** wired but unfed. Real production imports and call sites
(`crossover_v2_flow.py:178, 226, 7852, 7895, 7923`), real tests — but
`crossover_v2_flow.py:1755::STAGE1_INCLUDES_LATERAL = False` (owner pause,
2026-08-18) means no shipped session reaches them. Both docstrings say so:
*"No stage-1 session feeds this today."*

**Why they die rather than get fed** — three structural reasons, not taste:

1. `evaluate_candidate` consumes a **build product** at
   `FC_CORNER_COMPUTE_COST_S = 16.0` s per corner, capped at 5. An offline
   search evaluates thousands with no build. Different operations, not the same
   operation with a different budget.
2. `score_candidate`'s objective — `anchor.penalty_db + 0.5·lateral_excess +
   0.25·headroom` on unsmoothed mean-removed worst-deviation (`band_flatness`)
   — is **not** the grade (H3). Keeping it beside a commensurate objective is
   precisely the second source of truth the owner's values forbid.
3. The `FcCandidateEvaluation` memory contract (*"caps the selector at one
   analysis + 50 MB"*) exists because the sweep runs **inside a live session
   holding analyses**. The offline search holds reduced curves and never sees a
   `ProgramAnalysis`; the contract protects nothing.

**Salvaged, each with its justification comment intact:**

| moves to | what |
|---|---|
| `candidate_space.py` | `fc_candidate_set`, `resolve_fc_search_band`, `FcSearchBand`, `FcCandidateSet`, the four `FC_REJECT_*` slugs (incl. the H1 floor bound) |
| `forward_model.py` | `predict_pose_sum_db`, `branch_operators` |
| `search.py` | `select_fc`'s adjudication discipline — configured is golden, must beat by a margin, ties to configured, an incomplete comparison may never recommend |

**Deleted outright:** `band_flatness`, `score_candidate`, `FcCandidateScore`,
`BandFlatness`, `LATERAL_ROBUSTNESS_WEIGHT`, `HEADROOM_COST_WEIGHT`,
`FcCandidateEvaluation`, `Adjudication`, `sweep_candidates`,
`evaluate_candidate`, `adjudicate`, the budget forecast (`fc_sweep_budget_s`,
`FC_SWEEP_COMPUTE_BUDGET_S`, `fc_sweep_result_wait_s`), and the four `EVENT_*`
slugs.

**Call sites deleted with them:** `CrossoverV2Session._sweep_fc_candidates`,
`._adjudicate_fc`, `._evaluate_fc_candidate` (`crossover_v2_flow.py`), and the
`fc_selection` producer.

**One coupling that must be handled, not discovered later.**
`handle_v2_apply`'s Fc-accept branch (§8.2) is keyed on
`fc_selection.verdict == SELECTION_RECOMMEND` and on the `FcSelection` shape,
and `correction_crossover_v2.py` also imports `fc_sweep_result_wait_s` and the
`SELECTION_*` slugs for rendering. PR-4 therefore **re-points that branch at the
new `Shortlist` + adoption verdict** rather than deleting it — the apply route
is the thing §8.2 says to keep. `FcSelection` and the `SELECTION_*` vocabulary
are replaced by the shortlist's verdict; the branch's *body* (the call to the
declaration writer) is preserved.

**Bonus, and not small.** `crossover_v2_flow.py` is at its cap
(13,055 / 13,055) and `correction_crossover_v2.py` at its (8,774 / 8,774).
Deleting the dormant sweep **lowers both ceilings**, which the ratchet permits
for free (*"Lowering a cap is free; raising one needs an argued diff"*). This
is the only work in flight that gives the two most over-capped files in the
repo room back. PR-4 must state the new numbers.

**The honest cost.** `STAGE1_INCLUDES_LATERAL` is documented as *"paused, not
retired… kept intact for the redesigned lateral statistic the pause is waiting
on."* Deleting these forecloses re-arming by flipping one boolean. Acceptable
**only because this plan is that redesign** — the per-angle commensurate
objective replaces the coarse six-pose lateral gate. Hence: **they die in the
PR that lands the replacement, not before.** If PR-4 does not land, they stay.

### D2. `BaselineVerification.per_driver_measurements_captured` — **NOT deleted here; named follow-up**

The directive calls this a dead placeholder and suggests a same-PR kill. **At
HEAD that premise does not hold, and deleting it would be a behaviour change.**

It is one of **five** members of `BaselineVerification` (`profile.py:745`):
`channel_identity_verified`, `all_paths_protected`,
`per_driver_measurements_captured`, `crossover_nulls_captured`,
`gated_sum_captured` — all defaulting `False`, and `commissioned_ready()` is
their `all(...)`. That predicate gates `status == "commissioned"` validation at
`profile.py:859`.

Production writers, counted: `channel_identity_verified` has 4 references
outside `profile.py`; **the other four have zero.** The dead thing is not one
flag — it is four of five, and the struct.

Removing one member **loosens** `commissioned_ready()`: a profile invalid today
could become valid. That is AGENTS.md rule 11's enumerated-set blind spot
pointing at a *predicate* rather than at prose.

**Disposition (c): a named follow-up PR** resolving the whole struct in one
decision — wire all five, or delete the four unwired plus the gate they feed.
It cannot ride this plan: this plan does not touch `profile.py`, and the right
answer depends on whether commissioning intends to set them.

### D3. `crossover_v2_flow.spec_report_for_predicted_sum` — **RELOCATED** into `objective.py` (PR-3)

Not a deletion of behaviour; a move of ownership. It already does exactly the
reduction the objective needs (decimate → smooth(3) → `evaluate_flat_spec`).
Leaving it in the flow and writing a second copy in `objective.py` would be two
implementations of the grade's front door. It moves; the flow calls it.

Bonus: this removes lines from `crossover_v2_flow.py`, which is at cap.

### D4. The `M*C/P` composition path — **REUSED, not superseded**

`program_analysis._compose_configured_path_ir` composes the measured IR onto
the configured crossover (`S = M*C/P`) so the shipped MEASURE analysis sees
as-crossed branches. That path continues; this plan does not obsolete it.

`forward_model.driver_plants` computes `M/P` in the **transfer-function**
domain instead, because a search applies a different `C` per candidate.

**The duplication risk is closed rather than tolerated.** The shared thing is
the conditioning policy: the `−12 dB` floor on `|P|` and refuse-not-saturate on
the required band. Today that is a bare local, `minimum = 10.0 ** (-12.0/20.0)`.
**PR-2 names it as a module constant and imports it**, so one number has one
owner. The edit is line-neutral (a literal becomes a name), which matters
because `program_analysis.py` is at its cap (7,275 / 7,275).

### D5. `predicted_ripple_db` — **KEPT**

A capture-quality disclosure feeding
`MEASURE_PREDICTED_RIPPLE_DISCLOSURE_DB`, calibrated against a fixed hardware
corpus, and correct at its job. H5 says it was *misread*, not wrong. **What it
is owed is a docstring sentence**, not a deletion: at the definition, that it
must not be used to rank delay candidates, pointing at `predicted_sum` instead.
A misreading that cost a campaign its headline conclusion is worth one sentence
at the site.

### D6. `flat_spec`'s self-referencing default — **NARROWED, not deleted**

After PR-1, `reference_db` is supplied by every comparative caller. The derived
path stays for the one case with no baseline to reference — the entry baseline
itself. Enforced by a test, not by convention: any grading call on a comparison
path must pass an explicit reference.

### Deletion discipline — the checklist every deleting PR runs

Per AGENTS.md rule 11 and its named blind spot:

1. **Baseline first.** `bash scripts/tense-grep.sh --all` **before** the cut,
   banked; re-run after and diff. The default branch-scoped sweep is
   structurally blind to prose in files the diff never opens.
2. **Subject-vocabulary sweep — the part the tool cannot do.** Deleting a
   member of an enumerated set falsifies every description that lists the set
   without naming the deleted thing, in ordinary present tense with no roadmap
   token to match. So grep the deleted thing's **own vocabulary** and read the
   enumerations it appears in. For D1, at minimum: `fc_selection`, `fc_sweep`,
   `fc_selector`, `select_fc`, `FcSelection`, `keep_configured`,
   `recommend_alternative`, `no_alternative_evaluated`, `lateral`,
   `STAGE1_INCLUDES_LATERAL`, `R17`, `§9.8`, `#1894`,
   `MAX_PROPOSED_FC_CANDIDATES` — across `jasper/`, `tests/`, `docs/` and the
   PR template. Note that `docs/active-speaker-tuning-layers-design.md` and
   `docs/HANDOFF-crossover-measurement-v2.md` both describe the sweep as
   something that exists, and `commanded.py`'s docstring **names the
   alternative-Fc sweep by name** as one of two ways the corner can move
   between rounds.
3. **Doc-map scan.** `docs/doc-map.toml` routes `jasper/active_speaker/**` and
   `jasper/audio_measurement/**` to their canonical docs; run
   `scripts/docs-impact.py` and update, or record no-impact per rule 7.
4. **State the cap change.** Lower the `MAX_LINES_BY_PATH` entries in the same
   diff, with the new numbers.

---

## 10. Validation ladder

Five rungs. 1–3 are pure and run in CI. 4–5 decide whether the model gets a
vote.

### Rung 1 — pure math, analytic (CI, fast)

- An LR4 pair at Fc, aligned and in phase, sums to **0 dB ± 0.05 dB at Fc** and
  flat in the passband. The defining LR property; the model has it or is wrong.
- Flipping tweeter polarity gives a **deep null at Fc**; flipping back restores
  the sum exactly.
- The model at **zero residual delay** equals `predicted_branch_sum` with
  `residual_delay_us=0.0`, **bit-for-bit** — same function, same inputs.
- `crossover_response_complex` at orders 2 and 8 matches an independent
  textbook digital construction within **0.01 dB**, 20 Hz–20 kHz.
- **Mutation control:** perturb Fc by 1 %, τ by 10 µs, or a gain by 0.1 dB and
  assert the score moves. A test that passes against a stubbed model guards
  nothing.

### Rung 2 — round-trip against the shipped composition (CI, fixtures)

Feed the model the `(C, P, trims, delay, polarity)` a banked `candidate.json`
records; assert the predicted sum reproduces that round's banked
`predicted_sum` within **0.05 dB** across the graded band.

The strongest available statement that the new module and the shipped path are
the same arithmetic, and it is free — fixtures already exist in
`captures/wired-night-2026-08-19/predictions/` and `curves/`.

**If this fails, the module is wrong.** No hardware needed to know it.

### Rung 3 — objective commensurability (CI, fixtures)

Grade a banked measured curve twice — once through the on-device path, once
through `objective.py`. **Identical numbers, to floating point**, for the same
input and the same frozen reference. Fixtures:
`captures/wired-night-2026-08-19/packets/*.json` carry both the `spec` block and
the position curves.

**This rung is the point of PR-1.** If the two disagree, "a predicted win and a
measured win are the same quantity" is false and the campaign repeats its prior
failure.

### Rung 4 — prediction versus measured, on banked hardware (offline, decisive)

**The rung that gives the model its vote, and it costs no speaker time.** The
armrun bank (H6) is a delay sweep with per-driver on-axis and six-pose off-axis
captures plus a measured summed outcome, per arm.

For each of the 7 arms:

1. Re-derive per-driver complex responses from `*_measure.wav` and the six
   `*_lateral.wav` via `analyze_program_capture`.
2. Build `plants` once; predict the sum for that arm's **actual** applied
   `(τ, polarity, trims, Fc, order)`.
3. Grade the prediction through `objective.py` with the frozen reference.
4. Grade the arm's **measured** `verify` / `cloud_verify` the same way.
5. Correlate predicted rank against measured rank across the 7 arms.

**Pass condition, pre-registered before the run:** Spearman **ρ ≥ +0.6**, and
the model's top-ranked arm within the top two measured. Below that,
`sim_to_real_penalty` stays at its blocking value and the model may **bracket**
(choose which arms are worth measuring) but may not **rank**.

**Also required:** explain the `−550` arm's 4.63 `predicted_ripple_db` outlier
(H5's rider). An unexplained 9 dB swing in a metric that should not have moved
is a live defect, and this is where it surfaces.

This re-runs the experiment the campaign already ran, with the delay-sensitive
instrument instead of the delay-insensitive one. It vindicates the model or
convicts it properly. Today we have neither.

### Rung 5 — pre-registered on-device confirmation (hardware, gated)

Only after rung 4. Coarsest lever first: **polarity → delay → Fc → slopes**.
Each step banks a prediction **in the grading view's own units before the
speaker plays it**, requires **2–3σ** on frozen-reference pooled views and
**≥3 pooled repeats**, and **rolls back on loss**. Exit: **K ≈ 3–5 consecutive
rounds with no statistically real improvement** freezes the crossover and hands
control to P3.

**Rung 5 is blocked on two things outside this plan:**

- **The timing bar.** The design doc's gap 4: *"Nothing downstream should be
  built until the test passes on a de-quantized measurement"* — ≤ 20 µs (3σ)
  relative-phase residual, against a measured chain stability of sd 7.33 µs
  whose 3σ is **22.0 µs**, already above the bar before the estimator
  contributes, and confounded by the ±10.4 µs quantization floor (#2710).
- **The safety gate of H1/§8.4**, for the Fc and slope levers specifically.

**Neither blocks rungs 1–4.** The model, objective, search and offline
calibration are pure math on banked data; none plays a tone. Building them
while #2710 is resolved is the correct parallelism.

---

## 11. Execution sequence

Eight PRs, each independently landable and independently useful. Sizes are
rough new/changed product lines, excluding tests.

| # | what | size | test strategy | depends on |
|---|---|---|---|---|
| **PR-1** | Frozen reference: `evaluate_flat_spec(..., reference_db=None)`; **every comparative caller passes it** (`verification._pooled_residual`, `accountability`'s `_rms`, `round_evidence._post_residual`) | ~60 | rung 3 fixtures; a test pinning that a broadband cut scores **worse** frozen than self-referenced | — |
| **PR-2** | `candidate_space.py` + `forward_model.py`; name the `−12 dB` constant in `program_analysis.py` (line-neutral) | ~340 | rung 1 (analytic + mutation) and rung 2 (round-trip) | — |
| **PR-3** | `objective.py`; **relocate** `spec_report_for_predicted_sum` into it | ~200 | rung 3 — identical numbers, on-device path vs objective | PR-1, PR-2 |
| **PR-4** | `search.py`; **delete `fc_sweep.py` + `fc_selector.py` and their call sites**; re-point `handle_v2_apply`'s Fc branch at the shortlist; lower two caps | ~280 new, ~1,500 deleted | shortlist determinism; incumbent-always-present; flat-minimum tie-break; the full deletion checklist (§9) | PR-3 |
| **PR-5** | **Safety (H1/§8.4)**: floor admissibility in `candidate_space` + boundary re-check on apply | ~70 | a below-floor corner is refused by name, at both the proposer and the boundary | PR-2 |
| **PR-6** | Widen the declaration writer: `apply_measured_crossover_frequency` → `..._geometry` carrying slope; symmetric `_restore_sound_declaration` | ~110 | extend the existing Undo tests; an apply→Undo round trip that moves slope | PR-5 |
| **PR-7** | `crossover_prescription.py` + packet `crossover_shortlist` block + CLI `shortlist` verb | ~430 | every refusal slug tested; hostile-input battery mirroring `test_blend_prescription`; fail-closed never clamped | PR-4, PR-6 |
| **PR-8** | Rung 4 calibration: run the armrun bank, fit `sim_to_real_penalty` and the view weights, bank the result | ~120 (tooling) | the §10 rung-4 pass condition, pre-registered before the run | PR-3 |

**Ordering notes.** PR-5 can land first if convenient — it is a safety
improvement on its own and de-risks the largest hazard in the plan. PR-4 must
not land before PR-3, because the deletion is only justified by the
replacement. PR-6 must not land before PR-5.

**Every PR passes `/adversarial-review` in a separate agent to 0 blockers /
0 should-fixes before merge**, per AGENTS.md's standing method, and the
disposition is posted as a PR comment when the review returns. **PR-5 and PR-6
touch hearing safety and take the escalation** — a perspective-diverse panel
(correctness, hearing-safety, resilience), not a single reviewer.

---

## 12. Risks

The top three, ordered by how likely they are to make the predictions wrong,
each with the rung that catches it. (The safety gap of H1 is not listed here —
it is not a prediction risk, it is a blocking prerequisite, and §8.4 owns it.)

### R1 — The referee is blind to the axis the crossover most affects — **CONFIRMED IN DATA, no longer a risk**

> **Promoted from risk to finding, 2026-08-19.** The banked armrun shows the two
> measured axes anti-correlating with each other: mark RMS vs pooled RMS
> ρ = **−0.66**, and the crossover-region worst-deviation vs the broadband
> residual ρ = **+0.03** (uncorrelated). Three referees pick three different
> winning arms (pooled → `control`, mark → `a550`, the run-log's dip → `a350`),
> and measurement repeatability is 7–10× smaller than the arm-to-arm spread, so
> the disagreement is real rather than noise.
>
> Two things follow. **The vertical-polar dependency is blocking, not deferred**
> — a horizontal-only referee cannot adjudicate a lever that re-aims a lobe. And
> **the objective's currency must be settled before the objective is written**:
> `spec_convergence_residual` cannot see the feature the delay lever moves, so
> PR-3 must either add a region-scoped term or state plainly which defects it is
> not able to rank. Evidence:
> `captures/xover-armrun-2026-08-18/analysis/README-delay-arm-regrade.md`.


**The crossover's primary artifact is vertical lobing, and this rig measures
horizontal angles only.** Design doc gap 2: *"Vertical polar capability —
MISSING… P2 is the consumer that turns that deferral from tidy-later into
blocking."*

So the objective can be perfectly commensurate with the grade and the grade can
still be blind to what a crossover change does. A candidate that improves every
horizontal view can degrade the vertical response the listener is in.

**Why this is R1:** it is not a modelling error calibration can tune away. It
is a *missing dimension in the referee*. A sim-to-real term fitted on
horizontal data will happily certify a model that is wrong vertically.

**What rung 4 catches — partially, and the plan says so.** Rung 4 measures
predicted-vs-measured on the horizontal poses that exist. High ρ means the
model is right *about what was measured*; it says nothing about vertical, and
**a high ρ must not be read as general validation.** Mitigations, in order:

1. Restrict rung 5 to levers whose vertical effect is bounded by symmetry —
   polarity and delay change the lobe's *aim*, which the horizontal poses
   partially witness at the design axis.
2. Report the vertical blindness as a standing `not_evaluated` entry on every
   shortlist, so silence is never read as evidence.
3. Treat vertical capability as the gating dependency for the **slopes/order**
   lever specifically, where lobe *shape* is most at stake.

### R2 — Timing quantization eats the entire delay budget

The delay axis has the most evidence behind it (the armrun's real −6.90 →
−1.07 dB improvement) and the shakiest instrument. `#2710`: per-role
integer-sample quantization is **±20.833 µs** at 48 kHz; the acceptance bar is
**≤ 20 µs (3σ)**. The floor alone exceeds the whole budget.

Compounding it, the automatic aligner has produced **six distinct answers on
one speaker** (+96, +62, −231, −367, −280, +12.2 µs) against a direct-arrival
gap invariant at −405.7 ± 3.3 µs (n = 33) — which is why
`alignment_prescription.py` exists.

**Consequence:** the model can predict a sum for a delay the chain cannot
realize to better than ±21 µs — at 2 kHz, ±15° of phase, the whole margin a
±0.5 dB summation prediction near Fc can absorb.

**What rung 4 catches:** the armrun arms are 100 µs apart, comfortably outside
the floor, so rung 4 grades the *coarse* delay axis honestly. High ρ at 100 µs
spacing with residual scatter of order 20 µs is the quantization showing up as
an irreducible noise floor — and that directly sizes how fine a delay the
search may propose. **`candidate_space.delay_step_us` takes the realizable step
as a declared bound, not an assumption**, so the search cannot emit a delay the
chain cannot hit.

### R3 — The linearization term `K` is held fixed, but reality re-fits it

`branch_operators` includes `K`, the fitted per-driver correction. The shipped
sweep re-fits `K` per candidate — most of the 16 s. The offline search **holds
`K` fixed** at the incumbent's fit, because re-fitting thousands of times is
exactly what makes an offline search impossible.

So the model predicts "this crossover with the *current* linearization" while
the applied round emits "this crossover with a linearization re-fit *through*
it". The two differ most where the crossover moved most — near Fc, the region
being optimized.

**A systematic bias, not noise.** It will tend to *understate* the benefit of an
Fc move, because the re-fit would partly repair what the model shows as damage.
A search ranking by an understated benefit can rank correctly and still
mis-size the improvement — which matters, because rung 5's pre-registered
prediction is a *number*, not an ordering.

**What rung 4 catches:** the armrun arms hold Fc constant and vary delay, so
they isolate the delay axis from the re-fit interaction. A feature for grading
delay; a *gap* for Fc. **Rung 4 as specified validates delay, not Fc.** The Fc
lever needs its own calibration arm before it earns a vote, and until it has
one the shortlist discloses Fc rankings as bracketing guidance, not predictions.

**Cheap mitigation, to take first:** report the score **both ways** — `K` fixed
and `K` = unity. A candidate that wins under both is robust to the term the
model is holding still. One that wins under only one says so on the shortlist.

---

## 13. Explicitly out of scope

- **New filter types.** `SUPPORTED_CROSSOVER_TYPES` is a one-element set and
  the design-draft compiler has no representable slot for anything else.
  Adding one is an emitter + schema + runtime-contract change first.
- **Three-way and beyond.** The model is written role-generically, but bounds,
  enumeration and calibration are specified for a two-way.
- **FIR / linear-phase crossovers.** Different graph, latency contract and
  safety story.
- **Un-pausing the lateral walk.** `STAGE1_INCLUDES_LATERAL` stays `False`;
  re-arming is gated on #2711's re-introduction bar, which the design doc says
  nothing banked today clears. Rung 4 uses the **banked** armrun poses and does
  not need the flag.
- **Predicting `max_db_notch_excluded`** / the attempts-loop currency (H3).
- **Vertical polar capture** (R1) — the dependency is named, not built.
- **Resolving `BaselineVerification`** (§9 D2) — named follow-up.

---

## 14. Open questions for the owner

Four, each changing something concrete:

1. **Frozen reference: a parameter on `evaluate_flat_spec`, or a wrapper?**
   The plan takes the parameter (PR-1), because a wrapper creates two entry
   points to one grade. Confirm.
2. **May PR-4 delete `fc_sweep` / `fc_selector`, given the pause is documented
   as "paused, not retired"?** The plan says yes, conditional on PR-4 landing
   the replacement. If no, the repo carries two scoring systems — which the
   directive forbids — so this needs an explicit call.
3. **Is bracketing-only an acceptable first outcome?** If rung 4 returns
   0 < ρ < 0.6, the model chooses which arms to measure but does not rank them.
   Genuinely useful — it is what the armrun did — but not what "the search
   picks the winner" implies. Better agreed before rung 4 runs than
   re-litigated after.
4. **Does the slope lever wait for vertical polar capability (R1)?** The plan
   recommends yes: slope changes lobe *shape*, which horizontal poses cannot
   witness. That would defer PR-6's slope half and leave Fc/delay/polarity as
   the working levers.

---

*Written against `origin/main` 4b7e76db4. Every symbol cited was read at that
commit; every measured number is quoted from the dated artifact that produced
it.*
