# Handoff: privilege separation (WS1)

> **Status: current-state reference + approved phased plan.** Phases 1, 2,
> 3a, and the full 3b Tier-A user drop (3b-1 voice/mux/input, 3b-2 control,
> 3b-3 web) have landed and are validated on hardware — **all five Tier-A
> daemons now run non-root.** Phase 4 (secret credentialization) and the
> Tier-B reconciler drop remain designed, not yet built. This doc is the
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
| jasper-control | `jasper-control` | `systemd-journal` | **3b-2 (LANDED)** | a **polkit rule** (broker/supervisor `systemctl`/reboot + a root `jasper-doctor-json` oneshot for /system/diagnostics), group-readable config it reads off disk, and `systemd-journal` for the journal-based /state cards |
| jasper-web | `jasper-web` | `bluetooth`, `systemd-journal` | **3b-3 (LANDED)** | the big one: a **polkit rule** for NetworkManager (the `/wifi/` wizard), the `bluetooth` group (BlueZ Alias) + `systemd-journal` (`journalctl -k`), group-writable `/etc/bluetooth` + `camilladsp/configs`; `CAP_NET_ADMIN` scan-repair withheld (degrades fail-soft) — **wifi-lockout** is the worst-case brick, so it was gated on failed-connect-rollback validation under the dropped user |

**3b-1 (landed) — voice/mux/input.** The file model is deliberately minimal:
`/var/lib/jasper` becomes `root:jasper 0770` (group-aware `ensure_state_dir`,
owner stays root → rollback-safe) and `speaker_volume.json` becomes group-rw
(`0660`, the one file all three of voice/mux/control read+write fresh). The one
secret-exposure 3b-1 needs is the **Google OAuth token tree** (`/var/lib/jasper/
google/`): jasper-voice reads it *off disk* (not via env injection), so it
becomes group-`jasper` readable (`0750` dirs, `0640` files). That widens the
linked-member Gmail addresses + refresh tokens to the other jasper daemons
(mux/input — low attack surface); per-daemon isolation is Phase 4
(`LoadCredential`). No polkit. The **Spotify OAuth token cache**
(`/var/lib/jasper/spotify/caches/`) is the same off-disk-read class — jasper-voice
writes it, and the now-non-root jasper-control (`/transport` title-match router)
+ jasper-web (`/spotify` wizard) read it — so it is likewise group-`jasper`
readable (`0640`, dir `2750` setgid). spotipy writes the cache `0600` by default,
so [`jasper.accounts.build_cache_handler`](../jasper/accounts.py) re-chmods each
write and an `install_jasper` migration widens any pre-existing cache. This was a
regression caught by the 3b-3 review's all-surfaces off-disk-read audit and fixed
in the follow-up: before the fix the dropped readers logged "Couldn't read cache"
on every poll (22k+/day on jasper-control) and reported linked accounts as
needs-relink. Cross-user `/run` sockets work via the shared
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

**3b-2 (LANDED) — control.** `jasper-control` drops to a non-root
`jasper-control` user (primary group `jasper`, no supplementary groups —
no ALSA/input, just TCP + a localhost CamillaDSP WebSocket) with
`CapabilityBoundingSet=` (empty) + `SystemCallFilter=@system-service`. Two
coupled artifacts make the drop work:

- **The polkit rule** ([`deploy/polkit/49-jasper-control.rules`](../deploy/polkit/49-jasper-control.rules),
  installed to `/etc/polkit-1/rules.d/` by `install.sh`; polkitd auto-reloads).
  It grants the `jasper-control` user `org.freedesktop.systemd1.manage-units`
  **scoped per-unit** to the `MANAGED_UNITS` allowlist via `action.lookup("unit")`,
  plus `org.freedesktop.login1.reboot`/`power-off` and their `-multiple-sessions`/
  `-ignore-inhibit` variants. It keys on `subject.user` **only** — a sessionless
  system daemon has `subject.active == false`, so the desktop `subject.active`
  idiom would never fire. The allowlist is pinned set-equal to
  `restart_broker.MANAGED_UNITS` by `tests/test_polkit_jasper_control.py` so the
  broker authz and the polkit grant can't drift.

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

- **Group-readable secret env (`0640` group `jasper`).** `jasper-control` does
  two off-disk fresh reads a non-root user must keep doing: its `/state` +
  `/system/snapshot` read `home_assistant.env` (the HA bearer token), and
  `/system/diagnostics` spawns `jasper-doctor`, which fresh-reads **every**
  `env_load.ENV_FILES` path and (full profile) `Config.from_env` — the provider
  API keys + `GOOGLE_CLIENT_SECRET` + `JASPER_HA_TOKEN`. So `jasper.env`
  (`chgrp jasper` — it was `0640 root:root`, group `root`) plus the wizard
  secrets `voice_provider.env` / `spotify_credentials.env` /
  `google_credentials.env` / `home_assistant.env` become `0640` group `jasper`,
  and so does `control_token` — the Phase-2 gate token, which `_stored_token()`
  fails safe to gate-OFF on `EACCES`, so an unreadable token would **silently
  disable the mandatory gate** (the `StateDirectory` chown can make its owner
  `jasper-voice`, hence group-read, not owner-read, is required). Wizards write
  these at the new `SECRET_ENV_MODE` (`0640`); an install migration
  (`widen_control_secret_env_modes`) widens existing files on upgrade so a Pi
  that never re-saves a wizard doesn't break `/state` + the doctor. This widens
  the secrets to all `jasper`-group daemons — the same documented group-exposure
  accept as the 3b-1 Google tree; per-daemon isolation is Phase 4
  (`LoadCredential`). `/etc/avahi/services` becomes group-`jasper` writable
  (setgid) so the non-root daemon can still render the opt-in peering advert.

- **The full off-disk-read surface (the completeness the secret-env bullet
  alone missed).** `jasper-control` reads more than the secret env off disk for
  `/state` + `/system/diagnostics`, and the drop degrades each unless handled:
  - **`/system/diagnostics` runs the doctor as ROOT.** `jasper-doctor` is a
    root tool (audio/mixer/journal probes, `sudo -u <renderer> aplay`) — running
    it in-process from the non-root jasper-control made ~7 checks fail on
    permissions (false red). So the report is produced by a root
    `jasper-doctor-json.service` oneshot that jasper-control `systemctl start`s
    via its polkit manage-units grant (the unit is in `MANAGED_UNITS`); the
    doctor's `--json --out PATH` writes the report `0640` and exits 0 so a
    "report with failures" never flips the oneshot to `failed`. Full fidelity,
    no new privilege primitive.
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
Validated, including the self-review fixes: `/system/diagnostics` returns the
root-fidelity report (0 fails / 93 checks, matching `sudo jasper-doctor`); the
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
systemd's job, and the failure is observable, not silent. With 3b-2 landed,
`jasper-control`'s *own* supervisor/debug restarts (they call `systemctl`
directly, not the broker) are now polkit-authorized — noted in the
`HANDOFF-resilience.md` Tier-3 / system-supervisor sections and the
`HANDOFF-observability.md` debug-restart note.

## Phase 4 — secret credentialization (DESIGNED)

`LoadCredential=` + `systemd-creds encrypt` for the provider keys. JTS-specific
cost: secrets are read as env vars via `EnvironmentFile`, written by wizards at
runtime, so this is a `jasper/config.py` change (read `$CREDENTIALS_DIRECTORY`
for the ~8 secret fields) + a wizard-write-path change + the unit edit — a
contained refactor, not a one-liner. Cheap interim wins already covered by
Phase 1: per-daemon `EnvironmentFile` least-privilege and `ProtectHome=tmpfs`.

Last verified: 2026-06-16
