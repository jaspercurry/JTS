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

## Current position (egress + source half verified 2026-08-10 at `9cc41b987`; 2026-08-11 results dated inline)

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

### The source half is narrow, with one conditional exception

Every music lane enters at 48 kHz `S16_LE` except one: a USB DIRECT lane on a
box that is both wide-wired and USB-enabled. On every other lane the `S32`
container CamillaDSP captures does not describe surviving precision. The one
lane with a wide route built (USB DIRECT, since U2 PR-1) needs **two** things
to use it, and that pairing is the durable statement: the box's wire must
resolve `S32_LE` **and** its USB DIRECT lane must be on. Either alone leaves
the lane narrow. As
of 2026-08-11 jts3 is the only box that is both — armed wide, then USB-enabled
the same day (`captures/usb-enable-jts3-20260811T191749Z`). The exception
stopped being theoretical that same day: a 24-bit probe played from a Mac
survived jts3's chain end to end, measured at the fan-in lane, at Ring A, and
past CamillaDSP. That clears the width half of U2's exit gate; the U2 arc row
carries the numbers, the evidence, and what the arc still owes.

| Boundary | Position at 2026-08-10 (the USB row re-dated inline) |
|---|---|
| AirPlay | shairport-sync configured 44.1 kHz `S32`; its private aloop lane is a `plug:` wrapper pinned 48 kHz `S16_LE` |
| Spotify Connect | librespot requests `S24_3` with its own TPDF dither; the lane is pinned 48 kHz `S16_LE` |
| Bluetooth | bluealsa-aplay's negotiated PCM is adapted by the `plug:` lane to 48 kHz `S16_LE` |
| USB Audio Input | fan-in DIRECT opens `hw:UAC2Gadget` at `S32`. On a narrow wire — the shipped fleet default — `s32_high_word_to_s16` (bare arithmetic `>>16`, no rounding, no dither) still discards the low word before resample and mix. A wide route exists since U2 PR-1 ([#2223](https://github.com/jaspercurry/JTS/issues/2223)): when the box's wire resolves `S32_LE`, `push_capture_chunk` hands the resampler the gadget's `i32` untouched and it reaches the summed write intact. As of 2026-08-11 jts3 alone satisfies both conditions — wide wire and DIRECT lane on — and on that day the route carried a 24-bit probe end to end with its test step intact (numbers and evidence in the U2 arc row). It stays **dormant** on every narrow-wire box. Arming it is the per-box flip, not this row |
| Provider TTS | 24 kHz mono S16 in, resampled through float. Since U2 PR-2 the float survives to the IPC write and is quantized at the box's own wire scale: `AUDIO` (S16LE) on a narrow box — byte-identical to before — and `AUDIO32` (S32LE at the i32 spine scale) on a wide one. The verb IS the declaration, so no format is negotiated; both ends derive the width from the same CONJUNCTION of the box's two declared halves — `JASPER_FANIN_RING_WIRE_FORMAT=S32_LE` **and** the `shm_ring` coupling, since fan-in's aloop write is pinned narrow (`mixer::FORMAT`) however the box spelled its format — through one rule (`jasper_tts_protocol::TtsWireWidth::from_box_declaration`, which `Config::program_wire_is_wide` calls and `jasper.fanin_coupling.assistant_wire_is_wide` mirrors). fan-in's assistant gain is `apply_gain_i16` on a narrow sum and widen-then-`apply_gain`-in-f64 on a wide one, which is what stops a deep assistant attenuation from rounding the reply onto the S16 grid (`rust/jasper-fanin/src/tts.rs`) |
| Generated earcons | rendered in float, then baked once at daemon startup at the box's wire scale — `_to_pcm16` (24 kHz mono S16, byte-identical to before) or `_to_pcm32` (spine scale) in `jasper/voice/earcons.py`, both packing one shared normalization and tail fade. The wide bake's full scale is `32767 << 16`, not `i32::MAX`, so the two are the same sound at the same level |
| fan-in core | accumulates into an i64 scratch at the scale the box's wire names (`ProgramWidth`). Narrow — the shipped default — is the **S16 numeric scale** exactly as before, with `saturate_to_i16` at the summed write. Wide is the i32 spine scale, promoting each `i16` lane at its own sum entry (`rust/jasper-fanin/src/mixer.rs`). Since U2 PR-2 the program duck is width-dispatched too: narrow keeps its `f32` multiply and its (unreachable) `i32` clamp, wide computes in `f64` and keeps the `i64` headroom the duck exists to recover |
| fan-in → CamillaDSP | `jasper_capture` is a 48 kHz stereo `S16_LE` dsnoop; CamillaDSP's capture side already requests `S32` (`DEFAULT_CAPTURE_FORMAT`), so ALSA widens an already-narrow signal and restores nothing. **Narrow by declaration, not by driver** — the post-DSP content lane ROLE runs `S32_LE` on every box, on an snd-aloop substream pair (`hw:Loopback,0/1,6`) on the same card. Role-first on purpose: that pair also carries the mutually-exclusive bonded active-follower round-trip, which opens the raw device at `S16_LE`, so the bare device name proves nothing either way. Three places declare this lane's width and U2 PR-3 pinned them as one fact (`tests/test_aloop_program_lane_width.py`): fan-in's `mixer::FORMAT` (the writer, previously untested), the dsnoop slave, and the doctor's pin. There were four — `aec_tune`'s RAW dsnoop open could not absorb a move because dsnoop does not convert — and P7-2 retired it, so widening is cheaper by exactly that item. It is the last narrow hop on a `loopback` box and it is **P7/P9's to delete, not U2's to widen** — the per-box width capability is the ring's |

Three consequences: a later `S32` container is not proof of a wide path;
narrowing before attenuation lifts the effective quantization floor
relative to quiet tails; and an S16 source gains nothing from promotion
but stops losing the precision that resample and gain create.

### The ring wire is S16 unless a box declares otherwise

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
`JASPER_OUTPUTD_CONTENT_FORMAT` — so a ring-armed box is coherent at the wire it
resolves, whatever its program-lane default is (see
[Where this corrects #2285](#where-this-corrects-2285)). Because the accept-set
is wider than the wire, the attach can no longer
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
**As of 2026-08-11 exactly one box has said otherwise: jts3 declares
`JASPER_FANIN_RING_WIRE_FORMAT=S32_LE` and is armed wide** (see the Fleet table
and the arm/rollback lifecycle below for the procedure); every other box leaves
the key unset and is therefore narrow, byte-identically to before the mechanism
existed.
Until 2026-08-11 the Python resolver took no input at all and pinned the wire
narrow by policy, so such a box could not arm coherently in either direction —
found when jts3's arm halted on the format shear
(`captures/r7b-jts3-arm2-20260811T132227Z`). Arming wide also needs the
installer's ioplug provenance record, because a non-default wire renders a
conf.d `format` key an older `.so` cannot parse (`ring_wire_caps_ready`).

So ring v2 was never a layout redesign: it is a transport + reader/writer +
emitter + resolver problem. The transport layer (R1's crate, R2's ioplug),
both daemons (R3 fan-in, R4 outputd), and the resolver (R5a/R5b) are all
merged — the R1–R5 ladder is complete, and as of 2026-08-11 so are R7a and
R7b, with jts3 armed (the R-ladder table below carries each rung's state).
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
both ring ends to the ring's **resolved** wire — so a ring box is coherent
at whatever wire it resolves, and the fleet default resolves narrow, which
is how ring boxes held their certified S16 latency across the whole
R-ladder. Ring v2 changed which wire a box may resolve, not that mechanism:
since 2026-08-11 jts3 resolves `S32_LE` and both its ring ends follow
through the same kwargs path (E7 above).

#2285's banner is corrected: an earlier draft said the plan owns "U0–U9
sequencing", but the sequencing this plan actually ships is **U0–U4**
(see "Sequencing — the U arcs" below). The banner was fixed to U0–U4 on
2026-08-10, when this plan merged as canonical via PR
[#2293](https://github.com/jaspercurry/JTS/pull/2293).

### Fleet

| Box | Hardware / role | State (dated) |
|---|---|---|
| **jts3** | Pi 5 + DAC8x + XVF3800, active 2-way, the horn | **ARMED WIDE 2026-08-11** — the campaign's first wide ring box. Installed `7afec75d8` (armed at `5dcd872a4`, then **proven to survive a deploy and two `/sound/` saves** the same day — see below); coupling `shm_ring` + `shm_ring` with `choice=operator`; wire `S32_LE` on both ends; `JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT=1` against `active-content.ring`. DAC8x `LatencyFloor` live (outputd period/DAC buffer 128/256; DAC presentation **5.167 ms**), ring runtime chunk/target 128/128. `jasper-doctor` exit 0 with no failures; the arm-signature plan warn is expected and permanent. `aec-init` runs in **CORPUS mode** — no alignment artifact, so no commissioned identity ([#2254](https://github.com/jaspercurry/JTS/issues/2254); corpus exit is an owner decision, out of campaign scope). **The arm changed how the box sounds**, and that is its largest user-visible consequence: the re-emit published jts3's own adopted-but-never-published profile for the first time (treble −5.536 dB, crossover Fc 2000 → 1649 Hz, broadband within 0.24 dB), because the live graph had been ~22 h staler than the profile the box had already adopted. Not a ring artifact and not a volume bug — measurements and gain structures in `captures/endpoint-deploy-jts3-20260811T185255Z` (files 21, 24). [#2291](https://github.com/jaspercurry/JTS/issues/2291) owns the voicing question and identifies that profile as its 2026-08-10 incident-era artifact (the −13 dB tweeter trim being defect-downstream); the owner reviewed the change on 2026-08-11 and elected to **keep the current voicing** (recorded in [#2348](https://github.com/jaspercurry/JTS/issues/2348), which is scoped around that decision). USB Audio Input was enabled here later the same day, with the arm preserved (`captures/usb-enable-jts3-20260811T191749Z`), and the arm then survived an **unclean power pull** — EXT4 orphan-cleanup fingerprint on the way back, and every armed-state field byte-identical across the cycle (post-cycle readback in `captures/usb-hires-jts3-20260811T194132Z`, against the pre-cycle close-out in the enable capture), which is a stronger result than a graceful reboot would have given. The box had been up **21 days** when power was cut, and that figure's raw reading is in the enable capture, not the hi-res one: `captures/usb-enable-jts3-20260811T191749Z/00-pin-and-build.txt` line 11, `uptime: up 3 weeks, 7 hours, 5 minutes`, read about 20 minutes before the pull. The hi-res capture cannot show it — its persistent-journal boot list starts where the journal's retained window does, five days back. Arm evidence `captures/r7b-jts3-arm3-20260811T162742Z` |
| **jts.local** | Pi + single Apple dongle (card `A`), passive, USB-in box | Narrow, unchanged, **re-validated 2026-08-11**. Installed `5dcd872a4`; coupling `shm_ring` + `shm_ring`; combo ARMED; the ring header stayed byte-identical `S16_LE` across the deploy and doctor was check-for-check identical pre/post, with ~5 min of real USB DIRECT playback at zero xruns / clips / drops (`captures/u2-jtslocal-regression-20260811T162805Z`). Still owed and unchanged by that pass: voice INACTIVE (parked; **commission owed**), the live dongle edge unconfirmed against the registry's `S24_3LE`, and the **route-latency revalidation artifact** (stale and failing both before and after) |
| **jts4** | Zero 2 W streambox + InnoMaker, loopback | Not probed since 2026-08-10. Zero-class validation target for U1 |
| **jts5** | InnoMaker HAT, S32 edge live, composite/active testbox | Not probed since 2026-08-10. **Parked by owner intent — do not commission or play it for tests** |

**What is and is not safe on jts3 while it is armed** (2026-08-11). Deploys and
`/eq/` / `/sound/` saves are **safe**, and the interim restriction on them is
**lifted as of 2026-08-11**. PR
[#2343](https://github.com/jaspercurry/JTS/pull/2343) put every re-emit seam on
the live transport endpoint, and both halves were then proven on the armed box
the same day: one full deploy and two household-path `/sound/apply` saves each
left every armed-state field unchanged, both graph halves still naming the ring
at `S32_LE`, `writer_alive` true throughout
(`captures/endpoint-deploy-jts3-20260811T185255Z`). The #2339 seam fired exactly
as it had at arm3 and the outcome inverted — the statefile pointer still moved,
but to a graph whose sha256 is identical on both sides. An EQ save costs no
writer epoch: the apply is a live websocket load, not a CamillaDSP restart.
[#2337](https://github.com/jaspercurry/JTS/issues/2337) and
[#2339](https://github.com/jaspercurry/JTS/issues/2339) are closed against that
evidence.

The measurement/commissioning **wizard** flows are a separate case and **stay
off the armed box** — [#2344](https://github.com/jaspercurry/JTS/issues/2344),
addressed by PR [#2363](https://github.com/jaspercurry/JTS/pull/2363) (up
2026-08-12, not merged). The restriction is unchanged; what changed is that it is
now **enforced in code rather than only written here**, and that the mechanism
turned out to be two different defects rather than one:

- The **applied-summed measurement graph** re-emits the applied snapshot and
  loads it to play the excitation, so it inherited the snd-aloop lane the way
  the deploy and household seams did. It now reads the same
  `resolve_live_active_endpoint` derivation as the rest of that family.
- The **per-driver / summed commissioning graph** fails the other way round: it
  already resolved the ring by *name* through the fresh-emit chooser, but
  forwarded none of the rest of `active_emit_devices`, emitting a ring sink over
  a `plug:jasper_capture` source — the tap fan-in stops feeding under
  `shm_ring`. It now **refuses before it emits**, naming the release command,
  because teaching that emitter the ring is a hardware claim and its
  live-protection admission report asserts the tap capture route.

**No COMMISSIONING/WIZARD sweep has been run through the armed ring**, so the
first bullet is code-correct and hardware-unvalidated, and the restriction stands
until an on-device wizard sweep passes. A MEASURE-lane sweep is a different
claim and has already been made: the arm3 finale's `correction_substream` sweep
traverses the armed ring, which is what proved the lane clean.

Certified USB route-latency baseline to compare against: **p50 36.35 /
p95 37.93 / p99 38.29 ms** over 1094 impulses / 32.6 minutes. The quick
and promotion thresholds (p95 ≤ 40 ms; p99 ≤ 42 ms over ≥1000 jittered
impulses / ≥30 minutes) are owned by
[HANDOFF-usb-low-latency.md](HANDOFF-usb-low-latency.md) and
[testing-tooling.md](testing-tooling.md); they are named here only
because R-RING2 keys on them.

**Where R-RING2's route-latency gate stands, per box** (2026-08-11) — so an
executor can tell met from waived from inapplicable at a glance:

- **jts3 — INAPPLICABLE, not waived.** Its `route_profile` is `corrected_48k`,
  which makes no low-latency claim, so there is no certified distribution for a
  fresh artifact to be compared against. The doctor states it in those terms:
  `route latency evidence  route_profile=corrected_48k has no low-latency claim`
  (`captures/r7b-jts3-arm3-20260811T162742Z`, file 33). Arming it therefore did
  not skip the gate.
- **jts.local — STILL OWED.** It carries the USB low-latency route the baseline
  above certifies, so R6 needs the fresh artifact. The 2026-08-11 pass confirmed
  the existing one is stale and failing, before and after (unchanged by that
  deploy).
- **jts4 / jts5 — not reached.**

---

## Ratified decisions (R-rows owner-ratified 2026-08-10, later rulings dated inline — executors do not reopen)

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
— not merely on clearing the 40/42 ms ceilings. The gate binds a box that
*makes* a low-latency claim; where a box's `route_profile` carries none it is
inapplicable rather than waived (see the per-box status under the Fleet table).
[#2147](https://github.com/jaspercurry/JTS/issues/2147) (slot size derived
from the DAC floor) is **out of ring-v2 activation scope** — a compatible
follow-up only. Renderer migrations (P6) land **once**, on v2, never onto
ring v1.

**E7 — arm width follows R-WIDTH** (architect ruling, 2026-08-11). A box whose
live post-DSP path is already wide arms its ring **wide**; arming it at the
resolver's narrow default would insert a second quantization inside the
one-reduction invariant. Rationale and the arm that forced it are in
`captures/PLAN-ring-v2-rulings-2026-08-10.md` (search `E7`) — do not re-derive
here; the mechanism is the wire-resolution section above.

**R-POPS — parked, with a resurrection trigger.** The 2026-08-08 owner
report of new very-high-frequency pops after the flip is parked: the owner
re-tested 2026-08-10 and could not reproduce it, and the suspected cause is
transient config-load state from the measurement flow. **Trigger: any
recurrence under normal playback makes this a diagnose-first workstream
that precedes further pipeline flips.** Recorded so it is not lost.

**R-NOSTALE — an unconsumed capability is deleted, not parked** (owner ruling,
2026-08-14). This arc is **forward-only**: a shipped capability whose producer
half nothing ever wired is removed outright rather than left dormant "in case
v2 wants it". Dormant capability is not free — it is parsed, published, tested,
documented, and swept on every prose pass, and it reads to the next session as a
seam that exists. First application, ruled the same day: **`JASPER_FANIN_MUSIC_OUTPUT_PCM`
is DELETED** (PR [#2483](https://github.com/jaspercurry/JTS/pull/2483)) —
fan-in's optional second, pre-TTS "music-only" output PCM (multi-room
Increment 1). Its READ path was live (`Config::from_env`, `Mixer::new`'s open,
the per-period `write_music_only`, the `music_output` STATUS block) and **no
writer anywhere set the env var** — not `install.sh`, not a unit, not the
grouping reconciler, not a wizard — for the ~2 months it shipped, so removing it
changes the behaviour of no role. Nor was it load-bearing for the split it was
built for: on the passive bonded pair, Increment 5 PR-2 routes the leader's TTS
to `jasper-outputd`, which leaves fan-in's one output carrying no assistant, and
the roles that deliberately keep TTS in fan-in (active endpoint, non-parked sub)
never used this tap either. A second spelling of a guarantee the live path
already provides once — the single-source-of-truth value, applied to a
capability rather than to a value. Rebuild deliberately
against the then-current topology if multi-room v2 wants a fan-in tap; do not
revive this one. Full removed-code record and rebuild guidance:
[HANDOFF-multiroom.md](HANDOFF-multiroom.md) §0 (Increment 1).

---

## Sequencing — the U arcs

The U arcs are the top-level order. They group the P-rows (kept for
continuity — issues and PRs reference them) and the width workstream.

| Arc | Delivers | Rolls up | Exit gate |
|---|---|---|---|
| **U0 — stabilize + replan** — **COMPLETE** | this document (PR [#2293](https://github.com/jaspercurry/JTS/pull/2293), merged, gate 0/0 after fix round); the P5c deletion (PR [#2302](https://github.com/jaspercurry/JTS/pull/2302), merged, gate 0/0 after fix round); PR [#2281](https://github.com/jaspercurry/JTS/pull/2281), merged, gate 0/0 after delta | P5c | doc merged; `rate_match` + adaptive-buffer + stale cushion prose gone. jts.local commission + route-latency revalidation no longer gates U0 — it now proceeds under U1's R6 rung below, since ring v2 width activation needs exactly that fresh artifact and a bare pre-ring-v2 commission would be thrown away |
| **U1 — ring v2** | the R-RING2 design, build, and per-box activation (design ratified 2026-08-10 — see [Ring v2 design outcome](#ring-v2-design-outcome-u1) below) | P8 | R1–R5 ladder complete, R7a and R7b merged, and **jts3 ARMED WIDE 2026-08-11** — the campaign's first wide ring box, delivered at the third arm attempt (per-rung state in [Ring v2 design outcome](#ring-v2-design-outcome-u1)). The measured ~17.5-minute content-fill splice class is **dead on jts3** — structurally, because the aloop content hop that produced it is gone. The first armed window **corroborates that rather than proving it**: 755 s is about 72% of one ~1050 s fill period, so clean counters across it are consistent with the phenomenon, not a refutation of it. What the window did record: ring `frames_read` +36.2 M dead-flat at 47986–48070 frames/s, `empty_reads` 0 in every sample, zero DAC xruns, zero clipped samples, and a clean MEASURE-lane sweep across the armed ring (`captures/r7b-jts3-arm3-20260811T162742Z`). The standing watch is what makes this more than one sub-period observation, and it is a **conjunction, not a single counter**: `empty_reads` rising while `epoch_resets` stays **flat** is a drain/splice; **both** rising is a restart artifact. `epoch_resets` is the discriminator, not the alarm — a writer reattach's own drain gap also lands in `empty_reads`, because the reader's first-filled-slot latch is never reset for the life of the reader, so a bare `empty_reads` alarm would fire on every deploy. The same capture's durability test is the worked benign case: a deliberate CamillaDSP restart took `empty_reads` 0 → 24 with `epoch_resets` 1 → 2 (file 30). The erratum and its refinement are both on the record (`captures/PLAN-ring-v2-rulings-2026-08-10.md`). Remaining: jts.local's R6 activation, separately owner-gated (home box, not yet granted); then jts4 Zero-class validation; jts5 / bonded per the P8 scope ruling |
| **U2 — source width** | #2223's 3-PR ladder plus its Step 0 descriptor check; the TTS/earcon tail | width workstream | Step 0 is **fully answered as of 2026-08-11**: the USB gadget was already wide at the wire, the only narrowing on that lane was one fan-in call (`s32_high_word_to_s16`), and a real host does deliver the low bits — macOS applied no CoreAudio truncation. **PR-1 merged 2026-08-11** (PR [#2330](https://github.com/jaspercurry/JTS/pull/2330); three review rounds — 5 SF + 8 nits, then one convergent blocker the fix round itself introduced, both closing deltas 0/0), so the wide DIRECT lane ships **dormant on every narrow-wire box** and correct on a wide one; jts3 is the first live wide box, so "dormant everywhere" now reads "dormant on all narrow-wire boxes". jts.local took the same build and had its narrow path re-validated — ring header byte-identical, doctor check-for-check identical (`captures/u2-jtslocal-regression-20260811T162805Z`, 2026-08-11). **PR-2** (TTS / earcon / gain tail) carries the assistant half: the assistant IPC wire gains a second, self-describing payload verb (`AUDIO32`, S32LE at spine scale) that a box speaks only when BOTH halves of its declaration are wide — `JASPER_FANIN_RING_WIRE_FORMAT=S32_LE` **and** the `shm_ring` coupling, since fan-in's aloop write is pinned narrow (`mixer::FORMAT`) however the box spelled its format. One rule decides that (`TtsWireWidth::from_box_declaration`, which `Config::program_wire_is_wide` calls and `assistant_wire_is_wide` mirrors), and a coupling flip that changes the verdict `try-restart`s `jasper-voice` so the two ends cannot sit disagreeing; earcons bake at that same scale; fan-in's assistant gain moves to widen-then-gain-in-f64 on a wide sum; and PR-1's deferred correctness-n5 closes — the program duck's `i32` rails and `f32` mantissa become width-dispatched, so a spine-scale sum is no longer clamped at the `i32` rail before the duck can recover it. Every narrow-wire byte is unchanged, pinned end to end against goldens captured by running `origin/main`'s own tree (`tests/test_tts_wire_width.py`, `tests/test_earcons_wire_width.py`, and the narrow golden in `rust/jasper-fanin/src/tts.rs`). **This is NOT dormant on jts3, and that is the one box it is not dormant on**: jts3 declares `S32_LE` and is coupled `shm_ring`, which is exactly the conjunction the assistant width requires, so the deploy that carries PR-2 there activates the `AUDIO32` verb, the wide earcon bake, the f64 assistant gain, and the reworked program duck in the same step — no separate flip, no PR-3 gate. Every other box is narrow on the coupling half alone and takes the byte-identical path. The jts3 deploy therefore carries a listen check in the regime the change actually touches — assistant speech over hot program material, where the duck's rails moved — plus a `clipped_samples` watch across that window; forward-only, so the exposure is stated rather than deferred. **PR-3 closes the ladder, and it closes it by NOT building what its name suggested.** The per-box program-lane width capability already exists and is complete: `JASPER_FANIN_RING_WIRE_FORMAT` plus the `shm_ring` coupling, resolved by `resolve_ring_wire` and read by every declaring end, with PR-1 and PR-2 widening the source and assistant halves into it. A SECOND width mechanism on the snd-aloop `loopback` transport was scoped and declined on evidence: it would have to width-dispatch fan-in's realtime output write, teach the asound.conf render path a per-box format it does not carry (its whole dynamic surface is three placeholders — two DAC identity, one ALSA rate converter, none of them a lane format), break `jasper/cli/aec_tune.py`'s RAW dsnoop open (dsnoop does not convert, unlike the `plug:` wrapper every other consumer uses), and reopen PR-2's panel-ratified conjunction — with **no box to validate it on**: jts3 and jts.local are both `shm_ring`, jts4 is floorless and unprobed, jts5 is owner-parked. So the aloop lane stays narrow and its width becomes ONE pinned fact across its three declarers (`tests/test_aloop_program_lane_width.py`) — four when U2 PR-3 pinned them, until P7-2 retired `aec_tune`'s RAW open; fan-in's `mixer::FORMAT`, the writer the other two follow, had no test of any kind in either language before this. What PR-3 DOES build is the flip's missing precondition, banked by PR-2's resilience lens: a fan-in binary predating `AUDIO32` does not mis-read it — `read_command` returns `unknown TTS command`, the connection task logs `event=fanin.tts_socket.protocol_error` and drops the stream — and because cues share that socket the box goes **fully silent including the failure cue**. That is reproduced in-tree against a rejecting peer, with a narrow control proving the peer works, and the two orderings that make a stale peer unreachable are asserted instead of inherited: `After=jasper-fanin.service` at boot, and the installer parking `jasper-voice` (first entry of the canonical park set) before it restarts fan-in, checked per shell function so a new restart site cannot hide behind an unrelated earlier park. A capability handshake was considered and declined — R-WIDTH forbids cross-daemon negotiation, both orderings already close the window, and a fan-in STATUS read at voice startup would add a runtime dependency whose own failure mode silently narrows the fleet's one wide box. **Zero boxes change behaviour and no declaration moves.** With it the ladder is complete and the arc is exitable on merge; what was already settled is the gate's **width half — met on hardware 2026-08-11**. Two instruments carry that half and they are not interchangeable. The **standing** one is the bit-pattern fixture the gate named, in-tree since PR-1 and re-run by the Rust test lane: `U2_HIRES_VECTORS` (a 24-bit sample plus both 24-bit rails) drives `a_hi_res_direct_lane_keeps_its_low_bits_all_the_way_to_the_wide_payload` through the real sum entry and payload fill, asserting the exact bits each route keeps — wide publishes the sample bit for bit, narrow keeps the high word and nothing below it (`rust/jasper-fanin/src/mixer.rs`; the lane-level pattern is in `lane_resampler.rs`). The dated hardware probe **corroborates that on a real box; it does not replace the regression pin**: a 24-bit −30 / −110 / −30 dBFS probe played from the owner's Mac reproduced its 80 dB step to within **0.03 dB** at all three taps — the fan-in lane gauge, the Ring A capture, and post-DSP playback, where the probe still resolved at −125.09 dBFS on the far side of CamillaDSP's volume, correction, and active crossover. The probe tone read −107.21 dBFS (sd 0.15), ~11 dB below the 16-bit floor, with `epoch_resets` constant, `empty_reads` 0, and zero xruns or clipped samples across the window (`captures/usb-hires-jts3-20260811T194132Z`). Per-box flips stay the per-box acts they were, and a `loopback` box's route to wide is the ring's — arm, then declare, the ladder jts3 walked — not a second mechanism on the lane P7/P9 delete |
| **U3 — renderer ring ingress** | P6a–d, one lane at a time, **AirPlay LAST** with offset re-derivation. **ARC COMPLETE 2026-08-13: all four lanes are on rings** — librespot (P6a [#2389](https://github.com/jaspercurry/JTS/pull/2389)), bluealsa (P6b [#2393](https://github.com/jaspercurry/JTS/pull/2393)), correction (P6c, five PRs — see the P-row), airplay (P6d [#2409](https://github.com/jaspercurry/JTS/pull/2409)) — all default-empty | P6 | per-lane source pass (all OWED — no box armed); AirPlay adds a Music.app local-track loop + resync-log watch + bonded A/V spot-check |
| ↳ **P6a (Spotify) — MERGED 2026-08-13** ([#2389](https://github.com/jaspercurry/JTS/pull/2389), three review rounds, all lenses 0/0) | Built the seam the other three lanes re-point through: fan-in's third `Input` variant with the DIRECT lane's silent-idle/bounded-retry presence model, `source:"ring"` + a `ring{}` block in `/state`, the librespot ring PCM in conf.d, and the lane map (`jasper/renderer_lanes.py`) as the single writer of both ends of a flip. **Ships DEFAULT-EMPTY**: no box is armed, and every unarmed box is byte-identical. Two things it added beyond the Appendix-A sketch, each argued in the PR body: a dedicated `jts-ring` group + `UMask=0007` on the renderer unit (the setgid bit fixes a new ring file's group, only the umask fixes its mode, and both ends write the ring header), and the ioplug's **writer-side SPSC guard** — without it the doctor's own `aplay -D` probe would open a second writer and interleave slots into live music instead of taking EBUSY as it does on an aloop lane. **What it does NOT have: the on-box source pass.** No armed box exists yet, so "Spotify plays through the ring" is unproven on hardware and rides a coordinated jts3 window (the box is shared with [#2291](https://github.com/jaspercurry/JTS/issues/2291)) | | |
| **U4 — delete** | P7 dsnoop re-points, P9 snd-aloop removal after fleet burn-in, P10 polish + `audio-paths.md` rewrite | P7, P9, P10 | full-fleet deploy + doctor + every-source pass; reboot test per box |

**Parallel width tail**, independent of the arcs:
[#2255](https://github.com/jaspercurry/JTS/issues/2255) (bounded
per-child composite xrun recovery — the safety prerequisite) →
[#2257](https://github.com/jaspercurry/JTS/issues/2257) (packed-24
composite edge).

### P-row status

| P | Row | Status (2026-08-10; ring rows re-dated inline) |
|---|---|---|
| P0 | fan-in host-compliance persistence | **DONE** — `rust/jasper-fanin/src/host_compliance.rs` |
| P1 | Ring platform ship (inert) | **DONE** — `deploy/lib/install/ring-platform.sh`, `deploy/alsa/conf.d/60-jts-ring.conf`, `deploy/tmpfiles/jts-ring.conf` |
| P2 | Ring citizenship | **DONE** — emitters, `coupling_reconcile` `shm_ring` mode, topology contract, statefile seeding, artifact binder, `/state`, doctor |
| P3 | USB combo default-on where the gadget is present | **DONE** |
| P4 | Rings default on validated full-profile solo-stereo boxes | **DONE** — jts.local armed; the `--auto` pass still resolves `loopback` for a roleful box like jts3 and always will (roleful is excluded from auto arming by design); jts3's ring is **operator**-armed instead, since 2026-08-11. jts4 / jts5 excluded by topology and profile, not hostname |
| P5a | Delete Python usbsink pump + lean-FIFO lane + Rust solo aloop mode | **DONE** |
| P5b | Delete `transport_pipe` | **DONE** (2026-07-11) |
| P5c | Delete `rate_match` + adaptive-buffer + stale cushion recipes | **DONE** — PR [#2302](https://github.com/jaspercurry/JTS/pull/2302), merged 2026-08-10, gate 0/0 after fix round. Owned the `.env.example` and `HANDOFF-usb-low-latency.md` prose edits |
| P6a–d | Renderer lanes → ring ingress (librespot, bluealsa, correction, shairport LAST) | **P6a MERGED 2026-08-13** (PR [#2389](https://github.com/jaspercurry/JTS/pull/2389), `b07c98c13`; three review rounds, all lenses 0/0) — the seam build: fan-in's third lane source (`ring`, `rust/jasper-fanin/src/mixer/ring_capture.rs`), the librespot ring PCM + `plug:` wrapper in `deploy/alsa/conf.d/61-jts-renderer-lanes.conf`, and the activation rule + its single writer in `jasper/renderer_lanes.py`. **P6b MERGED 2026-08-13** (PR [#2393](https://github.com/jaspercurry/JTS/pull/2393), panel 0/0) — the bluealsa lane: one registry row plus the three generalizations a second lane forced (the label is `bluealsa`, fan-in's vocabulary, not the mux source name `bluetooth`; `renderer_user_in_ring_group` learned ROOT is capable without group membership; the doctor's ring-device map became DERIVED from the registry), with the Rust side needing no edit at all. **P6c COMPLETE 2026-08-13, five PRs, each panel/gate 0/0**: [#2395](https://github.com/jaspercurry/JTS/pull/2395) (P6c-0 — one owner for the correction-lane `aplay` spawn: ten inline argv sites onto one builder + three thin wrappers in `jasper.audio_measurement.correction_lane`, with a conventions guard against an eleventh); [#2397](https://github.com/jaspercurry/JTS/pull/2397) (fix — P6a's unarmed doctor branch returned a novel `"skip"` status that `render()` failed, exit 1 on every unarmed box; caught on `main` before any deploy shipped it); [#2398](https://github.com/jaspercurry/JTS/pull/2398) (P6c-i — `CORRECTION_PLAY_UMASK` rides every spawn via subprocess's `umask=`, jasper-web joins `jts-ring`, and the unitless-lane pin machinery built one PR ahead of the row that needs it); [#2402](https://github.com/jaspercurry/JTS/pull/2402) (P6c-ii — the `correction` row, the campaign's first UNITLESS lane: ephemeral aplay writers, `correction_play_device()` as the one call-time transport reader with the full device-fact sweep behind it, the doctor's never-fed WARN learning on-demand lanes, and armed payload-equals-spawn pins closing the imported-constant blind spot the panel's empirical sub-agent found). **DEFAULT-EMPTY throughout: no box is armed by any of these**, and every on-box source pass is OWED (see the U3 arc row) — P6c-ii additionally owes the readerless-capture rejection proof (its exit-0 residual's safety rests on the capture-quality SNR floors). **P6d MERGED 2026-08-13** (PR [#2409](https://github.com/jaspercurry/JTS/pull/2409), panel 0/0 + two late sub-agent rounds) — the airplay lane, the third delivery shape: `jasper-apply-airplay-mode` (the per-start conf renderer, single writer) reads the lane map's resolved `JASPER_SHAIRPORT_DEVICE` line, so the flip lands on the unit's next start and the Tier-3 wedge recovery re-renders from the map for free; `conf_renderer` + `arm_advisory` registry data fields; shairport-sync joins `jts-ring` on both installer paths with `UMask=0007`; the doctor judges the rendered conf against the lane map in both disagreement directions; offset provenance annotated (formula transport-independent in kind, tuned values empirically validated on aloop — re-derivation rides the per-box source pass, surfaced by the arm advisory). Its S3 survive-probe also parametrized the conf.d plug-wire pin over all four lanes, retroactively closing a three-PR coverage gap. **P6a–d COMPLETE — every renderer lane is on the ring transport, default-empty** |
| P7 | Re-point dsnoop consumers; drop the fan-in aloop mirror | **P7-1 (AEC bridge ALSA reference) — PR [#2415](https://github.com/jaspercurry/JTS/pull/2415) MERGED 2026-08-13**, the arc's first *consumer* retirement: `JASPER_AEC_REF_SOURCE=alsa` pointed `jasper-aec-bridge` at the summed aloop tap through `REF_DEVICE = "jasper_ref"`, and outputd's UDP speaker monitor has been the production default for long enough that the fallback is dead weight standing in front of P9's deletion. Consumers before mirror: the reader goes, the PCM definition does not (P9-B). Three halves land together — the bridge loses `REF_DEVICE`, `_ref_thread`, and its `alsaaudio` import (the durable L+R-sum / ref-gain rationale moves onto `_ReferenceFrameConverter`, which owns that DSP for the surviving transport); `jasper-aec-reconcile`, the single writer of the env var, stops publishing the retired spelling from its two **parked** branches (`bridge_running` of `"reference"` and `"0"`, both of which stop the bridge before returning — `park_managed_xvf` and `stop_disable_aec` respectively — so the value they wrote was a resting one, and the `"reference"` branch's `alsa` was in fact *self-contradictory* against that same file's `JASPER_AEC_CHIP_AEC_ENABLED=1`, a pairing the bridge itself rejected); and doctor stops offering `pcm.jasper_capture` / `jasper_ref` remediation for a producer that cannot exist, falling through to its existing source-neutral text. **The retired value is converged, not rejected** — a box parked by a pre-P7-1 reconciler still carries `alsa` on disk, and refusing to start would leave `jasper-voice` bound to a UDP mic nobody feeds, so the bridge warns (`event=aec_ref_source_retired`, naming the reconcile command) and uses `outputd_udp`; a genuinely unknown value stays the hard failure it always was. No install migration: the reconciler is the only writer of the key (into `/etc/jasper/jasper.env`, not the wizard's `aec_mode.env`) and rewrites it on every run, and `install.sh` enables and runs the reconciler unconditionally, so a deploy converges it without a second writer touching a key it does not own. A stale value therefore cannot survive from any reconciler-written state; the panel's resilience lens constructed the one non-reconciler path that reaches the bridge with it (a custom-profile box restarted manually on a stale env), which converges with the WARN rather than failing. Pinned by `tests/test_aec_ref_source_retirement.py` — an AST-over-values scan (docstrings excluded, so prose about the retirement neither satisfies nor trips the guard), the converge/reject pair, an ordering assertion that resolution precedes the stats snapshot doctor reads as provenance, a writer-side pin that `write_leg_env` assigns exactly one source, and — added in the panel's fix round — the first pin on the *surviving* chip-AEC precondition (`JASPER_AEC_CHIP_AEC_ENABLED=1` requires `JASPER_OUTPUTD_CHIP_REF_PCM`), which retiring the ALSA source left as the only guard between chip-AEC and a bridge forwarding the chip beam with nothing feeding the chip's USB-IN reference; the hearing-safety lens mutation-proved nothing caught its deletion. All nine mutation-verified against a green control, including a warns-but-does-not-return variant and a positive control that fails if the guard rejects a configured producer. **P7-2 (the tuner's reference leg) — PR [#2444](https://github.com/jaspercurry/JTS/pull/2444) MERGED 2026-08-14** (`882c173ae`; `2026-08-14T01:03Z` — this row said 08-13 until P7-5's marker re-read. The convention is UTC, derived rather than assumed from an entry whose UTC and owner-local days differ: P6a's "MERGED 2026-08-13" is #2389's `2026-08-13T01:21Z`, committed `2026-08-12 21:21 -0400`). The second *consumer* retirement, and the one that had to land before P7-4: `jasper-aec-tune`'s PASSIVE leg spawned `arecord -q -D jasper_capture` against the raw dsnoop, so on a ring-coupled box it was reading the lossy lane-7 mirror P7-4 deletes — merging it after P7-4 would have left the tool silently measuring against a silenced tap. It reads jasper-outputd's final speaker monitor now (`JASPER_OUTPUTD_REFERENCE_UDP_TARGET`, shipped default `127.0.0.1:9891`), the same stream the bridge binds, resolved through `merged_env_files()` so the reconciler's EMPTY value on its parked branches is diagnosed as *outputd is not publishing* rather than waited out. Wire format re-derived from the producer rather than assumed: headerless little-endian interleaved stereo int16, one narrowed playout period per datagram, at outputd's core-fixed 48 kHz — pinned Rust-side by `the_reference_datagram_is_exactly_one_s16_stereo_period`. Reference capture is a bound socket drained on a thread, bound BEFORE the mic `arecord` starts so its window brackets the mic's and so a port conflict is reported before anything is disturbed; the tool already stopped `jasper-aec-bridge`, which is what frees the port. **The measured number shifts and nothing persisted does.** The old tap sat upstream of CamillaDSP, so its lag carried a CamillaDSP-plus-outputd offset that was never part of `AUDIO_MGR_SYS_DELAY`; the new one is co-located, at outputd's publish point, with the chip's own USB-IN reference — outputd narrows ONE period and feeds both taps from it (`both_reference_taps_consume_one_narrowing_of_the_same_period`). So the move makes the estimate more correct, not merely different, and pre-P7-2 readings are archaeology rather than a baseline. The alignment artifact that gates chip-AEC arming (`/var/lib/jasper/chip-aec-alignment.json`) is untouched because this tool never wrote it: `jasper-aec-commission` is its sole writer and measures on its own capture path, so there is no stale artifact that could verify against the new method without re-measurement. `--apply` stays volatile and `jasper-aec-init` still overwrites it. The retired tripwire in `tests/test_aloop_program_lane_width.py` ("aec_tune must still open the raw dsnoop") is replaced by its inverse — an AST-over-values absence guard, docstrings excluded, mirroring P7-1's — so the module's own prose about the retirement neither satisfies nor trips it, and the lane drops from FOUR declarers to THREE in the same commit as `mixer.rs`'s "four-place fact" doc comment. Prose sweep across 17 files at head — 8 docs, plus 14 non-test/non-tool sites counting the asoundrc's five tuner-as-reader entries and its substream-7 allocation line, the modprobe allocation header, fan-in's `config.rs` and `mixer.rs`, doctor's `check_fanin_asound_wiring` remediation, and `pyproject.toml`'s alsaaudio dependency rationale (the dep stays — it is needed for the USB-mic relay and the probes' direct mic capture; only its claim to be there for an AEC *reference* was false). **Known residue, deliberately not edited here:** three `rust/jasper-fanin/src/mixer.rs` comments (`:666-667`, `:1747-1748`, `:2827-2828`) still justify the lossy aloop MIRROR by "the AEC-fallback dsnoop + aloop diagnostics" — both consumers are dead (P7-1 and P7-3/P7-2 respectively), which contradicts the P7 work row above. **Closed by P7-4** ([#2437](https://github.com/jaspercurry/JTS/pull/2437)), which deleted `RingOutput.mirror` and all three comments wholesale; deferring them here was the right call, since editing prose that dies on the next merge would only have manufactured a rebase conflict. Five further P7-1-era survivors sit in files this PR does not touch and are **owner: P7-5** — `jasper/cli/doctor/aec.py:942-945` (an operator-facing WARN still sending operators to `/proc/asound/Loopback` hw_params for a bridge that has not opened it since P7-1 — the highest-value one) and its `:934-935` comment, `jasper/correction/coordinator.py:40-41` ("It taps the music chain via dsnoop"), `AGENTS.md:2240` (architecture line still offering "or dsnoop tap" as a live AEC reference input), and `docs/HANDOFF-aec.md:2658-2659` ("The bridge already uses alsaaudio for ref capture"). **P7-3 (aloop-tap diagnostics) — PR [#2418](https://github.com/jaspercurry/JTS/pull/2418) MERGED 2026-08-13** (`3257a28ff`). The diagnostic consumers of the tap, classified per file rather than ported wholesale — and **none of them re-pointed**, because a deleted tap leaves nothing for a *tap* instrument to point at. DELETED: the chip-AEC experiment cluster (`jasper/chip_aec_experiment.py`, five `scripts/chip-aec-*.sh`, their shared `scripts/_chip_aec_experiment_lib.sh`, and two test modules) — the experiment concluded and its production path shipped in 2026-06, so the tap was the only thing still holding the harness up; and `scripts/airplay-receiver-timing-proof.py` (plus its test), whose *measured object* is lane 7 itself — it already refused outputd's production-default `shm_ring` content source, so it was unrunnable on the fleet's own topology before this arc reached it. KEPT AND TRIMMED: `scripts/aec-probe-timing.py`, the one candidate whose subject survives — it measures outputd's final speaker-reference UDP stream and the chip-ref writer tee, so only its third `jasper_capture` reference source came out, along with the now-orphan `capture_alsa_ref` helper and the `--jasper-capture-pcm` flag, pinned by a name-absence guard mirroring the one `aec-probe-latency.sh` already carries from its own earlier re-point. Doc sweep: two `testing-tooling.md` catalog rows and one index row removed, `AEC-DIAG-03`'s ref-source table and example trimmed, four `doc-map.toml` routes dropped (one of them the `scripts/chip-aec-*` glob, surfaced by the stale-glob guard rather than by grep), and `HANDOFF-airplay.md` keeps the 2026-06-29 `84-87 ms` receiver figure and its epistemic limits while losing the reproduce recipe. Four docs keep their references to the deleted files as archaeology, and they are archaeology for four different reasons — the labels are not interchangeable: `CHIP-AEC-EXPERIMENT.md` carries a `Status: historical` callout; the deep-audit pair is an audit *record* (`REVIEW-deep-audit-2026-07-11.md` is the immutable evidence, `REVIEW-deep-audit-ledger.md` calls itself the live tracker but its rows cite finding locations as they stood at audit time); `PLAN-usb-mic-export-latency-fix.md` carries no callout of its own and is classified in README's atlas as a verbatim point-in-time plan/execution record; and `docs/bass-extension-waves/limiter-bench-runner-implementation.md` was a spent session prompt wearing no status tag at all — this PR tags it historical (its runner shipped as `jasper/bass_extension/bench/`) and repoints its prior-art line at the surviving write-up. All 236 Markdown files still link-check clean because every one of those references is a code span, not a link. **Two guards found what grep could not**, and both are the reason a deletion PR runs the merge lane rather than a targeted selection: the doc-map stale-glob guard caught the `scripts/chip-aec-*` route, and `test_env_vars_codified` caught that the deleted teardown script had been the *only* codification surface for `JASPER_AEC_CHIP_HPF_HZ` — which turns out to be read at one site as a wake-event telemetry stamp and applied by nothing, since the managed-XVF profile fixes `AEC_HPFONOFF` as product policy. It is allowlisted as internal with that reasoning; the two `HANDOFF-aec.md` rows still advertising it as a live chip-HPF tuning knob are **flagged, not fixed** here — pre-existing drift, owner's call. **P7-4 (the fan-in aloop MIRROR) — PR [#2437](https://github.com/jaspercurry/JTS/pull/2437) MERGED 2026-08-14** (`6370dbf1f`; `2026-08-14T01:29Z`, verified via `gh`). The arc's first *producer* retirement, and the inversion the two consumer PRs were clearing the way for: `Mixer::new`'s `Coupling::ShmRing` arm opened `config.output_pcm` as a lossy, non-blocking side-tap and shadowed every published period onto `hw:Loopback,0,7`, which is the whole reason a ring-coupled box's dsnoop consumers kept seeing audio. **`Output::Alsa` is untouched** — a `loopback` box still writes the lane as its program path, and `config.output_pcm` survives for that arm — so this deletes a *second* writer, not the lane. **MERGE ORDER IS LOAD-BEARING: this lands AFTER P7-2 ([#2444](https://github.com/jaspercurry/JTS/pull/2444))**, because `jasper-aec-tune`'s RAW `arecord -D jasper_capture` is the one surviving reader that a ring box's now-silent lane would feed nothing. Were the order inverted, the tuner would fail *loudly and self-recoveringly* rather than silently: the **reference-RMS floor** (`jasper/cli/aec_tune.py`, `ref_rms < 50` → `raise TuneError`) fires first, before correlation is even attempted, and the run unwinds through its `finally:`; the correlation-confidence floor (`MIN_APPLY_CONFIDENCE`, which a silent reference drives to exactly 0.0, making `_apply_volatile_delay` refuse) is the backstop behind it, not the guard that trips. So no bad `AUDIO_MGR_SYS_DELAY` gets written on either path — but a reader with no producer is still the shape this arc exists to remove, and P7-2 merging first means that window never ships at all. Consumers enumerated and each given a verdict: CamillaDSP (ring boxes already capture `jts_ring_capture` — `capture_kwargs_for_coupling`; loopback boxes ride `Output::Alsa`), the doctor's `aec_probe` (outputd UDP since P7-1), `baseline-reemit --endpoint aloop` (moves sink *and* source together, and de-arming restores `loopback` coupling, where the lane has its primary writer back), commissioning's `capture_route_current` (protected by the owner's 2026-08-12 de-arm-first ruling on #2254 — commissioning only ever runs on the aloop path), the wake-corpus recorder (UDP, never ALSA), and `_loopback_playback_active` (already skips substreams >4 by construction). **This makes an already-shipped refusal's premise literally true**: the #2344 commissioning-emitter guard above says "the tap fan-in stops feeding under `shm_ring`" — a claim the mirror falsified when it was written, and P7-4 is what turns it into a fact. Also removed: the `mirror_frames` / `mirror_drops` STATUS keys, deliberately rather than pinning them at 0 — an absent key says "no mirror", a zero says "a mirror that wrote nothing", and only one of those is true (the same reasoning that un-folded `drops`). The retired in-crate bit-identity test goes with its subject; the ordering contract it deferred to (`test_step_fills_output_buf_once_above_the_transport_dispatch`) survives with a new rationale — `output_buf` is now the NARROW ring wire's published payload, so a saturate moved into the ALSA arm would have a narrow ring box publishing a stale buffer with every counter healthy. New guard `test_shm_ring_mixer_publishes_slots_and_opens_no_aloop_pcm` slices the `Coupling::ShmRing` arm, strips comments (a bare grep is defeated by the very comment documenting the removal), asserts no `open_output` / `open_music_output` / `PCM::new` / `output_pcm`, and carries a **positive control** proving the same slicer finds the opener in the `Coupling::Loopback` arm; four mutants verified against a green control and a no-op control. One deliberate non-change, named rather than hidden: on a WIDE ring box the shared `saturate_to_i16` into `output_buf` now feeds nothing (the wide wire publishes `wide_payload`; the mirror was `output_buf`'s last wide-path consumer), so ~512 clamp ops per period are dead work — bounded and tiny, and moving the saturate into the arms is a perf change with its own mutant surface, so it is left for a later pass rather than smuggled in here. Prose swept with the co-occurrence regex plus mirror/lane-7 attribution greps: fixed in `rust/jasper-fanin/{README.md,src/main.rs,src/config.rs,src/mixer.rs,src/state.rs}`, `docs/audio-paths.md`, `docs/HANDOFF-fan-in-daemon.md`'s topology diagram, `deploy/alsa/asoundrc.jasper`, `deploy/modprobe.d/snd-aloop.conf`, `README.md`'s diagram, and `tests/test_aloop_program_lane_width.py`'s docstring — and, load-bearing, `docs/HANDOFF-usb-latency-measurement.md`'s **"Rejected paths (do not re-chase)"** entry, which explicitly told a future session NOT to delete this mirror. That entry is marked superseded rather than deleted: its premise (the mirror fed "the AEC fallback dsnoop and aloop diagnostics") was retired by P7-1/P7-3, but its other claim — that removing the mirror **saves zero latency** — still stands, and that review must not become retroactive motivation for a change it did not make. **P7-5 (the doctor re-points and the routed prose survivors) — PR [#2462](https://github.com/jaspercurry/JTS/pull/2462) MERGED 2026-08-14** (`d7204b9a0`; `2026-08-14T05:02Z`, verified via `gh`; owner-local `01:02 -0400`, same day in both frames). The arc's last PR and the only one that moves no audio: every doctor surface whose PREMISE P7-1..P7-4 falsified is re-derived against both box kinds, keeping each check's logic and severity where its subject survives. Per-check verdicts. `check_fanin_asound_wiring` — true on both, prose fixed: every ALSA-graph assertion is file-level drift detection against `deploy/alsa/asoundrc.jasper`, not runtime, so they hold on a ring box where the definitions have neither writer nor reader — but the docstring still sold it as an AEC check ("the exact split-brain failure that can break AEC") after P7-1 made the tap invisible to AEC and P7-2 took the tuner off it; it names CamillaDSP's capture now, says out loud why it survives ring coupling, and (gate N1) exempts its one non-graph assertion, the trailing `audio_topology.env` existence probe. `check_loopback` — true on both, and it gains the docstring it never had; the pair allocation is pointed at where it lives canonically, `deploy/modprobe.d/snd-aloop.conf` (gate N2: `asoundrc.jasper` cross-references it, so "stated once" was the wrong word), rather than being re-taught here. `check_shairport_sync_loopback_plughw` — **logic fixed**: its three LEGACY branches (`jasper_renderer_in`, `plughw:Loopback,0,0`, raw `hw:Loopback,0,0`) hardcoded `shairport_substream` as the redeploy target, which is the WRONG device on an armed box, and a redeploy to it converges nothing; all three now name what `renderer_lanes.device_for` resolves for this box — the same single spelling of the armed→device rule the coherent branches already used — pinned over three stale values × armed/unarmed, and the three legacy tests, which silently read the HOST's lane map, now pin it like their neighbours. `check_mic_capture` / `check_mic_card_matches_config` — true on both (UDP transport, no aloop dependency), plus one inline fix: a remediation told operators to confirm the deleted `LoopbackAEC` card with `aplay -l | grep Loopback`, which lists the MUSIC card and reads as confirmation of the opposite. `_loopback_playback_active` (`doctor/_shared.py`) — logic UNCHANGED and correct (it answers exactly what its name asks, and the >4 skip must stay for the coupling where lane 7 has a writer); prose fixed twice — lane 7 is open-when-idle only under `loopback` now, and the coverage-limit disclosure enumerated ONLY USB Audio Input when a ring-armed renderer lane (U3/P6) is equally invisible. That second one reaches the operator, which is why it is the honesty fix that matters here: the AEC-output message that DOWNGRADES a silent-reference FAIL to OK explains itself with "if the speaker WAS playing … the silent ref is unexplained", and on an armed box the speaker playing unseen was materially more likely than the sentence admitted. `aec_probe` — its ref-source prose was already re-pointed at P7-1; its two correction-lane strings named `correction_substream` unconditionally and now report `correction_play_device()`, the lane's one transport reader. Checked and cleared: `renderers.py`'s other `/proc/asound/Loopback` reader (`_fanin_lane_busy_owner_matches`) already dispatches ring lanes to the ring header, and `HANDOFF-aec.md`'s lesson #6 is a dated incident lesson whose actor is the pre-P7-1 bridge by construction. **Verified-not-changed**, as asked: `audio_runtime.py`'s `output.pcm` config-echo tripwire comment is exactly right — `output_pcm` is still parsed unconditionally (`config.rs`), pushed into STATUS unconditionally (`state.rs`), and opened only in the `Coupling::Loopback` arm (`mixer.rs`). **The five routed P7-1-era survivors are closed**, and one of them was not a re-wording: `HANDOFF-aec.md`'s "the bridge already uses alsaaudio for ref capture" is false in the strongest sense — P7-1 deleted that import outright — so the RAM-saving idea resting on it had to be re-derived rather than re-phrased. Two more surfaced in the same files and were fixed with them: `HANDOFF-aec.md`'s opening paragraph still routed anyone touching `pcm.jasper_capture` to the AEC doc, and `_AEC_DRIFT_WARN_THRESHOLD`'s comment promised a baseline the re-pointed warn message no longer repeats. **HS-N2 from #2415's panel is the one code change, and re-deriving it moved the fix twice.** Doctor gated the authoritative schema-v4 reference-freshness assessment on `JASPER_AEC_REF_SOURCE` read from ENV, so a box parked by a pre-P7-1 reconciler — carrying the retired `alsa` on disk while the bridge converged to `outputd_udp` and published that — skipped the assessment and fell to the music-conditional journal fallback, the surface that returns OK for a dead reference whenever no snd-aloop renderer lane is open. The obvious fix (read the applied source, use it instead) was wrong in two ways the existing tests caught: reading `active_capture_plan.mic_reference_identity.ref_source` INVERTS this module's shipped ruling that the v4 `reference_input` block beats the epoch-based plan, and *replacing* env with the snapshot loses the env-says-outputd / receiver-says-otherwise runtime-identity FAIL. So the route resolves as an OR over the two — either end saying `outputd_udp` demands the contract — which is the fail-closed direction and leaves both the identity FAIL and the neither-end-outputd journal policy where they were. **The gate refused "can add a FAIL, never mask one" as an absolute and was right to** (SF-1): the assessor's ≤10 s startup grace returns OK *before* the journal is read, so a bridge restarted seconds ago whose PREDECESSOR's silent-ref windows are still inside the unit-scoped 90 s journal now reports OK where the env-gated path reported FAIL. The behaviour is correct — it is the identical grace an env-says-`outputd_udp` box has always taken, it is exactly what the grace exists for, and it self-corrects once `process_age` clears it — so the sentence was qualified and a grace/past-grace test pair now pins the convergence as intended rather than tolerated; none of the three original OR tests reached that path, all three running at the helper's default 60 s process age. `_applied_reference_source` reads the v4 field and is fail-soft: absent, malformed, or older snapshot returns None and env decides, so a rolling deploy is unaffected. **Two findings surfaced rather than fixed.** The drift branch this PR re-pointed **cannot fire**: the bridge stopped logging `drained N stale ref frames (drift)` in PR [#157](https://github.com/jaspercurry/JTS/pull/157) (2026-05-19, drain-newest → in-order single-frame consumption) and nothing in the tree emits that signature today, so `_AEC_DRIFT_WARN_THRESHOLD` is unreachable and every `drift=N` in the OK details is structurally 0 — a number that reads as evidence of no drift while being evidence of nothing. Recorded as a comment at the branch rather than deleted, because `jasper.audio_validation._measured_drift_delay_check` counts the same dead signature and retiring it is a decision, not a side effect of a prose re-point. Second: `_loopback_playback_active`'s armed-box blind spot is fixed as PROSE here because closing it for real needs a fan-in STATUS read or a lane-map consult inside a helper that is deliberately `/proc`-only — design, not a re-point. **P7-1 … P7-5 COMPLETE — every lane-7 consumer is re-pointed, the ring box's aloop writer is gone, and every doctor surface the four falsified is re-derived on both couplings.** The clause covers the sub-PRs, NOT everything named in this cell: P7-3's flagged `JASPER_AEC_CHIP_HPF_HZ` doc drift is still genuinely open as [#2419](https://github.com/jaspercurry/JTS/issues/2419) (owner's call — it advertises a knob nothing applies, and gates nothing in this arc). The mirror-residue comments and the five routed P7-1-era survivors are resolved in-cell above. P9 stays gated on P6 + P7 + P8; this closes P7's third of that gate only |
| P8 | Ring v2 | **OPEN — U1**, rescoped by R-RING2 to cover format and channels in one design. Every rung is merged as of 2026-08-11 (R1–R5, R7a, R7b) and jts3 is armed wide; what keeps P8a open is per-box activation — R6 (jts.local) is owner-gated, jts4 unvalidated. See [Ring v2 design outcome](#ring-v2-design-outcome-u1) below |
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
| R7a | DAC8x `LatencyFloor` + floor soak | **DONE** — soak PASSED 2026-08-11, all three 30-min windows, every gate (`captures/r7-jts3-20260811T051852Z`); PR [#2324](https://github.com/jaspercurry/JTS/pull/2324) merged 2026-08-11 at `LatencyFloor(256, 1536, 128, 256)`, delta 0/0; deployed to jts3 the same day, all gates, DAC presentation latency 63.833 → **5.167 ms** live and matching the soak to three decimals (`captures/r7-jts3-deploy-20260811T095945Z`) |
| R7b | jts3 active-half | **DONE** — PR [#2326](https://github.com/jaspercurry/JTS/pull/2326) merged 2026-08-11, fix round + three same-lens deltas 0/0. jts3 **ARMED WIDE 2026-08-11** at the third attempt: attempt 1 halted on a validator/reconciler deadlock at the documented mid-arm waypoint (`captures/r7b-jts3-arm-20260811T111338Z`, fixed by PR [#2329](https://github.com/jaspercurry/JTS/pull/2329)); attempt 2 halted read-only on a wire shear plus a width gate that proved two of three declaring ends and claimed three (`captures/r7b-jts3-arm2-20260811T132227Z`, fixed by PR [#2335](https://github.com/jaspercurry/JTS/pull/2335), which also built the per-box wire mechanism E7 needs and moved the capture half); attempt 3 completed the ladder and passed 12/12 (`captures/r7b-jts3-arm3-20260811T162742Z`). Each halt rolled the box back to entry with the dead-man armed and never fired. The mechanics of all three live in the arm/rollback lifecycle below |

R1 and R2 land the ring transport layer v2-capable in both languages —
wide/N-channel geometry is accepted and byte-copyable end to end — and it
stays inert on any box until that box is armed: a conf.d block declares a
non-default format/channels only where `jasper-audio-hardware-reconcile` has
rendered the box's resolved wire into it. As of 2026-08-11 that is jts3 alone
(`S32_LE`); every other box still opens S16/stereo.
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
onward. E1–E6 are not the last word on the arm: **E7** (2026-08-11,
above under Ratified decisions) amended the width a box arms at,
after jts3's second attempt hit the shear it predicts.

**Follow-ups filed rather than folded into a rung** (states verified
2026-08-11). Each is a filed issue, not a plan item — the issue owns the
detail.

| Issue | What | Where it stands |
|---|---|---|
| [#2294](https://github.com/jaspercurry/JTS/issues/2294) | Doctor observability — a dangling check name; floor-blocked ring ineligibility reports `ok` with no reason shown | Open. Targeted to ride R5, still open after R5a/R5b; the floor-render eligibility-explanation half was ruled into R7b's scope on 2026-08-11 |
| [#2306](https://github.com/jaspercurry/JTS/issues/2306) | P5c follow-ups, including the explicitly-owed Pi-side doctor pass | Open — rides the R6 session |
| [#2319](https://github.com/jaspercurry/JTS/issues/2319) | `camillagui.socket` — unauthenticated root listener on `0.0.0.0:5005` with `ReadWritePaths=/etc/camilladsp` | Open. Pre-existing, surfaced by the R7 hearing-safety repanel, out of R7 scope; the jts3 hardware runbooks stop the socket for their sequence |
| [#2327](https://github.com/jaspercurry/JTS/issues/2327) | jts3's corpus-mode chip-AEC `sys_delay` needs re-derivation after the DAC8x floor changed the period geometry | Open and **live-owed** from the 2026-08-11 floor deploy — jts3 has been running post-floor since then |
| [#2332](https://github.com/jaspercurry/JTS/issues/2332) | Rollback ladder step 2 is refused on an ordinary armed box (ring-plan endpoint mismatch) | Open. Owner-ruled 2026-08-12 (this issue): refuse-then-complete is documented as the canonical rollback route in the lifecycle section below; the validator is untouched. "Complete" means the box validates clean at the aloop endpoint, not that it plays audio — P9-C deleted that endpoint's PCM definitions, so a completed rollback is a parked box until re-armed. Hardware verification rides the #2254 corpus-exit session's de-arm ([owner ruling](https://github.com/jaspercurry/JTS/issues/2254#issuecomment-5267220207)) |
| [#2337](https://github.com/jaspercurry/JTS/issues/2337) | An `/eq/` or `/sound/` save on an armed roleful box re-emitted the snapshot's ALSA endpoint and silently disarmed the ring | **Closed** by PR [#2343](https://github.com/jaspercurry/JTS/pull/2343) (merged 2026-08-11) and proven on armed jts3 the same day — two household-path `/sound/apply` saves preserved every armed-state field. The jts3 operating restriction is **lifted 2026-08-11** (`captures/endpoint-deploy-jts3-20260811T185255Z`) |
| [#2338](https://github.com/jaspercurry/JTS/issues/2338) | Unify `build_baseline_profile_candidate` onto `active_emit_devices` — the second emit site never learned the both-halves lesson | **Closed** by PR [#2359](https://github.com/jaspercurry/JTS/pull/2359) (merged 2026-08-12, `f6e2ea640`) — filed at #2335's merge, same family as #2337/#2339. It routes the candidate builder's capture lane, wire format and latency geometry through `active_emit_devices` and walks the contract at four forwarding sites instead of two |
| [#2339](https://github.com/jaspercurry/JTS/issues/2339) | `reconcile-current-dsp` clobbered an armed ring graph, so arm step 3 and every deploy silently de-armed a roleful box | **Closed** by PR [#2343](https://github.com/jaspercurry/JTS/pull/2343) (merged 2026-08-11) — found live on jts3 during the arm, and proven fixed there the same day: a full deploy left the arm intact, the seam firing exactly as before onto an identical graph. Restriction **lifted 2026-08-11** (`captures/endpoint-deploy-jts3-20260811T185255Z`) |
| [#2340](https://github.com/jaspercurry/JTS/issues/2340) | Deploying to jts.local over its USB management address self-severs the deploy's own ssh — install succeeds on the Pi while the laptop hangs on a half-open socket | **Closed** by PR [#2358](https://github.com/jaspercurry/JTS/pull/2358) (merged `1caff2304`, 2026-08-11) — the deploy preflight now warns when `PI_HOST` resolves inside the USB gadget's management subnet, and `SSH_BATCH_OPTS` carries keepalives so a severed transport surfaces as an ssh error in about a minute instead of an unbounded hang |
| [#2344](https://github.com/jaspercurry/JTS/issues/2344) | A `web_commissioning` measurement sweep on an armed box excites the unfed aloop lane and measures silence | Open. The mechanism is **two** defects, not one, and they take opposite fixes: the applied-summed measurement graph inherited the snapshot's lane and now reads `resolve_live_active_endpoint` like the rest of that family, while the per-driver/summed **commissioning** graph resolved the ring by name but forwarded none of the rest of `active_emit_devices` and now **refuses** on an armed box instead of emitting a ring sink over the aloop capture — and that refusal is **permanent, not a stopgap**: the owner ruled forward-only on 2026-08-13 ([#2412](https://github.com/jaspercurry/JTS/issues/2412)), superseding the earlier de-arm → chip-AEC commission on the aloop path → re-arm shape this row used to describe — the aloop pipeline is deleted even though commissioning/corpus mode still depends on it, corpus mode is a small debug/experimental feature that may stay broken in the interim, and the failure stays fail-closed and loud. Code in PR [#2363](https://github.com/jaspercurry/JTS/pull/2363) (up 2026-08-12, not merged), mutation-proved; on-device armed-ring sweep **pending**, so the jts3 restriction is **ENFORCED** until it passes and this issue closes on the sweep validating the *first* half's route |
| [#2345](https://github.com/jaspercurry/JTS/issues/2345) | fan-in emits `tts.assistant_loudness.final_gain_db=+3.0` while the doctor asserts the clamp is `[-60, 0]` — the assistant can be boosted past a bound the doctor believes is enforced | **Closed** by PR [#2355](https://github.com/jaspercurry/JTS/pull/2355) (merged 2026-08-11, `3ef3e74cd`). **The doctor's assertion was the stale half.** The engine has had no fixed positive ceiling since `6304556a4` "Remove fixed TTS gain ceiling" (2026-07-01), which updated `audio-paths.md` and `HANDOFF-volume.md` and missed the doctor; positive gain there is intentional, because a pre-DSP decision pre-compensates for CamillaDSP's downstream attenuation. The `+3.0` was not a clamp at all but the computed peak cap (`max_peak_dbfs=-3.0` minus the uncalibrated fallback source peak `-6.0`) holding a `+5.0` request down, so whether ordinary music re-anchors the target positively never had to be answered. The doctor now asserts the per-decision contract `max(floor, min(requested_gain_db, peak_cap_gain_db))` — the real floor and the computed peak cap — rather than a positive ceiling nothing enforces. |
| [#2348](https://github.com/jaspercurry/JTS/issues/2348) | Gain-structure normalization at prescribe time — push the static trim set to the computed headroom ceiling | Open — [#2291](https://github.com/jaspercurry/JTS/issues/2291)'s prescribe stage, raised by the gain structures the arm's re-emit exposed on jts3 |

**P8 splits.** P8a is this ladder — solo-stereo width plus active
N-channel. P8b — composite ring plus bonded round-trip ingress — moves
out to later, and still hard-gates P9; jts5 stays on the aloop lane 6
bonded round-trip until P8b — lane 5 no longer exists anywhere in the
fleet, since P9-C deleted its PCM definitions once the ACTIVE ring became
the roleful transport.

**P8b item 1a — the composite content-PCM skip — LANDED.** The one piece of
P8b's item 1 that is independent of the rest and safe on its own, so it
landed alone rather than waiting on the arm-enabling set.
`PairedCompositeSink::new` opened and `.start()`ed its active content
capture PCM unconditionally, while its `AlsaBackend` sibling had carried a
`skip_content_pcm` arm since the ring shipped. Under `shm_ring` the run
loop reads the ring and never calls `read_content_period`, so a composite
would have held a **started, unread** aloop capture lane — and since P9-C
deleted `outputd_active_content_capture`'s PCM definitions, that open now
fails; four consecutive failures are parked out-of-band by
[`jasper-outputd-failure-reconcile`](../deploy/bin/jasper-outputd-failure-reconcile)
rather than escalating to `StartLimitAction=reboot` (the carve-out landed in
[#2261](https://github.com/jaspercurry/JTS/pull/2261), closing
[#2247](https://github.com/jaspercurry/JTS/issues/2247); see the rollback
warning below) — the box stays up, but a composite without the skip would sit
permanently parked and silent. Without 1a, item 1 does not gate P9-C, which is item 1's entire stated
purpose. The skip decision and its `/state` stand-in are now **one owner
each**, read by both sinks (`content_pcm_skipped`,
`synthetic_content_negotiated` in `rust/jasper-outputd/src/alsa_backend.rs`)
rather than a comparison spelled once per transport — a second copy is
exactly what let the two diverge in the first place.
**This changes no box's behaviour and arms nothing**: `Config::from_env`
still admits `shm_ring` only on a single-ALSA sink (`ring_active_ok`), so
no composite reaches the skip arm and the change is inert on the fleet —
which is exactly what made it safe to land alone, ahead of the set that
widens that gate. The rest of
item 1 (1b–1f), item 2's bounded per-child xrun recovery, and item 6 ride
[#2482](https://github.com/jaspercurry/JTS/issues/2482); the standing rule
from the design holds — **item 1 makes a composite *armable*, item 2 is what
makes arming one safe, and nothing may be armed between them.**

**Forced ordering inside jts3: R7a before R7b — discharged 2026-08-11.**
Both rungs have merged and jts3 is armed, so this is why the order was what
it was, not a live constraint. jts3 *was* blocked on a missing
`DacProfile.latency_floor` declaration for `HIFIBERRY_DAC8X`: the full
4-field `LatencyFloor` (`camilla_chunksize`, `camilla_target_level`,
`outputd_period_frames`, `outputd_dac_buffer_frames`, with
`camilla_target_level` enforced ≥4× `camilla_chunksize`). Per the ring v2
rulings record's round-2 amendment
(`captures/PLAN-ring-v2-rulings-2026-08-10.md`), declaring it for
`HIFIBERRY_DAC8X` also set jts3's CamillaDSP chunk/target — it touched
the **active graph's** chunksize, not just outputd's period. All three
panel lenses verified that ordering independently. It does not recur for
another DAC8x: `HIFIBERRY_DAC8X` now carries the declaration
(`jasper/audio_hardware/dac.py`), so any such box picks the floor up by
deploying. The ordering binds again for a profile that has no floor of its
own. One of those still matters to this campaign: `HIFIBERRY_DAC8X_STUDIO`, the
DAC8x-family member, whose entry in that same registry now says in code that it
declares no floor and ships the conservative global default instead — an
absence stated explicitly rather than left to be inferred from a missing field,
because the floorless-DAC contract tests and the no-floor doctor branch are all
written against that profile. It needs its own declaration and its own soak
before the ordering is discharged for it.

`INNOMAKER_HIFI_AMP_PRO` — the profile jts4 and jts5 both run — **now carries a
declaration** (2026-08-14, from jts4's own measurement), so it is no longer one
of them. The discharge is only PARTIAL, though, and the residual is the reason
to state it rather than tick it off: the declaration's **outputd** half is
measured on that board (period 128 / dac_buffer 512), while its **CamillaDSP**
half was chosen to TIGHTEN NOTHING against the shipped global default —
chunksize equal to it, target_level above it — precisely so it could ship
without a soak. So the ordering above is discharged for the measured half, and
still binds for any future TIGHTENING of the CamillaDSP half on this silicon:
moving that chunksize below the shipped default needs a loopback-lane soak on
this board first, and the registry entry plus
`test_innomaker_unmeasured_camilla_half_tightens_nothing` are what hold the
condition open.
**Correction from the R7 repanel:** jts3 runs `aec-init` in **CORPUS mode** — no
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
(policy, not layout), composite excluded from the STEREO ring by program
shape and — since P8b item 1 — ARMABLE on the ACTIVE ring at 4 channels,
though no composite box is armed: item 2's linked-group recovery
has LANDED (#2255, closed via #2496), so the remaining gate is an
operator-driven arm plus item 6's measured buffering check —
enabled, not armed;
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

**Rollback no longer restores audio.** P9-C deleted the aloop active-content
lane's PCM definitions (`outputd_active_content_playback` / `_capture`); the
name survives in the endpoint vocabulary, so `--endpoint aloop` still runs
and the ladder still completes, but it now re-points the box at a transport
that does not exist. The box finishes the ladder and then parks: four
consecutive content-lane open failures are parked out-of-band by
`jasper-outputd-failure-reconcile` (a record written to
`/run/jasper-outputd-content-lane.state`), spending 4 of
`StartLimitBurst=5` and never reaching `StartLimitAction=reboot` — the box
stays up and reachable, the speaker goes silent. Recovery is re-arming the
ring, not rolling back again. Rollback is now a de-arm for maintenance, not
a fail-safe.

**Step 1 accepts EITHER roleful boot graph — a box does not have to be
commissioned to arm.** A roleful box has two legal boot graphs, and the
fleet-typical composite (jts.local today, jts5 after re-fit) is on the
second one by design:

| Boot graph | Class | What step 1 does |
|---|---|---|
| APPLIED baseline | `approved_active_runtime` | re-emits it from its immutable snapshot |
| all-muted startup anchor | `all_muted_active_startup` | re-stages it from the box's own saved design draft + crossover preview |

An applied baseline wins when both are present, so a commissioned box
(jts3) behaves exactly as it always has. Any other class — a parked
graph, an unrecognised one, a topology with no roleful outputs — is
refused by name; step 1 never guesses which of the two a box was meant
to be. Only `--endpoint` is operator-supplied: the re-stage derives
everything else from persisted on-box state, so it cannot smuggle in a
draft the box never saved.

The anchor path publishes the same way the baseline path does, and for
the same reason: it stages into a scratch location first, re-proves the
result, and only then writes the live artifact plus the staged metadata
that LOCATES it (the runtime contract follows that metadata's
`config.path`). The anchor sits at a fixed path, so writing it *is*
moving the boot graph — there is no separate pointer to gate on, which
is why the proof has to come first.

**Step 1 moves BOTH device halves, and declares the RESOLVED wire.** The
coupling is end-to-end, so the re-emit's ring-endpoint graph captures
`jts_ring_capture` **and** plays the active ring: under `shm_ring` fan-in writes
Ring A and stops feeding the snd-aloop tap, so a graph that moved only its sink
would source a device nobody writes — silence with every daemon healthy, and
*quiet*, because the plan compares capture channels (Ring A and the tap are both
stereo) and the width gate only holds ring-**named** lanes to the wire.
`--endpoint aloop` runs the same derivation in reverse and names the
snd-aloop tap, but that lane has had no PCM definitions since P9-C, so the
resulting graph is un-openable and the box parks (see the rollback warning
above). The graph takes its
`format` from `resolve_ring_wire` and its CamillaDSP `chunksize`/`target_level`
from the certified ring geometry (`RING_CAMILLA_*`, chunk/target 128 with
`enable_rate_adjust: false`) — **never** the box's program-lane default or its
DAC `LatencyFloor`, which describe the LOOPBACK lane. jts3's floor target alone
(1536) is six times the whole 2-slot ring's 256-frame capacity. All of it comes
from one derivation keyed on the sink device
(`jasper.active_speaker.camilla_yaml.active_emit_devices`), so a caller that
re-points a graph at the ring cannot pick up one part and miss another.
A graph that declares the wrong wire anyway is refused at **step 3** by
`ring_edge_width_ready`, which inspects the loaded graph as a declaring end and
names the lane that sheared — it is not left to fail at the ioplug attach.
Two of these were live at `c4c9bfe1c` and halted the 2026-08-11 arm
(`captures/r7b-jts3-arm2-20260811T132227Z`): the re-emit inherited `S32_LE` from
the program lane, and the width gate reported "all declaring ends" while
structurally unable to see the graph. The capture half would have halted the
next attempt one rung further, in silence.

**Step 3 CONVERGES on an anchor box; it only re-emits on a commissioned
one.** Step 3's camilla rung is `reconcile_current_dsp`, and an all-muted
startup anchor is a *transient* active graph, which the carrier correctly
refuses to host EQ on (`eq_on_active_not_wired` → status `skipped`). The rung
used to read every `skipped` as "the ring config was NOT loaded" and fail,
rolling the whole box back to loopback — so the fleet-typical anchor box, the
one step 1 was widened for, could never pass step 3 at all. Observed on
jts.local, 2026-08-15, on its first composite arm: `ok=False changed=False
outputd=True fanin=True camilla=False recovered=True
detail=eq_on_active_not_wired result=arm_ring_camilla_failed` — fail-closed,
nothing broken, and no way forward. The rung now accepts that ONE refusal when
`jasper.fanin.coupling_reconcile.ring_endpoint_anchor_converged` can prove
**four** things from the artifacts on disk:

1. **identity** — the loaded graph's path is the staged record's `config.path`,
   on a record whose own `status` is `staged`. The loaded path is the one the
   **daemon** reports (`reconcile_current_dsp`'s skip payload carries
   `current_config_path`, taken from CamillaDSP's websocket), falling back to the
   statefile only when no daemon answer is in hand — the statefile is a durable
   pointer with several other writers and can name a graph the running daemon
   does not hold;
2. **endpoint** — it captures `jts_ring_capture` and plays
   `jts_ring_active_playback`;
3. **wire** — both ring lanes state the box's resolved wire, on format AND
   channels;
4. **all-muted** — every output ends in a **terminal** wired hard mute at
   `STARTUP_MUTE_GAIN_DB`, measured on the graph rather than inherited from the
   stager that emitted it. All three facts of
   `graph_safety.output_terminally_muted` (promoted from
   `runtime_contract._flat_output_terminally_muted`, which now delegates): the
   mute idiom, wired; the mute is TERMINAL for its channel; and no `bypassed`
   step anywhere. Fact 2 is the one that matters — CamillaDSP applies later
   steps after earlier ones, so a `+240 dB` Gain appended after the mute, the
   same gain appended into the mute step's own `names`, or a trailing `Dither`
   all undo it while a present-somewhere mute still reads as satisfied. Those
   are the three falsifications `_parked_graph_allowed` already records.

There is genuinely nothing to re-emit there: an all-muted anchor hosts no EQ,
and step 1 already put the graph at the endpoint. It reports
`detail=converged_anchor` — in the journal
(`event=fanin.coupling_reconcile result=camilla_converged_anchor`, INFO; the
refusal is `result=camilla_anchor_not_converged`, WARNING, carrying the reason)
and on the operator's stdout line — so a converged arm is never read as one that
wrote a graph. A commissioned box's applied baseline still RECONCILES exactly as
before, and every other `skipped` still fails and recovers to loopback: a
different refusal code, a per-driver commissioning load (told apart by PATH,
since it classifies like the anchor), an anchor still at the aloop endpoint,
one that moved only its sink, one at the wrong wire, or one that is not muted.

**Expected on an armed box: `jasper-doctor` reports `audio runtime plan: warn`,
permanently.** A box whose DAC declares a `LatencyFloor` carries two standing
plan warnings once the coupling is `shm_ring` — `JASPER_CAMILLA_CHUNKSIZE` /
`_TARGET_LEVEL` "effective value is 128 under shm_ring; … is the
loopback/hardware-floor value, not the ring runtime value". That is the plan
correctly reporting that the ring overrides the floor, not drift, and it does
not clear: the floor and the ring geometry both stay declared. On jts3 the pair
reads 256/1536 (floor) against 128/128 (ring). Read it as the arm's signature,
not a fault; the ring-specific health signals are `fan-in coupling`,
`camilla playback format`, and `ring conf floor`.

**Why the graph moves first.** The endpoint marker
(`JASPER_OUTPUTD_RING_ACTIVE_ENDPOINT`) is derived by the hardware
reconciler from the classification of the graph the statefile points at.
The graph's playback device is in turn chosen by `resolve_output_layout`
from that marker. Left to themselves the two only reproduce each other —
a fixed point that holds in BOTH directions, so a box can neither arm nor
release. `--endpoint` is the explicit operator act that breaks it: it
re-emits this box's roleful boot graph — applied baseline or all-muted
startup anchor, per the table above — against a NAMED endpoint,
publishes it over the artifact the statefile reads, and repoints the
statefile. Everything after that is derivation. (Ratified as Option B in the R7b panel round 2;
it amends E1's earlier coupling-first rollback ordering for the same
reason the arm needs it.)

**Deploys and household saves PRESERVE the arm; they do not choose an
endpoint.** A separate set of seams rebuilds the roleful graph from the
immutable applied snapshot without being asked to move anything: `jasper-sound
reconcile-current-dsp` (which `install.sh` runs on every deploy, and which step
3 of the ladder runs too), a `/sound/` or `/eq/` save, a bass-extension apply,
and — since #2344 — the commissioning wizard's applied-summed measurement load.
The snapshot is immutable by design, so it keeps naming whichever lane
was resolved at Apply time — the snd-aloop lane, forever, on an armed box. Each
of those four seams now reads
`jasper.active_speaker.playback_route.resolve_live_active_endpoint`, one
derivation that asks the **statefile-pointed graph** first and the marker only
when that graph cannot be read: the marker is derived FROM the graph, so in
every window where the two disagree — mid-arm after rung 1, mid-rollback after
its rung 1, or a marker left set over a graph that moved back — the graph is the
half the reconcilers are converging toward. An unarmed box is byte-identical to
before, and the deploy-time reason the reconcile exists at all (refresh the
artifact so CamillaDSP cannot reopen a stale statefile against freshly-created
ring files) is unchanged — it refreshes THROUGH the live endpoint.

Before that (issues #2339 / #2337), the ladder's own step 3 de-armed the box it
had just armed: on jts3, 2026-08-11, the coupling rung's reconcile re-emitted
the snapshot's ALSA lane over the ring graph rung 1 had published and re-pointed
the statefile at it, leaving fan-in and outputd on the ring and CamillaDSP on the
tap — silence with every daemon healthy, `writer_alive=False`, Ring A
`drop_no_reader` climbing, and recovery needing a fourth action the ladder does
not document (`captures/r7b-jts3-arm3-20260811T162742Z`, files 14-16). A plain
`jasper-camilla` restart never had this effect; the statefile holds (file 27).
The ladder stays three rungs.

The Layer-A drift check
(`jasper.active_speaker.setup_status._applied_layer_a_binding`, the gate in front
of room correction) is the fourth member of the same family and takes the
opposite rule for the opposite reason: it emits nothing, so it NEUTRALIZES the
transport axis by rebuilding its expectation against the endpoint of the graph
it was handed. Its fingerprint binds `output_devices`, so an armed box could
never match a snapshot-built expectation and the gate read "Apply that crossover
again before Room correction" for a transport move nobody asked about. Feeding
it the box-level derivation instead would put a third opinion — the statefile —
into a two-way comparison and turn ordinary device-resolution drift into a
crossover-drift claim. Whether the graph names the RIGHT transport stays with
`check_fanin_coupling` and `ring_edge_width_ready`.

**Why every intermediate state is safe (ARM ladder).** This is scoped to
arming — the ROLLBACK ladder does not converge back to silence-then-recovery
the same way; it converges to a parked box by design, per the rollback
warning above. After step 1 the graph on disk
names the ring while the coupling is still loopback. Nothing in steps 1 or
2 reloads CamillaDSP — `baseline-reemit` writes the artifact and repoints
the statefile, and `jasper-audio-hardware-reconcile` bounces outputd but
never `jasper-camilla` — so the *running* Camilla is still on the previous
graph and the box usually keeps playing through both rungs. At the next
Camilla load the new graph takes effect: Camilla captures a ring nobody
writes and writes a ring nobody reads, while outputd reads an unwritten ALSA
lane — silence, not wrong audio, on both halves. After
step 2 the marker is set but the bridge is still `direct`, and outputd's
ring-path allowlist is scoped to the `shm_ring` bridge, so the marker grants
nothing. Only step 3 moves audio. A crash between any two steps leaves
silence (the restart is itself a Camilla load) and re-running the ladder
converges.

That split — playing now, silent after the next load — is why the waypoint
needs a standing surface rather than an operator's memory: the box that
looks fine while you are standing at it is the box that comes back silent.

The ROLLBACK side's step-2 window is louder than silence, in one of two
ways depending on whether the validator can read the graph — both now
converge on a parked box, differing only in which refusal gets there:

- **When it can** (the ordinary armed box), rollback step 2 is **refused**.
  Its candidate is validated against the coupling, which is still
  `shm_ring` until step 3, while step 1 has already moved the graph to the
  ALSA lane — so it fails the ring-plan endpoint comparison (`transport
  plan is shm_ring but Camilla playback='outputd_active_content_playback'`)
  and exits 78 with the marker preserved. **This refusal is the documented,
  canonical rollback route** — owner-ruled 2026-08-12
  ([#2332](https://github.com/jaspercurry/JTS/issues/2332)), not a rough
  edge to close: step 3 does not gate on the marker, so running
  `jasper-fanin-coupling-reconcile loopback` anyway completes the rollback,
  the box validates clean again, and a later hardware reconcile clears the
  stale marker. **Validating clean is not the same as playing:** the env is
  coherent, but the graph names the deleted aloop transport, so outputd
  parks — a completed rollback is a parked box until re-armed. Verified
  through `validate-outputd-env` in a scratch probe,
  not on hardware; the first hardware run rides the pending jts3 corpus-exit
  session's de-arm, per the same
  [owner ruling](https://github.com/jaspercurry/JTS/issues/2254#issuecomment-5267220207).
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

That scoping is exact: it is the *staged-candidate* refusal (exit 78). The
reconciler's other preserve — the endpoint-contract refusal at exit 66 —
does move one key first, because one preserved shape spins rather than
idles. See `docs/HANDOFF-speaker-output-reference.md` (issue #2489).

**The refusal names its source, not just the offending key.** `outputd_env_invalid`
carries the Rust-shaped pair error — which names the two KEYS, enough for the
daemon reading one merged environment and not enough for an operator, who has to
edit one of several layers. It now also names WHERE each half came from:
`/etc/jasper/jasper.env`, the reconciler-owned `outputd.env`, or the packaged
systemd/outputd default. Two cases otherwise read as contradictions — the
reconciler UNSETS a key from `outputd.env` whenever `jasper.env` owns it, so an
operator sent to `outputd.env` finds the key absent; and a value the lab override
store owns is COPIED into `outputd.env` by the latency-floor pass, so deleting
that line is undone by the next reconcile. For the second, the refusal quotes the
store's own `created_at` and `reason` and points at
`jasper-audio-config overrides-clear`.

**The override store is not a bypass of the buffer-coherence repair.**
`_coherent_outputd_content_buffer_setting` wraps the ALREADY-resolved setting, so
it sees a `lab_override` and declines by SCOPE: it rewrites only JTS's own
`route_policy` value and never an operator/lab one. Operator-wins is the design;
the candidate refusal is the correct catch, which is why the repair is not
widened. Pinned by `test_coherence_repair_sees_the_override_and_declines_by_scope`.

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
survives it, verified at `9cc41b987` — except the **P8** row, which postdates
that stamp: outputd's second `shm_ring` branch arrived with R7b
([#2326](https://github.com/jaspercurry/JTS/pull/2326)) and its per-box state
is jts3's 2026-08-11 arm.

| Row | What survives, and where |
|---|---|
| **P6** (U3) | Four aloop renderer lanes — librespot, shairport-sync, bluealsa-aplay, correction sweeps — all `plug:` wrappers over `*_substream` PCMs. fan-in's `Input` carried an aloop `pcm` or a USB `direct` capture and nothing else, which is why the ring-reader lane source was a **net-new build** rather than a re-point; P6a adds it as a third variant (`ring`), so P6b–d ARE re-points through that seam |
| **P7** (U4) | Lane 7's readers and its ring-box writer are all retired now, in that order: the AEC bridge's `REF_DEVICE = "jasper_ref"` at **P7-1**, `aec_tune`'s `arecord -D jasper_capture` at **P7-2**, and fan-in's `RingOutput` lossy aloop MIRROR — the thing that made the dsnoop consumers survive ring coupling at all — at **P7-4**. The order is the point and it held: P7-2 re-pointed the tuner *before* P7-4 removed the writer, so the window in which a ring box's tuner reads a silenced tap never shipped. CamillaDSP is the tap's last consumer, and it reaches lane 7 only under `loopback` coupling — the one place the lane still has a producer. **What was left was doctor prose, and P7-5 took it**: `check_loopback` (`doctor/audio.py`) — true on both couplings, and now carrying the docstring saying why (P7-4 removed a WRITER, not the card) — and `check_shairport_sync_loopback_plughw` (`doctor/renderers.py`), which turned out to need a small LOGIC fix rather than prose: its three legacy branches named `shairport_substream` as the redeploy target on a box whose lane map resolves the ring. `check_fanin_asound_wiring` came off this list in pieces — its *attribution strings* were fixed in P7-1, its docstring's "can break AEC" framing in P7-5, and its wiring assertions stand until P9-B removes the PCMs. P7-4 additionally fixed `check_fanin_service`'s two mirror comments; that check's `output.pcm` equality is a config echo and keeps passing, but it is vestigial on a ring box — scope it per-coupling if a later phase stops configuring `output_pcm` there (P7-5 re-verified that tripwire against `config.rs` / `state.rs` / `mixer.rs`: still parsed and published unconditionally, opened only in the `Coupling::Loopback` arm, so it stands) |
| **P8** (U1) | outputd admits `shm_ring` on **two** paths since R7b — the stereo one (`is_full_range_stereo_lr_sink`) and an armed ACTIVE-ring endpoint (`ring_active_ok`), both in `Config::from_env` (`rust/jasper-outputd/src/config.rs`; read the predicates there rather than a copy here). The second is what jts3 runs, so a roleful box's post-crossover program is now a live ring content path — but only on an armed roleful box, and only at **two channels**; aloop lane 5, which used to carry this content, was deleted at P9-C and no longer exists as an alternative. P8a's channel half is still unexercised: no ring in the fleet has carried more than stereo (jts3's active ring is 2ch), so N>2 waits for a wider roleful box to arm. Unarmed roleful boxes and composite sinks still NAME the deleted lane-5 endpoint and now park rather than play — P9 stays hard-gated on P8 so that arming, not aloop, becomes the path for them; bonded ACTIVE followers' snapclient round-trip (`hw:Loopback,0,6`, pair 6) is unaffected and keeps working either way |
| **P9** (U4) | Both snd-aloop drop-ins still ship, and `deploy/alsa/asoundrc.jasper` still defines the renderer substreams, the passive `outputd_content_*` pair, and the `jasper_capture` / `jasper_ref` dsnoop taps — P9-C already deleted the active `outputd_active_content_*` pair (pair 5) ahead of the rest of P9, since the ACTIVE ring replaced it as the roleful transport |

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
  or config-mismatched artifacts do not count. This binds the boxes whose
  `route_profile` makes a low-latency claim; a profile that makes none has
  nothing to compare against and the gate is inapplicable there — per-box
  status under the Fleet table.
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
2. **Active/composite + bonded aloop dependencies — REALIZED and ACCEPTED,
   not avoided** (U1/U4). R7b removed one of these dependencies, and P9-C
   then closed the remaining gap the hard way: an *armed* roleful box
   carries its post-crossover program content on the active ring (outputd's
   second `shm_ring` branch) — at two channels, which is all any ring has
   carried — but an unarmed roleful box, or an unarmed roleful composite,
   now NAMES a deleted transport (aloop lane 5's PCM definitions are gone)
   instead of quietly falling back to a working one. The deletion converted
   what used to be silent breakage into a loud, fail-closed park (see the
   rollback warning above) — accepted, not mitigated. Bonded followers are
   untouched: they still write `hw:Loopback,0,6`, a lane P9-C did not
   touch. Composite output is ARMABLE on the active ring since P8b item 1
   (a roleful composite resolves a 4-channel active width, the conf.d
   renders it, and outputd admits it) but **no composite box is armed**.
   Item 2's linked-group recovery has LANDED (#2255, closed via #2496), so
   what remains is not a code gate: arming is an operator-driven ladder
   gated on item 6's measured buffering-regime check on the real box. So P9
   stays HARD-GATED on P8 on **both** halves: the capabilities P8b still
   owes (composite ring *arming*, bonded round-trip ingress) *and* the
   per-box arrival every remaining box has yet to make.
3. **A ring box silently narrows, or a wide box arms a narrow ring.**
   `ring_edge_width_ready` plus the reconciler's per-coupling emission
   are the belt; `check_camilla_playback_format` is the braces. Ring v2
   extends both rather than bypassing them. **Realized on jts3 2026-08-11**
   and closed by PR
   [#2335](https://github.com/jaspercurry/JTS/pull/2335) before any audio
   moved — the arm/rollback lifecycle section above names what sheared and
   which end the gate could not see.
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
   binder rejects. Exposure is per box and follows the `route_profile`: a
   box making no low-latency claim (jts3) has no binding to break, while a
   box that makes one carries a `usb_low_latency_48k` claim that goes red
   on that box the moment its artifact stops matching. jts.local is the
   known such box and its R6 owes the fresh artifact; an unprobed box's
   profile is unknown rather than clear — the Fleet rows above carry each
   box's dated probe state.
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

> **Amended by P6d.** Two sharpenings from building the lane. (1) The
> device flip is a THIRD shape, not the P6a/P6b env substitution:
> shairport reads a static conf, so `jasper-apply-airplay-mode` — already
> the conf's single writer from all four invocation sites, re-run at every
> unit start — became a lane-map READER of the already-rendered
> `JASPER_SHAIRPORT_DEVICE` line (never the armed set: the armed→device
> rule stays stated once, in `jasper/renderer_lanes.py`, and the bash side
> re-derives nothing). The unit deliberately carries NO
> `EnvironmentFile=` of the map and no device argv — one statement of the
> device, in the rendered conf, pinned so the conf-vs-argv double-statement
> drift class cannot creep in. The Tier-3 wedge supervisor's recovery
> restart runs the same `ExecStartPre`, so recovery follows the map for
> free. (2) The offset RE-derivation is per box and hardware-gated, so
> P6d ships the *visibility*, not new values: the formula's own terms are
> all downstream of fan-in and transport-independent in kind — what
> changes is the VISIBLE-delay half shairport disciplines against
> (honest ring occupancy vs the aloop fill lie), which is exactly why the
> empirical validation does not transfer. The provenance note in
> `deploy/shairport-sync.conf.template`, the `airplay` row's
> `arm_advisory` (printed by the arm CLI), and the doctor's
> armed-vs-rendered-conf coherence check carry that dependency to the
> operator; the re-derivation itself rides each box's per-lane source
> pass, per the risk register's top row.

**Per-lane clock reconciliation stays at fan-in ring-read.** The
one-rate-matcher-per-foreign-clock PLACEMENT does not move: the transport
changes, the reconciliation stays at fan-in's per-lane read. The ioplug is
a dumb frame carrier.

> **Corrected by P6a.** "Renderer lanes keep their `LaneResampler`" reads as
> though a renderer lane HAS one. None does — `lane_wants_resampler` is true
> only for the configured clock-crossing lane, which is the USB one, and that
> lane is `direct`, not a ring. A renderer's producer is DAC-paced through its
> own blocking write, so it needs no rate matcher. P6a's ring read path
> consequently renders slots straight into the lane buffer and never feeds
> `input.resampler`; arming both on one lane is REFUSED at config
> (`ConfigClassError`, so fan-in parks rather than reporting a resampler that
> reconciles nothing). If a ring lane ever does need one, that lane's own PR
> designs the placement — none of P6b–c did.

**ALSA conf shape per renderer.** Renderers emit native rates (AirPlay
44.1 k, BT variable), so each lane keeps its `plug:` wrapper layered over
a ring device — preserving `defaults.pcm.rate_converter` (the AEC HF-loss
history) and keeping renderer device names stable, so each flip is one
edit plus a restart rather than a unit-file change.

> **Amended by P6a.** The edit is an ENV edit, not a conf.d edit. A conf.d
> flip would rewrite an ALSA config block by regex while the fan-in half lived
> in a second file; instead both halves live in one file
> (`/var/lib/jasper/renderer_lanes.env`) with one writer, the renderer's
> `--device` reads it as `${JASPER_LIBRESPOT_DEVICE}`, and the conf.d ships
> static. What this paragraph actually promised is kept: the unit changes ONCE
> (to introduce the variable) and never again per box. And the restart is
> BOTH ends, not just the renderer — fan-in must re-read the lane map too.
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
wide-chain claim at that point was derived rather than re-probed (both
those rows were later replaced from hardware evidence — see the sixth pass
below). Appendix A was carried
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

A fifth pass (2026-08-11, the E7 ruling —
[#2335](https://github.com/jaspercurry/JTS/pull/2335)) rewrote the
lifecycle and wire-resolution paragraphs against the code it changed,
and nothing else: the wire-resolution section's "coherently narrow"
claim (the resolver took no per-box input at all until #2335; it now
reads `JASPER_FANIN_RING_WIRE_FORMAT`, the same key `jasper-fanin`
parses); the lifecycle's step-1 paragraph (the re-emit now moves BOTH
device halves, what it declares, and which gate refuses a shear); the
intermediate-safety paragraph's capture clause; and a new expected-warn
clause, whose claim was re-derived by building the plan for a
`hifiberry_dac8x` box under `shm_ring` rather than transcribed. All of
it comes from the changed source plus the jts3 arm-2 evidence
(`captures/r7b-jts3-arm2-20260811T132227Z`). Every other section
stands as last verified above.

A sixth pass (2026-08-11,
[#2346](https://github.com/jaspercurry/JTS/pull/2346)) reconciled the
**status, sequencing, and position** layers with the campaign's own record
(`captures/PLAN-ring-v2-rulings-2026-08-10.md`) and the merged PRs that record
cites — R7a [#2324](https://github.com/jaspercurry/JTS/pull/2324), R7b
[#2326](https://github.com/jaspercurry/JTS/pull/2326), the arm-waypoint fix
[#2329](https://github.com/jaspercurry/JTS/pull/2329), U2 PR-1
[#2330](https://github.com/jaspercurry/JTS/pull/2330), the shear fix
[#2335](https://github.com/jaspercurry/JTS/pull/2335), and the
endpoint-preservation fix
[#2343](https://github.com/jaspercurry/JTS/pull/2343). Every PR and issue state
in this pass was re-read live via `gh` rather than transcribed, and the jts3 and
jts.local figures were taken from the named evidence directories, not from the
record's summary of them. It rewrote the R-ladder's R7a/R7b rows, the U1 and U2
exit gates, the Fleet table and its new interim-restriction note, the follow-up
register, and the wire-resolution section's per-box reality; added E7 to
Ratified decisions; and trued up the three sentences jts3's arm falsified — the
USB source row's "so every box", the #2285 correction's "ring boxes stay
coherently S16", and the R1/R2 paragraph's "no conf.d on the fleet declares a
non-default format". The ACTIVE-ring arm/rollback lifecycle section was
deliberately NOT re-touched: #2329, #2335, and #2343 wrote it, and it stands as
their record. The same pass was then amended when the deploy-preserves-arm and
EQ-save proofs returned green: the armed-box restriction on deploys and `/eq/` /
`/sound/` saves is recorded as lifted, jts3's row carries its post-deploy build,
and #2345 joins the register — each read from
`captures/endpoint-deploy-jts3-20260811T185255Z` rather than from a summary of
it. A fix round after #2346's adversarial gate (0 blockers / 6 should-fixes)
then corrected four claims the campaign had outrun, each re-verified at its
source rather than from the finding: the splice watch signal is `empty_reads`,
not `epoch_resets` (`rust/jasper-ring/src/lib.rs` — `epoch_resets` counts writer
reattach and would stay flat through a recurrence; arm3's six samples all read
`empty_reads=0`); outputd admits `shm_ring` on **two** branches since R7b, not
one (`ring_active_ok` beside `is_full_range_stereo_lr_sink` in
`rust/jasper-outputd/src/config.rs`), which is the branch jts3 runs, so the
"aloop lane 5 is the only N-channel path" claim and risk 2's P9 rationale were
both rewritten; the R7a-before-R7b ordering is historicized as discharged; and
R-RING2's route-latency gate is now legible per box as met/owed/inapplicable
(jts3's `route_profile=corrected_48k` makes no low-latency claim — the doctor
says so in those words). The same round gave the arm's audible consequence a
home in jts3's row, added #2348, and moved the USB source-half claim to its
durable two-condition form after jts3's USB lane was enabled
(`captures/usb-enable-jts3-20260811T191749Z`). A host-occupancy micro-round then
deleted this file's remaining host-occupancy clauses outright: whether a host is
attached changes by the hour, which a day-dated document cannot hold honestly,
and the U2 arc row already owns the open question durably. The watch-rule round
then corrected it once more — it is the **conjunction**, not either
counter alone, because `saw_filled` is a reader-lifetime latch
(`rust/jasper-ring/src/lib.rs`), so a writer reattach's drain also lands in
`empty_reads` and a bare alarm would fire on every deploy; the arm3 capture's
own durability file is the worked benign case. That round also restored the
sub-period honesty caveat an earlier fix had deleted, made P9's hard gate
both-halves (capability *and* arrival), scoped "N-channel" to what has actually
run (2ch), and folded in two hardware results that landed while it was being
written: the jts3 USB hi-res validation, which settles Step 0's host-low-bits
question and met what the sweep round below then scoped to the gate's width
half, and the arm surviving an unclean
power pull — both from `captures/usb-hires-jts3-20260811T194132Z` and the enable
capture that precedes it, each read directly rather than from a summary. Two of
that round's changes went unrecorded here and are added now: the forced-ordering
paragraph's referent was corrected (`HIFIBERRY_DAC8X` carries the floor, so a
DAC8x box takes it by deploying), and risk 7's fleet-wide failure claim was
reconciled with per-box exposure, marking jts4 and jts5 unknown rather than
clear.

The sweep round, executed by a fresh closer agent against the reviewer's
full-file sweep, corrected two over-claims — how much of U2's exit gate the
hardware probe settled, and which instrument is authoritative for it — and tied
the remaining claims back to their sources. The 21-day uptime now cites the
capture that holds the raw reading rather than the one that summarizes it
(jts3's row says which, and why the hi-res capture's boot list cannot show
it). The floorless-profile illustration names
`INNOMAKER_HIFI_AMP_PRO` beside `HIFIBERRY_DAC8X_STUDIO`, because the registry
gives it no floor either and it is the profile jts4 and jts5 run — read out of
`jasper/audio_hardware/dac.py`, not inferred from the DAC8x family name. The USB
boundary row characterizes the probe instead of restating its decimal, which the
U2 arc row owns. U2's exit gate is scoped to the width half it proved, with the
arc explicitly not exitable while PR-2 and PR-3 are open, and the bit-pattern
fixtures are restored as the standing contract the dated probe corroborates
rather than replaces (`U2_HIRES_VECTORS` and its exit-gate test in
`rust/jasper-fanin/src/mixer.rs`, in-tree since PR-1). Risk 7's undated
jts4/jts5 tail becomes a pointer to the Fleet rows' dated probe state, the
shape the Gates section already uses. And two provenance stamps — the "Current
position" heading and the surviving-work table's `9cc41b987` — are scoped so
they no longer vouch for facts added after them.

A self-description micro-round then trued what these entries say about
themselves. The sweep round's "changed no facts" was false on its own terms
and now names the two over-claims it corrected. The forced-ordering paragraph
had inverted its own rule in its closing sentence: a floorless profile needs
its declaration and its soak before the ordering is *discharged* for it, not
before it binds. The round entries traded ordinals for descriptive labels —
they had drifted out of step with each other and collided with the pass
counter above, whose ordinals are cross-referenced and therefore stay. The
watch-rule entry now points forward to the scoping that superseded it instead
of restating a met gate; risk 7 points at the Fleet rows, which carry the
dated probe state its claim rests on; and the USB boundary row drops the last
number the U2 arc row owns, along with a "both halves" that collided with the
exit gate's width half. The egress and remaining source-half facts,
Appendix A, and Appendix B were not re-verified and stand as last verified
above.

Last verified: 2026-08-11
