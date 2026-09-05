# L2 — RESILIENCE (tree-wide, long-running daemons) · SHA 2d571e6b8

## A. Verdict

The **per-call** layer is in good shape: 24 of 25 `socket.socket()` sites arm a timeout, all 14
`create_subprocess_exec` sites are wrapped in `asyncio.timeout`/`wait_for`, only **2** `create_task`
sites tree-wide lack a strong reference, all **27** `threading.Thread(...)` pass `daemon=`, and all 8
bare `proc.wait()` calls are post-`kill()` reaps. The failures live one level up, at seams no tile
owned: (1) **the restart-policy matrix is inverted** — the two units with the tightest reboot ladder
(`jasper-control`, `jasper-aec-bridge`: burst 4 × `RestartSec=2` = **8 s from first fault to
`StartLimitAction=reboot`**) are the only two with no config-error park, while `jasper-fanin`,
`jasper-outputd` and `jasper-voice` all map permanent faults to exit 78/66 and `RestartPreventExitStatus`;
a bad env key or an unplugged XVF mic therefore reboots the Pi three times before
`jasper-bootloop-guard` disarms; (2) **the same env file is read-modify-written by a bash reconciler
and a Python reconciler with a lock only one of them takes**; (3) **steady-state cost is where the
1 GB budget actually goes** — `VolumeObserver` forks 2–3 processes *every second* forever (one of them
for a value its own docstring calls "diagnostics only, never dispatched") and `/state` forks
`nmcli`×2 + `journalctl -n 200` uncached on every 7 s dashboard poll; (4) the single transition that
ends in a reboot — the `Type=notify` progress-watchdog stall — emits no `event=` line anywhere. Six
hand-rolled `STATUS\n` socket readers exist against one canonical implementation, and three of the six
dropped its total deadline and byte cap. Growth is mostly bounded (`wake-events` WAVs capped +
doctor-watched, `rate-storms` keeps 20 files, `active_speaker` bundles prune); the real unbounded
stores are `usage.db` (no retention **and** a `strftime()`-on-column full scan on every wake) and
`correction/sessions` (warned by doctor, pruned by nobody, while its sibling `active_speaker` prunes).

## B. Scenario S3 — resource vanishes (detector → recovery → observability → gap)

| resource | detector (file:fn) | recovery action | observable as | gap |
|---|---|---|---|---|
| **USB DAC** | kernel `ENODEV` at `rust/jasper-outputd/src/alsa_backend.rs:1638` (else-arm of the xrun match); udev `deploy/udev/99-jasper-audio-hardware-reconcile.rules:1` → `jasper-audio-hardware-reconcile.service` | daemon exits 1 → `Restart=on-failure`; `ExecCondition` (`jasper-outputd.service:106`) tests `/proc/asound/$card` and **parks inactive** while absent; `ExecStopPost=jasper-outputd-failure-reconcile` (`:113`) bounded retry | `event=outputd.output_device_gate.park` from ExecCondition; bash `sed`-diff of the env file | **the daemon's own moment of going silent prints only a bare `Error: writing outputd DAC PCM …`** (no `event=`, T20 F4 confirmed). A DAC *present but failing* (EIO, flaky cable) passes ExecCondition → 5 restarts in 25 s → **reboot**. `output_hardware.probe_aplay_listing:661` runs `aplay -L` with **no timeout** on exactly the vanished path (T06 F4 confirmed) |
| **XVF mic** | `cli/aec_bridge.py:1167 validate_mic_device` → `MicDeviceUnavailable`; udev `99-jasper-aec-reconcile.rules:1` → `deploy/bin/jasper-aec-reconcile`; `jasper-voice.service:69 ConditionPathExists=!/var/lib/jasper/voice-input-absent` | reconciler rewrites mic env + `try-restart`; voice exits **66** and parks (`voice.service:219-220`) | `event=aec_reconcile.*`; voice's park is a clean `SuccessExitStatus` | **aec-bridge returns 1, not 78** → burst 4 in 8 s → **reboot** if udev/the reconciler has not yet written the ready marker. Voice does this correctly; the bridge next to it does not (F1) |
| **generic USB mic** | `cli/aec_bridge.py:1176 validate_usb_mic_device` → `UsbMicUnavailable`; `jasper-usbmic-apply.service` (`on-failure` 15min/4, no reboot) | same reconcile path | same | same exit-1 defect; `usbmic` units are correctly outside the reboot ladder |
| **network / WiFi** | `control/wifi_guardian_state._active_ssid:74,88` (`nmcli`, `timeout=3`); `jasper-wifi-recover.timer` `OnUnitActiveSec=3min`; `deploy/bin/jasper-wifi-guardian` | stashed-SSID re-apply; `jasper-wifi-scan-repair` | `/state.resilience.wifi_guardian`; `event=wifi_guardian.*` | the *reader* costs 2 `nmcli` + 1 `journalctl -n 200 --output=json` **per `/state` build with no cache** (F6); nothing coalesces concurrent `/state` builds |
| **CamillaDSP process** | `camilla.CamillaController._call:445` (`CAMILLA_ATTEMPT_BUDGET_S=5.0` per attempt) | `Restart=always` `RestartSec=2`, `StartLimitAction=**none**` + `OnFailure=jasper-camilla-recover.service` (forensic ALSA-holder capture, controlled restart, written park) | `event=camilla.operation_retry` (DEBUG); the recover script's park record | **the model the other units should copy.** Defect is client-side: `_call` retries once with **zero delay and no failure memory**, so a wedged (not refused) camilla makes every call ~10 s, forever (F8) |
| **fan-in ring** | `rust/jasper-fanin/src/mixer.rs:1101,2079` ring-stall detector; `ring_assets.ring_flow_state` | `RING_REATTACH_RETRY_PERIODS`; outputd's `classify_ring_attach_error` (`outputd/main.rs:187`) splits `InvalidInput/InvalidData` → exit 78 park vs everything else → restart | `event=fanin.ring_stall_*`, `event=outputd.<lane>.config_error` / `.attach_error` | classifier written twice, once per consumer (T20 F9); `warn!/info!` on the SCHED_FIFO thread (T19-1 F1) |
| **jasper-control socket** | `control/client.py:72 DEFAULT_TIMEOUT=2.0`; `restart_broker.py:608` `settimeout(timeout + _CLIENT_SOCKET_MARGIN_SEC)` | every caller fails soft | `event=restart_broker.denied` | `usbsink/volume_bridge._retry_declined:461` retries a declined POST **forever** at a 5 s ceiling (T04 F7 confirmed). A **polkit-denied** restart still answers HTTP 200 (T08 C1) |
| **LLM provider** | `voice/_supervisor.py:494 run_reconnect_with_backoff` + `backoff.py:14-16` (1 s → 60 s, ±25 % jitter) | reconnect ladder; `OutageTracker` terminal-cue escalation | `event=` + a spoken cue | the escalation cue is the tree's only untracked `create_task` (`_supervisor.py:299`, T01 F10 confirmed) — GC-able, exception swallowed. This is the branch that says "your provider is out of credit" |

## C. Restart-policy matrix — the units that can reboot the box

`StartLimitAction=reboot`, 5 units. `park?` = does a permanent config fault park instead of escalate.

| unit | Restart | Sec | Interval/Burst | fault→reboot | Watchdog | RestartPreventExitStatus | park? |
|---|---|---|---|---|---|---|---|
| jasper-control | on-failure | 2 | 300/4 | **8 s** | 30s | — | **NO** |
| jasper-aec-bridge | on-failure | 2 | 300/4 | **8 s** | 30s | — | **NO** (`ConditionPathExists` marker only, racy) |
| jasper-fanin | on-failure | 5 | 300/5 | 25 s | 30s | 78 | yes |
| jasper-outputd | on-failure | 5 | 300/5 | 25 s | 30s | 78 | yes (+ `ExecCondition` hardware gate) |
| jasper-voice | on-failure | 5 | 300/20 | 100 s | 30s | 66 78 | yes (+ `ConditionPathExists=!`) |

Guarded by `deploy/bin/jasper-bootloop-guard` (3 boots / 3600 s → runtime drop-ins forcing
`StartLimitAction=none`, self-clearing via `/run`). It earns its keep, but it is a **cross-boot** guard:
it costs the household 3 reboots (~5–10 min unreachable) before it trips. `jasper-camilla` is
deliberately outside the ladder (`StartLimitAction=none` + `OnFailure=jasper-camilla-recover`) and is
the pattern the two `park?=NO` units should adopt. No unit names its ADR.

## D. Findings — ranked by likelihood × blast radius on this hardware

| # | sev | file:line | what | evidence | cleanest fix |
|---|---|---|---|---|---|
| 1 | **Blocker** | `jasper/cli/aec_bridge.py:1077,1106,1150,1170,1176` + `deploy/systemd/jasper-aec-bridge.service:53-55` | Five **permanent** config/hardware faults return exit 1, indistinguishable from a transient `BridgeStalled`; unit escalates to `reboot` after 4 restarts × 2 s | `UnsupportedReferenceSource`, missing chip beam plan, missing `JASPER_OUTPUTD_CHIP_REF_PCM`, `MicDeviceUnavailable`, `UsbMicUnavailable` all `return 1`; `:1275,1279` return 1 for the transient class too. No `RestartPreventExitStatus=` | `return EX_CONFIG (78)` on the five permanent branches + `RestartPreventExitStatus=78` in the unit — copy `jasper-voice.service:219-220` verbatim. One constant, one unit line; test = one park pin |
| 2 | **Blocker** | `jasper/control/server.py:2178` `main()` + `deploy/systemd/jasper-control.service:18-20,57-58` | Any startup exception (port 8780 held, `build_server` `OSError`, bad env) exits 1 → 4 restarts in 8 s → **the recovery surface reboots the box** | `main()` has no try/except around `build_server(:2238)`; only `return 0` paths exist; no `RestartPreventExitStatus`, no `ExecCondition` | Wrap the startup block; `OSError` on bind → `return 78` + `RestartPreventExitStatus=78`. Also widen `StartLimitBurst`/`RestartSec` so the ladder is not the tightest on the box |
| 3 | **Blocker** | `deploy/bin/jasper-audio-hardware-reconcile:606,846` vs `jasper/fanin/coupling_reconcile.py:126,536,1741,2044` | `outputd.env`/`fanin.env` are read-modify-written by two processes; the Python side holds `/run/jasper-fanin-coupling.lock`, the udev-triggered bash side takes **no lock** — and the Python side *starts the bash unit from inside its own critical section* | bash: `cp` current file to a stage (`:606`) → mutate → spawn a Python validator (`:616-640`) → `mv` (`:846`). Window is seconds on a Zero 2 W. Python: `atomic_write_text` whole-file publish under the flock | Have the bash reconciler take the same flock (`flock -w 10 /run/jasper-fanin-coupling.lock`) around stage→publish. One `flock` line; no new machinery |
| 4 | **Should-fix** | `jasper/watchdog.py:110-127` (T18 F8 — **confirm**) | The progress-stall state — the one that ends in a systemd kill and, on the 5 reboot-ladder units, a Pi reboot — emits an unstructured `logger.warning` per tick and reaches neither `/state` nor doctor | no `log_event` import in the file; `:123` is prose, `:121` is `logger.exception` | `log_event(logger, "watchdog.progress_stalled", …)` + a `Heartbeat.stalled` field on `/state.resilience` |
| 5 | **Should-fix** | `jasper/volume_observers.py:136,169-173,282,308` (T04 — **confirm + sharpen**) | 1 Hz `while True` forks **2 processes per tick unconditionally**, ungated by the active source, forever: `busctl get-property …AirplayVolume` + `bluealsa-cli list-pcms` (+ a third `busctl` when BT is connected) ≈ 172k–260k spawns/day | `POLL_INTERVAL_SEC = 1.0` (`:80`); `asyncio.gather` at `:169` calls all three readers regardless of `current_active`; `_read_airplay_db`'s own docstring: *"Diagnostics only — this reading is logged, never dispatched"* | Delete `_read_airplay_db` outright (the canonical path is shairport's hook, ADR-0206), gate the BT pair on `current_active is Source.BLUETOOTH`, and replace `_run` with `control/supervisor_runtime.run_supervisor_loop` (it already supplies jitter + a structured crash event; the three `control/*_supervisor.py` consume it) |
| 6 | **Should-fix** | `jasper/control/wifi_guardian_state.py:74,88,116` + `state_aggregate.py:772-775` (T08 — **confirm**) | `/state` forks `nmcli`×2 + `journalctl -n 200 --output=json` on **every build, uncached**, 3 s each (9 s worst case) | `deploy/assets/rooms/js/main.js:59 POLL_MS = 7000` — ~26 spawns/min while a dashboard tab is open | TTL-cache `snapshot()` the way `_augment_source_payload` already caches (`state_aggregate.py:788-800 SOURCE_AVAILABILITY_TTL_SEC`); the journal scan needs ≤ 1/min |
| 7 | **Should-fix** | `jasper/voice/output_gate.py:89-106` (T01 F3 — **confirm, re-grade to easy**) | `begin_turn()` is `while True: … await resumed.wait()` — a wake landing in the acquire window while `MEASURE_PAUSE` opens goes silent and deaf for up to 120 s with no cue (non-negotiable 6) | the *bounded* shape is 15 lines above in the same file: `_wait_for_idle:70-79` already does `deadline = loop.time() + timeout` / `wait_for(..., remaining)` / `return False` | Give `begin_turn` the same deadline and return `None`; caller plays `internal_error`. No new constant needed beyond the existing `MEASUREMENT_PAUSE_TOTAL_TIMEOUT_SEC` |
| 8 | **Should-fix** | `jasper/camilla.py:445-478` (T05 #3 — **confirm**) | `_call` retries once with **zero delay** and no failure memory, on every call, forever. A wedged (not refused) camilla makes each call ≈ 2 × (`CAMILLA_ATTEMPT_BUDGET_S=5.0`) | the comment at `:448` records the journal flood and says the fix applied was demoting the log to DEBUG | Per-controller `last_failure` monotonic stamp; inside a ~1 s window skip the retry and short-circuit `best_effort` calls to their `None`/`False`. Timeout constant, no new test file |
| 9 | **Should-fix** | `jasper/usage.py:414,827-830` + `voice_daemon.py:1916,3512,4644` (**new**) | `usage.db` has **no retention, no index, and no VACUUM**, and `SpendCap.allowed()` full-scans `sessions` on **every wake** with `strftime('%s', started_at) >= ?` — a function on the column, so no index could ever help it | zero `DELETE`/`VACUUM`/`CREATE INDEX` in the file; one `sessions` row per session + one `connection_intervals` row per connection, forever | Store `started_at` as an epoch INTEGER (or add a generated column), index it, and prune rows older than the widest window any reader uses (`spend_month_to_date_usd`). Wake latency stops degrading with age |
| 10 | **Should-fix** | `route_latency/status_socket.py:86` vs `fanin/status.py:119`, `control/audio_health.py:247`, `control/airplay_health.py:1692`, `correction/runtime_integrity.py:102`, `audio_validation.py:508`, `cli/system_soak.py:240` (**new seam**; includes T11 #3) | **Six** hand-rolled sync `STATUS\n` readers against one canonical implementation. Three dropped the total deadline (per-`recv` timeout only) **and** the byte cap: a chatty or wedged daemon can hold a caller open indefinitely and buffer without bound on a 1 GB Pi | `status_socket.py:88-104` re-arms a *total* deadline per recv and caps at `_RESPONSE_MAX_BYTES`; `fanin/status.py` and `control/audio_health.py` are byte-identical re-derivations of that (diff = names + docstring); `airplay_health`, `runtime_integrity`, `audio_validation` have neither | Import `read_status_socket_or_none` in all six and delete ~150 LOC. The four async readers (`mux.py:603`, `renderer.py:120`, `control/uds.py:181`, `grouping_supervisor.py:452`) should likewise converge on `control/uds.read_status_body` |
| 11 | **Should-fix** | `jasper/renderer.py:112` (**new**) | `asyncio.open_unix_connection(MUX_CONTROL_SOCKET_PATH)` with **no connect timeout**, on the chain the file's own 14-line comment identifies as the hot one (`VolumeObserver._run → _tick → _active_source → here`, every tick) | the *read* below it is correctly bounded by `asyncio.timeout(1.0)`; only the connect is naked. `mux.py:591` wraps the identical call in `wait_for(..., 1.0)` | Wrap in `asyncio.timeout(1.0)` (not `wait_for` — same 3.11 reason the file already documents) |
| 12 | **Should-fix** | `jasper/wake_corpus/recording_backend.py:1404-1628` (T02 — **confirm**) | ~250 LOC of generation-token + `threading.Timer` retry for one clip write with **no attempt cap** — only the backoff is capped (`STOP_RETRY_MAX_SEC=1.0`), so a permanently-failing stop rearms at 1 Hz forever inside jasper-voice | `_stop_retry_attempts` feeds the exponent, never a give-up | Bounded blocking acquire + capped attempt count + one audible/`event=` give-up |
| 13 | **Should-fix** | `jasper/usbsink/volume_bridge.py:461-469` (T04 F7 — **confirm**) | `_retry_declined`: `while True: sleep; post; delay=min(delay*2, 5.0)` — retries a declined move forever at 1 POST / 5 s | `POST_RETRY_CEILING_SEC = 5.0` caps the *rate*, nothing caps the *duration* | N attempts or a staleness deadline + one `event=` on give-up |
| 14 | **Should-fix** | `jasper/accessories/bridge.py:727-731` (T09 #3 — **confirm, re-grade**) | Per-device reader tasks are unsupervised: `_read_device` catches only `OSError`, and the reaper is `del active[p]` with no exception inspection and no re-arm; the reap only runs when the *next* udev event arrives | Not fully silent — dropping the last ref makes asyncio's default handler log "Task exception was never retrieved" at GC — but it is unstructured and `/run/jasper-input/status.json` still reports `hid:{restarts:0,last_error:null}` | `add_done_callback` → `event=knob.reader_failed` + bounded-backoff `_maybe_start(path)`, counted in the status file |
| 15 | **Should-fix** | `jasper/correction/bundles.py` (no retention) vs `active_speaker/bundles.py:1017` (T11 #4 — **confirm**) | Correction session bundles (7+ raw WAVs, ~2 MB each) grow forever on the SD card while the sibling subsystem prunes | `cli/doctor/memory.py:571-600 check_correction_storage` *warns* at 512 MiB and its docstring says pruning "stays owned by the correction subsystem" — which implements none | Consume `active_speaker.bundles.enforce_retention` (or lift it to `audio_measurement/bundles.py`) after `write_info_json` |
| 16 | **Should-fix** | `rust/jasper-fanin/src/main.rs:201` + `mixer.rs:2799,2847`, `direct_capture.rs:816` (T19-1 F2 / T19-2 #7 — **confirm**) | Unbounded `mpsc::channel()` from the SCHED_FIFO thread into a writer that does `fdatasync` per event; each send heap-allocates `label.clone()` on the RT thread | the sibling TTS path in the same crate gets it right: `sync_channel(TAP_CHANNEL_CAPACITY)` at `mixer.rs:200` | `sync_channel(256)` + `try_send` drop-and-count; carry the lane index, not a `String` |
| 17 | **Should-fix** | `rust/jasper-outputd/src/state.rs:40,1934,1957-1961` (T20 #1 — **confirm**) | Blind `sleep(500 ms)` accept loop caps `/state` reads at ~2/s and is the stated justification for a 256-entry ring + 25 KiB of JSON workaround | `rust/jasper-fanin/src/state.rs:103 wait_for_listener` is a 20-line `poll()` fix already in the tree | Adopt `wait_for_listener`; then re-evaluate `ChipRefWriteRing` |
| 18 | **Should-fix** | `rust/jasper-outputd/src/alsa_backend.rs:1638-1641` (T20 #4 — **confirm**) | A DAC that disappears mid-run emits **no `event=` line** — only `EPIPE`/`ESTRPIPE` log; `ENODEV`/`EIO` propagate to a bare `Error: …` | the `else` arm returns `Err(e).context(...)` with no `eprintln!` | `eprintln!("event=outputd.dac.write_failed pcm=… errno=… action=exit")` |
| 19 | **Should-fix** | `rust/jasper-fanin/src/tts.rs:1290-1292` + `rust/jasper-outputd/src/tts.rs:254-256` (T19-2 #3 — **confirm**) | Thread-per-connection with **no cap and no `set_read_timeout`**, 512 KiB stack each, under `mlockall(MCL_FUTURE)` — a client that connects and never sends pins unswappable RAM forever | `state.rs:344` in the same crate *does* set a read timeout | Move the accept/read half into `jasper-tts-protocol` once, with a read timeout and one-client-at-a-time |
| 20 | **Should-fix** | `jasper/control/shairport_supervisor.py:339-352` + `control/server.py` restart handlers (T08 C1 — **confirm**) | `restart_shairport` awaits both children but **never inspects `returncode`**; `event=shairport.wedge_detected action=restart` and `restart_count++` fire *before* it, so `/state.resilience.shairport` reports restarts that a polkit denial silently prevented. Same shape: `POST /system/restart/audio` answers 200 for units outside `restart_broker.MANAGED_UNITS` | `restart_shairport` returns `None`; `shairport.restart_failed` only fires on an exception | Route through `restart_broker.manage_units` (it already returns `ok`/`rc`) and emit `event=…restart_failed rc=` |

### Astronaut engineering — delete

| file:line | why it can go |
|---|---|
| `rust/jasper-fanin/src/host_clock.rs:530-545` | `catch_unwind` is **dead in the shipped binary** — `Cargo.toml:85 panic = "abort"`, `rust-daemons.sh:215` builds `--release`. The `loop_result.is_err()` branch and `event=fanin.host_clock.thread_panic` cannot run (T19-2 #4 confirmed) |
| `jasper/watchdog.py:129-150` `_make_notifier` ImportError branch + the `sdnotify` dependency | `sdnotify>=0.3.2` is a hard dep (`pyproject.toml:12`), so the branch defends a hypothetical — and it fails *closed in the worst direction*: `start()` returns without `READY=1`, so a `Type=notify` unit hangs in activating. `jasper/web/_systemd.py:222-235` already sends the same datagram in 12 stdlib lines. Delete the dep, keep the `NOTIFY_SOCKET`-unset no-op, and collapse the two Python sd_notify implementations into one |
| `deploy/udev/99-jasper-audio-hardware-reconcile.rules:3` | `ENV{PRODUCT}=="05ac/110a/*"` can never match — udev formats `PRODUCT` with `%x` (no leading zeros); line 2's `5ac/110a/*` is the live rule. Dead duplicate |
| `deploy/avahi/jasper-control.service` (T24) | static fallback for a render path documented as "never raises"; a doctor row already covers a missing advert |
| `jasper/bluetooth/roles.py` (T04 F5) | `RoleStore.get`/`BluetoothEngine.roles` have zero callers; `bt_roles.json` is write-only |
| `tests/test_build_and_ci_contracts.py` lockfile-drift trio (T19-1 F6) | structurally impossible once `rust/Cargo.toml` declares a workspace |

## E. What only hardware/runtime can prove

- Whether the **udev → `jasper-aec-reconcile` → ready-marker removal** actually wins the race against
  aec-bridge's 4 × 2 s ladder on a Zero 2 W (F1's blast radius: 0 reboots vs 3). The bash reconciler
  spawns Python; if that takes > 8 s under memory pressure, the reboot happens first.
- Whether the F3 env-file race has ever fired. It needs a udev sound event concurrent with a coupling
  flip; both are operator-triggered, so the window is real but rare. A lost key here shows up as an
  audio-graph misconfiguration hours later, which is why nothing has traced it.
- Measured fork cost of F5/F6 on the Zero 2 W (`busctl`/`nmcli` RSS + scheduler impact under
  `jts-audio.slice`); the count is derived from the code, the *impact* is not.
- Whether `sessions` row count on the owner's box is already large enough for F9's scan to matter.
- Whether the SCHED_FIFO `warn!`/`info!` sites (T19-1 F1) actually block on a full journald socket.

## F. Coverage

**Opened (Python):** `volume_observers.py` (`_run`, `_tick`, `_read_airplay_db`,
`_read_bluetooth_volume`, `_busctl_get_property_value`), `camilla.py:420-500`, `voice/output_gate.py:70-115`,
`voice/_supervisor.py:270-310,463-545`, `voice/openai_session.py` (queue sites), `backoff.py:1-40`,
`audio_io.py:220-340`, `usage.py:405-470,555-560,754,790-850` + `SpendCap`, `wake_events.py` (retention grep),
`route_latency/status_socket.py:45-130`, `correction/runtime_integrity.py:85-135`, `fanin/status.py:100-145`,
`control/audio_health.py:236-275`, `control/airplay_health.py:1685-1715,320,532-601`,
`audio_validation.py:500-530`, `cli/system_soak.py:232-262`, `mux.py:350-382,590-620`, `renderer.py:108-145`,
`control/uds.py:168-205`, `control/server.py:1084-1135,2178-2332`, `control/restart_broker.py:330-375,600-640`,
`control/shairport_supervisor.py:330-365`, `control/supervisor_runtime.py:35-60`,
`control/system_metrics.py:300-330`, `control/state_aggregate.py:760-830`,
`control/wifi_guardian_state.py:60-235`, `control/bootloop_guard_state.py` (grep),
`accessories/bridge.py:635-745`, `usbsink/volume_bridge.py:440-485`, `fanin/coupling_reconcile.py:100-130,530-540,1730-1750,1900-2050`,
`output_hardware.py:650-675`, `watchdog.py:55-150`, `web/_systemd.py:220-240`, `cli/aec_bridge.py:1061-1295`,
`cli/doctor/memory.py:560-640`, `env_file.py`, `busctl.py`, `bluealsa_probe.py`, `tools/transport.py`,
`peering/transport.py:255-272`, `accessories/wiim_remote_mic.py:365-385`, `audio_measurement/playback.py:198-212`,
`web/sync_flow.py:150-282`.
**Opened (Rust):** `jasper-fanin/{main.rs:201, host_clock.rs:525-550, state.rs:87-110, tts.rs:1285-1340, Cargo.toml}`,
`jasper-outputd/{main.rs:140-205, state.rs:36-45,1930-1965, alsa_backend.rs:1605-1650, tts.rs:250-295, Cargo.toml}`.
**Opened (deploy):** all 58 files under `deploy/systemd/` (policy fields extracted mechanically, then
`jasper-{control,aec-bridge,voice,fanin,outputd,camilla,mux}.service` read in full),
`deploy/bin/jasper-bootloop-guard`, `deploy/bin/jasper-camilla-recover:1-45`,
`deploy/bin/jasper-audio-hardware-reconcile:594-660,846`, `deploy/bin/jasper-aec-reconcile` (marker + mic gate),
all four `deploy/udev/*.rules`, `deploy/assets/rooms/js/main.js:59`.
**Mechanical sweeps** (`scratchpad/L2-resilience/{sweep_subproc,sweep_loops,sweep_except,sweep_tasks,sweep_threads,units,socktimeout}.*`):
87 subprocess call sites · 79 `while True` loops · 545 broad `except` (127 fully silent, by package) ·
2 unreferenced `create_task` · 27 `Thread()` (all `daemon=`) · 25 `socket.socket()` vs `settimeout` ·
58 unit files × 11 policy fields · 137 `/var/lib/jasper*` state paths · env files by writer count.
**Skipped:** `jasper/web/` wizards beyond the socket/queue sweep (socket-activated, short-lived — out of
"long-running daemon" scope; `speaker_name_discovery` D-Bus hang is T18 F3, unchanged at HEAD);
`active_speaker/`/`audio_measurement/` internals (operator-driven, not resident — sampled only for the
`aplay` managers and retention comparison); `c/jts-ring-ioplug`; nginx and polkit (T24 owns them);
test files except where a claim needed a pin check.
