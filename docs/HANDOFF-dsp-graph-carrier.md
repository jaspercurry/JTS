# Handoff: program-domain DSP composition (the graph carrier)

> **Status: design-of-record (operational).** This is the canonical
> design + rollout plan for applying **preference EQ and room
> correction on top of any output topology** — flat/full-range,
> active 1/2/3-way (+ optional sub), and distributed (paired)
> speakers. It is kept in sync with code per the touched-subsystem
> rule. Companion docs:
> [HANDOFF-sound-preferences.md](HANDOFF-sound-preferences.md)
> (preference EQ surface), [HANDOFF-correction.md](HANDOFF-correction.md)
> (room correction), [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md)
> (the active crossover + Layer A/B/C model).

## Why this exists

A JTS speaker is a signal chain that can be physically distributed.
Two channel domains, separated by **one boundary — the split mixer**:

- **Program domain** (the music): **1–2 channels**. This is where
  **room correction (Layer B)** and **preference EQ (Layer C)** live —
  per-channel room PEQs (L≠R), a shared preference curve, and the
  headroom trim that keeps boosts below clip. These are *program*
  transforms; they ride the stereo bus once.
- **Driver domain** (the speaker): **N channels** (a stereo 3-way + sub
  is 7). This is the **active crossover (Layer A)** — the `2→N` split
  mixer plus per-driver crossover / delay / gain / limiter, and the
  tweeter's band-limiting high-pass.

The split mixer is the **relocatable seam**. In a solo box all layers
run in one CamillaDSP graph. In a **paired** setup the leader/transmitter
owns the program domain (B/C) and streams the corrected **2-channel**
program over Snapcast; the follower/receiver owns the driver domain
(Layer A) locally. Only 2 channels ever cross the wire — the N driver
channels never leave the box that owns the DACs.

**The bug that motivates this work:** `/sound/` preference-EQ apply
*refused* whenever an active-speaker baseline was the loaded config. The
two EQ entry points (live draft in
[`jasper/web/sound_setup.py`](../jasper/web/sound_setup.py) and durable
load/apply in [`jasper/sound/runtime.py`](../jasper/sound/runtime.py))
used to run an identical 3-arm branch:

```
if is_base_config(p):            room_peqs = []
elif is_jts_generated_config(p): room_peqs = extract_room_peqs_from_config(p)
else:                            raise RuntimeError("custom config ... cannot safely preserve ... Reset ...")
```

`is_jts_generated_config` ([`jasper/sound/camilla_yaml.py`](../jasper/sound/camilla_yaml.py),
`_JTS_GENERATED_RE`) does **not** recognize `active_speaker_baseline.yml`,
so every EQ apply on an applied active speaker hits the `else` and raises
`HTTP 502 {"error": "...custom config..."}`. Two sub-defects: (a) the
message calls JTS's own baseline "custom" and prescribes a reset that
would *destroy* the active graph; (b) the refusal is the **correct**
hardware behavior (re-emitting via `emit_sound_config` — a hard stereo
`2→2` template — would drop every crossover/limiter/HP and send
full-range to the DAC lanes), but the *remediation* and the *missing
capability* are the actual defects. There is also a real cross-surface
disagreement: [`jasper/correction/status.py`](../jasper/correction/status.py)
already classifies the active baseline as `managed=True` ("JTS
active-speaker baseline"), while `/sound/` calls the same file "custom".

## The design — a graph-carrier dispatcher

Invert the assumption. Today the call site hard-codes "the loaded graph
is a stereo `emit_sound_config`." Instead, the call site asks the
**currently-loaded graph kind** to re-emit *itself* with the user's
preference (and room-correction) filters applied — preserving its own
structure. Graph kinds that can safely host EQ do so; the rest fail
**closed** with an honest, typed reason.

New module **[`jasper/sound/graph_carrier.py`](../jasper/sound/graph_carrier.py)**
(sound owns preference EQ; it may import both `jasper.sound.camilla_yaml`
and `jasper.active_speaker.*` — neither imports back, preserving the
existing layering):

- `CarrierCannotHostEq(RuntimeError)` — carries a stable `reason_code`
  and a household-readable `message`; `.to_payload()` →
  `{"status": "blocked", "reason_code": ..., "message": ...}`.
- `carrier_for_loaded_config(current_path, *, config_dir) -> Carrier` —
  resolves by path **and** config *content* (never guesses), keyed on the same
  signals the safety classifier uses. The five carriers are **private**; the
  module's public surface is `carrier_for_loaded_config`, `CarrierCannotHostEq`,
  and `ReemitResult`. Resolution order is safety-critical — **content beats
  name**:
  - `is_base_config` / outputd-cutover → **base-flat carrier**
    (`reemit`: `room_peqs=[]` → `emit_sound_config`)
  - an active-speaker (roleful) graph → **active-graph carrier**, recognised by
    the **structural** signal the runtime classifier uses
    (`environment.classify_camilla_config_text(text)["classification"] ==
    "active_startup_candidate"` — the per-driver split mixer, not a `# Source:`
    comment a round-trip could strip), so the carrier and the verifier cannot
    drift (invariant 1) and a roleful graph is fenced **even when misnamed**
    like a sound/correction config. Baseline / startup / commissioning all
    match structurally; within the active branch the `# Source:` header
    (`ACTIVE_BASELINE_SOURCE`, the same signal the verifier's `is_baseline`
    branch keys on) decides which it is. *PR-1:* raised
    `CarrierCannotHostEq("eq_on_active_not_wired", …)` for all three. *PR-3
    (DONE):* a **SOLO baseline** inserts room PEQs and preference EQ pre-split
    — recomposed from the immutable applied-profile snapshot via
    [`recompose_applied_baseline_yaml`](../jasper/active_speaker/baseline_profile.py),
    NEVER through the stereo template (invariant 3). Startup/commissioning still
    refuse `eq_on_active_not_wired`; a **bonded** baseline refuses
    `eq_on_active_bonded_member` (invariant 7 — the active×grouping decision is
    deferred); a baseline whose applied snapshot is missing, stale, or invalid refuses
    `active_baseline_recompose_unavailable`. Active re-emit preserves existing
    `room_peq_*` filters by default; callers pass an explicit `room_peqs=[]`
    when they are intentionally measuring or resetting with Layer B cleared.
    All four are 200-with-body blocked outcomes, never a 5xx, and the durable
    apply's pre-check dry-runs the active carrier so a refusal records no
    `prepare_failed` state (SF-2).
  - active-leader program bake
    (`environment.CAMILLA_CLASS_PROGRAM_BAKE`) → **program-bake carrier**.
    This is a flat 2-channel program graph, but it is not DAC-bound: camilla#1
    writes `File` → Snapcast FIFO and camilla#2 owns Layer A driver protection.
    The carrier seam remains implemented, but Active's v1 manual Room authority
    is deliberately solo-only: primary `active_raw` here cannot prove the Layer
    A running on camilla#2. Active therefore projects
    `active_grouped_room_correction_not_supported` instead of a misleading
    crossover mismatch. A later Active-owned distributed identity must bind
    both daemons before Room can reach this carrier through ordinary Start.
    The carrier therefore bypasses the DAC-bound protected-tweeter flat-graph
    refusal only after grouping state resolves back to a pipe sink with
    `enable_rate_adjust=false`; otherwise it refuses
    `program_bake_pipe_unavailable`. This is the JTS5 class where
    the retained carrier can strip Layer B/C without calling the graph
    "custom" once distributed Active authority lands. The resolver also treats a JTS-generated
    `sound_current.yml` as this carrier when it is the one-time stale-marker
    recovery shape: readable protected-tweeter topology plus content proving
    `File` → Snapcast FIFO. Other generic pipe configs, such as passive
    multiroom `grouping_leader.yml`, stay in the ordinary sound/correction
    carrier and are never re-stamped as active program bakes. Room resolves
    this compatibility shape while the original filename is still available
    and stamps its collision-free running-graph snapshot with the program-bake
    source, preserving the same carrier for measurement and rollback.
  - `is_jts_generated_config` (name) → **sound/correction carrier**
    (`extract_room_peqs_from_config` → `emit_sound_config`) — today's two arms
    relocated **verbatim**, including the `member_camilla_kwargs()` splat
  - otherwise → **unknown carrier** → `CarrierCannotHostEq("unknown_config", …)`
- `Carrier.reemit(profile, *, room_peqs=None, out_path=None, profile_id=None, output_trim_db=0.0, member_kwargs=None) -> ReemitResult`
  — `out_path=None` returns YAML (live-draft); a path writes the file
  (durable), exactly like `emit_sound_config`. `ReemitResult` carries the
  emitted YAML + the room-PEQ count (telemetry). `room_peqs=None` means
  "preserve whatever the current graph carries"; an explicit list means
  "replace Layer B with exactly this set" (`[]` clears room correction for
  measurement/reset). Each carrier owns its own preservation strategy and its
  grouping kwargs: base/sound/program-bake default to
  `member_camilla_kwargs()` (a disk read); the bonded-leader bake is the one
  caller that passes `member_kwargs=member_camilla_kwargs(cfg)` explicitly (its
  pipe sink + rate_adjust off). The program-bake carrier requires those kwargs
  to describe the pipe sink; active/unknown carriers ignore them and refuse —
  see grouping boundary below.

**Stereo hosts refuse a protected-tweeter topology (L0).** The DAC-bound
stereo-host carriers (base-flat + sound/correction) emit a 2-channel program
graph with no per-driver crossover/protection, so a flat graph must never go
live when the saved output topology assigns a protected **tweeter** role —
full-range program would reach a compression driver (shrill + driver-damage
risk; see [HANDOFF-audio-measurement-core.md](HANDOFF-audio-measurement-core.md)
L0). The judgement is
[`runtime_contract.flat_program_graph_blocked_reason()`](../jasper/active_speaker/runtime_contract.py)
— a *topology* predicate, since the program lane is structurally flat — and
`_StereoHostCarrier` reads it at construction, so `can_host_eq` is `False` (the
durable pre-check refuses early, no spurious `prepare_failed`) AND `reemit`
re-asserts before emitting, so the pre-check-less **live-draft** SetConfig path
is covered too — a flat graph can never reach the DAC under a protected-tweeter
topology. The active-leader program-bake carrier is the deliberate exception:
it is flat, but it writes to the Snap FIFO rather than a DAC and must prove that
pipe-sink condition before emitting. Refusal is
`CarrierCannotHostEq("flat_graph_protected_tweeter", …)` for DAC-bound flat
hosts, or `CarrierCannotHostEq("program_bake_pipe_unavailable", …)` for a
program bake whose grouping pipe-sink predicate no longer holds. Both are
handled blocked outcomes; fail-closed on a corrupt / unreadable topology. The
refusal lives at the carrier (and, for the direct correction caller, at
`correction.runtime_safety.assert_flat_apply_safe`), **never** on the shared
`emit_sound_config` leaf — the multiroom solo-restore emit must stay lenient
(un-bonding must always succeed).

**Concrete dispatcher, not a `Protocol`/registry.** This is a 5-member
set and only the active/program-bake carriers need special behavior; per AGENTS.md
(avoid single-use abstractions) the dispatcher + recognizer is the
durable shape. Defer a `Protocol` to a "when a 3rd host kind exists"
appendix.

**Call sites.** `sound_setup.py`'s live-draft path and
`jasper.sound.runtime.load_profile_config` resolve the current graph with
`carrier_for_loaded_config(...).reemit(...)`. Map `CarrierCannotHostEq`
to a **`200` with `{status:"blocked", reason_code, message}`** (NOT a
502), so the UI renders an honest hint and the live-draft/active-speaker
UI's existing `status:"blocked"` vocabulary handles it directly. The carrier
is resolved **under the dsp-apply writer lock** so it always re-emits against
the config actually loaded — never a stereo config over an active graph a
concurrent load swapped in (a TOCTOU crossover-drop). Admission to that shared
boundary is deadline-bounded and cancellation-safe; once admitted, the caller
still owns the full mutation/confirmation/rollback transaction. The durable path also
does a **pre-transaction fast-check**: a steady-state non-hostable graph raises
`CarrierCannotHostEq` before recording an apply transaction, so a household EQ
apply on an active speaker records no `prepare_failed` state (a refusal is a
handled "blocked" outcome, not a DSP failure — jasper-doctor / `/state` stay
clean); live-draft likewise refuses inside its own writer lock. The route
discriminates `CarrierCannotHostEq` raw (the fast-check / live-draft) or via
`__cause__` (the in-lock re-check in the rare concurrent-swap race) —
`jasper/dsp_apply.py`'s public failure contract stays untouched. Collapsing
the copy-pasted branch (3 copies — see below) is itself a CLEAN win.

The HTTP `/audition` route shares this handler, so it too can return
`200 {status:"blocked"}`. No HTTP client calls `/audition` today (the
calibration-agent uses the in-process `audition_profile()` seam, which sees the
raised `CarrierCannotHostEq`); any future `/audition` HTTP client must branch
on `payload.status` the way the `/sound` page does for `/apply` and
`/live-draft`.

## Where room and preference EQ slot into the active graph (PR-3)

The active baseline pipeline
([`jasper/active_speaker/camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py),
`_emit_baseline_pipeline`) is:
`[active_baseline_headroom on channels 0,1] → split_active_<way>way (2→N)
→ per-driver [crossover LR, delay, baseline_gain, baseline_limiter] (+
wired tweeter high-pass)`.

Preference EQ + room correction are program-domain, so they MUST sit on
the program channels **before** the split mixer. Room correction (Layer B)
is per-channel PEQ from `/correction/`; preference EQ (Layer C) is the
saved `/sound/` profile. That placement is what makes both layers safe:

1. Upstream of every per-driver crossover → cannot move a crossover
   corner or leak energy into a band a driver can't handle.
2. Upstream of every per-driver `Limiter` and the tweeter HP → a
   boost cannot bypass the limiter.
3. **Explicit headroom is folded into the single
   `active_baseline_headroom` gain**, not added as a sibling pre-split
   attenuation. This gain carries baseline headroom plus the household's
   `output_trim_db` (manual headroom + loudness match), gated on the
   profile actually having EQ and any positive room boost. Preference
   boosts themselves ride at unity ("boosts boost"), while safety comes
   from their pre-split placement, the crossover/limiters/tweeter HP, and
   the 0 dB volume ceiling.

**Compose, don't text-splice.** `emit_active_speaker_baseline_config` grew
optional `room_peqs` and `preference_filters` params wired pre-split (separate
Filter steps on `[0, 1]` after the headroom step, before the split Mixer), so
all active-graph shape decisions stay in `active_speaker.camilla_yaml`. The
carrier composes via
[`recompose_applied_baseline_yaml`](../jasper/active_speaker/baseline_profile.py)
— a thin helper that rebuilds the structural baseline from the immutable preset,
corrections, playback device, and topology fingerprint captured by the explicit
Layer-A apply, then inserts the room and preference bands. Mutable design drafts,
crossover previews, and candidate measurements are deliberately not inputs, so
a later capture cannot alter production audio during an unrelated EQ recompose.
While a replacement candidate is staged, its state and content-addressed config
remain separate and the retained `applied_recomposition_profile` stays the one
carrier SSOT until apply succeeds.
The production measured-candidate lane uses this same baseline compiler and
state boundary: it requires the reviewed preset to match the compiled preset
exactly, emits the candidate's attenuation/polarity/delay corrections, and
promotes that immutable snapshot only after fresh live readback has produced a
retained applied proof. A failed or cancelled load restores the exact live
predecessor while the staged candidate continues to retain the prior applied
profile for carrier consumers.
It is a sibling of `build_baseline_profile_candidate`, **not** a new param on it:
the
durable baseline (`active_speaker_baseline.yml`, the reconcile fallback) stays
EQ-free, while the carrier writes the EQ'd baseline to `/sound`/`/correction`
apply targets. It never parses active topology out of the running config (the
extract-from-running-config anti-pattern); it only extracts current
`room_peq_*` filters when the caller asked to preserve Layer B. For the
live-draft slider, the helper returns the YAML text without a durable write.

### The safety contract (what makes PR-3 provably safe)

The verifier
([`runtime_contract.classify_camilla_graph`](../jasper/active_speaker/runtime_contract.py),
the `is_baseline` branch) **independently re-proves** the active baseline,
keyed on the `ACTIVE_BASELINE_SOURCE` header. For a valid baseline it
requires — and this is the floor PR-3 must never break, verified by the
keystone round-trip test:

- `active_baseline_headroom` wired on the program channels, with gain
  **present and non-positive** (`active_baseline_headroom_invalid` if
  missing/positive). ← folding a boost in keeps this true.
- Per tweeter output: a **wired `LinkwitzRileyHighpass`** with `freq>0`
  (this is the crossover HP, *not* a separately-named `as_tweeter_protective_hp`
  — that filter is startup/commissioning-only; the classifier *skips* the
  startup HP guard for baseline). Correcting the framing: the baseline's
  driver protection is **crossover HP + per-driver limiter
  (`clip_limit≤0`, `soft_clip`) + per-driver gain `≤0` + non-positive
  headroom + the `0 dB volume_limit` ceiling** — never a separate
  protective HP.
- Routes only outputs assigned by the saved topology
  (`active_baseline_routes_unknown_outputs`).

A clean baseline classifies `GRAPH_APPROVED_ACTIVE_RUNTIME` with
`allowed=True` (runtime_contract.py). The emitter↔verifier independence
stays — the carrier emits; the classifier re-proves. Only the
**filter-name vocabulary** is shared, via
[`jasper/active_speaker/graph_evidence.py`](../jasper/active_speaker/graph_evidence.py)
(do not hardcode spellings).

## Sharing — one stereo-domain prefix builder (PR-2, DONE)

The room-PEQ → preference → headroom assembly used to live only in
`sound/camilla_yaml.py:_emit_filter_definitions` (private, sound-only);
the active emitter duplicated none of it and shared only leaf primitives
(`jasper/camilla_emit.py`). It is now a single shared builder,
[`jasper/camilla_stereo_prefix.py`](../jasper/camilla_stereo_prefix.py):

```
build_stereo_prefix(sound_filters, room_peqs, *, room_peqs_right=None,
    output_trim_db=0.0, channel_delays_ms=None)
    -> (filters_yaml, left_chain_names, right_chain_names, trim_db)
```

`emit_sound_config` calls it (so the bonded-leader bake, which goes through
`emit_sound_config`, reuses it too); the solo-active pre-split section
(PR-3) will reuse it next. It returns filter DEFINITIONS + chain NAMES,
never the mixer/pipeline — there is no master_gain-vs-split coupling, so
each caller wires the names into its own pipeline. Output is
**byte-identical** to the prior emitter for every existing case (golden:
`tests/test_sound_camilla_yaml_golden.py`; builder unit test:
`tests/test_camilla_stereo_prefix.py`). No new DSP math — it reuses
`build_sound_filters` (the caller passes the already-built `FilterSpec`
list — **data**, not a `SoundProfile`), `total_positive_boost_db`, and the
`camilla_emit` leaves.

**Headroom-policy seam.** Active and stereo preference EQ now share the same
policy: preference boosts ride at unity, and explicit `output_trim_db` is the
only preference-layer global attenuation. Stereo room correction emits a
standalone `room_headroom` gain for the worst-case **room** boost only (`max`
over `room_peqs`/`room_peqs_right`). Active room correction folds positive room
boost into the existing `active_baseline_headroom` gain instead of adding a
sibling attenuation step; the verifier requires that headroom step to remain
present and non-positive.

**Layering note (why it's a neutral leaf module).** The builder takes the
preference `FilterSpec` list and emits it, so it needs `FilterSpec` +
`GAINLESS_BIQUAD_TYPES`. PR-2 promoted those (with `FILTER_EPSILON_DB`) from
`jasper.sound.profile` into the neutral
[`jasper/camilla_config_contract.py`](../jasper/camilla_config_contract.py)
(sibling to `PeqFilter`); `jasper.sound.profile` re-exports them, so every
existing `from jasper.sound.profile import FilterSpec` keeps working. This
keeps `camilla_stereo_prefix` (and PR-3's active emitter) free of any
`jasper.sound` import — preserving the sound→active one-way direction.

## Invariants → tests (test real things)

| # | Invariant | PR |
|---|---|---|
| 1 | `carrier_for_loaded_config` kind never disagrees with `classify_camilla_graph` on the same bytes (one classifier, no drift); a roleful/active graph is never resolved to Base/Sound | 1 |
| 3 | `emit_sound_config` is **never** called by ActiveGraph/Unknown carriers (mock + `assert_not_called`) — proves the crossover can't be silently dropped | 1 |
| 6 | ActiveGraph (pre-capability) + Unknown raise `CarrierCannotHostEq` with a **stable `reason_code`**; the route returns **200-with-body**, never 5xx — no silent failure | 1 |
| — | Recognizer mutual-exclusivity incl. an env-overridden baseline path (`JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH`) | 1 |
| — | Behavior-neutral: existing base / sound / correction apply+draft tests stay green (verbatim relocation) | 1 |
| 2 | **Keystone:** `ActiveGraphCarrier.reemit` output, fed back through `classify_camilla_graph` for the same topology, classifies `GRAPH_APPROVED_ACTIVE_RUNTIME` / `allowed=True` - inserting EQ never breaks the contract | 3 |
| 4 | Emitter-side: a `+N` dB preference boost keeps `active_baseline_headroom` unchanged without explicit trim and keeps `volume_limit == 0.0`; `output_trim_db` still folds into the headroom gain. **Test with a shelf, not just a peak** | 3 |
| 5 | The preference filter step is wired on the program channels strictly **before** the split mixer (pipeline index of pref step < Mixer step) | 3 |
| — | Active room PEQs are wired on the program channels before the split mixer; explicit `room_peqs=[]` replaces/purges existing room PEQs for measurement/reset | 2026-06-24 |
| — | Runtime verifier rejects `room_peq*` and other program-domain filters if they drift into follower/driver-domain steps | 2026-06-24 |
| — | `build_stereo_prefix` is byte-identical to today for the solo stereo case (golden) | 2 |
| 7 | Bonded-member + active baseline → `ActiveGraphCarrier` refuses with a clear reason (the deferred active×grouping decision) | 3 |

## Rollout

- **PR-1 (behavior-neutral, now):** `graph_carrier.py` (Base/SoundOrCorrection
  verbatim + ActiveGraph honest refusal + Unknown); rewire both
  `sound_setup.py` call sites; typed-200 response + UI hint. Tests 1, 3,
  6, recognizer mutual-exclusivity. Resolve the status.py-vs-`/sound`
  disagreement (the carrier recognizes the active baseline like status.py
  does). Docs: this file + README atlas + doc-map.
- **PR-1b (behavior-neutral, DONE):** the **third** copy of the 3-arm branch —
  the bonded-leader bake (`leader_config.py:apply_bonded_leader_config`) — now
  routes through the carrier. The leader passes its resolved
  `member_kwargs=member_camilla_kwargs(cfg)` (the pipe sink); a missing/flat
  current config is treated as base (no PEQs), preserving the leader's lenient
  `best_effort` read. An active/custom config now fails closed with the
  carrier's *typed* reason instead of the old "custom config" string, so a box
  running an active baseline still can't form a leader bond — the seam where
  active+leader later becomes possible. (`restore_solo_config` is deliberately
  NOT migrated: un-bonding must always succeed, so it stays lenient and never
  refuses.)
- **PR-2 (behavior-neutral, DONE):** extracted `build_stereo_prefix` into the
  neutral [`jasper/camilla_stereo_prefix.py`](../jasper/camilla_stereo_prefix.py)
  and rewired `emit_sound_config` to call it; promoted the `FilterSpec`
  contract (`FilterSpec`, `GAINLESS_BIQUAD_TYPES`, `FILTER_EPSILON_DB`) into
  `jasper/camilla_config_contract.py` (re-exported from `jasper.sound.profile`)
  so the builder stays import-clean of `jasper.sound`. Byte-identical golden
  (`tests/test_sound_camilla_yaml_golden.py`) + builder unit test
  (`tests/test_camilla_stereo_prefix.py`); existing sound tests unchanged. See
  the "Sharing" section above.
- **PR-3 (the capability — CI-green; hardware gate on jts3 pending):**
  `emit_active_speaker_baseline_config` grew `preference_filters` (pre-split,
  with boosts at unity); `recompose_applied_baseline_yaml` rebuilds the
  baseline with EQ from the immutable applied-profile snapshot; `_ActiveGraphCarrier` flips
  refuse→emit for the SOLO baseline (keyed on `ACTIVE_BASELINE_SOURCE`),
  refuses startup/commissioning (`eq_on_active_not_wired`), bonded
  (`eq_on_active_bonded_member`), and missing-evidence
  (`active_baseline_recompose_unavailable`); the durable apply pre-check
  dry-runs the active carrier (SF-2). Tests 2, 4, 5, 7 landed
  (`tests/test_active_speaker_runtime_contract.py`,
  `tests/test_active_speaker_baseline_profile.py`,
  `tests/test_sound_graph_carrier.py`). **On-device validation (jts3,
  2026-06-19, non-destructive):** against jts3's REAL applied mono active-2-way
  baseline + measured corrections, the flat recompose classified
  `GRAPH_APPROVED_ACTIVE_RUNTIME` with the then-current conservative baseline
  headroom. Current repo invariant after the zero-baseline-headroom change:
  flat recompose emits `active_baseline_headroom` at 0 dB, and a +6 dB
  preference (a +4 dB shelf + a +2 dB peak) still leaves headroom at 0 dB,
  rides pre-split, keeps `volume_limit: 0.0`, and still classifies APPROVED;
  an explicit 4 dB `output_trim_db` folds to `active_baseline_headroom: -4 dB`.
  jts3's own CamillaDSP 4.1.3 `--check` accepted the EQ'd graph ("Config is
  valid") in the prior hardware pass.
  **Still to do (human-gated):** the live load-and-listen audible smoke test
  (it changes playback on a shared lab Pi and needs a human at the speaker) and
  confirming the EQ'd baseline persists across a reconcile, before declaring the
  apply path shipped.

## Distributed active boundary

**Design-of-record:
[HANDOFF-distributed-active.md](HANDOFF-distributed-active.md).** That doc
OWNS the current runtime behavior; this section is only a terse boundary index.
Solo-active EQ remains safe in isolation because ordinary solo graphs do not
carry grouping state. Bonded active members use the distributed-active
driver-domain path: **CamillaDSP re-entry** (`snapclient → loopback → member
camilla [Layer A only] → outputd`) for active followers, and the two-Camilla
active-leader path documented in the design-of-record. Both reuse the shipped
emitter + `classify_camilla_graph` re-proof. The slices that formed the
boundary:

1. **Role / capture contract** — `OutputTopology`/commissioning gain a
   pure-data pairing-intent field; the reconciler resolves capture device
   + domain-mode per runtime role (the active emitter's `capture_device`
   param already exists; the compiler just threads it). *Slice 1.*
2. **Active follower** — the reconciler points the follower's CamillaDSP
   capture at the round-trip loopback, emits a driver-domain-only Layer-A
   graph, and disables outputd's `dac_content` `ChannelPick` on that box.
   *Slices 2–3.*
3. **Active leader** — a leader that is *also* active needs a **second**
   CamillaDSP (bake B/C → pipe; split for its own drivers), RAM-gated.
   *Slice 5.*
4. **Local sub driver** — unblock `baseline_subwoofer_not_supported` for a
   sub that is one of a single box's drivers (orthogonal to wireless).
   *Slice 6a.*
5. **Wireless sub member** — a separate bonded sub; where the LF crossover
   lives is tier-dependent (leader pre-bake for a dumb sub vs local Layer A
   for a brainy one). Its own design. *Slice 6b.*
6. **Follower-409 / delegation promise** — the POST block is already
   content-DSP-only; the gap is the page-level short-circuit hiding the
   local driver UI. Allow the active-speaker endpoints on a follower +
   render the local crossover UI so "local crossover stays with the DAC
   owner" becomes true. *Slice 4.*

## File map

- New (PR-1): [`jasper/sound/graph_carrier.py`](../jasper/sound/graph_carrier.py)
- New (PR-2): [`jasper/camilla_stereo_prefix.py`](../jasper/camilla_stereo_prefix.py)
  (`build_stereo_prefix`, `emit_filter_spec` — neutral shared program-domain
  prefix builder)
- Shared contract: [`jasper/camilla_config_contract.py`](../jasper/camilla_config_contract.py)
  (`PeqFilter`, `total_positive_boost_db`, and PR-2's promoted `FilterSpec` /
  `GAINLESS_BIQUAD_TYPES` / `FILTER_EPSILON_DB`)
- Rewire: [`jasper/web/sound_setup.py`](../jasper/web/sound_setup.py)
  (`_live_draft_profile`, `_load_profile_config`, the `/audition`,
  `/live-draft`, `/apply` POST dispatch)
- Shared durable runtime:
  [`jasper/sound/runtime.py`](../jasper/sound/runtime.py)
  (`load_profile_config`, `reconcile_current_dsp`)
- Recognizers/emitter: [`jasper/sound/camilla_yaml.py`](../jasper/sound/camilla_yaml.py)
  (`is_base_config`, `is_jts_generated_config`, `extract_room_peqs_from_config`,
  `emit_sound_config` — now calls `build_stereo_prefix`)
- Active emitter/candidate: [`jasper/active_speaker/camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py),
  [`jasper/active_speaker/baseline_profile.py`](../jasper/active_speaker/baseline_profile.py)
- Verifier + vocabulary: [`jasper/active_speaker/runtime_contract.py`](../jasper/active_speaker/runtime_contract.py)
  (`classify_camilla_graph`, `ACTIVE_BASELINE_SOURCE`, the `is_baseline`
  branch), [`jasper/active_speaker/graph_evidence.py`](../jasper/active_speaker/graph_evidence.py)
- Grouping boundary: [`jasper/multiroom/member_config.py`](../jasper/multiroom/member_config.py),
  [`jasper/multiroom/leader_config.py`](../jasper/multiroom/leader_config.py)
- UI: [`deploy/assets/sound-profile/js/main.js`](../deploy/assets/sound-profile/js/main.js)
- Tests: `tests/test_sound_graph_carrier.py` (new),
  `tests/test_sound_setup.py`, `tests/test_sound_camilla_yaml.py`,
  `tests/test_active_speaker_runtime_contract.py`,
  `tests/test_active_speaker_baseline_profile.py`

Last verified: 2026-07-15 (graph-carrier ownership rechecked against bounded,
cancellation-safe shared DSP-writer admission and the measured-candidate
baseline promotion boundary plus Room's locked Active-authority prepare guard;
carrier dispatch is unchanged)
