# HANDOFF — observability and debug mode

How JTS logging and diagnostic capture work today: the always-on floor, the
temporary verbosity toggle, and the bounded artifacts. Read
[HANDOFF-resilience.md](HANDOFF-resilience.md) first — this sits on top of that
ladder and does not restate it.

The shape is
[ADR-0143](adr/0143-observability-has-three-planes-and-debug-verbosity-is-additive-only.md)
(three planes; debug is additive only) and
[ADR-0144](adr/0144-diagnostics-leave-the-box-over-ssh-not-over-the-lan.md)
(no LAN diagnostics bundle). The May-2026 build record and cohort survey are in
[`historical/observability-design-2026-05.md`](historical/observability-design-2026-05.md).

## The `event=` spine

Cross-daemon state changes emit `event=<name> key=val …` lines
(`event=shairport.wedge_detected`, `event=system_supervisor.userspace_wedge`,
`event=wifi_guardian.recreate_ok`, `event=duck`,
`event=fanin.assistant_loudness`, …). `scripts/jasper-trace.sh` keys off them.
They are the cheap, high-signal, always-on observability floor — keep them.

**Emit through `jasper.log_event.log_event`, never a hand-written f-string.**
[`jasper/log_event.py`](../jasper/log_event.py) is the one renderer for the
spine. It is byte-identical to a hand-written line for clean values, but it
**logfmt-quotes/escapes** any value containing an ASCII space, `=`, a quote, a
backslash, any ASCII control (C0 plus DEL), NEL, or U+2028/U+2029. Backslash,
quote, newline, carriage return, and tab render as `\\`, `\"`, `\n`, `\r`,
`\t`; remaining controls and separators as literal `\uXXXX`. An untrusted field
(SSID, USB descriptor, Bluetooth/mDNS name, HA error body, free-text reason)
therefore cannot corrupt the `key=val` parse or create a second physical
journal line.

Mechanics: `level=logging.WARNING` sets severity; `exc_info=True` attaches a
traceback (the `logger.exception("event=…")` equivalent); a field whose name
collides with a reserved param (chiefly `level`, the volume level) or is not a
valid identifier (`from`) rides the explicit `fields={…}` mapping.
`JASPER_LOG_JSON=1` switches on an opt-in JSON sink for machine consumers.

A conventions guard
([`tests/test_log_event_conventions.py`](../tests/test_log_event_conventions.py))
fails CI on any new hand-written `logger.<level>("event=…")` call. Its
`DEFERRED_ACTIVE_ZONE` list — files left hand-written so the migration does not
churn a parallel work-stream's edits — is the **authoritative inventory**; a
staleness test fails CI if a listed file no longer has a hand-written line, so
the list cannot silently rot. To finish one: migrate its calls using the
fidelity rules (byte-identical for clean values; a legacy `%s` that can receive
a bool or `None` → `str()` so its spelling stays `True`/`False`/`None`;
`%r` → `repr()`; precision specs → pre-rendered f-strings; trailing prose → a
`note=` field; a field named `level` → the `fields=` mapping), then delete that
file's entry so the guard starts enforcing it.

Subsystem event catalogs live with their subsystem, not here — the
active-crossover commissioning lifecycle names, their common fields, and the
reserved-but-unemitted set are owned by
[`active-crossover-information-design.md`](active-crossover-information-design.md)
"Structured events" and `commissioning_capture.RESERVED_CROSSOVER_EVENTS`.

## Logging shape

Each long-running daemon (`jasper-voice`, `jasper-control`,
`jasper-aec-bridge`, `jasper-mux`, the renderers, and profile-gated adapters
such as `jasper-wiim-remote-mic`) calls `logging.basicConfig` once at startup
with a hardcoded `INFO` level and the format
`%(asctime)s %(levelname)s %(name)s: %(message)s`. There is no shared logging
module and no `dictConfig`. Beyond the Debug card there is **no general runtime
log-level knob**: `JASPER_LOG_LEVEL` reaches only one idle wizard
(`jasper/web/speaker_setup.py`), not the daemons. The level is read once at
startup, which is why the Debug card applies via a daemon restart (or, for
control, in-process).

### The heartbeat-vs-forensic split

The resilience layer is already disciplined about steady-state noise: the
shairport and system supervisors log **nothing on the healthy path** (one
`event=*.start` per boot, then silence until a failure); the Tier-1
`Heartbeat.bump()` logs nothing per frame; the AEC reconciler, WiFi guardian,
and WiFi recover timer are oneshot paths whose scripts emit no `event=` line on
a healthy tick (systemd still records ~2 activation lines per tick, kept low by
the ~3-min cadence).

Every recovery/decision line is WARNING or ERROR. That gives the split a debug
toggle can rely on:

- **Forensic — must always persist.** Every WARNING+/`event=` recovery,
  probe-fail, wedge, restart/reboot decision, `stash_stale`/`recreate_*`, the
  Tier-1 `heartbeat suppressed` breadcrumb, the bridge `BridgeStalled` warning.
  You get **one shot** at these when a rare failure fires. Never suppress.
- **Heartbeat / chatty — safe to quiet.** A small set of always-on INFO
  emitters, below.

**Steady-state verbosity hotspots** (from real Pi logs, music playing,
~110 lines/min combined):

| Source | Volume | Control point | Note |
|---|---|---|---|
| shairport PTP anchors | ~40/min (55% of shairport output) | `log_verbosity = 2` in `deploy/shairport-sync.conf.template` | **Intentional** — open AP2 "Pattern E" hunt ([HANDOFF-airplay.md](HANDOFF-airplay.md)). Do **not** lower until that bug closes. |
| AEC bridge `rms over` line | 1 / 5 s, always-on | the hardcoded `now - last_log > 5.0` gate in `aec_bridge.py`'s AEC loop | **Load-bearing** — `jasper-doctor` parses it from the journal continuously, so demoting it blinds the AEC health check. Manage via the flight recorder, not demotion. |

Assistant loudness is one `event=fanin.assistant_loudness` line per
assistant/cue segment plus fan-in `STATUS` telemetry under
`tts.assistant_loudness` — load-bearing without being journal spam. The
low-level `tts gain set` echo in `audio_io.py` is DEBUG.

## Persistent journald and its retention window

`deploy/journald/50-jts-persistent-storage.conf` sets `Storage=persistent`
capped at `SystemMaxUse=500M` so a watchdog reset's *previous-boot* logs
survive — the whole point of Tier 5 forensics. Cost per
[HANDOFF-resilience.md](HANDOFF-resilience.md): ~30 MB/hr → ~270 GB/yr against
~100 TBW SD endurance, **not a flash-wear emergency**. `SystemMaxUse` is a
retention ceiling, not a write-rate knob — a larger cap adds disk, not wear.

Global journald `RateLimit*` settings stay at systemd defaults. Two external
log sources have narrow per-unit overrides: `jasper-camilla.service`
(`LogRateLimitBurst=120` per 60 s), because CamillaDSP emits an unstructured
ALSA short-read WARN many times per second when the capture graph is degraded,
and `jasper-snapclient.service` (`LogRateLimitBurst=30` per 60 s), because an
optional bonded follower logs a connection-refused loop while its leader is
offline. journald still records the first burst and its suppression summary.

**The cap is also the retention window, so forensics have a volume-dependent
shelf life.** journald vacuums oldest-first: heavy log volume silently eats the
boot-time entries Tier 5 forensics depend on. Observed 2026-06-11 on jts3 under
lab-grade multiroom logging (at the earlier 200 MB cap) the journal sat at
188 MB and the *current* boot's first surviving entry was ~5 h after boot —
`event=bootloop_guard.ok` from 15 h earlier was already gone while the unit's
exit status showed it had run fine. The 500 MB cap widens that window ~2.5× at
the same volume; the failure mode is unchanged in principle. **Before treating
a missing journal line as "never happened", check `journalctl --list-boots`
first-entry timestamps** — `/state.resilience.*` and unit exit status are the
durable surfaces. A household speaker's far lower volume keeps a much longer
window; this bites lab Pis first.

## Production-plane surfaces

- **Resilience without logs.** `curl -s http://jts.local:8780/state | jq
  .resilience` (`shairport`, `grouping_supervisor`, `system_supervisor`,
  `wifi_guardian`), plus the doctor's supervisor-snapshot check, which reads
  the same snapshots and warns when a supervisor is kicking, rate-limited, or
  failing to converge. `/state.resilience.multiroom_cascade` is a bounded
  in-memory ring sourced from persistent journald (`multiroom.reconcile.*`,
  `restart_broker.*`, `grouping_supervisor.*`), classified into recent
  restart-cascade events so an operator can reconstruct "what restarted what,
  when" from `/state`. It is production truth, not a log bundle: small deque,
  fixed shape, a bounded 15-minute startup lookback, journal `occurred_at`
  preserved separately from sampler `observed_at`, and fail-soft to an
  empty/disabled/null snapshot.
- **Output hardware without probing audio.**
  `jasper-audio-hardware-reconcile` writes
  `/run/jasper-output-hardware/output_hardware.json` and logs
  `event=audio_hardware_reconcile.state_written` after each install/boot/udev
  pass. `/state.audio.output_hardware`, `/sound/output-topology`, and
  `jasper-doctor` all read that one artifact, so diagnostics can distinguish
  the active runtime role from the best observed physical shape.
- **Audio health** is one normalized, cached surface:
  [`jasper/control/audio_health.py`](../jasper/control/audio_health.py)
  composes the bounded AirPlay collector, a local outputd `STATUS` read, and a
  slow route/transport/artifact assessment into `/state.audio_health` and
  `/system/snapshot.audio_health`. Its household rendering is owned by
  [HANDOFF-management-ui.md](HANDOFF-management-ui.md). Four rules keep it from
  growing a second probe cadence or a second opinion, and are the ones to
  preserve when editing it:
  - **One sampler thread**, replacing the former standalone AirPlay sampler.
    Opening `/system/audio/` adds no resident worker and no probe work. Fast
    cadence is fixed-shape local UDS state; journal, MPRIS, Camilla, and
    route-artifact work stays on slower bounded cadences.
  - **Mux is the single owner of activity predicates** (AirPlay, USB, Spotify,
    Bluetooth). A lane becomes a current stream only when
    `jasper-mux STATUS.sources[<id>].playing` agrees, so free-running silent
    lanes are not fake sessions. Missing or unreadable mux state **fails
    closed** as "Playback activity unavailable" and preserves an
    already-observed session; it is never presented as healthy idle.
  - **The override chain refines, it does not stack opinions.** A stopped
    CamillaDSP reaches `overall` (the signal path cannot see it: fan-in and
    outputd both keep looping when the stage between them disappears, so a
    clean-path headline beside a stopped-processing incident would be an
    affirmative wrong answer); `jasper-outputd` and `jasper-voice` are excluded
    because they park `inactive` **by design**. The undeclared-hardware
    override (#2812) runs last and replaces only the two generic outputd
    shapes (`_UNDECLARED_OUTPUT_CODES`), requires both the reconciler's
    adoption gate and a genuine
    declared-topology mismatch, and rewrites the matching `path.*` incident row
    with identical wording so history and headline cannot disagree.
  - Incident lifecycle lives in
    [`jasper/control/audio_incidents.py`](../jasper/control/audio_incidents.py):
    ongoing conditions cannot be evicted by recovered blips, recovered entries
    coalesce, the browser shows at most five rows, and freeze frames are
    normalized/allowlisted and persisted atomically into a bounded
    `/var/lib/jasper/audio_health_incidents.json` ring (20 records, 128 KiB
    read cap) only at an incident transition. Corrupt, oversized, symlinked, or
    newer-schema input fails soft.

## The Debug card

A collapsed **Debug logging** card on `/system` expands to one checkbox per
subsystem — **voice**, **aec**, **control**, the three daemons with a clean
`basicConfig` seam. Each toggle raises that daemon's `jasper` logger to DEBUG.

- **SSOT:** [`jasper/debug_mode.py`](../jasper/debug_mode.py) reads
  `/var/lib/jasper/debug.env` fresh (pure resolver + `apply_for`, daemon-side,
  no web import). Each daemon calls `apply_for("<id>")` right after
  `basicConfig` and reads the file directly — no systemd `EnvironmentFile` and
  no install.sh seeding; a missing file resolves to a safe "off". Adding a
  subsystem is a row in `SUBSYSTEMS` plus an `apply_for` call.
- **Write / restart / expiry:**
  [`jasper/control/debug_control.py`](../jasper/control/debug_control.py) lives
  in jasper-control (long-lived) — it must, because the `/system` page server
  (:8772) idle-exits after 30 min and cannot own the timer. `set_debug` writes
  `debug.env` atomically, then applies per subsystem policy: always-on daemons
  restart; control applies **in-process** (a self-restart would drop the
  request and the timer). jasper-control runs non-root, so the restart it
  issues is polkit-authorized against the `MANAGED_UNITS` allowlist — see
  [HANDOFF-privilege-separation.md](HANDOFF-privilege-separation.md).
- **Endpoints:** `GET`/`POST /debug` on jasper-control (:8780), reached from
  the card via a dedicated `location /debug` nginx block (mirroring `/mic`,
  `/volume`); also surfaced at `/state.debug`.
- **Auto-expiry:** one shared TTL (2 h, re-armed per change), enforced twice.
  Each daemon self-quiets in process — `apply_for` arms a `threading.Timer`
  that drops its journal handler back to INFO with **no restart** — while a
  separate timer in control clears the `debug.env` SSOT, reconciled on control
  startup. The card shows a live countdown.
- **Additive-only**, floored at WARNING (ADR-0143): the toggle can only raise.
- **UI:** [`debug-card.js`](../deploy/assets/system-status/js/debug-card.js) —
  own fetch, client-side countdown, `h()`-escaped, confirm before the restart.

USB input is **not** a debug subsystem: its readiness unit has no resident
process; inspect fan-in `STATUS` and the usbsink doctor group instead. The USB
gadget forensics card samples controller counters, not daemon logs, and does
not extend this registry — its limits and retention are in
[HANDOFF-usb-gadget.md](HANDOFF-usb-gadget.md#opt-in-rolling-usb-forensics).

## The flight recorder

[`jasper/flight_recorder.py`](../jasper/flight_recorder.py) keeps a bounded
in-RAM verbose ring per daemon and dumps it **only** on an anomaly — the answer
to the central tension, that the intermittent bugs which matter most already
happened before anyone could flip a toggle.

- **Shape:** the `jasper` logger sits at DEBUG always; the journal handler
  stays at INFO (DEBUG while the Debug card is on), so journal volume is
  unchanged; `RingFlushHandler` buffers the last N **formatted strings** and
  flushes on WARNING+ or on demand.
- **Triggers:** automatic on any WARNING/ERROR (which already covers supervisor
  restart decisions, since those log ERROR), explicit `dump(reason)` from the
  `flag_recent_issue` voice tool, and `systemctl kill -s USR1 <unit>` for an
  operator. The SIGUSR1 handler is installed **unconditionally** so an
  unhandled signal cannot terminate a daemon.
- **Output:** a tagged burst re-emitted into journald as `event=flightrec.dump`
  … `event=flightrec.dump.end`, right after the triggering WARNING, so DEBUG
  context lands in the same timeline. `scripts/fetch-pi-logs.sh` also writes
  `log-noise-summary-latest.txt` with line counts and repeated-message
  fingerprints, so a noisy bundle can be triaged without runtime machinery.
- **Scope and cost:** voice + aec + control. `DEFAULT_CAPACITY = 1000` stores
  formatted lines (~0.3 KB each) → ~0.3 MB/daemon, ~0.9 MB total, ~0.1 % of a
  1 GB Pi. Off via `JASPER_FLIGHT_RECORDER=disabled`.
- **CPU caveat.** Pinning the `jasper` logger at DEBUG means
  `logger.isEnabledFor(DEBUG)` is always True for `jasper.*`, so the cheap-guard
  idiom no longer short-circuits a per-frame `logger.debug(...)` on a hot audio
  path. There is none today, and a comment at the `install()` site flags it —
  **keep hot-loop logging coarser than DEBUG, or rate-limit it.**

The payoff is that new verbose instrumentation can live at DEBUG: quiet in the
journal during healthy playback, still captured in RAM and dumped around
related anomalies. Keep low-volume reconstructive `event=` decisions at INFO
unless they become steady-state spam — and note the AEC `rms over` line must
stay INFO, because the doctor reads it *continuously*, which a dump-on-anomaly
model cannot serve.

Last verified: 2026-08-26 (triage pass — the three-plane boundary, the
`log_event` quoting/JSON contract and its conventions guard, per-daemon
`basicConfig`, the journald `Storage=persistent`/`SystemMaxUse=500M` conf and
both per-unit `LogRateLimit*` overrides, the Debug card's three-subsystem
registry and 2 h TTL, and the flight recorder's capacity, dump events, and
disable switch all rechecked against their owning files. The active-crossover
event catalog was deleted as a duplicate of
`active-crossover-information-design.md` "Structured events"; the deferred
log_event inventory now points at its machine-enforced list instead of copying
it. Audio-health prose compressed to its invariants — the module and
`HANDOFF-management-ui.md` own the rest. Tier A/B/C/D build record and cohort
survey moved to `historical/observability-design-2026-05.md`; the decisions
became ADR-0143 and ADR-0144.)
