# Loopback-retirement census — Phase 0 adjudication

**Pinned SHA:** `6e569e8dc8e572a8d648d332c414374b8394496e`
(verified by `git rev-parse HEAD` in
`/Users/jaspercurry/Code/JTS/.claude/worktrees/loopback-retirement-ring-3a4b6e`
before the first read; matched exactly.)

**Status of this file.** This is the campaign's single source of truth for
*what exists at this SHA*. Later designs cite it; none should restate it.
It states what IS. It contains no design proposal and no fix.

**Evidence discipline.** Every claim below carries a file+symbol (and a
quoted line where the exact text is load-bearing), or is explicitly labelled
`INFERENCE`. `UNKNOWN` is a legal verdict and is used where code cannot
settle the question. Numbers name the method that produced them.

---

## 1. METHOD + COVERAGE

### 1.1 Inherited layer (the mechanical enumerator)

The enumerator's method, restated honestly rather than improved:

- **Broad token sweep**, case-insensitive, over the whole tracked tree:
  `loopback` (2201 matching lines / 270 files), `aloop` (1266 / 205),
  `coupling` (3202 / 223). Union of file paths = **405 files** = the
  candidate set. Total clustered hit volume reported as **6209 lines**.
- **Four narrow-pattern cross-checks** against that union:
  `pcm_substreams`, `GROUPING_LOOPBACK`, `_recover_to_loopback`,
  `resolve_coupling`, `hw:Loopback`, `RING_CONFIRM_STRIKE` — zero gap; and
  `modprobe|modules-load`, which surfaced 4 extra files
  (`deploy/systemd/jasper-usbgadget.service`,
  `deploy/usbsink/jasper-usbgadget-up`,
  `deploy/usbsink/jasper-usbsink-name-patch`,
  `tests/test_usbgadget_script.py`), all verified by hand to be
  `modprobe libcomposite` (USB gadget), correctly excluded.
- **Depth:** `coupling_reconcile.py`, `fanin_coupling.py` and
  `coupling_auto.py` read in full; everything else grep-clustered by
  nearest enclosing symbol.
- **Declared caveat, carried forward:** `tests/` (138 files, ~1650 raw hit
  lines, ~640 clusters) is **condensed** — every test *file* is named and
  counted, but only a representative subset of clusters per file is listed.
  The untrimmed sweep is preserved as `g_tests.txt` in the enumerator's
  scratchpad. **Consequence for this census: test-side coverage is
  file-complete but not symbol-complete.** Where this census names a test
  contract it is because I opened it, not because the enumerator listed it.

### 1.2 Depth I added on top (files read in full or near-full by me)

| File | Extent |
|---|---|
| `jasper/fanin/coupling_reconcile.py` (5190 L) | module docstring, full AST symbol table, and full reads of `reconcile_coupling`, `_reconcile_coupling_inner`, `reconcile_auto`, `_route_mode_for_reconcile`, `_block_unsupported_coupling`, `ring_topology_ready`(+`_strict`), `ring_roleful_unattended_ready`, `default_ring_gates`, `_SHARED_RING_PREFLIGHTS`, `ring_route_ready`, `_leaves_live_shm_ring_bridge`, `_run_loopback_daemon_ops`, `_disarm`, the strike ladder (`_read/_record/_clear_ring_confirm_*`), `_reseed_loopback_statefile`, `_recover_to_loopback`, `_fail_ring_arm`, `read_persisted_coupling`, `_acquire_entry_lock`, `main` |
| `jasper/fanin/coupling_auto.py` (354 L) | **complete** |
| `jasper/fanin_coupling.py` (951 L) | module docstring + full constant block (L1–200); `transport_label`, `resolve_coupling`, `coupling_value_removed`, `is_shm_ring_coupling` |
| `deploy/modprobe.d/snd-aloop.conf` (58 L) | **complete** — the canonical pair-allocation owner |
| `deploy/systemd/jasper-fanin-coupling-auto.service` | **complete** |
| `jasper/audio_runtime_plan.py` | `coupling_supported_for_route`, `fanin_coupling_action`, `route_mode_from_grouping_config` |
| `jasper/active_speaker/runtime_contract.py` | `safe_graph_for_current_topology` docstring + head |
| `jasper/control/state_aggregate.py` | `_coupling_state` docstring + resolution head |
| Rust | `Coupling` enum, `Output` enum, `LaneSource` enum (`jasper-fanin`); `ContentBridgeMode`, content-PCM defaults (`jasper-outputd`) |

Plus four parallel read-only evidence sweeps I commissioned and adjudicated
(shared ALSA/module infra; AXIS-1-GROUPING; AXIS-2 + AXIS-3;
ring assets / #2526 / stale prose). Their findings are attributed inline.

### 1.3 NOISE-* spot-check (the enumerator's two added categories)

The enumerator added `NOISE-NETWORK` (the IP loopback interface / `127.0.0.1`
/ OAuth loopback redirect / SSRF guards) and `NOISE-GENERIC-COUPLING`
(ordinary-English "coupling"). Before discarding them I **spot-checked 22
individual hits** across both categories by reading the cited lines.

**20 of 22 confirmed genuine noise.** Two corrections:

1. **`jasper/control/grouping_supervisor.py:313` is AXIS-1-GROUPING, not
   NOISE-NETWORK.** The file's other three hits (L30, L39, L470) *are*
   network ("poll costs one loopback RPC (~1 ms)"), but L313 reads:
   > `# round-trip starvation of the active lane (the camilla#2`
   > `# loopback going silent) is a separate signal outputd does not`
   > `# yet surface — deferred until observed.`
   That is the grouping ALSA round-trip. A file-level NOISE label hid a real
   axis hit. **It is also a named observability gap** — see §8 Q4.
2. **`jasper/source_state.py:181` is AXIS-2, not AXIS-3.** The line
   ("`audio actually reaches the speakers (ALSA loopback typically owned by
   librespot)`") is about the renderer ingress lane, not AEC.

**Ruling: the NOISE-* categories are discarded as axis categories**, with the
caveat that *file-level* noise labels proved unsafe twice in 22 — a
per-line read is required wherever a NOISE-labelled file matters.

### 1.4 What this census does NOT cover

- Runtime/hardware behaviour. Nothing was deployed, run, or probed. Every
  claim is a claim about source at this SHA.
- Test-symbol completeness (see §1.1 caveat).
- Whether any behaviour is *correct*. This is an inventory, not a review.

---

## 2. CONSUMER MAP, per axis

Organised **per consumer** (a subsystem/symbol that depends on the
machinery), not per grep line. "Owning phase" is one of:
**PHASE-1** (grouping migration), **PHASE-2** (coupling deletion),
**STAYS** (deletion does not touch it — its axis is named), or
**OWNER-DECISION** (a scope call this census does not make).

### 2.1 AXIS-1 — the loopback coupling (fan-in→aloop→Camilla, Camilla→aloop→outputd)

The coupling's own machinery is three Python modules plus two Rust crates.
Everything else is a *reader*.

#### 2.1.1 The machinery itself (all PHASE-2)

| Consumer | Role | Breaks if the coupling dies |
|---|---|---|
| `jasper/fanin_coupling.py` — the vocabulary module | Owns `COUPLING_ENV_VAR` (`JASPER_FANIN_CAMILLA_COUPLING`), `COUPLING_LOOPBACK`/`COUPLING_SHM_RING`, `VALID_COUPLINGS`, `resolve_coupling`, `coupling_value_removed`, `is_shm_ring_coupling`, `capture_kwargs_for_coupling`, `content_lane_format_for_coupling`, `coupling_capture_kwargs_from_env`, the Ring A/B/ACTIVE geometry constants, `transport_label`. Import-cheap (stdlib only) by design so socket-activated web surfaces can resolve it. | The *selector* collapses. Ring constants survive; the two-valued token, its resolver, and every kwargs-shaping function keyed on it become vacuous. **PHASE-2** |
| `jasper/fanin/coupling_reconcile.py` (5190 L) | The ordered arm/disarm transition owner across all three audio daemons. Single writer of `JASPER_FANIN_CAMILLA_COUPLING` in `/var/lib/jasper/fanin.env` and the Ring B bridge keys in `/var/lib/jasper/outputd.env`. Owns the strike ladder, `_recover_to_loopback`, `_reseed_loopback_statefile`, the ring-eligibility gate family, the entry flock, and the CLI. | The whole disarm/recovery half loses its destination. See §3. **PHASE-2** |
| `jasper/fanin/coupling_auto.py` (354 L) | The **pure** default-resolution decision (P3/P4): operator-marker semantics, ring-gate short-circuit, USB-combo arming. Contains **zero** `if` branching on the coupling token (measured, §3.2) — it returns `COUPLING_LOOPBACK` as a *value* on first gate failure. | Its return type collapses to a boolean "ring or park". **PHASE-2** |
| `deploy/systemd/jasper-fanin-coupling-auto.service` | Boot + deploy oneshot; `ExecStart=…/jasper-fanin-coupling-reconcile --auto --reason systemd` (L86). Carries a hand-tallied `TimeoutStartSec=210` derived from a 160 s worst-case arm. | Timeout tally and the loopback confirm/disarm fork (61 s) both change. **PHASE-2** |
| `deploy/install.sh:1576-1578` | Enables the unit and runs `--auto --reason install` on every deploy. | **PHASE-2** |
| `rust/jasper-fanin/src/config.rs` — `pub enum Coupling { Loopback, ShmRing }` (L538-544) + `from_env_value` (L550) | The Rust normalizer that MUST agree with Python's `resolve_coupling`. Also `env_str("JASPER_FANIN_OUTPUT_PCM", "hw:Loopback,0,7")` (L726). | Enum collapses to one variant; the aloop output PCM default disappears. **PHASE-2** |
| `rust/jasper-fanin/src/mixer.rs` — `enum Output { Alsa(PCM), Ring(Box<RingOutput>) }` (L665-675) | The fan-in program-egress fork. The `Ring` arm's own comment: *"This arm opens NO ALSA PCM … on a ring-coupled box nothing writes `hw:Loopback,0,7` at all."* | `Output::Alsa` (the program write) is the deletion target. **PHASE-2** — but see §6: `Output::Alsa` is program-egress only; `LaneSource::Lane` (ingress) is AXIS-2 and independent. |
| `rust/jasper-outputd/src/config.rs` — `pub enum ContentBridgeMode { Direct, ShmRing }` (L44-53) | outputd's content-ingress fork. `Direct` (the shipped default, `jasper-outputd.service:76 Environment="JASPER_OUTPUTD_CONTENT_BRIDGE=direct"`) reads `JASPER_OUTPUTD_CONTENT_PCM=outputd_content_capture` — the pair-6 snd-aloop capture. | `Direct` is the loopback content hop. **PHASE-2** |
| `rust/jasper-outputd/src/alsa_backend.rs`, `shm_ring_source.rs`, `types.rs`, `state.rs` | outputd's content-capture entry, its ring source, and the `/state` echo of which hop it is on. | **PHASE-2** |

#### 2.1.2 Readers of the resolved coupling (all PHASE-2 unless noted)

Established by import evidence (`from jasper.fanin_coupling import …` /
`from jasper.fanin.coupling_reconcile import …`), not by grep proximity.

| Consumer (file → symbol) | What it imports / does | Breaks if the coupling dies |
|---|---|---|
| `jasper/audio_runtime_plan.py` — the SSOT planning layer | Imports `COUPLING_ENV_VAR`, `COUPLING_LOOPBACK`, `COUPLING_SHM_RING`, the four `RING_CAMILLA_*` constants, `read_persisted_coupling`, `load_topology_for_wire`. Owns `coupling_supported_for_route`, `fanin_coupling_action`, `CouplingSupport`, `transport_topology_for_coupling`, `transport_coherence_report`, `fanin_coupling_capture_kwargs`, `_effective_camilla_target_setting`/`_chunksize_setting`, `outputd_content_format_change`. | The single largest reader. Every `coupling: str` parameter in the plan collapses. The route-support matrix (§4 L3) has exactly one blocked combination and it is `shm_ring`+grouped — it inverts meaning under Phase 2. **PHASE-2** |
| `jasper/active_speaker/runtime_contract.py` → `safe_graph_for_current_topology` (L4499+) | `from jasper.fanin_coupling import COUPLING_SHM_RING, resolve_coupling`. Chooses the *flat* startup graph: ring-flat under `shm_ring`, else the loopback flat `outputd-cutover.yml`. Also `topology_supports_shm_ring`, `active_ring_channels_for_topology`, `parked_muted_exits`. | The flat-graph *pick* disappears (one file, not two). Roleful branches are already transport-agnostic per its own docstring. **PHASE-2** |
| `jasper/active_speaker/camilla_yaml.py` | `RING_ACTIVE_PLAYBACK_DEVICE`, `RING_CAPTURE_DEVICE`, `RING_PCM_DEVICES`, `RING_CAMILLA_*`, `resolve_ring_wire`. Emits the active-speaker capture/playback blocks. | Ring constants survive; the loopback-shape emit path is the target. **PHASE-2** |
| `jasper/sound/camilla_yaml.py` → `emit_sound_config`, `emit_flat_outputd_cutover_config`, `emit_flat_ring_config`, `render_flat_cutover_configs` | Same ring constants. **Emits the two flat startup graphs** — `outputd-cutover.yml` (loopback) and the ring flat config. | One of the two rendered flat configs is the loopback one. **PHASE-2** |
| `jasper/active_speaker/startup_load.py` → `build_driver_commission_load_preflight` | `RING_PCM_DEVICES`, `is_shm_ring_coupling`, `ring_active_endpoint_armed`, `FANIN_ENV_PATH`, `read_persisted_coupling`. Refuses/permits a commissioning load based on whether fan-in is "still loopback-coupled". | Preflight loses a refusal reason. **PHASE-2** |
| `jasper/active_speaker/setup_status.py` | `transport_label`. | `transport_label`'s `alsa` branch (see §7 S5). **PHASE-2** |
| `jasper/active_speaker/playback_route.py` → `resolve_live_active_endpoint` | `read_loaded_camilla_graph`. Handles "Legacy mid-rollback (graph=aloop, marker=ring)". | The mid-rollback state class disappears. **PHASE-2** |
| `jasper/active_speaker/staging.py`, `baseline_profile.py`, `commissioning_*.py`, `graph_safety.py` | Reference `coupling_reconcile.ring_roleful_unattended_ready`, `_anchor_is_all_muted`, `_acquire_entry_lock`, `transport_label`, and emit "snd-aloop defaults" prose in staged configs. | Mixed: gate references survive Phase 2, prose does not. **PHASE-2** (prose) / **STAYS** (gate references, AXIS-1 ring side) |
| `jasper/output_topology.py` → `resolve_output_layout` | `RING_ACTIVE_PLAYBACK_DEVICE`. Chooses the active endpoint device: *"two — snd-aloop by default, the ring when the reconciler's endpoint is armed."* | The two-way choice becomes one-way. **PHASE-2** |
| `jasper/sound/graph_carrier.py`, `jasper/sound/runtime.py` | Thread `fanin_coupling_capture_kwargs` / `coupling: str \| None` through four `reemit(...)` overloads and `StatefileCamillaController`. | Parameter threading collapses. **PHASE-2** |
| `jasper/web/sound_setup.py`, `jasper/web/correction_setup.py`, `jasper/correction/session.py` | All three import `coupling_capture_kwargs_from_env` to re-emit a graph with the box's current capture shape. | **PHASE-2** |
| `jasper/cli/active_speaker.py` (`--coupling` flag, `_cmd_runtime_safe_graph`, `_cmd_baseline_reemit`) | `read_persisted_coupling`, `RING_ACTIVE_PLAYBACK_DEVICE`. | The `--coupling` CLI flag is a Phase-2 deletion. **PHASE-2** |
| `jasper/cli/audio_config.py` (`_cmd_render_ring_conf_wire`, `_cmd_validate_outputd_env`) | `COUPLING_ENV_VAR`, `RING_ACTIVE_PLAYBACK_DEVICE`, `RING_SLOT_FRAMES`, `resolve_ring_wire`. (Its `_cmd_renderer_lanes` is AXIS-2 — see §2.3.) | **PHASE-2** for the coupling half. |
| `jasper/cli/output_topology_reset.py` → `_converge_camilla_graph` | `read_persisted_coupling`. | **PHASE-2** |
| `jasper/control/state_aggregate.py` → `_coupling_state` | Publishes `/state.audio_graph.coupling`: persisted intent, outputd content bridge, `intent_coherent`, live fan-in transport, and `choice` (`operator` vs `auto`). Reads **fresh from the env files, never `os.environ`** (jasper-control is not restarted on a coupling change). | The whole `/state` block's shape changes; `intent_coherent` becomes vacuous. **PHASE-2** |
| `jasper/control/audio_health.py` → `_transport_state`, `_read_transport_state`, `_stopped_dsp_signal` | Takes `coupling: str \| None`; note L503 *"fan-in's default `loopback` coupling is timer-paced"* — a **pacing** dependence, not just a naming one. | **PHASE-2**, and a behavioural one (see §8 Q2). |
| `jasper/control/restart_broker.py` | Documents that `coupling_reconcile` restarts fan-in to apply a coupling. | **PHASE-2** (prose) |
| `jasper/source_intent.py` → `_USB_COUPLING_UNIT`, `_reconcile_usbsink`, `source_reconcile_lock` | `_USB_COUPLING_UNIT = "jasper-fanin-coupling-auto.service"` (L91); the source coordinator starts it and preserves the global `source → coupling` order. | The unit survives Phase 2 only if the auto pass still has a decision to make. **OWNER-DECISION** — if the coupling token dies, does `jasper-fanin-coupling-auto` remain as the USB-combo owner? |
| `jasper/audio_io.py` → `tts_wire_is_wide` (L931-979) | Mirrors `jasper.fanin_coupling.assistant_wire_is_wide`. Assistant IPC width is `wire_format == S32_LE AND coupling == shm_ring`. | The width predicate loses a conjunct — and with it the voice-restart trigger in `reconcile_coupling` (L794-826). **PHASE-2** |
| `rust/jasper-tts-protocol/src/lib.rs` → `from_box_declaration(..., coupling_is_shm_ring: bool)` | The Rust half of the same width predicate. *"wide format + shm_ring coupling is the ONLY wide pairing"*. | **PHASE-2** |
| `deploy/bin/jasper-audio-hardware-reconcile` | `from jasper.fanin_coupling import content_lane_format_for_coupling`. **Single writer** of `JASPER_OUTPUTD_CONTENT_FORMAT`. Also starts `jasper-fanin-coupling-auto.service` (L1468). | **PHASE-2** |
| `deploy/bin/jasper-outputd-failure-reconcile:211` | Remediation string naming the three-step ACTIVE-ring re-arm. | **PHASE-2** (prose) |
| Doctor: `check_fanin_coupling_value` (L1429), `check_fanin_coupling` (L1490, the largest check), `check_fanin_service` (L547), `check_fanin_ring_stall` (L1043, skip-if-loopback), `check_camilla_playback_format` (L1189), `check_audio_runtime_plan` (L1299), `check_active_ring_split_transport` (L1848), `check_ring_*` family (L1951–2612), `check_outputd_service` (L3339), `_outputd_transport_health`, `_transport_route_remedy` — all in `jasper/cli/doctor/audio_runtime.py` | The coupling's observability surface. `check_fanin_asound_wiring` (L395) is shared — see §5(b) ruling. | **PHASE-2** for the coupling-valued checks; the `check_ring_*` family **STAYS** (ring side). |

**AXIS-1 consumer count: 32** (9 machinery + 23 readers), counting one row
per consumer as tabulated above. Method: distinct file+symbol groups with a
demonstrated import-level or unit-level dependency on the coupling selector
or its reconciler.

### 2.2 AXIS-1-GROUPING — the pair-6 bonded round-trip (PHASE-1's target)

**The transport.** Two module constants, `jasper/multiroom/reconcile.py`:

```
214  GROUPING_LOOPBACK_PLAYBACK = os.environ.get(
215      "JASPER_GROUPING_LOOPBACK_PLAYBACK",
216      "hw:Loopback,0,6",
217  )
218  GROUPING_LOOPBACK_CAPTURE = os.environ.get(
219      "JASPER_GROUPING_LOOPBACK_CAPTURE",
220      "hw:Loopback,1,6",
221  )
225  GROUPING_LOOPBACK_CAPTURE_FORMAT = "S16_LE"
```

Raw `hw:` on both ends — deliberately **not** `plug:`, so no resampler is
inserted. **There is a third member of the triple**
(`GROUPING_LOOPBACK_CAPTURE_FORMAT`, not env-overridable) that any migration
must move with the other two.

**AXIS-1-GROUPING defines no ALSA PCM.** It opens pair 6 by raw `hw:` name,
bypassing `asoundrc.jasper`'s aliases entirely
(`deploy/alsa/asoundrc.jasper:242-247` says so explicitly). Consequence for
§5(b): deleting every `pcm.` block from `asoundrc.jasper` would **not** break
the grouping round-trip; only unloading the module would.

**Which roles traverse pair 6** (from the module headers):

| Role | CamillaDSP instances | Traverses `hw:Loopback,{0,1},6`? | Loopback-capturing instance | `enable_rate_adjust` |
|---|---|---|---|---|
| Plain/passive leader | camilla#1 | **No** — `File` sink → SNAPFIFO | — | #1 = `false` |
| Active-speaker leader | camilla#1 (`:1234`) + camilla#2 (`:1235`, `jasper-camilla-crossover.service`) | **Yes** | camilla#2 | #1 = `false`, **#2 = `true`** |
| Passive/"dumb" follower | camilla#1, out of the bonded path | **No** — snapclient `--player file:` → `MEMBER_CONTENT_FIFO` → outputd `dac_content` `ChannelPick` | — | #1 = `true` (solo defaults) |
| **Active follower** | camilla#1, **in** the bonded path | **Yes** | camilla#1 | **`true`** |

| Consumer (file → symbol) | Role | Breaks if the loopback COUPLING dies | Phase |
|---|---|---|---|
| `jasper/multiroom/reconcile.py` → the three constants + `_assemble_args` (the sole writer-side reader; builds `--soundcard hw:Loopback,0,6 --player alsa`) | Owns the round-trip's identity | **Nothing** — pair 6's grouping use is independent of the AXIS-1 coupling. It breaks when the *module* goes, not when the coupling does. | **PHASE-1** |
| `jasper/multiroom/follower_config.py` → `precheck_active_follower` (L213-214), `apply_prebuilt_follower_config` (L332, log-only) | Passes `capture_device=GROUPING_LOOPBACK_CAPTURE, capture_format=GROUPING_LOOPBACK_CAPTURE_FORMAT` into `build_baseline_profile_candidate(..., driver_domain=True)` | — | **PHASE-1** |
| `jasper/multiroom/active_leader_config.py` → `precheck_active_leader` (L262-263) | Same two kwargs, for camilla#2 | — | **PHASE-1** |
| `jasper/multiroom/leader_config.py`, `member_config.py` | The FIFO (non-aloop) halves + the rate-adjust policy statement | — | **STAYS** (they are the non-aloop path) |
| `jasper/cli/doctor/grouping.py` → `check_grouping_aloop_remnant` + `_aloop_proc_root` / `_pair_from_loopback_pcm` / `_grouping_pair_index` / `_derive_registered_pairs` / `_aloop_substream_owner` | The bounded-remnant measurement (see §5(f)) | — | **PHASE-1** |
| `jasper/cli/doctor/grouping.py` → `check_grouping_rate_adjust`, `check_grouping_channel_pick`, `check_grouping_leader_pipe`, `check_crossover_unit_installed` | Assert the leader's `rate_adjust:false`, the aloop-vs-FIFO XOR, the leader pipe sink, and crossover-unit installation | — | **PHASE-1** (their prose encodes the aloop path) |
| `jasper/control/grouping_supervisor.py:313` | Names a **deferred** signal: *"round-trip starvation of the active lane (the camilla#2 loopback going silent) is a separate signal outputd does not yet surface — deferred until observed."* | — | **PHASE-1** (see §8 Q4) |
| `jasper/camilla_config_contract.py` → `snd_aloop_rate_adjust_oscillation_reason` | Shared AXIS-1 / AXIS-1-GROUPING guard — see §5(d) | — | **OWNER-DECISION** |
| `deploy/systemd/jasper-snapclient.service:61` | Comment naming the default `hw:Loopback,0,6` | — | **PHASE-1** (prose) |
| `rust/jasper-outputd/src/dac_content.rs` | The passive-member `dac_content` FIFO lane's header names aloop substreams | — | **STAYS** (FIFO path) |

**AXIS-1-GROUPING consumer count: 10.**

**The interlock — grouping and the ring are mutually exclusive at this SHA,
enforced from both directions, fail-safe.**

1. `jasper/multiroom/reconcile.py` (the ring-armed box refusal, ~L1890-1920)
   → `fall_back_to_solo()`, block reason `ring_armed_box_cannot_bond`:
   > *"a ring-armed box cannot join a bond until ring v2 (P8); disarm the ring
   > (`jasper-fanin-coupling-reconcile loopback`) to group this speaker.
   > Staying solo."*
2. `jasper/audio_runtime_plan.py` → `coupling_supported_for_route`
   (`_GROUPED_SHM_RING_REASON = "fanin_shm_ring_coupling_unsupported_while_grouped"`),
   consulted by `jasper/multiroom/active_leader_config.py:211-218`.

**A bonded box is therefore *required* to be on the `loopback` coupling at
this SHA.** Phase 1 must retire that gate, not merely add a ring lane. This
is the single hardest structural fact the campaign inherits.

**The clock question Phase 1 must re-answer.** The `rate_adjust: true` on the
loopback-capturing instance is justified *by snd-aloop specifically*, in
three places:

- `jasper/multiroom/member_config.py`: *"its sink is the ALSA loopback,
  **which HAS a clock to track**"*
- `jasper/multiroom/reconcile.py`: *"This is DELIBERATELY snd-aloop here (not
  the inv-2 FIFO): the active follower needs **the loopback's clock** for
  CamillaDSP's `rate_adjust` to track"*
- `jasper/multiroom/reconcile.py` → `snapclient_argv` docstring: *"the
  `snd_pcm_delay` trap is avoided not by dodging snd-aloop but by CamillaDSP
  owning the clock + `--latency` nulling the fixed pipeline latency"*

The ring contract states the **opposite** for its own transport —
`jasper/fanin_coupling.py`: `RING_CAMILLA_ENABLE_RATE_ADJUST = False`, because
(`jasper/active_speaker/camilla_yaml.py`) *"a blocking slot handshake gives
the rate controller nothing to adjust TO, and rate_adjust over an
snd-aloop-class transport is a documented oscillation shape in this repo."*

**Census verdict: rate-tracking does not carry over to a ring as-is.**
Whether a grouping ring can host a rate-tracked bonded endpoint is
**UNKNOWN from code** — see §8 Q1.

**Ring involvement that already exists on the grouping side** — and it is
*not* the audio path. `jasper/multiroom/reconcile.py` imports
`RING_ACTIVE_PLAYBACK_DEVICE` and `RING_ACTIVE_CONTENT_FILE` /
`ring_writer_lock_path` solely for (a) three `log_event` `pcm=` fields and
(b) the ACTIVE-ring **writer-lock release barrier** (§5(f)). That is the
*downstream* camilla#2 → outputd hop, not the grouping round-trip. **No
grouping audio crosses a ring at this SHA.**

### 2.3 AXIS-2 — renderer ingress (fleet default unarmed)

**The registry.** `jasper/renderer_lanes.py` → `RENDERER_LANES` defines **four**
lanes (declaration order only — the dataclass has no index field; fan-in's
lane index comes from `JASPER_FANIN_INPUT_RENDERERS` order):

| `label` | renderer | unit | `device_key` | `aloop_device` | `ring_device` |
|---|---|---|---|---|---|
| `spotify` | librespot | `librespot.service` | `JASPER_LIBRESPOT_DEVICE` | `librespot_substream` | `librespot_ring_lane` |
| `bluealsa` | bluealsa-aplay | `bluealsa-aplay.service` | `JASPER_BLUEALSA_DEVICE` | `bluealsa_substream` | `bluealsa_ring_lane` |
| `correction` | ephemeral `aplay` | **none** | `JASPER_CORRECTION_DEVICE` | `correction_substream` | `correction_ring_lane` |
| `airplay` | shairport-sync | `shairport-sync.service` | `JASPER_SHAIRPORT_DEVICE` | `shairport_substream` | `shairport_ring_lane` |

**There is no `usbsink` row** — USB is `LaneSource::Direct`, selected by a
different key (`JASPER_FANIN_USB_DIRECT` / `_DEVICE`, default `hw:UAC2Gadget`),
and Rust **refuses** `RENDERER_RING_LANES=usbsink` outright.

`device_for(lane, armed)` returns `lane.ring_device if armed else
lane.aloop_device`. `render_env_text` writes a device line for **every** lane,
armed or not — *"an omitted key would leave the previous armed value in place
… and 'disarm did nothing' is the worst possible failure for a rollback path."*

**FLEET DEFAULT = UNARMED, established six ways** (each verified):
(1) the module's own rule — *"the armed set is empty unless an operator
explicitly armed it, so an unarmed box is byte-identical to one on which this
mechanism does not exist"*; (2) `read_armed_labels()` returns `()` on a
missing/unreadable file — *"which is the shipped fleet state"*; (3) **nothing
writes the file at install** (`grep renderer_lanes deploy/install.sh` → no
hits; install ships only the *inert* conf.d); (4) every unit loads the file
optionally (`-` prefix) with an in-unit default naming the aloop device;
(5) Rust `env_csv_labels("JASPER_FANIN_RENDERER_RING_LANES")` → empty vec when
unset; (6) `deploy/alsa/conf.d/61-jts-renderer-lanes.conf:8-14` and
`tests/test_renderer_ring_lanes.py:10` (*"The default is nothing."*).

| Consumer | Role | Breaks if the loopback COUPLING dies | Phase |
|---|---|---|---|
| `jasper/renderer_lanes.py` → `RENDERER_LANES`, `device_for`, `render_env_text`, `read_armed_labels`, `arm_refusal_reason`, `render_renderer_lanes_env` | The lane registry + **single writer** of `/var/lib/jasper/renderer_lanes.env` | **Nothing.** Declared independent in both languages — Python: *"**They are independent transports.** A renderer ring carries renderer → fan-in; the coupling describes fan-in → CamillaDSP"*; Rust: *"fan-in does not consult the CamillaDSP coupling … (a loopback-coupled box can ring-ingress a renderer and vice versa)"* | **STAYS** (AXIS-2) |
| `jasper/cli/audio_config.py` → `_cmd_renderer_lanes` (`--arm`/`--disarm`/`--set`) | The only arm/disarm entry point; operator-triggered only, never at boot or install | — | **STAYS** |
| `deploy/systemd/librespot.service` (`--device ${JASPER_LIBRESPOT_DEVICE}`), `bluealsa-aplay.service.d/jts-output.conf` (`--pcm=${JASPER_BLUEALSA_DEVICE}`) | Env-substitution indirection, in-unit default = the aloop alias | — | **STAYS** |
| `deploy/bin/jasper-apply-airplay-mode` + `deploy/shairport-sync.conf.template` (`output_device = "__RENDERER_DEVICE__"`) | The conf-renderer shape: reads the map's **resolved** value at `ExecStartPre`, never the armed set | — | **STAYS** |
| `deploy/alsa/asoundrc.jasper` `*_substream` aliases (pairs 0,1,2,4) | The unarmed write side | — | **STAYS** until an AXIS-2 ruling |
| `deploy/alsa/conf.d/61-jts-renderer-lanes.conf` (8 PCMs: 4 `type jts_ring` at `period_frames 256`/`n_slots 16`, 4 `type plug` at 48 k/2/S16_LE) | The armed alternative; inert until armed | — | **STAYS** |
| `rust/jasper-fanin/src/mixer.rs` → `enum LaneSource {Lane, Direct, Ring}`, `open_input` / `open_direct_input` / `ring_capture::open_ring_input`, `lane_source()` | The three ingress arms. At most one of `direct`/`ring` is ever `Some`. | — | **STAYS** |
| `rust/jasper-fanin/src/config.rs` → the five compiled-in default input PCMs `hw:Loopback,1,{0..4}` | The fleet ingress roster (nothing in the repo writes `JASPER_FANIN_INPUT_PCMS`) | — | **STAYS** |
| Doctor: `check_shairport_sync_loopback_plughw`, `_ring_renderer_devices`, `_fanin_lane_busy_owner_matches`, `_probe_open_as_user` / `check_renderer_device_resolvable` (`jasper/cli/doctor/renderers.py`); `_FANIN_EXPECTED_ALOOP_INPUTS` / `_fanin_expected_inputs` / `check_fanin_asound_wiring` / `check_renderer_ring_lanes` (`audio_runtime.py`) | AXIS-2 observability. `_fanin_lane_busy_owner_matches` carries its own retirement note: *"It retires when the snd-aloop renderer lanes themselves are deleted… **Fleet arming state is not that trigger.**"* | — | **STAYS** |
| `jasper/cli/doctor/_shared.py` → `_loopback_playback_active()` | Despite the name, **measures AXIS-2 only** — reads `/proc/asound/Loopback/pcm0p/sub{0..4}/status` and explicitly skips sub 7 (fan-in's own output). Sole production caller: `jasper/cli/doctor/aec.py:1152`. | — | **STAYS** (see §7 S6) |
| `jasper/web/airplay_setup.py`, `jasper/source_state.py`, `jasper/control/airplay_health.py`, `jasper/bluetooth/handlers/a2dp_sink.py` | Household-facing copy and liveness heuristics that name the aloop lane | — | **STAYS** (prose drifts on an AXIS-2 ruling, not on Phase 2) |

**AXIS-2 consumer count: 11.**

**Every AXIS-2 consumer is STAYS with respect to Phase 2.** Not one of them
reads the coupling token.

### 2.4 AXIS-3 — correction/measurement + AEC

**The correction lane.** `jasper/audio_measurement/correction_lane.py` is the
declared SSOT for the alias name:

```
WAV -> correction_substream -> jasper-fanin -> CamillaDSP -> outputd
```

`CORRECTION_SUBSTREAM = "correction_substream"` = `hw:Loopback,0,4` (pair 4);
fan-in reads `hw:Loopback,1,4`.

**It is independent of the AXIS-1 coupling.** `correction_play_device()`'s
only input is `renderer_lanes.read_armed_labels()` — it never reads
`JASPER_FANIN_CAMILLA_COUPLING`. Resolution is **per call**, never cached at
import, and it is the lane's *one* transport reader so telemetry cannot
disagree with the spawn. On an armed box it returns `correction_ring_lane`;
on the fleet default it returns the aloop alias — *"byte-identical to
pre-P6c behaviour."*

**AEC: the snd-aloop reference path is already fully retired.**

| Consumer | Role | Breaks if the loopback COUPLING dies | Phase |
|---|---|---|---|
| `jasper/audio_measurement/correction_lane.py` → `CORRECTION_SUBSTREAM`, `correction_play_device`, `correction_play_argv`, `CORRECTION_PLAY_UMASK` | The measurement/commissioning playback lane. ~20 call sites all route through the one reader. | **Nothing** — independent of the coupling | **STAYS** (AXIS-3) |
| `jasper/cli/aec_bridge.py` → `REF_SOURCE = "outputd_udp"`, `OUTPUTD_REF_UDP_PORT = 9891`, `_resolved_reference_source`, `RETIRED_REF_SOURCE_ALSA` | The AEC bridge. **Its only reference source is UDP.** A stale `alsa` value converges with a WARN rather than refusing to start. | Nothing | **STAYS** |
| `jasper/audio_io.py` → `UdpMicCapture` (port 9876) | The mic transport that replaced the `LoopbackAEC` card in 2026-05-11 | Nothing | **STAYS** |
| `jasper/cli/aec_tune.py` (`DEFAULT_REFERENCE_UDP_TARGET = "127.0.0.1:9891"`) | The second former aloop reader, also migrated (U4/P7-2) | Nothing | **STAYS** |
| `deploy/bin/jasper-aec-reconcile` (writes `JASPER_OUTPUTD_REFERENCE_UDP_TARGET=127.0.0.1:9891`; chip ref = `hw:CARD=Array,DEV=0`) | The reference fanout owner — USB, not snd-aloop | Nothing | **STAYS** |
| `jasper/cli/doctor/aec.py`, `aec_probe.py` | AEC health. `aec_probe.py` **removed** its `_loopback_playback_active()` precheck at #2585 as *"PERMANENTLY INERT on a ring-armed box … it read as protection while protecting nothing."* | Nothing | **STAYS** |
| `deploy/alsa/asoundrc.jasper:314` → `pcm.jasper_ref` | **Defined with zero readers.** Retained only because P9-E, not P7, deletes the PCMs. The doctor asserts its *presence* as deployed-vs-shipped drift detection. | Nothing | **PHASE-2-adjacent** — see §5(b) |

**AXIS-3 consumer count: 7.**

**Verdict: AXIS-3's AEC half no longer touches snd-aloop at all.** Its one
remaining snd-aloop dependency is the *correction lane* (pair 4), reached by
`jasper/cli/aec_commission.py` and `jasper/cli/doctor/aec_probe.py` via
`run_correction_play(...)` on an unarmed box.

> **One honest nuance, labelled INFERENCE.** On a default box, *outputd
> itself* obtains the audio it monitors via `outputd_content_capture` =
> `hw:Loopback,1,6`. That is the AXIS-1 hop, not the AEC reference path — the
> bridge never touches it. The AEC reference path proper (outputd → bridge)
> reads zero snd-aloop.

---

## 3. THE STATE MACHINE REALITY

### 3.1 What "one bidirectional state machine" actually means

The enumerator's finding is **confirmed and sharpened**. `coupling_reconcile.py`
+ `coupling_auto.py` are not two code paths (a loopback one and a ring one)
that could be separated by deleting lines. They are **one transition engine
whose destination is a parameter**. Concretely:

- **One entry, one normalizer.** Every path enters through
  `reconcile_coupling` → `_reconcile_coupling_inner(desired_raw, …)`.
  `desired_raw` is normalized once by `resolve_coupling` (L909, L912) into a
  two-valued `desired`. There is no separate "loopback function".
- **The direction is computed, not coded.**
  `_reconcile_coupling_inner` L1003:
  `direction="disarm" if desired == COUPLING_LOOPBACK else "arm"`.
  The *same* function body produced both.
- **The ordered daemon sequence is shared and reversed, not duplicated.**
  ARM = outputd → fan-in → camilla (`_arm_ring`, L3928-4286).
  DISARM = camilla → fan-in → outputd (`_run_loopback_daemon_ops`, L4314-4332,
  called by `_disarm` L4335 *and* `_recover_to_loopback` L4682 *and*
  `_block_unsupported_coupling` via `_disarm` L1722). One sequence, three
  callers.
- **The ring half depends on the loopback half as its failure destination.**
  `_fail_ring_arm` (L3839) — the shared recovery for *every* arm-stage
  failure — calls `_recover_to_loopback`. The ring cannot fail without a
  loopback to fail *into*. That is the single most consequential structural
  fact for Phase 2.

**Three distinct transition states exist** (`CouplingResult.direction`):
`arm`, `disarm`, `confirm` — plus two non-transition results, `blocked`
(`_block_unsupported_coupling`) and `error` (env write failure, L982).
`AutoResult` wraps a `CouplingResult` and adds the `owned` axis
(auto-owned vs operator-frozen).

**Which states/transitions are loopback-reachable** (every one that can
*land on* loopback is enumerated in §4). Structurally:

| Transition | Loopback-reachable? | Evidence |
|---|---|---|
| `arm` (→ shm_ring) | **Yes, on every failure** — 4 preflight gates + period gate + slot gate + content-format converge (timeout *and* refusal) + 3 ordered daemon steps all route to `_fail_ring_arm` → `_recover_to_loopback` | L3928-4286 |
| `disarm` (→ loopback) | Yes — this *is* the loopback destination | `_disarm` L4335 |
| `confirm` (already at desired) | **Yes, two escapes**: ioplug-caps failure recovers immediately (L1026-1053); `RING_CONFIRM_STRIKE_LIMIT` consecutive camilla failures recover (L1087-1135). A third path *escalates* to `_arm_ring` (L1055-1077), which can then fail into loopback. | L1006-1153 |
| `blocked` | Yes — forces loopback + full disarm | L1649-1788 |
| `error` | No — aborts before daemon ops, restores snapshots | L960-984 |

**Not partitionable** is therefore precise: you cannot delete "the loopback
lines" because (a) the destination is a value, not a branch, and (b) the
recovery ladder's *only* safe destination is loopback. A Phase-2 deletion is
a re-targeting of the recovery destination, not a subtraction.

### 3.2 The measured loopback-branch-site count — the "~126" claim re-derived

**The campaign handoff claimed "~126 branch sites across
`coupling_reconcile.py` + `coupling_auto.py`". The number reproduces
exactly, and it is not a count of branch sites.**

| Method | `coupling_reconcile.py` | `coupling_auto.py` | Total |
|---|---:|---:|---:|
| **Lines containing the case-sensitive literal `loopback`** | 116 | 10 | **126** |
| Lines matching `/loopback/i` | 143 | 13 | 156 |
| Lines matching `/loopback\|aloop/i` | 147 | 13 | 160 |
| Lines matching `/loopback\|aloop/i` **excluding** strings & comments (Python `tokenize`) | 36 | 3 | **39** |
| Lines naming `COUPLING_LOOPBACK` | 21 | 3 | 24 |
| Lines naming `COUPLING_SHM_RING` | 12 | 2 | 14 |
| **`if`/`while`/ternary/`assert` conditions whose source text names `loopback`, `shm_ring`, `COUPLING_*`, or `is_shm_ring_coupling`** (Python `ast`) | **12** | **0** | **12** |

**Method for the 126:** `sum(1 for l in open(p) if 'loopback' in l)` over the
two files. **Method for the 12:** `ast.walk`, collecting `If`/`While`/`IfExp`/
`Assert` test nodes, rendering each via `ast.get_source_segment`, and matching
`/COUPLING_LOOPBACK|COUPLING_SHM_RING|is_shm_ring_coupling|shm_ring|loopback/i`
against the condition source only.

**Finding — the claim's label is wrong, its arithmetic is right.** 126 is a
grep line count dominated by prose: only **39 of 160** token lines are code at
all, and only **12** are genuine control-flow branches on the transport axis.
The remaining ~114 are docstrings and comments — which matters, because
*prose is the bulk of what a deletion has to rewrite*, and mistaking it for
branch logic would size the code work ~10× too high and the prose work far
too low.

**The 12 real branch sites, enumerated:**

| Line | Enclosing symbol | Condition |
|---|---|---|
| 549 | `_reconcile_camilla` | `status == "skipped" and coupling != COUPLING_SHM_RING` |
| 669 | `_restart_fanin_coordinated` | `coupling == COUPLING_LOOPBACK` |
| 1003 | `_reconcile_coupling_inner` (ternary) | `desired == COUPLING_LOOPBACK` |
| 1020 | `_reconcile_coupling_inner` | `desired == COUPLING_SHM_RING` |
| 1087 | `_reconcile_coupling_inner` | `desired == COUPLING_SHM_RING` |
| 1155 | `_reconcile_coupling_inner` | `desired == COUPLING_SHM_RING` |
| 1178 | `_reconcile_coupling_inner` (ternary) | `_leaves_live_shm_ring_bridge(outputd_snapshot.text)` |
| 1695 | `_block_unsupported_coupling` | `stale_non_loopback` |
| 1732 | `_block_unsupported_coupling` (ternary) | `do_kick_hardware is not None and _leaves_live_shm_ring_bridge(…)` |
| 3157 | `ring_topology_ready` | `topology_supports_shm_ring(topology)` |
| 4821 | `_outputd_actions` | `coupling == COUPLING_SHM_RING` |
| 4884 | `_sync_process_env_for_emit` | `coupling == COUPLING_SHM_RING` |

**`coupling_auto.py` has ZERO axis conditionals.** Its loopback selection is
purely a *value* assignment on gate short-circuit
(`coupling = COUPLING_LOOPBACK`, L266) plus a default parameter
(`current_coupling: str = COUPLING_LOOPBACK`, L207). This is the cleanest
module in the family and the least entangled.

**One branch is a behavioural dependence on snd-aloop, not a naming one.**
L669 in `_restart_fanin_coordinated`:
> *"On loopback the plain restart is kept (snd-aloop decouples the two)."*
The coordinated (camilla-paused) fan-in restart exists **because the ring
couples the two daemons where snd-aloop did not**. Under Phase 2 there is no
"plain restart" case left. Recorded here, not designed around.

### 3.3 Symbol-table corrections to the inherited inventory

The enumerator's condensed symbol table for `coupling_reconcile.py` contains
line-range errors (it did not distinguish nested `def`s, and several ranges
do not match the AST). Verified corrections for symbols this campaign will
cite:

| Symbol | Enumerator said | Actual (AST) |
|---|---|---|
| `do_reconcile` / `do_confirm_reconcile` | "L902-1155", listed as top-level | **nested** inside `_reconcile_coupling_inner`; `do_reconcile` L897, `do_confirm_reconcile` L902 |
| `_reconcile_coupling_inner` | not listed as a range | L830-1181 |
| `reconcile_auto` | "L1188-1613" | L1213-1616 |
| `_block_unsupported_coupling` | "L1620-1811" | L1649-1788 |
| `_arm_ring` | "L3853-4267" | L3928-4286 |
| `_disarm` | "L4314-4472" | L4335-4425 |
| `_recover_to_loopback` | "L4622-4710" | L4622-4716 |
| `ring_topology_ready` | "L3151-3420" (bundled) | L3091-3208 (+ `_strict` L3211-3218) |
| `main` | "L5002-5179" | L5001-5076 |

**Use the AST table (§1.2 / this section), not the enumerator's, for line
citations.** Prefer symbol names to line numbers per the repo's doc rule 5.

---

## 4. LOOPBACK-LANDING STATES

**This is Phase 2's design contract.** Every path by which a box selects, or
falls back to, the loopback coupling. Completeness here is the whole game, so
each entry names its evidence and I flag where I could not prove exhaustiveness.

### 4.1 Default and fail-safe resolution

**L1 — Unset key (the fresh/never-touched box).**
`jasper/fanin_coupling.py` → `resolve_coupling(None)` returns
`COUPLING_LOOPBACK`. Also `coupling_reconcile.read_persisted_coupling`
returns `COUPLING_LOOPBACK` on an unreadable `fanin.env`.
Rust mirror: `rust/jasper-fanin/src/config.rs` → `Coupling::from_env_value`,
*"Fail-safe to `Loopback` on unset/empty/unknown — matches Python's
`resolve_coupling`"*.

**L2 — Unparseable / removed value (the fail-safe rung).**
`resolve_coupling` returns loopback for any token not in `VALID_COUPLINGS` —
a typo, or the deleted `transport_pipe`. `coupling_value_removed` detects
"present but unrecognized"; `reconcile_auto` then converges the box **loudly
and ignoring the operator marker** (`result=removed_coupling_failsafe`),
rewriting `fanin.env` and running the ordered disarm.

**L3 — Route-blocked: any grouping-enabled box.**
`jasper/audio_runtime_plan.py` → `coupling_supported_for_route`: the **one**
blocked combination is `shm_ring` + `{active_leader, active_follower,
invalid_grouping}`.
> *"the ring is solo-stereo-only until ring v2 (P8); arming it on a bonded
> box would strand the leader's local output."*
Two enforcement points: `ring_route_ready` (pre-empts it in the auto pass so
the boot oneshot does not fail on a healthy grouped box) and
`_block_unsupported_coupling` (forces loopback + clears every
reconciler-owned outputd key + runs a full disarm).
**This is the interlock Phase 1 must resolve** — it is the symmetric half of
the multiroom reconciler's "ring-armed box cannot bond" gate
(`jasper/multiroom/active_leader_config.py:218`,
`jasper/multiroom/reconcile.py:1915`, both emitting
*"Run `jasper-fanin-coupling-reconcile loopback` before bonding."*).

### 4.2 Ring-eligibility gate refusals (auto pass and/or operator arm)

The shared spine is `_SHARED_RING_PREFLIGHTS` (L3365-3370), in this order:
`ring_topology` → `ring_assets` → `ring_wire_caps` → `ring_edge_width`.
`default_ring_gates()` prepends `ring_roleful_unattended`; the auto pass
appends `ring_route`, `ring_geometry`, `ring_slot_geometry`. **The first
failing gate short-circuits to loopback** with its detail as the reason
(`coupling_auto.resolve_auto_decision` L257-268).

**L4 — Ring-ineligible saved topology.** `ring_topology_ready` refuses:
explicit-mono; a PASSIVE composite dual-DAC ("neither ring"); a roleful
topology that resolves no ACTIVE-ring width; and a plain-stereo box carrying
a **stale roleful/subwoofer `speaker_groups`** from a prior campaign (the
refusal names `jasper-output-topology-reset` as the remedy).

**L5 — Unreadable topology (fail-CLOSED, both paths).**
`ring_topology_ready_strict`. Note the recorded direction change: the
operator arm *used to* fail open, and stopped, because *"its stated backstop
(outputd's own guard) was shown to fail open on the same error"*.

**L6 — Roleful box the UNATTENDED pass refuses.**
`ring_roleful_unattended_ready` — refuses by default, admits exactly two
proven graph shapes: (1) a hardware-fingerprint-matched applied baseline
(`applied_baseline_hardware_match`), (2) the all-muted staged anchor
(`_staged_anchor_identity` + `_anchor_is_all_muted`). Everything else —
unreadable topology, no applied record and no anchor, stale fingerprint, an
anchor not terminally muted, a corrupt (non-UTF-8) applied record — lands
loopback. **Auto-only:** `_arm_ring` does not run this gate
("an operator arriving at the explicit arm has already decided").

**L7 — Ring platform assets absent/half-installed.** `ring_assets_ready`.

**L8 — ioplug capability gap (the degraded-deploy walk).**
`ring_wire_caps_ready` — a stale `.so` beside new daemons cannot parse the
resolved wire's `format`/`channels`. A RECORD compare, never an open-probe.

**L9 — Wire-width disagreement across declaring ends.** `ring_edge_width_ready`
(conf.d vs fanin.env vs outputd.env vs the loaded graph — files written at
different times, so a half-applied render is what it catches).

**L10 — Period-geometry mismatch.** `ring_geometry_ready` — conf.d ring
period ≠ outputd's resolved DAC period.

**L11 — Ring-A slot-count mismatch.** `ring_slot_geometry_ready` — the
old-default `=8` residue class. Preceded by a self-heal
(`_migrate_stale_fanin_ring_slots`) in both the auto pass and `_arm_ring`.

**L12 — Composite wide-wire rule.** `composite_ring_wire_ready` — a roleful
COMPOSITE resolves width 4 and reaches the ACTIVE arm, so it carries this
extra condition.

**L13 — ACTIVE-ring endpoint not staged.** `active_ring_endpoint_proof`,
inside `ring_topology_ready`'s ACTIVE arm.

**L14 — Any gate that cannot evaluate.** `resolve_auto_decision` L258-263
catches `OSError|ValueError|RuntimeError|ImportError` per gate and converts
it to `ok=False`:
> *"A gate that cannot even evaluate is NOT proven eligible — fail safe to
> loopback (never arm a ring on an indeterminate gate)."*

### 4.3 Operator-frozen boxes

**L15 — `JASPER_FANIN_COUPLING_CHOICE=operator` + coupling `loopback`.**
`coupling_auto.is_operator_choice` / `resolve_auto_decision` returns
`owned=False` and preserves `current_coupling` verbatim. In `reconcile_auto`
this branch passes **`ring_gates=()`** — an *empty* tuple.

> **Load-bearing consequence, stated in the code itself** (`reconcile_auto`
> docstring): *"on an operator-pinned box the gate set is never even
> constructed and neither `ring_wire_caps_ready` nor `ring_edge_width_ready`
> runs, whatever the box's hardware would say. … it means 'the fleet's
> fail-closed wire gates protect this box' is FALSE for every pinned box, and
> **all three armed fleet boxes are pinned**."*

So a pinned-loopback box is inert to every eligibility gate, and a
pinned-`shm_ring` box is inert to every safety gate. Phase 2 must decide what
an operator marker *means* when there is only one transport
(**OWNER-DECISION**).

### 4.4 Failure-driven recovery (the ladder Phase 2 retargets)

**L16 — Any ARM-stage failure.** `_fail_ring_arm` → `_recover_to_loopback`.
Triggered by: each of the shared preflights, the period gate, the slot gate,
the content-format converge kick (**both** its timeout and its refusal
branch, which differ only in whether the kick is re-run), and each of the
three ordered daemon steps (outputd restart, fan-in restart, camilla
reconcile). `_recover_to_loopback` writes `fanin.env`, clears **every**
reconciler-owned outputd content-source key, syncs the process env, and runs
`_run_loopback_daemon_ops`.

**L17 — CONFIRM-path strike escalation.**
`RING_CONFIRM_STRIKE_LIMIT = 2` failures inside
`RING_CONFIRM_STRIKE_WINDOW_SEC = 24*3600`, persisted at
`/var/lib/jasper/ring-confirm-strikes.json`
(`_read/_record/_clear_ring_confirm_failures`) → `_recover_to_loopback`
with `result=confirm_ring_failure_escalated`.
**This corrects a premise in the campaign brief.** The brief states the
ladder was kept partly because *"oneshot units mean in-memory counters
accumulate nothing (persistence buys two-strike without a retry)"*. At this
SHA the counter **is** persisted to disk, with a 24 h window and explicit
clear-on-success (`_clear_ring_confirm_failures` on a successful confirm and
on a **complete** disarm — a *partial* disarm deliberately keeps the record).
A strike that cannot be written logs
`result=ring_confirm_strike_write_failed` rather than silently never
escalating. Phase 2 inherits a working two-strike ladder, not a stub.

**L18 — CONFIRM-path ioplug-caps failure recovers immediately.**
L1026-1053: an armed box whose *installed* ioplug cannot parse the wire goes
straight to `_recover_to_loopback` **without** re-arming, because
*"re-running the arm would meet the same -EINVAL. The only state that plays
audio is loopback."* Note this branch also calls
`_clear_ring_confirm_failures()`.

**L19 — CONFIRM-path incoherence escalates to a full arm.**
L1055-1077 (`_ring_confirm_needs_self_heal` → `_arm_ring`), which can then
land on any of L16's failures. This is the **mid-migration** state class.

**L20 — Camilla-unreachable recovery re-seeds the statefile directly.**
`_reseed_loopback_statefile` calls
`safe_graph_for_current_topology(topology, coupling=COUPLING_LOOPBACK)` and
`apply_safe_graph_decision_to_statefile`. Closes the hole where *"the env
said loopback; the graph never moved."* Topology-aware: a roleful box gets
its roleful/parked graph, not a flat stereo one.

**L21 — USB-intent fail-closed preserves the current coupling.**
`reconcile_auto` L1379-1410: a malformed/unreadable
`/var/lib/jasper/source_intent.env` narrows the pass to the USB safety action
and **preserves** the current valid coupling (forcing loopback only if the
persisted value is also removed). So this *holds* a loopback box on loopback
rather than landing it there.

### 4.5 Fresh-box semantics (post-P6/P7) — verified from code

A fresh box has no `JASPER_FANIN_CAMILLA_COUPLING` line, so **every fresh box
starts on loopback** by L1. `deploy/install.sh:1577` then runs
`jasper-fanin-coupling-reconcile --auto --reason install`, and the box's
class decides:

- **FLAT (plain-stereo single-sink) fresh box → gates onto the ring.**
  `ring_roleful_unattended_ready` returns `True, "topology is not roleful"`;
  `ring_topology_ready`'s STEREO arm admits it
  (`topology_supports_shm_ring`); the remaining gates are asset/geometry
  facts. If all pass, the auto pass arms `shm_ring`. The startup graph
  follows: `safe_graph_for_current_topology` selects the **ring** flat config
  under `shm_ring`, else the loopback flat `outputd-cutover.yml`.
- **ROLEFUL fresh box → parks all-muted, stays loopback.**
  `ring_roleful_unattended_ready` refuses (no applied baseline yet, and the
  loaded graph must be the published all-muted anchor to pass arm 2). Per
  `AGENTS.md`'s outputd section, the statefile is seeded from the
  active-speaker runtime contract: flat `outputd-cutover.yml` only when the
  saved topology permits, otherwise a matching **all-muted active startup
  graph**, and — when a roleful topology has not staged one yet — a generated
  **DAC-less all-muted parked graph** so a mid-commission box can still take
  deploys. `safe_graph_for_current_topology`'s docstring confirms the parked
  shape is transport-agnostic: *"its sink is a `File`, so it is DAC- and
  transport-agnostic by construction."*

**Verdict: the brief's characterisation is correct** — flat box gates onto
the ring, roleful box parks all-muted. Both, however, *begin* on loopback and
reach their end state through the auto pass.

### 4.6 Non-landing states worth recording

**L22 — Entry-lock contention aborts before any write.**
`_acquire_entry_lock` / `_handle_entry_lock_contention`: on contention past
the 10 s bounded wait the verb aborts **before** touching env or daemons, so
the box keeps whatever it had. The unit lands `failed` and is
doctor-visible; nothing retries automatically (recovery is event-driven:
next boot, deploy, `/sources/` toggle, or manual start).

**L23 — Env-write failure.** `_reconcile_coupling_inner` L960-984 restores
both snapshots and returns `direction="error"` before any daemon op.

### 4.7 Exhaustiveness statement

I enumerated these by three independent means: (a) reading every path in
`_reconcile_coupling_inner`, `reconcile_auto`, `_block_unsupported_coupling`,
`_arm_ring`, `_disarm`, and the confirm ladder; (b) an AST enumeration of the
12 axis conditionals (§3.2); and (c) a mechanical listing of **all 21
non-comment sites naming `COUPLING_LOOPBACK`** in `coupling_reconcile.py`,
each attributed to its enclosing function. Every such site maps to an entry
above.

**Residual risk, named:** a landing state that reaches loopback *without*
naming `COUPLING_LOOPBACK` — e.g. by writing the literal string `"loopback"`
or by relying on `resolve_coupling`'s fail-safe from a caller outside these
two files — would not be caught by (c). (a) and (b) cover the two files;
callers elsewhere are covered by the §2.1.2 import map. I did **not**
exhaustively prove the negative across all 405 candidate files.

---

## 5. SHARED INFRASTRUCTURE RULINGS

### (a) snd-aloop kernel module — who needs it, per axis

**Exactly two shipped files provision it** (`git ls-files | grep -i aloop`):

- `deploy/modprobe.d/snd-aloop.conf` — 57 lines of allocation comment plus
  one options line (L58):
  ```
  options snd-aloop enable=1 index=6 id=Loopback pcm_substreams=8 pcm_notify=0
  ```
- `deploy/modules-load.d/snd-aloop.conf` — the single line `snd-aloop`.

**Installed by** `deploy/install.sh` → `install_alsa()` (L1260-1270): two
verbatim `install -m 0644` copies, then `rmmod snd_aloop 2>/dev/null || true;
modprobe snd-aloop || true`. **No installer validation** — both are `|| true`,
so a failed load is silent at install time. No systemd unit loads snd-aloop
(the only `modprobe` in `deploy/systemd/` is
`jasper-usbgadget.service:54 ExecStartPre=/sbin/modprobe libcomposite`,
unrelated).

**`pcm_substreams=8` is the shipped value**, and the file records *why it does
not shrink*: pair 5 was freed by P9-C but **"`pcm_substreams` stays 8 — no
renumbering, exactly like pair 3."** Test-pinned at
`tests/test_fanin_wiring.py:174` (`assert "pcm_substreams=8" in conf`) and
`tests/test_doctor_grouping_remnant.py:229-243` (parses the options line and
asserts `grouping._ALOOP_SUBSTREAMS == _modprobe_substreams()`).

**RULING — who needs the module, per axis:**

| Axis | Needs the module? | Which pairs |
|---|---|---|
| AXIS-1 (coupling) | Yes | 6 (passive content), 7 (program) |
| AXIS-1-GROUPING | Yes | 6 (raw `hw:`, no PCM alias needed) |
| AXIS-2 (renderer ingress) | **Yes, on every unarmed lane** | 0, 1, 2, and 3 (fan-in's idle read fallback) |
| AXIS-3 (correction) | Yes, on an unarmed correction lane | 4 |

**If AXIS-1 dies but AXIS-2 lives: the module stays, `pcm_substreams` stays
≥5, and `deploy/modules-load.d/snd-aloop.conf` stays.** Only pairs 6 and 7
lose their AXIS-1 consumers; pair 6 keeps its grouping consumer until
Phase 1 lands. `pcm_substreams` cannot drop below 7 while the grouping pair
is index 6 — pinned by `tests/test_doctor_grouping_remnant.py:246-268`.

### (b) `deploy/alsa/asoundrc.jasper` — PCM attribution and the P9-E verdict

**Installed as** `/etc/asound.conf` via a render chain
(`deploy/install.sh:1326-1336`: copy → `jasper_asound_render_template` →
`/usr/local/sbin/jasper-render-asound-conf` → `/var/lib/jasper-asound/asound.conf`
→ symlink).

| Line | Definition | Target | Axis |
|---|---|---|---|
| 16 | `defaults.pcm.rate_converter` (rendered) | global | shared |
| 184 | `pcm.librespot_substream` | `hw:Loopback,0,0` | **AXIS-2** |
| 194 | `pcm.shairport_substream` | `hw:Loopback,0,1` | **AXIS-2** |
| 204 | `pcm.bluealsa_substream` | `hw:Loopback,0,2` | **AXIS-2** |
| 214 | `pcm.correction_substream` | `hw:Loopback,0,4` | **AXIS-3** |
| 248 | `pcm.outputd_content_playback` | `hw:Loopback,0,6` | **AXIS-1** |
| 258 | `pcm.outputd_content_capture` | `hw:Loopback,1,6` | **AXIS-1** |
| 268 | `ctl.outputd_content_capture` | card `Loopback` | AXIS-1 |
| 276-277 | `__OUTPUTD_DAC_PCM_BLOCK__` / `__OUTPUTD_DAC_CTL_BLOCK__` | rendered → `pcm.outputd_dac` | not aloop |
| 279 | `pcm.jasper_capture` (`type dsnoop`, `ipc_key 7778`) | `hw:Loopback,1,7` | **AXIS-1** |
| 314 | `pcm.jasper_ref` (`type plug` → `jasper_capture`) | pair 7 | **AXIS-1, zero readers** |
| 319 | `ctl.jasper_capture` | card `Loopback` | AXIS-1 |

Already deleted: `pcm.jasper_out` (absent), the pair-5
`outputd_active_content_*` pair (absent — deleted by **P9-C**), and
`usbsink_substream` (absent; pair 3's capture side is still opened by fan-in
with no alias).

**The other two shipped ALSA confs are ring-side, installed by
`deploy/lib/install/ring-platform.sh:293-295, 308-310`:**
`conf.d/60-jts-ring.conf` (`jts_ring_capture` = Ring A, `jts_ring_playback` =
Ring B, `jts_ring_active_playback` = ACTIVE ring — all
`period_frames 128`, `n_slots 2`, `format S32_LE`) and
`conf.d/61-jts-renderer-lanes.conf` (eight AXIS-2/AXIS-3 ring-lane PCMs).

**RULING — P9-E: PENDING. The doc prose is ACCURATE, not stale.**

`docs/audio-paths.md:770-776` says `pcm.jasper_ref` *"survives only as a
shipped definition until P9-E deletes the aloop PCMs."* At this SHA
`pcm.jasper_ref` is present verbatim at `asoundrc.jasper:314-317`, and so is
the **full pair-0/1/2/4/6/7 set**. The reduce has not landed.

What HAS landed is **P9-C**, its predecessor: pair 5's PCM definitions are
gone. `docs/HANDOFF-audio-graph-consolidation.md:1088` states the same
inventory; `:474` marks P9 `**OPEN — U4**`.

Five further shipped sites state the same pending fact and are likewise
accurate: `asoundrc.jasper:46`, `:138`, `:300`;
`jasper/cli/doctor/audio_runtime.py:411-413`, `:510`.

> **Coupled consequence for whoever executes P9-E.**
> `jasper/cli/doctor/audio_runtime.py` → `check_fanin_asound_wiring`
> currently returns **`fail`** when `pcm.jasper_ref` is *missing* from the
> deployed `/etc/asound.conf` (L513-521, remediation "Re-run install.sh"),
> and hard-asserts the four `*_substream` aliases (L446-451) and the
> `hw:Loopback,1,7` dsnoop (L486). A reduction that does not move that check
> in the same PR turns the whole fleet red.

### (c) `check_loopback()` — axis-agnostic, `fail` severity

`jasper/cli/doctor/audio.py:311-331`. Its entire test is:

```python
proc = _run(["aplay", "-L"])
if "CARD=Loopback" in proc.stdout:
    return CheckResult("snd-aloop", "ok", "CARD=Loopback present")
return CheckResult("snd-aloop", "fail", "Loopback device missing. ...")
```

**What it verifies:** one substring — that the card exists in `aplay -L`. It
checks **not** the module load state directly, **not** the substream count,
**not** `index=`/`pcm_notify=`, **not** which pairs exist, and **not** whether
anything can open a pair.

**RULING: fully axis-agnostic — and that is its declared design.** Its own
docstring: *"snd-aloop must be loaded — on both couplings, hence `fail`. A
`loopback`-coupled box runs its entire program path over this card. A
ring-coupled box still needs it for every lane the ring has not taken. …
**P9 removes snd-aloop itself, and this check goes with it.**"*

**It survives Phase 2 unchanged and correctly** (AXIS-2/AXIS-3 still need the
card). It becomes wrong only on a box where every axis has migrated.

> **A live severity contradiction to record.** `check_loopback` says the
> module **must** be present on every box (`fail` if absent), while
> `check_grouping_aloop_remnant` returns **`ok`** for the same absence
> (*"snd-aloop not loaded … no aloop remnant on this box"*). Both are
> intentional at this SHA and the remnant guard says so in its own comment
> (*"`check_loopback` … asserts the OPPOSITE"*), but the two cannot both be
> right once the remnant is grouping-only.

### (d) `snd_aloop_rate_adjust_oscillation_reason` — a TEST-ONLY contract

`jasper/camilla_config_contract.py:317` (with `is_async_resampler` at L307
and `ASYNC_RESAMPLER_TYPES = frozenset({"AsyncSinc", "AsyncPoly"})` at L304).

**Exact contract enforced.** It scans only the top-level `devices:` block for
`capture.device` and `resampler.type`, and rejects exactly one shape:

> capture device name contains `jasper_capture` **or** `Loopback`
> **AND** a `resampler:` block is present
> **AND** its `type` is `AsyncSinc` or `AsyncPoly`.

Rationale (docstring L328-333): *"A snd-aloop ALSA capture … at capture-rate
== playback-rate already rate-tracks via the loopback, so
`enable_rate_adjust: true` WITH an async resampler makes CamillaDSP's
adjuster and the resampler fight, producing the metastable AirPlay-dropout
oscillation. **The safe shape is enable_rate_adjust true AND NO async
resampler block.**"* It does **not** read `enable_rate_adjust` at all, and it
deliberately does not flag the bonded-leader `enable_rate_adjust: false` case.

**RULING — three findings, all load-bearing:**

1. **It has ZERO production callers.** Its own docstring says so: *"This is a
   TEST-TIME contract predicate, NOT a runtime emit-time guard: it has no
   callers in the emit path, so it does not fail-loud at config generation."*
   Verified: `grep` finds only the definition and six lines in
   `tests/test_camilla_config_contract.py`, plus one doc mention.
2. **It applies to BOTH axes by construction, but is exercised on ONE.**
   `_SND_ALOOP_CAPTURE_TOKENS = ("jasper_capture", "Loopback")` — the bare
   substring `Loopback` matches the grouping capture `hw:Loopback,1,6` as
   readily as the AXIS-1 tap. But **none of its five fixtures is a grouping
   config** (they are: the default solo `plug:jasper_capture` config; an
   injected `AsyncSinc` positive control; a stale `RawFile` capture; the
   bonded-**leader** pipe sink; the active-speaker **baseline** config).
   The grouping/follower driver-domain emitter is guarded instead by
   hand-written assertions in `tests/test_multiroom_follower_config.py:961-967`.
   **So the docstring's claim of "every JTS-generated snd-aloop capture
   config" is, at this SHA, false for the grouping emitters.**
3. **A ring device name silently exits the predicate at its first branch.**
   `jts_ring_*` contains neither token, so the guard returns `None` (safe) for
   any ring capture. That is correct for Ring A (which pins
   `RING_CAMILLA_ENABLE_RATE_ADJUST = False`) but means the guard offers the
   grouping migration **no** coverage on the far side.

**Phase ownership: OWNER-DECISION.** If AXIS-1 dies, every current fixture
goes away but the predicate stays meaningful for AXIS-1-GROUPING — it needs
re-pointing at the follower emitter, not deletion. Whether Phase 1 or Phase 2
owns that is a scope call.

### (e) `jasper/ring_assets.py` — ruling: **STAYS** (ring-side), consumed by the reconciler

The module owns five concerns: asset paths + presence; ioplug
provenance/capability records; conf.d parsing; conf.d **rendering**
(`render_ring_conf_wire` — the writer that reuses its own reader's regexes so
the two cannot drift); and on-disk ring header reads plus the derived
verdicts (`ring_geometry_matches_outputd`, `ring_slot_geometry_matches_conf`,
the stall alarm). It also owns `ring_writer_lock_path` — see (f).

**It contains no snd-aloop code.** `grep -inE 'aloop|loopback'` returns
exactly three lines, all prose: `:19` (module docstring, describing the
reconciler consumer's fail-safe), `:495` (a comment), and `:1072` — which is
a **shipped operator-facing string**: *"Until one of those, stay on loopback;
issue #2147 would make …"*.

Consumers: `jasper/cli/audio_config.py`, `jasper/multiroom/reconcile.py`,
`jasper/cli/doctor/audio.py`, `jasper/cli/doctor/audio_runtime.py` (heaviest,
~35 call sites), `jasper/renderer_lanes.py`, and
`jasper/fanin/coupling_reconcile.py` via **nine function-local imports**.

**RULING: `ring_assets.py` is AXIS-1's *replacement*, not its machinery.
It STAYS.** Phase 2 touches it only where its prose names loopback (the three
lines above — the `:1072` string is user-visible and must be retargeted). The
enumerator flagged it for adjudication because the reconciler consumes it;
that is a dependency direction, not co-ownership. Its one Phase-1-relevant
export is `ring_writer_lock_path`, and that already serves the grouping side.

### (f) The ACTIVE-ring writer-flock barrier and the grouping-remnant guard

> **Premise correction — the ticket numbers in the campaign brief do not
> resolve in this tree.** Exhaustive grep at this SHA:
>
> | Ticket | In-tree? | Evidence |
> |---|---|---|
> | **#2508** | **YES** — 6 sites in `jasper/cli/doctor/grouping.py` + 2 test pins | The **EOL issue for the snd-aloop grouping remnant** |
> | **#2539** | No in-tree reference | Squash-merge PR number only: `1b80cfd6e … (#2539)` |
> | **#2526** | No in-tree reference | Squash-merge PR number only: `eb9b0ef95 #2285: the arm barrier asks the ring, not /proc — P9-C PR-1 of 3 (#2526)`. Every bare `2526` in the tree is a numeric coincidence (notably `TimeoutStartSec=2526`, a **seconds** value in `jasper-grouping-reconcile.service`) |
> | **#2481** | **Zero occurrences anywhere** — tree and `git log` | — |
> | **#2581** | **No ticket reference** — two coincidental numeric substrings only | — |
>
> The barrier's **in-tree attribution is `#2285`, phase `P9-C`**, with the
> flock itself landed by **PR #2389 (2026-08-12)**. See §7 S1.

**What actually shipped (the barrier).**

- **Who takes the lock:** the **C ioplug writer only**.
  `c/jts-ring-ioplug/jts_ring_shm.c` → `acquire_writer_lock` opens
  `<ring>.writer.lock` (`O_RDWR|O_CREAT|O_CLOEXEC`), `fchmod`-heals the mode,
  and spins `flock(LOCK_EX|LOCK_NB)` under a 500 ms deadline. Held **for the
  life of the mapping** — released only at `jts_ring_writer_close` or the
  heartbeat-refusal cleanup. Rust `RingWriter`/`RingReader` take only the
  `.open.lock` transaction lock and **never** this one, which is what makes
  an fd on a `.writer.lock` unambiguously a C writer.
- **Who waits:** two Python readers.
  (1) `jasper/multiroom/reconcile.py` → `_probe_active_content_pcm_once` /
  `_wait_for_active_content_pcm_release` (0.8 s deadline,
  0.05 s poll) — **never `O_CREAT`**, releases the lock immediately (*"a
  BARRIER, not a lock handoff"*), and accepts the TOCTOU explicitly because
  camilla#2's own attach is the authority.
  (2) `jasper/cli/doctor/audio_runtime.py:2308` →
  `check_ring_writer_lock_exclusivity`.
  A second C writer waits 500 ms then gets `-EBUSY`.
- **What it prevents:** arming camilla#2
  (`jasper-camilla-crossover.service`) while camilla#1 still owns
  `/dev/shm/jts-ring/active-content.ring` — **the jts3 EBUSY reboot loop**.
- **On contention:** `busy`/`writer_lock_held` → block reason
  `active_content_pcm_busy`; timeout → `busy`/`timeout`; anything unaskable →
  `unknown` → `active_content_pcm_unverified`, logged at WARNING.
  **Both fail closed to solo-active; only a positive `released` arms.**
- **Why it replaced a `/proc` read, in its own words:** *"that path was
  snd-aloop pair 5, whose PCM definitions P9-C deleted. The lock is strictly
  the better signal independently — the kernel drops an `flock` on process
  exit INCLUDING SIGKILL, so it has no frozen-state window."*
- **The cross-language contract is declared in the C source**
  (`jts_ring_shm.c`): *"THIS LOCK NOW HAS PYTHON CONSUMERS. … Changing WHEN
  this lock is taken or released still changes what that barrier means: keep
  the 'held for the life of the mapping' property, or fix both readers in the
  same commit."* Suffix and lock-path construction are pinned across the two
  languages by `tests/test_ring_slot_ceiling_pin.py`.

**What actually shipped (the grouping-remnant guard).**
`jasper/cli/doctor/grouping.py` → `check_grouping_aloop_remnant`
(`@doctor_check(order=75.96, group="grouping", exclusive_group="audio-probe")`).
It walks 4 PCM dirs × 8 substreams of `/proc/asound/Loopback` and fails if any
**open** substream is outside a **derived** registered set. The set is derived,
never tabulated, from three owning constants:

```
pairs 0-4  _FANIN_EXPECTED_ALOOP_INPUTS   (jasper/cli/doctor/audio_runtime.py)
pair  7    _FANIN_EXPECTED_OUTPUT_PCM     (same file)
pair  6    GROUPING_LOOPBACK_PLAYBACK     (jasper/multiroom/reconcile.py)
```

→ registered set today = **{0,1,2,3,4,6,7}**; pair 5 is the only unregistered
index. Verdicts: card absent → `ok`; set underivable → `warn`; `/proc`
unreadable → `warn`; all open pairs registered → `ok`; an unregistered pair
open → `fail` naming the offender (`pid=/comm=/cgroup=`).

Its purpose is stated directly: *"A bounded remnant is only bounded if
something measures it, and the design names the failure mode directly (risk
5.1): **'the remnant becomes permanent by silence.'** This check is the
measurement."* It carries `#2508` in its own operator-facing text.

> **A Phase-1 landmine, recorded.** `_pair_from_loopback_pcm` returns `None`
> for anything that is not an `hw:Loopback,<d>,<s>` triple — *"a ring path, a
> plug wrapper, a renamed card."* So the moment `GROUPING_LOOPBACK_PLAYBACK`
> names a ring, `_grouping_pair_index()` → `None` → `_derive_registered_pairs()`
> → `None` (all-or-nothing by design) → **the entire check degrades to
> `warn`, not `ok`.** The "mechanical retirement" property holds for
> *narrowing* the set; a ring path on that constant is the one input shape
> that silences the instrument instead.

**Phase ownership:** the writer-lock barrier **STAYS** (ring-side, and it is
already the grouping side's arm proof). The remnant guard is **PHASE-1** —
its derived-set mechanism must be re-pointed when the grouping constant moves.

---

## 6. AXIS-2 SCOPE STATEMENT (for the owner — not decided here)

### 6.1 What still runs on snd-aloop by default at this SHA

The canonical allocation owner is `deploy/modprobe.d/snd-aloop.conf`, whose
single options line is:

```
options snd-aloop enable=1 index=6 id=Loopback pcm_substreams=8 pcm_notify=0
```

Its comment block is the pair map (quoted verbatim in §5(a)). Attributing
each pair to an axis:

| Pair | Use | Axis | Dies with Phase 2? |
|---|---|---|---|
| 0 | `librespot_substream` — Spotify ingress | AXIS-2 | **No** |
| 1 | `shairport_substream` — AirPlay ingress | AXIS-2 | **No** |
| 2 | `bluealsa_substream` — Bluetooth ingress | AXIS-2 | **No** |
| 3 | reserved; **fan-in still OPENS `hw:Loopback,1,3`** as the usbsink lane's idle read fallback when USB Audio Input is off | AXIS-2 | **No** |
| 4 | `correction_substream` — measurement/commissioning sweeps | AXIS-3 | **No** |
| 5 | UNALLOCATED (was the active content lane; PCMs deleted by P9-C, `pcm_substreams` **not** renumbered) | — | already gone |
| 6 | passive stereo content lane (`outputd_content_*`) **AND** the bonded grouping round-trip | AXIS-1 / AXIS-1-GROUPING | **Yes** (both halves) |
| 7 | fan-in summed program (`jasper_capture` dsnoop) | AXIS-1 | **Yes** |

The fleet default for ingress is stated in the Rust source itself —
`rust/jasper-fanin/src/mixer.rs`, `enum LaneSource`:
> `/// An snd-aloop capture substream — the shipped default for every renderer.`
> `Lane,`

and in the shipped conf that carries the ring alternative,
`deploy/alsa/conf.d/61-jts-renderer-lanes.conf` L8-16:
> *"INERT UNTIL ARMED. … On every unarmed box the renderer keeps writing its
> `*_substream` alias in /etc/asound.conf, byte-identically to before this
> file existed. … The snd-aloop lane definitions in
> `deploy/alsa/asoundrc.jasper` STAY until P9."*

### 6.2 What a "ring-always-ingress" decision would entail, at census grade

Stated as scope, not as a recommendation.

**Consumers affected — 12** (§2.3), of which the load-bearing ones are:

1. **The fleet-default flip itself.** Today the armed set is empty because
   *nothing writes* `/var/lib/jasper/renderer_lanes.env` at install or boot —
   the only writer is the operator CLI `jasper-audio-config renderer-lanes`.
   A ring-always decision needs a *reconciler* to own that file (pattern 3),
   which does not exist at this SHA.
2. **Four per-renderer writer shapes, not one.** librespot and bluealsa-aplay
   take `${JASPER_<X>_DEVICE}` substitution in `ExecStart`; shairport-sync
   takes a **conf-renderer** (`jasper-apply-airplay-mode` at `ExecStartPre`
   substituting `__RENDERER_DEVICE__` into the conf template); correction is
   **unitless** and resolves per `aplay` spawn in Python. Each has its own
   in-unit aloop default that would have to move.
3. **A restart obligation the arm CLI currently prints but does not perform.**
   `_cmd_renderer_lanes` prints
   `restart_required jasper-fanin.service <units>`; the flip is not live
   until those restart. `check_shairport_sync_loopback_plughw` exists
   specifically to catch that half-flip in both directions.
4. **The arm preflight refusals** (`arm_refusal_reason`): ring platform
   assets present, `61-jts-renderer-lanes.conf` installed, the renderer's
   runtime user in group `jts-ring`, and an expressible slot geometry
   (`input_buffer_frames / period_frames` ∈ [2,16]). A fleet flip must
   satisfy all four per renderer per box.
5. **A umask side effect that only exists when armed.** On an armed
   correction lane the ring FILE is created *inside* `aplay`'s process, so
   `CORRECTION_PLAY_UMASK = 0o007` becomes live where it governs nothing
   today.
6. **Doctor surface changes.** `_FANIN_EXPECTED_ALOOP_INPUTS` and
   `check_fanin_asound_wiring` are file-level drift checks against
   `asoundrc.jasper`; `_fanin_lane_busy_owner_matches` keeps an aloop
   `/proc` table; `_loopback_playback_active()` would return `False` while
   music plays (it says so itself).

**Two UNKNOWNs that gate the decision** (neither settleable from code):

- **No renderer has ever opened a ring lane PCM.**
  `deploy/alsa/conf.d/61-jts-renderer-lanes.conf:71-79` states: *"It has NOT
  been exercised against a real librespot open on hardware: the whole of P6a
  ships unarmed, so no renderer has ever opened one of these PCMs."* Whether
  that is still true needs a box.
- **Whether any fleet box currently has a lane armed.** The repo can only
  prove the shipped default. Settled by
  `ssh <box> 'cat /var/lib/jasper/renderer_lanes.env'` or
  `jasper-audio-config renderer-lanes` with no flags.

### 6.3 What leaving AXIS-2 alone entails — and the "snd-aloop no longer loads" clause

> **Read §7 S1 first.** The clause is attributed in the campaign brief to
> `#2481`, but **neither the ticket number nor that literal sentence exists
> anywhere in this tree or its git history** — `grep -rnw 2481` and
> `grep -rni "no longer loads|snd-aloop no longer|aloop no longer"` both
> return zero. The census therefore adjudicates the clause **as stated by the
> brief**, not as quoted from a repo artifact. The nearest in-repo statement
> of the same end state is a campaign *goal*, not a done-clause:
> `docs/HANDOFF-audio-graph-consolidation.md:52-55` — *"and **snd-aloop
> GONE**, taking `rate_match`, adaptive-buffer shrink, the legacy cushion
> recipes, the fan-in aloop mirror lane, and the dsnoop taps with it."*

**Ruling: the end state "snd-aloop no longer loads on any box" is NOT
reachable by Phase 1 + Phase 2 alone. It requires an AXIS-2 ruling, and an
AXIS-3 one.**

Evidence: after Phase 1 (grouping → grouping ring) and Phase 2 (coupling
deletion), pairs **6 and 7** lose their consumers. Pairs **0, 1, 2, 3** (AXIS-2)
and **4** (AXIS-3) do not. On a fleet-default box every renderer lane is
unarmed and therefore still writes its `*_substream` alias, and fan-in still
opens `hw:Loopback,1,3`. The module is therefore still required, and
`deploy/modules-load.d/snd-aloop.conf` still loads it.

Corollary: `pcm_substreams` **cannot be reduced below 7** while the grouping
pair (index 6) is live, and cannot be reduced below 5 while pairs 0–4 are
live. The modprobe comment records why a 9th pair is impossible
(`snd_aloop` caps at 8), which is *why* pair 6 is shared in the first place.

**Therefore the campaign's phase set is sufficient to delete the loopback
COUPLING, and insufficient to unload the module.** That gap is the
owner's scope call, not this census's.

---

## 7. STALE-PROSE LEDGER

Claims found already false at this SHA. **Recorded, not fixed** — these are
findings for later PRs.

### S1 — Four of the campaign's five ticket numbers do not resolve in this tree

Severity: **premise-level.** Not a repo defect; a defect in the campaign's own
citations.

| Ticket | Status at this SHA | Evidence |
|---|---|---|
| **#2508** | **Real and in-tree** — 6 sites in `jasper/cli/doctor/grouping.py` plus 2 test pins (`tests/test_doctor_grouping_remnant.py:96`, `:420`). It is *"the EOL issue for the snd-aloop grouping remnant."* | — |
| **#2539** | No in-tree reference. Squash-merge PR number only: `1b80cfd6e … (#2539)` | `git log` |
| **#2526** | No in-tree reference. Squash-merge PR number only: `eb9b0ef95 #2285: the arm barrier asks the ring, not /proc — P9-C PR-1 of 3 (#2526)`. Every bare `2526` in the tree is coincidence — notably `TimeoutStartSec=2526` in `deploy/systemd/jasper-grouping-reconcile.service`, a **seconds** value | `grep -rn '2526'` |
| **#2481** | **Zero occurrences** anywhere — tree *and* `git log` | `grep -rnw 2481`, `git log --oneline --all \| grep 2481` |
| **#2581** | **No ticket reference.** Two coincidental numeric substrings (`gets 12.2581 dB`; `ref=2581 mic=3693 aec=460`) | `grep -rnw 2581` |

**The truth for the two the brief describes by behaviour:**
- The "**#2526** release barrier (ACTIVE ring writer flock)" is attributed
  in-tree to **`#2285` / phase `P9-C`**; the flock landed in **PR #2389**
  (2026-08-12), per the provenance note at
  `jasper/active_speaker/playback.py:101-106`.
- The "**#2539** grouping-remnant doctor guard" is attributed in-tree to
  **`#2285` P9-C**, and its own operator-facing EOL pointer is **`#2508`**.

**Also absent: the literal done-clause.** `grep -rni "no longer loads|snd-aloop
no longer|aloop no longer"` → **zero matches**. The nearest in-repo statement
of that end state is `docs/HANDOFF-audio-graph-consolidation.md:52-55`, which
frames it as a campaign *goal*: *"and **snd-aloop GONE**, taking `rate_match`,
adaptive-buffer shrink, the legacy cushion recipes, the fan-in aloop mirror
lane, and the dsnoop taps with it."*

### S2 — The `reconcile.py:211-216` line drift — **claim CONFIRMED, and worse than stated**

Two errors, not one:

1. **Wrong lines.** The definitions are at **`:214`** (`GROUPING_LOOPBACK_PLAYBACK`,
   spanning 214-217) and **`:218`** (`GROUPING_LOOPBACK_CAPTURE`, spanning
   218-221), plus a **third** member the cited range misses entirely,
   `GROUPING_LOOPBACK_CAPTURE_FORMAT` at **`:225`**. Lines 211-213 are a blank
   comment line and the two ASCII-diagram comment lines.
2. **Wrong file, potentially.** `jasper/fanin/coupling_reconcile.py` contains
   **no `GROUPING_LOOPBACK` symbol at all**; its `:200-230` is the
   `FaninRingSlotsResolution` dataclass plus the `#2175` start-budget comment.
   The ticket can only have meant `jasper/multiroom/reconcile.py`. Two files in
   this repo are called `reconcile.py` and both are in scope for this campaign
   — a bare `reconcile.py:N` citation is ambiguous by construction.

### S3 — `/etc/alsa/conf.d/zz-jts-loopback.conf` — the file does not exist and nothing writes it

**8 references, all prose, all in `docs/`.** Verified absent:
`git ls-files | grep -i 'zz-jts'` → empty; `find . -name '*zz-jts*'` → empty;
`grep -rn -i 'zz-jts' --include='*.sh' --include='*.py' --include='*.service'
--include='*.conf'` → **zero**. `deploy/alsa/` holds exactly three files
(`asoundrc.jasper`, `conf.d/60-jts-ring.conf`,
`conf.d/61-jts-renderer-lanes.conf`).

| File:line | Text | Assessment |
|---|---|---|
| `docs/HANDOFF-persistent-live-session.md:120` | *"don't touch `/etc/alsa/conf.d/zz-jts-loopback.conf` or `/etc/camilladsp/v1.yml`"* | **The one misleading site.** Reads as a live instruction. Mitigated by the doc's `> **Status: historical.**` callout at L3 (which explicitly says file paths have drifted), so per AGENTS.md doc-rule 10 this is **not a rule violation** — but it is the site most likely to mislead a grepping agent, since the filename appears nowhere current. |
| `docs/historical/CLEANUP-moode-removal.md:90, 121, 289, 323, 848, 860, 919` | The moOde-era hijack, its deletion plan, and its verification | Correctly quarantined (`**Status:** _completed; archived 2026-05-08._`). `:323` and `:919` actually *assert the file must not exist* — i.e. they already agree with reality. |

**Ruling: stale, but correctly filed.** Zero code, deploy, or test references.
The filename should not appear in any census of shipped ALSA surface area.

### S4 — "solo-stereo-only **until ring v2 (P8)**" — 5 sites, 3 of them shipped operator-facing strings

**This is the ledger's most consequential stale-prose finding**, because the
wording is user-visible and it is now false on its face.

| File:line | Text | Kind |
|---|---|---|
| `jasper/audio_runtime_plan.py:284-286` (`_GROUPED_SHM_RING_DETAIL`) | *"the SHM ring is solo-stereo-only until ring v2 (P8)"* | **shipped string** |
| `jasper/audio_runtime_plan.py:824` | *"is solo-stereo-only until ring v2 (P8)"* | docstring |
| `jasper/fanin/coupling_reconcile.py:3399` | *"`shm_ring` is a solo-stereo-only coupling until ring v2 (P8)"* | docstring |
| `jasper/fanin/coupling_reconcile.py:3419` | *"box has no solo content path for the ring until ring v2 (P8)"* | **shipped string** |
| `jasper/multiroom/reconcile.py:1914` | *"cannot join a bond until ring v2 (P8)"* | **shipped string** |

**Ring v2 HAS shipped.** `jasper/ring_assets.py:65` (*"ring v2 R7b"*), `:557`
(*"since ring v2 they can declare DIFFERENT geometry"*),
`jasper/fanin_coupling.py:199` (*"The ACTIVE ring (ring v2 R7b)"*), and
`docs/HANDOFF-audio-graph-consolidation.md:447` (*"R1–R5 ladder complete, R7a
and R7b merged, and jts3 ARMED WIDE 2026-08-11"*). Campaign memory also
records #2412 CLOSED 2026-08-17.

**The behaviour is still correct** — P8's bonded and N>2 halves remain
unexercised (`HANDOFF-audio-graph-consolidation.md:1087`: *"P8a's channel half
is still unexercised: no ring in the fleet has carried more than stereo"*;
*"Remaining as of 2026-08-17: jts5 / bonded per the P8 scope ruling"*). **Only
the wording is wrong:** "until ring v2" reads as "ring v2 hasn't shipped", when
what actually remains is P8's bonded/N>2 activation. An operator who reads the
shipped string is told to wait for something that already arrived.

### S5 — `_loopback_playback_active()`'s docstring undercounts its callers by one

`jasper/cli/doctor/_shared.py` → the retirement note says *"delete it then,
together with its **two** callers' music-active gates."* There is now **one**
production caller (`jasper/cli/doctor/aec.py:1152`);
`jasper/cli/doctor/aec_probe.py:168-173` removed its gate at **#2585** and
left a comment saying so (*"It was PERMANENTLY INERT on a ring-armed box …
it read as protection while protecting nothing"*).

### S6 — `snd_aloop_rate_adjust_oscillation_reason`'s coverage claim is false for the grouping emitters

Its docstring: *"The regression test … feeds it **every** JTS-generated
snd-aloop capture config."* At this SHA the five fixtures in
`tests/test_camilla_config_contract.py` contain **no grouping/driver-domain
config** (`grep -n "driver_domain\|GROUPING\|grouping"` on that file → nothing),
even though the predicate's `"Loopback"` substring token matches
`hw:Loopback,1,6`. The grouping emitters are guarded separately by hand-written
assertions in `tests/test_multiroom_follower_config.py:961-967`.

### S7 — `docs/HANDOFF-usb-low-latency.md:1925-1927` names the wrong unit and the wrong variable

It describes an `ExecStopPost=-/usr/bin/amixer -c ${JASPER_USBSINK_MIXER_CARD} …`
line **in `deploy/systemd/jasper-usbsink.service`**. That file contains no such
line at this SHA (it is a process-free readiness marker whose only Exec lines
are `ExecStartPre=/usr/local/sbin/jasper-usbsink-wait-card 30` and
`ExecStart=/bin/true`). The mechanism lives at
`deploy/systemd/jasper-fanin.service:245`
(`ExecStopPost=-/usr/local/sbin/jasper-fanin-pitch-neutralize`), whose helper
derives the card from `JASPER_FANIN_USB_DIRECT_DEVICE`, not
`JASPER_USBSINK_MIXER_CARD`.

### S8 — `docs/audio-paths.md:311` — vocabulary softness, not a false claim

*"| Source slider … | Renderer-side, before Loopback | yes | no |"* — true for
an aloop lane, but on a ring-armed lane the renderer writes a SHM ring, not
"Loopback". The phrasing pre-dates ring-armed renderer lanes. **Every other
aloop/loopback/coupling claim in that file (20 matching lines audited) is
accurate at this SHA**, including its one explicit not-yet claim about P9-E.

### S9 — Not stale: three things that look stale and are not

Recorded so a later reader does not "fix" them.

- **`transport_label`'s `alsa` branch** (`jasper/fanin_coupling.py`) is
  deliberately live for every non-ring device, with an explicit
  *"do not 'tidy' the `alsa` branch away"* note. Only `/state`'s surface
  contract narrowed.
- **Every "until P9-E" / "STAYS until P9" claim** about the aloop PCM
  definitions is **TRUE** at this SHA (§5(b)) — including
  `deploy/alsa/conf.d/61-jts-renderer-lanes.conf:16-19`,
  `docs/audio-paths.md:773`, `docs/HANDOFF-fan-in-daemon.md:308` and `:755`,
  and `jasper/cli/doctor/audio_runtime.py:411-413` and `:507-512`.
- **The `#2147`-conditioned remediation strings** (`"Until one of those, stay
  on loopback; issue #2147 would make …"` in `jasper/ring_assets.py:1072`,
  `jasper/cli/doctor/audio_runtime.py:2738`, `:2181`,
  `deploy/alsa/conf.d/60-jts-ring.conf:81`) are tied to a still-open issue,
  not to the aloop retirement.

---

## 8. OPEN QUESTIONS

Things this census cannot settle from code, stated as questions with the
evidence that would settle each.

**Q1 — Can a grouping ring host a rate-tracked bonded endpoint?**
The `rate_adjust: true` on the loopback-capturing CamillaDSP is justified
*because snd-aloop has a clock* ("its sink is the ALSA loopback, **which HAS a
clock to track**"). The ring contract asserts the opposite for its own
transport (`RING_CAMILLA_ENABLE_RATE_ADJUST = False`; *"a blocking slot
handshake gives the rate controller nothing to adjust TO"*). **Nothing in the
tree answers what tracks the clock on a bonded active endpoint over a ring.**
*Settled by:* a design decision plus an S0-sync-class bench run (follower lock,
drift over a soak) on jts3/jts5 — the same instrument that validated the
current seam.

**Q2 — What paces fan-in's program egress once the loopback is gone?**
`jasper/control/audio_health.py` states *"fan-in's default `loopback` coupling
is **timer-paced**"*, and `jasper/fanin/coupling_reconcile.py:669` keeps a
plain (non-camilla-coordinated) fan-in restart *"On loopback … (snd-aloop
decouples the two)"*. Both are behavioural dependences on the aloop transport,
not naming ones. **Whether the ring's blocking slot publish fully substitutes
for both is not asserted anywhere.**
*Settled by:* reading `rust/jasper-fanin/src/playout.rs` + `RingOutput`'s
self-pacing against `audio_health`'s `_stopped_dsp_signal` on hardware, or a
deliberate statement from the pacing owner.

**Q3 — What does the operator-choice marker mean when there is one transport?**
`JASPER_FANIN_COUPLING_CHOICE=operator` currently freezes a two-valued choice
*and* suppresses the whole ring-gate set (`ring_gates=()`), which the code
itself flags as making the fleet's fail-closed wire gates inoperative on every
pinned box — *"all three armed fleet boxes are pinned."* Phase 2 removes the
choice but not the suppression side effect. **OWNER-DECISION**, not a code
question.
*Settled by:* an owner ruling on whether the marker survives as a
"don't-auto-converge-this-box" lever or is deleted with the token.

**Q4 — Does Phase 1 close the deferred round-trip-starvation signal?**
`jasper/control/grouping_supervisor.py:313`: *"round-trip starvation of the
active lane (the camilla#2 loopback going silent) is a separate signal outputd
does not yet surface — **deferred until observed**."* A grouping ring has a
writer lock, a heartbeat, and a stall alarm (`jasper/ring_assets.py`) that the
aloop pair never had. **Whether Phase 1 is the moment that deferral resolves is
a scope call.**
*Settled by:* an owner ruling; the ring-side instrument already exists.

**Q5 — Which issue numbers should the campaign actually cite?**
Per §7 S1, only **#2508** resolves in-tree; #2481, #2526, #2539 and #2581 do
not. The in-tree attribution for both mechanisms the brief names is **#2285 /
P9-C** (PRs #2389, #2526, #2539). **This census cannot tell whether #2481 /
#2581 exist on GitHub but are simply uncited in code, or do not exist.**
*Settled by:* `gh issue view 2481` / `gh issue view 2581`. (Not run — outside
the read-only scope granted.)

**Q6 — Has any renderer ring lane ever been opened by a real renderer, and is
any fleet box currently armed?**
`deploy/alsa/conf.d/61-jts-renderer-lanes.conf:71-79` says no renderer has ever
opened one. The armed set lives per box in
`/var/lib/jasper/renderer_lanes.env`, which the repo never writes.
*Settled by:* `ssh <box> 'cat /var/lib/jasper/renderer_lanes.env'`, or
`jasper-audio-config renderer-lanes` with no flags (reports).

**Q7 — After Phase 2, which of the two contradictory module-presence checks
wins?**
`check_loopback` returns **`fail`** when snd-aloop is absent; the remnant guard
returns **`ok`** for the same absence. Both are deliberate today (§5(c)), and
the remnant guard names the conflict in its own comment. They cannot both
survive an axis migration.
*Settled by:* an owner ruling tied to whichever phase makes the module
optional — which, per §6.3, is **not** Phase 2 alone.

**Q8 — Does the strike ladder's persistence change the KEEP rationale?**
The campaign brief's third KEEP leg was *"oneshot units mean in-memory counters
accumulate nothing (persistence buys two-strike without a retry)."* At this SHA
the counter **is** persisted (`/var/lib/jasper/ring-confirm-strikes.json`, 24 h
window, explicit clear-on-success and clear-on-complete-disarm, with a
`ring_confirm_strike_write_failed` event when it cannot be written). §4.4 L17.
**The other two KEEP legs — transients genuinely returning `ok=False`, and no
other rescuer existing — this census did not re-test.**
*Settled by:* re-reading PR #2659's adjudication against §4.4, since the
mechanism it describes and the mechanism in the tree differ.

**Q9 — Does pair 3's writer-less idle read survive a fan-in change?**
On a non-gadget box fan-in opens `hw:Loopback,1,3` purely as the usbsink lane's
idle read fallback — a capture with no writer, kept because a missing required
input is **fatal** to fan-in. It is an AXIS-2 dependency created by fan-in's
own roster, not by any renderer.
*Settled by:* a decision about whether fan-in's input roster becomes dynamic;
until then it is one more reason the module cannot unload.

**Q10 — Is the test-side census complete?**
§1.1's inherited caveat stands: `tests/` was enumerated file-complete but
symbol-condensed. This census opened the specific contracts it cites
(`test_camilla_config_contract.py`, `test_multiroom_follower_config.py`,
`test_doctor_grouping_remnant.py`, `test_ring_slot_ceiling_pin.py`,
`test_fanin_wiring.py`, `test_renderer_ring_lanes.py`,
`test_aec_ref_source_retirement.py`, `test_env_vars_codified.py`) but did not
sweep all 138 test files symbol-by-symbol.
*Settled by:* a targeted pass over `g_tests.txt` if Phase 2's blast radius
needs a test-contract inventory rather than a consumer inventory.

---

*Census produced 2026-08-17 against `6e569e8dc8e572a8d648d332c414374b8394496e`.
Read-only: no repo file was edited, no build, test, deploy, or ssh was run.*
