# Handoff: distributed active crossover (active speaker across a wireless pair)

> **Status: partly built.** The active **follower** (Slices 1–4) is landed and
> passed on-device; the active **leader**'s HW-free arm (Stage B Step 0) is
> built but has had no on-device bring-up; the matched pair (Stage C) is
> hardware-blocked on a second commissioned active speaker. This doc owns the
> **distributed-active boundary**: how an active speaker's driver-domain
> crossover (Layer A) runs across a wireless pair. Read alongside — do not
> restate — [HANDOFF-dsp-graph-carrier.md](HANDOFF-dsp-graph-carrier.md) (the
> program/driver split-mixer seam),
> [HANDOFF-active-speaker-dsp.md](HANDOFF-active-speaker-dsp.md) (Layer A/B/C
> + commissioning), and [HANDOFF-multiroom.md](HANDOFF-multiroom.md)
> (Snapcast transport, leader/follower role-state, inv-A/inv-B).
>
> The decisions this doc used to argue are ADRs:
> [0122](adr/0122-an-endpoints-layer-a-crossover-runs-in-camilladsp-not-outputd.md)
> (the endpoint crossover runs in CamillaDSP, not outputd),
> [0123](adr/0123-an-active-leaders-crossover-runs-after-the-round-trip.md)
> (an active leader runs two CamillaDSP),
> [0124](adr/0124-one-rate-loop-and-the-summer-never-merges-with-the-reference-publisher.md)
> (one rate loop; never merge the two outputd instances),
> [0125](adr/0125-leader-tts-and-the-follower-cue-inject-pre-crossover.md)
> (leader TTS and the follower cue inject pre-crossover),
> [0126](adr/0126-a-subwoofer-crossover-executes-on-the-receiver.md) (a sub's
> crossover executes on the receiver). The June 2026 bring-up evidence —
> S0-sync bench, Stage A/B on-device runs, the TTS latency measurements — is
> [historical/distributed-active-bringup-2026-06.md](historical/distributed-active-bringup-2026-06.md).

## Why this exists

Two capabilities could not be combined, and the combination is
**hardware-safety-critical**: a wireless stereo pair (leader bakes +
streams; follower receives + plays) and an active multi-driver speaker whose
CamillaDSP band-limits each driver. A full-range feed to a tweeter destroys
it, so an active speaker used to refuse to bond, fail-closed — the graph
carrier rejects a roleful active graph (`eq_on_active_bonded_member`,
[graph_carrier.py](../jasper/sound/graph_carrier.py)) and outputd's
round-trip lane refuses a non-`SingleAlsa` sink. Those fences were correct: a
bonded follower had no Layer-A path. This work builds the capability they
stood in for.

**The active leader is v1, not flagship-only** (owner, 2026-06-21): the
household runs multiple active speakers and needs any speaker to work as
either role. Only *validating* the two-active matched pair is
hardware-gated; the active-leader mechanism ships and validates against an
active-leader + passive-follower rig.

## The seam — relocate the split mixer, don't re-architect the chain

A JTS speaker is one signal chain across two channel domains separated by the
**split mixer**: the **program domain** (1–2 ch — Layer B room PEQ, Layer C
preference EQ, headroom trim) and the **driver domain** (N ch — Layer A's
`2→N` split, per-driver crossover/delay/gain/limiter, the tweeter
band-limiting high-pass, the `0 dB` ceiling).

Solo, all layers run in one CamillaDSP graph. In a pair the leader owns the
program domain and streams the corrected **2-channel** program; the follower
owns the driver domain locally. **Only 2 channels ever cross the wire**; the
N driver channels never leave the box that owns the DACs. We move *where
Layer A runs*, not the chain shape.

## Roles & capture contract

Bond role is **runtime** (`grouping.env`), exactly as `member_camilla_kwargs`
([member_config.py](../jasper/multiroom/member_config.py)) already resolves
the leader's pipe sink per role. So the commissioned artifact stays the
**driver-domain description** (crossover points, per-driver gain/delay/
limiter, tweeter HP — hardware truth, role-independent), and the reconciler
resolves capture device + domain mode per current role:

| Role | Capture | Layers emitted |
|---|---|---|
| Solo | `jts_ring_capture` (Ring A, fan-in) | B/C + A in one graph |
| Follower | the grouping ring (snapclient-fed) | **A only** (+ channel-select prefix; the leader baked B/C) |
| Leader (active) | camilla#1: fan-in → B/C → pipe; camilla#2: the grouping ring → A | split across two instances |

The compile/apply seam (`build_baseline_profile_candidate`,
`apply_baseline_profile`,
[baseline_profile.py](../jasper/active_speaker/baseline_profile.py)) threads
`capture_device` into `emit_active_speaker_baseline_config`; the default
resolves to Ring A on a solo box. `recompose_baseline_yaml` — the program-domain
Layer-C re-emit — takes no capture parameter at all, because it only ever
runs on the fan-in-fed program domain. `OutputTopology`
([output_topology.py](../jasper/output_topology.py)) carries a pure-data
`pairing_intent` (`solo | will_be_follower | has_follower`, absent == `solo`)
that records design intent and seeds reconciler defaults; the reconciler
keeps the final runtime say.

## The active follower — landed

`emit_active_speaker_driver_domain_config`
([camilla_yaml.py](../jasper/active_speaker/camilla_yaml.py)) composes
`channel_select (2→2 pick L/R/mono) → split_active_<way>way (2→N) →
per-driver [crossover, delay, non-positive gain, soft-clip limiter]`, with
**no** program headroom and **no** preference EQ. Channel-select runs FIRST
(inter-speaker axis), then the crossover splits (intra-speaker axis), and it
reuses the shared `jasper.camilla_emit.emit_channel_select_mixer` so the
follower and the member-config path cannot drift. `classify_camilla_graph`
([runtime_contract.py](../jasper/active_speaker/runtime_contract.py))
re-proves it through a `GRAPH_DRIVER_DOMAIN_BASELINE` arm keyed on the
emitter's `# Source:` marker: Layer A present (crossover HP + per-driver
limiter `clip ≤ 0` + gain `≤ 0` + `volume_limit == 0.0`), channel-select
present **and preceding the split**, program prefix absent.

[follower_config.py](../jasper/multiroom/follower_config.py) is the apply/
restore arm (mirroring `leader_config`): it builds and re-proves the
driver-domain config, swaps CamillaDSP glitch-free (snapclient writes the
grouping ring and this box's CamillaDSP captures the same PCM — one name, one
wire, owned by [grouping_ring.py](../jasper/multiroom/grouping_ring.py)),
stashes the prior solo-active config, and on unbond restores the **active**
baseline, never a passive graph.

The reconciler ([reconcile.py](../jasper/multiroom/reconcile.py)) detects an
active box (`is_active_speaker_box`), routes snapclient to the grouping ring
(ALSA player, not the dumb FIFO), **disables outputd's `dac_content`
ChannelPick** on this box, and runs a readiness GATE before tearing down the
solo path — a follower that cannot be made safe **fails safe to solo
active**. That includes the emit-gate refusal: if the emitter refuses an
unprotected-tweeter graph, the precheck converts it to
`ActiveFollowerError` / `ActiveLeaderError` (`driver_domain_emit_refused`) so
the reconciler's fail-safe-to-solo path catches it instead of the oneshot
crashing. `/state` carries an `endpoint` block (`active_crossover` |
`blocked` + reason).

**Web (Slice 4).** The content-DSP POST block
`_FOLLOWER_BLOCKED_CONTENT_DSP_POSTS`
([sound_setup.py](../jasper/web/sound_setup.py)) is narrow by design and
never contained the active-speaker endpoints. A bonded follower's `/sound/`
now mounts the same active-speaker setup UI a solo box renders, in *follower
mode* — the local driver/crossover/commissioning surface only, without the
content-EQ editor and now-playing plot, which stay the leader's job. So the
delegation card's promise ("local crossover and driver-protection work stays
with the speaker that owns the DAC path") is literally true at the UI, and
with the follower audio path above it is true end-to-end.

## The active leader — Stage B Step 0 landed, on-device owed

`emit_active_speaker_program_bake_config`
([camilla_yaml.py](../jasper/active_speaker/camilla_yaml.py)) emits camilla#1's
PROGRAM domain only (Layer B/C + headroom, `File`→`SNAPFIFO`,
`enable_rate_adjust: false`, **no** Layer A) by reusing
`jasper.sound.camilla_yaml.emit_sound_config`'s program assembly verbatim
under a distinct `# Source:` marker. `classify_camilla_graph` carries the
`GRAPH_PROGRAM_BAKE_PIPE` arm for it (ADR-0123): keyed strictly on
`devices.playback.type == File` via the shared
[`playback_is_pipe`](../jasper/multiroom/leader_config.py) parser, not
selectable as a solo speaker's own graph. After the bake is loaded the graph
carrier recognizes it as `active_leader_program_bake` and may re-emit
program-domain EQ/correction only while it still resolves to the Snapcast
pipe sink.

[active_leader_config.py](../jasper/multiroom/active_leader_config.py) plus
the reconciler's active-leader branch arm the two-instance bring-up on bond,
in this order: **disable camilla#2 → bake camilla#1 → re-seed
`crossover-statefile.yml` with the re-proven driver-domain graph → prove the
active-content PCM released → start camilla#2.** Re-seeding on *every*
reconcile matters for trim-only updates such as pair balance: the updated
graph is picked up by process start, not by trusting an idempotent
`systemctl enable --now` to reload a running crossover. camilla#2 currently
runs `rate_adjust` **ON** — the validated active-follower seam, no
`outputd-summer`, no leader TTS (ADR-0124 step 1).

Fail-closed, layered by three real incidents (the narrative is in the
historical appendix; these are the live rules):

- The precheck refuses the bond if `snapserver`/`snapclient` are not
  installed (`snapcast_unavailable`), and the reconciler does not bake unless
  snapserver is actually active — never bake camilla#1 onto a reader-less
  pipe.
- **camilla#2 is armed only if the bake succeeded**, i.e. camilla#1 has
  provably moved off the DAC to the wire. A second CamillaDSP must never take
  the DAC until the always-on camilla#1 has provably released it.
- The release signal is a **non-blocking exclusive `flock` on the ACTIVE
  ring's writer lock** — free means released, a live writer means `busy`, an
  absent or unopenable lock means `unknown`
  (`_probe_active_content_pcm_once`, [reconcile.py](../jasper/multiroom/reconcile.py)).
  `busy` and `unknown` **both** fail closed to solo-active, under
  `active_content_pcm_busy` and `active_content_pcm_unverified`; nothing arms
  on `unknown`, and the probe never creates the lock file.
- Unknown topology or unreadable manager/D-Bus state preserves the existing
  runtime graph and surfaces `active_speaker_topology_unknown` /
  `crossover_ownership_state_unknown`. After a teardown failure or unproven
  inactivity the reconciler performs no further role-derived mutation and
  never restores camilla#1; it reports a persistent blocked reason.
- Outputd's env writer proves the **paired** statefiles: `program_bake_pipe`
  is never an outputd endpoint by itself: it is a sentinel that makes the
  reconciler prove camilla#2 is a legal `driver_domain_baseline` on the
  active lane with `2 ≤ playback_channels ≤ active_lane_cap`. Failure
  recovery reads the same pair, so a deploy or restart cannot downgrade
  `outputd.env` while grouped active lanes are owned.
- On unbond camilla#2 is disabled and camilla#1 restored to the re-proven
  solo-active baseline (never passive, via
  `follower_config.restore_active_camilla_solo`). The discriminator uses both
  camilla#2's enabled intent and live activity; explicit-disabled plus
  inactive selects the active-**follower** restore path instead.

`/state.grouping.endpoint` surfaces `mode=active_crossover, role=leader` (or
`mode=blocked` + reason).

**Owed on-device (Steps 1–3):** the `jts3` bring-up + CPU/thermal gate (which
also resolves ADR-0124's open summer-build pick), then swap in
`outputd-summer` + camilla#2 `rate_adjust` OFF + the ≥24 h clock-lock soak
against ADR-0124's pre-registered signatures, then arm TTS + the follower
fail-closed cue. One Layer-B caveat to check: the camilla#1 bake passes
`room_peqs=[]` (an active baseline carries no Layer B today — only Layer C +
headroom), so confirm followers hear the same correction the leader applies
solo.

## Subwoofer — two different "subs"

- **Local sub driver (landed 2026-06-23)** — a sub on a *single* box's spare
  DAC output; orthogonal to wireless, a solo-active win. The active
  multi-output emitter emits a sub lane (clip-safe L+R mono sum → LR4
  low-pass → gain ≤ 0 → soft-clip limiter, `driver_protection` 50 Hz/300 ms)
  plus the complementary mains high-pass at the same Fc, for active mains and
  for a degenerate 1-way passive main. The re-proof
  ([graph_safety.py](../jasper/active_speaker/graph_safety.py):
  `sub_guard_present` + `mains_highpass_present` +
  `bass_management_corner_matched`, demanded by `runtime_contract.py`) is the
  matched safety net; the sub starts muted in staging and is structurally
  excluded from the audible-target resolver. A subless passive speaker is
  byte-identical. **On-device acoustic validation owed** (needs a
  commissioned ≥3-output DAC).
- **Wireless sub member (landed 2026-06-23 for the dumb path)** — see
  ADR-0126. `ChannelPick::Sub(corner)` in
  [dac_content.rs](../rust/jasper-outputd/src/dac_content.rs) mono-sums
  clip-safe then applies an LR4 low-pass; passive mains apply the LR4
  high-pass at the same corner from `JASPER_OUTPUTD_DAC_CONTENT_HP_HZ`. The
  corner is `GroupingConfig.crossover_hz` (`/rooms/`-set,
  `JASPER_GROUPING_CROSSOVER_HZ`, default 80 Hz, range 40–200); the
  reconciler forwards it as `JASPER_OUTPUTD_DAC_CONTENT_SUB_HZ` only for a
  `sub` member and clears the mains HP whenever there is no sub, the toggle
  is off, the member is the sub, or the member is an active endpoint.
  **Remaining:** the brainy/active-endpoint sub path, where Layer-A
  CamillaDSP owns the HP/LP rather than outputd `dac_content`.

## Clock domain + fail-closed (cross-cutting safety)

- **Never full-range to a tweeter — graph-resident protection.** The
  follower's loaded graph is *always* the re-proven driver-domain baseline;
  only the capture *source* varies. No capture content — stream, silence, or
  garbage — can produce a full-range driver feed. This is the
  active-crossover analogue of inv-1 and is strictly safer than the
  dumb-follower path.
- **Stream stall → silence, not full-range.** A capture underrun makes
  CamillaDSP emit silence *through* Layer A, and silence through a crossover
  is silence. Surface a cue ([cues/registry.py](../jasper/cues/registry.py))
  + a `/state` flag + dashboard card.
- **Fail-closed cue — v1 reality.** The reconciler is a oneshot that cannot
  play a cue (no `AudioCueManager`; a follower is voice-parked), so the v1
  signal is the solo-active fallback (the box keeps playing its own content —
  not silent) plus `/state.endpoint.blocked_reason`, doctor, and the
  `event=multiroom.reconcile.active_follower_blocked` log. The **audible**
  cue is ADR-0125's injection point and does not gate the follower core.
- **Self-recovery.** Unplug, brief WiFi loss, power cycle: unbond → the
  follower returns to solo active and plays local content; no silent restart
  loop. The reconciler owns the transition.
- **Clock domains.** snapclient stuffs to the server clock; camilla
  rate-tracks its capture only; outputd's DAC paces. What camilla#2's own
  knobs must be is ADR-0124; the ring's wire and slot geometry are
  `grouping_ring.py`'s. snapclient `--latency` is the knob that nulls
  camilla's fixed pipeline latency so an active follower stays sample-locked
  with a dumb follower. It ships compensating **nothing**
  (`DEFAULT_CLIENT_LATENCY_MS = 0`, `jasper.multiroom.config`) — no
  measurement has produced an offset that generalises across DACs, so a bond
  is *given* the nulling, it does not inherit it. The knob only holds if that
  latency is truly constant: **forbid SIGHUP config reloads during playback**
  on the crossover instance, and validate the nulling acoustically, never
  from the nominal `--latency` number alone.
- **Physical tweeter protection (hardware high-pass, amp mute-on-fault) is
  owner-handled offline and OUT OF SCOPE** — do not add it as a code
  requirement.

## Multi-Pi validation

The S0-sync de-risk gate (`snapclient → loopback → crossover-only CamillaDSP
→ DAC`; acceptance p99 inter-speaker offset < 5 ms over 2 h, no audible
resync, ≥24 h xrun soak) was benched on **snd-aloop** before Slice 3 and
passed on a telemetry basis the owner accepted;
[`scripts/s0-sync-bench.sh`](../scripts/s0-sync-bench.sh) +
[`scripts/s0-sync-measure.py`](../scripts/s0-sync-measure.py) are that
harness. **The bonded ingress has since moved to the grouping ring, which
exposes no `PCM Rate Shift`, so neither the bench nor its clock-lock evidence
transfers — a ring-seam de-risk is owed.** Two confirmations were also never
closed: the ≥24 h durability xrun soak, and the acoustic end-to-end p99,
which needs one mic placed *between* two co-located speakers (each onboard
mic hears only its own speaker). Full record in the historical appendix.

**Stage A — active follower: PASSED on-device (2026-06-21)**, `jts.local`
leader → `jts3` follower: live re-proof `allowed=True`, `rate_adjust` spread
8–23 ppm, 0 xruns, self-recovery verified; acoustic p99 still owed.

**Stage B — active leader: Step 0 built, Steps 1–3 owed.** Rig will be `jts3`
(active leader, real drivers) → `jts.local` (passive follower). Gates:
sustained CPU < ~70 %, zero xruns over 2 h, no thermal throttling (the
uncooled Pi 5 drops 2.4→1.5 GHz — **active cooling assumed**); ADR-0124's
clock-lock soak signatures; band-limited TTS to the tweeter with
TTS-to-glass ≈ the solo-active baseline, not the round-trip; and inv-B
falling back through Layer A, never silent and never full-range.

**Stage C — matched pair: BLOCKED.** Needs a second commissioned active
speaker with real drivers; only `jts3` qualifies today (`jts5` is a
dual-Apple-DAC bench box with no real drivers). Both boxes holding p99 < 5 ms
with the no-full-range re-proof is the remaining v1 gate after Stage B.

## Invariants → tests

| # | Invariant | Pinned by |
|---|---|---|
| 1 | Threading `capture_device` with the default reproduces the baseline **byte-for-byte** | `tests/test_active_speaker_baseline_profile.py` |
| 2 | `OutputTopology` pairing field round-trips and defaults to `solo`; absent field == `solo` | `tests/test_output_topology.py` |
| 3 | **Keystone:** the driver-domain-only emit, fed back through `classify_camilla_graph`, classifies `allowed=True` — relocating Layer A never breaks the contract | `tests/test_active_speaker_runtime_contract.py` |
| 4 | The driver-domain-only graph has **no** program-prefix filters and **no** positive gains; `volume_limit == 0.0`; channel-select precedes the split | `tests/test_active_speaker_driver_domain.py` |
| 5 | A follower whose driver-only graph cannot be re-proven **refuses to bond / fails to silence** | `tests/test_multiroom_follower_config.py`, `tests/test_multiroom_reconcile.py`, `tests/test_multiroom_state.py` |
| 6 | Active-speaker crossover endpoints return 200 on a follower; content-DSP POSTs still 409 | `tests/test_sound_setup.py` |
| 7 | Solo-impact: feature off → byte-identical configs, no new daemon construction | all of the above |

The active-leader arm adds `tests/test_multiroom_active_leader_config.py`,
`tests/test_active_speaker_program_bake.py` and the active-leader flows in
`tests/test_multiroom_reconcile.py` (including that a blocked reconcile
performs no args/env/unit-plan transition and no solo restore).

## File map

- Roles/capture: [output_topology.py](../jasper/output_topology.py),
  [baseline_profile.py](../jasper/active_speaker/baseline_profile.py),
  [camilla_yaml.py](../jasper/active_speaker/camilla_yaml.py)
- Emit + verifier: `camilla_yaml.py`,
  [runtime_contract.py](../jasper/active_speaker/runtime_contract.py),
  [graph_evidence.py](../jasper/active_speaker/graph_evidence.py),
  [graph_safety.py](../jasper/active_speaker/graph_safety.py)
- Bond wiring: [reconcile.py](../jasper/multiroom/reconcile.py),
  [follower_config.py](../jasper/multiroom/follower_config.py),
  [active_leader_config.py](../jasper/multiroom/active_leader_config.py),
  [member_config.py](../jasper/multiroom/member_config.py),
  [grouping_ring.py](../jasper/multiroom/grouping_ring.py)
- outputd lane: [dac_content.rs](../rust/jasper-outputd/src/dac_content.rs),
  [config.rs](../rust/jasper-outputd/src/config.rs)
- Web: [sound_setup.py](../jasper/web/sound_setup.py)

## Open questions

1. **Active-leader CPU/thermal + the summer-build pick.** RAM has headroom;
   the binding limit is CPU jitter and Pi 5 thermal throttling under a
   sustained two-CamillaDSP + summer + server + client load. The same `jts3`
   measurement resolves ADR-0124's open summer-build pick — lean-first unless
   RAM headroom or rate-match quality forces the two-instance build.
2. **Mixed-bond latency.** An active follower (camilla latency) plus a dumb
   follower (bare `ChannelPick`) in one bond: confirm `--latency` nulls the
   delta to within the sync target.
3. **The ring-seam de-risk** that the snd-aloop S0-sync bench no longer
   covers, and the two confirmations it never closed (≥24 h xrun soak,
   acoustic p99 with a between-speakers mic).

Last verified: 2026-08-26 (spine trim: every kept claim re-read against
`jasper/multiroom/{reconcile,follower_config,active_leader_config,config,
grouping_ring}.py`, `jasper/active_speaker/{camilla_yaml,runtime_contract,
baseline_profile,graph_safety}.py`, `jasper/output_topology.py`,
`jasper/web/sound_setup.py`, `rust/jasper-outputd/src/{dac_content,config}.rs`
and `deploy/systemd/jasper-camilla-crossover.service`. `outputd-summer` is
confirmed **unbuilt** — it appears only in docstrings — and
`rust/jasper-outputd/src/content_bridge.rs` is confirmed **deleted**. The
one-rate-loop ring statement in ADR-0124 is core-verified against
`c/jts-ring-ioplug/jts_ring_shm.h` only; its hardware pass is still owed.
Prior 2026-08-20: the grouping-ring cutover (bonded ingress off snd-aloop
pair 6). Prior 2026-08-15: the active-leader release signal became a
non-blocking exclusive `flock` on the ACTIVE ring's writer lock — its
2026-06-24 `jts3` on-device validation exercised the earlier procfs signal
and has NOT been re-run against the flock barrier.)
