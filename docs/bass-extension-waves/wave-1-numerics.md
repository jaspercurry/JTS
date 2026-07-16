# Wave 1 — numerics core (Codex implementation prompt)

> **Revision 2 (2026-07-16).** The first implementation attempt's
> adversarial review found six contract contradictions in revision 1;
> this revision resolves them. Deltas are listed in the changelog at
> the bottom. If you are continuing the existing
> `codex/bass-ext-wave-1-numerics` branch: re-read this file fully
> and reconcile the implementation to it before re-running the gate.

Read `docs/bass-extension-waves/README.md` (the charter) first; it is
binding. Then read this file completely before writing code.

## Mission

Build the pure numerical core of the Bass Extension feature: the
Linkwitz-Transform alignment math, the three enclosure adapters
(sealed / ported / passive-radiator), the target-family and anchor
derivation, and the harmonic/compression analysis additions to the
measurement kernel. Everything in this wave is deterministic pure
Python (numpy/scipy): **no I/O, no CamillaDSP, no HTTP, no asyncio,
no new dependencies.**

## Required reading (in order)

1. `docs/HANDOFF-bass-extension-plan.md` §1 (behavior + physics),
   §5.1 (schema shapes you produce), §6 (adapters — read carefully),
   §7.4–7.6 (anchors, thresholds, sustain test — read carefully).
2. `jasper/audio_measurement/sweep.py` — `SweepMeta` and the
   synchronized-ESS docstring (read carefully; your harmonic offsets
   derive from its `L`).
3. `jasper/audio_measurement/deconv.py` — you are appending to this
   file; match its style. Note `regularized_deconvolution_full`
   returns the full unwindowed IR.
4. `jasper/audio_measurement/analysis.py` — you are appending here
   too; note `smooth_fractional_octave`, `resample_log`.
5. Skim `jasper/audio_measurement/snr_policy.py` for existing
   band-level helpers before writing your own.
6. Skim `tests/test_correction_sweep_deconv.py` and
   `tests/test_audio_measurement_kernel.py` as test-style exemplars.

## Preflight facts (verify, then record base SHA)

- `jasper.audio_measurement.sweep.SweepMeta` exists with fields
  `f1, f2, L, duration_s, n_samples, sample_rate, amplitude_dbfs`;
  `synchronized_swept_sine` and `synchronized_sweep_metadata` exist.
- `jasper.audio_measurement.deconv.regularized_deconvolution_full`
  and `magnitude_response` exist.
- `jasper.volume_curve.percent_to_db` exists. NOTE: with the default
  `floor_db=None` it lazily reads the wizard sound-settings floor
  from disk (fail-soft to `DEFAULT_VOLUME_FLOOR_DB=-50.0`); it is
  pure only when passed an explicit `floor_db` — which is why
  `digital_anchor_level` below takes one.
- `scipy.optimize.least_squares` is importable in the repo venv.
- `jasper/bass_extension/` does not exist yet.

## File allowlist

Create:
- `jasper/bass_extension/__init__.py`
- `jasper/bass_extension/alignment.py`        (~150 lines)
- `jasper/bass_extension/adapters/__init__.py`
- `jasper/bass_extension/adapters/base.py`    (~150 lines)
- `jasper/bass_extension/adapters/sealed.py`  (~200 lines)
- `jasper/bass_extension/adapters/ported.py`  (~250 lines)
- `jasper/bass_extension/adapters/passive_radiator.py` (~120 lines)
- `jasper/bass_extension/targets.py`          (~200 lines)
- `tests/test_bass_extension_alignment.py`
- `tests/test_bass_extension_adapters.py`
- `tests/test_bass_extension_targets.py`
- `tests/test_audio_measurement_harmonics.py`

Modify (append-only):
- `jasper/audio_measurement/deconv.py`   (harmonic extraction, ~120 lines)
- `jasper/audio_measurement/analysis.py` (compression/THD/tracking, ~150 lines)

Do NOT create `profile.py`, `ladder.py`, `scheduler.py`, or
`runtime.py` — those are Waves 2/4/5. Do not modify any other file.

## Frozen interfaces

### `alignment.py` (pure functions, s-domain analytic responses)

```python
def lt_boost_db(f0_hz: float, fp_hz: float) -> float:
    """40*log10(f0/fp). Positive when extending (fp < f0)."""

def linkwitz_transform_params(f0_hz, q0, fp_hz, qp) -> dict:
    """Exact CamillaDSP Biquad parameter dict:
    {"type": "LinkwitzTransform", "freq_act": f0_hz, "q_act": q0,
     "freq_target": fp_hz, "q_target": qp}. Validates all finite,
    positive, fp <= f0, 0.3 <= q <= 1.2; raises ValueError otherwise."""

def second_order_highpass_db(freqs_hz: np.ndarray, f0_hz, q) -> np.ndarray:
    """|s^2 / (s^2 + (w0/q)s + w0^2)| in dB."""

def lt_response_db(freqs_hz, f0_hz, q0, fp_hz, qp) -> np.ndarray:
    """|H_LT| in dB (zero pair at f0/q0, pole pair at fp/qp)."""

def butterworth_highpass_db(freqs_hz, corner_hz, order: int) -> np.ndarray

def boost_headroom_db(target_chain_db: np.ndarray,
                      natural_chain_db: np.ndarray) -> float:
    """max(target - natural) over the grid, floored at 0.0.
    ALWAYS computed on a dense log grid (>= 480 points, 10-500 Hz) —
    never from the 40*log10 formula alone: when qp > q0 the LT grows
    a peak near fp that EXCEEDS the DC boost, and the grid max must
    capture it (pinned by test)."""

def peaking_response_db(freqs_hz, f0_hz, q, gain_db) -> np.ndarray:
    """RBJ peaking-EQ analog prototype magnitude in dB."""

def low_shelf_response_db(freqs_hz, f0_hz, q, gain_db) -> np.ndarray:
    """RBJ low-shelf analog prototype magnitude in dB."""
```

Analytic (analog-prototype) magnitudes are the spec — including for
the shaping biquads via the two RBJ helpers above, which is how
adapters evaluate `Peaking`/`Lowshelf` members without any digital
math. CamillaDSP owns the digital realization, and the prototype-vs-
bilinear error at <500 Hz / 48 kHz is far below our tolerances. Do
not implement bilinear transforms or digital biquads.

### `adapters/base.py`

```python
class CaptureRole(StrEnum):
    WOOFER_NEARFIELD = "woofer_nearfield"
    PORT_NEARFIELD = "port_nearfield"
    PR_NEARFIELD = "pr_nearfield"

@dataclass(frozen=True)
class MagnitudeCurve:
    freqs_hz: tuple[float, ...]      # ascending
    magnitude_db: tuple[float, ...]  # same length; shape-relative dB

@dataclass(frozen=True)
class CabinetInfo:                   # from driver_safety cabinet block
    enclosure_kind: str
    radiator_count: int | None
    effective_radiating_diameter_mm: float | None   # the powered driver's
    baffle_width_mm: float | None
    passive_radiator_diameter_mm: float | None = None
        # PR only; wizard-entered later (Wave 4). None => PR nearfield
        # used unscaled, recorded as a fit note.

COMMISSION_FLOOR_HZ = 20.0   # no target corner below this, any adapter

@dataclass(frozen=True)
class FitRefusal:
    refusal: str      # a BassExtensionRefusal *value* string (enum lands in Wave 2)
    detail: str

@dataclass(frozen=True)
class TargetSpec:
    target_id: str
    fp_hz: float
    qp: float | None                  # None for ported/PR members (no LT)
    filters: tuple[Mapping[str, Any], ...]   # CamillaDSP Biquad param dicts
    boost_headroom_db: float
    subsonic: Mapping[str, Any] | None
        # {"type": "ButterworthHighpass", "freq": <hz>, "order": <2|4>}
    limiter_threshold_dbfs: float | None = None   # frozen at accept (Wave 4)
```

Plant fits: `SealedPlantFit(f0_hz, q0, fit_rms_db)` (parametric —
carries no curve), `PortedPlantFit(fb_hz, knee_hz, knee_slope_db_oct,
fit_rms_db, natural_curve)`, `PassiveRadiatorPlantFit(= ported fields
+ notch_hz)`. For ported/PR the measured curve IS the model:
`natural_curve: MagnitudeCurve` is the woofer-dominant measured
magnitude resampled onto a fixed 96-point log grid over
[10, 500] Hz (via `analysis.resample_log`), shape-normalized so the
200–400 Hz mean is 0 dB — this is what `predicted_response` adds the
member's filters to, and what Wave 2 persists in the profile's
`natural` payload. Every fit variant additionally carries
`notes: tuple[str, ...] = ()` for non-fatal fit annotations (e.g.
`"pr_nearfield_unscaled"`, `"already_at_floor"`). Each frozen, each
with `adapter_id`, `adapter_version`, `to_dict()`/`from_dict()`
(strict round-trip, reject unknown keys).

```python
class EnclosureAdapter(Protocol):
    adapter_id: str
    adapter_version: int
    required_captures: tuple[CaptureRole, ...]
    def fit_plant(self, captures, cabinet) -> PlantFit | FitRefusal: ...
    def generate_family(self, plant, *, margin: MarginPolicy,
                        n_targets: int = 5) -> tuple[TargetSpec, ...]: ...
    def predicted_response(self, plant, target, freqs_hz) -> np.ndarray: ...

ADAPTERS: dict[str, EnclosureAdapter]   # keys: sealed_v1, ported_v1, passive_radiator_v1
def adapter_for_enclosure(enclosure_kind: str) -> EnclosureAdapter | None
    # sealed->sealed_v1, vented->ported_v1, passive_radiator->passive_radiator_v1, else None
```

`generate_family` invariants (assert in tests): deepest first; the
LAST member is always the natural target (`filters == ()`,
`boost_headroom_db == 0.0`); every member carries the subsonic spec
(sealed included); ordering is deepest-first with primary key
`boost_headroom_db` **non-increasing** (ties are legal — ported/PR
retreat members that only raise the HP corner all have boost 0.0)
and tie-break = effective high-pass/subsonic corner **ascending**
(lower corner = deeper = earlier); `target_id`s unique. Degenerate
case: when no meaningful extension exists (see the sealed floor rule
below), the family is exactly `(natural,)` and callers treat a
single-member family as "no extension available" — never an error.

### `targets.py`

```python
@dataclass(frozen=True)
class MarginPolicy:
    name: str                     # conservative | normal | aggressive
    boost_cap_db: float           # 6.0 / 9.0 / 12.0
    rung_step_db: float           # 3.0 / 3.0 / 3.0
    digital_margin_db: float      # 4.0 / 3.0 / 2.0
    compression_fail_db: float    # 1.5 / 2.0 / 3.0
    thd_fail_ratio: float         # 0.03 / 0.10 / 0.20
    sustain_duration_s: float     # 90.0 / 60.0 / 30.0
    sustain_sag_fail_db: float    # 1.5 (all tiers)
    sustain_fc_shift_fail_pct: float  # 5.0 (all tiers)
    subsonic_corner_ratio: float  # ×fb for ported/PR: 0.75 / 0.70 / 0.65
    subsonic_order: int           # 4 / 4 / 2

MARGINS: dict[str, MarginPolicy]  # exactly the three above

def digital_anchor_level(boost_headroom_db: float,
                         digital_margin_db: float,
                         floor_db: float = -50.0) -> int:
    """Highest listening_level (0-100) whose
    percent_to_db(level, floor_db=floor_db) + boost <= -margin.
    Takes an EXPLICIT floor_db (default = volume_curve's
    DEFAULT_VOLUME_FLOOR_DB) so this stays deterministic/pure;
    commissioning (Wave 4) passes the household's configured floor.
    Returns 100 when unconstrained, 0 when nothing fits."""

@dataclass(frozen=True)
class AnchorPoint:
    target_id: str
    max_listening_level: int
    evidence: str                 # measured | spot_verified | derived

def interpolate_anchors(targets: tuple[TargetSpec, ...],
                        measured: tuple[AnchorPoint, ...],
                        margin: MarginPolicy) -> tuple[AnchorPoint, ...]:
    """One AnchorPoint per non-natural target. Measured points pass
    through unchanged. Between two measured neighbors, a target's
    level is linear in its boost delta (equal-excursion: each dB of
    boost given up buys one dB ≈ its listening-level equivalent via
    volume_curve). Every result is clamped by digital_anchor_level.
    Raises ValueError if fewer than one measured point or if a
    measured point violates monotonicity (deeper target must have a
    lower or equal anchor than every shallower one)."""
```

### `adapters/sealed.py` (`sealed_v1`)

- `fit_plant`: needs `WOOFER_NEARFIELD`. Normalize the curve to its
  200–400 Hz mean (shape-relative). First pass: coarse f0 estimate =
  frequency where the smoothed curve is −6 dB re passband. Fit window
  `[0.3·f0_est, 3·f0_est]` intersected with the curve's support;
  `scipy.optimize.least_squares` over `(f0, q, level_offset)` against
  `second_order_highpass_db`, bounds f0∈[15, 200], q∈[0.3, 1.5].
  Iterate the window once with the fitted f0. Refuse
  (`"bass_extension_fit_quality_insufficient"`) when in-window RMS
  residual > 1.5 dB.
- **Order sanity check**: also fit `a·second_order + (1−a) ramp`? No —
  keep it simple and specified: additionally fit a 3rd-order
  Butterworth-highpass magnitude (same free f0/level, fixed shape);
  if its RMS beats the 2nd-order fit by > 0.5 dB, refuse with the
  leakage hint in `detail`.
- `generate_family`: deepest
  `fp = max(COMMISSION_FLOOR_HZ, f0 / 10**(margin.boost_cap_db/40))`,
  `qp = 0.65`; intermediates spaced ~3 dB of boost apart (equal
  ratios in fp); natural member last. Subsonic:
  `ButterworthHighpass, freq = 0.5·fp_deepest (floor 15 Hz), order 2`.
  **Floor rule:** when `fp_deepest >= 0.99·f0` (the speaker's natural
  corner is already at/below the commission floor, or the boost cap
  buys no meaningful extension), return the degenerate `(natural,)`
  family with note `"already_at_floor"` — `linkwitz_transform_params`
  keeps its `fp <= f0` validation and is simply never called with an
  inverted pair.
- `predicted_response`: natural 2nd-order HP × LT × subsonic, in dB.

### `adapters/ported.py` (`ported_v1`)

- `fit_plant`: needs `WOOFER_NEARFIELD`; accepts optional
  `PORT_NEARFIELD`. `fb` = the sharp local minimum of the smoothed
  woofer curve in [15, 120] Hz (parabolic refinement over the 3 bins
  around the minimum). Refuse `"bass_extension_tuning_not_located"`
  when no local minimum at least 4 dB below its shoulders exists.
  Knee: on the woofer-dominant region above fb, `knee_hz` = the
  −3 dB point re passband mean; `knee_slope_db_oct` = local slope one
  octave below the knee. When the port curve is supplied, verify its
  maximum lies within ±20 % of `fb` (else refuse tuning-not-located
  with detail "woofer minimum and port maximum disagree").
- `generate_family`: **no LinkwitzTransform anywhere.** Deepest
  member: subsonic HP at `margin.subsonic_corner_ratio × fb`
  (order per margin) + at most TWO shaping biquads (`Lowshelf` and/or
  `Peaking` CamillaDSP param dicts) fitted with bounded
  `least_squares` to flatten the region `[1.2·fb, 2·knee_hz]` toward
  the passband mean; shelf/peaking gains bounded to [0, +6] dB,
  Q ∈ [0.4, 1.5]. This is a constrained solve of ≤5 scalar
  parameters — NOT a generic EQ optimizer; do not add stages,
  iterations, or search beyond one `least_squares` call. Retreat
  members: raise the effective HP corner in equal log steps toward
  `knee_hz` and drop the shaping biquads (deepest members keep them,
  shallower members halve then drop the shelf gain). Natural member
  last: no shaping filters, subsonic HP at the margin corner
  (the subsonic never retreats away — it is protective).
- `predicted_response`: the fit's stored `natural_curve`
  (interpolated onto the caller's grid) + the member's filters
  evaluated via the alignment.py response helpers, in dB. (For
  ported we predict *relative to the measured natural curve*, not a
  parametric box model — which is exactly why the curve lives on the
  fit.)

### `adapters/passive_radiator.py` (`passive_radiator_v1`)

Subclass-by-composition of ported (share helpers via module-level
functions, not inheritance gymnastics): requires `PR_NEARFIELD`.
Additional landmark: `notch_hz` = the frequency below `0.9·fb` where
the scaled PR magnitude and the woofer magnitude are closest
(magnitude crossover — the cancellation-region proxy). Scale factor =
`passive_radiator_diameter_mm / effective_radiating_diameter_mm` when
the cabinet provides both; else unscaled with fit note
`"pr_nearfield_unscaled"`. Refuse
`"bass_extension_pr_notch_not_located"` when no approach within 3 dB
exists in [10, 0.9·fb]. Constraints on the PR family: subsonic corner
≥ `1.1 × notch_hz` (overrides the margin ratio when higher); shaping
is **Peaking biquads only — no Lowshelf** (a positive low shelf
boosts everything below its corner, straight through the notch;
Peaking skirts decay), Q ∈ [0.7, 1.5], gain [0, +6] dB; and the
binding constraint is **composite**, not per-filter: each member's
`predicted_response − natural_curve` must be ≤ +0.5 dB at every grid
point at/below `notch_hz` (evaluate with the alignment.py helpers;
assert at generation, refuse the member otherwise). Tolerance on the
notch estimator is provisional (`algorithm_version` rides it); the
synthetic test below pins today's behavior.

### `deconv.py` additions (append-only)

```python
def harmonic_time_advance_s(meta: SweepMeta, order: int) -> float:
    """L * ln(order); the harmonic image leads the linear IR by this."""

def extract_harmonic_ir(full_ir: np.ndarray, sample_rate: int,
                        direct_peak_idx: int, meta: SweepMeta,
                        order: int) -> np.ndarray:
    """Window the order-N harmonic image out of the unwindowed IR.
    Center = direct_peak_idx - round(advance*fs); half-width = 40% of
    the gap to the nearest neighboring image (orders N±1), Hann
    tapered. Raises ValueError if the window would cross t=0 or the
    linear IR window."""

def harmonic_magnitude_response(harmonic_ir, sample_rate, order,
                                n_fft=None) -> tuple[np.ndarray, np.ndarray]:
    """(excitation_freqs_hz, magnitude_db): FFT the image, then map
    output frequency f -> excitation frequency f/order (Farina
    convention), returning the curve on the excitation axis."""
```

### `analysis.py` additions (append-only)

```python
THIRD_OCTAVE_BASS_BANDS_HZ: tuple[tuple[float, float], ...]
    # 1/3-octave band edges covering 20–200 Hz (center-freq series
    # 20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200)

def band_levels_from_magnitude(freqs, magnitude_db, bands) -> tuple[float, ...]
    # power-mean per band; reuse snr_policy helpers if they fit,
    # otherwise implement locally — do NOT modify snr_policy.

def thd_curve(fund_freqs, fund_db, harmonics: Mapping[int, tuple[np.ndarray, np.ndarray]],
              band=(20.0, 200.0)) -> tuple[np.ndarray, np.ndarray]:
    """(freqs, thd_ratio) where thd = sqrt(sum_n mag_n^2)/mag_1,
    all interpolated onto the fundamental's excitation-frequency grid
    within band."""

def compression_curve(rungs: Sequence[tuple[float, tuple[float, ...]]],
                      ) -> tuple[tuple[float, ...], ...]:
    """rungs = [(commanded_level_db, per-band levels dB), ...] in
    ascending commanded order. Returns per-rung per-band compression
    in dB vs the linear extrapolation from the FIRST rung
    (measured - (first + commanded_delta)); first rung is all zeros."""

def tracking_error_db(freqs, measured_db, predicted_db,
                      band) -> tuple[float, float]:
    """(rms, max_abs) of (measured - predicted - band_mean_offset)
    within band — level-offset-invariant by construction."""
```

## Tests (pinned coverage; deterministic, no I/O)

`test_bass_extension_alignment.py`:
- `lt_boost_db(61,31) ≈ 11.75` (abs 0.02); `(40,20) ≈ 12.04`;
  `(50,35) ≈ 6.20`; boost of identity transform is 0.
- `lt_response_db` at DC-ward grid point equals `lt_boost_db` within
  0.1 dB for qp ≤ 0.71; cascade `second_order_highpass(f0,q0) + LT`
  equals `second_order_highpass(fp,qp)` within 0.05 dB everywhere.
- `linkwitz_transform_params` validation rejections (fp>f0, NaN,
  q out of range).

`test_bass_extension_adapters.py`:
- Sealed round-trip: synthesize `second_order_highpass_db` curves
  (f0 ∈ {45, 61, 80}, q ∈ {0.55, 0.707, 0.9}) + passband; clean fit
  recovers f0 within ±1 %, q within ±0.02. With 0.5 dB RMS gaussian
  noise (seeded): ±5 % / ±0.1.
- Sealed order-sanity: a synthetic 3rd-order rolloff refuses with the
  leakage detail; a clean 2nd-order does not.
- Family invariants (all adapters): natural-last, deepest-first,
  non-increasing boost with corner tie-break, unique target_ids,
  subsonic always present, ported/PR contain no `LinkwitzTransform`
  dict, PR shaping is Peaking-only with composite boost ≤ +0.5 dB
  at/below notch, PR subsonic ≥ 1.1×notch.
- Sealed floor rule: f0 = 18 Hz (fit bounds permit it) → degenerate
  `(natural,)` family with note `"already_at_floor"`; f0 = 24 Hz with
  conservative cap → whatever extension the cap allows, all corners ≥
  `COMMISSION_FLOOR_HZ`.
- Low-Q headroom pin: for f0 = 60, q0 = 0.5 → fp = 40, qp = 0.65, the
  grid-computed `boost_headroom_db` EXCEEDS `lt_boost_db(60, 40)`
  (the qp > q0 peak near fp must be captured; a formula-only
  implementation fails this test).
- Ported/PR `predicted_response` round-trip: natural member's
  predicted response equals the stored `natural_curve` (interpolated)
  within 0.1 dB.
- Ported fb: synthetic 4th-order vented magnitude (construct from two
  resonators or a published-shape polynomial — your choice, keep it
  in a test helper) with known fb → located within ±3 %; port-curve
  disagreement refuses.
- PR notch: build a synthetic complex-summed woofer+PR pair with a
  known cancellation notch; the magnitude-crossover estimate lands
  within ±20 % of truth; absent crossover refuses.
- `adapter_for_enclosure` mapping incl. unknown → None.

`test_bass_extension_targets.py`:
- `digital_anchor_level`: boost 11.8 dB, margin 3 dB → the returned
  level L satisfies `percent_to_db(L) <= -14.8 < percent_to_db(L+1)`;
  boost 0 → 100.
- `interpolate_anchors`: single measured point → linear-in-boost
  derivation, all clamped; measured monotonicity violation raises;
  three measured points pass through unchanged with interpolated
  members between.
- `MARGINS` values pinned exactly as specified above (a
  change-detector test — thresholds ride `algorithm_version`).

`test_audio_measurement_harmonics.py`:
- Build a synchronized sweep (`synchronized_swept_sine`, 8 s, 10 Hz –
  500 Hz, fs 48 k); pass it through `y = x + 0.03·x²` (and a variant
  with `0.01·x³`); deconvolve with `regularized_deconvolution_full`;
  assert the H2 (H3) image peak sits at `direct_peak −
  round(fs·L·ln(2 or 3))` within ±2 samples; recovered H2 ratio via
  `thd_curve` is within ±1 dB of the injected level across 30–150 Hz.
- `extract_harmonic_ir` window-collision ValueError case.
- `compression_curve` on synthetic soft-clipped rung levels;
  `tracking_error_db` offset-invariance.

## Anti-overengineering fences (wave-specific)

Do NOT build: a filter-design framework; digital/bilinear biquad
realizations (analytic s-domain prototype evaluation via the
alignment.py helpers is the required approach — the fence bans
implementing digital filters, not evaluating prototype responses);
caching/memoization; plotting; CLI entry points; logging;
dataclass base classes or a "Curve" utility library; adapter
auto-discovery (the `ADAPTERS` dict is literal); phase handling
(magnitude-only is the v1 contract); any file I/O. If scipy's
`least_squares` plus one residual function can't express a fit you're
attempting, the design is wrong — stop and report rather than
hand-rolling an optimizer.

## Acceptance commands

```
.venv/bin/pytest tests/test_bass_extension_alignment.py \
  tests/test_bass_extension_adapters.py \
  tests/test_bass_extension_targets.py \
  tests/test_audio_measurement_harmonics.py -q
scripts/test-fast
.venv/bin/python -c "from jasper.bass_extension.adapters.base import ADAPTERS; print(sorted(ADAPTERS))"
```

Also include in the PR description the family/anchor table your code
produces for the worked example (sealed f0=61 Hz, q0=0.72, normal
margin) — it should resemble plan §1.1's shape.

## Changelog

- **Rev 2 (2026-07-16)** — resolves the six contract contradictions
  found by the first implementation's adversarial review:
  (1) `PortedPlantFit`/`PassiveRadiatorPlantFit` now carry
  `natural_curve` (96-point log grid, 200–400 Hz-mean-normalized) —
  the empirical model `predicted_response` needs; (2) `CabinetInfo`
  gains optional `passive_radiator_diameter_mm`, and all fits gain
  `notes`; (3) `COMMISSION_FLOOR_HZ` + the sealed floor rule define
  the low-f0 degenerate `(natural,)` family; (4) PR shaping is
  Peaking-only with a composite ≤ +0.5 dB at/below-notch constraint,
  and the family ordering invariant is non-increasing boost with a
  corner tie-break (strict ordering was unsatisfiable for
  corner-only retreat members); (5) no allowlist change — spec files
  are never the implementer's to edit; stop-and-report was correct;
  (6) `peaking_response_db`/`low_shelf_response_db` added to
  alignment.py and the digital-math fence reworded to make prototype
  evaluation explicitly the required approach. Plus new pinned tests:
  low-Q (qp > q0) headroom-peak capture, floor-rule degeneracy,
  ported/PR natural-member round-trip.
