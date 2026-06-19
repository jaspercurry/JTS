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
*refuses* whenever an active-speaker baseline is the loaded config. Both
EQ entry points (`_live_draft_profile`, `_load_profile_config` in
[`jasper/web/sound_setup.py`](../jasper/web/sound_setup.py)) run an
identical 3-arm branch:

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
preference (and room-correction) filters folded in — preserving its own
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
  resolves by path **and** the config's `# Source:` header (never
  guesses), keyed on the same signals the safety classifier uses:
  - `is_base_config` / outputd-cutover → **BaseFlatCarrier**
    (`reemit`: `room_peqs=[]` → `emit_sound_config`)
  - `is_jts_generated_config` → **SoundOrCorrectionCarrier**
    (`extract_room_peqs_from_config` → `emit_sound_config`) — today's two
    arms relocated **verbatim**, including the `member_camilla_kwargs()`
    splat
  - `# Source:` from the active-speaker emitter module (baseline, startup, OR
    commissioning — matched by the module prefix derived from
    `runtime_contract.ACTIVE_BASELINE_SOURCE`, so **content beats a sound-like
    filename**) → **ActiveGraphCarrier** — *PR-1:* raises
    `CarrierCannotHostEq("eq_on_active_not_wired", …)`; *PR-3:* folds
    preference EQ pre-split into the active *baseline* (startup/commissioning
    stay refusing)
  - otherwise → **UnknownCarrier** → `CarrierCannotHostEq("unknown_config", …)`
- `Carrier.reemit(profile, *, out_path=None, profile_id, output_trim_db, member_kwargs=None) -> ReemitResult`
  — `out_path=None` returns YAML (live-draft); a path writes the file
  (durable), exactly like `emit_sound_config`. `ReemitResult` carries the
  emitted YAML + the preserved room-PEQ count (telemetry). Each carrier owns
  its own preservation strategy and its grouping kwargs: Base/Sound default to
  `member_camilla_kwargs()` (a disk read); the bonded-leader bake is the one
  caller that passes `member_kwargs=member_camilla_kwargs(cfg)` explicitly
  (its pipe sink + rate_adjust off). ActiveGraph/Unknown ignore it and refuse
  — see grouping boundary below.

**Concrete dispatcher, not a `Protocol`/registry.** This is a 4-member
set and only the active carrier needs new behavior; per AGENTS.md
(avoid single-use abstractions) the dispatcher + recognizer is the
durable shape. Defer a `Protocol` to a "when a 3rd host kind exists"
appendix.

**Call sites.** Replace both `sound_setup.py` triplet branches with
`carrier_for_loaded_config(...).reemit(...)`. Map `CarrierCannotHostEq`
to a **`200` with `{status:"blocked", reason_code, message}`** (NOT a
502), so the UI renders an honest hint and the live-draft/active-speaker
UI's existing `status:"blocked"` vocabulary handles it directly. The
durable path wraps the raise as `DspApplyError`; the route discriminates
`CarrierCannotHostEq` (raw, from live-draft) or via `__cause__` (from the
durable wrap) — `jasper/dsp_apply.py`'s contract stays untouched.
Collapsing the copy-pasted branch (3 copies — see below) is itself a
CLEAN win.

## Where preference EQ slots into the active graph (PR-3)

The active baseline pipeline
([`jasper/active_speaker/camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py),
`_emit_baseline_pipeline`) is:
`[active_baseline_headroom on channels 0,1] → split_active_<way>way (2→N)
→ per-driver [crossover LR, delay, baseline_gain, baseline_limiter] (+
wired tweeter high-pass)`.

Preference EQ + room correction are program-domain, so they MUST sit on
the program channels **before** the split mixer. That placement is what
makes them safe:

1. Upstream of every per-driver crossover → cannot move a crossover
   corner or leak energy into a band a driver can't handle.
2. Upstream of every per-driver `Limiter` and the tweeter HP → a
   preference *boost* cannot bypass the limiter.
3. **Headroom is folded into the single `active_baseline_headroom`
   gain**, not added as a sibling pre-split attenuation. Reduce available
   pre-split headroom by `total_positive_boost_db(preference + room)`
   (the exact mechanism `emit_sound_config` uses for room boosts), so the
   corrected program cannot exceed unity before the split.

**Compose, don't text-splice.** Grow `emit_active_speaker_baseline_config`
/ `build_baseline_profile_candidate` an optional `preference_filters`
param wired pre-split, so all active-graph shape decisions stay in
`active_speaker.camilla_yaml`. `ActiveGraphCarrier.reemit` composes
from the **saved baseline candidate** (`build_baseline_profile_candidate`),
not from an extracted running config.

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

## Sharing — one stereo-domain prefix builder (PR-2)

Today the room-PEQ → preference → headroom assembly lives only in
`sound/camilla_yaml.py:_emit_filter_definitions` (private, sound-only);
the active emitter duplicates none of it and shares only leaf primitives
(`jasper/camilla_emit.py`). To make EQ/room-correction a genuinely shared
pipeline, extract that assembly into a builder, e.g.
`build_stereo_prefix(profile, room_peqs, *, room_peqs_right=None,
output_trim_db=0.0, channel_delays_ms=None) -> (filters_yaml,
left_chain_names, right_chain_names, trim_db)`, **byte-identical** to
today's output for the solo case (golden-tested). Reused by: plain
stereo (`emit_sound_config`), the bonded-leader bake
([`jasper/multiroom/leader_config.py`](../jasper/multiroom/leader_config.py)),
and the solo-active pre-split section (PR-3). No new DSP math — it reuses
`build_sound_filters`, `total_positive_boost_db`, and the `camilla_emit`
leaves.

## Invariants → tests (test real things)

| # | Invariant | PR |
|---|---|---|
| 1 | `carrier_for_loaded_config` kind never disagrees with `classify_camilla_graph` on the same bytes (one classifier, no drift); a roleful/active graph is never resolved to Base/Sound | 1 |
| 3 | `emit_sound_config` is **never** called by ActiveGraph/Unknown carriers (mock + `assert_not_called`) — proves the crossover can't be silently dropped | 1 |
| 6 | ActiveGraph (pre-capability) + Unknown raise `CarrierCannotHostEq` with a **stable `reason_code`**; the route returns **200-with-body**, never 5xx — no silent failure | 1 |
| — | Recognizer mutual-exclusivity incl. an env-overridden baseline path (`JASPER_ACTIVE_SPEAKER_BASELINE_CONFIG_PATH`) | 1 |
| — | Behavior-neutral: existing base / sound / correction apply+draft tests stay green (verbatim relocation) | 1 |
| 2 | **Keystone:** `ActiveGraphCarrier.reemit` output, fed back through `classify_camilla_graph` for the same topology, classifies `GRAPH_APPROVED_ACTIVE_RUNTIME` / `allowed=True` — folding EQ never breaks the contract | 3 |
| 4 | Emitter-side: a `+N` dB preference boost reduces pre-split headroom by `≥ total_positive_boost_db(prefs)` and keeps `volume_limit == 0.0`. **Test with a shelf, not just a peak** | 3 |
| 5 | The preference filter step is wired on the program channels strictly **before** the split mixer (pipeline index of pref step < Mixer step) | 3 |
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
- **PR-2 (behavior-neutral):** extract `build_stereo_prefix`; rewire
  `emit_sound_config` to call it. Golden test.
- **PR-3 (the capability, hardware-gated on jts3):** `preference_filters`
  pre-split + folded headroom; `ActiveGraphCarrier` composes from the
  saved candidate; refuse if bonded-member. Tests 2, 4, 5, 7. Validate
  the program-level EQ placement + headroom math on real hardware before
  ship.

## Deferred — distributed active (separate design increment)

These are unbuilt/undesigned today; the active and multiroom subsystems
have **zero cross-references**. Solo-active EQ (PR-1→3) is safe in
isolation precisely because active configs are currently *fenced off*
from grouping (the leader bake refuses them; a follower parks its
CamillaDSP). Each item below is a roadmap decision, not in scope here:

1. **Commissioning role capture.** `OutputTopology`/commissioning have no
   solo / will-be-follower / has-a-follower field; the active baseline's
   *capture* is hard-defaulted to the solo fan-in (`plug:jasper_capture`)
   and no caller can override it. Distributed-active needs this bit
   (it gates capture device + host-B/C-vs-delegate). **Slice 1** of this
   increment.
2. **Active follower.** The streamed program reaches a follower at
   `jasper-outputd`'s `dac_content` lane (post-CamillaDSP), where the only
   transform is `ChannelPick` (no filtering). Running Layer A on a
   follower needs either a per-driver split *in outputd* (Rust) or
   re-entry of streamed PCM into the follower's CamillaDSP (capture
   repoint), plus role-aware emission and reconsidering the blanket
   follower-409 (which currently also blocks follower-*local*
   crossover/driver edits).
3. **Active leader.** A leader that is *also* an active speaker must both
   bake B/C into the streamed program and run a local split for its own
   drivers — two emitters that don't compose today.
4. **Sub as 4th driver role.** Data-modeled and protection-bounded, but
   the active compiler hard-blocks it (`baseline_subwoofer_not_supported`).
5. **Wireless sub crossover.** `sub` currently → `ChannelPick::Mono`
   (full-range mono, no LF crossover); the 80 Hz LR4 lowpass exists only
   in the dead `channel_split` recipe. Undecided which box applies it.
6. **Doc↔code gap:** the follower delegation page promises "local
   crossover and driver-protection work stays with the speaker that owns
   the DAC path" — a promise no code keeps yet. Align when (2) lands.

## File map

- New: [`jasper/sound/graph_carrier.py`](../jasper/sound/graph_carrier.py)
- Rewire: [`jasper/web/sound_setup.py`](../jasper/web/sound_setup.py)
  (`_live_draft_profile`, `_load_profile_config`, the `/audition`,
  `/live-draft`, `/apply` POST dispatch)
- Recognizers/emitter: [`jasper/sound/camilla_yaml.py`](../jasper/sound/camilla_yaml.py)
  (`is_base_config`, `is_jts_generated_config`, `extract_room_peqs_from_config`,
  `emit_sound_config`, the `room_headroom`/`total_positive_boost_db` block)
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
  `tests/test_active_speaker_runtime_contract.py`

Last verified: 2026-06-19
