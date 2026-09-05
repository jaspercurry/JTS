# JTS codebase quality review — 2026-09-05

**Audited tree:** `2d571e6b8` (`origin/main` at review start, 2026-09-05 06:56 EDT).
**Method:** a research pass on structure and quality measurement (the rubric is in the evidence
directory), then the five-phase comb from `docs/DEEP-AUDIT-PLAYBOOK.md`: Phase 0 cartography (6 agents),
Phase 1 tiled read of every product file (38 tiles, 573k lines), Phase 2 cross-cutting lenses and
end-to-end scenarios (11 agents), Phase 3 adversarial verification (3 skeptic agents over 55 sub-claims;
the reviewer independently reproduced the top findings), Phase 4 synthesis with a completeness critic.
~65 Opus/Sonnet subagents. Evidence lives beside this file in `docs/codebase-quality-review-2026-09-05/`:
the findings register (CSV), every agent report, the briefs, and the skeptic verdicts.
**Relation to prior work:** builds on `docs/DEEP-AUDIT-2026-08-25.md` (11 days and ~1,100 commits ago)
and on the four programs in flight — the general steward queue (#4030/#4085), attached hardware (#4027,
ADR-0235), the web UI cleanup (#4031), and the tuning-rightsize waves. Findings already queued there are
marked, not re-argued.

> Frozen snapshot. Finding IDs (`R-nnn`) key into the register; disposition belongs in the steward
> issues, not in edits to this file.

---

## 1. Executive summary

**The machinery is good; the seams are not.** Eleven days after the last audit the repo is measurably
better at the module level: the executed import graph is acyclic apart from one 4-module cycle, the tree
has zero orphan modules, zero unreferenced scripts, zero uninstalled units, zero real TODO markers, and
the hearing ceiling holds by construction at every emitter (the skeptic could not construct a positive
`volume_limit` from any shipped surface). What this review found is almost entirely *between* files and
*between* processes — places no single-file read can see, and no in-flight program owns:

1. **Four things are wrong in a way that matters today.** The WiFi passphrase is written unquoted into
   a file the recovery guardian `source`s as root. The *default* attended-sudo deploy path skips the
   identity and downgrade guards and rsyncs anyway. Privileged actions answer `200 ok` when polkit
   denies them. The multi-speaker election drops the local wake report whenever a peer's multicast
   arrives first, so the speaker that heard the user better stands down (only when peering is on).
2. **The structure is a flat bag with deferred imports holding it together.** 115 flat top-level
   modules; a 107-file `active_speaker/` root; 1,708 function-local `import jasper…` statements, 755 of
   them with no reason. Three web pages of 4–5k lines are each built around one closure that captures
   two to four values. The canonical primitives exist (`atomic_io`, `env_load`, `log_event`,
   `service_units`, `json_fields`, `status_socket`) and are bypassed beside their own import lines.
3. **Right-sizing is still the biggest lever, and the honest number is smaller than it looks.** The
   tuning zone is 41% of `jasper/` and its dissolution has stalled at the class boundary. Only 27% of
   829 `JASPER_*` tokens are knobs anything turns. But the *verified* deletable code is ~2,300 product
   lines plus ~1,600 test lines (plus ~7,900 if the bass-extension park is lifted) — the tiles'
   first-pass deletion claims were refuted 15 times on re-read. The suite is 1.4× the product and pins
   1,645 private names; it is honest, and it is aimed at the wrong altitude.

**Verification changed the grades.** Of 55 sub-claims the tiles and lenses raised at Blocker, the
skeptics confirmed 14 at Blocker or Should-fix, downgraded 24, refuted 12, and upgraded 3 (the peering
drop, the silent wake-leg death, the 1 % mute loop). Seventeen of the twenty-two tile "blockers" had been
graded Should-fix by the agent that read the code and promoted during aggregation — the same failure
mode the previous audit recorded. Everything in §2 below survived.

**Grades** (static evidence only; §9 lists what only hardware can prove).

| Attribute | Grade | Δ vs 08-25 | Confidence | One line |
|---|---|---|---|---|
| Hardware/audio safety (NN-1, NN-2) | **A−** | = | high | Ceiling one-owner at every emitter and not constructible above 0 dB from any shipped input; the raw upload door is belt-and-braces, and the doctor reads the persisted file rather than the running graph. XVF brick guard holds; one unused write command to delete. |
| Secrets (NN-3) | **C+** | new | high | Compartments exemplary. The PSK stash is sourced unquoted as root (R-001); the general redactor misses every underscore-prefixed key name and has zero tests — latent today because its three consumers carry provider text, not env dumps. |
| Deploy integrity (NN-4, NN-8) | **C+** | new | high | The default attended-sudo path bypasses both guards (verified by execution); `SKIP_INSTALL=1` bypasses them for the checkout only; health is advisory by design and says so. Staleness traps are genuinely closed. |
| Resilient | **B** | ↓ from A | medium | Held port 8780 reboots the box up to three times before the bootloop guard; `outputd.env` has a lost-update window; three uncapped STATUS readers; 2–3 forks/second at idle. Many mechanisms earn their keep. |
| Observable | **B−** | ↓ from A− | high | Excellent primitives, no contract: 1,309 event names with no registry, 9 publish mechanisms, the cue manager emits nothing, `/state.voice.wake_legs` is labelled runtime truth and is the configured dict, "daemon dead" ≠ "speaker silent", no silence detector. |
| Clean — separation & SSOT | **C+** | = | high | Module graph acyclic; package graph is a 23-package SCC held by 15 shelving mistakes; ≈20 spellings of the sample rate; primitives bypassed beside their import. |
| Right-sized | **C** | ↑ from C− | high | Tuning zone 41% of `jasper/`; 289 knobs nobody turns; ~2.3k product + ~1.6k test LOC verified dead. |
| Tests | **B−** | = | high | Honest and broad; 1,645 private-name patches; guards that measure the wrong thing (env-codified, atomic-io ratchet, unit allowlist); the NN-3 redactor has zero tests; denial paths untested by construction. |
| Docs | **B** | ↑ from C+ | high | Governed corpus, claims verified; 157 ADRs with no index, 3 batch-ADRs, one 1,694-line plan that declares itself complete, one prior-audit sentence the code contradicts. |
| Newcomer followability | **C** | ↑ from C− | high | Where things live is derivable from the graph, not from the tree. |

**Overall: B engineering, C+ proportionality.** The last audit said the repo needed an editor. It still
does, but the first job now is a **welder**: a dozen seam fixes, most under 30 lines, that make the
non-negotiables true by construction (§2). Then the structural moves (§3) that let every later steward
round find things without grep.

**Genuinely strong, earned:** the audio data plane is one pipeline with one ring-layout owner pinned
across C/Rust/Python; `ensure_volume_limit_db` as the single funnel both emitter families pass
through; `VolumeOwner` + `volume_latch` arbitration; `source_intent.request_source_intent` (the one
privileged path that *proves* convergence); the file-mailbox → root-worker shape in
`usb_gadget_forensics` and `accessories/reconcile`; `jasper-camilla`'s park-with-record policy;
`renderer_lanes.RENDERER_LANES`; `jasper/fanin/latency_mode.py` (394 lines, 2% prose); the C ring
core; the doctor's typed contract; `tests/test_cue_registry_coverage.py`'s two-sided guard;
`build-sandbox.sh`'s OOM inversion; `content_fill.rs`; the aec-bridge mic-vanish ladder; the
`research` scheduler's `wait_for` + server-side cancel. These are the templates the rest should copy.

---

## 2. Fix first — findings that survived verification

Severity is the skeptic's, not the tile's. Territory per #4085: S = general steward, D =
doctor/state/resilience, H = hardware input, W = web UI, T = tuning. "Reachable" = on the owner's shipped
configuration (full-profile Pi 5 with XVF3800 + Apple dongle; streambox Zero 2 W; peering off by default).

### 2.1 Blockers

| # | What | Where | Fix (smallest diff) | Terr. | Verified by |
|---|---|---|---|---|---|
| R-001 | **WiFi passphrase written shell-unquoted into a file the guardian `source`s as root.** Any PSK with a space silently defeats WiFi recovery (`$JASPER_WIFI_PSK` comes back empty; the guardian takes the open-network branch); shell metacharacters execute as uid 0. Reachable by every household whose passphrase has a space, and by anyone who can POST to `/wifi/`. | `jasper/wifi_guardian_persistence.py:186-188`; `deploy/bin/jasper-wifi-guardian:126-131` (`set -a; source`); root oneshot unit; `deploy/lib/install/env-migrations.sh:571-573` | Quote on write (`shlex.quote` / `jasper_env_quote_value`) in both writers **and** stop sourcing: parse `^KEY=` the way `read_stash` already does. Pin with a space-bearing and a `$(…)`-bearing PSK. | S | reviewer (three code points) + L4 (executed) |
| R-002 | **The default attended-sudo deploy path bypasses the identity and downgrade guards and rsyncs anyway.** When passwordless sudo is absent and a human is at a tty, the script prints `identity: skipped` / `direction: skipped`, then deploys with `--delete` onto a peer_id that does not match `.env.local` and a commit that is a downgrade — no flag, no `JASPER_DEPLOY_ALLOW_DOWNGRADE`, no `JTS_ACCEPT_NEW_IDENTITY`. BRINGUP names the lab-Pi case as recurring. Every guard test pins `SUDO_INTERACTIVE=0`; the one test that sees the skip pins it as intended, by regexing source text. `SKIP_INSTALL=1` skips the same guards but only the checkout is at risk. | `scripts/deploy-to-pi.sh:167-199,238,602-675,625,682-687,763,985` | Capture the manifest and peer_id over a second non-tty `ssh -o BatchMode=yes` channel (only `sudo` needs the pty) so the guards survive attended sudo; hoist the identity guard above `SKIP_INSTALL`. One pin: a downgrade aborts under `SUDO_INTERACTIVE=1`. | S | reviewer (read) + S4 and P3 (both executed under a pty with stubbed ssh/rsync) |
| R-003 | **Privileged actions answer `200 ok` when polkit denies them.** `jasper-usbsink-volume.service` is in the audio-refresh list but in neither `restart_broker.MANAGED_UNITS` nor the polkit rule; `POST /system/restart/audio` `Popen`s `systemctl try-restart …` and never reads rc; systemd calls `TryRestartUnit` per unit so the others restart and this one is silently denied. Eleven in-tile mutation sites discard rc the same way; `restart_count` increments before the swallow. PR #4120 does **not** fix this (it changes the installer's root-side set). | `jasper/control/handlers/system.py:459-471,504-517,360`; `shairport_supervisor.py:410-421`; `restart_broker.py:98`; `deploy/polkit/49-jasper-control.rules:50-100` | Route every in-tile mutation through `restart_broker.manage_units` (same call length; returns `ok`/`rc`). Replace the hand-written set in `tests/test_restart_broker.py:131` with a derived one: `local_sources` registry tuples ∪ `debug_mode.SUBSYSTEMS` ∪ `RECONCILE_UNITS` ∪ `CORE_AUDIO_RESTART_UNITS` ⊆ `POLKIT_MANAGE_UNITS`. | S/D | reviewer (set difference) + S1 + P3 (re-derived at runtime) |
| R-004 | **The multi-speaker election stands down the speaker that heard the user better.** When a peer's WAKE multicast (~1 ms) beats local detection (30–150 ms jitter, per the config's own comment — i.e. essentially always), `_on_peer_wake` enters CANDIDATE "so our local detector can join when it fires", and `_on_local_wake` then returns `[]` — the local report is never ranked and the machine emits `StandDown`. Separately, SUPPRESSED + weak wake returns no action, the ARBITRATE future never resolves, and voice's 0.5 s client timeout fails open to `WIN`, inverting LOSE into WIN. Gated on `JASPER_PEERING=on` (off by default; the `/rooms/` page turns it on). | `jasper/peering/state.py:284-337,408-412`; `peering/daemon.py:96,363-391,461-464`; `voice_daemon.py:3944-3945,4011-4013` | In CANDIDATE with no local report, record it and `BroadcastWake` instead of returning `[]`; every terminal path emits `StandDown`; derive both timeouts from one constant with the client's above the daemon's (today the daemon's 0.65 s fail-open is unreachable on the wire). Pin with the pure state machine (the skeptic's `drive_sm.py` is the harness). Must land before peering is turned on. | S | S5 + P3 (drove the state machine) |

### 2.2 Should-fix, in blast-radius order

| # | What | Where | Fix | Terr. | Verified by |
|---|---|---|---|---|---|
| R-005 | Secondary wake-leg and manual-mic tasks are bare `create_task`s with no done-callback; the shutdown path retrieves and discards the exception inside `except (CancelledError, Exception): pass`, so a dead leg's traceback reaches the journal **nowhere, ever** — and `/state.voice.wake_legs` is labelled "actually-armed … runtime truth" while publishing the configured dict. On the XVF box a dead leg is a silently halved wake surface. | `voice_daemon.py:2373-2386,2500-2505,4783-4788` | `add_done_callback` that logs `event=wake.leg_died`; publish the tasks still `not done()`. | S | S5 + P3 (upgraded) |
| R-006 | 1 % listening level mutes the speaker and re-mutes at 1 Hz forever: `percent_to_db(0) == percent_to_db(1) == −50`, the level predicate says unmuted and the dB predicate says muted, and the reconciler's own write goes through the dB path, so `mute_drift` is true every tick and `event=volume.reconciled` lands in persistent journald every second. | `jasper/volume_coordinator.py:1993-1998,2066-2079,1811,1822-1825,1893`; `volume_curve.py:102-104` | Give `_write_camilla_db_with_mute` an explicit `mute` parameter fed from the level-domain intent; pin that level 1 converges in one write. | S | S6 + P3 (ran both predicates; note the muter is the dB path, not `_set_camilla`) |
| R-007 | The streambox profile has no boot volume restore and no drift repair: `VolumeCoordinator.initialize` and `VolumeObserver` are constructed only in `jasper-voice`, which streambox disables; `/sound/` can repair drift only when a human saves EQ. | `deploy/lib/install/systemd-units.sh:1121-1127`; `voice/daemon_main.py:834,855`; `volume_observers.py:159` | Start a `VolumeObserver` (or at least `initialize()`) from `jasper-control` on the streambox profile. | S | S6 + P3 |
| R-008 | `get_travel_routes` declares `untrusted_output=True` and never arms the taint monitor (`make_travel_routes_tools` takes no monitor while every sibling pack passes one), so third-party route text enters the context untainted and the next consequential Home Assistant action skips its confirmation gate. `dispatch_tool` never reads the flag: the declaration is decorative. Needs both packs enabled. | `tools/travel_routes.py:114,145`; `tools/packs.py:315-316` vs `:326,359,366`; `tools/home_assistant.py:278-288` | `packs.py:316` passes `monitor=d.untrusted_monitor`; better, `dispatch_tool` calls `monitor.mark()` whenever `tool.untrusted_output` and the payload is non-empty, and the per-tool calls delete. Pin: every `untrusted_output=True` tool is built with a monitor. | S | S5 + P3 |
| R-009 | A held port 8780 reboots the box: `control/server.py main()` binds inline with no try/except, exit 1, `StartLimitBurst=4 × RestartSec=2` = 8 s to `StartLimitAction=reboot`; the bootloop guard bounds it at three reboots. (The "unplugged mic reboots the box" half was refuted: the bridge sits behind a `ConditionPathExists` marker the reconciler clears.) | `jasper/control/server.py:2178,2241`; `deploy/systemd/jasper-control.service:18-20,56-58`; `jasper/cli/aec_bridge.py:1077-1176` | `try/except OSError` → `return 78` + `SuccessExitStatus=78` (copy `jasper-voice.service:219`); give the five permanent aec-bridge faults exit 78 too. | D | L2 + P3 (split) |
| R-010 | `outputd.env` lost-update window: the udev-triggered bash reconciler does `cp` → mutate → spawn a Python validator → `mv` with no lock while `coupling_reconcile.py` read-modify-writes under `/run/jasper-fanin-coupling.lock`; the ordered spine is safe, a hotplug landing inside the Python window is not. `aec_mode.env` has the same shape. | `deploy/bin/jasper-audio-hardware-reconcile:600-613,838-849`; `jasper/fanin/coupling_reconcile.py:889-919,1739`; `deploy/lib/jasper-env-file.sh:75` | One new `flock /run/jasper-outputd-env.lock` around both writers (not the coupling entry lock — the bash unit is started from inside it); make `install.sh:1416` source the env-file lib. | H | L2 + L4 + P3 |
| R-011 | `POST /system/audio-quality` can never succeed: it writes `/var/lib/jasper-asound/asound.conf` from a `ProtectSystem=strict` unit whose `ReadWritePaths` omit it. Fails loudly (502) — a broken feature, not a lie. `systemctl reload avahi-daemon` is granted to neither service user; both docstrings say avahi inotify-watches the directory. | `deploy/systemd/jasper-control.service:139,160`; `deploy/bin/jasper-render-asound-conf:15,56`; `peering/avahi.py:126-138`; `avahi_service.py:114-134` | Add the path to `ReadWritePaths` (+ a unit-vs-writer path pin); **delete** both avahi reloads. | D/S | S1 + P3 |
| R-012 | Three sync `STATUS\n` readers with a per-op timeout and no byte cap (an unbounded-memory primitive on a 1 GB Pi); `renderer.py:112` connects to the mux socket with no timeout on the chain `VolumeObserver` walks every tick (the `TimeoutError` in its `except` is dead); `usage.db` has no index and no retention and `SpendCap.allowed()` full-scans it with `strftime()` on the column on **every wake** (on by default: `.env.example:203`). | `audio_validation.py:505-517`; `control/airplay_health.py:1690-1700`; `correction/runtime_integrity.py:101-112`; `jasper/renderer.py:112`; `usage.py:414,823-833` | Route the three through `route_latency.status_socket.read_status_socket_or_none` (and move that module to `platform/`); `asyncio.wait_for(…, 1.0)` on the connect; integer epoch column + index + retention. | S | L2 + P3 |
| R-013 | No surface reports wake recency or mic level between wakes: the watchdog catches frames *stopping*, nothing catches frames of *silence* — the classic XVF3800 failure (chip alive, capture channels muted) that `install.sh:2163-2166` names as the thing doctor exists to catch. | `voice/input_presence.py:26`; `cli/doctor/wake.py:42,277`; `voice_daemon.py:685,790-810` | `/state.voice.last_wake_at` + a rolling idle-frame RMS; one doctor check that fails when RMS sits at the floor for hours with the mic unmuted. | D | S5 + P3 (partial carve-out noted) |
| R-014 | `redact_secrets` misses every underscore-prefixed key name (`\b` cannot match after `_`): 8 of 23 realistic shapes leak, incl. `JASPER_WIFI_PSK=`, `JASPER_HA_TOKEN=`, `GOOGLE_CLIENT_SECRET=`. Latent today — its three consumers carry provider HTTP bodies and exception text, and the live provider keys are caught by the prefix regex — but it is the sole guard on `/state.voice.connection_error` (served on `0.0.0.0:8780`), one of eight redactors in two languages, and it has **zero tests**. | `jasper/secret_redaction.py:23-31`; consumers `voice/_supervisor.py:29`, `cli/doctor/_shared.py:44`, `cli/doctor/voice.py:24`; `flight_recorder.py:63-90` (publishes the DEBUG ring on WARNING, no redaction) | Replace the leading `\b` with `(?<![A-Za-z0-9])`; one parametrized pin; converge the eight redactors to two; apply it in `RingFlushHandler.emit`. | S | reviewer (ran it) + L4 + P3 (downgraded on reach) |
| R-015 | `/sound/` and `/correction/` run two divergent implementations of the summed test-tone flow: opposite `audio` defaults (`bool(raw.get("audio"))` vs `bool(raw.get("audio", True))`), different `duration_ms` handling, different refusal shapes — an identical POST plays a tone on one page and is silent on the other. Owner call 1 in #4085. | `web/sound_setup.py:4173-4358` vs `active_speaker/web_commissioning.py:2204-2333` | `/sound/` delegates to `web_commissioning.start_summed_test`; delete the copy. | W/T | T16-2 + P3 |
| R-016 | NN-1 belt-and-braces: `set_active_config_raw` / `patch_config` validate nothing and deliberately leave `config_file_path` unchanged; `check_camilla_volume_limit` reads that persisted file, so the running graph is never checked for `volume_limit`. Not reachable today (every caller uploads emitter output that passed `ensure_volume_limit_db`); becomes reachable the moment a future emitter bypasses the funnel. ADR-0101 says the non-negotiable tier gets the pin anyway. | `jasper/camilla.py:891-941,1000-1039`; `cli/doctor/audio.py:1065-1112`; `web/sound_setup.py:1391-1408` | Parse-and-refuse in `set_active_config_raw` with the predicate `dsp_apply._volume_limit_safety_error` already spells; refuse a `devices` key in `patch_config`; have the doctor compare against `get_active_config_raw()` when readable; `log_event` the fader clamp (`camilla.py:158` is prose). | S/D | T05 + S2 + S6 + P3 (constructibility refuted) |
| R-017 | Wake edges with no cue: button + spend cap returns `"CAP"` (HTTP 503 to the accessory, but the household hears nothing and no `log_event`); research-window cancel awaits up to 20 s inside the main mic loop and drops the wake with a prose WARN (the 64-frame queue overflows and the heartbeat stops against `WatchdogSec=30s`); the `MEASURE_PAUSE`-in-acquire-window case is real but sub-millisecond and bounded at 120 s. | `voice_daemon.py:4644-4645,3466-3478,2647`; `voice/output_gate.py:85-102` | Cue + `log_event` on the CAP path; play `INTERNAL_ERROR_CUE_SLUG` before the cancel-timeout return and move the wait into `_arbitrate_acquire_drain`; `_measurement_pause_detailed` returns `BUSY` while `_acquiring`. | S | T01 + S5 + P3 (F3 downgraded) |
| R-018 | `jasper-web-streambox.service` runs the same `python -m jasper.web` as root with 5 hardening directives vs 19. The `User=` deferral is legitimate (documented removal condition, pinned); the 11 uid-independent directives are the gap. | `deploy/jasper-web-streambox.service` vs `deploy/jasper-web.service:126-202` | Copy the 11 across; extend `test_systemd_hardening.py` to require them on both units. | W/S | T24 + S1 + P3 |
| R-019 | The accessory reader tasks are unsupervised: `_read_device` catches only `OSError`, the reap loop `del`s the task without inspecting it and only runs on the *next* udev event, and `_maybe_start` fires only on `add` — a still-plugged accessory whose reader died is permanently dead while the status file reports the bridge healthy. | `accessories/bridge.py:536-546,701,728-742` | In the reap loop log `active[p].exception()` and re-`_maybe_start(p)`; count it in the status file. | H | T09 + L2 + P3 (worse than filed) |
| R-020 | `staging.py` logs a metadata-write failure and still returns `status: "staged"` while the reader answers `not_staged` (fails closed; misleads the operator). `calibration.store_calibration` writes household state with bare `write_text` while importing `atomic_write_text`. | `active_speaker/staging.py:1892-1914`; `audio_measurement/calibration.py:36,521,540` | Set `payload["status"] = "unstaged"` on the exception; use `atomic_write_text`. | T | T14-2 + T12-2 + P3 |
| R-021 | `heal_shared_state_modes` spawns `/usr/bin/python3` from 12 install call sites per deploy (an ADR-0226 breach, not just cost); `jasper.identity` pulls the peering state machine (10.7 ms on x86, several× on the Zero) into six processes for one path constant; `chip_aec/health.py` → `alignment` → `audio_measurement.ramp` pulls `asyncio` + `ssl` into every reconciler shim for a verdict table. | `deploy/lib/install/env-migrations.sh:22-43`; `jasper/identity.py:42`; `jasper/peering/__init__.py:60-65`; `chip_aec/alignment.py:22` | Call the heal once from `main()`; drop the eager `state`/`rank` re-exports; move the two-line helper to a leaf. | S/H | T23 + S4 + T07 + L5 + P3 |

Also fix-now, smaller (all confirmed): `CLEAR_CONFIGURATION` (`jasper/xvf/xvf_host.py:88`) is a
write-only XVF command with zero consumers that writes the same DataPartition whose corruption is the
documented brick cause — delete it and add it to the forbidden set (NN-2). `output_hardware.py:660`
runs `aplay -L` with no timeout on the DAC-vanished path (bounded only by the unit's
`TimeoutStartSec=50s`). `rust/jasper-fanin/src/main.rs:201` is an unbounded `mpsc::channel()` fed from
the SCHED_FIFO thread into an `fdatasync` writer — a queue that grows exactly under xrun load; the
sibling tap in the same crate already uses `sync_channel`. `max_peak_dbfs` accepts a positive value
(nothing sets it; delete the knob or `.min(0.0)`). `outputd/main.rs:144`'s `notify_systemd(…)?` should
be `let _ =`. `jasper-host-clock`'s `Dll` is constructed and never ticked — two constant fields sit in
`/state` as live diagnostics; delete them (the claim that a Python test pins them was refuted).

### 2.3 Downgraded or refuted on verification (so nobody re-files them)

`prepare_period` allocating on the fan-in audio thread — **refuted** (zero allocation shapes in the
function; the `warn!` is in the next function on a misconfiguration path). The two "AEC reference
available" predicates disagreeing — they agree on the shipped XVF box; DRY only. `audio_quality.env`
two writers — seed-once + one runtime writer with a header, correct. The research poll loop with no
attempt ceiling — bounded at the right layer by `wait_for(300 s)` with a server-side billing cancel.
`research` + `calibration_agent` as two OpenAI clients — three different APIs for three concerns.
`wake_events.sqlite3` unbounded — rows only (~1 KB each); the 128 MiB audio sweep exists. `openai`
provider keys leaking through `redact_secrets` — caught by the prefix regex. Deploy health "never gates"
— true, and AGENTS.md does not promise a gate; `surface_system_health` says "ADVISORY" and names the
false-positive class that makes a gate a nanny today; emit a machine-readable line instead. The stdlib
"venv broken" probe — the code gates on RAM correctly; the previous audit's sentence is what is wrong.
`crossover_v2`'s import charter — false, but tuning is Pi-5-only and numpy is present; a docstring fix.

---

## 3. Structure — the target shape

### 3.1 Layers and packages

Three independent measurements agree: the *module-level* import graph of `jasper/` (749 modules) is a
DAG except one 4-module cycle in `bass_extension/adapters/` (`base.py:109-111` imports its three
siblings at file bottom; each imports `base` at top — fix: a 3-entry `adapters/registry.py`). Every
other reported cycle exists only through **function-local imports**: 1,708 of them knit 72 modules into
one strongly-connected component whose minimum feedback set is 25–32 edges — so the job is not
"invert an import" but "put the fact in the module that owns it". At *package* granularity the graph
is a **23-package SCC** (`web ↔ cli ↔ control ↔ voice ↔ active_speaker ↔ tools ↔ config …`) held by 15
module-level edges, ten of which are shelving mistakes:

| Misfiled module | Why it is a library, not what its directory says | Move to |
|---|---|---|
| `cli/aec_bridge_{config,engines,telemetry}.py` (1,246 LOC), library half of `cli/wake_enroll.py` (673) | only `cli.aec_bridge:main` / `wake_enroll:main` are entry points; `wake_corpus/bridge_session.py`, `audio_profile_state.py:30`, `audio_validation.py:46` import them | `jasper/aec/`, `jasper/wake/` (thin `main()` shims stay in `cli/`) |
| `web/_common.py` env-atomics + `systemctl` half (of 1,511 LOC), `web/_systemd.py` (541) | `control/debug_control.py:37`, `control/usb_gadget_forensics.py:13`, `cli/doctor/correction.py` import a wizard module for platform primitives | `jasper/platform/` |
| `control/client.py`, `control/uds.py` | an IPC client imported by `audio_validation`, `measurement_window`, `usbsink/volume_bridge`, `accessories/bridge` | `jasper/platform/` beside `busctl` |
| `route_latency/status_socket.py` | the canonical bounded STATUS reader, used by daemons, filed inside a one-CLI package | `jasper/platform/` |
| `web/correction_crossover_v2.py` (5,440 LOC) | no HTTP, no routes, no CSRF — it is the crossover-v2 conductor engine parked at a web address | `active_speaker/crossover_v2/` whole, not the two sections #4031 Phase D.5 names |
| `web/correction_crossover_backend.py` (1,698), `web/volume_floor_tone.py` | zero HTTP; three `active_speaker/` modules document them as their downstream | `active_speaker/` |
| `correction/level_match.py` | imported by `web/correction_crossover_backend.py`, not by Room | beside the `audio_measurement/ramp.py` kernel it adapts |

**The layer contract** (the L1 report ships a working `import-linter` config, run at HEAD):

| Layer | Members |
|---|---|
| L1 platform | `atomic_io env_load env_file log_event json_fields backoff busctl transition_log flight_recorder debug_mode service_units secret_redaction watchdog doctor_contract percentiles os_fault memory_policy model_downloads http_security` + the relocations above |
| L2 contracts (frozen vocabularies, no I/O) | `camilla_config_contract music_sources librespot_state source_state airplay_mode spotify_uri install_profile capture_protocol dsp_numpy wake_legs wake_ports wake_conditions identity identity_state` |
| L3 net + hardware | `mdns avahi_service control_advert usb_network usbgadget wifi_* speaker_name* oauth_redirect audio_hardware/ output_hardware mics/ xvf/ usb_mic ring_assets renderer renderer_lanes bluealsa_probe usbsink/ chip_aec/ aec_engines/` |
| L4 audio-core | `camilla* dsp_apply fanin/ fanin_coupling multiroom/ sound/ output_topology* audio_io audio_runtime_* audio_quality audio_profile_state audio_input_view audio_validation* transport_coherence tts_routing measurement_window enhanced_aec aec_sweep local_sources/ volume_* assistant_loudness assistant_volume` |
| L5 tuning | `active_speaker/ audio_measurement/ correction/ bass_extension/ calibration_agent/ attribution/ research/` |
| L6 daemons + integrations | `voice_daemon config voice/ control/ mux source_* bus spotify_* bluetooth/ accessories/ peering/ wake* wake_corpus/ cues/ tools/ transit/ usage timers conversation_history accounts google_* home_assistant weather subway citibike` |
| L7 surfaces | `web/ cli/` |

At HEAD the full layers contract is broken by 55 pairs / 109 chains; three narrow forbidden contracts
are **2–6 edges from green** and can be merged as ratchets immediately: `platform-is-a-leaf` (2 edges,
both `env_load` → reconciler path constants), `contracts-are-leaves` (4 edges), `surfaces-are-leaves`
(6 edges, all the misfiled modules above). Because import-linter's graph counts function-local imports,
the contract cannot be evaded by deferring an import — which is exactly the property a regex test lacks.

**Top-level regrouping.** The T18 proposal (8 packages) was tested against the real graph: it
*relocates* the SCC into a 31-module `audio/` that becomes mutually dependent with nine other packages.
The corrected move table (L1 §3) keeps `platform/ net/ identity/ wake/`, splits `audio/` along the
L2/L3/L4 line, and marks which moves are pure `git mv` (platform, net, wake, aec, assistant) and which
need one of the cuts C1–C11 first. Cheapest-first order: C1 (3 lines), then C2/C5/C6/C7/C8 ("move one
constant" or "pass one argument", ~40 lines total, six modules leave the SCC), then the four
signature inversions, then the one structural split (`runtime_contract.py` → `types` + `queries`).

**Deferred-import rule for AGENTS.md** (currently unwritten; 755 of 1,708 sites have no reason): a
function-local `import jasper…` needs one of three reasons on the line above — `# lazy: <numpy|scipy|
aiohttp>, ADR-0226` in a `web/`/`cli/` entry point; `# cycle: <module> imports us at module level` plus
an issue; or a genuine runtime branch. Never a local import of a module the file already imports at
top (109 such sites today — pure deletion).

### 3.2 God files — the concrete splits

| File | LOC | The split (from the tile that read it) | In flight? |
|---|---|---|---|
| `jasper/voice_daemon.py` `WakeLoop` | 4,528 LOC, 96 methods, ~90 attrs | Three seams share no mutable state with the wake→turn loop: measurement window (11 methods, its own 2,421-line test file; #4104 moves it), research announcer (11 methods), conversation capture (3). ~800 LOC out, no new layer. Also: 185 LOC of test doubles (`for_tests`) shipped in the daemon → `tests/`. `daemon_main.run()` (643 LOC) → `_build_services(cfg) -> Services` + `Services.aclose()`. | #4104 (hold only) |
| `jasper/web/correction_setup.py` `_make_handler` | 994 LOC, CC 156 | The closure captures exactly two values (`cfg["hostname"]`, `cfg["idle_hold"]`); put `cfg` on the server and make `Handler` module-level; routes table + `correction_room.py` + `correction_capture.py`. The closure is why `tests/test_correction_setup.py:419` asserts on *source text*. **Not in #4031 Phase D's ledger** — add it. | no |
| `jasper/web/sound_setup.py` | 4,962 LOC; 55 `_active_speaker_*` functions / 2,784 LOC | Closure captures four values; 34 of 38 POST routes need at most one → frozen `SoundRoutes` + a `POST_ROUTES` dict mirroring the existing `_GET_JSON_ROUTES`. The 2,784-line commissioning backend is a second, already-divergent implementation of `active_speaker/web_commissioning.py` (different blocker codes, `duration_ms` handling, opposite `audio` defaults) — owner call 1 in #4085: `web_commissioning` owns it. | #4031 D.2 |
| `jasper/active_speaker/runtime_contract.py` | 5,152 LOC (4,465 code), 102 defs | Six concerns whose callers already separate them: the topology contract (read by `output_topology`/doctor/multiroom), ring/outputd widths (fanin/control), the flat-graph verifier (only `sound/camilla_yaml.py`) — move it next to the emitter it mirrors; keep only the active-graph verifier here. `_active_graph_evidence` (768 LOC, CC 150) splits on branch lines already in the source. | no |
| `jasper/active_speaker/baseline_profile.py` | `build_baseline_profile_candidate` 985 LOC, CC 138 | Per-driver derivation is a loop body; lift it. | T |
| `jasper/active_speaker/crossover_v2_flow.py` `CrossoverV2Session` | 3,703 LOC, 155 methods | The cutover plan's §6 target is ≈1,500; the shrink so far is barrels and prose (158 → 155 methods). §6.1 (eight constants) untouched; a fifth barrel §6 never mapped (`:736-806`). `TuningSession` and `CrossoverV2Session` are both constructed in the same request (`web/correction_crossover_v2.py:6286-6305`) — the join W5-b has not landed and the vocabulary has already forked (`POSITION_AXIS_*` in both `spatial.py:604` and `contracts.py:1340`, nothing checks they agree). | T (w9) |
| `jasper/multiroom/reconcile.py` `main()` | 864 LOC, 8 nonlocal role flags | Extract pure `(cfg, topology) -> RoleDecision`; the flags collapse. | S |
| `jasper/control/server.py` + `handlers/` | 2,331 + 215 `_server.<name>` reach-throughs over 79 names | #4114 in flight; `handlers/measurement.py` (82 LOC, zero reach-throughs) is the proof it is avoidable. | #4114 |
| `jasper/control/audio_health.py` / `airplay_health.py` | 2,979 / 1,773 | Sampler bodies; leave until the `/state` contract (§4) lands. | D |
| `rust/jasper-outputd/src/state.rs::snapshot_json` (959 LOC), `config.rs::from_env` (534/593) | | Mechanical splits named in the T20 report; both daemons' `from_env` share the same 30-key shape. | S |
| `deploy/install.sh` | forked by profile: two `main()` lists differing in 6 rows, 363 lines of dry-run heredoc prose, a 202-line test keeping them in sync | One `STEPS` table (`name \| profiles \| fn \| plan phrase`) that `main()` iterates and `--dry-run` renders; hoist the three steps that run at a different altitude per profile first; the loop is also where the rollback transaction (full-profile only today) and a per-step `jasper_install_log` belong. | S |
| `deploy/index.html` | 1,551 lines: 481 inline CSS + 730 inline ES5 JS, the only page not on `deploy/assets/<page>/`, re-implements `http.js`'s `controlToken()` | Fold into the asset convention; #4031 owns. | W |

### 3.3 One home per primitive

The tree does not have a duplicate-*subsystem* problem — every large seam probed is genuinely
converged (one CamillaDSP emitter layer, 23 modules on one DSP analysis kernel, one doctor framework,
one ring-layout owner). It has a duplicate-*primitive* problem, and in almost every case the canonical
module already exists:

| Concern | Canonical | Bypasses at HEAD | Guard that would hold it |
|---|---|---|---|
| Atomic file write | `jasper/atomic_io.py` | 15 hand-rolls incl. 2 files that already import it; the `tests/test_atomic_io_conventions.py` ratchet keys on `mkstemp`, so 19 files using fixed `path + ".tmp"` (the *less* safe pattern) escape it; `google_creds.save_token:235` does this in the secrets compartment | key the ratchet on `os.replace`/`rename`, not `mkstemp` |
| Env-file read | `env_load.parse_env_file` | 19 modules with private `KEY=value` parsers (6 on `/var/lib/jasper/*.env`); 6 spellings of "is this value true" | `platform-is-a-leaf` + one forbidden-pattern lint |
| Env-file write | Python `web/_common.write_env_file`; bash `deploy/lib/jasper-env-file.sh` | `install.sh:1416` non-atomic `sed`+`>>`; two lock domains (R-010); no owner header from `write_env_file` (5 of ~20 writers comply with AGENTS.md's header rule) | `owner:` kwarg emitting the header; one `flock` path |
| Structured log | `log_event` (1,455 sites, best-governed thing in the tree) | 59 raw `event=` format strings; 8 `print("event=…")` the conventions test does not see | extend `test_log_event_conventions.py` to `print` |
| systemd unit state | `service_units.read_unit_states` ("the one reader", ADR-0233) | 1 consumer; 3 rival multi-unit readers, 4 rival block parsers, 11 single-property probes | the doctor's rule-1 drift check, widened |
| STATUS socket read | `route_latency.status_socket.read_status_socket_or_none` | 6 sync hand-rolls (3 without deadline or cap) + 4 async | move to `platform/`, forbid `recv(` loops elsewhere |
| JSON numeric guards | `jasper/json_fields.JsonFields` | 25 hand-rolled `_finite_*`; `_finite_or_none` ×4 and `_refuse` byte-identical across two crossover modules | review rule |
| Canonical-JSON fingerprint | `audio_measurement.evidence_identity.json_fingerprint` (25 importers) | 12 sites in `active_speaker/` with **three semantically different** implementations (`default=str` vs strict), 8 `[0-9a-f]{64}` regexes tree-wide, 5 `sha256_file` | one helper; delete the rest |
| `_utc_now()` | none | 22 copies in `active_speaker/`, 17+ elsewhere, 3 incompatible timestamp formats; two modules import a sibling's *private* copy | one function in `platform/` |
| dB math | `audio_measurement/analysis.py` | 39 inline `20*log10` with 5 different floors; 5 `dbfs()` converters; 3 band-power means; 5 filter-magnitude forks (3 unguarded by the parity fixture; `bass_extension` uses an analog prototype and disagrees near Nyquist by construction) | extend `scripts/check-peq-parity.mjs` |
| Path constants | none — 136 distinct `/var/lib/jasper*` literals over 255 Python sites + 89 shell/systemd | `state_paths.py` claims ownership and holds 3 of 9; `/var/lib/jasper/build.txt` in 5 modules + install.sh + `_lib.sh` | `jasper/platform/paths.py`; the cross-language pins stay |
| Sample rate 48000 | **none** | ≈20 declarations across Python/Rust/C/ALSA conf/CamillaDSP template; `outputd/shm_ring_source.rs:159` hardcodes it two files from `types.rs::SAMPLE_RATE`; `JASPER_FANIN_SAMPLE_RATE` is a knob that can only break the box | `pub const RATE_HZ` in `jasper-ring/layout.rs` re-exported like `RING_SLOT_FRAMES`; one row in `test_ring_slot_ceiling_pin.py` |
| `n_slots 2..=16` | `jasper-ring/layout.rs` | 7 sites; MAX pinned across 3, MIN nowhere | `pub use` in both daemons; one Python pair |
| CamillaDSP `devices:` block | values: `ensure_volume_limit_db` (one owner) | the *text* is retyped 7 times with two formatters of the same field | `render_devices_block(ActiveEmitDevices)` |
| Rust daemon skeleton | none | `json_string`, `push_kv_*` (×2 + tts-protocol's `push_json_*`), hand-rolled `sd_notify` vs the `sd-notify` crate, `EXIT_CONFIG`/`lock_memory`/`HELPER_STACK_BYTES`, two UDS servers each missing the other's defence, ~180 LOC TTS server shell already drifted, `env_u32` diverging (fanin defaults on 0, outputd bails) | #4085 item 3; a `jasper-daemon` crate **and a Cargo workspace** (8 crates, 8 lockfiles, 8 CI steps, the crate list spelled in 5 places, `jasper-clock` compiled four times on the Pi; the assumed blocker — divergent `[profile.release]` — does not exist) |
| Wizard boilerplate | `web/_common.py` | no primitive bypasses (came back clean) — but 19× the same handler scaffold; `send_rejected_form` (#4075) adopted on 2 of 25 pages | Phase B of #4031, not Phase D |
| JS helpers | `deploy/assets/shared/js/` | three clipboard-copy implementations (the shared one is the weakest); duplicate `renderSection`; three poll loops that never adopted `startPolling`; a bespoke `el()` DOM builder that evades the `h`/`svg` anti-duplication test; `correction/js/main.js`'s AudioWorklet drifted from `measurement-audio.js` (missing gap counters, different int16 rounding) | widen the existing convention tests |

### 3.4 Configuration knobs

Of 829 real `JASPER_*` tokens, 227 are live (read and written), **289 are read with a default and
written by nothing**, 190 are set only by tests, 42 have no consumer. `tests/test_env_vars_codified.py`
passes vacuously because a prose mention counts as codification, and its docstring cites an AGENTS.md
rule deleted in the doctrine reset. Verified deletions (the skeptic corrected the cartography):

| Knob / axis | Verdict | LOC out |
|---|---|---|
| `JASPER_WAKE_LEG_{DTLN,CHIP_AEC,…}` "hard-seeded 0" | **refuted** — set by `audio_profile_state.py:209` and toggled live via `POST /aec/leg`; keep, label as expert controls | 0 |
| `JASPER_TTS_TRANSPORT` | one legal value | ~40 (PR #4105 in flight) |
| `JASPER_DUCK_TRANSPORT=camilla` + `Ducker` | reachable in theory (bonded followers), no writer emits it | 87 + 387 test |
| 10 dead wizard `main()`s + `jasper-web`/`jasper-sound-web` console scripts + 7 `*_WEB_HOST` | every page is served by `python -m jasper.web`; only `tests/test_console_scripts_import.py` keeps them | ~330 |
| `JASPER_RAMP_*` | 13 are real (shipped as empty assignments); 2 dead | 4 |
| 30 `JASPER_AEC3_*` | already a typed `_KnobSpec` registry — declare it a lab pack, do not delete | 0 |
| `JASPER_FANIN_SAMPLE_RATE`, `JASPER_OUTPUTD_CONTENT_BRIDGE=direct` (has a stated expiry), six volume knobs nobody sets, 4 calibration-agent knobs with zero references, 5 `active_speaker` knobs set only by tests | delete | small each |

**Replacement contract** for the codified-env test: every `JASPER_*` read in `jasper/` must have a
writer in {`install.sh`, `deploy/lib/install/`, `deploy/bin/`, a unit `Environment=`, a wizard, a
reconciler}, or match a declared pack pattern (`_PATH|_FILE|_DIR|_SOCKET$` seams; the `aec_sweep`
registry; `_WEB_(HOST|PORT)$`), or sit in an explicit constant-not-a-knob list with a one-line reason.
Measured allowlist: ~180 entries (2.6× today's 69) — the honest cost of a guard that means something;
two-sided so it can only shrink.

### 3.5 The tuning zone

`jasper/active_speaker/` (176 files, 117k LOC, 437 classes — 3.5× the next package), plus
`audio_measurement/` and `correction/`, is 41% of `jasper/` and the previous audit never read it. Six
structural agents read it this time. Verdict: **not one system — the root is a flat bag of five to
seven concerns whose hierarchy is already spelled into the filenames** (`commissioning_*` ×14,
`crossover_*` ×8, `driver_*` ×6, `measurement_*` ×5, plus a near-homonym `commission_*` ×3);
`crossover_v2/` already proves the subpackage shape works. Each tile proposed a layout from existing
names; reconciled: `declaration/ graph(emit)/ commissioning/ measurement/ linearization/ safety/
session/ web/ bench/`, with `wizard_client.py` moving out.

What the wave-9 program has and has not done, against its own `REFACTOR-CUTOVER-2026-08.md`:

- Landed: `crossover_v2_flow.py` 9,228 → 4,636; §6.2's FOLD ruling (`record_store._ROUTES` on one
  bank); `EngineSeams` gained no sixth seam; the delta probe and `verify_absolute_tolerance_db`.
- Stalled: the class (§3.2); §6.1 untouched; §5's W5-a/W5-b joins (two engine construction sites,
  two session systems in one request); the plan document itself is stale in a load-bearing place
  (cites `EngineSeams` as five fields incl. `recommend`; HEAD has four in a 133-line file) and
  declares itself "VERIFIED-COMPLETE" while sibling ADR-0228 retired its other half — retire it into an
  ADR too (ruling 13).
- Dead and verified by the deletion skeptic: the `crossover_v2_flow` barrel (134 re-exports: 26 with
  a production importer, 93 test-only, 15 unread; cutover deletion step 1); 143 of 207 lazy doors in
  `active_speaker/__init__` with zero readers; `program_analysis/__init__.py` publishing 32 private
  names (~20 with no production consumer, three imported *cross-package* by
  `harmonic_evidence.py:803`); the audio-lab aplay tone backend (~330 LOC + `audio_lab.py` + two knobs
  + ~600 lines of a 1,231-line test file — `tone_backend_status` hard-codes `audio_enabled = False`);
  the level-match methods on `correction/session.py` plus their refusal copy (288 LOC — `level_match.py`
  itself is live through the crossover backend and is merely misfiled); `delay_graph.
  DelayCandidateConfirmation`; `frequency_view.build_frequency_view`; `calibration.supported_model_
  options` (its docstring names a caller that rebuilds it inline). Refuted on re-read: `quality_model.
  ROOM/DRIVER/RAMP` are three distinct, imported instances, not one object.
- Owner decision the zone's shape depends on: `commissioning_apply.py:888-905` carries a
  `KNOWN DEFECT (issue #2202)` saying the v1 commissioning apply *cannot succeed on real hardware*; it
  terminates a 21,667-LOC chain still wired via `web/correction_crossover_backend.py:1487`, and it is
  a second durable apply door contradicting master-plan invariant 5 (`handle_v2_apply` is "the only
  path"). ADR-0228 row 9 records "repair, not abandon" with both premises no longer holding; PR #3836
  awaits re-ruling. Decide once; the deletion or the repair is the largest single move in the repo.
- Cheap and mechanical inside the zone: 12 `_utc_now` copies, 12 fingerprint sites (three
  semantics), 24 local numeric validators beside an exported `_common.finite_float`, `_strict_object`
  ×5, 9 spellings of the evidence path layout, six-fold `camilla_yaml` emitter prologue and five-fold
  write tail, 16 `public = _private` aliases, 28 copies of the "rebuild the result envelope per failure
  branch" idiom (~600 lines), six sites hand-forwarding the same 7 `ActiveEmitDevices` fields behind a
  15–25-line essay each (one `emit_kwargs()` makes the defect unrepresentable).
- The `crossover_v2` package's import-cost charter is false: `contracts.py:25` imports a 3-field
  dataclass from `branch_chain.py`, which imports numpy at line 22; `refusal_copy.py:30-32` pulls
  `spatial`/`capture_dispatch` (scipy) for 15 string constants — measured at 967 modules / 1.14 s /
  ~105 MB RSS on the first crossover request, inside the one unit with no memory ceiling. Two
  five-line fixes (declare the codes in `contracts.py`'s S12 block; move `CrossoverSection` to a leaf).
- One correctness hole with no pin: `staging.py:1893` logs a metadata-write failure and still returns
  `status: "staged"` while the reader answers `not_staged`.

`bass_extension/` (ADR-0018 parked): the park's boundary is narrower than the ADR implies, and
narrower than the tiles claimed. Unreachable from any shipping root: `limiter_evidence.py`,
`bench/{executor,stimulus,live_proof,cross_check,analysis}.py` and the `__init__` apply/bypass/recover
*functions* — ~4,000 product LOC with ~3,900 test LOC (`bench/executor.py` is reached only by its
tests; the CLI `raise SystemExit`s naming #1738 first). Live and to keep: `profile/targets/alignment`,
the adapters, `bench/{render,derivation,manifest,activation,context}`, and the `__init__` path
constants (`BASS_EXTENSION_APPLY_INTENT_PATH` has six production importers). Only `bench/excitation.py`
(85) is truly importer-free, and ADR-0018 §3 names it as "wire or delete when bass resumes". ADR-0018
says an orphan sweep is not authority to delete; it is authority to state that ~7,900 lines are buying
a plan that a git tag and the ADR would preserve equally well. Owner decision.

---

## 4. Systems — resilience and observability

**Publish topology.** Thirteen producers, **nine mechanisms** (two UDS JSON protocols, two UDS line
protocols, a websocket, `busctl` per request, in-process singletons lost on restart with no marker,
env/JSON files under `/var/lib` and `/run` whose `observed_at` no reader computes an age for, journal
scraping with 2×`nmcli` + `journalctl -n 200` per uncached `/state` build, read-only SQLite). Only 5
of 25 `/state` sections carry any freshness marker. `/state` has one builder and no owner: no schema,
no version, no validation; `/system/snapshot` is a second builder for overlapping keys;
`audio_graph.fanin/outputd` re-project probes the same response already carries verbatim (−25 KB per
response). Recommendation: one mechanism per producer class (long-lived daemon: UDS STATUS through
`platform/status_socket`; oneshot reconciler: a JSON file with `observed_at` and a reader that
publishes `age_s`; Rust: what they do today), a `schema_version` and a top-level key-set test, and
per-section freshness.

**Event discipline.** 1,309 distinct event names with no registry; 45 flat names in `correction/`;
state transitions with no event at all: the hardware classifier (`reconcile.degraded` is write-only),
outputd going silent (`ENODEV`/`EIO` surface as a bare `Error:` and `main.rs:144`'s `notify_systemd(…)?`
masks the real exit code), the new tuning engine (zero `event=` lines), the live autolevel ramp
(12 printf lines; the *dead* ramp has structured events), daemon start/stop for voice/mux/control,
the NN-1 fader clamp (prose), the watchdog progress-stall state (prose, reaches neither `/state` nor
doctor), the deep-quiet volume reconcile refusal (silent). Cheapest fix per package: an `EVENTS:
frozenset[str]` with an AST membership test; `scripts/journal-review.sh` already treats `event=` as a
vocabulary and is invoked by nothing — put it on a weekly timer.

**The cue manager is unobservable.** `AudioCueManager.play` — the NN-6 mechanism — has zero
structured events, zero `/state` fields, zero doctor checks: "the speaker fell silent when it should
have cued" cannot be seen. And the "unknown deafness" class (mic streaming silence, model degraded,
threshold drifted after an AEC change) has no detector anywhere: the wake-events store holds exactly
that fact and no live surface reads it. Two cheap additions: `log_event` on every `play` branch + a
`cues` block in `/state`; one doctor check + `/state.voice.last_wake_at` from the row the store already
writes.

**Doctor.** 172 registered checks; 87 cannot reach `fail` (two security-posture regressions top out at
`warn`, so they never move the exit code `install.sh:2173` gates on). The typed contract
(`doctor_contract.py`, 97 lines) is right. Rule 4 (evidence once per run) is not held:
`_parked_follower_result` is called by 14 checks each re-reading the multiroom config while the memo
for exactly that fact has 2 users; 8 more `load_config()` calls in `grouping.py`. Rule 5 (`--core`) is
unshipped and `jasper-deploy-health` (900 LOC + 1,642 test) remains the deploy gate. Three facts are
computed by both doctor and `/state` (combo-armed predicate, wifi-guardian stash-vs-active, chat store
health). The `voice` module is omitted on streambox although ADR-0217 stages `jasper-voice` there.
`render()` prints all 172 rows with no filter; the dashboard renders them in one table. Push back on
#4127 (it omits two crossover checks on streambox to dodge a 15 s stall the full profile then pays
twice; memoizing the reader fixes both).

**Steady-state cost (measured, laptop-relative).** An idle full-profile speaker with no browser open
forks ~155–210 processes per minute; 120–180 of them are `VolumeObserver._run` in `jasper-voice`,
which forks 2–3 processes per second forever, ungated by active source (one reads a value its own
docstring calls "diagnostics only — never dispatched"); #4125 fixes half. `/sources/` adds 15 forks +
30 D-Bus session connects per minute per open tab; the landing page's `/volume` poll at 500 ms builds a
new event loop per request (`handlers/volume.py:21`; 19 more `asyncio.run(` in `control/`). `/state`
itself is on-demand only (nothing in `deploy/assets/` fetches it — the wizards poll their own routes),
so its 3 subprocesses per build are a doctor-run cost, not a dashboard cost. ADR-0226 was applied to
the unit files and stopped there: every `ExecCondition=` is POSIX shell, the guards are bash, mux
defers its fork-backed probes — but `heal_shared_state_modes` still spawns `/usr/bin/python3` from 12
install call sites, `chip_aec/health.py` → `alignment` → `audio_measurement.ramp` pulls `asyncio` +
`ssl` (83 ms / 186 modules) into every reconciler shim to import a verdict table, and `jasper.identity`
drags the peering state machine into six processes (10.7 ms on x86 for one path constant).

**Memory.** Fourteen units declare `MemoryMax` summing to 1,464 MB — 3.5× the Zero 2 W. The OOM
ladder (`_oom_adj.py`) protects the audio chain in a defensible order and has **no bottom rung**: every
`jasper-*-web` unit sits at the default 0, tied with `udevd`, `dbus` and `logind` — the processes
ADR-0226's incident names as killed — and `jasper-correction-web` has neither `MemoryMax` nor
`OOMScoreAdjust` while being the one unit that pays scipy: `crossover_v2/refusal_copy.py:30-32`, a
"pure copy" module, costs **967 modules / 1.14 s / ~105 MB RSS** for 15 string constants (T13-1's
finding, re-graded to Blocker by measurement). Only three daemons import numpy at module scope, all via
`audio_io.py:19`. `jasper-web` (14 wizards, one interpreter, 43 MB, socket-activated) is the model
ADR-0225 asks for. On-device build: the low-memory threshold is 1.2 GB, so a 1 GB Pi 5 *and* the Zero
2 W both ship fan-in/outputd at `opt-level=0` (#4137 raises it to 2 — good; `lto="fat"` is dead on the
owner's hardware); `jasper-clock` compiles four times per deploy without a workspace; the prebuilt
ARM64 bundle (~1,700 LOC of installer + test + workflow) is wired to nothing on the deploy path. The
Pi-side copy of 11 of 12 `deploy/lib/install/*.sh` (5,565 lines) has no consumer (`install.sh` sources
all twelve from the checkout; only `build-sandbox.sh` is read from `/usr/local/lib`) — a one-line glob fix.

**Astronaut engineering, verified deletable:** `host_clock.rs`'s `catch_unwind` (dead under
`panic = "abort"`); the `sdnotify` dependency + its ImportError branch (fails closed in the worst
direction); a udev rule line that can never match (`05ac/…` — udev formats `PRODUCT` with `%x`);
`deploy/avahi/jasper-control.service` static fallback; `bluetooth/roles.py` (`bt_roles.json` is
write-only); the Bluetooth handler "plugin framework" (224 LOC of Protocol + registry for three
one-`yield` bodies); the `first-party-runtime.sh` 581-line two-phase-commit journal (its activating
env var is absent from the deploy-to-pi forwarding list, so it is unreachable via the only sanctioned
path); `jasper-host-clock`'s `Dll` is constructed and never ticked (`dll_err_frames()` is constant
`0.0` and `dll_locked()` constant `false`, published to `/state` as live diagnostics; no test pins
them) — delete the two fields and fold the crate into its one consumer.

**Resilience wins to keep exactly as they are:** the aec-bridge mic-vanish ladder (verified end to
end incl. the reconciler stopping and disabling the bridge before the reboot fuse); outputd's
`try_lock`-only playout thread; fan-in's `sync_channel` tap; the DAC hotplug recovery (udev →
reconciler → un-park, plus a USB-remove belt); the camilla statefile fallback that converges with the
daemon down; `commissioning_run.py`; the duck's 30 s idle-release TTL that un-ducks a dead voice daemon.

---

## 5. Tests

19,461 test functions, 585k LOC vs 424k product. The suite is honest (the skeptics kept refuting
"delete this test" rather than "this test is theater") and its size is scenario breadth, not
copy-paste. The debt is *altitude* and *aim*:

- **1,645 patches of private names across 191 files.** `test_mux.py` drives the arbiter through 410
  private-attribute reaches instead of its UDS protocol. Every Rust consolidation edits
  `test_rust_runtime_panic_freedom.py` (#4093 landed the marker — good; keep going).
- **209 files read repo source as text.** The nine opened were all legitimate structured contracts
  (unit directives, forbidden-command AST scans, argv pins). The violations are narrower: `inspect.
  getsource` pins in `test_correction_setup.py:419` (forced by the closure), `test_wire_contracts.py:513`
  (a literal line of `mux.py`), four in the active-speaker zone, `test_wifi_setup_ui.py` (JS
  source-text substrings — the page's *only* test), and prose pins like `assert "ADR-0100" in detail`.
  Rust: `config.rs:620-626` carries a comment *forbidding production code from using a constant*
  because a Python test scrapes the literals out of the file.
- **Guards that measure the wrong thing:** `test_env_vars_codified.py` (§3.4); `test_atomic_io_
  conventions.py` (keys on `mkstemp`, so the less-safe pattern escapes); `test_managed_units_cover_
  every_routed_client_unit` (a hand-written set — exactly how `jasper-usbsink-volume` slipped); the
  regression-scenario guard sees only `@tool`-decorated tools and misses three; `test_ring_slot_
  ceiling_pin.py:89-98` carries a live `xfail` for a constant that exists.
- **Non-negotiable coverage:** seven of eight have exemplary heavy tests. NN-3's mechanism
  (`redact_secrets`) has **none**. The denial paths of every privileged action are untested — the
  `fake_popen` returns an object with no `returncode` attribute, proof by construction that production
  never reads it.
- **Cheap consolidations (~1,140 LOC):** parametrization clusters (`test_baseline_reemit_*` ×17,
  `test_detect_echo_*` ×12, …), single-use helper files, duplicated wizard boilerplate across 10
  files, `tests/test_restart_broker.py`'s 13 tests of a test-only helper (~450 lines), the
  `crossover_v2_flow`/`program_analysis` barrels' test-only exports.
- **CI:** the pytest job's 30 → 45 min bump cites an unresolved "late-run descriptor exhaustion" with
  no issue and no expiry; `mypy` runs twice per matrix run; `[tool.mypy]` sets neither
  `check_untyped_defs` nor `disallow_untyped_defs`, so `audio_measurement/`'s analysis core (2/11
  functions annotated in `deconv.py`) type-checks as `Any` whether or not it is on the 86-module
  baseline; the renames→full-lane rule skips the required docs link check.

---

## 6. Docs and prose

The corpus is governed (doc-map CI-enforced, root docs verified claim by claim, zero real TODO markers
in code) and the previous audit's prose sweep held outside the tuning zone. Remaining:

- **ADRs:** 157, no index, nothing references `docs/adr/` programmatically; 79 dated one day; three
  batch-ADRs (0227, 0228, 0231) bundle 29 decisions against one-decision-per-file; six live docs still
  link the deleted HANDOFF corpus. Add a generated index; stop batching.
- **Plans that outlived themselves:** `REFACTOR-CUTOVER-2026-08.md` (1,694 lines, self-declared
  complete, one load-bearing stale claim); `multiroom-pairing-reliability-plan.md` (1,230 lines,
  "rescued plan, not executed", 235 commits behind); `install-hardware-tier-and-staleness.md`
  (self-declares "not ongoing operational truth", pointed at three times from `install.sh`);
  `PROMPT-subwoofer-deletion.md` (fully landed, duplicates ADR-0236). The no-orphan-doc test globs only
  top-level `docs/*.md`, so `docs/bass-extension-waves/` (16 files) and `docs/ux-audit-2026-09-03/`
  (9) are structurally invisible to it. `docs/DEEP-AUDIT-2026-08-25.md` is stale on host_compliance
  (deleted the next day).
- **Comments:** 34–43% of lines in the voice, DSP-control, deploy-unit and install tiles; the worst
  are essays over one-line facts (`ring-platform.sh:391-459`, 69 lines above five `rm -f`;
  `jts-ring.conf`, 89 comment lines for one `d` line — but `60-jts-ring.conf` earns every paragraph).
  Two hundred and seven history/PR/date narrations in the active-speaker root alone. Comments that are
  factually backwards at HEAD: `openai_session.py:801-804`, `outputd/config.rs:230-234,246-247`,
  `control/handlers/system.py:428` ("runs as root"), `sound/settings.py:148-150`,
  `graph_carrier.py:713-719` ("no production caller" — two callers), `uds.py:22` ("used by doctor").
  Missing module docstrings: `voice_daemon.py`, `audio_io.py`. `prompt.py:8-38` is dated eval history
  pointing at a CLAUDE.md section that no longer exists.
- **Units:** 2,361 narrative lines vs 8 `ADR-NNNN` pointers across 4,215 unit lines; no unit names
  the ADR behind its `StartLimitAction=reboot`.

---

## 7. The program — sequenced, owned, guarded

Every wave keeps CI green and features identical unless marked as an owner decision. PR size per
AGENTS.md; one concern per PR. Territories as in #4085.

**Wave 0 — welding (this week; ~20 PRs, most < 60 lines).** §2.1's four blockers first (R-001 PSK
quoting; R-002 the attended-sudo guards; R-003 broker routing + the derived allowlist pin; R-004 the
peering state machine, before peering is turned on), then §2.2 in order, plus the small items at the end
of §2.2. Each lands with one behavior pin. Owners: S for 001–006, 008, 012, 014–017, 021; D for 009, 011,
013 and 016's doctor row; H for 010, 019; W for 015, 018; T for 020.

**Wave 1 — the guards that measure the right thing (~8 PRs).** Derived unit-allowlist test; the
`redact_secrets` parametrized pin; `atomic_io` ratchet keyed on rename; the env-contract replacement
(§3.4); the regression-scenario guard enumerated from the registry; `EVENTS` frozensets per package;
`log_event` conventions extended to `print`; the three narrow import-linter contracts merged as
ratchets (`platform-is-a-leaf`, `contracts-are-leaves`, `surfaces-are-leaves`) in `scripts/test-merge`.

**Wave 2 — verified deletions (~2,300 product + ~1,600 test LOC confirmed by the deletion skeptic;
one PR per row, each body pasting the negative proof).** Peering's mDNS/STATUS/PING half (373 + 485
test); nine wizard `main()`s and the `jasper-web`/`jasper-sound-web` console scripts (334); the
`crossover_v2_flow` barrel (14 now, +87 after redirecting tests) and the 143 dead lazy doors;
`program_analysis` private exports (46, after test redirect); `audio_hardware/__init__` (80); the
audio-lab tone backend (330 + ~600 test); the session-level level-match methods + refusal copy (288 +
180 test); `bluetooth/roles.py` + the test-only volume/mux paths that leave fan-in's Rust `AUTO` verb
with no producer (131 + ~15 Rust + 120 test); `CLEAR_CONFIGURATION`; dead ring/resampler/host-clock
symbols (233); the crossover_v2 barrels and four dead `InterventionProposal` fields (67); `HAClient.
list_agents` + the dead CSRF helper (54); the astronaut list in §4; `Ducker` + `JASPER_DUCK_TRANSPORT`
(87 + 387 test); the Pi-side install-lib copies (a one-line glob). Owner decisions priced separately:
the `bass_extension` park (~4,000 + ~3,900 test), the v1 commissioning chain (§3.5), `jasper-deploy-
health` (900 + 1,642 test, once `--core` exists), `s0-sync-*` vs `multiroom-spike-*` (2,150 LOC, both
self-declared throwaway, 4 test files pinning their `--help`). Refuted deletions the next round must
not re-file: `level_match.py` itself, `bass_extension/__init__.py`'s constants, `HAClient.healthcheck`/
`.config`, `ring_stall_verdict`, `GOOGLE_ROUTES_SECRET_FILE`, `accounts`↔`google_creds` (converge, do
not delete), the 345 file-local public defs (a visibility fix worth 0 LOC).

**Wave 3 — relocation without behavior change (~12 PRs, `git mv` + import rewrite).** The seven
misfiled modules (§3.1); `platform/` (the 20 primitives + `status_socket`); `net/`; `wake/`; `aec/`;
C1 and the five constant moves C2/C5/C6/C7/C8; the Cargo workspace + `jasper-daemon` crate;
`jasper-host-clock` into fan-in. Guard: the layers contract adopted L1–L3 first, then ratcheted.

**Wave 4 — the splits (~10 PRs, each single-concern, each leaving the file smaller).** `WakeLoop`'s
three seams and its test doubles; `daemon_main.run` → `Services`; `correction_setup` (add it to
#4031 Phase D); `sound_setup` routes table + backend move to `web_commissioning`; `runtime_contract`
→ `types` + `queries`; `multiroom/reconcile.main` → `RoleDecision`; `install.sh` STEPS table (+ the
rollback transaction for both profiles); `snapshot_json` / `from_env`; `ring_assets` on its own three
concerns; `coupling_reconcile.reconcile_auto`'s combo half into `coupling_auto`.

**Wave 5 — one home per primitive (opportunistic, when a file is open; ~20 small PRs).** The §3.3
table, cheapest rows first: `_utc_now`, fingerprints, `json_fields`, STATUS readers, env parsers,
the sample-rate constant, the `devices:` renderer, the Rust skeleton, the JS helpers.

**Wave 6 — systems (D territory, ~10 PRs).** `/state` schema + freshness + one mechanism per class;
cue-manager instrumentation; wake-recency detector; `speaker_silent=True` on daemon-dead doctor
branches; doctor rule 4 memoization; `--core` as the deploy gate; restart-policy matrix (park-with-
record for fanin/outputd); the `VolumeObserver` gate + `supervisor_runtime` loop (−100–150 forks/min);
`MemoryMax` + a positive `OOMScoreAdjust` for every wizard unit; `refusal_copy`/`contracts` made
numpy-free; `usage.db` index and retention; `wake_events` row retention; correction bundle retention;
one long-lived loop for the control handlers instead of `asyncio.run` per request.

**Standing rules to stop regrowth** (the cheapest items in this report): the deferred-import rule
(§3.1) in AGENTS.md; the layers contract in CI; every new `JASPER_*` needs a writer or a pack; every
new helper's PR migrates at least its own siblings; no batch-ADRs; every `StartLimitAction=reboot`
names its ADR; every guard names its removal condition (14 installer one-shots and
`RECONCILE_DUCK_SKIP_DB` carry none today).

---

## 8. Owner decisions (priced, not scheduled)

1. **v1 commissioning apply** (#2202, ADR-0228 row 9, PR #3836): repair or delete a 21,667-LOC chain
   whose terminus says it cannot succeed on hardware. The zone's shape depends on this.
2. **`bass_extension` parked half**: ~3,800 product + ~8,000 test LOC unreachable from any shipping
   root; ADR-0018 forbids deletion on orphan grounds. Tag + ADR preserves the plan equally well.
3. **`sound_setup` ↔ `web_commissioning`** (owner call 1 in #4085): 2,784 lines of already-divergent
   orchestration across the T/W territory line. Recommendation: `web_commissioning` owns; #4031 D.2
   becomes a routes-table PR.
4. **Peering**: it ships installed-but-off; R-004 makes its election wrong when on. Fix it (join the
   in-flight CANDIDATE, `StandDown` on every terminal path, one timeout constant) or delete the dead
   half and keep only the advert, and say which in PLAN.md.
5. **Cargo workspace**: recommended; the only real caveat is keeping `-p` separation for the Zero-class
   low-RAM build path.
6. **Streambox volume ownership** (R-007): move restore/reconcile into `jasper-control`, or document
   that a mic-less streambox has no volume restore.
7. **`caplog.text` migration** (91 files) and the private-name patch population: opportunistic, but
   the two worst files (`test_mux.py`, `test_control_server.py`) are worth a dedicated PR each.
8. **`experiments/usb-turntable`**: production code under `experiments/`, driven deliberately as a
   subprocess ("never an import"). Rename the directory out of `experiments/`; do not promote.

---

## 9. What only hardware/runtime can prove

Whether the udev-triggered bash reconciler ever actually lands inside `coupling_reconcile.py`'s
read→write window (the shape is proven, a collision is not); whether the reconciler's marker removal
reliably beats four aec-bridge restarts; the 1 Hz mute storm at 1 % on a real box (one `journalctl -u
jasper-control | grep volume.reconciled`); whether an XVF3800 with capture channels muted presents as
frames-of-silence rather than frames-stopping; the 120 s deafness window (needs `MEASURE_PAUSE` opened
inside the acquire window on metal); the peering race timings at non-default arbitration windows; whether a CamillaDSP statefile volume wins over
`speaker_volume.json` after a restart; the real `/state` byte size and build time under memory
pressure; journald rate-limit drops; whether a cue is audible when outputd is dead; every latency,
AEC-convergence and wake-rate number; the push-target handoff transient against a slow Spotify
round trip; the `modprobe.d` options that never apply because `rmmod snd_aloop` is masked.

---

## 10. Method, coverage, and what was refuted

**Coverage.** 573,044 lines of product code were tiled into 38 named agents; each reported opening
every file in its tile (files over 1,500 lines at structural altitude: imports, all signatures,
every function over 40 lines, and everything touching persistence, subprocess, threads, sockets or
exceptions). Tests (585k lines) were swept mechanically and sampled; docs (78k lines) were swept
mechanically with 8 ADRs and every root doc spot-checked claim by claim. Phase 0 measured 100% of the
tree mechanically (AST complexity, import graph, env ledger, orphan graph, duplicate scan). Known
gaps: `scripts/test-fast`, `test-merge`, `use`, `jasper-pipe-probe`, `rust-ci-needed` fell in no tile's
file list (T27 read the first three); `docs/historical/` and `docs/research/` were counted, not read;
Rust and C comment ratios were not computed; the `p3` skeptics covered the Blockers, the deletions
over 100 LOC, and the Phase 2 seam findings — Should-fix items below that line carry the tile's
evidence only.

**Premises this review corrected** (so the next round does not inherit them): the "34-module import
cycle" is a resolver artifact — the executed graph has one 4-module cycle; `correction_crossover_v2.py`
has no `_make_handler` (it is an engine at a web address); there are 55, not 35, `_active_speaker_*`
functions in `sound_setup`; the fan-in `host_compliance` machinery was deleted the day after the last
audit (that report is stale on it); `jasper-wake-corpus-web` and `jasper-aec-sweep-config` are
documented operator tools, not orphans; the `JASPER_WAKE_LEG_*` legs are live expert controls, not
dead branches; `wake_model.env`'s two writers are both locked; `jasper-apply-airplay-mode` is not an
`outputd.env` writer; the previous audit's "96 systemctl sites" was 74 argv sites, ~36 mutating; the
CI classifier is 354 + 583 lines, not 545 + 1,087; `jasper-clock` and `jasper-host-clock` are not
twins (the latter depends on the former); `attribution/` cannot fold into one consumer (it has two);
jasper-control's `/state` is not polled by any page (the wizards poll their own routes), so its
per-build subprocesses are a doctor-run cost; the wake-events WAV cap is 128 MiB, not 1 GiB (the
unbounded rows half stands); the correction bundle-root scan is gated to idle screens (10 s), not
900 ms; `lto="fat"` is on two crates, not three.

**Adversarial verification.** Three skeptic agents re-read 55 sub-claims (22 tile blockers → 33
sub-claims; 20 deletion claims; 14 Phase 2 seam items): 14 confirmed at the filed grade, 24 downgraded,
12 refuted, 3 upgraded, plus five new findings from the re-read (§2). Three claims were settled by
execution rather than reading: the attended-sudo deploy bypass (stubbed `ssh`/`rsync` under a pty), the
peering state machine (driven directly), and the 1 % mute predicates (evaluated numerically). The
reviewer personally reproduced R-001 (three code points), R-014 (ran the redactor), R-002's flag branch
(read the guard block), and R-003 (computed the allowlist difference), and read the NN-1, deploy and
control tile reports in full. Every verdict is in `reports/p3-*.md`; the register carries a `verify`
column naming which rows were re-read.
