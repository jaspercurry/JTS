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
  types it; this also closes #712's `/rooms/` token gap for free (every
  canonical page now carries it).
- **Extended gated set.** Added `/system/restart/voice` + `/system/restart/audio`
  to the four #712 routes (poweroff / reboot / mic-mute / grouping-set).
- **Honest posture (the chosen tradeoff).** Because the token is auto-delivered
  over the LAN, a determined LAN device that fetches the page can read it too —
  so this is **defense-in-depth against drive-by / CSRF / casual curl on the
  annoyance-class routes, not a hard boundary** against a compromised LAN device.
  The real containment of the serious threats (secret theft, persistence, pivot)
  is the daemon hardening (Phase 1) + the user drop (Phase 3). This posture was
  chosen deliberately to keep plug-and-play frictionless.

Pinned by `tests/test_control_token.py` (ensure/current/idempotence/0600/meta),
`tests/test_http_js_control_token.py` (meta-first delivery), and the
server-frozenset-derived gating tests in `tests/test_control_server.py`.

## Phase 3 — restart broker + Tier-A user drop (DESIGNED; gated on recovery validation)

The broker and the user drop are one phase: the broker is only load-bearing once
the clients are non-root (a root client could `systemctl` directly). `jasper-control`
is already the de-facto broker (9 privileged restart sites live there). Finish it
as a local **UNIX socket + `SO_PEERCRED`** for in-host clients (`jasper-web`'s 13
restart sites, `jasper-mux`'s librespot recovery, `correction`'s renderer pause —
peer-cred uid check *is* the auth), with a **closed verb vocabulary** scoped to a
unit allowlist and **one polkit rule** granting the broker's user that allowlist;
the broker's own restart stays systemd's job (no bootstrap deadlock).

Then drop the 5 Tier-A daemons to dedicated users, add `CapabilityBoundingSet=` +
`SystemCallFilter=@system-service`:

| Unit | User | Groups | Special grants |
|---|---|---|---|
| jasper-voice | `jasper-voice` | `audio` | RT via `LimitRTPRIO`/`LimitMEMLOCK` (proven on `jasper-aec-bridge`); udev rule for XVF USB `2886:001a`; secrets via `LoadCredential` |
| jasper-control | `jasper-control` | — | one polkit rule → manage the unit allowlist |
| jasper-web | `jasper-web` | — | broker client; the `/etc/bluetooth` write moves behind a broker verb |
| jasper-mux | `jasper-mux` | — | broker client (librespot recovery) |
| jasper-input | `jasper-input` | `input` | `/dev/input/event*` |

**The drop is gated on recovery-path validation, not happy-path** (validate
recovery under the dropped user, or ship hardened-root and don't pretend the
drop is done):

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

## Phase 4 — secret credentialization (DESIGNED)

`LoadCredential=` + `systemd-creds encrypt` for the provider keys. JTS-specific
cost: secrets are read as env vars via `EnvironmentFile`, written by wizards at
runtime, so this is a `jasper/config.py` change (read `$CREDENTIALS_DIRECTORY`
for the ~8 secret fields) + a wizard-write-path change + the unit edit — a
contained refactor, not a one-liner. Cheap interim wins already covered by
Phase 1: per-daemon `EnvironmentFile` least-privilege and `ProtectHome=tmpfs`.

Last verified: 2026-06-16
