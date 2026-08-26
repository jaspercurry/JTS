# Observability tiers — design record (2026-05) — historical

> **Status: historical.** The build record and cohort survey behind the debug
> card and the flight recorder, frozen as written in May 2026. The shipped
> behaviour is [HANDOFF-observability.md](../HANDOFF-observability.md); the
> decisions are
> [ADR-0143](../adr/0143-observability-has-three-planes-and-debug-verbosity-is-additive-only.md)
> and [ADR-0144](../adr/0144-diagnostics-leave-the-box-over-ssh-not-over-the-lan.md).
> Nothing here is current operational truth.

## Tier A — INFO demotions (2026-05-30, rechecked 2026-06-01)

Code review found the redundant `tts gain set` echo in `audio_io.py`
(`TtsPlayout.set_gain_db`) safe to demote to DEBUG. The AEC `rms over` line was
kept at INFO because `jasper-doctor`'s `_assess_aec_bridge_output` parses it
continuously. The old voice-side `event=tts_gain.compute` line was removed when
assistant loudness ownership moved into the audio mix owner; its replacement,
`event=fanin.assistant_loudness`, is lower-volume structured decision telemetry
and stayed INFO.

## Tier B — the Debug card (2026-05-30)

One checkbox per subsystem on `/system`, each raising that daemon's `jasper`
logger to DEBUG. Shairport's config-file `log_verbosity` and mux's
`--log-level` were deferred as a different mechanism.

Restart-to-apply was the accepted MVP; a hot SIGHUP re-read was noted as a
possible follow-up, with the observation that restarting a daemon to *start*
debugging a live issue is mildly self-defeating — which is what motivated Tier
C. The write/expiry owner was placed in `jasper-control` because the `/system`
page server (:8772) idle-exits after 30 minutes and cannot own a TTL timer.

## Tier C — the flight recorder (2026-05-30)

*Mechanism.* A custom `logging.Handler` over a `deque`. The stdlib
`MemoryHandler` was evaluated and rejected: it flushes on capacity and routes
through a target handler whose INFO level would drop the buffered DEBUG lines.

| Component | Level | Effect |
|---|---|---|
| `jasper` logger | DEBUG always | DEBUG records get *created* |
| journal `StreamHandler` | INFO (DEBUG when the Tier-B toggle is on) | journal volume unchanged — DEBUG never hits the SD card |
| `RingFlushHandler` | DEBUG | buffers the last N DEBUG+ records; flushes only on WARNING+/explicit |

```python
class RingFlushHandler(logging.Handler):                   # level = DEBUG
    def emit(self, record):
        self.buffer.append(self.format(record))            # deque(maxlen=N) of STRINGS
        if record.levelno >= logging.WARNING:              #   -> bounded RAM, no arg pinning
            self.flush_buffer("auto:" + record.levelname.lower())
```

*Dump target: journal burst.* Re-emitting into journald tagged
`event=flightrec.dump` reused the 500 MB retention cap and `fetch-pi-logs.sh`,
and put DEBUG context in the same timeline as the anomaly. The target was left
pluggable so dump-files could be added later.

*A doctor-fail auto-trigger was dropped in review*: it sent SIGUSR1 to all three
daemons on every failing doctor run — high blast radius, low marginal value
over the WARNING auto-flush, and a daemon-kill hazard if the handler were ever
missing. That hazard is why the SIGUSR1 handler is installed unconditionally.

*Cost.* An earlier draft stored `LogRecord` objects — ~1.3 MB/daemon and an
unbounded tail if a hot DEBUG line logged a big object. Storing formatted
strings removed both, giving ~0.3 MB/daemon at the default capacity.

*Honest grounding.* The custom handler plus the general pattern (Linux ftrace
snapshot triggers, Android logd, OpenTelemetry tail-sampling, Rust
`tracing-appender`) plus the Pi cohort's RAM-logging consensus (DietPi RAMlog,
log2ram). **No Pi appliance in the comparable cohort ships a structured log
flight recorder** — it was sound-by-analogy, not cohort-corroborated. JTS
already did this for *audio* (the wake-event 6 s pre/post rings); Tier C
generalised it to logs.

## Cohort grounding (2026-05-30)

Validated against Home Assistant OS, balenaOS, piCorePlayer, Volumio, moOde,
OctoPrint, and DietPi:

- A per-subsystem auto-expiring debug toggle and a download-diagnostics button
  are **cohort-standard**. JTS's auto-expiry is a refinement over OctoPrint's
  "we just warn you it's on"; the diagnostics button was declined (ADR-0144).
- A WARN+ floor in persistent journald plus chatty detail in RAM is reasonable
  and slightly *ahead* of the audio-hobbyist tier (Volumio/moOde keep
  persistent logs too), conditional on actually doing the INFO→DEBUG demotion.
  The managed tier (HA OS, balenaOS, piCorePlayer) keeps logging volatile via
  a read-only root.
- **JTS is ahead of the cohort** on watchdog (hardware watchdog plus a
  userspace-liveness supervisor — the gap Poettering's canonical systemd
  writeup says the hardware watchdog cannot cover) and on memory resilience
  (OOMScoreAdjust / zram / MGLRU / cgroups).

**Flagged out of scope, still open:** the cohort's primary durability answer to
JTS's actual past incident (unclean-power ext4 corruption) is a read-only or
overlay root and/or a supercapacitor UPS HAT for graceful shutdown on power
loss. JTS has neither. Not an observability decision, but the
highest-leverage durability gap the research surfaced —
[HANDOFF-resilience.md](../HANDOFF-resilience.md) owns it.

Key sources: Home Assistant [logger](https://www.home-assistant.io/integrations/logger/)
+ [diagnostics](https://www.home-assistant.io/integrations/diagnostics/);
OctoPrint [logging plugin](https://docs.octoprint.org/en/main/bundledplugins/logging.html);
kernel [ftrace snapshot](https://docs.kernel.org/trace/ftrace.html);
[OTel tail-sampling](https://opentelemetry.io/blog/2022/tail-sampling/);
piCorePlayer [RAM-root](https://docs.picoreplayer.org/faq/my_changes_disappeared/);
HA OS [read-only partitions](https://developers.home-assistant.io/docs/operating-system/partition/);
Poettering [systemd watchdog](http://0pointer.de/blog/projects/watchdog.html);
Dzombak [reduce Pi SD writes](https://www.dzombak.com/blog/2024/04/pi-reliability-reduce-writes-to-your-sd-card/).
