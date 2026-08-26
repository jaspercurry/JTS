# ADR-0163: Installer builds run the inverse of the audio-daemon memory policy

- **Date:** 2026-08-26
- **Status:** Accepted (recorded when HANDOFF-build-sandbox.md was trimmed to
  its operational spine; the mechanism itself shipped as Workstream A of
  `docs/install-update-resilience-plan.md`)

## Context

An in-service update compiles heavy C/C++/Rust on the same 1 GB Pi that is
currently serving audio. On jts2 (2026-06-21) `meson compile` fanned out to
`nproc` `-O3 cc1plus` jobs, exhausted RAM plus swap, and the kernel OOM-killer
took out **nginx** and **jasper-voice** — not the compiler. The speaker went
deaf and unreachable while its own installer ran.

Bounding `-j` alone does not fix this. A single webrtc translation unit
(`audio_processing_impl.cc`) peaks over 1 GB in `cc1plus` at `-O3`; on a 1 GB
box that cannot fit in RAM at any `-j`. Bounded parallelism lowers the
*probability* of pressure; it cannot bound the *victim*.

The runtime protections JTS already ships do not cover the build window:

- The `OOMScoreAdjust` ladder in the unit files and the live write in
  `migrate_memory_resilience` both run at the **end** of `main()`, after every
  build. During the build the running daemons sit at whatever their
  currently-installed (old) units set — on a far-behind box, adj 0.
- nginx is package-owned, so installs predating the JTS recovery drop-in had no
  adjustment and no restart policy. That is why it died first.
- `jts-audio.slice`'s `MemorySwapMax=0` and the per-unit `MemoryMax`/
  `MemoryHigh` directives are silent no-ops until the memory cgroup controller
  is enabled, which needs a `cmdline.txt` edit **and a reboot**.

## Decision

**Every heavy installer build runs inside a transient `systemd-run --scope`
whose memory policy is the deliberate inverse of the audio daemons' policy**,
on top of RAM-aware `-j` bounding. One policy, one file
(`deploy/lib/install/build-sandbox.sh`), applied at every heavy call site.

| Property | Audio daemons | Installer builds |
|---|---|---|
| `OOMScoreAdjust` | strongly negative (never kill) | strongly positive (`900`, kill me first) |
| swap | `MemorySwapMax=0` (latency) | allowed (slow is fine; completion matters) |
| CPU / IO weight | high / default | low (`20`) so the build yields to playback |
| `MemoryHigh` | throttle to protect latency | soft ~85 % MemTotal, leaving PID1/sshd headroom |
| `MemoryMax` | bound the daemon | **off by default**; opt-in via env |

`OOMScoreAdjust=` on the build's own scope is a per-process
`/proc/PID/oom_score_adj` write, so it works **without** the memory cgroup
controller — the containment holds on a never-rebooted, far-behind box, which is
exactly the box that needs it.

Containment is best-effort by construction: in `auto` mode it wraps the build
only when root **and** `systemd-run` on PATH **and** `/run/systemd/system`
exists. Otherwise (CI, a macOS dev box, a container, `--dry-run` sourcing) it
execs the command directly and unchanged. There is no post-failure retry — a
contained command's exit status propagates verbatim.

## Consequences

- **A build that will not fit dies alone.** It throttles, leans on swap, and
  finishes slowly; only a whole-box OOM kills it, and then the install aborts
  under `set -e` with the old version still serving.
- **A hard `MemoryMax` stays opt-in.** A cap low enough to help would kill a
  legitimate single-TU compile and regress installs that used to squeak by on
  swap. Failing observably is offered
  (`JASPER_BUILD_SANDBOX_MEMORY_MAX=`), never imposed.
- **No wall-clock cap by default either.** A slow Zero 2 W build must not be
  killed mid-compile; `JASPER_BUILD_SANDBOX_RUNTIME_MAX` is opt-in for the same
  reason.
- **The two levers are complementary, not alternatives.** Bounded `-j` changes
  how likely pressure is; containment changes who dies under it. Removing
  either re-opens a distinct failure mode seen on real hardware.
- **Containment does nothing for build *duration*.** On the Zero 2 W tier the
  binding constraint is CPU time, not RAM, which is why prebuilt first-party
  artifacts remain the right follow-up rather than a competing fix
  (`docs/HANDOFF-first-party-arm64-artifacts.md`).
- **Every containment decision is greppable.** `event=build_sandbox.*` lines go
  to the deploy transcript and to persistent journald, so the decision survives
  the watchdog reboot a real build-OOM can trigger.

## References

- `docs/HANDOFF-build-sandbox.md` — the operational spine: knobs, build
  inventory, low-memory park/unpark, event vocabulary.
- `docs/HANDOFF-resilience.md` — the runtime memory-resilience stages this
  policy complements but does not depend on.
