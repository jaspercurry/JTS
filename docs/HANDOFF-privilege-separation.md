# Handoff: privilege separation (WS1)

> **Status: current-state reference + approved phased plan.** Phases 1, 2,
> 3a, and the full 3b Tier-A user drop (3b-1 voice/mux/input, 3b-2 control,
> 3b-3 web) have landed and are validated on hardware — **all five Tier-A
> daemons now run non-root.** Phase 4a (secret compartmentalization, Group A:
> the LLM API keys + Google moved to a `jasper-secrets` compartment readable
> only by voice+web) and Phase 4b (Group B: HA + Spotify moved to
> `jasper-intsecrets` readable/writable by voice+control+mux+web) have landed.
> The remaining WS1 scope is now explicit below: the streambox `jasper-web`
> drop still needs Zero-class hardware validation, and the Tier-B privileged
> support surfaces are mapped for small follow-up PRs. The first Tier-B DAC
> mixer increment has landed: boot pin + drift monitor now run as
> `jasper-recon`, while the udev hotplug fast path remains a documented root
> exception. The Phase 4
> mechanism was revised from the original `LoadCredential`/`systemd-creds`
> sketch to group compartments after a hardware probe (see Phase 4). This doc is the
> threat-model + ADR for the work; update it as each phase lands.

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
  `jasper-dongle-recover`, `jasper-wifi-guardian`,
  `jasper-wifi-recover`; `jasper-aec-tune`, `jasper-wake-enroll`,
  `jasper-noise-capture`): short-lived, systemd-launched, **not
  network-reachable**, and the components doing the scariest privileged ops
  (nmcli network recreate, journal/kernel-log scan repair, amixer, XVF USB
  writes, `/etc` writes).

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
  `/var/lib/jasper/control_token` (0640 group `jasper`, atomic) if absent, so
  the gate is mandatory with no operator action. It is **idempotent and never
  rotates** an existing token (a household's stored copy stays valid). Failure is
  non-fatal — the gate fail-safes to off rather than blocking the recovery
  surface.
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

Pinned by `tests/test_control_token.py` (ensure/current/idempotence/0640/meta),
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
source of truth `MANAGED_UNITS` allowlist, plus a tiny `START_ONLY_UNITS`
allowlist for fixed root helpers that may only be `start`ed. Every request and
denial emits a stable `event=restart_broker.*` line with the peer uid/pid, verb,
units, reason.

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
only path, as intended. `MANAGED_UNITS | START_ONLY_UNITS` is the same unit set
the 3b polkit rule will grant `jasper-control`; the broker enforces the narrower
start-only semantics so authz and the polkit grant can't drift. Pinned by
[`tests/test_restart_broker.py`](../tests/test_restart_broker.py) (verb
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
| jasper-voice | `jasper-voice` | `audio`, `jasper-secrets`, `jasper-intsecrets` | **3b-1 + 4a/4b (LANDED)** | clean: no caps/RT/udev/polkit; config via env injection; 4a/4b groups grant only the secret compartments it must read/write |
| jasper-mux | `jasper-mux` | `jasper-intsecrets` | **3b-1 + 4b (LANDED)** | broker client (librespot recovery); shared broad file is `speaker_volume.json`; Spotify token refresh writes the 4b compartment |
| jasper-input | `jasper-input` | `input` | **3b-1 (LANDED)** | trivial: `/dev/input/event*`, posts to control over TCP, no files |
| jasper-control | `jasper-control` | `systemd-journal`, `jasper-intsecrets` | **3b-2 + 4b (LANDED)** | a **polkit rule** (broker/supervisor `systemctl`/reboot + a root `jasper-doctor-json` oneshot for /system/diagnostics), fresh HA/Spotify reads via `jasper-intsecrets`, group-readable non-secret config it reads off disk, and `systemd-journal` for journal-based /state cards |
| jasper-web | `jasper-web` | `bluetooth`, `systemd-journal`, `jasper-secrets`, `jasper-intsecrets` | **3b-3 + 4a/4b (LANDED)** | the big one: a **polkit rule** for NetworkManager (the `/wifi/` wizard), `jasper-secrets`/`jasper-intsecrets` for wizard-owned secret compartments, the `bluetooth` group (BlueZ Alias) + `systemd-journal` (`journalctl -k`), group-writable `/etc/bluetooth` + `camilladsp/configs`; `CAP_NET_ADMIN` scan-repair withheld (degrades fail-soft) — **wifi-lockout** is the worst-case brick, so it was gated on failed-connect-rollback validation under the dropped user |

**3b-1 (landed) — voice/mux/input.** The file model is deliberately minimal:
`/var/lib/jasper` becomes `root:jasper 0770` (group-aware `ensure_state_dir`,
owner stays root → rollback-safe) and `speaker_volume.json` becomes group-rw
(`0660`, the one file all three of voice/mux/control read+write fresh). The one
secret-exposure 3b-1 needed at the time was off-disk token access: Google OAuth
tokens and Spotify OAuth cache files were temporarily group-`jasper` readable so
the dropped readers would not lose access. Phase 4a/4b later narrowed those
again: Google moved to `jasper-secrets`; Spotify moved to `jasper-intsecrets`
because voice/control/mux/web can all refresh tokens. The regression that led to
the cache-mode guard is still relevant: before
[`jasper.accounts.build_cache_handler`](../jasper/accounts.py) re-chmodded
spotipy's `0600` writes, dropped readers logged "Couldn't read cache" on every
poll (22k+/day on jasper-control) and reported linked accounts as needs-relink.
Cross-user `/run` sockets work via the shared
`jasper` group. A UNIX socket
needs **write** permission to `connect()`, so
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
contents to jasper-voice (its sole owner since S2 — see below; group stays `jasper`,
modes preserved) — so cross-daemon reads must rely on **group** read (`0640`+),
never owner. That's why `speaker_volume.json` is `0660` and the Google tree is
`0640`; files only one daemon reads (its own state, or root-read files like the
control token) keep owner-only modes. Pinned by `tests/test_systemd_hardening.py`
(User/Group/caps/syscall-filter per dropped unit; the deferred daemons stay
root; the install↔unit user contract).

**Shared files must be group-WRITABLE, not just group-readable (2026-06-19 fix).**
The paragraph above is about cross-daemon *reads*; the multi-*writer* files are
the trap. `usage.db`, `wake-events.sqlite3`, `timers.db`, and
`speaker_volume.json` are written by more than one same-`jasper`-group daemon,
and — before S2 (below) — the StateDirectory chown flipped their *owner* between
jasper-voice and jasper-mux on each restart. A file created at the umask-default
`0644` is group-read-**only**, so once the owner flipped, the non-owner daemon got
`sqlite3.OperationalError: attempt to write a readonly database` — and the
speaker then played the (false) `cant_connect` cue instead of answering.
Fix: **jasper-voice/-mux/-control set `UMask=0007`** (the same directive the
fanin/outputd *sockets* use, here applied to *files*) so everything they create
lands `0660`; `env-migrations.sh`'s `heal_shared_state_modes` repairs
pre-existing `0644` files on upgrade as a tight **allowlist** (deliberately
never the `0600` WiFi PSK in `wifi_guardian.env` — a blanket `chmod -R g+w`
would have leaked it); and `jasper-doctor`'s `check_state_dir_group_writable`
flags drift before it bites. Pinned by `tests/test_systemd_hardening.py` (the
UMask contract), `tests/test_install_state_group_write.py` (the heal + the
PSK-safety property), and `tests/test_doctor_state_files.py`.

**S2 — single StateDirectory owner.** `jasper-voice` is now the *sole*
`StateDirectory=jasper` owner; `jasper-mux` dropped its `StateDirectory` and
reaches `/var/lib/jasper` via `ReadWritePaths` instead. That removes the
owner-*flip* entirely — only voice re-chowns the tree, always to
`jasper-voice:jasper` — so a non-owner write no longer depends on restart order.
`UMask=0007` is still required (voice's single-direction re-chown still leaves
mux a non-owner of files voice created, so mux writes them via the group bit),
and `heal_shared_state_modes` still backfills pre-existing drift. The dir's
existence is guaranteed by voice's `StateDirectory` + `install.sh`'s
`ensure_state_dir`; mux's `Restart=always` rides out any transient
not-yet-created window. Pinned by `tests/test_systemd_hardening.py` (mux has no
`StateDirectory`; its `ReadWritePaths` covers `/var/lib/jasper`) and
`tests/test_mux.py`.

**3b-2 (LANDED) — control.** `jasper-control` drops to a non-root
`jasper-control` user (primary group `jasper`, no supplementary groups —
no ALSA/input, just TCP + a localhost CamillaDSP WebSocket) with
`CapabilityBoundingSet=` (empty) + `SystemCallFilter=@system-service`. Two
coupled artifacts make the drop work:

- **The polkit rule** ([`deploy/polkit/49-jasper-control.rules`](../deploy/polkit/49-jasper-control.rules),
  installed to `/etc/polkit-1/rules.d/` by `install.sh`; polkitd auto-reloads).
  It grants the `jasper-control` user `org.freedesktop.systemd1.manage-units`
  **scoped per-unit** to the `MANAGED_UNITS | START_ONLY_UNITS` allowlist via
  `action.lookup("unit")`, plus `org.freedesktop.login1.reboot`/`power-off` and
  their `-multiple-sessions`/`-ignore-inhibit` variants. It keys on
  `subject.user` **only** — a sessionless system daemon has
  `subject.active == false`, so the desktop `subject.active` idiom would never
  fire. The allowlists are pinned set-equal to the matching `restart_broker`
  constants by `tests/test_polkit_jasper_control.py` so the broker authz and the
  polkit grant can't drift.

  **`manage-unit-files` is deliberately NOT granted — this corrects the
  original design above.** Hardware testing (Pi 5, systemd 257, polkit 126)
  found that `manage-unit-files` (1) is invoked by systemd with **NULL details**,
  so `action.lookup("unit")` is undefined and it **cannot be unit-scoped**, and
  (2) is **consulted by `systemctl restart`** (the SysV-compat / unit-file path),
  so an unconditional `manage-unit-files` YES **silently re-opens
  restart-of-ANY-unit** (cron, nginx, sshd…) — defeating the per-unit
  `manage-units` allowlist that is the whole point. The only `enable`/`disable`
  `jasper-control` ever needed was a redundant defensive `enable jasper-voice`;
  voice's authoritative enable/disable is owned by the **root**
  `jasper-aec-reconcile` (Tier B), so that `_enable_systemd_unit` call was
  removed. The broker keeps `enable`/`disable-now` in its verb *vocabulary* (for
  a future root client / Phase-4 grant), but they fail-soft for the non-root
  broker.

- **Group-readable config/env on the broad state path (`0640` group `jasper`).**
  `jasper-control` does off-disk fresh reads a non-root user must keep doing:
  `/system/diagnostics` spawns `jasper-doctor`, which fresh-reads
  `env_load.ENV_FILES`, while `/state` reads selected state/config directly.
  Before Phase 4, that forced `spotify_credentials.env`,
  `google_credentials.env`, and `home_assistant.env` to become `0640` group
  `jasper`. After Phase 4, the broad upgrade helper owns only the non-secret
  files that remain in `/var/lib/jasper`: `voice_provider.env` (keyless),
  `control_token`, `household_secret`, and sound profile/settings state. The
  actual provider/integration secrets moved to `jasper-secrets` /
  `jasper-intsecrets`, and those compartment migrations own their permissions.
  `household_secret` stays on the broad state path because jasper-web mints the
  device-to-device credential while jasper-control verifies/adopts/clears it; an
  existing owner-only copy would degrade grouping auth to fail-safe open.
  `/etc/jasper/jasper.env` still needs `chgrp jasper` because it is created
  `0640 root:root`, and `control_token` must stay group-readable or
  `_stored_token()` fails safe to gate-OFF on `EACCES`, **silently disabling the
  mandatory gate**. The broad migration refuses symlinks before running root
  `chgrp`/`chmod` in the group-writable state directory. `/etc/avahi/services`
  becomes group-`jasper` writable
  (setgid) so the non-root daemon can still render the opt-in peering advert.

- **The full off-disk-read surface (the completeness the secret-env bullet
  alone missed).** `jasper-control` reads more than the secret env off disk for
  `/state` + `/system/diagnostics`, and the drop degrades each unless handled:
  - **`/system/diagnostics` runs the doctor as ROOT.** `jasper-doctor` is a
    root tool (audio/mixer/journal probes, `sudo -u <renderer> aplay`) — running
    it in-process from the non-root jasper-control made ~7 checks fail on
    permissions (false red). So the report is produced by a root
    `jasper-doctor-json.service` oneshot that jasper-control starts with
    `systemctl --no-block` via its polkit manage-units grant (the unit is in
    `MANAGED_UNITS`); the doctor's `--json --out PATH` writes the report
    `0640` and exits 0 so a "report with failures" never flips the oneshot to
    `failed`. `/system/diagnostics` serves the last cached report immediately
    and only schedules a background refresh when the snapshot is stale or
    missing. Full fidelity, no new privilege primitive, no dashboard request
    blocked on a live doctor run.
  - **`systemd-journal` supplementary group.** Three `/state` cards
    (`airplay_health`, `dial`, `wifi_guardian`'s last-action) read the journal;
    a non-root reader needs the group.
  - **Non-secret state widened to `0640`:** `sound_profile.json` /
    `sound_settings.json` (the EQ config the sound card reads).
  - **The WiFi PSK stash stays `0600` — deliberately NOT widened.** Unlike the
    secrets above (whose *values* jasper-control needs), it needs only the SSID,
    so exposing the PSK to the group would be gratuitous. `enabled` derives from
    a `stat`; the SSID fails-soft to `None` (gated on `os.access` so the read
    isn't even attempted, no WARNING spam); `active_ssid` (nmcli) + `last_action`
    (journal) carry the resilience story.

Measured on hardware (jts.local, `systemd-analyze security`): **jasper-control
6.6 → 2.6 OK** (2.6, not 2.5, for the `systemd-journal` supplementary group).
Validated, including the self-review fixes: `/system/diagnostics` returns a
root-fidelity cached report and schedules stale refreshes in the background; the
`/state` wifi-guardian / sound cards populate with **zero** permission-denied
WARNINGs; the non-root peering-advert write into the setgid `/etc/avahi/services`
succeeds. And the original matrix:
non-root with `NRestarts=0` under `@system-service` (no SIGSYS); manage-units
**scoping confirmed** (allowlisted units restart, `cron`/`nginx`/`sshd` denied);
the `shairport_supervisor` `reset-failed`+restart path works under polkit; a
**real `systemctl --no-block reboot` run as `jasper-control` fired** and the Pi
recovered with `jasper-control` back non-root + healthy; secrets readable; the
token gate still 403s an unauthenticated POST.

**3b-3 (LANDED) — web.** `jasper-web` (the wizard HTTP server) drops to a
non-root `jasper-web` user (primary group `jasper`, supplementary groups
`bluetooth` + `systemd-journal`) with `CapabilityBoundingSet=` (empty) +
`SystemCallFilter=@system-service`. The mechanism for each privileged surface
was empirically determined on hardware (jts.local, NM 1.52, polkit 126, systemd
257) via reversible `pkcheck` / `sudo -u jasper-web` / dummy-NM-profile probes
before the drop:

- **NetworkManager** ([`deploy/polkit/49-jasper-web.rules`](../deploy/polkit/49-jasper-web.rules)).
  The `/wifi/` wizard drives `nmcli`; NM's implicit-`any` defaults (the slot a
  sessionless daemon falls under) **deny** every action it needs:
  `settings.modify.system`/`.own` (`auth_admin_keep`/`auth_self_keep`),
  `network-control` + `wifi.scan` (`auth_admin`), `enable-disable-wifi` (`no`).
  So a JS polkit rule keyed on `subject.user == "jasper-web"` granting exactly
  those five actions is **required and load-bearing** — proven on hardware:
  without it a real `nmcli connection modify` as jasper-web is DENIED; with it
  it (plus `wifi rescan`, `connection delete`, and the `-s` saved-PSK GetSecrets
  the guardian stash needs) succeed identically to root. `netdev` is **neither
  necessary nor sufficient** on modern NM (polkit is authoritative) — omitted.
  Pinned by `tests/test_polkit_jasper_web.py`.
- **BlueZ adapter Alias/Powered** (`/speaker` rename, BT radio) — the
  **`bluetooth` group** (a D-Bus policy grant, not polkit). Proven:
  `sudo -u jasper-web busctl set-property … Adapter1 Alias` succeeds in-group.
- **`journalctl -k`** (Wi-Fi scan-suppression diagnostics) — the
  **`systemd-journal` group** (also fail-soft: returns `None`, so non-load-bearing).
- **NL80211 scan-repair** ([`jasper/wifi_scan_repair.py`](../jasper/wifi_scan_repair.py))
  needs `CAP_NET_ADMIN`; the cap is **deliberately withheld** — the most
  network-exposed daemon stays cap-less and the repair **degrades fail-soft**
  (the netlink send is `try/except`-wrapped → `event=wifi_scan_repair.attempt_failed`
  WARNING → the `/wifi/` page keeps "Join by name").
- **Group-writable dirs for atomic replace.** `os.replace()` needs write on the
  *directory*, so `/etc/bluetooth` (the BlueZ name persists across the rename's
  `bluetooth.service` restart, so `main.conf` is load-bearing, not just the
  runtime D-Bus Alias) and `/var/lib/camilladsp/configs` (the `/sound/` EQ
  editor) become `root:jasper 2775` (setgid). `/etc/avahi/services` was already
  `2775` from 3b-2; adding it to the unit's `ReadWritePaths` also fixes a
  **latent bug** — the `/speaker` avahi re-render silently no-op'd under
  `ProtectSystem=strict` (the dir was outside the writable set). The WiFi PSK
  stash stays `0600` owner `jasper-web` (the root guardian reads it fine — root
  reads all, so no group-widening).

`jasper-web` already routes its restarts through the broker (`manage_units`,
and it's in `BROKER_CLIENT_USERS`); once non-root the broker is the only path.
The full-profile unit is dropped and hardware-validated; the **streambox** web
unit stays root this increment (a Pi-class that can't be validated here) —
`install.sh` installs the web polkit rule + dir widenings in both profiles so
the streambox drop is a later one-line unit edit.

Measured on hardware (jts.local, `systemd-analyze security`): **jasper-web
6.5 → 2.5 OK**. Validated: jasper-web `NRestarts=0` non-root serving `/system/`
+ `/wifi/` + `/sound/` + `/speaker/` (200, **zero permission-denied** WARNINGs in
the journal — the off-disk-read audit, not just HTTP 200); NM scan/list, BlueZ
Alias set, and writes to all three widened dirs work as jasper-web; the restart
broker socket is reachable (group `jasper`); and the **failed-connect rollback**
ran under the dropped user — a nonexistent-SSID connect failed and `connection
up <active>` re-activated without `wlan0` ever dropping (the wifi-lockout brick
path the whole increment was gated on).

**The drop is gated on recovery-path validation, not happy-path** (validate
recovery under the dropped user, or ship hardened-root and don't pretend the
drop is done). The 3b-1 increment validated the mux→broker path; the
control-as-non-root paths were validated with the 3b-2 drop, and the
web-as-non-root paths (including the wifi-lockout brick path) with 3b-3
(✅ below):

| Recovery path (changed by the drop) | Now runs as | Validation result |
|---|---|---|
| `system_supervisor` reboot | control (polkit) | ✅ ran the exact `systemctl --no-block reboot` as `jasper-control` → authorized, Pi rebooted + recovered non-root |
| `shairport_supervisor` restart | control (polkit) | ✅ `reset-failed`+restart of `shairport-sync`/`nqptp` authorized as `jasper-control`; non-allowlisted units denied |
| `jasper-web` config-save restarts | web (non-root) → broker | ✅ (3b-3) non-root `jasper-web` reaches the broker socket (group `jasper`, `0660`); broker proxies `restart jasper-voice` (allowlisted) |
| `jasper-web` failed-connect rollback | web (polkit NM) | ✅ (3b-3) as `jasper-web`: a nonexistent-SSID connect failed, then `connection up <active>` re-activated — `wlan0` never dropped (no lockout) |
| `jasper-mux` librespot recovery | mux → broker | ✅ (3b-1) mux→broker; `librespot` is in the allowlist |

Class-2 (unchanged — still root — regression smoke only): `jasper-wifi-guardian`
(kill `wpa_supplicant`), `jasper-wifi-recover` (run scan repair + guardian
activation when Wi-Fi is down), `jasper-aec-reconcile` (remove
`/proc/asound/Array`), `jasper-dac-init` (set Headphone 50%, reboot),
`jasper-dongle-recover` (re-enumerate dongle).

**Accepted trade — the broker becomes a restart *dependency*.** Once the
clients are non-root, `manage_units`' root fallback is structurally gone
(`geteuid() != 0`), so the broker is the only path. A wedged or mid-restart
`jasper-control` therefore means an in-flight wizard config-save restart
*fails* (fail-soft: logged `event=restart_broker.unavailable`, the wizard's
own warning fires, the config still persisted) rather than falling back to a
direct `systemctl`. This is the deliberate cost of one auditable privileged
boundary, and it is bounded: `jasper-control` is the most heavily supervised
daemon (Tier-1 watchdog + `StartLimitAction=reboot`), its own restart stays
systemd's job, and the failure is observable, not silent. With 3b-2 landed,
`jasper-control`'s *own* supervisor/debug restarts (they call `systemctl`
directly, not the broker) are now polkit-authorized — noted in the
`HANDOFF-resilience.md` Tier-3 / system-supervisor sections and the
`HANDOFF-observability.md` debug-restart note.

## Phase 4 — secret compartmentalization (4a + 4b LANDED)

Closes the documented group-secret-exposure ACCEPT carried by 3b: the secret env
files + the Google/Spotify token trees were `0640` group `jasper`, readable by
ALL five jasper daemons. Phase 4 narrows each secret to only the daemons that use
it.

### Mechanism — group compartments, NOT LoadCredential/systemd-creds

The original sketch (`LoadCredential=` + `systemd-creds encrypt`) was revised
after probing the mechanism on jts.local (systemd 257). Three JTS realities make
it the wrong fit:

1. **Secrets are written at runtime by a non-root wizard** (jasper-web).
   `systemd-creds encrypt` needs the host key
   (`/var/lib/systemd/credential.secret`, `0600 root`) or a TPM; the Pi 5 has
   **no usable TPM** (`systemd-analyze has-tpm2` = partial, no driver/firmware),
   so encryption falls back to a host-key file *on the same SD card* as the
   ciphertext — near-worthless against the only at-rest threat (card theft), and
   a non-root wizard can't encrypt at all without a new privileged broker (the
   opposite of WS1's goal).
2. **Cross-daemon fresh reads.** jasper-control reads `home_assistant.env` (HA
   token) and `voice_provider.env` (provider name) *fresh on every /state*
   because it is not restarted on a wizard save. `LoadCredential` loads once at
   unit start → would go stale or force extra restarts.
3. **Mutable off-disk token trees** (Google, Spotify) are read+written by 2–4
   daemons → `LoadCredential` can't model them; they need group/ACL anyway.

And the isolation `LoadCredential`'s per-unit injection would add (even from a
same-user compromise) does not materialize here: jasper-voice + jasper-web both
legitimately need ~every secret (voice uses them; web writes + renders them), so
the two daemons that matter can't be isolated from each other by *any* mechanism.
Group compartments deliver the realizable exclusion (mux/control/input lose
access to secrets they don't use) at a fraction of the brick risk, preserve the
fresh reads, and cover the token trees uniformly. Plaintext on disk — the threat
model (trusted LAN; the structural gap was *root* RCE, closed by the 3b drop)
does not call for at-rest encryption, and no-TPM host-key encryption wouldn't
deliver it anyway.

**The StateDirectory constraint (why secrets must relocate).** Both jasper-voice
and jasper-mux declare `StateDirectory=jasper`, and systemd **recursively chowns
`/var/lib/jasper` to the unit's `User:Group` whenever the top-level owner doesn't
match** — so every file there is forced to group `jasper`, owner flip-flopping
between voice and mux. A dedicated secret group therefore **cannot** be applied
to files under `/var/lib/jasper` (verified on hardware: a file chgrp'd to a test
group reverted to `jasper` on the next mux start). The secrets must live in
**sibling directories** outside the StateDirectory, created by `install.sh` + a
`tmpfiles.d` rule (which *can* set an arbitrary group, unlike StateDirectory).

### Two compartments

| Dir (group; mode 2770 setgid) | Members | Holds |
|---|---|---|
| `/var/lib/jasper-secrets/` (`jasper-secrets`) | voice, web | `voice_keys.env` (the 3 LLM API keys, split out of `voice_provider.env`), `google_credentials.env`, the `google/` OAuth token tree |
| `/var/lib/jasper-intsecrets/` (`jasper-intsecrets`) | voice, control, mux, web | `home_assistant.env`, `spotify_credentials.env`, the Spotify token cache |

`2770` setgid: members rwx (read/write/traverse), non-members get nothing
(stronger than the old group-read — they can't even traverse); setgid → token
files written at runtime inherit the compartment group. `voice_provider.env` (now
keyless: provider + model/voice) and `transit.env` (the low-value MTA key) stay
in `/var/lib/jasper` group `jasper`, because jasper-control reads them fresh for
`/system/` and `/state.transit` and the MTA key is not worth a split.

### Phase 4a — Group A (LANDED): LLM keys + Google

- New `jasper-secrets` group ([`service-users.sh`](../deploy/lib/install/service-users.sh)),
  members jasper-voice + jasper-web (a `SupplementaryGroups=` on each unit).
- `/var/lib/jasper-secrets/` created by `install.sh` (`ensure_secrets_dir`) +
  [`deploy/tmpfiles/jts-secrets.conf`](../deploy/tmpfiles/jts-secrets.conf)
  (boot self-heal — `tmpfiles.d` can set the `jasper-secrets` group, which
  `StateDirectory` cannot).
- The 3 provider API keys are **split** out of `voice_provider.env` into
  `voice_keys.env` (`KEYS_FILE` in
  [`jasper/voice/provider_state.py`](../jasper/voice/provider_state.py)); the
  `/voice` wizard writes both via one `_write_split` helper, and `provider_state`
  + jasper-control keep reading the keyless `voice_provider.env` for the active
  provider. `config.py` is unchanged for the keys (still read via
  `EnvironmentFile`); only the Google path defaults move.
- `migrate_secrets_phase4a` (install) does a guarded **atomic `mv`** of the
  `google/` tree + `google_credentials.env` out of `/var/lib/jasper` (rewriting
  the absolute `token_path`s baked into `accounts.json`), re-groups them to
  `jasper-secrets`, and splits the keys out of `voice_provider.env` +
  `jasper.env`. Idempotent; never strips a key from the broad files until it is
  confirmed written to `voice_keys.env`.
- Result: jasper-mux/-control/-input lose the LLM API keys + the Gmail/Calendar
  refresh tokens (the monetizable + identity-grade secrets); only voice + web
  read them.

Pinned by `tests/test_systemd_hardening.py` (`test_secrets_compartment_phase4a`
— group created, voice+web source the secret files + can write the compartment,
mux/control/input do NOT), `tests/test_google_creds.py`
(`test_install_creates_google_dir_setgid` → 2770 group jasper-secrets),
`tests/test_voice_setup.py` (the split: keys land in `voice_keys.env`, never the
broad file), and `tests/test_secret_env_modes.py` (google_credentials.env no
longer in the broad widen set).

**Validated on hardware (jts.local, build 249a8f2a):** the install migration
moved the live Google tree (1 linked account) — `accounts.json`'s baked
`token_path` was rewritten and the token file was preserved byte-for-byte (mtime
unchanged) — and split the 3 API keys into `voice_keys.env`; the old
`/var/lib/jasper/google*` paths are gone and `voice_provider.env` is keyless
(group `jasper`, so control still reads the active provider). Compartment is
`2770 root:jasper-secrets`, files `0640`. Runtime: `systemd-analyze security`
**jasper-voice 2.3 OK / jasper-web 2.5 OK** (unchanged), `NRestarts=0`;
jasper-voice connected to OpenAI (key via `EnvironmentFile`) and logged
`google: 1 account(s) linked` (Google tree read via the group **with no
`ReadWritePaths` grant** — voice only reads); jasper-web rendered `/voice/` +
`/google/` 200; control `/state` shows `provider=openai`. **Exclusion confirmed:**
jasper-mux/-control/-input each get `Permission denied`; all-surfaces journal
audit **zero permission-denied**. A **second deploy** (the idempotent re-run,
`moved=0`) completed cleanly — confirming the migration's `set -e` safety.

### Phase 4b — Group B (LANDED): HA + Spotify

The remaining integration secrets live in a second sibling compartment,
`/var/lib/jasper-intsecrets/` (`2770` setgid, group `jasper-intsecrets`), members
{jasper-voice, jasper-control, jasper-mux, jasper-web}; jasper-input is excluded.
Machinery mirrors Phase 4a: group creation in
[`service-users.sh`](../deploy/lib/install/service-users.sh), direct install +
boot self-heal via
[`deploy/tmpfiles/jts-intsecrets.conf`](../deploy/tmpfiles/jts-intsecrets.conf),
a guarded `migrate_secrets_phase4b` in
[`env-migrations.sh`](../deploy/lib/install/env-migrations.sh), and unit
`SupplementaryGroups=` + `ReadWritePaths=` on voice/control/mux/web.

**Files relocated out of `/var/lib/jasper`:** `home_assistant.env` (the
`JASPER_HA_TOKEN`); `spotify_credentials.env` (`SPOTIFY_CLIENT_ID` — PKCE,
semi-public, moved with the rest so the compartment is whole); the Spotify token
cache — both the legacy `.spotify-cache` and the multi-account `spotify/` tree
(`accounts.json` + `caches/<name>.json`). Runtime defaults now point at
`/var/lib/jasper-intsecrets/.spotify-cache` and
`/var/lib/jasper-intsecrets/spotify/accounts.json`.

**Reader/writer matrix (verify against the units before editing):** the HA token
is read by {voice (the HA tool), control (`/state` HA card, fresh off-disk read),
web (`/ha` wizard)} — **NOT mux**. It still lands in the {voice,control,mux,web}
group, so mux gains a read it doesn't need — the documented 2-group accept (a 3rd
group would isolate it; deferred). Spotify (creds + cache) is read by all of
{voice, control, mux, web}.

**The key 4a-vs-4b difference: Spotify is read-WRITE, Google was read-only.**
spotipy persists refreshed tokens via `accounts.build_cache_handler` (the
`_GroupReadableCacheFileHandler` in [`jasper/accounts.py`](../jasper/accounts.py),
which re-chmods `0640` after every `save_token_to_cache`). voice, control
(`volume_ops`), and mux all build routers that refresh → **all WRITE the cache**.
So unlike 4a (voice read-only, no write grant), **4b has
`/var/lib/jasper-intsecrets` in `ReadWritePaths` on voice + control + mux + web**.

**`accounts.json` bakes absolute paths** — like google's `token_path`, spotify's
`accounts.json` stores absolute `cache_path` values
(`/var/lib/jasper/spotify/caches/<name>.json` on pre-4b installs);
`migrate_secrets_phase4b` rewrites that prefix on move (parallel to 4a's google
rewrite) so the per-account caches do not orphan. The new-location defaults live
in
[`jasper/config.py`](../jasper/config.py) (`spotify_cache_path`,
`spotify_accounts_path`) + [`jasper/accounts.py`](../jasper/accounts.py)
(`default_cache_path_for`).

Pinned by `tests/test_systemd_hardening.py` (`test_secrets_compartment_phase4b`
— group created, the four member daemons source/write the relocated Spotify
file/dir, old broad paths gone, input excluded), and
[`tests/test_install_secrets_migration.py`](../tests/test_install_secrets_migration.py)
(HA/Spotify move, `accounts.json` `cache_path` rewrite, legacy cache move,
idempotent re-run). The 4a migration lessons still apply: helpers end cleanly
under `set -e`, `mv` is followed by `chown -R root:jasper-intsecrets`, and
control reads `home_assistant.env` fresh for `/state` through group access (no
LoadCredential). `transit.env`'s MTA key stays group `jasper` (low value,
control reads `/state.transit` fresh).

**Validated on hardware (jts.local, build 470c3750-dirty):** first deploy moved
`spotify_credentials.env` + the live Spotify tree into
`/var/lib/jasper-intsecrets/`, rewrote `accounts.json` to the new cache prefix,
and left the old broad Spotify paths absent; no HA file existed to move
(`jasper-doctor` reports Home Assistant not configured). Compartment is
`2770 root:jasper-intsecrets`, files `0640`; voice/control/mux/web can read +
write there, while `sudo -u jasper-input` gets `Permission denied`. Runtime:
`systemd-analyze security` stayed OK (voice 2.3, control 2.6, mux 2.3, web 2.5),
`/spotify/`, `/ha/`, and `/system/data.json` returned 200, all checked daemons had
`NRestarts=0` (web idle-exited via socket activation), and the all-surfaces
journal audit had zero permission-denied lines. A **second deploy** completed
cleanly with no move lines, proving the idempotent re-run path. The Spotify
`invalid_grant` warning is pre-existing revoked-token state, not a migration
failure.

## Remaining WS1 scope (2026-06-17)

### Streambox `jasper-web` drop

The full-profile `deploy/jasper-web.service` is non-root and hardware-validated.
The streambox profile still installs
[`deploy/jasper-web-streambox.service`](../deploy/jasper-web-streambox.service)
as root on purpose. The streambox/Zero-class device was not available during the
3b-3 validation pass, and the streambox unit has a narrower service/socket
shape even though it serves many of the same wizard surfaces.

Do not remove the deferral test until validation happens on a streambox target.
The validation matrix for the drop is:

- socket activation and idle exit for `jasper-web-streambox.socket` /
  `jasper-web-streambox.service`
- profile-scoped env-file chain, including `/var/lib/jasper-intsecrets` for
  `/spotify/`
- `ReadWritePaths` for the streambox surfaces it serves
- HTTP 200 plus zero permission-denied journal lines for `/spotify/`,
  `/sources/`, `/sound/`, `/speaker/`, `/wifi/`, and `/rooms/`
- the same recovery-sensitive checks as full-profile web where the surface
  exists: NetworkManager rollback for `/wifi/`, broker reachability for
  config-save restarts, BlueZ speaker rename if Bluetooth is installed, and
  grouping reconcile through `/rooms/`

Pinned by
`tests/test_systemd_hardening.py::test_streambox_web_unit_stays_root_until_validated`.

### Tier-B privileged support inventory

Tier-B is not one unit. It is a set of privileged boot/udev/operator support
surfaces, plus adjacent long-running helpers, where "drop the unit user" is only
safe after separating **policy** from the privileged **apply** step. Some can
probably run as a dedicated `jasper-recon` user with narrow groups; others
should remain fixed-argv root-owned helpers because the privileged operation is
the whole job.

Rules for all Tier-B slices:

- Keep the resilience ladder from
  [`HANDOFF-resilience.md`](HANDOFF-resilience.md) intact. Wi-Fi recovery, DAC
  hotplug, AEC/mic reconcile, and dongle re-enumeration must not become less
  reliable.
- Do not add broad sudo or broad polkit grants. If a non-root policy process
  needs root, add a tiny root helper or a broker verb with closed argv and a
  unit/action allowlist.
- Do not expand `restart_broker.MANAGED_UNITS` for the deferred support units
  unless the specific slice designs that as its boundary. In particular,
  `jasper-wifi-guardian.service`, `jasper-wifi-recover.service`,
  `jasper-dac-init.service`, `jasper-audio-hardware-reconcile.service`, and
  `jasper-dongle-recover.service` stay outside the Tier-A restart broker today.
  If a graph transition needs one fixed helper, prefer `START_ONLY_UNITS` over
  general brokerability and pin that narrower verb contract in tests.
- A hardware-sensitive change is not "landed" until the relevant hardware path
  is validated on-device. For docs/tests-only work, say "planned" or "mapped."

| Surface | Trigger | Privileged operation today | Likely split |
|---|---|---|---|
| `jasper-dac-init.service` + [`deploy/bin/jasper-dac-init`](../deploy/bin/jasper-dac-init) | boot and output-hardware reconcile | Detect Apple USB-C DACs with `aplay -L`; run `amixer -c <card> sset Headphone 100% unmute`. | **Landed:** runs as `jasper-recon` (primary `jasper`, supplementary `audio`) with empty capabilities and `SystemCallFilter=@system-service`. No broker needed. |
| `jasper-headphone-monitor.service` + [`deploy/bin/jasper-headphone-monitor`](../deploy/bin/jasper-headphone-monitor) | long-running DAC drift monitor | Poll the same Apple DAC mixer and self-heal drift back to 100%. | **Landed:** same `jasper-recon` + `audio` permission contract as `jasper-dac-init`. |
| Apple dongle udev fast path + [`99-jasper-apple-dongle.rules`](../deploy/udev/99-jasper-apple-dongle.rules) | Apple dongle sound-card / USB add | udev runs the hotplug `amixer -c $card sset Headphone 100% unmute` fast path as root and writes USB autosuspend `power/control=on`; it also triggers `jasper-dongle-recover.service`. | **Root exception remains:** the immediate `RUN+=amixer` repair path stayed root-owned to preserve hotplug recovery. A later PR can replace it with a fixed root helper or systemd oneshot if real replug/`udevadm test` validation proves the replacement preserves Headphone repinning. |
| `jasper-wifi-guardian.service` + [`deploy/bin/jasper-wifi-guardian`](../deploy/bin/jasper-wifi-guardian) | boot after NetworkManager wait-online | Read the root-only PSK stash, inspect NM state, run `nmcli connection up`, `nmcli dev wifi connect`, and cleanup of broken profiles. | `jasper-recon` could own the stash plus a narrow NetworkManager polkit rule parallel to `jasper-web`, but the failed-connect/recreate path is lockout-sensitive. Not first without Wi-Fi hardware validation. |
| `jasper-wifi-recover.service` + timer + [`deploy/bin/jasper-wifi-recover`](../deploy/bin/jasper-wifi-recover) | periodic (~3 min) timer, manual operator retry | If Wi-Fi is active, exit after one NM read. If Wi-Fi is down, inspect recent kernel logs for brcmfmac scan suppression, run the bounded `jasper.wifi_scan_repair` CLI when warranted, then invoke the Wi-Fi guardian. | Keep root with the guardian for now. The scan-repair and guardian handoff are lockout-sensitive and should move only after the Wi-Fi recovery slice has hardware validation under a narrower user/polkit/root-helper design. |
| `jasper-aec-init.service` + `jasper-aec-init` | boot and AEC reconcile restarts | Raw XVF3800 USB control writes through `xvf_host`; `amixer` on the `Array` UAC mixer; volatile chip profile only, with `SAVE_CONFIGURATION` and `REBOOT` deliberately forbidden. | Needs a hardware-gated design. Either `jasper-recon` gets a device-specific udev group for the XVF control endpoint plus `audio`, or a root helper owns the raw USB writes. Brick-loop hazards mean this is not an early slice. |
| `jasper-aec-reconcile.service` + [`deploy/bin/jasper-aec-reconcile`](../deploy/bin/jasper-aec-reconcile) | install, boot, udev sound add/remove, `/wake/`, grouping | Read/write AEC env state, inspect `/proc/asound`, self-heal XVF mixer with `amixer`/`alsactl store`, and orchestrate `jasper-aec-init`, `jasper-aec-bridge`, `jasper-voice`, and `jasper-outputd`. | Split policy from apply. The policy can become `jasper-recon`; unit orchestration and mixer persistence likely need a root helper or a dedicated reconciler broker. Preserve stale-UDP clearing and voice parking. |
| `jasper-audio-hardware-reconcile.service` + [`deploy/bin/jasper-audio-hardware-reconcile`](../deploy/bin/jasper-audio-hardware-reconcile) | install, boot, udev sound add/remove | Detect output hardware, write `/var/lib/jasper/outputd.env` and `/run/jasper-output-hardware/output_hardware.json`, render `/etc/jasper/asoundrc.jasper.template`, toggle `jasper-dac-init`/monitor, and kick outputd/AEC reconcile. | Split policy from root apply. Rendering `/etc/jasper/*` and unit orchestration should stay behind a fixed root helper; state observation can move to `jasper-recon`. |
| `jasper-dongle-recover.service` | Apple dongle sound-card udev add | Fixed `systemctl reset-failed` and `start` sequence for Camilla/outputd/audio-hardware/AEC after Card A reappears. | Keep as a root-owned fixed-argv recovery helper unless a later reconciler broker explicitly absorbs it. It is already tiny and exists to preserve hotplug self-healing. |
| `jasper-grouping-reconcile.service` + `jasper.multiroom.reconcile` | `/rooms/`, install, grouping state changes | Apply snapserver/snapclient role changes via `systemctl`, write grouping env/FIFO runtime state, and kick `jasper-aec-reconcile` rather than restarting voice directly. | Candidate for `jasper-recon` policy with a root apply helper for systemctl. It is already broker-routed from Tier-A clients; do not broaden that surface while changing other Tier-B units. |
| `jasper-identity-reconcile.service` + timer + [`deploy/bin/jasper-identity-reconcile`](../deploy/bin/jasper-identity-reconcile) | boot and 5-minute timer | Read hostname/Avahi over subprocess/DBus and write non-secret `/var/lib/jasper/identity.env`. | Probably safe to move to `jasper-recon` or another non-root writer after verifying `/var/lib/jasper` write ownership. Low security value compared with hardware recovery paths, so do not use it to claim Tier-B done. |
| `jasper-usbsink-init.service` + `deploy/usbsink/jasper-usbsink-*` | `/sources/` USB audio input enable/disable | Load/unload kernel modules, patch `usb_f_uac2`, create/remove ConfigFS gadget descriptors under `/sys/kernel/config`. | Root helper by design. Treat separately from the reconciler drop; the privileged operation is kernel/configfs setup. |
| `jasper-bootloop-guard.service` + [`deploy/bin/jasper-bootloop-guard`](../deploy/bin/jasper-bootloop-guard) | early boot | Write runtime systemd drop-ins under `/run/systemd/system` to disarm reboot loops after repeated bad boots. | Keep root. This is part of the T5.1 safety ladder and should not depend on the restart broker or a non-root user. |

Operator CLIs are Tier-B-adjacent, not daemon RCE surface, but they should not be
forgotten:

| CLI | Privileged operation today | Direction |
|---|---|---|
| `jasper-aec-tune` | Stops/starts services, adjusts Camilla volume, captures audio, writes `/var/lib/jasper/aec_delay.txt`, and talks to XVF hardware. | Leave sudo-only until the AEC root-helper boundary exists. |
| `jasper-wake-enroll` | Requires root so it can stop/start `jasper-voice` and bind/capture the same mic/UDP resources without fighting the daemon. | Later split could use the restart broker plus an `audio`/state-writer user, but this is operator-only and not first. |
| `jasper-noise-capture` | Same stop/start and capture shape as wake enrollment, without active wake prompts. | Same direction as wake enrollment. |

### First Tier-B DAC mixer slice (landed 2026-06-17)

The first implementation slice after this mapping was **DAC mixer pinning**:
`jasper-dac-init.service` and `jasper-headphone-monitor.service` now run as
`jasper-recon` with primary group `jasper` and supplementary `audio`.
The Apple dongle udev fast path remains a root-owned exception by design.

Why this slice is first:

- it has no NetworkManager lockout risk
- it has no raw XVF USB control path and no chip brick hazard
- it does not need `systemctl` orchestration
- failure is observable (`event=apple_dongle.*`) and degrades to reduced analog
  headroom rather than a dead speaker
- the shell helpers already have focused tests and narrow, fixed operations

Validated before landing on Apple USB-C DAC hardware (`jts.local`, 2026-06-17):

- installer creates `jasper-recon` with primary group `jasper` and
  supplementary `audio`
- service unit(s) declare `User=jasper-recon`, `Group=jasper`, empty
  `CapabilityBoundingSet=`, and an appropriate system-call filter
- on Apple USB-C DAC hardware, `id jasper-recon` shows primary group `jasper`
  plus supplementary `audio`, and `sudo -u jasper-recon -g jasper amixer -c
  <card> -- sset Headphone 100% unmute` succeeds. The Raspberry Pi OS sudo on
  the validation host does not support the checklist's literal `-G audio`
  option, so the group membership was verified separately.
- the udev hotplug `amixer` fast path is deliberately left root-owned and
  documented/tested as the exception
- reboot with the dongle present keeps the control pinned at 100%
- `udevadm test /sys/class/sound/controlC2` queues the root `RUN+=amixer`
  command and `jasper-dongle-recover.service`
- headphone-monitor drift correction works under `jasper-recon` (70% → 100%)
- real Apple USB-C DAC remove/reinsert repins the control and recovers through
  `jasper-dongle-recover` and the output-hardware reconciler
- `jasper-doctor` reports no output-hardware regression and the journal has no
  permission-denied lines from the changed units

Remaining DAC mixer privilege work:

- replace the root `RUN+=/usr/bin/amixer ...` udev fast path only if a fixed
  root helper or systemd oneshot preserves immediate hotplug repinning under
  `udevadm test` and real Apple-dongle replug validation

For future Tier-B slices, do not compensate for failed hardware validation by
granting broad sudo, adding the units to `MANAGED_UNITS`, or weakening the
hotplug recovery path.

### Always-on core-audio daemons (`jasper-camilla` / `jasper-outputd` / `jasper-fanin`) — the un-tiered root surface

The Tier A/B split above does not name these three. They are **always-on** (like
Tier A) but **not network-facing** (unlike Tier A — Tier A *is* the RCE surface),
and they are long-running daemons, **not** the boot/udev one-shots of Tier B. So
they fall through the taxonomy and run as root unaddressed: `jasper-camilla` as
`root:root`; `jasper-outputd` / `jasper-fanin` as `root:jasper` (group-joined for
the cross-user TTS/control sockets — §3b-1, **not** a user drop). All three carry
`StartLimitAction=reboot` (verified on `jts.local`, 2026-06-21). This is the last
structural root surface in an otherwise-dropped system. Whether to drop it is a
**deliberate decision, not an oversight** — recorded here so it is considered,
with an honest account of what it buys.

**What dropping them buys — be honest (low direct, real second-order):**

- **They are not the RCE attack surface.** Unlike the Tier-A daemons (which parse
  untrusted SSIDs, form input, OAuth callbacks, mic audio, third-party LLM
  responses), the core-audio daemons read **local ALSA** + **JTS-written config
  files** — trusted local inputs, not network data. An attacker does not reach
  camilla/outputd/fanin *first*. So the **direct** marginal risk reduction is
  **low**, and dropping the network daemons first (Phase 3, done) was the right
  priority.
- **The benefit is second-order, and real:**
  - **Blast-radius containment.** Today a bug *in* camilladsp (a third-party Rust
    binary) or outputd reachable via a crafted config/audio, **or** a pivot from a
    compromised Tier-A daemon into the root audio stack, is **full-root device
    compromise** — read every secret, rewrite any file, persist, pivot to the LAN
    (the same lateral-movement risk this whole effort exists to contain — see the
    threat model). Non-root bounds it to the audio pipeline.
  - **Completeness / auditability.** "Every daemon is least-privilege **except**
    two always-on root holdouts" is the coherence gap where reasoning breaks — the
    false-degraded `/state` bug (PR #900) lived *exactly* at a root-writer /
    non-root-reader seam. "No `jasper-*` daemon runs as root" is a defensible,
    auditable posture; the holdouts are what an external pentest flags.
- **Bottom line: this is defense-in-depth + posture, NOT a correctness fix.** The
  active read-permission bug class (root-writes / non-root-reads) is closed by the
  group-readable config writers (PR #900 `sound/camilla_yaml`, PR #901
  `bluetooth/roles`) + the doctor permissions check — **without** touching these
  daemons. So this is "reach for it for completeness," not "must-fix."

**Why it is gated and high-risk — do NOT do it early:**

- **It depends on the read-permission work landing first.** Dropping camilla to
  non-root makes it a **new non-root reader** of every config it loads — so the
  group-readable-writer burn-down (the `test_atomic_io_conventions` allowlist) and
  the doctor permissions check (verifying each non-root daemon can read its
  declared inputs) **must be airtight first**, or camilla cannot open its config
  and the speaker plays **no audio**.
- **Reboot hazard.** All three carry `StartLimitAction=reboot` — a misconfigured
  drop (wrong `/dev/snd` ACL, denied real-time scheduling) does not degrade, it
  **reboot-loops the box**. Needs the `jasper-bootloop-guard` armed + on-device
  validation, exactly like the DAC-init slice above.
- **Critical path.** These *are* the audio path; a regression is "no sound" — the
  most user-visible failure mode.

**Design sketch (for when it is done):** a dedicated non-root audio user (extend
`jasper-recon`, or a new `jasper-audio`) with primary group `jasper` (config
read) + supplementary `audio` (`/dev/snd/*`), real-time scheduling via `rtprio`
limits + `CAP_SYS_NICE` instead of full root, and the configs already
group-readable (the prerequisite above). Sequence **camilla first** — it is the
least hardware-entangled (a config reader + ALSA, no XVF/USB control writes) and
its `systemd-analyze security` score is the easy win — then `outputd` / `fanin`
(more entangled with the loopback/DAC topology, though their socket group is
already set up). Validate on a bench / `jts3`, never the household box first.

**Decision trigger.** Do this when the goal is a *fully* defensible least-
privilege posture (e.g. external security-audit readiness); defer indefinitely if
the goal is closing *known* risk — that is already done by the phases above.
Either way it is the **last** WS1 phase and **must follow** the doctor
permissions check.

Last verified: 2026-06-19 (core-audio daemon root-surface section added
2026-06-21; the daemons' root + `StartLimitAction=reboot` status verified on
`jts.local` that day)
