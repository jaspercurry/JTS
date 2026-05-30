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
- **Auto-expiry:** one shared TTL (2 h, re-armed per change). At
  expiry **each daemon quiets itself in process** — `apply_for` arms a
  per-process `threading.Timer` that drops that daemon's journal
  handler back to INFO, **no restart** (so a forgotten session can't
  blip wake while the household is mid-use). Control's in-process
  toggle goes through the same `apply_for` path, so it self-quiets the
  same way. A separate `threading.Timer` in control clears the
  `debug.env` SSOT at expiry (so `/state` reads off + the next start
  is clean); reconciled on control startup. The card shows a live
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
        self.buffer.append(self.format(record))            # deque(maxlen=N) of STRINGS
        if record.levelno >= logging.WARNING:              #   -> bounded RAM, no arg pinning
            self.flush_buffer("auto:" + record.levelname.lower())
    def flush_buffer(self, reason):                        # also called by dump()
        ...  # write a tagged burst of the buffered lines, then clear
```

*Decisions (2026-05-30):*
- **Dump target: journal burst.** On flush, re-emit the buffered
  records into journald tagged `event=flightrec.dump`, right after
  the triggering WARNING — reuses the 200 MB journald cap (retention)
  + `fetch-pi-logs.sh`; DEBUG context lands in the same timeline as
  the anomaly. (Target stays pluggable so dump-files can be added
  later.)
- **Triggers:** automatic on any WARNING/ERROR (built into
  `flushLevel`) — which already covers supervisor restart decisions
  (`event=shairport.wedge_detected`, `event=system_supervisor.userspace_wedge`),
  since those log ERROR — plus explicit `dump(reason)` from the
  `flag_recent_issue` voice tool, and a manual `systemctl kill -s
  USR1 <unit>` for an operator. (A doctor-fail auto-trigger was
  considered and **dropped** in review: it sent SIGUSR1 to all three
  daemons on every failing doctor run — high blast radius, low
  marginal value over the WARNING auto-flush, and a daemon-kill
  hazard if the handler were ever missing. The SIGUSR1 handler is
  installed *unconditionally* so an unhandled signal can't terminate
  a daemon.)
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

*Cost (measured).* The ring stores **formatted strings**, not
`LogRecord` objects, so RAM is bounded by line length and never pins a
large object passed as a log arg. At the default N=1000: ~0.3 MB/daemon,
~0.9 MB across voice+aec+control — under ~0.1% of a 1 GB Pi. Tunable
(capacity) and off-switchable (`JASPER_FLIGHT_RECORDER=disabled`). (An
earlier draft stored `LogRecord` objects — ~1.3 MB/daemon and an
unbounded tail if a hot DEBUG line logged a big object; the string store
removed both.)

*CPU caveat (hot paths).* Pinning the `jasper` logger at DEBUG means
`logger.isEnabledFor(DEBUG)` is **always True** for `jasper.*` — so the
usual cheap-guard idiom no longer short-circuits a per-frame
`logger.debug(...)` on a hot audio path (it builds a record + a string
every frame). There is none today (checked: `aec_bridge.py` /
`voice_daemon.py` only log DEBUG on error/status-change paths), and a
comment at the `install()` site flags it — keep hot-loop logging
coarser than DEBUG or rate-limit it.

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
`dump()` from the `flag_recent_issue` voice tool, plus a manual
`systemctl kill -s USR1 <unit>` for an operator (supervisor restarts
auto-flush — they already log ERROR). The handler is installed
unconditionally so an unhandled SIGUSR1 can't terminate a daemon. Off
via `JASPER_FLIGHT_RECORDER=disabled`. Tests:
`tests/test_flight_recorder.py` plus the flag-dump test in
`test_tools_diagnostic.py`. **Remaining: on-device verification** —
deploy, then confirm a WARNING produces an `event=flightrec.dump`
burst in `journalctl`, and that "flag that" + a doctor FAIL each
trigger one.

**Tier D — considered and removed (2026-05-30).** A one-tap
"Download diagnostics" button (GET `/diagnostics-bundle` →
`pi-bundle.sh` tarball) was built, then removed in review. For a
maintainer-operated household speaker it added little over the
existing SSH flow (`scp pi@jts.local:/tmp/jasper-bundle-*.tar.gz`)
while widening the surface: any LAN device behind the management
guard could pull all logs + config in one shot, the redaction is
*name-based* (misses inline secret **values** and non-secret-but-
private fields — home coords, SSID, HA URL), and the flight recorder
now puts more DEBUG into the journal that such a bundle would ship.
`scripts/pi-bundle.sh` stays the SSH-only diagnostics path it always
was; the `/system` "Run diagnostics" button (read-only
`jasper-doctor`, no config/logs) stays. Revisit (with value-level
redaction) only if JTS ships to households the maintainer can't SSH
into.

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
