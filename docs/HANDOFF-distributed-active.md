# Handoff: distributed active crossover (active speaker across a wireless pair)

> **Status: design-of-record (ratified 2026-06-20; active-leader realization
> ratified 2026-06-21; hardware-gated slices pending).** Architecture, engine,
> slice plan, v1 scope, AND the Stage-B active-leader clock realization are
> ratified — **v1 gates on the matched-pair leader**. Stage A (the active
> *follower*, Slice 3) has **passed** on `jts3`; the active *leader* (Slice 5)
> is ratified-not-built; the matched pair (Stage C) is hardware-blocked (needs a
> 2nd commissioned active speaker). See "On-device status (2026-06-21)". This is
> the canonical design + slice plan for running an active
> speaker's **driver-domain crossover (Layer A)** across a wireless pair: a
> **follower** (and, in v1, the leader's own drivers) runs Layer A locally,
> while the **leader** owns the program domain (Layer B room correction +
> Layer C preference EQ) and streams the corrected stereo program. It builds
> on the merged
> graph-carrier track (PR-1→3). Companion docs — read for the layers this
> composes, do not restate them here:
> [HANDOFF-dsp-graph-carrier.md](HANDOFF-dsp-graph-carrier.md) (the
> program/driver split-mixer seam + the carrier dispatcher),
> [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md) (Layer
> A/B/C + commissioning), [HANDOFF-multiroom.md](HANDOFF-multiroom.md)
> (Snapcast transport, leader/follower role-state, inv-A/inv-B). This doc
> OWNS the distributed-active boundary; the graph-carrier doc's "Deferred
> — distributed active" section points here.

## Why this exists

Two JTS capabilities cannot be combined today, and the combination is
**hardware-safety-critical**:

- A **wireless stereo pair** (leader bakes + streams; follower receives +
  plays — [HANDOFF-multiroom.md](HANDOFF-multiroom.md) §2 canonical flow).
- An **active multi-driver speaker** whose CamillaDSP splits the program
  across woofer/mid/tweeter and **band-limits each driver** (Layer A —
  [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md)). Sending
  a full-range feed to a tweeter can destroy it.

**Product requirement (why the active leader is v1, not flagship-only —
confirmed by the owner 2026-06-21).** The household runs **multiple
active-crossover speakers** and needs **full any-role flexibility**: any
speaker, active or passive, must work as either **leader or follower**. So an
*active speaker leading a pair* is a v1 requirement, not a "someday" feature —
this was pressure-tested for over-engineering on 2026-06-21 and the active
leader was confirmed *earned*, not gold-plating. The only thing genuinely
hardware-gated is **validating** the two-active *matched pair* (Stage C, which
needs a second commissioned active speaker); the active-leader mechanism
(Stage B) does **not** block on that — it ships and validates against an
active-leader + passive-follower rig (the buildable target). Build for that
rig; validate the matched pair when the hardware exists.

Today an active-crossover speaker **refuses to bond**, fail-closed:
`apply_bonded_leader_config` routes the loaded config through the graph
carrier and an active (roleful) graph is rejected
(`CarrierCannotHostEq("eq_on_active_bonded_member")`,
[graph_carrier.py](../jasper/sound/graph_carrier.py)); the follower's
`jasper-outputd` round-trip lane refuses a non-`SingleAlsa`
(active/composite) sink (`dac_content_lane_rejects_non_single_alsa_sink`).
Those fences are **correct** — a bonded follower has no Layer-A path
today (its CamillaDSP is parked out of the bonded audio path), so the
only safe behavior is to not bond. This increment builds the real
capability the fences stand in for.

## The seam — relocate the split mixer, don't re-architect the chain

Per [HANDOFF-dsp-graph-carrier.md](HANDOFF-dsp-graph-carrier.md), a JTS
speaker is one signal chain across **two channel domains** separated by
the **split mixer**:

- **Program domain** (1–2 ch): Layer B (room PEQ, L≠R) + Layer C
  (preference EQ) + headroom trim. Rides the stereo bus once.
- **Driver domain** (N ch): Layer A — the `2→N` split + per-driver
  crossover / delay / gain / limiter + the tweeter band-limiting
  high-pass + the `0 dB` ceiling.

In a **solo** box all layers run in one CamillaDSP graph. In a **pair**
the leader owns the program domain and streams the corrected **2-channel**
program over Snapcast; the follower owns the driver domain locally. **Only
2 channels ever cross the wire**; the N driver channels never leave the
box that owns the DACs. The split mixer is the relocatable seam — we move
*where Layer A runs*, not the chain shape. This is the multiroom doc's
already-decided "two-kinds split" (content DSP = leader-side, baked into
the stream; driver DSP = on the box driving the DAC —
[HANDOFF-multiroom.md](HANDOFF-multiroom.md) §7.5).

## The engine decision — CamillaDSP re-entry (ratified 2026-06-20)

The streamed program reaches a follower at outputd's `dac_content` lane,
post-CamillaDSP, where the only transform is `ChannelPick`
(Stereo/Left/Right/Mono — duplication or a clip-safe −6 dB average, **no
filtering** — [dac_content.rs](../rust/jasper-outputd/src/dac_content.rs)).
Running Layer A there needs one of two engines:

| Dimension | A — split inside outputd (Rust) | **B — CamillaDSP re-entry** ✅ |
|---|---|---|
| New code | 100% greenfield safety-critical DSP in a reboot-on-fail daemon (biquads, LR crossovers, limiters, delays, the `2→N` split) **plus** relaxing the dac_content lane's hard 2-in/2-out `SingleAlsa` constraint to N-channel | Reuses the **shipped** Layer-A emitter + `driver_protection` + the `0 dB` ceiling. New code ≈ a capture param + a driver-domain-only emit variant + reconciler wiring (Python, mostly parameterization) |
| Re-proof | No verifier exists for a Rust graph; would re-implement the safety contract | `classify_camilla_graph` re-proves the emitted config **verbatim** — transfers with one new classification arm |
| Follower RAM/CPU | +Rust DSP **+** N-channel lane rework | **~Neutral**: the follower already runs CamillaDSP (today the inv-B fallback lane); Option B repurposes that same instance — no new process |
| Clock domain | outputd DAC clock + snapclient stuffing (2 domains) | 3 non-overlapping domains (multiroom decision 3): snapclient stuffs → loopback; camilla rate-tracks the loopback capture (bit-perfect, no resampler); outputd DAC paces. snapclient `--latency` nulls camilla's fixed latency |
| Limiter / HP owner | Re-implement in Rust | The shipped CamillaDSP per-driver limiter + crossover-HP + `0 dB` ceiling, unchanged |
| Fail-closed on stall | Build in Rust | Graph-resident protection (see below) |
| "Swap the engine, not the topology" | Violates | Honors |

**Decision: Option B**, matching the multiroom doc's pre-decided
follower path: `snapclient → loopback → camilla [crossover/protection
only] → outputd active sink`, with snapcast's per-client `--latency`
compensating camilla's fixed latency
([HANDOFF-multiroom.md](HANDOFF-multiroom.md) §7.5). The one genuine cost
is camilla's fixed latency in the synced path (compensated) and the
on-device rate-domain tuning — honor the `rate_adjust`+`AsyncSinc`
oscillation trap (never both when capture rate == playback rate).

### Endpoint crossover mode — one capability, three uses

Option B is a **single reusable capability**, not three: an **endpoint
crossover** CamillaDSP that runs *this box's* Layer A on whatever stereo
program it is handed. The same config shape serves three roles, so the
"active leader" and "wireless sub" below are **not separate engines** —
they are the follower capability applied on another box. The differences
are **operational (RAM, voice, sub sync), not architectural**:

- **Active follower** — pick L/R, split across its drivers.
- **Active leader's own drivers** — the leader is *brains + an endpoint*:
  one CamillaDSP bakes B/C → the wire; a **second** instance (this same
  endpoint config) runs the leader's own crossover on the round-tripped
  stream. Two instances exist only because **one CamillaDSP drives one
  sink**, and the leader must feed both the wire (2 ch) and its own DACs
  (N ch). See gap 3.
- **Wireless sub** — an endpoint whose topology is *one driver with a
  low-pass*; it applies the sub crossover **locally**, the leader specifies
  the corner, and everyone still receives the **one** shared stereo stream.
  See gap 5.

## Roles & capture contract (gap 1)

Bond role is **runtime** (`grouping.env`), exactly as
`member_camilla_kwargs` ([member_config.py](../jasper/multiroom/member_config.py))
already resolves the leader's pipe sink per role. So the **commissioned
artifact stays the driver-domain description** (crossover points,
per-driver gain/delay/limiter, tweeter HP — hardware truth, role
independent); the **reconciler resolves capture device + domain-mode per
current role**:

| Role | Capture | Layers emitted |
|---|---|---|
| Solo | `plug:jasper_capture` (fan-in) | B/C + A in one graph (today's `recompose_baseline_yaml` + baseline) |
| Follower | round-trip loopback (snapclient-fed) | **A only** (+ channel-select prefix; no B/C — leader baked them) |
| Leader (active) | camilla#1: fan-in → B/C → pipe; camilla#2: round-trip loopback → A | split across two instances (gap 3) |

Two enabling facts make this cheap:

- The active baseline emitter **already takes `capture_device`**
  (`emit_active_speaker_baseline_config`,
  [camilla_yaml.py](../jasper/active_speaker/camilla_yaml.py)); the
  compiler `build_baseline_profile_candidate`
  ([baseline_profile.py](../jasper/active_speaker/baseline_profile.py))
  **now threads it** (Slice 1, landed) — as does `apply_baseline_profile`, so
  the compile/apply seam takes a capture device. (`recompose_baseline_yaml`,
  the program-domain Layer-C EQ re-emit, deliberately does **not** — it only
  runs on the fan-in-fed program domain, so it always uses the default
  capture; see its docstring.) Default (`plug:jasper_capture`) unchanged,
  golden byte-identical.
- The playback device is already role/topology-resolved
  (`resolve_active_playback_device`).

Slice 1 also adds a **pure-data pairing-intent field** to `OutputTopology`
([output_topology.py](../jasper/output_topology.py)) — `solo |
will_be_follower | has_follower` — that records design intent and seeds
reconciler defaults. No behavior yet; it answers "is there a wireless
speaker?" at commission time so later slices can read it. The reconciler
keeps the final runtime say (mirrors `member_camilla_kwargs`).

## The active follower (gap 2) — the core

The reconciler's follower branch:

1. Points the follower's CamillaDSP **capture at the round-trip loopback**
   (snapclient writes it; today snapclient feeds `MEMBER_CONTENT_FIFO` →
   outputd's `dac_content` — [reconcile.py](../jasper/multiroom/reconcile.py)).
2. Emits a **driver-domain-only baseline**: `channel-select (2→2 pick
   L/R/mono) → split_active_<way>way (2→N) → per-driver [crossover, delay,
   gain, limiter] (+ tweeter HP)` — **no** program prefix, **no** EQ
   headroom (the leader baked B/C). Channel-select runs FIRST (inter-
   speaker axis), then the crossover splits (intra-speaker axis) — exactly
   [channel_split.py](../jasper/multiroom/channel_split.py)'s documented
   composition order.
3. **Disables outputd's `dac_content` ChannelPick on this box** — camilla
   now owns both the channel-pick and the split. This replaces the
   `dac_content_lane_rejects_non_single_alsa_sink` fence with the real
   capability (keeping an equivalent fail-closed: if the driver-only graph
   can't be re-proven, refuse to bond / silence + cue).

The driver-domain-only emit is a **parameterization of the existing
emitter** (compose, don't text-splice — the PR-3 `recompose_baseline_yaml`
pattern), and `classify_camilla_graph`
([runtime_contract.py](../jasper/active_speaker/runtime_contract.py)) grows
a **driver-domain-only baseline** classification arm: Layer A present
(crossover HP + per-driver limiter `clip_limit≤0` + per-driver gain `≤0` +
`0 dB volume_limit`), channel-select present, program prefix absent. The
emitter↔verifier independence stays; the carrier emits, the classifier
re-proves.

## Web: narrow the follower-409, make the promise true (gap 2)

Two facts, both verified:

- The POST block `_FOLLOWER_BLOCKED_CONTENT_DSP_POSTS`
  ([sound_setup.py](../jasper/web/sound_setup.py)) is **already narrow** —
  content-DSP only (`/apply`, `/audition`, `/live-draft`, `/settings`,
  `/profiles/*`). The active-speaker crossover endpoints are not in it.
- But `_index_html` returns `_follower_sound_html` for the **entire**
  `/sound/` page when `bonded_follower_active()`, so the local crossover /
  commissioning UI is hidden on a follower. The delegation card promises
  "Local crossover and driver-protection work stays with the speaker that
  owns the DAC path" — a promise no code keeps, because (a) the UI is
  hidden and (b) the bonded audio bypasses where that crossover lives.

**Slice 4 (this increment) ships the web half** — the HW-free surface; the
runtime audio path that actually relocates Layer A is Slice 3. `_index_html`
still renders the delegation card on a follower, but the follower page now also
mounts the **same** active-speaker setup UI `main.js` renders on a solo box: the
shell emits a `sound-follower-data` island, and `main.js` boots in *follower
mode* — it renders only the local driver/crossover/commissioning surface
(expanded as the primary content) and omits the Off/Saved/Draft content-EQ
editor + now-playing plot, which stay the leader's job. The active-speaker
commissioning/crossover endpoints are allowed (they were never in the block
set); content-DSP POSTs still 409. So the delegation card's "local crossover and
driver-protection work stays with the speaker that owns the DAC path" is now
literally true **at the UI** (Slice 4); combined with the follower audio path
above (Slice 3) it is true end-to-end. Invariant 6 is pinned by
[test_sound_setup.py](../tests/test_sound_setup.py) (content POST → 409,
active-speaker read → 200, active-speaker POST → reaches its handler, block-set
disjoint from `/active-speaker/*`) and the follower-render path by the
sound-profile JS harness
([sound_profile_harness.mjs](../tests/js/sound_profile_harness.mjs)).

**Cross-slice contract — Slice 4 surfaces the controls; Slice 3 owns the
runtime fail-closed.** Because the active-speaker endpoints are (correctly,
per invariant 6) reachable on a follower, Slice 4 makes the commissioning /
baseline-apply controls *discoverable* on a follower before Slice 3 wires the
follower audio path. The actions are graph-protected today (commission-load arms
"the protected floor (silent)" through the per-driver crossover/limiter graph
with the `0 dB` ceiling — [sound_setup.py](../jasper/web/sound_setup.py)
`_active_speaker_commission_load_payload`) and fail to a surfaced status, not a
silent no-op. But the **bonded-follower audio topology** (does the loaded config
reach the DACs? does a commission tone interfere with the bonded stream? what
happens across bond/unbond?) is **Slice 3's responsibility**, and Slice 3 MUST
land an on-device contract proving the follower commission/apply path is
**fail-closed** (no full-range to a tweeter, no interference with the bonded
program) before the matched-pair gate (Slice 5). This is the active-crossover
analogue of the "Clock domain + fail-closed" invariants below — owned there, not
at the web layer.

## The active leader (gap 3) — brains + an endpoint (not a harder design)

A matched pair of two active speakers is **not** a different design: the
leader is **brains + an endpoint** — it runs the *same* endpoint-crossover
config a follower runs, on its own drivers, plus the bake. The leader plays
its own channel via its **own localhost snapclient**
([HANDOFF-multiroom.md](HANDOFF-multiroom.md) §2), so its driver domain is
post-round-trip too — the leader is its own receiver.

**Why two CamillaDSP (the clean version).** Every member, leader included,
plays the round-trip, so its DACs are fed by its own *receiver* side.
Compare a passive vs an active leader's receiver job:

| | Sender job | Receiver job (own DACs) | CamillaDSP |
|---|---|---|---|
| Passive leader | bake → wire *(camilla)* | channel-pick — **outputd, no DSP** | **1** |
| Active leader | bake → wire *(camilla)* | **crossover** — outputd has no DSP, so camilla | **2** |

A passive leader's receiver job is dumb (outputd channel-pick), so one
instance suffices. An active leader's receiver job is the **crossover**,
and outputd has no DSP — so it runs in a second CamillaDSP, *literally the
follower's endpoint config*.

**Why "more channels in one instance" can't merge them — it's time, not
channel count.** Within one CamillaDSP you *can* open many channels and
chain stages, so the obstacle is not the N-channel shape. It is that the
leader's two outputs sit at **different points in the sync timeline**: the
wire feed is the **pre-stream source** (produced *before* snapserver), while
the DAC feed must play the **round-tripped, network-buffered** stream to
stay phase-locked with the follower (the leader plays its own localhost
snapclient precisely to inherit the *same dynamic buffer* the follower has).
One pipeline pass emits at **one** time point, so the crossover has to sit
*after* the round-trip — downstream of snapserver→snapclient — which is a
separate process from the pre-stream bake. **A lighter "just add a sync
delay" doesn't exist, because sync is not a delay:** it is continuous
**clock-drift correction** between two independent DAC oscillators (a
control loop that stuffs/resamples — two DACs slide ~1 ms/min apart at
typical ppm, so a stereo pair comb-filters within minutes without it). A
fixed/queried `Delay` is a scalar and can't track that; the localhost
snapclient round-trip **is** the lightest correct way to get it (it reuses
snapcast's proven sync engine, which the leader already runs even when
passive). And drift is also why "compute the crossover in one wider
instance and split downstream" fails: the N driver channels would land on
the **un-corrected** side, and snapcast only drift-corrects the *stereo*
stream — there is no N-channel snapclient — so the crossover must follow the
corrected stereo. The sync mechanism and the instance count are **orthogonal**:
sync is settled (the round-trip); the second instance is purely "the
crossover runs after the corrected stream." This is exactly why **solo**
needs one instance: no follower → no wire output and no clock to match → the
crossover stays in the single low-latency graph. The added crossover latency is its chunk buffer (~a few–
20 ms, tunable), is **fixed and nulled by snapcast's per-client `--latency`**
(never desyncs the pair), and is the *same* latency a solo active speaker
already carries.

The only way to collapse it to one is to put the crossover in outputd
(Option A / Rust) — rejected, because it discards the proven crossover
engine + the `classify_camilla_graph` re-proof. So two **light** CamillaDSP
(the second is biquad crossovers + limiters, no room FIR — a few-to-low-tens
of MB, low-single-digit % of a core, **measure on `jts3`**) is the price of
a verifiable crossover; it exists only on a box that is *both* leader *and*
active.

Two **operational** costs distinguish it from a follower — neither is a
blocker:

- **RAM** — two CamillaDSP on a 1 GB Pi. The second is a light *driver-DSP*
  instance (biquad crossovers + limiters, no room-correction FIR), not a
  second content-DSP, but still +RAM — **measure on `jts3` before shipping**.
- **Voice/TTS — the genuine extra.** A follower receives no voice (parked);
  a leader does, and the multiroom **inv-A** design mixes leader TTS late
  at `jasper-outputd`, **after** the round-trip — i.e. after the crossover
  instance. On an *active* leader that bypasses the per-driver crossover, so
  TTS must be **routed through Layer A or band-limited at the mix point**
  (full-range speech into a tweeter otherwise). Tractable; the follower is
  immune because it is voice-parked.

So design-wise it composes from already-built pieces; it just **validates a
beat behind the follower** (RAM + TTS band-limiting). Its **inv-B fallback**
(direct fan-in when the self-loop stalls,
[HANDOFF-multiroom.md](HANDOFF-multiroom.md) inv-B) **must also route
through Layer A**, or the fallback leaks full-range to the leader's own
drivers. **v1 gates on the leader** (owner decision, 2026-06-20) — a pair of
two identical active speakers is the flagship case, so v1 is not "done" until
the matched pair works, including the `jts3` RAM measurement and the TTS
band-limiting decision.

### Stage B — the ratified active-leader realization (2026-06-21)

Gap 3 establishes *why* an active leader runs two CamillaDSP. This is the
ratified *how* — the clock realization, the one constraint that must survive
future "consolidation," the pair budget, and the build decision that stays open
until the `jts3` measurement. (Decisions confirmed 2026-06-21: build the leader
mixer; split the on-device gates music-first then TTS; reuse `emit_sound_config`
for the camilla#1 bake plus a pipe-sink verifier exemption.)

**One hard clock crossing, one rate loop.** Count *crossings*, not stages.
`snapserver → DAC` is the only hard crossing (two real crystals, absorbed
continuously). The leader's own TTS/cue is a **soft input** — no independent
crystal producing it at a fixed wrong rate, just buffered and consumed at the
DAC's pace — so it is *not* a crossing and needs *no* loop. The combined stream
therefore has **exactly one** rate loop. Two configurations follow from this:

- **Music-only (no leader TTS):** camilla#2 *is* the loop — it reads the
  snapclient round-trip loopback with `enable_rate_adjust` ON, exactly the
  **already-validated active-follower seam** (`snapclient → loopback → camilla
  [rate_adjust] → DAC`). No mixer, no new clock topology — the leader's own
  drivers are driven by the follower endpoint config verbatim, while camilla#1
  bakes the wire.
- **With leader TTS:** TTS must be summed **pre-crossover** (camilla#2 has a
  single capture and cannot mix a second source), so a summing stage moves in
  front of camilla#2 and **becomes the sole loop**; camilla#2 then runs
  `enable_rate_adjust` **OFF** (a passive, DAC-clocked crossover/EQ block, the
  ppm absorbed upstream by the one loop keeping its output buffer fed). Running
  camilla#2's `rate_adjust` *and* the upstream matcher is two loops referenced
  to the same terminal error through the shared buffer — the documented
  `rate_adjust`+resampler oscillation (CamillaDSP #207); a "near-idle trim" only
  widens the stable region, it does not survive the load/thermal/scheduler
  swings a music+voice Pi throws. **One live loop. Not two.**

> **CONSTRAINT — DO NOT MERGE THE TWO `jasper-outputd` INSTANCES.** The
> summing+rate-match outputd (`outputd-summer`, **upstream** of the crossover)
> and the DAC-owning, AEC-reference-publishing outputd (`outputd-final`,
> **downstream** of the crossover) are two instances of the same binary that
> **must stay separate**, and the reason is **invisible from their config** —
> they read as obvious duplication. The reason: **inv-A requires the AEC
> reference to equal the *post-crossover* final electrical** (TTS-inclusive), so
> the box cancels *its own band-limited* voice instead of waking on / talking
> over it. That pins the reference publisher downstream of camilla#2's crossover;
> the summer must be upstream (it feeds camilla#2). Merge them and the published
> reference becomes *pre-crossover* — AEC silently stops cancelling the speaker's
> own TTS, with no error, no config diff, and no test failing unless one asserts
> reference == post-crossover. If you are reading the config and about to
> consolidate these two units, **this paragraph is why you must not.**

**Topology (the with-TTS final form):**

```
renderers─fanin─music tap─► camilla#1 (:1234)  B/C+headroom, File→SNAPFIFO ─►snapserver─wire─►follower
            (pair 7)                                   │
                       leader snapclient (--host 127.0.0.1, --player file → FIFO)
                                                       ▼
       outputd-summer  ◄── TTS + commanded duck (soft inputs)   ──  THE ONE LOOP (content_bridge=rate_match) + sum
                                                       │  (pipe; or loopback in the two-instance build)
                                                       ▼
       camilla#2 (:1235)  crossover, rate_adjust OFF  (passive, DAC-clocked by backpressure)
                                                       │  (pair 5)
                                                       ▼
       outputd-final   DAC owner + AEC reference  (POST-crossover ⇒ inv-A, band-limited TTS in the reference)
                                                       ▼  DAC (tweeter-safe)
```

**Summing, not sidechain.** JTS ducking is *commanded* (`PROGRAM_DUCK_ON/OFF`
over the TTS socket, a ramped gain — `jasper-fanin`'s `program_duck_gain()`),
not an auto-detecting sidechain compressor. So TTS *and* the duck fold into
`outputd-summer` (the single matcher); there is no separate downstream ducker.
The duck follows for free: point the leader's `JASPER_TTS_OUTPUTD_SOCKET` at
`outputd-summer` and the in-band duck command rides the same socket.
`JASPER_OUTPUTD_TTS_SOCKET` on `outputd-final` stays **unset** (its
post-crossover 2-ch mixer is the full-range-to-tweeter hazard — the recorded
latent guard hazard, now closed belt-and-suspenders in
[#925](https://github.com/jaspercurry/JTS/pull/925) via
`JASPER_OUTPUTD_ACTIVE_LANE`, so the mixer fails closed on an active sink even
if the socket were set).

**Pair budget — only DAC-side hops force loopbacks.** Pairs are consumed by
components that demand a real ALSA device, not by stages; our own daemons
pipe/FIFO for free. snapclient writes a **FIFO**; `outputd-summer` writes a
**pipe** that camilla#2 **File-captures** (`type: File`/`Stdin` — which has *no*
rate-adjust, exactly matching camilla#2's `rate_adjust` OFF; see
[HANDOFF-multiroom.md](HANDOFF-multiroom.md) "File/pipe has no rate-adjust"). So
the only forced loopbacks are pair 7 (`fanin→camilla#1`, dsnoop multi-reader)
and pair 5 (`camilla#2→outputd-final`); renderers hold 0–4; **pair 6 is free**
(consumed only if the summer writes a loopback — the two-instance build below).
No 9th pair, no second `snd-aloop` card. *File-capture-frees-pair-6 is confirmed
in principle; nail it empirically in the camilla#1/#2 emit work.*

**camilla#1 program bake — verifier exemption (safe by construction).** camilla#1
emits the program domain only (Layer B/C + headroom, **no** Layer A), `File` sink
→ `SNAPFIFO`, `enable_rate_adjust: false`. It bypasses the graph carrier (as the
follower arm does), and `classify_camilla_graph` gains one arm: **a flat program
graph whose `devices.playback.type == File` is safe regardless of topology** — no
DAC is attached, so no driver can be over-driven. Key it strictly on the playback
*type* and reuse the existing `playback_is_pipe` parser
([leader_config.py](../jasper/multiroom/leader_config.py)) so the exemption and
the leader-pipe liveness check cannot disagree. The dangerous direction (a flat
*Alsa*-sink graph reaching the DAC) is **not** exempted — the existing tweeter
block still fires.

**Sequencing — isolate the new clock topology from the 2-instance bring-up.**
Because the music-only path *is* the validated follower seam, the on-device gates
split so a failure has one candidate cause: (1) bring up the active leader on the
**validated seam** (camilla#1 bake + camilla#2-as-follower-endpoint `rate_adjust`
ON, no summer) — proves the two-instance setup + CPU/thermal + music sync on a
proven clock; (2) swap in `outputd-summer` + camilla#2 `rate_adjust` **OFF**
(still music-only) — isolates the **new** clock topology, gated by the
pre-registered soak signatures below; (3) arm TTS + the commanded duck as a soft
input into the now-proven summer, plus the follower fail-closed cue (same
injection point). A failed soak in step 2 points unambiguously at the summer
topology, not at the 2-instance setup or at TTS.

**OPEN — the summer build (resolved by the `jts3` measurement, not here).** What
`outputd-summer` is built from is **not settled**: (a) a **second
`jasper-outputd` instance** — maximum reuse of the shipped
`content_bridge=rate_match` + TTS mixer, reference-publish off; heavier (two
outputd processes) and outputs a loopback, so it consumes pair 6 — vs (b) a
**lean pipe-writing summer** reusing only the `content_bridge` rate-match logic —
less RAM, frees pair 6 via camilla#2 File-capture, some new code. **Resolution
mechanism:** the `jts3` RAM/CPU measurement inside the Slice-5 CPU/thermal gate,
plus a soak A/B. **Order logic:** prefer **lean-first** if RAM is the binding
constraint on the 1 GB Pi (it usually is, per the OOM history); fall back to the
**two-instance** build if a from-scratch summer's rate-match quality does not
match the shipped `content_bridge`. Do not encode either choice in config before
that measurement.

## Subwoofer — two different "subs" (gaps 4 & 5)

These are conflated in shorthand but are distinct designs:

- **Local sub driver (gap 4 / slice 6a — LANDED 2026-06-23):** a sub on a
  *single* box's spare DAC output. The two old hard-blocks
  (`baseline_subwoofer_not_supported`, `subwoofer_staging_not_supported`) are
  **lifted behind a sub-aware safe path**: the active multi-output emitter
  ([camilla_yaml.py](../jasper/active_speaker/camilla_yaml.py)) now emits a sub
  lane (clip-safe L+R mono-sum → LR4 low-pass → gain ≤0 → soft-clip limiter,
  `driver_protection` 50 Hz/300 ms) plus the complementary mains high-pass at the
  same Fc (bass management), for active mains AND a degenerate 1-way passive main
  (`profile.py` `LocalSubwoofer` + 1-way support; `output_topology` carries the
  per-sub `crossover_fc_hz`). The graph re-proof
  ([graph_safety.py](../jasper/active_speaker/graph_safety.py)
  `sub_guard_present` + `mains_highpass_present` + `bass_management_corner_matched`,
  demanded by [runtime_contract.py](../jasper/active_speaker/runtime_contract.py))
  is the matched safety net (no hardware loop); the sub starts muted in staging
  and is structurally excluded from the audible-target resolver. A subless passive
  speaker is byte-identical (unchanged flat `emit_sound_config`). UI: a crossover
  Fc control on the `/sound/` subwoofer card + a called-out "subwoofer filter"
  band in the PEQ view. **On-device acoustic validation owed** (needs a
  commissioned ≥3-output DAC — DAC8x/jts3). **Orthogonal to wireless — a
  solo-active win.**
- **Wireless sub member (gap 5):** a *separate* bonded sub box. **Where its
  filtering runs is a hardware-target tradeoff, not a fixed rule** —
  `channel_split.py` already emits the sub fragment for *either* host:
  - **Receiver-side (brainy sub):** the sub runs endpoint-crossover mode,
    picks mono from the **one shared stereo stream**, and low-passes
    **locally** (leader specifies the corner). Reuses the follower path
    verbatim — no transport change, no extra leader work — but the sub must
    run CamillaDSP (the Zero 2 W "crossover endpoint" tier).
  - **Sender-side (dumb sub):** the **leader pre-bakes** the sub's filtering
    (low-pass crossover **+** an optional subsonic/excursion-protection
    high-pass) and streams a finished mono sub channel; the sub is pure
    `ChannelPick`. Lets the sub be the cheapest possible box (no CamillaDSP),
    at the cost of a **second leader bake + a separate, loosely-synced sub
    stream** — the shared 2-ch stereo stream can't carry a pre-filtered sub
    channel without stripping the mains' bass or changing the pinned format.
    Loose sync is fine (bass is non-localizable — the multiroom
    "loose-sub-sync" note). This is `channel_split.py`'s documented
    "leader pre-bakes a DUMB endpoint's dedicated stream" path.

  **Default: receiver-side** — it follows the "crossover on the receiver"
  rule and reuses the follower path; sender-side is the **exception** for a
  maximally-cheap dumb sub. **Setting vs execution are independent:** the
  crossover corner is *set* on the leader's pair page (cohesive — the leader
  orchestrates the pair) even though the low-pass *executes* on the sub.
  **Shipped (2026-06-23) — the dumb receiver-side path.** `'sub'` is now its
  own `ChannelPick::Sub(corner)` in `jasper-outputd` (`dac_content.rs`): it
  mono-sums (clip-safe, the existing `Mono` average) then applies a 4th-order
  Linkwitz-Riley low-pass (LR4 — two cascaded Butterworth biquads, Q=1/√2,
  stateful, 48 kHz) before the DAC. Receiver-side, on the **same** dumb
  full-range round-trip lane (no CamillaDSP on the sub, no second stream). The
  corner is `GroupingConfig.crossover_hz` (`/rooms/`-set,
  `JASPER_GROUPING_CROSSOVER_HZ`, default 80 Hz, range 40–200); the reconciler
  forwards it to outputd as `JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ` **only** for a
  `sub` member. Fail-closed everywhere: a sub never plays full-range — the FIFO
  path, the inv-B fallback period (`apply_pick_to_fallback_period`, wired in
  `main.rs` before trim/duck/publish, active from the first period since the
  policy starts in fallback), and a missing filter (→ silence) all enforce
  mono+LP-or-silence. The earlier `'sub'` → `ChannelPick::Mono` (full-range)
  behavior is retired. **This path does NOT high-pass the mains** — they stay
  full-range and the sub *adds* lows (the interim model); the complementary
  mains-HP below still needs an **active** leader (a passive leader has no
  local DSP to high-pass).

  **Bass management is the intended model** (superseding the interim "mains
  full-range, sub *adds* lows" `channel_split.py` ships today): with a sub
  present, the channels that previously carried the bass get a complementary
  **high-pass** at the same corner. The sub low-pass and the mains' high-pass
  are **two halves of one crossover** (shared Fc, complementary LR slopes sum
  flat), so they are **configured as one unit on the leader's pair page** and
  the system derives + distributes both: the sub LP executes on the sub, each
  main's HP folds into **that main's own active crossover** (the bottom of its
  woofer band) — both receiver-side. The shared stream stays full-range so the
  sub still gets its bass. **Clean when the mains are active** (the HP is just
  a parameter on their existing graph); a **passive** main has no local DSP to
  high-pass, so a sub with passive mains needs a brainy main or separate
  streams — resolve at 6b.

Both paths reuse the LR4 primitive (`emit_linkwitz_riley`); they differ in
*which box runs it* and whether the sub needs its own stream.

## Clock domain + fail-closed (cross-cutting safety)

- **Never full-range to a tweeter — graph-resident protection.** The
  follower's loaded graph is *always* the re-proven driver-domain
  baseline; only the capture *source* varies. So no capture content —
  stream, silence, or garbage — can produce a full-range driver feed.
  This is the active-crossover analogue of inv-1 and is strictly safer
  than the dumb-follower path.
- **Stream stall → silence, not full-range.** Loopback underrun →
  CamillaDSP emits silence through Layer A (silence through a crossover is
  silence). Surface a cue ([cues/registry.py](../jasper/cues/registry.py))
  + a `/state` flag + dashboard card (AGENTS.md "no silent failure").
- **Self-recovery (AGENTS.md resilience).** Unplug / brief WiFi loss /
  power cycle: un-bond → follower returns to solo active and plays local
  content; no silent restart loop. The reconciler owns the transition.
- **Clock domains + the bit-perfect config (pin these on the follower
  crossover instance).** snapclient stuffs to the server clock; camilla
  rate-tracks the loopback *capture* only (no resampler); outputd's DAC paces.
  For the bit-perfect virtual-clock path the instance MUST set **real DAC =
  playback (clock master), loopback = capture (slaved)** — invert it and you
  lose bit-perfect and fall back to resampling. `enable_rate_adjust: true`,
  **resampler null** (no AsyncSinc when capture rate == playback rate — the
  documented `rate_adjust`+resampler oscillation, CamillaDSP #207), **chunksize
  ≥ 1024** (512 → EPIPE underruns on a Pi) and a fixed `target_level`.
  snapclient `--latency` nulls camilla's fixed pipeline latency so an active
  follower stays sample-locked with a dumb follower — but only if that latency
  is **truly constant**: **forbid SIGHUP config reloads during playback** on the
  crossover instance, and validate the nulling **acoustically** (the S0-sync
  gate below), never trust the nominal `--latency` number alone.

## Layering (preserve the one-way direction)

The new coupling is **multiroom → active_speaker** (one-way; multiroom
already imports `jasper.sound`). `active_speaker` **never** imports
multiroom (its lone current `multiroom` mention is a doc comment in
[runtime_contract.py](../jasper/active_speaker/runtime_contract.py)). The
capture-device + domain-mode parameterization keeps `active_speaker`
ignorant of grouping — it accepts a capture device and a domain mode; the
multiroom reconciler decides them per role. This preserves the invariant
that makes solo-active EQ safe in isolation.

## External validation (2026-06-20)

A source-cited external design review pressure-tested the load-bearing claims and
**confirmed** the engine decision (CamillaDSP bit-perfect loopback rate-tracking;
the `rate_adjust`+resampler oscillation trap — CamillaDSP #207) and every safety
building block (LR4 sums flat; sub+mains = one crossover; clock drift ~1 ms/min,
audible within ~1 min). It sharpened two seams this doc now reflects: (1) the
snapclient→loopback→downstream-CamillaDSP sync seam is the #1 risk and must pass
the **S0-sync de-risk gate** before Slice 3 (builders report failing exactly this
shape); (2) the 1 GB-RAM question is really **CPU + thermal** (active cooling) —
Q1 reframed. It also pinned the follower crossover config (clock-master direction,
`chunksize ≥ 1024`, no SIGHUP during playback — folded into "Clock domain").

**Physical tweeter protection (hardware high-pass / amp mute-on-fault) is
owner-handled offline and is OUT OF SCOPE for these slices** — do not add it as a
code requirement. The software fail-closed (graph-resident protection +
stall→silence) remains the in-band behavior; hardware backstops are the owner's
domain.

## Slice plan

**v1 = Slices 1–5** (the follower core **plus** the matched-pair leader —
v1 gates on the leader per the owner decision). **6a/6b are post-v1.** The
slices land safest-first; each is independently mergeable.

| Slice | v1? | Scope | HW? |
|---|---|---|---|
| **0** | — | This design-of-record + README atlas + doc-map wiring | no |
| **S0-sync** | ✅ | **De-risk gate** — bench the snapclient→loopback→CamillaDSP sync seam with 2 throwaway active followers; acceptance = p99 < 5 ms over 2 h (two-mic acoustic), no audible resync, + ≥24 h `snd-aloop` xrun soak. **Gates Slice 3** | **yes** (2 Pis) |
| **1** | ✅ | Role/capture: thread `capture_device`; pure-data `OutputTopology` pairing field. Golden byte-identical solo | no |
| **2** | ✅ | Driver-domain-only active emit variant + `classify_camilla_graph` arm + keystone round-trip test | no |
| **3** | ✅ | Reconciler wires the active **follower** (capture→loopback, disable outputd pick, fail-closed silence + **cue injected into camilla's input, pre-Layer-A, follower-local** — Q2 step 5); lift `non_single_alsa_sink`. **Gated by S0-sync.** Pin clock-master / chunksize≥1024 / no-SIGHUP-during-playback | **yes** (2 Pis) |
| **4** | ✅ | Narrow follower-409 + render local driver UI on a follower's `/sound/`; make the delegation promise true | no |
| **5** | ✅ | Active **leader** (2nd CamillaDSP; realization ratified 2026-06-21 — single rate loop = `outputd-summer`, camilla#2 `rate_adjust` **OFF**, the two `jasper-outputd` instances kept **separate** for inv-A, Option-3 TTS as a soft input, inv-B-through-Layer-A). Sequence: validated-seam music → swap-in-summer (soak gate) → arm TTS. **The v1 gate**; **CPU/thermal + summer-build pick** measured on `jts3` (active cooling). Details: "Stage B — the ratified active-leader realization" | yes |
| **6a** | — | Local sub driver — **LANDED 2026-06-23**: sub lane (LR4 LP) + bass-management mains-HP in the active multi-output emitter (active + degenerate-1-way passive), matched re-proof, both guards lifted behind the safe path, `/sound/` Fc control + called-out PEQ band. On-device acoustic check owed (≥3-output DAC) | mixed |
| **6b** | — | Wireless sub member + bass management. **Dumb receiver-side sub LANDED (2026-06-23)** — outputd `ChannelPick::Sub` LR4 low-pass + `crossover_hz` SSOT + `/rooms/` sub role + `/sound/` CTA (HW-free green; `jts`→`jts4` on-device validation in progress). The N-member `JASPER_GROUPING_ROSTER` + an "add a subwoofer to a stereo pair" (2.1) `/rooms/` flow landed 2026-06-23 (unbond disables every member — no orphaned sub). Remaining: bass-management mains-HP (needs an active leader) and the brainy/active-endpoint sub | mixed |

Slices 1–2 are hardware-free and independently shippable; 3 is where
on-device begins; **5 is the v1 gate** (matched pair proven on hardware).

**Landed so far:**

- **Slice 1** — the compile/apply seam (`build_baseline_profile_candidate`,
  `apply_baseline_profile`) threads `capture_device` into
  `emit_active_speaker_baseline_config` (default `plug:jasper_capture` keeps the
  solo baseline byte-identical; `recompose_baseline_yaml` deliberately keeps the
  default — program-domain EQ always captures from fan-in), and `OutputTopology`
  carries a pure-data `pairing_intent` (`solo | will_be_follower | has_follower`,
  absent == `solo`). Invariants 1, 2, and 7 are pinned by
  [`tests/test_active_speaker_baseline_profile.py`](../tests/test_active_speaker_baseline_profile.py)
  and [`tests/test_output_topology.py`](../tests/test_output_topology.py).

- **Slice 2** — the **driver-domain-only emit variant** + the **verifier arm**.
  `emit_active_speaker_driver_domain_config`
  ([camilla_yaml.py](../jasper/active_speaker/camilla_yaml.py)) composes the
  follower's Layer-A graph — `channel_select (2->2 pick L/R/mono) ->
  split_active_<way>way (2->N) -> per-driver [crossover, delay, non-positive
  gain, soft-clip limiter]` (tweeter band-limited by its crossover high-pass),
  with **no** program-domain headroom and **no** preference EQ (the leader baked
  Layer B/C). It reuses the baseline emitter's per-driver definitions verbatim
  (the relocated Layer A is byte-for-byte the solo chain), and the channel-select
  recipe is now the shared `jasper.camilla_emit.emit_channel_select_mixer` so the
  follower and the multiroom member-config path can't drift. `classify_camilla_graph`
  ([runtime_contract.py](../jasper/active_speaker/runtime_contract.py)) grows a
  `GRAPH_DRIVER_DOMAIN_BASELINE` arm keyed on the new `# Source:` marker: Layer A
  present (crossover HP + per-driver limiter `clip<=0` + gain `<=0` + `volume_limit
  == 0.0`), channel-select present **and preceding the split**, program prefix
  absent. Emitter↔verifier stay independent. It is **not** wired into
  `safe_graph_for_current_topology` selection (that is Slice 3) — keeps invariant 7.
  Invariants 3 (keystone round-trip) and 4 are pinned by
  [`tests/test_active_speaker_runtime_contract.py`](../tests/test_active_speaker_runtime_contract.py)
  and [`tests/test_active_speaker_driver_domain.py`](../tests/test_active_speaker_driver_domain.py).

- **Slice 3** — the reconciler wires the active **follower** (code landed;
  on-device validation owed). The compile/apply seam
  (`build_baseline_profile_candidate` / `apply_baseline_profile`,
  [baseline_profile.py](../jasper/active_speaker/baseline_profile.py)) grew a
  `driver_domain` + `program_channel` + `capture_format` mode that emits the
  Slice-2 driver-domain graph; default off keeps the solo baseline
  byte-identical. A new `jasper.multiroom.follower_config`
  ([follower_config.py](../jasper/multiroom/follower_config.py)) is the
  active-follower apply/restore arm (mirrors `leader_config`): it builds +
  **re-proves** (`classify_camilla_graph`) the driver-domain config, swaps
  CamillaDSP glitch-free (snapclient → `hw:Loopback,0,6` → CamillaDSP captures
  `hw:Loopback,1,6` — pair 6 is the passive content lane, free on an active
  follower since its outputd is always Composite/active (reads pair 5) and never
  opens the passive lane; snd_aloop caps at 8 pairs so no dedicated extra pair
  exists — `enable_rate_adjust`, no resampler, `chunksize` 1024,
  `S16_LE`), stashes the prior solo-active config, and on un-bond restores the
  **active** baseline (never a passive graph). The reconciler
  ([reconcile.py](../jasper/multiroom/reconcile.py)) detects an active box
  (`is_active_speaker_box`), routes snapclient to the round-trip loopback (ALSA
  player, not the dumb FIFO), DISABLES outputd's `dac_content` ChannelPick on
  this box (camilla owns the pick + split), and runs a readiness GATE before
  tearing down the solo path — a follower that can't be made safe **fails safe
  to solo active** (it never bonds an unprovable graph; invariant 5 +
  self-recovery). The outputd fence
  ([config.rs](../rust/jasper-outputd/src/config.rs)) is "lifted" in the real
  sense: Option B routes the active sink around the `dac_content` lane, so the
  `dac_content_lane_rejects_non_single_alsa_sink` guard (kept — it still guards
  the dumb-follower lane) simply never fires on the active-follower path; a
  positive test pins that an active sink + no dac_content parses. `/state` grows
  an `endpoint` block (`active_crossover` | `blocked` + reason). Invariants 5
  (+ the keystone re-proof) are pinned by
  [`tests/test_multiroom_follower_config.py`](../tests/test_multiroom_follower_config.py),
  [`tests/test_multiroom_reconcile.py`](../tests/test_multiroom_reconcile.py),
  and [`tests/test_multiroom_state.py`](../tests/test_multiroom_state.py).

  **Fail-closed cue — v1 reality.** The reconciler is a oneshot that cannot
  play a cue (no `AudioCueManager`; a follower is voice-parked). So the v1
  fail-closed *signal* is the solo-active fallback (the box keeps playing its
  own content — not silent) plus the `/state` `endpoint.blocked_reason` +
  doctor + `event=multiroom.reconcile.active_follower_blocked` log. The
  **audible** cue through the follower's Layer A on a parked follower / runtime
  stall is resolved by **Q2 spike step 5** (ratified — see the "Decision" record
  below): inject it into the follower's camilla input (pre-Layer-A,
  follower-local) via a long-running writer (the grouping supervisor /
  `jasper-control`, since the reconciler oneshot and parked `jasper-voice`
  cannot), never a post-camilla mix. It still does **not** gate the follower
  core. The hard safety guarantee (no full-range to
  the tweeter) holds unconditionally: the loaded graph is always the re-proven
  driver-domain baseline (or the solo-active baseline on fail-closed), so
  stream / silence / garbage all resolve to silence-through-Layer-A, never a
  full-range feed.

- **Slice 4** — the HW-free web half: a bonded follower's `/sound/` renders the
  local driver/crossover/commissioning UI (the active-speaker endpoints were
  never in the content-DSP 409 block); invariant 6 is pinned by
  [`tests/test_sound_setup.py`](../tests/test_sound_setup.py). (Slice 3 owns
  the runtime audio path that makes the delegation promise true end-to-end.)

- **Slice 5 (partial — leader camilla#1 bake + verifier exemption)** — the
  HW-free emit + verifier half of the active *leader*, per "camilla#1 program
  bake — verifier exemption" above. `emit_active_speaker_program_bake_config`
  ([camilla_yaml.py](../jasper/active_speaker/camilla_yaml.py)) emits the
  PROGRAM domain only (Layer B/C + headroom, `File`→`SNAPFIFO`,
  `enable_rate_adjust: false`, **no** Layer A) by reusing
  `jasper.sound.camilla_yaml.emit_sound_config`'s program assembly verbatim and
  re-stamping a distinct DAC-less-bake `# Source:` marker
  (`ACTIVE_PROGRAM_BAKE_SOURCE`). It bypasses the graph carrier exactly as the
  follower arm does — the carrier fence `eq_on_active_bonded_member` (the
  interactive `/sound` EQ path) is untouched. `classify_camilla_graph`
  ([runtime_contract.py](../jasper/active_speaker/runtime_contract.py)) gains one
  arm: a flat program graph whose `devices.playback.type == File` is allowed
  regardless of topology (`GRAPH_PROGRAM_BAKE_PIPE`), keyed STRICTLY on the
  File-pipe playback via the shared
  [`playback_is_pipe`](../jasper/multiroom/leader_config.py) parser so the
  exemption and the leader-pipe liveness check can't disagree. The dangerous
  direction (a flat *Alsa*-sink graph reaching the DAC under a roleful topology)
  stays blocked, and the pipe bake is **not** selectable as a solo speaker's own
  graph (its File sink feeds the FIFO, not the DAC — `safe_graph_for_current_topology`
  excludes it). Emitter↔verifier stay independent. **The reconciler wiring that
  *runs* camilla#1 in this mode (the two-instance bring-up) landed HW-free as
  Stage B Step 0** (see the B-Step-0 callout under "On-device status"); the
  `outputd-summer` + the on-device gates remain later steps. Pinned by
  [`tests/test_active_speaker_program_bake.py`](../tests/test_active_speaker_program_bake.py)
  and the program-bake arms in
  [`tests/test_active_speaker_runtime_contract.py`](../tests/test_active_speaker_runtime_contract.py).

## Multi-Pi validation (Slice 3+)

**S0-sync de-risk gate — run BEFORE Slice 3.** The snapclient→loopback→
downstream-CamillaDSP seam is the single hardest part: Snapcast/CamillaDSP
builders report failing to sync exactly this shape. Before investing in the
Slice-3 reconciler, bench it with two throwaway "active followers"
(snapclient → loopback → a crossover-only CamillaDSP → DAC), two-mic acoustic
capture. **Acceptance: p99 inter-speaker offset < 5 ms over a 2-hour run, no
audible resync**, plus a ≥24 h `snd-aloop` xrun soak. **If this gate fails, an
active wireless *follower* is not viable** — fall back to active-stays-solo-or-
leader (the leader runs its own crossover locally on the round-trip; followers
stay dumb/passive). Do not build Slice 3 until S0-sync passes.

Two Pis: leader = `jts3.local`, a second as follower (commissioned active
2-way). Deploy with `PI_HOST=jts3.local bash scripts/deploy-to-pi.sh`.
Gates:

- **Safety bar:** no full-range reaches the tweeter — the re-proof
  (`classify_camilla_graph` on the live follower config) **plus** an
  on-seat listen.
- **Sync:** inter-speaker error meets the multiroom target (p99 < 5 ms)
  with camilla's fixed latency nulled by `--latency`.
- **Fail-closed:** pull the stream mid-play (unplug / `tc netem`) → the
  follower goes **silent + cue**, never full-range. The cue is injected into the
  follower's **camilla input (pre-Layer-A), follower-local** (Q2 step 5) — so it
  too is band-limited and never reaches the tweeter full-range.
- **Self-recovery:** un-bond → follower self-recovers to solo active.

### S0-sync de-risk gate — bench result (2026-06-20)

A throwaway BENCH run **before** Slice 3 wires the reconciler, to prove (or
disprove) that a wireless **active** follower stays sample-locked through the
one seam the active path adds and the dumb path deliberately avoids:
`snapclient → snd-aloop → crossover-only CamillaDSP → real DAC`. (The dumb
follower path uses `--player file` → raw FIFO precisely to dodge snd-aloop; the
multiroom spike already validated *its* p99 budget. S0 isolates the **new**
risk: the snd-aloop re-entry + the `rate_adjust`/no-resampler capture-from-
loopback clock seam against the DAC.) Harness (throwaway, no product code):
[`scripts/s0-sync-bench.sh`](../scripts/s0-sync-bench.sh) +
[`scripts/s0-sync-measure.py`](../scripts/s0-sync-measure.py). Topology:
snapserver + follower#1 on `jts3` (HifiBerry DAC8x), follower#2 on `jts4`
(Pi Zero 2 W, USB dongle — the cheap-follower tier, so a stricter soak); each
`snapclient → hw:Loopback → camilla [crossover-only, `volume_limit:0`,
`enable_rate_adjust`, no resampler, chunksize 1024, fixed `target_level`] →
DAC`, with `snapclient --latency`. (`jts.local` was briefly used as the second
box to try its onboard mic for the acoustic gate — see below.)

**Method.** The seam's clock-lock is measured **directly** from camilla's
websocket (state + `buffer_level` vs target + `rate_adjust` + raw capture rate
via `pycamilladsp`) — the most direct signal that the loopback holds against
the DAC — alongside a **≥24 h snd-aloop xrun soak** (journal-clean gate) and
CPU/temp/Pss. snapcast's per-client offset is the inter-client sync proxy.

**Result (telemetry basis the owner accepted; ~0.65 h xrun-clean — full ≥24 h
durability soak TODO, boxes reclaimed early):**
- **Clock-lock: PASS (LOCKED, both followers, on every pair exercised —
  jts3+jts4 and jts3+jts.local).** Over the ~0.65 h run, `state=RUNNING`
  throughout; `buffer_level` holds target (jts3 999–1055, mean 1025/1024; jts4
  964–1109, mean 1032/1024); `rate_adjust` tight and stable (~0.99980–1.00007,
  i.e. < ±0.03 %). camilla logs `Capture device supports rate adjust` —
  HEnquist's bit-perfect loopback method engages (no resampler). **0 xruns.**
  Notably the weak Zero 2 W (`jts4`) locks as cleanly as the Pi 5s.
- **snd-aloop xrun soak: clean over ~0.65 h, then the lab boxes were reclaimed
  — the full ≥24 h durability soak is NOT yet run** (re-run via
  `s0-sync-bench.sh --soak 24` on a dedicable Pi to catch slow thermal/drift/
  leak failures the short run can't). Steady-state cost: camilla ≈ 5.5 MB Pss,
  snapclient ≈ 5 MB; temps jts3 ~40 °C, jts4 52–55 °C (Zero 2 W), load < 1.1,
  no throttling.
- **Inter-client sync:** snapclient `diff to server` ≈ 0 ms steady-state
  (sub-ms) — necessary-not-sufficient (does not see camilla's contribution;
  the clock-lock telemetry above does).
- **Acoustic p99: DEFERRED.** The onboard mics (jts3 XVF; and jts.local's
  XVF + USB-PnP, tried as a mic-equipped second box) **cannot** measure the
  inter-speaker offset — each is dominated by its
  own close speaker, so the autocorrelation can't resolve the faint far
  speaker (it returns "no clean peak"; an earlier constant ~0.29 ms read was an
  analyzer artifact = the search-window floor, since fixed). The acoustic p99
  needs a single mic placed **between** the two speakers at comparable level —
  the speakers must be co-located. **Owner accepted the telemetry de-risk
  (2026-06-20)**; the acoustic end-to-end p99 is an explicit follow-up.

**Findings for Slice 3 (operational, hardware-learned):**
- **Borrowing the DAC reboots a live JTS box.** The essential audio units
  (`jasper-fanin`/`camilla`/`outputd`/`voice`/`aec-bridge`) carry
  `StartLimitAction=reboot`; stopping them lets a re-trigger fail-loop into a
  reboot (hit 3× on `jts3`). The bench disarms first via the same `/run`
  drop-in (`StartLimitAction=none`) that `jasper-bootloop-guard` uses, verifies
  it, then stops. **Slice 3 does NOT have this problem** — the reconciler swaps
  the chain *in place* (no DAC contention); the bench hits it only because it
  displaces the whole stack. Worth knowing for any future DAC-borrowing bench.
- **snapserver does not reliably hold a pipe's read end** (`mode=read` AND
  `mode=create` both ENXIO'd a writer); the bench feeds via a `process://`
  source. Production feeds the snapfifo from CamillaDSP's `File` output, which
  sidesteps this.

**Verdict + consequence.** On the telemetry basis the owner accepted, the
**clock seam holds — the active wireless follower stays sample-locked** (both
followers lock with a tight, stable `rate_adjust` and 0 xruns; snapcast sub-ms
inter-client sync). Provisional **PASS → Slice 3 is GO**, with two
confirmations outstanding before it's unconditional: the **≥24 h durability
xrun soak** (only ~0.65 h run so far, clean — re-run on a dedicated Pi to catch
slow thermal/drift/leak failures), and the **acoustic end-to-end p99 < 5 ms**
once a between-speakers mic is placed. (A later xrun-soak failure would
downgrade to "retry a constructed/hardware loopback per the prior-art note"
before shelving.)

**Slice 5 (the v1 gate) adds the matched-pair gates** — two *active*
speakers, one as leader:

- **Leader CPU/thermal (the real limit, not RAM):** two CamillaDSP (bake +
  driver crossover) + snapserver + snapclient on a 1 GB Pi 5 — RAM has big
  headroom, so gate on **sustained CPU < ~70%, zero xruns over 2 h, and no
  thermal throttling** (the uncooled Pi 5 drops 2.4→1.5 GHz — **active cooling
  assumed**). Capture real `htop`/temp under load on `jts3`.
- **Leader TTS (Option 3, ratified — Q2 spike):** "Hey Jarvis" replies on the
  leader reach the tweeter **band-limited via camilla#2's Layer A** — TTS is
  summed into the crossover instance's input loopback (post-snapclient, so it
  never traverses the round-trip), the content-duck follows the same point, and
  the outputd 2-ch TTS mixer is **not** armed on the active leader. Confirm no
  full-range speech to the tweeter and TTS-to-glass ≈ the solo-active baseline
  (~85–125 ms DSP+playout), not the ~400 ms round-trip.
- **Matched-pair sync + safety:** both active speakers hold p99 < 5 ms and
  pass the no-full-range re-proof; leader self-loop stall → inv-B falls
  back to direct fan-in **through Layer A** (cue + `/state`), never silent
  and never full-range.
- **Leader clock-lock soak — pre-registered signatures (fixed BEFORE the run,
  not rationalised after).** The single-loop realization (see "Stage B — the
  ratified active-leader realization") must produce the *stationary* signature
  over a ≥24 h `snd-aloop` soak once `outputd-summer` + camilla#2 `rate_adjust`
  OFF are in the path. Log the **resampler ratio**, the **fill of every buffer**
  at the crossing, and an **end-to-end latency probe** for the whole run. Three
  signatures, fixed now so there is nothing to rationalise against later:
  - **One clean loop (PASS):** ratio is a stationary random walk around the true
    crystal offset (≈ constant few-ppm, bounded noise); fills stationary;
    latency flat.
  - **Two coupled loops (the rejected `rate_adjust`-ON-camilla#2 failure):**
    low-frequency *hunting* in the ratio and/or a *beat* between two fills;
    latency breathes.
  - **No matcher (a transparent summer with no rate-match):** monotone fill
    *ramp* → underrun; latency creeps ~1 ms/min.
  This is the discriminator the seconds-scale inter-client diff cannot see, and
  it is why the **≥24 h soak** (still owed from S0-sync) gates the clock-topology
  step before it locks.

### On-device status (2026-06-21) — Stage A passed, Stage B ratified-not-built, Stage C blocked

**Stage A — the active *follower* (Slice 3) on-device validation: PASSED** (the
"on-device validation owed" above is discharged for the follower). Rig: bond
`jts.local` (passive leader) → `jts3` (active follower).
- `/state.grouping.endpoint = active_crossover`; the **live** re-proof
  (`classify_camilla_graph` on the running follower config) returned the
  driver-domain baseline `allowed=True` — no full-range path to the tweeter.
- **Clock-lock under real music:** camilla `:1234` `rate_adjust` spread
  **8–23 ppm**, `buffer_level` steady ~**2040–2085 / 2048**, **0 xruns**,
  `state = RUNNING`.
- On-seat listen confirmed good; **self-recovery** verified — unbond → both boxes
  return to clean solo, `jts3` re-proves its active baseline.
- **Outstanding (telemetry accepted in lieu):** acoustic inter-speaker
  **p99 < 5 ms** needs a mic placed *between* the two speakers (each onboard mic
  hears only its own speaker). Owner accepted the telemetry de-risk; the acoustic
  number is **not fabricated** and stays an explicit follow-up.

**Stage B — the active *leader* (Slice 5): design ratified (see "Stage B — the
ratified active-leader realization" above); the HW-free reconciler arm (Step 0)
is BUILT, on-device bring-up (Steps 1-3) not yet run.** Rig will be
`jts3` (active leader, real drivers) → `jts.local` (passive follower) — exercises
the active-leader code on real drivers without a second active speaker. Gates are
pre-registered above (CPU/thermal, the clock-lock soak signatures, band-limited-
tweeter TTS, inv-B-through-Layer-A).

> **B1 landed (2026-06-21) — dormant camilla#2 infrastructure only.** The
> second CamillaDSP instance (camilla#2, the endpoint-crossover instance on
> `:1235`) now ships as INERT infrastructure: `jasper-camilla-crossover.service`
> + `jasper-camilla-crossover-guard` are installed, the `JASPER_CAMILLA2_*`
> config fields + `crossover_controller()` exist, and install.sh seeds
> `crossover-statefile.yml` through the active-speaker runtime contract. The
> unit is **not boot-enabled and not yet wired into any reconciler** — a later
> PR arms it only when the box is both leader and active. Two B1 safety
> invariants are pinned by tests: camilla#2 carries **NO
> `StartLimitAction=reboot`** (it fails closed to silence, never reboots the
> household speaker, unlike the always-on camilla#1), and its crossover guard
> repairs ONLY to the re-proven **driver-domain (Layer-A-intact) baseline,
> never flat** (a flat crossover would send full-range to the tweeter). The
> summer build, the `rate_adjust` OFF + summing topology, the CPU/thermal +
> clock-lock soak, and the reconciler gating all remain unbuilt.

> **Stage B Step 0 landed (2026-06-22) — HW-free reconciler arm, music-only seam
> (no summer).** [`jasper.multiroom.active_leader_config`](../jasper/multiroom/active_leader_config.py)
> + the grouping reconciler's active-leader branch
> ([`reconcile.py`](../jasper/multiroom/reconcile.py)) now arm the two-instance
> bring-up on bond: camilla#1 runs the program bake (File→`SNAPFIFO`,
> `emit_active_speaker_program_bake_config`) and camilla#2 is armed (`systemctl
> enable --now jasper-camilla-crossover.service`) on a `crossover-statefile.yml`
> **RE-SEEDED with the re-proven driver-domain (Layer-A-intact) graph** — closing
> the B1 seam (the install seed is flat; the crossover guard repairs a dead pipe,
> not a flat statefile). snapclient writes the round-trip loopback (the leader is
> its own receiver) and camilla#2 runs **`rate_adjust` ON — the already-validated
> active-follower seam, no `outputd-summer`, no leader TTS yet**. Fail-closed: if
> either graph can't be re-proven the box **refuses to bond and falls back to
> solo active** (invariant 5, self-recovery); on unbond camilla#2 is disabled and
> camilla#1 restored to the re-proven solo-active baseline (never passive, via the
> shared `follower_config.restore_active_camilla_solo` ladder). The unbond
> teardown is gated on the camilla#2 unit being enabled, so the active-FOLLOWER
> path stays byte-identical. `/state.grouping.endpoint` surfaces
> `mode=active_crossover, role=leader` (or `mode=blocked` + reason on fail-closed).
> Pinned by [`tests/test_multiroom_active_leader_config.py`](../tests/test_multiroom_active_leader_config.py)
> + the active-leader flow tests in
> [`tests/test_multiroom_reconcile.py`](../tests/test_multiroom_reconcile.py).
> **Owed (Steps 1-3, on-device):** the `jts3` bring-up + CPU/thermal gate (which
> also resolves the open summer-build pick), then swap in `outputd-summer` +
> camilla#2 `rate_adjust` OFF + the ≥24 h clock-lock soak, then arm TTS + the
> follower fail-closed cue. One Layer-B caveat to verify on-device: the camilla#1
> bake passes `room_peqs=[]` (an active baseline carries no Layer B today — only
> Layer C/preference + headroom), so confirm the followers hear the same
> correction the leader applies solo.

**Stage C — matched pair (two identical active speakers, one as leader):
BLOCKED.** Precondition is a **second commissioned active speaker with real
drivers**; today only `jts3` qualifies (`jts5` is a dual-Apple-DAC bench box with
*no* real drivers). Until that hardware exists, Stage C — both boxes holding
p99 < 5 ms with the no-full-range re-proof, the leader's Option-3 TTS reaching its
own tweeter band-limited, matched-pair sync + safety — is the remaining v1 gate
after Stage B.

## Invariants → tests

| # | Invariant | Slice |
|---|---|---|
| 1 | Threading `capture_device` with the default reproduces today's baseline **byte-for-byte** (golden) | 1 |
| 2 | `OutputTopology` pairing field round-trips and defaults to `solo`; absent field = `solo` (non-breaking) | 1 |
| 3 | **Keystone:** the driver-domain-only emit, fed back through `classify_camilla_graph`, classifies the new driver-domain arm `allowed=True` — relocating Layer A never breaks the contract | 2 |
| 4 | The driver-domain-only graph has **no** program-prefix filters and **no** positive gains; `volume_limit == 0.0`; channel-select precedes the split | 2 |
| 5 | A follower whose driver-only graph cannot be re-proven **refuses to bond / fails to silence** (no full-range emit) | 3 |
| 6 | Active-speaker crossover endpoints return 200 on a follower; content-DSP POSTs still 409 | 4 |
| 7 | Solo-impact: feature off → byte-identical configs + no new daemon construction (the multiroom solo-impact contract) | all |

## File map

- Roles/capture: [output_topology.py](../jasper/output_topology.py),
  [baseline_profile.py](../jasper/active_speaker/baseline_profile.py),
  [camilla_yaml.py](../jasper/active_speaker/camilla_yaml.py)
- Driver-domain emit + verifier:
  [camilla_yaml.py](../jasper/active_speaker/camilla_yaml.py),
  [runtime_contract.py](../jasper/active_speaker/runtime_contract.py),
  [graph_evidence.py](../jasper/active_speaker/graph_evidence.py)
- Follower wiring: [reconcile.py](../jasper/multiroom/reconcile.py),
  [member_config.py](../jasper/multiroom/member_config.py),
  [channel_split.py](../jasper/multiroom/channel_split.py)
- outputd lane: [dac_content.rs](../rust/jasper-outputd/src/dac_content.rs),
  [config.rs](../rust/jasper-outputd/src/config.rs)
- Web: [sound_setup.py](../jasper/web/sound_setup.py)
- Companion DoRs: [HANDOFF-dsp-graph-carrier.md](HANDOFF-dsp-graph-carrier.md),
  [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md),
  [HANDOFF-multiroom.md](HANDOFF-multiroom.md)

## Q2 spike — active-leader (and solo-active) TTS band-limiting

> **Status: ratified (2026-06-20, measured on `jts3`).** This spike GATES
> Slice 5 (the active leader) and tightens Slice 3's fail-closed cue; it does
> NOT gate the follower core (Slices 1–4) — a follower is voice/TTS-parked
> (`JASPER_GROUPING_VOICE_PARK=1`,
> [reconcile.py](../jasper/multiroom/reconcile.py)), so it has no
> conversational TTS to band-limit. **Decisions:** (1) solo-active TTS is
> already tweeter-safe + in-reference today — **not a gap** (it rides fanin,
> upstream of CamillaDSP's Layer A); (2) **leader-only voice** ratified
> (de-facto true via the follower voice-park + inv-A); (3) **Option 3** (TTS
> summed into the crossover instance's input, upstream of Layer A) is the
> leader mechanism; (4) the follower fail-closed cue uses the **same**
> injection point (into camilla's input, follower-local). **Measured
> incremental TTS-band-limiting latency on `jts3`:** Option 2 ≈ **< 1 ms**,
> Option 3 ≈ **+85–125 ms** (= the solo-active path JTS already ships), the
> rejected Option 1 (snapcast round-trip) ≈ **+400 ms** (`buffer_ms`) —
> Options 2 & 3 confirmed OUT of the round-trip. Full record in
> "Decision (ratified 2026-06-20)" below.**

**Rules today (verified — `rust/jasper-outputd/src/config.rs`, multiroom
inv-A).** TTS is mixed at `jasper-outputd` (final stage), **low-latency** (past
the snapcast round-trip) and **upstream of the AEC-reference publish tap** —
inv-A: the reference must `== final DAC content`, TTS-inclusive, or the speaker
wakes on / talks over its own voice. The outputd TTS mixer is **2-channel
`single_alsa`-only**, so it does not apply to an active (N-channel) sink as-is.

**The trilemma.** Tweeter-safe TTS on an active speaker wants three things that
fight on a leader: **P1** through Layer A (tweeter-safe); **P2** no snapcast
round-trip (low-latency); **P3** summed before the outputd publish tap (in the
AEC reference — non-negotiable). Today's outputd mix = P2+P3, not P1; the fan-in
"upstream" path = P1+P3, not P2 on a leader.

**Latency — the part that actually matters, and the key clarification.** The big
latency (the snapcast round-trip, ~hundreds of ms) is incurred **only** by
sending TTS *upstream of the bake* (Option 1). The tweeter-safe options do
**not** put TTS through snapcast: outputd is past the round-trip, and camilla#2
(the crossover) is *fed by* the round-trip loopback but mixing TTS **into** that
loopback enters **after** snapclient — so TTS traverses only the crossover DSP,
not snapcast. The incremental latency of tweeter-safe leader TTS is therefore
**DSP latency, not round-trip latency**: ~the crossover instance's chunk (tens
of ms, tunable — the same a *solo* active speaker already pays) for the camilla
path, or ~one biquad pass (sub-ms) for a protective filter at outputd. **The
spike measures these on `jts3` and confirms the round-trip is out of the TTS
path for the chosen option.**

**Options.**

| # | Where TTS is band-limited | P1 | P2 | P3 | Cost |
|---|---|---|---|---|---|
| 1 | upstream at fan-in (before the bake) | ✅ | ✗ round-trip | ✅ | laggy; also streams TTS to the follower (inv-A forbids) |
| 2 | at outputd, add per-driver protection to the TTS lane | ✅ | ✅ | ✅ | safety-critical DSP in the reboot-on-fail Rust daemon; "minimal" = either skip-tweeter (muffled voice) or reimplement the crossover (the thing Option-B avoided) |
| 3 | into camilla#2's input (post-round-trip, pre-crossover) | ✅ | ✅ | ✅ | reuses the **verified** crossover + `classify_camilla_graph` re-proof, outputd stays dumb; cost = a new TTS mix point on the loopback + the content-duck must follow + the crossover chunk latency |
| 4 | **leader-only voice** (product decision) | — | — | — | see below |

**Option 4 — leader-only voice (likely yes).** TTS is **already** leader-local
(the follower is voice-parked; inv-A keeps TTS out of the stream), so "voice
plays only from the leader" is de-facto true — ratify it. You don't need stereo
voice; this **removes any need to stream TTS / sync it**, killing the
round-trip-latency worry outright. It does **not** by itself make the *leader's
own* TTS tweeter-safe (the leader is still active), so it **combines with**
Option 2 or 3 for the leader's own drivers. UX: the assistant "lives" on the
leader — fine in one room.

**Solo active is the simpler half and the foundation.** A *solo* active speaker
has no round-trip, so "TTS at fan-in → the one CamillaDSP (which contains Layer
A) → outputd" is P1+P2+P3 for free — and the model the leader case extends. But
the outputd-2ch-only constraint means solo-active TTS can't use the outputd
mixer either, so confirm solo-active TTS actually works today (it may itself be
an unverified gap). Fix solo first.

## Decision (ratified 2026-06-20 — measured on `jts3`, solo active 2-way @ 48 kHz)

The checklist was worked in order; this is the record.

**1. Solo-active TTS today — WORKS, not a gap.** Traced in code and confirmed
on `jts3`. There is exactly one TTS transport
(`JASPER_TTS_TRANSPORT=outputd` — the wire protocol, the only supported value);
the *socket* decides **where** it mixes. Solo defaults route TTS to
`JASPER_TTS_OUTPUTD_SOCKET=/run/jasper-fanin/tts.sock` with
`JASPER_DUCK_TRANSPORT=fanin` ([config.py](../jasper/config.py)), i.e. **into
fanin, upstream of CamillaDSP**. So on a solo active speaker TTS rides
`fanin (music+TTS) → CamillaDSP (Layer A) → outputd → DAC`: it is split by the
per-driver crossover — on `jts3`, the tweeter `LinkwitzRileyHighpass @ 2 kHz`
in the live `active_speaker_baseline.yml` — and is therefore **tweeter-safe**,
and it is **in the AEC reference** (outputd publishes its final electrical
output; `/state.outputd.reference_outputs.speaker_reference_source =
outputd_final_electrical`, TTS-inclusive). The outputd 2-channel `single_alsa`
TTS mixer ([config.rs](../rust/jasper-outputd/src/config.rs)) is **not used** on
the solo path and does **not** silently drop TTS — it is the *bonded-member*
mixer, armed only by the reconciler. **No fix needed; this is the foundation the
leader case extends.**

  > **Latent guard hazard — RESOLVED in [#925](https://github.com/jaspercurry/JTS/pull/925).**
  > The outputd TTS-mixer guard rejected `content_channels != 2`, but an active
  > 2-way speaker can *also* be 2-channel (woofer/tweeter, like `jts3` today), so
  > the guard would have *permitted* the outputd mixer on a 2-ch active sink —
  > where mixing post-crossover is full-range to the tweeter (unsafe). Option 3
  > already sidesteps this by construction (the reconciler never arms
  > `JASPER_OUTPUTD_TTS_SOCKET` on an active leader); the belt-and-suspenders fix
  > now also makes it structural: `JASPER_OUTPUTD_ACTIVE_LANE` (set by
  > `jasper-audio-hardware-reconcile` on a 2-ch active sink) teaches outputd that
  > the invariant is "full-range stereo L/R sink," not "exactly 2 channels," so
  > the TTS mixer, the rate-match bridge, and the dac_content lane all fail closed
  > on an active lane regardless of channel width.

**2. Leader-only voice — RATIFIED.** A bonded follower is voice/AEC-parked
(`JASPER_GROUPING_VOICE_PARK=1`, set for an active bonded follower in
[reconcile.py](../jasper/multiroom/reconcile.py)), and inv-A keeps TTS off the
stream — so "voice plays only from the leader" is already true. Ratified for v1:
the assistant "lives" on the leader (fine in one room). This removes any need to
stream or sync TTS, killing the round-trip-latency worry outright. It does **not**
by itself make the *leader's own* TTS tweeter-safe (the leader is active) — that
is Option 3.

**3. Latency — measured on `jts3`. None of the tweeter-safe options route TTS
through snapcast.**

| Path | TTS injected at | Incremental vs today's outputd mix | Tweeter-safe? |
|---|---|---|---|
| (a) outputd mix [today; **unsafe** on active] | outputd `OutputCore`, post-crossover | 0 — reference (TTS-to-glass ≈ DAC playout **63.7 ms**) | ✗ |
| (c) Option 2 — protective filter on the TTS lane at outputd | outputd, + one biquad | **< 1 ms** (biquad group delay; no added buffering) | ✓ but muffled, or re-impl crossover |
| (b) Option 3 — TTS into the crossover instance's input | camilla#2 input loopback (post-snapclient) | **+85–125 ms** (camilla chunk 21 ms + playback buffer 43 ms + content-bridge handoff ~63 ms; = the solo-active path) | ✓ |
| Option 1 — upstream of the bake [**rejected**] | fanin, pre-stream | **+~400 ms** (snapcast `buffer_ms` round-trip) | ✓ but laggy + streams TTS to follower (inv-A) |

Measured anchors (live `/state`, solo active, current buffering):
`dac.snd_pcm_delay_ms = 63.7`; `content_bridge.fill_frames ≈ 3026` (~63 ms,
rate_match); camilla `chunksize 1024` (21.3 ms) + `target_level 2048` (42.7 ms).
Option 3's delta is **bounded DSP latency** (not round-trip), tunable toward a
~21 ms (one-chunk) floor, and is exactly what a solo active speaker already pays
for its own voice today. Option 1's +400 ms is the snapcast playout
(`buffer_ms`, default 400, range 150–1500) — the disqualifier the other options
avoid because they inject TTS **downstream** of snapclient.

**Conversational-latency budget.** The band-limiting stage must add only **local
DSP latency** (bounded), never the synced-stream round-trip. Target:
leader/follower TTS-and-cue-to-glass ≈ the **solo-active** baseline (the
~85–125 ms of DSP+playout an active speaker already carries), with a working
ceiling of **≤ ~150 ms** for the band-limiting + playout stage. Option 2 (< 1 ms)
and Option 3 (+85–125 ms) both satisfy it; Option 1 (≥ 400 ms) does not. Because
the leader already buffers its own *music* at the round-trip depth and a solo
active speaker already accepts the camilla path for its *voice*, **Option 3
introduces no new latency class.**

**4. Leader mechanism — Option 3 (TTS into camilla#2's input loopback).** Chosen
over Option 2:
- **Safety / engine.** Reuses the **shipped, verified** per-driver crossover +
  the `classify_camilla_graph` re-proof; adds **no** safety-critical DSP to the
  reboot-on-fail Rust daemon. Option 2's tweeter-safe forms are either
  *skip-tweeter* (a low-pass → muffled < 2 kHz voice, bad UX — speech consonants
  live above the crossover) or *re-implement the per-driver crossover in outputd
  Rust* (exactly what the Option-B engine decision rejected, plus relaxing the
  2-ch outputd constraint to N-channel).
- **Fidelity.** Full-band voice through the real crossover, not muffled.
- **Unification (decisive).** The follower fail-closed cue (item 5) *must* inject
  upstream of Layer A regardless — so Option 3 makes leader-TTS and follower-cue
  **one** mechanism, validated once. Option 2 leaves the follower cue needing a
  separate camilla-input path anyway → two mechanisms to build and maintain.
- **outputd stays dumb** ("swap the engine, not the topology" / "no DSP in
  outputd" honored).
- **Cost.** +85–125 ms TTS latency (acceptable per the budget above) + a TTS mix
  point on the loopback feeding camilla#2 + the content-duck must follow that
  same point (so the reference still carries the ducked program, inv-A).

**5. Follower fail-closed cue — into the follower's camilla input (through Layer
A), follower-local.** The base safe state is **silence** (a starved loopback →
CamillaDSP emits silence *through* the crossover = silence = safe) — already the
Slice 3 reality (see "Fail-closed cue — v1 reality" above: the reconciler
oneshot can't play a cue, and the hard no-full-range guarantee holds via the
re-proven driver-domain baseline). This item resolves the **audible** cue Slice 3
deferred: inject it at the **same point as Option 3** — the camilla#2 input
loopback, upstream of Layer A — so it is band-limited (tweeter-safe) and
**follower-local (no round-trip)**. It must **not** be mixed at outputd
post-camilla (full-range to the tweeter; the 2-ch outputd mixer also assumes
L/R, not woofer/tweeter). Player ownership is a Slice 3/5 build detail:
`jasper-voice` is parked and the reconciler is a oneshot, so a follower-local
**long-running** writer — the grouping supervisor / `jasper-control`, which
already watches the stream for starvation — writes the cue WAV into the camilla
input; never `jasper-voice`, never the reconciler oneshot, never a post-camilla
mix.

## Open questions

1. **Active-leader CPU/thermal + the summer-build pick** (reframed from "RAM"
   per the 2026-06-20 external review) — RAM has big headroom; the binding limit
   is CPU jitter + Pi 5 thermal throttling under a sustained two-CamillaDSP +
   summer + server + client load. Measure sustained CPU + temp + xruns on `jts3`
   (active cooling) before Slice 5. **The same measurement resolves the open
   summer-build pick** (a second `jasper-outputd` instance vs a lean
   pipe-writing summer — see "Stage B — the ratified active-leader realization");
   lean-first unless RAM headroom or rate-match quality forces the two-instance
   build.
2. **Active-leader TTS band-limiting** — ✅ **RESOLVED (2026-06-20 design /
   2026-06-21 realization).** Leader-only voice ratified; leader TTS uses
   **Option 3** (summed pre-crossover into the single matcher, through Layer A);
   follower fail-closed cue uses the same injection point; measured deltas
   (Option 2 < 1 ms, Option 3 +85–125 ms, the rejected round-trip +400 ms) are in
   "Decision (ratified 2026-06-20)". The 2026-06-21 realization pins the
   *mechanism*: one rate loop (the summer), camilla#2 `rate_adjust` OFF, and the
   **two-outputd-instance constraint** (summer upstream, reference publisher
   downstream — never merge) in "Stage B — the ratified active-leader
   realization". Remaining for Slice 5 is the *build + on-device validation*, not
   any design choice.
3. **Mixed-bond latency** — an active follower (camilla latency) + a dumb
   follower (bare ChannelPick) in one bond: confirm `--latency` nulls the
   delta to within the sync target.
4. **Wireless sub host** — receiver-side low-pass (brainy sub: reuses the
   follower path, one shared stream, no extra leader work) vs sender-side
   leader-pre-bake (dumb cheap sub: a second leader bake + a separate
   loosely-synced sub stream, but no CamillaDSP on the sub). Driven by the
   target sub hardware; both reuse `channel_split.py`'s sub fragment.
   Decide in 6b — no need to lock it before the follower core ships.

Last verified: 2026-06-23 (dumb receiver-side wireless sub [gap 5] landed
HW-free — `jasper-outputd` `ChannelPick::Sub` LR4 low-pass + `crossover_hz`
SSOT [config/validate/`/grouping/set`/reconcile/state/doctor] + `/rooms/` sub
role + `/sound/` CTA, all behind tests [869 Python + Rust scratch-crate + JS
harnesses]; adversarially reviewed ship/no-must-fix; on-device `jts`→`jts4`
validation in progress. Bass-management mains-HP and the brainy/active sub
remain. Prior 2026-06-21: Stage-B active-leader realization ratified — one rate
loop = `outputd-summer`, camilla#2 `rate_adjust` OFF, the **two `jasper-outputd`
instances kept separate for inv-A** [summer upstream / reference publisher
downstream — never merge], TTS as a soft input, File-capture frees pair 6,
camilla#1 program-bake verifier exemption keyed on `playback.type == File`,
pre-registered clock-lock soak signatures, open summer-build pick resolved by the
`jts3` measurement; see "Stage B — the ratified active-leader realization" +
"On-device status (2026-06-21)". Stage A (active *follower*, Slice 3) PASSED on
`jts3`/`jts.local`; Stage C matched pair hardware-blocked. Same-day: Q2 spike
ratified — solo-active TTS confirmed tweeter-safe + in-reference on `jts3` (not a
gap); leader-only voice ratified; leader TTS = Option 3; latency measured on
`jts3` (Option 2 < 1 ms, Option 3 +85–125 ms, rejected round-trip +400 ms). Prior
(2026-06-20): Slice 3 active-follower code landed — driver_domain apply seam,
jasper.multiroom.follower_config, reconciler active-follower branch + readiness
gate/fail-safe-to-solo, outputd fence pin, /state endpoint surface; on-device
validation on jts.local-leader + jts3-active-follower owed. External design
review folded in — S0-sync gate, CPU/thermal reframe, clock-master /
chunksize≥1024 / no-SIGHUP pins)
