# FACTS — wave 4 (bass evidence and runtime), seat-matched tuning program #4502

**Verified at `origin/main` `a1994ef69` (2026-09-09 09:48 EDT / 13:48 UTC merge of
PR #4636).** All `file:line` citations below are against this exact SHA unless
marked otherwise. Read via a throwaway local clone
(`/tmp/.../scratchpad/JTS-main`, hard-linked from `/home/user/JTS`, checked out
to `a1994ef69`); `/home/user/JTS` itself was not modified. `git fetch origin`
was run first; `origin/main` did not move during this session.

Read-only fact-gathering only. No design, no recommendations below.

---

## 0. THE HEADLINE FALSE PREMISE — wave 3 has not landed on `origin/main`

Every "wave 4" row in `PLAN.md` §5 is gated on wave 3 ("Wave 4 — bass evidence
and runtime (lane D, **after 3**)", §6a run-order step 5: "**Wave 4** after 3.x
and 4.1"). **At `a1994ef69`, none of wave 3's rows have merged:**

- `grep -n "GRAPH_SCOPES" jasper/active_speaker/crossover_v2/measure_spec.py:125`
  → `GRAPH_SCOPES = (GRAPH_SCOPE_DRIVERS, "base", "speaker_tune", "candidate",
  "room_candidate")`. **No `"bass_candidate"` scope exists** (row 3.3's
  deliverable). Zero repo-wide hits for the string `bass_candidate`.
- `jasper/cli/bass_extension_bench.py:181-190` (`_run_live`) still raises
  `SystemExit("live bench execution requires binding each target's TargetPlan
  from the household's confirmed bass-extension profile and the on-device
  PlayAndCapture collaborator ... — neither is wired here yet")` — the exact
  refusal row 3.1 exists to remove. `PlayAndCapture` is still an unimplemented
  `Protocol` (`jasper/bass_extension/bench/executor.py:148`).
- `jasper/bass_extension/adapters/sealed.py:141` (`fit_plant`) still hard-requires
  `CaptureRole.WOOFER_NEARFIELD` — ADR-0260's "`fit_plant` is re-pointed at the
  median" (row 3.2) has not happened.
- No protection-ladder module, no `bass-fit`/`bass-ladder` CLI view, no
  Layer-2 scheduled-candidate kind anywhere in `jasper/`.
- `git log --oneline --all --grep="row 3\."` and `--grep="row 4\."` on this
  clone return **zero commits** (contrast with wave 1/2, whose "row 1.1" …
  "row 2.3" commit trailers are all present and squash-merged — `82e82a03`,
  `c36dd0ce`, `7ef2adbe`, `d85d0815`, `b083c06c`, `7db6bca7`, `d7d5fdc1`, etc.).
- Corroborating (not authoritative — local, off-`origin/main` state):
  `/home/user/wt/3-3` is a live worktree on unmerged branch
  `claude/seat-w3-3-3-bass-candidate-kind`; `/home/user/wt/PRE-3-4.md` (an
  orchestrator pre-review dated after this session's fetch) independently
  confirms the same branch-not-merged topology and puts `origin/main`'s tip at
  `f28148e0`, an ancestor of this report's `a1994ef69`.
- `PLAN.md`'s own §9 status log (read at
  `origin/claude/loudspeaker-tuning-architecture-iephfa`, not `origin/main`)
  says the same in its own words: "Nothing from lane D can land in this
  state" (2026-09-09 11:40Z) and lists eight lane-D branches, 160–208 commits
  behind main, **none opened as a PR**.

**Consequence for a wave-4 brief:** row 4.2 ("runtime scheduler … patching the
named rung filters") has no rung filters to patch (no scheduled-candidate
kind, §0 above), and row 4.3 ("first production caller … apply of a scheduled
candidate") has no scheduled candidate to apply. Both rows' stated proof and
gate presuppose wave-3 artifacts that do not exist at this SHA. Only row 4.1
(the limiter bench campaign) rests on machinery that already exists
independent of wave 3 — see §1–§2.

---

## 1. The limiter bench as it stands at HEAD

### 1.1 `jasper/bass_extension/bench/` — module inventory (line counts, `wc -l`)

| Module | Lines | Owns (one line, from its own docstring) |
|---|---:|---|
| `__init__.py` | 35 | Package map/overview; states the campaign is bench-only, fail-closed, no production wiring. |
| `context.py` | 66 | Trusted limiter-domain constants (`LIMITER_DOMAIN_MIN_DBFS=-120.0`, `LIMITER_DOMAIN_MAX_DBFS=0.0`, `context.py:51-52`) bound to the emitter's own `-120..0` `clip_limit` validation. |
| `excitation.py` | 85 | Maps one manifest `StimulusRequest` onto the existing `prepare_driver_excitation_plan` — composes, never reimplements, admission. |
| `sink.py` | 107 | Bundle artifact sink: bytes/JSON → content-addressed `ArtifactIdentity`; the package's one I/O surface. |
| `analysis.py` | 152 | Campaign pass/fail verdicts, composed from existing measurement kernels (`tracking_error_db`, etc.); thresholds come from `MarginPolicy`, never invented here. |
| `live_proof.py` | 250 | R6a ingress-transparency + R4(a) fader-bracket pure predicates over already-fetched status payloads. |
| `stimulus.py` | 285 | R6 runner-owned lead-in/lead-out silence padding around existing stimulus generators, without modifying them. |
| `bundle.py` | 292 | Pure emitter shaping the frozen bundle schema from `limiter-evidence-protocol.md`; writes no files. |
| `activation.py` | 297 | The fail-closed temporary-graph-activation seam (never writes the on-disk config; `patch_config` for `clip_limit`, `reload()` to restore). |
| `cross_check.py` | 353 | R10 live cross-check: rendered post-limiter peak vs. `get_playback_peak_all()`, fail-closed. |
| `manifest.py` | 364 | Operator-authored `campaign_manifest`; pure; refuses on any missing operator-authorized input, never defaults. |
| `runner.py` | 638 | Campaign orchestrator: manifest → activation → measurement collaborators → bundle emitter; owns only sequencing. `TargetPlan` dataclass at `runner.py:84`. |
| `derivation.py` | 732 | Pure config derivation for the offline pre/post-limiter tap render (R1–R3, R7). |
| `render.py` | 822 | Binary resolution (R5) + bounded-subprocess offline render (R8/R9); does not import `dsp_apply`. |
| `executor.py` | 1,200 | The real on-device `RoleExecutor`; defines the still-unimplemented `PlayAndCapture` Protocol at line 148. |
| **Total** | **5,678** | |

### 1.2 `jasper/cli/bass_extension_bench.py` (195 lines) today

- Entry point registered at `pyproject.toml:204`:
  `jasper-bass-extension-bench = "jasper.cli.bass_extension_bench:main"`.
- Default posture (no flag, or `--dry-run`): loads the operator's manifest-inputs
  JSON, validates `margin_policy_name` against `MARGINS`
  (`jasper/bass_extension/targets.py:30`, keys `conservative`/`normal`/…),
  calls `author_campaign_manifest`, prints the plan (`_print_plan`,
  `bass_extension_bench.py:64-72`: margin, target ids, `STIMULUS_ROLES`, trusted
  limiter domain, bundle kind), and returns — **opens no device, socket, or
  CamillaDSP connection** (`bass_extension_bench.py:122-124`).
- `--live` (`bass_extension_bench.py:93-102,126,181-190`): resolves the render
  binary (`resolve_render_binary`, R5), installs a `SIGINT` → `Stop()` handler,
  then **unconditionally raises `SystemExit`**: "live bench execution requires
  binding each target's `TargetPlan` from the household's confirmed
  bass-extension profile and the on-device `PlayAndCapture` collaborator … —
  neither is wired here yet." (`_run_live` docstring, `:129-154`, cites GitHub
  issue #1738, "Bass extension: final bench-campaign binding.")
- Never wires the pure evidence producer into a runtime path, never persists a
  profile, calls no profile writer (module docstring, `:5-21`).

### 1.3 `limiter_evidence.py` (1,213 lines) — no production caller, grep-proved

- Module docstring (`limiter_evidence.py:7-10`): **"This module deliberately
  has no production caller."** — stated at module level, not on any one
  function.
- `produce_limiter_thresholds(evidence, *, required_context)`
  (`limiter_evidence.py:1194-1213`) is the sole public entry point; its own
  docstring is unremarkable ("Return measured thresholds or one typed,
  deterministic refusal") — the "no production caller" language lives only in
  the module docstring, not attached to this function.
- Grep-proved zero non-module, non-test callers repo-wide for
  `produce_limiter_thresholds`, `LimiterThresholdSet`, `LimiterEvidenceRefusal`,
  `LimiterRefusalReason`, `TargetLimiterThreshold`. The only importers are the
  module itself and five test files: `test_bass_extension_bench_executor.py`,
  `test_bass_extension_limiter_protocol.py`,
  `test_bass_extension_limiter_evidence.py`, `test_bass_extension_bench_runner.py`,
  `test_bass_extension_bench_bundle.py`.
- Not fully isolated at the *submodule* level, though: `bench/render.py`'s
  `RenderError`/`extract_channel`/`resolve_render_binary` and
  `bench/derivation.py`'s `ALLOWED_FILTER_TYPES` are imported by
  `jasper/active_speaker/bench/compare.py:64,127,211`,
  `jasper/active_speaker/branch_peak.py:35`, and
  `jasper/cli/active_speaker_emit_bench.py:65` — pure helpers already reused
  by the (unrelated) active-speaker emit-bench tooling. The **pure-producer
  module** `limiter_evidence.py` itself has no such caller; the bench
  **package** is not hermetically sealed.

---

## 2. What the limiter-evidence protocol and tap-realization amendment freeze

Both docs (`docs/bass-extension-waves/limiter-evidence-protocol.md`, 513
lines, protocol revision `2026-07-19b`; `limiter-tap-realization.md`, 1,030
lines, accepted 2026-07-24, "Rev 7 (errata)" is the latest changelog entry)
are still present verbatim at HEAD and still carry no status banner beyond
what's quoted below — I read both in full per the task's instruction.

### 2.1 Bundle required contents (`limiter-evidence-protocol.md` "Replayable
accepted bundle")

Root has exactly `kind`, `schema_version`, `protocol_revision`,
`evidence_fingerprint`, `measured_context`, `campaign_manifest`,
`retained_facts`, `targets`. `measured_context` (and the separately-supplied
`required_context`) has exactly 15 named fields including
`limiter_domain_min_dbfs`/`max_dbfs`/`fingerprint` ("trusted outputs of a
reviewed context builder bound to … `emit_active_speaker_baseline_config`
limiter-range validation," not manual values), `camilladsp_build_id`,
`tap_implementation_id`, `detector_reference` (frozen exact string
`instantaneous_float_sample_peak_dbfs_re_unity_at_limiter_input`). Each
`target` is a strict discriminated union: an early-stop arm
(`refused`/`aborted` + `stop_receipt` + `partial_artifacts`) or an `evaluated`
arm carrying a `discovery_activation_receipt`, `candidate_sources`, a
`discovery_restoration_receipt`, and `candidates_least_to_most_permissive`
(each candidate: `limiter_threshold_dbfs`, activation/restoration receipts,
`digital_transfer_probe`/`sweep_transparency`/`sustain_stress` records,
disposition `accepted`|`limiter_transparency_failed`).

### 2.2 Acceptance rules

Every candidate/source peak must lie inside the trusted closed limiter domain
and at or below `baseline_limiter_clip_limit_dbfs`; candidate settings are
strictly increasing in stored order, non-decreasing from deepest target
through natural (a deeper target may never be more permissive than a
shallower one — violation is `out_of_envelope`); an `accepted` candidate
requires transfer pass + quality pass + protection pass + transparency pass +
`digital_clamp_passed` true + exact activation/source/restoration bindings;
`ordered_owner_chain` must contain `bass_ext_lt`, then `bass_ext_subsonic`,
then the context's `limiter_name`, in that order. The producer stops at the
first `accepted` candidate, advancing past only `limiter_transparency_failed`
candidates whose other verdicts passed.

### 2.3 Replay contract

`produce_limiter_thresholds(evidence, *, required_context)` is pure — no I/O,
no clock, no defaulting — total over its JSON input domain (missing → `missing`,
type/shape wrong → `inconsistent`, `measured_context != required_context` →
`stale`, envelope/ordering/domain violations → `out_of_envelope`, in that
precedence). The pre-production fence (protocol doc, "Pre-production fence")
states only `limiter_evidence.py` and its one test file may implement this
skeleton, and it "must have no import or call from
`jasper.bass_extension.__init__`, `apply_bass_extension`, a graph emitter,
daemon, web backend, or other production path" — verified true at HEAD (§1.3).

### 2.4 The gap the protocol names, precisely

Protocol doc, "Replayable accepted bundle": *"The context builder is not
present in this revision, so production wiring remains blocked rather than
reconstructing the emitter's domain here."* At HEAD, `bench/context.py`
**does now exist** and its `LIMITER_DOMAIN_MIN_DBFS`/`MAX_DBFS` constants
(`-120.0`/`0.0`) are literally identical to the emitter's own validated bound
(`jasper/active_speaker/camilla_yaml.py:2168-2170`: `if limiter_clip_limit_db
< -120 or limiter_clip_limit_db > 0: raise ...`) — so the context builder the
protocol wanted has been built. **What still does not exist is any code that
feeds a real bench-produced bundle plus this context into
`produce_limiter_thresholds` as a production call** — §1.3 shows zero such
callers — and the doc's own "Wave 4 production remains blocked until Jasper
produces a real accepted bundle, the bundle and replay pass independent
review at zero Blockers and zero Should-fixes, and a later Wave 4 contract
revision names its exact fingerprint and authorizes a trusted caller" is still
true verbatim: no such contract revision exists in `docs/adr/` or elsewhere at
HEAD.

### 2.5 Tap realization — what the tap is, where it sits, what must never change

The tap is **not a live probe**: `limiter-tap-realization.md` (accepted
amendment) establishes that "A live tap is not realizable" — `CamillaController`
exposes no interior-stage PCM read and CamillaDSP drives one playback device
per process. Instead, pre/post-limiter artifacts for **all three** stimulus
roles are produced by **deterministic offline re-renders** through the exact
deployed `camilladsp` binary (file→file, no hardware), from **derived configs**
that are proof-carrying truncations of the *proved live graph* for that pass
(R1–R10). Fixed points that must never change:
- **R2**: the truncation boundary is exact — `names[:i]` (pre-limiter,
  limiter name absent) / `names[:i+1]` (post-limiter, limiter name present),
  where `i` is the limiter's index in the live owner step's `names`; any other
  length refuses.
- **R3**: only `enable_rate_adjust→false` and `devices.playback.format` (set to
  the receipted processing precision, `F64_LE` for the pinned v4.1.3 build) may
  differ on the device block; every filter/mixer/pipeline key is unchanged;
  `devices.capture.channels` is validated then **deleted** (not retained) from
  the derived capture block.
- **R5**: the render binary must be the exact one `jasper-camilla.service`
  is running (`v4.1.3` pinned); `JASPER_CAMILLADSP_BIN` is ignored and a
  set-but-different value refuses the whole campaign.
- **R7**: the owner path admits only `Biquad`/`BiquadCombo`/`Conv`/`Delay`/
  `Gain`/`Limiter`/`Mixer`; any other stage type, or any `bypassed` step,
  refuses.
- **R10**: the live cross-check — the render's post-limiter peak vs.
  `CamillaController.get_playback_peak_all()` — is "the only rule that binds a
  render to reality; it is not optional," fail-closed on disagreement, an
  unavailable reading, or an absent tolerance.
- Explicitly **not authorized** by this amendment (its own "What this does NOT
  authorize" section): "No production wiring of `produce_limiter_thresholds`;
  no profile writer; no scheduler; no live per-stage tapping attempts; no
  CamillaDSP fork or patch; no new daemon, route, socket, timer, or unit."

---

## 3. The runtime path a scheduler would use

### 3.1 `jasper/camilla.py` (1,189 lines)

**`_coerce_main_volume_db`** (`camilla.py:143-165`) — the exact clamp:

```python
clamped = max(MIN_MAIN_VOLUME_DB, min(MAX_MAIN_VOLUME_DB, value))
if clamped != value:
    log_event(logger, "camilla.main_volume_clamped", level=logging.WARNING,
              requested_db=value, clamped_db=clamped)
```

`MIN_MAIN_VOLUME_DB = -150.0` (`camilla.py:38`);
`MAX_MAIN_VOLUME_DB = DEFAULT_VOLUME_LIMIT_DB` (`camilla.py:39`), and
`DEFAULT_VOLUME_LIMIT_DB = 0.0` (`camilla_config_contract.py:141`) — so
`MAX_MAIN_VOLUME_DB` is `0.0`: the exact non-negotiable-#1 ceiling. Docstring:
"CamillaDSP itself can accept positive gain unless the loaded YAML has
`devices.volume_limit` set. This wrapper is the runtime defense-in-depth
boundary for every Python caller." **`set_volume_db`** (`camilla.py:666-686`)
calls this coercer then `c.volume.set_main_volume(target)` inside `_call`.

**`patch_config`** (`camilla.py:964-1004`) wraps CamillaDSP 4.1's generic
`PatchConfig` query verbatim (`c.query("PatchConfig", arg=patch)`) — pyCamillaDSP
has no first-class wrapper for it in the pinned version. Docstring states the
contract precisely: **"Serialized but NOT ducked"** — the duck exists only for
structural swaps that can move total graph gain by tens of dB; a patch writes
declared parameters of filters *already running*, so no duck; **"That safety
is a property of the CALLERS, not of `PatchConfig`"** — every caller must
bound what it patches (a clamped trim, one limiter value) and never patch
anything structural. It still serializes under `self._graph_mutation(...,
duck=False)` → the global DSP writer lock (below). No rate limiting beyond the
lock itself and `CAMILLA_ATTEMPT_BUDGET_S = 5.0` (`camilla.py:49`), a per-call
timeout/composite-attempt budget, not a scheduler throttle.

**R1 vs. what `patch_config` supports today:** the plan's R1 mechanism
(`docs/HANDOFF-bass-extension-plan.md:1013-1024`, "§8.3 The transition
mechanism") is "stepped `PatchConfig` on the live sealed filter pair
(confirmed)" — interpolate `(fp, Qp)` in 4–8 steps over 0.5–1 s, "Precedent:
`runtime_balance.py` patches a named Gain live today." At HEAD, `patch_config`
is a fully generic pass-through — it accepts *any* dict patch; nothing in it
is bass-specific or step-aware. The generic mechanism already exists and is
already exercised in production for exactly one narrow case:
`jasper/multiroom/runtime_balance.py:86-99`
(`camilla_patch_for_trim`) builds `{"filters": {PAIR_BALANCE_FILTER:
{"parameters": {"gain": trim, "inverted": False, "mute": False}}}}` and
`runtime_balance.py:163` calls `camilla.patch_config(camilla_patch_for_trim(trim),
best_effort=True)` from `apply_local_trim`. Grep-proved production callers of
`.patch_config(`: `crossover_v2/session_graph.py:338` (measurement-session
graph changes), `multiroom/runtime_balance.py:163` (the Gain-trim precedent),
`bass_extension/bench/activation.py:263` (bench-only, patches `clip_limit`
during the limiter campaign — not production). **No stepped/interpolated R1
sequencer exists anywhere in the repo** (grepped for `instant_retreat`,
`gated_re_extend`, `bass_scheduler`, `listening_level_scheduler` — zero hits) —
row 4.2 would build it from scratch on top of this one generic, unstepped
`patch_config` primitive.

### 3.2 Where the applied graph lives and who may rewrite it

**The single global writer lock**: `jasper/dsp_apply.py`'s `_dsp_apply_lock`
(`:499-599`) is an advisory file lock at `CANONICAL_DSP_WRITER_LOCK_PATH`,
re-entrant per-`asyncio.Task` (`:517-524`), and gates every mutation on one
extra check: if `BASS_EXTENSION_APPLY_INTENT_PATH`
(`/var/lib/jasper/bass_extension_apply_intent.json`,
`jasper/bass_extension/__init__.py:8-10`) exists, it raises
`BassExtensionApplyPending` and refuses the mutation outright (`:518-522`,
`:578-581`) — even on re-entry. `camilla_graph_mutation`
(`dsp_apply.py:647-660`) is the async-context-manager wrapper every writer
uses; `CamillaController._graph_mutation` (`camilla.py:759-789`) is the
controller-side counterpart that additionally ducks the main fader across a
structural swap (`duck=True` default; `patch_config`/`set_active_config_raw`
pass `duck=False`). "Instant retreat" (any bass-scheduler write) would have to
go through this exact same lock and the same `BassExtensionApplyPending` gate.

**`apply_intent.py` (168 lines)**: `ApplyIntent`/`decode_apply_intent`
(`:23-111`) is the durable two-phase-commit record's pure decoder, still
called from exactly one production site,
`jasper/active_speaker/runtime_contract.py:3843`
(`_snapshot_profile_summary`, used to classify authority validity if a
leftover intent file is found). **`_intent_payload`** (`apply_intent.py:136-168`,
the writer-side encoder) has **zero callers anywhere in the repo** — grep-proved.
This is because the only caller that ever built an intent record and wrote it
to `BASS_EXTENSION_APPLY_INTENT_PATH` was the apply/bypass/recover transaction
in `bass_extension/__init__.py`, which PR #4563 ("Retire the bass wizard and
the parked apply pathway," row 1.3/1.4) deleted — `bass_extension/__init__.py`
is now 10 lines (`BASS_EXTENSION_RUNTIME_ADAPTER_IDS`,
`BASS_EXTENSION_APPLY_INTENT_PATH`, nothing else). **Nothing in production
writes an apply-intent file today**; the read-side machinery
(`dsp_apply.py`'s gate, `runtime_contract.py`'s classifier,
`bass_extension/profile.py:524-536`'s `/state`
`apply_recovery_required` flag) all still check for the file's *existence*,
but it can only appear from a stale/manual leftover, never from a live
write path. Row 4.3's scheduled-candidate apply would be the first thing in
years to need this machinery live again (or would bypass it entirely — an
open design question, not a fact to report further here).

### 3.3 Existing precedent for patching a live filter's parameters

Yes — see §3.1: `runtime_balance.py`'s pair-balance trim already does exactly
this today, live, in production, patching one named `Gain` filter's
`gain`/`inverted`/`mute` parameters via `patch_config`. No stepped/ramped
version exists.

### 3.4 Latency / period-size budget

Not separately re-derived here beyond what the docs already state:
`docs/HANDOFF-bass-extension-plan.md:1042-1046` — "Both mechanisms add zero
buffer latency (minimum-phase IIR; no chunk/queue/rate changes), so the 40 ms
USB cert is untouched." CPU: "≤ 4 extra biquads (R1) … far below 1% of a Pi 5
core." I did not find any period-size constant specific to a bass scheduler in
`jasper/` at HEAD (none exists — there is no scheduler code yet, §0).

---

## 4. Where listening level is known at runtime

Two **different**, non-interchangeable "level" concepts exist in this tree.
Reading the task's phrasing against the code, this distinction is itself a
fact worth flagging up front:

**(a) The live user volume knob** — `jasper/volume_coordinator.py`'s
`VolumeState` (`:157-206`): `listening_level: int` (0–100, persisted; "the
level to restore after a temporary mute," `:164`), `effective_percent`
property (`:192-194`, `0` while temporarily muted). `db_to_percent`/
`percent_to_db` (`jasper/volume_curve.py:98`, `DEFAULT_VOLUME_FLOOR_DB = -50.0`
at `:26`) convert this 0–100 scale to/from `main_volume_db`. `/state` exposes
both: `jasper/control/state_aggregate.py:1058-1059` —
`"main_volume_db": camilla["main_volume_db"], "listening_level_percent":
listening_level` (fed from `_read_persisted_volume`, `:555-567`, which reads
`VolumeState.from_record(record).effective_percent`). **This is the figure a
bass scheduler would key on** — it is already computed, already 0–100, already
surfaced on `/state`, and `jasper/bass_extension/targets.py` already has a
convention for consuming it (below). No live code currently reads this figure
*for bass-extension purposes* — nothing subscribes to volume changes to drive
a bass family selection (§0/§3: no scheduler exists).

**(b) The one-time measurement-session calibration constant** —
`jasper/active_speaker/seat_level_reference.py` (392 lines). Its own docstring
(`:5-27`) is explicit about scope: it is **not** a live listening-level signal.
It is "the measured seat-SPL reference volume" used to derive "the crossover
session's fixed measurement volume" (`session_volume_plan.py`'s
`min(reference, max(driver caps))`), replacing the codified guess
`MEASUREMENT_REFERENCE_VOLUME_DB = -20.0`. Ownership is deliberately narrow:
**one writer** (`seat_level_ramp.py`, after a closed-loop SPL ramp), **one
reader** (`session_volume_plan.measurement_reference_volume_db`), and
"absent is normal" (never having run the leveling step just falls back to the
`-20.0` default). `SeatLevelTarget` (`seat_level_reference.py:102-146`) is the
frozen `(low_db_spl, high_db_spl)` band the operator requests for *that one
measurement run*; it is not stored and is not a speaker property. **This
module has no relationship to a runtime bass scheduler** — it calibrates one
number used once per measurement session, not a continuously-read "how loud
is it right now" figure.

**The bass family is already keyed on the *live* figure (a), by convention,
today** — in the not-yet-emitted profile schema: `jasper/bass_extension/targets.py`
has `TargetPoint.max_listening_level: int` (`:95`, validated `0 <= … <= 100` at
`:121`), converted to dB via the SAME `percent_to_db` used by the volume
system (`targets.py:154-155,166`, `floor_db=DEFAULT_VOLUME_FLOOR_DB`).
`jasper/bass_extension/profile.py:162,171-174` parses/round-trips this field;
`profile.py:310-313` similarly validates a `clean_ceiling.listening_level`
field (`0-100` int). So the mapping the task asks about ("does anything
already map volume to a listening-level figure the bass family could be keyed
on") is **yes, at the data-model level** — the profile schema already speaks
in the 0–100 `listening_level` units the live volume system uses — but **no
runtime code reads the live `listening_level` and selects a rung by it**; that
selection function is exactly what row 4.2 (not landed) would add.

---

## 5. The emitter's headroom accounting (for wave 5.1)

**Function**: `_emit_baseline_filter_definitions`
(`jasper/active_speaker/camilla_yaml.py:1839-1939`), called from exactly one
site, `emit_active_speaker_baseline_config`
(`camilla_yaml.py:3550-…`, the call at `:3675`).

**Callers of `emit_active_speaker_baseline_config`** (production, grep-proved):
`jasper/active_speaker/baseline_profile.py:2789`
(inside `build_baseline_profile_candidate`, `:2026`) and `:3268` (inside
`recompose_applied_baseline_yaml`, `:3105`); `jasper/active_speaker/
measured_crossover_candidate.py:854`; `jasper/active_speaker/bench/loop.py:479,485`
(bench-only, two variants for a differential check).

**Exact charge formula** (`camilla_yaml.py:1896-1912`):

```python
trim_db = max(0.0, output_trim_db)
total_headroom_db = (
    baseline_headroom_db
    + total_positive_boost_db(room_peqs)
    + linearization_headroom_db(linearization, branch_context=...)
    + trim_db
)
```

with `baseline_headroom_db` defaulted to `BASELINE_HEADROOM_DB = 0.0`
(`camilla_yaml.py:130`) and a hard ceiling `MAX_PROGRAM_HEADROOM_DB = 40.0`
(`camilla_yaml.py:1221`) that raises `ActiveSpeakerConfigError` if exceeded
(`:1913-1919`). The result is negated into one `Gain` filter named
`active_baseline_headroom` (`:1920-1926`), emitted pre-split on channels
`[0, 1]` (`_emit_baseline_pipeline`, `:1969-1988`), ahead of every crossover,
limiter and tweeter high-pass.

**What is charged today**: room-correction PEQ boost
(`total_positive_boost_db(room_peqs)` — the room candidate kind from wave 2,
PR #4544/#4520/#4546) and linearization boost
(`linearization_headroom_db(...)`, "the SAME quantity the fit discloses as
`LinearizationFit.headroom_cost_db`," per the inline comment at `:1889-1890`)
and the household's manual output trim (`max(0.0, output_trim_db)`).

**What is NOT charged, and why there is nothing to charge yet**: no bass-boost
term appears in this formula. This is not an oversight to fix — it reflects
what the emitter actually puts in the graph today. `_bass_extension_emission`
(`camilla_yaml.py:499-539`) always selects `natural = profile.targets[-1]`
(`:513`, the last/shallowest/safest family member) and emits it via
`emit_linkwitz_transform_biquad(..., freq_act=natural.fp_hz, q_act=natural.qp,
freq_target=natural.fp_hz, q_target=natural.qp)`
(`_emit_bass_extension_definitions`, `:559-563`) — **`freq_act == freq_target`
and `q_act == q_target`**, i.e. the LT biquad is a structural no-op at the
natural corner; it applies zero boost. **The live graph, at HEAD, never emits
a boosted bass-extension target** — only the flat/natural member ever reaches
CamillaDSP. So "the one disclosed gain" has nothing to absorb from bass yet;
wave 5.1's job (folding bass boost in) has no boost to fold until wave 4.3
lands a scheduler-chosen, boosted target. `natural.boost_headroom_db` is
carried informationally into `_bass_extension_profile_summary`
(`:544-556`, feeds `/state`) but is never added into
`total_headroom_db` — confirmed by the formula above.

---

## 6. Row 4.3's apply path today, function by function

There is no "scheduled candidate" apply path yet (§0). The nearest existing
analogue — the full apply chain a bass-extension profile's *natural* target
rides today, end to end:

1. **Web door**: `jasper/web/sound_active_speaker.py` or
   `jasper/web/correction_crossover_v2.py` call
2. **`apply_baseline_profile`** (`baseline_profile.py:3778-…`) — acquires
   `dsp_writer_lock(baseline_config_path(config_path).parent, source=
   "active_speaker_baseline_apply")` (`:3823-3826`), optionally refreshes
   inputs, then calls
3. **`_apply_baseline_profile_locked`** (`baseline_profile.py:3852-…`) —
   builds a `reviewed_candidate` (`write=False`), **re-evaluates the
   household's persisted bass-extension profile fresh at apply time** via
   `evaluate_bass_extension_profile(topology=..., applied_baseline_state=
   reviewed_candidate)` (`:3958-3963`) rather than trusting any caller-supplied
   bass state, takes `candidate_bass_emission_profile = bass_evaluation.profile`
   only if `status == "accepted"`, then calls
4. **`build_baseline_profile_candidate`** (`baseline_profile.py:2026-…`,
   `write=True, bass_extension_profile=candidate_bass_emission_profile`) which
   calls
5. **`emit_active_speaker_baseline_config`** (`camilla_yaml.py:3550`) →
   `_bass_extension_emission` (always `targets[-1]`, §5) →
   `_emit_baseline_filter_definitions` (the headroom gain, §5) →
   full YAML text, written to `config_path` on disk (this is the one point
   where a durable file changes, distinct from the live CamillaDSP graph).
6. Back in `_apply_baseline_profile_locked`, after fingerprint/staleness
   checks (`matches_expected`, `refuse_stale`, `:3927-3952`) and a
   `crossover_snapshot_state` validity check, the flow proceeds to
7. **`apply_dsp_config`** (`dsp_apply.py:764-…`, call site
   `baseline_profile.py:4086`) — the actual CamillaDSP load/confirm/rollback
   transaction, itself wrapped in the same writer-lock family
   (`dsp_writer_lock`/`camilla_graph_mutation`, `dsp_apply.py:628-660`).

`build_baseline_profile_candidate` accepts `bass_extension_profile:
BassExtensionProfile | None = None` (`baseline_profile.py:2049`) as a plain
parameter — there is no separate "scheduled candidate" object type, no
prescriber door, no candidate-bank entry for bass (unlike the room candidate
kind, which has `MeasuredCrossoverCandidate`/`candidate_bank.py`/
`require_candidate_trial`, per the wave-2 brief's §1). Row 4.3 would need to
either (a) make step 3's `bass_evaluation.profile` selection level-aware
instead of hardcoding `targets[-1]` inside `_bass_extension_emission`, or (b)
introduce a genuinely new candidate path — which of these is not yet decided
anywhere in the repo.

---

## 7. Non-negotiable surface for wave 4

**Every write/clamp site for a limiter threshold, a gain, a level ceiling, or
`devices.volume_limit`** that a scheduler or campaign would come near:

- `jasper/camilla.py:143-165` (`_coerce_main_volume_db`) — the main-fader
  clamp, `[-150.0, 0.0]` dB (§3.1).
- `jasper/camilla_config_contract.py:141` — `DEFAULT_VOLUME_LIMIT_DB = 0.0`,
  the value every emitted CamillaDSP config's `devices.volume_limit` carries.
- `jasper/active_speaker/camilla_yaml.py:2168-2170` — emitter-side
  `limiter_clip_limit_db` bound check, `-120 <= x <= 0`, raises
  `ActiveSpeakerConfigError` outside it. This is the exact bound
  `bench/context.py:51-52`'s `LIMITER_DOMAIN_MIN/MAX_DBFS` mirrors.
- `jasper/active_speaker/camilla_yaml.py:1913-1919` — `MAX_PROGRAM_HEADROOM_DB
  = 40.0` (`:1221`) ceiling on the combined pre-split headroom gain (§5).
- `jasper/bass_extension/bench/activation.py` — the bench's own
  fail-closed activation seam that writes `clip_limit` via `patch_config`
  during a limiter campaign (never the on-disk file — `activation.py`'s own
  docstring: "never write the on-disk CamillaDSP config file").
- `jasper/active_speaker/driver_safety.py:118-…` (`DriverSafetyProfile`,
  `DriverSafetyProfileError`/`DriverSafetyProfileStaleLowLimitError`) and
  `jasper/active_speaker/excitation_safety_plan.py:535`
  (`resolve_driver_excitation_ceilings`) / `:737`
  (`prepare_driver_excitation_plan`) — **the declared driver caps API**: the
  former returns a permitted band/max peak, the latter admits or refuses a
  fully caller-authored excitation request; neither *chooses* the request
  (the limiter-evidence protocol's own "Required bench owner" section makes
  this same point).
- **The commissioning SPL stop**: `jasper/active_speaker/seat_level_ramp.py`.
  `REFUSE_SPL_CEILING_EXCEEDED = "spl_ceiling_exceeded"` (`:155`);
  `over_ceiling_detail` (`:230-235`, message literally "commissioning stop
  {ceiling} dB SPL"); enforced live inside the watched fade/ramp loops at
  `:451-460` and `:802-808` (`if observed_db_spl > spl_ceiling_db_spl: ...
  refusal=REFUSE_SPL_CEILING_EXCEEDED`), stopped via `_fade_and_stop`
  (`:713-…`). The ceiling itself: `jasper/active_speaker/profile.py:559`
  (`max_commissioning_level_db_spl: float = 85.0`), bounds-checked
  `45 <= x <= 85` at `:605`. Sole reader named in-code:
  `jasper/active_speaker/commission_wiring.py:139-147` ("The one reader of
  `safety.max_commissioning_level_db_spl`").
- **AGENTS.md non-negotiable #2** ("never call `SAVE_CONFIGURATION` on the
  XVF3800"): grep-proved **zero** occurrences of `SAVE_CONFIGURATION` anywhere
  in `jasper/` at HEAD — no path calls it, bench included.

---

## 8. Open contradictions

**8.1 — Resolved, not a contradiction (verified before reporting): the
"deadness test" deletion timing.** `docs/adr/0257-...md` §1 (as originally
written) says the parked apply/bypass/recover transaction's "deadness tests"
(`tests/test_bass_extension_plan_status.py`) "stay exactly as they are until
the PR that lands the first production caller; that PR deletes them." At
HEAD, `tests/test_bass_extension_plan_status.py` **does not exist** — it was
deleted by PR #4563 ("Retire the bass wizard and the parked apply pathway,"
row 1.3 of wave 1), which has landed, while no production caller (row 4.3) has
landed. Read in isolation, ADR-0257 §1 is stale/contradicted by HEAD. **But**
`docs/adr/0259-...md` §5 explicitly amends ADR-0257 §1: "The engine's
candidate apply supersedes the parked Layer-2 apply pathway. The pathway and
its deadness tests are deleted in the retire wave, not by the PR that lands
the first production caller." PR #4563's commit message ("Retire the bass
wizard and the parked apply pathway (row 1.4)… the three deadness test
files") cites exactly this. **Conclusion: consistent, not contradictory** — a
reader of ADR-0257 alone (without also reading ADR-0259 §5) would be misled,
which is worth flagging as a documentation-navigation trap for the next
session, but it is not itself a fact in error.

**8.2 — `docs/HANDOFF-bass-extension-plan.md` §0 and §8.3 are stale at HEAD.**
The plan's one-paragraph summary (`:70-96`) says "Wave 3 prepared the sealed
identity graph and dormant apply/bypass/recovery transaction" as a
present-tense fact about the tree. At HEAD, that transaction is gone —
`jasper/bass_extension/__init__.py` is 10 lines (§3.2 above), and its own
docstring is now "Bass-extension apply-intent path and payload shape;
runtime-eligible adapters" — no apply/bypass/recover code exists at all
anymore (deleted whole by #4563, per ADR-0259 §5's authorization, §8.1 above).
The plan's §7.2 heading itself already carries a correction pointer
("retired under ADR-0259 §3," line 683) — so this staleness is *partially*
self-disclosed in the doc, but §0's summary paragraph is not updated to match
and still describes the transaction as "dormant" (present) rather than
deleted.

**8.3 — Three unrelated "wave" numbering schemes coexist in this repo; do not
conflate them.** (a) `seat-tuning-program/PLAN.md`'s own Waves 0–6 — this is
the numbering the task's "wave 4" refers to (limiter bench campaign / runtime
scheduler / first production caller). (b) `docs/HANDOFF-bass-extension-plan.md`
§12's **own, older, different** Waves 0–7 for the bass-extension product
itself, where "Wave 4" = "Commissioning backend" and "Wave 5" = "Runtime
scheduler" (`HANDOFF-bass-extension-plan.md:1642,1683`) — i.e. the HANDOFF
doc's "Wave 5" is the closest analogue to the seat-tuning plan's "Wave 4 row
4.2," not its own "Wave 4." (c) A **third, wholly unrelated** program
currently merging into `origin/main` under branch names like
`claude/observability-speaker-refactor-3y2do7-w4-1`,
`-w3-7`, `-w2-6`, etc. (PRs #4636, #4634, #4633, #4632, #4631, #4623 …) — this
is a different initiative with its own independent w0–w4 numbering and has
**nothing to do with bass extension, room correction, or seat-matched
tuning**; several of its "w4-*" branches are among the most recent merges to
`origin/main` and could easily be mistaken for seat-tuning wave-4 work by
branch-name pattern-matching alone. A wave-4 brief author must not search git
log for "wave 4" or "w4" and assume a hit is this program's.

**8.4 — No contradiction found between ADR-0257/0259/0260 as a trio.** ADR-0257
(bass resumes) is explicitly amended in part by both ADR-0259 §5 (apply-pathway
deletion timing, §8.1) and ADR-0260 (supersedes ADR-0257 §3's nearfield-fit
protection basis outright, replacing it with declared-plant-facts +
distortion-ladder + limiter-evidence). Reading all three together, the
amendment chain is internally consistent and each amendment is correctly
cross-referenced in the superseding document's front-matter. The only
open item is that ADR-0260 §3's "This supersedes ADR-0257 §3" mandate
(`fit_plant` re-pointed at the seat-cube median) has not yet been implemented
in code — `adapters/sealed.py:141` still hard-requires
`CaptureRole.WOOFER_NEARFIELD` — but the ADR itself flags this forward
("stale from this date and is corrected in the wave that lands the bass fit,"
ADR-0260 Consequences) rather than asserting it as already done, so this is a
tracked gap, not a doc/code contradiction.
