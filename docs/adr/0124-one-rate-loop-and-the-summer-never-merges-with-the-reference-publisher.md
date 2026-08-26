# ADR-0124: One rate loop, and the summer never merges with the reference publisher

- **Date:** 2026-08-26
- **Status:** Accepted (ratified on the design 2026-06-21; the build is owed —
  recorded here when HANDOFF-distributed-active.md was trimmed to its
  operational spine)

## Context

An active leader runs two CamillaDSP (ADR-0123). Its combined output must
carry music *and* the leader's own TTS/cue, and camilla#2 has a single
capture, so it cannot mix a second source: TTS has to be summed **before**
the crossover.

Count *crossings*, not stages. `snapserver → DAC` is the only hard clock
crossing — two real crystals, absorbed continuously. The leader's own TTS/cue
is a **soft input**: no independent crystal produces it at a fixed wrong
rate, it is just buffered and consumed at the DAC's pace. It is not a
crossing and needs no loop.

## Decision

**The combined stream has exactly one rate loop, and the summing stage that
owns it is a separate `jasper-outputd` instance from the DAC-owning one.**

- **Music-only (no leader TTS):** snapclient *is* the loop — it tracks the
  server clock and writes the grouping ring, which camilla#2 captures. This
  is the already-validated active-follower seam. camilla#2 adds no second
  loop: its `enable_rate_adjust` follows the sink it plays into (false on the
  active ring), and on a ring capture the request cannot be actuated at all —
  a ring PCM is an ioplug, so CamillaDSP finds no mixer element to steer.
- **With leader TTS:** a summing stage (`outputd-summer`) moves in front of
  camilla#2 and **becomes the sole loop**; camilla#2 then runs
  `enable_rate_adjust` **OFF** — a passive, DAC-clocked crossover/EQ block,
  with the ppm absorbed upstream by the one loop keeping its output buffer
  fed.

Running camilla#2's `rate_adjust` *and* the upstream matcher is two loops
referenced to the same terminal error through the shared buffer — the
documented `rate_adjust`+resampler oscillation (CamillaDSP #207). A
"near-idle trim" only widens the stable region; it does not survive the
load/thermal/scheduler swings a music+voice Pi throws. **One live loop, not
two.**

## Consequences

- **DO NOT MERGE THE TWO `jasper-outputd` INSTANCES.** The summing +
  rate-matching `outputd-summer` (upstream of the crossover) and the
  DAC-owning, AEC-reference-publishing `outputd-final` (downstream of it) are
  two instances of the same binary that must stay separate, and the reason is
  **invisible from their config** — they read as obvious duplication. The
  reason is multiroom's inv-A: the AEC reference must equal the
  *post-crossover* final electrical output, TTS-inclusive, so the box cancels
  its own band-limited voice instead of waking on or talking over it. That
  pins the reference publisher downstream of camilla#2; the summer must be
  upstream, because it feeds camilla#2. Merge them and the published
  reference becomes *pre-crossover*: AEC silently stops cancelling the
  speaker's own TTS, with no error, no config diff, and no test failing
  unless one asserts reference == post-crossover.
- **Summing, not sidechain.** JTS ducking is *commanded*
  (`PROGRAM_DUCK_ON/OFF` over the TTS socket, a ramped gain), not an
  auto-detecting sidechain compressor, so TTS and the duck both fold into the
  single matcher and the duck follows for free — point the leader's TTS
  socket at `outputd-summer` and the in-band duck command rides it.
  `outputd-final`'s TTS socket stays **unset**: its post-crossover 2-ch mixer
  is the full-range-to-tweeter hazard, closed belt-and-suspenders by
  `JASPER_OUTPUTD_ACTIVE_LANE` so the mixer fails closed on an active sink
  even if the socket were set.
- **The on-device gates split so a failure has one candidate cause:** (1)
  bring the active leader up on the *validated* seam (bake +
  camilla#2-as-follower-endpoint, `rate_adjust` ON, no summer) — proves the
  two-instance setup, CPU/thermal and music sync on a proven clock; (2) swap
  in `outputd-summer` + camilla#2 `rate_adjust` OFF, still music-only —
  isolates the **new** clock topology; (3) arm TTS and the commanded duck as
  a soft input into the now-proven summer. A failed soak in step 2 points
  unambiguously at the summer topology.
- **Pre-registered soak signatures**, fixed before the run so there is
  nothing to rationalise against afterwards. Over a ≥24 h soak, log the
  resampler ratio, the fill of every buffer at the crossing, and an
  end-to-end latency probe:
  - **One clean loop (PASS):** ratio is a stationary random walk around the
    true crystal offset (constant few-ppm, bounded noise); fills stationary;
    latency flat.
  - **Two coupled loops (the rejected `rate_adjust`-ON failure):**
    low-frequency *hunting* in the ratio and/or a *beat* between two fills;
    latency breathes.
  - **No matcher (a transparent summer):** monotone fill *ramp* → underrun;
    latency creeps ~1 ms/min.
  This is the discriminator a seconds-scale inter-client diff cannot see.
- **Open, and deliberately not encoded in config before the measurement:**
  whether `outputd-summer` is a second `jasper-outputd` instance
  (reference-publish off, heavier, outputs a loopback) or a lean pipe-writing
  summer (less RAM, frees a loopback pair via camilla#2 `File`-capture, some
  new code). Both once read as "maximum reuse of the shipped
  `content_bridge=rate_match` module"; that module has since been deleted, so
  neither is a reuse any more — each would compose the shared
  `jasper-resampler` primitives directly, as `jasper-fanin`'s lane resampler
  already does. Prefer lean-first if RAM is the binding constraint on the
  1 GB Pi; fall back to the two-instance build if a from-scratch summer's
  rate-match quality does not clear the bar.
