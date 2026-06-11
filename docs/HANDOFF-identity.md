# Handoff: speaker identity — names, collisions, and the reconciler

How a JTS speaker knows what it's called, what happens when two
speakers fight over a name, and how the system keeps the management UI
reachable through all of it. Canonical home for the identity layer —
other docs link here.

## The three names (plus one)

A speaker's "name" is really three loosely-coupled names plus a
display name:

| Name | Set by | Lives in | Consumed by |
|---|---|---|---|
| **OS hostname** | Pi Imager / `hostnamectl` | `/etc/hostname` | What Avahi *tries* to advertise (`<hostname>.local`) |
| **Avahi effective hostname** | Avahi (RFC 6762 conflict resolution) | avahi-daemon runtime state | What the LAN *actually resolves*. Differs from `<os>.local` after a collision rename (`jts.local` → `jts-2.local`) |
| **`JASPER_HOSTNAME`** | install.sh seed / operator | `/etc/jasper/jasper.env` | The *intended* identity: management-host allowlist, TLS cert CN/SAN, Spotify/Google OAuth bounce (`?host=`), spoken management URLs, `Config.management_url` |
| **Display name** | `/speaker/` wizard | `/var/lib/jasper/speaker_name.env` | AirPlay / Spotify Connect / Bluetooth / USB device names, `_jasper-control._tcp` TXT `name=` |

The display name is independent and already wizard-owned (single
writer, restart fan-out on save) — it needs no reconciliation. The
first three are the fragile set: **nothing keeps them in sync**, and
before the reconciler existed, drift was silent until something
user-visible broke.

## What breaks when the names drift

- **Collision rename** (two devices claim `jts`): Avahi silently
  renames the loser to `jts-2.local`. The OS hostname doesn't change,
  no log says it happened, and the only name the speaker still answers
  to is one the management-host allowlist used to reject — a full UI
  lockout, the same user-visible shape as the 2026-06-11
  `Host: 0.0.0.0` regression.
- **Manual `hostnamectl` rename**: `<new>.local` resolves (Avahi
  follows the OS hostname), but `JASPER_HOSTNAME` is stale — the TLS
  cert warns on `/correction/`, OAuth bounces land on the old name,
  cues speak the old URL.
- **Stale laptop state**: `.env.local` / ssh aliases still point at
  the old name; deploys target whoever owns it now.

## The identity reconciler

[`deploy/bin/jasper-identity-reconcile`](../deploy/bin/jasper-identity-reconcile)
— a `Type=oneshot` unit run at boot **and every 5 minutes**
(`jasper-identity-reconcile.timer`), because a collision rename lands
when the *other* device joins the LAN, not when we boot. Zero resident
RAM; each run is a handful of subprocesses (~10 ms).

It is deliberately a **pure observer** (mirrors the WiFi guardian's
`stash_stale` no-op philosophy): the single writer of
`/var/lib/jasper/identity.env`, and it never rewrites `jasper.env`,
never renames the host, never restarts daemons. Convergence is the
operator's deliberate act (`scripts/rename-speaker.sh`); the
reconciler makes drift visible and keeps the UI reachable meanwhile.

```sh
# What it writes (mode 0644 — hostnames are LAN-broadcast by definition):
JASPER_IDENTITY_OS_HOSTNAME=jts3
JASPER_IDENTITY_AVAHI_HOSTNAME=jts3.local    # effective, post-rename
JASPER_IDENTITY_CONFIGURED_HOSTNAME=jts3.local
JASPER_IDENTITY_AVAHI_AVAILABLE=1
JASPER_IDENTITY_COLLISION=0                  # avahi base != os hostname
JASPER_IDENTITY_DRIFT=0                      # configured != avahi
JASPER_IDENTITY_CHECKED_AT=2026-06-11T16:40:00Z
```

Avahi's effective name comes from
`busctl call org.freedesktop.Avahi / org.freedesktop.Avahi.Server
GetHostNameFqdn`; if avahi/busctl is unavailable the script falls back
to assuming `<os>.local` and flags `AVAHI_AVAILABLE=0`.

## How the management UI stays reachable (the lockout fix)

Two layers in
[`jasper/http_security.py`](../jasper/http_security.py), both
additive to the existing allowlist (configured name, OS hostname,
private IPs, `JASPER_MANAGEMENT_ALLOWED_HOSTS`):

1. **Avahi-suffix family, pure logic** —
   `_is_avahi_suffix_of_local_hostname` accepts `<os-hostname>-N` /
   `<os-hostname>-N.local` for numeric N. Closes the lockout window
   *instantly* (no file, no subprocess) for the collision-rename case.
   Scoped tight: our own hostname base + purely numeric suffix only;
   `.local` can't be attacker public DNS (RFC 6762 reserves it).
2. **Reconciler-observed names** —
   [`jasper/identity_state.py`](../jasper/identity_state.py)
   `effective_hostnames()` reads `identity.env` (mtime-cached, one
   `stat()` per request, fail-soft empty set when absent) and the
   allowlist accepts anything the speaker verifiably answers to.

Long-lived daemons must use `identity_state` (fresh file reads — the
[`provider_state`](../jasper/voice/provider_state.py) lesson), never
cache identity from `os.environ` at startup.

## Observability

```sh
# Live state (status: ok | drift | collision | absent):
curl -s http://jts3.local:8780/state | jq .resilience.identity

# Reconciler journal:
journalctl -u jasper-identity-reconcile | grep event=identity_reconcile

# Doctor (identity coherence + cert SAN vs advertised name):
sudo /opt/jasper/.venv/bin/jasper-doctor | grep -E "identity|cert"

# Manual run:
sudo systemctl start jasper-identity-reconcile
```

Doctor checks:
- `check_identity_coherence`
  ([network.py](../jasper/cli/doctor/network.py)) — collision/drift
  warnings with remediation, plus snapshot-staleness (timer dead?).
- `check_correction_cert_hostname`
  ([correction.py](../jasper/cli/doctor/correction.py)) — leaf-cert
  SAN must cover the advertised name; warn → redeploy regenerates.
- `check_hostname_avahi_consistency` (pre-existing) — live
  avahi-resolve probe of `<os>.local` vs our own IPs.
- `check_management_surface` ([web.py](../jasper/cli/doctor/web.py))
  — end-to-end browser-path probe (nginx → wizard → control guard),
  also run by every deploy.

## Renaming a speaker — the supported way

```sh
bash scripts/rename-speaker.sh jts4          # from the laptop
bash scripts/rename-speaker.sh jts4 --no-deploy
```

One operation converges everything: collision-probes the new name via
avahi *from the Pi*, `hostnamectl` + `/etc/hosts`, `JASPER_HOSTNAME`
in `jasper.env`, avahi restart, immediate identity-reconcile, laptop
`.env.local`/`CLAUDE.local.md` flip, then a full deploy under the new
name (TLS leaf cert SAN regeneration + daemon restarts + the
management-surface verification probe). Renaming by hand leaves the
derived surfaces drifted — don't; if you did, the doctor + dashboard
will say so, and a `rename-speaker.sh` to the *same* name re-converges
the derived surfaces.

Other checkouts pointing at the old name: `bash scripts/use
<new>.local`.

## Collision playbook (two speakers, one name)

Symptom: a speaker stops answering at its name; `/state.resilience
.identity` on the renamed one shows `status=collision`; doctor warns;
the UI is still reachable at the suffixed name
(`http://jts-2.local/`).

Fix: pick a unique name for one of them —
`bash scripts/rename-speaker.sh <unique-name>` against the renamed
speaker (its `.env.local` checkout or `PI_HOST=jts-2.local`).

## Boundaries / non-goals

- The reconciler does not auto-rename or auto-rewrite `jasper.env` —
  a wrong automated write here could fight an operator mid-rename;
  visibility + reachability are the automated part, convergence is
  deliberate.
- Display-name plumbing stays with the `/speaker/` wizard.
- Peer addressing by stable `peer_id` (multiroom leader pinning,
  laptop deploy-target verification) is the next layer up — see the
  multiroom docs once landed.

Last verified: 2026-06-11
