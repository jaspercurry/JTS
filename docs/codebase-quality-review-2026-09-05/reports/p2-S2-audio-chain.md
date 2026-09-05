# P2 / S2 — audio chain: renderer → fan-in → CamillaDSP → outputd → DAC. SHA 2d571e6b8

## A. Verdict

The transport is in far better shape than the control plane around it. **Ring layout is a
model of cross-language ownership** — `rust/jasper-ring/src/layout.rs` owns offsets, the C
`_Static_assert`s and `jasper/ring_assets.py` mirror them, and four contract tests
(`test_ring_slot_ceiling_pin.py`, `test_ring_emitter_ioplug_negotiation.py`,
`test_fanin_coupling_rust_contract.py`, `test_ring_assets.py`) pin offsets, the 128-frame slot,
the channel count and the format enum across Python/Rust/C. **CHANNEL-COUNT and RING-LAYOUT are
one-owner-plus-pins; SAMPLE-RATE is not owned at all** (≈20 independent declarations, zero
cross-language pin, backstopped only by the ring header's `rate != 48_000` refusal).
**GAIN-CEILING has one owner for the *value* (`ensure_volume_limit_db`) and seven hand-typed
`devices:` templates for the *text*, plus two websocket doors that bypass the check entirely** —
and the one runtime detector reads the durable file that those doors deliberately do not touch
(§D1, the sharpest finding in this scenario). Lifecycle self-heals in every order I could trace
*except* the outputd-period/conf.d divergence, which the code's own doctor docstring admits
nothing preflights. Config apply mid-playback is a 25 dB duck + ~1 s fade, not a gap — except
on the `/sound/live-draft` and `patch_config` doors, which are un-ducked by design.

## B. Happy path — hops and process boundaries

| # | hop | file:function | boundary |
|---|---|---|---|
| 1 | renderer writes its lane | `deploy/alsa/asoundrc.jasper:108-144` (`*_substream`, `plug`→`hw:Loopback,0,N`) **or**, on an armed box, `deploy/alsa/conf.d/61-jts-renderer-lanes.conf` (`*_ring_lane` → `jts_ring` ioplug) | **PROC** shairport-sync / librespot / bluealsa-aplay / ephemeral `aplay` |
| 2 | lane map resolution | `jasper/renderer_lanes.py:460 render_renderer_lanes_env` → `/var/lib/jasper/renderer_lanes.env`; unit `ExecStart`/`ExecStartPre` substitutes | install/CLI time |
| 3 | fan-in reads lanes | `rust/jasper-fanin/src/config.rs:609-628` (`hw:Loopback,1,0..4` + labels) → `mixer/pcm_open.rs:319` (aloop) / `mixer/ring_capture.rs:323` (ring) / `mixer/direct_capture.rs` (USB gadget) | **PROC** jasper-fanin |
| 4 | gate + sum | `mixer.rs` (`AUTO`/`SELECT`/`NONE` from `/run/jasper-fanin/control.sock`, driven by jasper-mux) | UDS |
| 5 | TTS mix after duck | `rust/jasper-fanin/src/tts.rs` `TtsMixer`; duck `config.rs:1046-1066` (fail-loud on positive); gain `jasper-tts-protocol/src/loudness.rs:446` | UDS `/run/jasper-fanin/tts.sock` ← jasper-voice |
| 6 | publish Ring A | `mixer.rs:1424-1432` `Geometry{rate: config.sample_rate, channels: CHANNELS, period_frames: RING_SLOT_FRAMES, n_slots}` → `RingWriter` | **SHM** `/dev/shm/jts-ring/program.ring` |
| 7 | CamillaDSP captures Ring A | `deploy/alsa/conf.d/60-jts-ring.conf` `pcm.jts_ring_capture` → `c/jts-ring-ioplug/pcm_jts_ring.c` reader | **PROC** camilladsp (`-p 1234`) |
| 8 | DSP graph | file at statefile `config_path`; emitted by `jasper/sound/camilla_yaml.py:456 emit_sound_config` or `jasper/active_speaker/camilla_yaml.py` (6 emitters); devices from `active_speaker/camilla_yaml.py:338 active_emit_devices` | file + ws:1234 |
| 9 | CamillaDSP writes Ring B | `pcm.jts_ring_playback` (or `jts_ring_active_playback` on a roleful box) | **SHM** `content.ring` / `active-content.ring` |
| 10 | outputd reads Ring B | `rust/jasper-outputd/src/main.rs:415 ShmRingSource::new(path, config.period_frames, …)` → `shm_ring_source.rs:150` | **PROC** jasper-outputd |
| 11 | write DAC | `alsa_backend.rs:1610 writei` on `outputd_dac`; width per DAC profile (`jasper/audio_hardware/dac.py`) | **HW** |

## B2. Failure branch per hop — does it surface?

| hop | failure | surface | verdict |
|---|---|---|---|
| 1 | renderer writes an unarmed ring PCM | ioplug create-or-attach **succeeds**, timer-paced silence (`60-jts-ring.conf` documents this) | **quiet**; only `check_renderer_ring_lanes` catches the arm mismatch |
| 3 | a configured lane cannot be opened | fan-in fails loud at start (`config.rs`), exit non-78 → `Restart=on-failure` | loud |
| 3 | fan-in config-class fault | exit 78 → `RestartPreventExitStatus=78` parks unit `failed` | loud; `check_fanin_service` FAIL |
| 3 | fan-in flaps 5×/300 s | `StartLimitAction=reboot` — **reboots the Pi** | loud, blunt (see D9) |
| 6 | Ring A geometry ≠ conf.d | `RingWriter` create refuses (`InvalidInput`) → `event=fanin.ring.config_error` | loud |
| 7 | fan-in dead, CamillaDSP alive | C ioplug fabricates timer-paced silence (`pcm_jts_ring.c:212-242`), `silence_periods` logged **at close only** | **quiet at this hop**; caught upstream by `check_fanin_service` |
| 7 | CamillaDSP dead, fan-in alive | writer free-runs; after `STUCK_READER_GRACE_NS`=1 s → `event=fanin.ring.stall_detected reason=no_reader`, then `stall_unrecovered`; `/state.fanin.shm_ring.drop_no_reader` | loud |
| 7/9 | CamillaDSP crash-loops | `Restart=always` RestartSec=2, `StartLimitAction=none`, `OnFailure=jasper-camilla-recover` → park record in `/run` + `check_camilla_recover_park` | loud, well built |
| 8 | inline graph installed with bad/absent `volume_limit` | **nothing** — doctor reads the statefile's durable file, which `set_active_config_raw` leaves untouched | **LIES** (D1) |
| 8 | CamillaDSP rejects a config | `CamillaConfigRejected` (`camilla.py:477`), distinct from unavailable; `apply_dsp_config` rolls back and writes `DspApplyState` | loud |
| 8 | CamillaDSP unreachable | `_call` retries **once with zero delay**, then `CamillaUnavailable`; ~10 s per call against a wedged daemon, forever | loud but self-DoSing (D8, = p1-T05 #3) |
| 10 | `JASPER_OUTPUTD_PERIOD_FRAMES` ≠ conf.d `period_frames` (128) | hard attach mismatch → `event=outputd.shm_ring.config_error` + `outputd.config_invalid_runtime` → exit 78 park | loud, but **nothing preflights it** — `audio_runtime_ring.py:1085-1089` says so in its own docstring; issue #2147 |
| 10 | outputd restart races the old reader | `EBUSY` from the SPSC guard → restart arm (`main.rs:187-201`), RestartSec=5 > 2 s liveness | self-heals |
| 10 | Ring B empty ≥ 2 s | `content_fill.rs` → `event=outputd.content.deaf` / `.recovered` | loud, and the only "deaf right now" surface in the tree |
| 11 | DAC vanishes at boot | `ExecCondition` gate → unit skipped (**not** failed), `event=outputd.output_device_gate.park` on stderr | loud enough; `check_outputd_service` reports inactive→FAIL |
| 11 | DAC vanishes mid-run (`ENODEV`/`EIO`) | `alsa_backend.rs:1638` returns `Err(...).context(...)` with **no `event=`**; Rust `Termination` prints a bare `Error: …` | **quiet at the moment of going silent** (= p1-T20 #4, confirmed) |
| 11 | xrun | `event=outputd.xrun` + `try_recover`, give-up after `MAX_RECOVERIES_PER_PERIOD` | loud |
| 11 | shutdown error masked | `main.rs:144 notify_systemd("STOPPING=1")?;` — a notify failure replaces the real DAC error and skips park classification | **LIES** (= p1-T20 #3, confirmed) |

**Config apply mid-playback.** `_graph_mutation` (`camilla.py:795`) ducks `main_volume` by
`GRAPH_SWAP_DUCK_DB=25.0`, sleeps `MAIN_VOLUME_RAMP_SETTLE_S=0.45`, swaps, and releases under
`asyncio.shield`. Audible as a ~1 s fade down/up, not a gap. Three exceptions ship un-ducked:
`plan_live_edit`'s `duck=False` arm (a moved trim lands as a level step — ADR-0219),
`patch_config` (always `duck=False`), and `set_active_config_raw(duck=False)` from the
measurement session graph. The README's "full-scale transient" is a *different* hazard
(README.md:129) — source-switch between push-volume and Camilla-master carriers, owned by
`VolumeCoordinator.prepare_source_handoff`, not by the graph swap. Both are real; the docs do
not distinguish them. Separately, a CamillaDSP **reload** detaches and re-attaches both ring
ends; Ring A/B are 2 slots (5.3 ms), so any reload longer than that drops audio, and a reload
over 2 s trips `outputd.content.deaf`.

## C. Invariant ownership

| fact | owner | other spellings | pinning test | proposed single source |
|---|---|---|---|---|
| **SAMPLE-RATE 48000** | **none** | ≈20 decls: `camilla_config_contract.py:139`, `sound/profile.py:74`, `fanin/latency_mode.py:18`, `audio_io.py:666`, `assistant_loudness.py:39`, `cli/doctor/audio_runtime_outputd.py:741` (bare literal), `fanin/config.rs:645` (env `JASPER_FANIN_SAMPLE_RATE`), `fanin/mixer.rs:2462`, `fanin/tts.rs:44`, `outputd/types.rs:11`, `outputd/shm_ring_source.rs:159` (bare literal in a crate that declares `SAMPLE_RATE` two files over), `tts-protocol/loudness.rs:19`, `ring/layout.rs:212` + `ring/writer.rs:537` (bare), `c/pcm_jts_ring.c:254`, `c/jts_ring_shm.c:197,248` (bare), 4 lanes × `asoundrc.jasper`, 4 lanes × `61-jts-renderer-lanes.conf`, `outputd-cutover.yml:14` | **NONE** cross-language | `Geometry::validate_self` already refuses ≠48000; make `layout.rs` `pub const RATE_HZ` the Rust owner (fanin/outputd/tts-protocol re-export it, as they already do for `RING_SLOT_FRAMES`), C mirrors via the existing `_Static_assert` habit, and extend `test_ring_slot_ceiling_pin.py` with a rate row |
| **CHANNEL-COUNT (stereo program = 2)** | `jasper/fanin_coupling.py:201 RING_A_CHANNELS` | `ring_assets.py:111`, `runtime_contract.py:657,767`, `fanin/mixer.rs:63`, `outputd/types.rs:12`, `tts-protocol/lib.rs:34`, `c/pcm_jts_ring.c:170`, 6× literal `channels: 2` in the active YAML templates, 4× `channels 2` in asoundrc | **`test_fanin_coupling_rust_contract.py:222`** pins 5 sites; `test_runtime_contract_ring.py` pins a 6th | good as-is; add `tts-protocol/lib.rs:34 CHANNELS` to the pin and emit the YAML `channels:` line from `active_emit_devices` |
| **CHANNEL ceiling 8** | `ring/layout.rs:80 MAX_RING_CHANNELS` | `c/jts_ring_shm.h:49`, outputd's `JASPER_OUTPUTD_ACTIVE_CHANNELS` bound | `test_ring_slot_ceiling_pin.py:81` (3-way, equal-not-literal) | correct |
| **RING_SLOT_FRAMES 128** | `ring/layout.rs:64` (`pub use` by both daemons) | `c` `JTS_RING_DEFAULT_PERIOD`, `fanin_coupling.py:63`, 3× conf.d literal | `test_ring_emitter_ioplug_negotiation.py:119-125` + `test_audio_hardware_reconcile.py` | **the model** — nothing to fix |
| **n_slots 2..=16** | `ring/layout.rs:71-72` | **7 sites**: `c/jts_ring_shm.h:61,74`; `fanin/config.rs:32-33`; `outputd/config.rs:82-83`; `fanin_coupling.py:707-708`; `fanin_coupling.py:862-863`; `renderer_lanes.py:128-129` (the site p1-T21 missed) | only **MAX** pinned, across only 3 of 7 (`test_ring_slot_ceiling_pin.py:159`); **MIN pinned nowhere**; all 3 Python copies unpinned | both Rust copies → `pub use jasper_ring::{MIN_N_SLOTS, MAX_N_SLOTS}`; one Python pair in `fanin_coupling`, imported by the other two; add a MIN row to the pin |
| **header offsets / format enum / open-lock** | `ring/layout.rs` | C `_Static_assert`s, `ring_assets.py` | `test_ring_slot_ceiling_pin.py:122,183,244,310` | correct |
| **GAIN-CEILING `volume_limit ≤ 0.0`** | `camilla_config_contract.py:435 DEFAULT_VOLUME_LIMIT_DB` + `:438 ensure_volume_limit_db` | 7 hand-typed `devices:` blocks (`sound/camilla_yaml.py:674`; `active_speaker/camilla_yaml.py:2153,2331,2752,3437,3669,3881`), `outputd-cutover.yml:18`, `s0-sync-bench.sh:264`; fader mirror `camilla.py:39` | `dsp_apply.py:185` gates the file path; `cli/doctor/audio.py:1065` checks the durable file; `active_speaker/environment.py:375` classifies | emit the `devices:` block from `active_emit_devices` instead of 7 f-string templates; parse-and-refuse at `set_active_config_raw` (§D1) |
| **assistant peak ceiling** | `tts-protocol/loudness.rs:58 max_peak_dbfs: -3.0` | fan-in env `JASPER_OUTPUTD_ASSISTANT_MAX_PEAK_DBFS` (`fanin/config.rs:1131`) — **outputd ignores it** (`outputd/core.rs:110` uses `AssistantLoudnessConfig::default()`) | none; `test_wire_contracts.py:748` only asks whether *either* daemon reads the key | validate `≤ 0` fail-loud in fan-in (same shape as the two duck checks at `config.rs:1048,1060`); rename the fan-in-only keys `JASPER_FANIN_*` or make outputd read them |
| **camilla port 1234** | `camilla_config_contract.py:143 DEFAULT_CAMILLA_PORT` (read by `camilla.primary_controller`) | `jasper-camilla.service:75`, `camillagui/config.yml:17`, `bass_extension/__init__.py:368,439` (**hardcoded, bypasses `JASPER_CAMILLA_HOST/PORT`**) | none | `bass_extension` → `primary_controller()`; render the unit's `-p` from the constant at install |
| **socket paths** | `jasper/tts_routing.py:14,17`, `route_latency/status_socket.py:54` | `fanin/config.rs:1109,1116`, `jasper-voice.service:185`, `jasper-outputd.service` `Environment=` | `test_wire_contracts.py:520` (good, names its 2 deliberate exceptions) | correct |
| **SINGLE-WRITER env** | `fanin.env`+`outputd.env` → `fanin/coupling_reconcile.py:109`; `grouping-outputd.env` → `multiroom/reconcile.py` (layered **after** outputd.env in the unit — legitimate override, not a second writer); `renderer_lanes.env` → `renderer_lanes.py:465`; `60-jts-ring.conf` → install.sh lays, `ring_assets.render_ring_conf_wire` substitutes in place | — | headers name the writer in each file | correct; the layering is well-documented and I could not fault it |

## D. Findings (ranked)

| # | sev | file:line | what | evidence | cleanest fix |
|---|---|---|---|---|---|
| 1 | **Blocker** | `jasper/cli/doctor/audio.py:1065` + `jasper/camilla.py:891,1000` | **Non-negotiable 1 has an unguarded write door *and* a blind detector, and they are the same door.** `set_active_config_raw` uploads arbitrary YAML with no `volume_limit` parse; `patch_config` forwards an arbitrary mapping (a `{"devices": {"volume_limit": 12}}` patch is structurally accepted). Both deliberately leave the persisted `config_file_path` unchanged — and `check_camilla_volume_limit` reads exactly that persisted file (`_evidence.camilla_config_path()` → statefile `config_path`). **No check anywhere reads the running graph's `volume_limit`.** | `camilla.py:915-920` calls `config.set_active_raw(config)` with no parse; `camilla.py:1034` `c.query("PatchConfig", arg=patch)`; `doctor/audio.py:1069` `config_path = evidence.camilla_config_path()`; `get_active_config_raw` has 15 callers, none in `cli/doctor/` | Parse in `set_active_config_raw` with the stdlib `parse_camilla_devices_config` + the predicate `dsp_apply._volume_limit_safety_error:185` already spells; refuse a `devices` key in `patch_config`; **and** add one doctor check on `get_active_config_raw()`. Confirms + **upgrades p1-T05 #1** from Should-fix — the tile found the door, not the blind spot behind it. |
| 2 | Should-fix | ≈20 sites (table §C row 1) | **SAMPLE-RATE has no owner and no pin** — the only invariant of the five in that state. `outputd/shm_ring_source.rs:159` writes `rate: 48_000` while `outputd/types.rs:11` declares `pub const SAMPLE_RATE` in the same crate; `ring/layout.rs:212` and `c/jts_ring_shm.c:197` each hardcode the validator's literal | greps in §C | `pub const RATE_HZ` in `ring/layout.rs`, re-exported like `RING_SLOT_FRAMES` already is; one rate row in `test_ring_slot_ceiling_pin.py` |
| 3 | Should-fix | `rust/jasper-fanin/src/config.rs:645` | **`JASPER_FANIN_SAMPLE_RATE` is an unrequested knob that can only break the box**, and fan-in disagrees with itself about it: lane opens use `config.sample_rate` (`pcm_open.rs:319`) and Ring A uses it (`mixer.rs:1425`), but direct USB capture (`pcm_open.rs:240,285`) and impulse-tap timestamping (`mixer.rs:2703`) use the private `SAMPLE_RATE_HZ` literal | 3 spellings inside one crate; every downstream stage is fixed at 48 kHz (ring header, CamillaDSP template, asoundrc, `doctor/audio_runtime_outputd.py:741`) | Delete the env read; use the shared constant everywhere. AGENTS.md: "no new `JASPER_*` knob unless … hardware genuinely varies" |
| 4 | Should-fix | `rust/jasper-outputd/src/core.rs:110` | **Six `JASPER_OUTPUTD_ASSISTANT_*` keys are read only by jasper-fanin; outputd hardcodes `AssistantLoudnessConfig::default()`.** On a passive bonded member (the only box whose assistant path *is* outputd) the documented operator retunes are silently inert | `grep JASPER_OUTPUTD_ASSISTANT rust/jasper-outputd/` → only `..._REFERENCE_PATH`; `docs/audio-paths.md:~450` lists the keys as the retune surface | Either read them in `outputd/config.rs` or rename the fan-in-only ones `JASPER_FANIN_*`. `test_wire_contracts.py:748` checks the union of both daemons and structurally cannot catch this |
| 5 | Should-fix | `rust/jasper-fanin/src/config.rs:1131` | `max_peak_dbfs` accepts an arbitrarily **positive** value while the two duck knobs 12 lines earlier fail loud on positive. **Re-grade of p1-T19-2 #8:** the tile's mitigation ("CamillaDSP's clamped volume is downstream") does not hold — `volume_limit` caps the *fader*, not the signal, and `emit_sound_config`'s passive graph emits **no `Limiter`** (only the six active per-driver graphs do, `active_speaker/camilla_yaml.py:215,451`). A positive value is bounded only by digital full scale at the DAC narrow | `config.rs:1048,1060` bail on `> 0`; `loudness.rs:446 peak_cap_gain = max_peak_dbfs - source_peak_dbfs`; `loudness.rs:622 sanitize_tts_gain_db` clamps only the low side; `grep -c Limiter jasper/sound/camilla_yaml.py` → 0 | Same `anyhow::bail!` shape as the duck checks |
| 6 | Should-fix | `jasper/active_speaker/camilla_yaml.py:2153,2331,2752,3437,3669,3881` + `jasper/sound/camilla_yaml.py:674` | **Seven hand-typed `devices:` blocks** — the *values* have one owner (`active_emit_devices:338`, `ensure_volume_limit_db`), the *text* does not. Two formatters of the same field (`{volume_limit_db!r}` ×6 vs `{volume_limit_db:.1f}`), and a literal `channels: 2` on the capture side of all six active templates that no pin covers | side-by-side diff of `:2153` and `:3881` — identical but for `{output_count}` | One `render_devices_block(ActiveEmitDevices, …) -> str`; the emitters interpolate one `{devices_yaml}`. Confirms **p1-T05 §NON-NEG-1** ("no for the template") |
| 7 | Should-fix | `rust/jasper-outputd/src/main.rs:144` | `notify_systemd("STOPPING=1")?;` runs before `runtime_error_exit_code(e)`, so a notify failure replaces the real DAC error and the park classification never happens | confirms **p1-T20 #3** verbatim at HEAD | `let _ = notify_systemd("STOPPING=1");` |
| 8 | Should-fix | `rust/jasper-outputd/src/alsa_backend.rs:1638` | A DAC that disappears mid-run emits **no `event=`** — only `EPIPE`/`ESTRPIPE` log; `ENODEV`/`EIO` propagate to a bare `Error: …` | confirms **p1-T20 #4** at HEAD | `eprintln!("event=outputd.dac.write_failed pcm=… errno=… action=exit")` on the else arm |
| 9 | Should-fix | `jasper/camilla.py:445-478` | `_call` retries once with **zero delay**, forever, at ~10 s per call against a wedged daemon; the recorded fix for the resulting journal flood was demoting the log to DEBUG | confirms **p1-T05 #3** at HEAD (comment at `:453-459`) | per-controller failure stamp; short-circuit `best_effort` inside a ~1 s window |
| 10 | Should-fix | `deploy/systemd/jasper-fanin.service:8-10`, `jasper-outputd.service:8-10` | **Two of the three audio daemons reboot the Pi on a flap** (`StartLimitBurst=5` / `StartLimitAction=reboot`), while camilla — the one that actually crash-loops in practice — parks with a written record and a doctor check (`StartLimitAction=none` + `OnFailure=jasper-camilla-recover`). The blunt half has no removal condition | unit files; `deploy/bin/jasper-camilla-recover:263-265` is the model | Give fanin/outputd the same park-with-record treatment, or at least an `OnFailure=` that writes a record before the reboot |
| 11 | Should-fix | `jasper/fanin_coupling.py:707,862` + `jasper/renderer_lanes.py:128` + `fanin/config.rs:32` + `outputd/config.rs:82` | **`2..=16` is declared at 7 sites; the MAX is pinned across 3 and the MIN across none.** Confirms **p1-T21 #3**, and adds the site it missed (`renderer_lanes.py:128-129`) | `test_ring_slot_ceiling_pin.py:159` covers C + `layout.rs` + `outputd/config.rs` only | Rust: `pub use jasper_ring::{MIN_N_SLOTS, MAX_N_SLOTS}`. Python: one pair in `fanin_coupling`, imported by `renderer_lanes` and the outputd resolver. Add a MIN row |
| 12 | Should-fix | `jasper/bass_extension/__init__.py:368,439` | `CamillaController("127.0.0.1", 1234)` hardcoded twice, bypassing `primary_controller()` and both `JASPER_CAMILLA_HOST`/`PORT` | `camilla.py:1058` is the one construction site everything else uses | `primary_controller()`. Confirms p1-T05's cross-tile pointer |
| 13 | Nit | `deploy/systemd/camillagui.service` + `deploy/camillagui/config.yml:17` | **camillagui is a full, unclamped second writer to ws:1234** — it can set `main_volume`, load any config and edit filters with none of `camilla.py`'s clamps. Loopback-bound (`camillagui.socket:18`), SSH-tunnel only, and the socket file records that a LAN bind was deliberately removed | file refs | Earns its keep as a lab door; worth one line in `docs/audio-paths.md`'s volume-knob table, which does not mention it |
| 14 | Nit | `jasper/cli/doctor/audio_runtime_outputd.py:741` | `if sample_rate != 48000` — a bare literal in the one runtime check that would catch a rate drift | file ref | import the constant from finding 2's owner |
| 15 | Nit | `jasper/active_speaker/linearization_fit.py:327`, `crossover_v2/feature_optics.py:99`, `bass_extension/alignment.py:28,86,119` | Confirms **p0-duplicates #5** at HEAD: 5 filter-magnitude implementations, only the `sound/profile.py:905` ↔ `eq-math.js:35` ↔ CamillaDSP triple guarded (`scripts/check-peq-parity.mjs`). `bass_extension` uses an **analog prototype**, so it disagrees with the emitted digital filter near Nyquist by construction | file refs | as p0 proposed: one vectorized RBJ evaluator; extend the CI parity fixture |
| 16 | Nit | `tests/test_ring_slot_ceiling_pin.py:89-98` | Live `pytest.xfail` for "`MAX_RING_CHANNELS` is not declared in `layout.rs` yet". It is (`layout.rs:80`), so the branch is dead scaffolding on a cross-language safety pin | confirms p1-T21's cross-tile pointer | delete the branch |
| 17 | Earns-its-keep | `deploy/alsa/conf.d/60-jts-ring.conf` | 150 lines of comment for 21 lines of PCM — but every paragraph names a non-derivable constraint (why the period is fixed at 128, why an un-armed ring open *succeeds* instead of failing, which render rule applies per DAC floor) and it corrects an earlier wrong comment in place. This is what AGENTS.md's "why-pointer" rule is for | file itself | keep |
| 18 | Earns-its-keep | `jasper/renderer_lanes.py:208-283 RENDERER_LANES` | One row per lane carrying every per-lane fact (unit, env key, aloop device, ring device, preflight user, conf renderer). Adding a lane is one entry plus two files. The correct shape, and the model finding 6 should copy | file itself | keep |
| 19 | Earns-its-keep | `rust/jasper-outputd/src/content_fill.rs` | 97 production lines answering the one question no cumulative counter can — "is the speaker deaf *right now*". The only such surface on the chain; derives its threshold from the negotiated DAC geometry rather than a literal | file itself | keep |

**Confirmed from tiles, not re-reported as new:** p1-T05 #1/#3, p1-T19-2 #8 (re-graded, D5),
p1-T20 #3/#4/#6/#7, p1-T21 #3 (extended), p0-duplicates #5. Not re-reported: p1-T24's port table
(web sockets, off this chain).

## E. What only hardware/runtime can prove

1. **Whether the C ioplug's writer-dead silence actually keeps CamillaDSP DAC-paced** across a
   full fan-in restart — the fabrication path (`pcm_jts_ring.c:212-242`) has no host test.
2. **The reload gap.** Ring A/B are 2 slots ≈ 5.3 ms. Whether a CamillaDSP `SetConfig`/reload
   detach+reattach fits inside that, or produces an audible dropout / trips
   `outputd.content.deaf`, is a scope-on-the-DAC question.
3. **The audibility of the 25 dB `_graph_mutation` duck** at ordinary listening level, and
   whether `plan_live_edit`'s un-ducked arm (ADR-0219) really lands as an inaudible level step
   during a slider drag.
4. **The outputd-period/conf.d divergence.** I traced that it parks loudly; I did not verify
   which of CamillaDSP or outputd loses the race on a real box, i.e. whether the operator sees
   `outputd.shm_ring.config_error` or a camilla start-limit park.
5. **D5's exploitability**: whether a positive `max_peak_dbfs` produces audible clipping or is
   swallowed by the DAC's own headroom. Do not test this on a speaker you care about.
6. Whether `fanin.ring.stall_detected reason=no_reader` fires spuriously at boot, when fan-in
   (`Before=jasper-camilla`) writes for >1 s before CamillaDSP attaches.
7. Every ring free-run / stuck-demotion path (p1-T21 flagged the same limit).

## F. Coverage

**Read in full:** `docs/audio-paths.md` (772), `deploy/alsa/conf.d/60-jts-ring.conf`,
`deploy/alsa/conf.d/61-jts-renderer-lanes.conf`, `deploy/modprobe.d/snd-aloop.conf`,
`deploy/alsa/asoundrc.jasper` (lane half), `deploy/camilladsp/outputd-cutover.yml`,
`deploy/systemd/jasper-{fanin,camilla,outputd,camilla-recover,fanin-coupling-auto,camillagui*}.service`,
`deploy/systemd/camillagui.socket`, `deploy/camillagui/config.yml`,
`rust/jasper-ring/src/layout.rs`, `rust/jasper-outputd/src/content_fill.rs`,
`rust/jasper-outputd/src/shm_ring_source.rs` (prod half), `jasper/tts_routing.py` (head),
`jasper/renderer_lanes.py:100-300`, `tests/test_ring_slot_ceiling_pin.py`.

**Read in the parts the flow touches:** `jasper/camilla.py` (`_call`, `_graph_mutation`,
`set_volume_db`, `adjust_volume_db`, `set_main_mute`, `set_config_file_path`,
`set_active_config_raw`, `get_active_config_raw`, `patch_config`, `reload`,
`_coerce_main_volume_db`, `primary_controller`), `jasper/camilla_config_contract.py`
(constants + `ensure_volume_limit_db`), `jasper/dsp_apply.py` (`_volume_limit_safety_error`,
`validate_camilla_config`, `apply_dsp_config` signature), `jasper/sound/camilla_yaml.py`
(devices template + emit), `jasper/active_speaker/camilla_yaml.py` (`capture_device_for_playback`,
`active_emit_devices`, `_assert_volume_limit`, all 6 templates), `jasper/fanin_coupling.py`
(ring constants + both slot resolvers + `capture_kwargs_for_coupling`), `jasper/ring_assets.py`
(constants), `jasper/web/sound_setup.py:1350-1435` (live-draft),
`jasper/multiroom/reconcile.py:560-615,1395-1430`, `jasper/cli/doctor/audio.py:1065-1120`,
`jasper/cli/doctor/audio_runtime_{fanin,outputd,ring,camilla}.py` (check inventory +
`check_outputd_service`, `check_ring_geometry_coherence`, `check_ring_conf_floor_render`),
`rust/jasper-fanin/src/config.rs` (lanes, rate, period, slots, duck, assistant),
`rust/jasper-fanin/src/mixer.rs` (Ring A geometry, stall tracker, `SAMPLE_RATE_HZ`, `CHANNELS`),
`rust/jasper-fanin/src/mixer/pcm_open.rs`, `rust/jasper-ring/src/{lib,writer}.rs` (reader/writer
transaction, SPSC guard, liveness, publish outcomes), `rust/jasper-outputd/src/{main,config,
core,alsa_backend,types}.rs` (relevant functions), `rust/jasper-tts-protocol/src/loudness.rs`
(config + `decide_gain` peak cap), `c/jts-ring-ioplug/{jts_ring_shm.h,jts_ring_shm.c,
pcm_jts_ring.c}` (constants, liveness, silence fabrication), `deploy/bin/jasper-camilla-{recover,
pipe-guard}`, `tests/test_fanin_coupling_rust_contract.py`,
`tests/test_ring_emitter_ioplug_negotiation.py`, `tests/test_wire_contracts.py:520,748`.

**Skipped and why:** the AEC/UDP reference leg (a tap off outputd, not a hop in the chain);
`jasper/multiroom/` beyond the outputd env layering (bonded topology is its own scenario);
`active_speaker` commissioning/measurement flows (they use the chain but do not define it);
all Rust `#[cfg(test)]` bodies except the two I cite; `jasper/mux.py` and `source_events.py`
(hop 4's *policy*, owned by a different scenario). Verified every tile lead I reused against
HEAD; each is marked confirm / extend / re-grade above.
