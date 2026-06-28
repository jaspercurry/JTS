# Research: how PipeWire achieves low-latency + resilience (and what JTS borrows)

> **Status: research.** A study of PipeWire's *actual* implementation (master
> branch source + design docs), done 2026-06-27 to extract the real techniques
> behind its low latency and clock resilience. **JTS does not use PipeWire and
> will not** — WirePlumber's multi-GB RAM runaways OOM the 1 GB Pi, and the
> "swap the engine, not the topology" rule stands (see AGENTS.md). This doc is
> *prior-art research*: PipeWire is the reference implementation of low-latency
> Linux audio, so we mine its algorithms, not its architecture. The borrows are
> surgical and algorithmic; the graph runtime is explicitly out of scope. The
> adoption plan at the end is the actionable output.

## TL;DR

The single highest-leverage borrow is **`spa_dll`** — PipeWire's ~20-line
self-tuning second-order delay-locked loop for clock reconciliation. It fixes,
by construction, the `rate_adjust + AsyncSinc` oscillation JTS has already been
bitten by, and it gives every clock-domain boundary a measurable ppm readout.
Everything else is either *already done more simply* by our snd-aloop +
CamillaDSP topology, or is the central-daemon architecture we deliberately
rejected.

---

## Part 1 — The real techniques (with source anchors)

### 1. Driver/follower graph + double-buffered quantum
Per connected component PipeWire elects one **driver** (`priority.driver`);
everyone else is a follower. Scheduling is not a precomputed order — it falls
out of atomic counters in a memfd-shared `struct pw_node_activation`: `required`
(static upstream count), `pending` (per-cycle countdown). A node runs when
`pending` hits 0 and decrements its downstream peers on finish
(`impl-node.c:trigger_targets`); the wavefront propagates source→sink by
counters reaching zero. The quantum is a first-class field
(`spa_io_clock.duration`) and is **double-buffered**: a change writes
`target_duration`/`target_rate` + bumps a seqlock `target_seq`, and the driver
copies target→current only at the **next cycle boundary** — the live cycle is
never mutated.

### 2. The DLL clock-matching loop + adaptive resampler
A ~20-line second-order delay-locked loop (`spa/include/spa/utils/dll.h`) turns
"buffer-fill error" into a rate ratio near 1.0. `spa_dll_update(err)` runs three
cascaded integrators; the **`z3` integrator gives zero steady-state frequency
*and* phase error** — a constant ppm offset settles to a steady ratio with no
residual drift (a naïve first-order "nudge rate by buffer error" loop cannot do
this; it leaves a standing offset and oscillates). The error comes from
`alsa-pcm.c:update_time()`: `err = delay - target`, where
`target = threshold + headroom` (~one quantum + margin) and `delay` is refined
below whole-frame resolution via `snd_pcm_htimestamp()`. The output `corr` both
sets the next timer wakeup *and* drives `SPA_IO_RateMatch.rate` into a polyphase
windowed-sinc resampler (`resample-native*.c`). The loop **self-tunes its
bandwidth** every 3–5 s: `bw = (|err_avg| + sqrt(err_var))/1000`, clamped
`[0.016, 0.128]` — wide to acquire fast, narrow once locked to reject jitter. A
device that *is* the graph clock never resamples (`matching=false`).

### 3. Timer-based ALSA scheduling (headroom, not period size)
PipeWire ignores ALSA's period IRQ: it opens `SND_PCM_NONBLOCK`,
`snd_pcm_hw_params_set_period_wakeup(..., 0)`, and arms its own `timerfd` with
`TIMER_ABSTIME` against `CLOCK_MONOTONIC`. Absolute deadlines prevent error
accumulation. The graph quantum is decoupled from the device period; the ALSA
buffer becomes pure **headroom** (`api.alsa.headroom`) that absorbs scheduling
jitter. Latency is a tunable margin *over the hardware pointer*, not a
consequence of period size.

### 4. Xrun recovery that quantifies the gap and resets the loop
On `-EPIPE`, `alsa_recover()` computes lost frames from the trigger timestamp
and **adds them to `clock->xrun`** so downstream A/V-sync consumers learn
exactly how much time was lost. Recovery is a group operation over the driver +
all `snd_pcm_link`ed followers (`do_drop`→`do_prepare`→`do_start`), and
`do_prepare` calls **`spa_dll_init()`** so the rate loop re-locks from scratch
instead of carrying stale integrator state.

### 5. Lock-free RT data loop
The data thread is a single `epoll` loop with no per-cycle heap alloc and no
lock. The graph hand-off is: a finished node `SPA_ATOMIC_DEC(peer->pending)`;
the peer that hits 0 CAS-flips state and writes 8 bytes to its `eventfd`. The
only syscall in the dependency hand-off is the eventfd write. Non-RT→RT control
uses a per-thread SPSC ringbuffer + an eventfd kick — never a lock. A missed
wakeup is *counted as an xrun*, not swallowed.

### 6. RT priority + zero-copy
RT: the data thread is `SCHED_FIFO` (direct or via RTKit), and RTKit refuses RT
unless the thread carries a bounded `RLIMIT_RTTIME` — so a spinning RT thread
gets `SIGXCPU`'d instead of wedging the box. Zero-copy: a SPA buffer owns
*descriptors* (an `fd` = memfd/dmabuf, mapped once) + a per-cycle
`spa_chunk {offset,size,stride}`; fds cross the socket via `SCM_RIGHTS` with
memfd sealing. Every cycle moves zero bytes between processes.

---

## Part 2 — JTS verdict per technique

| Technique | Verdict for JTS |
|---|---|
| **1. Driver/follower + double-buffered quantum** | **Have the spirit** (a single static hardware master + reconciler-owned topology — a justified simplification, no re-election needed). **Borrow** the `target_*`/`target_seq` *stage→barrier→swap* discipline to change a buffer/rate without a daemon restart or glitch (the lean lane-switch + active-speaker want exactly this). |
| **2. DLL + adaptive resampler** | **Have it coarsely** via CamillaDSP `enable_rate_adjust` (its AsyncSinc *is* the polyphase resampler). **Borrow** `dll.h` verbatim wherever CamillaDSP is *not* mediating — the highest-leverage item (Part 3). |
| **3. Timer/headroom ALSA model** | **Can't adopt** (presumes one daemon owns every device — the fanout we rejected). Borrow only the *framing*: latency is a margin over the hardware pointer; fix underruns with headroom, not smaller periods. |
| **4. Xrun recovery** | **Partially have it.** Two cheap steals: (a) *quantify* lost frames on xrun and surface to clock consumers (matters for bonded drift); (b) **re-init the rate loop on recover** so it doesn't carry stale state. |
| **5. Lock-free RT loop** | **Borrow** the SPSC-ring + eventfd cross-thread-control pattern anywhere `outputd`/`fanin` takes a mutex a hot thread contends on. **Do not** rebuild the activation-graph engine — the kernel's PCM blocking already plays that role; that is the rejected re-architecture. |
| **6a. SCHED_FIFO + RLIMIT_RTTIME** | "No CPU caps" decision stands. **But** this confirms: *if* CamillaDSP is ever given RT, pair it with `RLIMIT_RTTIME` (the SIGXCPU safety valve). |
| **6b. Zero-copy buffers** | **Already do it, simpler** — snd-aloop's kernel ring *is* the shared buffer (no fd-passing protocol). One narrow borrow: a sealed `memfd` + eventfd beats a byte-pipe for a future Rust↔Rust non-PCM hop. |

---

## Part 3 — The biggest borrow: lift `spa_dll`

Two free-running clocks that must stay sample-aligned is a delay-locked-loop
problem by definition, and JTS has it in several places it currently
hand-waves: a future **wireless subwoofer** receiver, **multiroom followers**,
and the **chip-AEC SRO** recovery (`content_bridge.rs`'s `RateController`
already *wants* to be a DLL). Where JTS does run its own loop (CamillaDSP
`rate_adjust`) it has already hit the first-order-oscillation failure mode the
DLL's `z3` integrator + adaptive bandwidth are built to avoid.

The **software-AEC3 reference vs mic** is deliberately *not* on that list:
WebRTC AEC3 already self-compensates render-vs-capture drift via its delay
estimator, and `jasper-outputd` (the reference sender) cannot see the mic clock
anyway. The DAC-clock observer in increment 2 measures the DAC playout crystal
vs *nominal* — observability for the real sites above, not a software-AEC
control loop.

The cost is near zero: `dll.h` is ~20 lines of self-contained math, no PipeWire
deps. The hard-won engineering is three constants and two clamps, copyable
verbatim: the coefficients (`w0 = 1-exp(-20w)`, `w1 = 1.5w/period`, `w2 = w/1.5`),
the variance-driven bandwidth (`bw = (|err_avg| + sqrt(err_var))/1000`, clamp
`[0.016, 0.128]`), and the `max_resync`→hard-jump rule. And it comes with
built-in observability that matches COAH doctrine: PipeWire publishes the
correction as `clock.rate_diff` — JTS should surface the per-domain ppm + error
mean/variance on `/state`, turning "is it drifting?" from an invisible analog
problem into a measured, bounded signal.

Companion borrow (same code area): PipeWire **distrusts `snd_pcm_htimestamp`** —
refine delay with it, but sanity-check each timestamp against `CLOCK_MONOTONIC`
and disable after N lies. This is the generalized form of JTS's existing
`resync_threshold_in_seconds=0.2` Apple-dongle workaround
(the dongle is a known delay liar).

---

## Part 4 — Adoption plan (surgical, algorithmic, principle-aligned)

North star: **one** shared clock primitive (single source of truth), pure and
modular, observable everywhere, composed at each site without re-architecting
the topology. Each increment is independently shippable, hardware-free-testable,
and any audio-touching one is default-safe + soak-gated. Sequenced
low-risk-high-leverage first.

| # | Increment | Principle it serves | Risk |
|---|---|---|---|
| **1** | **`jasper-clock` shared crate**: port `spa_dll` to Rust — a pure `Dll` (`update(err) → ratio`, adaptive bandwidth, resync jump) exposing its ppm + error stats + lock state. No I/O, no audio. Unit-test the convergence/no-oscillation properties. | SSOT · simple/elegant (lift, don't derive) · modular (pure) · performant (no alloc) | none (no consumer yet) |
| **2 ✅** | **First consumer, observe-only — DAC-clock observer**: a `Dll` in `jasper-outputd` measuring the DAC playout crystal vs *nominal* wall-clock (the one clock outputd can see), surfacing `dac_clock_ppm` (+var, lock, neutral acquiring/steady/drifting verdict) on `/state` + doctor. This is **not** the `:9891`-ref-vs-mic drift — AEC3 self-compensates that and outputd can't see the mic — so there is **no software-AEC resampling follow-up**. Pure clock-domain observability for the real DLL sites. *(= foundation-review G2.)* **Landed** (#1067/#1069); measured ~−115 ppm on the jts3 HiFiBerry. | observable (the headline) · measure-before-fix | low (observe-only) |
| **3** | **Converge the ad-hoc loops** in `aec_clock.rs` (`SroEstimator`) + `content_bridge.rs` (`RateController`) onto the shared `Dll`. Site-specific I/O (error source, resampler) stays local; the loop math is the one shared primitive. | DRY (delete duplicate loop math) · SSOT · separation of concerns | med (chip-AEC SRO path) — behind existing default-off/self-verify gates + soak |
| **4** | **`rate_diff` everywhere**: every `Dll` instance publishes ppm + error stats + resync counters on `/state`/doctor (mirrors `clock.rate_diff`). | observable · shared (one telemetry shape) | low |
| **5** | **htimestamp-distrust helper** in `jasper-clock`: refine via `snd_pcm_htimestamp`, sanity-check vs `CLOCK_MONOTONIC`, disable after N lies — generalizing the dongle workaround into one shared place. | DRY · SSOT · resilience | low |
| **6** | **stage→barrier→swap apply path**: borrow `target_*`/`target_seq` so a buffer/rate/config change applies at a cycle barrier instead of a daemon restart. Enables the **4b-iv lean lane-switch** (buffered↔lean, glitch-free) + active-speaker buffer changes. | elegant · separation of concerns (apply-path owns the swap) | larger design — pair with the 4b-iv live wiring |

**Status (2026-06-27):** increments 1–5 have landed (#1067 the `jasper-clock`
crate + htimestamp helper; #1069 the DAC-clock observer, the ad-hoc-loop
converge onto the shared `Dll`, and the `rate_diff` telemetry). Increment 2
shipped as a **DAC-clock observer**, re-framed from the original "software-AEC"
idea (see Part 3) — the software-AEC resampling follow-up was dropped because
AEC3 self-compensates. Increment 6 (stage→barrier→swap) is the 4b-iv live
wiring, in progress.

**Shared resampler (2026-06-28):** the windowed-sinc interpolator that pairs
with the `spa_dll` loop (PipeWire's `resample-native*.c` analogue, item 2
above) is now its own shared crate, **`jasper-resampler`** — a pure crate
(no I/O / no ALSA, same doctrine as `jasper-clock`, which it depends on for
the `RateController`). `jasper-outputd`'s `content_bridge` *consumes* it
(killing the duplicated sinc/ring/controller it used to embed), and the
Python/usbsink drift rate-match (`docs/HANDOFF-usbsink.md` §3.4) consumes a
**C++ mirror** of the same algorithm (`jasper_resampler/`, a pybind11 binding
like `jasper_aec3`, because the repo has no PyO3/maturin to bind the Rust
crate from Python). The two implementations are pinned bit-for-bit by
`tests/test_resampler_contract.py` against a committed golden vector. So
`spa_dll` is the one shared *loop* and `jasper-resampler` is the one shared
*resampler*; usbsink is the first capture-follower consumer of both.

**Convergence with the audio-foundation review** (`docs/HANDOFF-audio-latency-foundation.md`):
increments 2/4 *are* that review's **G2**; PipeWire's `RLIMIT_RTTIME` point is its
**G4**. The two independent investigations point at the same small set — strong
signal those are the real foundation wins.

**Deliberately NOT borrowed** (separation of concerns / no re-architecture): the
graph runtime — driver election, activation records, the eventfd cycle engine,
RT/SCHED_FIFO fanout, memfd/`SCM_RIGHTS`. snd-aloop + CamillaDSP + custom Rust is
the chosen topology; the kernel's PCM blocking is the scheduler; snd-aloop is the
zero-copy. The borrow is the clock-tracking **algorithm**, not the architecture.

---

## Source anchors (PipeWire `master`)

`spa/include/spa/utils/dll.h` (`spa_dll_set_bw`/`spa_dll_update`/`spa_dll_init`);
`spa/plugins/alsa/alsa-pcm.c` (`update_time`, `get_status`, htimestamp `get_avail`,
`setup_matching`, `alsa_recover`, `set_timeout`);
`spa/include/spa/node/io.h` (`spa_io_clock`/`spa_io_position`, `target_*`/`target_seq`);
`src/pipewire/impl-node.c` (`process_node`, `trigger_targets`);
`src/pipewire/private.h` (`trigger_target_v1`, `pw_node_activation_state`);
`src/pipewire/data-loop.c`; `src/modules/module-rt.c` (RTKit + `RLIMIT_RTTIME`);
`spa/include/spa/buffer/buffer.h` + `src/pipewire/mem.c` (zero-copy / `SCM_RIGHTS`);
`docs.pipewire.org/page_scheduling.html`.

Last verified: 2026-06-28 (added the shared-resampler note — jasper-resampler
crate + its C++ usbsink mirror; PipeWire master; techniques traced to the cited
source and design docs by a 4-area code-reading pass + synthesis).
