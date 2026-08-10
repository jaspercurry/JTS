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
| USB Audio Input | fan-in DIRECT opens `hw:UAC2Gadget` at `S32`, then `s32_high_word_to_s16` (bare arithmetic `>>16`, no rounding, no dither) discards the low word before resample and mix — child [#2223](https://github.com/jaspercurry/JTS/issues/2223) |
| Provider TTS | 24 kHz mono S16 in; resampled through float, cast back to S16 before IPC; fan-in applies assistant gain at i16 (`apply_gain_i16`) |
| Generated earcons | rendered in float, then baked to 24 kHz mono S16 at daemon startup (`_to_pcm16` in `jasper/voice/earcons.py`) |
| fan-in core | accumulates into i32 scratch **at the S16 numeric scale**; `saturate_to_i16` at the summed write (`rust/jasper-fanin/src/mixer.rs`) |
| fan-in → CamillaDSP | `jasper_capture` is a 48 kHz stereo `S16_LE` dsnoop; CamillaDSP's capture side already requests `S32` (`DEFAULT_CAPTURE_FORMAT`), so ALSA widens an already-narrow signal and restores nothing |

Three consequences: a later `S32` container is not proof of a wide path;
narrowing before attenuation lifts the effective quantization floor
relative to quiet tails; and an S16 source gains nothing from promotion
but stops losing the precision that resample and gain create.

### Ring v1 is S16 on the wire, and that is a consumer problem

The wire is pinned S16 — `Geometry::validate_self`
(`rust/jasper-ring/src/layout.rs`) hard-rejects any other sample format,
and the C ioplug advertises a single-entry `formats[]` of
`SND_PCM_FORMAT_S16_LE` with `JTS_RING_CHANNELS = 2` and
`JTS_RING_RATE = 48000`. But **the layout is already wider than its
consumers**: the header self-describes rate, channels, and sample format,
and reserves `SAMPLE_FORMAT_S32LE = 2` ("Reserved for future wide/active
lanes"). Ring v2 is therefore a Rust-reader + C-ioplug + emitter problem,
not a layout redesign. Certified geometry is 2 slots × 128 frames per
ring, CamillaDSP chunk/target 128, `enable_rate_adjust: false`; Ring A
contributes ≈5.3 ms at 48 kHz.

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
`content_lane_format_for_coupling(COUPLING_SHM_RING)` must equal
`RING_WIRE_FORMAT` (`jasper/fanin/coupling_reconcile.py`). The planning
consequence: the box-wide `S32` default does **not** disarm rings. A
ring-armed box installs the coupling's own CamillaDSP kwargs, which force
both ring ends to the ring's wire format, so ring boxes stay coherently
S16 — keeping their certified latency — until ring v2 arms them wide.
Wide is the loopback path's property in the interim.

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
| **U0 — stabilize + replan** | this document; the P5c deletion (in flight in parallel); PR [#2281](https://github.com/jaspercurry/JTS/pull/2281) gate + merge; jts.local commission + route-latency revalidation (owner-gated) | P5c | doc merged; `rate_match` + adaptive-buffer + stale cushion prose gone; jts.local commissioned with a fresh artifact matching the certified distribution |
| **U1 — ring v2** | the R-RING2 design, build, and per-box activation | P8 | jts3 N-channel first (it kills the measured ~17.5-minute content-fill splice class); then jts.local width + recertification; then jts4 Zero-class validation. jts5 / bonded per the P8 scope ruling |
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
| P5c | Delete `rate_match` + adaptive-buffer + stale cushion recipes | **IN FLIGHT 2026-08-10** — its own PR, which also owns the `.env.example` and `HANDOFF-usb-low-latency.md` prose edits |
| P6a–d | Renderer lanes → ring ingress (librespot, bluealsa, correction, shairport LAST) | **OPEN — U3.** Net-new build: fan-in's `Input` is aloop-PCM-or-USB-DIRECT only, with no ring-reader variant |
| P7 | Re-point dsnoop consumers; drop the fan-in aloop mirror | **OPEN — U4** |
| P8 | Ring v2 | **OPEN — U1**, rescoped by R-RING2 to cover format and channels in one design |
| P9 | snd-aloop removal | **OPEN — U4**, hard-gated on P6 + P7 + P8 |
| P10 | Polish sweep + `audio-paths.md` rewrite | **OPEN — U4** |

Deletions stay separate PRs by repo guardrail.

## What still has to be deleted or built

The 2026-07-03 no-dupes audit is preserved in the appendix; this is what
survives it, verified at `9cc41b987`.

| Row | What survives, and where |
|---|---|
| **P5c** (in flight) | `rate_match` is fully live in `rust/jasper-outputd/src/content_bridge.rs` plus its `JASPER_OUTPUTD_CONTENT_BRIDGE_{RING,TARGET,MAX_ADJUST}_*` keys, and bleeds into `jasper-apply-airplay-mode`, `jasper-audio-hardware-reconcile`, the doctor, `audio_runtime_plan`, `fanin_coupling`, `multiroom/reconcile`, and `.env.example`. Adaptive buffer = `jasper/fanin/buffer_reconcile.py` + mux's `_settle_adaptive_buffer`. Stale 256 + 256 cushion prose sits in `.env.example` and `HANDOFF-usb-low-latency.md` |
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
`audio_backend_latency_offset_in_seconds` (rendered by `renderers.sh` from
the configured downstream buffers) MUST be re-derived for the ring graph,
because the ring holds frames the offset math attributed to the aloop
ring; and the 0.2 threshold may be revisitable afterwards, but only on
measurement — keep 0.2 through the migration. This is why AirPlay
migrates last.

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

Last verified: 2026-08-10 (scope: the live plan, H1 through "Cross-program
coordination" — egress facts, source-half boundaries, ring v1 wire and
header, `ring_edge_width_ready` semantics, P-row inventory, doctor-check
locations, the dither inventory, and the DAC edge-format table were
re-read against `9cc41b987`. Fleet rows came from a same-day probe of jts3
and jts.local only; jts4 and jts5 were not probed, and jts3's wide-chain
row is derived, as its cell says. Appendix A was carried forward with only
its ring/ioplug constants and `Input` shape re-checked — its shairport
offset and resync-threshold claims were not. Appendix B was NOT
re-verified and is retained as archaeology.)
