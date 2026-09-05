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

Hardware/audio safety is already A− and needs only R-016's belt-and-braces row (in P4's doctor work
and P3's clamp event). The tuning zone is parked: its steward stood down with wave 9 on main
(close-out on #3769; PR #4138 open, owner-gated). Every prompt keeps the zone read-only and files
the tuning-zone rows its attribute needs under an owner-gated heading in its plan, so the owner
ticks them at the plan gate instead of a lane widening into a 263k-line domain unasked.

## Sequencing

Every lane starts with a scout and a one-page plan that stops at the owner's triage; those phases
never conflict, so all eight can be kicked off at once. The hard ordering is only about code
landing on `main`:

1. **P6 deletions → P5 moves → P5 splits.** Do not move what is about to be deleted; do not split a
   file that is about to move. P5's cycle fix, layers contract and deferred-import rule need no wait.
2. **P5 moves → P7 execution.** Tests travel with their modules in P5's PRs and die with their
   subjects in P6's; P7 rewrites only what neither list names until both have merged.
3. **P5 moves → P8's last PR** (the stale-path pass). Everything else in P8 starts now.
4. **P6's peering deletion → P3's `peering/state.py` fix**, if both are open at once.
5. **The six voice PRs and their close-out → P9.** Its Waves 3 and 6 also wait on the owner's
   ten-turn timeline numbers (the brief's ledger row 0.2).

| Batch | Lanes | Why together |
|---|---|---|
| 1 (now) | P1, P2, P6, P5 (plan, cycle fix, contract) | Disjoint files; P1/P2 are small and close the two non-negotiable seams; P6 clears the ground P5 moves on |
| 2 (as P1/P2 close) | P3, P4, P8, P9, P5 (moves) | P3/P4 split `jasper/control/` by file (stated in their prompts); P9 starts once the six voice PRs and their close-out are in; P8 is prose-only |
| 3 (after P5 moves merge) | P7, P5 (splits), P8 (stale-path pass) | Tests and docs follow the tree |

## Where each lane runs

The owner has three Claude plans: two cloud plans (remote sessions, no route to the LAN) and one
local plan (the owner's machine, which reaches the Pis). Rule: a lane goes to the cloud unless it
needs the box; the local plan is the hands for everyone; spread the load so no plan runs more than
two lanes at once.

| Plan | Batch 1 (now) | Batch 2 | Batch 3 |
|---|---|---|---|
| Local (hardware) | #4209 USB host-volume regression (evidence, bisect), then the ops lane (deploy, measure, verify for every other lane); the hardware-input lane (#4027) when its close-out is in | ops lane: P9's rows 0.2 and 1.4, P3's idle measurements, P2's spare-Pi installs | ops lane |
| Cloud A | P1 secrets, P6 right-sizing | P3 resilience, P5 moves | P7 tests |
| Cloud B | P2 deploy integrity (code with stubbed ssh/rsync; every install-path PR gets one spare-Pi run via the local plan), P5 (plan, cycle fix, layers contract) | P9 voice loop, P4 observability, P8 docs | web UI lane (#4031 successor), P8's stale-path pass |

Hardware asks from a cloud lane are comments on the ops lane's issue, with the exact command and
the expected reading; the ops lane answers on the asking lane's issue.

Four sessions at a time is a comfortable ceiling: every lane rebases before each push, and more
than that makes `main` thrash. The general steward (#4085) has stood down with nothing open; its
round-2 queue is folded into P2–P8. The doctor/state steward overlaps P3/P4: stand it down (merge
what is green, close what is not, one close-out comment) before batch 1, or point it at these
issues; the Wave-6 systems rows in the review go to P4 once it has. #4031's Phase D and #4027 are
being landed the same way; their close-outs redraw the map before the fresh sessions start.
Waves 1–2 of the steward round (26 PRs, the sensitive-tier #4163 and #4187 among them) are on
`main` and **not deployed**; the owner's deploy is their hardware confirmation and gates P3's
daemon rows.

## Programs still landing (the map is redrawn once they have)

- **Voice loop** (brief `docs/VOICE-AUDIT-2026-09-05.md` on PR #4186; code PRs #4191, #4192,
  #4198, #4203, #4206 — open, rebased on `main`, none merged or hardware-verified). Decision: the
  wake→turn loop becomes its own concern lane rather than being split across P3–P8, because its
  latency ruler (`event=turn.timeline`) and wave order only make sense in one head. It will own
  `jasper/voice_daemon.py`, `jasper/voice/`, `jasper/cues/`, `jasper/tools/`, `jasper-voice.service`,
  the wake legs and the provider adapters — that is **P9 (#4208)**, and P1–P8 now name it as the
  owner of those files; the WakeLoop / `daemon_main` god-file rows moved out of P5 into it. Landing order for the six PRs:
  #4186, #4191, #4192, #4206, #4198, #4203 (the last two rebase after their pairs merge); #4186 and
  this review both edit `docs/doc-map.toml`, so whichever merges second rebases once.
- **Web UI** (#4031, session mid-PR on #4196), **hardware input** (#4027), **doctor/state**: close-outs
  pending; each becomes or feeds a lane in the redrawn map.
- **Idle-efficiency / ops review** (#4139): the daily deploy + health + idle-efficiency pass over
  jts.local, jts3 and jts4 — the only lane with hands on the boxes, since the remote sessions cannot
  reach the LAN. Proposed shape in the redrawn map: a measure-deploy-and-file lane (P10) that owns
  no source tree, deploys `main`, measures, and files rows on the owning lane's issue; it opens code
  PRs only as owner-approved one-offs. Every "measure once on hardware" row in P3, P4 and P9 is an
  ask on its issue. Deploy state at its last comment: jts3 and jts4 on `bb5173924` (12:24), jts.local
  on `6a255e2e7`; the general steward's #4163/#4187 (merged 17:09) are on no box yet.
