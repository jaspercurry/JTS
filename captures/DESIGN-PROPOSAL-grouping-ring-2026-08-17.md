# Phase 1 design — the bonded grouping round-trip moves onto a ring

**Version: v2.1** (2026-08-18). v1 was reviewed at
`captures/DESIGN-REVIEW-grouping-ring-2026-08-18.md` and returned **not sealed**
(2 blockers / 9 should-fixes / 6 nits / 4 confirmed-stronger); v2 answered all of them and
the delta re-review (same file, "DELTA RE-REVIEW — v2") returned **0 blockers / 1
should-fix / 4 nits**, with both blocker resolutions holding under re-derivation. v2.1
closes those five. Per the P8B precedent, **withdrawn arguments stay visible as withdrawn**
rather than being silently rewritten.

**Pinned SHA:** `6e569e8dc8e572a8d648d332c414374b8394496e`
(verified by `git rev-parse HEAD` in
`/Users/jaspercurry/Code/JTS/.claude/worktrees/loopback-retirement-ring-3a4b6e`
before the first read of both rounds; matched exactly.)

**Scope:** #2481 (grouping ingress off aloop pair 6 onto a ring) + #2508 (remnant EOL +
`HANDOFF-multiroom.md` truing) + #2581 (the bonded round-trip's first hardware pass).
**Not** multiroom v2, not a snapcast replacement, not the Phase-2 coupling deletion,
not N>2.

**Sources cited, never restated:** `captures/LOOPBACK-CENSUS-2026-08-17.md`,
`captures/RESEARCH-rate-tracking-ring-2026-08-17.md`,
`captures/P8B-DESIGN-PROPOSAL-2026-08-14.md` §5,
`captures/PLAN-loopback-retirement-2026-08-18.md`,
`captures/DESIGN-REVIEW-grouping-ring-2026-08-18.md`.

**Honesty legend.** `[M]` measured — read from source at this SHA, file:line given.
`[I]` inferred — the reasoning is stated inline; not observed. `[N]` absent — searched
and not found; the search scope is named. **Every figure the review supplied was
re-derived here rather than inherited**; where my re-derivation disagrees with the
review, §14 says so with evidence.

---

## v1 → v2 changelog

| # | v1 said | v2 says | Why |
|---|---|---|---|
| **C-1** | §3.2: the ring geometry is **forced** by the emitter's 1024 chunk floor → `period_frames 1024 / n_slots 2`. | **WITHDRAWN.** The floor is an snd-aloop artifact, not a geometry input. Chunk on the ring path is **128**, and the geometry is **`period_frames 128 / n_slots 16`**. | B2. The repo had already adjudicated this and v1 missed the owner: `_effective_camilla_chunksize_setting` (`audio_runtime_plan.py:2754-2786`) forces `RING_CAMILLA_CHUNKSIZE` under `shm_ring` and calls 1024 *"the loopback/hardware-floor value, not the ring runtime value"* `[M]`. v1 would also have put chunk 1024 into the ACTIVE ring's 256-frame playback buffer. |
| **C-2** | §3.5: *"the chunk floor is renamed and re-justified, not removed"*; §7.2 pinned `chunk == GROUPING_RING_PERIOD_FRAMES` as an equality either way. | **WITHDRAWN.** `FOLLOWER_LOOPBACK_MIN_CHUNKSIZE` and its clamp are **deleted**. The equality pin survives, but as `128 == 128` on the ring branch, not as a frozen 1024. | B2. Its only use site is `camilla_yaml.py:3660-3661`, reached only by `driver_domain=True`, whose only two production callers both move to the ring in this phase `[M]` — the condition becomes unreachable. |
| **C-3** | §5.1 fact 3: *"on a bonded **active endpoint**, `shm_ring` changes exactly one thing"*, evidenced from the follower. | **AMENDED — the sentence was true for the follower and false for the leader.** A leader also runs camilla#1 from `emit_active_speaker_program_bake_config`, whose `capture_device` defaults to `plug:jasper_capture` — the tap fan-in stops feeding under `shm_ring`. §5.2 now carries the fix. | B1. Confirmed at `active_leader_config.py:325-330` (no `capture_device` passed) + `camilla_yaml.py:3796-3803` (the default) + `camilla_config_contract.py:26` `[M]`. |
| **C-4** | §3.2 `[I]`: snapclient's delay sample is **phase-locked** because POLLOUT is granted *"exactly when a slot frees"*. | **WITHDRAWN — the cited mechanism is not in the file.** POLLOUT is granted whenever a publish would not block (`pcm_jts_ring.c:891-902`) `[M]`. §3.2 now states the real physics: `.delay` = `occupancy_slots × period + stage_frames`, so **the slot size bounds the coarseness of the sync signal**. | S7. This is what makes 128 the right period and 1024 the wrong one — the corrected physics argues *for* the new geometry, not against it. |
| **C-5** | §3.2: the ioplug tick at period 1024 is *"5.33 ms ≈ 188 Hz — at or below snapclient's own wake rate"*, cited `pcm_jts_ring.c:866-879`. | **CORRECTED.** `tick_ns_for` clamps to [0.25 ms, 2 ms] (`pcm_jts_ring.c:264-272`) `[M]`. At 128 the tick is 0.667 ms → **~1500 Hz**, which is what Ring A already imposes on every armed box today. The "at or below snapclient's wake rate" comparison is dropped. | S1. My citation pointed at `poll_descriptors`, not the cadence owner. |
| **C-6** | §3.5: `RestartSec=3s`, and the `StartLimitBurst=4` number was quoted but not acted on. | **EXTENDED.** `StartLimitBurst` → **6**, satisfying the platform's `> 5` ring-writer rule, alongside `RestartSec=3s` and `UMask=0007`. | S2. |
| **C-7** | §9 had no entry for `c/jts-ring-ioplug/jts_ring_shm.h`. | **ADDED**, plus the guard that owns it: the writer-cadence enumeration becomes executable rather than prose, so the ADD direction is caught. | S3. |
| **C-8** | §5.2: *"`solo`/`unknown` keep their existing fail-open behaviour verbatim"* — `invalid_grouping` unnamed. | **NAMED.** `invalid_grouping` flips from blocked to allowed; the derivation and why it is safe are stated, and T-5 now **derives** the predicate from a `GroupingConfig` fixture instead of parametrizing a free boolean. | S4. |
| **C-9** | §5.2: D5's reorder is *"a statement reorder, not a behaviour change"*. | **CORRECTED.** It changes the operator-visible reason token in one reachable cell. The change is stated, justified, and pinned. | S5. |
| **C-10** | §9 named six `HANDOFF-distributed-active.md` sites and claimed *"Phase 1 owns all five [solo-stereo-only sites] — none rides to Phase 2"*. | **WITHDRAWN as a completeness claim.** §9 is retitled **the change set**, marked explicitly non-exhaustive for prose, and PR-6's sweep is named as the owner of completeness. The six-vs-thirteen gap, the two conflated tokens, `jasper/sound/graph_carrier.py:414`, `member_config.py` and the additions are all folded in. | S6. |
| **C-11** | §10.2 implied both boxes exercise the grouping ring. | **CORRECTED.** jts4 is a passive dumb member; the grouping ring is opened on **one** box. The evidence scope is stated in the plan and will be stated in the evidence file. | S8. |
| **C-12** | §8.2 named five touched test files. | **EXTENDED to eight** — `tests/test_multiroom_active_leader_config.py`, `tests/test_multiroom_rate_adjust.py`, and **`tests/test_active_speaker_driver_domain.py`** (which the review did not find; see §14). | S9 + my own finding. |
| **C-13** | §10.2 ran S5 as the first spike, reading `grouping_follower.yml`. | **RESTRUCTURED.** That file cannot exist on this fleet in any phase; S5 is demoted to an opportunistic pre-deploy hard-sync baseline. | A1. |
| **C-14** | EG-6 was surfaced and left. | **Fixed, in its own single-gate PR outside the stack.** | A2. |
| **C-15** | §13 posed four owner decisions. | **All four are RULED** (jts.local = leader; baseline applied at step 0.2; dummy loads stay; #2481 closes narrow). §13 records them as settled context. | Owner. |
| **C-16** | §5.2 passed `dac_content_lane_armed` as a bool without saying why. | The bool parameter is stated as a **load-bearing import-direction constraint**. | A3-iii. |
| **C-17** | six smaller claims (ioplug key list, PR-1 "inert", five citation drifts, EG-3's pointer, the UMask invariant, the fallback's "proven"). | All corrected in place. | N1-N6. |

## v2 → v2.1 changelog

| # | v2 said | v2.1 says | Why |
|---|---|---|---|
| **D-1** | §7.2's T-8 kill row: *"pass the resolver's full dict → `emit_sound_config` must raise"*. | **REWRITTEN as T-8a / T-8b, each naming its emitter.** The guard on this path is a **`TypeError`** at the bake call site — `emit_active_speaker_program_bake_config` declares no playback kwargs at all (`camilla_yaml.py:3796-3809`, verified: zero `playback`/`queuelimit`/`enable_rate_adjust` parameters) `[M]`. `emit_sound_config`'s `ValueError` is demoted to **sibling evidence**, with an explicit "do not satisfy T-8b through it" note. | ND-A (should-fix). `emit_sound_config` is the *passive*-leader pipe emitter, which the narrowed gate still blocks — an implementer could route the mutation through it and bank a green kill that proves nothing about the bake. |
| **D-2** | §5.2(a): *"The leader's program bake becomes its fourth caller."* | **RECOUNTED.** The resolver has **five** existing production call sites — method shown, all five cited — so the bake is the **sixth call site** and the **fourth class of caller**. v2 conflated the two counts. | ND-1. |
| **D-3** | §5.2(a)'s decline: *"a 1024-frame chunk reading Ring A's 128-frame slots"*. | **CORRECTED, and the residual is smaller than v2 stated.** The bake's chunk resolves to the active DAC profile's `LatencyFloor.camilla_chunksize` on the unset-env path (`camilla_config_contract.py:212`) `[M]`; on jts.local that is **256** (`dac.py:385-390`) `[M]`, i.e. **two** slots per chunk. 1024 is the fleet worst case, not the box under test. | ND-2 — accepted in direction, **refuted in value**: the reviewer's replacement number was 128, which is the Apple profile's `outputd_period_frames`, not its `camilla_chunksize`. See §14 item 6. |
| **D-4** | §3.2 took `n_slots = 16` without noting it is the ceiling. | **CONSTRAINT STATED.** 16 is `JTS_RING_MAX_SLOTS` exactly, so depth and sync-coarseness are no longer independently tunable upward, and buying more depth is a three-language constant bump (C header + `rust/jasper-ring/src/layout.rs:55` + `rust/jasper-outputd/src/config.rs:65`) with `tests/test_ring_slot_ceiling_pin.py` and the header's `_Static_assert`s. | ND-3. Visible at design time rather than met mid-soak. |
| **D-5** | PR-6's row: *"`HANDOFF-distributed-active.md`'s **thirteen** sites"*. | **nineteen**, matching §14 item 4. | ND-4 — v2's own corrected number contradicted three sections later. |

**Unchanged and not relitigated** (the review's settled list): §3.1 (own conf.d file; not
joining `RING_CONF_PCMS` / `RING_PCM_DEVICES`), §3.3 (S16_LE), §3.4's no-`rm -f`
decision, §4's deletion of the two env overrides, §5's *shape* (one predicate, two
consumers), §6.1's PR-ordering, §8.1's probe-not-pin, the #2672 pins, the strike ladder.

---

## 0. Premise corrections

Seven inherited claims are false or incomplete at this SHA. **None is a STOP** — each is
resolved below.

### PC-1 — the 1024 clamp is a **defect report**, not a measurement *(restated in v2)*

v1 discovered that `emit_active_speaker_driver_domain_config` clamps the chunk to 1024 —

```python
    # S1: floor the loopback capture chunksize (see FOLLOWER_LOOPBACK_MIN_CHUNKSIZE).
    if chunksize < FOLLOWER_LOOPBACK_MIN_CHUNKSIZE:
        chunksize = FOLLOWER_LOOPBACK_MIN_CHUNKSIZE
```

`jasper/active_speaker/camilla_yaml.py:3660-3661`, constant at `:190` `[M]` — and
correctly reported that the research memo's "chunk 128" was wrong. **v1 then treated the
clamp as the geometry's forcing input. That was the error.** Four facts, each verified
directly in this round:

1. **It is a snd-aloop-EPIPE artifact by its own words.** `camilla_yaml.py:183-189`:
   *"the follower driver-domain graph captures the leader's stream from an snd-aloop
   loopback whose period underruns (EPIPE) below ~1024 frames"* `[M]`. The ring raises no
   EPIPE at all — zero `EPIPE`/`ESTRPIPE`/`XRUN` in any plugin C file `[N]`.
2. **The repo has already adjudicated the ring's chunk, and it is 128.**
   `jasper/audio_runtime_plan.py:2754-2786`, `_effective_camilla_chunksize_setting`,
   docstring *"Return the Camilla chunksize generated YAML actually emits"*, forces
   `RING_CAMILLA_CHUNKSIZE` under `shm_ring` and warns that any other value *"is the
   loopback/hardware-floor value, not the ring runtime value"* `[M]`. **This is a second
   owner of the same fact that v1 did not find**, and it names 1024 as the loopback value
   in so many words.
3. **The solo path has no clamp.** `git grep FOLLOWER_LOOPBACK_MIN_CHUNKSIZE` returns the
   definition (`camilla_yaml.py:190`) and one use site `[M]`. The `chunksize=devices.chunksize`
   / `target_level=` forward appears **four times** in `baseline_profile.py` (`:2397-2398`
   driver-domain, `:2413-2414` `emit_active_speaker_baseline_config`, `:2835-2836`, `:2988-2989`)
   `[M]` — so the solo roleful graph on a ring-armed box receives the identical value and
   emits **128**, and only the driver-domain branch is clamped. So today's shipped production graph is the coherent chunk 128 / target 128 /
   buffer 256, and 1024 exists only on the path the gate currently makes unreachable.
4. **1024 would land in a 256-frame playback buffer.** The driver-domain graph's playback
   is the ACTIVE ring at `period_frames 128 / n_slots 2`
   (`deploy/alsa/conf.d/60-jts-ring.conf`) `[M]`, and the repo's rule is in the same file:
   *"chunk 256 would span the entire 2-slot buffer"* `[M]`.

**Restated PC-1: the clamp is the one thing on this path that must not survive the
migration.** §3.2 re-derives the geometry from chunk 128; §3.5 deletes the clamp.

### PC-2 — `grouping_allowed=false` on jts.local is NOT the ring/bond interlock *(unchanged)*

`grouping_allowed` is produced at three sites in `jasper/active_speaker/setup_status.py` —
`False` on unreadable topology (`:731`), `True` for a passive box (`:767`), and
`not blocked` for an active speaker (`:1141`) `[M]`. Its blockers are active-speaker
**commissioning readiness**; the message names grouping explicitly (`:1025-1032`) `[M]`.
Its one consumer is `_active_speaker_grouping_evaluation`
(`jasper/control/server.py:354-370`), enforced at
`jasper/control/handlers/grouping.py:174-181` keyed **only** on `body.get("enabled")` —
no role discrimination `[M]`. It blocks **both** roles and never blocks unbonding.
jts.local reads `false` because its active graph is `active_speaker_staged_startup.yml`,
a staged safety graph. **Hardware-plan prerequisite; owner-ruled as step 0.2.**

### PC-3 — P8B risk 5.3 is already fixed at HEAD *(unchanged)*

`deploy/systemd/jasper-snapclient.service` documents two player shapes; "NOT an ALSA
sink" is scoped to the DUMB branch (`:51-57`) and the active branch says *"That IS an
ALSA sink"* (`:60-66`) `[M]`. Repo-wide `NEVER an ALSA sink` → zero `[N]`. **Risk 5.3
closes with no work.** The pair-6 prose in that comment is ordinary Phase-1 work (§9).

### PC-4 — the memo's leader/follower inversion is unnecessary — **and needs a second half** *(amended in v2)*

**First half, confirmed.** An active-speaker LEADER traverses the same ingress through
camilla#2 with the identical call —
`build_baseline_profile_candidate(…, capture_device=GROUPING_LOOPBACK_CAPTURE,
capture_format=GROUPING_LOOPBACK_CAPTURE_FORMAT, driver_domain=True, …)` at
`active_leader_config.py:255-268` versus `follower_config.py:205-219` `[M]`. Same emitter,
same L0 tweeter gate, same graph shape.

**Second half, new.** The leader **also** runs camilla#1 from a *different* emitter whose
capture default is coupling-blind. So the correct statement is: *the leader exercises the
same ingress, **plus** a second CamillaDSP instance the ingress argument does not cover.*
That second instance is B1, and §5.2 fixes it. OD-1's recommendation survives — it is
true of camilla#2 — but it is no longer the whole story.

### PC-5 — pair 6 loses its only doctor registration *(unchanged; review C1 strengthened it)*

`_derive_registered_pairs` (`jasper/cli/doctor/grouping.py:1069-1108`) derives the
registered set from three contributors, and pair 6's only contributor is
`GROUPING_LOOPBACK_PLAYBACK` `[M]` — while pair 6 is also the AXIS-1 passive content lane
every loopback-coupled box opens. **Pair 6 must be re-sourced before the transport flip.**
The review adds, and I confirm, that this is a **bigger edit than v1 costed**:
`grouping_pair` is the function's second return value, read at `:1189`, `:1248`, `:1256`,
`:1259`, with a *"deliberately NO grouping-pair-missing branch"* comment at `:1190-1195`
and a test (`test_grouping_pair_is_always_registered`) that asserts the opposite of the
new state. §6.1's cost estimate is corrected accordingly.

### PC-6 — jts4 is a passive dumb member; the grouping ring is opened on one box *(new in v2)*

`_assemble_args` sends a box down the `--soundcard` ALSA path **only** when
`active_endpoint` is true (`jasper/multiroom/reconcile.py:582-589`); otherwise it takes
`--player file:filename=` into the member FIFO and never opens the grouping ring `[M]`.
`active_endpoint = active_follower or active_speaker_leader` (`:1834`), and
`active_follower = active and cfg.role == "follower" and box_is_active` (`:1822`), where
`box_is_active` comes from `_active_speaker_box_state()` reading the saved output
topology `[M]`.

The fleet probe records jts4 running `sound_current.yml` — the solo/passive sound config,
not an active-speaker graph — with `grouping_allowed = true`, which for a passive box is
the early return at `setup_status.py:767` (`active_group_count == 0`) `[M/I]`.
**Conclusion: jts4 is passive → a dumb member → the active-follower ingress is not
exercised on this fleet.** §10.2 step 0.4 states the command that establishes this before
the pass begins, and §10.3 states the resulting evidence scope. This is a scope fact, not
a blocker: the leader's camilla#2 runs the byte-identical emitter call (PC-4 first half),
so the code path closes on the leader instance plus review, and the *role* instance is
recorded as undemonstrated.

### PC-7 — three test modules break, not two *(new in v2)*

The review named `tests/test_multiroom_active_leader_config.py` (`:160`, `:188`) and
`tests/test_multiroom_rate_adjust.py` (`:164-172`). A third breaks on the clamp deletion
and is named nowhere: **`tests/test_active_speaker_driver_domain.py`**, which imports
`FOLLOWER_LOOPBACK_MIN_CHUNKSIZE` at `:228` and `:245` and asserts the emitted chunksize
equals it at `:233`, `:238`, `:248` `[M]`. §8.2 assigns all three.

---

## 1. Current state, cited

The bonded active-endpoint chain, as built `[M]`:

```
leader  : fanin → camilla#1 (plug:jasper_capture → Layer B/C → File sink)
                → /run/jasper-snapserver/snapfifo
        → snapserver --stream.source pipe://…&sampleformat=48000:16:2&codec=flac
                     --stream.buffer 400                         (reconcile.py:453-487)
        → LAN
endpoint: snapclient --host <leader> --latency <cfg.client_latency_ms>
                     --soundcard hw:Loopback,0,6 --player alsa    (reconcile.py:582-589)
        → snd-aloop pair 6
        → CamillaDSP captures hw:Loopback,1,6 (S16_LE, raw hw:, no plug)
        → Layer A (channel-select → 2→N split → per-driver crossover/limiter/tweeter HP)
        → jts_ring_active_playback → jasper-outputd → DAC(s)
```

**Two CamillaDSP instances on an active-speaker leader**, and only one is the endpoint:
camilla#1 is the *program bake*
(`emit_active_speaker_program_bake_config`, `active_leader_config.py:325-330`), camilla#2
is the *driver domain* (`build_baseline_profile_candidate(driver_domain=True)`,
`:255-268`) `[M]`. An active **follower** runs only the driver-domain instance, as its
camilla#1. A **dumb member** takes the FIFO path and touches neither pair 6 nor the ring.
`active_endpoint = active_follower or active_speaker_leader` (`reconcile.py:1834`) is the
sole discriminator `[M]`.

Transport identity, `jasper/multiroom/reconcile.py:214-225` `[M]`:
`GROUPING_LOOPBACK_PLAYBACK` (`hw:Loopback,0,6`, env-overridable at import),
`GROUPING_LOOPBACK_CAPTURE` (`hw:Loopback,1,6`, same), `GROUPING_LOOPBACK_CAPTURE_FORMAT`
(`"S16_LE"`, not env-overridable).

Ring platform: three PCMs in `deploy/alsa/conf.d/60-jts-ring.conf`, all
`period_frames 128 / n_slots 2 / format S32_LE` `[M]`; four renderer-lane PCMs in
`61-jts-renderer-lanes.conf` at `period_frames 256 / n_slots 16` under their own registry
`[M]`. The directory is `/dev/shm/jts-ring` at `3775 root jts-ring` `[M]`. The ioplug
accepts `path`, `period_frames` (1..65536), `n_slots` (2..16), `format`
(`S16_LE`|`S32_LE`), `channels` (2..8), **skips `type`, `comment` and `hint`**
(`pcm_jts_ring.c:1035-1036`) and refuses any other key with `-EINVAL` (`:1031-1105`)
`[M]` *(N1)*.

Mutual exclusion, five sites `[M]`: D1 `jasper/multiroom/reconcile.py:1891-1920`
(hand-rolled, role-agnostic, `fall_back_to_solo()`); D2
`jasper/audio_runtime_plan.py:817-848` (`coupling_supported_for_route` — the policy SSOT);
D3 `jasper/fanin/coupling_reconcile.py:911-928` → `_block_unsupported_coupling`; D4
`ring_route_ready` (`:3396-3421`); D5 `jasper/multiroom/active_leader_config.py:211-219`.

---

## 2. Target state

```
endpoint: snapclient --host <leader> --latency <cfg.client_latency_ms>
                     --soundcard jts_ring_grouping --player alsa
        → /dev/shm/jts-ring/grouping.ring   (128-frame slots × 16, S16_LE, 2 ch)
        → CamillaDSP captures jts_ring_grouping (S16_LE, chunk 128)
        → Layer A (unchanged)
        → jts_ring_active_playback → jasper-outputd → DAC(s)

leader's camilla#1: capture follows the live coupling
        (jts_ring_capture under shm_ring, plug:jasper_capture under loopback)
        → Layer B/C → File sink → snapfifo          [B1's fix]
```

One transport for the grouping ingress on every box. snd-aloop pair 6 keeps exactly one
consumer (the AXIS-1 passive content lane) until Phase 2. snapclient is the sole
rate-tracker everywhere, by construction — which makes
`docs/HANDOFF-multiroom.md:630-631`'s inv-5 sentence literally true for the first time.

---

## 3. Design Q1 — the grouping ring

### 3.1 Where it lives *(settled in review; unchanged)*

**`deploy/alsa/conf.d/62-jts-ring-grouping.conf`, defining `pcm.jts_ring_grouping`.** Not
a fourth block in `60-jts-ring.conf`: `render_ring_conf_wire`
(`jasper/ring_assets.py:916-948`) raises `ValueError` for any `RING_CONF_PCMS` member
without a `per_block` width entry `[M]`, and `61-jts-renderer-lanes.conf` is the shipped
precedent for a second ring geometry under its own registry `[M]`. The grouping ring is
**not** added to `RING_CONF_PCMS` or `RING_PCM_DEVICES` — the latter is what
`active_emit_devices` tests to pick the ring device profile (`camilla_yaml.py:466`), and
adding a *capture* device there would be a defect.

### 3.2 Geometry — re-derived from the corrected chunk and the corrected physics

> **v1's derivation is WITHDRAWN.** v1 read the chunk floor as the forcing input and
> proposed `period_frames 1024 / n_slots 2`. PC-1 shows the floor is the artifact, not the
> input. v1 also argued a phase-lock that the code does not implement (C-4). Both are
> replaced below; the *depth* argument (~42.7 ms for a network-fed writer) survives, and
> the corrected physics now argues **for** a small slot rather than being neutral about it.

**Input 1 — the chunk is 128.** `active_emit_devices`' ring branch returns
`chunksize=RING_CAMILLA_CHUNKSIZE` (=128, `jasper/fanin_coupling.py:94`) `[M]`;
`baseline_profile.py:2397` forwards it `[M]`; §3.5 removes the clamp that overrode it; and
`_effective_camilla_chunksize_setting` already declares 128 to be *the* ring runtime value
`[M]`.

**Input 2 — the slot size bounds the sync signal's coarseness.** `.delay` is
`occupancy_slots × period_frames + stage_frames` (`pcm_jts_ring.c:455-457`), and
`stage_frames` returns to 0 on every full-slot publish (`:425-430`) `[M]`. The occupancy
term is slot-quantized; the stage term is frame-granular. snapclient consumes that value
as time-to-DAC (`client/stream.cpp:379-381`) against hard-sync medians of **2 ms** (long)
and **5 ms** (short) `[M, memo §1.5]`. So the coarse component of the signal is one slot:
2.667 ms at 128 frames, 5.33 ms at 256, **21.3 ms at 1024**. v1's geometry would have put
a 21.3 ms quantum into a loop whose thresholds are 2 ms and 5 ms. `[I]` on the
consequence; `[M]` on both mechanisms.

**Input 3 — depth is the jitter budget, and it is independent of slot size.** The writer
is network-fed and cannot be assumed co-scheduled with its reader (unlike fan-in), which
is the memo's Architecture-B risk 3.

| Parameter | Value | Derivation |
|---|---|---|
| `period_frames` | **128** | = the ring path's chunk (input 1) — one slot per chunk, the relationship every other ring in the tree ships. Also the finest coarse-component the plugin allows at this chunk (input 2). |
| `n_slots` | **16** | `JTS_RING_MAX_SLOTS` (`jts_ring_shm.h:90`) `[M]`; buffer = 2048 frames = **42.67 ms** (input 3), the same depth the renderer-lane rings give their network-fed writers. **This is the hard ceiling — see the constraint below.** |
| `format` | **S16_LE** | §3.3 (settled) |
| `channels` | **2** | the snapcast stream is `sampleformat=48000:16:2` (`reconcile.py:470`) `[M]` |
| `path` | `/dev/shm/jts-ring/grouping.ring` | §3.4 (settled) |

What each number buys, and its cost:

- **Scheduling margin: 42.67 ms**, against the 5.33 ms a naive 2-slot copy of Ring A gives.
  Note this is the *same* buffer depth v1 proposed (2×1024 = 16×128 = 2048 frames) —
  v2 keeps the depth and makes the slot 8× finer.
- **Sync-signal coarseness: 2.667 ms**, below the 5 ms short-median and just above the 2 ms
  long-median. `[I]` A symmetric one-slot wander is ±1.33 ms about the mean, inside both.
- **snapclient's negotiation.** It asks `period_time` 20000 µs and `buffer_time` 80000 µs
  (`client/player/alsa_player.cpp:41-43`) then clamps with `_near` `[M, memo §1.5]`.
  Against single-valued constraints it lands at 2667 µs / 42667 µs. It writes
  `framesAvail` per iteration, not one period, so its own wake rate is not pinned to the
  period `[M, memo §1.5]`.
- **The cost, stated plainly: the ioplug tick is 0.667 ms → ~1500 Hz.** `tick_ns_for`
  computes `period/4` clamped to [0.25 ms, 2 ms] (`pcm_jts_ring.c:264-272`) `[M]`. At 1024
  it would have been 500 Hz (not v1's 188 Hz — C-5). **This is not a new cost class:
  Ring A ships `period_frames 128` and CamillaDSP polls it on every armed box in the
  fleet today** `[M]`, so a 1500 Hz ioplug tick is the measured-in-production rate on this
  exact plugin and hardware. S3's CPU bar measures it for snapclient specifically.
- **The absolute delay stays far from the one hard gate.** Max reportable delay =
  16×128 + 127 = 2175 frames = **45.3 ms**, against `bufferMs_` = 400 ms
  (`jasper/multiroom/config.py:72`), the threshold at which snapclient outputs no chunk at
  all (`client/stream.cpp:260-266`) `[M]`.

**Named fallback if S0 falsifies the negotiation:** `period_frames 256 / n_slots 16`
(4096 frames = 85.3 ms; tick 1.33 ms → 750 Hz; coarse component 5.33 ms, i.e. *at* the
short-median threshold — so the fallback trades sync granularity for CPU, and that trade
must be re-measured, not assumed). The review's C4 independently confirms this geometry is
legal against a larger chunk, because chunk == one slot is **not** required on the capture
side: `jts_ring_capture_transfer` loops `while (delivered < size)` destaging one slot per
call (`pcm_jts_ring.c:757-809`, `:652`) `[M]`. *(N6)* `61-jts-renderer-lanes.conf:8-14`
declares itself **INERT UNTIL ARMED** and the fleet default is unarmed `[M]`, so 256×16 is
**shipped and legal, not exercised** — v1's word "proven" is withdrawn, and if the
fallback is taken it must establish itself on metal like any other geometry.

**The constraint this geometry accepts, stated rather than discovered** *(ND-3)*.
`n_slots = 16` is `JTS_RING_MAX_SLOTS` **exactly** (`jts_ring_shm.h:90`, range check
inclusive at `pcm_jts_ring.c:1094-1101`) `[M]`, so the design takes the ceiling and leaves
**zero depth headroom**. Depth and sync-coarseness were independent inputs on the way in;
after this choice they are not independently tunable *upward* — the only remaining depth
axis is `period_frames`, which is also the coarseness knob (input 2), so buying more depth
would cost sync granularity. **If S3's soak ever demands more depth than 42.67 ms**, the
change is not a conf.d edit: `JTS_RING_MAX_SLOTS` is held in lockstep across the C header,
`rust/jasper-ring/src/layout.rs:55` and `rust/jasper-outputd/src/config.rs:65`, pinned by
`tests/test_ring_slot_ceiling_pin.py`, and the header's layout is `_Static_assert`ed
(`jts_ring_shm.h:223-226`) `[M]` — so it is a three-language constant bump with its pins,
on the shared ring platform every other ring depends on. Named here so that cost is visible
at design time rather than met mid-soak.

**What S2/S3 must measure to validate the choice** (S7's ask): S2 records the delay series
**as snapclient samples it** — once per write iteration — *and* the occupancy distribution
over the run, not only a free-running ≥100 Hz sample of `snd_pcm_delay`. Occupancy
stability is what decides whether a 2.667 ms quantum is invisible or is the signal; S3's
zero-hard-sync bar is the gate, and S2 is what makes an S3 result interpretable rather
than pass/fail.

### 3.3 Wire format — S16_LE *(settled in review; unchanged)*

The hop the grouping ring replaces carries S16_LE (`reconcile.py:225`), because snapclient
decodes to the snapserver-pinned `sampleformat=48000:16:2` (`:470`) `[M]`. S32_LE would
either fail negotiation — the shipped ring PCMs are raw `type jts_ring`, not
`plug`-wrapped, and every hw_params dimension except access is single-valued
(`pcm_jts_ring.c:943-1007`) `[M]` — or force a `plug` wrapper, which the current design
refuses. `format S16_LE` and `channels 2` are declared **explicitly**, because this
block's wire is bound to the snapcast stream, not to the coupling's wire. Pinned by T-2.

### 3.4 Ring-file lifecycle and ownership *(settled in review; one correction)*

**Path: `/dev/shm/jts-ring/grouping.ring`. One name for both roles** — a box is a leader
or a follower, never both, and the role-dependent naming (`content.ring` vs
`active-content.ring`) belongs to the coupling's content hop `[M]`.

**Creation: none needed.** `ring_mapping_open` is shared by the writer and reader entry
points and does `O_CREAT | O_EXCL` at mode `0660` after ensuring the parent directory
(`jts_ring_shm.c:564-584`) `[M]`; whichever end opens first creates it. The only fatal open
is valid-magic-but-wrong-geometry (`-EINVAL`), which is loud.

**Permissions.** `/dev/shm/jts-ring` is `3775 root jts-ring` with setgid `[M]`. Both ends
run as **root** — neither `jasper-snapclient.service` nor `jasper-camilla.service` carries
`User=` `[M]` — so the AGENTS.md PR-#214 class does not bite. `UMask=0007` is added to
`jasper-snapclient.service` because the platform states the rule
(`deploy/tmpfiles/jts-ring.conf`) `[M]`. *(N5)* **The design does not claim the rule
currently holds:** `jasper-camilla.service` writes Ring B and also carries no `UMask=`,
so the tmpfiles sentence is already inexact today. Adding the line to the unit this design
touches is right; fixing camilla's is out of scope and is recorded in §12 as EG-7.

**Registration in `ring_assets.py`: none.** Path and lock-path constants live with the
rest of the grouping transport's identity (§4); the lock path is derived by calling the
existing `ring_writer_lock_path(...)` (`ring_assets.py:86-94`) `[M]` — one suffix rule,
one owner, already cross-language-pinned.

**Teardown: deliberately none.** `install_jts_ring_platform` `rm -f`s the three coupling
ring files on every deploy (`deploy/lib/install/ring-platform.sh:373-375`) `[M]`. **Do not
add `grouping.ring`.** The deploy bounces the coupling's audio graph, so those three have
no live writer at that moment; it does not bounce `jasper-snapclient.service`, so
unlinking `grouping.ring` under a live bonded endpoint would leave snapclient writing an
unlinked inode while camilla re-attaches to a fresh file — a silent split-brain, and the
exact pathname-vs-inode residual the C source already warns about (`jts_ring_shm.c:816`)
`[M]`. The residual of *not* deleting it: after a conf.d geometry change, a box that
already created the ring since boot meets `-EINVAL` at open until a reboot clears the
tmpfs — fail-loud and visible. Documented beside the existing `rm -f` lines.

### 3.5 Writer exclusivity, restart behaviour, and the clamp's deletion

**The flock needs no new machinery.** The C writer takes `flock(LOCK_EX|LOCK_NB)` on
`<ring>.writer.lock` for the life of the mapping; a second writer gets `-EBUSY` with
`event=jts_ring.writer.busy … reason=writer_lock_held` (`jts_ring_shm.c:751-863`) `[M]`.

**The unit adopts the ring-writer contract in full** *(C-6, S2)*. The contract is two
parts, and v1 quoted the second without acting on it:

| Key | Now | Phase 1 | Why |
|---|---|---|---|
| `RestartSec` | `2s` | **`3s`** | `jts_ring_shm.h:115-118`: a ring writer's `RestartSec` must exceed the 2 s heartbeat or a fast respawn races its own frozen heartbeat into `-EBUSY` `[M]`. |
| `StartLimitBurst` | `4` | **`6`** | `tests/test_renderer_ring_lanes.py::test_every_ring_writing_renderer_tolerates_a_burst_of_refusals` asserts `> 5` for every ring writer, because *"RestartSec alone is not enough: systemd's default limit … is what turns a transient refusal loop into a parked unit"* `[M]`. snapclient at 4 is below even systemd's default. |
| `UMask` | absent | **`0007`** | §3.4. |

Raising the burst **preserves** the unit's stated anti-storm intent (bounded, no reboot,
`StartLimitAction=none`) and makes a follower whose leader is powered off retry six times
in 300 s instead of four before parking — more availability, not less. No exemption is
claimed.

**The chunk clamp and its constant are DELETED** *(C-2, B2)*.
`FOLLOWER_LOOPBACK_MIN_CHUNKSIZE` has exactly one use site (`camilla_yaml.py:3660-3661`),
reachable only through `driver_domain=True`, whose only two production callers are
`follower_config.py:215` and `active_leader_config.py:264` — and
`baseline_profile.py:2381` says so in its own comment (*"only the multiroom reconciler
passes `driver_domain=True`"*) `[M]`. After Phase 1 both callers name the ring, so the
clamp's condition is unreachable.

> **v1's "rename and re-justify" is WITHDRAWN.** Keeping a floor whose only justification
> is snd-aloop's EPIPE behaviour, on a transport that raises no EPIPE, and sizing a new
> ring to it, propagates an artifact that §3.5 itself identified as an artifact.

*Alternative considered and rejected:* make the clamp conditional on an snd-aloop capture.
Rejected because the condition is never true in production after this phase — a guard on a
dead path is dead code, and the real guard is now a contract, not a runtime clamp: T-2
pins `GROUPING_RING_PERIOD_FRAMES == RING_CAMILLA_CHUNKSIZE == the conf.d block's
period_frames`, which is checkable at merge rather than silently corrected at emit.

---

## 4. Design Q2 — naming and the single source of truth *(settled in review; unchanged)*

**New module: `jasper/multiroom/grouping_ring.py`.** Import-cheap, owning the transport's
identity:

```
GROUPING_RING_PCM            = "jts_ring_grouping"
GROUPING_RING_FILE           = "/dev/shm/jts-ring/grouping.ring"
GROUPING_RING_CONF_D         = "/etc/alsa/conf.d/62-jts-ring-grouping.conf"
GROUPING_RING_FORMAT         = "S16_LE"
GROUPING_RING_CHANNELS       = 2
GROUPING_RING_PERIOD_FRAMES  = 128
GROUPING_RING_SLOTS          = 16
```

1. **One name replaces three.** A ring PCM is one device opened in two directions — the
   ioplug branches its hw_params on `io->stream` (`pcm_jts_ring.c:943-1007`) `[M]` — so
   snapclient's `--soundcard` and CamillaDSP's `capture.device` are the same string, and
   the census's S2 asymmetry disappears with the asymmetric member.
2. **The env indirection is deleted.** `JASPER_GROUPING_LOOPBACK_PLAYBACK` / `_CAPTURE`
   are read at import (`reconcile.py:214-221`), classified internal-only
   (`tests/test_env_vars_codified.py:154-164`), and written nowhere `[M/N]`. Keeping them
   would let an operator point the transport at a device whose geometry contradicts the
   conf.d the emitter and T-2 agree on. Retires P8B risk 5.2. The household-facing knob on
   this path (`JASPER_GROUPING_CLIENT_LATENCY_MS`) is untouched.
3. **Not in `reconcile.py`**, which is 2656 lines and whose three constants are imported by
   four production modules and six test modules `[M]` — a doctor check currently imports
   the whole reconciler to read a device name.
4. **Single writer of the follower's capture truth.** One source; two `capture_device=`
   consumers; one `--soundcard` consumer. "The playback side and the capture side agree"
   stops being a claim anyone has to check.

---

## 5. Design Q3 — retiring the interlock, and the leader's second CamillaDSP

### 5.1 What the gate is actually about *(amended — v1's fact 3 was true for one role)*

D2's detail states the mechanism: *"arming it on a bonded box would strand the leader's
local output (outputd reads Ring B while camilla#1 still bakes the aloop/loopback grouped
program)"* (`jasper/audio_runtime_plan.py:283-290`) `[M]`. Three measured facts:

1. A roleful box's outputd playback endpoint is `RING_ACTIVE_PLAYBACK_DEVICE`
   **unconditionally** (`jasper/output_topology.py:1895-1926`) `[M]`;
   `JASPER_OUTPUTD_CONTENT_BRIDGE` is an independent key `[M]`.
2. The dumb-member branch of `outputd_grouping_env` pins
   `JASPER_OUTPUTD_CONTENT_BRIDGE = "direct"` as *"the lane's hard requirement"*
   (`jasper/multiroom/reconcile.py:706`); the active-endpoint branch clears the lane and
   pins nothing (`:683-690`) `[M]` *(N3: v1 cited `:685-696`)*.
3. **For the active FOLLOWER**, `shm_ring` therefore changes only whether fan-in writes
   Ring A or aloop pair 7 — and camilla#1's capture is overridden to the grouping lane
   either way (`baseline_profile.py:1842-1847`), so fan-in's egress has no reader under
   either coupling. `[I]`

> **v1 generalised fact 3 to "an active endpoint" and evidenced only the follower. That is
> WITHDRAWN.** For the active-speaker **leader**, camilla#1 is not the driver-domain
> instance at all. It is emitted by `emit_active_speaker_program_bake_config`
> (`active_leader_config.py:325-330`) **with no `capture_device` argument**, and the
> emitter's signature binds `capture_device: str = DEFAULT_CAPTURE_DEVICE`
> (`camilla_yaml.py:3796-3803`) where `DEFAULT_CAPTURE_DEVICE = "plug:jasper_capture"`
> (`camilla_config_contract.py:26`) `[M]` — the snd-aloop fan-in tap. Under `shm_ring`
> fan-in writes Ring A and stops feeding that tap. `active_emit_devices`' own docstring
> names the outcome: *"a graph whose sink is the ring while its source is still
> `plug:jasper_capture` captures a device nobody writes — **digital silence with every
> daemon healthy** … That trap is QUIET"* (`camilla_yaml.py:415-424`) `[M]`. On a leader
> the sink is a `File` pipe rather than a ring, but the source half and the quietness are
> identical — and camilla#1 is the producer of the **whole bond's** audio.

**Verdict, restated: the gate has two subjects.** One is the dumb-member `dac_content`
lane. The other is the leader's coupling-blind program bake. **Phase 1 retires the first
by narrowing, and removes the second by making the bake coupling-aware** — the direction
the conductor set, and the one that keeps the mission intact (the hardware pass needs a
ring-armed bonded leader; jts.local is `shm_ring` today).

### 5.2 The change

**(a) The bake follows the live coupling — using the resolver that already exists.**

There is no need for a new map. `jasper/fanin_coupling.py:885-927`,
`coupling_capture_kwargs_from_env()`, is *"the one call shape a config emitter uses to
thread the SHARED fan-in→Camilla coupling into a live re-emit"*, returns `{}` for
`loopback` (byte-identical) and the ring kwargs for `shm_ring`, reads the token
**file-fresh** `[M]`. Its docstring names this exact failure mode: *"Without this an armed
box's `/sound/` or `/correction/` save would emit a *loopback* capture/playback config and
silently revert CamillaDSP off the rings (a silent audio outage)"* `[M]`.

**It has five existing production call sites** — method: `grep -rn
"coupling_capture_kwargs_from_env(" jasper/ deploy/`, minus its own definition module and
minus bare `from … import` lines — namely `audio_runtime_plan.py:2351`
(`apply_capture_precedence`), `correction/session.py:2105`, `web/correction_setup.py:3129`
and `:6827`, and `web/sound_setup.py:1745` `[M]`. **The leader's program bake becomes the
sixth call site, and the fourth class of caller.** *(v2 said "fourth caller", which
conflated the two counts.)*

One subtlety, and the code makes it loud rather than silent: the resolver returns the
**full end-to-end** kwargs including `playback_device` / `playback_format`, and
`emit_sound_config` **raises** if a `playback_format` is passed alongside
`playback_pipe_path` (`jasper/sound/camilla_yaml.py:268-277`) `[M]`. So the bake takes the
**capture half only** — `capture_device` and `capture_format`, the two kwargs its
signature already accepts — and its `File`/SNAPFIFO sink is untouched. That filter lives
at the one call site with a comment naming why; it is not a second resolver, and there is
no second caller to share it with. **Promotion trigger stated:** if a second File-sink
emitter ever needs it, promote the filter into `fanin_coupling.py` beside its parent
rather than copying it.

*Examined and declined:* also forwarding `chunksize` / `target_level` from the ring's
`RING_CAMILLA_*` constants so camilla#1's cadence matches the solo ring graph. Declined
because the observed break is the capture *device*, and a chunk larger than Ring A's
128-frame slot is legal by the mechanism the review established in C4 (the capture
transfer loops one slot per destage, `pcm_jts_ring.c:757-809`) `[M]`.

**And the residual is smaller than v2 stated, because the bake's chunk is DAC-derived, not
the global default.** The bake passes `chunksize=None`, so it resolves through
`resolve_camilla_chunksize()`, whose unset-env path returns **the active DAC profile's
codified `LatencyFloor.camilla_chunksize`** — `fallback = default if profile_floor is None
else profile_floor` (`jasper/camilla_config_contract.py:212`, `:229-246`) `[M]`; the global
`DEFAULT_CHUNKSIZE` (1024) applies only to a DAC that declares no floor. **On jts.local —
the box under test — the Apple-dongle profile declares `camilla_chunksize=256`**
(`jasper/audio_hardware/dac.py:385-390`) `[M]`, so camilla#1 would read Ring A **two slots
per chunk**, not eight. 1024 is the fleet worst case, not the value here. *(v2's
"1024-frame chunk" overstated the residual; §14 item 6 records where this parts company
with the reviewer's own replacement number.)* Recorded rather than silently skipped, and
camilla#1's health signals are the hardware plan's S6.

**Owner and class:** this lands in the **same PR as the gate narrowing** (PR-5,
safety-class, 3-lens panel). It is not separable: the gate is what makes the
coupling-blind bake reachable, so shipping one without the other is the hazard.

**(b) The gate narrows to its remaining subject.** `coupling_supported_for_route(coupling,
route_mode, *, dac_content_lane_armed: bool)` blocks `shm_ring` iff `route_mode` is
grouping-enabled **and** `dac_content_lane_armed`. The reason token becomes
`fanin_shm_ring_unsupported_with_dac_content_lane`, and the detail names the lane and
stops saying "until ring v2 (P8)".

**`dac_content_lane_armed` has one derivation**, exported from `jasper/multiroom/`: a pure
predicate over `(GroupingConfig, active_endpoint)` that is true exactly when
`outputd_grouping_env` would emit a non-empty `JASPER_OUTPUTD_DAC_CONTENT_FIFO`. It is
derived from the *same function that writes the lane*, so gate and writer cannot disagree.
The review's C2 confirms the shape is forced: `route_mode_from_grouping_config` classifies
purely on `cfg.role`, so `active_follower` is returned for a dumb member and an active
follower alike — the route mode alone genuinely cannot discriminate `[M]`.

**`invalid_grouping` flips from blocked to allowed, and that is correct** *(C-8, S4)*.
`invalid_grouping` = `cfg.enabled and cfg.error is not None`; that falls past both
branches of `outputd_grouping_env` into the off-path return (`reconcile.py:727-730`), so
`dac_content_lane_armed` is False. It is safe because the box is **definitively solo**:
`active = cfg.enabled and cfg.error is None` (`reconcile.py:1814`) `[M]`, so no bond forms,
no `active_endpoint` exists, and no bake runs. It is *determinate*, unlike `unknown` (a
transient read failure), which is why relaxing it is not a relaxation on an indeterminate
state. Today's behaviour — forcing a healthy solo box onto loopback because its grouping
config has a typo — is the outlier.

**Import direction is load-bearing, not incidental** *(C-16, A3-iii)*.
`audio_runtime_plan` types `route_mode_from_grouping_config(cfg: Any)` and keeps its one
`jasper.multiroom.config` import lazy inside a function (`:1357`), while
`active_leader_config.py:73` imports `audio_runtime_plan` at module level `[M]`. **Passing
`dac_content_lane_armed` as a plain `bool` is what keeps that direction legal**; a
module-level import of the predicate into `audio_runtime_plan` would invert it and create
a cycle. Stated here so a later refactor does not undo it.

**(c) D1 stops hand-rolling.** `jasper/multiroom/reconcile.py:1891-1920` keeps its shape
and its `fall_back_to_solo()` fail-safe but asks the predicate instead of testing
`read_persisted_coupling() == COUPLING_SHM_RING`, and reports D2's `support.reason`. This
is the drift the census could not see: D1 and D2 encode *different* rules today and
coincide only because D2's is stricter.

**(d) D5's reorder is a behaviour change, and here is what changes** *(C-9, S5)*. The
coupling check moves below the strict topology load
(`active_leader_config.py:211-219` → after `:225-236`). In the cell where a box is both
ring-armed and has an unreadable `topology.json` — reachable, and the code says so
(*"The 2026-05-23 filesystem-loss class corrupts topology.json too"*) `[M]` — the
operator-visible reason changes from `coupling_support.reason` to `topology_unreadable`.
Both outcomes are fail-closed, so there is no safety change. **It is acceptable, and
better, for two reasons:** the new predicate genuinely *needs* the topology (it derives
`active_endpoint` from it), so checking coupling first would mean guessing; and
`topology_unreadable` is the more specific and more actionable blocker, so the operator
fixes the real thing first instead of chasing the coupling and then meeting the topology
error on the retry. The new precedence is pinned by a test.

### 5.3 Scope discipline — what Phase 1 does not touch, and what Phase 2 inherits

**Not touched:** the strike ladder and its persistence
(`/var/lib/jasper/ring-confirm-strikes.json`, `RING_CONFIRM_STRIKE_LIMIT = 2`, the 24 h
window, clear-on-success, `ring_confirm_strike_write_failed`) `[M]`;
`_recover_to_loopback`; `_fail_ring_arm`; the confirm ladder; every other landing state in
census §4.

**The #2672 property survives untouched.** The confirm rung's refusal —
`if payload.get("transport") == "statefile": return False, …` at
`jasper/fanin/coupling_reconcile.py:541-542`, ahead of all three acceptance branches
(`:543-544`, `:549-550`, `:552-583`) `[M]` *(N3: v1 cited `:519-535`, which is the comment
prose that explains it)* — lives inside `_reconcile_camilla`, which this design does not
open. Its pins are `tests/test_fanin_coupling_reconcile.py:508` (parametrized
flat/roleful × confirm/arm), `:546`, `:606`, `:630` (websocket positive control), and
`tests/test_ring_gates_recovery.py:1439`, `:1468`, `:1488` `[M]` — all seven independently
re-verified by the review. The gate change is upstream of the confirm ladder. **PR-5 runs
those seven tests explicitly and says so in its body.**

**What Phase 1 hands Phase 2:**

| Census landing state | After Phase 1 |
|---|---|
| **L3** (route-blocked: any grouping-enabled box) | **Narrowed.** A bonded *active endpoint* no longer lands loopback; a bonded *dumb member* still does, and its subject is now named. Phase 2 retires L3 when it retargets the `dac_content` lane. |
| L1, L2, L4–L23 | Unchanged. |
| `snd_aloop_rate_adjust_oscillation_reason` (census §5(d)) | **Simplified.** Its `"Loopback"` token stops matching any live grouping emitter; it becomes a pure AXIS-1 predicate and dies with AXIS-1. |
| `check_grouping_aloop_remnant` | Retargeted and re-homed (§6.1); no grouping input remains. |
| Pair 6 | Exactly one consumer (the AXIS-1 passive content lane). |
| `pcm_substreams` | **8, unchanged** (§9). |
| `_effective_camilla_chunksize_setting` | Still single-valued on a two-CamillaDSP leader — approximate before and after Phase 1. §12 EG-8. |

---

## 6. Design Q4 — doctor truth

### 6.1 The remnant guard: re-source pair 6 first *(cost corrected)*

**(a) Re-source pair 6 (behaviour-identical).** Add `_OUTPUTD_CONTENT_ALOOP_PCM =
"hw:Loopback,0,6"` beside `_FANIN_EXPECTED_OUTPUT_PCM = "hw:Loopback,0,7"`
(`jasper/cli/doctor/audio_runtime.py:157`) `[M]` and make `_derive_registered_pairs` read
it. The registered set is identical ({0,1,2,3,4,6,7}); only the *source* of pair 6 moves.
Pinned against `asoundrc.jasper`'s `pcm.outputd_content_playback` slave.

**(b) Drop the grouping contributor, rename, re-home.** With (a) landed, deleting
`_grouping_pair_index()` leaves the set unchanged; the check then measures fan-in and
outputd lanes and nothing about grouping, so it moves to
`jasper/cli/doctor/audio_runtime.py` and is renamed `check_aloop_registered_substreams`.
`order=75.96` and `exclusive_group="audio-probe"` are preserved verbatim. Its `#2508` text
is replaced.

> **v1's cost estimate — *"three lines of relocation and one `__init__` export"* — is
> WITHDRAWN.** The review's C1 is right and I reproduce it: `grouping_pair` is the
> function's **second return value**, read at `:1189`, `:1248`, `:1256`, `:1259`; the
> *"deliberately NO grouping-pair-missing branch"* comment at `:1190-1195` becomes false;
> and `tests/test_doctor_grouping_remnant.py::test_grouping_pair_is_always_registered`
> asserts the opposite of the new state `[M]`. It is a **signature change with four call
> sites, a comment, and a test that inverts.** Costed accordingly in the wave map.

### 6.2 inv-5 becomes a uniform invariant

`check_grouping_rate_adjust` is **leader-only** (`jasper/cli/doctor/grouping.py:262`) and
its docstring asserts the opposite for followers `[M]` — already false on a ring-armed box
and false everywhere after Phase 1, because rate-adjust on an ioplug capture is inert by
construction (ioplug reports `card = -1`; CamillaDSP builds no HCtl; `SetSpeed` falls
through with no final `else` — memo §1.1, four `[M]` citations).

**Post-migration invariant:** *snapclient is the sole rate-tracker on every bonded box; no
CamillaDSP in a bonded chain runs `enable_rate_adjust: true`.*

- **Scope widens** from `is_active_leader(cfg)` to `is_active_member(cfg)`.
- **Severity stays `warn`.** A stray `true` on a ring capture is inert — an observability
  lie, not a hazard — and the check's job is to catch a bond apply that did not land.
  Escalating to `fail` would red a fleet over a cosmetic key. Never-nanny.
- **Fix the fail-soft asymmetry while in the file:** `_devices_rate_adjust_from_text`
  returns `None` for absent *or* unparseable and the check tests `if rate_adjust is True`,
  so a config with no key reports `ok "rate_adjust off"` — a claim the file does not
  support `[M]`. Becomes `warn "could not confirm"`.
- **Docstring trued**; the follower exception is deleted with the ioplug-inertness reason.
- **The widening's test home is `tests/test_multiroom_rate_adjust.py`**, whose docstring
  opens *"inv-5 (docs/HANDOFF-multiroom.md §2)"* and whose `:164-172` spy is keyed to
  `is_active_leader` by name `[M]` — see §8.2.

### 6.3 The `check_loopback` contradiction — untouched, and why *(unchanged)*

`check_loopback` `fail`s when `CARD=Loopback` is absent; the remnant guard returns `ok`
for the same absence `[M]`. After Phase 1 the grouping path needs no snd-aloop, so it has
no stake in the question, and both checks sit entirely inside AXIS-1/2/3. Their
reconciliation belongs to whichever phase makes the module optional — per census §6.3,
**neither Phase 1 nor Phase 2**. The only edit is to the remnant guard's header comment,
which cross-references the conflict from the grouping side and no longer should.

### 6.4 The deferred round-trip-starvation signal *(unchanged)*

`jasper/control/grouping_supervisor.py:313` defers a starvation signal *"until observed"*
`[M]`. Phase 1 does not build the surface — nothing has been observed, silence is legal,
and a supervisor path on a hypothesis is astronaut engineering. Phase 1 **retargets that
comment** to name the instrument the ring now provides. If S4 or the soak observes
starvation it is fixed in-session — a finding, not a ticket.

---

## 7. Design Q5 — the follower emit, and the guard test

### 7.1 What changes in the emit *(unchanged; still two kwargs)*

`precheck_active_follower` passes `capture_device` / `capture_format`
(`follower_config.py:213-214`); `precheck_active_leader` passes the same two for camilla#2
(`active_leader_config.py:262-263`); `build_baseline_profile_candidate` overrides only
those two against `active_emit_devices`' result (`baseline_profile.py:1842-1847`) and
forwards `chunksize` (`:2397`) and `target_level` (`:2398`) untouched `[M]` *(N3)*.
The emitted capture block becomes
`{type: Alsa, device: "jts_ring_grouping", format: S16_LE}` and — with the clamp gone —
`chunksize: 128`.

**The L0 emit gate stays in path.** `_assert_tweeter_outputs_protected`
(`camilla_yaml.py:736`) is called at `:3767`, inside
`emit_active_speaker_driver_domain_config`, before the YAML is written `[M]`; both
prechecks convert its error so the reconciler fails closed to solo
(`follower_config.py:219-225`, `active_leader_config.py:269-277`) `[M]`. The change is two
kwargs into the same function, so the gate cannot be routed around.

### 7.2 The clock-seam guard, retargeted, with a mutation-grade kill proof

`tests/test_multiroom_follower_config.py:928-979` `[M]`. Its fixture hardcodes
`playback_device="hw:CARD=DAC8x,DEV=0"`, so it takes the non-ring branch and never sees
production's shape.

- **Parametrize the fixture over both playback branches** — the DAC device *and*
  `RING_ACTIVE_PLAYBACK_DEVICE`.
- `test_follower_clock_seam_chunksize_at_least_1024` → **renamed**, and asserts the
  emitted chunksize **equals `GROUPING_RING_PERIOD_FRAMES`** on the ring branch (128 == 128)
  and `DEFAULT_CHUNKSIZE` on the DAC branch. *(v1 pinned the same equality but at 1024;
  C-2.)*
- `test_follower_clock_seam_no_resampler_rate_adjust_on` → renamed and inverted for the
  ring branch: `enable_rate_adjust is False`, no resampler on either branch.
- `test_follower_clock_seam_raw_hw_loopback_capture` → rewritten as
  `test_follower_capture_is_the_raw_grouping_ring`: capture device is `GROUPING_RING_PCM`,
  not `plug`-wrapped, format is `GROUPING_RING_FORMAT`. The `startswith("hw:")` assertion
  is deleted — it was only ever a proxy for "no plug".
- Header comment rewritten: snapclient is the sole tracker; CamillaDSP's rate-adjust would
  be inert here; the chunk and the ring slot are one number.

**Mutation kills** — every new pin proved to fail on a real mutation, with a no-op control
(and, per the recorded traps, `PYTHONDONTWRITEBYTECODE=1` plus a `__pycache__` purge for
same-length literals, and proof the edit landed *and* the test ran):

| Pin | Mutation that must fail it |
|---|---|
| chunk == ring period (ring branch) | `GROUPING_RING_PERIOD_FRAMES` → 256 |
| chunk == ring period (ring branch) | `RING_CAMILLA_CHUNKSIZE` → 256 |
| `enable_rate_adjust is False` (ring branch) | `RING_CAMILLA_ENABLE_RATE_ADJUST` → `True` |
| capture device | `GROUPING_RING_PCM` → `jts_ring_capture` |
| capture format | `GROUPING_RING_FORMAT` → `S32_LE` |
| conf.d ↔ constants (T-2) | edit the conf.d `period_frames`, then `format`, independently |
| **T-8a — the BAKE's capture follows the coupling.** Emitter under test: `emit_active_speaker_program_bake_config`, called from `active_leader_config.py:325-330`. | Revert the bake **call site** to the emitter's own default → under `shm_ring` the emitted camilla#1 YAML must contain `plug:jasper_capture`, and `test_leader_bake_capture_follows_coupling` must go **red**. |
| **T-8b — the BAKE takes the capture half only.** Same emitter. | Splat the resolver's full dict into the **bake** call → **`TypeError`** at the call site, because `emit_active_speaker_program_bake_config`'s keyword-only signature (`camilla_yaml.py:3796-3809`) declares **no** `playback_device`, `playback_format`, `queuelimit` or `enable_rate_adjust` parameter at all `[M]`. That `TypeError` is the bake's own loudness and is the kill this row pins. |

> **Do not satisfy T-8b through `emit_sound_config`.** Its `ValueError` on a
> `playback_format` beside a `playback_pipe_path` (`jasper/sound/camilla_yaml.py:270-277`)
> `[M]` is **sibling evidence** that playback kwargs are wrong for a pipe sink — it is not
> the guard on this path. `emit_sound_config` is the *passive*-leader pipe emitter, which
> the narrowed gate still blocks; routing the mutation through it records a green kill that
> proves nothing about the bake. Named explicitly because this repo has a recorded trap of
> mutation harnesses failing silently in both directions.
| no-op control | a comment-only edit in each mutated file leaves the suite green |

---

## 8. Design Q7 + test strategy

### 8.1 snapclient hardening: probe and record, do not pin *(settled in review; unchanged)*

`provision.py:208-219` installs bare package names with no version; "0.31.0" appears only
in comments (`:53`, `reconcile.py:515`) as a one-time human check; the only runtime
verification is `shutil.which` presence `[M]`. **Decision: probe and record the version;
do not pin the apt package.** An apt pin turns a Trixie point release into a failed
install → grouping provisioning fails → the household loses grouping, and it blocks
security updates — a hard stop for something that is not a safety hazard.
`ensure_snapcast_installed` runs `snapclient --version` after a successful install
(bounded, fail-soft, never raises), records it in the provision status it already writes,
and one doctor line warns on drift from the validated version. *Not chosen:* an ALSA
open-probe of `jts_ring_grouping`, because it creates the ring file and contends for the
writer lock; the repo already prefers a record comparison for exactly this reason
(`ring_wire_caps_ready`) `[M]`.

### 8.2 Test strategy per PR

Hardware-free, in `scripts/test-merge`'s lane.

- **T-1 (PR-1)** — conf.d shape: `62-jts-ring-grouping.conf` defines exactly
  `pcm.jts_ring_grouping` with only keys the ioplug accepts, **allowing `type`, `comment`
  and `hint`, which it explicitly skips (`pcm_jts_ring.c:1035-1036`)** *(N1)*; `n_slots`
  within 2..16.
- **T-2 (PR-1)** — the SSOT pin: conf.d `period_frames` == `GROUPING_RING_PERIOD_FRAMES`
  == `RING_CAMILLA_CHUNKSIZE`; conf.d `format` == `GROUPING_RING_FORMAT`; conf.d
  `channels` == `GROUPING_RING_CHANNELS`; and `GROUPING_RING_FORMAT`'s bit depth == the
  `sampleformat=48000:16:2` in `snapserver_argv`. Mutation-killed per §7.2.
- **T-3 (PR-1)** — install: the conf.d is copied and covered by the asset manifest/doctor
  presence check; and an explicit assertion that `grouping.ring` is **absent** from the
  deploy-time `rm -f` list, with the reason in the docstring. *(N2)* **PR-1 is not
  risk-free:** the file lands in `/etc/alsa/conf.d/`, which alsa-lib parses on **every**
  PCM open fleet-wide, so T-1 is an availability guard, not tidiness. A malformed block
  would break every renderer on every box.
- **T-4 (PR-2)** — the registered set is byte-identical before and after re-sourcing pair
  6; `_OUTPUTD_CONTENT_ALOOP_PCM` matches `asoundrc.jasper`'s
  `pcm.outputd_content_playback` slave; and
  `test_grouping_pair_is_always_registered` is retargeted to the new second return value.
- **T-5 (PR-5)** — the narrowed predicate, **derived, not parametrized** *(S4)*: build
  `GroupingConfig` fixtures (solo / valid-leader / valid-follower / invalid) × `active_endpoint`
  ∈ {True, False}, compute `dac_content_lane_armed` from the same function that writes the
  lane, and assert the verdict. Reachable cells only — the free-boolean matrix v1 specified
  would have pinned `invalid_grouping × armed=True`, which the real derivation cannot
  produce. Plus: D1 and D2 return the same verdict for every cell; the new D5 precedence;
  and the seven #2672 pins run and pass.
- **T-6 (PR-3)** — the retargeted clock-seam guard with its mutation table; the snapclient
  argv test asserting `--soundcard jts_ring_grouping`; `test_env_vars_codified` updated.
  **The unit pin reads the C constant, not a hardcoded window** *(S3)*: `RestartSec` is
  compared against `_ring_liveness_window_sec()`'s parse of
  `JTS_RING_WRITER_LIVENESS_TIMEOUT_NS`, the way the existing renderer pin does — a
  bespoke "2 s" would become a second owner and drift.
- **T-7 (PR-3)** — **the writer-cadence enumeration becomes executable** *(S3)*.
  `c/jts-ring-ioplug/jts_ring_shm.h:118-119` declares *"Every ring writer's cadence, so the
  set is checkable rather than assumed"* and enumerates **five** writers at `:120-151`
  (librespot, bluealsa-aplay's JTS drop-in, shairport-sync, jasper-camilla, and the
  ephemeral correction-lane `aplay`) `[M]`; `jasper-snapclient.service` becomes the sixth.
  Today's guard
  (`test_the_ring_liveness_window_is_enumerated_for_every_writer`) iterates
  `RENDERER_LANES` and therefore structurally **cannot** see a non-renderer writer `[M]`.
  Retarget it to iterate the header's own enumeration: every enumerated unit must exist,
  its `RestartSec` must match the enumerated value and exceed the liveness window, and its
  `StartLimitBurst` must satisfy `> 5`; a separate assertion keeps every `RENDERER_LANES`
  entry present in the enumeration. **One owner (the header), no duplication, and the ADD
  direction is caught** — this is the same enumerated-set blindness the design invokes for
  its own prose sweep, in the opposite direction.
- **T-8 (PR-5)** — the bake follows the coupling: under `shm_ring` the emitted camilla#1
  bake captures `jts_ring_capture` at the resolved wire format, its playback is still
  `type: File` at `SNAPFIFO`, and `enable_rate_adjust` is false; under `loopback` the
  emitted YAML is **byte-identical to today**. Mutation-killed per §7.2.
- **T-9 (PR-4)** — the provision version record is written and surfaced; the doctor drift
  warn fires on mismatch and is `ok` on match.
- **Sweeps (PR-6)** — `bash scripts/tense-grep.sh` on the branch diff, **plus** a subject
  sweep, because the deleted thing is a member of enumerated sets and the tense sweep is
  blind to that class. Sweep terms, **run as separate greps because they are separate
  tokens** *(S6)*: `pair 6`, `substream 6`, `Loopback,0,6`, `Loopback,1,6`,
  `GROUPING_LOOPBACK`, `round-trip loopback`, `round-trip snd-aloop`, `the round-trip`,
  `inv-2`, `inv-5`, `solo-stereo-only`, `until ring v2`, `ring writer`. Every enumeration a
  hit appears in is read, not just the hit line.

**Test modules that break, assigned** *(C-12, S9, PC-7)*:

| Module | Breaks on | Edit |
|---|---|---|
| `tests/test_multiroom_active_leader_config.py:160, :188` | PR-3 (constant deleted) | function-local import → `GROUPING_RING_PCM`; expected device updated. This is also the module that pins the leader's camilla#2 build — the instance B1 is about. |
| `tests/test_active_speaker_driver_domain.py:228, :233, :238, :245, :248` | PR-3 (clamp + constant deleted) | assertions retarget from `FOLLOWER_LOOPBACK_MIN_CHUNKSIZE` to `RING_CAMILLA_CHUNKSIZE` on the ring branch. **Not found by the review — see §14.** |
| `tests/test_multiroom_rate_adjust.py:145, :164-172` | PR-5 (`is_active_leader` → `is_active_member`) | the spy is keyed to the old name by design; retarget it, and put §6.2's widening coverage here (the module already exercises `is_active_member` at `:16`, `:47-59`). |
| `tests/test_renderer_ring_lanes.py` | PR-3 (T-7) | the enumeration guard is retargeted. |
| `tests/test_doctor_grouping_remnant.py` | PR-2 | `test_grouping_pair_is_always_registered` inverts. |
| `tests/test_env_vars_codified.py:154-164` | PR-3 | two override rows deleted. |
| `tests/test_multiroom_follower_config.py:928-979` | PR-3 | §7.2. |
| `tests/test_multiroom_reconcile.py` (argv + env bodies) | PR-3 | expected `--soundcard` updated. |

**Method note, adopted** *(S9)*: v1 derived its prose impact set by subject sweep and its
test impact set by inspection. v2 uses the mechanical version for both — `git grep -l` for
every deleted symbol and every renamed predicate — which is how the third breaking module
was found.

---

## 9. Design Q8 — the change set

> **Retitled from "the deletion set".** v1's table listed only deletions and read as
> exhaustive; it was neither. **This table is explicitly NON-EXHAUSTIVE for prose**, and
> PR-6's sweep (§8.2) is the owner of completeness. Line numbers below are load-bearing
> where given, but the *set* is not a bound.

**`pcm_substreams` stays `8`.** Phase 1 removes only the grouping consumer of pair 6; pair
6 keeps its AXIS-1 consumer and pairs 0-4 keep theirs `[M]`.

### Deletions and edits

| Site | What changes |
|---|---|
| `deploy/modprobe.d/snd-aloop.conf:25-34` | the grouping half of pair 6's allocation row + the "safe to share" rationale. The `options` line is untouched. |
| `jasper/multiroom/reconcile.py:185-225` | the three constants, the ASCII diagram, the "DELIBERATELY snd-aloop … needs the loopback's clock" argument |
| `jasper/multiroom/reconcile.py:186-196`, `:517-525` | the `--latency` "is nulled" claim (§12 EG-1) and the `snd_pcm_delay`-trap rationale |
| `jasper/multiroom/reconcile.py:1891-1920` | D1 re-pointed at the predicate (§5.2c) |
| `jasper/multiroom/follower_config.py:18-19` | module-docstring chain diagram |
| `jasper/multiroom/active_leader_config.py:24-27`, `:211-219`, `:325-330` | camilla#2's "[rate_adjust ON]" prose; D5's reorder; **the bake's capture kwargs (B1)** |
| **`jasper/multiroom/member_config.py`** *(added, S6)* | its module docstring is the ONE place stating the member-config policy and says the active follower *"captures the round-trip snd-aloop loopback"* — false after Phase 1. Its dumb-follower sentence stays true (AXIS-1). |
| `jasper/active_speaker/camilla_yaml.py:183-190`, `:3657-3661` | `FOLLOWER_LOOPBACK_MIN_CHUNKSIZE` + its clamp **deleted** (§3.5) |
| **`jasper/sound/graph_carrier.py:414`** *(added, S6)* | *"shm_ring is solo-stereo-only; the active baseline keeps its roleful ALSA capture/playback graph"* — the **behaviour is right** (active recomposition resolves devices through `active_emit_devices`, not the flat coupling kwargs) but the stated reason is stale. **PR-6's file set is widened to include this file**, which no other PR opens. |
| `deploy/systemd/jasper-snapclient.service:26-27`, `:60-66`, `:69` | `StartLimitBurst` 4→6, `RestartSec` 2s→3s, `UMask=0007`, and the pair-6 player comment |
| **`c/jts-ring-ioplug/jts_ring_shm.h:120-151`** *(added, S3/C-7)* | the five-writer cadence enumeration gains a sixth, `jasper-snapclient.service RestartSec=3` — and T-7 makes the enumeration executable so this cannot silently rot again |
| `deploy/alsa/asoundrc.jasper:242-247` | "the grouping round-trip shares substream 6 … bypassing these aliases" |
| `jasper/cli/doctor/grouping.py:236-283`, `:932`, `:946-951`, `:976`, `:1041-1108`, `:1189`, `:1248`, `:1256`, `:1259`, `:1190-1195` | inv-5 widening; the grouping contributor, `_grouping_pair_index`, the second return value and its four readers, the "deliberately NO grouping-pair-missing branch" comment |
| `jasper/control/grouping_supervisor.py:313` | comment retargeted to the ring's instruments (§6.4) |
| **`solo-stereo-only` — 6 sites, 4 files** *(corrected, S6)* | `audio_runtime_plan.py:270`, `:285`, `:824`; `coupling_reconcile.py:3399`; `multiroom/reconcile.py:1893`; `sound/graph_carrier.py:414`. Verified by `git grep -n "solo-stereo-only"` `[M]`. |
| **`until ring v2` — a DIFFERENT token, 5 sites** *(corrected, S6)* | `audio_runtime_plan.py:285`, `:824`; `coupling_reconcile.py:3399`, `:3419`; `multiroom/reconcile.py:1914`. Verified by `git grep -n "until ring v2"` `[M]`. v1 conflated the two greps, which is how `:270` and `graph_carrier.py:414` fell out. |
| **`inv-5` homes beyond the doctor** *(added, S6)* | `jasper/fanin_coupling.py:936`, `jasper/sound/camilla_yaml.py:386` |
| `docs/HANDOFF-multiroom.md` | **#2508 item 3.** inv-2's *"never snd-aloop"* restated as what it protects (snapclient's writer never lands on a device that lies about time-to-DAC); inv-5 loses its follower exception; the `:1772` chain diagram and `:1678` retained-invariants block trued; `Last verified:` bumped with scope. |
| `docs/HANDOFF-distributed-active.md` | **nineteen `round-trip` sites, not the six v1 named and not the thirteen the review named** — `:125, :147, :148, :177, :264, :286, :300, :324, :357, :706, :792, :903, :986, :1163, :1182, :1189, :1257, :1288` plus `:1183`, verified by `git grep -n "round-trip snd-aloop\|round-trip loopback\|the round-trip"` `[M]` (§14). Includes the `:147`/`:148` role table, `:177`, and `:357` (*"with `enable_rate_adjust` ON"* — false on both halves). Separately, v1's six sites (`:428, :430, :466-467, :697-698, :1372, :1405`) are all genuinely about pair 6 but never spell "round-trip" on the hit line, so a literal grep cannot see them — **which is why PR-6's sweep, not any line list, owns completeness here.** |
| **`docs/HANDOFF-dsp-graph-carrier.md:486`** *(added, S6)* | *"capture at the round-trip loopback, emits a driver-domain-only Layer-A"* |
| **`docs/HANDOFF-audio-graph-consolidation.md:1087, :1148`; `docs/HANDOFF-fan-in-daemon.md:160`; `docs/HANDOFF-speaker-output-reference.md:455, :1674, :1675`** *(added, S6)* | each carries a literal `hw:Loopback,{0,1},6` and must be **read against the migration** rather than assumed AXIS-1 |
| `docs/audio-paths.md`, `docs/doc-map.toml` | scanned per the touched-subsystem rule |

### Additions (which a §9-scoped implementer would otherwise miss) *(S6)*

| Site | What is added |
|---|---|
| `deploy/alsa/conf.d/62-jts-ring-grouping.conf` | new file (§3.1) |
| `jasper/multiroom/grouping_ring.py` | new module (§4) |
| `jasper/cli/doctor/audio_runtime.py:157` area | `_OUTPUTD_CONTENT_ALOOP_PCM`, and the relocated + renamed `check_aloop_registered_substreams` |
| `jasper/multiroom/` (predicate home) | `dac_content_lane_armed`'s pure derivation (§5.2b) |
| `deploy/lib/install/ring-platform.sh` | the new conf.d in the install + manifest path; a comment beside the `rm -f` block explaining the grouping ring's deliberate absence |
| `jasper/multiroom/provision.py` | the version probe + record (§8.1) |

**#2481's literal done-clause.** *"snd-aloop no longer loads on any box"* is **not reachable
by Phase 1 + Phase 2** — AXIS-2 and AXIS-3 survive with eleven STAYS consumers (census
§2.3, §6.3). **Owner-ruled: #2481 closes narrow** (§13). The close-out comment states the
gap in those words and claims only what Phase 1 delivers. New code comments cite `#2508`
and the in-tree `#2285`/P9-C attribution, not the GitHub-only numbers (census S1).

---

## 10. Wave map, and the hardware plan

### 10.1 Waves

Every PR gets an INDEPENDENT adversarial review to 0/0, delta re-reviews from the same
reviewer, and its disposition posted when the review returns. Safety-class = a 3-lens panel.

| PR | Content | Class | Deploy |
|---|---|---|---|
| **PR-0** | **EG-6, alone and outside the stack** *(C-14, A2)*: the `migrate_grouping` key-list bug. Its body carries the read-only fleet evidence (`grep -n '^JASPER_GROUPING' /etc/jasper/jasper.env` on both boxes) and states which direction the fix takes. **Not in the stack** because `migrate_grouping` runs inside `install.sh` on every deploy on every box, and adding `JASPER_GROUPING` to the migrated set would promote any stale `jasper.env` value fleet-wide — a behaviour change that must not ride the stack's single deploy. | single gate | its own |
| **PR-1** | The platform: `62-jts-ring-grouping.conf`, `jasper/multiroom/grouping_ring.py`, install + manifest + doctor asset presence. No consumer moves. T-1..T-3. | **safety-class** (a conf.d file alsa-lib parses on every PCM open fleet-wide — N2) | no |
| **PR-2** | Doctor re-sourcing: pair 6 from `_OUTPUTD_CONTENT_ALOOP_PCM`; the second-return-value signature change, its four call sites, the comment, and the inverting test. T-4. *(Costed per C1, not v1's three lines.)* | single gate | no |
| **PR-3** | The transport flip: `_assemble_args`, both prechecks' capture kwargs, delete `GROUPING_LOOPBACK_*` + the two env overrides, **delete the chunk clamp and its constant**, the three unit-file keys + prose, the header's writer enumeration, retargeted clock-seam guard + T-7's executable enumeration. Breaks four test modules (§8.2). T-6, T-7. | **safety-class** | no |
| **PR-4** | snapclient version probe + doctor drift warn. T-9. | single gate | no |
| **PR-5** | **The gate AND the bake, atomically**: narrow D2's predicate, re-point D1, reorder D5, make the leader's camilla#1 capture follow the coupling (B1), retire the remnant guard's grouping input + rename + re-home, inv-5 widened + asymmetry fixed, and the six `solo-stereo-only` + five `until ring v2` sites in the files it opens. T-5, T-8. **Not separable**: the gate is what makes the coupling-blind bake reachable. | **safety-class** | no |
| **PR-6** | Truth + sweeps: `--latency` prose (EG-1), `HANDOFF-multiroom.md` truing (#2508 item 3), `HANDOFF-distributed-active.md`'s nineteen sites (§14 item 4), `member_config.py`, `graph_carrier.py:414`, `HANDOFF-dsp-graph-carrier.md`, the three `hw:Loopback,*,6` docs, modprobe row, asoundrc note, EG-3/EG-4, tense-grep + the subject sweep. **Owner of deletion-set completeness.** | single gate | **yes** — the stack deploys once, here |
| **HW** | The hardware pass (§10.2). Closes #2581, #2508, #2481. | gated step | — |

Ordering, each with its reason: **PR-2 before PR-3** (PC-5 — otherwise the flip
un-registers pair 6 and reds every loopback-coupled box). **PR-3 before PR-5** (the gate
must not admit a bonded ring-armed box before its ingress is on the ring). **PR-6 last**
so the sweep runs over the finished diff. PR-0 and PR-4 are order-independent.

Merge hygiene: each PR rebases on `origin/main` immediately before push and again before
merge; PR-3's tree-scanning-adjacent enumeration guard and PR-2's doctor test are checked
against the **merge result** (`git merge-tree --write-tree origin/main HEAD`, scanning
`git ls-tree -r --name-only <oid>`), not the branch, per AGENTS.md item 12.

### 10.2 The hardware plan

**Owner-ruled context (settled):** jts.local (`pi@192.168.1.74`, LAN IP always) = LEADER;
jts4 (`ssh jts4`) = FOLLOWER; jts.local's durable baseline **will** be applied as step 0.2;
dummy loads stay. jts3 forbidden, jts5 unplugged. The L0 emit gate is in path at every
step (§7.1).

**Step 0 — prerequisites.**
1. **Heal jts4's dead reconcilers.** `jasper-grouping-reconcile.service` and
   `jasper-source-intent-reconcile.service` have been `failed` since the 14:16 boot (a
   boot-time `jasper-usbgadget.service` restart timeout cascaded through the usbsink leg).
   `systemctl start` both, confirm active, capture the journal. The recovery model is
   event-driven (next boot, deploy, `/sources/` toggle, manual start) and the state *is*
   visible (the doctor flags it independently); if the observed window is longer than a
   household would tolerate, that is a finding to fix in-session.
2. **Apply jts.local's durable baseline** (owner-ruled). Until `grouping_allowed` reads
   `true`, `/grouping/set` returns 409 for either role (PC-2). Confirm:
   `curl -s http://192.168.1.74:8780/state | jq .active_speaker_setup.grouping_allowed`.
3. **Deploy the sealed stack** to both boxes via `bash scripts/deploy-to-pi.sh` only,
   dedicated detached worktree per target, LAN IP for jts.local. `jasper-doctor` green on
   the ring/coupling checks on both; the known parked mic/chip-AEC/calibration warnings
   unchanged.
4. **Record jts4's active/passive classification** *(PC-6, S8)*, before the pass, with the
   command:
   `curl -s http://jts4:8780/state | jq '.active_speaker_setup | {active, active_group_count}'`.
   `active_group_count == 0` ⇒ passive ⇒ dumb member ⇒ the grouping ring is opened on
   **one** box (jts.local, by its own localhost snapclient into camilla#2). This is
   recorded, not worked around.

**Spikes.** Run in this order. **S0 gates everything after it.**

- **S5 — demoted to an opportunistic pre-deploy baseline** *(C-13, A1)*.
  > **v1's S5 is WITHDRAWN as specified.** It read
  > `/var/lib/camilladsp/configs/grouping_follower.yml`, which **cannot exist on this
  > fleet in any phase**: D1 refuses any bond while the coupling is `shm_ring` and both
  > boxes are `shm_ring` today, and after the flip jts.local is the LEADER (writing
  > `grouping_active_leader_crossover.yml`, `active_leader_config.py:93`) while jts4 is
  > passive `[M]`. v1 also ran it both "first, pre-flip" and "after step 0.3, post-deploy",
  > and gated PR-1 on it — three incompatible sequencings.
  >
  > PC-1 is code-verified twice independently, so **nothing about PR-1 needs a field
  > read.** What survives is obtainable and useful: **record snapclient's hard-sync
  > frequency on the current build as the baseline any ring-ingress change must beat**,
  > and — once step 0.2 has applied a baseline and a bond exists — read the leader
  > crossover config's `enable_rate_adjust` / `chunksize`. Opportunistic; gates nothing.
- **S0 — does snapclient negotiate and run against the ioplug at all?** Point a snapclient
  at `jts_ring_grouping` with a live reader attached.
  **PASS:** hw_params reports `sample rate: 48000 Hz, channels: 2, buffer time: 42667 us,
  periods: 16, period time: 2667 us, period frames: 128`; **no** `snd_pcm_avail_delay
  failed: … bad state (-77)` (the snapcast#1154 shape); no `Can't write to PCM device`. A
  `Period time too large, changing from 20000 to 2667` line is expected and benign.
  **FAIL:** any `Can't set …`; the `-77` line repeating; `-EBADFD` / repeated re-init.
  **If S0 fails, stop and take §3.2's fallback geometry — which must then establish itself
  on metal, not inherit "proven" (N6).**
- **S1 — is CamillaDSP's rate adjust inert on a ring capture?** `enable_rate_adjust: true`
  on a ring capture, no resampler, debug log level. **PASS:** `Capture device supports rate
  adjust` **ABSENT**; `Setting capture loopback speed to …` / `… gadget speed …` / `…
  async resampler speed …` all **ABSENT**. **FAIL:** any present. Do **not** use
  `capture_status.rate_adjust` as evidence — it publishes the request, not the applied
  value (`device.rs:897`, `:972`).
- **S2 — delay honesty, ripple, and occupancy** *(extended per S7)*. Log `snd_pcm_delay` at
  ≥100 Hz for 10 min under steady playback, **and** record the series as snapclient samples
  it (once per write iteration) **and** the occupancy distribution over the run.
  **PASS:** mean stable within ±1 ms over 10 min; coarse component ≤ one slot (2.667 ms);
  absolute ≤ 45.3 ms always (≪ `bufferMs_` 400 ms); occupancy concentrated rather than
  bimodal across slot boundaries. **FAIL:** a slow ramp; excursions beyond one slot;
  anything above 400 ms. This is also where `--latency`'s number comes from (EG-1).
- **S3 — snapclient stability over time (the real gate).** Bonded pair, ≥2 h, then ≥24 h.
  **PASS:** **zero** hard syncs after the first 60 s settle (`buffer_` median > 2 ms /
  `shortBuffer_` > 5 ms / `miniBuffer_` > 50 ms); zero `outputBufferDacTime > bufferMs`;
  zero `Failed to get chunk` in steady state; soft-sync rate inside ±500 ppm **without
  pinning at the clamp**; zero CamillaDSP `Capture read short`, zero `Prepare playback
  after buffer underrun`; close-time `full_waits` / `drop_no_reader` ≈ 0; snapclient CPU
  < ~10 % of one core, RSS ≈ 5 MB. **The CPU line is load-bearing at this geometry** — the
  ioplug tick is ~1500 Hz (§3.2) and snapclient polls it. **FAIL:** any recurring.
- **S4 — the >2 s dead-reader cliff.** SIGSTOP CamillaDSP ≥3 s while snapclient writes;
  resume. **PASS:** the dropout is bounded and snapclient recovers; `drop_no_reader`
  incremented **and visible somewhere an operator can see**; steady state within one
  `--stream.buffer` (400 ms). **FAIL:** unbounded drift; snapclient never resyncs; counters
  only in a close-time log nobody reads; recovery requires a restart. If the observability
  half fails, that is an in-session fix (a `/state` counter), not a deferral.
- **S6 — camilla#1 on the bonded leader under `shm_ring`** *(new, B1's hardware signal)*.
  The combination *Ring A capture + `File`/pipe sink* has never shipped: each half is
  shipped independently (Ring A capture on every solo armed box; the pipe sink on every
  bonded leader) but not together. **PASS:** camilla#1's log shows the ring capture
  attached; zero `Capture read short`; the snapfifo has a live reader and snapserver
  reports clients; audio reaches jts4. **FAIL:** silence with healthy daemons — the exact
  shape B1 exists to prevent — or recurring short reads.

**Step 6 — the S0-sync bench's owed bar (EG-2).** The ≥24 h xrun soak **folds into S3 at no
extra cost** and is in scope. The acoustic p99 < 5 ms is **owner-ruled re-scoped out**: it
needs two live speakers and jts.local stays on dummy loads. Phase 1 delivers the electrical
half and records the acoustic half as still owed against #889.

### 10.3 Evidence scope, stated before the pass rather than at close-out *(S8, PC-6)*

The evidence file says exactly this and no more:

- **Demonstrated on hardware:** the grouping ring as a transport (snapclient writer +
  CamillaDSP reader), on jts.local's **active-speaker-LEADER** instance (camilla#2), which
  runs the byte-identical `build_baseline_profile_candidate(driver_domain=True, …)` call as
  an active follower — verified in code, with only `state_path` / `config_path` differing.
- **Not demonstrated on this fleet:** the active-**FOLLOWER** role instance
  (`grouping_follower.yml`), because jts4 is passive and no other roleful box is available
  (jts3 forbidden, jts5 unplugged). It closes on code review plus the leader instance.
- **Not demonstrated:** the acoustic p99 (owner-ruled, owed against #889).
- **Not reachable:** "snd-aloop no longer loads on any box" (owner-ruled; #2481 closes
  narrow).

**Step 7 — evidence file** at `captures/8.7-EVIDENCE-grouping-ring-<date>.md`:
print-what-you-assert, every number with the command that produced it, every pass/fail
signal with its observed value, and the four scope statements above verbatim.

---

## 11. Risks, with mitigations

| # | Risk | Mitigation |
|---|---|---|
| R1 | **snapclient cannot negotiate the single-valued ioplug hw_params**; snapcast#1154's `snd_pcm_start()`-on-empty-buffer shape against a plugin PCM is open upstream. | S0 gates everything. Fallback geometry named (§3.2) — and explicitly *not* "proven" (N6). PR-1 ships the conf.d without a consumer, so a failed S0 costs one file. |
| R2 | **The sync signal is coarse-grained by the slot size** (2.667 ms) against a 2 ms long-median. | v1's phase-lock `[I]` is withdrawn; the geometry now *minimises* the coarseness rather than arguing around it. S2 records the series snapclient actually samples plus the occupancy distribution; S3's zero-hard-sync bar is the gate. |
| R3 | **Drops are structurally invisible to the writer** — `.transfer` always returns `size`, the publish result is discarded, no `-EPIPE` is ever raised, `full_waits` surfaces only in a close-time `SNDERR`. | S3 reads the close-time counters; S4's pass bar *requires* the counter be operator-visible, and failing that half is an in-session fix. |
| R4 | **The untested half of the plugin is exactly the half a new writer exercises** — `make test` never compiles `pcm_jts_ring.c` (`Makefile:51-55` vs `:76-77`). | Named, not fixed: a C harness is a larger, separable piece of work. S0/S3/S4 exercise those paths on metal. |
| R5 | **The gate narrowing removes a fail-closed gate**, and B1 shows what the gate was silently protecting. | The gate is narrowed, never deleted (the dumb-member cell stays blocked), and its **second** subject is removed rather than left implicit: the bake becomes coupling-aware in the same PR. PR-5 is safety-class. T-5 pins both directions agreeing; T-8 pins the bake and mutation-kills the regression. |
| R6 | **`--latency` ships at 0 while the code claims the pipeline latency "is nulled"** — inherited, not introduced. | EG-1: prose trued in PR-6; a measured default only if S2/S3 yield one number that generalises. |
| R7 | **snapclient version drift** lands silently on the next grouping opt-in. | §8.1: recorded probe + doctor drift warn. No apt pin. |
| R8 | **A conf.d geometry change cannot take effect on a box that already created the ring since boot** (§3.4's deliberate no-`rm -f`). | Fail-loud by construction: `-EINVAL` at open, snapclient goes `failed`, visible on `/state` + doctor. Remedy documented beside the `rm -f` block. |
| R9 | **jts4's reconcilers stayed dead 9+ h with no retry.** | Step 0.1 heals it and states the recovery model; an unacceptable window is an in-session fix. |
| R10 | **The ioplug tick at 128 frames is ~1500 Hz**, and snapclient polls it. | Not a new cost class — Ring A ships `period_frames 128` and CamillaDSP polls it on every armed box today `[M]`. S3's CPU bar measures it for snapclient specifically; the fallback geometry halves it if needed. |
| R11 | **Ring A capture + `File` pipe sink is a combination that has never shipped** (camilla#1 on a bonded leader under `shm_ring`). | Each half is independently shipped. S6 is its dedicated hardware signal, with silence-with-healthy-daemons as an explicit FAIL. |

---

## 12. Evidence gaps carried, and their disposition

- **EG-1 — `--latency`.** `DEFAULT_CLIENT_LATENCY_MS = 0` (`jasper/multiroom/config.py:80`)
  while `reconcile.py:186-196` / `:517-525` claim the fixed pipeline latency "is nulled"
  `[M]`. The knob is household-settable (0..1500). **Disposition:** PR-3 deletes the aloop
  rationale; PR-6 states what is true. S2 produces the ring's contribution; the rest needs
  the acoustic measurement that is re-scoped out. **A measured default is set only if
  S2/S3 yield a number that generalises; otherwise the value stays 0 and the doc says so.**
- **EG-2 — the S0-sync bench's unmet bar.** 24 h soak folds into S3; the acoustic p99 is
  owner-ruled re-scoped (§10.2 step 6).
- **EG-3 — the stale doc pointer** *(N4, corrected)*. `audio_runtime_plan.py:1671-1674`
  carries the `~194 ms` comment and **no doc pointer at all** — v1 mislocated the break.
  The real stale chain is one hop out: `docs/HANDOFF-usb-latency-measurement.md:159` cites
  a `HANDOFF-usb-low-latency.md` *"conservation law"* section, and
  `git grep -n "conservation law" docs/HANDOFF-usb-low-latency.md` returns **zero** `[M/N]`,
  verified directly in both rounds. **Disposition: PR-6** (which opens the docs), not PR-5.
- **EG-4 — the repo's oscillation attribution.** `camilla_yaml.py:137-153` gives two
  reasons for `RING_CAMILLA_ENABLE_RATE_ADJUST = False`; the second is a mis-attribution
  (the documented oscillation requires an async resampler, which the ring cannot have —
  memo §1.2) `[M]`. **Fix in passing** in PR-3, which already opens that file.
- **EG-5 — `ring_confirm_strike_write_failed` has zero test hits** `[N]`, independently
  confirmed by the review. Out of Phase-1 scope; Phase 2 inherits it named.
- **EG-6 — `migrate_grouping` migrates `JASPER_GROUPING_ENABLED` while `load_config` reads
  `JASPER_GROUPING`** (`env-migrations.sh:652-660` vs `multiroom/config.py:583`; the former
  has exactly one hit repo-wide) `[M/N]`. **Fixed in PR-0, outside the stack** (C-14).
- **EG-7 — the UMask invariant is already inexact, and by exactly one unit** *(N5)*.
  Verified across `deploy/systemd/`: `jasper-fanin.service:130`, `jasper-outputd.service:64`,
  `librespot.service:38`, `shairport-sync.service:50` and
  `bluealsa-aplay.service.d/jts-output.conf:41` all carry `UMask=0007`; **`jasper-camilla.service`
  carries none anywhere in its 127 lines** `[M/N]` — while the same header's enumeration
  names `jasper-camilla` as a Ring B writer. So `deploy/tmpfiles/jts-ring.conf`'s *"Every
  unit that writes a ring therefore carries `UMask=0007`"* is false for exactly one unit
  today. Phase 1 fixes the unit it touches (snapclient) and **does not claim the invariant
  holds**. Camilla's is surfaced, not fixed — it is an AXIS-1 unit and belongs with whoever
  owns that sweep.
- **EG-8 — `_effective_camilla_chunksize_setting` is single-valued on a two-CamillaDSP
  leader.** It answers one chunk for a box that runs two. Approximate before and after
  Phase 1; recorded so Phase 2 inherits it named, and so §3.2's use of it as the ring-chunk
  authority is not read as a claim that it models a bonded leader.

---

## 13. Owner decisions — RULED (settled context)

All four v1 questions are answered. Recorded here so the design reads as settled rather
than open.

- **OD-1 — leader/follower assignment: jts.local = LEADER, jts4 = FOLLOWER.** The memo's
  inversion is not taken. Rationale confirmed: the active-speaker leader traverses the same
  ingress through camilla#2 (PC-4 first half); it keeps snapserver off the 512 MB Zero 2 W;
  the dummy-load box carries the re-pointed CamillaDSP; and it exercises the ACTIVE-ring
  writer-lock handoff, which has never had a hardware pass. **Amended by PC-4's second
  half:** the leader also runs camilla#1, which the ingress argument does not cover — that
  is B1, fixed in PR-5, and given its own hardware signal (S6).
- **OD-2 — jts.local's durable baseline WILL be applied**, as hardware step 0.2.
- **OD-3 — dummy loads stay; the acoustic p99 is re-scoped out**, owed against #889 and
  recorded in the evidence file (§10.3).
- **OD-4 — #2481 closes narrow.** The done-clause gap is stated in the close-out comment;
  axes 2/3 are a successor scope question that gates nothing here.

---

## 14. Where this revision DISAGREES with the review

Six items, each with evidence, all from an independent re-derivation rather than
inheritance. Three are additions; three are corrections to review sub-claims. **None
overturns a finding** — S6's, S3's and ND-2's conclusions get stronger, not weaker.
Items 1-5 were adjudicated in the delta re-review (4 and 5 withdrawn in this design's
favour; 1-3 split with these halves standing); item 6 is new in v2.1.

1. **The review's S9 is incomplete: a third test module breaks, and it is the one the
   clamp deletion hits.** `tests/test_active_speaker_driver_domain.py` imports
   `FOLLOWER_LOOPBACK_MIN_CHUNKSIZE` at `:228` and `:245` and asserts the emitted chunksize
   equals it at `:233`, `:238`, `:248` `[M]`. The review's B2 asks for the clamp to be
   re-derived but does not name the module that pins it. Folded into §8.2 as PC-7.

2. **B1's option-2 parenthetical — *"it puts a second consumer on Ring A"* — is not
   correct.** Ring A is SPSC, and on a roleful box camilla#1 is its only reader `[M]`. On a
   bonded active leader, camilla#1 reads Ring A and camilla#2 reads the *grouping* ring;
   there is no contention and no second Ring-A consumer. The review's other concern in the
   same sentence — that camilla#1's pacing changes — is real and is why S6 exists; but the
   change is smaller than "a new derivation is owed": *Ring A capture* is shipped on every
   solo armed box and *File pipe sink* is shipped on every bonded leader. What is new is
   only their combination, which is exactly what S6 measures. Recorded as R11.

3. **S7's "quantized to whole slots" is an upper bound, not an identity.** `.delay` is
   `occupancy_slots × period_frames + **stage_frames**`, and `stage_frames` is zero only
   when the writer submits whole multiples of `period_frames` (`pcm_jts_ring.c:418-433`,
   `:455-457`) `[M]`. snapclient writes `framesAvail` per iteration, not one period
   (`alsa_player.cpp:641-648`) `[M, memo §1.5]`, so the stage term is generally non-zero and
   the signal is frame-granular with a slot-sized *coarse component*. **This does not change
   the conclusion — it strengthens it**: the slot size bounds the coarseness either way, so
   128 is right and 1024 was wrong. §3.2 states the bounded form rather than the identity,
   and S2 measures the actual series rather than assuming either.

4. **S6 undercounts `HANDOFF-distributed-active.md` too: nineteen sites, not thirteen.**
   `git grep -n "round-trip snd-aloop\|round-trip loopback\|the round-trip" --
   docs/HANDOFF-distributed-active.md` returns **19** hits; the review's thirteen are each
   real, and `:903, :1163, :1182, :1189, :1257, :1288` are missing from its list `[M]`. The
   review's *conclusion* is thereby strengthened, and so is its lesson: v1 named six line
   numbers, the review named thirteen, an independent grep found nineteen, and v1's own
   six are invisible to that grep because they discuss pair 6 without spelling
   "round-trip". **This is why §9 is now marked non-exhaustive and PR-6's sweep owns
   completeness — no line list is a bound.**

5. **S3's enumeration has five entries, not four.** `jts_ring_shm.h:120-151` also lists
   `bluealsa-aplay.service RestartSec=5 — in the JTS drop-in
   deploy/systemd/bluealsa-aplay.service.d/jts-restart.conf` `[M]`, which the review's
   summary omits. snapclient is therefore the **sixth** writer, not the fifth. Worth
   recording because bluealsa-aplay is precisely the writer whose omission the guard's own
   docstring cites as the reason the enumeration exists.

6. **ND-2 is right in direction and wrong in value: the bake's chunk on jts.local is 256,
   not 128.** The nit correctly catches that v2's "1024-frame chunk" is not the box under
   test — `resolve_camilla_chunksize`'s unset-env path returns the DAC profile's floor, not
   the global default (`camilla_config_contract.py:212`) `[M]`, which I had initially
   mis-read as a `max()` and re-checked. But its replacement number cites the conf.d's
   *"an Apple box, whose floor IS 128"*, and that sentence is about `outputd_period_frames`
   — a different `LatencyFloor` field. The field `_active_camilla_floor("camilla_chunksize")`
   reads is `camilla_chunksize`, and the Apple-dongle profile declares **256** with its
   measurement in the adjacent comment (*"CamillaDSP chunk 256 / target 1536, outputd period
   128"*, `jasper/audio_hardware/dac.py:381-390`) `[M]`. So camilla#1 reads Ring A **two
   slots per chunk** on jts.local — still coherent, still stronger than v2's claim, but not
   the one-slot cadence the nit asserts. §5.2(a) states 256.

Everything else in the review — round 1 and the delta — is accepted and folded, including
both blockers and ND-A.

---

*Design proposal v2.1 produced 2026-08-18 against
`6e569e8dc8e572a8d648d332c414374b8394496e`. Read-only: no repo file was edited, no build,
test, deploy, or ssh was run in any round.*
