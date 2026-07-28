# Multiroom pairing reliability: investigation, architecture, and delivery plan

> **Status: active implementation plan and investigation record.** This
> document explains the intent, evidence, branch work, review findings, and
> remaining delivery plan for reliable paired-speaker playback. It is not the
> authority for what a deployed speaker currently does. Current shipped
> behavior and operator guidance remain in
> [HANDOFF-multiroom.md](HANDOFF-multiroom.md); active-speaker realization
> remains in [HANDOFF-distributed-active.md](HANDOFF-distributed-active.md);
> source parking remains in
> [HANDOFF-source-lifecycle.md](HANDOFF-source-lifecycle.md). Once this campaign
> is complete, those canonical handoffs must describe the landed behavior and
> this file becomes the durable design and investigation record.
>
> **Deployment boundary:** no deployment, service restart, or live
> configuration mutation was performed during the review that produced this
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

Those are the right foundations, but the branch is not yet ready to deploy.
The independent SEVA review found one release blocker: an HTTP success means a
speaker accepted a request, not that it reached the requested audio state.
Pair creation can therefore still report success or leave a half-pair when a
later reconcile fails. The remaining plan closes that gap without adding a
generic distributed-transaction framework.

## Product vision

### The household experience

For a normal stereo pair:

1. The household chooses two speakers.
2. Both speakers are checked before either is changed.
3. The interface shows **Pairing** while the roles are converging.
4. Success is shown only after both speakers prove the same pair request
   became effective.
5. Music moves to the paired path once, with no repeated teardown.
6. The balance control changes level live without changing topology.
7. A wake response plays from the leader only and does not wait behind the
   network music buffer.
8. Brief Wi-Fi loss, a peer reboot, or a transient DSP control failure either
   self-recovers or leaves a precise, actionable status.
9. Dissolving the pair restores each speaker's prior solo behavior and sources.

The household should never need to understand CamillaDSP instances, FIFOs,
Snapcast streams, source parking, or commissioning artifacts to make this
work.

### Engineering qualities

The finished feature should be:

- **Reliable:** one request has one unambiguous outcome; success cannot mean a
  half-applied pair.
- **Resilient:** ordinary network, process, and reboot faults converge without
  manual repair when the underlying resource returns.
- **Observable:** requested, effective, pending, degraded, and blocked states
  are distinguishable in `/state`, doctor, structured logs, and the Rooms UI.
- **Low latency:** each kind of delay has one reason and one owner; no buffer is
  retained without measured value.
- **Hardware-safe:** every active speaker continues to use its own applied
  crossover, limiter, driver gain, linearization, and headroom.
- **Simple:** reuse the existing reconcilers and receipts; do not add a generic
  saga engine, another policy daemon, or a second configuration authority.
- **Idle when unused:** solo speakers pay no Snapcast runtime or grouping
  polling cost.

## Truth model and ownership

Pairing becomes understandable when its different truths are named rather
than collapsed into one `enabled` boolean.

| Truth | Owner | Durable authority | What it means |
|---|---|---|---|
| Pair intent | Rooms / grouping control plane | `/var/lib/jasper/grouping.env` | What the household requested |
| Effective local role | Grouping reconciler | Boot- and request-qualified `effective-role.json` | What this speaker actually landed |
| Driver-domain DSP | Active-speaker Apply transaction | Immutable applied baseline profile | The local hardware-safe Layer-A graph |
| Fan-in route | Fan-in coupling reconciler | Reconciler-owned coupling state | Which input transport is effective |
| Source availability | Source coordinator | Desired source intent plus effective-role permission | Which local sources may run |
| Network synchronization | Snapcast | Live server/client state | Transport, clock estimate, and dejitter |
| Fixed endpoint compensation | Planned calibration owner | One fingerprinted endpoint-latency receipt | Stable local path difference passed to Snapcast |
| Acoustic seat alignment | Room/balance/sync calibration | CamillaDSP delay in the calibration authority | Arrival-time difference caused by placement |
| Live pair balance | Grouping control plane plus effective DSP endpoint | Desired trim plus proved live value | A scalar level change, never a topology change |
| Household status | Backend state composition | The authorities above | Honest presentation; the browser does not invent truth |

### Non-negotiable single-source-of-truth rules

1. `grouping.env` stores intent. It never proves that a role landed.
2. `effective-role.json` is accepted only when its request fingerprint and boot
   identity match the current request and boot.
3. The applied active-speaker profile is the only production source for local
   crossover and driver protection. Drafts and measurements are candidates
   until Apply.
4. Grouping may ask the coupling and source owners to converge. It may not
   write their state or reproduce their policy.
5. Endpoint latency lives in one calibration receipt. It must not be copied
   into both that receipt and grouping intent.
6. The backend owns domain health. Browser JavaScript presents the verdict; it
   does not infer success from client counts.
7. Structural reconcile and scalar live control are different paths.

## Investigation record

### 1. Initial grouping-readiness refusal

The first visible failure was:

> Couldn't pair — 192.168.1.74: speaker software does not provide grouping
> readiness — update both speakers, then retry

Version skew was a reasonable first suspicion, but the deeper architectural
problem was active-speaker readiness. A speaker already playing safely in solo
mode had an immutable applied crossover, yet grouped role construction could
consult mutable commissioning inputs again. That duplicated the meaning of
“ready” and allowed new or incomplete setup evidence to invalidate an already
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
preserving least privilege.

The review found a remaining resilience problem: any future transient live
patch failure still triggers structural reconcile. The completed design must
persist the desired scalar, report it pending, retry it through a bounded
coalesced scalar path, and invoke structural reconcile only when topology or
service evidence proves structural drift.

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

This is a separate, hardware-soaked workstream because merging the summer and
final reference-publisher roles would break the output-reference contract.

### 5. Persistent fixed offset

The observed active paths had different final output queue depths:

| Endpoint | Observed queue | Approximate time at 48 kHz |
|---|---:|---:|
| JTS3 active path | 3,056 frames | 63.7 ms |
| JTS passive path | 512 frames | 10.7 ms |
| Difference before other path delays | 2,544 frames | 53.0 ms |

Both Snapcast clients used zero configured client latency. This is evidence of
a missing fixed-endpoint compensation mechanism, not proof that the correct
value is always 53 ms. The value must be measured and fingerprinted against the
actual DAC, topology, sample rate, and local audio path.

Snapcast should continue to own network clock and jitter. Endpoint calibration
should provide only the stable local offset through Snapcast's existing client
latency input. Room calibration separately owns acoustic arrival at the chosen
seat.

### 6. What the status surface can and cannot prove

Snapcast's available server state proves useful facts such as connection,
stream binding, mute, volume, and reported latency. It does not currently
provide a trustworthy JTS-consumed signal for follower buffer fill, drift, or
acoustic lock.

Therefore:

- a running process is not proof of audible playback;
- FIFO bytes are not proof of clock lock;
- the number of audible clients is not proof that the requested clients are
  present;
- “fits the AirPlay timing budget” is not proof that two speakers are aligned;
- no automatic resynchronization claim is valid until a trustworthy alignment
  signal exists.

## Work completed on this branch

### Applied-profile projection for grouped active speakers

The active-speaker carrier now has one shared decoder for immutable applied
recomposition inputs. Solo recomposition and grouped driver-domain projection
consume the same:

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
linearization boost. The emitter does not certify itself.

Primary code:

- [`AppliedDriverDomainConfig` and applied recomposition](../jasper/active_speaker/baseline_profile.py)
- [`emit_active_speaker_driver_domain_config`](../jasper/active_speaker/camilla_yaml.py)
- [`classify_camilla_graph`](../jasper/active_speaker/runtime_contract.py)
- [leader role projection](../jasper/multiroom/active_leader_config.py)
- [follower role projection](../jasper/multiroom/follower_config.py)

### Grouping/coupling ownership

Before bond wiring, grouping now requests one fresh pass from
`jasper-fanin-coupling-auto.service`. Grouping then reads the effective coupling
and proceeds only if it is compatible.

This preserves:

- one coupling writer;
- the coupling owner's route matrix;
- operator-pinned coupling intent;
- fail-safe solo behavior when convergence cannot be proved.

Grouping does not write `fanin.env` or duplicate hardware/coupling policy.

### Safer primary pair ordering

The normal two-speaker Rooms flow now:

1. preflights both targets;
2. writes the remote follower first;
3. writes the local leader only after the follower accepts;
4. makes one authoritative follower-disable call if the leader write fails;
5. reports whether that compensation succeeded.

Advanced explicit-member fan-out retains its documented concurrent,
partial-failure behavior.

This reduces the most common half-pair failure, but it does not yet close the
accepted-versus-effective gap described in the release blocker below.

### Semantic CamillaDSP live verification

The runtime contract no longer treats a successful WebSocket command as proof
that the desired graph is active. It reads the live config back, normalizes
semantically irrelevant representation differences, compares the result, and
makes one short retry for the observed Camilla activation race.

This is a bounded control-path check, not work added to the audio hot path.

### Live-balance writer permission

`jasper-control.service` now receives write access to the one canonical
CamillaDSP mutation lock file. It does not receive write access to the
configuration directory. Tests pin that least-privilege boundary.

### More honest active-endpoint runtime state

The runtime state now:

- marks the passive outputd FIFO signal not applicable on active endpoints;
- preserves a connected local Snapcast identity despite stale duplicate rows;
- reports requested role separately from effective active endpoint;
- distinguishes pairing, blocked, degraded, unknown, and solo presentation.

The review found that final positive status still needs backend
identity-qualified roster proof; the current browser-side client-count fallback
must not ship.

## Independent SEVA review and staff audit

An independent SEVA agent reviewed every changed file, relevant callers,
tests, service/deploy surfaces, and mapped documentation. It made no edits,
rebases, deployments, service changes, or live configuration changes.

### Release blocker

#### Pair creation stops at request acceptance

`POST /grouping/set` persists intent and queues reconciliation. Its HTTP 2xx
response does not mean the requested role became effective. The primary pair
coordinator currently compensates only direct request failure.

A later coupling refusal, graph validation failure, Camilla activation failure,
unit failure, or source-handoff failure can still produce a half-pair.

**Required correction:** return the request fingerprint, wait for both matching
terminal effective-role receipts, and consider the pair successful only when
both roles land. On terminal failure or an unproved deadline, make a bounded
best-effort return to solo through the same authoritative API and report any
cleanup that remains unconfirmed.

This needs a narrow coordinator, not a general saga framework.

### Should-fix findings

1. **Effective endpoint is not yet the universal runtime authority.**
   `runtime_balance.active_endpoint` still derives the target from requested
   config plus hardware presence. State and supervisor paths consume related
   facts differently. One request- and boot-qualified resolver in
   `multiroom/effective_role.py` should serve live balance, `/state`, doctor,
   and recovery.

2. **The green leader badge is not roster-qualified.** The browser accepts an
   audible-client count, allowing an unrelated or stale client to substitute
   for the requested follower. The backend has Snapcast client identity
   available but strips it before the domain verdict. Until identity-qualified
   proof exists, the UI must remain **Status unknown** or say
   **Streaming—alignment unverified**.

3. **Malformed applied linearization can be silently discarded.** An absent
   legacy linearization map is valid. A present malformed role entry must fail
   applied-snapshot validation rather than compile as “no linearization.”

4. **Scalar failure escalates too quickly.** A transient balance failure
   invokes the structural reconciler. It should remain a pending scalar update
   with bounded retry unless structural drift is independently proved.

5. **Desired balance is displayed as effective balance.** The Rooms snapshot
   needs separate desired/effective/pending fields so a failed live patch is
   visible.

6. **Active-lane liveness has no equivalent recovery signal.** The grouping
   supervisor correctly skips passive FIFO starvation on active endpoints, but
   it does not yet observe and repair the active return/crossover lane. Add
   repeated-evidence hysteresis and bounded repair only after a trustworthy
   signal is identified.

7. **Canonical documentation and user copy contain contradicted claims.**
   Multiroom/distributed-active documentation says leader TTS is already local;
   hardware showed it traversing the group. Correction reset copy says clearing
   mutable measurements makes a still-applied speaker ungroupable; the applied
   profile is intentionally still valid. Source-lifecycle prose says grouping
   never directly asks a coupling owner, while the new pre-bond handoff does
   exactly that without taking over ownership.

### Complexity and maintainability findings

The branch adds substantial code because it closes real cross-layer safety and
truth gaps. Most of it is earned. The following should be simplified:

- two near-identical bounded “drain previous activation, then run one fresh
  owner pass” functions in the grouping reconciler should share one narrow
  helper;
- a validation path converts a typed validation result to a dictionary and
  then checks an `ok_to_apply` key that is not serialized; use the typed
  property before conversion;
- the Camilla emitter has three production branches primarily to preserve a
  monkeypatch seam; one explicit full call is easier to audit;
- approximately 175 lines of browser domain-verdict logic should shrink once
  the backend owns the status.

These are local simplifications. They do not justify a generic orchestration,
health-check, or policy framework.

### High-risk categories checked without a new issue

- **Positive gain and driver safety:** grouped projection remains
  attenuation-only, retains the 0 dB ceiling and per-driver limiter/protection,
  and independently proves linearization headroom.
- **Configuration ownership:** grouping asks existing coupling/source owners
  and does not become another writer.
- **Systemd privilege:** the observed balance mutation needs one lock file; the
  branch grants exactly that file.
- **Solo resource cost:** Snapcast remains off when grouping is off.
- **Web safety:** the changed Rooms UI continues to render runtime strings as
  text nodes through the shared DOM builder.
- **Secrets:** no new credential or token is logged or added to diagnostics.
- **Audio hot path:** semantic Camilla verification occurs on control
  mutations, not per audio block.

## Delivery plan

### Phase 0 — integrate the moving base

The reviewed branch was one commit behind `origin/main`. Read-only merge
analysis found no textual conflict, but textual compatibility is not behavioral
proof.

Before release:

1. rebase onto the latest remote `main`;
2. inspect the integrated diff;
3. rerun targeted and mapped suites;
4. do not deploy until the release blocker and should-fix correctness items are
   closed.

### Phase 1 — one effective-endpoint resolver

Promote the existing request fingerprint and boot qualification in
`multiroom/effective_role.py` into one public resolver that returns a small
domain result such as:

- `transitioning`;
- `effective_solo`;
- `effective_leader`;
- `effective_follower`;
- `blocked`;
- `unknown`.

The result should carry the matching request fingerprint, effective endpoint,
and typed block reason. It should be consumed by:

- `/state` and doctor;
- live balance;
- grouping supervisor;
- Rooms status composition;
- the primary pair coordinator.

No consumer should reconstruct effective role from requested config plus
hardware presence.

### Phase 2 — complete the primary two-speaker transaction

Extend the existing primary pair flow, not advanced fan-out:

1. preflight both speakers;
2. submit the remote follower request;
3. submit the local leader request;
4. retain each returned request fingerprint;
5. poll each existing effective-role surface with a bounded deadline;
6. succeed only when both matching receipts report the intended effective
   roles;
7. if either terminally refuses—or completion cannot be proved by the
   deadline—request solo on both endpoints;
8. report whether cleanup is confirmed, pending, or unreachable.

The coordinator should be restart-tolerant through durable speaker receipts,
but it does not need its own database, job framework, or general rollback
language.

### Phase 3 — make balance a resilient scalar path

For pair trim:

1. persist desired trim once;
2. resolve the effective local DSP endpoint;
3. apply the live scalar under the canonical writer lock;
4. prove the live value semantically;
5. publish `desired`, `effective`, `pending`, and `last_error`;
6. on a transient failure, retry through one bounded, coalesced scalar repair;
7. invoke structural reconcile only when role/topology/unit evidence says the
   graph itself is wrong.

Changing balance must not restart Snapcast, rebuild the active graph, or
change the endpoint-latency calibration.

### Phase 4 — move grouping truth out of the browser

The backend should join the requested roster to stable Snapcast client
identity and emit one domain verdict.

Suggested household states:

| State | Meaning |
|---|---|
| Solo | No pair requested or effective |
| Pairing | Matching request is still converging |
| Couldn't pair | A terminal typed refusal kept the speaker safe |
| Degraded | The role landed but a required runtime path failed |
| Streaming | Requested identities are connected and audible |
| Alignment unverified | Audio evidence exists but no trustworthy lock signal exists |
| Aligned | Reserved for a future trustworthy alignment proof |
| Status unknown | Evidence is missing, stale, or contradictory |

The browser should render this verdict and supporting detail. It should not
promote client counts into **Grouped**. Rename the current AirPlay “Synced”
label to **Fits AirPlay timing budget** or equivalent.

### Phase 5 — close integrity, simplification, and documentation gaps

- Fail closed on present malformed applied linearization.
- Use the typed validation result before serialization.
- Extract the one small owner-convergence helper used twice.
- Simplify the Camilla emitter call path.
- Update correction Start Over copy.
- Reconcile TTS, applied-profile, coupling-handoff, and status claims in the
  canonical handoffs.
- Pin every corrected promise with a targeted test.

### Phase 6 — establish the latency budget empirically

#### Fixed endpoint calibration

Create one persistent endpoint-latency receipt containing:

- measured local latency;
- topology identity;
- DAC/profile identity;
- sample rate and channel layout;
- local route/coupling identity;
- measurement method and timestamp;
- schema/version identity needed to invalidate incompatible receipts.

The reconciler consumes this receipt and passes the value to Snapcast's client
latency control. Missing, stale, or mismatched receipts are observable. Do not
infer an absolute acoustic latency from queue depth alone.

#### Network buffer and codec ladder

The current 400 ms grouped buffer is a legitimate conservative Wi-Fi jitter
budget. It is not the cause of a stable inter-speaker offset, but it has not
been re-earned as the smallest stable value.

After endpoint compensation works, test:

- PCM and FLAC;
- 150, 200, 250, 300, and 400 ms buffers;
- normal household Wi-Fi;
- a controlled interference/loss case;
- peer reboot and reconnect;
- sustained playback long enough to expose clock drift.

Choose the lowest-latency codec/buffer combination that passes the reliability
gate. Do not build an adaptive controller before fixed settings are measured
and shown insufficient.

### Phase 7 — leader-local active-speaker TTS

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
- CPU, temperature, memory, xruns, and TTS-to-glass latency pass an on-device
  soak.

The expected incremental cost is local crossover/summer latency, not the
400 ms network buffer.

## Resilience contract

| Fault | Required behavior | Evidence surface |
|---|---|---|
| Peer unreachable before pair | Change neither speaker | Pair result + structured event |
| Follower accepts, leader request fails | Return follower to solo | Compensation result |
| Request accepted, later reconcile refuses | Return both to solo or clearly report incomplete cleanup | Matching terminal receipts |
| Brief Wi-Fi loss | Snapcast reconnects without rebuilding DSP | Runtime state and bounded recovery event |
| Peer reboot | Persisted intent re-converges using new-boot receipts | Boot-qualified effective role |
| Stale Snapcast client row | Never create a false healthy result | Identity-qualified roster verdict |
| Live balance control transient | Keep music path, show pending, retry scalar | Desired/effective/pending trim |
| DSP topology truly drifts | Run one coalesced structural reconcile | Stable cause and action events |
| Active return path starves | Detect with repeated trustworthy evidence and bounded repair | Active-lane health |
| Coupling owner fails | Stay safely solo; retain requested intent and reason | Typed block reason |
| Applied profile is malformed | Refuse grouped graph; do not drop safety filters | Validation issue |
| Endpoint receipt is stale | Refuse to claim calibrated alignment | Receipt status in state/doctor |
| Leader voice response | Play locally through Layer A and final reference | TTS route state + latency probe |

Recovery loops must have bounded cadence, hysteresis where evidence can flap,
and stable structured events for cause, action, and outcome. A persistent
failure must not create journal spam or repeated audible topology rebuilds.

## Latency model

“Pair latency” is not one number. Each component has a different purpose and
owner:

| Component | Owner | Why it exists | Optimization rule |
|---|---|---|---|
| Snapcast group buffer | Snapcast/group config | Absorb Wi-Fi jitter | Lowest soak-proven stable value |
| Codec framing | Snapcast codec | Network/CPU trade-off | Prefer the measured lower-latency stable choice |
| Client endpoint latency | Endpoint calibration | Equalize stable local paths | Persistent fingerprinted receipt |
| CamillaDSP chunk/queue | Local DSP owner | Safe deterministic processing | Tune only within xrun/safety limits |
| Acoustic seat delay | Room calibration | Align physical arrival | Explicit calibration, not transport inference |
| TTS local path | Leader summer/crossover | Safety and AEC reference | Exclude network buffer |

The design should optimize total experience without deleting a buffer that
buys reliability. Every retained delay should answer “what failure does this
prevent?” and every compensation should have exactly one authority.

## What we deliberately will not build

To keep the implementation proportionate:

- no generic distributed saga or transaction engine;
- no new grouping database;
- no second coupling or source policy writer;
- no browser-side health state machine;
- no adaptive buffer controller before fixed-setting evidence;
- no custom network clock/sync engine;
- no automatic resync based only on FIFO bytes or process state;
- no hardcoded 53 ms endpoint offset;
- no post-crossover active-speaker TTS shortcut;
- no broad plugin/health framework for this one workflow;
- no leader election for a two-speaker v1 pair.

These are not missing abstractions. They are complexity intentionally excluded
until a named product need and evidence justify them.

## Validation and release gates

### Hardware-free verification already completed

At the reviewed branch snapshot:

- 874 directly changed Python tests passed;
- 677 broader caller/integration tests passed;
- the Rooms grouping-view JavaScript tests passed;
- all 118 JavaScript files passed syntax checking;
- changed Python passed Ruff;
- `git diff --check` passed;
- documentation impact-map validation passed;
- documentation impact identified the expected mapped subsystems;
- changed-document link checking passed.

The independent SEVA agent separately reran the high-risk grouping,
active-speaker, coupling/source, API, JavaScript, lint, and documentation
checks.

### Required hardware acceptance

Hardware validation begins only after explicit deployment approval.

The release matrix must include:

1. solo playback on each speaker before pairing;
2. active-to-active pairing;
3. active-to-passive pairing where supported;
4. remote unavailable before create;
5. injected follower and leader terminal refusal;
6. pair create, dissolve, recreate, and reboot replay;
7. balance movement during continuous audio with no structural restart;
8. stale Snapcast client identity;
9. brief and sustained Wi-Fi interruption;
10. endpoint-latency calibration invalidation after route/topology change;
11. codec/buffer ladder soak;
12. wake response from the leader only;
13. TTS-inclusive AEC reference;
14. CPU, memory, temperature, and xrun soak;
15. `/state`, doctor, Rooms UI, and structured-log agreement at every stage.

### Definition of done

This campaign is complete when:

- pair creation is effective-state transactional for the primary two-speaker
  flow;
- no supported failure leaves an unreported half-pair;
- active and passive endpoints use the same requested/effective truth model;
- balance changes remain scalar and self-repair without audible graph rebuild;
- the UI never claims alignment from weak evidence;
- endpoint latency has one durable calibrated authority;
- the shipped buffer/codec choice is supported by soak evidence;
- leader TTS is local, driver-safe, and in the final AEC reference;
- ordinary peer/network/process recovery needs no operator intervention;
- all canonical handoffs and user copy match hardware-observed behavior;
- the integrated branch passes independent SEVA and the subsequent adversarial
  review;
- final on-device acceptance passes before rollout beyond the test speakers.

## File and ownership map

| Concern | Primary implementation |
|---|---|
| Group intent and validation | [`jasper/multiroom/config.py`](../jasper/multiroom/config.py) |
| Structural convergence | [`jasper/multiroom/reconcile.py`](../jasper/multiroom/reconcile.py) |
| Effective-role receipt | [`jasper/multiroom/effective_role.py`](../jasper/multiroom/effective_role.py) |
| Runtime and pair-lock state | [`jasper/multiroom/state.py`](../jasper/multiroom/state.py) |
| Scalar balance | [`jasper/multiroom/runtime_balance.py`](../jasper/multiroom/runtime_balance.py) |
| Runtime recovery | [`jasper/control/grouping_supervisor.py`](../jasper/control/grouping_supervisor.py) |
| Pair control/API | [`jasper/web/rooms_setup.py`](../jasper/web/rooms_setup.py), [`jasper/control/server.py`](../jasper/control/server.py) |
| Rooms presentation | [`deploy/assets/rooms/js/main.js`](../deploy/assets/rooms/js/main.js), [`grouping-view.js`](../deploy/assets/rooms/js/grouping-view.js) |
| Applied Layer-A authority | [`jasper/active_speaker/baseline_profile.py`](../jasper/active_speaker/baseline_profile.py) |
| Active graph emission | [`jasper/active_speaker/camilla_yaml.py`](../jasper/active_speaker/camilla_yaml.py) |
| Independent graph proof | [`jasper/active_speaker/runtime_contract.py`](../jasper/active_speaker/runtime_contract.py) |
| Fan-in coupling | [`jasper/fanin/coupling_reconcile.py`](../jasper/fanin/coupling_reconcile.py) |
| Source parking/restoration | [`jasper/source_intent.py`](../jasper/source_intent.py) |
| Canonical operational multiroom truth | [`docs/HANDOFF-multiroom.md`](HANDOFF-multiroom.md) |
| Active endpoint realization | [`docs/HANDOFF-distributed-active.md`](HANDOFF-distributed-active.md) |
| Source lifecycle truth | [`docs/HANDOFF-source-lifecycle.md`](HANDOFF-source-lifecycle.md) |

## Review sequence from here

1. Agree on this goal, authority model, and scoped plan.
2. Implement the release-blocking and should-fix corrections.
3. Rebase onto the latest remote `main` and rerun integrated verification.
4. Run independent SEVA again against the final diff.
5. Obtain explicit permission and perform hardware acceptance.
6. Reconcile canonical docs with the observed result.
7. Review the evidence together.
8. Only after that agreement, run the separate adversarial review.
9. Address any adversarial findings and repeat the relevant gates.
10. Deploy only with explicit approval.

Last verified: 2026-07-28
