# Independent adversarial DESIGN review — Phase 1 grouping ring

**Target:** `captures/DESIGN-PROPOSAL-grouping-ring-2026-08-17.md` (1,095 lines)
**Bar:** 0 blockers / 0 should-fixes before seal.
**Pinned SHA:** `6e569e8dc8e572a8d648d332c414374b8394496e` — verified by
`git -C /Users/jaspercurry/Code/JTS/.claude/worktrees/loopback-retirement-ring-3a4b6e rev-parse HEAD`
as the first action; matched exactly. Every code citation below is from that worktree.
Read-only: no repo file edited, no build, no test run, no ssh, no deploy.

---

## Verdict

| Severity | Count |
|---|---|
| **BLOCKER** | **2** |
| **SHOULD-FIX** | **9** |
| NIT | 6 |
| CONFIRMED-STRONGER | 4 |

**Not sealed.** The two blockers are not wording problems: each is a case where the
design's own central argument is false for one of the two roles it enables, and in
both cases the hardware plan's *first* step runs straight into it.

Both blockers share a root cause worth naming once: **the design derived the
follower's chain carefully and then assumed the leader is the same chain with
different paths.** PC-4 established that at the *emitter* — correctly, and I
reproduce it — but the leader also runs a **second** CamillaDSP whose config comes
from a different emitter with different defaults, and neither blocker is visible
unless you read that one.

---

## BLOCKERS

### B1 — the narrowed gate legalizes a silent bond: an active-speaker LEADER's camilla#1 captures a device fan-in stops writing under `shm_ring`

**The design's claim** (§5.1, fact 3):

> So on a bonded **active endpoint**, `shm_ring` changes exactly one thing: whether
> fan-in writes Ring A or aloop pair 7 — and on an active *follower* camilla#1's
> capture is overridden to the grouping lane anyway, so fan-in's egress has no
> reader under either coupling. `[I]`, from (1), (2) and `baseline_profile.py:1842-1847`.

and the verdict it supports:

> **Verdict: the gate's subject is the dumb-member `dac_content` lane. Its application
> to active endpoints is over-broad.**

**Re-derivation.** The sentence is true for the active *follower* and false for the
active-speaker *leader*. A leader runs **two** CamillaDSP instances, and only camilla#2
is built by the driver-domain emitter whose capture the design re-points.

camilla#1 on an active-speaker leader is the *program bake*, emitted here —
`jasper/multiroom/active_leader_config.py`, step 2 of `precheck_active_leader`:

```python
    emit_active_speaker_program_bake_config(
        profile,
        room_peqs=[],
        output_trim_db=output_trim_db(profile, settings),
        out_path=LEADER_BAKE_CONFIG_PATH,
        profile_id=f"grouping-{cfg.bond_id or 'bond'}",
    )
```

**No `capture_device` is passed.** The emitter's signature
(`jasper/active_speaker/camilla_yaml.py:3796-3809`) binds the default:

```python
def emit_active_speaker_program_bake_config(
    profile: SoundProfile,
    *,
    room_peqs: list[PeqFilter] | None = None,
    output_trim_db: float = 0.0,
    capture_device: str = DEFAULT_CAPTURE_DEVICE,
```

and `jasper/camilla_config_contract.py:26` is
`DEFAULT_CAPTURE_DEVICE = "plug:jasper_capture"` — the **snd-aloop fan-in tap**, not
Ring A (`RING_CAPTURE_DEVICE = "jts_ring_capture"`, `jasper/fanin_coupling.py:103`).
This path never calls `active_emit_devices`, so no coupling-aware resolution reaches it.

The repo states the resulting failure mode itself, in `active_emit_devices`'
docstring — `jasper/active_speaker/camilla_yaml.py:415-424`, the twenty lines
immediately above the `:466-485` range the design cites:

> THE COUPLING IS END-TO-END AND SO IS THIS: under `shm_ring` fan-in writes Ring A
> and stops feeding the snd-aloop tap, so a graph whose sink is the ring while its
> source is still `plug:jasper_capture` captures a device nobody writes — **digital
> silence with every daemon healthy**. That trap is QUIET: the plan compares capture
> CHANNELS (2 == 2 passes) and the arm's width gate only holds ring-NAMED lanes to
> the wire, so a tap capture is not-inspected rather than refused.

The leader's sink is a `File`/FIFO rather than a ring, but the *source* half is
identical and so is the quietness: nothing inspects a tap capture.

**Why the gate is what stops this today.** `jasper/multiroom/active_leader_config.py:211-219`
(D5) refuses the bond before anything is emitted:

```python
    coupling_support = coupling_supported_for_route(
        read_persisted_coupling(), "active_leader"
    )
    if not coupling_support.supported:
        raise ActiveLeaderError(...)
```

§5.2 says "D3, D4, D5 need no logic change — they already consume D2." Narrowing D2 so
an active endpoint is permitted therefore opens D5 for the leader, and
`outputd_grouping_env`'s active-endpoint branch (verified at
`jasper/multiroom/reconcile.py:683-690`, every `OUTPUTD_DAC_CONTENT_*` key set to `""`)
makes `dac_content_lane_armed` **False** for a leader — so the new predicate permits
exactly this cell.

**Blast radius.** The leader's camilla#1 is the producer of the whole bond's audio:
`plug:jasper_capture` → Layer B/C → `File` → `/run/jasper-snapserver/snapfifo` →
snapserver → every member. A silent capture is a **silent group**, on every speaker,
with `jasper-doctor` green and no cue. `docs/HANDOFF-distributed-active.md:148`
records the same topology (`camilla#1: fan-in → B/C → pipe`).

**And this is the first hardware step.** OD-1 recommends jts.local = LEADER; the fleet
probe recorded `JASPER_FANIN_CAMILLA_COUPLING=shm_ring` on jts.local. Step 0.3 deploys
the sealed stack, and S0 is then run against a bond whose program source is a device
nobody writes.

**What would make it pass** — any one of:
1. Keep the leader blocked: make the predicate `dac_content_lane_armed OR
   leader_program_bake_armed`, where the second conjunct is derived the same way (from
   the function that emits the bake), and re-scope §5.1's verdict to "the gate has two
   subjects, and Phase 1 retires one."
2. Move the leader's camilla#1 capture in the same PR: pass `capture_device` /
   `capture_format` from `active_emit_devices(...)` into
   `emit_active_speaker_program_bake_config`, so the program bake's source follows the
   coupling. This is a real design change (it puts a second consumer on Ring A and
   changes camilla#1's pacing — see B2's neighbourhood), and needs its own derivation
   plus a hardware signal for camilla#1, not just a kwarg.
3. Scope Phase 1's gate narrowing and hardware plan to the active **follower** only,
   and leave the leader on loopback coupling until Phase 2 — which also means OD-1's
   recommendation must change, because a `shm_ring` leader is the thing being deferred.

Whichever is chosen, §5.1's `[I]` needs re-stating so it covers both roles explicitly
rather than reasoning about "an active endpoint" and then evidencing only the follower.

---

### B2 — the 1024 clamp PC-1 discovered is not a geometry input to design around; on the ring path it is a defect, and §3.2 pins it into a test

**The design's claim** (§3.2):

> | `period_frames` | **1024** | = the emitter's capture chunk (`FOLLOWER_LOOPBACK_MIN_CHUNKSIZE`, `camilla_yaml.py:186-190`). One slot per chunk — Ring A's shipped relationship. |

and (§3.5):

> **Why not lower the chunk to 128 and copy Ring A's 128/2 exactly?** … It is declined
> for Phase 1 because it converts a transport swap into a *latency re-tune of a
> safety-critical crossover graph*…

and (§7.2) the pin it ships:

> `test_follower_clock_seam_chunksize_at_least_1024` → asserts the emitted chunksize
> **equals `GROUPING_RING_PERIOD_FRAMES`** on both branches.

**Re-derivation.** CamillaDSP has ONE `devices.chunksize`; it governs the **playback**
side as well as the capture side. The emitted YAML puts it above both blocks
(`camilla_yaml.py:3735-3751`). The design applies the one-slot-per-chunk rule to the
new capture ring and never checks the same number against the ring the graph plays
*into*.

The chain, verified end to end:

- `active_emit_devices` ring branch returns `chunksize=RING_CAMILLA_CHUNKSIZE`
  (`jasper/fanin_coupling.py:94` → **128**), `target_level=RING_CAMILLA_TARGET_LEVEL`
  (**128**), `queuelimit=1`.
- `build_baseline_profile_candidate` forwards them verbatim to the driver-domain
  emitter (`baseline_profile.py:2388-2401`, `chunksize=devices.chunksize`).
- The driver-domain emitter clamps, unconditionally
  (`camilla_yaml.py:3657-3661`, read directly):

```python
    # S1: floor the loopback capture chunksize (see FOLLOWER_LOOPBACK_MIN_CHUNKSIZE).
    if chunksize < FOLLOWER_LOOPBACK_MIN_CHUNKSIZE:
        chunksize = FOLLOWER_LOOPBACK_MIN_CHUNKSIZE
```

  → **1024**. `target_level` is **not** clamped, so the emitted pair is
  `chunksize: 1024` / `target_level: 128`.
- That graph's playback device is `RING_ACTIVE_PLAYBACK_DEVICE`. Its shipped geometry
  (`deploy/alsa/conf.d/60-jts-ring.conf`, block `pcm.jts_ring_active_playback`) is
  `period_frames 128 / n_slots 2` → **buffer = 256 frames**, and the ioplug advertises
  both single-valued (`pcm_jts_ring.c:996-1004`: PERIOD_BYTES min==max, PERIODS
  min==max). The period is not free to move: the same file states
  *"the ring slot IS one outputd DAC period"*, and a non-128 declared floor makes the
  reconciler **refuse** `shm_ring` (`reason=ring_slot_fixed_128`).

So the driver-domain graph a bonded active endpoint runs after Phase 1 is
**chunk 1024 into a 256-frame playback buffer** — chunk spanning four whole buffers.
The repo's own rule, in the same conf.d the design quotes for the one-slot argument
(`60-jts-ring.conf:48-49`):

> The paired CamillaDSP ring config is chunk 128 / target 128 / queuelimit 1;
> **chunk 256 would span the entire 2-slot buffer.**

`c/jts-ring-ioplug/jts_ring_shm.h:72-89` records what happened the last time the buffer
was smaller than the chunk (buffer 512 vs chunk 1024): *"the rate controller chased an
unreachable target, wound up, and drove the writer full (full_waits ~= every publish)
into stall/underrun flapping."*

**This is new, not pre-existing.** The *solo* active graph on a ring-armed roleful box
goes through `emit_active_speaker_baseline_config`, which receives the same
`devices.chunksize` (`baseline_profile.py:2406-2418`) and has **no clamp** — so today's
production graph is the coherent chunk 128 / target 128 / buffer 256. The 1024 pairing
exists only on the driver-domain path, which is unreachable today because the gate
refuses it. **Phase 1 is what makes it reachable**, on both roles (the leader's
camilla#2 runs the same emitter).

**A second owner already contradicts it.** `jasper/audio_runtime_plan.py:2754-2786`,
`_effective_camilla_chunksize_setting`, docstring *"Return the Camilla chunksize
generated YAML actually emits"*, forces `RING_CAMILLA_CHUNKSIZE` (128) under
`shm_ring` and warns that any other value *"is the loopback/hardware-floor value, not
the ring runtime value"*. After Phase 1 the generated YAML says 1024 while this
function — the one `/state` and the plan surfaces read — says 128. That is a
directly-checkable SSOT break the design creates and does not mention, and §3.2's
"SSOT" paragraph counts the places the geometry is spelled without counting this one.

**§3.5's reasoning is inverted by the same evidence.** 128 is not a "latency re-tune"
imported from outside — it is the value `active_emit_devices` already chooses for the
ring branch, the value the ACTIVE ring's slot is built for, and the value the plan
already claims is effective. 1024 is the outlier: an snd-aloop-EPIPE floor
(`camilla_yaml.py:183-189` states exactly that reason) applied to a transport that
raises no EPIPE at all — which §3.5 itself concedes when it renames and re-justifies
the constant. Keeping the floor and sizing a **new** ring to it propagates an artifact
the same section identifies as an artifact.

**What would make it pass.** Re-derive the chunk before the ring geometry, not after.
Concretely, the design must resolve which of these is true and say so with evidence:

- **(a)** the floor does not apply on the ring path at all (its stated reason is
  falsified there), so the driver-domain emitter's clamp becomes conditional on an
  snd-aloop capture — chunk stays 128, `target_level` stays 128, the grouping ring is
  **128 × 2** (matching Ring A and the ACTIVE ring), and §3.2's whole "forced by 1024"
  derivation is replaced. The §3.2 arguments that then need re-answering are the
  scheduling-margin and snapclient-negotiation ones, which is real work — but it is the
  work the geometry section owes; or
- **(b)** 1024 is genuinely required on the capture side, in which case the design must
  state what `chunk 1024 / target_level 128 / playback buffer 256` does to the ACTIVE
  ring, reconcile `_effective_camilla_chunksize_setting`, and carry a hardware signal
  for the playback side (`full_waits`, `Prepare playback after buffer underrun` on the
  driver-domain instance) — none of which is in S0–S4 today.

Until that is settled, §7.2's mutation pin (`chunk == GROUPING_RING_PERIOD_FRAMES`)
must not ship: it freezes whichever answer is chosen, and the design chose without
asking.

---

## SHOULD-FIX

### S1 — the ioplug wake-rate figure is wrong by 2.7×, and the citation points at the wrong function

**Claim** (§3.2): *"The ioplug's poll fd is a bare timerfd at period/4
(`pcm_jts_ring.c:866-879`). At 1024 that is 5.33 ms ≈ 188 Hz — at or below snapclient's
own wake rate at its default geometry."*

**Re-derivation.** `pcm_jts_ring.c:264-272` is the tick owner, and it clamps:

```c
static uint64_t tick_ns_for(uint32_t period_frames) {
    uint64_t period_ns = (uint64_t)period_frames * 1000000000ull / JTS_RING_RATE;
    uint64_t tick_ns = period_ns / 4;
    if (tick_ns < 250000ull) tick_ns = 250000ull;
    if (tick_ns > 2000000ull) tick_ns = 2000000ull;
    return tick_ns;
}
```

At `period_frames = 1024`: `period_ns` = 21.33 ms, `/4` = 5.33 ms, **clamped to 2 ms →
500 Hz**, not 188 Hz. The 128-frame figure (~1500 Hz) is right because 0.667 ms is
inside the band. The claim "at or below snapclient's own wake rate" is false either
way: a 1024-frame period means snapclient wakes ~47×/s, so the timerfd runs ~10× its
rate. `:866-879` is `poll_descriptors_count` / `poll_descriptors` — where the fd is
handed out, not where the cadence is set (`arm_timer`, `:274-283`), which is likely how
the clamp was missed. Note also the fallback geometry is **more** expensive, not less:
256 frames → 1.33 ms → 750 Hz.

**Pass:** correct the number and the citation, and drop or re-argue the "at or below
snapclient's wake rate" comparison. The decision may well survive (500 Hz still beats
1500 Hz), but a stated measurement in the load-bearing table must be right.

### S2 — the ring-writer contract is two-part; the design adopts one half and walks past the other

**Claim** (§3.5): *"`jasper-snapclient.service` ships `RestartSec=2s` — exactly on the
boundary. **Set `RestartSec=3s`.** … the unit burns one of its four `StartLimitBurst`
attempts, and four of those in 300 s park a bonded endpoint silent."*

**Re-derivation.** The design names the burst number and then does not act on it.
`deploy/systemd/jasper-snapclient.service:26-27` is `StartLimitIntervalSec=300` /
`StartLimitBurst=4`. The repo's standing rule for ring writers is not RestartSec alone —
`tests/test_renderer_ring_lanes.py:1806-1828`,
`test_every_ring_writing_renderer_tolerates_a_burst_of_refusals`:

> RestartSec alone is not enough: systemd's default limit (5 starts in 10 s) is what
> turns a transient refusal loop into a parked unit. Each ring writer declares a window
> generous enough that a burst cannot reach it.

with `assert int(burst.group(1)) > 5`. snapclient at 4 is below even systemd's default.

**Pass:** either raise `StartLimitBurst` to satisfy the platform rule the other three
ring writers satisfy, or state in the design why this writer is exempt (e.g. "its only
EBUSY source is its own predecessor, which `RestartSec=3s` removes; the remaining
refusal — a geometry `-EINVAL` — is permanent and parking is the desired outcome"). The
exemption may be right; leaving it unstated after quoting the number is not.

### S3 — a new ring writer is added, and the platform's enumerated set of ring writers is not updated (and its guard structurally cannot catch it)

`c/jts-ring-ioplug/jts_ring_shm.h:115-160` carries an explicit enumeration:

> Every ring writer's cadence, so the set is checkable rather than assumed:
> `- librespot.service RestartSec=5` … `- jasper-camilla RestartSec=2` …
> `- shairport-sync.service RestartSec=5` … `- correction lane: EPHEMERAL aplay writers`
> … Pinned by … `tests/test_renderer_ring_lanes.py` (each renderer's RestartSec against
> this constant).

Phase 1 makes `jasper-snapclient.service` the fifth ring writer. The design's §9
deletion set has **no entry for `c/jts-ring-ioplug/jts_ring_shm.h`**, so the sentence
"Every ring writer's cadence" becomes false on merge. The guard that exists to prevent
exactly this — `test_the_ring_liveness_window_is_enumerated_for_every_writer`
(`tests/test_renderer_ring_lanes.py:1832-1866`), whose docstring says *"A writer missing
from that list is one nobody checked — which is how bluealsa-aplay reached P6b as the
third writer with its cadence unknown to the repo"* — iterates `rl.RENDERER_LANES` and
therefore **cannot see** a non-renderer writer. It will stay green while the property it
names rots.

This is the ADD direction of the enumerated-set class the design correctly invokes for
its own subject sweep (§8.2); the sweep list (`pair 6`, `Loopback,0,6`, …) cannot catch
it because the new subject is *ring writer*, not *loopback*.

Related: T-6's *"a unit-file pin for `UMask=0007` and `RestartSec` > the 2 s heartbeat,
each with the citation in the test docstring"* creates a **second** owner of a property
the header + `_ring_liveness_window_sec()` already own. The existing pin reads the C
constant; a bespoke hardcoded "2 s" would drift from it.

**Pass:** add the snapclient entry to the header enumeration in the same PR, and make
T-6's pin read `JTS_RING_WRITER_LIVENESS_TIMEOUT_NS` the way
`_ring_liveness_window_sec()` does rather than hardcoding the window.

### S4 — `invalid_grouping` silently flips from blocked to allowed, and T-5's matrix hides it

**Claim** (§5.2): *"blocks `shm_ring` iff `route_mode` is grouping-enabled **and**
`dac_content_lane_armed`. `solo`/`unknown` keep their existing fail-open behaviour
verbatim."*

**Re-derivation.** `_GROUPING_ENABLED_ROUTE_MODES = frozenset({"active_leader",
"active_follower", "invalid_grouping"})` (`audio_runtime_plan.py:279-281`) — three
modes, and the design's sentence accounts for two of the five total by name (`solo`,
`unknown`) while never naming the third grouping-enabled one.

Mechanically, `invalid_grouping` means `cfg.enabled and cfg.error is not None`. That
falls past **both** branches of `outputd_grouping_env` into the final off-path return
(`reconcile.py:727-730`, `OUTPUTD_DAC_CONTENT_FIFO_ENV: ""`), so
`dac_content_lane_armed` is **False** and `shm_ring` becomes **allowed** where it is
blocked today.

The change is probably correct — `active = cfg.enabled and cfg.error is None`
(`reconcile.py:1814`) means no bond forms at all on an invalid config, so D1 already
never fires there and the box is effectively solo. But it is an unstated relaxation of a
fail-closed gate, which is exactly the kind of thing this review exists to catch.

Worse, T-5 as specified cannot catch it: *"parametrized over `{active_leader,
active_follower, invalid_grouping} × {dac_content_lane_armed True, False}`"*
parametrizes the predicate **independently of the config**, so it pins the
`invalid_grouping × armed=True` cell that the real derivation can never produce, and
never asserts what the real derivation answers for an invalid config.

**Pass:** name `invalid_grouping`'s new verdict and its justification in §5.2, and make
T-5 **derive** `dac_content_lane_armed` from a `GroupingConfig` + `active_endpoint`
fixture rather than parametrizing it as a free boolean — otherwise the test matrix
proves things about combinations that do not exist.

### S5 — D5's reorder is a behaviour change, not "a statement reorder"

**Claim** (§5.2): *"D5 must move its coupling check below its strict topology load
(`active_leader_config.py:225-236`) so it has the topology the predicate needs; that is
a statement reorder, not a behaviour change."*

**Re-derivation.** Today the coupling check is at `:211-219` and the strict topology load
at `:225-236`. In the cell where a box is *both* ring-armed *and* has an unreadable
`topology.json` — reachable, and the code says so (*"The 2026-05-23 filesystem-loss class
corrupts topology.json too"*) — the raised `ActiveLeaderError` changes from
`coupling_support.reason` to `"topology_unreadable"`. That token is operator-facing: it
reaches the follower-status file, `/state`, and the journal. Both outcomes are
fail-closed, so there is no safety change, but the reported reason changes and any test
or doc pinning the current precedence changes with it.

**Pass:** state the precedence change and pin the new order, or keep the coupling check
first by having the predicate tolerate an absent topology (fail-closed to "armed").

### S6 — the deletion set is materially incomplete by its own subject, and its line lists read as exhaustive

**Claim:** §9 is titled *"the deletion set, swept by subject"*, and its
`docs/HANDOFF-distributed-active.md` row names exactly six sites
(`:428, :430, :466-467, :697-698, :1372, :1405`).

**Re-derivation** — one of the design's own §8.2 sweep terms, run over tracked
non-test files:

```
git grep -n "round-trip snd-aloop\|round-trip loopback\|the round-trip" -- jasper/ deploy/ docs/
```

`docs/HANDOFF-distributed-active.md` returns **:125, :147, :148, :177, :264, :286, :300,
:324, :357, :706, :792, :986, :1183** — thirteen sites, none of them among the six
named, several directly falsified:

- `:147` — the transport column of the role table: `| Follower | round-trip loopback (snapclient-fed) | …`
- `:148` — `| Leader (active) | camilla#1: fan-in → B/C → pipe; camilla#2: round-trip loopback → A |`
- `:177` — *"Points the follower's CamillaDSP **capture at the round-trip loopback**"*
- `:357` — *"snapclient round-trip loopback with `enable_rate_adjust` ON"* — false on
  **both** halves after Phase 1, and it is an inv-5 site the design's §6.2 claims to be
  truing in code while leaving the doc sentence standing
- `:706` — *"routes snapclient to the round-trip loopback (ALSA…"*
- `:986` — *"snapclient writes the round-trip loopback"*

Two whole files are missing from the table:

- `docs/HANDOFF-dsp-graph-carrier.md:486` — *"capture at the round-trip loopback, emits
  a driver-domain-only Layer-A"*
- `jasper/multiroom/member_config.py` — its module docstring is the ONE place that
  states the member-config policy, and says of the active follower: *"it captures the
  round-trip snd-aloop loopback and runs the driver-domain crossover so the tweeter is
  never fed full-range."* False after Phase 1. (Its dumb-follower sentence — *"its sink
  is the ALSA loopback, which HAS a clock to track"* — stays true; AXIS-1.)

**And one completeness claim is falsified outright.** §9's census-S4 row asserts:

> **Census S4 — all 5 sites, 3 shipped operator-facing strings** … Every one is inside a
> file this design's gate PR opens, so **Phase 1 owns all five** — none rides to Phase 2.

`git grep -n "solo-stereo-only"` over tracked non-test files returns **six** sites in
**four** files:

```
jasper/audio_runtime_plan.py:270      <- not in the design's list
jasper/audio_runtime_plan.py:285
jasper/audio_runtime_plan.py:824
jasper/fanin/coupling_reconcile.py:3399
jasper/multiroom/reconcile.py:1893
jasper/sound/graph_carrier.py:414     <- not in the design's list, and no PR opens this file
```

`jasper/sound/graph_carrier.py:414` — *"# shm_ring is solo-stereo-only; the active
baseline keeps its roleful"* — is in a file no PR in the wave map touches, so "none rides
to Phase 2" is false as written. (The design's five also conflate two tokens: `:3419` and
`reconcile.py:1914` carry *"until ring v2 (P8)"*, not *"solo-stereo-only"* — both need
truing, but they are different greps and counting them as one set is how `:270` and
`graph_carrier.py:414` fell out.)

**And §9 is a *deletion* table, while two of the design's own described edits are
*additions* to files the table never names**: `jasper/cli/doctor/audio_runtime.py`
(§6.1(a) names it twice with a line number — the new `_OUTPUTD_CONTENT_ALOOP_PCM`, and
the relocated + renamed `check_aloop_registered_substreams` lands there) and
`jasper/control/grouping_supervisor.py:313` (§6.4's retargeted comment). An implementer
scoping the diff from §9 will not see either. Say so, or retitle the table.

Two further un-listed `inv-5` homes, which matter because §6.2 widens inv-5's scope from
leader-only to every bonded role: `jasper/fanin_coupling.py:936` and
`jasper/sound/camilla_yaml.py:386` (*"inv-5: an active bond member runs rate_adjust off
(snapclient is the sole…"*). And three docs carry `hw:Loopback,{0,1},6` without appearing
in the table at all — `docs/HANDOFF-audio-graph-consolidation.md`,
`docs/HANDOFF-fan-in-daemon.md`, `docs/HANDOFF-speaker-output-reference.md` — each of
which needs reading against the migration rather than assuming AXIS-1 scope.

**Pass:** either complete the table, or mark it explicitly non-exhaustive and make
PR-5's sweep the owner of completeness — and in either case drop the "Phase 1 owns all
five / none rides to Phase 2" claim or extend a PR's file set to cover
`jasper/sound/graph_carrier.py`. Naming six specific line numbers in a repo whose
documentation rule says line numbers are load-bearing when used will read to the
implementer as the whole set.

### S7 — the `[I]` phase-lock argument cites a mechanism the code does not have, and the true mechanism is worse (slot-quantized delay)

**Claim** (§3.2): *"`[I]` snapclient samples the delay once per write iteration, and it
wakes when POLLOUT is granted — which the plugin grants **exactly when a slot frees**
(`pcm_jts_ring.c:881-904`) — so the sample is *phase-locked* to the same point in the
sawtooth, and the sampled series should be near-constant rather than a full-amplitude
swing."*

**Re-derivation.** `pcm_jts_ring.c:891-902` does not grant POLLOUT when a slot frees; it
grants POLLOUT whenever a publish **would not block**:

> Report POLLOUT (writable) iff a publish would proceed without blocking: the ring has
> space, OR there is no live reader … A FULL ring WITH a live reader is genuinely
> not-yet-writable

With `n_slots = 2` and a one-in-one-out steady state, occupancy sits at 0 or 1, so
POLLOUT is granted on essentially every 2 ms timerfd tick. The wake is timer-driven, not
slot-edge-driven; the cited mechanism is not the one in the file.

The real phase-lock comes from elsewhere, and it is sharper than "near-constant".
`.delay` is `slots * period_frames + stage_frames` (`:455-457`), and `stage_frames`
returns to 0 on every publish (`:425-430`). A writer submitting exactly `period_frames`
per `writei` therefore samples the delay with `stage_frames == 0` every time, so the
value snapclient sees is **quantized to whole slots**: {0, 21.33, 42.67} ms at the
proposed geometry. Not a smooth sawtooth it can average through — a 21.33 ms **quantum**
in the signal feeding a sync loop whose hard-sync medians are 2 ms and 5 ms. Whether
that is benign depends entirely on whether occupancy is *stable*, which neither S2 nor
S3 as written measures (S2 samples the free-running delay at ≥100 Hz — a different
series from the one snapclient consumes). The fallback geometry's quantum is 5.33 ms.

To the design's credit R2 says the `[I]` is *"not relied on"* and S3's zero-hard-sync bar
is the gate — that framing is right, and this finding does not overturn it.

**Pass:** replace the mechanism with the one the code supports, state the quantization
explicitly (it is the honest form of the risk), and add "occupancy distribution over the
run" to S2's recorded outputs so S3's result is interpretable rather than just pass/fail.

### S8 — the hardware plan may not exercise the follower ingress at all, and the design never establishes that it does

§10.2 sets jts.local = LEADER, jts4 = FOLLOWER, and §7.1 argues the emit-path diff is
exercised because leader and follower share the emitter.

But `_assemble_args` (`reconcile.py:582-597`) sends a box down the ALSA/`--soundcard`
path **only** when `active_endpoint` is true; otherwise it takes `--player
file:filename=` into the member FIFO and never opens the grouping ring.
`active_endpoint = active_follower or active_speaker_leader` (`:1835`), and
`active_follower = active and cfg.role == "follower" and box_is_active` (`:1822`), where
`box_is_active` comes from the saved output topology declaring active driver groups.

The fleet probe records jts4 with `sound_current.yml` (not an active-speaker graph) and
`grouping_allowed = true` — and PC-2's own derivation shows `grouping_allowed = True` is
what a **passive** box returns from the early return at `setup_status.py:767`
(`active_group_count == 0`). If jts4 is passive, it is a **dumb member**: the grouping
ring is opened on exactly one box in the plan — jts.local, by its own localhost
snapclient — and the active-**follower** ingress this design exists to move is never
exercised in Phase 1.

I cannot read the box, so this is a question the design must answer, not a fact I have
proven. But it is load-bearing: S0–S4's pass/fail signals are written as if two boxes
exercise the transport.

**Pass:** state jts4's active/passive classification with the command that establishes
it, and if it is passive, say plainly that Phase 1 validates the leader endpoint only
and that the active-follower ingress rides on jts.local's crossover instance (or
re-scope the fleet).

### S9 — the per-PR test impact set is incomplete: two test modules break, and neither is named anywhere in the design

§8.2 lists each PR's tests, and §9 lists five test files as touched
(`test_env_vars_codified`, `test_doctor_grouping_remnant`,
`test_multiroom_follower_config`, `test_multiroom_reconcile`,
`test_aloop_program_lane_width`). Two more break, both verified:

**`tests/test_multiroom_active_leader_config.py` — breaks on PR-3.** Line 160:

```python
    from jasper.multiroom.reconcile import GROUPING_LOOPBACK_CAPTURE, SNAPFIFO
```

and line 188:

```python
    assert f'device: "{GROUPING_LOOPBACK_CAPTURE}"' in crossover_yaml
```

PR-3 deletes that constant (§4 point 2; §9's `reconcile.py:185-225` row). The import
is function-local, so it fails when the test runs rather than at collection — a red
test either way, and the assertion would also need its expected device changed to
`GROUPING_RING_PCM`. Worth noting the design **cites this exact file** in §1 (for D5's
unit test at `:210`), so it was open and the constant use was not noticed. This is also
the test that pins the leader's camilla#2 build — the one B1 is about.

**`tests/test_multiroom_rate_adjust.py` — breaks on PR-4.** It is named nowhere in the
design. `:164-172`:

```python
    def spy_is_active_leader(cfg):
        ...
    monkeypatch.setattr(cfgmod, "is_active_leader", spy_is_active_leader)
```

with the docstring at `:145` making the *call-time resolution of `is_active_leader`* the
property under test. §6.2 changes the production call to `is_active_member`, so the spy
stops firing and the assertion at `:172` fails. (The module already imports and exercises
`is_active_member` at `:16,47-59`, so the widening fits its vocabulary — only the spy is
keyed to the old name.) This module is also the primary home of inv-5's test coverage
(its docstring opens *"inv-5 (docs/HANDOFF-multiroom.md §2)"*), so §6.2's widening
belongs here, not only in the doctor module.

**Pass:** add both to the owning PRs' test lists with the edit each needs. More
generally: the design derived its *prose* impact set by subject sweep and its *test*
impact set by inspection; the second needs the same discipline — `git grep -l` for each
deleted symbol and each renamed predicate is the mechanical version.

---

## NITS

- **N1** — §1's accepted-key list for the ioplug omits that `comment`, `type`, and
  `hint` are explicitly skipped before the unknown-key refusal (`pcm_jts_ring.c:1035-1036`).
  T-1's "only keys the ioplug accepts" should not fail a block carrying a `comment`.
- **N2** — §10.1 calls PR-1's conf.d *"inert — nothing opens it"*. Inert as a **PCM**;
  not inert as a **file**: it lands in `/etc/alsa/conf.d/`, which alsa-lib parses on
  every PCM open fleet-wide. T-1 is therefore load-bearing for availability, not
  tidiness — worth saying, since it also means PR-1 is not risk-free just because no
  consumer moves.
- **N3** — citation drift in load-bearing spots (substance intact in every case):
  `FOLLOWER_LOOPBACK_MIN_CHUNKSIZE` is at `:190`, not `:186-190`; the chunksize forward
  is `baseline_profile.py:2397`, not `:2398` (`:2398` is `target_level`);
  `_wait_for_active_content_pcm_release` is at `reconcile.py:1043-1092`, not
  `:2431-2521` (that range is a caller-side block inside `main()`);
  `is_active_speaker_box` is `:851-865`, not `:827-865` (`:827-849` is the separate
  `_active_speaker_box_state`); `outputd_grouping_env`'s active branch is `:683-690`,
  not `:685-696`; the confirm rung's "three acceptance branches" are at `:543-544`,
  `:549-550`, `:552-583`, not `:519-535` (which is comment prose).
- **N4** — EG-3 locates the stale pointer at `audio_runtime_plan.py:1671-1674`. The
  `~194 ms` comment is there verbatim, but it carries **no doc pointer at all**; a
  repo-wide grep for `usb-latency-measurement.md:159` returns zero. The real stale chain
  is one hop further out: `docs/HANDOFF-usb-latency-measurement.md:159` cites
  `HANDOFF-usb-low-latency.md "conservation law"`, and
  `git grep -n "conservation law" docs/HANDOFF-usb-low-latency.md` returns **zero hits**
  — so the disposition ("fix in passing where touched") is right but points at the wrong
  file. PR-4 opens `audio_runtime_plan.py`; the broken reference lives in
  `HANDOFF-usb-latency-measurement.md`.
- **N5** — §3.4 says *"Both ends of this ring run as root … so the AGENTS.md PR-#214
  class does not bite"*, then adds `UMask=0007` to snapclient. Correct, and worth
  noting for symmetry: `jasper-camilla.service` writes Ring B and also carries no
  `UMask=` (verified across `deploy/systemd/`), so the tmpfiles sentence *"Every unit
  that writes a ring therefore carries `UMask=0007`"* is already inexact. Adding the
  line to snapclient is right; the design should not imply the invariant currently
  holds.
- **N6** — §3.2 calls the fallback geometry *"the renderer-lane geometry already shipped
  for network-fed ring writers, so it is a **proven shape** rather than a guess."*
  `deploy/alsa/conf.d/61-jts-renderer-lanes.conf:8-14` says of itself: *"**INERT UNTIL
  ARMED.** … Nothing resolves these names until `jasper-audio-config renderer-lanes --arm
  <label>` has written the matching `JASPER_<RENDERER>_DEVICE` … On every unarmed box the
  renderer keeps writing its `*_substream` alias"*, and the campaign lifeline records
  per-lane arming as operator-explicit with the fleet default **unarmed**. Shipped is not
  exercised. Since the fallback is the named contingency for the single highest risk
  (R1/S0), either evidence that 256×16 has actually run somewhere, or downgrade "proven"
  to "shipped and legal" — which S0's own fallback branch would then have to establish
  on metal like any other geometry.

---

## CONFIRMED-STRONGER

- **C1 — PC-5 is right, and both readings are right about different edits.** I
  reproduce `_derive_registered_pairs` (`jasper/cli/doctor/grouping.py:1069-1108`) and
  confirm pair 6's only contributor is `GROUPING_LOOPBACK_PLAYBACK`. The two scenarios
  genuinely differ: pointing the constant at a **ring value** makes
  `_pair_from_loopback_pcm` return `None`, which trips the all-or-nothing
  `return None` and yields **`warn`** on every box (the conductor's spot-check);
  **deleting** the contributor without re-sourcing leaves `{0,1,2,3,4,7}`, so pair 6's
  live AXIS-1 holder becomes an offender and the check returns **`fail`** naming it (the
  design's claim). Both are true of different edits, and PR-2-before-PR-3 is correctly
  ordered for the one that matters.
  **Stronger, and this changes §6.1's cost estimate:** `grouping_pair` is also the
  function's second return value and is read at `:1189`, `:1248`, `:1256`, `:1259`; the
  *"deliberately NO grouping-pair-missing branch"* comment at `:1190-1195` and
  `tests/test_doctor_grouping_remnant.py::test_grouping_pair_is_always_registered` both
  become false when the contributor goes. §6.1(b) calls the re-home *"three lines of
  relocation and one `__init__` export"* — it is not; it is a signature change with four
  call sites, a comment, and a test that asserts the opposite of the new state.

- **C2 — the gate's second conjunct is the right shape, for a reason the design does not
  give.** `route_mode_from_grouping_config` (`audio_runtime_plan.py:864-876`) classifies
  purely on `cfg.role`, so `active_follower` is returned for a **dumb** member and an
  **active** follower alike — the mode names are misleading and the route mode alone
  genuinely cannot discriminate. The discriminator therefore *must* come from elsewhere,
  and `outputd_grouping_env`'s branch on `active_endpoint` (verified: active branch
  clears every `OUTPUTD_DAC_CONTENT_*` key at `:683-690`; dumb branch pins
  `OUTPUTD_CONTENT_BRIDGE_ENV: "direct"` at `:706`) makes the predicate derivable exactly
  as §5.2 describes. Also confirmed: `active_leader_check` has **zero** production
  suppliers (`git grep` over `jasper/` + `deploy/` finds only the internal
  pass-throughs), so `_route_mode_for_reconcile`'s binary leader/solo branch is
  test-only and the full five-way classification is what production runs.

- **C4 — the fallback geometry is legal against chunk 1024, and I confirm it the way the
  design did not show.** Two questions had to be answered separately. *(i) Against the
  ioplug's accepted ranges:* `period_frames 256` is inside `1..65536` and `n_slots 16` is
  exactly `JTS_RING_MAX_SLOTS` (`pcm_jts_ring.c:1094-1101`, `jts_ring_shm.h:90`), and 16
  is also what the header's own raise-to-16 rationale targets — legal, at the ceiling.
  *(ii) Against a CamillaDSP chunk of 1024 reading 256-frame slots:* legal, and by a
  mechanism worth recording, because "chunk == one slot" is **not** required on the
  capture side. `jts_ring_capture_transfer` (`pcm_jts_ring.c:757-809`) loops
  `while (delivered < size)` calling `capture_refill_destage`, which destages **one slot
  at a time** (`stage_capacity_frames == period_frames`, `:652`); a 1024-frame read
  therefore drains four slots per call, or returns a short read and re-polls when the
  ring runs dry. The rule that does bind is the buffer-span one the design quotes, and
  4096 ≫ 1024 satisfies it with room. So the fallback is sound as a *grouping-ring*
  geometry — which is worth stating explicitly, because B2 means the chunk on the other
  side of that graph is still an open question either way.

- **C3 — A3-iii's two hazards are both absent, and the design's shape is why.** No
  import cycle: `audio_runtime_plan` deliberately types `route_mode_from_grouping_config(cfg: Any)`
  and keeps its one `jasper.multiroom.config` import lazy inside a function (`:1357`),
  while `jasper/multiroom/active_leader_config.py:73` imports `audio_runtime_plan` at
  module level. Passing `dac_content_lane_armed` as a **bool** keeps that direction
  intact; a module-level import of the new predicate into `audio_runtime_plan` would
  invert it, so the design should say the bool parameter is load-bearing rather than
  incidental. No permission problem either: both `jasper-fanin-coupling-auto.service`
  and `jasper-grouping-reconcile.service` declare no `User=` (root), and
  `load_config` is total — *"File absent / unreadable => the all-off config"*
  (`multiroom/config.py:561-580`) — so an unreadable config fails **open** to `solo`,
  identical to today's behaviour and not made worse by the narrowing.

---

## Adjudication of the conductor's amendments

### A1 — S5 re-sequencing: **UPHELD, and the contradiction is deeper than recorded**

The amendment says S5 gates PR-1 but is sequenced post-deploy, and needs a bonded
roleful endpoint that cannot exist pre-OD-2. All three legs check out, and there is a
fourth:

1. **Triple sequencing contradiction, in the design's own text.** §10.2 lists S5 under
   *"Steps S0–S5 … Run in this order"*, which follows Step 0.3 (*"Deploy the sealed
   stack to both boxes"*) — i.e. post-flip. Yet S5's own text says it runs *"On the
   **pre-flip** build"* and that a 128 reading means *"§3.2's geometry must be
   re-derived **before PR-1 merges**"* — a gate on the first PR, before any deploy.
2. **The state S5 reads cannot exist on this fleet, in any phase.** D1
   (`reconcile.py:1902-1920`) refuses to form **any** bond while
   `read_persisted_coupling() == COUPLING_SHM_RING`, and the fleet probe recorded
   `shm_ring` on **both** boxes. So no `grouping_follower.yml` exists on either box now.
3. **And it cannot exist after the flip either, on this fleet.** `grouping_follower.yml`
   is written only by the active-**follower** arm (`follower_config.py:59`); jts.local is
   the designated LEADER and writes `grouping_active_leader_crossover.yml`
   (`active_leader_config.py:93`), and jts4 — if passive, per S8 — never runs the
   driver-domain emitter at all. S5's literal `grep enable_rate_adjust
   /var/lib/camilladsp/configs/grouping_follower.yml` has no target in this plan.
4. **The resolution is sound and the evidence is now stronger than the amendment
   assumed.** PC-1 is code-verified twice independently (my own read of
   `camilla_yaml.py:3657-3661` plus `baseline_profile.py:2388-2401`), so nothing about
   PR-1 needs a field read. Demoting S5 to an opportunistic pre-deploy baseline is right.

**Adopt A1**, and additionally fix S5's target: the useful pre-deploy baseline is
snapclient's hard-sync frequency on the current build (which S5 already asks for and
which *is* obtainable), not a config read of a file the fleet cannot produce.

### A2 — EG-6 fixed in PR-5: **UPHELD on "fix it"; DISSENT on "in PR-5"**

The bug is real and I reproduce it independently. `deploy/lib/install/env-migrations.sh:652-660`
lists `JASPER_GROUPING_ENABLED`; `git grep -n JASPER_GROUPING_ENABLED` returns **exactly
one hit repo-wide** — that line. `multiroom/config.py:583` reads bare `JASPER_GROUPING`,
which is also what the single control-plane writer emits
(`jasper/control/server.py:_write_grouping`). The consuming loop is
`grep -E "^${k}=" "${jasper_env}"` — an exact anchored match, so no prefix rescue. So the
migration migrates a key nothing reads, and the key that matters has no migration path.

**Fix-findings applies** — surfacing a located, one-line-class bug converts the session's
work into the owner's to-do list, and the design's own justification ("quietly changing a
migration key list inside a transport PR is scope creep") is answered by simply not doing
it quietly.

**But PR-5 is the wrong home, for a reason the amendment does not weigh.** PR-5 is the
**only deploying PR** in a five-PR stack. `migrate_grouping` runs inside `install.sh` on
**every deploy on every box**, and the fix that actually closes the gap is *adding*
`JASPER_GROUPING` to the migrated set — which means any box carrying a stale
`JASPER_GROUPING=` line in `/etc/jasper/jasper.env` would have that value promoted into
`grouping.env` at install time. Given the standing fact that a Pi's `jasper.env` is a
frozen install-time seed, that is a fleet-wide behaviour change bundled into the single
deploy of an unrelated stack — the worst moment to discover it.

**Adopt A2's direction, reject its placement.** Fix EG-6 in its **own single-gate PR**,
outside the stack, whose body carries the read-only fleet evidence
(`grep -n '^JASPER_GROUPING' /etc/jasper/jasper.env` on both boxes — one command, no
deploy) and states which direction the fix takes and why. That satisfies fix-findings
without loading the deploying PR.

### A3 — the three questions, answered from code

**A3-i — fan-in on a bonded active endpoint under `shm_ring`, Ring A reader-less.**
Answered, and it is the seam B1 sits on. For the **active follower**: camilla#1's capture
is overridden to the grouping lane (`baseline_profile.py:1842-1847`), so Ring A has no
reader; the writer's no-reader path is bounded by construction — the ioplug's dual-mode
`avail` keeps the writer moving and `publish` free-run-drops with `drop_no_reader`
counted (`pcm_jts_ring.c:360-368`, `:426-430`), which is the same "write into a void"
shape the aloop pair-7 egress already has today, so the design's `[I]` is correct here.
The grouping reconciler does **not** park fan-in for this. For the **active-speaker
leader**: Ring A *also* has no reader, because camilla#1 is not on Ring A at all — it is
on `plug:jasper_capture`, which fan-in stops feeding under `shm_ring`. So the honest
answer to A3-i is: bounded and quiet on the follower; **silently wrong on the leader**.
That is B1.

**A3-ii — the leader's camilla#1 Ring-A-capture → `File` snapfifo playback.** The
premise is **refuted**: camilla#1 on a bonded active leader does not capture Ring A. It
captures `plug:jasper_capture` (`emit_active_speaker_program_bake_config`'s default,
`camilla_config_contract.py:26`) and plays to `File` at `SNAPFIFO`
(`sound/camilla_yaml.py:393-401`), with `enable_rate_adjust=False` enforced twice — the
call passes `False` and `emit_sound_config` **raises** if a pipe sink is given `True`
(`sound/camilla_yaml.py:278-284`). Pacing: the `File` backend has no output clock (the
emitter says so at `camilla_yaml.py:3826-3828`), so the graph is paced by its ALSA
**capture** clock plus write-blocking backpressure from snapserver's read end — which is
why the apply must run after snapserver is up (*"a FIFO write-open blocks until a reader
exists"*). Under **loopback** coupling that capture clock is snd-aloop's kernel timer and
the arrangement is sound. Under **shm_ring** the capture device has no writer at all, so
the question is not "is the pacing sound" but "is there any signal" — and there is not.
Its chunksize also comes from `resolve_camilla_chunksize()`, not ring geometry, and the
1024 clamp does not reach it.

**A3-iii — import direction and process permissions.** Both hazards absent; see **C3**.
The one thing to add to the design: the bool parameter on
`coupling_supported_for_route` is what keeps the direction legal, so it should be stated
as a constraint ("`audio_runtime_plan` must not import the predicate at module level")
rather than left as an implementation detail a later refactor could undo.

---

## What changes the campaign's premises

1. **PC-4 needs a second half.** It is confirmed at the emitter — the leader's camilla#2
   and the follower's camilla#1 are byte-identical builds apart from two paths (verified:
   the only kwarg differences are `state_path` and `config_path`). But the leader **also**
   runs camilla#1 from a different emitter with a coupling-blind capture default. OD-1's
   argument *"It exercises the seam under test"* is true of camilla#2 and false of
   camilla#1, and the difference is what B1 is. Whatever OD-1 decides, PC-4 should be
   amended to say "same ingress, **plus** a second instance the ingress argument does not
   cover."

2. **PC-1 is a defect report, not a measurement.** The campaign has been treating the
   1024 clamp as the geometry's forcing input. It is an snd-aloop-EPIPE artifact
   (`camilla_yaml.py:183-189` states that reason) that (a) is falsified on the ring path
   by the design's own §3.5, (b) is not applied to `target_level`, (c) contradicts
   `_effective_camilla_chunksize_setting`'s claim about what the YAML emits under
   `shm_ring`, and (d) puts a 1024-frame chunk into a 256-frame playback ring. The
   geometry question should be re-opened with the clamp treated as the thing under
   examination.

3. **A previously-unnamed second owner of the effective-chunksize fact exists**:
   `jasper/audio_runtime_plan.py:2754-2786`. The census's consumer inventory should carry
   it, because Phase 1 breaks its docstring.

4. **`ring_confirm_strike_write_failed` (EG-5) is confirmed to have zero occurrences
   repo-wide besides its own source line** — `git grep` returns exactly one hit. Phase 2
   inherits it named, as the design says.

5. **The fleet may not contain an active-follower-capable box** (S8). If jts4 is passive,
   Phase 1 can validate the leader endpoint and the snapclient-vs-ioplug seam, but the
   active-**follower** ingress — the config `grouping_follower.yml`, the path #2481 names
   — closes on code review plus jts.local's crossover instance, not on a hardware pass of
   that role. That belongs in the evidence file as a stated re-scope alongside the
   acoustic p99, not discovered at close-out.

---

## What is right, and should not be relitigated in the fix round

Stated so the fix round does not churn settled ground: §3.1 (own conf.d file, and NOT
joining `RING_CONF_PCMS` — the `render_ring_conf_wire` ValueError at
`ring_assets.py:930-936` is real, and `RING_PCM_DEVICES` membership is what selects the
ring device profile, so adding a capture PCM there would be a genuine defect); §3.3
(S16_LE, and the reasoning that S32_LE would either fail single-valued negotiation or
force a `plug` wrapper — `jts_ring_set_hw_constraints` confirms every dimension but
access is single-valued); §3.4's no-`rm -f` decision and its stated `-EINVAL` residual;
§4's deletion of the two import-time env overrides (confirmed unwritten anywhere in the
tree); §5's *shape* (one predicate, two consumers, derived from the function that writes
the lane — see C2); §6.1's PR-2-before-PR-3 ordering (see C1); §8.1's probe-not-pin
decision; §5.3's scope discipline — I verified all seven named #2672 pins exist and
assert what the design says they assert (`tests/test_fanin_coupling_reconcile.py:508`
is parametrized flat/roleful × confirm/arm; `:630` is the websocket positive control;
`tests/test_ring_gates_recovery.py:1439/:1468/:1488` are the ladder wiring), the
statefile refusal is genuinely ahead of all three acceptance branches inside
`_reconcile_camilla`, and none of it is reachable from the proposed diffs. The strike
ladder is untouched. No Phase-2 scope leaked in.

---

*Review produced 2026-08-18 against `6e569e8dc8e572a8d648d332c414374b8394496e`. Fix
rounds return to this reviewer for delta re-review.*

---

# DELTA RE-REVIEW — v2, 2026-08-18

Same reviewer, same pinned SHA (`git -C …/loopback-retirement-ring-3a4b6e rev-parse HEAD`
→ `6e569e8dc8e572a8d648d332c414374b8394496e`, re-verified). Delta pass over the v2
in-place revision and its 17-row changelog. Read-only; no repo file edited.

## Delta verdict

| | Round 1 | v2 |
|---|---|---|
| BLOCKER | 2 | **0** — both resolved |
| SHOULD-FIX | 9 | **1** — all nine folded; one *new*, introduced by v2 |
| NIT | 6 | 4 — all six folded; four new |

**Not sealed, by one line.** The single remaining should-fix is a wrong exception named
in a mutation-kill row; it is a one-sentence correction and needs inspection, not a
re-derivation round.

## B1 — RESOLVED

Re-derived the three things the resolution rests on:

1. **Does the resolver return what the bake needs?** `coupling_capture_kwargs_from_env`
   exists at HEAD (`jasper/fanin_coupling.py:885-927`) and returns **eight** keys under
   `shm_ring`, including `playback_device: RING_PLAYBACK_DEVICE` and `playback_format`
   (pinned by `tests/test_fanin_coupling.py:164-179`). Splatting that whole dict into the
   bake would redirect camilla#1 from the snapfifo to **Ring B** — the exact
   strand-the-leader failure D2's original detail warns about. **v2 states this itself**
   (§5.2(a): *"the resolver returns the full end-to-end kwargs … So the bake takes the
   capture half only — `capture_device` and `capture_format`, the two kwargs its signature
   already accepts"*), and those are indeed the only two of the eight that
   `emit_active_speaker_program_bake_config` accepts (`camilla_yaml.py:3796-3809`). The
   filter is correct and correctly scoped.
2. **Is `{}` byte-identical under loopback?** Yes. `capture_kwargs_for_coupling` returns
   `{}` for absent / `""` / `loopback` / `fifo`, pinned at
   `tests/test_fanin_coupling.py:331-343`; `**{}` leaves `capture_device` /
   `capture_format` on their `DEFAULT_*` bindings, i.e. today's bytes.
3. **Is the atomicity argument sound?** Yes, and it is the right call. The gate is what
   makes the coupling-blind bake reachable — before PR-5 a bonded active leader on a
   `shm_ring` box cannot exist (D5 refuses at `active_leader_config.py:211-219`). Shipping
   the bake fix alone is inert; shipping the gate alone is B1. Bundling both in PR-5 as
   safety-class with a 3-lens panel is the only ordering that never exposes the hazard.

S6 is added with a concrete failure bar (*"silence with healthy daemons — the exact shape
B1 exists to prevent"*). The kwarg-forwarding decline is recorded rather than skipped.
**B1 closed.**

## B2 — RESOLVED, and the load-bearing deletion claim holds

The claim I was asked to attack hardest: *the clamp's only use site is reached only by
`driver_domain=True`, whose only two production callers both move to the ring this phase.*
Re-derived over `jasper/` (not tests):

```
git grep -n FOLLOWER_LOOPBACK_MIN_CHUNKSIZE -- jasper/
  camilla_yaml.py:190   (definition)
  camilla_yaml.py:3657  (its comment)
  camilla_yaml.py:3660-3661  (the one use site)

git grep -rn "driver_domain=True" -- jasper/     → active_leader_config.py:264
                                                   follower_config.py:215
                                                   (all other hits are docstrings/comments)

git grep -rn emit_active_speaker_driver_domain_config -- jasper/
  → exactly ONE call site: baseline_profile.py:2388
```

Both callers pass `chunksize=devices.chunksize`; on any active-speaker box the playback
resolves to `RING_ACTIVE_PLAYBACK_DEVICE` unconditionally
(`output_topology.py:1895-1926`), so `active_emit_devices` takes the ring branch and
`devices.chunksize` is `RING_CAMILLA_CHUNKSIZE` = 128. With both callers on the ring, the
condition `128 < 1024` would fire on every production emit — so the clamp must go, not be
renamed. **The deletion is correct and the reachability argument is sound.** The three
direct-emitter tests at `test_active_speaker_driver_domain.py:228-248` are the only other
consumers, and v2 folds them (PC-7).

New geometry re-derived independently, all correct:

| | v2 claims | my derivation |
|---|---|---|
| slot | 2.667 ms | 128 / 48000 = 2.6667 ms ✓ |
| depth | 2048 frames = 42.67 ms | 16 × 128 = 2048; /48000 = 42.667 ms ✓ |
| tick | 0.667 ms ≈ 1500 Hz | `tick_ns_for(128)`: 2,666,667/4 = 666,667 ns, inside [0.25, 2] ms ✓ |
| max delay | 2175 frames = 45.3 ms | 16×128 + 127 = 2175; /48000 = 45.31 ms ≪ 400 ms ✓ |
| n_slots | 16 = `JTS_RING_MAX_SLOTS` | range check is `2..=16` inclusive (`pcm_jts_ring.c:1098`) ✓ |
| "same depth v1 proposed" | 2×1024 = 16×128 | both 2048 ✓ |

And the corrected physics fixes the second half of B2 for free: camilla#2 now captures the
grouping ring at chunk 128 (one slot) and plays the ACTIVE ring at chunk 128 into its
2-slot 256-frame buffer — the shipped coherent pairing, and `_effective_camilla_chunksize_setting`
stops being contradicted. **B2 closed.**

*Checked for a regression the flip could have caused:* v2's 128-frame period makes
snapclient's `period_time` clamp 20000 → 2667 µs, which v1 had used as an argument *for*
1024. v2 answers it (§3.2, *"It writes `framesAvail` per iteration, not one period, so its
own wake rate is not pinned to the period"*), and the memo backs both halves
(`RESEARCH-rate-tracking-ring-2026-08-17.md:430` records the expected clamp log verbatim;
`:436` records the `framesAvail` write size). The memo also marks the negotiation outcome
`[I]` unobserved (`:703`) — which is precisely what S0 gates, with PR-1 shipping the
conf.d inert so a failed S0 costs one revert. Managed, not hidden.

## S1–S9, N1–N6 — all folded, verified row by row

Spot-verified in the v2 text, not taken from the changelog: S1 (`:317-322`, 1500 Hz with
`tick_ns_for` cited and the wake-rate comparison dropped) · S2 (`:404`, `StartLimitBurst`
4→6 with the `> 5` rule quoted; `:876` in the change set) · S3 (`:877`, `jts_ring_shm.h:120-151`
added, sixth writer, and T-7 makes the enumeration executable) · S4 (`:556-564` names
`invalid_grouping` and argues determinacy; `:792` T-5 derived, not parametrized) · S5
(`:580-591`, reason-token change stated, justified, pinned) · S6 (`:924` §9 retitled the
change set, non-exhaustive, PR-6 owns completeness; `member_config.py`,
`graph_carrier.py:414`, `HANDOFF-dsp-graph-carrier.md`, the three `hw:Loopback,*,6` docs
and the §9 *additions* section all present) · S7 (`:283-291` bounded-coarseness form;
`:339-344` S2 now records the series as snapclient samples it **and** the occupancy
distribution) · S8 (`:1031-1050`, evidence scope stated before the pass) · S9 (`:837-839`,
three modules with per-PR attribution) · N1 (`:222`, `:776`) · N2 · N3 · N4 (`:1080`) ·
N5 (`:372-373`, and EG-7) · N6 (`:334-337`, "proven" withdrawn, INERT-UNTIL-ARMED quoted).

## The five disagreements — re-measured, not defended

1. **Third breaking test module — SPLIT (author right on the count).** `git grep -n
   FOLLOWER_LOOPBACK_MIN_CHUNKSIZE -- tests/` returns `test_active_speaker_driver_domain.py`
   at `:228`, `:233`, `:238`, `:245`, `:248`, exactly as claimed. My S9 conclusion (the test
   impact set was incomplete) **CONFIRMED and strengthened**; my enumeration "two modules"
   **WITHDRAWN** — it is three, and the third is the one my own B2 breaks. I grepped tests
   for the deleted *device* constants and the renamed predicate but not for the constant my
   own blocker asked to delete.
2. **"A second consumer on Ring A" — SPLIT.** The parenthetical is **WITHDRAWN**: the ring
   enforces single-reader (`jts_ring_reader_open` → `-EBUSY`, *"the SPSC guard firing"*,
   `pcm_jts_ring.c:644-650`), camilla#1 is Ring A's only reader on a roleful box, and
   camilla#2 reads the *grouping* ring — no contention, no second consumer. The other half
   of the same sentence — camilla#1's pacing changes — **CONFIRMED**, and S6 is the right
   response.
3. **"Quantized to whole slots" — SPLIT.** The identity is **WITHDRAWN**: the memo records
   snapclient writing `framesAvail`, not one period (`RESEARCH-…:436`, citing
   `alsa_player.cpp:641-648`), so `stage_frames` is generally non-zero and the signal is
   frame-granular. My conclusion — the slot size is what the sync loop feels, so 1024 was
   wrong — **CONFIRMED**, and v2's bounded form (`occupancy_slots × period` is the coarse
   component) states it more accurately than I did.
4. **Thirteen vs nineteen sites — WITHDRAWN.** Recount:
   `git grep -c "round-trip snd-aloop\|round-trip loopback\|the round-trip" --
   docs/HANDOFF-distributed-active.md` → **19**, and the six the author adds
   (`:903, :1163, :1182, :1189, :1257, :1288`) are exactly my delta. My thirteen came from a
   `head -40` on a multi-file grep — a truncated list presented as an enumeration, which is
   the same failure the finding was about. The author's lesson is the right one: no line
   list is a bound, which is why §9 is now non-exhaustive and PR-6's sweep owns completeness.
5. **Four vs five enumeration entries — WITHDRAWN.** `grep -nE "^//   - "
   c/jts-ring-ioplug/jts_ring_shm.h` → **five**: librespot `:120`, **bluealsa-aplay `:121`**,
   jasper-camilla `:126`, shairport-sync `:128`, correction lane `:135`. My S3 prose omitted
   bluealsa-aplay (though the block I quoted contained it). snapclient is the **sixth**
   writer. The author's point lands: bluealsa-aplay is the writer whose omission the guard's
   own docstring gives as the reason the enumeration exists.

Net: two of my sub-claims were wrong (D4, D5 counts), two were overstated (D2's
parenthetical, D3's identity), one was incomplete (D1). **No finding is overturned; three
are strengthened.**

## NEW findings introduced by v2

### SHOULD-FIX — ND-A: T-8's second mutation row names the wrong kill mechanism for the path under test

§7.2's table:

> | **bake sink stays a File pipe (T-8)** | pass the resolver's full dict → `emit_sound_config` must raise |

and §5.2(a) builds the same evidence chain. The `emit_sound_config` guard is real —
`jasper/sound/camilla_yaml.py:270-277` raises `ValueError` on `playback_format` alongside
`playback_pipe_path` — **but that emitter is not on the path this PR changes.** The
active-leader bake is `emit_active_speaker_program_bake_config`, whose signature
(`camilla_yaml.py:3796-3809`) has **no** `playback_device`, `playback_format`, `queuelimit`
or `enable_rate_adjust` parameter at all, so splatting the resolver's full dict there is a
`TypeError`, not that `ValueError`. (`emit_sound_config` is the pipe-sink emitter for the
*passive* leader, which the narrowed gate still blocks.)

Both failures are loud, so the design's conclusion — take the capture half only — is
unaffected. The problem is the mutation row: an implementer following it literally can
satisfy it by routing the mutation through `emit_sound_config` and record a green kill that
proves nothing about the bake. Given this repo's recorded trap that mutation harnesses fail
silently in both directions, a kill row must name the mechanism on the path it guards.

**Pass:** state that the bake emitter accepts no playback kwargs, so the kill is a
`TypeError` raised at the bake call site, and keep the `emit_sound_config` `ValueError` as
the *sibling evidence* that playback kwargs are wrong for a pipe sink rather than as the
guard on this path.

### NITs

- **ND-1** — §5.2(a): *"The leader's program bake becomes its fourth caller."* At HEAD the
  resolver already has five production call sites (`audio_runtime_plan.py`,
  `correction/session.py`, `web/correction_setup.py` ×2, `web/sound_setup.py`); the bake is
  the sixth site (fourth *class* of caller). Cosmetic, but it is a count, and counts have
  been this review's recurring failure mode on both sides.
- **ND-2** — §5.2(a): *"a 1024-frame chunk reading Ring A's 128-frame slots."* The bake
  leaves `chunksize=None`, and `resolve_camilla_chunksize` resolves
  `JASPER_CAMILLA_CHUNKSIZE` → **the active DAC profile's codified floor** →
  `DEFAULT_CHUNKSIZE` (1024) (`camilla_config_contract.py:229-237`, `:105`). jts.local is an
  Apple-dongle box whose declared floor **is 128** (`60-jts-ring.conf`: *"an Apple box, whose
  floor IS 128"*), so on the box under test camilla#1 would read Ring A one slot at a time —
  coherent, not the 8× gulp the sentence describes. 1024 is the worst case across the fleet,
  not the value here. Saying so makes the decline stronger, not weaker; as written it
  overstates the residual it is accepting.
- **ND-3** — §3.2 takes `n_slots = 16`, which is `JTS_RING_MAX_SLOTS` exactly. Legal (the
  range check is inclusive), but it leaves **zero headroom**: raising it later means moving
  a constant that `tests/test_ring_slot_ceiling_pin.py` holds equal across the C header,
  `rust/jasper-ring/src/layout.rs` and `rust/jasper-outputd/src/config.rs`. The only
  remaining depth axis is `period_frames`, which is also the sync-coarseness knob — so depth
  and coarseness are no longer independently tunable in the up direction. One sentence.
- **ND-4** — internal inconsistency created by the revision: §14 item 4 corrects the
  `HANDOFF-distributed-active.md` count from thirteen to **nineteen**, but PR-6's row
  (`:924`) still reads *"`HANDOFF-distributed-active.md`'s **thirteen** sites"*. Harmless
  because §9 is now explicitly non-exhaustive and the sweep owns completeness — but it is the
  author's own corrected number contradicted three sections later.

## Settled, and not re-opened

Round 1's settled list stands unchanged. v2's own "unchanged and not relitigated" block
matches it. Owner rulings (§13: jts.local = leader, baseline applied at step 0.2, dummy
loads stay, #2481 closes narrow) are taken as settled context and were not reviewed.

**Recommendation:** fix ND-A (one sentence) and optionally ND-1..ND-4 (one sentence each),
then seal. No further re-derivation is owed — ND-A is verifiable by inspection.

---

*Delta re-review 2026-08-18 against `6e569e8dc8e572a8d648d332c414374b8394496e`. Read-only.*

---

# CONFIRM PASS — v2.1, 2026-08-18

Same reviewer. Pinned SHA re-verified: `6e569e8dc8e572a8d648d332c414374b8394496e`.
Scope, as set by the conductor: the five changelog rows D-1..D-5, the T-8a/T-8b rewrite,
and the ND-2 number dispute. Nothing else re-opened. Read-only.

## Verdict: **SEALED — 0 blockers / 0 should-fixes / 0 open nits**

| | R1 | v2 | v2.1 |
|---|---|---|---|
| BLOCKER | 2 | 0 | **0** |
| SHOULD-FIX | 9 | 1 | **0** |
| NIT | 6 | 4 | **0** |

## ND-A (the one should-fix) — CLOSED

`emit_active_speaker_program_bake_config`'s signature, read directly at
`jasper/active_speaker/camilla_yaml.py:3796-3809`, declares exactly:
`room_peqs`, `output_trim_db`, `capture_device`, `capture_format`, `sample_rate`,
`chunksize`, `target_level`, `volume_limit_db`, `out_path`, `profile_id`. A grep of that
range for `playback_device|playback_format|queuelimit|enable_rate_adjust` returns **0**.
**T-8b's central claim is correct**: splatting the resolver's eight-key dict into the bake
raises `TypeError` at the call site, and that is the bake's own loudness.

The rewrite is what was asked for and slightly more: T-8a (`:791`) and T-8b (`:792`) each
name their emitter and their call site, and `:794` carries the explicit
*"Do not satisfy T-8b through `emit_sound_config`"* note with the reason
(`emit_sound_config` is the **passive**-leader pipe emitter, which the narrowed gate still
blocks — so a kill routed through it proves nothing about the bake). The recorded-trap
citation is present. ND-A closed.

## ND-2 — the author is right; I was wrong, and I say so plainly

I re-measured my own 128 at both sites the conductor named.

- `resolve_camilla_chunksize(profile_floor=_UNSET)` resolves
  `_active_camilla_floor("camilla_chunksize")`; on the unset-env path `raw` is empty and
  `_resolve_camilla_int` returns `fallback = default if profile_floor is None else
  profile_floor` (`jasper/camilla_config_contract.py:212-214`) — i.e. **the profile floor**,
  not `DEFAULT_CHUNKSIZE`.
- The Apple profile's floor (`jasper/audio_hardware/dac.py:385-390`) is:

```python
    latency_floor=LatencyFloor(
        camilla_chunksize=256,
        camilla_target_level=1536,
        outputd_period_frames=128,
        outputd_dac_buffer_frames=256,
    ),
```

  above the measurement comment *"Measured stable floor on Apple-dongle lab boxes:
  CamillaDSP chunk 256 / target 1536, outputd period 128 / dac_buffer 256."*

**So the bake's unset-env chunk on jts.local is 256, not 128.** My 128 was
`outputd_period_frames` — a *different field of the same dataclass* — which I reached from
the `60-jts-ring.conf` comment *"an Apple box, whose floor IS 128"*, a sentence about the
ring slot / outputd period, not about the CamillaDSP chunk. The author's diagnosis of my
error is exactly right.

**Adjudication: ND-2's direction stands, its value is withdrawn and corrected.** The
finding — v2's flat "a 1024-frame chunk" is the fleet worst case, not the value on the box
under test — is confirmed. The corrected number is **256 = two Ring-A slots per chunk** on
jts.local: the residual is smaller than v2 stated (two slots per read, not eight), but it
is *not* the one-slot coherence I claimed. v2.1 already carries the corrected number and
the corrected citation (D-3, and §14 item 6).

That is three of my sub-claims corrected by the author across two rounds (the 13-vs-19
count, the four-vs-five enumeration, and now this). Each correction strengthened the
finding it touched; none overturned one. Recording it here because the standing rule cuts
both ways: a reviewer's number is a claim, and re-measuring it is the reviewer's job, not
the author's.

## D-1..D-5 — verified against the v2.1 text

| Row | Verified |
|---|---|
| **D-1** | T-8a `:791` / T-8b `:792`, each naming its emitter and call site; the do-not-satisfy note at `:794`. `TypeError` is the named kill; `emit_sound_config`'s `ValueError` demoted to sibling evidence. ✓ |
| **D-2** | Recounted to **five** existing production call sites → the bake is the **sixth site / fourth class**. Matches my own `git grep -c` (audio_runtime_plan 1, correction/session 1, correction_setup 2, sound_setup 1). ✓ |
| **D-3** | Corrected to **256** with `camilla_config_contract.py:212` + `dac.py:385-390`, and the two `LatencyFloor` fields distinguished. ✓ |
| **D-4** | 16 stated as `JTS_RING_MAX_SLOTS` exactly, with the consequence named (depth and sync-coarseness no longer independently tunable upward) and the three-language bump enumerated. ✓ |
| **D-5** | thirteen → **nineteen**, matching §14 item 4. ✓ |

## Seal

Both blockers resolved (v2, re-derived independently). All nine round-1 should-fixes
folded. The one v2-introduced should-fix closed. All six round-1 nits and all four
v2-introduced nits folded — none waived, none carried. Round 1's settled list and the
owner's §13 rulings were not re-opened.

**The design is sealed for implementation at 0 blockers / 0 should-fixes.** The gates that
remain are the per-PR adversarial reviews (PR-1/PR-3/PR-5 safety-class, 3-lens) and the
hardware pass — not this document.

---

*Confirm pass 2026-08-18 against `6e569e8dc8e572a8d648d332c414374b8394496e`. Read-only: no
repo file was edited, no build, test, deploy, or ssh was run in any of the three rounds.*
