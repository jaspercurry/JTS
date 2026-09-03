# The agent tuning bench — design and work order

> **Status: historical.** Snapshot from 2026-07-27 when the tuning bench was
> framed as a prescriptive verb/schema/lexicon ladder. Preserved for
> primary-source archaeology — specific contracts below are not current
> planning authority. Read this for the investigation and original rationale;
> current direction lives in
> [`tuning-master-plan.md`](../tuning-master-plan.md) (it superseded
> [`llm-native-tuning-workbench-plan.md`](llm-native-tuning-workbench-plan.md),
> which replaced this design, on 2026-08-21).
> Durable evidence this design reasoned from is laptop-side and gitignored:
> `captures/iloud-comparison-20260727/`.

## 0. The mandate

On 2026-07-27 an ad-hoc laptop agent session measured JTS3 and an iLoud
Micro Monitor with a calibrated mic, level-matched them to 0.15 dB,
connected the owner's verbatim words ("dull, lacking that shine or
shimmer") to a measured −7 to −11 dB tweeter-band deficit, ruled out room
/ placement / distortion / compression by measurement, applied a
revertible EQ overlay, re-measured to ±0.9 dB of the reference, and
restored the speaker to its exact pre-session state — and found a 10 dB
defect the product's own pipeline had shipped and graded silently. The
owner's ask, distilled:

1. **Make that loop repeatable by any session.** Any Claude/Codex session,
   on any JTS speaker, should run measure → chart → discuss → attribute →
   fix → verify → restore without an expert hand-rolling a kit.
2. **Not an embedded OS agent yet.** The near-term target is
   agent-assisted sessions from a laptop. The in-product experience
   (~2-month horizon) comes later and borrows these primitives.
3. **Layer disambiguation is the intellectual core.** Ears hear the total
   stack — preference EQ + room + linearization + integration — blurred
   together. Attribution must become a shared model, not room
   correction's private machinery.
4. **"Proof upon room correction, then abstract."** Working hypothesis to
   prove or refute: the calibration-agent contracts have the right
   *shape* but the wrong *scope*. Verdict in §2.2: **confirmed, with an
   amendment.**

## 1. What the one-off proved — the properties to keep

These are the properties that made the session work, each traced to where
this design makes it durable (section reference in parentheses):

| Property | One-off implementation | Where it lands |
|---|---|---|
| Verbatim perceptual words first, every word mapped to a measured correlate or an explicit "no correlate found" | REPORT.md §1/§4, hand-collected | Lexicon + attribution schema (§5, §7) |
| Level match before any comparison (0.15 dB) | band-limited pink + stimulus-gain baking | `level`/`compare` verbs (§4.1) |
| Overlay-not-canonical writes, one-command revert, verified restoration table | `payoff_eq.py` + `PRE_SESSION_STATE.json` | Overlay lifecycle + session ledger (§6) |
| Ruled-out alternatives by measurement before claiming a cause | gated-vs-in-room, drive-level delta, THD, ETC | Discriminator catalog (§5) |
| The fix loop closed acoustically — refine fitted against the *measured* residual, not the model | `payoff_eq.py refine` | `verify` verb now; PR-L5 delta-probe when it lands (§8) |
| Analysis as thin composition of the production kernel, no reimplemented DSP | `analyze_iloud.py` over `jasper.audio_measurement` | The bench is composition-only (§10) |

## 2. Audit: what exists, what generalizes, what is missing

Four audits (calibration-agent contracts; disclosure machinery; capture
and analysis primitives; the in-flight U-/L-ladders) were run against
this checkout at `1d172dd44`. Synthesis follows; the verdict tables are
condensed from the audit briefs.

### 2.1 The measurement kernel is complete; the workbench is not

`jasper/audio_measurement/` (24 modules) plus
`jasper/active_speaker/flat_spec.py` is a genuine shared kernel — sweep
generation (`synchronized_swept_sine`), deconvolution and gating
(`deconvolve`, `gate_impulse_response`), calibration
(`parse_calibration_text`, `apply_calibration_curve`), smoothing and
grids (`smooth_fractional_octave`, `resample_log`), spatial combine +
echo/geometry honesty (`combine_positions`, `detect_echo`), null
identification (`identify_interference_nulls`), spec grading
(`evaluate_flat_spec`), before/after deltas (`before_after_delta`),
harmonics (`thd_curve`, `extract_harmonic_ir`) — all tested, all reused
by both session kits. A one-call WAV→IR replay wrapper already exists
(`jasper.correction.interop.impulse_response_from_capture`), and a
neutral evidence-bundle manifest layer exists
(`jasper.audio_measurement.bundles`). The packaging precedent also
exists: `jasper/bass_extension/bench/` + `jasper-bass-extension-bench`
is a shipped operator-supervised bench (manifest → `--dry-run` default →
`--live` → replayable bundle) with a fail-closed temporary-graph
activation lifecycle.

What is **missing** is exactly the workbench layer both kits hand-rolled:

1. **A laptop measurement-mic capture driver.** Nothing in `jasper/` or
   `scripts/` generates a sweep, records a UMIK, and applies calibration
   end-to-end; `scripts/capture-correction-diagnostic.py` is a passive
   observer. Both real drivers are gitignored session kits.
2. **One canonical analysis JSON.** Four frequency-grid conventions are
   in flight (room correction: 480-pt log grid; crossover-v2 cloud:
   linear, stride-decimated to 512; S0 kit: linear stride to 1500; iLoud
   kit: 900-pt log), three level references, two band tables — and THD
   is emitted by **none** of them despite `thd_curve` shipping tested.
3. **A general overlay snapshot/apply/revert verb.** The only true
   prior-state undo in the product is crossover-v2's `handle_v2_restore`
   (`pre_apply_profile` stash, Layer-A-scoped). Everything else is
   intra-transaction rollback (`dsp_apply`), policy-driven auto-revert
   (`correction/acceptance.py`), layer-strip, or
   regenerate-from-intent. `/sound/audition` (`sound_audition.yml`) is
   reversible by never persisting — including a built-in `bypass` mode —
   but is capped at preference-EQ shape (≤8 bands, ±12 dB) and stores no
   pre-state.
4. **A session ledger.** Both kits invented a private `session.json`;
   neither used the repo's artifact-manifest writer.

`docs/testing-tooling.md` has no row for "capture a calibrated sweep" or
"apply/revert an EQ overlay" — the index confirms the gap.

### 2.2 The calibration agent: right shape, wrong scope — confirmed, amended

The owner's hypothesis is **substantially correct**. The shipped shape —
redacted evidence packet → narrate (`interpret`) → propose bounded JSON →
validate (`response.py`) → simulate (`proposal_sim.py`) → confirm-gated
apply through the layer's own substrate — is exactly the loop the iLoud
session ran by hand, and its safety architecture is doctrine-conformant
(the model holds no handle, emits only text; recursive prohibited-key
blocklist; server re-validates and re-simulates). **Do not replace it.**

The scope defect is concrete: the action taxonomy is **keyed on DSP layer
instead of parameterized by it**. Three of the six action constants
(`ACTION_AUDITION`, `ACTION_COMMIT`, `ACTION_PROPOSE_CORRECTION_PEQ`) are
the same operation — "propose a bounded biquad set for layer L, simulate,
confirm-gate" — forked per layer; adding linearization and crossover the
current way costs ~7 parallel edits per layer, and the strict OpenAI
response schema (every action property `required` on every action) grows
with each one.

The amendment: **the most cross-stack piece already exists and is dead.**
`actions.run_validated_action_plan` + the injected-`ActionExecutor` seam
is precisely the layer-neutral, host-mediated apply runner a cross-stack
agent needs — and the shipped P6 tuning LLM bypassed it
(`correction_advisor._review_actions` is a parallel re-implementation).
The second half of that finding — `_advisor_packet_for_model`
hand-writing a policy rather than calling `advisor_context._advisor_policy`
— is resolved: the hand-written list existed only to satisfy a per-action
confidence veto in `response.py`, and retiring that veto under
[`measurement-loop-doctrine.md`](../measurement-loop-doctrine.md) deleted the
list along with it. `_advisor_policy` is now the single policy authority
and emits advisories, not permissions. Roughly 55% of the package is
live (model client, key provisioning, spend gate, interpret/propose
routes), 45% dormant scaffold (`tools.py`, `cli.py`, `actions.py`,
`sound_actions.py`, the runtime-read corpus — which the full-speaker
install profile does not even stage). Already-general as-is:
`model_client`, `key_provisioning`, `check_number_provenance`, the spend
ledger. Widening is therefore a **rename + parameterize + fork-merge**,
not a rebuild — the concrete map is recorded in §8.2 for the future
product-agent session. No other DSP layer has any LLM surface today, so
there is nothing else to un-fork.

### 2.3 Disclosure: several parallel copy layers, one shared grammar

There is **no shared copy module** (`find jasper -name "*copy*" -o -name
"*disclos*"` is empty). Room and crossover each own a screen envelope
with different vocabularies (schema-9 `sections` vs schema-8 `steps`);
bass and taste-EQ copy is predominantly **browser-owned**; the doctor
re-embeds homeowner sentences verbatim; the capture page owns its own
failure prose. But both measurement domains independently converged on
the same architectural split, and that split is the real asset:

- **Evidence modules emit codes + numbers only; the domain flow/envelope
  owns the sentence** (`confidence.py` → `_NUDGE_COPY`;
  `interference_nulls.py` → `_carve_out_*_copy`; `flat_spec.py` →
  `_flatness_details_lines`; `envelope.py` → `_VERDICT_HEADLINE`).
- A recurring **general grammar**: closed code→sentence catalogs with
  retryability and one recovery action; measured-vs-expected-with-
  tolerance sentences; "we deliberately did *not* do X, because W"; the
  dual `{disclosure, expert}` register (`carve_outs_by_band` is the most
  developed instance); honest-absence vocabulary (`SpecFlatness.evaluable`
  vs `.passed`).
- One proven cross-domain shared translator exists
  (`describe_ramp_refusal`, Room-owned, Active-consumed) — the precedent
  that sharing a policy-free primitive across domains is permitted.

What a generalized DSP-change disclosure layer would need that nothing
has: a **change verb** ("we changed X by Y" — `DspApplyState` records
every mutation's prior/candidate configs but no delta and no words), a
**DSP-layer identity vocabulary** (no shared household-facing names for
the five layers), register-as-a-field, and homes for two orphan code
vocabularies that are pure attribution data computed and discarded today
(`linearization_envelope.EnvelopeReason` — 11 "why we limited correction
here" codes with zero consumers — and
`linearization_fit.HF_SUPPRESSION_REASONS`).
`room-correction-information-design.md` explicitly forbids a generic
framework and requires "a policy-free primitive after a concrete second
consumer proves the seam" — the bench is that second consumer, and §8.3
scopes what it adopts now versus what waits for the product extraction.

### 2.4 The in-flight ladders: substrate and collision map

The bench builds ON, and must not collide with:

- **Safe substrate (no open rung touches these):**
  `spatial_combine.py`, `interference_nulls.py`, `flat_spec.py`,
  `linearization_envelope.py`, `deconv/gating/analysis/calibration/
  sweep`, `dsp_apply.py`, `correction/interop.py`.
- **Active-edit zones (do not propose structural changes):**
  `crossover_v2_flow.py` (PR-U1 landed; PR-L4 and PR-U3 pending),
  `sound/profile.py` + `camilla_yaml.py` + `linearization_fit.py` +
  `eq-math.js` (PR-L2), `audio_measurement/calibration.py` (PR-L1),
  `program_analysis.py` (PR-L3), `web/correction_setup.py` tier picker
  (PR-U3).
- **PR-L5's delta-probe has no code yet.** It is the single most
  bench-relevant planned primitive — a generic "apply, re-measure,
  classify realized-vs-commanded, auto-rollback on non-matched" loop.
  The bench must consume it, not fork it (§8.4).

## 3. Architecture: two LLM seats over one deterministic bench

The owner's LLM boundary doctrine is fixed: detection and measurement
verdicts stay algorithmic; the LLM owns dialogue, attribution
walkthrough, and plan narration over deterministic primitives. Applying
it here yields the design's central move — there are **two LLM seats**,
and one set of deterministic primitives under both:

- **Seat 1 (now): the session agent.** Claude/Codex on the laptop *is*
  the model; it needs no model client, no response-validation gate, no
  spend ledger — it needs **deterministic verbs with refusal semantics**
  (a CLI that will not do unsafe things) plus a **protocol document**
  that scripts the session discipline. The human approval loop is the
  conversation itself.
- **Seat 2 (later, ~2 months): the in-product agent.** The widened
  calibration agent (§8.2). Its executors — the things its validated
  actions actually call — must be the **same Pi-side primitives** the
  bench drives over SSH, so the product LLM arrives as a thin conductor
  over tools that already exist and have been exercised for months.

Everything in §§4–7 is Seat-agnostic by construction: canonical JSON,
the overlay lifecycle, the attribution vocabulary, and the lexicon are
data contracts both seats read.

## 4. Q1 — the bench contract and packaging

### 4.1 The verbs

The minimal stable set, derived from what the kits hand-rolled twice.
Each is a thin composition of the kernel — the bench adds **no new DSP
math**:

| Verb | What it does | Composes |
|---|---|---|
| `preflight` | laptop mic sanity: record, per-channel RMS/peak, refuse digital silence (the macOS mic-permission trap), identify mic channel | sox/coreaudio + the S0 kit's silence floor |
| `stimulus` | generate sweep + band-limited pink, bake per-target level-match gains, single-channel drive safety, push to target Pis | `synchronized_swept_sine`, the iLoud kit's `write_single_channel_wav` invariant |
| `level` | play pink on one target, report band RMS at the mic; iterate to a stated match tolerance | the iLoud kit's `band_rms_dbfs`; match is baked into stimulus gain, never speaker volume (quantization + restoration reasons) |
| `sweep` | play sweep on one target while recording laptop-side; N repeats; JSON sidecar per capture | the iLoud kit's `capture_once` |
| `analyze` | WAVs → canonical analysis JSON (§4.3) + charts | `deconvolve`/`direct_arrival_window`/`gate_impulse_response`, `apply_calibration_curve`, `smooth_fractional_octave`, `resample_log`, `thd_curve`, `spatial_average_db`, `normalize_to_band` |
| `compare` | two-source A/B: common-gate negotiation, difference curves, band deltas, ruled-out inputs (drive-level delta, THD, ETC) | `analyze` ×2 + the iLoud kit's common-gate logic |
| `overlay` | `snapshot` / `design` / `apply` / `refine` / `revert` / `status` — the safety envelope (§6), driving the Pi-side owner | `jasper-dsp-overlay` (new, §6) + `jasper.sound.profile`'s filter evaluator |
| `ledger` | `open` / `record` / `verify-restore` / `close` — the session state ledger | `jasper.audio_measurement.bundles` manifest primitives |

The phone-relay capture surface stays **read-only in v1**: the bench
consumes product bundles (via `jasper-correction-bundle inspect
--recompute` and `impulse_response_from_capture`) rather than driving
the relay. Driving it is proven possible (the S0 kit impersonated the
phone) but adds a second transport for no v1 gain — the laptop UMIK is
the calibrated bench mic, and per-driver/cloud evidence is better taken
from the product's own instrument.

### 4.2 Packaging

**A `jasper/tuning_bench/` package + `jasper-tuning-bench` console
script, one protocol HANDOFF doc, and a thin repo skill.** Rationale:

- The CLI + protocol doc are the **agent-agnostic contract**. A Codex
  session (no skill machinery) gets everything from
  `docs/HANDOFF-tuning-bench.md` — the 2026-07-27 session itself proved
  a well-written doc is a sufficient entry point (it ran from
  `SESSION-PROMPT.md`).
- `.claude/commands/tuning-bench.md` is sugar: it loads the HANDOFF and
  enforces the session discipline (owner's words verbatim before charts;
  ping before any sound; level-match before comparison; ledger open at
  start, verified restoration at close).
- In-repo (not `scripts/`) because it is Python with tests, mirrors
  `jasper/bass_extension/bench/` exactly, and rides `.env.local` /
  `PI_HOST` conventions via the same laptop-side entry pattern. Heavy
  imports (scipy/matplotlib) stay lazy so the package is inert on the
  Pi runtime it inevitably ships inside.
- Per the extensibility doctrine's decision tree this is **not** a new
  contract and **not** an on-Pi Feature — it is an operator instrument
  (the bass-bench class). The one Pi-side piece (§6's overlay owner) is
  a small CLI over existing `dsp_apply` machinery.

### 4.3 The canonical analysis JSON — `jts_tuning_bench_analysis` v1

One schema ends the four-grid divergence **for bench outputs** (product
surfaces converge later, when next touched — not rewritten now):

- **Grid:** the shipped product convention — `resample_log` 480-point
  log grid, 20 Hz–20 kHz, interpolation (never stride subsampling, which
  aliases combed curves). Grid parameters recorded in the payload.
- **Blocks:** `meta` (session id, target host, mic + calibration
  identity with `sign_convention` — miniDSP files are response curves
  and must be negated; the bench asserts this from day one rather than
  waiting for PR-L1), `stimulus` (`SweepMeta`, drive channel, gains),
  `gating` (per-capture fragments, negotiated common gate,
  `f_valid_hz`), `curves` (gated + in-room, per-target, anchor-band
  normalization stated), `bands` (the perceptual band table — §7's
  vocabulary — plus `SPEC_BANDS` gauges when grading against flat),
  `thd` (median/p90 + curve — the first consumer of the shipped-but-
  unwired `thd_curve`), `level` (band RMS references, match residual),
  `repeats` (spread), `attribution` (§5), and a reserved `delta_probe`
  block (§8.4).
- **Written as an evidence bundle** via
  `jasper.audio_measurement.bundles` (`record_artifact` /
  `write_json_artifact`) so bench sessions carry the same
  sha256-manifested forensic shape as product bundles, instead of a
  third bespoke `session.json`.

### 4.4 What stays session artifact

Narrative reports (REPORT.md-shape), charts beyond the standard set,
experiment-specific drivers (e.g. the two-live-cabinet single-channel
rig logic beyond the safety invariant), and handoff prompts remain
per-session files under `captures/<session>/`. The bench owes them a
stable substrate, not a template engine.

## 5. Q2 — layer attribution

The blur problem is: the ear hears the composed stack; a complaint names
no layer. The product already has most of the discriminators; what it
lacks is the shared vocabulary and the record shape.

**The layer vocabulary** adopts the five-layer model
(`active-speaker-tuning-layers-design.md`) as ids —
`linearization` (1a), `integration` (1b), `bass` (2), `room` (3),
`preference` (4) — plus the attribution-only causes a complaint can
resolve to that are not DSP layers: `hardware` (directivity, decay,
distortion, compression), `placement` (room/boundary geometry), `level`
(the loudness confound), `source` (program material). First code
instance lives in `jasper/tuning_bench/attribution.py`; promotion to a
shared module happens when the second consumer (Seat 2) needs it —
rule-of-two, per doctrine. Naming note: avoid "correction" and
"envelope" in new identifiers — both are already three-way overloaded
in-repo.

**The discriminator catalog** — what separates which causes, all but one
already shipped:

| Discriminator | Separates | Primitive | Status |
|---|---|---|---|
| Level match | `level` from everything | §4.1 `level` verb | kit-proven, promote |
| Gated vs in-room agreement | speaker layers from `room`/`placement` | `gate_impulse_response` + both curves in §4.3 | shipped |
| Drive-level re-measure | `hardware` compression from linear causes | re-run `sweep` at −10 dB, delta | kit-proven |
| THD curves | distortion from tonal balance | `thd_curve` | shipped, unwired |
| ETC / reflection detection | `placement` from speaker | `detect_first_reflection`, echo machinery | shipped |
| Config A/B (layer bypass) | `preference` / `room` / overlay contribution | `/sound/audition` `bypass` mode today; §6 overlay lifecycle generally | partial → §6 |
| Per-driver vs summed | `linearization` from `integration` | product crossover-v2 bundles, read via replay | shipped (read-only) |
| Cross-position spread | `placement`/interference from speaker | cloud instrument, null registry | shipped |
| Delta-probe classes | model error vs driver vs placement, post-change | PR-L5 | planned — consume, don't fork |

**The attribution record** (embedded in analysis JSON and the ledger):

```
{complaint:   {verbatim, captured_at, anchoring: independent|anchored},
 hypotheses:  [{id, cause, band_hz?, source: lexicon|agent|owner}],
 discriminators: [{kind, artifact_ref, result, supports: [...], rules_out: [...]}],
 verdict:     {attributed_to, confidence, no_correlate_found, notes}}
```

The `anchoring` field encodes the honesty lesson from REPORT.md §1:
words captured after a chart was shown are confirmatory, not
independent, and the record must say so. The two orphan code
vocabularies audit B surfaced (`EnvelopeReason`,
`HF_SUPPRESSION_REASONS`) are exactly the "why we limited correction
here" evidence attribution wants — the schema's `artifact_ref` can point
at them; wiring them to user-facing copy remains Seat-2 work.

## 6. Q3 — the safety envelope

The overlay pattern becomes THE contract, owned by a small Pi-side
primitive — recommendation: `jasper/dsp_overlay.py` + `jasper-dsp-overlay`
console script — built on the existing apply machinery, driven by the
bench over SSH, and later callable as a Seat-2 executor:

- **Snapshot** records `{config_file_path, config sha256, main_volume_db,
  listening_level, applied-profile fingerprint, dsp_write_epoch}` to a
  state file before any change (generalizing crossover-v2's
  `pre_apply_profile` stash and the kit's `PRE_SESSION_STATE.json`).
- **Apply** writes the overlay to its **own** config file
  (`tuning_overlay.yml` beside `sound_audition.yml`), validated through
  `validate_camilla_config` (which enforces the `volume_limit: 0.0`
  ceiling), loaded under `dsp_writer_lock`, recorded as a
  `DspApplyState` with `source="tuning_bench"`. It refuses, by
  construction and by contract test, to write any other config name —
  canonical files are not reachable from this code path.
- **The config and the master volume move as one transaction.** A
  cut-only overlay carries makeup gain at the master; the two must
  apply and revert together, and an in-place refine re-points the same
  path (never bounces through the canonical config — the un-tilted
  config against a +10 dB makeup master is the loud-transient trap the
  kit documented).
- **Revert** restores path + volume, verifies (path, sha, volume,
  fingerprint — the restoration table the session printed), deletes the
  overlay, and the ledger refuses to close a session without a verified
  restoration or an explicit owner-directed hold.
- **Scope of edit:** an overlay may add filters to the pre-split stage
  and may adjust *existing* per-driver linearization gains within
  `[commissioned value, 0]` ("reduce our own cuts", the PR-L5
  first-class operation). It never touches limiters, protection,
  delays, mixers, or devices — contract-tested.
- **Fail-safe direction is a feature:** a reconcile or `jasper-camilla`
  restart points CamillaDSP back at the canonical config. The lifecycle
  documents this as the backstop (overlay lost ⇒ speaker reverts to
  saved sound), and `overlay status` detects it instead of assuming.
- **Truthful household surface:** the overlay filename registers in
  `describe_current_config`'s vocabulary so `/correction/` reports
  "Tuning-bench overlay active" rather than "a config JTS did not
  generate" (audit B's filename-regex footgun).

Rails, convention → structural:

| Rail | Today | After PR-B2 |
|---|---|---|
| `volume_limit: 0.0` ceiling | structural (validator + doctor + clamp) | inherited via `validate_camilla_config` |
| Canonical configs untouched by sessions | convention (kit discipline) | structural: overlay owner writes only its own file |
| Pre-state snapshot + verified restoration | convention (kit + REPORT table) | structural: ledger gate |
| Config+volume as one transaction | undocumented trap | structural: lifecycle invariant |
| Headroom spend disclosed, not silently capped | owner ruling (PR-L5 doctrine) | `overlay design` prints makeup/headroom; cut-only remains the default shape |
| Conservative start levels, ramp | protocol prose | CLI defaults (−12 dBFS sweep, −24 pink) + protocol doc; "ping before sound" stays a human rule in the HANDOFF/skill |
| Heavy analysis never on the Pi | convention | structural: analysis runs laptop-side by construction |

Per the owner's standing safety ruling this is a tinker-project envelope
— revertibility and hearing protection, not ceremony. Everything above
is either already shipped machinery or a thin wrapper on it.

## 7. Q4 — the perceptual lexicon

A pure-data table, `jasper/tuning_bench/lexicon.py`: entries
`{term, synonyms, hypotheses: [{cause, band_hz, direction}],
discriminators: [kinds], notes}`, seeded from the session's proven
mappings ("dull / lacking shine or shimmer" → HF level 3–16 kHz;
"harsh" → 4–8 kHz level *or* distortion *or* resonance; "boxy" →
100–300 Hz; "nasal" → 500 Hz–1 kHz or baffle-step; "thin/under-bass" →
LF extension; "presence" → 3–4 kHz) plus the standard psychoacoustic
vocabulary, each entry naming which discriminators test it.

The honesty contract, stated in the module docstring and enforced by
the report shape: **the lexicon generates hypotheses to test, never
conclusions.** A bench report maps every captured term either to a
measured correlate or to an explicit "no correlate found" — the latter
is itself a finding (it pointed at directivity/distortion in the
session brief). The product later reuses the same table for its guided
"describe what you hear" step; nothing in it is bench-specific.

## 8. Q5 — the bridge to product

### 8.1 Constraints adopted now

1. **Product-grade outputs from day one:** canonical JSON on the product
   grid, written as manifested evidence bundles (§4.3) — no bench-only
   formats to rewrite.
2. **Bench verbs are the future executors:** the Pi-side overlay owner
   and the analysis contract are the exact seams Seat 2's validated
   actions will call (`ActionExecutor` shape).
3. **Copy in the shared grammar:** bench reports use the two-register
   `{disclosure, expert}` pattern, measured-vs-expected-with-tolerance
   sentences, and honest-absence vocabulary, so their copy is liftable
   into the product disclosure layer when it is extracted (§8.3).
4. **The layer/attribution vocabulary is one namespace** (§5) shared by
   both seats.

### 8.2 The Seat-2 widening (recorded, not built now)

When the product walkthrough flow is scheduled, the calibration agent
widens rather than a parallel build — the audit's concrete map, recorded
here for that session:

- Rename `jasper/calibration_agent/` → a layer-neutral name; ~9 call
  sites.
- Introduce a **layer-descriptor registry** (the transit pattern): one
  pure-data record per DSP layer — bounds provider, filter vocabulary,
  simulate, apply executor, packet builder, prompt fragment. The host
  walks it with zero per-layer knowledge.
- Collapse the action taxonomy to four verbs — `explain`,
  `request_evidence`, `propose_filter_change {layer_id, filters,
  persistence: ephemeral|persistent}`, `propose_setting_change` — making
  the strict response schema O(1) in layers.
- **Resolve the fork:** merge `correction_advisor._review_actions` back
  into `actions.run_validated_action_plan` (add a simulate executor).
  The policy half of this item is done — `_advisor_policy` is the one
  authority, and its room-confidence-gates-taste-EQ coupling stopped
  being a gate when the veto was retired: those reasons now ride out as
  each action's `policy_advisories`, so there is no coupling left to
  inherit, only provenance to carry.
- Split `_SYSTEM_INSTRUCTIONS` into a layer-neutral core + per-layer
  fragments; `sound_actions.py` finally wires as the `preference`
  executor (reconciling the undeclared `advisor` audition mode).
- Carry-forward footguns from the audit: the spend ledger's
  single-writer constraint; the runtime corpus is unreachable on the
  full-speaker install profile (decide: bake into prompts or delete);
  `check_number_provenance` is advisory, not a guarantee; two private
  cross-module imports (`_curve_summary`, `_SYSTEM_INSTRUCTIONS`) need
  pinning before any rescope.

### 8.3 The disclosure extraction (Seat-2-adjacent, not bench work)

The bench does not build a shared disclosure module — the
information-design doctrine requires the seam be proven by a concrete
second consumer first, and the bench's reports *are* that proof in the
shared grammar. The extraction, when it happens, reuses:
`dsp_apply`/`DspApplyState` as the universal change spine,
`describe_current_config` as the what-is-loaded classifier, the
evidence/copy split, the closed-catalog + mirrored-allowlist pattern,
and the `{disclosure, expert}` register. It must add the change verb
("we changed X by Y because W"), the layer vocabulary as a field, a
`/state` DSP section, and copy homes for the orphan `EnvelopeReason` /
`HF_SUPPRESSION_REASONS` codes.

### 8.4 The delta-probe shared contract

PR-L5's delta-probe — realized-vs-commanded per-frequency map,
classified `matched` / `model-error` / `level-dependent` /
`spatially-costly`, auto-rollback on non-matched — **is this bench's
verification core, and L5 remains its implementation owner.** Until it
lands, the bench's `verify` composes shipped primitives
(`before_after_delta`, residual fitting against the measured result —
the session's proven method); the analysis schema reserves a
`delta_probe` block matching L5's classification vocabulary so adopting
the shared module is a data-source swap, not a schema break. The bench
ladder does not implement the classifier. If bench scheduling ever
pressures L5's, that is a sequencing decision for the coordinator — not
a license to fork.

## 9. The PR ladder

Small, serial, each independently mergeable and adversarially reviewed
to the standard gate. No rung touches an active-edit zone (§2.4).

- **PR-B0 — this document** (docs lane): the design, doc-map + README
  registration.
- **PR-B1 — capture + analysis:** `jasper/tuning_bench/` (`mic`,
  `stimulus`, `remote`, `analyze`, `compare`) + `jasper-tuning-bench`
  CLI + `jts_tuning_bench_analysis` v1 + charts. Composition-only over
  the kernel; THD wired in; calibration loaded with the correct
  response-curve sign from day one. Hardware-free tests over fixture
  WAVs; one on-hardware smoke against JTS3 documented in the PR.
- **PR-B2 — overlay lifecycle + ledger:** `jasper/dsp_overlay.py` +
  `jasper-dsp-overlay` (Pi side), bench `overlay`/`ledger` verbs,
  `describe_current_config` registration, contract tests (canonical
  files unreachable; config+volume single transaction; verified
  restoration; protection blocks untouchable).
- **PR-B3 — attribution + lexicon:** `attribution.py` + `lexicon.py`
  pure data + schemas wired into analyze/compare outputs and the report
  template (two-register grammar).
- **PR-B4 — protocol + packaging:** `docs/HANDOFF-tuning-bench.md` (the
  canonical agent entry, Codex-compatible), `.claude/commands/
  tuning-bench.md`, `docs/testing-tooling.md` rows, README/doc-map
  updates.

Sequencing: B1 can start immediately after the gate (no L/U
dependency). B2 follows B1. The bench sidesteps PR-L2's shelf-model trap
by emitting explicit-Q shelves and fitting refinements against measured
residuals regardless of L2's status. B1 shares its
calibration-sign fixture with PR-L1 when both exist. Acceptance for the
ladder as a whole: a fresh session on JTS3, driven only by the HANDOFF
protocol + CLI, reproduces the iLoud session's loop (compare → attribute
→ overlay → verify → restore) with zero hand-rolled code.

## 10. Scope fences

- **No implementation from this doc** before the review gate.
- **No in-product agent work** in the B-ladder; §8.2 is a recorded plan
  for a later session.
- **No shared disclosure framework now**; the bench writes in the
  grammar, extraction waits for the product consumer.
- **No delta-probe fork** (§8.4).
- **No new DSP math in the bench** — composition of the kernel only; a
  missing primitive is a kernel PR, not a bench reimplementation.
- **No phone-relay driving in v1** — product bundles are read-only
  inputs.
- **No re-litigation** of settled instrument decisions (parent-plan
  firewall) and no edits to the §2.4 active zones.
- **The owner's ear stays the final gauge** — the bench never
  auto-applies on a listening question; it applies, discloses, and asks.

## 11. Open items for review

1. **Naming:** `jasper-tuning-bench` / `jasper/tuning_bench/` /
   `docs/tuning-bench-design.md` (avoids the "correction"/"envelope"
   collisions). Confirm or rename.
2. **Seat-2 scheduling:** §8.2 is written for a future session — confirm
   it waits for the product-walkthrough design rather than starting
   after the B-ladder.
3. **Coordination flag for the U-ladder** (observed in audit, not bench
   scope): the express tier currently reaches no flatness sentence
   (`_flatness_details_lines` fires only on the cloud-verify phase that
   express omits) — worth confirming PR-U3's disclosure copy covers the
   §1.3 "absence is stated" promise.

Last verified: 2026-07-27
