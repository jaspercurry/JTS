# Handoff: observability & debug-mode design

How JTS logging works today, the principle that keeps it
diagnosable without spamming the SD card, and the planned
per-subsystem debug toggle. Read [HANDOFF-resilience.md](HANDOFF-resilience.md)
first — this sits on top of that resilience ladder and does not
restate it.

> **Status: current-state reference + approved design.** The
> "Current state" section is operational truth (verified
> 2026-05-30). The "Plan" section is approved-but-not-yet-built:
> Tier A + B next, C + D later. When Tier B/C ship, move their
> rows from "Plan" to "Current state" and bump the footer.

---

## Current state (operational truth)

**Logging is plain `logging.basicConfig(level=INFO)` per daemon.**
Each long-running daemon (`jasper-voice`, `jasper-control`,
`jasper-aec-bridge`, `mux`, the renderers) calls `basicConfig`
once at startup with a hardcoded `INFO` level and the format
`%(asctime)s %(levelname)s %(name)s: %(message)s`. There is no
shared logging module and no `dictConfig`. Beyond the
per-subsystem **Debug card** (Tier B below) there is **no general
runtime log-level knob**: `JASPER_LOG_LEVEL` reaches only one idle
wizard (`jasper/web/speaker_setup.py`), not the daemons. The level
is read once at startup — which is why the Debug card applies via a
daemon restart (or, for `control`, in-process).

**The spine is the structured `event=` line.** Cross-daemon state
changes emit `event=<name> key=val …` lines (`event=shairport.wedge_detected`,
`event=system_supervisor.userspace_wedge`, `event=wifi_guardian.recreate_ok`,
`event=duck`, `event=tts_gain.compute`, …). `scripts/jasper-trace.sh`
keys off them. They are the cheap, high-signal, always-on
observability floor — keep them.

**Persistent journald is deliberate, not an oversight.**
`deploy/journald/50-jts-persistent-storage.conf` sets
`Storage=persistent` capped at 200 MB so a watchdog reset's
*previous-boot* logs survive — the whole point of Tier 5
forensics (see [HANDOFF-resilience.md](HANDOFF-resilience.md)).
Cost per that doc: ~30 MB/hr → ~270 GB/yr against ~100 TBW SD
endurance — **not a flash-wear emergency.** No `RateLimit*`
override today (systemd defaults apply).

**The heartbeat-vs-forensic split — the load-bearing principle.**
The resilience layer is *already* disciplined about steady-state
noise:
- the shairport + system supervisors log **nothing on the healthy
  path** — one `event=*.start` per boot, then silence until a
  failure (the `_tick` healthy branch returns without logging);
- the Tier-1 `Heartbeat.bump()` logs nothing per frame;
- the AEC reconciler and WiFi guardian are oneshot — one line per
  hardware/boot event.

Every recovery/decision line is **WARNING or ERROR**. That gives a
clean split a debug toggle can rely on:

- **Forensic — must always persist:** every WARNING+/`event=`
  recovery, probe-fail, wedge, restart/reboot decision,
  `stash_stale`/`recreate_*`, the Tier-1 `heartbeat suppressed`
  breadcrumb, the bridge `BridgeStalled` warning. You get **one
  shot** at these when a rare failure fires. Never suppress.
- **Heartbeat / chatty — safe to quiet:** a small set of always-on
  INFO emitters. There are essentially three (below).

**The three steady-state verbosity hotspots** (from real Pi logs,
music playing, ~110 lines/min combined):

| Source | Volume | Control point | Note |
|---|---|---|---|
| shairport PTP anchors | ~40/min (55% of shairport output) | `log_verbosity = 2` in `deploy/shairport-sync.conf.template` | **Intentional** — open AP2 "Pattern E" hunt ([HANDOFF-airplay.md](HANDOFF-airplay.md)). Do **not** lower until that bug closes. |
| AEC bridge `rms over` line | 1 / 5 s, always-on | the hardcoded `now - last_log > 5.0` gate in `aec_bridge.py`'s AEC loop | **Load-bearing** — `jasper-doctor`'s `_assess_aec_bridge_output` parses it from the journal, so demoting it blinds the AEC health check. Manage via Tier C, not demotion. |
| voice `event=tts_gain.compute` | ~9/min while music plays | INFO in `_apply_gain` (`voice_daemon.py`) | **Load-bearing** — deliberate reconstruction record for the 2026-05-24 ducking bug. Keep at INFO until Tier C can hold it in RAM. |
| voice `tts gain set` echo | mirrors the line above | INFO→DEBUG in `TtsPlayout.set_gain_db` (`audio_io.py`) | Redundant with `final_db=` above. **Demoted (Tier A, 2026-05-30).** |

(The `gemini_session` "live connection:" lines are
bursty-per-reconnect, not continuous — lower priority.)

**Resilience state is observable without logs:**
`curl -s http://jts.local:8780/state | jq .resilience` (`shairport`,
`system_supervisor`, `wifi_guardian`), the `jasper-doctor`
checks, and the `event=*` journal lines. Note: the `/system/`
dashboard does **not** render a resilience card today (the docs
correctly never claim it does) — adding one is a natural
extension of the debug card below.

---

## Plan (approved 2026-05-30; not yet built)

**Design invariant (non-negotiable):** debug mode is **additive
only**. It may *raise* verbosity; it must **never** lower a daemon
below WARNING and **never** suppress the forensic `event=` lines
the resilience layer depends on. There is no "quiet mode" that can
silence WARN+. Same spine as the "no silent failure paths" rule in
[AGENTS.md](../AGENTS.md).

**Tier A — done (2026-05-30).** Code review found only *one* of the
three "hotspots" safe to demote: the redundant `tts gain set` echo
in `audio_io.py` (`TtsPlayout.set_gain_db`) → DEBUG, since during
music it merely echoes `final_db=` from the richer
`event=tts_gain.compute` line. Also added a drop-to-`1` earmark
comment to shairport `log_verbosity = 2`. The other two were found
**load-bearing** on code review and deliberately left at INFO: the
AEC `rms over` line is parsed by `jasper-doctor`
(`_assess_aec_bridge_output`), and `event=tts_gain.compute` is the
deliberate reconstruction record for the 2026-05-24 ducking bug.
Both are the proper targets for the flight recorder (Tier C) —
high-volume but load-bearing, so manage them by holding verbose
detail in RAM and dumping on anomaly, not by demotion.

**Tier B — done (2026-05-30; pending on-device verification).** A
collapsed **Debug logging** card on `/system` expands to one
checkbox per subsystem (**voice**, **aec**, **control** — the
daemons with a clean `basicConfig` seam; shairport's config-file
`log_verbosity` and mux's `--log-level` are a different mechanism,
deferred). Each toggle raises that daemon's `jasper` logger to
DEBUG. As built:

- **SSOT:** [`jasper/debug_mode.py`](../jasper/debug_mode.py) reads
  `/var/lib/jasper/debug.env` fresh (pure resolver + `apply_for`,
  daemon-side, no web import — mirrors `provider_state.py`). Each
  daemon calls `apply_for("<id>")` right after `basicConfig`, so it
  reads the file **directly** — no systemd `EnvironmentFile` or
  install.sh seeding needed (a missing file resolves to a safe
  "off").
- **Write / restart / expiry:**
  [`jasper/control/debug_control.py`](../jasper/control/debug_control.py)
  lives in **jasper-control** (long-lived) — it *must*, because the
  `/system` page server (:8772) idle-exits after 30 min and can't
  own the auto-expiry timer. `set_debug` writes `debug.env`
  atomically, then **restarts voice/aec to apply** but applies
  **control in-process** (a self-restart would drop the request +
  the timer).
- **Endpoints:** `GET`/`POST /debug` on jasper-control (:8780),
  reachable from the card via a dedicated `location /debug` nginx
  block (mirroring `/mic`, `/volume`); the card fetches the absolute
  path. Also surfaced in `/state.debug`.
- **Auto-expiry:** one shared TTL (2 h, re-armed per change) via a
  `threading.Timer` in control; on fire it clears the flags +
  restarts the affected daemons back to INFO; reconciled on control
  startup (clear stale / re-arm pending). The card shows a live
  countdown.
- **Additive-only**, floored at WARNING (the invariant) — the toggle
  can only raise to DEBUG.
- **UI:** self-contained
  [`debug-card.js`](../deploy/assets/system-status/js/debug-card.js)
  (own fetch + client-side countdown; `h()`-escaped; confirm before
  the restart). install.sh ships it with the page's other `js/`.

Restart-to-apply is the accepted MVP (a hot SIGHUP re-read is a
possible follow-up — restarting a daemon to *start* debugging a live
issue is mildly self-defeating, but the flight recorder, Tier C, is
the real answer for the already-happened case). Backend is covered
by `tests/test_debug_mode.py` + `tests/test_debug_control.py`.
**Remaining: on-device verification** — the card renders client-side
and its toggles trigger real daemon restarts, so after a deploy open
`http://jts.local/system/`, toggle **voice**, and confirm
`journalctl -u jasper-voice` shows DEBUG lines + the countdown and
auto-quiet fire.

**Tier C — flight recorder (built 2026-05-30; pending on-device
verification).** A bounded in-RAM verbose ring per daemon,
dumped **only** on an anomaly. This is the real answer to the
central tension: the intermittent bugs that matter most **already
happened** before anyone could flip the Tier-B toggle, so capture
the verbose window around every anomaly automatically.

*Mechanism — decouple the logger level from the journal level.* A
small custom `logging.Handler` over a `deque` (stdlib `MemoryHandler`
was evaluated but it flushes on capacity and routes through a target
handler whose INFO level would drop the buffered DEBUG lines):

| Component | Level | Effect |
|---|---|---|
| `jasper` logger | DEBUG always | DEBUG records get *created* |
| journal `StreamHandler` | INFO (DEBUG when the Tier-B toggle is on) | **journal volume unchanged** — DEBUG never hits the SD card |
| `RingFlushHandler` (new) | DEBUG | buffers the last N DEBUG+ records in a `deque(maxlen=N)`; flushes only on WARNING+/explicit |

```python
class RingFlushHandler(logging.Handler):                   # level = DEBUG
    def emit(self, record):
        self.buffer.append(record)                         # deque(maxlen=N): drops oldest
        if record.levelno >= logging.WARNING:
            self.flush_buffer("auto:" + record.levelname.lower())
    def flush_buffer(self, reason):                        # also called by dump()
        ...  # write a tagged burst of the buffer to the dump stream, then clear
```

*Decisions (2026-05-30):*
- **Dump target: journal burst.** On flush, re-emit the buffered
  records into journald tagged `event=flightrec.dump`, right after
  the triggering WARNING — reuses the 200 MB journald cap (retention)
  + `fetch-pi-logs.sh`; DEBUG context lands in the same timeline as
  the anomaly. (Target stays pluggable so dump-files can be added
  later.)
- **Triggers (all in v1):** automatic on any WARNING/ERROR (built
  into `flushLevel`), plus explicit `dump(reason)` from the
  `flag_recent_issue` voice tool, supervisor restart decisions
  (`event=shairport.wedge_detected`, `event=system_supervisor.userspace_wedge`),
  and failing `jasper-doctor` checks.
- **Scope (v1):** voice + aec + control.

*Tier-B integration (done).* `apply_for` now also flips the journal
*handler* level via `set_console_debug` (the logger is held at DEBUG
by the recorder for the ring); same toggle behaviour, and the
committed Tier-B tests still pass.

*The payoff (closes the Tier-A loop).* With the ring in place,
**`event=tts_gain.compute` can finally move to DEBUG** — quiet in
the journal during music, but still captured in RAM and dumped
around any related anomaly, preserving the after-the-fact
reconstruction it exists for. Same for any future verbose
instrumentation: RAM-only, persisted only when something breaks.
(The AEC `rms over` line still stays INFO — `jasper-doctor` reads it
*continuously*, which a dump-on-anomaly model can't serve.)

*Cost.* ~N × 0.3 KB. N=2000 ≈ 600 KB/daemon; voice+aec+control ≈
1.8 MB — trivial on a 1–2 GB Pi, tunable.

*Honest grounding.* A small custom `logging.Handler` (stdlib
`MemoryHandler` evaluated — see Mechanism) + the general pattern
(Linux ftrace snapshot triggers, Android logd, OpenTelemetry
tail-sampling, Rust `tracing-appender`) + the Pi cohort's
RAM-logging consensus (DietPi RAMlog, log2ram). **No Pi-appliance in
the comparable cohort ships a structured log flight-recorder** — it
is sound-by-analogy, not cohort-corroborated. JTS already does this
for *audio* (the wake-event 6 s pre/post rings); Tier C generalizes
it to logs.

*As built.* [`jasper/flight_recorder.py`](../jasper/flight_recorder.py)
(`RingFlushHandler` + `install()` + `dump()` + a SIGUSR1 handler)
wired into voice/aec/control startup; `debug_mode.apply_for` gained
`set_console_debug` so the toggle moves the journal handler; explicit
`dump()` from the `flag_recent_issue` voice tool and a `systemctl
kill --kill-whom=main -s SIGUSR1` from a failing `jasper-doctor` run
(supervisor restarts auto-flush — they already log ERROR). Off via
`JASPER_FLIGHT_RECORDER=disabled`. Tests:
`tests/test_flight_recorder.py` plus trigger tests in `test_doctor.py`
/ `test_tools_diagnostic.py`. **Remaining: on-device verification** —
deploy, then confirm a WARNING produces an `event=flightrec.dump`
burst in `journalctl`, and that "flag that" + a doctor FAIL each
trigger one.

**Tier D — done (2026-05-30; pending on-device verification).** A
"Download diagnostics" button on `/system` runs
[`scripts/pi-bundle.sh`](../scripts/pi-bundle.sh) (logs + redacted
config → tarball) and streams it as a one-tap download. As built:
- **Endpoint:** `GET /diagnostics-bundle` on jasper-control (:8780),
  reached via a dedicated `location /diagnostics-bundle` nginx block
  (the `/debug` pattern; `proxy_buffering off` + a long read timeout
  so it streams). control runs as root, so it can run the bundle;
  `_run_diagnostics_bundle()` captures the path the script prints,
  streams the bytes, and deletes the /tmp tarball. Single-flight
  (`_bundle_lock`): a concurrent click gets 409.
- **install.sh** stages `pi-bundle.sh` + `_diagnostic_redaction.sh`
  at `/opt/jasper/scripts/` (the main rsync excludes `scripts/`).
- **UI:** the button (in the Run-diagnostics card) fetches the blob
  and triggers a download; a 409/502 surfaces as a message, not a
  browser error page.
- **Gate:** a client-side confirm warns it's heavy I/O that may
  briefly affect audio (the Sonos "may interrupt" idiom). A *hard*
  server-side refuse-while-playing gate was **deferred** — the bundle
  is read-only gathering (journalctl + file reads + tar), not
  audio-destructive, so a confirm is proportionate; it's a one-line
  add in the handler if it proves disruptive on-device.

Tests: `tests/test_control_diagnostics_bundle.py`. **Remaining:
on-device verification** (the real bundle + browser download need
the Pi).

---

## Why this shape (cohort grounding, 2026-05-30)

Validated against the comparable Raspberry Pi appliance/fleet
cohort (Home Assistant OS, balenaOS, piCorePlayer, Volumio, moOde,
OctoPrint, DietPi):

- **(c) per-subsystem auto-expiring debug toggle and (d)
  download-diagnostics are cohort-standard** — OctoPrint, Home
  Assistant, Volumio, Sonos all ship them. JTS's auto-expiry is a
  refinement over OctoPrint's "we just warn you it's on."
- **(a) WARN+ floor in persistent journald + (b) chatty detail in
  RAM** are reasonable and slightly *ahead* of the audio-hobbyist
  tier (Volumio/moOde keep persistent logs too) — conditional on
  actually doing the INFO→DEBUG/RAM demotion. The managed tier
  (HA OS, balenaOS, piCorePlayer) keeps logging volatile via
  read-only root.
- **JTS is ahead of the cohort** on watchdog (hardware watchdog +
  userspace-liveness supervisor — the gap Poettering's canonical
  systemd-watchdog writeup says the hardware watchdog cannot
  cover) and on memory resilience (OOMScoreAdjust / zram / MGLRU /
  cgroups).

**Out of scope here but flagged (separate workstream →
[HANDOFF-resilience.md](HANDOFF-resilience.md)):** the cohort's
primary durability answer to JTS's actual past incident
(unclean-power ext4 corruption) is read-only/overlay root and/or a
supercapacitor UPS HAT for graceful shutdown on power loss. JTS
has neither. Not an observability decision, but the
highest-leverage durability gap the research surfaced.

Key sources: Home Assistant [logger](https://www.home-assistant.io/integrations/logger/)
+ [diagnostics](https://www.home-assistant.io/integrations/diagnostics/);
OctoPrint [logging plugin](https://docs.octoprint.org/en/main/bundledplugins/logging.html);
kernel [ftrace snapshot](https://docs.kernel.org/trace/ftrace.html);
[OTel tail-sampling](https://opentelemetry.io/blog/2022/tail-sampling/);
piCorePlayer [RAM-root](https://docs.picoreplayer.org/faq/my_changes_disappeared/);
HA OS [read-only partitions](https://developers.home-assistant.io/docs/operating-system/partition/);
Poettering [systemd watchdog](http://0pointer.de/blog/projects/watchdog.html);
Dzombak [reduce Pi SD writes](https://www.dzombak.com/blog/2024/04/pi-reliability-reduce-writes-to-your-sd-card/).

---

Last verified: 2026-05-30
