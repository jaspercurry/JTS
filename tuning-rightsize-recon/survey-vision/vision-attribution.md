# vision-attribution — room-versus-speaker attribution at HEAD f4ff89731

Read-only survey. Every claim cited `path:line` at `f4ff89731979abfb0f27a4afb3f220d22d21e78c`.

---

## 0. Headline

There are **two unconnected attribution systems** in the tree, and the one
named `attribution` is the one the LLM cannot reach.

| System | Verdict vocabulary | Reachable from a CLI? | Band it can answer in |
|---|---|---|---|
| `jasper/attribution/` (mechanisms M2/M5/M7, findings, promotion) | `mechanism` + `fix_class` + `confidence` | **No** — no console script imports it | **≥ 4 kHz only** (see §4.1) |
| `crossover_v2/feature_classifier` + `gate_sweep` + `close_reference` | `classification` / `window_verdict` / `verdict` | Yes — `jasper-round-views` | ~450 Hz–16 kHz / 200 Hz–20 kHz / per spec band |

The methodology's §6a ladder is built entirely out of the **second** system.
The first system is a persisted evidence artifact for the web wizard's
household copy and never enters the LLM's loop.

---

## 1. `jasper/attribution/` — what it computes, who calls it, what it writes

### 1.1 What each module is

| File | What it is | Computes anything? |
|---|---|---|
| `closed_sets.py:44` `FIX_CLASSES` | `delay, polarity, eq, refit, carve, physical, document_as_physics, measure_differently` | no — vocabulary |
| `closed_sets.py:66` `CONFIDENCE_TIERS` | `confident, likely, unsure` — words, never scores | no |
| `closed_sets.py:86` `PROBES` | `P1..P7` (reverse-null, position-variance, two-level, rotation, design-axis, Farina-harmonic, repeat-variance) — **ids only, nothing here executes a probe** (`closed_sets.py:76-80`) | no |
| `closed_sets.py:110` `EVIDENCE_TIERS` | `adjudicated, corroborating, model_derived, refuted` — tier of the *seed observation*, not of a finding | no |
| `mechanisms.py:37-39` | The whole registry: **three** ids — `M2` source-fixed HF reflection, `M5` boundary/SBIR, `M7` inter-driver level-frame | no — frozen data |
| `findings.py:280` `Finding` | `{mechanism, band_hz, evidence, confidence, fix_class, household_copy, probes_run, probes_recommended, cites}` + validation | no — "It computes nothing" (`findings.py:11`) |
| `findings.py:97-115` | `_HARDWARE_NOUNS` — refuses household copy naming horn/tweeter/…/desk | validator |
| `promotion.py:110` `promote_carve_outs` | maps a **banked carve-out record** to a finding | routing only: "no signal is analysed and no threshold applied here" (`promotion.py:9-10`) |
| `promotion.py:213` `promote_level_frame_disagreement` | maps one banked level-frame record to an M7 finding | routing only |
| `position_evidence.py:255` `position_evidence_block` | per-position magnitude curves + gate scalars, "Serialization, not analysis … no verdict is reached" (`position_evidence.py:9-11`) | no |
| `storage.py:41/86/122` | publish path, citation minting, hash-verified read-back | no |
| `session_identity.py` | cross-store id + `capture_session_id` alias | no |

**There is no detector.** `__init__.py:8` states it: *"There is no resident
daemon and no detector yet."* The plan's WO-4 (per-mechanism detectors, one
module per mechanism) is unbuilt — `docs/historical/attribution-stage-plan.md:888-895`.
Nothing in the package carries the word `room` or `speaker` as a field: the
room/speaker split is *implied by the mechanism id* (M5 = room, M2/M7 =
speaker) and is nowhere machine-stated.

### 1.2 Who calls it in production — **not one of the ten tuning binaries**

`git grep -n "jasper.attribution" -- jasper scripts` returns four real call
sites (the rest are docstring cross-references):

| Caller | What it uses | Surface |
|---|---|---|
| `jasper/web/correction_crossover_v2.py:3162` | `promote_carve_outs` | **web wizard** — `_publish_findings`, called from `publish_cloud` (`:3457`) |
| `jasper/web/correction_crossover_v2.py:3358-3361` | `promote_level_frame_disagreement` | **web wizard** — `bind_findings_publisher` |
| `jasper/active_speaker/crossover_v2/record_store.py:21,27,137` | `FINDING_SET_SCHEMA`, `findings_relative_path` | store routing |
| `jasper/active_speaker/crossover_v2/spatial.py:2241,2358` | `position_evidence_block` | serialization into the cloud artifact |

The generated tool menu (`docs/tuning-operator-runbook.md:427-438`) lists **ten
tool rows**; `git grep -n attribution -- jasper/cli scripts` hits nothing but
unrelated prose (`jasper/cli/audio_config.py:438`, `jasper/cli/system_soak.py:458`).
**Verdict: TRUE — attribution is reachable only from the web wizard, and read
back only through the evidence packet.**

### 1.3 What it writes (post-PR #4024)

Route is keyed on the schema tag, not a kind string —
`record_store.py:136-141`:

```
FINDING_SET_SCHEMA: _Route(
    lambda capture, r: findings_relative_path(capture, _required(r, "phase")),
    enveloped=False, routing_keys=("phase",))
```

* publish path: `storage.py:53` → `crossover_v2/{capture_session_id}/findings_{phase}.json`
* full bundle-relative path: `storage.py:66` → `evidence/v1/artifacts/crossover_v2/{capture}/findings_{phase}.json`
  (`EVIDENCE_ROOT = "evidence/v1"`, `commissioning_evidence_store.py:69`)
* the caller injects `phase` as a routing key and the route takes it back off
  (`correction_crossover_v2.py:2816-2828`); file content is exactly
  `FindingSet.to_dict()` (`findings.py:507-518`)

Three phase spellings are actually produced (`journey.py:26,39`):

| File | Producer | Read by? |
|---|---|---|
| `findings_cloud_measure.json` | `_publish_findings` at cloud-measure close | **nothing** |
| `findings_cloud_verify.json` | `_publish_findings` at cloud-verify close | `evidence_packet.py:2725` — the only reader in the tree |
| `findings_measure.json` | `bind_findings_publisher` (M7 level frame, `correction_crossover_v2.py:3325-3335`) | **nothing** |

---

## 2. The classifier path — `feature_classification.json`

### 2.1 Per-feature fields and their closed sets

`classify_round` (`feature_classifier.py:2166`) writes an artifact with top-level
`schema / generated_by / thresholds / measurement / controls_ok /
controls_disclosure / controls / timing_scatter / pose_bank / rows`
(`feature_classifier.py:2371-2434`). One `rows[]` entry per feature; the column
register is `LAB_ROW_FIELDS` (`feature_classification.py:111-143`, 30 columns).

The seven-key gate view is `FeatureVerdict` (`feature_classification.py:226-277`).

| Field | Vocabulary (closed) | Says what |
|---|---|---|
| `classification` | `interference-barred` / `room` / `defect-cuttable (min-phase peak)` / `defect-boostable (min-phase dip)` / `ambiguous` — `feature_classification.py:74-85`, `CLASSIFICATIONS` frozenset | **the room/speaker verdict.** The parentheticals are part of the value (`tuning-methodology.md:505`) |
| `egd_verdict` | `MIN-PHASE` / `NON-MIN-PHASE` / `ambiguous` — `feature_classification.py:56-60` | minimum-phase test (excess group delay vs a matched non-min-phase scale) |
| `egd_verdict_raw` | same set | what the numbers said **before** the known-answer controls gated them (`feature_classification.py:52-55`) |
| `gate_verdict` | `STABLE` / `MOVED` / `ambiguous` — `feature_classification.py:66-68` | window-invariance; `MOVED` = "a property of the window" |
| `confidence` | `high` / `medium` / `low` — shared `TrustLevel` (`feature_classification.py:243-249`) | not a score |
| `measured_q`, `depth_db`, `pooled_db`, `is_dip` | numbers | the feature itself |
| `excursion_us`, `excursion_sd_us`, `nbhd_sd_us`, `p2p_us`, `nmp_scale_us`, `frac_of_nmp`, `z_local`, `lead_sensitivity_us`, `clean` | numbers | the EGD working |
| `resolved_gates`, `excess_loss_vs_null`, `gate_slack`, `gate_notes`, `gate_rungs`, `gate_sensitivity` | numbers + `gate_sweep`'s own blocks | the window-ladder working, **in gate_sweep's frame, not this row's `depth_db` frame** (`tuning-operator-runbook.md:1022-1026`) |
| `pose_persistence` | per-pose `{pose_id, position_deg, vertical_deg, role, resolved, pooled_db, centre_hz}` (`feature_classifier.py:1707-1738`) | **position spread across angle-capture poses — raw, no verdict** |
| `controls_ok`, `timing_corroborated`, `decay`, `fdw_rungs`, `cycles_in_primary_gate` | | corroboration and disclosure |

Uncertainty is labelled by kind, not pooled: `LAB_ROW_UNCERTAINTY`
(`feature_classification.py:169-202`) tags `excursion_sd_us`/`nbhd_sd_us`
`random` and `lead_sensitivity_us` `systematic`; `LAB_ROW_NOT_AN_UNCERTAINTY`
(`feature_classification.py:208-224`) names `p2p_us` and `gate_slack` as *not*
error bars. `UNCERTAINTY_UNSEPARATED` (`:162`) is deliberately not a third kind.

### 2.2 Composition rule (machine-stated, strict precedence)

`feature_classifier.py:2094-2109`:

```
NON-MIN-PHASE          -> interference-barred   (whatever the gate said)
elif gate MOVED        -> room                  (whatever the phase said)
elif MIN-PHASE+STABLE  -> defect-boostable (dip) / defect-cuttable (peak)
else                   -> ambiguous
```

`controls_ok=False` demotes `egd_verdict` to `ambiguous` while keeping the real
`gate_verdict` (`feature_classifier.py:2101-2103`) and stamps
`controls_disclosure` at the top of the artifact.

### 2.3 Two hard band limits nobody states in one place

* `ADMISSIBLE_PHASES = {verify, cloud_verify}` (`feature_classifier.py:290`) —
  **post-apply only.** A `measure`-shaped round refuses with
  `classification_round_shape_inadmissible` and the remedy "point at a
  verify-shaped one" (`feature_classifier.py:143-146, 259`).
* `classifiable_band_hz` (`feature_classifier.py:1175-1185`) =
  `[trusted_lo · 2^(1/3), trusted_hi · 2^(-1/3)]`. With the shipped
  `DEFAULT_GATE_MS = SEARCH_T_MAX_MS = 7.0 ms` (`feature_classifier.py:303`,
  `gating.py:126`), `f_trusted = 2.5/0.007 = 357.1 Hz` (`gating.py:197-209`,
  `TRUSTED_FLOOR_MULTIPLIER = 2.5` at `:143`), so **the lower edge is ≈ 450 Hz**.
  Both `--at` and auto-detection are filtered by it
  (`feature_classifier.py:2242-2247`).

So the classifier answers ~450 Hz–~12.7 kHz on a verify round, and nothing else.

---

## 3. The gate path, the close-reference path, and the §6a ladder

### 3.1 `gate_sweep` — the room/speaker engine

`gate_sweep.py:5` states the job: *"Room or speaker: the same feature read
through a ladder of gate windows."* Verdict vocabulary
(`gate_sweep.py:163-171`): `stable` / `moved` / `unresolved`; routes
`sigma_growth` / `depth_delta` / `centre_shift`. Any one route alone is enough
(`gate_sweep.py:580-604`):

| Route | Threshold | Where |
|---|---|---|
| `sigma_growth` | across-pose σ at longest valid rung ÷ shortest ≥ **2.0**, and only read above **0.2 dB** σ at the long rung | `gate_sweep.py:127, 142` |
| `depth_delta` | \|null-model-**corrected** delta\| > **0.5 dB** | `gate_sweep.py:154` |
| `centre_shift` | \|log2 centre ratio\| > **1/24 octave** | `gate_sweep.py:159` |

Grid is **200–20000 Hz at 1/48 oct** (`gate_sweep.py:100-102`), deliberately
lower than the classifier's 300 Hz floor "the features under investigation sit
at 358 and 441.6 Hz" (`gate_sweep.py:96-99`). Ladder `3 4 5 7 9 12 20 ms`
(`gate_sweep.py:70`). Resolution bars: `invalid < 2.5 cycles`, `grey < 5`
(`gate_sweep.py:76-78`). CLI: `jasper-round-views gate-sweep` writes
`gate_sweep.json`, "Evidence for an attribution argument, never an EQ
instruction" (`jasper/cli/round_views/sweeps.py:21-22`). Sibling `spec-sweep`
stamps the same verdict onto each spec band's worst bin
(`sweeps.py:8-14`, → `spec_gate_sensitivity.json`).

### 3.2 `close_reference` — how much of a far read was the room

Verdicts `agreement` / `room_dominated` / `unresolved`
(`close_reference.py:127-129`), four named unresolved reasons (`:132-135`),
decided at `close_reference.py:229-257`: `rms_delta_db ≤ tolerance` **and**
`residual_rel_direct_db < −12 dB` (`ROOM_RESIDUAL_FLOOR_DB`, `:116`) →
`agreement`; both false → `room_dominated`. It publishes a verdict **by design**
and the code says so, contrasting itself with `gate_sweep`
(`close_reference.py:225-228`). Granularity: **one row per `SPEC_BANDS` entry
per window** (`close_reference.py:262-315`), and `SPEC_BANDS[0]` is
`(GATED_SPEC_LOWER_EDGE_HZ, 2000.0, 1.5)` (`flat_spec.py:36-40`) — so the whole
low-mid decade gets **one** verdict. `--distance` sizes the capture from driver
diameter + fc (`jasper/cli/round_views/close_reference.py:78-93`) and reads no
round.

### 3.3 §6a rung by rung — which artifact, which field, is it built?

`docs/tuning-methodology.md:679-728`.

| Rung | Question | Artifact | Field | Built? |
|---|---|---|---|---|
| 1 (`:690-695`) | Where are the two floors? | spec report / cloud evidence / evidence packet | `entanglement_floor_source` **read first**, then `entanglement_floor_hz`, `trusted_floor_hz`, per-band `room_entangled_below_hz` (`flat_spec.py:105,134`; `position_evidence.py:60-70`) | **BUILT.** No dedicated subcommand: read off `spec-sweep`/`entry` or the packet |
| 2 (`:697-705`) | Is this feature the room or the speaker? | `gate_sweep.json` | `features[].sensitivity.sigma_growth_ratio`, `corrected_delta_db`, `n_valid_rungs`, `window_verdict`, `window_verdict_reasons` | **BUILT** — `jasper-round-views gate-sweep --at-hz` |
| 3 (`:707-717`) | How much of the far read was the room? | `close_reference.json` | `alignment.trusted` first, then `windows[].bands[].verdict` | **BUILT for the comparison; the CAPTURE is not.** The doc says it in-line: *"the close-reference program row is #3498's amendment item 1 and is not built, so today you declare the distance yourself"* (`:713-714`) |
| 4 (`:719-726`) | Does the feature move with HEIGHT? | `gate_sweep.json` `sigma_by_axis.elevation`, packet `elevations_deg` | present-or-absent block; "a family whose own angle took one value is **absent, not zero**" (`tuning-operator-runbook.md:1051`) | **PARTIAL.** `capture_plan.py:456,463` can stage non-zero `vertical_deg`; the methodology says "Until a round banks a pose with a non-zero `vertical_deg` the deciding experiment is owed" (`:723-724`). No tool *names* the unsampled axis as a missing view |

Runbook field-by-field guides exist and are pointed at from §6a (`:727-728`):
"Reading the gate and the reflector path honestly" (`:918`), "Reading a gate
sweep" (`:998`), "Reading a close-reference comparison" (`:1108`), "Reading σ
honestly" (`:1204`). All four are current and specific.

### 3.4 The "attribute room effects, prescribe nothing there" rule — where it lives

**It is not one sentence; it is four, in three files, and it is never machine
enforced at the room verdict.**

| Statement | Where |
|---|---|
| "It licenses no filter — it is evidence for an attribution argument, never a verdict or an EQ instruction" | `docs/tuning-methodology.md:702-704` |
| "It still prescribes nothing" (close-reference) | `docs/tuning-methodology.md:716` |
| "the difference estimates the room's share … It is an attribution, **not** a target: no filter follows from it" | `docs/tuning-operator-runbook.md:1182` |
| "Correcting into a positional dip … **no EQ, ever**; route it to position, placement, or corner choice" | `docs/tuning-methodology.md:841` (FAILURE CATALOG) |
| "Position-dependent dips must never be filled" | `docs/tuning-methodology.md:494` |
| "Evidence for an attribution argument, never an EQ instruction" | `jasper/cli/round_views/sweeps.py:21-22` |
| "a position-variant interference null routes `physical` and NEVER `eq`" | `jasper/attribution/mechanisms.py:110-113` (M5), enforced in `promotion.py:53-57` |
| ADR-0002's discriminator: "No → it describes the room, the rig, or the result; disclose and recommend, never block" | `docs/measurement-loop-doctrine.md:319-321` |

The only **structural** protection is the merged exclusion mask
(`evidence_packet.py:2870-2874`: *"the mask is the only structural protection
against cutting an interference null"*) — which is built from
`identify_interference_nulls`, i.e. **≥ 4 kHz only** (§4.1). A `classification
== room` row *unvouches* a filter (`driver_prescription.py:1090-1105` →
`unvouched` count) but does not refuse it, and unvouched is explicitly "not
'will not'" (`driver_prescription.py:1082-1084`).

---

## 4. Worked case — a 3 dB dip at 400 Hz on-axis

### 4.1 The order an LLM would open things, and what it gets

| # | Artifact / command | Field it reads | What it can conclude at 400 Hz |
|---|---|---|---|
| 1 | evidence packet / `spec-sweep` — `positions[].gate_entanglement_floor_source` then `_hz`; band `room_entangled_below_hz` | `measured_reflection` / `declared_geometry` / `unknown` (`position_evidence.py:212-221`) | On a typical rig `t_bounce ≈ 2.5–3 ms` → entanglement floor ≈ 830–1000 Hz, so 400 Hz is **below** it: "NO choice of `T` separates speaker from room there" (`tuning-methodology.md:600-605`). `unknown` is the *ordinary* answer (`position_evidence.py:216-219`) and sends the climb to §0's declaration |
| 2 | `jasper-round-views gate-sweep <round> --at-hz 400` → `gate_sweep.json` | `features[].window_verdict` + `window_verdict_reasons` + `sensitivity.{sigma_growth_ratio, corrected_delta_db, centre_shift_oct, sigma_growth_readable, n_valid_rungs}` | **This is the one machine-stated verdict at 400 Hz.** Grid reaches 200 Hz (`gate_sweep.py:100`). Cycles = `400 × rung/1000`, so rungs 3/4/5 ms are `invalid` (<2.5 cycles) and the valid span is 7→20 ms — a real sensitivity, over 4 rungs |
| 3 | `jasper-round-views classify-features <bundle>` → `feature_classification.json` | `rows[].classification`, `egd_verdict`, `gate_verdict`, `pose_persistence` | **REFUSES.** 400 Hz < the ≈450 Hz `classifiable_band_hz` lower edge at the shipped 7 ms gate (`feature_classifier.py:1175-1185, 303`); with `--at 400` the feature list comes back empty → `classification_no_features_detected` carrying `classifiable_band_hz` in its detail (`feature_classifier.py:2248-2259`). Also refuses outright on a `measure`-shaped round (`:290`). **So no minimum-phase evidence exists at 400 Hz** |
| 4 | `jasper-round-views close-reference --distance …` then a human capture, then `--far-round/--close-round` | `alignment.trusted`, then `windows[].bands[].verdict` | Answers, but **for the whole 250 Hz–2 kHz band as one row** (`flat_spec.py:37`), and only after a capture the toolbox cannot take (`tuning-methodology.md:713-714`) |
| 5 | `feature_classification.json` `rows[].pose_persistence[]` | per-pose `pooled_db`, `centre_hz`, `position_deg`, `vertical_deg` | **Unavailable at 400 Hz** (rung 3 refused). Even where available it is a raw table with no spread statistic and no verdict (`feature_classifier.py:1707-1738`) |
| 6 | `jasper-round-views distortion` → `harmonic_distortion.json` | H2/H3 at `PROBE_FREQUENCIES_HZ` — 400 Hz **is** a probe point (`harmonic_evidence.py:140-143`) | Weak-positive only: harmonics are the speaker's by construction, but a *raised* H2/H3 at 400 Hz does not make the *dip* the speaker's. Level dependence — the discriminating half — is "evidence the record does not carry yet" (`tuning-operator-runbook.md:851-854`) |
| 7 | `findings_cloud_verify.json` via the packet's `findings` block | `mechanism`, `fix_class`, `confidence`, `cites` | **Structurally empty at 400 Hz.** The only producer is `promote_carve_outs`, fed by `identify_interference_nulls` over `echo_band_hz`, which is the tweeter's declared measurement band clamped to a floor of `ECHO_BAND_HF_REGIME_FLOOR_HZ = 4000.0` (`verification.py:1873`, `spatial.py:1742-1760`) or the `DEFAULT_ECHO_BAND_HZ = (5000, 19000)` fallback (`spatial_combine.py:82`). **Nothing below 4 kHz can ever become an M2/M5 finding.** |

### 4.2 Machine-stated vs LLM judgment vs guess

| Sub-question | Status |
|---|---|
| Is the feature above or below the room's floor? | **MACHINE-STATED** — `gate_entanglement_floor_hz` + `_source` pair (`position_evidence.py:203-221`) |
| Did the window move this feature? | **MACHINE-STATED** — `window_verdict` + three named routes (`gate_sweep.py:760-761`) |
| Is it minimum-phase? | **GUESS at 400 Hz** — the instrument that answers refuses below ≈450 Hz. The honest read is "unavailable"; the methodology explicitly bars inventing it (`tuning-methodology.md:527-528`) but no artifact *at 400 Hz* records that unavailability as a per-feature fact |
| How much of the 3 dB is the room's share? | **MACHINE-STATED but coarse** — `close_reference` gives it per 250 Hz–2 kHz band, not at 400 Hz; and the capture is out-of-band work (`tuning-methodology.md:713-714`) → **partly out-of-band** |
| Does it move with mic angle? | **JUDGMENT (fine)** where `pose_persistence` exists — a table the LLM weighs. **UNAVAILABLE at 400 Hz** |
| Does it move with mic *height*? | **UNMEASURED** — rung 4 owed (`tuning-methodology.md:719-726`); `sigma_by_axis.elevation` simply absent, and absence is only readable by knowing the convention |
| Which *mechanism* (SBIR vs cabinet vs crossover-region cancellation)? | **GUESS.** `mechanisms.py` registers three ids and none of them is reachable here. The runbook's signature table (`tuning-operator-runbook.md:836-842`) is offered as "a hypothesis rather than a finding" — correct per principle 2, but there is no artifact to write the hypothesis *into* |
| May I prescribe? | **JUDGMENT, correctly.** Every surface says "prescribes nothing"; the only structural bar (the merged mask) does not reach 400 Hz |

**Net:** an LLM at 400 Hz gets exactly **one** defensible machine verdict —
`gate_sweep`'s `window_verdict` — plus two floors. `stable` there is a genuinely
weak claim: the corrected-delta and centre-shift routes are blind to a
minimum-phase question nobody asked, and the min-phase instrument that would
answer it refused. A confident SPEAKER verdict at 400 Hz **is not reachable from
the artifacts today**; a confident ROOM verdict is (any `moved` route firing).
The asymmetry is unstated anywhere.

---

## 5. Is there one place? Can `inventory` tell the LLM what is missing?

### 5.1 One place — partly

`feature_classification.json` is the **closest thing**: one row per feature
carrying the verdict *and* 30 columns of working, including the gate ladder's
own blocks (`gate_rungs`, `gate_sensitivity`) and the per-pose table
(`pose_persistence`). `LAB_ROW_FIELDS` is explicitly a reader's allowlist so the
packet republishes the whole row (`feature_classification.py:104-110`,
`evidence_packet.py:2470-2496`).

What it does **not** carry, and what therefore lives in four other files:

| Evidence | Lives in |
|---|---|
| close-reference agreement | `close_reference.json` — per spec band, never joined to a feature |
| entanglement floor / room floor | cloud artifact `positions[]` and the spec report |
| harmonic evidence | `harmonic_distortion.json` — per (capture, role) band |
| mechanism id + fix class + confidence tier | `findings_{phase}.json` — never joined to a feature, never keyed by frequency compatibly |

The evidence packet juxtaposes them (`evidence_packet.py:2843-2913`:
`spec`, `positions`, `honesty_mask`, `findings`, `reflections`,
`feature_classification`, `harmonics`) but does **not** join them — and says so
deliberately for the two views inside the classification block
(`evidence_packet.py:2449-2456`: "Not joined into one list per feature … pairing
them by frequency is a judgement this module does not make"). That refusal is
principled; the *cross-artifact* absence is not stated anywhere.

Two concrete packet defects found:

1. **The packet reads only one of three finding phases** —
   `evidence_packet.py:2725` hard-codes `findings_cloud_verify.json`.
   `findings_cloud_measure.json` and `findings_measure.json` (the M7
   level-frame finding, `correction_crossover_v2.py:3325-3335`) are written and
   never read by anything.
2. **The packet erases the ran/never-ran distinction the schema exists to
   preserve.** `_mapping(None) == {}` (`evidence_packet.py:385-386`), so an
   absent file yields `"findings": {"findings": [], …}`
   (`:2875-2878`), and the `not_evaluated` entry fires only when the key *is* a
   list (`:2654`). So "attribution never ran" and "attribution ran and found
   nothing" render identically — the exact conflation `produced_by` was added to
   prevent (`findings.py:186-190`, `storage.py:12-14`). The packet also drops
   `produced_by` and `session` on the way through.

### 5.2 `inventory` — no

`ARTIFACT_BY_VIEW` (`jasper/cli/round_views/_common.py:79-103`) holds **17**
view artifacts. `inventory` reports presence + `produced_by` for exactly those
(`jasper/cli/round_views/inventory.py:53-66`). **None of the three
`findings_{phase}.json` names is in it**, nor the cloud artifact, nor the
evidence packet. So `inventory` cannot say "this round has no attribution
findings" or "attribution never ran here".

It also cannot say the two things that decide whether the attribution views can
answer at all: whether the round is `verify`-shaped (gating `classify-features`,
`feature_classifier.py:290`) and whether more than one pose was banked (gating
`gate-sweep`, refusal `gate_sweep_single_pose`,
`tuning-operator-runbook.md:1090`). It reports `banked` and paths only.

---

## 6. Gaps, and the smallest honest change per gap

Marked **[toolbox defect]** (an LLM would have to write a script / guess /
go out-of-band) vs **[by-design judgment]** (principle 2 — leave it).

| # | Gap | file:line | Size | Class |
|---|---|---|---|---|
| G1 | **The `attribution` package is unreachable from any CLI.** 2,047 lines, three mechanisms, a hash-verified citation chain — and no console script imports it. An LLM wanting a finding set must read `evidence/v1/artifacts/crossover_v2/*/findings_*.json` by hand. | `record_store.py:137`; no hit in `jasper/cli` | **small**: one `jasper-round-views findings <bundle> [--phase P]` reading all three phases through `storage.read_finding_set` (which already exists, `storage.py:91`), plus one `ARTIFACT_BY_VIEW` row per phase so `inventory` reports it | **toolbox defect** |
| G2 | **`inventory` names 17 views and none of the attribution artifacts**, so principle 3's "`inventory` names what is missing" does not hold for this axis. | `_common.py:79-103`, `inventory.py:53` | **tiny** (falls out of G1) | **toolbox defect** |
| G3 | **The classifier's ≈450 Hz floor is invisible until it refuses.** Nothing tells the LLM in advance that `classify-features` cannot answer at 400 Hz on the shipped gate, and nothing records "min-phase unavailable here" as a per-feature fact. The refusal detail does carry `classifiable_band_hz` (`feature_classifier.py:2253`), which is honest but arrives only after the run. | `feature_classifier.py:1175-1185, 303` | **tiny**: print `classifiable_band_hz` in `classify-features`'s stderr summary and in `inventory`'s row for it | **toolbox defect** |
| G4 | **The packet reads one finding phase of three**, and the M7 level-frame finding is written to a path no reader opens. | `evidence_packet.py:2725` vs `correction_crossover_v2.py:3325-3335` | **tiny**: read all three, key the block by phase | **toolbox defect** |
| G5 | **"attribution ran and found nothing" is indistinguishable from "attribution never ran"** in the packet, defeating the one distinction `produced_by` exists for. | `evidence_packet.py:385-386, 2654, 2875-2878` | **tiny**: carry `produced_by`/`present` through and emit the `not_evaluated` entry on absence too | **toolbox defect** |
| G6 | **Nothing below 4 kHz can ever produce an attribution finding**, and no surface says so. An LLM reading an empty `findings` block at 400 Hz will read it as "no mechanism found" rather than "this instrument does not look here". | `verification.py:1873`, `spatial_combine.py:82`, `spatial.py:1742-1760` | **tiny**: publish the resolved `echo_band_hz` beside the findings block (the cloud artifact already banks it, `spatial.py:2342`) and say the band bounds the finding set | **toolbox defect** |
| G7 | **No artifact joins a feature to a room/speaker verdict + full evidence trail across the four views.** The packet's non-join inside the classification block is principled (`evidence_packet.py:2449-2456`); the cross-artifact non-join is not stated at all. | `evidence_packet.py:2843-2913` | **medium** if a joined view is wanted; **tiny** if instead each view publishes the key the others join on (feature centre in Hz + the frame it is stated in) | **toolbox defect** for the missing join key; **by-design judgment** for the join itself |
| G8 | **`close-reference` answers per spec band, so the low-mid decade gets one verdict.** A 400 Hz question is answered by a 250–2000 Hz row. | `close_reference.py:262-266`, `flat_spec.py:37` | **small**: accept `--at-hz` and emit a narrow-band row beside the spec rows, the way `gate-sweep` already does | **toolbox defect** |
| G9 | **Rung 3's capture is out-of-band work.** The methodology names it in-line: the close-reference program row is not built, so the operator declares the distance by hand and `mark_distance_m` is pinned at 1.0 for every pose (#3498, `close_reference.py:22-25`). | `tuning-methodology.md:713-714` | **medium**: a close-reference program row in the measure spec so `--close-m` is banked rather than declared | **toolbox defect** (this is exactly "talk out-of-band") |
| G10 | **Rung 4's axis is unmeasured and its unmeasuredness is only readable by convention** (`sigma_by_axis.elevation` absent). | `gate_sweep.py:534-537`, `tuning-operator-runbook.md:1051` | **tiny**: publish an explicit `elevation: {sampled: false, reason: …}` NOT-RUN block, mirroring `pose_bank`'s own NOT-RUN shape (`feature_classifier.py:1750-1758`) | **toolbox defect** |
| G11 | **`pose_persistence` is a raw per-pose table with no spread statistic.** The LLM must eyeball N rows to judge position-variance — a wall of numbers, if not of curves. | `feature_classifier.py:2353-2356` | **tiny**: add the σ across resolved poses beside the table (the number, not a verdict) | **toolbox defect** for the statistic; **by-design judgment** for the verdict |
| G12 | **Level dependence — the discriminator that would separate rattle/compression from a passive defect — is not carried.** The runbook says so plainly. | `tuning-operator-runbook.md:851-854` | **medium** (needs a second drive level in the capture plan) | **toolbox defect**, but a real measurement cost |
| G13 | **`classification == room` unvouches a filter; nothing refuses one.** Consistent with doctrine ("disclose and recommend, never block", `measurement-loop-doctrine.md:320-321`) and with principle 2. | `driver_prescription.py:1082-1105` | — | **by-design judgment — leave it** |
| G14 | **`gate_sweep` publishes evidence and no attribution; `close_reference` publishes a verdict.** The asymmetry is deliberate and stated in code (`close_reference.py:225-228`). | | — | **by-design judgment — leave it** |
| G15 | **§6a never points at `classify-features`.** Its four rungs are the spec report, `gate-sweep`, `close-reference` and elevation poses; the min-phase discriminator is introduced separately in §6 (`tuning-methodology.md:501-505, 516`). A reader climbing the ladder never runs the classifier. | `tuning-methodology.md:679-728` | **tiny**: one pointer sentence in rung 2 naming `classify-features` and its band limit | **toolbox defect** (a missing methodology pointer is principle 1's fourth clause) |

### What I could not determine

Whether `entanglement_floor_source` reads `measured_reflection` often enough in
practice for rung 1 to be decisive — the repo carries no `captures/` corpus at
this checkout (`ls /home/user/JTS/captures` is empty), so the P1 and
gate-sweep-validation READMEs the code cites
(`gate_sweep.py:124-127`, `tuning-operator-runbook.md:1105-1107`) could not be
opened, and the 400 Hz worked case is reasoned from the constants rather than
from a banked round.
