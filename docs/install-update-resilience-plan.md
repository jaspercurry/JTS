# Install / update flow resilience — problem brief & workstreams

> **Status: planning brief (2026-06-21).** This documents *problems* and
> *open questions* for hardening the JTS install/update flow. It is **not**
> current operational truth and deliberately prescribes no single solution.
> It will go stale as the workstreams below land — once a workstream ships,
> the code plus the operational docs it updates (`HANDOFF-aec.md`,
> `HANDOFF-resilience.md`, …) are the truth, not this file. Origin: a
> 2026-06-21 update of `jts2.local` (a 1 GB Pi 5, **748 commits behind**
> `main`) that failed mid-install and left the speaker degraded.

## Mission

Make the JTS install/update flow (`scripts/deploy-to-pi.sh` →
`deploy/install.sh`) resilient across **(a)** the full Raspberry Pi hardware
range — 512 MB Pi Zero 2 W through 1/2/4/8/16 GB Pi 5 — and **(b)** the full
range of deploy scenarios — fresh flash vs. in-service update, and small vs.
very large version jumps — and to keep the speaker correct as components are
hot-plugged/unplugged at runtime.

The brief below is the shared context. The **Workstreams** at the bottom are
ready-to-paste kickoffs for separate context windows; each resolves one slice
and follows AGENTS.md (diagnose first, prior art first, surgical, pin with
tests, PR flow).

## Grounding incident (what triggered this)

On 2026-06-21 we updated `jts2.local`, a **1 GB Pi 5** that was **748 commits
(~3 weeks) behind `main`** — an *update of a long-running, in-service speaker*,
not a fresh install. The deploy failed and left the box degraded. The problems
below are confirmed data points, not the whole space.

## Problems observed (evidence-based)

1. **Source builds OOM-kill low-RAM boxes.** `install.sh`'s vendored
   `webrtc-audio-processing` v2.1 build ran `meson compile` with no job cap →
   `nproc` (4) parallel `-O3 cc1plus` → exhausted 991 MB RAM + 495 MB swap.
   (Point-fixed in PR #899 by RAM-budgeting the job count, but treat that as
   **one instance of a class**: any heavy build/compile step is currently
   unbounded w.r.t. the box's memory — the Rust build and the source-built
   shairport-sync/nqptp are the same shape.)
2. **The OOM cascade killed unrelated production daemons.** The kernel OOM
   killer didn't only kill the compiler — it killed **nginx** (web UI down,
   `status=9/KILL`) and stopped **jasper-voice**. A *build step* during an
   *update* took down the *live speaker's* core services. There is no memory
   isolation between build work and running production daemons.
3. **Failure left a half-updated, inconsistent box.** `set -e` aborted
   `install.sh` mid-run: the Python venv was already re-installed to new code,
   but daemons weren't restarted / were down. No rollback, no resume, no
   "previous good" generation to fall back to.
4. **The build manifest lied.** `/var/lib/jasper/build.txt` was written with
   the *new* SHA *early*, before the build step that then failed — so the box
   advertised a successful update (and the next deploy's direction-guard would
   treat it as already-updated) while actually being broken. The success marker
   is not gated on actual success.
5. **The failure wasn't self-evident from the tooling.** That nginx/voice were
   OOM-killed was only discoverable by SSHing in and reading
   `systemctl`/journal; the deploy tooling didn't surface "your live daemons
   were collateral-killed."
6. **Hardware-dependent daemons crash-loop instead of parking when their
   device is absent.** Separately, `jts2` has **no microphone attached**; with
   the config pointed at the XVF3800 `Array`, `jasper-voice` failed to open it
   and systemd restarted it ~20× until "start request repeated too quickly"
   parked it (`No input device matching 'Array'`). The AEC reconciler is meant
   to keep voice from even trying when no mic is present, but there's a window
   (unit enabled → starts on boot → crashes → reconciler parks it later) where
   it thrashes rather than cleanly idling. Bounded, but not clean.
7. **A successful update can leave a core function dead and not notice it.** The
   eventual successful deploy verified only the management surface
   (nginx/control) and reported success while voice was down for lack of a mic.
   Verification scope ≠ system health. (Generalizes #4/#5.)

## Variability axes the flow must handle

- **Hardware tier (RAM is the sharp edge, not the only one).** 512 MB
  (Zero 2 W) → 16 GB. Also CPU (Zero 2 W's 4×A53 ≪ Pi 5's A76 — a "3-5 min"
  source build may be 30+ min or effectively infeasible), arch (armhf vs
  arm64), swap, and storage class. OPEN: which SKUs are *supported* vs.
  *best-effort*, and is the Zero 2 W viable for the full daemon set? (Prior art:
  the streambox / "Zero-class" install profile — see `dumb-endpoint-bringup.md`.)
- **Fresh install vs. in-service update — the crux.** A fresh box has no
  production daemons competing for RAM and nothing to disrupt; "failure" just
  means setup didn't finish. An *update* runs against **live daemons holding
  RAM**, so a heavy build has *less* memory available *and* can take down the
  running service. The update path may need to protect/quiesce/contain in ways
  the fresh path doesn't.
- **Magnitude of version skew.** This update was 748 commits behind. OPEN: does
  being far behind *independently* raise risk, or is it just "more migrations"?
  Hypotheses to test: (a) more one-shot migrations (env/unit/schema) in a single
  run = more places to fail; (b) cold/invalidated build caches — our webrtc
  cache "lacked expected provenance" → full rebuild; a current box would have
  skipped it — so far-behind updates disproportionately trigger the expensive,
  OOM-prone rebuilds; (c) crossing multiple breaking topology changes at once
  (socket ports, unit names, file ownership, group membership). Determine
  whether staleness needs explicit handling (stepwise / checkpointed updates).
- **Runtime hardware dynamism (hot-plug / hot-unplug) — "treat it like a
  computer."** The mic (XVF3800), output DAC/dongle, USB host, and satellites
  can be attached/detached *while the speaker is running*. The system must
  converge automatically in BOTH directions: on **unplug**, dependent functions
  degrade gracefully (clean park + an observable signal, never crash-loop or
  wedge); on **plug-in**, the dependent function comes up on its own ("just
  works") with no redeploy or manual restart, and reasonably promptly
  (event-driven, not only on boot/deploy/timer). This is also an *install/update*
  requirement: a deploy must succeed and leave the box correct regardless of
  which components happen to be attached at deploy time — e.g. updating a
  speaker with no mic installs fine and leaves voice cleanly parked pending a
  mic; it must not fail, crash-loop, or falsely report voice healthy.

## Relevant prior art already in the repo (build on; don't reinvent)

- **Reconcilers** are the existing "converge to hardware present" mechanism:
  `jasper-aec-reconcile` (mic/AEC), `jasper-audio-hardware-reconcile` /
  `jasper-dac-init` (DAC), `jasper-identity-reconcile`, plus the pure-data
  DAC/wake registries. Hot-plug hardening is mostly about making reconciliation
  event-triggered, bidirectional, and crash-loop-free — not a new mechanism.
- `scripts/pi-run-diagnostic.sh` — bounded `systemd-run`
  (MemoryMax/MemoryHigh/MemorySwapMax/OOMScoreAdjust) for heavy Pi-side work. A
  memory-contained-execution pattern already exists in the repo.
- `deploy/lib/install/rust-daemons.sh` — a low-memory build profile (jobs=1,
  opt-level=2, lto=false) gated at 768 MB via `rust_build_memtotal_kb`.
  Per-builder and ad hoc; there's no single hardware-tier build strategy. (The
  PR #899 webrtc fix added a second, graduated instance — `_webrtc_compile_jobs`
  in `deploy/install.sh`.)
- `deploy/lib/install/memory-resilience.sh` — `_compute_min_free_kbytes`,
  OOMScoreAdjust ladder, MGLRU tuning ("Stage 1 memory resilience").
- Install **profiles** (`full` / streambox / "Zero-class") —
  `tests/test_install_profile_tiers.py`, `docs/dumb-endpoint-bringup.md`. Some
  hardware tiering exists; it isn't yet the organizing principle for *build*
  strategy.
- The webrtc source is fetched from a **GitHub release**
  (`build-deps-v1/…tar.gz`) — we already ship build-input artifacts out-of-band;
  a channel for *prebuilt per-arch binaries* could reuse it.
- `bash deploy/install.sh --dry-run` — install-plan preview surface.
- The deploy **direction guard**, **build manifest**, and post-deploy
  **management-surface probe** in `scripts/deploy-to-pi.sh`.
- AGENTS.md already codifies the runtime principle ("JTS is a production
  speaker — design for resilience": *reasonable physical actions … removing a
  satellite … must not put the speaker in a state it can't self-recover from*).
  The mic case is a concrete test of that promise.

## Open questions (lay out options + trade-offs; don't pre-pick)

- Should **heavy builds be memory-contained** (cgroup/systemd-run) so they
  structurally cannot starve/kill live daemons? At what limits, and what happens
  when a contained build can't fit?
- Should we **build off-box / ship prebuilt per-arch artifacts** instead of
  source-building on a 1 GB (or 512 MB) Pi at all? Maintenance/signing/
  provenance cost? (`HANDOFF-supply-chain.md` owns the provenance policy.)
- Should an update **quiesce or shield production** during risky phases
  (maintenance window), provision **temporary swap**, and/or **tier the build
  strategy** by detected RAM/CPU up front?
- Should **success markers be atomic** (build.txt only on verified success), and
  should there be **rollback / A-B generations / resume-from-failure** so a
  failed update never leaves a worse-than-before box?
- Should the post-deploy **verification cover real system health** (the voice
  daemon, AEC bridge, renderers) rather than only the management surface?
- Should hardware-dependent daemons be **gated on their device being present**
  (reconciler-owned start / presence condition) so they never start-and-crash-loop
  when it's absent — vs. start-and-retry?
- Should reconciliation be **triggered by hotplug (udev) events**, not just
  boot/deploy/timer, so plug-in converges within seconds? Latency target?
- How should a function that's **intentionally idle for missing hardware** be
  reported (doctor ✗ vs. ! vs. "expected: no mic")? Distinguish "broken" from
  "correctly idle because the component isn't installed."
- How do we **test the matrix** without owning every SKU (memory-cgroup-
  constrained CI, QEMU, a canary-Pi lab)? What guard tests pin the invariants?

## Definition of done (the invariants the hardening should establish)

- An update to a **healthy in-service speaker never leaves it worse than
  before** — it fully succeeds, or cleanly aborts/rolls back with the old
  version still serving.
- Install **and** update succeed, or **degrade gracefully and loudly**, across
  512 MB → 16 GB.
- A failed update is **observable** (tooling reports what happened, including
  collateral) and **recoverable**, and **never reports false success**.
- **No build step can starve or kill a live production daemon.**
- The **build manifest reflects reality.**
- Components can be **attached or detached at runtime** and the speaker
  converges to a correct state on its own — no redeploy, no manual restart,
  **no crash-loop** — in both directions.
- A function idle only because its hardware is absent is reported as
  **expected/idle, not failed**, and an update never reports it healthy when it
  isn't.

---

## Workstreams (ready-to-paste fresh-session prompts)

Each block is a self-contained kickoff for its own context window. They share
the brief above and say to read this file first. Pursue them independently; #B
and #C can land before #A, and #D informs all of them.

### Workstream A — Memory-safe, production-isolated builds across tiers

```text
Read docs/install-update-resilience-plan.md for full context (problems #1, #2;
the hardware-tier and fresh-vs-update axes; the prior art list).

Mission: make every heavy build step in deploy/install.sh safe on a 1 GB Pi 5
(and ideally a 512 MB Zero 2 W) WITHOUT killing live production daemons during
an in-service update. The WebRTC AEC3 build was point-fixed (_webrtc_compile_jobs,
RAM-budgeted -j); generalize the lesson. In scope: the Rust daemon builds, the
source-built shairport-sync/nqptp, and any other compile/build the installer
runs. Out of scope: re-architecting AEC (see AGENTS.md "Architecture is fixed").

Investigate and propose (with trade-offs) before implementing:
- Should heavy builds run memory-contained (systemd-run with MemoryMax/Swap,
  like scripts/pi-run-diagnostic.sh) so an OOM kills only the build, never nginx
  or jasper-voice? What happens when a contained build won't fit?
- Should we ship prebuilt per-arch artifacts (the webrtc tarball already comes
  from a GitHub release; HANDOFF-supply-chain.md owns provenance) instead of
  source-building on tiny Pis at all?
- A unified, RAM/CPU-aware build-parallelism + opt-level policy rather than the
  current per-builder ad hoc (rust-daemons.sh's 768 MB on/off vs the webrtc
  graduated -j). One helper, one place, tested across SKUs.

Diagnose with evidence; follow AGENTS.md (prior art first, surgical, pin claims
with tests — mirror tests/test_install_helpers.py). Deliver a design note +
a scoped PR. Validate on a memory-constrained environment and say what needs
real-hardware confirmation.
```

### Workstream B — Atomic, verifiable, recoverable updates

> **Status: landed (2026-06-21).** The build manifest is now the
> verified-install marker (written last, gated by `set -e`); deploy
> verification surfaces OOM collateral + post-restart voice/AEC/renderer
> health and gates on the manifest advancing. Full A-B generations were
> analysed and deferred. Operational truth + the rollback decision:
> [HANDOFF-install-update-transaction.md](HANDOFF-install-update-transaction.md).
> The prompt below is preserved as the originating brief.

```text
Read docs/install-update-resilience-plan.md for full context (problems #3, #4,
#5, #7; the deploy direction-guard / manifest / verification prior art).

Mission: make a JTS update a transaction — it either fully succeeds or leaves
the speaker no worse than before, and it NEVER reports false success.

Investigate and propose (with trade-offs) before implementing:
- Gate the build manifest (/var/lib/jasper/build.txt) on VERIFIED success, so a
  mid-install abort can't leave the box advertising a SHA it isn't cleanly
  running (this misled the deploy direction-guard on jts2, 2026-06-21).
- Broaden the post-deploy verification in scripts/deploy-to-pi.sh beyond the
  nginx/control management-surface probe to cover real system health (voice
  daemon, AEC bridge, renderers) — distinguishing "down because broken" from
  "intentionally idle because the hardware is absent" (see Workstream C).
- Rollback / resume-from-failure / A-B generations so a failed update reverts to
  the last-good state rather than persisting a half-updated box. What's the
  cheapest version that meets the "never worse than before" bar on a 1 GB Pi?
- Surface collateral damage: when a build/update OOM-kills an unrelated daemon,
  the tooling should say so, not exit silently.

Diagnose with evidence; follow AGENTS.md; pin behavior with tests
(tests/test_install_helpers.py, tests/test_deploy_wiring_guards.py,
scripts/_lib.sh guard tests are the shape). Deliver a design note + scoped PR(s);
flag what needs real-hardware confirmation.
```

### Workstream C — Runtime hardware hot-plug / unplug resilience

```text
Read docs/install-update-resilience-plan.md for full context (problem #6; the
"runtime hardware dynamism" axis; the reconciler prior art).

Mission: treat the speaker like a computer — components (mic/XVF3800, output
DAC/dongle, USB host, satellites) can be attached or detached WHILE RUNNING, and
the speaker must converge to a correct state on its own, in BOTH directions,
with no redeploy, no manual restart, and NO crash-loop.

Concrete failing case to fix and pin: with no mic attached, jasper-voice
crash-looped ~20× ("No input device matching 'Array'") before systemd parked it,
instead of cleanly idling until a mic appears. On plug-in it should come up
automatically and promptly.

Investigate and propose (with trade-offs) before implementing:
- Should hardware-dependent daemons be gated on device presence (reconciler-owned
  start / a presence condition) so they never start-and-crash-loop when the
  device is absent — vs. start-and-retry-with-backoff?
- Should reconciliation be triggered by hotplug (udev) events, not just
  boot/deploy/timer, so plug-in converges within seconds? Latency/robustness?
- How to report a function that's correctly idle for missing hardware (doctor
  state, /state, dashboard, cue) as "expected: no mic" vs. "broken"?
- Both directions must hold for the DAC/output and satellites too, not just the
  mic. AGENTS.md "design for resilience" already codifies the principle; this
  makes it a verified property.

Build on the existing reconcilers (jasper-aec-reconcile,
jasper-audio-hardware-reconcile, jasper-identity-reconcile); do not invent a new
mechanism. Diagnose with evidence; follow AGENTS.md; pin with tests. Deliver a
design note + scoped PR; flag what needs a real plug/unplug hardware pass.
```

### Workstream D — Hardware-tier awareness, cross-tier testing, and the stale-update question

> **Design note + recommendation delivered:**
> [`install-hardware-tier-and-staleness.md`](install-hardware-tier-and-staleness.md).
> Bottom line: tier ≠ profile (add detected RAM/CPU/arch up front,
> orthogonal to full/streambox); migrations are convergent so far-behind
> is *not* a migration-pile-up risk — it amplifies risk via cold build
> caches, so stepwise updates are **rejected** in favour of safe builds
> (A) + atomic updates (B); plus a cross-SKU test strategy and a scoped
> tier-detection + arch-guard change in `deploy/install.sh`.

```text
Read docs/install-update-resilience-plan.md for full context (the hardware-tier
and version-skew axes; the install-profile prior art).

Mission: make the installer explicitly hardware-tier-aware and testable across
the SKU range without owning every board, and answer whether being far behind
changes the update's risk profile.

Investigate and propose (with trade-offs) before implementing:
- Detect the hardware tier (RAM/CPU/arch) up front and choose strategy from it
  (build parallelism, swap provisioning, source-vs-prebuilt, which optional
  components to build at all — e.g. is the software WebRTC AEC3 build even
  relevant on a chip-AEC speaker or a Zero 2 W?). Relate to the existing
  full/streambox/Zero-class profiles (tests/test_install_profile_tiers.py,
  docs/dumb-endpoint-bringup.md) rather than adding a parallel concept.
- A test strategy for the matrix without all the hardware: memory-cgroup-
  constrained CI, QEMU, and/or a canary-Pi lab. What guard tests pin the
  cross-tier invariants?
- The stale-update question: does 748-commits-behind independently raise risk,
  or is it just "more migrations"? Investigate (a) one-shot migration pile-up,
  (b) cold/invalidated build caches forcing expensive rebuilds, (c) crossing
  multiple breaking topology changes at once. Decide whether stepwise/
  checkpointed updates are warranted or unnecessary complexity.

Diagnose with evidence; follow AGENTS.md; pin with tests. Deliver a design note +
recommendation (and scoped PR if a clear, low-risk improvement falls out); flag
what needs real-hardware confirmation across SKUs.
```
