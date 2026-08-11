# Handoff: unified audio program — one graph, one quantization

> **Status: active campaign plan (unified 2026-08-10).** Canonical
> architecture + sequencing for the unified audio program, whose two
> halves land in one order: **consolidate** the playback graph onto one
> transport primitive (SHM slot rings + the `jts_ring` ioplug) and one
> clock discipline, and **preserve** each source's meaningful native
> precision until a single deliberate width reduction at the DAC edge.
> It replaces the standalone 2026-07-03 consolidation plan that used to
> live at this path and owns repository facts, U0–U4 sequencing, gates,
> and progress for both halves. GitHub master
> [#2285](https://github.com/jaspercurry/JTS/issues/2285) owns navigation
> and discussion; where its body and this file disagree, **this file
> wins** (its own banner says so — see
> [Where this corrects #2285](#where-this-corrects-2285)).
>
> **Part of this file is historical.** The live plan runs from here to
> "Cross-program coordination", followed by
> [Appendix A](#appendix-a--renderer-ring-ingress-design-u3--p6) (forward
> design for U3). Everything under
> [Appendix B — campaign archaeology](#appendix-b--campaign-archaeology)
> is a frozen record of how the graph reached this state, not current
> truth.
>
> Companions — link, never restate: [audio-paths.md](audio-paths.md) (the
> shipped lane map; rewritten at campaign end),
> [HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md) (ring
> evidence, USB DIRECT, host clock, the certified route and its gates),
> [HANDOFF-speaker-output-reference.md](HANDOFF-speaker-output-reference.md)
> (the output contract), [HANDOFF-aec.md](HANDOFF-aec.md) (mic/reference
> contracts, commissioning identity),
> [HANDOFF-audio-latency-foundation.md](HANDOFF-audio-latency-foundation.md)
> (clock-domain archaeology).

## The invariant, and the end state it implies

One program spine, as wide as the widest thing on it, with **exactly one
information-losing width reduction — at the DAC edge, in outputd, driven
by `DacProfile` data**. Every foreign clock is reconciled exactly once at
its ingress; every duplicate transport and rate matcher is deleted. Width
and rate are separate axes: 48 kHz stays pinned (AEC alignment identity
depends on it), and purpose-built narrow taps stay narrow by contract.

That resolves to: **one transport** — SHM slot rings
(`rust/jasper-ring` + `c/jts-ring-ioplug`) carrying renderer → fan-in,
fan-in → CamillaDSP (Ring A), and CamillaDSP → outputd (Ring B);
**one clock** — outputd's blocking DAC write paces the graph, with the
USB gadget reconciled by the host-clock servo (`rust/jasper-host-clock`)
+ `LaneResampler`, each network renderer by its per-lane `LaneResampler`
at fan-in ring-read, TTS clockless over its socket (already the end
state), and bonded followers by snapclient sample-stuffing (stays);
**one quantizer** — outputd's finalizer; and **snd-aloop GONE**, taking
`rate_match`, adaptive-buffer shrink, the legacy cushion recipes, the
fan-in aloop mirror lane, and the dsnoop taps with it, leaving no orphan
env keys and an honest `.env.example`.

---

## Current position (verified 2026-08-10 at `9cc41b987`)

### The egress is DONE

The wide-output-path program completed 2026-08-08 (nine merges, every PR
gated 0/0, 3-lens panels on the audio path). Its sealed record is
`captures/PLAN-wide-output-path-2026-08-07.md`; its predecessor is
`captures/PLAN-final-edge-format-2026-08-05.md`. What shipped:

- outputd's program spine is `i32` (`pub type ProgramSample = i32` in
  `rust/jasper-outputd/src/types.rs`); gain/ramp math is f64.
- The loopback content hop declares `S32_LE`
  (`camilla_config_contract.DEFAULT_PLAYBACK_FORMAT`); pipe sinks are
  permanently decoupled at `DEFAULT_PIPE_SINK_FORMAT = "S16_LE"` (the
  snapserver `48000:16:2` contract).
- One edge conversion per DAC, declared as registry data on
  `DacProfile.final_edge_format` (`jasper/audio_hardware/dac.py`), with
  `jasper-audio-hardware-reconcile` the sole runtime writer of
  `JASPER_OUTPUTD_DAC_FORMAT`.

| Profile | Declared final edge |
|---|---|
| Apple USB-C dongle | `S24_3LE` |
| HiFiBerry DAC8x | `S32_LE` |
| InnoMaker HiFi AMP Pro | `S32_LE` |
| HiFiBerry DAC8x Studio | `S16_LE` (class default; wider edge unproved on real Studio hardware) |
| Dual Apple composite | `S16_LE` (class default; packed-24 child writer is #2257) |

- **Zero dither in JTS-owned quantizers.** The S16 edge uses
  round-to-nearest saturating. The only dither in the tree is
  librespot's own renderer-side `--dither tpdf`, which is a renderer-local
  policy the source-width arc audits, not an outputd policy.

### The source half is still narrow

Every music lane enters at 48 kHz `S16_LE`, so the `S32` container
CamillaDSP captures does not describe surviving precision.

| Boundary | Position at 2026-08-10 |
|---|---|
| AirPlay | shairport-sync configured 44.1 kHz `S32`; its private aloop lane is a `plug:` wrapper pinned 48 kHz `S16_LE` |
| Spotify Connect | librespot requests `S24_3` with its own TPDF dither; the lane is pinned 48 kHz `S16_LE` |
| Bluetooth | bluealsa-aplay's negotiated PCM is adapted by the `plug:` lane to 48 kHz `S16_LE` |
| USB Audio Input | fan-in DIRECT opens `hw:UAC2Gadget` at `S32`. On a narrow wire — the shipped default, so every box — `s32_high_word_to_s16` (bare arithmetic `>>16`, no rounding, no dither) still discards the low word before resample and mix. A **dormant** wide route exists since U2 PR-1 ([#2223](https://github.com/jaspercurry/JTS/issues/2223)): when the box's wire resolves `S32_LE`, `push_capture_chunk` hands the resampler the gadget's `i32` untouched and it reaches the summed write intact. Arming it is the per-box flip, not this row |
| Provider TTS | 24 kHz mono S16 in; resampled through float, cast back to S16 before IPC; fan-in applies assistant gain at i16 (`apply_gain_i16`) |
| Generated earcons | rendered in float, then baked to 24 kHz mono S16 at daemon startup (`_to_pcm16` in `jasper/voice/earcons.py`) |
| fan-in core | accumulates into an i64 scratch at the scale the box's wire names (`ProgramWidth`). Narrow — the shipped default — is the **S16 numeric scale** exactly as before, with `saturate_to_i16` at the summed write. Wide is the i32 spine scale, promoting each `i16` lane at its own sum entry (`rust/jasper-fanin/src/mixer.rs`) |
| fan-in → CamillaDSP | `jasper_capture` is a 48 kHz stereo `S16_LE` dsnoop; CamillaDSP's capture side already requests `S32` (`DEFAULT_CAPTURE_FORMAT`), so ALSA widens an already-narrow signal and restores nothing |

Three consequences: a later `S32` container is not proof of a wide path;
narrowing before attenuation lifts the effective quantization floor
relative to quiet tails; and an S16 source gains nothing from promotion
but stops losing the precision that resample and gain create.

### The ring is S16 on the wire today, and that is a consumer problem

The **layout** is no longer the constraint. Since R1
([#2297](https://github.com/jaspercurry/JTS/pull/2297), merged),
`Geometry::validate_self` (`rust/jasper-ring/src/layout.rs`) accepts
`SAMPLE_FORMAT_S16LE` **or** `SAMPLE_FORMAT_S32LE`, at 2..=`MAX_RING_CHANNELS`
(8) channels — `S32LE` is no longer "Reserved". Read the R-ladder in
[Ring v2 design outcome (U1)](#ring-v2-design-outcome-u1) for what each rung
does and does not arm; wide is a value-space widening of existing header
fields, so ring `VERSION` stays 1.

The **wire** is still S16 on every box that has not said otherwise, and by a
different mechanism than the layout: the resolver answers it.
`jasper.fanin_coupling.resolve_ring_wire` is the one per-box resolution every
declaring end reads, for every ring end and for outputd's
`JASPER_OUTPUTD_CONTENT_FORMAT` — so a ring-armed box stays coherently narrow
even on a box whose program-lane default is `S32_LE` (see "Where this corrects
#2285"). Because the accept-set is wider than the wire, the attach can no longer
be relied on to refuse a drift *inside* it: the ends are compared rather than
assumed. The C ioplug takes `format`/`channels` from its conf.d block, but
[`deploy/alsa/conf.d/60-jts-ring.conf`](../deploy/alsa/conf.d/60-jts-ring.conf)
declares neither on any of its three PCM blocks, so all of them open at the
`S16_LE`/2ch defaults (`JTS_RING_RATE = 48000` stays pinned) until
`jasper-audio-hardware-reconcile` renders the box's resolved wire into them.

**"Coherently narrow" is the resolver's DEFAULT, not a fleet-wide law** (E7,
2026-08-11 — the ruling record is `captures/PLAN-ring-v2-rulings-2026-08-10.md`;
do not re-derive it here). The format axis has exactly one input,
`JASPER_FANIN_RING_WIRE_FORMAT`, which `jasper-fanin` and
`resolve_ring_wire_format` classify identically
([`tests/test_ring_wire_format_contract.py`](../tests/test_ring_wire_format_contract.py)
pins that against the Rust source); unset means narrow, which is why the fleet
is. A box whose live post-DSP path is already **wide** declares the wide wire
and re-runs the hardware reconciler BEFORE it arms, so the ring joins the
one-quantization invariant instead of adding a second reduction inside it.
Until 2026-08-11 the Python resolver took no input at all and pinned the wire
narrow by policy, so such a box could not arm coherently in either direction —
found when jts3's arm halted on the format shear
(`captures/r7b-jts3-arm2-20260811T132227Z`). Arming wide also needs the
installer's ioplug provenance record, because a non-default wire renders a
conf.d `format` key an older `.so` cannot parse (`ring_wire_caps_ready`).

So ring v2 was never a layout redesign: it is a transport + reader/writer +
emitter + resolver problem. The transport layer (R1's crate, R2's ioplug),
both daemons (R3 fan-in, R4 outputd), and the resolver (R5a/R5b) are all
merged — the R1–R5 ladder is complete.
Certified
geometry is 2 slots × 128 frames per ring, CamillaDSP chunk/target 128,
`enable_rate_adjust: false`; Ring A contributes ≈5.3 ms at 48 kHz.

### What the legacy `direct` hop costs, measured

Nothing on the aloop content hop absorbs the content-producer-vs-DAC
offset, so it accumulates until the capture ring drains and outputd
zero-fills a short read. On jts3 (2026-07-27, issue #1768) that was
**metronomic: one fill every ~17.5 minutes, all day** — `count` +1,
`empty_periods` +2, `partial_periods` +2 each time, ~2176 frames inserted
per event over ~1050 s ≈ **43 ppm**, the expected crystal-vs-crystal
offset between two free-running clocks. Each fill INSERTS samples into
the emitted timeline: audible as a brief tear, and it displaces the rest
of the program in time — which is how it corrupted a crossover MEASURE
capture (#1765) as a clean +64-sample splice. This is the argument FOR
the ring graph and AGAINST reviving `rate_match`: a second rate matcher
inside an already DAC-paced domain is the duplicate-clock class the end
state forbids, whereas the one-clock ring graph removes the hop and the
offset with it. On a `direct` box the fill is expected and observable
(`event=outputd.content_fill`, plus the
`outputd_content_fill_increased` measurement-integrity gate).

### Where this corrects #2285

#2285's body describes `ring_edge_width_ready` as a global-constant
equality against `DEFAULT_PLAYBACK_FORMAT`. That was its shipped shape in
the wide-output-path program's PR-1; **PR-6 (2026-08-08) reworked it into
a kwargs-coherence check** —
`content_lane_format_for_coupling(COUPLING_SHM_RING)` must equal the
box's resolved ring wire format (`jasper/fanin/coupling_reconcile.py`;
R5a repointed both sides at `resolve_ring_wire`). The planning
consequence: the box-wide `S32` default does **not** disarm rings. A
ring-armed box installs the coupling's own CamillaDSP kwargs, which force
both ring ends to the ring's wire format, so ring boxes stay coherently
S16 — keeping their certified latency — until ring v2 arms them wide.
Wide is the loopback path's property in the interim.

#2285's banner is corrected: an earlier draft said the plan owns "U0–U9
sequencing", but the sequencing this plan actually ships is **U0–U4**
(see "Sequencing — the U arcs" below). The banner was fixed to U0–U4 on
2026-08-10, when this plan merged as canonical via PR
[#2293](https://github.com/jaspercurry/JTS/pull/2293).

### Fleet

| Box | Hardware / role | State at 2026-08-10 |
|---|---|---|
| **jts3** | Pi 5 + DAC8x + XVF3800, active 2-way, the horn | Installed `9cc41b987` (~03:40). Coupling `loopback` + `direct`, coherent; `choice=auto`; combo disarmed. AEC profile `auto`, voice ACTIVE — chip-AEC confirmed active; **commission state to re-verify at U1 activation**. Expected wide end-to-end (content S32 → i32 → DAC S32) — that follows from the DAC8x registry edge plus the installed build, and was proved live by the wide-output-path program's PR-7, but was not re-probed this pass |
| **jts.local** | Pi + single Apple dongle (card `A`), passive, USB-in box | Installed `9cc41b987` (~03:40). Coupling `shm_ring` + `shm_ring`, coherent; `choice=auto`; combo ARMED. Profile `xvf_chip_aec`, voice INACTIVE (parked; **commission owed**). Registry declares the dongle edge `S24_3LE`; the live negotiated value is unconfirmed by this pass. **Route-latency revalidation artifact owed** |
| **jts4** | Zero 2 W streambox + InnoMaker, loopback | Not probed this pass. Zero-class validation target for U1 |
| **jts5** | InnoMaker HAT, S32 edge live, composite/active testbox | Not probed this pass. **Parked by owner intent — do not commission or play it for tests** |

Certified USB route-latency baseline to compare against: **p50 36.35 /
p95 37.93 / p99 38.29 ms** over 1094 impulses / 32.6 minutes. The quick
and promotion thresholds (p95 ≤ 40 ms; p99 ≤ 42 ms over ≥1000 jittered
impulses / ≥30 minutes) are owned by
[HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md) and
[testing-tooling.md](testing-tooling.md); they are named here only
because R-RING2 keys on them.

---

## Ratified decisions (owner-ratified 2026-08-10 — executors do not reopen)

These sit on top of the wide-output-path program's D1–D10, which remain
binding for the egress (`captures/PLAN-wide-output-path-2026-08-07.md` §3).

**R-WIDTH — one reduction, declared by data.** A fixed wide program spine
with exactly one information-losing width reduction, at the DAC edge, in
outputd, driven by `DacProfile` data. **No cross-daemon format
negotiation, ever**: declare → open → verify → park is the contract
(consistent with D1/D2 and the sealed program's "format negotiation
loops: never" exclusion). Purpose-built narrow taps stay narrow by
contract — AEC and wake carriers, the mic wire, `rms_dbfs` meters, the
multiroom snapclient pipe, diagnostic taps. 48 kHz is pinned.

**R-RING2 — one design, both axes.** Ring v2 covers sample format (`S32`)
**and** N channels in a single design, preserving frame geometry (2 slots
× 128 frames) and pacing. Per-box activation is gated on a **fresh**
route-latency artifact compared against the certified distribution above
— not merely on clearing the 40/42 ms ceilings.
[#2147](https://github.com/jaspercurry/JTS/issues/2147) (slot size derived
from the DAC floor) is **out of ring-v2 activation scope** — a compatible
follow-up only. Renderer migrations (P6) land **once**, on v2, never onto
ring v1.

**R-POPS — parked, with a resurrection trigger.** The 2026-08-08 owner
report of new very-high-frequency pops after the flip is parked: the owner
re-tested 2026-08-10 and could not reproduce it, and the suspected cause is
transient config-load state from the measurement flow. **Trigger: any
recurrence under normal playback makes this a diagnose-first workstream
that precedes further pipeline flips.** Recorded so it is not lost.

---

## Sequencing — the U arcs

The U arcs are the top-level order. They group the P-rows (kept for
continuity — issues and PRs reference them) and the width workstream.

| Arc | Delivers | Rolls up | Exit gate |
|---|---|---|---|
| **U0 — stabilize + replan** — **COMPLETE** | this document (PR [#2293](https://github.com/jaspercurry/JTS/pull/2293), merged, gate 0/0 after fix round); the P5c deletion (PR [#2302](https://github.com/jaspercurry/JTS/pull/2302), merged, gate 0/0 after fix round); PR [#2281](https://github.com/jaspercurry/JTS/pull/2281), merged, gate 0/0 after delta | P5c | doc merged; `rate_match` + adaptive-buffer + stale cushion prose gone. jts.local commission + route-latency revalidation no longer gates U0 — it now proceeds under U1's R6 rung below, since ring v2 width activation needs exactly that fresh artifact and a bare pre-ring-v2 commission would be thrown away |
| **U1 — ring v2** | the R-RING2 design, build, and per-box activation (design ratified 2026-08-10 — see [Ring v2 design outcome](#ring-v2-design-outcome-u1) below) | P8 | R1–R5 ladder complete (all merged). R7's design converged (v2 + ratified errata); jts3's hardware phase — 30-minute soak windows per owner amendment, box-side dead-man timer per owner directive — **PASSED 2026-08-11** (all three windows, every gate; DAC presentation latency 63.833 → 5.167 ms), clearing R7a's DAC8x `LatencyFloor(256, 1536, 128, 256)` — its PR drafting dispatched 2026-08-11 — and R7b's implementation dispatched 2026-08-11, in flight. jts.local's R6 activation stays separately owner-gated (home box, not yet granted). jts3 still kills the measured ~17.5-minute content-fill splice class once R7 lands. Then jts4 Zero-class validation; jts5 / bonded per the P8 scope ruling |
| **U2 — source width** | #2223's 3-PR ladder plus its Step 0 descriptor check; the TTS/earcon tail | width workstream | bit-pattern fixtures prove low bits survive fan-in. Loopback boxes may flip before U1 completes; ring boxes flip after |
| **U3 — renderer ring ingress** | P6a–d, one lane at a time, **AirPlay LAST** with offset re-derivation | P6 | per-lane source pass; AirPlay adds a Music.app local-track loop + resync-log watch + bonded A/V spot-check |
| **U4 — delete** | P7 dsnoop re-points, P9 snd-aloop removal after fleet burn-in, P10 polish + `audio-paths.md` rewrite | P7, P9, P10 | full-fleet deploy + doctor + every-source pass; reboot test per box |

**Parallel width tail**, independent of the arcs:
[#2255](https://github.com/jaspercurry/JTS/issues/2255) (bounded
per-child composite xrun recovery — the safety prerequisite) →
[#2257](https://github.com/jaspercurry/JTS/issues/2257) (packed-24
composite edge).

### P-row status

| P | Row | Status (2026-08-10) |
|---|---|---|
| P0 | fan-in host-compliance persistence | **DONE** — `rust/jasper-fanin/src/host_compliance.rs` |
| P1 | Ring platform ship (inert) | **DONE** — `deploy/lib/install/ring-platform.sh`, `deploy/alsa/conf.d/60-jts-ring.conf`, `deploy/tmpfiles/jts-ring.conf` |
| P2 | Ring citizenship | **DONE** — emitters, `coupling_reconcile` `shm_ring` mode, topology contract, statefile seeding, artifact binder, `/state`, doctor |
| P3 | USB combo default-on where the gadget is present | **DONE** |
| P4 | Rings default on validated full-profile solo-stereo boxes | **DONE** — jts.local armed; jts3 correctly resolves `loopback` (roleful topology); jts4 / jts5 excluded by topology and profile, not hostname |
| P5a | Delete Python usbsink pump + lean-FIFO lane + Rust solo aloop mode | **DONE** |
| P5b | Delete `transport_pipe` | **DONE** (2026-07-11) |
| P5c | Delete `rate_match` + adaptive-buffer + stale cushion recipes | **DONE** — PR [#2302](https://github.com/jaspercurry/JTS/pull/2302), merged 2026-08-10, gate 0/0 after fix round. Owned the `.env.example` and `HANDOFF-usb-low-latency.md` prose edits |
| P6a–d | Renderer lanes → ring ingress (librespot, bluealsa, correction, shairport LAST) | **OPEN — U3.** Net-new build: fan-in's `Input` is aloop-PCM-or-USB-DIRECT only, with no ring-reader variant |
| P7 | Re-point dsnoop consumers; drop the fan-in aloop mirror | **OPEN — U4** |
| P8 | Ring v2 | **OPEN — U1**, rescoped by R-RING2 to cover format and channels in one design. R1/R2 merged — see [Ring v2 design outcome](#ring-v2-design-outcome-u1) below |
| P9 | snd-aloop removal | **OPEN — U4**, hard-gated on P6 + P7 + P8 (P8b specifically, per the P8a/P8b split — see [Ring v2 design outcome](#ring-v2-design-outcome-u1) below) |
| P10 | Polish sweep + `audio-paths.md` rewrite | **OPEN — U4** |

Deletions stay separate PRs by repo guardrail.

### Ring v2 design outcome (U1)

R-RING2 (above) went through a 3-lens adversarial design panel
(correctness / hearing-safety / resilience) on 2026-08-10 — 10
blockers found and resolved across the three rounds (4 correctness, 3
hearing-safety, 3 resilience). This is a compact summary — the
ratified record is `captures/PLAN-ring-v2-design-2026-08-10.md` (the
draft) and `captures/PLAN-ring-v2-rulings-2026-08-10.md` (the rulings,
all three panel rounds, and the final synthesis), both untracked local
sealed records per house convention. Read those; this section points
at them rather than restating them.

**The R-ladder P8 rescopes to:**

| Rung | Scope | Status (2026-08-11) |
|---|---|---|
| R1 | `rust/jasper-ring` crate | **DONE** — PR [#2297](https://github.com/jaspercurry/JTS/pull/2297), merged, gate 0/0 |
| R2 | C ioplug | **DONE** — PR [#2296](https://github.com/jaspercurry/JTS/pull/2296), merged, gate 0/0 |
| R3 | fan-in | **DONE** — PR [#2308](https://github.com/jaspercurry/JTS/pull/2308), merged; live-audio-path 3-lens panel, fix round to 0/0 |
| R4 | outputd | **DONE** — PR [#2310](https://github.com/jaspercurry/JTS/pull/2310), merged; 3-lens panel (found the reader-side twin of R3's marker-breadth defect, fixed symmetrically), delta 0/0 |
| R5a | schemas / parsers / renderer | **DONE** — PR [#2314](https://github.com/jaspercurry/JTS/pull/2314), merged; gate 0/2/5, fix round to 0/0 |
| R5b | gates / recovery / observability | **DONE** — PR [#2320](https://github.com/jaspercurry/JTS/pull/2320), merged; gate 0/4/7, fix round to 0/0 — **closes the R1–R5 ladder** |
| R6 | jts.local width activation | **owner-gated** (home box; not yet granted) |
| R7a | DAC8x `LatencyFloor` + floor soak | **soak PASSED** 2026-08-11 (all three 30-min windows, every gate; DAC presentation latency 63.833 → 5.167 ms) — floor CLEARED at `LatencyFloor(256, 1536, 128, 256)`; floor PR drafting dispatched 2026-08-11 |
| R7b | jts3 active-half | design converged (v2 + ratified errata) — implementation dispatched, in flight |

R1 and R2 land the ring transport layer v2-capable in both languages —
wide/N-channel geometry is accepted and byte-copyable end to end — but
inert until R6/R7 arms anything: no conf.d on the fleet declares a
non-default format/channels yet, so every box still opens S16/stereo.
CI hardening (PR [#2300](https://github.com/jaspercurry/JTS/pull/2300),
merged) closes a gap R2's review surfaced: the ioplug's `.so` now
compiles in the `rust` CI job's C step, contract-pinned so the check
can't silently regress.

**R7 package (2026-08-11).** A second 3-lens panel on the R7a/R7b
redesign found 7 blockers / 14 SF (repanel rounds 1–3: correctness,
resilience, hearing-safety), forcing a design revision to v2; each
lens then returned a focused delta, and the consolidated errata
(E1–E6, all ratified) closed the panel CONVERGED. Deepest find: an
incoherent stereo-predicate pair could silently admit the active ring
as a stereo sink onto the horn — closed by a bail-on-incoherent-pair
check plus a mode-scoped allowlist in `Config::from_env`. Full
record: `captures/PLAN-ring-v2-rulings-2026-08-10.md`, "R7 PACKAGE"
onward.

Three follow-ups filed rather than folded into a rung:
[#2294](https://github.com/jaspercurry/JTS/issues/2294) (doctor
observability — a dangling check name, and floor-blocked ring
ineligibility reporting `ok` with no reason shown) — targeted to ride
R5, still open now that R5a/R5b have merged;
[#2306](https://github.com/jaspercurry/JTS/issues/2306) (P5c
follow-ups, including the explicitly-owed Pi-side doctor pass) rides
the R6 session;
[#2319](https://github.com/jaspercurry/JTS/issues/2319)
(`camillagui.socket` — unauthenticated root listener on
`0.0.0.0:5005` with `ReadWritePaths=/etc/camilladsp`, surfaced by the
R7 hearing-safety repanel; pre-existing, out of R7 scope) — open.

**P8 splits.** P8a is this ladder — solo-stereo width plus active
N-channel. P8b — composite ring plus bonded round-trip ingress — moves
out to later, and still hard-gates P9; jts5 stays on aloop lanes 5/6
until P8b.

**Forced ordering inside jts3: R7a before R7b.** jts3 is blocked on a
missing `DacProfile.latency_floor` declaration for `HIFIBERRY_DAC8X`:
the full 4-field `LatencyFloor` (`camilla_chunksize`,
`camilla_target_level`, `outputd_period_frames`,
`outputd_dac_buffer_frames`, with `camilla_target_level` enforced ≥4×
`camilla_chunksize`). Per the ring v2 rulings record's round-2 amendment
(`captures/PLAN-ring-v2-rulings-2026-08-10.md`), declaring it for
`HIFIBERRY_DAC8X` also sets jts3's CamillaDSP chunk/target — it touches
the **active graph's** chunksize, not just outputd's period. All three
panel lenses verified that ordering independently. **Correction from
the R7 repanel:** jts3 actually runs `aec-init` in **CORPUS mode** — no
alignment artifact exists (the 2026-08-08 closeout's corpus-exit was
undone by open trap
[#2254](https://github.com/jaspercurry/JTS/issues/2254)) — so there is
no commissioned identity on jts3 for the floor change to invalidate.
The one jts3 chip-AEC recommission this section used to require is
DROPPED from the R7 runbook as inapplicable; that recommission logic
stays load-bearing for jts.local's eventual R6 and any genuinely
commissioned box. Exiting corpus mode on jts3 is a separate owner
decision, out of this campaign's scope.

**Key ratified rulings** (rationale in the rulings file): R-WIDTH
stands unchanged, including its no-cross-daemon-format-negotiation-ever
contract; ring `VERSION` stays 1 (value-space widening on existing
fields, not a layout change); `MAX_RING_CHANNELS = 8`, mono excluded
(policy, not layout), composite excluded (moves to P8b);
`MAX_SLOT_BYTES` stays 64 KiB (round 2 proposed tightening to 8 KiB;
round 3's resilience lens overruled it — 32 KiB is #2147's legitimate
future case); `resolve_ring_wire` stays equality-only, never ranking;
renderer migrations (P6) land once, on v2, never onto v1 (reaffirmed
from R-RING2).

**The ACTIVE-ring arm/rollback lifecycle.** This is the durable home for
the ordering; the docstrings on `jasper-active-speaker baseline-reemit`,
`_outputd_actions`, and `check_fanin_coupling` point here rather than
each restating it.

A roleful box's ACTIVE ring is armed by an operator, in three steps, in
this order — and the order is forced, not stylistic:

```
ARM       jasper-active-speaker baseline-reemit --endpoint ring
       -> systemctl start jasper-audio-hardware-reconcile     (marker -> 1)
       -> jasper-fanin-coupling-reconcile shm_ring            (path -> active ring)

ROLLBACK  jasper-active-speaker baseline-reemit --endpoint aloop
       -> systemctl start jasper-audio-hardware-reconcile     (marker -> cleared)
       -> jasper-fanin-coupling-reconcile loopback            (ring keys unset)
```

**Step 1 declares the RESOLVED wire, and a shear is caught at the gate.** The
re-emit's ring-endpoint graph takes its `format` from
`resolve_ring_wire` and its CamillaDSP `chunksize`/`target_level` from the
certified ring geometry (`RING_CAMILLA_*`, chunk/target 128 with
`enable_rate_adjust: false`) — **never** the box's program-lane default or its
DAC `LatencyFloor`, which describe the LOOPBACK lane. jts3's floor target alone
(1536) is six times the whole 2-slot ring's 256-frame capacity. Both come from
one derivation keyed on the sink device
(`jasper.active_speaker.camilla_yaml.active_sink_params`), so a caller that
re-points a graph at the ring cannot pick up one half and miss the other.
A graph that declares the wrong wire anyway is refused at **step 3** by
`ring_edge_width_ready`, which inspects the loaded graph as a declaring end and
names the lane that sheared — it is not left to fail at the ioplug attach.
Both defects were live at `c4c9bfe1c` and halted the 2026-08-11 arm
(`captures/r7b-jts3-arm2-20260811T132227Z`): the re-emit inherited `S32_LE` from
the program lane, and the width gate reported "all declaring ends" while
structurally unable to see the graph.

**Why the graph moves first.** The endpoint marker
(`JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT`) is derived by the hardware
reconciler from the classification of the graph the statefile points at.
The graph's playback device is in turn chosen by `resolve_output_layout`
from that marker. Left to themselves the two only reproduce each other —
a fixed point that holds in BOTH directions, so a box can neither arm nor
release. `--endpoint` is the explicit operator act that breaks it: it
re-emits the applied baseline against a NAMED endpoint, publishes it over
the artifact the statefile reads, and repoints the statefile. Everything
after that is derivation. (Ratified as Option B in the R7b panel round 2;
it amends E1's earlier coupling-first rollback ordering for the same
reason the arm needs it.)

**Why every intermediate state is safe.** After step 1 the graph on disk
names the ring while the coupling is still loopback. Nothing in steps 1 or
2 reloads CamillaDSP — `baseline-reemit` writes the artifact and repoints
the statefile, and `jasper-audio-hardware-reconcile` bounces outputd but
never `jasper-camilla` — so the *running* Camilla is still on the previous
graph and the box usually keeps playing through both rungs. At the next
Camilla load the new graph takes effect: Camilla writes a ring nobody reads
and outputd reads an unwritten ALSA lane — silence, not wrong audio. After
step 2 the marker is set but the bridge is still `direct`, and outputd's
ring-path allowlist is scoped to the `shm_ring` bridge, so the marker grants
nothing. Only step 3 moves audio. A crash between any two steps leaves
silence (the restart is itself a Camilla load) and re-running the ladder
converges.

That split — playing now, silent after the next load — is why the waypoint
needs a standing surface rather than an operator's memory: the box that
looks fine while you are standing at it is the box that comes back silent.

The ROLLBACK side's step-2 window is louder than silence, in one of two
ways depending on whether the validator can read the graph:

- **When it can** (the ordinary armed box), rollback step 2 is **refused**.
  Its candidate is validated against the coupling, which is still
  `shm_ring` until step 3, while step 1 has already moved the graph to the
  ALSA lane — so it fails the ring-plan endpoint comparison (`transport
  plan is shm_ring but Camilla playback='outputd_active_content_playback'`)
  and exits 78 with the marker preserved. This is a real rough edge, not a
  dead end: step 3 does not gate on the marker, so running
  `jasper-fanin-coupling-reconcile loopback` anyway completes the rollback,
  the box validates clean again, and a later hardware reconcile clears the
  stale marker. Verified through `validate-outputd-env` in a scratch probe,
  not on hardware.
- **When it cannot** (graph evidence unreadable, so the endpoint comparison
  is skipped), the marker clears and outputd restarts onto a cleared marker
  against a still-armed active ring path — the crossed pair its startup
  biconditional refuses — so **outputd** exits 78 and parks.
  `RestartPreventExitStatus=78` means restarting outputd does not clear it
  (the crossed declaration is still on disk); running step 3 does.

Either way the refusal is now non-destructive — see the refusal promise
below.

**The mid-arm waypoint is a NOTE, not an error.** The post-step-1 state
above is also what `jasper-audio-hardware-reconcile` validates against
when you run step 2, and until 2026-08-11 that validator called it a
contradiction — so step 2 refused the state step 1 has to create, step 3
refuses without step 2's marker, and **no ordering completed**. Observed
on jts3 at `8f021e6ac`: exit 78, `preserved=1`, detail `post-DSP route has
no registered outputd capture for Camilla playback jts_ring_active_playback`
(`captures/r7b-jts3-arm-20260811T111338Z`). `transport_coherence_report`
(`jasper/audio_runtime_plan.py`) now recognizes `jts_ring_active_playback`
under a loopback plan **by name** and reports it as a note: the CLI exits 0
printing `ok note=…`, the reconciler logs
`event=audio_hardware_reconcile.outputd_env_note` (only on a pass that
actually stages a changed `outputd.env` — the validator runs on the staged
candidate, so a reconcile of an already-converged waypoint box logs
nothing), and `jasper-doctor`'s
`jasper-outputd` check **warns** — because the statefile repoint survives a
reboot, so a box left at the waypoint comes back silent even though it was
playing when the operator walked away. `jts_ring_playback`
(the full-range stereo ring) under a loopback plan keeps the hard error:
no ladder creates it, so there is no next step that makes it coherent.

The note is name-only on purpose. It does **not** re-check rolefulness,
conf.d staging, or ring width — `outputd_active_lane_decision` in step 2 is
the one arm authority, and a second derivation in the validator is the
drift that produced the deadlock.

**A refused reconcile leaves the box running exactly as before.** When
`jasper-audio-hardware-reconcile` rejects a staged `outputd.env` it logs
`outputd_env_invalid … preserved=1`, then
`outputd_candidate_rejected action=preserve_runtime_env services=unchanged`,
and exits 78 **without stopping anything**. The runtime env is
byte-unchanged and the exit precedes every render, so the box is still
running the configuration it was running a second ago. It used to park
(stop `jasper-voice` *and* `jasper-outputd`) first; on jts3 that took the
assistant down cleanly and silently, and recovery needed a hand-run
`systemctl start jasper-aec-reconcile`.

**What holds the pair coherent.** outputd enforces a biconditional at
startup: the active ring file may be read only by an armed active
endpoint, and an armed active endpoint may read only that file. The two
halves have two writers — the hardware reconciler owns the marker, the
coupling reconciler owns the path — so the path is DERIVED from the marker
(`_outputd_ring_path_for`) rather than preserved. A preserved path is how
an armed box gets handed the full-range stereo ring and parks at exit 78
with every daemon reporting healthy.

**How to verify step 2 landed.** Read `/var/lib/jasper/outputd.env`. The
single field that moves is `JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT`, from the
explicit-empty `''` to `1`. Two traps, both hit on jts3:

- **`JASPER_OUTPUTD_ACTIVE_LANE=1` is not the arm.** It is already `1` on
  an UNARMED roleful box — it means "this box has an active lane", not
  "the ring is armed". Checking it tells you nothing about step 2.
- **`jts_ring_active_playback` is a graph PCM name, never an env value.**
  It appears in the CamillaDSP config the statefile points at (and in the
  reconciler's `active_endpoint=` log field); grepping `outputd.env` for it
  will always come up empty, armed or not.

Cleared means the explicit-empty `=''`, not an absent key.

**Running either ladder pins the box to operator coupling.** Any explicit
`jasper-fanin-coupling-reconcile <coupling>` — including step 3 of the
rollback — stamps `JASPER_FANIN_COUPLING_CHOICE=operator` into
`/var/lib/jasper/fanin.env`, and that survives deploys. A pinned box is
skipped by every later `--auto` pass (boot and install), so it holds the
coupling it was pinned to.

**The way back is deleting the key, and nothing does that for you.** The
marker is binary by construction: present-and-`operator` vs absent, with
absent meaning auto-owned (`is_operator_choice` in
`jasper/fanin/coupling_auto.py`). But the single write site hardcodes
`operator`, `--auto` never clears it (it declines to stamp, which is not
the same as unstamping), and both positional values re-stamp it — so
"re-run the reconciler to go back to auto" does not work. Today the only
route is to remove the `JASPER_FANIN_COUPLING_CHOICE=operator` line from
`/var/lib/jasper/fanin.env` as root and then run
`jasper-fanin-coupling-reconcile --auto`. That is a hand-edit of a file the
reconciler is meant to own solely; a proper `--release` affordance is
tracked as a follow-up.

**Operator symptom → step.** `jasper-doctor`'s `fan-in coupling` check
warns when the loaded graph does not name the ring the marker selects, and
names this ladder in its remediation. On a roleful box with a CLEARED
marker it deliberately does not report "expected `jts_ring_playback`":
that device is a forbidden token for every active emitter, so no roleful
graph can ever name it.

## What still has to be deleted or built

The 2026-07-03 no-dupes audit is preserved in the appendix; this is what
survives it, verified at `9cc41b987`.

| Row | What survives, and where |
|---|---|
| **P6** (U3) | Four aloop renderer lanes — librespot, shairport-sync, bluealsa-aplay, correction sweeps — all `plug:` wrappers over `*_substream` PCMs. fan-in's `Input` carries an aloop `pcm` or a USB `direct` capture and nothing else, so the ring-reader lane source is a **net-new build**, not a re-point |
| **P7** (U4) | `aec_tune` runs `arecord -D jasper_capture`; the AEC bridge carries `REF_DEVICE = "jasper_ref"`; fan-in's `RingOutput` keeps a lossy aloop MIRROR on lane 7 — which is *why* the dsnoop consumers survive ring coupling, and why the re-point may follow the default flip but must precede snd-aloop removal. Doctor pins to rewrite: `check_loopback` (`doctor/audio.py`), `check_fanin_asound_wiring` (`doctor/audio_runtime.py`), `check_shairport_sync_loopback_plughw` (`doctor/renderers.py`) |
| **P8** (U1) | outputd hard-gates `shm_ring` to a full-range stereo single-ALSA sink (`is_full_range_stereo_lr_sink` in `rust/jasper-outputd/src/config.rs`), so aloop lane 5 is the **only** N-channel content path, and bonded ACTIVE followers' snapclient writes `hw:Loopback,0,6`. Both are why P9 is hard-gated on P8 |
| **P9** (U4) | Both snd-aloop drop-ins ship, and `deploy/alsa/asoundrc.jasper` defines the renderer substreams, the `outputd_content_*` / `outputd_active_content_*` pairs, and the `jasper_capture` / `jasper_ref` dsnoop taps |

The aloop surface is broad and concentrated: a loose
`Loopback|_substream|snd-aloop` grep matched ≈180 files at `9cc41b987`
(most of them prose), with `docs/HANDOFF-fan-in-daemon.md`,
`deploy/alsa/asoundrc.jasper`, `tests/test_doctor_renderers.py`, and
`rust/jasper-fanin/src/config.rs` holding the largest shares. Re-derive
that count with the grep after any deletion PR rather than trusting this
number.

## Gates every arc passes

Owned elsewhere; listed so no arc can claim to be done without them.

- **AEC / mic / wake** ([HANDOFF-aec.md](HANDOFF-aec.md)) — every current
  wire shape and frame cadence unchanged; no blocking work, allocation,
  or variable-time conversion in the output/reference hot loops; AEC
  reference byte-identical for unchanged S16 fixtures (or a narrowly
  justified rounding difference with an equivalence test); wake/ASR
  parity. Recommission **only** when the live route or the DAC-edge
  format actually changes — D6 keeps the content-lane format out of
  `AlignmentIdentity` deliberately.
- **USB route latency**
  ([HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md)) — a fresh
  `jasper-route-latency-harness` artifact per change to fan-in width,
  ring layout, Camilla coupling, buffer geometry, or route identity,
  compared as a full distribution against the certified baseline. Stale
  or config-mismatched artifacts do not count.
- **Pi budget** — no new resident daemon, hot-loop thread, or poll loop;
  zero allocation in steady-state fan-in/outputd loops; work stays
  O(frames × channels); validated on the 1 GB Pi 5 class and on Zero 2 W
  for the paths that run there. **No buffer or period increase is accepted
  as an unmeasured stability response to wider samples.**
- **Hearing safety** — `devices.volume_limit: 0.0` in every generated
  config; `CamillaController.set_volume_db` keeps clamping positive writes.

## Risk register

1. **AirPlay sync regression** (highest, U3). The ioplug's honest
   occupancy-derived delay changes the number shairport compensates;
   mis-deriving the offset reintroduces the resync-storm class. Mitigate:
   migrate LAST, re-derive `audio_backend_latency_offset_in_seconds` in
   the same PR, keep `resync_threshold_in_seconds = 0.2` through the
   migration, per-lane rollback to the aloop conf until P9, re-run the
   bonded A/V spot-check.
2. **Active/composite + bonded aloop dependencies survive naive
   deletion** (U1/U4). Ring v1 is stereo-pinned, outputd requires a
   full-range stereo sink for `shm_ring`, active N-channel content rides
   lane 5, and bonded followers write `hw:Loopback,0,6`. P9 is HARD-GATED
   on P8.
3. **A ring box silently narrows, or a wide box arms a narrow ring.**
   `ring_edge_width_ready` plus the reconciler's per-coupling emission
   are the belt; `check_camilla_playback_format` is the braces. Ring v2
   extends both rather than bypassing them.
4. **Fleet box wedged mid-migration.** Coupling transitions are owned by
   the ordered reconciler with fail-safe-to-loopback, and camilla's
   ExecStartPre statefile re-seed means any restart reverts to the
   contract config (fail-safe = silence, not noise). Every rollback
   before U4 is env-only.
5. **Deploy ordering on a format flip.** install.sh's core bounce
   restarts Camilla conditionally and last, which can wedge a
   content-format delta (old Camilla holds the lane at the old format;
   new outputd's open fails). The wide-output-path program solved this in
   its PR-6; any U1/U2 flip inherits that obligation.
6. **Streambox divergence.** jts4 runs the full renderer graph with only
   voice/AEC parked and has no mic to notice breakage audibly — include
   it explicitly in every arc's validation.
7. **Certification regression.** Do not arm a coupling the artifact
   binder rejects: `usb_low_latency_48k` claims then fail fleet-wide and
   the doctor goes red.
8. **CamillaDSP config drift.** Ring configs come from the emitters,
   never hand-written YAML; `camilladsp --check` and the `volume_limit`
   ceiling stay enforced.

## Done criteria

- **Code**: no `snd-aloop` / `Loopback` references outside historical
  docs; `rate_match`, adaptive-buffer, and cushion-recipe code deleted;
  guard tests assert the production route refuses every deleted env knob.
- **Width**: a format/provenance ledger for every shipped source and
  coupling matches live readback; automated contracts prevent an
  unrecorded information-losing conversion upstream of outputd's
  finalizer; S16 fixtures stay byte-transparent through ordinary
  gain/duck plumbing; every registered DAC reaches its widest **proved
  and transport-supported** edge, and unknown hardware fails safe.
- **Config**: both snd-aloop drop-ins gone; `/etc/asound.conf` carries
  only DAC + ring-lane definitions; `.env.example` documents exactly the
  surviving keys. **Clock discipline**: one rate matcher per foreign
  ingress, each visible in `/state` (fill / target / lock / ppm / xruns).
- **Fleet**: jts.local, jts3, jts4, jts5 on rings with ≥ 7-day burn-in
  each, zero sustained resampler unlocks / ring rails / xruns; jts.local
  re-certified (quick + promotion artifacts under the ring-v2 identity);
  per-box AirPlay / Spotify / BT / USB passes; bonded pair S0-sync bench
  if bonded.
- **Listening**: music fades, provider TTS tails, opening/closing
  earcons, and the tweeter/horn path all covered — with the subjective
  verdict recorded **separately** from the architectural one. "The pop is
  gone" is not an acceptance criterion for this program (see R-POPS).
- **Docs**: `audio-paths.md` rewritten to the ring lane map; the
  usbsink / speaker-output-reference / fan-in handoffs updated; README
  atlas and doc-map routing current.

## Cross-program coordination

**jts3 is shared with the crossover-v2 program**
([#2291](https://github.com/jaspercurry/JTS/issues/2291)). Every audible
or hardware session on jts3 — U1 activation, recommissioning, listening
verdicts — is announced and serialized with the owner's measurement runs:
a capture corrupted by an audio-graph experiment costs that program a
whole run.

## Appendix A — renderer ring ingress design (U3 / P6)

Design reference for the U3 executor, kept out of the campaign spine
above. This is forward design, not narrative.

**Delay honesty — the shairport question.** The ioplug reports an honest,
occupancy-derived playback position (`jts_ring_pointer` /
`jts_ring_pointer_report`; four adversarial rounds fixed the
dishonest-pointer and mod-buffer-alias classes), and `jts_ring_delay`
([`c/jts-ring-ioplug/pcm_jts_ring.c`](../c/jts-ring-ioplug/pcm_jts_ring.c))
returns `published-unread slots × period + staged frames`. A sync engine
reading `snd_pcm_delay` therefore sees truth — the OPPOSITE of the aloop
history, where `snd_pcm_delay` returned loopback ring FILL rather than DAC
latency and caused the ~60 s resync glitch storm until
`resync_threshold_in_seconds = 0.2` (PR #83). Consequences: shairport's
`audio_backend_latency_offset_in_seconds` — substituted into the rendered
conf by `derive_audio_backend_latency_offset` in
`deploy/bin/jasper-apply-airplay-mode`, re-run on every shairport-sync
start via the unit's `ExecStartPre` (`renderers.sh` installs the
template and the renderer script, and seeds once) — MUST be re-derived for the
ring graph, because the ring holds frames the offset math attributed to
the aloop ring; and the 0.2 threshold may be revisitable afterwards, but
only on measurement — keep 0.2 through the migration. This is why
AirPlay migrates last.

**Per-lane clock reconciliation stays at fan-in ring-read.** Renderer
lanes keep their `LaneResampler` exactly as on aloop: the transport
changes, the one-rate-matcher-per-foreign-clock placement does not. The
ioplug is a dumb frame carrier.

**ALSA conf shape per renderer.** Renderers emit native rates (AirPlay
44.1 k, BT variable), so each lane keeps its `plug:` wrapper layered over
a ring device — preserving `defaults.pcm.rate_converter` (the AEC HF-loss
history) and keeping renderer device names stable, so each flip is one
conf edit plus a renderer restart rather than a unit-file change.
Definitions ship system-wide 0644 in conf.d, and **each migration PR
re-runs the PR #214 probe** (`sudo -u <runtime-user> aplay -D <device> …`,
codified as `check_renderer_device_resolvable`). The ioplug WRITER creates
the ring file, so renderer users (`shairport-sync`, `pi`) must create and
write under `/dev/shm/jts-ring/`, which P1 ships via tmpfiles.d with group
write.

**Teardown/hotplug semantics.** aloop pairs persist param-locked across
renderer restarts (`pcm_notify=0`); rings are more forgiving — a reader on
an empty or writer-less ring emits silence, a writer free-runs
(drop-oldest) if the reader dies, and heartbeat plus stale-ring guards are
shipped. fan-in's per-lane reader mirrors the USB DIRECT precedent:
`Input.pcm` is already `Option<PCM>` and `None` on the direct lane, so U3
adds a third lane source beside `lane` and `direct` with the same
silent-idle, bounded-retry presence model and `/state` `source:"ring"`
labelling.

---

## Appendix B — campaign archaeology

> **Status: historical.** Frozen record of how the graph reached its
> 2026-08-10 state: the 2026-07-03 file-level audit that opened the
> consolidation campaign, the P3/P4 default-flip write-up, and the
> jts.local lab→product ring migration (completed). Preserved for
> primary-source archaeology — specific facts here drift. Current
> operational truth is everything **above** this heading.

### Audit provenance (main @ `c287ee13`, 2026-07-03)

The campaign opened with a direct file-inspection audit of
`deploy/alsa/asoundrc.jasper`, `deploy/modprobe.d/` + `modules-load.d/`,
renderer units + `renderers.sh`,
`rust/jasper-{fanin,outputd,usbsink-audio,ring,host-clock,resampler,clock}`,
`c/jts-ring-ioplug/`, `scripts/ring-proto/`,
`jasper/{fanin_coupling,audio_runtime_plan,output_topology,camilla_config_contract}.py`,
`jasper/fanin/`, `jasper/usbsink/`, `jasper/sound/`,
`jasper/multiroom/{reconcile,follower_config,active_leader_config}.py`,
`jasper/cli/doctor/{audio,renderers,aec}.py`,
`jasper/cli/{aec_tune,aec_bridge}.py`,
`deploy/lib/install/systemd-units.sh`, and `.env.example`.

It found three coexisting USB-ingress generations (a Python/PortAudio
pump, a Rust solo/aloop bridge with a catch-up sawtooth, and a lean-FIFO
lane), three CamillaDSP→outputd content bridges, five aloop renderer
playback lanes, and the snd-aloop workaround ecosystem around them. Its
load-bearing finding was that **the lean lane was unarmable on a
production box** — `output_mode="fifo"` existed only in the Python lab
bridge — so a cleanup that deleted the pump but kept the lean consumers
would have preserved a silent-audio trap. A1 and A3 were therefore
deleted together in the USB dead-pipeline sweep, and A2 followed on
2026-07-10.

### Ring geometry evidence for the default flip

The 40 ms-descent PoC
measured 35.4 ms tap→ref on the 2-slot / chunk-128 geometry, and the
2026-07-06 primed product-path run measured 54.3 ms tap→ref with
chunk 128 / target 128 / queuelimit 1. Reconstruction put the old 8-slot
deep-queue default at ≈90–95 ms end-to-end and the shipped geometry at
≈48.8 ms.

### P3/P4 default flip (landed 2026-07)

The default flip landed as one PR built on the #1169 fix batch.
`jasper.fanin.coupling_auto` holds the pure decision;
`reconcile_auto` (the `--auto` CLI mode) owns the env I/O, so the
reconciler stays the single env writer. It runs on deploy
(`resolve_fanin_coupling_default`) and at boot
(`jasper-fanin-coupling-auto.service`). On a full-profile, solo,
ring-eligible box the default coupling resolves to `shm_ring` behind the
wire-width gate, the install-profile gate, the manual-arm preflights
(assets, topology, period geometry, slot geometry), and two auto-only
gates — `ring_route_ready` (a grouped box resolves loopback so the boot
unit does not fail on a healthy leader/follower) and
`ring_topology_ready_strict` (an unreadable topology resolves loopback
instead of arm→rollback-churning every boot). Any gate failing resolves
loopback. `JASPER_FANIN_COUPLING_CHOICE=operator` freezes an operator's
revert, and `/state.audio_graph.coupling.choice` reports operator-vs-auto.
Every reconcile entry runs under one advisory flock
(`/run/jasper-fanin-coupling.lock`) so two concurrent passes cannot
interleave their ordered daemon transitions; contention aborts at ERROR
with exit 1 rather than reporting an unapplied change as applied.

The USB combo arms only on a box that both has the hardware-resolved
gadget capability and has USB Audio Input turned ON by the household
(canonical `/var/lib/jasper/source_intent.env`). When armed, the auto
pass is the single writer of the three fan-in USB keys; off a DIRECT box
they are written their explicit `disabled` values rather than unset,
because a stale `enabled` in `jasper.env` loads first and would otherwise
win. `DEFAULT_CUSHION_DECAY_FLOOR_FRAMES` ships the hardware-validated
576.

**Finding G resolved: Ring-A slot default is 2.** `DEFAULT_FANIN_RING_SLOTS = 2`
with the packaged `jts_ring_capture` conf.d block pinning `n_slots = 2`;
at `period_frames = 128` Ring A contributes ≈5.3 ms instead of the old
8-slot placeholder's ≈21.3 ms.

### jts.local lab→product ring migration (completed)

jts.local was ring-armed via the lab `ring-proto` tooling, which wrote
marked env blocks into the same `fanin.env` / `outputd.env` the coupling
reconciler owns, dropped `98-jts-ring*-proto.conf` files defining the
same PCM names as the shipped `60-jts-ring.conf`, and used 16 slots
against a hand-written CamillaDSP YAML. The migration tore both lab rings
down (`scripts/ring-proto/disarm.sh`, plus `--ring-a`), deployed the
product path, and re-armed through
`jasper-fanin-coupling-reconcile shm_ring`. jts.local's coupling is
reconciler-owned and coherent as of the 2026-08-10 probe.

---

Scope: the live plan, H1 through "Cross-program coordination" — egress
facts, source-half boundaries, ring v1 wire and header,
`ring_edge_width_ready` semantics, P-row inventory, doctor-check
locations, the dither inventory, and the DAC edge-format table were
re-read against `9cc41b987`. The ACTIVE-ring arm/rollback lifecycle
section was separately re-verified at `8f021e6ac` against the jts3 arm
attempt (`captures/r7b-jts3-arm-20260811T111338Z`): the waypoint,
step-2 verification, refusal, and coupling-pin paragraphs are from that
run plus the code, except the rollback step-2 window, which is a scratch
probe of `validate-outputd-env` and outputd's unit — not hardware. Fleet rows came from a same-day probe of
jts3 and jts.local only; jts4 and jts5 were not probed, and jts3's
wide-chain row is derived, as its cell says. Appendix A was carried
forward with only its ring/ioplug constants and `Input` shape
re-checked — its shairport offset and resync-threshold claims were
not. Appendix B was NOT re-verified and is retained as archaeology.
A same-day follow-up pass (also 2026-08-10) added the U0-complete
status roll-up (#2281/#2293/#2302, each gate 0/0), the ring v2
R1/R2/CI-hardening merge status and the new "Ring v2 design outcome"
section — transcribed from the panel's rulings record, not re-derived
— corrected the U1 exit-gate ordering to match the ratified
jts.local-before-jts3 resequencing, and fixed the two one-word nits
named in #2293's gate disposition. It did not re-touch anything else:
the egress/source-half facts, the fleet probe, and Appendix A/B stand
as last verified above. A third same-day pass (R4's fix round,
[#2310](https://github.com/jaspercurry/JTS/pull/2310)) re-verified the
ring-wire section against `rust/jasper-ring/src/layout.rs` and
`jasper/fanin_coupling.py` and rewrote it: R1 falsified its
"`validate_self` hard-rejects any other format / `S32LE` is Reserved"
claim, and the wire's narrowness now comes from the resolver, not the
layout. Nothing else in this pass. A fourth pass (2026-08-11) verified
the R-ladder's remaining PR states live via `gh` — R3
[#2308](https://github.com/jaspercurry/JTS/pull/2308), R4
[#2310](https://github.com/jaspercurry/JTS/pull/2310), R5a
[#2314](https://github.com/jaspercurry/JTS/pull/2314), and R5b
[#2320](https://github.com/jaspercurry/JTS/pull/2320) are all MERGED,
closing the R1–R5 ladder — and transcribed the R7 repanel's outcome
from the rulings record: 7 blockers / 14 SF across three lenses,
design revised to v2, consolidated errata E1–E6 ratified, panel
CONVERGED. It corrected the R7a/R7b "forced ordering" section for the
repanel's corpus-mode discovery (jts3 has no alignment artifact, so
the recommission step is dropped as inapplicable there — the
load-bearing case moves to jts.local's R6), updated the U1 exit gate
for the running jts3 hardware phase (30-minute soak windows, box-side
dead-man timer, both per owner direction on 2026-08-11), and confirmed
[#2319](https://github.com/jaspercurry/JTS/issues/2319) (camillagui
posture) as filed and open, alongside re-verified-current
[#2294](https://github.com/jaspercurry/JTS/issues/2294) /
[#2306](https://github.com/jaspercurry/JTS/issues/2306). It did not
re-touch the egress/source-half facts, the fleet probe, or Appendix
A/B.

A fifth pass (2026-08-11, the E7 ruling — this PR) rewrote two
paragraphs against the code it changed, and nothing else: the
wire-resolution section's "coherently narrow" claim (the resolver took
no per-box input at all until this PR; it now reads
`JASPER_FANIN_RING_WIRE_FORMAT`, the same key `jasper-fanin` parses),
and the lifecycle's step-1 paragraph (what the ring re-emit declares,
and which gate refuses a shear). Both are re-derived from the changed
source plus the jts3 arm-2 evidence
(`captures/r7b-jts3-arm2-20260811T132227Z`), not transcribed. Every
other section stands as last verified above.

Last verified: 2026-08-11
