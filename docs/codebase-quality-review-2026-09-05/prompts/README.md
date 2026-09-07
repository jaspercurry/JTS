# Prompts to take each non-A attribute to A

One self-contained prompt per attribute from the review's grade table. Paste a file's full contents as
the kickoff message of a fresh Fable session on this repo. Each prompt makes the three rules the owner
cares most about impossible to miss — Fable delegates to Opus/Sonnet and does no lane work; every PR
gets `/code-review` and `/simplify` before merge; every finding is re-verified at HEAD — and each ends
at a plan gate the owner triages before code is written.

| File | Issue | Attribute | From |
|---|---|---|---|
| `P1-secrets.md` | #4193 | Secrets (NN-3) | C+ |
| `P2-deploy-integrity.md` | #4194 | Deploy integrity (NN-4, NN-8) | C+ |
| `P3-resilience.md` | #4195 | Resilience | B |
| `P4-observability.md` | #4197 | Observability | B− |
| `P5-structure-and-god-files.md` | #4199 | Separation & SSOT, followability, god files | C+ / C |
| `P6-right-sizing.md` | #4200 | Right-sizing | C |
| `P7-tests.md` | #4201 | Tests | B− |
| `P8-docs.md` | #4202 | Docs and prose | B |
| `P9-voice-loop.md` | #4208 | The voice loop (wake → turn → answer) — a concern lane, built on `docs/VOICE-AUDIT-2026-09-05.md` | B / B− / C |
| `P11-web-ui.md` | #4212 | The web UI (`jasper/web/`, assets, nginx) — a concern lane, built on the #4211 hand-off and `docs/web-ia.md` | C+ / B− |
| `P12-hardware.md` | #4213 | Attached hardware (DACs, I2S amps, mics, usbsink, accessories, the two reconcilers) — a concern lane, built on ADR-0235 and the #4027 hand-off; asks from other lanes go to #4027 | A− (safety) / C (input side) |

Hardware/audio safety is already A− and needs only R-016's belt-and-braces row (in P4's doctor work
and P3's clamp event). The tuning zone is parked: its steward stood down with wave 9 on main
(close-out on #3769; PR #4138 open, owner-gated). Every prompt keeps the zone read-only and files
the tuning-zone rows its attribute needs under an owner-gated heading in its plan, so the owner
ticks them at the plan gate instead of a lane widening into a 263k-line domain unasked.

## Sequencing

Every lane starts with a scout and a one-page plan that stops at the owner's triage; those phases
never conflict. The hard ordering is only about code landing on `main`:

1. **P6 deletions → P5 moves → P5 splits.** Do not move what is about to be deleted; do not split a
   file that is about to move. P5's cycle fix, layers contract and deferred-import rule need no wait.
2. **P5 moves → P7 execution.** Tests travel with their modules in P5's PRs and die with their
   subjects in P6's; P7 rewrites only what neither list names until both have merged.
3. **P5 moves → P8's last PR** (the stale-path pass). Everything else in P8 starts now.
4. **P6's peering deletion → P3's `peering/state.py` fix**, if both are open at once.
5. **The last two voice PRs (#4198, #4203) and their close-out → P9.** Four of six are merged.
   Its Waves 3 and 6 also wait on the owner's ten-turn timeline numbers (the brief's ledger row 0.2).
6. **#4210 → P11.** The web lane's first job is merging the Sound-URL PR that is already open;
   its Phase D (`active_speaker/commissioning_*`) waits on P6's owner decision about the v1-apply
   chain, and its C.R1 wave carries P6's wizard `main()` deletions, so those two coordinate on
   #4212 before either branches.


## Where each lane runs

The owner has three Claude accounts: **James Crane** (remote), **Dip** (remote) and **Space Hater**
(the owner's machine — the only one that reaches the Pis). One lane per account at a time: start
the next lane on an account when the previous one has posted its handoff issue, or when it is
parked at its plan gate waiting on the owner. Anything that needs a box runs on Space Hater; a
cloud lane's hardware ask is a comment on #4027 with the exact command and the expected reading,
and the lane on Space Hater answers on the asking lane's issue. There is no separate ops lane.

| Order | James Crane (remote) | Dip (remote) | Space Hater (local, hardware) |
|---|---|---|---|
| 1 | **P6 done** (handoff #4387). Account idle | P2 round 2 (#4194: wave B landing — #4403, #4368, #4392; row 6, wave C and the Cargo workspace go to the handoff) **and** P5 (#4199: 12 rows on `main` incl. #4354; six PRs rebasing, then the handoff) | **P11 paused** (#4212 note 5573398816: W1 + C.S3/C.S4/C.S5 on `main`; W2 deploy-and-eyeball owed) **and P3 done** (handoff #4416: P0 + wave 1 on `main`; jts4 `37e9557a5`, idle forks 4.8 → 2.4/s) |
| 2 | — | P7 tests #4201 (after P2 round 2; execution after P5's moves merge) | — |
| 3 | — | P8 docs #4202 (stale-path pass last) | — |

James Crane has no Fable credit and about a sixth of its weekly budget, so it runs P6 on Opus to
completion and nothing after; P7 and P8 move to Dip. P3, which touches the clamp paths and the
daemons' restart policy and needs box measurements, runs on Space Hater after P11, where Fable and
the hardware both are. An account may run two lanes at once when each has its own session. An Opus
coordinator gets one extra sentence in its kickoff: read every builder's diff before trusting its
report, and one row at a time.

**Owner at the box.** jts.local is a night of merges behind `main`; say "deploy jts.local" to the P3
session once jts3 and jts4 read clean. Then, on jts.local: move the host volume slider to ~50 % then ~25 % (the
#4209 check, recipe on #4324; a Space Hater session reads `event=usbsink.volume_observed`), then speak ten turns
(P9's gate 0.2; the session reads `event=turn.timeline`). jts3: with a Space Hater session watching the journal (recipe on #4324), unplug the XVF3800 (H1: the reboot-window number) and listen for the mic-loss
cue (H2), then replug. jts4: nothing. Read ADR-0244 (the
server-VAD path is deleted rather than kept as a knob; the May A/B lost 0/5, 3/5, 0/5; a re-run
restores it from git history) and object on #4208 only if you want that experiment path kept.

**State on 2026-09-07 17:15 UTC (paused).** Done: P1 (#4279), P4 (#4327), P12 (#4324), P9 (#4385),
P6 (#4387), P3 (#4416). Paused with a handoff note: P11 (#4212, 5573398816). Landing their cut: P2
(wave B) and P5 (six PRs), each followed by a handoff issue. Not started: P7, P8. ADR ledger #4405 in
use (0248 health gate, 0250 host-clock, 0251 control park, 0252 P2's staging tree); the 0249 copy was
deleted. Hardware: jts4 `37e9557a5`, jts3 `d4cb8b49d`, jts.local `162ab4088` (the owner's word).
Since the review's baseline `2bc95106d` (2026-09-05 10:57 UTC) about 245 PRs have merged; code lines
at 14:30 were `jasper/` +1.9k, `rust/` −0.4k, `deploy/` +0.4k, `scripts/` −1.0k, `tests/` +10.4k —
the tree is not smaller yet; the shrinking rows are the deferred ones. The coordinator's hourly check
is a Routine that reads with the unauthenticated REST API (the authenticated quota is shared by every
session and ran out once at 14:00 UTC).

**Duplicates (owner's rule, 2026-09-07).** No duplicate code stays because it is untested; untested is
the reason to converge, not to keep. A duplicate found in scope is converged this round by the lane
that owns the file, with one behaviour pin on the real path. An issue records a duplicate; it does
not park one.

**Merge word (rule change).** The owner's triage at the plan gate is also the merge word for every
PR in that plan, sensitive tier included, once `/code-review`, `/simplify` and (where the tier
demands it) `/adversarial-review` have no open blockers. No lane waits for a per-PR word; four
lanes stalled on that on the first night.

**Paused (owner's call, 2026-09-07 14:45 UTC).** Each running lane finishes a cut and hands off; nothing
new starts. The cuts: P2 lands wave B (rows 4, 5, 6) and stops before wave C and the Cargo workspace;
P5 lands its nine built PRs and #4354 and stops before rows 16–20; P11 lands W1 and stops before the
URL moves (W2–W6); P3 lands P0 and wave 1 (R1–R6) and stops before wave 2. Then, with the owner at
home: deploy `main` to jts.local, the box items above, and a fresh grade pass over `main` by the
coordinator. **Restart order when the pause ends:** P7 tests (#4201) and P8 docs (#4202) first, since
both sweep what the moves left behind; then the deferred rows from each handoff (P2 wave C and the
Cargo workspace, P5 rows 16–20 and PR 19, P11 W2–W6, P3 waves 2–4, P6's env-knob contract), each
resuming from its triage, not from a new plan.

**Overnight mode.** When the owner is away, the coordinator session runs the lanes on a
half-hourly check. The channel is each lane's own issue, nothing else. On each check that finds `main` or an issue moved, an Opus subagent reads
`main`, the open PRs, #4027 and the newest comments on every active lane issue; the coordinator then
posts only where a lane waits. A comment starting **Coordinator (overnight)** is the owner's word for
the night: plan-gate triage, the merge word, the answer to every open call and, after a handoff, the
number of the session's next lane. Quiet hours post nothing. The check must be a Routine: a session-only cron dies with the idle
container (it did, 05:45–12:00 UTC on 2026-09-07). Held for the owner: tuning-zone rows
(parked), any row that needs the owner at a box, deploys to jts.local (spares jts3 and jts4 only),
and any change to a non-negotiable. The lane side is one paste into every running session:

> Overnight mode until the owner says otherwise. The coordinator session manages the lanes; your
> channel is your lane's issue, the one your kickoff named. Once an hour, read its newest comments.
> A comment that starts "Coordinator (overnight)" is the owner's word for the night: plan-gate
> triage, the merge word, the answer to every open call, and the number of your next lane. Between
> comments, work: execute the triaged plan and merge on green once /code-review, /simplify and,
> where the tier demands it, /adversarial-review have no open blockers; at a new plan gate, post the
> plan and keep the hourly check going until the coordinator's comment lands. Tuning-zone rows stay
> parked; anything that needs the owner at a box waits; if you can reach the Pis, deploy only to
> jts3 and jts4 tonight. When your lane is done, post "<lane> done" with the handoff issue number on
> your lane issue; when the coordinator names your next lane, read that issue's body and start it in
> this session. Do not wait for a human paste tonight.

Kickoff message for a lane — paste it into a fresh session on the named account, changing only the
issue number and the lane name in the last sentences:

> You are Fable: the architect, strategist, coordinator, debugger and the one with taste. You do
> not do the work yourself — every survey, scout, edit and test run is delegated to a Sonnet
> (mechanical) or Opus (judgement) subagent, and you name the model on every `Agent` call. Every
> PR gets `/code-review` and `/simplify` before merge, no exceptions. The goal is a smaller,
> simpler codebase: less cruft, less prose, one source of truth per fact, clear contracts, no god
> files — never bigger. Your full brief is issue #4193 in jaspercurry/JTS (quality lane P1,
> secrets). Read `AGENTS.md`, then read that issue in full and follow it exactly; it ends at a plan
> gate where you stop and wait for me. My answer there is also the merge word for every PR in your
> plan, sensitive tier included, once its review passes have no open blockers. Other lanes run
> concurrently; their issues are named in the brief.

On Space Hater add: *You are on my machine and the Pis are reachable; other lanes' hardware asks
arrive as comments on #4027 — answer them on the asking lane's issue.*

The local account's GitHub API quota is one per machine: builder briefs there forbid `gh`; the lane
session polls CI itself, one slow waiter, through `gh api` no faster than every two minutes;
rebase instead of `gh run rerun`; targeted tests locally, the full suite on CI (the doctor and
hardware streams' lessons on #4028 and #4027; each prompt's Mechanics repeats them).

Three lanes at a time keeps `main` calm: every lane rebases before each push. Every earlier
program has stood down or handed off — the general steward (#4085), the tuning steward (#3769),
the doctor/state stream (#4028), the idle-efficiency review (#4139), the web coordinator (#4211),
the hardware coordinator (#4027) — and their queues are folded into the lanes. The
2026-09-05 deploys (jts.local and jts4 on `3959524a6`, jts3 on `964baa037`) carry the steward
round's #4163/#4187; fan-in and outputd are stable on them.

## Where each hand-off went

- **Voice loop** (brief `docs/VOICE-AUDIT-2026-09-05.md`, #4186 merged; #4191, #4192, #4206 merged;
  #4198 and #4203 open and rebasing after them; none hardware-verified beyond the owner's deploy of
  `3959524a6`, which carries #4191 only). Decision: the
  wake→turn loop becomes its own concern lane rather than being split across P3–P8, because its
  latency ruler (`event=turn.timeline`) and wave order only make sense in one head. It will own
  `jasper/voice_daemon.py`, `jasper/voice/`, `jasper/cues/`, `jasper/tools/`, `jasper-voice.service`,
  the wake legs and the provider adapters — that is **P9 (#4208)**, and P1–P8 now name it as the
  owner of those files; the WakeLoop / `daemon_main` god-file rows moved out of P5 into it. Landing order for the six PRs:
  #4186, #4191, #4192, #4206, #4198, #4203 (the last two rebase after their pairs merge); #4186 and
  this review both edit `docs/doc-map.toml`, so whichever merges second rebases once.
- **Web UI**: the coordinator's hand-off is #4211 and the lane is **P11 (#4212)**; its first job
  is merging #4210 (Sound URLs under `/sound/`, `/correction/` aliases deleted). Phase B is already
  on jts.local and jts3 (`3959524a6` / `964baa037`); the owner's phone eyeball of the new landing
  and the two hubs is the acceptance for it.
- **Attached hardware**: the coordinator's hand-off (ADR-0235; eleven of thirteen PRs merged, #4189
  and #4205 in flight — the latter in the voice loop's files, and its ADR renumbers because `main`
  took 0238) is the lane **P12 (#4213)**; #4027 stays the program's tracking issue and the address
  for other lanes' hardware asks. Its first row is the USB host-volume regression #4209.
- **Doctor/state stream** (brief and standing entry point: #4028; ADR-0233): landed `--core`
  (#4177), the `/state` contract (#4166) and nine more doctor PRs, measured `--core` against
  `jasper-deploy-health` on jts4 and redeployed jts4; its last message reads as a hand-off. Its
  queue is folded: the warn→skipped sweep, the two `state_aggregate` payload helpers, the unread
  outputd park and the memory-pressure row are P4's; the deploy switch (carrying the config-free
  `--core` fix) + deletion and the installer's first-boot `jasper-control` gap are P2's; fan-in's `last_drop_ms`
  is P3's Rust row; the shield-through-cancel twin is an owner-gated tuning row in P5. One
  un-isolated non-critical warning on jts4 is P4's to find. Treat the stream as stood down.
- **Idle-efficiency review** (#4139): stood down; its measured baselines, tickets and leave-alone
  list are folded into P2, P3, P4, P5, P6, P9 and P11. No ops lane replaces it: the lane running
  on Space Hater answers hardware asks posted on #4027.
