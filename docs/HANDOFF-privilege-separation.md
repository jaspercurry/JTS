# Handoff: privilege separation (WS1)

> **Status: current-state reference + approved phased plan.** Phase 1
> (hardened root) has landed and is validated on hardware. Phases 2–4 and
> the Tier-B follow-up are designed and committed-to below, not yet built.
> This doc is the threat-model + ADR for the work; update it as each phase
> lands.

How JTS contains a compromise of its always-on daemons, and the staged plan
to go from "every daemon is root" to "least-privilege service users behind a
single restart broker." Read [HANDOFF-resilience.md](HANDOFF-resilience.md)
first — privilege separation must not weaken the self-healing ladder that doc
describes; that constraint shapes the whole plan.

## Threat model (be honest about what each phase buys)

JTS is a household-LAN appliance with an always-on microphone and several
network-facing daemons (`jasper-control` on `0.0.0.0:8780`; the `jasper-web`
wizards parsing untrusted SSIDs / form input / OAuth callbacks; `jasper-voice`
parsing mic audio + third-party LLM/API responses). The accepted posture is a
*trusted LAN* (see [SECURITY.md](../SECURITY.md)), but "trusted LAN" is a weak
deferral excuse for the one structural gap: **every `jasper-*` daemon runs as
root, so any RCE in any of them is full-root device compromise** — read every
secret, rewrite any file, pivot to the LAN. Lateral movement from a single
compromised IoT device is precisely the documented real-world risk.

What each phase actually contains:

- **Phase 1 — hardened root (landed):** a root RCE can no longer write most of
  the filesystem (`ProtectSystem=strict`), load kernel modules, change kernel
  tunables, enter new namespaces, or pivot through root's SSH key
  (`ProtectHome=tmpfs` on the daemons that don't need a home). It does **not**
  hide the provider API keys — they live in `/etc/jasper/jasper.env` +
  `/var/lib/jasper/*.env`, which a still-root process owns and reads regardless
  of capabilities. Closing secret disclosure is Phase 3/4's job, not Phase 1's.
- **Phase 2 — restart broker + mandatory token:** collapses the privileged
  restart surface to one auditable boundary and closes the unauthenticated-LAN
  hole on the destructive routes.
- **Phase 3 — user drop:** non-root service users genuinely cannot escalate or
  read another daemon's secrets; this is where `CapabilityBoundingSet` and
  `SystemCallFilter` carry full value.
- **Phase 4 — secret credentialization:** per-daemon secret compartmentalization
  even from a same-user compromise.

## Scope: Tier A (drop) vs Tier B (stays root in v1, tracked follow-up)

The 40 privileged-restart sites and 8 self-healing reconcilers split cleanly:

- **Tier A — always-on, network-facing daemons** (`jasper-voice`,
  `jasper-control`, `jasper-web`, `jasper-mux`, `jasper-input`): the RCE attack
  surface. These get hardened (Phase 1) and dropped (Phase 3).
- **Tier B — udev/boot-triggered one-shots + operator sudo-CLIs**
  (`jasper-aec-reconcile`, `jasper-aec-init`, `jasper-dac-init`,
  `jasper-dongle-recover`, `jasper-wifi-guardian`; `jasper-aec-tune`,
  `jasper-wake-enroll`, `jasper-noise-capture`): short-lived, systemd-launched,
  **not network-reachable**, and the components doing the scariest privileged
  ops (nmcli network recreate, amixer, XVF USB writes, `/etc` writes).

Keeping Tier B root in v1 is what makes the Tier-A drop *safe* — the highest-risk
self-healing paths (Wi-Fi recovery, AEC reconcile, DAC pinning) are untouched by
the drop. **Tier B is committed follow-up work, not abandoned:** it gets the
Phase-1 hardening directives now (no user change), and a dedicated `jasper-recon`
service user + the broker in a later increment. Tracked here so it is not lost.

## Phase 1 — hardened root (LANDED)

Each Tier-A unit gained, on top of its existing `ProtectSystem`/`ProtectHome`/
`PrivateTmp`/`NoNewPrivileges`:

```
ProtectSystem=strict                 # was =full; writes pinned by ReadWritePaths
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectKernelLogs=true                # OMITTED on jasper-control + jasper-web *
ProtectControlGroups=true
RestrictNamespaces=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 [AF_NETLINK **]
SystemCallArchitectures=native
```

Plus `ProtectHome=tmpfs` on `jasper-voice` + `jasper-mux` (they need no home;
hides `/root`). `jasper-mux` additionally gained the basic stanza it lacked
(`NoNewPrivileges`/`ProtectSystem`/`ProtectHome`/`PrivateTmp`).

Per-unit nuances (the reason a uniform block would break things):
- `*` **`ProtectKernelLogs` omitted on `jasper-control`** — it spawns
  `jasper-doctor`, whose watchdog-reset fingerprint check reads `dmesg`
  (`/dev/kmsg`). Omitted on `jasper-web` for the same shell-out caution.
- `**` **`AF_NETLINK` added** on `jasper-input` (pyudev hot-plug monitor),
  `jasper-control` + `jasper-web` (diagnostic/network subprocesses). Omitted on
  `jasper-voice`/`jasper-mux`, which use only UNIX + INET sockets.
- **`jasper-control` keeps `ProtectHome=read-only`** (not tmpfs) — diagnostic
  subprocesses introspect home/ALSA routing.
- **`CapabilityBoundingSet` + `SystemCallFilter` are deliberately deferred to
  Phase 3** — on a still-root process they are modest hardening (uid 0 bypasses
  most checks), and they carry brick risk on the audio/USB paths; they deliver
  full value, and get per-daemon validation, with the user drop.

**Measured on hardware (jts.local, `systemd-analyze security`):**

| Unit | Before | After |
|---|---|---|
| jasper-voice | 8.8 EXPOSED | **6.2 MEDIUM** |
| jasper-control | 8.8 EXPOSED | **6.6 MEDIUM** |
| jasper-web | 8.7 EXPOSED | **6.5 MEDIUM** |
| jasper-mux | 9.6 UNSAFE | **6.2 MEDIUM** |
| jasper-input | 8.7 EXPOSED | **6.2 MEDIUM** |

Validation: all daemons `NRestarts=0` (no watchdog loop), voice mic capture +
multi-leg wake alive under the sandbox, `jasper-doctor` unchanged (1
non-critical warning), `/system/`, `/healthz`, `/state` all serving. The
stanza is drift-guarded by [`tests/test_systemd_hardening.py`](../tests/test_systemd_hardening.py).

## Phase 2 — mandatory, invisible control token (LANDED)

#712 shipped the token as an opt-in, default-off floor. Phase 2 makes it
**always armed** without giving the household anything to do:

- **Auto-generated on startup.** `control_token.ensure_token()` (called once in
  `jasper-control`'s `main()`) writes a `secrets.token_urlsafe(32)` to
  `/var/lib/jasper/control_token` (0600, atomic) if absent, so the gate is
  mandatory with no operator action. It is **idempotent and never rotates** an
  existing token (a household's stored copy stays valid). Failure is non-fatal —
  the gate fail-safes to off rather than blocking the recovery surface.
- **Invisible delivery.** `canonical_page()` embeds the token in a
  `<meta name="jts-control-token">` tag — emitted on every wizard page, which is
  served only behind the management-host / Fetch-Metadata **read guard**.
  `http.js` reads the meta tag first (then localStorage), so the dashboard rides
  the token on every destructive POST automatically. The household never sees or
  types it; this closes #712's `/rooms/` token gap for free **for the
  browser → its own speaker case** (every canonical page now carries it). It does
  **not** close the cross-device grouping fan-out (see the caveat below).
- **Extended gated set.** Added `/system/restart/voice` + `/system/restart/audio`
  to the four #712 routes (poweroff / reboot / mic-mute / grouping-set).
- **Honest posture (the chosen tradeoff).** Because the token is auto-delivered
  over the LAN, a determined LAN device that fetches the page can read it too —
  so this is **defense-in-depth against drive-by / CSRF / casual curl on the
  annoyance-class routes, not a hard boundary** against a compromised LAN device.
  The real containment of the serious threats (secret theft, persistence, pivot)
  is the daemon hardening (Phase 1) + the user drop (Phase 3). This posture was
  chosen deliberately to keep plug-and-play frictionless.

> **Device-to-device caveat (2026-06-16).** The `/rooms/`-gap-closed claim above
> holds only for *browser → its own speaker*. The mandatory token does **not**
> authenticate the cross-device grouping fan-out (each speaker mints a distinct
> token, so the leader can't satisfy a follower's gate) — that path now 403s. The
> reconciled design (a separate household credential for the machine-to-machine
> path) lives in
> [HANDOFF-control-plane-auth.md](HANDOFF-control-plane-auth.md), which owns the
> device-to-device control-plane auth question.

Pinned by `tests/test_control_token.py` (ensure/current/idempotence/0600/meta),
`tests/test_http_js_control_token.py` (meta-first delivery), and the
server-frozenset-derived gating tests in `tests/test_control_server.py`.

## Phase 3 — restart broker + Tier-A user drop

This phase ships as **two PRs**, by design. The broker and the user drop *are*
coupled (the broker is only load-bearing once the clients are non-root — a root
client could `systemctl` directly), but they have opposite risk profiles: the
broker is a self-contained, fully-testable refactor with zero file-ownership or
brick exposure, while the drop is ownership-heavy and brick-sensitive and is
**gated on the recovery-path validation matrix below**. Bundling a safe refactor
with a gated drop into one un-reviewable PR is the wrong shape; splitting also
honours the WS1 directive to "ship hardened-root and make the drop a validated
fast-follow" if recovery validation isn't clean.

### Phase 3a — restart broker (LANDED)

`jasper-control` was already the de-facto broker (its own ~9 privileged restart
sites). [`jasper/control/restart_broker.py`](../jasper/control/restart_broker.py)
finishes it as a local **UNIX socket + `SO_PEERCRED`** at
`/run/jasper-control/restart.sock` (`RuntimeDirectory=jasper-control`; the
in-repo's first peer-cred reader — peer uid check *is* the auth), with a
**closed verb vocabulary** (`restart` / `try-restart` / `start` / `stop` /
`enable` / `enable-now` / `disable-now` / `reset-failed`) scoped to the single
source of truth `MANAGED_UNITS` allowlist. Every request and denial emits a
stable `event=restart_broker.*` line with the peer uid/pid, verb, units, reason.

The clients now route through it via `restart_broker.manage_units(...)`:
`jasper-web`'s restart sites (`_common.restart_systemd_units` /
`_enable_systemd_unit`, `sources` enable/disable, `airplay` shairport restart,
`speaker` rename, `wake-corpus` bridge-output + voice start/stop),
`jasper-mux`'s librespot recovery, and `correction`'s renderer pause. While the
clients are still root, `manage_units` falls back to a **direct `systemctl` if
the broker is unreachable** — logged loudly (`event=restart_broker.
fallback_direct`) so a silently-broken broker path is caught *before* the user
drop removes the safety net. Once a client is a non-root service user
(`geteuid() != 0`) the fallback is structurally impossible: the broker is the
only path, as intended. `MANAGED_UNITS` is the same list the 3b polkit rule will
grant `jasper-control`, so broker authz and the polkit grant can't drift. Pinned
by [`tests/test_restart_broker.py`](../tests/test_restart_broker.py) (verb
vocabulary, unit allowlist, peer-cred auth, wire contract, root fallback).

### Phase 3b — Tier-A user drop

The 5 Tier-A daemons drop to dedicated non-root users in a shared `jasper`
group (cross-daemon `/run` socket + `/var/lib/jasper` access), each with
`CapabilityBoundingSet=` (empty — **no daemon needs a capability**) +
`SystemCallFilter=@system-service`. The investigation corrected two
over-specifications in the original table: **jasper-voice needs no RT and no XVF
udev rule** — it makes zero `sched_setscheduler`/`mlock` calls (the RT process
is `jasper-aec-bridge`, which stays root) and reaches the XVF mic as the ALSA
`Array` card via the `audio` group, not a raw USB endpoint (only the root
`jasper-aec-init` opens raw USB); and **the dropped daemons read secrets via
systemd `EnvironmentFile=` injection** (root reads them pre-drop), so they need
no on-disk secret access for their own startup.

The daemons split by risk into **three increments**, shipped separately because
they have sharply different blast radii — bundling them would force shipping a
wifi-lockout-risk change you could only happy-path test:

| Unit | User | Groups | Increment | Why this increment |
|---|---|---|---|---|
| jasper-voice | `jasper-voice` | `audio` | **3b-1 (LANDED)** | clean: no caps/RT/udev/polkit; config via env injection |
| jasper-mux | `jasper-mux` | — | **3b-1 (LANDED)** | broker client (librespot recovery); only shared file is `speaker_volume.json` |
| jasper-input | `jasper-input` | `input` | **3b-1 (LANDED)** | trivial: `/dev/input/event*`, posts to control over TCP, no files |
| jasper-control | `jasper-control` | — | **3b-2 (designed)** | needs a **polkit rule** (broker `systemctl`/reboot) **and** group-readable secret env (the `jasper-doctor` it spawns fresh-reads every env file) — a deliberate secret-exposure change |
| jasper-web | `jasper-web` | `netdev`?/`bluetooth`? | **3b-3 (designed)** | the big one: NetworkManager polkit, BlueZ, `CAP_NET_ADMIN` scan-repair, `/etc/{bluetooth,avahi}` writes — and **wifi-lockout** is the worst-case brick for a headless speaker |

**3b-1 (landed) — voice/mux/input.** The file model is deliberately minimal:
`/var/lib/jasper` becomes `root:jasper 0770` (group-aware `ensure_state_dir`,
owner stays root → rollback-safe) and `speaker_volume.json` becomes group-rw
(`0660`, the one file all three of voice/mux/control read+write fresh). The one
secret-exposure 3b-1 needs is the **Google OAuth token tree** (`/var/lib/jasper/
google/`): jasper-voice reads it *off disk* (not via env injection), so it
becomes group-`jasper` readable (`0750` dirs, `0640` files). That widens the
linked-member Gmail addresses + refresh tokens to the other jasper daemons
(mux/input — low attack surface); per-daemon isolation is Phase 4
(`LoadCredential`). No polkit. Cross-user `/run` sockets work via the shared
`jasper` group: a UNIX socket needs **write** permission to `connect()`, so
`jasper-control` joins the group (stays root) and `jasper-fanin`/`jasper-outputd`
join it with `UMask=0007` — their TTS/control sockets become `root:jasper 0770`
(the prior umask-derived `0755` only let root connect). `jasper-mux`'s control
socket gains a `chmod 0660`.

Measured on hardware (jts.local, `systemd-analyze security`, after a clean
reboot): **jasper-voice 6.2 → 2.3, jasper-mux 6.2 → 2.2, jasper-input 6.2 → 2.3**
(MEDIUM → OK); jasper-control stays 6.6 (deferred to 3b-2). Validated: all
daemons active with `NRestarts=0` under `SystemCallFilter=@system-service` (no
SIGSYS), a TTS cue rendered end-to-end through the non-root voice→fanin path,
`control`→`voice.sock` and `mux`→broker (the 3b-1 recovery path) work cross-user,
`speaker_volume.json` converges across voice/mux/control, voice reads its Google
tokens, and the Class-2 reconcilers stay root and run clean.

**systemd `StateDirectory=jasper` recursively chowns** `/var/lib/jasper`'s
contents to whichever of jasper-voice/jasper-mux started (group stays `jasper`,
modes preserved) — so cross-daemon reads must rely on **group** read (`0640`+),
never owner. That's why `speaker_volume.json` is `0660` and the Google tree is
`0640`; files only one daemon reads (its own state, or root-read files like the
control token) keep owner-only modes. Pinned by `tests/test_systemd_hardening.py`
(User/Group/caps/syscall-filter per dropped unit; the deferred daemons stay
root; the install↔unit user contract).

**3b-2 / 3b-3 (designed).** control pulls in the polkit rule (one rule granting
`jasper-control` the `MANAGED_UNITS` allowlist via `org.freedesktop.systemd1.
manage-units` + `manage-unit-files`, plus `org.freedesktop.login1.reboot`/
`power-off`) and the group-readable env-file modes its spawned `jasper-doctor`
needs. web pulls in NetworkManager access (a polkit rule or `netdev` membership
— verify on the Pi), BlueZ (`bluetooth` group or polkit), the `/etc/bluetooth`
+ `/etc/avahi/services` writes (chown to `jasper` or move behind a broker verb),
and a decision on the `CAP_NET_ADMIN` scan-repair (grant the cap, or accept its
graceful degradation to manual "Join by name"). Both stay hardened-root until
then.

**The drop is gated on recovery-path validation, not happy-path** (validate
recovery under the dropped user, or ship hardened-root and don't pretend the
drop is done). The 3b-1 increment validates the mux→broker path now; the
control-as-non-root paths validate with 3b-2:

| Recovery path (changed by the drop) | Now runs as | Induced-failure test |
|---|---|---|
| `system_supervisor` reboot | control (polkit) | hang `/healthz` / drop sshd banner → 3 ticks → confirm `systemctl reboot` fires |
| `shairport_supervisor` restart | control (polkit) | wedge RTSP `:7000` → confirm `reset-failed`+restart fires |
| `jasper-web` config-save restarts | web → broker | save a voice/source change → confirm broker restart lands |
| `jasper-mux` librespot recovery | mux → broker | force double-grab timeout → confirm librespot restart via broker |

Class-2 (unchanged — still root — regression smoke only): `jasper-wifi-guardian`
(kill `wpa_supplicant`), `jasper-aec-reconcile` (remove `/proc/asound/Array`),
`jasper-dac-init` (set Headphone 50%, reboot), `jasper-dongle-recover`
(re-enumerate dongle).

**Accepted trade — the broker becomes a restart *dependency*.** Once the
clients are non-root, `manage_units`' root fallback is structurally gone
(`geteuid() != 0`), so the broker is the only path. A wedged or mid-restart
`jasper-control` therefore means an in-flight wizard config-save restart
*fails* (fail-soft: logged `event=restart_broker.unavailable`, the wizard's
own warning fires, the config still persisted) rather than falling back to a
direct `systemctl`. This is the deliberate cost of one auditable privileged
boundary, and it is bounded: `jasper-control` is the most heavily supervised
daemon (Tier-1 watchdog + `StartLimitAction=reboot`), its own restart stays
systemd's job, and the failure is observable, not silent. When 3b lands, note
this in the `HANDOFF-resilience.md` Tier-3 row and `HANDOFF-observability.md`
debug-restart note — both of `jasper-control`'s *own* supervisor/debug
restarts become polkit-authorized (they call `systemctl` directly, not the
broker), which is a doc change those subsystems' canonical files will need.

## Phase 4 — secret credentialization (DESIGNED)

`LoadCredential=` + `systemd-creds encrypt` for the provider keys. JTS-specific
cost: secrets are read as env vars via `EnvironmentFile`, written by wizards at
runtime, so this is a `jasper/config.py` change (read `$CREDENTIALS_DIRECTORY`
for the ~8 secret fields) + a wizard-write-path change + the unit edit — a
contained refactor, not a one-liner. Cheap interim wins already covered by
Phase 1: per-daemon `EnvironmentFile` least-privilege and `ProtectHome=tmpfs`.

Last verified: 2026-06-16
