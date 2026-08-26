# HANDOFF — Multi-device peering

When a household runs multiple JTS speakers on the same LAN, all of them hear
the same "Hey Jarvis" and — without coordination — all of them answer at
once. **Peering** picks exactly one winner per wake event and suppresses the
rest. It is **off by default**; the user flips it on at
`http://jts.local/rooms/`.

Read this before modifying `jasper/peering/` or the wake-handler integration
in `jasper/voice_daemon.py`. The two decisions behind the design are
[ADR-0127](adr/0127-wake-arbitration-is-hubless-and-costs-a-solo-speaker-nothing.md)
(hubless P2P with a deterministic pure ranking function; a solo speaker pays
nothing) and
[ADR-0128](adr/0128-peering-fails-open-so-arbitration-can-never-silence-a-speaker.md)
(every arbitration failure resolves to WIN).

## Architecture

```
   Pi A (room=living, primary=1)         Pi B (room=bedroom)
   ┌────────────────────────────┐        ┌────────────────────────────┐
   │  jasper-voice              │        │  jasper-voice              │
   │  WakeLoop                  │        │  WakeLoop                  │
   │   └─ _peer_arbitrate ──┐   │        │   └─ _peer_arbitrate ──┐   │
   │                        │   │        │                        │   │
   │  jasper-control        ▼   │        │  jasper-control        ▼   │
   │   ├─ HTTP :8780            │        │   ├─ HTTP :8780            │
   │   └─ peering daemon ◀──┐   │        │   └─ peering daemon ◀──┐   │
   └────────────────────────┼───┘        └────────────────────────┼───┘
                            │ UDS /run/jasper-control/peering.sock│
                            ─────────────────────────────────────
                              voice ↔ peering RPC (per-Pi local)

                            │ mDNS-SD `_jasper-peer._udp`         │
                            ─────────────────────────────────────
                              peer discovery (cross-LAN)

                            │ Multicast 239.192.0.1:5354 TTL=1    │
                            ─────────────────────────────────────
                              arbitration messages (cross-LAN)
```

The peering daemon's multicast socket and state machine come up as soon as
`mode=on`; the mDNS advertisement/browsing is what gates whether a sibling is
ever actually seen. Multicast carries five JSON message types (`WAKE`,
`CLAIM`, `HEART`, `END`, `HELLO`), max ~300 bytes each.

## Module layout

Separated by I/O profile so each piece is independently testable.

| File | Purity | What it does |
|---|---|---|
| [config.py](../jasper/peering/config.py) | pure | `PeeringConfig` + env-file loader. Owns `peer_id` idempotency, mode parsing, room-name derivation, and every default/clamp below. |
| [rank.py](../jasper/peering/rank.py) | pure | `WakeReport` + `rank(reports)` — the deterministic winner pick. Its module docstring owns the tier cascade and why each eps is what it is. |
| [state.py](../jasper/peering/state.py) | pure | `PeeringStateMachine` — `Event` in, `Action` out, no I/O. Five states: IDLE / CANDIDATE / WINNER / ACTIVE / SUPPRESSED. |
| [transport.py](../jasper/peering/transport.py) | I/O | Multicast UDP socket + JSON codec for the five message types. |
| [avahi.py](../jasper/peering/avahi.py) | I/O | Renders `/etc/avahi/services/jasper-peer.service` from the template. Installs on `mode=on`, uninstalls on `mode=off`. |
| [discovery.py](../jasper/peering/discovery.py) | I/O | `AsyncZeroconf` browse of `_jasper-peer._udp`. Lazy-imported so zeroconf never loads when peering is off. |
| [uds.py](../jasper/peering/uds.py) | I/O | Unix-socket server for voice → peering RPC (the existing newline-ASCII + JSON protocol). |
| [daemon.py](../jasper/peering/daemon.py) | I/O | asyncio orchestrator. Translates state-machine `Action`s into I/O. **No business logic** — that all lives in `state.py`. |

**Integration points** outside the package:

| File | What it adds |
|---|---|
| [config.py](../jasper/config.py) | `Config.peering_enabled` + `Config.peering_uds_socket`, from `JASPER_PEERING`. |
| [voice_daemon.py](../jasper/voice_daemon.py) | `_peer_arbitrate`, `_peering_send`, `_notify_peering_session_started/_ended`, `_wake_late_cancelled`, `_frame_rms_dbfs`; `_handle_wake_frame` spawns `_arbitrate_acquire_drain` as a background task. |
| [control/server.py](../jasper/control/server.py) | `start_peering_daemon_if_enabled()` — a background thread with its own asyncio loop iff `JASPER_PEERING=on`; no-op when off. |
| [cli/doctor/](../jasper/cli/doctor/__init__.py) | `check_peering_mode` (env-file sanity) and `check_peering_discovery` (sibling count via `avahi-browse`). |
| [web/rooms_setup.py](../jasper/web/rooms_setup.py) | The canonical toggle. `POST /peering` writes `/var/lib/jasper/peering.env` through `jasper.peering.config` and restarts voice + control. |
| [deploy/avahi/jasper-peer.service.template](../deploy/avahi/jasper-peer.service.template) | mDNS service template with `__PEER_ID__` / `__ROOM__` / `__PRIMARY__`, rendered at runtime. |
| [deploy/install.sh](../deploy/install.sh) | `install_peering_template()` — installs the template, generates the stable `peer_id` UUID. |
| [jasper-voice.service](../deploy/systemd/jasper-voice.service) + [jasper-control.service](../deploy/systemd/jasper-control.service) | **Both** must source `EnvironmentFile=-/var/lib/jasper/peering.env` so `JASPER_PEERING` reaches each `Config`. `jasper-control.service` must also grant `ReadWritePaths=/etc/avahi/services`, because peering renders the advert under `ProtectSystem=full`. Guarded by `tests/test_peering_plumbing.py` + `tests/test_control_systemd.py`. |

`/peers/` — a standalone wizard on port 8776 — is **deleted**: no route, no
redirect, no socket, no page CSS. `/rooms/` is the only user-facing peering
surface, and a stale bookmark should fail rather than keep a second surface
alive.

## Wake event end-to-end

Peering-on path, from "Hey Jarvis" to "winner answers". The only tuned number
is the 150 ms arbitration window.

1. openWakeWord fires on every Pi that heard the utterance. Each WakeLoop
   computes its score, derives `can_serve` from spend cap + paused state,
   sets `_acquiring` (the main mic loop now buffers frames into
   `_acquire_buffer`, capped at 20 s), spawns `_arbitrate_acquire_drain` as a
   background task, and **returns to draining new frames**.
2. The background task checks `_wake_late_cancelled()` — user mute, or an
   open room-correction window.
3. `_peer_arbitrate()` makes the UDS call to `jasper-control`
   (`ARBITRATE {score, snr, rms, can_serve}`). The daemon multicasts `WAKE`,
   schedules the arbitration timer, collects peer `WAKE`s, and on expiry runs
   `rank()` over the collected reports.
4. **WIN** → the daemon multicasts `CLAIM` and returns `{result, epoch}`.
   **LOSE** → the task logs `event=peering.wake.lost` and returns silently;
   `finally` clears `_acquiring`, drops the buffer, and sets the refractory.
5. On WIN: re-check the late-cancel gates, re-check the spend cap (playing
   `cant_connect` if it now blocks), fire the chirp, `_begin_turn()` opens
   the LLM session, `_notify_peering_session_started` puts the daemon into
   heartbeat broadcast, and `_acquire_buffer` drains into the session in FIFO
   order before live frames take over.
6. On session end, `_end_turn` notifies peering, which multicasts `END`;
   peers' SUPPRESSED state clears.

**Correctness properties that constrain edits here:**

- **The main mic loop never blocks on peering.** The arbitration round-trip
  has a 500 ms hard ceiling and runs in its own task, so the loop keeps
  iterating and the watchdog keeps being patted.
- **Fail-open on every peering error** (ADR-0128) — missing UDS, timeout, or
  a malformed response all return WIN.
- **Late-cancel re-runs after arbitration**, because up to 500 ms passed.
- **Losers stay silent**: no chirp on LOSE.

## The state machine

[state.py](../jasper/peering/state.py), five states, pure and event-driven;
the daemon translates the returned `Action`s into I/O. Tests
([test_peering_state.py](../tests/test_peering_state.py)) drive synthetic
event sequences — no sockets, no timers, no asyncio.

```
                       ┌─────────────────────────────────────┐
                       │                IDLE                  │
                       │  (peering on, no in-flight wake)     │
                       └──┬──────────────────────┬────────────┘
        local wake fires  │                      │  peer CLAIM seen
                          ▼                      ▼
                  ┌──────────────┐         ┌──────────────┐
                  │  CANDIDATE   │         │  SUPPRESSED  │
                  │ collect 150ms│         │  (foreign    │
                  │  WAKEs       │         │   session)   │
                  └──┬───────────┘         └──┬───────┬───┘
          window      │                       │       │
          elapses     │                       │       │ HEART missed 2 s
        ┌─────────────┴───────────┐           │       │ OR END seen
        │                         │           │       ▼
        ▼                         ▼           │  (back to IDLE)
   ┌─────────┐               ┌─────────┐      │
   │ WINNER  │               │  LOSER  │      │ local wake above
   │ (CLAIM) │               │ → IDLE  │      │ break_threshold
   └────┬────┘               └─────────┘      │
        │                                     └──► CANDIDATE (contest)
        │ session opens (voice notifies)
        ▼
   ┌─────────┐  HEART every 1 s while in this state
   │ ACTIVE  │
   └────┬────┘
        │ session ends (silence detector, spend, user END)
        ▼
    send END, → IDLE
```

**Heartbeat semantics.** A winner in ACTIVE multicasts `HEART` every 1 s;
SUPPRESSED peers reset a 2 s timeout on each one. If the winner crashes
mid-session the heartbeats stop, the timers fire, and the suppressed peers
return to IDLE within ~2 s — the next wake picks a fresh winner with no
double response.

**Stickiness.** A foreign session in flight keeps IDLE peers out of
CANDIDATE on their own wakes *unless* the local score exceeds
`break_threshold`, which breaks suppression and starts arbitration fresh. So
the user can grab a different speaker by speaking directly to it, while a
faint far-room false-fire cannot interrupt a live conversation.

## Configuration

| Path | Default | Purpose |
|---|---|---|
| `/var/lib/jasper/peering.env` | absent | wizard-managed. Absent → `JASPER_PEERING=off`. |
| `/var/lib/jasper/peer_id` | UUID generated at install | stable per-Pi identity across reboots and re-installs. **Never user-edited.** |
| `/etc/jasper/avahi-templates/jasper-peer.service` | installed by `install.sh` | the advert template. |
| `/etc/avahi/services/jasper-peer.service` | absent until `mode=on` | the rendered advert. **Its presence is what makes this Pi visible to siblings.** Written by `jasper-control`, hence that unit's `ReadWritePaths`. |
| `/run/jasper-control/peering.sock` | runtime | `jasper-control` owns the server side. Keep it under `RuntimeDirectory=jasper-control` — the non-root service user cannot bind under `/run/jasper`. |

Env vars, all parsed and clamped in
[peering/config.py](../jasper/peering/config.py):

- `JASPER_PEERING` — `off` (default) or `on`; anything else parses to `off`.
- `JASPER_PEER_PRIMARY` — `1` marks the household primary, a bias inside the
  confidence band, never an absolute override.
- `JASPER_PEER_ROOM` — legacy room label kept as a data-compatibility
  fallback (derived from hostname: `jts-bedroom` → `bedroom`, bare `jts` →
  `default`). The `/speaker/` identity page owns the room label in current
  builds.
- `JASPER_PEER_ARB_WINDOW_MS` — arbitration collection window, default 150,
  clamped to [50, 500].
- `JASPER_PEER_BREAK_THRESHOLD` — local score needed to break suppression
  mid-session, default 0.85, clamped to [0.5, 0.99].

`/rooms/` writes `JASPER_PEERING` and `JASPER_PEER_PRIMARY`; the two tuning
vars are operator-managed by hand and web saves preserve them.

## Operations

**Turning it on.** `http://jts.local/rooms/` → the Wake response card. Save
triggers `systemctl --no-block restart jasper-voice jasper-control`; allow
~3–5 s before peers see this Pi in their directory.

**Verifying.**

```sh
# Doctor shows mode + sibling count
sudo /opt/jasper/.venv/bin/jasper-doctor | grep peering

# Live multicast traffic when sessions are active
sudo journalctl -u jasper-control -f | grep -E "event=peering"

# Active sessions / current state
curl -s --unix-socket /run/jasper-control/peering.sock - <<< "STATUS"
```

`peering: mode` parses the env file — `ok` for both `off` and `on`, `warn`
only on a malformed value (which resolves to `off` silently, and the operator
probably wants to know). `peering: discovery` runs `avahi-browse -rt
_jasper-peer._udp`, filters out our own id, and reports the sibling count.

**Two-Pi smoke test (the ship gate).** Toggle peering on at both; each should
see the other within ~30 s; stand between them and ask a question; **exactly
one** answers; `journalctl -u jasper-voice | grep peering` shows
`event=peering.wake.won` on one and `event=peering.wake.lost` on the other,
with both scores in the log lines.

**Logging.** Every wake emits structured `event=peering.*` lines
(`discovery.peer_seen`, `wake.won`, `wake.lost`,
`session.heartbeat_missed`, `foreign.ended`);
`scripts/jasper-trace.sh` filters journals down to `event=` lines.

## Cost when off

| Resource | Peering off | Peering on |
|---|---|---|
| `jasper-control` peering thread | not started (returns early, before importing anything) | started, owns its own asyncio loop |
| zeroconf module | not imported | imported lazily (~5–8 MB Pss) |
| Multicast UDP socket | not opened | bound to 239.192.0.1:5354 |
| Avahi service file | not installed | rendered from template, Avahi reloaded |
| `_peer_arbitrate` | returns WIN immediately, no UDS call and no loop yield | UDS call, up to 500 ms |
| `_notify_peering_session_*` | no-op | UDS call |
| `_frame_rms_dbfs` (per wake) | still computed (~80 µs) | same |
| `_arbitrate_acquire_drain` | runs, every peering check short-circuits | full path |

Net cost for a single-Pi household: no observable difference from before
peering existed. Pinned by
`test_peer_arbitrate_disabled_returns_win_without_io`.

## Known gaps

- **The concurrent multi-waker race** — two speakers waking on the same
  utterance each mint their own epoch, and convergence is best-effort, not
  proven. See ADR-0127; no two-Pi hardware repro yet.
- **SNR is never populated.** `_frame_rms_dbfs` sends RMS but `snr_db=None`
  always — a real value needs a rolling noise-floor estimator we do not
  track. With SNR uniformly absent the SNR tier is a no-op and ranking falls
  through to RMS; usually the confidence band has already decided.
- **Per-peer mic gain calibration** is unimplemented. Ranking assumes
  openWakeWord confidence is gain-invariant, which holds well enough across
  identical XVF3800 hardware but not across a mixed fleet.
- **No tests on a real LAN.** Every test mocks the transports; the cross-LAN
  multicast path has never been exercised on hardware.
- **Multicast on consumer mesh networks** may be dropped or rate-limited,
  with no unicast fallback wired up (ADR-0127).

The dropped-`import` incident that took `jasper-web` down after the `/peers/`
wizard landed is why [test_web_main_imports.py](../tests/test_web_main_imports.py)
exists: it pins that every referenced `*_setup` module is imported, that
`WIZARD_SPECS` has unique routes/env-vars/ports, and that `ruff --select=F821`
is clean across the whole peering surface.

**External design references** consulted during the original research: Sonos
US10181323 (confidence-broadcast arbitration), Apple AU2016410253B2 (P2P BLE
arbitration), Amazon ICASSP 2022 (arXiv 2112.04914, end-to-end device
arbitration), RFC 2365 (admin-scoped multicast — where 239.192.0.0/14 comes
from), RFC 6762 (mDNS), python-zeroconf (browse-only; Avahi remains the only
mDNS responder on the host).

Last verified: 2026-08-26 (spine trim: module layout, integration points,
every default and clamp, the heartbeat interval/timeout and the fail-open
paths re-read against `jasper/peering/{config,rank,state}.py`,
`jasper/voice_daemon.py` and `tests/test_web_main_imports.py`. `snr_db` is
confirmed still hard-coded `None` at the one call site, and `/peers/` is
confirmed absent from `jasper/web/` and the nginx confs. The tier cascade and
its eps rationale were deleted here rather than restated — `rank.py`'s module
docstring owns them. Prior 2026-06-26: JTS4 streambox peering enable
re-verified — `/rooms/peering` writes `JASPER_PEERING=on`, `jasper-control`
starts the daemon, the UDS lives under `/run/jasper-control` so the non-root
service can bind it, and `jasper-control.service` allows
`/etc/avahi/services` writes under `ProtectSystem=full`.)
