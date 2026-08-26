# outputd — design of record and decision history (2026-05 … 2026-08) — historical

> **Status: historical.** The design pass that produced `jasper-outputd`: the
> DAC-agnostic active-output transport change set, the north-star/PipeWire
> analysis, the implementation specification and rollout plan, the dated
> decision record, and the per-pass revision log. Rules from it that still
> constrain the code were lifted forward into
> [HANDOFF-speaker-output-reference.md](../HANDOFF-speaker-output-reference.md),
> which is current operational truth; read this for *why*, and expect its
> "current" phrasing to describe the dated snapshot in that subsection.

## DAC-agnostic active-output transport (design-of-record)

> **Status: design-of-record, 2026-06-17 — Rust transport cleanup mostly built;
> hardware verification pending.** Finalized after a multi-agent design pass
> (3 architects + 6 adversarial critics) and an external hardware-grounded
> review. The Stage-7 outputd cleanup now routes single ALSA and paired composite
> through one `run_alsa` loop; Linux/ALSA and dual-Apple hardware regression are
> still the required proof. This is the canonical transport design for active
> crossover; the commissioning flow that rides it is in
> [HANDOFF-active-speaker-dsp.md](../HANDOFF-active-speaker-dsp.md) "Single audio
> path commissioning". The principle that governs every line below: **dispatch
> on clock-domain *shape* (single coherent device vs paired composite), with
> channel width and the channel map as DATA from the `DacProfile`/topology — never
> a per-DAC code branch.** Adding a DAC of an established shape is a `DacProfile`
> row; a new shape pays transport code once.

### One path, and why outputd stays in it

`fan-in (stereo) → CamillaDSP (the sole 2→N width fan-out) → outputd (reads the
N-channel content lane, demuxes to physical DAC channel(s), publishes the AEC
reference) → DAC`. CamillaDSP owns all width/EQ/gain/delay/limiter authority
(`emit_active_speaker_*` already sizes `playback channels: {output_count}`).
outputd stays the final owner because (a) a *composite* DAC needs an aggregator
that CamillaDSP — which targets one ALSA device — cannot be, and (b) outputd
owns the AEC reference, the playout ledger, and clip accounting. **TTS/cues are
NOT an outputd concern in active mode:** they enter at fan-in (stereo,
pre-CamillaDSP — `jasper-voice.service` `JASPER_TTS_OUTPUTD_SOCKET` →
`/run/jasper-fanin/tts.sock`), so voice rides the crossover/protection chain at
every width. The active loop therefore needs **no** TTS lane; `OutputCore`/the
TTS mixer stays conditional on `tts_socket_path` being set and is fail-closed to
stereo single-ALSA output. The old dual/composite loop gap was **real clip
accounting** (it hardwired `clipped_samples=0`) plus sharing the same
reference/state path as single ALSA; the Stage-7 cleanup moved composite into
the unified `run_alsa` loop so both sink shapes now record the written period's
full-scale sample count.

### The transport debt this change paid down

The original active-lane transport, `DualAppleBackend`
(`rust/jasper-outputd/src/alsa_backend.rs`), was welded to two stereo USB DACs:
two child PCMs, `snd_pcm_link`, inter-DAC drift tracking,
`deinterleave_4ch_to_dual_stereo` (ch0/1→DAC A, ch2/3→DAC B). Stage 1 renamed the
shape to `PairedCompositeSink` and `SinkMode::Composite` while keeping the
`dual_apple` wire value stable; Stage 7 removed the separate runtime loop and
wrapped `PairedCompositeSink` behind `RuntimeAlsaSink` beside the coherent single
`AlsaBackend`. The pair remains exactly two children. M>2 composite output is
still out of scope.

### The change set (build to this)

**1. Transport dispatches on clock-domain shape via a small runtime sink
boundary.** One loop body serves both widths; both get the state + reference +
clip path:

```rust
enum RuntimeAlsaSink {
    Single(AlsaBackend),
    Composite(PairedCompositeSink),
}

impl RuntimeAlsaSink {
    fn content_channels(&self) -> u16;
    // `ProgramSample` = i32 since the wide-path spine landed; these read and
    // write outputd's internal program, not a wire format.
    fn read_content_period(&mut self, out: &mut [ProgramSample]) -> Result<usize>;
    fn write_period(&mut self, samples_nch: &[ProgramSample]) -> Result<()>;
    fn start(&self) -> Result<()>;
    fn dac_delay_frames(&self) -> Result<u64>;
    fn mark_runtime_status(&self, state: &OutputdState);
}
```

- `AlsaBackend` = today's single backend with the **DAC-write** `CHANNELS=2`
  literals replaced by runtime `dac_channels` at the enumerated write sites only
  (`alsa_backend.rs` open + `write_dac_period` framing). Content-read framing
  follows the same runtime width. **Width 2 is byte-identical to today.** Covers
  single Apple (2ch), DAC8x (8ch), any future coherent single DAC — zero per-DAC
  code.
- `PairedCompositeSink` = the renamed dual-Apple transport behind the same
  boundary. It keeps the existing A/B child PCM env, `snd_pcm_link`,
  delay-divergence guard, and fail-closed runtime-health behavior. **Stays
  two-child** — a pairwise drift guard cannot be half-`Vec`-ified. M>2 composite
  is a genuinely *new* sink impl (explicitly out of scope; named in the
  active-speaker doc), not a config row.
- **No `single_alsa_multi` sink string.** Width is already carried by
  `active_outputd_lane_channels`; a second "is this wide?" field invites drift.
  `config.rs` keeps `SinkMode { SingleAlsa, Composite }` (rename `DualApple` →
  `Composite`; keep `"dual_apple"` as a parse alias one release). `dac_channels`
  reads `JASPER_OUTPUTD_ACTIVE_CHANNELS` (validated `2..=8`; **required** for a
  wide single DAC — fail-closed `EXIT_CONFIG`/78 if unset; the reconciler always
  emits it from `active_outputd_lane_channels`). `types.rs CHANNELS=2` stays as
  the reference/content-read/chip width.

The loop body is now a single `run_alsa` over `RuntimeAlsaSink`: read N-channel
content → `write_period` → mark sink runtime status → read DAC delay → publish
the correct reference fold → `state.mark_period(..., clipped)`. The old
`run_alsa_dual_apple` fork and `downmix_dual_active_reference` helper were
deleted in the Stage-7 cleanup.

**2. The AEC reference is mono — verified — so the fold is trivial.** Both
consumers collapse the reference to mono: software AEC3 sums L+R→mono
(`aec_bridge.py` "L+R summed to mono"); the chip-AEC USB-IN producer downmixes
(`main.rs` `chip_ref_downsampler_downmixes_and_decimates`, the XVF USB-IN being a
2ch endpoint fed the downmixed signal — `HANDOFF-xvf3800.md` §3). **No consumer
uses L vs R separately.** Therefore:
- `fold_reference` sums **all driven active lanes** into one mono signal, then
  publishes it into the existing stereo reference (L = R) so the published
  contract (`speaker_reference_channels: 2`) is unchanged and the bridge/chip
  producer are untouched. There is **no per-DAC L/R fold to author** — the driven
  set is derived from the CamillaDSP output channel count / topology (single
  source of truth; see §data-model).
- **Clip-proof scaling.** Scale the sum by **1/N** (N = number of driven lanes):
  N correlated full-scale lanes sum to N×, so 1/N guarantees the result stays in
  range regardless of correlation. (`1/√N` is power-preserving only for
  *uncorrelated* lanes — a woofer+sub share LF, L/R are correlated — so it can
  still clip; a clipped reference is uniquely harmful because the linear AEC
  cannot model the nonlinearity.) Accumulate in `i32`; the AEC adapts its own ERL
  so the lower level costs nothing. The pairwise composite reference path is now
  named `fold_reference_pairwise_composite` and stays byte-identical to the old
  `downmix_dual_active_reference`: `[avg(ch0,ch1), avg(ch2,ch3)]` per frame
  (regression test asserts equality); the N-lane path is the new clip-proof sum.
  *Precondition note:* `1/N` is clip-proof **absolutely** (not relying on
  band-splitting). Band-splitting is why the reference rarely approaches the `N×`
  worst case — at any instant one lane is hot and the sum stays well below
  full-scale — so `1/N`'s conservatism costs no real SNR. Do **not** "optimize"
  back to `1/√N`: it is power-preserving only for uncorrelated lanes and would
  reintroduce the clip hazard the moment a future mode routes full-range content
  to multiple lanes.
- **Match the fold to what the mic can hear (don't normalize on inaudible
  energy).** A reference dominated by sub energy the mic can't pick up inflates
  the NLMS denominator without contributing correlation, slowing convergence in
  the voice band. The software AEC3 path **already** high-passes the reference at
  125 Hz (`aec_bridge.py`), so sub content is already out of *its* denominator;
  the open item is the **chip** path — verify the XVF3800 USB-IN reference band
  and the mic-array low-frequency roll-off, and high-pass the fold to match mic
  sensitivity if needed. The XVF exposes only **2** reference channels (not 3),
  so a separate sub reference is impossible — this is a "what goes into the sum"
  question, which is why the mono fold is the right shape.

**3. Reconciler computes one `OutputTransportPlan`; it dispatches on `kind`, not
DAC id.** `apply_audio_runtime_env()` reads the resolved `DacProfile`:
- `kind == "single"` with an active lane → `JASPER_OUTPUTD_SINK=single_alsa`,
  `JASPER_OUTPUTD_ACTIVE_CHANNELS=<active_outputd_lane_channels>`,
  `CONTENT_PCM=outputd_active_content_capture`, `DAC_PCM=outputd_dac`. No
  child-PCM env, no composite policy.
- `kind == "composite"` → `SINK=composite` + the child PCMs from
  `dac_channel_map`; the existing `apply_observed_composite_policy` (serial-pinned
  A/B order, drift evidence) runs **only here**.

*(P9-C note: `CONTENT_PCM=outputd_active_content_capture` named a live aloop
PCM when this was written. P9-C deleted that pair's definitions once the
ACTIVE ring became the roleful transport; an armed box now reads the ring
instead, and this env value is what an unarmed/rolled-back box still gets —
opening it now fails and outputd parks. See
[audio-paths.md](../audio-paths.md).)*

The `OutputTransportPlan` (`sink`, `transport_channels`, `channel_map`,
`dac_pcms`, `clock_domain_contract`) is the **single env+`/state` truth**,
computed once and *read* on `/state` — never re-derived per `/state` hit.

**Stable identity + invalidation (a Stage-0 decision, not a later fix).**
`dac_pcms` and `clock_domain_contract` are exactly the fields that shift when a
USB DAC re-enumerates or the Apple dongles return with different card indices
across a reboot — a class of drift that has bitten JTS before
([HANDOFF-identity.md](../HANDOFF-identity.md)). So the plan MUST key on a **stable
card identifier** — `hw:CARD=<name>` (the `DacProfile` already matches on card
*name* regex, not index), or a serial where available — **never a numeric card
index.** With `type plug` banned, a stale plan pointing at the wrong device now
fails *closed* (silent until reconcile re-runs) rather than playing remixed
content at the wrong drivers — but the cure is to not go stale: the reconciler
(the single writer) recomputes the plan on **boot and on udev add/remove/change**,
the same triggers it already self-heals on. Bake stable-identifier resolution
into the Stage-0 resolver relocation onto `OutputTopology` — that is the cheapest
place to get it right and the most expensive to get wrong later.

> **Stage 0.3 landed (Python data model + stable identity).** `OutputLayout` /
> `OutputTransportPlan` + `resolve_output_layout` live in
> [`jasper/output_topology.py`](../../jasper/output_topology.py); the active-speaker
> resolvers and `ActivePlaybackRouteCapability` are thin readers of them. Every
> physical-DAC PCM is built by the single `stable_card_pcm` chokepoint
> (`hw:CARD=<name>`), and `is_stable_card_pcm` rejects numeric-index / `plug` /
> `plughw` forms at the `OutputTransportPlan` boundary, so the card-index drift
> class fails closed before the Rust transport (Stage 1) and the reconciler env
> emission (Stage 2) ride the plan. The plan is recomputed fresh from the topology
> per call (no cached index); wiring the *udev/boot env emission* of it is Stage 2.

> **Stage 2a landed (reconciler env + wide content lane + width gate + DAC8x
> profile flip).** `jasper-audio-hardware-reconcile`'s `apply_audio_runtime_env`
> emits the wide single env (item 3) — `JASPER_OUTPUTD_SINK=single_alsa`,
> `JASPER_OUTPUTD_ACTIVE_CHANNELS=<active_outputd_lane_channels>`,
> `JASPER_OUTPUTD_CONTENT_PCM=outputd_active_content_capture` — for a recognized
> coherent single DAC **only when the active-speaker runtime contract proves the
> already-loaded endpoint graph**. For solo active that endpoint is the graph in
> `outputd-statefile.yml`. For an active leader, `outputd-statefile.yml` may be
> the safe `program_bake_pipe` (`File`→`SNAPFIFO`, not a DAC); in that case the
> gate follows `crossover-statefile.yml` and requires the camilla#2 graph to be a
> re-proven `driver_domain_baseline` targeting `outputd_active_content_playback`.
> Any missing/unsafe/wrong-device/over-cap paired graph fails closed to the
> byte-identical stereo path. The gate **drives what we use**: it reads the live
> endpoint config's actual playback width W, accepts `2 ≤ W ≤ cap`
> (`active_outputd_lane_channels`), and emits **that W** as
> `JASPER_OUTPUTD_ACTIVE_CHANNELS` (a managed var cleared in every non-active
> branch). A DAC8x running a 2-way drives 2 outputs, an 8-driver speaker drives
> 8 — outputd opens the DAC at W. The active content lane (item 4) is raw
> `type hw` — card/device/subdevice only, exactly like the `outputd_dac` block;
> the ALSA `hw` plugin rejects `channels`/`rate`/`format` as unknown fields, so
> the width is set by the openers and locked by snd-aloop, with
> `type plug`/`plughw:` banned. The DAC8x/DAC8x-Studio `DacProfile`s declare the
> active lane (item 6, `supports_active_outputd_lane=True`; the Apple USB-C
> dongle and the InnoMaker HiFi AMP Pro each declare
> `active_outputd_lane_channels=2`, DAC8x/DAC8x-Studio declare
> `8`, and the Stage 1 transport carries any width ≤ cap). Because the gate
> accepts the config's actual width, the existing
> per-speaker emitters (which emit the driver count) engage active mode directly
> — **no full-width-padding producer is needed.** **Load-bearing hardware fact
> (verify on jts3 at Stage 3/4):** outputd opening the DAC at W < its physical
> channel count must succeed and idle the undriven outputs safely; if a future
> DAC requires native-width opens, that becomes a per-DAC `DacProfile` property,
> not a reason to pad universally. **2b landed:** the masked commissioning emitter
> is wired into staging — `stage_protected_startup_config` stages the production
> graph with `audible_outputs=frozenset()` (the all-muted boot config), the
> software guard proves the tweeter is muted via its per-output
> `as_out{idx}_commission_mute`, and a `staged_candidate_fully_muted` gate
> enforces crash-recovery-MUTED on every staged boot config — see
> [HANDOFF-active-speaker-dsp.md](../HANDOFF-active-speaker-dsp.md)
> critical-path step 2.

*(P9-C note: this Stage 2a design routed a single recognized DAC's active
content over a raw `type hw` aloop substream at width N — snd-aloop pair 5.
P9-C deleted that substream's PCM definitions once the ACTIVE ring became
the roleful transport, so this whole raw-hw-aloop shape (including items 4-6
below) is now the pre-arm/rolled-back state, not the armed one; an armed
box's content instead crosses Ring B. See
[audio-paths.md](../audio-paths.md)
for current behavior.)*

**4. One wide snd-aloop content substream — width on the substream, not more
substreams.** The kernel caps loopback substreams at `MAX_PCM_SUBSTREAMS=8` (you
cannot raise that without patching the module); but one substream carries up to
`channels_max=32` and adopts the playback side's channel count. So the fix is to
make the active content lane **one substream at width N**, not to add substreams:
- Render the active-content lane width from `JASPER_OUTPUTD_ACTIVE_CHANNELS`.
  *(2a implementation note: a `__OUTPUTD_ACTIVE_CONTENT_CHANNELS__` render
  token was the original plan here but was never implemented in
  `asoundrc.jasper` — the ALSA `hw` plugin rejects `channels`/`rate`/`format`
  as unknown fields, so the active lane shipped as plain `type hw`
  card/device/subdevice instead, with its width set by the openers
  (CamillaDSP's `playback: channels: N` and outputd's
  `JASPER_OUTPUTD_ACTIVE_CHANNELS`) and locked by snd-aloop, not pinned in
  the conf. See the "Stage 2a landed" callout above. P9-C later deleted this
  substream pair's PCM definitions entirely once the ACTIVE ring became the
  roleful transport — see
  [audio-paths.md](../audio-paths.md).)*
- **All format adaptation is explicit and owned by CamillaDSP; the active ALSA
  path fails closed on channel, rate, AND format mismatch.** Ban `type plug`
  (and `plughw:`, which is `plug`+`hw`) on the active path — use width-exact
  `hw:`/`dmix` so any mismatch fails at `snd_pcm_hw_params` (open error) instead
  of silently remixing 8→4 (or resampling, or reformatting) onto live drivers
  (the single most dangerous fail-open in the feature; `plug` is the automatic
  channel/rate/format-conversion plugin). A contract test rejects `plug`/`plughw`
  anywhere on the active path.
- **Second, independent fail-closed layer:** CamillaDSP refuses to start if its
  mixer output channel count ≠ the playback device `channels`. Rely on both.
- **Open-ordering constraint:** snd-aloop locks rate/format/channels to the first
  opener of a substream pair, so the active-content playback (CamillaDSP) and any
  reference capture on the paired substream must agree on width; document the open
  order. If PipeWire/PulseAudio is present it may grab the loopback device —
  out of scope for the appliance, noted for dev hosts.

**5. Width-aware cutover gate — drive what we use, not the DAC's full width.**
Replace the `channels: 4` grep with a capacity check: the active config's
**actual** playback width W must be a valid active width **within the DAC's
cap** (`2 ≤ W ≤ active_outputd_lane_channels`), and the reconciler emits that
**actual W** as `JASPER_OUTPUTD_ACTIVE_CHANNELS` so outputd opens the DAC at
exactly W. `active_outputd_lane_channels` is the **cap** (the most outputs
outputd will drive on this DAC), not a fixed width — a 100-output DAC powering a
2-way drives 2, never 100. This matches the `<=` model
`ActivePlaybackRouteCapability` already uses (`required_active_output_count <=
transport_channel_count`); a config wider than the cap fails closed. Renamed
`active_four_channel_shape_missing` → `active_graph_width_out_of_range got=W
cap=N`. (Earlier drafts used a fixed `== transport width` gate that would have
forced narrow speakers to pad to the DAC's full channel count with muted lanes;
rejected — see the "Stage 2a landed" callout above.)

**6. `DacProfile` additions (pure data, IO-free, fail-closed at import).**
- `connection: "usb" | "i2s"` declares which host interface the final-output
  DAC consumes. I²S profiles must declare their registered `dtoverlay`; USB
  profiles cannot. The USB-role resolver consumes this data without growing a
  per-DAC branch.
- `dac_channel_map: tuple[ChannelMapEntry, ...] | None` — `(camilla_out_index,
  physical_dac_channel)` permutation. **No gain field** (CamillaDSP owns gain).
- `is_coherent_single() -> bool` predicate (folds `kind=="single" and
  coherent_clock_domain`). **Device resolvers move to `OutputTopology`**, not onto
  the IO-free registry (they read env + card_id, which would break `dac.py`'s
  contract). No `reference_fold` field — the driven-channel set is the fold input
  and is derived from the topology/CamillaDSP output, validated against the
  active lane width at import.
- **Profile flip is last:** set `supports_active_outputd_lane=True` +
  `active_outputd_lane_channels` on a DAC only once the transport above can carry
  it, so the Python route never resolves a lane the transport can't serve.

### Resilience (every failure: detect → fail-closed → observable)

- **Composite child loss:** the fail-closed action is a **bail**, never a mute
  — no mute-all-children path exists in the tree. A child that actually
  *disappears* is caught on the write path, but **not** by the next bullet's
  recovery ladder: that ladder is the xrun path and `write_dac_fail_closed`
  enters it only on `EPIPE`/`ESTRPIPE`, so a vanished device's
  `ENODEV`/`ENXIO` takes the bare propagate beneath it and emits no
  `event=outputd.xrun` line of its own. Don't read an *absent* xrun line as
  "no removal" — nor a present one as "just an xrun": a removal can surface
  first as an `EPIPE` underrun, which logs the xrun before `try_recover` fails
  on the now-absent device, since that `eprintln!` precedes the recovery
  attempt.
  Recovery is out-of-band: udev → `jasper-audio-hardware-reconcile` clears
  `JASPER_OUTPUTD_DUAL_DAC_A_PCM`/`_B_PCM` and acts on outputd. Where it lands
  is decided by the **saved** topology, not by what survived: a saved
  **roleful** composite **parks** the box (`JASPER_OUTPUTD_BACKEND=fake`)
  behind a named `saved_composite_partially_present` blocker rather than
  running the survivor as a stereo DAC — owner is
  [`jasper/output_hardware.py`](../../jasper/output_hardware.py)
  `apply_saved_topology_policy`. Rolefulness, not `kind == "composite"`, is the
  gate: a *passive* composite may legally place every declared speaker on one
  child's outputs, so losing the other child must not park a working stereo.
  That box — and any box with no saved composite — rewrites
  `JASPER_OUTPUTD_SINK=single_alsa` for the surviving DAC and restarts outputd.
  The reconciler half of that is shipped code; the end-to-end convergence is still
  **awaiting its on-Pi pass** — it is item 5 of
  [HANDOFF-hotplug-resilience.md](../HANDOFF-hotplug-resilience.md)'s "Needs a
  real plug/unplug hardware pass" list, so read it as intent, not as a result.
  The in-loop PCM-state check is narrower than it looks, and is **not** the
  primary child-loss detector: it lives in
  `PairedCompositeSink::check_delay_delta`
  (`rust/jasper-outputd/src/alsa_backend.rs`), runs **after** each period's
  write in `write_dual_period`, and is entered only when both children already
  read `State::Running` — so what it catches is the race where a child leaves
  `Running` between that gate and the two `snd_pcm_delay` reads, raising
  `outputd dual Apple bad PCM state: dac_a=… dac_b=…`. That error propagates
  out of `run_alsa` to `main` and, not being config-class (which would exit 78
  into `RestartPreventExitStatus`), exits non-zero onto the same
  `Restart=on-failure` → `StartLimitBurst=5` → `StartLimitAction=reboot` ladder
  the next bullet's bails ride. The observable surface is the
  `/state.dual_apple` block (rendered only when `sink_mode == "dual_apple"`):
  `dac_a_pcm`, `dac_b_pcm`, `linked`, `delay_delta_frames`,
  `delay_delta_baseline_frames`, `delay_delta_error_frames`,
  `max_delay_delta_frames`, plus the recovery counters the next bullet names.
  **Never built — do not go looking:** a `sink.health()` API and an
  `event=outputd.composite.child_lost` line. This bullet asserted both, plus a
  mute-all-children response, as current truth until 2026-08-20. Nothing in the
  tree asks for any of the three, and mute-all is incompatible with the
  reconcile-to-`single_alsa` convergence above, so they are retired here rather
  than filed as work. (That #2255 *superseded* them is an inference, not a
  recorded decision — #2255's scope is the bail-on-first-xrun fix alone, and it
  never mentions child loss.) **Still owed, by contrast:** the per-child array
  under a width-agnostic `composite` `/state` block. The Observability section
  below prescribes it and `SinkMode::as_str` defers it as "a separate change",
  so that container is live work — only the `.state` leaf this bullet used to
  spell is unprescribed. **Real gaps remain, and there are at least four:** the
  divergence branch logs `event=outputd.dual_apple.delay_diverged` before it
  bails, and the reprime branches log
  `event=outputd.dual_apple.reprime`, but four bails are silent — the
  bad-PCM-state one beside the divergence check; `start_dacs`' group-start
  refusal ("did not both enter Running state", which is the docstring's "a
  group start that does not reach `Running`" the next bullet points at); and
  the two child-write bails, a repeated `Ok(0)` ("writei returned 0 frames
  repeatedly") and the non-`EPIPE` `writei` propagate that a removal takes.
  `write_dac_fail_closed` carries exactly one `eprintln!` in its whole body,
  the xrun line, so every other exit from it is silent, and `start_dacs` has
  none at all; `run_alsa` propagates with a bare `?` and `main` logs only
  config-class errors, so nothing upstream rescues them. Those faults are
  visible only as the bail message and the restart — this section's
  "observable" promise is the composite's weakest, not a single missing line.
  Child-presence gating is the
  **reconciler's**, not the unit's: `dual_apple_runtime_mapping`
  (`jasper/output_hardware.py`) refuses unless exactly two child devices with
  PCMs resolve, while the single `ExecCondition` on `jasper-outputd.service`
  tests only the one resolved `JASPER_AUDIO_DAC_CARD` under `/proc/asound`.
- **Unified xrun policy (#2255).** One recovery budget, `xrun_policy` in
  `alsa_backend.rs`, is shared by the coherent single sink's `write_dac_frames`
  and the composite's child write: three recoveries per period, then bail.
  `Ok(0)` rides the same budget on both paths. The composite bails when the
  recovery ladder cannot complete a rung, or when the pairwise delay-divergence
  guard trips (`write_dual_period`'s docstring enumerates every rung; that
  guard's own function also bails on a child that has left `Running`, per the
  bullet above) — never on the first xrun, which used to exit 1 into
  `Restart=on-failure` → `StartLimitBurst=5` → `StartLimitAction=reboot`.
  **The recovery is the LINK GROUP's, not per-child.** `dac_a.link(&dac_b)`
  makes `snd_pcm_recover` a group prepare, so the composite recovers once, then
  re-primes BOTH children to `prime_periods()` depth through the same
  interleaved child write the run loop uses, then calls `start_dacs()` as an
  explicit group start. The prime depth stays one period below
  `start_threshold` on purpose: a full-buffer refill would auto-start the group
  the moment child A filled, before child B was primed, baking in up to a full
  period (128 frames ≈ 2.667 ms at 48 kHz) of permanent A/B skew — which on an
  active 2-way IS the woofer/tweeter time alignment. The pairwise baseline is
  then re-latched under a MAGNITUDE bound (`dual_max_delay_delta_frames`, the
  same constant the steady-state guard uses); a pair that comes back outside it
  fails closed instead of blessing the offset. An **unlinked** pair keeps the
  bail: with no atomic group restart its post-recovery skew is unbounded and
  unverifiable, which is also why `link=ok` is an arm-time precondition for a
  composite on the SHM ring (park-class refusal — it keeps the loopback
  transport). Per-child attribution is `event=outputd.xrun
  source=dual_dac_a|dual_dac_b`; the re-prime reports
  `event=outputd.dual_apple.reprime
  status=ok|alignment_refused|xrun_during_reprime` (the third is an xrun on a
  pair the recovery had just prepared — fail-closed, never a second nested
  recovery); and
  `/state.dual_apple` carries `dac_a_xruns` / `dac_b_xruns` /
  `group_recoveries` / `delay_baseline_relatches` /
  `reprime_alignment_failures`. Sink-level `dac_xrun_count` is unchanged.
  Persistence beyond one period belongs to the restart ladder: a composite that
  exhausts its budget on every start rides `Restart=on-failure`, where a
  repeated content-lane open failure instead gets parked out-of-band on its 4th
  by `jasper-outputd-failure-reconcile`. That helper has one failure class, the
  content lane's, and its 4-of-5 bound is derived from a startup wait that a
  runtime xrun does not have.
- **Width mismatch (CamillaDSP N vs outputd M):** two shapes, two exits.
  When the lane *installs* a width other than the one outputd requested, the
  content readback carries `FinalSinkStartupConfigError` → `EXIT_CONFIG`/78,
  parked, no crash-loop. When the lane *refuses* the request outright — a raw
  active lane whose peer already locked the pair — `hw_params` fails and the
  open exits 1 on the ordinary restart ladder, because that same failure is how
  outputd waits for CamillaDSP on every boot. Belt-and-suspenders on both since
  the two openers derive from one `OutputTransportPlan`.
- **Initial final-sink open/negotiation failure:** configuration-class exit 78,
  parked instead of consuming the restart/reboot budget. Content-capture
  startup and later runtime faults keep their retryable semantics up to a
  bound: four consecutive content-lane open failures are parked out-of-band by
  `jasper-outputd-failure-reconcile` rather than reaching
  `StartLimitAction=reboot` (see the restart-policy section above).
- **DAC hotplug:** reconciler re-derives on udev (pattern-3 self-heal); replug
  re-arms **muted** via the masked startup config.
- **Config-shear during DAC re-enumeration:** the reconciler stages and validates
  `outputd.env` buffer/period pairs (content and DAC buffers) before replacing
  the prior file. If outputd still exits 78 from a transient hotplug shear, the
  failure helper runs one bounded
  `jasper-audio-hardware-reconcile --no-restart` pass and no-block retries
  outputd; a repeated exit 78 parks instead of looping into reboot policy.

### Observability

- **Real clip accounting at every width** — the Stage-7 cleanup removed the
  hardwired `clipped_samples=0` composite path. A clipping active period now
  reports nonzero on `/state` for single and paired-composite output alike (the
  commissioning "no clip" gate is otherwise vacuously green).
- **Width-agnostic `/state` block — decouple the wire string from the Rust type
  name.** The serialized `/state` value is a cross-language contract (Rust
  `state.rs` writes it; the Python doctor reads it). Renaming the internal type
  (`DualAppleBackend`→`PairedCompositeSink`, `SinkMode::DualApple`→`Composite`)
  must **not** be coupled to a serialization-format break. Either keep the wire
  value stable while the type is renamed, or migrate every occurrence in one
  atomic commit guarded by a **round-trip (serialize→parse) test**; either way
  rename the block to a width-agnostic `composite` shape with a per-child array
  and keep `dual_apple` as a **read alias** one release, migrating the doctor's
  `=="dual_apple"` branches + the snapshot test in the same PR.
- **"Why didn't my lane arm" via stable `issues[].code`** on `OutputHardwareState`
  (not bare stderr). `check_outputd_service` becomes table-driven keyed by
  `sink_mode` (today's 2-mode allowlist would FAIL every new DAC); the width check
  diffs reconciler-resolved width against outputd's negotiated `dac.channels`.
- **Edge-triggered hot-loop events with `*_count` companions** — no per-period
  logging in the sink loop (a flapping child must not emit 48000 lines/sec).

### Performance / resource (1 GB Pi)

- **Zero allocation on the DAC-write hot path at any width** — preallocated
  per-child period buffers; preallocated fold scratch. Test mirrors
  `steady_state_reuses_segment_write_buffer_capacity`.
- **`OutputCore`/reference sequence tracking/ledger-loudness stay conditional
  on TTS** — a solo stereo speaker allocates none of it; the minimal clip/ledger
  counters the active loop needs are cheap scalars, not the full `OutputCore`.
- **Composite drift-sync cost gated to composite** — `SingleAlsaSink` pays
  nothing; M=2 keeps exactly today's 2 `snd_pcm_delay` ioctls/period.
- **No new threads, no new poll loops, no new resident process** — reconciler
  reuses the existing boot/udev shell-out; N-channel passthrough is O(frames×N).
- **Pi-5 RP1 multichannel I2S headroom:** CamillaDSP-Nch + outputd + voice + AEC
  on one Pi 5 is plausible but not free (documented RP1 XRUNs at high rates under
  load). Budget 48 kHz + a comfortable period; **load-test at Stage 3/6** and
  state the per-SKU channel ceiling (a Pi Zero 2W cannot do Nch DSP + AEC). This
  is monitored on `/system`, not CPU-capped (per the JTS "visibility over
  constraints" stance).

### Safety + verification (jts3, real bi/tri-amp speaker, live drivers)

`volume_limit: 0.0` holds in the active config; per-driver limiters and the
protective tweeter high-pass live in the CamillaDSP graph. Verify on jts3 in the
staged order in the active-speaker doc, starting muted, unmuting one output at the
calibration floor woofer-first/tweeter-last, with a **live high-pass-presence
assertion** before the tweeter is unmuted. Single-DAC has no drift/link to soak;
the stereo↔active cutover and xrun behavior do.

## Problem Boundaries

This is primarily an output problem, not an input problem.

Future record-player, HDMI, USB, AirPlay, Spotify, Bluetooth, or
network sources are content inputs. They should enter the content
graph once, flow through source policy and DSP, and automatically
become part of the final speaker-output reference.

The output-reference problem starts later:

```text
all audible program material -> final mix / protection / DAC write
                              -> exact speaker_output_reference
```

The reference must represent what the speakers were asked to emit
after source selection, ducking, TTS gain, cue gain, safety clamps,
and any future output protection. It should not be reconstructed by
summing several delayed side channels inside the AEC bridge.

## North Star

The long-term architecture is a small JTS-native output owner,
provisionally named `jasper-outputd`:

```text
CONTENT PATH
  renderers -> jasper-fanin -> jasper-camilla
                                      |
                                      v
                              content_post_dsp
                                      |

ASSISTANT PATH                       v
  TTS / cues / chirps -------> jasper-fanin -> jasper-camilla -> jasper-outputd
                                  |
                                  +--> speaker_output_reference
                                  |      -> AEC bridge
                                  |      -> wake/corpus capture
                                  |      -> telemetry/debug consumers
                                  |
                                  +--> tts_playout_ledger
                                         -> realtime truncation
                                         -> barge-in decisions
                                         -> corpus provenance
```

`jasper-outputd` should be boring on purpose. It is not a desktop
audio server. It owns exactly the final JTS speaker boundary:

- read the post-Camilla content stream
- accept assistant/cue/chirp PCM from the voice daemon
- apply final mix, clamps, and future speaker-protection processing
- write to the physical DAC
- publish one coherent `speaker_output_reference`
- report playout ledger events for realtime-model turn state

Once this exists, the AEC bridge consumes `speaker_output_reference`
instead of the pre-Camilla content tap. Barge-in during assistant
speech becomes structurally possible because the echo canceller sees
the assistant audio the microphone is hearing.

## Why This Is Better Than The Fan-In Mirror

The abandoned fan-in mirror would have improved one local failure
mode: AEC/corpus/debug consumers contending with Camilla on a shared
`dsnoop` surface during heavy corpus collection.

It would not have solved the product-level problem:

- it still excluded TTS/cues/chirps
- it was still pre-Camilla
- it did not know what actually drained to the DAC
- it could not drive realtime truncation decisions
- it did not help future active speaker protection

That makes it a useful investigation artifact, not the architecture.

The output-owner direction solves the deeper problem once, at the
right boundary. It gives us a single point where "audible output" is
mixed, measured, protected, referenced, and accounted for.

## What To Take From PipeWire

Do not implement PipeWire as a dependency for JTS. The daemon,
session manager, compatibility layers, arbitrary dynamic graph,
desktop hotplug policy, and plugin surface are far larger than this
appliance needs.

The useful lessons are smaller and specific:

- **Node / port / link vocabulary.** Treat `content_in`, `tts_in`,
  `dac_out`, `speaker_reference_out`, and `telemetry_out` as explicit
  ports even if they are implemented with ALSA plus Unix sockets.
- **One timing driver per graph.** The DAC write loop should drive the
  final output graph. Optional consumers must not become playback
  timing owners. PipeWire's graph scheduler describes this explicitly:
  a driver node starts each cycle, and dependent nodes run only when
  their upstream dependencies complete.
- **Async side consumers.** AEC, corpus, and debug readers receive
  failure-isolated copies; the software UDP monitor is nonblocking and the
  optional chip-reference writer has its own bounded queue/retry state. If a
  reference output falls behind or disappears, it drops/counts reference
  periods rather than blocking playback. PipeWire's async links use the same
  idea and add a cycle of latency rather than putting side work in the
  synchronous graph completion path. Multi-room does not use an outputd side
  consumer: the leader's CamillaDSP writes the snapserver pipe and member
  outputd consumes snapclient's explicit FIFO lane. See
  [HANDOFF-multiroom.md](../HANDOFF-multiroom.md) §2.
- **Explicit ring semantics.** Use bounded storage, monotonic sequence
  numbers, underrun/overrun counters, and clear drop policy rather
  than hidden buffering. PipeWire's `spa_ringbuffer` is only two
  atomic indices over caller-owned memory, and its read/write helpers
  explicitly report underrun/overrun conditions.
- **Rate matching at clock boundaries.** A loopback capture clock and
  a physical DAC clock can both be nominally 48 kHz while drifting by
  tens of ppm. The production shape is not "make the ALSA buffer huge";
  it is an explicit bridge with a target fill, a low-bandwidth
  controller, a high-quality variable-rate resampler, and counters for
  clamp/underrun/overrun/resync behavior.
- **Four-stream AEC shape.** Echo cancellation is easiest to reason
  about when playback/reference and capture/cleaned-mic streams are
  explicit surfaces, not incidental taps.
- **Small backend interfaces.** Keep AEC engines and output transports
  behind narrow traits/interfaces so WebRTC AEC3, future engines, ALSA
  hardware, and test fakes are swappable without changing topology.

What not to take:

- WirePlumber/session-manager policy
- PulseAudio/JACK compatibility
- arbitrary user-routable audio graphs
- module loading as a runtime extension mechanism
- PipeWire as another always-on service in the product
- a hybrid "mostly ALSA plus a little PipeWire" topology

References verified 2026-05-27:

- <https://docs.pipewire.org/page_scheduling.html>
- <https://docs.pipewire.org/ringbuffer_8h_source.html>
- <https://docs.pipewire.org/aec_8h_source.html>
- <https://docs.pipewire.org/page_module_echo_cancel.html>

## Design Requirements

Playback requirements:

- One process owns the final DAC write path.
- Music and TTS keep playing if AEC/corpus/debug consumers crash.
- Optional reference consumers are never in the blocking playback path.
- Queue sizes and drop behavior are explicit and observable.
- The normal steady-state path stays cheap enough for 1 GB Pi 5 units.

Signal requirements:

- `speaker_output_reference` includes content, TTS, cues, chirps, and
  future system sounds.
- The reference is emitted after gains, ducking, final mix, and future
  safety/protection processing.
- AEC receives one coherent reference stream, not separately delayed
  content and TTS references.
- Frames carry sample rate, channel count, frame count, sequence, and
  monotonic timestamp metadata.

Realtime requirements:

- TTS playout has a durable ledger: provider item id, local playout id,
  queued frames, written frames, estimated drained frames, flushed
  frames, and final status.
- Barge-in can answer "what part of the assistant response did the
  user actually hear?"
- Provider-specific truncation APIs stay behind the voice-provider
  abstraction where possible.
- Corpus/debug captures can mark provenance damage when reference,
  mic, or playout-ledger data was missing or dropped.

Future hardware requirements:

- Active speaker DSP/protection must sit on every audible path,
  including TTS and cues.
- TTS must not permanently bypass crossovers, limiters, driver
  protection, or level guards.
- Adding HDMI, record-player ADC, USB input, or other content sources
  should not create new AEC reference work; they join the content path
  upstream of the output owner.

## Language Boundary

Use Rust for the realtime output owner.

`jasper-outputd` should own the final DAC loop, content/TTS mixing,
bounded queues, xrun recovery, reference fanout, sequence counters,
and playout accounting. Those are realtime-ish, stateful, and easier
to make boring in Rust than in Python.

Keep Python for voice policy:

- provider sessions and provider-specific truncation/cancel events
- wake/session state machines
- tool execution
- TTS generation requests
- cue selection and text rendering
- volume policy such as "what gain should TTS target right now?"

The clean split is:

```text
Python decides what should happen.
Rust owns the audio clock and reports what actually happened.
```

## Non-Goals

- Do not build a general-purpose audio server.
- Do not support arbitrary user graphs or dynamic plugin routing.
- Do not make AEC mandatory for playback.
- Do not make corpus/debug capture part of the realtime audio clock.
- Do not re-open chip-AEC or PipeWire migration as part of this work.
- Do not preserve the fan-in content-mirror spike as a compatibility
  path.

## Implementation Specification

Build pieces off-path first, but do not leave a permanent halfway
production architecture. The production cutover should move final
content playback and assistant playback under `jasper-outputd`
together.

### Service Shape

- New Rust binary: `jasper-outputd`.
- Service style mirrors `jasper-fanin`: `Type=notify`,
  `WatchdogSec`, progress-gated watchdog pings, audio slice,
  bounded memory, no disk I/O on the hot path.
- Hot path uses preallocated buffers. No allocation, logging, file I/O,
  blocking IPC, or network I/O in the DAC write loop.
- One thread owns ALSA playback to the DAC. Side consumers are fed by
  bounded queues/rings and sender threads.
- `READY=1` means the selected backend is actually usable. For the
  ALSA backend, emit it only after the PCMs are opened, negotiated
  period/buffer state is captured, the DAC has been primed with
  silence, playback has started, and the STATUS socket has already
  bound.
- Initial sample shape: 48 kHz, stereo, S16_LE. Expose negotiated
  period/buffer sizes in state; do not hide ALSA's actual values.
- Initial period policy: keep Camilla's 1024-frame chunk shape unless
  measurement justifies changing it. Do not force a 960-frame graph
  solely to match AEC's 20 ms frame; the AEC adapter can reframe.

### Ports

`content_in`:

- Source: private post-Camilla loopback capture.
- Proposed ALSA lane: use the currently reserved snd-aloop substream 6.
  Camilla writes to `hw:Loopback,0,6`; `jasper-outputd` reads
  `hw:Loopback,1,6` through named aliases.
- Shape: 48 kHz stereo `S32_LE` — `DEFAULT_PLAYBACK_FORMAT`
  (`jasper/camilla_config_contract.py`), pinned on both `plug` slaves in
  `deploy/alsa/asoundrc.jasper` and requested by outputd through
  `JASPER_OUTPUTD_CONTENT_FORMAT`, which
  `jasper-audio-hardware-reconcile` emits from the same coupling-aware
  function that decides what CamillaDSP writes. (It was `S16_LE` through
  2026-08-07; the wide-output-path program widened it so the one output
  quantization happens at the DAC edge. An armed SHM ring takes the ring's own
  wire format instead — `jasper.fanin_coupling.resolve_ring_wire`, which
  defaults `S32_LE` since 2026-08-15 and read `S16_LE` before it.)
- Ownership: Camilla is the only writer; `jasper-outputd` is the only
  reader. No `dsnoop` on this lane.

`tts_in`:

- Source: voice daemon and cue manager.
- Transport: local Unix socket with ordered, reliable framing. Prefer
  `SOCK_SEQPACKET` with bounded message sizes; a length-prefixed Unix
  stream is acceptable if testing shows Python support is cleaner.
- Commands: start segment, audio chunk, set target gain, end segment,
  flush segment/session.
- Large cues must be chunked by the client. Do not send multi-second
  cue files as one IPC message.
- Rust enforces the final gain clamp even if Python computed the
  target gain.

`dac_out`:

- Sink: physical Apple USB-C DAC hardware, preferably direct `hw:` or
  the smallest stable ALSA alias around it.
- Ownership: `jasper-outputd` is the only normal writer.
- `pcm.jasper_out`, the pre-cutover convergence point, was retired
  from the tree (issue #2240).

`speaker_reference_out`:

- Source: exact mixed samples sent toward `dac_out`, after content
  gain, TTS gain, cue gain, clipping policy, and future protection.
- Canonical shape: 48 kHz stereo S16_LE plus metadata.
- Metadata: stream id, sequence, monotonic timestamp, sample rate,
  channels, format, frame count, clipped sample count.
- Delivery: per-consumer bounded queues. Slow consumers drop/count;
  they never block `dac_out`.
- Publish only after the corresponding DAC period write succeeds. A
  prepared-but-unwritten period must not advance reference sequence,
  playout ledger, or "frames heard" counters.
- AEC bridge initially consumes this and keeps its existing
  downmix/resample/HPF/AEC-frame logic.

`playout_events`:

- Metadata-only event stream; do not persist audio or transcript text.
- Fields: local segment id, provider item id where available, kind
  (`assistant`, `cue`, `chirp`), gain, frames queued, frames written,
  estimated frames drained, frames flushed, `audio_played_ms`, status,
  start/end/flush monotonic timestamps.
- Consumers: voice daemon, wake/corpus metadata, `/system` state.

### Mixer Semantics

- Mix content and assistant audio with saturating accumulation one step wider
  than the samples, then clamp back — matching `jasper-fanin`'s simple and
  testable behavior. On outputd's i32 program spine that is an **i64**
  accumulator clamped to the i32 rails (`mixer::mix_saturating`); fan-in's own
  mixer is still i16 samples in an i32 accumulator.
- Report clipped samples per period and per segment.
- TTS/cues must be mixed after content ducking. In current production
  that happens in `jasper-fanin` before CamillaDSP crossover/protection.
- Future protection/limiting belongs after final mix and before both
  `dac_out` and `speaker_reference_out`.

### Barge-In Contract

When user speech is detected during assistant playback:

- detect user speech while assistant audio is playing
- voice daemon sends `flush` to `jasper-outputd`
- outputd drops queued assistant frames, keeps or fades content
  according to current ducking policy, and returns per-segment
  `audio_played_ms`
- send the appropriate provider truncation/cancel event
- preserve the transcript state that matches what the user heard

The provider abstraction should hide vendor naming, but not the core
datum: how much assistant audio was actually heard.

### Rollout Plan

1. **Off-path Rust core.** Add `jasper-outputd` with fake content,
   fake TTS, fake DAC, fake reference consumers, and no deployment
   wiring. Unit-test queue behavior, clipping, sequence numbers,
   playout ledger math, and flush semantics. **Landed 2026-05-28.**
2. **Pi cutover.** Add the post-DSP loopback lane, point
   Camilla playback at it, route TTS/cues to outputd, and let outputd
   own the DAC. This is one topology cutover, not a permanent split
   mode. **Landed on main 2026-05-28:** lane aliases, cutover Camilla
   config/statefile, outputd ALSA backend, TTS socket transport, state
   socket, doctor, and system dashboard are in-tree.
3. **Soak before AEC switch.** Verify normal music, AirPlay, Spotify,
   Bluetooth, USB input, TTS, cues, duck/restore, dongle recovery, and
   zero output xruns. **Landed:** outputd is mandatory in the packaged
   topology and exposes STATUS/doctor surfaces for DAC/reference health.
4. **Move AEC reference.** Switch `jasper-aec-bridge` from
   `pcm.jasper_ref` to outputd's speaker monitor. Treat reference drops
   as capture-health degradation, not playback failure. **Landed
   2026-06-08:** software AEC, chip-AEC, corpus, and diagnostics consume
   the same outputd monitor contract. The `pcm.jasper_ref` fallback the
   bridge kept alongside it has since been retired, and so has the
   timing probe that briefly inherited the alias (U4/P7-3), so nothing
   opens `pcm.jasper_ref` at all; the underlying `pcm.jasper_capture`
   tap survives for CamillaDSP alone, `jasper-aec-tune` having moved
   onto the same monitor at U4/P7-2.
5. **Enable robust barge-in.** Wire the local TTS flush and final
   playout-ledger acknowledgement to provider truncation/cancel logic,
   capture barge-in telemetry, and use the "volume down while assistant
   is speaking" path as the first product acceptance test.

### Required Tests

- Rust unit tests for mixer saturation, no-allocation steady-state
  paths where feasible, ring full/empty behavior, sequence gaps,
  reference fanout drops, and playout ledger math.
- Python tests that the `TtsPlayout` replacement preserves
  `write/flush/expected_drain_at/wait_drained` semantics.
- ALSA config tests for the post-DSP lane names and no raw `hw:`
  readers that would steal a loopback substream.
- Integration probe on Pi: 30 minutes each of AirPlay, Spotify,
  Bluetooth, USB input, and TTS-over-music with no output xruns.
- Corpus capture-health test proving reference packet loss/drops mark
  affected clips compromised.
- Barge-in test: assistant speaks, user interrupts, the local TTS path
  flushes, and provider truncation receives an `audio_played_ms` within
  one output period of the final playout-ledger estimate.

### Success Criteria

- Only `jasper-outputd` writes to the physical DAC during normal
  operation.
- `speaker_output_reference` includes content, TTS, cues, and chirps.
- AEC/corpus/debug can crash or fall behind without affecting audible
  playback.
- Normal outputd RSS target is under 20 MB.
- Output xrun count is zero in a realistic 24-hour soak.
- Barge-in during assistant speech produces a measured truncation point
  and does not leave the realtime model believing unheard audio was
  heard.

## Open Design Questions

- Should `jasper-outputd` be Rust from the start, matching
  `jasper-fanin`, or Python first for faster iteration? Decision:
  Rust for the realtime core; Python remains the policy/client layer.
- Should assistant PCM enter over Unix datagrams, a Unix stream, shared
  memory rings, or a small local protocol? The answer should be driven
  by backpressure and flush semantics, not convenience alone. Current
  leaning: ordered Unix socket protocol, not best-effort datagrams.
- Should `speaker_reference_out` publish post-limiter stereo, mono
  summed AEC-ready frames, or both? AEC wants mono 16 kHz; corpus and
  debugging often want higher-fidelity stereo provenance.
- What is the exact DAC target: direct hardware PCM, `plughw`, or a
  very small ALSA wrapper? The goal is to avoid using `dmix` as the
  main architecture boundary while preserving stable device setup.

## Decision Record

- 2026-05-27: Treat robust barge-in during assistant speech as a known
  product requirement, not a speculative future enhancement.
- 2026-05-27: Abandon the fan-in content-reference mirror as the
  strategic direction. It addressed corpus/debug pressure but not the
  final speaker-reference problem.
- 2026-05-27: Prefer a JTS-native output owner over adopting PipeWire.
  Borrow PipeWire's graph, scheduling, and ring-buffer lessons; do not
  ship PipeWire's desktop audio stack.
- 2026-05-27: The long-term reference must be
  `speaker_output_reference`, not `content_reference`.
- 2026-05-27: Implementation should build testable pieces off-path,
  then cut over production audio as one output-owner topology. Avoid a
  permanent TTS-only or content-only half-architecture.
- 2026-05-28: Land the real transport in `jasper-outputd`: ALSA
  capture from `outputd_content_capture`, direct DAC playback to
  `outputd_dac`, runtime xrun counters, negotiated buffer/period state,
  `/state`/doctor/dashboard surfaces, and structured `event=` logs.
- 2026-05-28: Add production-polish observability: content
  empty/partial/EAGAIN counters, then-outputd TTS queue over-budget
  duration, aggregate `event=outputd.tts_flush` traces, and source-handoff
  IDs that correlate mux journal lines with `/source/state.last_handoff`.
  The outputd TTS pieces in this historical entry were superseded by the
  2026-06-08 fan-in TTS contract below.
- 2026-05-28: Convert the work into branch-as-switch form for lab
  validation. Deploying `codex/outputd-cutover` enabled outputd and
  pointed Camilla at a separate outputd statefile. This was superseded
  later the same day by the mainline merge; rollback now means
  disabling outputd and deploying a pre-outputd release or branch.
- 2026-05-28: Merge the cutover into main, then add the remaining
  playout-ledger contract polish: provider item identity on TTS
  segments, synchronous `FLUSH_SYNC` acknowledgements with
  `audio_played_ms`, and DAC-delay-based drained-frame estimation.
- 2026-06-01: Move assistant loudness policy fully into outputd. Python
  now owns only provider profile seeding/learning; outputd owns content
  loudness measurement, peak-aware gain decisions, STATUS telemetry,
  and correction-window meter pause/resume.
- 2026-06-01: Add the disabled-by-default outputd content bridge
  (`JASPER_OUTPUTD_CONTENT_BRIDGE=rate_match`) for DAC-paced
  rate-matching validation. Packaged production remains `direct`; the
  bridge is a lab-gated pipeline fix for snd-aloop content-lane drift.
  **(Deleted 2026-08-10 — see the content-bridge bullet above.)**
- 2026-06-02: Split final-output DAC role from Apple mixer ownership.
  `outputd_dac` may target the Apple USB-C dongle or the JTS3 DAC8x;
  `jasper-audio-hardware-reconcile` now owns install/boot/udev-triggered
  DAC role convergence, and `jasper-dac-init`/
  `jasper-headphone-monitor` are enabled only for the recognized Apple
  final-output role, with runtime-safe helper scripts for
  manual/operator starts. Added the outputd-only DAC8x validation profile
  `hifiberry_dac8x_outputd_stability` for content-pipeline soaks that
  should not fail just because chip-AEC/voice is parked.
- 2026-06-17: Removed the explicit DAC8x-family `JASPER_OUTPUT_DAC_ROUTE`
  render path. `outputd_dac` renders directly to the detected final-output
  card; active-speaker per-driver ownership lives in the saved topology and
  protected active graph.
- 2026-06-02: Added the first product speaker-output topology contract
  (`jasper.output_topology`, `/var/lib/jasper/output_topology.json`,
  `/sound/output-topology`). It is a no-audio, no-Camilla, no-ALSA
  persistence/evaluation surface for DAC lanes, speaker groups, active
  driver roles, subwoofer routing, and identity/tweeter-protection
  evidence. Safe playback remains a separate active-speaker session.
- 2026-06-03: Added the active-speaker playback-readiness gate and the
  artifact-first topology channel-test slice. Default installs still verify
  artifacts only; an explicit lab `aplay` backend can emit short, clamped
  non-tweeter tests after readiness passes. **Superseded:** the product later
  converged on protected commission-load/ramp testing; the readiness report and
  per-driver topology planner were removed in July 2026.
- 2026-06-04: `jasper-doctor` now gates Apple-dongle-specific USB and
  headphone-gain checks on `JASPER_AUDIO_DAC_ID=apple_usb_c_dongle`, so
  HiFiBerry/DAC8x systems report the selected output role instead of false
  Apple-dongle failures.
- 2026-06-09: `jasper.output_topology` now consumes
  `jasper.audio_hardware.dac` for known DAC labels, physical output counts,
  clock-domain labels, and clock-domain reports. This makes dual Apple a known
  four-output topology shape with a measured-sync-required clock contract,
  distinguishing profile shape from aggregate runtime enablement and leaving
  runtime activation with hardware reconcile/outputd.
- 2026-06-09: Added `jasper.output_hardware` and
  `/run/jasper-output-hardware/output_hardware.json` as the observed output hardware state
  contract. `jasper-audio-hardware-reconcile` writes the artifact during
  install/boot/udev convergence, publishes outputd runtime env separately,
  and parks dual Apple with the fake backend until outputd has a real
  four-channel active graph. `/state`,
  `/sound/output-topology`, and `jasper-doctor` now read the same artifact.
- 2026-06-08: Retired outputd's then-disabled TTS IPC implementation after
  rollback no longer needed it. At that point fan-in was the sole
  production TTS/cue IPC owner; outputd owned final electrical output and
  monitor/reference fanout. Dual-Apple outputd activation was also
  graph-gated: hardware observation alone records the composite profile,
  but outputd switches to the four-channel sink only after the
  active-speaker startup config is loaded and CamillaDSP's outputd
  statefile points at that active graph.
- 2026-06-09: Documented the current robust barge-in contract:
  local speech detection triggers the active TTS transport flush first,
  the final playout ledger supplies `audio_played_ms`, and provider
  adapters reconcile conversation state after the local audio stop.
  First acceptance target is interrupting assistant speech with a local
  volume command and no second wake word.
- 2026-06-11: Multiroom Increment 5 PR-2 reintroduced an outputd TTS
  socket only for active bonded members. Solo stays fan-in-owned, while a
  bonded member mixes its own assistant audio in outputd after the
  snapcast round trip and before reference publication.
- 2026-06-21: Wired the solo fan-in TTS `FLUSH_SYNC` ack to a real
  per-segment playout ledger (`rust/jasper-fanin/src/playout.rs`),
  replacing the hardcoded `max_audio_played_ms=0` / `events=[]`. fan-in is
  pre-CamillaDSP and cannot see the DAC clock, so its `audio_played_ms` is
  the mix-commit count (frames committed to the snd-aloop program,
  DAC-rate-paced) and over-reads true playout by the fixed downstream
  pipeline depth — the conservative direction for truncation. Exact
  DAC-clock precision (subtracting outputd's reported DAC delay) and the
  provider-adapter consume side remain follow-ups.

## Revision log

Newest first. Each entry names what that pass re-checked; a claim not
re-touched since carries forward from its most recent entry below.

- **2026-08-10 (one-clause truth-up, ring v2 R5a's fix round —
  [#2314](https://github.com/jaspercurry/JTS/pull/2314)).** The Ingress
  paragraph attributed the SHM ring's `S16_LE` pin to
  `jasper.fanin_coupling.content_lane_format_for_coupling`. R5a introduced
  `resolve_ring_wire` as the one resolution every declaring end reads;
  `content_lane_format_for_coupling` (via `capture_kwargs_for_coupling`) now
  delegates to it rather than computing the format itself, so the pin is
  `resolve_ring_wire`'s — re-verified against `jasper/fanin_coupling.py`.
  **Nothing else in this pass**; the footer date below is the last
  FULL-document pass and is deliberately not bumped.
- **2026-08-10 (one-clause truth-up, ring v2 R4's fix round —
  [#2310](https://github.com/jaspercurry/JTS/pull/2310)).** The Ingress
  paragraph attributed the SHM ring's `S16_LE` wire to
  `jasper_ring::Geometry::validate_self`. R1
  ([#2297](https://github.com/jaspercurry/JTS/pull/2297)) widened that
  accept-set to S16LE **or** S32LE at 2..=8 channels, so the pin is now
  `jasper.fanin_coupling.content_lane_format_for_coupling`'s, not the
  layout's — re-verified against both sources. The wire itself is
  unchanged (`S16_LE` on every box), which is why the quantization
  paragraph above needed nothing. **Nothing else in this pass**; the
  footer date below is the last FULL-document pass and is deliberately
  not bumped.
- **2026-08-08 (full-document pass, this PR).** Re-read the entire doc
  against current code, including the `chip_ref_writer.recent_writes`
  paragraph landed by the entry directly below. Corrected two internal
  contradictions in the Robust Barge-In Contract's provider-truncation
  status — the "Status (2026-06-21)" callout still said Step 4 (provider
  cancel/truncate) was "deliberately not wired yet," and the "still
  intentionally not done" section listed Gemini's pack (PR-5) as
  remaining work — both stale against
  `jasper/voice/turn_playback.py::_flush_for_interrupt` and
  `jasper/voice/openai_session.py`, which have driven `cancel_response` +
  `truncate_assistant_audio` for the OpenAI/Grok pack since PR-4 landed the
  same day (2026-06-21); PR-5 (Gemini) also landed that day as a *final*
  no-op, not deferred wiring. Converted the footer's former run-on
  "prior … pass" chain into this dated revision log; the `Last verified:`
  footer below now records only the latest pass. Everything else
  re-verified accurate
  against `rust/jasper-outputd`, `jasper/audio_hardware/dac.py`,
  `jasper/output_topology.py`, `jasper/multiroom/reconcile.py`,
  `rust/jasper-fanin/src/playout.rs`, and the systemd/install/reconciler
  scripts named throughout.
- **2026-08-08 (#2253/#2264).** The STATUS-payload passage now also
  documents the chip-reference writer's bounded per-write observation ring
  `chip_ref_writer.recent_writes`: the one STATUS field whose absence is a
  hard refusal rather than a blank surface, since `jasper-aec-init`
  resolves chip-AEC `SYS_DELAY` from it. Re-stated against
  `rust/jasper-outputd/src/state.rs` and `jasper/cli/aec_init.py`.
- **2026-08-08.** Two same-day landings on the FINAL-EDGE hop — a separate
  hop and env var (`JASPER_OUTPUTD_DAC_FORMAT`) from the content-lane
  `S32_LE` flip below. (1) DAC output: the base HiFiBerry DAC8x now also
  declares an `S32_LE` final edge alongside InnoMaker (PR-7, jts3 hardware
  probe 2026-08-07); DAC8x Studio's non-flip re-stated against the registry
  comment. (2) The packed `S24_3LE` edge went LIVE on the Apple USB-C
  dongle (PR-8, jts.local open-proof); the dual-Apple composite stays
  `S16_LE` because the paired sink has no packed-24 child write path — a
  composite and its child profile can now legitimately declare different
  widths, resolved by armed-profile id. Separately, a DAC8x Studio ROUTING
  fix (#2250) narrowed the base profile's card-match regex so Studio
  silicon no longer inherits the base row's `S32_LE` edge or its approved
  chip-AEC status; two residuals stay documented rather than closed
  (#2258).
- **2026-08-08 (earlier, scoped).** snd-aloop content lane widened to
  `S32_LE` end to end — `DEFAULT_PLAYBACK_FORMAT`, the passive lane's
  `plug` slave pins, the reconciler-emitted `JASPER_OUTPUTD_CONTENT_FORMAT`,
  the cutover seed, the `content_in` port shape, the boundary paragraph's
  ingress half, the `rate_match` coherence emission, and the rollback
  runbook's NOT-rollback-symmetric rule — all re-stated against the code;
  an armed SHM ring keeps this hop at its own `S16_LE` wire format.
- **2026-08-07.** outputd's internal program spine widened to i32 with
  exactly one quantization at the DAC edge — Current Operational Truth,
  the InnoMaker S32 edge paragraph, the `rate_match` S16-only constraint,
  the `RuntimeAlsaSink` signatures, and Mixer Semantics; the boundary
  paragraph corrected to split egress-narrows from ingress-widens after a
  review found the snapclient round-trip FIFO — a SOURCE — misfiled among
  the egress wires.
- **2026-08-05.** InnoMaker boot-intent reconciliation rechecked; the
  InnoMaker final-edge `plug` deleted from `jasper-asound-render.sh`
  (PR-4, format-foundation) — `outputd_dac` now renders raw `type hw` for
  every registered single DAC profile, and the S32_LE hardware-edge proof
  moved from the render's pinned slave to outputd's own client-edge
  readback.
- **2026-08-04.** Passive-stereo runtime alias, generic registered-single
  reconciliation, staged-candidate rejection parking, and final-sink
  startup exit 78.
- **2026-07-24.** Post-DSP turn-start `VolumeContext` atomicity in
  `PREPARE_ASSISTANT`, with missing/rejected context pinned fail-closed to
  silence.
- **2026-07-23.** The shared `MixStage` engine, per-period mute/live-regain
  mix loop, learned/persisted quiet-room reference, and the shared
  `tts.assistant_loudness` STATUS renderer.
- **2026-07-16.** Pre-DSP fan-in volume-context ownership.
- **2026-07-14.** DAC connection declaration and the output-hardware USB
  role artifact.
- **2026-07-12.** outputd control-socket command cap/deadline and STATUS
  JSON contract against `rust/jasper-outputd/src/state.rs`; the historical
  readiness entry marked superseded by the protected commission ramp.
- **2026-07-10.** Optional-reference failure isolation and full transport
  coherence against `rust/jasper-outputd`, `jasper.audio_runtime_plan`,
  `jasper.camilla_config_contract`, `jasper.cli.audio_config`, the staged
  audio-hardware reconciler, and doctor; the ring/default outputd bridge
  text against `jasper.fanin_coupling`, `jasper.fanin.coupling_auto`, and
  `jasper.fanin.coupling_reconcile`.
- **2026-07-06.** outputd config-shear resilience against
  `jasper.audio_runtime_plan`, `jasper.cli.audio_config
  validate-outputd-env`, `deploy/bin/jasper-audio-hardware-reconcile`, and
  `deploy/bin/jasper-outputd-failure-reconcile`, including content and DAC
  buffer/period validation; Camilla/outputd install choreography against
  `deploy/lib/install/systemd-units.sh` and
  `deploy/bin/jasper-camilla-recover`.
- **2026-06-24.** Active-endpoint and wireless-sub TTS route exceptions
  against `jasper.multiroom.tts_route.expected_grouping_tts_route`,
  `jasper.multiroom.reconcile.outputd_grouping_env`,
  `jasper.multiroom.reconcile.voice_grouping_env`, and
  `jasper.cli.doctor.grouping`.
- **2026-06-12 through 2026-06-22 (earlier passes).** fan-in solo
  `FLUSH_SYNC` playout-ledger ack against
  `rust/jasper-fanin/src/{playout,tts}.rs`; the active-speaker runtime
  graph boundary against `jasper.active_speaker.runtime_contract`,
  `outputd_active_lane_decision`'s paired active-leader statefile proof,
  install outputd-statefile selection, the doctor runtime graph check, and
  `resolve_output_layout`; Stage-7 outputd loop unification against
  `rust/jasper-outputd`; solo fan-in vs. passive bonded-member outputd TTS
  ownership; the voice playback seam path after the
  `jasper/voice/turn_playback.py` extraction.
- **2026-08-15 (P9-C, audio-graph consolidation #2285).** Corrected several
  passages that assumed the aloop active-content pair
  (`outputd_active_content_playback` / `_capture`, snd-aloop pair 5) still
  had live PCM definitions: it was deleted once the ACTIVE ring became the
  roleful transport. Fixed the assistant-audio ASCII chain, the
  `asoundrc.jasper` ALSA-surfaces list, the dual-Apple active-lane gate
  (which now checks the ring endpoint, not the aloop name), and a
  never-implemented `__OUTPUTD_ACTIVE_CONTENT_CHANNELS__` render-token claim
  (verified against `deploy/alsa/asoundrc.jasper`, whose only render tokens
  are `__RATE_CONVERTER__`, `__OUTPUTD_DAC_PCM_BLOCK__`, and
  `__OUTPUTD_DAC_CTL_BLOCK__`); added pointer notes to the design-of-record
  sections that still describe the deleted raw-hw-aloop shape. See
  [audio-paths.md](../audio-paths.md).
- **2026-08-15 (ring wire default → wide).** Re-verified only the width claims
  the SHM ring's default-format flip falsified, against
  `jasper.fanin_coupling.resolve_ring_wire_format` /
  `content_lane_format_for_coupling` and
  `deploy/alsa/conf.d/60-jts-ring.conf`. The **Ingress** paragraph claimed the
  ring was "`S16_LE` on every box today because
  `jasper.fanin_coupling.resolve_ring_wire` holds that wire narrow by policy" —
  false in both halves, and the policy half had already been false since
  2026-08-11 gave the resolver a per-box input. The ring now defaults `S32_LE`,
  so it left the "arrives narrow" list, and the Current Operational Truth
  paragraph's "`S16_LE` wires that remain" list lost it too; both now name the
  operator rollback pin (`JASPER_FANIN_RING_WIRE_FORMAT=S16_LE`, which nothing
  in the repo writes) as the only narrow route. The `content_in` port note in
  the design-of-record section drops the stale token for a pointer at the
  resolver. **Nothing else in this pass** — the DAC-edge table, the rollback
  runbook, barge-in, and multiroom stand as last verified.
  Canonical: [audio-paths.md](../audio-paths.md).
- **2026-08-20 (composite child-loss bullet — never-implemented resilience
  surface).** Re-verified only the "Composite child loss" bullet in the
  design-of-record Resilience list, which described a `sink.health()` pre-write
  check whose non-Running child muted **all** children and reported
  `event=outputd.composite.child_lost` / `/state.composite.children[].state`.
  All four were false: no `fn health` exists anywhere in `rust/`; the check is
  `PairedCompositeSink::check_delay_delta`, which runs *after* the period write
  in `write_dual_period` (its sole caller) and `anyhow::bail!`s rather than
  muting; and a repo-wide grep for `child_lost` returned exactly one hit — the
  doc line itself. The bullet also contradicted its own neighbour, which
  already documented the real `/state.dual_apple` block (`CompositeStatus`,
  `alsa_backend.rs`; serialized under `sink_mode == "dual_apple"` in
  `state.rs`). Rewrote it to the shipped bail → `Restart=on-failure` →
  `StartLimitAction=reboot` path, pointed at the reconcile-to-`single_alsa`
  convergence that actually handles a departed child, and recorded the genuine
  observability gaps: `write_dac_fail_closed` holds exactly one `eprintln!` and
  `start_dacs` none, so the bad-PCM-state bail, the group-start refusal, a
  repeated `Ok(0)`, and the non-`EPIPE` propagate are all silent, unlike the
  divergence and reprime branches beside them. Split the trailing
  gating claim, which was half true — `dual_apple_runtime_mapping` does require
  two child PCMs, but the unit's single `ExecCondition` checks only the one
  resolved `JASPER_AUDIO_DAC_CARD`. **Four corrections from this PR's
  adversarial review, recorded because each was the same defect class the pass
  set out to remove.** First, only `sink.health()` and `child_lost` are dead:
  a per-child array under a width-agnostic `composite` `/state` block is **live
  owed work** (prescribed in Observability below, deferred by
  `SinkMode::as_str` as "a separate change"), so retiring that container would
  have misdirected the reader doing that migration — the retirement is now
  scoped to the two dead symbols plus the mute-all response, and rests on
  "nothing in the tree asks for these" rather than on an unrecorded #2255
  supersession (#2255's scope is the bail-on-first-xrun fix alone). Second, the
  neighbouring unified-xrun bullet claimed the composite "bails on exactly two
  things", which `write_dual_period`'s own docstring falsifies — it names a
  four-rung ladder (recover → re-prime → group start → re-latch) and five bail
  conditions across it, plus the divergence guard — so that sentence was
  corrected in place. Third, this pass's own first draft routed a *departed*
  child through that recovery ladder; `write_dac_fail_closed` enters the ladder
  only on `EPIPE`/`ESTRPIPE`, so an `ENODEV`/`ENXIO` removal takes the bare
  propagate and emits no `event=outputd.xrun` of its own — the bullet now says
  so in **both** directions, because the first draft of that very correction
  then overclaimed the other way ("do not go looking for one after a
  removal"): the xrun `eprintln!` is unconditional and precedes `try_recover`,
  so a removal that surfaces first as an underrun DOES log one before recovery
  fails on the vanished device. Which errno a real removal raises first is a
  kernel behaviour no static read settles, which is why the doc now scopes the
  negative to the errno rather than to the scenario. Fourth, this pass's first
  draft also called the bad-PCM-state bail "the one place" the section's
  observable promise is unmet; it is one of at least four, per the
  `eprintln!` counts above — a universal asserted from a verified narrow case,
  which is the same shape as the other three. (The count was hedged to "at
  least" precisely because an exact enumeration is the failure mode this entry
  keeps recording; the review then named the fourth, in `start_dacs`.)
  **Nothing else in this pass:** the remaining Resilience
  bullets, the change set, and Observability stand as last verified, which is
  why the footer below still reads 2026-08-15.

Last verified: 2026-08-15 (two scoped passes — see the two 2026-08-15 entries
above for what each re-verified; prior 2026-08-08 was the last full-document
pass, against `f78dcd597`; revision log above)
