# L5 — Pi resource budget (Pi 5 1 GB full / Pi Zero 2 W 415 MB streambox) @ 2d571e6b8

**Measurement note.** All numbers are laptop (x86, CPython 3.11, warm page cache), best-of-3
`-X importtime`, deps installed into `scratchpad/L5-pi-performance/v` (the repo `.venv` is empty).
`sounddevice`, `openai`, `google-genai`, `camilladsp`, `openwakeword` were **not** installed, so
`voice_daemon`/`cues.cli` figures are **lower bounds**. Ratios and module counts transfer to ARM;
absolute ms do not (a Zero 2 W is roughly 8–15× slower on interpreter start). Scripts:
`scratchpad/L5-pi-performance/{importtime,blame,rss,loops,shspawn}.py`.

## A. Verdict

ADR-0226 has been **applied where it was audited and not generalised**. The `Exec*=`/udev/path-unit
surface is now genuinely clean — every `ExecCondition=` I opened is POSIX shell, the camilla
guards are pure bash with a Python repair-only tail, and the usbgadget composer reads a
reconciler-published marker before it will pay an interpreter. Mux already defers its two
fork-backed probes to 5 s. That is the good half. The bad half is that the doctrine stops at the
unit file: (1) **`jasper-voice` still forks 2–3 processes every second, forever**, for a value it
throws away — the single largest steady-state cost on the box (T04-F1, confirmed); (2) the two
**udev-driven reconcilers spawn ~6 and ~17 short-lived interpreters per hotplug pass**, several of
them paying 25 ms of `asyncio`+`ssl` because a two-line pure function was imported from a
1,400-line async module (T07-2, confirmed and traced); (3) the **import-cost charters in
`crossover_v2` are not just false, they are catastrophically false** — `refusal_copy`, the "pure
copy" module, costs **967 modules / 1.14 s / ~105 MB** because of two imports serving 15 string
constants (T13-1 C1/C2, confirmed, re-graded **Blocker**); and (4) the **OOM ladder has no bottom
rung** — nothing is marked sacrificial except a compiler and one observer, and the three units with
*no* `MemoryMax` at all (`jasper-control`, `jasper-web`, `jasper-correction-web`) include the one
that can balloon to 115 MB. Build-side, the `[profile.release] lto="fat"` blocks are **dead on the
owner's hardware** (both boxes fall under the 1.2 GB low-memory threshold, so both ship the
real-time audio daemons at `opt-level=0`), and the prebuilt first-party ARM64 bundle — ~1,700 LOC
of installer+test+workflow — is **wired to nothing on the deploy path**.

## B. Import cost

### B1 — resident daemon entry modules

| entry module | unit | mods | ms | RSS kB | heaviest subtrees (ms) |
|---|---|---:|---:|---:|---|
| `jasper.cli.doctor` | jasper-doctor-json | 374 | 330 | 38 216 | asyncio 43, yaml 14, http 11, sqlite3 |
| `jasper.control.server` | jasper-control | 392 | 309 | 36 336 | asyncio 44, urllib 20, yaml 20, http 12 |
| `jasper.voice_daemon` | jasper-voice | 348 | 233† | 40 676 | **numpy 65**, asyncio 54, sqlite3 |
| `jasper.cues.cli` | (install) | 313 | 184† | 39 760 | **numpy 80**, asyncio 23 |
| `jasper.cli.aec_bridge` | jasper-aec-bridge | 305 | 177 | — | **numpy 65**, asyncio 17 |
| `jasper.accessories.reconcile` | jasper-accessory-reconcile | 203 | 110 | — | asyncio 42 |
| `jasper.mux` | jasper-mux | 200 | 108 | 22 348 | asyncio 43 |
| `jasper.cli.usb_mic` | jasper-usbmic | 192 | 102 | — | asyncio 19 |
| `jasper.accessories.bridge` | jasper-input | 188 | 83 | 22 068 | asyncio 41, http 13, email 10 |
| `jasper.bluetooth.no_code_agent` | bt-agent | 181 | 78 | — | asyncio 41, dbus_next 14 |
| `jasper.cli.usbsink_volume_main` | jasper-usbsink-volume | 168 | 74 | — | asyncio 49, http 14 |
| `jasper.web.__main__` (+14 wizards) | jasper-web | 152 (422) | 74 | 43 144 | http 31 — **no numpy** |
| `jasper.fanin.coupling_reconcile` | jasper-fanin-coupling-auto | 149 | 100 | — | **no asyncio, no numpy** |
| `jasper.cli.wiim_remote_ce` | jasper-wiim-remote-ce | 144 | 63 | — | asyncio 41 |
| bare interpreter baseline | — | 35 | — | 10 156 | — |

† lower bound (sounddevice/openai/google-genai absent).

### B2 — short-lived shims on boot / hotplug / reconcile paths (the ADR-0226 hot paths)

| shim | spawned from | mods | ms | avoidable share |
|---|---|---:|---:|---|
| `jasper.cli.audio_input_profile` | `deploy/bin/jasper-aec-reconcile:702` | 212 | 128 | **27 ms asyncio + 7 ms ssl** |
| `jasper.cli.chip_aec_policy` ×2 | `jasper-aec-reconcile:792, :1779` | 199 | 111 | same 34 ms, twice |
| `jasper.cli.xvf_profile` | `jasper-aec-reconcile:260` | 120 | 52 | — |
| `jasper.cli.measurement_mic` | `jasper-aec-reconcile:1198` | 54 | 18 | model shim ✅ |
| `jasper.accessories.mic_env` | `jasper-aec-reconcile:1588` | 37 | 10 | model shim ✅ |
| `jasper.output_hardware` | `jasper-audio-hardware-reconcile:393` | 133 | 66 | — |
| `jasper.cli.audio_config` ×4 | `…-reconcile:627,661,1275,1288,1560` | 134 | 80 | — |
| `jasper.audio_hardware.usb_port_role` | `…-reconcile:455`, usbgadget compose:212 | 120 | 58 | — |
| `jasper-active-speaker runtime-safe-graph` | `…-reconcile:1529`, camilla guards (repair only) | 262 | 190 | yaml 15 |
| `jasper-sound render-flat-cutover` | `…-reconcile:1498` | 185 | 92 | asyncio 42 |
| `jasper.multiroom.reconcile` | `jasper-grouping-reconcile.service:38` | 199 | 115 | asyncio 24 (via `source_intent`) |
| `jasper.usb_network promote` | `jasper-usb-network-plan.service:8` | 99 | 41 | — |
| `jasper.wifi_scan_repair` | `jasper-wifi-recover:118` (repair only) | 126 | 52 | — |
| `jasper.model_downloads` ×4 | `deploy/lib/install/model-staging.sh` | 146 | 70 | http 22 + email 15 |

**Hot-path answer.** udev `ACTION=="add|remove", SUBSYSTEM=="sound", KERNEL=="controlC*"`
(`deploy/udev/99-jasper-aec-reconcile.rules:12`, `99-jasper-audio-hardware-reconcile.rules:14`)
starts **both** reconcilers on every sound-card add/remove. Static spawn-site analysis
(`shspawn.py`) counts **6 Python-spawning functions, all reachable from top level**, in
`jasper-aec-reconcile` (≈430 ms laptop, 6 interpreter starts, ~130 MB of transient RSS peaks) and
**17 spawn sites** across 5 functions in `jasper-audio-hardware-reconcile` (two of them the
190 ms `jasper-active-speaker`). A dongle replug pays both.

### B3 — the three avoidable import chains (traced, `blame.py`)

| chain | cost | fix |
|---|---|---|
| `chip_aec.health:34` → `chip_aec.alignment:22` → `audio_measurement.ramp:42 import asyncio` | **+25 ms asyncio, +7 ms ssl, ~72 modules** on `health`, `audio_input_profile`, `chip_aec_policy` (×3 shims/pass) | `ramp:78 capped_gap_step_db` is `min(target-measured, cap)` — two lines, zero deps. Move it (or the `PER_UNIT_IDENTITY_FIELDS` frozenset) to a leaf. **Confirms T07-2.** |
| `identity.py:42 from .peering import config` → `peering/__init__.py:60-65` eager `.rank` + `.state` | `jasper.peering.state` is the **single largest non-numpy self-time in six daemons** (10.1–13.9 ms each: mux, voice, control, doctor, cues, `jasper.config`) | drop the `state`/`rank` re-exports. **Confirms T09-4.** |
| `crossover_v2.refusal_copy:30-32` → `capture_dispatch` → `audio_measurement.program_analysis` → `…alignment` → **scipy** | `refusal_copy` alone = **967 mods / 1 138 ms / ~105 MB**; `web.correction_crossover_v2` = **1 012 mods / 1 170 ms / 114 768 kB RSS** (scipy 868 ms of it) | **Confirms T13-1 C1+C2 and re-grades C2 to Blocker.** The 15 `SCREEN_*→REASON_*` rows at `refusal_copy:941-957` are the whole reason scipy is in this graph. |

Also: `cues/manager.py:36` imports `wait_tts_drained_owned` from `audio_io.py`, whose `:19 import
numpy as np` serves the mic-capture classes only — so `jasper-cues regenerate` (`install.sh:2040`)
pays 80 ms + ~18 MB of numpy for a socket drain helper. `jasper.fanin.coupling_reconcile` (149
mods, no asyncio, no numpy) is the model to copy.

## C. Steady-state polling inventory

| loop | file:line | cadence | per-tick work | forks/min |
|---|---|---|---|---:|
| `VolumeObserver._run` (jasper-voice) | `volume_observers.py:135,80` | **1.0 s, flat** | mux UDS ×2, `busctl` shairport (result **only logged**, `:180-186`), `bluealsa-cli list-pcms`, `busctl` MediaTransport1 if BT, camilla reconcile | **120–180** |
| `Mux.run` patrol | `mux.py:352, 186-189` | 1.0 s, alert-woken | UDS + file; the 2 fork-backed probes deferred to `EVENT_BACKED_PROBE_SEC=5.0` | 24 |
| `AudioHealth._run` | `control/audio_health.py:2507` | 5 s | /proc+/sys; mpris busctl @30 s; camilla @30 s; **`journalctl` ×2 @30 s**; `systemctl show` for camilla restart age | ~8 |
| `SystemMetrics._run` | `control/system_metrics.py:306` | 5 s | /proc+statvfs+sysfs only; `vcgencmd` @30 s; one **batched** `systemctl show` @30 s | 4 |
| `_SystemdWatchdog` | `web/_systemd.py:459` | WatchdogSec/2 | sd_notify write | 0 |
| `jasper-wifi-recover.timer` | `.timer` OnUnitActiveSec=3min | 3 min | `journalctl -k` (Python only on repair) | 0.3 |
| `jasper-identity-reconcile.timer` | OnUnitActiveSec=5min | 5 min | **pure bash, 266 lines, no interpreter** ✅ | 0 |
| landing `/volume` | `deploy/index.html:954` | **500 ms**, visibility-gated | `handlers/volume.py:21` `asyncio.run()` — a **new event loop per request** | 0 |
| landing `/source/state`, `/mic` | `index.html:1279,1408` | 3 s each | mux UDS | 0 |
| `/system/data.json` | `index.html:863,955` | 5 s + 20 s | proxy to control's ring buffer | 0 |
| `/sources/state` | `assets/sources/js/main.js:30` | **4 s per tab** | `sources_setup.py:324 _gather_state` → 1 `systemctl show` (`web/_unit_snapshot.py:117`) + **2 fresh D-Bus session connects** (`bluetooth/adapter.py:140` `state()`, `has_paired_hid()`) | **15** + 30 D-Bus |
| correction envelope | `assets/correction/js/main.js:2131,131` | 900 ms **active only**, 10 s idle | idle path calls `_room_readiness()`; the bundle-root scan is gated to idle/result screens (`correction_setup.py:2610-2614`) | 0 |
| `/state` (jasper-control) | `state_aggregate.py:1130-1143` | **on demand only** | 1 `busctl` + 2 `nmcli` + 1 `journalctl` (`wifi_guardian_state.py:74,88,116`) | — |

**Idle full-profile speaker, no browser: ~155–210 subprocess forks/min, of which jasper-voice is
120–180.** With one `/sources` tab open, +15 forks and +30 D-Bus session connects per minute.

## D. Memory

| | |
|---|---|
| Units declaring `MemoryMax` | 14, summing to **1 464 MB** — 3.5× the Zero 2 W's 415 MB |
| Units with **no** ceiling and **no** `OOMScoreAdjust` | `jasper-web`, `jasper-web-streambox`, `jasper-correction-web`, and every reconciler oneshot |
| Daemons importing numpy at module scope | **3**: `voice_daemon`, `cli.aec_bridge`, `cues.cli` (all via `audio_io.py:19`) — none import scipy |
| Lazy scipy blast radius | `web.correction_crossover_v2` → **+790 modules, 115 MB RSS** on the first crossover request, inside the one unit with no `MemoryMax` |
| jasper-web design | 14 wizards, one interpreter, socket-activated, 10-min idle exit, 43 MB total — **the model ADR-0225 asks for** ✅ |

**OOM ladder** (`jasper/_oom_adj.py:29-47`, mirrored in the unit files): outputd −950, camilla −900,
fanin −800, aec-bridge −700, control −600, voice/camilla-crossover −500, nginx −450, ssh −250,
mux/input/bt-agent/snapclient/snapserver/usbmic −300, usbsink-volume +100, enhanced-aec-install +900.
The audio chain **is** protected, consistently and in a defensible order. **The wizards are not
sacrificed** — every `jasper-*-web` unit sits at the default 0, i.e. tied with `udevd`, `dbus` and
`logind`, the exact processes ADR-0226's incident report names as OOM-killed. The ladder has 8
protected rungs and 2 sacrificial ones, and the biggest sacrificeable heap on the box is on
neither.

## E. Build / deploy cost on device

- **`lto="fat"` is dead code on the owner's hardware.** `rust/jasper-fanin/Cargo.toml:74-83` and
  `rust/jasper-outputd/Cargo.toml:53-56` set `opt-level=3, lto="fat", codegen-units=1`; but
  `deploy/lib/install/rust-daemons.sh:15` sets `RUST_LOW_MEMORY_BUILD_THRESHOLD_KB=1200000` and
  `:59-61` overrides to `LTO=false, CODEGEN_UNITS=16, OPT_LEVEL=0` below it. A 1 GB Pi 5 reports
  ~950 000 kB MemTotal → **both** of the owner's boxes build the real-time audio daemons at
  **`opt-level=0`**. That is a correctness-of-intent problem in the *fast* direction, not the slow
  one. (Only 2 crates carry `lto=fat`, not 3 — brief refuted.)
- **The prebuilt path is not on the deploy path.** `JASPER_FIRST_PARTY_RUNTIME_BUNDLE` is set by
  nothing in `scripts/`, `.github/` or `docs/` — only documented at `install.sh:115` and exercised
  by `tests/test_first_party_arm64_release.py`. So `deploy/lib/install/first-party-runtime.sh`
  (581 LOC) + that test (1 098 LOC) + a manual-dispatch workflow exist, and every
  `deploy-to-pi.sh` still compiles Rust on the Pi.
- **8 crates, no workspace** → `jasper-clock` etc. compiled once per consumer target dir
  (T21-1, confirmed by inspection: `rust-daemons.sh:149-197` stages 6 crates *per daemon*).
- **`heal_shared_state_modes` runs 12× per install**, each a `/usr/bin/python3` heredoc over a
  ~36-entry allowlist (`env-migrations.sh:42,64,175`; `ensure_state_dir` called from
  `install.sh:980,1318,1492,2005`, `python-runtime.sh:24,124,449`, `env-migrations.sh:455,560`,
  `model-staging.sh:28,45`, `renderers.sh:214`). **Confirms T23** (I count 12 sites, not 11).
- Venv: `pip install --upgrade pip==26.1.2 wheel==0.47.0` + `--no-deps openwakeword` + the editable
  install run on **every** deploy (`python-runtime.sh:240,259` — and again at `:469`), plus two `rsync -a --delete` of
  `jasper/` into `/opt/jasper/`.

## F. Ranked fixes (Pi-seconds or MB saved × frequency)

| # | sev | site | fix | why it ranks here |
|---|---|---|---|---|
| 1 | Should-fix | `volume_observers.py:171-175` (T04-F1) | probe only `current_active`'s source; delete `_read_airplay_db` + `AIRPLAY_DB_MIN/MAX`; adopt mux's `EVENT_BACKED_PROBE_SEC` defer | removes **~100–150 forks/min forever**, the single largest steady-state cost on the box |
| 2 | **Blocker** | `crossover_v2/refusal_copy.py:30-32` (T13-1 C2, **re-graded up**) | declare the 15 `SCREEN_*` codes in `contracts.py`'s S12 block; drop the `spatial`/`capture_dispatch` imports | closes the **only** scipy door: −790 modules, **−105 MB RSS**, −0.9 s per first crossover request |
| 3 | Should-fix | `crossover_v2/branch_chain.py:22` ← `contracts.py:25` (T13-1 C1) | move the 3-field `CrossoverSection` to a leaf | makes `contracts` numpy-free (−60 ms, −20 MB) and makes three written charters true |
| 4 | Should-fix | `chip_aec/alignment.py:22` (T07-2) | move `capped_gap_step_db` (2 lines) or `PER_UNIT_IDENTITY_FIELDS` to a leaf | **−34 ms × 3 shims × every hotplug/boot pass**; adds no code |
| 5 | Should-fix | `jasper-web`/`-correction-web`/`-streambox` units + `_oom_adj.py` | give the wizards `MemoryMax=` and a **positive** `OOMScoreAdjust` (+200…+500) | the ladder currently has no bottom rung; on 415 MB the kernel picks `dbus`/`logind` instead |
| 6 | Should-fix | `peering/__init__.py:60-65` (T09-4) | drop the eager `state`/`rank` re-exports | −10 ms and ~15 modules in **six** processes incl. every reconciler shim that touches `jasper.config` |
| 7 | Should-fix | `rust-daemons.sh:15` vs `jasper-{fanin,outputd}/Cargo.toml` | either raise the threshold above 1 GB, or use `opt-level=2, lto="thin"` as the low-memory profile | today the RT audio daemons ship **unoptimised** on both owner boxes |
| 8 | Should-fix | `sources_setup.py:324` (T16-1-10) | memoise `_gather_state` for ~2 s, or reuse one BlueZ session | −15 forks and −30 D-Bus connects per minute per open tab |
| 9 | Should-fix | deploy path | set `JASPER_FIRST_PARTY_RUNTIME_BUNDLE` in `deploy-to-pi.sh` (or delete the 1 700 LOC) | removes on-Pi Rust compilation from every deploy, **or** removes dead machinery |
| 10 | Should-fix | `audio_io.py:19` + `cues/manager.py:36` | split the numpy-using capture classes out of `audio_io`, or move `wait_tts_drained_owned` | `jasper-cues regenerate` stops paying 80 ms + 18 MB of numpy on every install |
| 11 | Should-fix | `env-migrations.sh:42` (T23) | make `heal_shared_state_modes` idempotent-by-stamp, or call it once from `main` | 11 of 12 interpreter starts per install are pure duplicate |
| 12 | Should-fix | `wake_events.py` (T02) | add row retention (`DELETE` older than N days) beside the existing WAV sweep at `:796` | `grep -c "DELETE FROM\|VACUUM"` = 0; doctor already budgets 300 MiB for a DB documented as "grows forever" |
| 13 | Should-fix | `rust/` (T21-1) | add `rust/Cargo.toml [workspace]` | 4 redundant builds of `jasper-clock` per deploy; collapses `rust-daemons.sh:149-197` |
| 14 | Nit | `control/wifi_guardian_state.py:165` (T08-C7, **re-graded down**) | cache on the stash file's mtime | `/state` is **not** UI-polled (see §G) — this is a doctor-run cost, not a 3 s cost |
| 15 | Nit | `handlers/volume.py:21` + 19 more `asyncio.run(` in `control/` | one long-lived loop thread, or a `run_coro` helper | a new event loop per request at 2 Hz per visible tab |

## G. Claims I refute or re-grade

| claim | verdict |
|---|---|
| "correction household poll globs the bundle root every 500–900 ms" | **Refuted.** `correction_setup.py:2610-2614` confines `list_bundles` to `REPORT_SECTION_SCREENS`; `assets/correction/js/main.js:2126-2133` polls those at `idleEnvelopeRefreshMs=10000`, and the 900 ms path is active-capture screens only. The code comment states the rule and the code obeys it. |
| T08-C7 "`/state` is polled by the landing page, /rooms (7 s), /sources (4 s), /system (5 s)" | **Refuted.** Nothing in `deploy/assets/` or `deploy/index.html` fetches jasper-control's `/state`; nginx does not proxy it. Those are the **wizards' own** `./state` routes. Control's `/state` is built by `cli/doctor/_evidence.py:288` (once per doctor run) and laptop tooling. C7 stands as a cost, drops as a frequency. |
| "1 GiB WAV cap default" (wake_events) | **Refuted.** `wake_events.py:82 DEFAULT_MAX_AUDIO_BYTES = 128 * 1024 * 1024`. The unbounded-**rows** half of T02 is confirmed (`grep "DELETE FROM\|VACUUM" jasper/wake_events.py` → 0 hits). |
| "lto=fat ×3" | **Refuted** — 2 crates (`jasper-fanin`, `jasper-outputd`); and both are overridden on the owner's hardware (§E). |
| "PR #4137 in flight lowers opt-level on low-memory hosts" | **Already at HEAD** — `rust-daemons.sh:22-61`. The remaining issue is that the 1.2 GB threshold catches the Pi 5 1 GB too. |
| T16-3 "24 local re-imports of modules that import in <0.15 s" | **Confirmed as harmless-but-noisy.** Measured: `output_topology` 0.14 s, and these are re-imports of already-loaded modules — a dict lookup. The cost is readability, not Pi-seconds; keep it as a tidiness finding, not a performance one. |
| T07-2 (6 shims/pass), T09-4, T04-F1, T13-1 C1/C2, T21-1, T23 | **All confirmed**, with numbers above. |

## H. What only hardware/runtime can prove

1. Absolute Pi-seconds. Every ms here is x86. The ARM multiplier for interpreter start (and
   especially for numpy/scipy `.so` relocation off an SD card, cold) needs
   `bash scripts/pi-run-diagnostic.sh -- python -X importtime -c 'import …'` on both boxes.
2. **Real RSS.** `ru_maxrss` after import ≠ steady-state PSS with shared libs and glibc arenas.
   `systemd-cgtop`/`MemoryCurrent` on a live box is the only honest number, and it is what decides
   whether the §D ceilings are right.
3. Whether `bluealsa-cli`'s `probe_suppressed()` circuit breaker is actually open on a real
   speaker (which would cut the §C fork count) — journal-only.
4. Whether the Zero 2 W ever reaches the scipy path at all: `jasper-correction-web` is
   socket-activated there, so the 115 MB spike may be unreachable in practice.
5. Whether `opt-level=0` fan-in/outputd actually meet their deadlines on a Zero 2 W — the reason
   `lto=fat` was written in the first place. If they do, delete the profile blocks; if they don't,
   fix the threshold. Only xrun counts settle it.

## I. Coverage

**Measured** (`-X importtime` ×3 + RSS): 35 modules across every daemon entry point, every
`python -m jasper.*` reachable from `deploy/systemd/` and `deploy/bin/`, and the crossover_v2 /
audio_io / peering / ramp chains. Import-parent blame run on 13 of them.

**Opened**: `docs/adr/0225`, `0226`; all 63 `deploy/systemd/` units + the 6 root `deploy/*.service`
+ 6 `.socket` + 2 `.timer` + 3 `.path` + 4 `deploy/udev/*.rules`; `deploy/bin/jasper-aec-reconcile`,
`jasper-audio-hardware-reconcile`, `jasper-camilla-pipe-guard`, `jasper-wifi-recover`,
`jasper-identity-reconcile`, `deploy/lib/jasper-camilla-guard-common.sh`,
`deploy/usbsink/jasper-usbgadget-compose.sh`; `deploy/lib/install/{rust-daemons,memory-resilience,
env-migrations,python-runtime,systemd-units,first-party-runtime}.sh`, `deploy/install.sh` (spawn
sites), `scripts/deploy-to-pi.sh` (rsync block); `jasper/{volume_observers,busctl,bluealsa_probe,
_oom_adj,audio_io,identity,wake_events}.py`, `jasper/peering/__init__.py`,
`jasper/chip_aec/{health,alignment}.py`, `jasper/audio_measurement/ramp.py` (imports + the one
function), `jasper/control/{state_aggregate,wifi_guardian_state,airplay_health,audio_health,
system_metrics}.py`, `jasper/control/handlers/volume.py`, `jasper/web/{__main__,correction_setup,
sources_setup}.py`, `jasper/correction/bundles.py`, `jasper/cli/doctor/memory.py`,
`jasper/mux.py` (patrol + probe-defer), `jasper/fanin/coupling_reconcile.py` (lock loop);
`rust/*/Cargo.toml`; `deploy/index.html` + `deploy/assets/{sources,correction}/js/main.js`;
`.github/workflows/first-party-arm64-release.yml`.

**Scanned by script**: all 1 100+ `jasper/**/*.py` for `while`/`for` loops containing a sleep (72
hits, table in §C covers every resident one); all `deploy/bin/*` for interpreter spawns.

**Skipped**: the Rust/C sources themselves (T21 owns them; I took only the profile blocks and the
build wiring); `experiments/`; the measurement/tuning CLIs that are operator-invoked and therefore
outside ADR-0226's hot-path rule; `jasper/voice/` provider sessions (no resident loop cost beyond
`voice_daemon`'s own three, all sub-1 Hz and fork-free).
