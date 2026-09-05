# Multiroom pairing reliability: investigation, architecture, and delivery plan

> **Status: rescued plan, not yet executed.** Extracted verbatim from draft
> PR #1851's branch (`claude/multiroom-pairing-reliability-v2`) on
> 2026-08-08 — the plan existed only on that branch, which never merged and
> is being closed with a pointer to this file. WO-1 (this document's own
> prescribed "branch correctness pass," deploy-gating findings
> F-2/F-6/F-7/F-8) never landed on `main`. On the branch, only WO-1 item 10
> (F-22 — mapping this doc into `docs/doc-map.toml`'s `multiroom-grouping`
> entry) had landed; that item is OVERRIDDEN by this rescue, which
> classifies the doc under `document_classes.session_artifacts` instead, so
> the body's "already done" claim for item 10 does not hold on `main`. The
> branch was 235 commits behind `main` at rescue time. The PR's review
> record — four independent
> reviewers, covering a confirmed release blocker, missing receipt/
> fingerprint plumbing, four deploy-gating findings, and corrected Snapcast
> facts — lives on the closed PR, not here. Every fact below reflects the
> 2026-07-28 snapshot the branch was written against and MUST be
> re-verified against current `main` before any part of this plan is
> executed. Companion tracking issue: #1852.

> **Status: active implementation plan and investigation record (v2).** This
> document explains the intent, evidence, branch work, review findings, and
> the prescriptive delivery plan for reliable paired-speaker playback. It is
> not the authority for what a deployed speaker currently does. Once this
> campaign is complete, this file becomes the durable design and
> investigation record.
>
> **v2 provenance:** v1 of this plan was written alongside the branch work and
> reviewed by one independent SEVA agent. v2 (2026-07-28) is the result of a
> second, multi-agent validation pass: one reviewer re-verified every code
> claim against the rebased branch, one re-derived the delivery plan against
> the repo's ownership seams, one validated the latency model against
> Snapcast upstream source and docs (v0.31.0, the version trixie ships), and
> one re-ran the hardware-free verification on the rebased base. Everything
> they confirmed, refuted, or found missing is folded in below. The delivery
> plan is now a sequence of **work orders** (WO-1 … WO-10) prescriptive
> enough to implement without inventing design: each names its files, API
> shapes, tests, and acceptance criteria. Where this plan names a file,
> function, constant, or test, that is the prescription; a deviation needs a
> stated reason in the implementing PR's description.
>
> **Deployment boundary:** no deployment, service restart, or live
> configuration mutation was performed during the reviews that produced this
> plan. Hardware rollout requires a separate, explicit approval.

## Executive summary

The product goal is simple to state:

> Two compatible JTS speakers should pair in one action, play the intended
> channels in stable alignment, accept balance changes without interrupting
> playback, keep assistant speech local to the leader, recover from ordinary
> faults, and explain any state it cannot repair.

The implementation must keep that experience simple without making the
architecture simplistic. A paired speaker crosses several existing ownership
boundaries:

- the Rooms control plane owns requested group membership;
- the grouping reconciler owns structural role and Snapcast convergence;
- the applied active-speaker profile owns driver protection and crossover;
- CamillaDSP owns local DSP execution;
- Snapcast owns network transport, clock estimation, and dejitter;
- the fan-in coupling owner owns the solo-versus-group-compatible input route;
- the source coordinator owns source parking and restoration;
- outputd owns final playout and the electrical AEC reference.

The design succeeds only if those owners cooperate through small, explicit
contracts. It fails when grouping reconstructs another subsystem's policy,
when requested state is presented as effective state, or when a fast scalar
change falls back to a full structural rebuild.

The work on this branch corrects several important boundary violations:

1. Grouped active speakers now project their local driver graph from the
   immutable applied profile instead of rebuilding it from mutable setup data.
2. Grouping asks the existing fan-in coupling owner to converge rather than
   writing coupling state itself.
3. CamillaDSP live changes are proved through semantic readback.
4. The control daemon receives the exact lock-file permission needed for live
   balance changes.
5. Active endpoints no longer look unhealthy merely because the passive
   outputd FIFO is correctly inactive.
6. The primary pair flow writes the remote follower before the local leader and
   compensates a direct write failure.

Those foundations were re-verified line-by-line in the v2 review and hold.
The branch is still not ready to deploy. The confirmed release blocker: an
HTTP 2xx from `POST /grouping/set` means a speaker *accepted* a request, not
that it reached the requested audio state — pair creation can still report
success or leave a half-pair when a later reconcile step fails. The v2 review
additionally found that the receipt, fingerprint, and wire-contract plumbing
the fix depends on **does not exist yet**, that the branch ships a
count-based green badge its own plan forbids, that an upgrade/migration
phase was missing entirely, and that Snapcast's client identity and latency
seams need two specific corrections (`--hostID`, and picking exactly one of
the two additive latency inputs) before the identity and calibration work
can be durable. The work orders below close all of it without a generic
distributed-transaction framework.

## Owner constraints (binding)

These are the product owner's standing constraints for this campaign. Every
work order below was shaped by them; do not trade them away during
implementation:

1. **Reliability and resilience first.** A pair that is stable beats a pair
   that is fast. One request has one unambiguous outcome.
2. **Latency as low as reasonable — never below soak-proven stability.**
   Every retained buffer must answer "what failure does this prevent?"
3. **Snapcast owns network synchronization.** Clock estimation, dejitter,
   drift correction, and post-outage resync are the library's job — its
   client continuously corrects deviation (typically < 0.2 ms) by
   sample-level rate trimming plus hard resync gates, with no external help.
   We configure Snapcast (buffer, codec, chunk size, per-client latency,
   client identity); we never re-implement or second-guess its sync engine,
   and **JTS never restarts snapclient to repair sync** — the only
   sanctioned repairs are structural (unit down, wrong stream binding, no
   producer on the FIFO).
4. **Automatic recovery.** Every fault in the resilience contract either
   self-recovers when the resource returns or surfaces a precise, actionable
   state. No operator-intervention loops for ordinary faults.
5. **COAH proportionality.** The will-not-build list is binding. No saga
   engines, no new daemons, no browser-side health state machines.
6. **Prescriptive delivery.** The implementing agent must not have to invent
   file paths, API shapes, or sequencing.

## Truth model and ownership

Pairing becomes understandable when its different truths are named rather
than collapsed into one `enabled` boolean.

| Truth | Owner | Durable authority | What it means |
|---|---|---|---|
| Pair intent | Rooms / grouping control plane | `/var/lib/jasper/grouping.env` (single writer: `jasper/control/server.py::_write_grouping`) | What the household requested |
| Effective local role | Grouping reconciler | Boot- and request-qualified receipt at `/var/lib/jasper-grouping/effective-role.json` | What this speaker actually landed |
| Driver-domain DSP | Active-speaker Apply transaction | Immutable applied baseline profile | The local hardware-safe Layer-A graph |
| Fan-in route | Fan-in coupling reconciler | Reconciler-owned coupling state | Which input transport is effective |
| Source availability | Source coordinator | Desired source intent plus effective-role permission | Which local sources may run |
| Network synchronization | Snapcast | Live server/client state | Transport, clock estimate, and dejitter |
| Fixed endpoint compensation | Endpoint-latency receipt (WO-8) | `/var/lib/jasper-grouping/endpoint-latency.json`, consumed by the member's own reconciler into `snapclient --latency` | Stable local path difference passed to Snapcast |
| Snapclient identity | JTS peer identity via `--hostID` (WO-6) | `/var/lib/jasper/peer_id` | Stable client id for roster proof, latency persistence, and stale-row GC |
| Acoustic seat alignment | Room/balance/sync calibration | CamillaDSP delay in the calibration authority | Arrival-time difference caused by placement — never endpoint latency |
| Live pair balance | Grouping control plane plus effective DSP endpoint | Desired trim plus proved live value | A scalar level change, never a topology change |
| Household status | Backend state composition (`derive_grouping_verdict`, WO-6) | The authorities above | Honest presentation; the browser does not invent truth |

### Non-negotiable single-source-of-truth rules

1. `grouping.env` stores intent. It never proves that a role landed.
2. The effective-role receipt is accepted only when its request fingerprint
   and boot identity match the current request and boot, and only a
   **terminal** receipt (`outcome` of `landed` or `refused`) proves an
   outcome — a `converging` receipt proves only that the reconciler started.
3. The applied active-speaker profile is the only production source for local
   crossover and driver protection. Drafts and measurements are candidates
   until Apply.
4. Grouping may ask the coupling and source owners to converge. It may not
   write their state or reproduce their policy.
5. Endpoint latency has exactly one authority: the calibration receipt,
   consumed into `snapclient --latency` by the member's own reconciler.
   Snapcast's *other* latency input — the server-persisted per-client value
   behind `Client.SetLatency` — is **additive** with the CLI flag inside
   snapclient, so it must stay 0 forever: delete the unused
   `snapcast_rpc.set_client_latency` helper. The pre-existing
   `JASPER_GROUPING_CLIENT_LATENCY_MS` is demoted to an operator override
   (override beats receipt; `0` means no override) resolved in exactly one
   place. Companion rule: the `/sync/` acoustic seat calibration must never
   silently absorb endpoint latency — its CamillaDSP channel delay is a
   different truth with a 100 ms ceiling that could only ever partially
   absorb the endpoint offset.
6. The backend owns domain health. Browser JavaScript presents the verdict; it
   does not infer success from client counts.
7. Structural reconcile and scalar live control are different paths.
8. New keys on the `/grouping` and `/state.grouping` wire contracts are
   **additive**, and every consumer treats an absent key as `unknown` —
   never as success. A new-code speaker must interoperate with an old-code
   peer mid-deploy (WO-7).

## Investigation record

### 1. Initial grouping-readiness refusal

The first visible failure was:

> Couldn't pair — 192.168.1.74: speaker software does not provide grouping
> readiness — update both speakers, then retry

Version skew was a reasonable first suspicion, but the deeper architectural
problem was active-speaker readiness. A speaker already playing safely in solo
mode had an immutable applied crossover, yet grouped role construction could
consult mutable commissioning inputs again. That duplicated the meaning of
"ready" and allowed new or incomplete setup evidence to invalidate an already
safe, currently playing profile.

The correction is not to weaken hardware readiness. It is to use the right
authority: grouping projects the already-applied Layer-A profile and adds only
role-specific routing.

### 2. Pair formed but appeared degraded

JTS3 later reported:

> snapcast clients are connected but not all audible on the JTS stream

The investigation separated several signals that had previously been blended:

- requested role;
- effective active endpoint;
- expected systemd units;
- leader stream production;
- Snapcast client connection, stream binding, mute, and volume;
- passive outputd FIFO byte flow;
- actual clock/alignment proof.

An active endpoint correctly bypasses the passive `dac_content` FIFO because
its local crossover graph owns the final bonded path. Treating that absent FIFO
as starvation was a false degradation signal. The branch now marks that signal
not applicable for active endpoints.

The investigation also found that Snapserver can retain stale client rows. A
disconnected historical row with the local hostname must not overwrite a
connected row with the same name. The state reducer now treats local presence
as existential.

The v2 review located the **root cause** of the stale-row class, which the
reducer fix only defends against: snapclient's default identity is the MAC of
the *first non-loopback interface* (not the interface used to reach the
server), so a NIC add/remove/reorder or a reinstall mints a new client id;
and snapserver's `server.json` registry has **no pruning whatsoever** — rows
persist until an explicit `Server.DeleteClient`, which JTS never calls. WO-6
fixes the identity (`--hostID` from the JTS peer identity) and adds the GC.

### 3. Balance change caused delay and disruption

The balance slider is a scalar level control, but the observed failure path
became structural:

1. the desired pair trim was persisted;
2. the control daemon attempted a live CamillaDSP patch;
3. its systemd sandbox could not take the canonical DSP writer lock;
4. the failure triggered the full grouping reconciler;
5. the graph and services were torn down and rebuilt, producing an audible
   interruption and repeated roughly nine-second recovery cycles.

The branch grants `jasper-control.service` write access to exactly
`/var/lib/camilladsp/configs/.dsp_apply.lock`, not the surrounding
configuration directory. That fixes the observed permission failure while
preserving least privilege. (The v2 review found the grant itself needs a
`-` prefix — finding F-6.)

The escalation problem is precisely located: `apply_local_trim` in
[`jasper/multiroom/runtime_balance.py`](../jasper/multiroom/runtime_balance.py)
returns a typed result and never escalates; the unconditional escalation is
`jasper/control/server.py::_post_grouping_set`, which calls
`_kick_grouping_reconciler()` whenever `live_apply.applied` is false. WO-5
replaces that with a bounded, coalesced scalar retry and escalates only on
independent structural evidence.

### 4. Assistant speech played from every speaker

The hardware observation disproved current documentation that described
leader-local TTS as already landed for active grouped speakers. In the observed
path, leader TTS entered the shared program lane before the grouped bake, so it:

- traversed Snapcast;
- paid the group buffer;
- reached every member;
- remained driver-safe because each active speaker still applied local Layer A.

The safe low-latency target is a leader-local input to the leader's crossover
instance, after the Snapcast return but before local Layer A, followed by the
final output/AEC-reference publisher. This preserves:

- leader-only speech;
- local crossover and limiter safety;
- TTS-inclusive AEC reference;
- local DSP latency instead of the network buffer.

The previously ratified shape remains appropriate:

```text
grouped music: Snapcast return ─┐
                               ├─ local summer → active crossover → final outputd → DAC
leader TTS: local TTS socket ──┘                                      └─ AEC reference
```

This is a separate, hardware-soaked workstream (WO-10) because merging the
summer and final reference-publisher roles would break the output-reference
contract.

The v2 review found one stronger instance of this defect class the v1 plan
missed: `jasper/multiroom/tts_route.py::expected_grouping_tts_route` routes an
active **leader** (`voice_parked=False`) to the fan-in socket that feeds the
shared program bake, and `jasper/cli/doctor/grouping.py::check_grouping_tts_lane`
reports that state **ok** — the doctor certifies the observed defect as
healthy. WO-1 makes the doctor honest before any other TTS work.

### 5. Persistent fixed offset

The observed active paths had different final output queue depths:

| Endpoint | Observed queue | Approximate time at 48 kHz |
|---|---:|---:|
| JTS3 active path | 3,056 frames | 63.7 ms |
| JTS passive path | 512 frames | 10.7 ms |
| Difference of the final queues alone | 2,544 frames | 53.0 ms |

Both Snapcast clients used zero configured client latency. This is evidence
of a missing fixed-endpoint compensation mechanism — but the v2 review found
the 53 ms figure is a **lower bound from an incomplete method**, not the
offset: differencing only the final DAC queues omits the CamillaDSP pipeline
that only the active endpoint pays (chunk 1024 ≈ 21.3 ms + target level
2048 ≈ 42.7 ms + content-bridge handoff), and the content-bridge
fill is a live DLL-steered value, not a constant. The true active-vs-passive
fixed offset is plausibly ~115–130 ms. The receipt value must therefore come
from an **end-to-end measurement** (the correlation primitive in
[`jasper/multiroom/sync_measure.py`](../jasper/multiroom/sync_measure.py),
acoustic or loopback, both members on the same stream) — never from summing
or differencing internal queue gauges.

Snapcast continues to own network clock and jitter. Endpoint calibration
provides only the stable local offset through Snapcast's per-client latency
input (sign convention: positive latency makes that client play *earlier* —
it is subtracted from the client's share of the buffer). Room calibration
separately owns acoustic arrival at the chosen seat and must not absorb this
offset (SSOT rule 5's companion rule).

### 6. What the status surface can and cannot prove

Snapcast's server state (`Server.GetStatus`) proves useful facts: connection,
stream binding, mute, volume, and the *configured* latency echoed back. It
does not — in any version through 0.35.0 — expose a measured sync error,
follower buffer fill, drift, or acoustic lock. Snapclient *measures* its
deviation continuously (the `Chunk:` stats lines in its own journal, DEBUG
level, positional format with no stability promise) but never reports it to
the server; the 2018 upstream feature request for exactly that was closed
with nothing shipped. Nothing in 0.31.0 (what trixie's apt provides)
through 0.35.0 changes this, so there is no version upgrade that rescues
observability.

Therefore:

- a running process is not proof of audible playback;
- FIFO bytes are not proof of clock lock;
- the number of audible clients is not proof that the requested clients are
  present;
- "fits the AirPlay timing budget" is not proof that two speakers are aligned;
- the **Aligned** UI state stays reserved indefinitely: the only trustworthy
  alignment proof available to JTS is its own acoustic measurement
  (`sync_measure.py`), used as an acceptance instrument — not a runtime
  health signal;
- and none of this implies Snapcast isn't syncing: its client auto-resyncs
  continuously and needs no help. The gap is *observability*, not sync.

One correction to v1's framing: the backend does **not** strip Snapcast client
identity before the verdict — `state.py::_stream_client_signal` publishes
per-row `name`/`connected`/`stream_id`/`muted` all the way to the browser. The
real gap is the **join key**: the grouping roster stores a directory display
name and an address, snapclient rows carry an unstable MAC-derived id and a
bare hostname, and `state.py` matches on hostname. An identity-qualified
verdict needs the stable `--hostID` identity (WO-6), not just a comparison
the browser forgot to make.

## Work completed on this branch (re-verified 2026-07-28)

Every claim in this section was independently re-verified against the rebased
branch by the v2 review. Confirmed items are stated as fact; corrections are
noted inline.

### Applied-profile projection for grouped active speakers

The active-speaker carrier now has one shared decoder for immutable applied
recomposition inputs (`_applied_recomposition_inputs`). Solo recomposition and
grouped driver-domain projection consume the same:

- active-speaker preset and crossover;
- playback route;
- per-driver correction;
- applied linearization;
- baseline identity;
- bass-extension profile.

Grouped projection adds only:

- leader/follower capture;
- inter-speaker channel selection;
- attenuate-only pair balance;
- a dedicated non-positive `active_driver_headroom`;
- the grouped output route.

The independent runtime classifier proves the required mixer/filter ordering,
the absence of program Layer B/C in the local driver graph, the 0 dB ceiling,
limiter and tweeter protection, and sufficient attenuation for applied
linearization boost. The emitter does not certify itself. The v2 review
confirmed the headroom proof is exact (the classifier reads the emitted
`active_driver_headroom` gain off the graph and re-proves the worst branch
chain against it; blockers separately prove no room/program filters exist on
the driver-domain graph, so attribution is complete), and confirmed no new
refusal class: any profile the driver-domain emit would refuse at
`MAX_PROGRAM_HEADROOM_DB` would already have refused solo.

Balance signs were also re-proved end-to-end: `cfg.trim_db` is per-member and
non-positive, `recommend_trims` lifts so exactly one side is 0, each endpoint
projects `pair_trim_db = max(0.0, -trim_db)` from its **own** `grouping.env`,
and the emitter negates it back to a non-positive Gain the classifier
independently re-checks.

Primary code:

- [`AppliedDriverDomainConfig` and applied recomposition](../jasper/active_speaker/baseline_profile.py)
- [`emit_active_speaker_driver_domain_config`](../jasper/active_speaker/camilla_yaml.py)
- [`classify_camilla_graph`](../jasper/active_speaker/runtime_contract.py)
- [leader role projection](../jasper/multiroom/active_leader_config.py)
- [follower role projection](../jasper/multiroom/follower_config.py)

### Grouping/coupling ownership

Before bond wiring, grouping now requests one fresh pass from
`jasper-fanin-coupling-auto.service` (`_converge_grouping_coupling` in
[`jasper/multiroom/reconcile.py`](../jasper/multiroom/reconcile.py) — bounded
drain-then-fresh-start, gated to enabled bonds, fail-safe to solo). Grouping
then reads the effective coupling and proceeds only if it is compatible.

This preserves:

- one coupling writer;
- the coupling owner's route matrix;
- operator-pinned coupling intent;
- fail-safe solo behavior when convergence cannot be proved.

Grouping does not write `fanin.env` or duplicate hardware/coupling policy.

### Safer primary pair ordering

The normal two-speaker Rooms flow now:

1. preflights both targets (including the local leader);
2. writes the remote follower first;
3. writes the local leader only after the follower accepts;
4. makes one authoritative follower-disable call if the leader write fails;
5. reports whether that compensation succeeded.

The v2 review verified the mechanics: the target selectors match
`_stereo_pair_members_from_intent`'s exact shape, the local-leader write is a
loopback POST, and the compensation body `{"enabled": false, "trim_db": 0.0}`
is a complete disable under the control server's parse rules (no omitted
field can cause rejection).

Advanced explicit-member fan-out retains its documented concurrent,
partial-failure behavior.

This reduces the most common half-pair failure, but it does not close the
accepted-versus-effective gap (finding F-1, WO-3/WO-4).

### Semantic CamillaDSP live verification

The runtime contract no longer treats a successful WebSocket command as proof
that the desired graph is active. It reads the live config back, normalizes
semantically irrelevant representation differences
(`_camilla_live_semantic_fingerprint`), compares the result, and makes one
short retry for the observed Camilla activation race.

The v2 review probed the normalization for semantic collisions and found
none: CamillaDSP's serde config model makes explicit `null` and absent
equivalent for every optional field, booleans are excluded from the
integral-float collapse, and list positions are preserved. The
`volume_limit` safety ceiling is checked by a separate dedicated guard and is
unaffected.

This is a bounded control-path check, not work added to the audio hot path.

### Live-balance writer permission

`jasper-control.service` now receives write access to the one canonical
CamillaDSP mutation lock file. It does not receive write access to the
configuration directory. Tests pin that least-privilege boundary
(`tests/test_control_systemd.py::test_readwritepaths_pins_control_write_contracts`).
The v2 review found the grant must be missing-tolerant — finding F-6; WO-1
fixes it.

### More honest active-endpoint runtime state

The runtime state now:

- marks the passive outputd FIFO signal not applicable on active endpoints;
- preserves a connected local Snapcast identity despite stale duplicate rows
  (the sticky `own_client_connected` reduction, pinned by
  `tests/test_multiroom_state.py::test_runtime_pair_lock_ignores_disconnected_stale_duplicate_of_own_client`);
- reports requested role separately from effective active endpoint;
- distinguishes pairing, blocked, degraded, unknown, and solo presentation.

The solo-speaker snapshot promise holds: the endpoint status read remains
gated on `cfg.enabled`, so a solo speaker still performs no status-file read
and its `/state` payload is unchanged.

Two verdict problems remain shipped on this branch and are deploy-gating:
the browser's positive "Grouped" badge is count-based (F-2), and
`jasper/web/rooms_setup.py::_rooms_view` derives `state: "paired"` from
requested intent alone (F-11). WO-1 removes the false green now; WO-6 builds
the one backend verdict.

## Findings register

Merged findings from the independent SEVA review (v1) and the v2 multi-agent
validation. Each carries an ID used by the work orders. **Deploy-gating**
means: must be fixed before any deployment of this branch to hardware,
including lab boxes used for the acceptance matrix.

| ID | Severity | Deploy-gating | Finding | Fixed by |
|---|---|---|---|---|
| F-1 | Blocker | yes | Pair creation stops at request acceptance: `POST /grouping/set` persists intent, fires `systemctl --no-block start` via the kick script, and returns 200. Every later refusal (coupling, ring, precheck, camilla swap, unit start) happens after the browser was told the pair formed. | WO-3, WO-4 |
| F-2 | Blocker | yes | The branch ships the count-based green badge SEVA said must not ship: `groupingStatusView`'s `healthyLeaderRoster` requires only `audible >= roster.length + 1`, and `tests/js/rooms_grouping_view_test.mjs` pins it green with no client names at all. Any unrelated audible snapclient (or a connected duplicate row inflating `audible`) substitutes for the requested follower. | WO-1 |
| F-3 | Blocker (plan) | — | The "terminal effective-role receipt" WO-4 needs does not exist: the receipt is written once, early, before any unit/DSP work, and never revised on the primary create path — a fingerprint-matching receipt is fully compatible with `rc == 1`. | WO-3 |
| F-4 | Blocker (plan) | — | No wire surface carries the request fingerprint: `POST /grouping/set`'s response and `GET /grouping`'s snapshot both omit it, and the fingerprint is computed locally per-speaker, so the coordinator cannot derive the peer's. | WO-3 |
| F-5 | Blocker (plan) | — | v1's rollback-on-deadline policy was wrong twice: it would auto-tear-down every first-ever pair (the first bond runs the Snapcast apt install, which exceeds any HTTP deadline), contradicting the grouping supervisor's ratified "no auto-unwind from a poll" rule; and follower-first rollback is undone within 30 s by the leader's `_reassert_peer_tick`. | WO-4 (three-outcome design, leader-first rollback) |
| F-6 | Should-fix | yes | `ReadWritePaths=/var/lib/camilladsp/configs/.dsp_apply.lock` has no `-` prefix: a missing lock file fails jasper-control's namespace setup and the management surface never starts. install.sh's deliberate `touch` runs **after** the jasper-control restart; today only an incidental, explicitly fail-open path (`reconcile_sound_dsp_state` → `dsp_writer_lock` `O_CREAT`) creates it first. No doctor check exists. | WO-1 |
| F-7 | Should-fix | yes | `check_grouping_tts_lane` returns **ok** for the hardware-observed active-leader TTS leak (`expected_grouping_tts_route` routes an unparked active leader to the fan-in socket feeding the shared bake). A doctor check certifying a known defect blocks honest acceptance. | WO-1 |
| F-8 | Should-fix | yes | The branch's own edit claims roster-identity proof ("the authoritative roster's Snapcast clients … connected, on the right stream, unmuted, audible") that the code does not perform — new doc-vs-code drift introduced while fixing old drift. | WO-1 |
| F-9 | Should-fix | no | A present-but-malformed applied linearization entry is silently dropped: `_applied_recomposition_inputs` discards non-Mapping maps and non-Sequence role values before `_validated_linearization` (which is fail-closed) ever sees them. Pre-existing, but this decoder now also feeds grouped driver graphs; failure direction is hardware-benign yet ships a tonally different graph with no issue emitted. | WO-1 |
| F-10 | Should-fix | no | Upgrade window on a bonded active box: `outputd_active_lane_decision` (via `jasper-audio-hardware-reconcile`) classifies the **stale pre-PR grouped graph** during the same install run that ships the stricter classifier, silently degrading outputd to the ordinary stereo lane until the grouping reconciler re-emits and the hardware reconcile re-runs (next boot at latest). No teardown, no cue. | WO-7 |
| F-11 | Should-fix | no | `_rooms_view` is a third verdict: `state: "paired"` from `enabled and bond_id` alone (requested intent), gating `can_balance_pair`. Three verdicts exist (pair_lock, groupingStatusView, _rooms_view). | WO-6 |
| F-12 | Should-fix | no | Scalar balance failure escalates unconditionally: `_post_grouping_set` kicks the structural reconciler whenever `live_apply.applied` is false. Desired vs effective balance is not distinguishable anywhere (`_pair_balance_snapshot` reads only desired `trim_db`). | WO-5 |
| F-13 | Should-fix | no | Endpoint latency has two additive Snapcast inputs and a latent second authority: `snapclient --latency` (shipped, from `client_latency_ms`) and server-persisted `Client.SetLatency` (defined in `snapcast_rpc.py`, zero production callers, would persist into snapserver's `server.json`). Both subtract from the client buffer — wiring both double-compensates silently. | WO-8 |
| F-14 | Should-fix | no | The "Pairing" state effectively never renders during a real create: it keys off `blocked_reason == "role_transition_in_progress"`, which is set only on a role transition from a previously parked role — not on the common solo→bond create. | WO-6 |
| F-15 | Should-fix | no | Boot-qualification asymmetry in `effective_role.py`: `_status_matches_current_boot` is consulted only on the requested-follower branch; a solo or leader request never boot-qualifies, so a prior-boot receipt can satisfy the sources-allowed checks. | WO-2 |
| F-16 | Should-fix | no | Missing upgrade/migration contract: the request fingerprint is `sha256(repr(GroupingConfig))`, so any config-shape change (WO-6 adds a member field) invalidates every deployed receipt; install.sh restarts the grouping reconciler on **every** deploy (full bond re-apply); and the two speakers of a pair deploy sequentially, so mixed-version pairs are a normal state, not an edge case. | WO-7 |
| F-17 | Should-fix | no | Snapclient identity is the first-non-loopback-NIC MAC (not the interface used to reach the server): NIC churn or a reinstall mints a new client id, orphans the tuned `server.json` row (and any persisted latency), and defeats a durable roster join key. Snapserver never prunes rows and JTS never calls `Server.DeleteClient`. | WO-6 |
| F-18 | Should-fix | no | JTS ships `codec=flac` with snapserver's default `chunk_ms=20`, the exact pairing upstream documents as non-optimal (FLAC needs ~26 ms chunks and adds ~26 ms codec latency) — and the repo's own prior research already said "PCM first" and was not followed by the default. | WO-8 (chunk_ms explicit), WO-9 (codec decision) |
| F-19 | Nit | no | `compile_applied_driver_domain_config` checks `validation.get("ok_to_apply")` on a dict that never contains it (`ok_to_apply` is a property, `to_dict` is `asdict`) — behaviorally inert (the surviving status check is equivalent) but dead and misleading; the same idiom pre-exists at two more sites (`baseline_profile.py` solo path, `jasper/cli/active_speaker.py`). | WO-1 |
| F-20 | Nit | no | Orphaned state files on upgraded boxes: `/var/lib/jasper/active_speaker_follower_profile.json` and `/var/lib/jasper/active_leader_crossover_profile.json` have zero remaining readers/writers but linger on disk. | WO-1 |
| F-21 | Nit | no | Duplication slated for cleanup: two near-identical drain-then-fresh owner-handoff functions in `reconcile.py`; the emitter's three-branch monkeypatch-seam call in `camilla_yaml.py::_emit_baseline_pipeline`; ~50 lines of browser verdict logic. | WO-1 (helper, emitter), WO-6 (browser) |
| F-22 | Nit | no | `docs/doc-map.toml`'s `multiroom-grouping` entry did not list this plan doc, so docs-impact would not route grouping changes to it (and the `test_root_and_top_level_docs_are_intentionally_mapped` guard failed CI on any branch carrying the doc). | Fixed with the v2 plan commit |
| F-23 | Nit | no | `jasper/multiroom/balance.py`'s comment reasons about per-member buffer asymmetry that is structurally impossible (`--stream.buffer` is server-global; snapclient has no buffer flag; a follower's `JASPER_GROUPING_BUFFER_MS` is inert) — a load-bearing misstatement for measurement-error reasoning. | WO-1 |

Confirmed-correct items from the validation (no action): the coupling
handoff, the primary-pair coordinator mechanics and compensation payload, the
sticky `own_client_connected` reduction and its test, the timeout budget
arithmetic (the 2786 s derivation and 2796 s kick drain, both pinned by
tests), the semantic-fingerprint normalization, the trim sign chain, the
linearization headroom proof, and the solo `/state` snapshot promise.

## Delivery plan — work orders

Execute in order. WO-1 must land on this branch before merge. WO-2 through
WO-6 are separate PRs in sequence (WO-5 may run in parallel with WO-4). WO-7's
contract tests ride with WO-6's schema change; its install-ordering fix is its
own small PR. WO-8/9 follow hardware acceptance of the pairing core; WO-10 is
its own hardware-soaked workstream.

Shared rules for every work order:

- Hardware-free pytest in the same PR for every behavior change; test names
  below are prescriptions.
- Structured `event=` logs for every new failure path; no journal spam
  (coalesce or rate-limit anything that can flap).
- New wire-contract keys are additive; absent keys read as `unknown`.
- Scan the mapped canonical docs per the touched-subsystem rule and update
  them in the same PR when behavior lands.
- Rebase onto current `origin/main` before push
  (`git merge-base --is-ancestor origin/main HEAD` exits 0);
  `scripts/test-fast` before push, `scripts/test-merge` before merge.

### WO-1 — branch correctness pass (this branch, before merge; deploy-gating)

1. **Remove the false green (F-2).** In
   `deploy/assets/rooms/js/grouping-view.js`, delete the
   `expectedClients` / `healthyLeaderRoster` block and its `return`
   (~lines 168–204): a leader with count-only evidence falls through to
   `Status unknown`. In `tests/js/rooms_grouping_view_test.mjs`, replace the
   test pinning green-from-counts (lines ~111–138) with its inverse:
   `leader with audible count but no identity proof stays "unknown"`.
2. **Revert the overstated doc sentence (F-8).** The new "Green Grouped
   requires … authoritative roster's Snapcast clients" sentence must
   describe what the code proves after item 1 (explicit healthy pair lock
   only), until WO-6 lands identity proof.
3. **Make the lock grant missing-tolerant (F-6).** In
   `deploy/systemd/jasper-control.service`, change the entry to
   `-/var/lib/camilladsp/configs/.dsp_apply.lock`. Extend
   `tests/test_control_systemd.py::test_readwritepaths_pins_control_write_contracts`
   to assert the `-` prefix. Add one doctor check
   (`check_dsp_apply_lock_file`, in the doctor's sound/DSP domain module):
   warn when the lock file is absent or not group-writable, with the exact
   remediation in the detail. Note in the unit comment that install.sh's
   `touch` runs after the restart, which is why the prefix is required.
4. **Make the TTS doctor honest (F-7).** In
   `jasper/multiroom/tts_route.py::expected_grouping_tts_route`, return a
   distinct route kind (`active_leader_shared_path`) for an unparked active
   leader, and make `jasper/cli/doctor/grouping.py::check_grouping_tts_lane`
   report **warn** for it ("grouped active-leader TTS traverses the shared
   Snapcast path; leader-local summer not yet implemented") until WO-10
   lands. Test:
   `tests/test_multiroom_tts_route.py::test_active_leader_route_is_not_ok_before_local_summer`.
5. **Fail closed on malformed applied linearization (F-9).** In
   `jasper/active_speaker/baseline_profile.py::_applied_recomposition_inputs`,
   a present `linearization` that is not a Mapping, or a role entry that is
   not a Sequence, returns a blocker issue
   (`applied_linearization_malformed`) instead of silently narrowing to
   `{}`/dropping the role. Absent stays valid. Test:
   `tests/test_active_speaker_baseline_profile.py::test_present_malformed_linearization_role_fails_validation`.
6. **Delete the dead `ok_to_apply` dict idiom (F-19)** at all three sites
   (`compile_applied_driver_domain_config`, the solo path in
   `baseline_profile.py`, `jasper/cli/active_speaker.py`): use the typed
   `CamillaConfigValidationResult.ok_to_apply` property before `to_dict()`.
7. **Extract the owner-handoff helper (F-21).**
   `_converge_owner_unit(unit, *, start_timeout, event_prefix) -> bool` in
   `reconcile.py`; both `_converge_grouping_coupling` and
   `_converge_sources_after_role` become thin wrappers. Behavior-preserving;
   existing tests must pass unmodified.
8. **Simplify the emitter call path (F-21).** Collapse
   `camilla_yaml.py::_emit_baseline_pipeline`'s three-branch
   `_driver_baseline_filter_chain` dispatch to one explicit full call; adjust
   the safety tests that patch the two-argument seam to patch the full
   signature instead.
9. **Orphan cleanup (F-20).** Add removal of
   `/var/lib/jasper/active_speaker_follower_profile.json` and
   `/var/lib/jasper/active_leader_crossover_profile.json` to the install
   migrations (`deploy/lib/install/env-migrations.sh`, alongside its
   existing stale-file removals).
10. **Docs routing (F-22): already done** — the plan doc is mapped in
    `docs/doc-map.toml`'s `multiroom-grouping` entry as of the v2 plan
    commit (the docs-impact guard test enforces it).
11. **Fix the impossible-asymmetry comment (F-23).** Correct
    `jasper/multiroom/balance.py`'s buffer-asymmetry reasoning: the stream
    buffer is server-global, snapclient has no buffer flag, and a follower's
    `JASPER_GROUPING_BUFFER_MS` is inert.
12. **Correction Start Over copy.** In
    `deploy/assets/correction/js/crossover/main.js::startOverConfirmMessage`,
    the grouped branch's claim that clearing measurements changes the next
    group re-form is now false (the applied profile survives); reword to say
    the applied crossover remains in force until a new Apply. Re-evaluate
    whether `correction_crossover_flow`'s `_active_group_member` /
    `envelope["grouping_member"]` still earn their keep; remove if dead.

Acceptance: `scripts/test-fast` green; the JS harness green; no green
"Grouped" reachable from count-only evidence (test-pinned); doctor warns on
the active-leader TTS lane and on a missing lock file.

### WO-2 — one effective-role resolver

Files: `jasper/multiroom/effective_role.py` (primary),
`jasper/multiroom/runtime_balance.py`, `jasper/control/grouping_supervisor.py`,
`jasper/multiroom/state.py`, `jasper/cli/doctor/grouping.py`.

What already exists in `effective_role.py` and is reused, not rebuilt:
`grouping_request_fingerprint(cfg)` (sha256 of `repr(GroupingConfig)`),
`read_current_boot_id()` / `normalise_boot_id()` /
`_status_matches_current_boot()`, the hardened receipt reader
(`read_effective_role_status`, bounded, nofollow, parent-ownership-verified),
and the receipt path `/var/lib/jasper-grouping/effective-role.json`.

1. Add the resolver:

   ```python
   @dataclass(frozen=True)
   class EffectiveRole:
       outcome: str          # "transitioning"|"effective_solo"|"effective_leader"
                             # |"effective_follower"|"blocked"|"unknown"
       endpoint: str         # "active_crossover"|"passive"|"none"
       requested_fingerprint: str
       boot_id: str
       blocked_reason: str   # typed, "" when not blocked
       matches_request: bool

   def resolve_effective_role(
       cfg: GroupingConfig, *, status=None, boot_id_reader=None,
   ) -> EffectiveRole: ...
   ```

2. **Uniform qualification (F-15).** The resolver applies fingerprint AND
   boot qualification for every role, not only requested-follower; a
   mismatch on either → `outcome="unknown"`. Keep
   `effective_local_sources_park_reason`'s existing behavior byte-for-byte
   (it is a safety gate with its own tests); reimplement it in terms of the
   resolver only if `tests/test_multiroom_effective_role.py` stays green
   unmodified — otherwise leave it alone and note why.
3. Convert consumers so no caller reconstructs effective role from requested
   config plus hardware presence:
   - `runtime_balance.py::active_endpoint`: target =
     `resolve_effective_role(cfg).endpoint == "active_crossover"`, with the
     injected `active_box_reader` as fallback when the receipt is `unknown`;
   - `grouping_supervisor.py::active_endpoint`: same;
   - `state.py::read_grouping_state`: build the `endpoint` block from the
     resolver;
   - doctor: new `check_grouping_effective_role` reporting
     `outcome` + `blocked_reason`.
4. Tests:
   `tests/test_multiroom_effective_role.py::test_resolve_effective_role_requires_boot_match_for_leader`,
   `…::test_resolve_effective_role_fingerprint_mismatch_is_unknown`,
   `…::test_resolve_effective_role_blocked_carries_typed_reason`;
   `tests/test_multiroom_runtime_balance.py::test_active_endpoint_uses_effective_role_not_requested_config`.

### WO-3 — terminal receipts and the fingerprint wire contract (F-3, F-4)

Prerequisite for WO-4. Files: `jasper/multiroom/reconcile.py`,
`jasper/multiroom/effective_role.py`, `jasper/control/server.py`,
`jasper/multiroom/state.py`.

1. **Terminal receipt.** Add `"outcome"`
   (`"converging"|"landed"|"refused"`) and `"role"`
   (`"solo"|"leader"|"follower"`) to `_write_follower_status`'s payload. The
   existing early publish passes `outcome="converging"` — do NOT change its
   `local_sources_allowed` timing (that is the fail-closed source gate and it
   is correct). Add **one final publish** immediately before
   `log_event(logger, "multiroom.reconcile.done", rc=rc)`:
   `outcome = "landed" if rc == 0 else "refused"`,
   `role = cfg.role if active else "solo"`, `blocked_reason` carried through.
   `read_effective_role_status` gains both keys with fail-soft defaults
   (`"unknown"` / `""`) so a pre-upgrade receipt can never read as `landed`.
2. **Fingerprint on the POST response.** `_post_grouping_set`'s response
   gains `"requested_fingerprint": grouping_request_fingerprint(after_grouping)`
   (`after_grouping` is already in scope). This is the only way a
   coordinator can know the peer's fingerprint — it is computed from the
   peer's own persisted config.
3. **Receipt on GET /grouping.** `read_grouping_state` adds an
   `"effective_role"` block —
   `{outcome, role, endpoint, requested_fingerprint, boot_id, blocked_reason,
   matches_request}` — built from the WO-2 resolver. **Not** gated on
   `cfg.enabled` (rollback proof requires reading a solo landing); preserve
   the never-bonded zero-cost promise by gating the read on
   `cfg.enabled or os.path.exists(FOLLOWER_STATUS_FILE)`, and update the two
   docstrings that state the byte-for-byte promise in the same PR.
4. Tests:
   `tests/test_multiroom_reconcile.py::test_reconcile_publishes_terminal_receipt_on_success`,
   `…::test_reconcile_publishes_refused_receipt_when_rc_nonzero`,
   `…::test_early_receipt_is_converging_not_landed`;
   `tests/test_control_server.py::test_grouping_set_returns_request_fingerprint_of_persisted_config`;
   `tests/test_multiroom_state.py::test_effective_role_block_present_when_solo_after_unbond`,
   `…::test_never_bonded_snapshot_has_no_effective_role_key`.

### WO-4 — the primary two-speaker transaction (F-1, F-5)

Files: `jasper/web/rooms_setup.py` (coordinator),
`deploy/assets/rooms/js/main.js` (pending handling),
`deploy/nginx-jasper.conf`.

Design decisions (settled — do not re-open):

- **Synchronous inside `_save_bond`,** not a background job: jasper-web is
  socket-activated with idle-exit (a background poller can die mid-poll),
  and `_save_bond` already runs on a `ThreadingHTTPServer` thread. Mirror
  the in-file prior art (`_swap_channels`, `_set_pair_balance` — both
  already do bounded rollback with tests); do not invent a new pattern.
- **Deadline:** `BOND_CONVERGE_DEADLINE_SEC = 20.0`,
  `BOND_CONVERGE_POLL_INTERVAL_SEC = 1.0`,
  `BOND_CONVERGE_DEADLINE_CEILING_SEC = 45.0`. Add
  `proxy_read_timeout 90s;` to the `location /sound/pair/` block in
  `deploy/nginx-jasper.conf` (it currently rides nginx's 60 s default while
  neighbouring blocks set explicit values).
- **Three outcomes, not two** (this corrects v1's step 7):

  | Terminal condition | `payload["pair"]` | HTTP | Rollback? |
  |---|---|---|---|
  | both receipts `outcome=="landed"`, fingerprints match, current boot | `"paired"` | 200 | no |
  | either receipt `outcome=="refused"` (typed `blocked_reason`) | `"refused"` | 502 | **yes** |
  | deadline elapsed, neither refused | `"pending"` | 202, `ok: true` | **no** |

  `"pending"` is a normal state, not a failure: the first bond a household
  ever forms runs the Snapcast apt provisioning and will always outlive an
  HTTP deadline. The browser renders **Pairing** and the existing periodic
  `/rooms.json` poll resolves it. Auto-unbind on a deadline miss is
  forbidden — it contradicts the grouping supervisor's ratified "no
  auto-unwind from a 30 s poll" rule and would tear down every first pair.
- **Rollback ordering: leader first, then follower.** While the leader
  stays bonded, its `GroupingSupervisor._reassert_peer_tick` re-POSTs the
  follower back into the bond within 30 s and would undo a follower-first
  rollback. Encode the reason as a comment at the rollback site.
- **Restart tolerance is free:** the receipts are durable on each speaker's
  disk and the UI re-reads `/rooms.json`; no coordinator journal, no new
  files in jasper-web.

Flow: preflight both → POST follower (retain its
`requested_fingerprint`) → POST local leader (retain fingerprint) → poll
each member's `GET /grouping` `effective_role` block at 1 Hz until both
terminal, a refusal, or the deadline → map to the outcome table → on
`refused`, roll back leader-then-follower via
`{"enabled": false, "trim_db": 0.0}` and report per-member cleanup as
`confirmed` / `pending` / `unreachable`.

Browser: `main.js`'s bond submit handler renders `pair: "pending"` as the
Pairing badge (no error), and `describeBondFailure` keeps its existing
compensation strings.

Tests (`tests/test_web_rooms_setup.py`):
`test_bond_waits_for_both_terminal_receipts`,
`test_bond_returns_pending_when_deadline_elapses_without_refusal`,
`test_bond_pending_does_not_roll_back`,
`test_bond_rolls_back_on_typed_terminal_refusal`,
`test_bond_rollback_disables_leader_before_follower`,
`test_bond_ignores_receipt_with_mismatched_fingerprint`,
`test_bond_ignores_receipt_from_a_prior_boot`.

### WO-5 — balance as a resilient scalar path (F-12)

Files: `jasper/control/server.py` (primary),
`jasper/web/rooms_setup.py` (`_pair_balance_snapshot`),
`jasper/multiroom/runtime_balance.py` (unchanged apply mechanics).

1. **Delete the unconditional kick.** In `_post_grouping_set`, a trim-only
   change with `live_apply.applied == false` no longer calls
   `_kick_grouping_reconciler()`.
2. **Pending record + bounded retry.** Persist
   `/run/jasper-control/grouping-trim-pending.json`
   (`RuntimeDirectory=jasper-control` already exists and the daemon already
   writes a sibling file there): `{"desired_db", "last_attempt_at",
   "attempts", "last_error"}`. Runtime dir, not `/var/lib` — the desired
   trim is already durable in `grouping.env`. Retry through the **existing**
   coalescer pattern: mirror `_GroupingReconcilerKickCoalescer`
   (leading-edge + trailing-guarantee); do not write a second coalescer
   shape. Bounded attempts (3) with the coalescer absorbing bursts;
   `event=grouping.trim_retry` on each attempt,
   `event=grouping.trim_pending` on exhaustion — then stop retrying until
   the next user input or reconcile; never spin.
3. **Escalate only on structural evidence.** Kick the reconciler only when
   `resolve_effective_role(cfg).outcome` disagrees with the requested role,
   or `read_unit_active_states` reports a start-desired unit not `active`.
   Retry exhaustion alone is not structural evidence.
4. **Desired vs effective on the wire.** `_pair_balance_snapshot`'s `ok`
   payload gains `desired_balance_db`, `effective_balance_db`, `pending`,
   `last_error`, sourced from the pending record and the proved live value;
   reuse the existing member `/grouping` round-trip
   (`_get_member_grouping_for_balance_snapshot`, 0.75 s timeout) rather than
   adding a second cross-speaker call. The Rooms balance UI shows a pending
   marker instead of silently displaying desired as effective.
5. Changing balance must not restart Snapcast, rebuild the active graph, or
   change endpoint-latency calibration (assert in tests: no reconciler kick
   recorded).

Tests:
`tests/test_control_server.py::test_grouping_trim_failure_does_not_kick_reconciler`,
`…::test_grouping_trim_failure_records_pending`,
`…::test_structural_drift_still_kicks_reconciler`;
`tests/test_web_rooms_setup.py::test_balance_snapshot_separates_desired_from_effective`.

### WO-6 — one backend verdict; identity-qualified status (F-2 root cause, F-11, F-14, F-17)

Files: `jasper/multiroom/reconcile.py` (`snapclient_argv`),
`jasper/multiroom/config.py`, `jasper/multiroom/state.py`,
`jasper/multiroom/snapcast_rpc.py`, `jasper/web/rooms_setup.py`,
`deploy/assets/rooms/js/grouping-view.js`,
`deploy/assets/rooms/js/main.js`.

1. **Stable snapclient identity (F-17).** `snapclient_argv` gains
   `--hostID <local peer id>` (the stable identity at
   `/var/lib/jasper/peer_id`, the same identity-layer value the deploy
   guard uses). This makes the snapserver client id survive NIC churn and
   reinstalls, gives persisted per-client state a stable key, and provides
   the roster join key. Note: on an existing deployment this mints one new
   client row per member (the old MAC-keyed rows become stale once) — the
   GC in item 2 and the sticky reducer absorb that; add a release-matrix
   row for it.
2. **Stale-row GC.** On unbond and on roster shrink, the reconciler calls
   `Server.DeleteClient` for departing/stale ids (snapserver never prunes
   its registry on its own). Fail-soft, same shape and coalescing as
   `ensure_groups_on_stream`. Event: `event=multiroom.snapcast_client_gc`.
3. **The join key on the wire.** `GET /grouping` gains additive
   `"client_id"` (the local box's snapclient id). `_save_bond` records each
   member's id into the roster: `BondMember` gains `client_id`. The verdict
   requires every roster `client_id` connected + audible on the wanted
   stream; `state.py`'s self-match switches from hostname to id. Absent
   `client_id` (legacy bond or old-code peer) → verdict caps at
   `alignment_unverified`; never green. NOTE: adding a `BondMember` field
   changes `repr(GroupingConfig)` and therefore invalidates deployed
   receipts — WO-7's contract test must ride in this same PR.
4. **One producer.** New pure function in `state.py`:

   ```python
   def derive_grouping_verdict(cfg, *, runtime, endpoint, effective_role,
                               stream_clients, self_client_id) -> dict:
       # -> {"state": "solo"|"pairing"|"blocked"|"degraded"|"streaming"
       #             |"alignment_unverified"|"aligned"|"unknown",
       #     "label": str, "detail": str, "missing_members": [str]}
   ```

   PURE and total, same discipline as `_derive_pair_lock`. Published under
   `snapshot["verdict"]`. `"aligned"` is reserved and unreachable today —
   pin that with a test so nobody wires it to weak evidence.
   `"pairing"` derives from `effective_role.outcome in
   {"converging", "unknown"}` with `cfg.enabled` and a matching fingerprint
   — the real create window — not from `role_transition_in_progress` (F-14).
5. **Fold in `_rooms_view` (F-11).** Its `state` and `can_balance_pair`
   derive from `verdict.state`, deleting the requested-only `"paired"`.
6. **Shrink the browser.** `groupingStatusView` reduces to rendering
   `g.verdict` with a fail-soft default (absent verdict → Status unknown);
   the household-state table (Solo / Pairing / Couldn't pair / Degraded /
   Streaming / Alignment unverified / Aligned / Status unknown) is produced
   backend-side only.
7. **Label honesty.** Rename the AirPlay `"Synced"` label in
   `airplayLipSyncRow` to `"Fits AirPlay timing budget"`; update the two JS
   test assertions.

Tests:
`tests/test_multiroom_state.py::test_verdict_requires_identity_qualified_roster`,
`…::test_verdict_aligned_is_unreachable`,
`…::test_verdict_pairing_during_converging_receipt`,
`…::test_legacy_bond_without_client_id_caps_at_alignment_unverified`;
`tests/test_multiroom_reconcile.py::test_snapclient_argv_pins_host_id`;
JS: update `rooms_grouping_view_test.mjs` to assert render-only behavior.

### WO-7 — upgrade and mixed-version contract (F-10, F-16)

1. **Receipt schema contract test** (rides with WO-6's `BondMember` change):
   `tests/test_multiroom_upgrade.py::test_prior_schema_receipt_reads_unknown_never_landed`
   — a receipt written by the previous code (missing `outcome`, older
   fingerprint) resolves to `outcome="unknown"`, never `landed`. Plus
   `tests/test_multiroom_state.py::test_grouping_response_roundtrip_tolerates_absent_new_keys`
   pinning SSOT rule 8 (additive keys, absent reads unknown).
2. **Close the F-10 window.** In `deploy/install.sh`, after
   `reconcile_grouping_state` (which re-emits the grouped configs from the
   new emitter), re-run the audio-hardware reconcile once
   (`systemctl start jasper-audio-hardware-reconcile.service` on boxes where
   the unit exists) so `outputd_active_lane_decision` classifies the
   **re-emitted** graph within the same deploy instead of degrading the
   active lane until next boot. Keep it a bounded oneshot start with the
   same failure tolerance as the surrounding install steps.
3. **Release-matrix rows** (hardware): "deploy leader then follower with
   music playing; the pair recovers with no operator action"; "mixed-version
   pair (one speaker on old code) keeps playing and reports honest
   non-green status until both are updated"; "first deploy after WO-6 mints
   new snapclient identities; the pair re-converges and the stale MAC-keyed
   rows are GC'd."

### WO-8 — endpoint-latency receipt (F-13, F-18) [plumbing]

Ownership follows the closest in-repo precedent: an immutable measured
artifact that a reconciler projects (the applied-baseline-profile shape;
AGENTS.md pattern 3 — the reconciler stays the daemon-facing writer). The
compensation model is **member-local and self-contained**: each member
measures its own fixed local path latency L (from snapclient handoff to
output) and plays earlier by L via `snapclient --latency L`. Aligning every
member at a common reference this way needs no cross-speaker plumbing and no
leader-side RPC writes. The cost: positive latency is subtracted from that
client's share of the group buffer, so compensation and buffer are coupled
(see the constraint below and WO-9).

1. **Receipt:** `/var/lib/jasper-grouping/endpoint-latency.json`, schema:

   ```json
   {"schema": 1, "latency_ms": 0, "topology_id": "…", "dac_profile": "…",
    "sample_rate": 48000, "channels": 2, "route_id": "…",
    "method": "…", "measured_at": "…"}
   ```

   Read through the same hardened bounded reader discipline as
   `read_effective_role_status` (bounded size, nofollow, parent-ownership
   check).
2. **Consumer:** `reconcile.py::snapclient_argv` uses the receipt value when
   present AND its fingerprint fields match the current topology/DAC/route;
   precedence: operator override (`JASPER_GROUPING_CLIENT_LATENCY_MS` ≠ 0)
   beats receipt beats default 0, resolved in this one function. Log
   `event=multiroom.endpoint_latency.{applied,stale,missing,overridden}`
   once per reconcile. Applying a changed receipt restarts snapclient — an
   audible ~1–2 s gap on that member — which is acceptable for a rare
   calibration event and is exactly why balance (WO-5) must never ride this
   path.
3. **One latency input only (F-13).** Delete
   `snapcast_rpc.set_client_latency` (zero production callers). The server-
   persisted per-client latency and the `--latency` flag are **additive**
   inside snapclient (`bufferLen = max(0, buffer − serverLatency −
   cliLatency)`); wiring both double-compensates silently. Server-side
   latency stays 0 forever; assert that in the GC/identity test.
4. **Saturation guard.** Positive latency saturates silently: when
   `latency ≥ buffer`, the client's effective buffer clamps to 0 and it
   lives in continuous underrun with no error. Refuse (validation) a receipt
   with `latency_ms > buffer_ms − 100`, and add a doctor warn
   (`check_grouping_endpoint_latency`: present / stale / mismatched /
   absent / too-close-to-buffer).
5. **Explicit chunk size (F-18).** `snapserver_argv` sets `chunk_ms`
   explicitly in the source URI instead of inheriting the default 20 ms:
   `chunk_ms=26` while the codec remains FLAC (upstream documents FLAC
   needs ~26 ms chunks); `chunk_ms=20` when WO-9 selects PCM. One constant
   per codec in `config.py`, not scattered.
6. **Measurement method:** the receipt writer is a hardware-session
   deliverable (WO-9) using the `sync_measure.py` correlation primitive
   end-to-end — never queue-depth differencing. The plumbing lands first so
   a hand-written receipt can be validated on the bench.

Tests:
`tests/test_multiroom_reconcile.py::test_snapclient_latency_prefers_operator_override_then_receipt`,
`…::test_stale_receipt_falls_back_and_logs`,
`…::test_receipt_too_close_to_buffer_is_refused`,
`…::test_snapserver_argv_pins_chunk_ms_per_codec`;
doctor-check test in the doctor test module;
`tests/test_multiroom_snapcast_rpc.py`: remove the `set_client_latency`
tests with the helper.

### WO-9 — buffer/codec ladder soak [hardware]

After WO-8's plumbing. The two knobs are **coupled** (endpoint compensation
is subtracted from the buffer), so treat this as a small 2-D matrix, not two
independent sweeps, and sweep one variable at a time within it:

- codecs: PCM vs FLAC at fixed buffer and per-codec `chunk_ms` (F-18). FLAC
  adds a ~26 ms codec-latency floor; PCM adds none (~1.1 Mbit/s stereo at
  48 kHz/16-bit — trivial for the household LAN). PCM is the expected
  winner for the lower rungs; keep FLAC as the constrained-bandwidth
  fallback.
- buffers: 150 / 200 / 250 / 300 / 400 ms. The floor for any rung is
  `max(endpoint latency across members) + jitter margin`; trixie's snapcast
  0.31.0 has no artificial floor (the historical 400 ms minimum was removed
  in 0.26.0), and no newer snapcast version changes anything relevant — do
  not chase an upgrade.
- conditions: normal household Wi-Fi; a controlled interference/loss case;
  peer reboot and reconnect; sustained playback long enough to expose clock
  drift.

Instrumentation: the acceptance **proof** is the acoustic measurement
(`sync_measure.py` correlation, both members on the same stream); the bench
**instrument** may scrape the follower snapclient journal (`Chunk:` DEBUG
stats and the hard-resync INFO lines) as an instability counter, but that
format has no upstream stability promise and must never become a product
health surface.

Reconnect expectation (budget it, so the soak isn't misread): snapclient
stops output immediately on disconnect and retries on a fixed 1 s timer, so
outage recovery ≈ 1 s + effective-buffer refill. Shrinking the buffer
400→150 improves steady-state latency but only the refill term of recovery —
diminishing returns on outage length are expected and not a failure.

Choose the lowest-latency codec/buffer combination that passes the
reliability gate; the current 400 ms / FLAC defaults stay until then. Do not
build an adaptive controller before fixed settings are measured and shown
insufficient — snapclient's bounded rate trim already is the adaptive layer.

### WO-10 — leader-local active-speaker TTS [hardware-soaked workstream]

Build the ratified pre-crossover local summer as a separate change:

```text
Snapcast return + leader TTS → local summer → active crossover → final outputd
```

Required contracts:

- TTS never enters the Snapcast stream;
- follower voice remains parked;
- Layer A protects leader TTS;
- program ducking affects the local music input;
- final outputd remains the sole TTS-inclusive AEC-reference publisher;
- the two outputd roles, if used, remain distinct;
- **fail-safe:** if the local summer cannot be armed, the leader's TTS route
  falls back to **parked** (silence plus an audible cue via
  `jasper/cues/registry.py`, per the no-silent-failure rule) — never to
  today's shared-path leak;
- CPU, temperature, memory, xruns, and TTS-to-glass latency pass an on-device
  soak.

Gate: do not start WO-10 until WO-1 item 4 has landed (the doctor must warn
honestly for the whole implementation window, not report "ok"). The expected
incremental cost is local crossover/summer latency, not the network buffer.

## Resilience contract

| Fault | Required behavior | Evidence surface |
|---|---|---|
| Peer unreachable before pair | Change neither speaker | Pair result + structured event |
| Follower accepts, leader request fails | Return follower to solo | Compensation result |
| Request accepted, later reconcile refuses | Terminal `refused` receipt on that speaker; coordinator rolls back leader-first or clearly reports incomplete cleanup | Matching terminal receipts |
| Pair create outlives the HTTP deadline (e.g. first-bond Snapcast provisioning) | `pending` — no rollback; UI shows Pairing; the periodic poll resolves it | 202 response + `/rooms.json` |
| Wi-Fi loss mid-pair-create | Same as deadline miss: `pending`, no rollback; persisted intent converges when the network returns | 202 response + receipts |
| Brief Wi-Fi loss during playback | Snapcast reconnects on its own (fixed 1 s retry + buffer refill; output stops immediately during the outage) without JTS rebuilding DSP: the grouping reconciler is not kicked and the CamillaDSP statefile is unmodified across the outage | Runtime state + `/state.resilience` |
| Peer reboot | Persisted intent re-converges: the receipt's `boot_id` changes and `outcome` returns to `landed` within one supervisor interval (30 s) | Boot-qualified effective role |
| Leader reboots while follower persists intent | Follower stays bonded and silent; leader re-asserts within one supervisor interval | `event=grouping_supervisor` reassert |
| snapserver crashes mid-play | systemd `Restart=` on the snapserver unit is the recovery owner; runtime health shows degraded until it returns | `/state.grouping.runtime.units` |
| Deploy restarts jasper-camilla / grouping reconciler mid-bond | Music interrupts for the bounce; the bond re-converges from persisted intent with no operator action; the receipt returns to `landed` | Terminal receipt + install log |
| NTP clock step | Snapcast owns it; JTS takes no action (the supervisor's rate-limit window already uses `time.monotonic()`) | — |
| Stale Snapcast client row | Never create a false healthy result; departing ids are GC'd at unbond | Identity-qualified roster verdict |
| Sync drifts or hiccups | Snapclient's own engine corrects it (rate trim + hard resync); JTS never restarts snapclient for sync | — |
| Live balance control transient | Keep music path, show pending, bounded coalesced retry; never kick the structural reconciler on retry exhaustion alone | Desired/effective/pending trim |
| DSP topology truly drifts | One coalesced structural reconcile, triggered only by role/unit evidence | Stable cause and action events |
| Active return path starves | **No trustworthy signal exists today.** The verdict reports `alignment_unverified` and the doctor says so; do not add a repair loop before a signal ships | Verdict + doctor |
| Household credential missing/rotated during rollback | Rollback POST failure is reported as `cleanup_unconfirmed` naming the peer; UI offers Disband | Pair result + event |
| Coupling owner fails | Stay safely solo; retain requested intent and reason | Typed block reason |
| Applied profile is malformed | Refuse grouped graph; do not drop safety filters | Validation issue |
| Endpoint receipt is stale or too close to the buffer | Fall back to override/default; refuse to claim calibrated alignment | Receipt status in state/doctor |
| Leader voice response | Play locally through Layer A and final reference (WO-10); until then the doctor warns | TTS route state + latency probe |

Recovery loops must have bounded cadence, hysteresis where evidence can flap,
and stable structured events for cause, action, and outcome. A persistent
failure must not create journal spam or repeated audible topology rebuilds.

## Latency model

"Pair latency" is not one number. Each component has a different purpose and
owner:

| Component | Owner | Why it exists | Optimization rule |
|---|---|---|---|
| Snapcast group buffer (server-global) | Snapcast stream config (leader) | Absorb Wi-Fi jitter; it is the end-to-end budget | Lowest soak-proven stable value (WO-9); floor = max endpoint compensation + jitter margin |
| Codec + chunk framing | Snapcast codec/`chunk_ms` | Sets the floor on how low the buffer can go (FLAC ≈ +26 ms; PCM none) | Per-codec explicit `chunk_ms`; prefer the measured lower-latency stable choice |
| Client endpoint latency | Endpoint calibration receipt → `snapclient --latency` (member-local) | Equalize stable local paths at a common reference | Persistent fingerprinted receipt (WO-8); one input, never two |
| CamillaDSP chunk/queue | Local DSP owner | Safe deterministic processing | Tune only within xrun/safety limits |
| Acoustic seat delay | Room calibration (`/sync/`, 0–100 ms channel delay) | Align physical arrival | Explicit calibration, run after endpoint compensation; never absorbs endpoint offset |
| TTS local path | Leader summer/crossover (WO-10) | Safety and AEC reference | Exclude network buffer |

The design should optimize total experience without deleting a buffer that
buys reliability. Every retained delay should answer "what failure does this
prevent?" and every compensation should have exactly one authority.

## What we deliberately will not build

To keep the implementation proportionate:

- no generic distributed saga or transaction engine;
- no new grouping database or coordinator journal (the per-speaker receipts
  are the durable state);
- no second coupling or source policy writer;
- no browser-side health state machine;
- no adaptive buffer controller before fixed-setting evidence (snapclient's
  bounded rate trim already is the adaptive layer);
- no custom network clock/sync engine — Snapcast owns sync;
- no restarting snapclient to repair sync — its engine converges on its
  own; sanctioned repairs are structural only (unit down, wrong stream
  binding, no producer);
- no automatic resync based only on FIFO bytes or process state;
- no consuming snapclient debug logs as a product health surface (bench
  instrument only, WO-9);
- no second Snapcast latency input (`Client.SetLatency` stays unused and is
  deleted);
- no hardcoded endpoint offset (the 53 ms figure was a lower-bound estimate
  from an incomplete method; measure end-to-end);
- no post-crossover active-speaker TTS shortcut;
- no broad plugin/health framework for this one workflow;
- no leader election for a two-speaker v1 pair;
- no auto-unbind on a deadline miss (only a typed terminal refusal rolls
  back);
- no snapcast version chase: trixie's 0.31.0 has everything this campaign
  needs, and nothing through 0.35.0 adds sync observability.

These are not missing abstractions. They are complexity intentionally excluded
until a named product need and evidence justify them.

## Validation and release gates

### Hardware-free verification

At the reviewed branch snapshot, and re-run during the v2 review on the
rebased base (current `origin/main`): the directly changed test modules
(469 tests across the runtime-contract/baseline-profile/reconcile suites
alone), the broader caller/integration sweep, the Rooms grouping-view
JavaScript test, full-JS syntax checking, Ruff, `git diff --check`,
documentation impact-map validation, and changed-document link checking all
passed. Every work order re-runs `scripts/test-fast` before push and
`scripts/test-merge` before merge, per the repo testing contract.

### Required hardware acceptance

Hardware validation begins only after explicit deployment approval, and only
after WO-1 has landed (deploy-gating findings F-2/F-6/F-7/F-8).

The release matrix must include:

1. solo playback on each speaker before pairing;
2. active-to-active pairing;
3. active-to-passive pairing where supported;
4. remote unavailable before create;
5. injected follower and leader terminal refusal (typed `refused` receipts;
   leader-first rollback observed);
6. pair create, dissolve, recreate, and reboot replay;
7. first-ever pair on a fresh box (Snapcast provisioning path → `pending` →
   Pairing badge → resolves to paired with no rollback);
8. balance movement during continuous audio with no structural restart;
9. stale Snapcast client identity (including the one-time identity churn
   when WO-6's `--hostID` first deploys);
10. brief and sustained Wi-Fi interruption (playback and mid-create);
11. deploy to leader then follower with music playing (mixed-version window);
12. endpoint-latency calibration invalidation after route/topology change;
13. codec/buffer ladder soak (WO-9, acoustic proof via `sync_measure.py`);
14. wake response from the leader only (WO-10);
15. TTS-inclusive AEC reference (WO-10);
16. CPU, memory, temperature, and xrun soak;
17. `/state`, doctor, Rooms UI, and structured-log agreement at every stage.

### Definition of done

This campaign is complete when:

- pair creation is effective-state transactional for the primary two-speaker
  flow, with `paired` / `refused` / `pending` as the only outcomes;
- no supported failure leaves an unreported half-pair;
- active and passive endpoints use the same requested/effective truth model
  (one resolver, one verdict);
- balance changes remain scalar and self-repair without audible graph rebuild;
- the UI never claims alignment from weak evidence (identity-qualified or
  capped at `alignment_unverified`);
- endpoint latency has one durable calibrated authority with one consumer
  and one Snapcast input;
- the shipped buffer/codec choice is supported by soak evidence;
- leader TTS is local, driver-safe, and in the final AEC reference;
- ordinary peer/network/process recovery needs no operator intervention;
- all canonical handoffs and user copy match hardware-observed behavior;
- the integrated branch passes independent review against this plan;
- final on-device acceptance passes before rollout beyond the test speakers.

## File and ownership map

| Concern | Primary implementation |
|---|---|
| Group intent and validation | [`jasper/multiroom/config.py`](../jasper/multiroom/config.py) (single env writer: `jasper/control/server.py::_write_grouping`) |
| Structural convergence + snapcast argv | [`jasper/multiroom/reconcile.py`](../jasper/multiroom/reconcile.py) |
| Effective-role receipt + resolver | [`jasper/multiroom/effective_role.py`](../jasper/multiroom/effective_role.py) |
| Runtime and pair-lock state; verdict | [`jasper/multiroom/state.py`](../jasper/multiroom/state.py) |
| Scalar balance | [`jasper/multiroom/runtime_balance.py`](../jasper/multiroom/runtime_balance.py) |
| Runtime recovery | [`jasper/control/grouping_supervisor.py`](../jasper/control/grouping_supervisor.py) |
| Pair control/API + coordinator | [`jasper/web/rooms_setup.py`](../jasper/web/rooms_setup.py), [`jasper/control/server.py`](../jasper/control/server.py) |
| Snapcast RPC surface | [`jasper/multiroom/snapcast_rpc.py`](../jasper/multiroom/snapcast_rpc.py) |
| Rooms presentation | [`deploy/assets/rooms/js/main.js`](../deploy/assets/rooms/js/main.js), [`grouping-view.js`](../deploy/assets/rooms/js/grouping-view.js) |
| Applied Layer-A authority | [`jasper/active_speaker/baseline_profile.py`](../jasper/active_speaker/baseline_profile.py) |
| Active graph emission | [`jasper/active_speaker/camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py) |
| Independent graph proof | [`jasper/active_speaker/runtime_contract.py`](../jasper/active_speaker/runtime_contract.py) |
| Fan-in coupling | [`jasper/fanin/coupling_reconcile.py`](../jasper/fanin/coupling_reconcile.py) |
| Source parking/restoration | [`jasper/source_intent.py`](../jasper/source_intent.py) |
| TTS route truth | [`jasper/multiroom/tts_route.py`](../jasper/multiroom/tts_route.py) |
| Acoustic sync measurement | [`jasper/multiroom/sync_measure.py`](../jasper/multiroom/sync_measure.py) |
| Endpoint-latency receipt (WO-8) | `/var/lib/jasper-grouping/endpoint-latency.json`, consumed by `reconcile.py::snapclient_argv` |

## Review sequence from here

1. Agree on this v2 plan (goal, authority model, work orders).
2. Land WO-1 on this branch; merge after `scripts/test-merge` and review.
3. Land WO-2 … WO-6 as sequenced PRs (WO-5 may parallel WO-4; WO-7's
   contract tests ride with WO-6), each with its prescribed tests, each
   rebased onto current `main` before push.
4. Run independent adversarial review against the integrated result.
5. Obtain explicit permission and run the hardware acceptance matrix
   (items 1–11 first; 12–13 after WO-8/9; 14–15 after WO-10).
6. Reconcile canonical docs with the observed result.
7. Deploy beyond the test speakers only with explicit approval.

Last verified: 2026-07-28
