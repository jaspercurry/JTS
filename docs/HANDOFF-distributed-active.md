# Handoff: distributed active crossover (active speaker across a wireless pair)

> **Status: design-of-record (ratified 2026-06-20; hardware-gated slices
> pending).** Architecture, engine, slice plan, and v1 scope are ratified —
> **v1 gates on the matched-pair leader**; on-device validation on `jts3`
> remains. This is the canonical design + slice plan for running an active
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
  simply never threads it. Slice 1 is a thread-through — default
  unchanged, golden byte-identical.
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

The fix makes the promise true: explicitly **allow** the active-speaker
commissioning/crossover endpoints on a follower (local driver work), keep
content-DSP POSTs 409'd, and **render the local driver/crossover UI** on a
follower's `/sound/` instead of only the delegation card. Combined with
the follower audio path above, "local crossover stays with the DAC owner"
becomes literally true.

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

## Subwoofer — two different "subs" (gaps 4 & 5)

These are conflated in shorthand but are distinct designs:

- **Local sub driver (gap 4):** a sub that is one of a *single* box's N
  drivers. The active compiler hard-blocks it
  (`baseline_subwoofer_not_supported`,
  [baseline_profile.py](../jasper/active_speaker/baseline_profile.py);
  `subwoofer_staging_not_supported`,
  [staging.py](../jasper/active_speaker/staging.py)) even though the data
  model and protection bounds exist
  ([driver_protection.py](../jasper/active_speaker/driver_protection.py),
  50 Hz / 300 ms). Fix = add the sub lane (LR4 low-pass) to the compiler.
  **Orthogonal to wireless — a solo-active win.** Its own slice.
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
  Today `'sub'` → `ChannelPick::Mono` (full-range, no LF crossover) — the
  fence either path lifts.

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
| **3** | ✅ | Reconciler wires the active **follower** (capture→loopback, disable outputd pick, fail-closed + cue); lift `non_single_alsa_sink`. **Gated by S0-sync.** Pin clock-master / chunksize≥1024 / no-SIGHUP-during-playback | **yes** (2 Pis) |
| **4** | ✅ | Narrow follower-409 + render local driver UI on a follower's `/sound/`; make the delegation promise true | no |
| **5** | ✅ | Active **leader** (2nd light CamillaDSP; TTS band-limiting; inv-B-through-Layer-A) — **the v1 gate**; **CPU/thermal** + TTS measured on `jts3` (active cooling assumed) | yes |
| **6a** | — | Local sub driver — unblock `baseline_subwoofer_not_supported` (solo-active) | mixed |
| **6b** | — | Wireless sub member + bass management (receiver-side LP/HP, unified leader config) | yes |

Slices 1–2 are hardware-free and independently shippable; 3 is where
on-device begins; **5 is the v1 gate** (matched pair proven on hardware).

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
  follower goes **silent + cue**, never full-range.
- **Self-recovery:** un-bond → follower self-recovers to solo active.

**Slice 5 (the v1 gate) adds the matched-pair gates** — two *active*
speakers, one as leader:

- **Leader CPU/thermal (the real limit, not RAM):** two CamillaDSP (bake +
  driver crossover) + snapserver + snapclient on a 1 GB Pi 5 — RAM has big
  headroom, so gate on **sustained CPU < ~70%, zero xruns over 2 h, and no
  thermal throttling** (the uncooled Pi 5 drops 2.4→1.5 GHz — **active cooling
  assumed**). Capture real `htop`/temp under load on `jts3`.
- **Leader TTS:** "Hey Jarvis" replies on the leader reach the tweeter
  **band-limited** (routed through Layer A or HP'd at the outputd mix) —
  no full-range speech to the tweeter.
- **Matched-pair sync + safety:** both active speakers hold p99 < 5 ms and
  pass the no-full-range re-proof; leader self-loop stall → inv-B falls
  back to direct fan-in **through Layer A** (cue + `/state`), never silent
  and never full-range.

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

> **Status: open — this spike GATES Slice 5 (and informs Slice 3's
> fail-closed cue). It does NOT gate the follower core (Slices 1–4): a
> follower is voice/TTS-parked, so it has no TTS to band-limit. Direction
> leaning (owner, 2026-06-20): ratify *leader-only voice* (already de-facto
> true) + the cheapest tweeter-safe LOCAL band-limit. The snapcast round-trip
> is NOT in the tweeter-safe options — measure the DSP-only delta on `jts3`.**

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

**The spike — answer in this order, then write the decision back here:**

1. **Solo-active TTS today** *(HW-light)* — trace the TTS transport on a solo
   active speaker: does it route fan-in → CamillaDSP (Layer A) → outputd
   (band-limited, in-reference), or is the 2ch-only constraint silently dropping
   it? Fix if broken. This is the foundation for everything below.
2. **Ratify leader-only voice** *(product)* — follower stays voice-and-TTS-parked.
3. **Measure the latency delta on `jts3`** for (a) today's outputd mix
   [baseline, unsafe], (b) Option 3 camilla#2-input [+crossover chunk],
   (c) Option 2 outputd protective filter [+~one biquad]. Confirm none routes
   TTS through the snapcast round-trip. Set the conversational-latency budget.
4. **Pick the leader mechanism** — Option 3 (verified crossover, +chunk, dumb
   outputd) vs Option 2 (cheapest latency but muffled-or-reimplemented). Weigh
   fidelity vs latency vs "no DSP in outputd."
5. **Resolve the follower fail-closed cue** — same injection-point class, but
   follower-local (no round-trip), so the solo answer from (1) applies: the
   cue must pass through the follower's Layer A, not be injected post-camilla.
6. **Ratify** — replace this "open" status with the decision; update Slice 5
   (and Slice 3's cue handling) scope before building.

## Open questions

1. **Active-leader CPU/thermal** (reframed from "RAM" per the 2026-06-20
   external review) — RAM has big headroom; the binding limit is CPU jitter +
   Pi 5 thermal throttling under a sustained two-CamillaDSP + server + client
   load. Measure sustained CPU + temp + xruns on `jts3` (active cooling) before
   Slice 5.
2. **Active-leader TTS band-limiting** — gates Slice 5. Full analysis, the
   P1/P2/P3 trilemma, the four options, and the ordered spike checklist are in
   the **"Q2 spike"** section above. Direction leaning: ratify leader-only voice
   + the cheapest tweeter-safe local band-limit; the snapcast round-trip is NOT
   in the tweeter-safe options (it's DSP latency, not round-trip latency).
3. **Mixed-bond latency** — an active follower (camilla latency) + a dumb
   follower (bare ChannelPick) in one bond: confirm `--latency` nulls the
   delta to within the sync target.
4. **Wireless sub host** — receiver-side low-pass (brainy sub: reuses the
   follower path, one shared stream, no extra leader work) vs sender-side
   leader-pre-bake (dumb cheap sub: a second leader bake + a separate
   loosely-synced sub stream, but no CamillaDSP on the sub). Driven by the
   target sub hardware; both reuse `channel_split.py`'s sub fragment.
   Decide in 6b — no need to lock it before the follower core ships.

Last verified: 2026-06-20 (external design review folded in: S0-sync gate,
CPU/thermal reframe, clock-master / chunksize≥1024 / no-SIGHUP pins)
