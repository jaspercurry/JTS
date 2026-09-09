# Facts — seat-matched tuning, wave 5 (headroom) and wave 6 (pair/3-way)

Verified at `origin/main` **`d112fa5a136d1b3da48945432ebfacb60710ae40`** (fetched
2026-09-09 15:10Z, detached worktree `/home/user/wt/facts56`). Read-only; nothing
in the tree was edited.

**Headline finding, stated up front because it changes both briefs' framing:**
wave 3 (bass candidate kind, scheduled family emission) and wave 4 (runtime
scheduler) have **not landed on `main`**. `jasper/bass_extension/` has no
`candidate_field.py`, no scheduled-candidate kind, no runtime scheduler; the
last merge touching the package is `7ef2adbef` (row 1.3, the wizard retire).
Everything wave 3/4-shaped that PLAN.md's status log narrates (PRs #4641,
#4643, the 3.2/3.3/3.4 stack) is still on unmerged branches under review. This
means: **wave 5's stop-and-report condition ("a boosted rung reaching
CamillaDSP without a matching headroom charge") does not and cannot fire at
HEAD** — the current code path can only ever emit the *natural* (unboosted)
bass target, enforced redundantly three times (below). The wave 5 brief should
not assume the level bug is live; it should assume the emission path for a
boosted rung does not exist yet and say what the headroom formula must gain
*when* it lands.

---

## Wave 5 — one headroom budget

### 1. The `active_baseline_headroom` charge formula

`_emit_baseline_filter_definitions`, `jasper/active_speaker/camilla_yaml.py:1839-1939`.
The exact formula (`:1897-1912`):

```
total_headroom_db = baseline_headroom_db
                   + total_positive_boost_db(room_peqs)
                   + linearization_headroom_db(linearization, branch_context=...)
                   + trim_db                      # trim_db = max(0.0, output_trim_db)
```

- `baseline_headroom_db` — caller-supplied (validated `0..40`, `:3648-3649`),
  the household's own commissioning headroom.
- `total_positive_boost_db(room_peqs)` — `jasper/camilla_config_contract.py:176-189`:
  `max(0.0, sum(f.gain for f in filters if f.gain > 0.0))`, the sum of all
  *positive* room-PEQ gains (an upper bound on combined peak).
- `linearization_headroom_db` (`camilla_yaml.py:1736-1780`) — the **worst
  single branch's** realized peak (`crossover ⊗ linearization ⊗ trim`), not a
  sum across branches, computed via `branch_context` (`_branch_context`,
  `:1809-1836`, itself built from `branch_chain.sections_by_role` — a lazy
  import, only paid when `linearization_has_boost` is true, `:1783-1806`).
  0.0 for a cut-only linearization without evaluating anything (no numpy
  import on that path).
- `trim_db` — the household's manual headroom/loudness-match trim,
  non-negative only.
- **There is no bass term.** `bass_extension` is threaded into
  `_emit_baseline_filter_definitions` (`:1848`) only to place its filters in
  the per-driver chain (`_emit_baseline_driver_definitions` at `:1927-1933`);
  it never contributes to `total_headroom_db`.

**Ceiling and refusal** (`:1913-1919`): `if total_headroom_db >
MAX_PROGRAM_HEADROOM_DB: raise ActiveSpeakerConfigError(...)`.
`MAX_PROGRAM_HEADROOM_DB = 40.0` (`camilla_yaml.py:1221`, "NOT a cap on the
correction — a refusal"). This is a **refusal on emit**, not a clamp: nothing
downstream ever silently truncates the trim.

**Callers of `_emit_baseline_filter_definitions`:** exactly one, the public
`emit_active_speaker_baseline_config` (`camilla_yaml.py:3550`, call at
`:3675`). Callers of *that*, all in the tree today:
- `baseline_profile.build_baseline_profile_candidate` (`baseline_profile.py:2045`,
  call at `:2826`) — threads `room_peqs=room_peqs` where
  `room_peqs = candidate_room_peqs(measured_candidate) if room_correction else
  ()` (`:2687`). This is the production apply path for a measured candidate.
- `baseline_profile.recompose_applied_baseline_yaml` (`:3143`, call at `:3306`)
  — re-emits the already-applied baseline (e.g. on revert/preference change).
- `measured_crossover_candidate.compile_candidate_config` (`:827`, call at
  `:854`) — used to render a candidate's YAML for measurement/trial, not
  apply.
- `bench/loop.py::plan_emit_loop` (`:453`, calls at `:479,485`) — the design
  bench's "treated" vs "control" differential emission, offline.

### 2. Where the gain sits in the pipeline

`_emit_baseline_pipeline` (`camilla_yaml.py:1969-2046`), stage order:

```
room_peq_names (channels [0,1])         ← Layer B, pre-split
blend_correction_names (channels [0,1]) ← crossover-blend correction, pre-split
active_baseline_headroom (channels [0,1])  ← THE ONE GAIN
preference_filter_names (channels [0,1])← Layer C, pre-split, rides at unity
split_active_<n>way (Mixer)             ← ── split boundary ──
  per role: [bass_mgmt_hp?, crossover HP/LP, linearization, bass_extension,
             delay, baseline_gain, baseline_limiter]     (per-driver chain,
                                                            _driver_baseline_filter_chain)
  local sub: [sub_lowpass, bass_extension?, sub_baseline_gain, sub_baseline_limiter]
```

So `active_baseline_headroom` sits **strictly before the split mixer** —
upstream of every crossover, every per-driver limiter, and (see below) upstream
of the bass-extension stage, which lives *inside* the post-split per-driver /
per-sub chain. It is therefore upstream of the entire bass stage; a bass boost
that reached the graph would need its own term added to the pre-split gain
computed above it, exactly as room and linearization boosts already do.

One asymmetry worth flagging for the brief: **there is no protective tweeter
high-pass in the baseline chain at all.** `_driver_baseline_filter_chain`
(`:1042-1068`) has no call to `_protective_tweeter_hp_name`/
`_protective_tweeter_hp_frequency` — those exist only in `_driver_filter_chain`
(`:1015-1030`), used by the **startup** pipeline (`_emit_pipeline`,
`:1942-1966`/`_emit_filter_definitions`, `:1084-1130`). The baseline path
relies instead on an emit-time gate, `_assert_tweeter_crossover_honours_
declared_floor(preset)` (`:3617`), which refuses to emit at all if the
crossover HP corner sits below the declared protection floor — i.e. the
durable graph has no separate "protective" filter because the crossover HP
*is* the protection, proven at build time rather than layered in the graph.

### 3. The bass term — can a boosted rung reach CamillaDSP?

**No — not at HEAD, and it is closed three independent ways:**

1. **The profile's own invariant.** `BassExtensionProfile.__post_init__`
   (`jasper/bass_extension/profile.py:268-274`): `natural = targets[-1]`; if
   `natural.target_id != "natural" or natural.filters or
   natural.boost_headroom_db != 0.0: raise ValueError(...)`. A profile object
   literally cannot exist with a non-zero-boost natural target.
2. **The emitter only ever reads the natural target.** `_bass_extension_emission`
   (`camilla_yaml.py:500-545`): `natural = profile.targets[-1]`, and refuses
   (`ActiveSpeakerConfigError`) unless `natural.target_id == "natural"`. Every
   non-natural (boosted) entry in `profile.targets` is simply never looked at
   by the emitter.
3. **The graph-safety re-proof independently re-checks zero boost from the
   emitted text.** `bass_extension_block_valid` (`jasper/active_speaker/
   graph_safety.py:807-...`, the check at `:892`): `boost != 0.0` is one of
   the disjuncts that fails the proof — it re-derives the same invariant from
   the rendered YAML, not from the profile object.

The `_emit_bass_extension_definitions` function (`:566-584`) emits exactly one
Linkwitz-transform (`emit_linkwitz_transform_biquad`, `freq_act=freq_target=
natural.fp_hz`, `q_act=q_target=natural.qp` — i.e. **at-rest, no transform
applied**) plus the mandatory subsonic high-pass — this is a flat pass-through
tuned to itself, not a boost.

**Consequence for the brief:** the plan's own stop-and-report condition
("if a boosted rung can reach CamillaDSP without a matching charge... that is
a level bug") cannot be evaluated against current behavior because the
boosted-rung emission path (wave 3.3) doesn't exist yet. What wave 5 can state
today is the *shape* the future charge must take (a fourth additive term,
keyed off whatever rung the runtime scheduler selects) and that
`graph_safety.bass_extension_block_valid`'s `boost != 0.0` proof must be
widened *in the same PR* that adds the term — the plan's own wave-3 status log
(§9, entries for 3.4) already names this as "the NN-tier hunk the adversarial
review exists for," confirmed unwidened at HEAD.

**Boundary note relevant to wave 3/4 landing near wave 5:** `bass_extension`
is still absent from `PACKAGE_BOUNDARIES`
(`tests/test_audio_measurement_boundary_ssot.py:236-252`, exactly 3 rows:
`audio_measurement`, `active_speaker`, `cli` — no `bass_extension` row), and
`camilla_yaml.py` imports `BassExtensionProfile` only under `TYPE_CHECKING`
(`:102-103`, with a runtime-import-only-when-needed comment at `:105-106`).
Both match PLAN.md §4's claim and are unchanged at HEAD.

### 4. "Its cost in maximum level" — what exists today, and in what units

Three "layers" the brief will want to compare are **not expressed in the same
units**:

- **Room:** `level_cost_db` on the candidate's `room_correction` field,
  required equal to `boost_db_total`
  (`measured_crossover_candidate.py:313-325`), itself required equal to "the
  largest per-side positive gain sum" (`:305-320`, `max(side_boosts)`) — a dB
  number, disclosed on the candidate and surfaced through the `room-grade`
  view.
- **Linearization:** `headroom_cost_db` on `LinearizationFit`
  (`linearization_fit.py:446,491`), disclosed live during a tuning session via
  `crossover_v2_flow.py:3433,3459-3464` (`_candidate_headroom_cost_db` →
  `worst_headroom_cost_db`) and in the durable-state candidate summary
  (`crossover_v2/durable_state.py:707-717,925`) and the envelope
  (`crossover_envelope_v2.py:258`) — also dB.
- **Bass:** no dB cost is disclosed anywhere. `/state.bass_extension`
  (`jasper/control/state_aggregate.py:681-684` →
  `bass_extension.profile.bass_extension_state_summary`, `profile.py:524-631`)
  discloses `deepest_hz`, `natural_hz`, `margin` (a **string** policy name —
  `MARGINS` in `jasper/bass_extension/targets.py:16-61`, e.g.
  "conservative"/"normal"/"aggressive", each with its own
  `digital_margin_db`), and `anchors` — each anchor carries `target_id` and
  `max_listening_level` (**an int 0–100**, `targets.py:95,121`, mapped to dB
  internally via `percent_to_db`), not a dB figure. `boost_headroom_db` itself
  (computed in `alignment.boost_headroom_db`, `alignment.py:103`) is never
  surfaced through `/state` or any candidate envelope — it is internal to the
  profile/adapters.

So today: room and linearization costs are dB numbers an operator can read
side by side; bass's cost is a percent-of-max-volume figure plus a named
policy string, with no dB figure disclosed at all. A "one disclosed number in
maximum level" (plan §1's table, PLAN.md wave 3.4/5.1 wording) does not exist
yet in any form that unifies these three.

### 5. Runbook verb order today

`docs/tuning-operator-runbook.md`:
- `## One possible flow` (`:31-84`) is the **speaker-only** 9-step sequence
  (orient → plan → stage/measure → bank → inspect → freeze → propose/stage →
  decide → adopt).
- `## Room` (`:208-239`) is a **separate section**, its own 8-step sequence
  (seat-cube capture → bank → `room-ceiling` → `room-median` →
  `room-persistence` → propose/compose/measure the room candidate →
  `room-grade`).
- **There is no `## Bass` section.** Only a stub exists, in a different file:
  `docs/tuning-methodology.md` §12 "Bass" (`:279-286`) is two sentences ending
  "This section is written in the wave that lands the bass fit view and the
  protection ladder" — i.e. explicitly a placeholder, not real guidance.
- `close-reference` is **not** described in the runbook's flow sections at
  all — it appears only in the tool-menu table row (`:292`) and the
  "find the analysis that answers the question" table (`:310`). The line the
  plan's wave 5.2 wants ("`close-reference` named as the on-demand room-gain
  split") **already exists**, but in `tuning-methodology.md:284-286`, not the
  runbook, and inside the same "written in a later wave" stub paragraph as
  above. So: the room-gain-split framing is already drafted; it just isn't in
  the runbook, and it isn't a real section yet, only a placeholder note.
- Net: today there is no single sequence anywhere ordering
  "speaker tune → bass family → room candidate → bass re-check" — the runbook
  has two disconnected flows (speaker, room) and zero bass guidance. 5.2 is
  writing this from nothing, not editing an existing (wrong) order.

---

## Wave 6 — the pair and the 3-way

### 6. Per-side emission today: `room_peqs` vs `room_peqs_right`

**These are two different mechanisms solving two different problems, not one
concern under two names**, despite ADR-0258's framing ("a side axis by
another name, on the other graph"):

- **Active emitter (`jasper/active_speaker/camilla_yaml.py`):** `room_peqs`
  is a single `Sequence[PeqFilter]` applied on `channels: [0, 1]` before the
  split mixer (`_emit_baseline_pipeline:1982-1988`) — i.e. **one room PEQ set,
  identical on every physical output**, whatever the layout. There is no
  `room_peqs_right` anywhere in this module.
- **Flat/passive emitter (`jasper/sound/camilla_yaml.py:340-394`):**
  `room_peqs_right` is documented explicitly as "the **multi-room
  leader-bake** axis: a DIFFERENT room correction per channel in ONE config —
  channel 0 gets `room_peqs` (**the leader's seat**), channel 1 gets
  `room_peqs_right` (**the follower's seat**)" (`:367-371`). This is a
  Snapcast **bonded-leader/follower** (different rooms, different listeners)
  concept, not a stereo cabinet's left/right physical sides. `None` duplicates
  `room_peqs` onto channel 1 (solo byte-identical contract); `[]` bakes a flat
  follower segment.

**The candidate already carries a `sides` mapping** (from wave 2.1, landed):
`MeasuredCrossoverCandidate.room_correction["sides"]`
(`measured_crossover_candidate.py:248-312`) is validated per side
(`layout_sides = SIDES_BY_LAYOUT[preset.channel_map.layout]`), with per-side
filter lists, per-side boost caps, and `boost_db_total = max(side_boosts)`
(worst-side charge — same "worst branch" pattern as linearization).

**The refusal the plan names exists, confirmed, with the generic slug:**
`_validated_room_correction` (`:182-202`):

```python
_ROOM_INVALID = "room_correction_invalid"
if len(layout_sides) > 1:
    _refuse(_ROOM_INVALID,
        "the emitter takes one room PEQ list, so a room set on a "
        f"{len(layout_sides)}-sided layout would emit only "
        f"{layout_sides[0]!r}; remove this bound when per-side room "
        "emission lands (ADR-0258)")
```

So: for `layout == "stereo"` (2 sides), a `room_correction` with real content
is refused **whole**, before any per-side validation runs, always under the
same generic `room_correction_invalid` code used for every other room-field
malformation — there is no dedicated refusal code for this case yet, exactly
as the plan says. `candidate_room_peqs` (`:744-763`) is the function that, for
the (currently only reachable) 1-side case, extracts
`room_correction["sides"][SIDES_BY_LAYOUT[layout][0]]` for the emitter.

**Convergence direction (ADR-0258 consequence, unbuilt):** the active
emitter's future per-side stage and `sound/camilla_yaml.py`'s
`room_peqs_right` are meant to converge into one mechanism — but they start
from materially different use cases (per-cabinet room correction within one
listening position vs. per-room correction across two Snapcast-bonded
listening positions), so "converge" here means the *emission primitive*
(a per-channel PEQ stage) can be shared, not that the two *use cases* become
one.

### 7. The side-solo capture graph

**Nothing today composes a graph that plays one side only.** Evidence:
- `GRAPH_SCOPES` at HEAD (`crossover_v2/measure_spec.py:125`) =
  `(GRAPH_SCOPE_DRIVERS, "base", "speaker_tune", "candidate",
  "room_candidate", "candidate_branches")` — six scopes (PLAN.md §4 is stale
  here: it names only four, pre-dating wave 2.1's `"room_candidate"` and
  whatever landed `"candidate_branches"`). None is side-scoped; the dispatch
  in `session_graph.py:161-181`+ keys only on these named scopes.
  `composition.py` (the seam that composes the playback graph,
  `bind_engine_seams`/`bind_program_playback_seams`/`bind_program_composer`,
  `:42,98,172`) has no side/solo concept either — no hit for "solo",
  "side_only", "isolate" in either file.
- **An existing, adjacent vocabulary does exist, but keys on driver ROLE, not
  output SIDE:** the "isolated-driver" capture/admission path
  (`jasper/active_speaker/program_admission.py`: `_channel_roles:189`,
  `readmit_program_from_wav:614`, `readmit_summed_program_from_wav:671`;
  `jasper/active_speaker/commissioning_evidence_store.py`:
  `isolated_driver_evidence_relative_path:304`, `publish_isolated_driver_
  evidence:1069`, etc.) isolates **one driver role's excitation** (e.g. woofer
  alone) for commissioning proof, using the per-output `startup_muted` /
  commission-mute machinery already present on every `PhysicalOutput` /
  `OutputChannel`. It is the closest existing pattern a side-solo graph could
  imitate (mute every output whose `side` != the target side), but it is not
  itself reusable without a new scope: it composes on role, and the code that
  resolves "which channels does this role own" (`_channels_for_role`,
  `camilla_yaml.py:492-497`) explicitly ignores `side` — it returns every
  output with a matching `driver_role` regardless of side.
- Per ADR-0258 and PLAN.md §3, this graph is explicitly **the tuning-flow
  agent's** to build, not this program's; wave 6.1 is blocked on it.

### 8. Topology: what a "side" is, and whether a pair is representable

- `SUPPORTED_LAYOUTS = {"mono", "stereo"}`; `SIDES_BY_LAYOUT = {"mono":
  ("mono",), "stereo": ("left", "right")}` (`jasper/active_speaker/profile.py:
  80-84`).
- Each output in `ActiveChannelMap.outputs` (`profile.py:250-283` `OutputChannel`,
  `:285-348` `ActiveChannelMap`) carries `index`, `side`, `driver_role`,
  `label`, `startup_muted` — so a physical channel is keyed on **(side,
  role)**, and `_channels_for_role` collects every output whose role matches,
  across sides.
- **`ActiveChannelMap.validate_for_way` (`:304-342`) requires the output set
  to be EXACTLY `{(side, role) for side in sides for role in roles}`** — no
  more, no fewer: a duplicate `(side, role)` raises `"duplicate output for
  {side}/{role}"` (`:325-330`), and any output whose `(side, role)` is not in
  that exact required set is rejected as `"unexpected output channels"`
  (`:336-337`). **A two-cabinet stereo pair is structurally representable
  today** (`layout: "stereo"`, one output per role per side) — but neither of
  the two shipped preset fixtures uses it
  (`jasper/active_speaker/presets/*.json` are both `"layout": "mono"`); no
  production preset is stereo. `layout: "stereo"` appears only in tests
  (`test_active_speaker_profile.py`, `test_audio_runtime_plan.py`,
  `test_active_speaker_local_subwoofer.py`,
  `test_active_speaker_emit_bench_derivation.py`,
  `test_active_speaker_commissioning_evidence.py`).
- **Per-role facts (crossover, linearization, delay/gain/polarity correction)
  are keyed by ROLE ONLY, applied identically to every side that carries the
  role** — see contradiction #12 below; this directly implements ADR-0258
  rule 3's "per-model fact... applies to every side" half, but leaves the
  "per-cabinet facts attach to a side" half (level trim named explicitly)
  unimplemented for anything except room PEQ.
- `bass_management_corner_hz()` (`jasper/output_topology.py:1077-1095`)
  returns **one** float (or `None`): it walks `subwoofer_speaker_groups`,
  returns the first declared per-channel `crossover_fc_hz` it finds, else the
  package default. It names one bass system for the whole household output
  topology, with no side/group distinction — confirms PLAN.md §4's claim.
  Note: `output_topology.py`'s own "side"-adjacent vocabulary
  (`SpeakerGroup`/`SpeakerChannel`, household-wide main/sub groups with an
  (x, y, rotation) `SpeakerPosition`) is a **different, coarser model** from
  `active_speaker.profile`'s per-cabinet `side` — `SpeakerChannel` has no
  `side` field at all, only `role`. Two vocabularies, two packages, not
  currently unified (context for a later contradiction, not itself
  contradictory: they answer different questions — household output routing
  vs. one active cabinet's internal channel map).

### 9. What a "bass system" is, and per-unit fitting

- The bass **owner** is resolved once per household by
  `_bass_extension_emission` (`camilla_yaml.py:500-545`) from
  `profile.bass_owner`: either `kind == "woofer_way"` with `roles=(role,)`
  (channels = every output carrying that role, **across sides**, via
  `_channels_for_role`), or `kind == "local_sub"` (the single
  `preset.local_subwoofer.physical_output_index` — `LocalSubwoofer` is
  singular on `ActiveSpeakerPreset`, `profile.py:473,651-655`; there is no
  concept of two local subs).
- **The emitted filters are named globally, not per channel/side:**
  `BASS_EXTENSION_LT_FILTER = "bass_ext_lt"`, `BASS_EXTENSION_SUBSONIC_FILTER
  = "bass_ext_subsonic"` (`camilla_yaml.py:132-133`) — one Linkwitz-transform
  definition, referenced by name in whichever channels' Filter step owns the
  role (`_bass_extension_chain_names`, `:587-604`). **If a stereo preset had
  the bass-owner role on both sides, both sides would get byte-identical
  transform values** — there is no mechanism today to give left and right
  woofers different corners/Q, let alone different fitted families.
- **What would have to change for a per-unit fit check (6.2):** (a) the bass
  owner would need to resolve per side, not per household; (b) the family/
  profile object (`BassExtensionProfile`) would need a side key or one profile
  per side; (c) the emitted filter names would need to become per-side (like
  `_room_peq_name(i)` already is per-filter-index) so two Filter steps can
  carry different transform parameters; (d) `fit_plant`/the median-based fit
  (wave 3.2, unlanded) would need to consume a per-side seat-cube median
  rather than the one median it fits today.
- **No excursion model exists to check a "per-unit fit" against.**
  `driver_safety._normalise_cabinet` (`jasper/active_speaker/driver_safety.py:
  486-514`) accepts exactly five keys — `enclosure_kind`, `radiator_count`,
  `effective_radiating_diameter_mm`, `baffle_width_mm`,
  `lf_reconstruction_capability` — no `f0`, `Q`, `Xmax`, or `Sd` field
  anywhere in this schema. This matches the plan's wave-3.2 review finding
  (PLAN.md §9, 2026-09-09 14:40Z entry) and is **still true at HEAD** since
  that PR hasn't landed: "no excursion margin is computable anywhere."

### 10. Cardioid — what exists for a second output of the bass role

**Nothing exists, and the schema actively refuses it, not merely omits it.**
`ActiveChannelMap.validate_for_way` (`profile.py:304-342`) requires the output
set to equal **exactly** `{(side, role) for side in sides for role in roles}`:
a second output claiming the same `(side, role)` pair raises `"duplicate
output for {side}/{role}"` (`:325-330`) before any other check runs, and any
output outside that exact set is refused as `"unexpected output channels"`.
This means ADR-0258 §2's cardioid design ("the same source signal... emitted
as another output of that role") **cannot be expressed by the current channel
map at all** — not "not yet designed" but "the validator that exists today
would reject the shape ADR-0258 describes," so landing it needs a schema
change (a new key beyond `(side, role)`, e.g. a variant/instance discriminator)
before any delay/polarity/level-per-output question is even reachable.

Per-output delay/gain/polarity **for one role today is a single value shared
across every channel carrying that role** (`_correction_value`/
`_correction_bool`, `camilla_yaml.py:1134-1156`, keyed `corrections[role]`,
no side or instance axis) — so even setting aside the schema block, the
emitter has no per-output (only per-role) delay/polarity/level primitive to
build a second cardioid output's *own* delay/polarity/level from. Room PEQ is
the only stage in the whole baseline emitter with any per-channel-set
machinery today (and even that is one-set-for-all-channels until wave 6.1).

Confirms the plan's own framing: 6.3 needs a design conversation first, and a
3-way preset that doesn't exist in the shipped presets (`bc_de250_dayton_
e150he44_v1.json` and `epique_e150he44_eminence_f110m8_safe_v1.json` are both
2-way).

### 11. Non-negotiable surface either wave comes near

**Clamps / caps / ceilings, all confirmed present and unweakened at HEAD:**

| Site | Value | File:line |
|---|---|---|
| `DEFAULT_VOLUME_LIMIT_DB` / `ensure_volume_limit_db` | `0.0` | `jasper/camilla_config_contract.py:141,144` |
| `_coerce_main_volume_db` clamp | `[MIN_MAIN_VOLUME_DB=-150.0, MAX_MAIN_VOLUME_DB=DEFAULT_VOLUME_LIMIT_DB]` | `jasper/camilla.py:39-40,149-...` |
| `_assert_volume_limit` (active-speaker emit gate) | restates `ensure_volume_limit_db` | `camilla_yaml.py:417-422` |
| `MAX_PROGRAM_HEADROOM_DB` | `40.0`, refusal not clamp | `camilla_yaml.py:1221` |
| `MAX_LINEARIZATION_BOOST_DB` (per-filter) | `12.0` | `camilla_yaml.py:1210` |
| `MAX_LINEARIZATION_FILTERS_PER_DRIVER` | `8` | `camilla_yaml.py:1200` |
| `STARTUP_LIMITER_CLIP_LIMIT_DB` / `BASELINE_LIMITER_CLIP_LIMIT_DB` | `-12.0` / `-1.0`, per-driver soft-clip Limiter | `camilla_yaml.py:127,131` |
| `ROOM_MAX_FILTER_BOOST_DB` / `ROOM_MAX_TOTAL_BOOST_DB` | `6.0` / `6.0` | `jasper/audio_measurement/room_limits.py:87-88` |
| `ROOM_MAX_FILTERS_PER_SIDE` | `8` | `room_limits.py:89` |
| `ROOM_PEQ_Q_MIN`/`MAX` | `1.0`/`8.0` | `room_limits.py:60-61` |
| `ROOM_BOUNDARY_MIN_HZ`/`MAX_HZ` | `250.0`(=`GATED_SPEC_LOWER_EDGE_HZ`)/`500.0` | `jasper/audio_measurement/room_boundary.py:97-106,115` |
| Bass subsonic protection mandatory | raises if `target.subsonic is None` on any target | `camilla_yaml.py:511-514` |
| Bass natural-boost invariant | `boost_headroom_db != 0.0` → `ValueError` at profile construction | `bass_extension/profile.py:268-274` |
| Bass graph-safety re-proof | `boost != 0.0` fails the proof independently of the profile | `active_speaker/graph_safety.py:892` |
| DSP writer lock vs. pending bass apply intent | `BassExtensionApplyPending` refuses every mutation while `BASS_EXTENSION_APPLY_INTENT_PATH` exists | `jasper/dsp_apply.py:509-521,578-581` |

**`BASS_EXTENSION_APPLY_INTENT_PATH` has no writer anywhere in the tree** —
only readers (`dsp_apply.py`, `runtime_contract.py:4300,4588`,
`multiroom/follower_config.py:523`, `cli/doctor/active_speaker.py:172`,
`bass_extension/profile.py:530,535`) check `.exists()`. Confirms PLAN.md §9's
(2026-09-09 14:30Z) claim, still true at HEAD: "nothing has written that file
since #4563."

**`deploy/install.sh` and `deploy/lib/install/`:** zero references to
`bass_extension` or `room_correction` anywhere in either. These waves are
pure-Python emitter/candidate/view changes; nothing here needs an install-time
change (no new systemd unit, no new config file, no new install step) as of
HEAD, and neither wave's rows say otherwise.

**XVF3800 `SAVE_CONFIGURATION` (non-negotiable #2):** not touched by anything
in `jasper/active_speaker/`, `jasper/bass_extension/`, or `jasper/camilla.py`
at HEAD — this is a mic-array/AEC-chip concern, orthogonal to the DSP/output
path these waves touch. No evidence found that wave 5/6 comes near it.

### 12. Open contradictions

1. **ADR-0258 rule 3 vs. the actual `corrections` shape.** The ADR states
   "Per-cabinet facts attach to a side: ... the level trim, the bass fit
   check." At HEAD, the driver-alignment correction dict consumed by the
   emitter — `corrections: dict[str, dict[str, float | bool]]`
   (`_correction_value`/`_correction_bool`/`_validated_driver_corrections`,
   `camilla_yaml.py:1134-1185`) — is **keyed by role only**, with no side
   axis whatsoever; `gain_db`/`delay_ms`/`inverted` apply identically to
   every side carrying that role. So "the level trim" is, at HEAD,
   structurally a **per-model** (role) fact, not a per-cabinet (side) fact —
   the one axis ADR-0258 names explicitly as per-cabinet is implemented as
   per-role, same as crossover and linearization. Only room PEQ has any
   per-side representation today (and it's refused for >1 side, per #6
   above).
2. **ADR-0258's "a side axis by another name" characterization of
   `room_peqs_right`.** The ADR states `jasper/sound/camilla_yaml.py`'s
   `room_peqs_right` is "a side axis by another name, on the other graph,"
   implying it is the same concern as the active emitter's future per-side
   room stage. The module's own docstring (`sound/camilla_yaml.py:367-371`)
   frames it exclusively as "the multi-room **leader-bake** axis... channel 0
   gets the leader's seat, channel 1 gets the follower's seat" — a
   Snapcast-bonded two-room concept, not a stereo cabinet's two physical
   sides. The two are structurally similar (a per-channel PEQ list) but
   conceptually distinct (two rooms/listeners vs. two drivers in one room);
   worth the wave-6 brief stating this precisely rather than inheriting the
   ADR's "one concern" framing uncritically.
3. **PLAN.md §4's `GRAPH_SCOPES` line is stale.** It states
   `GRAPH_SCOPES=("drivers","base","speaker_tune","candidate")` (four). At
   HEAD it is six: `(GRAPH_SCOPE_DRIVERS, "base", "speaker_tune", "candidate",
   "room_candidate", "candidate_branches")` (`measure_spec.py:125`) — expected
   drift from wave 2.1 landing `"room_candidate"` and something else adding
   `"candidate_branches"` (not investigated further; out of scope for this
   fact pass, but the wave 6 brief should re-verify what `"candidate_branches"`
   is before assuming the scope table's shape).
4. **`docs/extensibility.md` does not govern this program's extension
   points.** Its "five extension contracts" (§3: Tools, Sources, Model
   providers, Hardware profiles, Features) are entirely about voice-assistant
   extensibility (LLM tools, audio sources, model backends, hardware
   profiles, cross-layer features) — none of the five names candidate kinds,
   emitter stages, measurement programs, or round views, which is the
   vocabulary PLAN.md §7 actually extends. Reading it for wave 5/6 context is
   reasonable per the task's instructions, but it should not be cited as
   defining these waves' extension points; PLAN.md §7 ("rows add to
   `measurement_programs.py`, `ARTIFACT_BY_VIEW`, the candidate bank, and the
   emitter") is the only doc that does.
5. **`docs/audio-paths.md` is silent on everything wave 5/6 asks about.** It
   documents the fan-in/ring/outputd routing topology *above* CamillaDSP
   (renderer mixing, ducking, TTS mix points); it has no section on
   CamillaDSP's internal filter-chain ordering, headroom, room PEQ, or bass
   extension. Not a contradiction with the code, but it cannot be cited as
   evidence for pipeline ordering (item 2 above) — that evidence exists only
   in `camilla_yaml.py` itself.
6. **No contradiction found between ADR-0256/0257/0259/0260 and HEAD** beyond
   what's already itemized above: the room ceiling mechanism
   (`room_boundary.room_ceiling_hz`, `:126-131`) matches ADR-0256 rule 1
   exactly (clamped trusted floor, fallback to `ROOM_BOUNDARY_DEFAULT_HZ`,
   disclosed source); the seat-cube pose vocabulary, close-reference
   retention, and "no nearfield rung" from ADR-0260 are all reflected in
   current code (`_normalise_cabinet` has no `WOOFER_NEARFIELD`-style
   required key; `close_reference.py`'s module docstring frames itself as
   optional/on-demand, matching ADR-0260 §4).
