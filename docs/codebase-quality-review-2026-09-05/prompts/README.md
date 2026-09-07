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
| 1 | P6 right-sizing #4200 — running on Opus since 2026-09-07 00:45 (#4326, #4329 merged); P1 is done (handoff #4279) | P2 round 2 from the handoff #4248 — #4305 disposed (`c5a2f07bb`); the 11-row plan on #4194 (comment 5563789051) is triaged (comment 5564510021, the merge word); executing waves A → B → C; P4 is done (handoff #4327) | **Two sessions.** P12 #4213 (all Phase 3 rows merged; left: H1 and H2 on jts3 with the owner, the #4209 slider on jts.local, the jts4 deploy for HW-3/HW-5, R6, #4317, handoff) **and** P9 #4208 (running: the D rows; B1–B6 wait for the ten turns on jts.local) |
| 2 | P7 tests #4201 (execution after P5's moves merge) | P5 structure #4199 (moves after P6's deletions merge) | P11 web UI #4212, once P12 has handed off |
| 3 | P8 docs #4202 (stale-path pass last) | — | P3 resilience #4195 (P12, P4 and P6 have posted its rows; P4's row 16 waits on its bridge `rms` emitter; its measurements need the boxes) |

James Crane has no Fable credit left, so its lanes run with Opus as the coordinator: P6 (plan
already written and triaged; mostly Sonnet deletions), then P7 and P8. P3, which touches the
clamp paths and the daemons' restart policy and needs box measurements, moves to Space Hater after
P11, where Fable and the hardware both are. An Opus coordinator gets one extra sentence in its
kickoff: read every builder's diff before trusting its report, and one row at a time.

**Owner at the box (next session at home).** Nothing needs a deploy first; every build is already
on its box. jts.local (`162ab4088`): move the host volume slider 0 → 50 → 100 and hold (the #4209
check; the P12 session reads `event=usbsink.volume_observed raw= muted=`), then speak ten turns
(P9's gate 0.2; the session reads `event=turn.timeline`). jts3 (`60c38f5ab`): with the P12 session
watching the journal, unplug the XVF3800 (H1: the reboot-window number) and listen for the mic-loss
cue (H2), then replug. jts4: nothing; the P12 session deploys it for HW-3/HW-5. Read ADR-0244 (the
server-VAD path is deleted rather than kept as a knob; the May A/B lost 0/5, 3/5, 0/5; a re-run
restores it from git history) and object on #4208 only if you want that experiment path kept.

**State on 2026-09-07 03:15 UTC.** P1 done (handoff #4279; residuals filed as #4281, #4282, #4284,
#4304). P2 round two executing on Dip (11 rows; HW-6 dropped; row 11 — the `first-party-runtime.sh`
seam and the airplay-mode push design — approved to land last; its box rows go to #4027 for the
Space Hater sessions, none needs the owner). P4 done (handoff #4327: 12 PRs, `audio_graph` deleted, ADR-0245,
the class-A rule stated in `doctor_contract.py`; rows 15b/16 wait on P9's `wake_legs_dead` and
P3's bridge `rms` emitter). P6 running on James Crane as Opus (two PRs merged). P9: 24 PRs merged (ADR-0244), the daemon split in
progress; P4's `wake_legs_dead` ask answered (#4331). P12: every Phase 3 row
merged (#4242 reopened and merged), H3 and the P1/P2 hardware rows PASS on jts3, HW-6's premise
does not hold on ring boxes. Deployed: jts3 `60c38f5ab`, jts.local and jts4 `162ab4088`. Three
ADR-number collisions in one night (0238, and 0240 twice): a lane takes the next free number in its
merge commit, never at draft time. A filename-uniqueness pin already exists
(`tests/test_docs_linkcheck.py`, since #3897); what it cannot catch is a stale citation of a
reassigned number, which #4302 fixed by hand — no new machinery for that.

**Merge word (rule change).** The owner's triage at the plan gate is also the merge word for every
PR in that plan, sensitive tier included, once `/code-review`, `/simplify` and (where the tier
demands it) `/adversarial-review` have no open blockers. No lane waits for a per-PR word; four
lanes stalled on that on the first night.

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
