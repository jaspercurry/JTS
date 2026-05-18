# HANDOFF — Multi-device peering

When a household runs multiple JTS speakers on the same LAN, all of
them hear the same "Hey Jarvis" and — without coordination — all of
them answer at once. **Peering** is the coordination protocol that
picks exactly one winner per wake event and suppresses the rest.

This doc is the home base for the subsystem. Read it before
modifying anything in `jasper/peering/` or the wake-handler
integration in `jasper/voice_daemon.py`. The pre-existing design
hub at [docs/satellites.md](satellites.md) "Microphone arbitration"
covered the *multi-mic-around-one-Pi* case (one Pi, satellite mics
contributing audio); this doc covers the *multi-Pi* case (N
autonomous JTS speakers, each with its own mic and LLM session).
The two share signal-priority intuition but diverge in nearly
every other dimension.

If you're a fresh context window: skim §1–§3, then go to §5 for the
day-1 operational picture. The rest is rationale.

---

## 1. Goal and constraints

What the user asked for, verbatim during design:

- LAN-only, no cloud arbitration, no third-party service.
- Lightweight enough to deploy multiple instances around a house.
- **If only one device exists, nothing peering-related should
  even run.** Single-Pi households pay zero CPU / RAM / network.
- A binary toggle in the JTS.local UI that someone has to flip on
  deliberately.
- Losers play no sound. They lost.
- "Primary speaker" is a *bias*, not a hard rule.
- Session stickiness: once a peer wins a wake, it owns the
  conversation until either silence/end or a fresh wake word
  initiates new arbitration.
- TTS reply through the winner's own speakers (not routed to a
  designated primary).

These are the constraints that shaped every architecture decision
below.

---

## 2. Architecture in one diagram

```
   Pi A (room=living, primary=1)         Pi B (room=bedroom)
   ┌────────────────────────────┐        ┌────────────────────────────┐
   │  jasper-voice              │        │  jasper-voice              │
   │  WakeLoop                  │        │  WakeLoop                  │
   │   └─ _peer_arbitrate ──┐   │        │   └─ _peer_arbitrate ──┐   │
   │                        │   │        │                        │   │
   │  jasper-control        ▼   │        │  jasper-control        ▼   │
   │   ├─ HTTP :8780 (existing) │        │   ├─ HTTP :8780 (existing) │
   │   └─ peering daemon ◀──┐   │        │   └─ peering daemon ◀──┐   │
   │       (NEW)            │   │        │       (NEW)            │   │
   └────────────────────────┼───┘        └────────────────────────┼───┘
                            │ UDS /run/jasper/peering.sock        │
                            ─────────────────────────────────────
                              voice ↔ peering RPC (per-Pi local)

                            │ mDNS-SD `_jasper-peer._udp`         │
                            ─────────────────────────────────────
                              peer discovery (cross-LAN)

                            │ Multicast 239.192.0.1:5354 TTL=1    │
                            ─────────────────────────────────────
                              arbitration messages (cross-LAN)
```

**Two transports, deliberately separated:**

- **mDNS-SD** answers "is anyone else on the network?" The peering
  daemon doesn't spin up its multicast socket or state machine until
  it sees at least one sibling. Discovery is cheap, idempotent, and
  uses the same Avahi daemon JTS already runs for `_jasper-control._tcp`.
- **Multicast UDP** carries the actual arbitration messages
  (`WAKE`, `CLAIM`, `HEART`, `END`, plus periodic `HELLO`). 5
  message types, JSON-encoded, max ~300 bytes each.

**Why P2P, not a hub?** The user wanted N=1 to be free and N≥2 to
just work. A hub-and-spoke design with an arbitration server
needs that server even when N=1. P2P with deterministic ranking
(every peer applies the same pure function to the same multicast
message set and reaches the same conclusion) means there's no
leader to elect and no SPOF. Inspired by the Sonos / Apple
patents documented in [satellites.md](satellites.md#what-everyone-else-ships).

---

## 3. Module layout

Separated by I/O profile so each piece is independently testable.

| File | Purity | What it does |
|---|---|---|
| [jasper/peering/config.py](../jasper/peering/config.py) | pure | `PeeringConfig` + env-file loader (`load_config`). Owns the `peer_id` idempotency, the on/off mode parsing, the room-name default derivation. |
| [jasper/peering/rank.py](../jasper/peering/rank.py) | pure | `WakeReport` dataclass + `rank(reports)` — the deterministic winner-pick. Cascade of tiers (see §4). |
| [jasper/peering/state.py](../jasper/peering/state.py) | pure | `PeeringStateMachine` — accepts `Event` instances, returns `Action` instances. No I/O. Five states: IDLE / CANDIDATE / WINNER / ACTIVE / SUPPRESSED. |
| [jasper/peering/transport.py](../jasper/peering/transport.py) | I/O | Multicast UDP socket + JSON encode/decode of the 5 message types. `MulticastTransport` is an asyncio wrapper. |
| [jasper/peering/avahi.py](../jasper/peering/avahi.py) | I/O | Renders `/etc/avahi/services/jasper-peer.service` from the template at `/etc/jasper/avahi-templates/`. Installs on `mode=on`, uninstalls on `mode=off`. |
| [jasper/peering/discovery.py](../jasper/peering/discovery.py) | I/O | `AsyncZeroconf` wrapper for browsing `_jasper-peer._udp`. Lazy-imported so the zeroconf module doesn't load when peering is off. |
| [jasper/peering/uds.py](../jasper/peering/uds.py) | I/O | Unix-socket server for voice → peering RPC. Mirrors the existing `voice.sock` newline-ASCII + JSON protocol. |
| [jasper/peering/daemon.py](../jasper/peering/daemon.py) | I/O | The asyncio orchestrator. Wires discovery + transport + state + UDS together. Translates state-machine `Action`s into actual I/O. **No business logic** lives here — the logic is all in state.py. |

**Integration points** (small, surgical changes outside the peering package):

| File | What it adds |
|---|---|
| [jasper/config.py](../jasper/config.py) | `Config.peering_enabled` (bool) + `Config.peering_uds_socket` (path). Read from `JASPER_PEERING` env. |
| [jasper/voice_daemon.py](../jasper/voice_daemon.py) | New helpers: `_peer_arbitrate`, `_peering_send`, `_notify_peering_session_started/_ended`, `_wake_late_cancelled`, `_frame_rms_dbfs`. Restructured `_handle_wake_frame` to spawn `_arbitrate_acquire_drain` as a background task. |
| [jasper/control/server.py](../jasper/control/server.py) | `start_peering_daemon_if_enabled()` — spawns a background thread with its own asyncio loop iff `JASPER_PEERING=on`. No-op when off. |
| [jasper/cli/doctor.py](../jasper/cli/doctor.py) | Two new checks: `check_peering_mode` (env-file sanity) and `check_peering_discovery` (sibling-peer count via `avahi-browse`). |
| [jasper/web/peering_setup.py](../jasper/web/peering_setup.py) | New `/peers/` wizard on port 8776. Toggle + room label + primary flag. Writes `/var/lib/jasper/peering.env`, restarts both voice + control daemons. |
| [deploy/avahi/jasper-peer.service.template](../deploy/avahi/jasper-peer.service.template) | mDNS service-file template with `__PEER_ID__` / `__ROOM__` / `__PRIMARY__` placeholders, rendered at runtime. |
| [deploy/install.sh:install_peering_template()](../deploy/install.sh) | Installs the template, generates a stable `peer_id` UUID at `/var/lib/jasper/peer_id`. |

---

## 4. The ranking function — cascade of tiers

`jasper/peering/rank.py:rank(reports) → peer_id`. Pure, deterministic.
Every peer applies it to the same input set and gets the same answer.
That's the safety property of the whole P2P design.

Tiers, applied in order. Each tier filters the candidate pool. A
single survivor returns immediately; multiple survivors fall through.

| Tier | Signal | Tiebreaker eps |
|---|---|---|
| 1 | `can_serve=True` peers win over `can_serve=False` (paused / spend cap reached). If *no* peer can serve, the highest-confidence one wins anyway so exactly one peer plays the failure cue. | — |
| 2 | openWakeWord confidence — top score defines the band. | within 0.05 of top |
| 3 | Primary flag — if exactly one primary peer is in the confidence band, it wins. If multiple primaries, restrict to primaries. **This is the "bias" — it only kicks in within the band, not as an absolute override.** | — |
| 4 | SNR (higher wins) | within 3 dB of top |
| 5 | RMS in dBFS (higher wins) | exact |
| 6 | Lowest `peer_id` UUID lexicographically. Final deterministic tiebreaker. | — |

**Why confidence as the primary signal, not raw audio energy:**
documented at length in [satellites.md "What a naive first instinct
gets wrong"](satellites.md#what-a-naive-first-instinct-gets-wrong).
Short version: raw RMS varies by mic gain and is biased by
reverberation; openWakeWord confidence is the closest thing to a
gain-invariant proximity signal we have.

**Why a band (eps), not a sort:** detection-time jitter on identical
audio is ~0.03; a strict sort would let microsecond-scale CPU
scheduling decide arbitration. The band absorbs that noise and
defers to physical signals (SNR, RMS) only when scores are
genuinely close.

---

## 5. Wake event end-to-end

This is the path from "Hey Jarvis" to "winner answers" with peering
on. Numbers are approximate; the only one that's actually tuned is
the 150 ms arbitration window.

```
T+0    ┌──────────────────────────────────────────────────────────┐
       │ openWakeWord fires on all Pis that heard the utterance.  │
       │ Each Pi's WakeLoop:                                       │
       │   1. detector.feed → score (e.g. 0.87)                    │
       │   2. compute can_serve from spend_cap + paused state      │
       │   3. set _acquiring=True; main mic loop now buffers frames│
       │      into _acquire_buffer (cap 20s)                       │
       │   4. spawn _arbitrate_acquire_drain as a bg task          │
       │   5. main mic loop returns to draining new frames         │
       └──────────────────────────────────────────────────────────┘
                            │
T+0    bg task: _arbitrate_acquire_drain
                            │
                            ▼
T+~1   ┌──────────────────────────────────────────────────────────┐
       │ _wake_late_cancelled() — abort if user muted or a room    │
       │ correction window is open. (Same checks run again post-arb)│
       └──────────────────────────────────────────────────────────┘
                            │
                            ▼
T+~5   ┌──────────────────────────────────────────────────────────┐
       │ _peer_arbitrate() — UDS call to jasper-control:            │
       │   "ARBITRATE {score, snr, rms, can_serve}"                 │
       │ Peering daemon broadcasts WAKE on multicast, schedules     │
       │ a 150 ms arbitration timer, collects peer WAKEs, and       │
       │ when the timer fires, runs rank() on collected reports.    │
       └──────────────────────────────────────────────────────────┘
                            │
              ┌─────────────┴──────────────┐
              │                            │
            WIN                          LOSE
              │                            │
T+~155 ms     ▼                            ▼
       ┌────────────────────┐   ┌────────────────────────────────┐
       │ peering broadcasts │   │ Daemon returns {result:"LOSE"} │
       │ CLAIM on multicast │   │ over UDS. _arbitrate_acquire   │
       │ Returns {result:   │   │ _drain logs event=peering.wake │
       │ "WIN", epoch}      │   │ .lost and returns silently.    │
       └────────────────────┘   │ finally: _acquiring=False,     │
                                │ buffer cleared, refractory set │
T+~155                          └────────────────────────────────┘
       ▼
       ┌─────────────────────────────────────────────────────────┐
       │ Re-check late-cancel gates                              │
       │ Check spend_cap (now); if blocked → play cant_connect   │
       │ Fire chirp (async task)                                 │
       │ _begin_turn() — opens LLM session                        │
       │ _notify_peering_session_started → daemon starts          │
       │ broadcasting HEART every 1 s on multicast                │
       │ Drain _acquire_buffer into the LLM session in FIFO       │
       │ order → live frames flow normally from main mic loop     │
       └─────────────────────────────────────────────────────────┘
                            │
                            ▼
T+session  user speaks, LLM responds. Existing voice flow.
                            │
                            ▼
T+session_end
       ┌─────────────────────────────────────────────────────────┐
       │ _end_turn:                                              │
       │   _notify_peering_session_ended(reason)                 │
       │   peering broadcasts END on multicast                   │
       │   peers' SUPPRESSED state clears                        │
       │   chirp off, duck restore, refractory set, state=WAKE   │
       └─────────────────────────────────────────────────────────┘
```

**Critical correctness properties:**

- **The main mic loop never blocks on peering.** `_arbitrate_acquire_drain`
  runs as a separate asyncio task. While it's awaiting the UDS
  round-trip (up to 500 ms hard ceiling), the main mic loop keeps
  iterating, frames buffer into `_acquire_buffer`, and the watchdog
  heartbeat keeps patting systemd.
- **Fail-open on every peering error.** If the UDS doesn't exist
  (peering daemon not running), the socket times out, or the
  response is malformed → return "WIN". A wedged peering daemon
  cannot silence the speaker. Every code path in
  `_peer_arbitrate` returns "WIN" by default. See
  [jasper/voice_daemon.py:_peering_send](../jasper/voice_daemon.py).
- **Late-cancel after arbitration.** Between arb start and arb end
  (up to 500 ms), the user could mute via dial or kick off a room-
  correction measurement. Both check again at `post_arb`.
- **Losers stay silent.** No chirp on LOSE. The chirp moved from
  "fires immediately on wake" to "fires only on WIN" — the only
  user-visible change in solo mode is that gate-failure cues now
  play *during* a brief acquiring-state window instead of before
  it (acoustically equivalent; the cue plays through TtsPlayout
  either way).

---

## 6. The state machine

`PeeringStateMachine` in [jasper/peering/state.py](../jasper/peering/state.py).
Five states, pure event-driven. Returns `Action` values; the
daemon translates them into I/O.

```
                       ┌─────────────────────────────────────┐
                       │                IDLE                  │
                       │  (peering on, no in-flight wake)     │
                       └──┬──────────────────────┬────────────┘
        local wake fires  │                      │  peer CLAIM seen
                          ▼                      ▼
                  ┌──────────────┐         ┌──────────────┐
                  │  CANDIDATE   │         │  SUPPRESSED  │
                  │  collect 150ms│         │  (foreign    │
                  │  WAKEs        │         │   session)   │
                  └──┬───────────┘         └──┬───────┬───┘
          window      │                       │       │
          elapses     │                       │       │ HEART missed 2s
        ┌─────────────┴───────────┐           │       │ OR END seen
        │                         │           │       ▼
        ▼                         ▼           │  (back to IDLE)
   ┌─────────┐               ┌─────────┐     │
   │ WINNER  │               │  LOSER  │     │ local wake above
   │ (CLAIM) │               │ → IDLE  │     │ break_threshold (0.85)
   └────┬────┘               └─────────┘     │
        │                                    └──► CANDIDATE (contest)
        │ session opens (voice notifies)
        ▼
   ┌─────────┐  HEART every 1s while in this state
   │ ACTIVE  │
   └────┬────┘
        │ session ends (silence detector, spend, user END)
        ▼
    send END, → IDLE
```

**Pure → easy to test.** All the state machine tests live in
[tests/test_peering_state.py](../tests/test_peering_state.py) and
drive transitions with synthetic event sequences. No sockets, no
timers, no asyncio.

**Heartbeat semantics.** Once a winner enters ACTIVE, it broadcasts
HEART every 1 s. SUPPRESSED peers reset a 2 s heartbeat-timeout
timer on each HEART. If the winner crashes mid-session, the
heartbeats stop, the SUPPRESSED peers' timers fire, and they
return to IDLE within ~2 s. The user might hear their next "Hey
Jarvis" pick a fresh winner with no double-response.

**Stickiness.** A foreign session in flight prevents IDLE peers
from entering CANDIDATE on their own wake events — *unless* the
local score exceeds `break_threshold` (default 0.85), in which
case the SUPPRESSED state breaks and arbitration starts fresh.
This is the "wake word resets stickiness" feature the user asked
for: the user can grab a different speaker by speaking the wake
word directly to it, but a far-away false-fire on a faint wake
doesn't interrupt the active conversation.

---

## 7. Configuration

| File | Default | Purpose |
|---|---|---|
| `/var/lib/jasper/peering.env` | (absent) | wizard-managed. Absent → `JASPER_PEERING=off` resolves. |
| `/var/lib/jasper/peer_id` | UUID generated at install | stable per-Pi identity, persists across reboots and re-installs. **Never user-edited.** |
| `/etc/jasper/avahi-templates/jasper-peer.service` | installed by `install.sh` | template with `__PEER_ID__` / `__ROOM__` / `__PRIMARY__` placeholders. |
| `/etc/avahi/services/jasper-peer.service` | (absent until mode=on) | rendered file. **Its presence is what makes this Pi visible to siblings.** |

Env vars (all `JASPER_PEER*` namespace):

- `JASPER_PEERING` — `off` (default) or `on`. Anything else parses
  to `off` (fail-safe).
- `JASPER_PEER_ROOM` — human label shown to siblings in their
  `/peers/` UI. Defaults to derived-from-hostname (`jts-bedroom`
  → `bedroom`; bare `jts` → `default`).
- `JASPER_PEER_PRIMARY` — `1` if this Pi is the household primary;
  small bias in ranking (see Tier 3 in §4).
- `JASPER_PEER_ARB_WINDOW_MS` — arbitration collection window.
  Default 150 ms, clamped to [50, 500].
- `JASPER_PEER_BREAK_THRESHOLD` — local wake score required to
  break suppression mid-session. Default 0.85, clamped to [0.5, 0.99].

The wizard at `/peers/` writes the first three. The latter two are
operator-managed (edit the env file by hand); the wizard preserves
them across saves.

---

## 8. Operations

### Turning peering on

Visit `http://jts.local/peers/`. Single toggle, plus a room name
field and a primary checkbox. Save triggers `systemctl restart
--no-block jasper-voice jasper-control` so both daemons pick up
the new mode. Allow ~3-5 s before peers see this Pi appear in
their `/peers/` lists.

### Verifying

```sh
# Doctor shows mode + sibling count
sudo /opt/jasper/.venv/bin/jasper-doctor | grep peering

# Live multicast traffic when sessions are active
sudo journalctl -u jasper-control -f | grep -E "event=peering"

# Active sessions / current state
curl -s --unix-socket /run/jasper/peering.sock - <<< "STATUS"
```

The doctor's two checks:

- **`peering: mode`** — parses `/var/lib/jasper/peering.env`. `ok`
  for both `off` (default) and `on` (deliberate); `warn` only when
  the file has a malformed value (e.g. `JASPER_PEERING=banana` →
  silently resolves to `off`, but the operator probably wants to
  know).
- **`peering: discovery`** — runs `avahi-browse -rt _jasper-peer._udp`
  and counts sibling peer_ids. Filters out our own. Reports `0
  sibling peers visible (single-device mode)` when alone, or
  `N sibling peer(s) visible: <list>` otherwise.

### Two-Pi smoke test (the actual ship gate)

Once we have a second Pi:

1. Toggle peering on at both via `/peers/`.
2. Each wizard should show the other peer within ~30 s.
3. Stand between them. Say "Hey Jarvis, what time is it?"
4. **Exactly one** should respond.
5. `journalctl -u jasper-voice | grep peering` shows
   `event=peering.wake.won` on one Pi and `event=peering.wake.lost`
   on the other, with the winner's score and loser's score in the
   log lines.

### Logging

Every wake event emits structured `event=peering.*` lines:

```
event=peering.discovery.peer_seen peer=<uuid> room=bedroom primary=0 addr=192.168.1.42
event=wake.detected score=0.87 threshold=0.50
event=peering.wake.won epoch=<uuid> reports=2
event=peering.wake.lost epoch=<uuid> winner=<peer> winner_score=0.91 my_score=0.79
event=peering.session.heartbeat_missed epoch=<uuid> peer=<peer> after_ms=2100
event=peering.foreign.ended peer=<peer> reason=user_silence
```

`scripts/jasper-trace.sh` filters journals down to these `event=`
lines for cross-daemon debugging.

---

## 9. The "off-by-default" guarantee

This is the most important UX property and the design constraint
that shaped almost everything. When `JASPER_PEERING=off`:

| Resource | When peering off | When peering on |
|---|---|---|
| `jasper-control` peering thread | not started (`start_peering_daemon_if_enabled` returns early before importing anything) | started, owns its own asyncio loop |
| zeroconf module | not imported | imported (lazy, ~5-8 MB Pss) |
| Multicast UDP socket | not opened | bound to 239.192.0.1:5354 |
| Avahi service file | not installed at `/etc/avahi/services/jasper-peer.service` | rendered from template, Avahi reloaded |
| `voice_daemon._peer_arbitrate` | returns "WIN" immediately, no UDS call | UDS call to peering daemon, up to 500 ms |
| `voice_daemon._notify_peering_session_*` | no-op (no I/O) | UDS call |
| `voice_daemon._frame_rms_dbfs` (per wake) | still computed (~80 µs); negligible | same |
| Wake handler restructure (`_arbitrate_acquire_drain`) | runs but every peering check short-circuits | full path |

Net cost for a single-Pi household with `JASPER_PEERING=off`: zero
observable difference from before peering existed. The mic loop's
behaviour is unchanged, the chirp timing is unchanged (peer_arbitrate
returns synchronously without yielding to the loop when peering is
disabled — verified by `test_peer_arbitrate_disabled_returns_win_without_io`).

---

## 10. Open questions and what we didn't do

Documented honestly so future-you doesn't have to reverse-engineer
the gaps.

**Per-peer microphone gain calibration.** The ranking function
assumes openWakeWord confidence is gain-invariant across mics.
In practice it's *roughly* invariant for identical XVF3800
hardware running identical firmware, which is what production
JTS uses. Heterogeneous fleets (one XVF + one USB conference mic)
would benefit from a per-peer calibration multiplier on the score
before broadcasting. Deferred until we see real-world fleets with
mixed hardware.

**Stereo speaker echo divergence.** Single-reference linear AEC
plus stereo speakers around a centered mic has a hard cancellation
ceiling regardless of peering. Independent of this work; tracked
in the in-progress mic test sequence (Tests 0/A/B/C from the AEC
investigation).

**TTS reply through the winner's speakers.** The user explicitly
chose this UX. The alternative — route the LLM response audio
through a designated primary speaker — would require streaming PCM
between Pis, which is a much bigger architectural change
(documented as out-of-scope in [satellites.md](satellites.md)).

**Live mode toggle without restart.** Currently the wizard restarts
both jasper-voice and jasper-control via `systemctl restart
--no-block`. ~3 s of unavailability for the user. Live-toggle via
SIGHUP is doable but requires both daemons to re-read peering.env
on signal, plus the peering daemon to bring up / tear down its
sockets and Avahi advertisement dynamically. Worth ~50 lines of
extra plumbing only if the restart UX bothers a user.

**SNR computation.** Voice daemon's `_frame_rms_dbfs` sends RMS in
dBFS to the ranking function, but `snr_db=None` always — proper
SNR needs a rolling noise-floor estimator we don't currently track.
The ranking function falls through to RMS when SNR is missing.
Mild precision loss in the SNR tiebreaker tier (Tier 4); usually
the confidence band (Tier 2) already picks the winner.

**No tests on a real LAN.** All unit + integration tests run on
the laptop with mocked transports. A real two-Pi acceptance test
needs hardware. The first deploy will be the first real validation
of the cross-LAN multicast path.

**Multicast on consumer mesh networks.** Some routers (notably
eero, Google Wifi) drop or rate-limit multicast packets. We don't
currently have a unicast-UDP fallback wired up — the design hooks
are in `transport.py` but the per-peer multicast-health detector
isn't implemented. Mentioned as a follow-up in PR #142's risk
register. Watch for: peers visible in `/peers/` (mDNS works) but
no arbitration messages ever exchanged (multicast doesn't).

---

## 11. The lost-edit incident (PR #146 retrospective)

Worth recording so the same bug class doesn't recur silently.

**What happened:** PR #146 wired the `/peers/` wizard into
`jasper/web/__main__.py`. Two of my edits — adding `peering_setup`
to the `from . import (...)` tuple and adding `peers_port = int(...)`
to the port-var block — got dropped during the edit. The wiring
code that *references* both names landed; the declarations didn't.
Python compiled the file fine because it doesn't resolve function-
body names at module-load time. The bug only surfaced when systemd
tried to start `jasper-web` post-deploy, at which point all eight
web wizards (`/spotify`, `/voice`, `/wake`, `/wifi`, `/airplay`,
`/sources`, `/google`, `/bluetooth` — wait, /bluetooth is a
separate daemon — so the seven wizards on jasper-web) went down
crash-looping.

**Defense in depth, added afterward** ([tests/test_web_main_imports.py](../tests/test_web_main_imports.py)):

- `test_every_referenced_setup_module_is_imported` — regex: every
  `<name>_setup.X` in `__main__.py` requires `<name>_setup` in the
  import tuple.
- `test_every_referenced_port_var_is_defined` — regex: every
  `<name>_port` reference requires a `<name>_port = ...`
  assignment.
- `test_peering_surface_has_no_undefined_names` — invokes `ruff
  check --select=F821` over the whole peering surface
  (`jasper/peering/`, `voice_daemon.py`, `peering_setup.py`,
  `__main__.py`, `control/server.py`, `cli/doctor.py`). Catches
  the general "name referenced but not defined" pattern.

The pattern-specific tests catch the exact `__main__.py` shape;
the ruff test catches the same bug class anywhere in the peering
surface. Either test would have caught PR #146's bug pre-merge.

---

## 12. References

**Cross-references inside the codebase:**

- [docs/satellites.md "Microphone arbitration"](satellites.md) —
  the pre-peering design doc that covered the multi-mic-around-
  one-Pi case. Multi-mic and multi-Pi share signal-priority
  intuition but the implementation in this doc is the multi-Pi
  variant.
- [jasper/peering/__init__.py](../jasper/peering/__init__.py) —
  module overview docstring + public exports.
- [jasper/voice_daemon.py:_arbitrate_acquire_drain](../jasper/voice_daemon.py)
  — the wake-handler integration point.
- [jasper/web/peering_setup.py](../jasper/web/peering_setup.py) —
  the `/peers/` wizard.

**External design references** (consulted during the original
research):

- Sonos US10181323 — confidence-broadcast arbitration patent
- Apple AU2016410253B2 — peer-to-peer BLE arbitration for HomePod
- Amazon ICASSP 2022 paper (arXiv 2112.04914) — "End-to-end Alexa
  Device Arbitration"
- RFC 2365 — Administratively Scoped IP Multicast (where
  239.192.0.0/14 comes from)
- RFC 6762 — Multicast DNS
- python-zeroconf — the browse-only library we use; Avahi
  remains the only mDNS responder on the host

**PRs that shaped this subsystem:**

- #142 — `peering: add multi-device wake arbitration plumbing (OFF by default)`
- #146 — `peering: wire WakeLoop + add /peers/ wizard`
- #148 — `web: restore missing peering_setup import + peers_port var`
  (the hotfix retrospective above)
- #149 — `test: ruff F821 across the peering surface`

---

## TL;DR for a fresh context window

If you are a fresh Claude or LLM landing here:

1. Peering is OFF by default. A solo speaker pays nothing.
2. The user flips it on at `http://jts.local/peers/`. Both
   `jasper-voice` and `jasper-control` restart.
3. With peering on, peers discover each other via mDNS-SD
   (`_jasper-peer._udp`) and arbitrate every wake event over
   multicast UDP. The winner answers; the losers stay silent.
4. The ranking is a pure function ([rank.py](../jasper/peering/rank.py))
   — every peer picks the same winner from the same multicast
   message set. No leader, no consensus.
5. Everything fails open. A wedged peering daemon cannot silence
   the speaker; the voice daemon falls back to solo behaviour.
6. To verify: `jasper-doctor` shows `peering: mode` + `peering:
   discovery` lines; `journalctl -u jasper-voice | grep peering`
   shows `event=peering.wake.won` / `event=peering.wake.lost`.
7. The pre-existing `docs/satellites.md` was the design hub for
   the multi-mic-around-one-Pi case. This doc is the multi-Pi
   variant.
