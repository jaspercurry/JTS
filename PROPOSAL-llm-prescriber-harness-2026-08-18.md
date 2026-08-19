# Proposal — the LLM prescriber harness

> **Decision brief, 2026-08-18.** Read-only analysis against `origin/main` at
> `8f70d95bc`. Nothing was built. Answers step **C** of the post-flat-campaign
> sequence ([handoff](NEXT-SESSION-PROMPT-2026-08-18-post-flat-campaign.md) §6).
> A **deployment** brief on top of an existing design authority, not a new
> architecture.

---

## BLUF

**You already ratified this architecture, you already ship one instance of it,
and you already ran the loop by hand.**

1. **The design exists.** `docs/llm-native-tuning-workbench-plan.md`
   (2026-07-28) is the named planning authority. Its §5.0 already *decided*
   your "v0-simplest" shape: a CLI on the Pi, a laptop agent as an SSH client.
   It is **not built** — there is no `jasper/tuning_workbench/`.
2. **The whole pipeline you described already ships once — for *room
   correction*.** `correction/evidence.py:build_evidence_packet` assembles the
   blob; `calibration_agent/advisor_context.py` redacts it through field
   allowlists; `prompt.py:build_advisor_prompt_package` renders versioned
   instructions + a response contract; `response.py` validates what comes
   back; `actions.py` runs only bounded, approved actions. That is your v1,
   end to end. It simply does not exist for the crossover/linearization
   domain, and there is no import in either direction.
3. **v0 already ran.** Last night's jts3 round was driven end to end by 635
   lines of agent-written glue in `captures/xover-blenditer-2026-08-18/tools/`
   — `walk_driver.py` posts positions, `armrun_mint.py` mints sessions and
   attaches an `alignment_prescription`, `armrun_verdict.py` reads verdicts
   back.

So the question is not "should we build a harness." It is **which parts of the
harness we re-write every session deserve to be repo code.** That glue has a
copy lineage — `armrun_mint.py`'s docstring says "Same rule as
`s2drv_mint.py`" — the single-source-of-truth failure you would reject
anywhere else.

**Recommendation — two builds, in this order, nothing else yet:**

1. **A crossover evidence packet** — the direct sibling of
   `build_evidence_packet`, emitting one versioned JSON document per round.
   Every tier needs it, the *deterministic* trend engine needs it too, and it
   only reads. **Do not name it `export_bundle`** — that is taken by
   `correction/bundle_tools.py:export_bundle`, which writes REW `.frd`/`.wav`
   and is a registered CLI subcommand.
2. **A2, the excess-phase instrument** — your boost reframe makes it
   load-bearing (below), and it is already ranked #1 on the
   data-left-on-the-table list at zero hardware cost.

Defer the write side (a `propose` intake) until the packet has been used by
hand for a few rounds. Defer the paste tier until both exist.

---

## The loop, in one paragraph

A round runs (a human follows the mic-placement screen, or the turntable
drives the same seam); the speaker banks receipts, per-position flatness,
distortion, integrity counters and level metadata; the packet command
assembles that into one JSON document carrying the same honesty framings the
tools already print; an LLM reads it plus speaker/driver/room identity and
returns a JSON candidate; the speaker runs its **existing** deterministic
validators and either refuses with a machine-readable reason the model can act
on, or applies through the **existing** apply path; a verify round measures
the result and the next document carries it. The model proposes; the harness
disposes.

---

## What exists tonight

The blob is **mostly already written** — spread across a directory tree
instead of gathered into one document. Verified against last night's bundle at
`captures/xover-blenditer-2026-08-18/receipts/blend1/`.

| ingredient | owner | status |
|---|---|---|
| round verdict — `adoption`, `round_axes`, `round_measurements`, `verification`, `evidence_identities`, `proposal_fingerprint` | `crossover_v2/contracts.py:RoundReceipt.to_dict()` → `.../round_receipt.json` | banked |
| the honest per-round view — `curve`, `flatness`, `spec.bands[]`, `positions`, `null_registry`, `carve_outs`, `phase`, `trusted_floor_hz` | `.../cloud_verify.json` (37 KB) | banked |
| findings + **`field_descriptions`** (self-describing) | `.../findings_cloud_verify.json` | banked |
| per-position integrity — `gate_disclosure`, `gate_window_ms`, `glitch_detected`, `summed_ripple_db`, `wav_sha256` | `.../positions/*.json` | banked |
| session identity — `topology_fingerprint`, role→output map, mic `calibration_id`/`_sha256`, `build_sha`, `apply`, `rollback_target`, `compile_validation` | bundle `info.json` | banked |
| flatness per octave and per position (#2700) | `flat_spec_views.py` — `log_pooled_residual`, `role_split_flatness`, `directivity_table`; every level carries `not_evaluated_reason` | shipped; renderer `scripts/render-metric-views.py` is lab tooling, not installed |
| **first reflection, as a time** | `gating.py:detect_first_reflection` → `first_reflection_ms`, `reflection_onset_ms` in the `gating` fragment of every capture result | **shipped, per capture — the closest answer to what you asked for** |
| reflection clustering | `spatial_combine.py` → `geometry.median_tau_us`, `clustered_fraction`, `reason`; `echo_band_hz` + provenance | shipped |
| frame ledger / capture integrity | `frame_ledger.py:FrameLedger.to_dict()` rides `analysis.frame_ledger` on every capture | computed always; **persisted only under the lab dump gate** |
| artifact integrity + privacy class | `bundles.py:ArtifactEntry` (`sha256`, `generated_by`, `sensitivity`) | shipped |
| safety limits — commissioning SPL (one per preset, **not** per driver) and the per-driver hard rail | `profile.py:SafetyEnvelope` (65 / 85, validated 45–85); `DriverSpec` + `driver_protection.py` | shipped |
| external-driver seam for the human/turntable step | `POST /crossover/v2/position-ready` | shipped |
| externally-measured value as a bounded, provenanced candidate | #2692 — `crossover_v2/alignment_prescription.py` | shipped |
| harmonic distortion H2/H3 (#2702) | `distortion.py:HarmonicReading` | **computable, not banked** — no `to_dict()`; `scripts/harmonic-distortion-replay.py` is its only caller |
| Fc selection | `fc_selector.py:FcSelection` | **inert on shipped rounds** — lateral walk paused (#2717); "No stage-1 session feeds this today" |
| numeric mic **angle** per position | — | **absent** — only a role is banked; the lab recovers angle by parsing walk-driver log text |
| per-bin minimum-phase vs excess-phase class | — | **absent — this is A2** |
| reflection **distance**, room dimensions, placement, prefs | `runtime-context-schema.md` is a "proposed schema" with no writer | **absent, and partly by ruling** — `gating.py` derives a bound "never from assumed room geometry" (2026-07-31, #1966). Confirm before treating as a gap |
| **one command emitting all of this as one document** | precedent exists for the *other* domain (`build_evidence_packet`) | **absent for crossover — the gap** |

### The write side is nearly free, and it must not be a second prescriber

Three facts a builder needs, none of which is yours to decide:

- **The strict reader exists.** `blend_filters_from_mapping` already validates
  a filter list to hostile-data standard — rejects a `Mapping`, a `str`, a
  numeric-looking string, a `bool` (`gain=True` "would read as a +1 dB
  boost"), non-finite values, positive gain, and more than two filters. Its
  docstring names its own purpose: guarding "the point where data that left
  the process comes back into it." A pasted model response is that point.
- **#2692 wrote the pattern.** `alignment_prescription.py` = **provenance**
  (`basis_artifacts`, required, so a human can go check) + **a
  physics-derived bound** (`±half_period_us(fc_hz)` from the declared basis,
  not the incumbent) + **fail-closed at the one request boundary** + **fail-soft
  on read-back**. It enters at the *estimate*, so graph and prediction cannot
  describe different speakers, and is never inherited by a re-measure.
- **Apply cannot be the door, and that is correct.**
  `MeasuredCrossoverCandidate.fingerprint` is `field(init=False)` — a content
  hash the caller cannot set, re-derived on read and refused as
  `candidate_tampered`. A model-authored value enters **upstream at
  candidate-build time**, exactly where `alignment_prescription` does.

And the ownership call: `jasper/calibration_agent/` is live (console script,
OpenAI-only, strict JSON schema, spend-capped by `web/correction_tuning.py`),
and already solves several hazards below — closed `ALLOWED_ACTIONS`, a
recursive `_PROHIBITED_KEYS` blocklist, server-side re-validation on apply,
and `proposal_sim`, which runs the model's candidate through *the same
acceptance judge a real verify faces*. It just speaks room-correction PEQ.
**Extend it; do not build a rival.** (Two assumption corrections:
`docs/calibration-agent/**` is not read by the live path, only the offline
CLI; and the two domains bank to different roots with no shared format.)

---

## The three tiers

**S** = a day, **M** = a few days, **L** = a week-plus. All three reuse the
same validators; they differ only in who carries the JSON.

**v0 — Claude Code over SSH. Works today. Scope: S–M.** Adds nothing to the
product; what deserves building is the read side, itemised in "this week"
below. Leave candidate-minting ad-hoc — it is genuinely per-experiment.

**v1 — generate a prompt, paste the answer back. Scope: M.** A done-screen
action renders packet + identity + response format as pasteable text; a POST
takes the model's JSON back. The model never touches the speaker — **a human
is the transport.** The workbench plan says an untrusted-agent deployment
"would need a genuinely user-mediated channel the model principal cannot
write." The paste tier **is** that channel: it does not weaken the trust
boundary, it removes the model's transport authority and leaves only data
authority — so the hardening job is the **parser**, not the channel. It is M
rather than L only because `build_advisor_prompt_package` already does this
renderer's job for the other domain. Two easily-missed costs: the renderer
must not land in `jasper/web/correction_crossover_v2.py` (**8395 lines**, the
named god-file risk site), and the response POST needs a strict reader, never
a lenient `json.loads`.

**v2 — the model runs learning experiments. Scope: L.** Ratified in shape
already (workbench plan §5.5, over `set_active_config_raw()`, which loads a
graph live without moving the durable anchor). Under the reframe below this
tier is **how the boost evidence gets gathered** — no longer a distant nicety,
but it still follows A2 and the packet.

---

## The decisions

### Decision 1 — the evidence bar a warranted boost must clear

**Framing, per your ruling: cuts-only is the current law, not the permanent
one.** Adding energy is sometimes right. The job is not to defend a blanket
prohibition nor to carve a hole in it — it is to define **what a boost must
prove**, and to build the instrument that can prove it.

Two premise corrections. *First, cuts-only is region-scoped, not global:*

| region | boost | owner |
|---|---|---|
| the crossover/blend region | **0.0 dB — refused, not clamped** | `camilla_yaml.py:MAX_BLEND_CORRECTION_GAIN_DB`, plus 3 further independent gates |
| inside a driver's own band | **allowed, total uncapped**; per-filter cap 12.0 dB | `linearization_fit.py` (`FitVocabulary.allow_boost`), `MAX_LINEARIZATION_BOOST_DB` |

*Second, what actually blocks a null-targeting boost is the null-exclusion
gate — the condition on which boost was legalized at all.* `_lift_stage`
clamps lift per bin by `envelope.allowed_depth_db`, "already zero wherever the
interference-null registry or the position screen excluded a band. So boost
cannot fill a measured interference null." Your 2026-07-27 ruling:
"Null-exclusion stays as a measured fact (registry-gated)."

**The first-principles split the bar should encode.** A dip is not one thing:

- A **minimum-phase dip** — driver, box or baffle — is a genuine shortfall of
  radiated energy. Adding energy fixes it. JTS already boosts here.
- An **interference null** — direct sound cancelling a delayed copy — swallows
  whatever you feed it, because the boost lifts the reflection along with the
  direct sound. `audio_measurement/interference_nulls.py` formalizes this with
  a two-path depth ceiling of `20*log10((1+r)/(1-r))`.

Today the split is by *registry*: provable nulls are named, everything else is
unclassified. **The instrument that makes it per-bin is already queued and
needs no hardware** — A2 (§7a-1) reconstructs minimum phase from banked
magnitude and subtracts it from measured phase, which in the handoff's words
upgrades "the blanket 'no EQ at ~1 kHz' rule into an instrument."

**The proposed bar — five legs, four already built:**

| # | leg | instrument | status |
|---|---|---|---|
| 1 | the bin classifies **minimum-phase**, and is not in the null registry | A2; `interference_nulls.py` | **the one gap** |
| 2 | the dip is **stable across positions**, not a single-point artifact | cross-position screen + `variance_cap.py`, which names the failure: inverting "a feature which moves with the microphone manufactures ripple whose 'correction' is itself audible" | shipped |
| 3 | **bounded depth** — never more than the measured dip | `PER_FILTER_BOOST_CAP_DB` / `MAX_LINEARIZATION_BOOST_DB` | shipped |
| 4 | **headroom and hearing safety unchanged** — absorbed, not emitted into the ceiling | `total_positive_boost_db` → `active_baseline_headroom` (refuses past 40.0 dB); `SafetyEnvelope`; per-driver limiters | shipped |
| 5 | **verified by measurement after apply**, or put back | `delta_probe.classify_delta_probe`; `DELTA_PROBE_ROLLBACK_VERDICTS` roll back automatically | shipped |

**What needs your ruling — the numbers and the sequencing.** Proposed:

- **Depth: open the new class at ≤3 dB**, well under the 12 dB established
  boost already enjoys, and never more than the measured dip. A new permission
  should not open at the old permission's ceiling.
- **Stability: the dip must appear at every measured position but one**,
  reusing the existing screen's tolerance rather than inventing a second.
- **Sequencing:** (1) build A2; (2) bank its per-bin class into the packet, so
  *both* prescriber options see it; (3) run bounded, **auto-reverted probe
  rounds** — "does energy into this dip hold at all positions?" — through the
  existing apply/verify/delta-probe loop with adoption pinned to restore;
  (4) only then land the boost design, with these numbers, through the normal
  review gates.
- **Until (4) lands, cuts-only stays the shipped contract in the blend
  region** — amended on evidence, not on argument.

The experiment tier is therefore not a carve-out from safety; it is **the
sanctioned way to gather the evidence the bar demands.** Its plumbing is
nearly free: `crossover_v2/coordinator.py` already runs capture → plan → apply
→ verify → adopt as normal operation, so a probe round is that loop with
adoption pinned. The new engineering is the permission layer and leg 1.

### Decision 2 — does the paste tier become a product feature?

v0 sends measurement data to a model **you** are already talking to. v1 makes
"send this household's measurements to a third-party frontier model" a feature
the product offers a stranger. That is a product and privacy commitment, not
an engineering call.

- **Option A — the harness stays a lab/owner tool.** Build the packet and the
  promoted position driver; drive from Claude Code over SSH. No end-user
  surface, no new disclosure, no new attack surface.
- **Option B — ship the paste tier to households.** Needs the strict intake,
  the renderer kept out of the god file, and a `PRIVACY.md` model-transfer
  disclosure that the workbench plan §14 assigns and nobody has written.

**Recommendation: A now; B only once the loop earns it.** A is a strict
prerequisite for B anyway. The trigger for B should be evidence from A — if
model-in-the-loop rounds beat the deterministic prescription, the feature has
earned its disclosure; if not, you saved the build.

---

## Hazards and mitigations

The workbench plan §11 risk table still applies and is not restated. Five that
matter here:

1. **The pasted response is hostile data.** Schema-only parsing, a byte cap via
   `_common.read_json_object(..., max_bytes=)`, and no string from the response
   reaching a shell, filename, YAML document or log format string — the model
   supplies **numbers into a fixed shape**. The posture already exists:
   `_handle_crossover_v2_position_ready` refuses `int(1.5)` and `int(True)`
   because they "would silently coerce a malformed index into a VALID one", and
   `_PROHIBITED_KEYS` is a blocklist worth reusing.
2. **Prompt injection into the generated prompt.** The packet carries
   household-authored strings. Keep it data-only: no instruction text assembled
   from banked fields, free text excluded or fenced. A model that ignores the
   response format is a parse failure, not a security event.
3. **The packet must carry its own honesty.** The banked data already does —
   `spec.bands[]` carries `evaluable`/`n_excluded`/`graded_lo_hz`, `geometry`
   carries `reason` rather than a fabricated number, `findings_*.json` ships
   `field_descriptions`, the views carry `not_evaluated_reason` at every level.
   Copy these **verbatim**, never flatten a `not_evaluated` into a zero, and add
   a test that fails the build if the packet grows a summary the underlying view
   refuses to publish — the rule `tests/test_flat_spec_views.py` already
   enforces (no view may grow a `passed` field).
4. **Secrets and PII.** Serials are already safe: `household_mic.py` keeps a
   one-way `serial_hash` and a last-4 display, never the full serial. For the
   rest, use the existing `sensitivity` vocabulary plus `advisor_context.py`'s
   allowlist pattern — default to `derived` + `config` + `debug_safe`, require a
   flag for anything else, reference raw WAVs by `sha256`. `PRIVACY.md` still
   owes the model-transfer disclosure.
5. **The refusal has to be readable — today it often is not.** A real gap:
   `AlignmentPrescriptionRefused.reason` is a closed slug set but the catch site
   **interpolates it into prose** and leaves `CrossoverV2Refused.code` unset;
   `blend_filters_from_mapping` returns `None` with no reason;
   `_validated_blend_correction` raises free text. Only
   `MeasuredCrossoverCandidateError(code, detail)` and `failures.py`'s closed
   `FAILURE_CODES` do it properly. Thread the existing slug through instead of
   interpolating it. Without this, "the speaker tells the LLM why" degrades to
   "the LLM guesses from prose."

---

## What v0 could do this week

Needs **zero new product code**, because it happened last night.
`GET /crossover/envelope` (schema v14) is the state machine's whole truth and
carries the mic-placement prose as JSON in `verdict_text` — an agent can relay
instructions to a person verbatim, no HTML scraping. `armrun_mint.py` posts
`/crossover/v2/session` using the envelope's own minted action;
`walk_driver.py` drives positions via `/crossover/v2/position-ready`;
`armrun_verdict.py` reads verdicts the two-layer way;
`harmonic-distortion-replay.py` and `severed-twin-replay.py` re-analyse banked
captures with no hardware, binding captures to sessions by `wav_sha256` rather
than filename; `/crossover/v2/apply` and `/crossover/v2/restore` close the
loop.

**One premise correction before you plan around it:**
`POST /crossover/v2/complete` is **not on `origin/main`** — the POST allowlist
has `session`, `verify`, `apply`, `restore`, `decline`, `position-ready` and no
`complete`. That route lives on the unmerged branch
`origin/claude/w2b-wired-capture-source` (the #2662 wired-capture work), where
it is the wired session's all-spots-measured confirmation. On plain `main` it
404s.

So the honest list is **promotion, not features**:

| do | scope | why now |
|---|---|---|
| the crossover **evidence packet** — walk a round's tree, emit one versioned JSON document, allowlist-redacted, honesty fields verbatim | **M** | the one real gap; serves v0/v1/v2 *and* the deterministic trend engine; `correction/evidence.py` + `advisor_context.py` are the template |
| **A2** — the excess-phase / minimum-phase per-bin split | **M** | leg 1 of the boost bar; zero hardware; already ranked #1 on the data-left-on-the-table list |
| promote `walk_driver.py` into repo code | **S** | it holds real safety invariants in a captures directory |
| carry `first_reflection_ms` (and `median_tau_us` + `reason`) into the packet | **S** | the first-reflection fact you asked for already exists per capture and is easy to miss |
| thread refusal slugs instead of interpolating them | **S** | hazard 5; without it the loop cannot self-correct |
| bank a numeric `angle_deg` per position | **S–M** | today only a role is banked; the lab recovers angle by parsing log text |

A caution for whoever builds it: the natural home for a "generate the prompt"
button is `jasper/web/correction_crossover_v2.py`, already 8395 lines. The
packet belongs behind its own owner with a CLI, per workbench plan §5.0.

---

## Non-goals

- **No auto-adoption.** A model-authored candidate is an ordinary candidate:
  same acceptance, safety-envelope, variance and verify gates, and the round's
  own `adoption` verdict decides keep or restore. The model never grades itself.
- **No second validator, no second prescriber, no second capture stack.** The
  intake reuses the existing strict readers and apply path; new actions extend
  `jasper/calibration_agent/`; the relay/phone/room flow is untouched and the
  wired-mic source (#2662) proceeds on its own track.
- **No new DSP math and no new manifest envelope.** The packet is an ordinary
  neutral evidence bundle, and must not reuse the name `export_bundle`.
- **No room-geometry inference.** Reflection *time* is measured and banked;
  turning it into a distance would contradict the 2026-07-31 `gating.py`
  ruling — a question for you, not a gap to close quietly.
- **No weakening of the shipped contract by this document.** Cuts-only remains
  law in the blend region until a boost design lands through the normal gates.
- **Not a replacement for the deterministic option.** The trend engine remains
  live; every read-side piece here serves it identically, which is the point.
- **Not built this session.** Read-only analysis; `origin/main` unchanged.
