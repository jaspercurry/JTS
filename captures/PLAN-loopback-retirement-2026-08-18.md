# Loopback retirement — session lifeline

Successor mission to #2285 (ring v2, closed 2026-08-17 at 39 merges). Owner ruling:
(1) migrate bonded grouping off the pair-6 aloop remnant onto a grouping ring
(#2481 + #2508, #2581 rides on the hardware pass), then (2) delete the loopback
coupling — the deprecated fallback pipe — entirely. ONE transport everywhere.

Handoff prompt: `captures/NEXT-SESSION-PROMPT-2026-08-18-loopback-retirement.md`.
Predecessor lifeline: `captures/PLAN-ring-v2-rulings-2026-08-10.md` (traps + rulings).

## Method (standing; every sub-brief restates it)

- Conductor (Fable): plans, diagnoses from evidence, dispatches, reviews, records.
  NEVER implements, never runs builds, no product-code edits.
- Builders: Opus (judgment) / Sonnet (mechanical). Model pinned on every dispatch.
- Every PR through INDEPENDENT adversarial review to 0 blockers / 0 should-fixes
  before merge; safety-class (playback graphs, CamillaDSP configs, multi-room sync,
  strike ladder, arm path) gets a 3-lens panel (correctness / hearing-safety /
  resilience). Delta re-reviews by the SAME reviewer. Dispositions POSTED on the PR
  when the review returns. Builder STOPs are licensed.
- Owner values: 80/20 · SSOT + separation of concerns paramount · fail loudly ·
  never-nanny · silence always legal · forward-only · fix findings, don't file them ·
  prose states what IS.

## Fleet + permissions (owner grant)

- jts.local (pi@192.168.1.74, ALWAYS LAN IP): armed composite roleful, LEADER for
  grouping tests. Speaker outputs on DUMMY LOADS — stay unless owner says otherwise.
- jts4 (ssh jts4): Zero-2W streambox, ring-armed, choice=auto. MEMBER/FOLLOWER.
- jts3 FORBIDDEN (peer session runs linearization). jts5 unplugged — leave it.
- Deploys: scripts/deploy-to-pi.sh only, dedicated detached worktree per target.

## Session record

### 2026-08-17 — Session open: worktree pinned, tickets read, census dispatched

Worktree `/Users/jaspercurry/Code/JTS/.claude/worktrees/loopback-retirement-ring-3a4b6e`
at 6e569e8dc == origin/main (ancestry exit 0, zero carried commits). Tickets re-read
at HEAD: #2481 (option B — `jts_ring_grouping` ioplug PCM; env indirection exists at
`jasper/multiroom/reconcile.py:211`; prerequisites: rate-tracking CamillaDSP against
a ring NEVER SHIPPED in either direction (`RING_CAMILLA_ENABLE_RATE_ADJUST=False` is
the shipped state) — THE load-bearing unknown; ring geometry beyond 2×128 is a
measured choice; snapclient 0.31.0 unpinned, hw_params negotiation vs the ioplug's
single-valued constraints untested; `--latency`/A-V sync re-derivation; GROUPING_*
constants read at module import). #2508 (remnant EOL + music tap only-with-a-writer +
HANDOFF-multiroom.md truing). #2581 (bonded round-trip never exercised ring-armed;
#2526 moved the release barrier onto the ACTIVE ring's writer flock, #2539 shipped
the remnant doctor guard — both pinned by tests only, no hardware pass ever).

Strike-ladder KEEP adjudication retrieved from the predecessor lifeline (lens C,
2026-08-17, PR #2659 disposition): (a) confirm path is a real websocket round-trip —
transients genuinely return ok=False, so collapse-to-1-strike bounces a HEALTHY box
to loopback on one blip; (b) no other rescuer exists (`check_active_ring_split_transport`
returns a CheckResult only; `jasper-camilla-recover.service` never touches the
coupling) — deletion = wedged boxes recover NEVER; (c) oneshot + no Restart= + no
timer means an in-memory strike counter accumulates nothing — persistence is what
buys two-strike without a retry. Refinement: escalation cadence is ownership-
dependent (auto-owned real confirm vs operator-frozen synthesized). STOP 16 ruling:
deletion CANCELLED, adjudicated-keep in #2659's body. Phase 2 retargets this exact
machinery: recovery = re-emit/re-arm the ring; unrestorable → park SILENT and LOUD.
The three legs must survive the retarget (persistence still buys two-strike; the
transient-blip rationale still holds; the park replaces loopback as "the outcome
that restores" — now "the outcome that fails loudly").

P8B §5 (v3.1) extraction for Phase 1: bonded active-follower chain today is
snapclient `--soundcard hw:Loopback,0,6` → Camilla captures `hw:Loopback,1,6`
(rate_adjust: TRUE, sole tracker per inv-5 `check_grouping_rate_adjust`), raw hw
S16_LE no plug → per-driver crossover → ACTIVE ring → outputd. Pair 6 is
double-booked BY DESIGN: loopback-coupled solo box uses it as the Camilla→outputd
content hop; bonded follower uses it as snapclient→Camilla ingress. Option B =
replace only the ingress half with a fourth conf.d ring PCM. v1's geometry
objection WITHDRAWN in v2 (target_level governs playback side; capture side runs
RING_CAMILLA_TARGET_LEVEL=128/chunk 128/queuelimit 1 → 2-slot rule satisfied);
the real blocker is the never-shipped rate-track-against-ring combination.
Risks carried: 5.2 import-time env, 5.3 snapclient unit's stale "NEVER an ALSA
sink" claim, 5.4 unpinned snapclient.

Transport-map observation AT HEAD (hypothesis for the census, not a fact):
`docs/audio-paths.md` still says pcm.jasper_ref "survives only as a shipped
definition until P9-E deletes the aloop PCMs" — FUTURE tense, so the P9-E
asoundrc reduce appears un-landed; renderer-ingress lanes 0-4 remain aloop with
per-lane ring arming operator-explicit and fleet-default UNARMED. If confirmed,
axis-2 (renderer ingress) is a live aloop consumer independent of the coupling,
and #2481's "snd-aloop no longer loads on any box" done-clause is reachable only
via an owner scope ruling on axis 2. Census adjudicates; owner decides.

DISPATCHED: Phase 0 census enumerator (Sonnet, read-only, background) over the
pinned worktree — raw inventory to scratchpad, axes labeled (1-coupling /
1-grouping / 2-renderer-ingress / 3-correction-AEC / docs-prose / unknown).
Adjudicator (Opus) follows on its return; census SSOT lands at
`captures/LOOPBACK-CENSUS-2026-08-17.md`. Task list: 1=census, 2=Phase-1 design,
3=Phase-1 hardware (#2581), 4=Phase-2 deletion; 1→2→3→4 dependency chain.

### 2026-08-17 — Census returned; adjudicator + fleet prober dispatched

Enumerator: 405 files / 6,209 matching lines at the pinned SHA; four narrow-pattern
cross-checks with zero gap; tests condensed (flagged honestly). Axis file-entries:
AXIS-1 224, AXIS-3 74, AXIS-2 47, AXIS-1-GROUPING 26, noise (network-loopback +
generic-English "coupling") 86, shared/unknown 8. Surprises carried into
adjudication: coupling_reconcile.py + coupling_auto.py are ONE bidirectional state
machine (not partitionable into loopback-vs-ring lines — Phase 2 is a reshape, not
a branch-prune); snd-aloop module + check_loopback() are axis-agnostic shared infra;
`snd_aloop_rate_adjust_oscillation_reason` (camilla_config_contract) is shared
AXIS-1/AXIS-1-GROUPING; modprobe/asoundrc are multi-axis; two docs reference a
nonexistent `/etc/alsa/conf.d/zz-jts-loopback.conf`; #2481's body cites
reconcile.py:211-216 but the constants live at :214/:218; ring_assets.py is a
boundary case. Opus adjudicator dispatched → census SSOT. Sonnet fleet prober
dispatched (STRICTLY read-only) → measured jts.local + jts4 state for the design
brief.

### 2026-08-17 — OWNER DIRECTIVE ×2 (recorded verbatim in intent)

1. **Research the biggest risk FIRST**: rate-tracking CamillaDSP against a ring —
   the load-bearing unknown. Subagent researches online (CamillaDSP upstream,
   snapcast 0.31.0) + in-repo (ioplug delay/avail/blocking semantics, RING_CAMILLA
   constants, bonded follower's generated config, inv-5, the aloop oscillation
   record). Dispatched (Opus) → `captures/RESEARCH-rate-tracking-ring-2026-08-17.md`.
   Conductor hypothesis handed to it AS A TARGET (not a conclusion): aloop inserts
   a third (kernel-timer) clock forcing two trackers; the ring has no clock of its
   own, so back-pressure may propagate the DAC clock to snapclient and make it the
   sole tracker — rate_adjust stays OFF (shipped Ring-A semantics), dissolving the
   unknown. Crux: does the ioplug report honest delay/avail to a writer (repo
   records snd_pcm_delay LYING on aloop-family devices).
2. **Conductor plan-review gate**: Fable personally reviews any plan BEFORE
   proceeding — elegant · modular · clean · single source of truth · separation of
   concerns · 80/20 · not overly complex. Only after that pass does the plan go to
   the independent adversarial design review, and only after 0/0 does
   implementation get delegated. End state: the promised land — grouping on the
   ring, then a large net code deletion.

Gate order for Phase 1 (and Phase 2 alike): census+research+fleet evidence →
design proposal (Opus) → CONDUCTOR VALUES REVIEW (me) → independent adversarial
design review to 0/0 → seal → implementation waves (delegated) → per-PR gates at
0/0 → hardware pass → merge → deploy+validate.

### 2026-08-17 — Fleet probe returned (read-only): both boxes at 620065dff, ring
plumbing green on both; TWO findings the design must absorb

Measured (same-day coordinated deploy, ~6 min apart; identity clean on both;
`JASPER_FANIN_CAMILLA_COUPLING=shm_ring` on both; snd_aloop loaded on both with
7/8 pairs registered — the #2508 remnant note; snapcast 0.31.0-1 installed on
both, all snap units disabled+inactive; grouping.env/grouping-outputd.env/
grouping-voice.env(0B) present on both):

- **jts.local**: dual-Apple composite roleful READY, active graph
  `active_speaker_staged_startup.yml`, ACTIVE ring + Ring A live, writer locks
  held by the expected owners. Doctor 3F/7W — all in the KNOWN parked
  mic/chip-AEC/calibration domain; every ring/coupling check green.
  **FINDING 1: `active_speaker_setup.grouping_allowed = false` on the designated
  LEADER** (jts4 has `true`). Hypothesis (unverified): the gate blocks bonded-
  MEMBER conversion on roleful crossover boxes, not leading. The design must pin
  the gate's exact semantics from code — if it blocks leading too, the owner's
  leader/member test plan needs a ruling.
- **jts4**: InnoMaker I2S, `sound_current.yml`, content.ring + Ring A live (note:
  ring FILENAME differs by role — content.ring vs active-content.ring; no tooling
  may hardcode one). Doctor 5F/5W.
  **FINDING 2: `jasper-grouping-reconcile.service` AND
  `jasper-source-intent-reconcile.service` are `loaded failed failed`, dead since
  this morning's boot (14:16), no auto-retry, 9+ h.** Root cause from journal:
  boot-time `systemctl restart jasper-usbgadget.service` timed out (11 s) →
  source-intent-reconcile failed its usbsink leg → cascaded into
  grouping-reconcile (`event=multiroom.reconcile.source_converge_failed`) → both
  parked FAILED. Doctor independently flags it ("USB combo consistency … a failed
  post-toggle kick"). Also 3 doctor checks timed out identically at 15 s (capture
  relay + 2 crossover-v2 checks) — clustered, likely Zero-2W contention, not
  independently diagnosed. OWED: heal jts4's reconcilers (one systemctl start)
  when hardware work begins; Phase 1 design must state how grouping-reconcile
  failures self-heal — this is a live specimen of the no-retry gap, same class as
  the strike-KEEP leg (c) (oneshot + no Restart= accumulates nothing).

Probe-brief errata (mine): I invented `fanin-coupling.env` — the real file is
`/var/lib/jasper/fanin.env` on both boxes. And `/var/lib/jasper/` is 0770
jasper-voice:jasper — unprivileged `ls` silently returns EMPTY (looks like "no
files"); every future probe uses `sudo -n` there.

### 2026-08-17 — CENSUS SEALED: `captures/LOOPBACK-CENSUS-2026-08-17.md`
(1,408 lines, 8 sections, 60 consumers). The census is the SSOT — designs cite
it, nothing restates it. Digest of premise-changing findings ONLY:

1. **The "~126 branch sites" handoff number is mislabeled**: 126 reproduces as
   literal-'loopback' LINES; only 39 are code tokens and only 12 are genuine
   control-flow branches (`ast` method in the census); coupling_auto.py has ZERO
   axis conditionals. Phase 2 is prose-heavy and state-machine-reshape-shaped,
   not branch-prune-shaped.
2. **P9-E is PENDING at HEAD** (asoundrc still ships all 10 blocks incl.
   pcm.jasper_ref:314; pair 5's deletion was P9-C). Coupled hazard:
   `check_fanin_asound_wiring` FAILs when jasper_ref is MISSING — any reduce must
   move that check in the same PR or it reds the fleet.
3. **23 loopback-landing states** enumerated (census §4) — Phase 2's design
   contract.
4. **Strike-ladder ground truth**: strikes ARE persisted —
   `/var/lib/jasper/ring-confirm-strikes.json`, 24 h window, clear-on-success,
   `ring_confirm_strike_write_failed` when unwritable. (This CONFIRMS the KEEP
   ruling's substance; my lifeline restatement of leg (c) had garbled the
   oneshot/in-memory hypothetical into a claim about the code. The retarget must
   preserve these persistence semantics.)
5. **A bonded box cannot be ring-armed today — enforced from both directions.**
   Phase 1 retires a GATE, not just adds a lane. And the remnant doctor guard
   self-degrades to permanent WARN (not ok) the moment GROUPING_LOOPBACK_* holds
   a ring value (`_pair_from_loopback_pcm` → None) — Phase 1 owns the guard's
   replacement in the same wave.
6. **In-tree prose justifies rate_adjust BY aloop's clock specifically** ("which
   HAS a clock to track") while the ring pins RATE_ADJUST=False — consistent
   with the dissolution hypothesis; the research memo arbitrates.
7. **"snd-aloop no longer loads on any box" is UNREACHABLE by Phase 1+2**: AXIS-2
   (renderer ingress, pairs 0-3) and AXIS-3 (correction, pair 4) survive; all 11
   AXIS-2 consumers are STAYS (none read the coupling token). Reaching literal
   zero-aloop needs an OWNER SCOPE RULING on axes 2+3 — flagged, does not gate
   Phase 1 or the coupling deletion.
8. Stale-prose ledger in census §7 — headline: "solo-stereo-only until ring v2
   (P8)" at 5 sites, 3 of them SHIPPED operator-facing strings; ring v2 landed.
   Phase 1 PRs own the sites they touch; the rest ride Phase 2's subject sweep.
9. Method caution recorded: file-level NOISE labels proved unsafe 2/22
   spot-checks — nobody trusts a NOISE label without reading the site.
10. Ticket-traceability note: #2481/#2539 have zero in-tree occurrences (GitHub-
   only); in-tree attribution for the barrier is #2285/P9-C via PR #2389. #2508
   is the one ticket wired in-tree (6 sites + 2 test pins).

### 2026-08-17 — RESEARCH MEMO SEALED: `captures/RESEARCH-rate-tracking-ring-2026-08-17.md`
**The load-bearing unknown is DISSOLVED — by archaeology, not by the hypothesis.**

- Verdict PARTIAL on my hypothesis: mechanism CONFIRMED (ring has no clock;
  back-pressure propagates the DAC consumption clock to the writer — pinned by
  test_occupancy_tracks_reader_drain), premise REFUTED — #2481's "the follower
  needs rate-tracking-on-ring" is false at HEAD: enable_rate_adjust is keyed off
  the PLAYBACK device (camilla_yaml.py:468-484), playback resolves to the ACTIVE
  ring unconditionally (output_topology.py:1895-1918), the follower emit
  overrides capture only (baseline_profile.py:1839, :2388-2400) → a ring-armed
  follower already GENERATES rate_adjust:false / chunk 128 / target 128. And the
  stronger reason: rate_adjust on ANY ioplug capture is INERT BY CONSTRUCTION
  (ioplug card=-1 → no HCtl → no rate-shift element → SetSpeed falls through,
  camilla v4.1.3 device.rs:896-917). The choice is forced, not preferred.
  Snapclient = sole tracker (server→consumption clock).
- The stale pin: tests/test_multiroom_follower_config.py:942-968 asserts the
  aloop-era shape (True / >=1024, DAC-playback fixture) — Phase 1 retargets it.
- Ioplug delay to a writer: ring-fill only, max 8 ms @ 2×128 (pcm_jts_ring.c:
  440-460) — honest for Camilla, a FIXED LATE OFFSET for snapclient
  (stream.cpp:379-381) → re-derive DEFAULT_CLIENT_LATENCY_MS (=0 today,
  jasper/multiroom/config.py:80).
- Oscillation CANNOT transfer: requires rate_adjust:true AND async resampler on
  aloop capture; ring emits no resampler. (Repo's "two controllers fighting"
  attribution also doesn't match v4.1.3 — prose-truing candidate when touched.)
- Spikes S0-S5 with pass/fail signals (memo §3): S0 snapclient-negotiates-ioplug
  GATES EVERYTHING (watch snd_pcm_avail_delay -77, snapcast#1154 open); S1
  inertness; S2 delay stability; S3 the architecture gate (2 h + 24 h soak,
  zero hard syncs, soft sync unpinned, CPU <10%); S4 dead-reader cliff (SIGSTOP
  camilla → bounded dropout, recovery <400 ms); S5 free config-shape check.
- Evidence gaps named: the ~194 ms Ring A+B measurement justifying
  RATE_ADJUST=False survives only as a code comment with a STALE doc pointer
  (audio_runtime_plan.py:1671-1677); the S0-sync bench bar (acoustic p99 <5 ms,
  ≥24 h soak) was NEVER met (~0.65 h accepted) — #889's owed items land in
  Phase 1's hardware pass or get explicitly re-scoped.
- Memo proposes jts4=LEADER / jts.local=FOLLOWER (dummy loads = the safe box for
  the re-pointed Camilla; possibly the only active-follower-capable box) — this
  INVERTS the owner's leader designation → OWNER-DECISION item riding the design
  review, after the designer pins grouping_allowed's exact gate semantics.

DISPATCHED: Phase 1 design author (Opus, background) →
`captures/DESIGN-PROPOSAL-grouping-ring-2026-08-17.md`, citing census + research
memo + fleet digest; 10 design questions, owner values binding, wave map with
per-PR test strategy + hardware plan embedding S0-S5. Gate: conductor values
review → independent adversarial design review to 0/0 → seal → implement.

### 2026-08-18 — DESIGN DELIVERED (1,094 lines, no STOP) + CONDUCTOR VALUES
REVIEW: **PASS with three amendments**

The proposal's own premise corrections: PC-1 the follower emits chunk **1024**
not 128 (FOLLOWER_LOOPBACK_MIN_CHUNKSIZE clamp INSIDE the emitter — the research
memo missed it; geometry is therefore 1024×2 slots, 42.67 ms, S16_LE, 2ch);
PC-2 grouping_allowed=false on jts.local is COMMISSIONING READINESS (staged-not-
applied baseline), blocks both roles, unrelated to the interlock; PC-3 P8B risk
5.3 already fixed at HEAD; PC-4 the memo's leader/follower inversion is
unnecessary (an active-speaker LEADER traverses the same ingress through
camilla#2 with the identical emitter call — keeps the owner's designation AND
snapserver off the Zero 2W); PC-5 the doctor landmine is worse than the census
recorded (pair 6's ONLY registration is the grouping constant; removal without
re-sourcing FAILs every loopback-coupled box → PR-2 re-sources before PR-3
flips). Headliners: one PCM name replaces three constants (new import-cheap
jasper/multiroom/grouping_ring.py; both import-time env overrides DELETED —
risk 5.2 retired); own conf.d 62-jts-ring-grouping.conf (60- is machine-rendered
and would refuse); gate NARROWED not deleted — real subject is the dumb-member
dac_content lane (CONTENT_BRIDGE=direct), one predicate derived from the SAME
function that writes the lane, D1 stops hand-rolling; doctor check re-homed to
audio_runtime; inv-5 widened to every bonded role at WARN (never-nanny);
snapclient version PROBE not apt pin; UMask=0007 + RestartSec=3s unit fixes;
pcm_substreams stays 8; 5 PRs + HW (PR-1/3/4 safety-class 3-lens), single deploy
at PR-5; census-S4 all five sites owned by Phase 1.

CONDUCTOR SPOT-CHECKS (all seven CONFIRMED in code): the 1024 clamp + comment;
D1's interlock + fall_back_to_solo; _derive_registered_pairs' three contributors
+ all-or-nothing None (the landmine is real, and stronger: grouping-contributor
None kills the WHOLE derivation); D2's strand-the-leader detail verbatim; EG-6
LIVE BUG (migration migrates JASPER_GROUPING_ENABLED, load_config reads
JASPER_GROUPING, sole occurrence is the migration line); dumb-member
CONTENT_BRIDGE=direct pin + active-branch clears; RestartSec=2s, no UMask.

AMENDMENTS (to the design review as conductor findings — verify, don't inherit):
A1 S5 sequencing contradiction (gates PR-1 but sequenced post-deploy; needs a
bonded roleful endpoint that can't exist pre-OD-2): PC-1 is code-verified +
T-6-pinned; demote S5's field-read to opportunistic pre-deploy baseline, never a
PR gate. A2 EG-6 is FIXED in PR-5 (fix-findings rule; direction determined by
evidence of what writers write), not merely surfaced. A3 three questions the
review answers from code: (i) fan-in's behavior on a bonded active endpoint
under shm_ring with Ring A reader-less (bounded? parked by the grouping
reconciler?); (ii) the leader's camilla#1 Ring-A-capture → File(snapfifo)
playback — never-run combination, pacing soundness from code; (iii) the new
predicate's import direction (coupling ← multiroom) — cycle risk + can the
coupling reconciler's process actually read GroupingConfig (file perms)?

DISPATCHED: independent adversarial design review (Opus, background), 0/0 bar,
delta rounds same reviewer. OD-1..OD-4 going to the owner in parallel; EG-6 fix
noted to the owner under the standing fix-findings rule.

### 2026-08-18 — OWNER RULINGS on OD-1..OD-4 (all four = the recommended option)

OD-1 **jts.local = LEADER** (owner's original designation stands). OD-2 **apply
jts.local's durable baseline** as hardware step 0.2 (dummy loads, emit gate in
path). OD-3 **keep dummy loads** — electrical evidence grade; acoustic p99 stays
owed against #889, recorded in the evidence file. OD-4 **#2481 closes narrow**
(grouping-on-ring, pair 6 one consumer left, module loads for axes 2/3);
zero-aloop successor decided when Phase 2 completes. EG-6 fix proceeding under
the standing rule (owner did not object).

### 2026-08-18 — DESIGN REVIEW RETURNED: **NOT SEALED — 2 B / 9 SF / 6 N / 4
confirmed-stronger** (`captures/DESIGN-REVIEW-grouping-ring-2026-08-18.md`).
Root cause of both blockers: the design derived the FOLLOWER chain and assumed
the LEADER is the same chain — but the leader runs a SECOND CamillaDSP
(camilla#1 program bake) from a DIFFERENT emitter.

- **B1**: narrowed gate legalizes a silent bond — bonded leader's camilla#1
  bake defaults capture to plug:jasper_capture (camilla_config_contract.py:26;
  bake emitter takes no capture kwarg), which under shm_ring NOBODY WRITES —
  "digital silence with every daemon healthy… That trap is QUIET"
  (camilla_yaml.py:415-424's own words). D5 is what blocks it today; the design
  said D5 needs no change. OD-1 + jts.local@shm_ring makes this the FIRST
  hardware configuration. Fix direction: the bake emitter must become
  coupling-aware (Ring A capture under shm_ring), not merely re-blocked.
- **B2**: the 1024 clamp is itself the defect on the ring path — chunk 1024
  into the ACTIVE ring's 256-frame playback buffer breaks the repo's own
  chunk-spans-buffer rule; audio_runtime_plan.py:2754-2786 already declares 128
  "what generated YAML actually emits" under shm_ring (a second owner the
  design's 1024 claim falsifies); the solo path coherently emits 128. §3.5 was
  INVERTED: 128 is coherent, 1024 is the snd-aloop artifact. Geometry
  re-derivation required — compounded by S7 (.delay is SLOT-QUANTIZED: bigger
  slots = coarser sync signal vs snapclient's 2/5 ms medians — argues SMALLER
  slots, opposite of §3.2's direction).
- SFs: S1 wake rate 500 Hz not 188 (tick_ns_for clamps 2 ms); S2
  StartLimitBurst=4 violates ring-writer burst>5 rule; S3 the ring-writer
  cadence enumeration (jts_ring_shm.h:115-160) unswept + its guard can't see
  snapclient; S4 invalid_grouping flips blocked→allowed silently + T-5 pins an
  unreachable cell; S5 D5 reorder DOES change operator-visible reason; S6
  deletion set incomplete BY SUBJECT (6 solo-stereo-only sites in 4 files, one
  in graph_carrier.py:414 which NO PR opens; 13 HANDOFF-distributed-active
  sites; member_config.py); S7 phase-lock [I] cites a mechanism the code lacks
  (POLLOUT on space, not slot-edge); S8 jts4-as-passive = dumb member on FIFO —
  never opens the grouping ring; the active-FOLLOWER instance is undemonstrated
  on this fleet (leader instance demonstrates the same emitter call — evidence
  file must state the scope honestly); S9 two test modules break unnamed.
- Adjudications: A1 UPHELD deeper (grouping_follower.yml unobtainable on this
  fleet in ANY phase); A2 UPHELD on fix / DISSENT on placement — EG-6 gets its
  OWN single-gate PR outside the stack (accepted); A3(i) = B1 on the leader;
  A3(ii) premise refuted (camilla#1 captures plug:jasper_capture, not Ring A);
  A3(iii) clean (bool parameter keeps import direction legal — state as
  constraint; both reconcilers root, perms fine).
- Settled-do-not-relitigate: §3.1, §3.3, §3.4 no-rm, §4 env deletion, §5 shape,
  §6.1 ordering, §8.1; all seven #2672 pins verified; strike ladder untouched;
  no Phase-2 leak.

FIX ROUND dispatched to the SAME design author (v2 in place, changelog at top,
per the P8B precedent) → delta re-review by the SAME reviewer.

### 2026-08-18 — v2 DELIVERED (17-row changelog, both blockers accepted, five
evidence-backed DISAGREEMENTS with the review)

- **Geometry inverted: 128 × 16** (period=chunk=128 — the relationship every
  shipped ring carries; n_slots=16 → same 42.67 ms jitter depth v1 wanted, 8×
  finer slots → 2.667 ms sync-signal quantization vs snapclient's 2/5 ms
  medians; tick ~1500 Hz = Ring A's shipped cost class). The clamp + constant
  DELETED (use-site becomes unreachable — both driver_domain callers move to
  the ring this phase).
- **B1 resolved coupling-aware via the EXISTING resolver**:
  `coupling_capture_kwargs_from_env()` (fanin_coupling.py:885) gains the bake
  as its fourth caller — capture half only (misuse raises loudly), File/SNAPFIFO
  sink untouched, atomic with the gate narrowing in PR-5 (not separable: the
  gate is what makes a coupling-blind bake reachable), new hardware signal S6
  (leader silence-with-healthy-daemons = explicit FAIL), mutation kills added.
- Wave map v2: **PR-0** EG-6 outside the stack (own deploy) · PR-1 platform
  (safety) · PR-2 doctor re-source · PR-3 transport flip + clamp deletion
  (safety) · PR-4 snapclient version probe · PR-5 gate+bake atomic (safety) ·
  PR-6 truth/sweeps + THE stack deploy · HW (S5 demoted → S0-S4 → S6 new).
  Spine PR-2→3→5→6; PR-0/PR-4 order-independent.
- §9 retitled "the change set," explicitly non-exhaustive for prose; PR-6's
  sweep owns completeness (v1's "owns all five" completeness claim WITHDRAWN).
- Author's five disagreements (each with evidence, for the delta to adjudicate):
  (1) a THIRD breaking test module the review missed —
  test_active_speaker_driver_domain.py pins the clamp at :228/:233/:238/:245/
  :248 [CONDUCTOR SPOT-CHECK: CONFIRMED]; (2) B1's "second consumer on Ring A"
  parenthetical wrong (Ring A is SPSC; camilla#2 reads the GROUPING ring) —
  pacing half kept as R11+S6; (3) S7's quantization is an upper bound, not an
  identity (stage_frames ≠ 0 for partial writes) — STRENGTHENS the conclusion;
  (4) S6 undercounts: 19 round-trip sites in HANDOFF-distributed-active, not 13
  (v1 said 6 — three counts, three answers = why the sweep owns completeness);
  (5) S3's enumeration has FIVE entries incl. bluealsa-aplay, snapclient is the
  SIXTH writer.

CONDUCTOR SPOT-CHECKS on v2's new load-bearing claims (all three CONFIRMED):
the resolver's docstring quote verbatim + its callers; the
_effective_camilla_chunksize_setting "loopback/hardware-floor value, not the
ring runtime value" warning verbatim (the repo HAD adjudicated 128-under-ring;
v1 missed the owner); the third test module's clamp pins at the cited lines.

DELTA RE-REVIEW dispatched to the SAME reviewer: verify both blocker
resolutions + all 17 changelog rows against the v2 text; adjudicate the five
disagreements by RE-MEASURING its own claims; settled list stays settled;
verdict to 0/0 or findings.

### 2026-08-18 — DELTA VERDICT: 0 B / 1 SF / 4 N — one line from seal

- **B1 RESOLVED under attack**: reviewer re-derived the resolver's EIGHT
  shm_ring keys incl. playback_device=RING_PLAYBACK_DEVICE (splatting would
  redirect camilla#1 to Ring B — v2 already states this and takes the capture
  half only, the only two keys the bake emitter accepts); {} byte-identical
  under loopback (pinned); atomicity adjudicated the ONLY safe ordering.
- **B2 RESOLVED, deletion claim held**: one definition, one use site, exactly
  one production call of the emitter, exactly two driver_domain callers — both
  moving to the ring; 128<1024 would fire on every emit. All new numbers
  re-derived (2.667 ms / 42.67 ms / 1500 Hz / 45.3 ms max delay / 16 the
  inclusive ceiling). The fix repairs the ACTIVE-ring playback incoherence for
  free. The one potential regression (v1's period_time-clamp argument) checked
  and answered; S0 gates it with an inert conf.d.
- Disagreements: #4 and #5 WITHDRAWN by the reviewer on its own recount (19
  sites; 5 entries — its 13 came from a head -40 truncation "presented as an
  enumeration, the same failure the finding was about"); #1/#2/#3 SPLIT with
  the author's halves standing (three breaking modules not two; Ring-A
  parenthetical withdrawn — SPSC enforced, camilla#2 reads the GROUPING ring;
  quantization = upper bound, bounded form better). Two reviewer sub-claims
  wrong, two overstated, one incomplete; zero findings overturned; three
  strengthened.
- Remaining: **ND-A (SF)** — T-8's kill cites emit_sound_config's raise
  (passive path); the BAKE emitter's loud path is a TypeError; row must be
  unsatisfiable through the wrong emitter. Nits: ND-1 resolver has 5 call
  sites not 3; ND-2 the bake chunk resolves to the DAC-profile floor (128 on
  jts.local — decline safer than stated); ND-3 n_slots 16 = JTS_RING_MAX_SLOTS
  hard ceiling (depth no longer tunable upward — name the bump path); ND-4
  PR-6 row still says thirteen.

MICRO-ROUND dispatched to the author (v2→v2.1, five edits, nothing else). On
return: reviewer confirm-by-inspection → SEAL → builders dispatch.

### 2026-08-18 — **DESIGN SEALED: 0 B / 0 SF / 0 open nits** (three dated
review sections, 1,198 lines). ND-A closed at the signature (grep of the bake
emitter's kwarg range for playback tokens = 0 → the splat kill IS a TypeError);
ND-2 settled THE AUTHOR'S WAY (reviewer re-measured: its 128 was
outputd_period_frames read from a conf.d sentence about the ring slot; the
bake's unset-env chunk on jts.local is 256 = two Ring-A slots per chunk;
"the author's diagnosis of my error is exact"). D-1..D-5 all verified in the
v2.1 text. Reviewer's closing record: three of its own sub-claims corrected
across the rounds (13-vs-19, 4-vs-5, 128-vs-256), each correction strengthening
the finding it touched, none overturning one. Round-1 settled list and the
owner's §13 rulings never re-opened. Remaining gates = per-PR adversarial
reviews (PR-1/3/5 = 3-lens panels) + the hardware pass.

### 2026-08-18 — PR-0 DELIVERED: PR #2695 (de885238b), gate dispatched

Direction PROVEN, not assumed: git -S shows the key mismatch existed at
grouping's FOUNDING commit (3e85f3558) — wrong at birth despite its own
"Mirror config's env keys" comment; the sole control-plane writer
(_write_grouping) and the reader both use JASPER_GROUPING; the migration array
was the outlier. Fix = one array entry + contract test (shell_keys ⊆
config_keys — subset per the transit precedent, NOT exact equality: the
migration's own comment documents the 10 newer wizard-only keys as intentionally
excluded — a judgment call flagged for the gate). Mutation proofs complete
(bug-reintroduction red on both named tests, byte-identical restores, no-op
control green). Lanes: test-fast 98 / test-merge 21562, exits captured unpiped.
Fleet evidence REAL: jts.local + jts4 + jts3 greps clean (healthy = no stale
key in jasper.env; live grouping.env carries correctly-keyed JASPER_GROUPING=off
written by the real writer). Builder removed its worktree after push-verify.

CONDUCTOR NOTE (brief-tightening, my omission): the builder ssh'd jts3
READ-ONLY for fleet evidence — harmless (grep/cat only, no load, no writes) but
jts3 is the peer session's box and MY brief omitted the fleet-permission list.
Every future brief that could touch hardware carries it explicitly:
jts.local + jts4 granted, jts3 FORBIDDEN, jts5 unplugged.

PR-0 GATE dispatched (single adversarial gate, Opus): re-derive direction,
adjudicate the subset-vs-equality call, re-run mutation/no-op logic, verify
fleet-evidence claims are labeled honestly.

### 2026-08-18 — PR-1 DELIVERED: PR #2696 (f5023bd78, base 200d54578,
MERGEABLE); 3-lens panel dispatched

547 lines: conf.d 62- (128×16/S16_LE/2ch explicit) + grouping_ring.py (99
lines, registry-membership REFUSED per §3.1) + install wiring + T-1..T-3 (355
test lines) + a prose-only second commit scoping its own loose docstring claim.
Lanes: test-fast 725 / test-merge 21573 unpiped; ruff/bash -n/shellcheck clean.
Mutation table: 4 rows (one BEYOND brief — the bit-depth leg "was an argument
standing in for a guard"), same-length-literal discipline applied, re-run
verbatim at HEAD, harness kept in scratchpad.

**BUILDER DRIFT FINDING against the sealed design (for the panel to
adjudicate before any erratum lands):** §3.4's "the deploy does not bounce
jasper-snapclient.service" is FALSE at HEAD and at the sealed SHA — snapclient
is in JASPER_CORE_GRAPH_PARK_UNITS; the rm -f asymmetry decision SURVIVES on
ORDERING (install_jts_ring_platform runs before install_systemd_units in
main(), so nothing has stopped the writer at unlink time). Two smaller
same-paragraph corrections: the "bounces the graph so no live writer" claim is
backwards; the cited C warning is about the .writer.lock, not ring files
(analogue, not identity). The builder wrote the ORDERING truth into the shipped
comment + T-3 docstring. Also: ring_asset_presence deliberately does NOT cover
the new conf.d (it is the shm_ring ACTIVATION gate — a missing grouping conf.d
must not refuse the coupling's arm; T-3 pins the install line instead, the real
sibling precedent); two stale 61- enumerations trued in passing;
HANDOFF-multiroom/distributed-active zero-hit evidence recorded for
no-doc-impact; FORBIDDEN_TEST_PCM_TOKENS non-entry reasoned in the body
(ingress side of the fence).

3-LENS PANEL dispatched (correctness / hearing-safety / resilience, parallel,
own checkouts, no ssh): each lens re-derives its domain; the §3.4 ordering
claim and the presence-gate scoping are named adjudication targets; the design
erratum lands only on panel confirmation.

### 2026-08-18 — PR-0 GATE: 0 B / 2 SF / 3 N — fix round dispatched; AND an
ENVIRONMENT HAZARD caught by the gate

- **SF1**: the comment the PR body cites as its justification states the
  OPPOSITE of the code — "still reaches the daemon" is false three ways (the
  unit stopped loading grouping.env at c3ea20e1b — the comment was written by
  #1217 while the line existed; load_config reads ONLY the file, never
  os.environ; nothing reads migrated keys from env). A THIRD-HOP RELAY failure:
  DA-0086's evidence line → #1217's comment → this PR's body, none verified.
  Empirically proven inert (sandboxed migrate run). Clears: correct the two
  lines in the file being edited; the PR's conclusion survives on its stated
  rationale.
- **SF2**: the benign case was the only case modeled. Bare legacy
  JASPER_GROUPING=on (no BOND_ID) + empty grouping.env → validate errors →
  route_mode=invalid_grouping → coupling_supported_for_route blocks shm_ring →
  **ring disarms to loopback on every deploy/boot** + doctor FAIL (snapcast
  check gates on enabled alone). Needs BOTH preconditions; jts.local + jts4
  doubly protected (gate verified the wizard-wins strip-without-moving branch
  empirically). Clears: body names the consequence; a test pins the bare-flag
  shape.
- Nits: doc-map matches TWO subsystems not one (conclusion still right);
  "mirrors transit" is actually the inverse direction (test docstring honest,
  body overstates); repro test fails KeyError-first (readability assert).
  Gate's adjudication: SUBSET IS RIGHT (stricter than transit on the
  dead-literal axis; equality would force migrating 10 wizard-computed keys —
  the unverified call the PR correctly refuses). Residual named: the excluded
  set lives only in prose — difference-pin (config_keys - shell_keys ==
  documented exclusions) recommended, offered to the builder adopt-or-decline.
- Gate strengths noted: writer-reader-outlier triangulation held under
  re-derivation; both tests derive truth from source text; scope exact.

**ENVIRONMENT HAZARD (cross-agent checkout contamination):** at 08:22 something
ran `checkout FETCH_HEAD` against the GATE'S worktree, moving its HEAD from
de885238b to f5023bd78 (the PR-1 branch) mid-review. The gate caught it on a
routine tree check, proved the blast radius bounded (every conclusion-bearing
run collected the fix's tests, impossible at the foreign SHA), discarded the
one tainted guard run, restored, re-ran with rev-parse assertions BRACKETING
the command. Suspected cause: a concurrent agent using a generic checkout path
(gh pr checkout into a shared clone). MITIGATION NOW STANDING: every review/
build agent creates a UNIQUELY-NAMED directory it owns; never checkout in a
directory it didn't create; bracket conclusion-bearing runs with HEAD
assertions; warnings sent mid-flight to all three PR-1 lenses. Every future
brief carries an explicit unique path.

### 2026-08-18 — ROOT CAUSE FOUND (Lens A's disclosure): subagents spawn with
THE CONDUCTOR'S WORKTREE as cwd, and the adversarial-gate agent definition runs
`git checkout --detach FETCH_HEAD` in cwd — the PR-0 gate and Lens A were
time-sharing MY worktree unknowingly (reflog: my branch → de885238b [PR-0
gate] → f5023bd78 [Lens A] → de885238b [gate restoring]). CONSEQUENCES:
(1) my worktree sits DETACHED at de885238b — no reads from it, no restore,
until lenses B+C finish (they may be inside it); all my earlier spot-checks
predate the gates (clean); captures/ writes go to the main checkout (separate
tree — unaffected). (2) STANDING RULE: every gate/panel brief carries an
explicit unique checkout path AND "your spawn cwd is the conductor's worktree —
run NO git commands there." Lens A's migration pattern is the gold standard:
`git archive <sha> | tar -x` into an owned dir — no .git, structurally immune
to checkout, verified 2311 files byte-identical, blob-hash brackets on every
conclusion-bearing run.

### 2026-08-18 — PR-1 LENS A (correctness): 0 B / 1 SF / 3 N

- **Drift adjudication: builder RIGHT on both halves** (snapclient in
  JASPER_CORE_GRAPH_PARK_UNITS[4]; platform-before-units on both install
  paths, already pinned by test_install_ring_platform_sequencing) — the §3.4
  design erratum is warranted, plus a second erratum line for the T-3
  doctor-presence deviation (justified: ring_asset_presence is the coupling
  ARM GATE — joining it would let a missing grouping conf.d refuse the arm;
  61- has no presence check either; ".install-manifest is web-assets only").
- **SF (lens A's own catch): the builder's REPLACEMENT universal is false on
  1 GB-class boxes** — park_audio_clients_for_core_graph_restart has a THIRD
  call site (park_low_memory_build_units, systemd-units.sh:991) that runs
  BEFORE the ring-platform step on both paths, gated by build_swap_required
  (MemTotal < 1200000 kB). On a low-memory box snapclient IS stopped at rm -f
  time; the only restore paths are inside install_systemd_units (after) or the
  EXIT trap. Decision unchanged (not unlinking correct everywhere); the
  comment + T-3 docstring must QUALIFY the conditional. The stakes are real:
  a maintainer who disproves the universal on a low-memory box concludes the
  comment is stale and adds the rm -f — the exact split-brain it prevents.
- Nits: rm-f set now has TWO guards, the new one hardcoding literals where the
  sibling imports ring_assets constants (use the constants — one owner); the
  T-3 deviation should be recorded as a design erratum; 62- echo content
  unasserted (transitively covered).
- Re-derived: 13-mutation table (own harness, isolation mutations beyond the
  builder's), ioplug accepted-key set from the parser, import cost measured
  (40 modules/~27 ms vs reconciler's +122/+57), §3.1 negatives, inverse-miss
  sweep across five languages, scope exact (no PR-2..6 file touched, nothing
  opens the PCM).

Holding the consolidated PR-1 fix round until lenses B + C land.

### 2026-08-18 — PR-1 LENS B (hearing-safety): 0 B / 1 SF / 0 N; conductor
probe REFUTES its venv-trap claim (lane evidence stands)

- Lens B was the OTHER contamination half (its FETCH_HEAD checkout in the
  conductor worktree = reflog HEAD@{1}); detected via vanished files, migrated
  to an owned dir, no residue, all conclusion-bearing runs bracketed.
- **SF (real, validated): T-1 catches key names, not malformation.** The
  "exactly one PCM" guard matches only `pcm.X {` — the ALIAS form
  (`pcm.jts_ring_active_playback "jts_ring_grouping"`) is INVISIBLE to the
  suite, and 62- sorts LAST in conf.d so it holds override authority over both
  siblings: a future alias edit could repoint the ACTIVE driver-domain ring at
  the S16 ingress ring, suite green. Plus stray-brace/top-level-token
  malformations caught by NOTHING (empirical: identical stray-brace mutation
  → 60- caught 8-failed, 61- caught 13-failed, 62- 440-passed-NOT-caught —
  the siblings' incidental coverage is not parity). Clears with B's validated
  5-line guard (brace/quote balance + prefix/suffix-empty around the one
  block). Alsa-lib parse-error blast radius labeled inferred (macOS), finding
  independent of it via the alias cases.
- **Fence adjudication: builder RIGHT, no entry owed now or in PR-3** — the
  fence's own rule fences audio-past-the-crossover; the grouping ring's reader
  is a CAPTURE device whose output graph passes emit gate + volume_limit +
  protection (fail-safe-to-solo on unprovable). Precedent note for the record:
  #2326 fenced the ACTIVE ring at definition time IN the defining commit — so
  had the category call gone the other way, "consumer ships later" would not
  have deferred it. CARRY TO PR-3: the fence comment's unfenced-set
  enumeration gains the grouping clause WITH the consumer (AGENTS.md #11).
- Inertness verified STRICTER than the precedent claim (ACTIVE ring shipped
  with 24+ files wired + marker gate; this ships ZERO consumers); namespace
  unique across all 24 shipped pcm./ctl. names; both install.sh hunks inside
  the dry-run heredoc (prose, not executable); permissions walk clean (sticky
  bit load-bearing vs cross-member lock unlink; pre-create race = PR-3's,
  same-kind as renderer lanes).
- **CONDUCTOR PROBE (decisive, one command): PYTHONPATH BEATS the editable
  finder** — fake package resolved under PYTHONPATH; WITHOUT PYTHONPATH the
  venv resolves jasper to THE MAIN CHECKOUT (the real trap, already covered by
  the standing pin-PYTHONPATH rule + the import-check both builders ran and
  passed). Lens B's "PYTHONPATH cannot beat it" is REFUTED — overstated from
  its tangled mid-contamination setup. ALL LANE EVIDENCE STANDS. Its
  PYTHONSAFEPATH/sys.path[0] cwd note remains a good hygiene addition.

Panel so far: A 0/1/3 · B 0/1/0. Awaiting C, then ONE consolidated fix round
(A-SF low-memory conditional in comment+docstring; B-SF the malformation
guard; A-nits constants-not-literals + echo content; design errata to the
author after C: §3.4 bounce claim + T-3 doctor-presence deviation).

### 2026-08-18 — PR-1 LENS C (resilience): 0 B / 2 SF / 2 N — PANEL COMPLETE:
0 blockers / 4 SF / 5 nits across three chairs

- **C-S1 (the panel's best catch, third layer of the onion):** the builder's
  replacement ORDERING rationale is true but NOT OPERATIVE — (a) not a
  differentiator (the three unlinked rings also have live writers at that
  instant); (b) the named harm is unreachable (the bounce sequence is
  park-writer → reader re-attaches → start-writer; had grouping.ring been
  unlinked the bounce closes the window exactly as for the coupling three).
  THE TRUE REASON IS FAILURE-ESCALATION ASYMMETRY: jasper-fanin carries
  StartLimitAction=reboot (stale ring = fatal attach = REBOOT MID-INSTALL
  before the manifest → unlink MANDATORY, the documented trap); snapclient
  carries NO StartLimitAction by explicit unit design ("follower degrades,
  visible; never reboots the household") → stale ring costs 4 retries + a
  failed unit → unlink buys nothing. Durable unit property vs call-order
  accident. C proved the ordering is ALREADY mechanically pinned (reorder
  main() → pre-existing sequencing test reds) — a red test beats a comment,
  another reason ordering must not be the recorded rationale. Also proved the
  comment's cited premise is unpinned by THIS PR (drop snapclient from the
  park list → none of the PR's tests red).
- **C-S2:** the builder's doctor-presence refusal disposed only of the GATING
  form. The non-gating observer exists: check_ring_reader_stall's
  hand-maintained tuple — the grouping ring is EXACTLY the witness-less
  C-ioplug shape the check exists for (at PR-3 a stalled grouping ring leaves
  snapclient active+connected while frames vanish; /state grouping reads unit
  state + snapserver connectivity → silent failure). Clear is nearly free:
  add the ring to the tuple NOW — empirically inert (verdict.present=False →
  skip) and self-arming when PR-3 writes the file.
- C nits: N1 echo asymmetry (cosmetic); **N2 PRE-EXISTING AGENTS.md drift:
  "SKIP_RESTART=1 … forwarded into install.sh" but install.sh NEVER READS IT
  (zero hits across deploy/) — routed to PR-6's truth ledger.**
- C confirms: presence-gate scoping CORRECT (deferred-loud acceptable for the
  inert phase; at PR-3 missing conf.d → snapclient failed 4×, visible, never
  reboots); ring-enumeration sweep clean; check_ring_writer_lock_exclusivity
  is prefix-generic → grouping gains exclusivity coverage FREE at PR-3;
  tmpfiles untouched → §3.4 geometry residual unreachable at this PR. C also
  hit the contamination (same shared-worktree mechanism), migrated to a
  no-.git export, 2311/2311 byte-verified brackets.

**CONDUCTOR ADJUDICATION (A vs C on the recorded rationale):** not a factual
conflict — both verified the ordering holds mechanically; they differ on which
reason gets RECORDED. C's escalation asymmetry wins: durable, differentiating,
and immune to A's low-memory conditional (which made any who-is-parked-when
universal false regardless). Fix round rewrites the comment + T-3 docstring on
escalation asymmetry with no parked-when universal; A's SF is thereby SUBSUMED
(its delta confirms). The design erratum will carry the escalation-asymmetry
reason. Conductor worktree RESTORED to its branch (clean, 6e569e8dc).

### 2026-08-18 — OWNER DIRECTIVE ×3: "100% clarify every other place the old
audio graph is actually used — there may be some confusion there"

Correct instinct — the session's sharpest catches all lived in the gap between
DEFINED and ACTUALLY-OPENED-AT-RUNTIME (the bake's plug:jasper_capture, pair-6
double-booking, jasper_ref reader-less, lane-7 writer-less+reader-less on ring
boxes). DISPATCHED: old-graph usage cartographer (Opus, background, owned
no-.git export at current origin/main) → the OPENS-verb truth table (every
aloop pair 0-7 + every aloop-backed alias × box state → who opens it or
NOBODY, evidence-cited; DEFINES/REFERENCES/OPENS rigorously distinguished) +
a READ-ONLY fleet snapshot (which Loopback substreams are actually open on
jts.local + jts4 right now, fuser-named openers, lane-arming + coupling env;
jts3 FORBIDDEN + jts5 untouched carried explicitly in the brief this time) +
a CONFUSIONS LEDGER (every wrong/ambiguous prose claim about old-graph usage
across audio-paths.md, the census, the sealed design, AGENTS.md, HANDOFFs —
incl. testing the "fleet default is unarmed" claim against fleet reality).
Deliverable: captures/OLD-GRAPH-USAGE-MAP-2026-08-18.md. Feeds Phase 2's
deletion walk + the axes-2/3 owner decision. Task #7.

### 2026-08-18 — PR-0 FIX ROUND at 8fb8320ee; gate delta dispatched (owned
no-.git export mandated)

SF1 fixed with the builder re-deriving all three disproofs itself (git show
c3ea20e1b; read_env_file_state pure file read; no-env-reader grep) — comment
now states unmigrated-is-inert with the citation; body names the three-hop
relay plainly. SF2: bare-enable test at the config boundary + the full chain
in the body + the wizard-wins strip verified empirically in a sandbox. Nits:
doc-map both-subsystems fixed (the glob its first grep missed);
**difference-pin ADOPTED** (config_keys - shell_keys == _DOCUMENTED_EXCLUDED_
KEYS) with the sharp reason — SF1's stale claim sat NEXT TO this exact prose
enumeration with no test catching it; readability asserts added
(mutation 3's informative result: a different validation fired, the BOND_ID
assertion still caught — specificity question handed to the delta). Round-2
mutations all bracketed; lanes 99 / 21563 unpiped. Delta to the SAME gate with
the owned-export discipline (its first pass ran in the conductor worktree —
the contamination mechanism — never again).

IMPLEMENTATION OPEN. Dispatched in parallel (disjoint files): PR-0 builder
(Sonnet — EG-6 migration-key fix, own worktree pr0-grouping-migration-key,
evidence-determined direction, contract test, fleet-evidence command in body)
and PR-1 builder (Opus — the inert platform: 62-jts-ring-grouping.conf at
128×16/S16_LE/2ch, jasper/multiroom/grouping_ring.py, install+manifest+doctor
presence, the no-rm-f asymmetry comment, T-1..T-3 with T-2's mutation kills;
worktree pr1-grouping-ring-platform). Both branch from CURRENT origin/main
(rule 0); design citations pinned at 6e569e8dc — drift at HEAD is report-first.
Gates follow on PR-open: PR-0 single adversarial-gate; PR-1 three-lens panel.
Conductor merges after 0/0 + disposition posted; spine PR-2→3→5→6 dispatches
serially as gates clear.

### 2026-08-18 — v2.1 DELIVERED (D-1..D-5); ONE number dispute carried to the
confirm pass

T-8 rewritten as T-8a (bake capture follows the coupling — call-site revert →
plug:jasper_capture under shm_ring → named test red) / T-8b (splat → TypeError
at the BAKE call site, keyword-only signature declares no playback kwargs) with
an explicit do-not-satisfy-through-emit_sound_config note citing the
mutation-harness trap. D-2: resolver has FIVE call sites → bake is the SIXTH
call site / fourth CLASS of caller (v2 conflated the counts). D-3: ND-2
accepted in DIRECTION, refuted in VALUE — the bake's unset-env chunk is
LatencyFloor.camilla_chunksize = 256 on the Apple profile (dac.py:381-390),
not the reviewer's 128 (which is outputd_period_frames, a different field);
two slots per chunk on jts.local — residual smaller than v2 stated. The author
also disclosed its OWN initial misread of _resolve_camilla_int (docstring
suggested max(env,floor); line 212's unset path returns the floor) — the nit's
direction survives only because it re-derived at the line. D-4: n_slots 16 =
JTS_RING_MAX_SLOTS ceiling stated + the three-language bump path named. D-5:
thirteen→nineteen. CONFIRM PASS dispatched to the reviewer (five rows +
adjudicate the 256-vs-128 number + re-verify T-8b's signature claim). On
SEALED: builders.

(NOTE on this file's ordering: several 2026-08-18 entries were inserted at
mid-file anchors rather than appended, so physical order ≠ strict chronology
from the "DESIGN SEALED" entry onward. All content is present; from here on,
entries append at the tail.)

### 2026-08-18 — **PR-0 SEALED 0/0/0**; disposition POSTED
(pull/2695#issuecomment-5328592896); auto-merge ARMED

Delta verdict: all five items cleared on independent re-derivation. The
difference-pin adjudicated a COMPLETE PARTITION (7 migrated + 10 documented-
excluded = all 17 reader keys, disjoint, exact — an 18th key fails the suite
until classified). The BOND_ID substring adjudicated CORRECTLY SPECIFIC
(mutation 3 proves the weaker form survives deletion of the pinned check — the
half-guarded-site class; load_config's docstring publishes the validation
order as contract). The gate corrected its OWN pass-1 single-line grep on the
record (two multi-line os.environ sites, both correctly excluded — "the
builder's evidence was tighter than mine"). The import-jasper pre-check earned
its keep AGAIN in the owned-export flow (first resolution went to the
spawn-cwd worktree via sys.path[0]; re-ran from inside the export). Gating
precondition: three pytest legs IN_PROGRESS at review time — auto-merge armed
(--squash --auto), mergeStateStatus=BLOCKED pending checks, state to be
verified MERGED before anything depends on it (exit-code-lies rule). Deploy
folded into the stack deploy (fleet greps found no box carrying the legacy
precondition — an isolated deploy observes nothing; recorded in the
disposition).

CONDUCTOR SEQUENCING NOTE: PR-2 now waits for PR-1's MERGE, not merely its fix
round — the C-S2 stall-tuple fix moved a PR-1 edit into
jasper/cli/doctor/audio_runtime.py, the same file PR-2 re-sources. No two
agents touch one file; the spine is PR-1 → PR-2 → PR-3 → PR-5 → PR-6, with
PR-4 slotted where no files overlap.

### 2026-08-18 — OLD-GRAPH USAGE MAP SEALED:
`captures/OLD-GRAPH-USAGE-MAP-2026-08-18.md` (owner-directed; 17-entry ledger)

**Headline: the old graph is nearly unused on the granted fleet.** jts.local:
NOTHING opens aloop (all 32 substreams closed; /dev/snd holders = outputd's 2
Apple DACs + fan-in's hw:UAC2Gadget). jts4: exactly ONE — fan-in holds
hw:Loopback,1,3 as the usbsink IDLE READ FALLBACK with no writer (hardware
cause, errno=19 no gadget — a capture reading silence forever; NOTE for
Phase 2/axis-2: deleting the module breaks this fallback path on gadget-less
boxes unless fan-in learns to park the lane instead). Both boxes fully armed
BOTH senses (all 4 renderer lanes ring-armed AND shm_ring coupling); neither
camilla touches aloop. Dead in EVERY state: pair 5, hw:Loopback,0,3,
pcm.jasper_ref-as-device, the ctl.* aliases, 4 vestigial constants. Openers
only in states the fleet doesn't run: pairs 0/1/2/4 (unarmed lanes), 6
(direct bridge / bonded endpoint), 7 (loopback coupling / bonded leader bake).

**The confusion has ONE repeating shape, now named:** three RUNTIME layers
name an aloop device that is never opened — fan-in's boot log
(event=fanin.config_loaded output=hw:Loopback,0,7 on every boot of both
ring-coupled boxes), a /state field, and a persisted env file — and the
doctor's check_fanin_service asserts one of them (_FANIN_EXPECTED_OUTPUT_PCM).
Measurement-preferring investigators get misled by all three. PHASE-2 NAMED
REQUIREMENT: the deletion design fixes the three-layer lie (STATUS/log carry
the RESOLVED transport).

### 2026-08-18 — **PR-1 SEALED 0/0/0 — PANEL FULLY CLOSED**; disposition
POSTED (pull/2696#issuecomment-5328781854); auto-merge ARMED

Fix round at b6dcd4128 (5 items + testing-tooling.md truing via the fourth
doc-map subsystem). THE COUNTER-MEASUREMENT: the builder could not reproduce
lens B's 60-/61--caught numbers, probed the mechanism (value parsers
brace-match into block bodies → an outside-the-block brace — also the alias's
shape — is invisible to ALL THREE ring conf.d files), corrected the docstring
in its own commit, and named the residual (60-/61- have NO structural guard —
ledgered for a later PR). Builder also disclosed a SIGTERM-mid-mutation
near-miss (61- left dirty in its own worktree, caught on git status, restored
byte-identical) → NEW RULE: mutation harnesses carry trap-EXIT restores.
Scoped confirms: A PASS (rationale now matches the pre-existing sequencing
guard's own docstring — ONE OWNER for the reason; the low-memory site named
in-tree as why order-claims don't hold); B PASS — re-measured and WITHDREW its
own numbers as a harness artifact (StringIO substitution behind "a control
that could not fail"); its guard shipped STRONGER than proposed
(exactly-one-unnested-block); C PASS (tuple-drop mutation re-run by hand;
inertness verified from the shipped loop; corrected its own transposed
function labels). Reviewer self-corrections this PR: three — each
strengthening the finding it touched. Auto-merge armed; MERGED to be verified
by state before branch cleanup.

### 2026-08-18 — **PR-1 #2696 MERGED (squash 87910abc6)** — the inert grouping-
ring platform is on main; PR-0 in flake-rerun

CI all green on the merged HEAD (3 pytest legs + full farm); content verified
on origin/main independently by the builder. Branch deleted AFTER a fresh
state==MERGED verify; builder worktree removed clean (content squash-carried).
The 60-/61- structural-guard residual got a durable owner: **issue #2697**
(hygiene) — filed because the residual outlived the PR that discovered it;
carries the harness caution (no-op controls that cannot fail; trap-EXIT
restores). PR-0 #2695: py3.11 leg failed ONE test —
test_dsp_apply::test_cancelled_dsp_writer_waiter_cannot_acquire_late, a 100 ms
wall-clock deadline missed by 60 ms on a runner simultaneously showing the
documented loaded-runner signatures (errno=24/11 spawn retries in an unrelated
test); py3.12/13 green on the same commit; diff has zero DSP-apply overlap;
same HEAD passed 21,563 locally. Rerun triggered per discipline (never
conclude from diagnosis alone); occurrence recorded on #2658 (the flake-
generator issue) naming this test a bounded-wait/fake-clock candidate. Merge
watch round 2 armed (distinguishes rerun-in-progress from rerun-failed-final).

### 2026-08-18 — **SESSION CLOSE: everything in flight LANDED.** PR-0 #2695
MERGED (squash d1a4f4c27 — the current main tip; the py3.11 flake rerun went
green, occurrence on #2658) and PR-1 #2696 MERGED (squash 87910abc6). Both
branches deleted after state==MERGED verifies; all builder worktrees removed;
residual #2697 filed. Handoff prompt FINAL at
captures/NEXT-SESSION-PROMPT-2026-08-18-loopback-phase1-landing.md (state
section patched to both-merged); memory hub updated. The session's tally:
census + research memo + usage map sealed; design sealed 0/0 over three
rounds; 2 PRs merged at 0/0 with dispositions posted; 5 owner rulings
recorded (OD-1..4 + the plan-review gate); 3 new traps minted (spawn-cwd
contamination → owned no-.git exports; no-PYTHONPATH→main-checkout; trap-EXIT
mutation restores); 2 tickets touched (#2658 occurrence, #2697 filed).
REMAINING (next session, per the handoff): PR-2..PR-6 → hardware pass
(#2581/#2508/#2481 close) → Phase 2 design → the deletions. The owner listens
at the end.

Ledger highlights: L-04 "fleet default is unarmed" — TRUE as a code default,
FALSE as a fleet description (both boxes fully armed; census §6.2 had honestly
flagged this exact unknown + the settling command); **L-05 the SEALED DESIGN
§3.2 N6 tags fleet-default-unarmed [M] — a measured-tag on a claim the census
flagged unmeasured — and load-bearing for "256×16 shipped, not exercised";
the truth STRENGTHENS the fallback (256×16 runs in production on 8 PCMs on
this fleet)** → design erratum list grows to three items. Premise findings:
(1) coupling↔multiroom welded BOTH directions — both boxes today cannot bond
without disarming to loopback; AXIS-1 is the REQUIRED bonded transport until
our PR-5 lands (confirms the campaign, sharpens the ordering); (2) SIX
operator probe sites hardcode correction_substream and are ALREADY silently
broken on the armed fleet (write to a cable nothing reads; product spawns
correct via correction_play_device) → routed to PR-6's ledger, with a
scope-judgment note (any executable site may need the resolver, not a prose
fix); (3) two unconditional doctor hazards (jasper_ref-removal FAILs
check_fanin_asound_wiring — P9-E moves check+conf together;
check_loopback FAILs on absent card — on jts.local that reds a box over a
card NOTHING uses) → Phase-2 inputs; (4) "armed" means two different things
and no document says so → PR-6 vocabulary fix. Calibration honesty kept:
AXIS-2/3-survive claims HOLD as code claims; the survivors are DORMANT on
this fleet — the cartographer corrected its own subagents' over-calls.

### 2026-08-19 — **CLOUD SESSION PICKUP (waves PR-2..PR-6).** Conductor is a
cloud Claude Code session on branch claude/loopback-retirement-phase1-survey-7bg0mu
(cut from main 4b7e76d); builders/gates are sub-agents per the method. No Pi
access — the hardware pass runs later via an operator-run Pi-side agent from a
brief this session will produce; gh CLI absent (GitHub MCP instead); the laptop
trap catalog re-derived for container paths (/home/user/JTS; owned no-.git
exports for gates unchanged). captures/ (gitignored, laptop-local) transported
onto the branch as commit 2f3640e9f (7 docs, TEMPORARY — drop before merge;
harvest lifeline appends from branch history back to the laptop copy). State
verified fresh: PR-0/PR-1 merged with dispositions confirmed by comment id;
#2481/#2508/#2581/#2697 open, no post-08-18 scope changes; no PR-2..6 work
anywhere (no branches, no PRs, wave markers all pre-PR-2: no
_OUTPUTD_CONTENT_ALOOP_PCM, clamp present, snapclient on hw:Loopback,0,6,
StartLimitBurst=4, no UMask, header enumerates five writers).

**POST-SEAL DRIFT (design pin 6e569e8dc is an ancestor of HEAD; PR #2719
landed 08-18 21:39, after seal):** (1) `_active_speaker_box_state()` →
`_output_topology_state()` returning `(active, flat_allowed)`;
(2) `outputd_grouping_env` gained `flat_output_allowed` — the clearing branch
is now `if active_endpoint or not flat_output_allowed:`, so §5.2(b)'s
two-input predicate can no longer mirror the writer BY CONSTRUCTION. PR-5
carries this as required analysis: the predicate gains the third input sourced
the same way as the writer's caller; C-16's import-direction constraint
preserved; T-5 fixtures widened to the third input. (3) `ring_topology_ready`
now refuses CONTRACT_UNCONFIGURED (+ a new strict variant) — the design never
cites it; context only, no claim falsified. Citation drift: camilla_yaml.py
±182 lines (bake emitter def now :3978, clamp :3839-3843; signature
byte-identical so T-8b's TypeError kill holds); graph_carrier.py:414→:440;
setup_status.py grouping_allowed sites → :719/:836/:1210 (re-read before
hardware step 0.2); the web-wizard resolver call sites moved;
tests/test_fanin_coupling_reconcile.py churned +204/−101 since seal — re-read
the #2672 pins at PR-5 time.

**Design errata 3 → 6** (conductor-found, ride the same errata message):
(4) §10.1's PR-3 row says "four test modules"; §8.2's own table names SIX on
PR-3 — the table governs. (5) §10.1's PR-6 row lists "EG-3/EG-4",
contradicting §12's EG-4 disposition "Fix in passing in PR-3" — §12 governs;
EG-4 moves to PR-3's brief, PR-6 verifies it landed. (6) §5.2(b)'s "gate and
writer cannot disagree" is false at HEAD post-#2719 (third input) —
adaptation above.

**CONDUCTOR RULING — §6.1(b) split across PR-2/PR-5** (ambiguity found by the
design-reader): PR-2 = §6.1(a) + the C1-costed contributor drop / signature
change (required by T-4's "retargeted to the NEW second return value" and the
§8.2 breaks-table's test_doctor_grouping_remnant.py→PR-2); PR-5 = the rename +
re-home (its row claims those verbs). PR-2's builder escalates if the code
makes the split incoherent rather than improvising.

**Execution shape:** waves land as commit series on THIS branch (owner
topology ruling pending — commits stay sliceable into per-wave branches);
NOTHING merges until the owner's Pi agent validates the deployed stack (owner
re-sequencing of §10.1's merge-then-deploy order). Dispositions recorded here
when each review returns; posted to PRs when PRs exist.
