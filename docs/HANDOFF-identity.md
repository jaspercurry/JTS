# Handoff: speaker identity — names, collisions, and the reconciler

How a JTS speaker knows what it is called, what happens when two speakers fight
over a name, and how the system keeps the management UI reachable through it.
Canonical home for the identity layer — other docs link here.

## The three names (plus one)

| Name | Set by | Lives in | Consumed by |
|---|---|---|---|
| **OS hostname** | Pi Imager / `hostnamectl` | `/etc/hostname` | What Avahi *tries* to advertise (`<hostname>.local`) |
| **Avahi effective hostname** | Avahi (RFC 6762 conflict resolution) | avahi-daemon runtime state | What the LAN *actually resolves*. Differs from `<os>.local` after a collision rename (`jts.local` → `jts-2.local`) |
| **`JASPER_HOSTNAME`** | install.sh seed / operator | `/etc/jasper/jasper.env` | The *intended* identity: management-host allowlist, TLS cert CN/SAN, Spotify/Google OAuth bounce (`?host=`), spoken management URLs, `Config.management_url` |
| **Display name** | fresh-install hostname seed, then the `/speaker/` wizard | `/var/lib/jasper/speaker_name.env` | AirPlay / Spotify Connect / Bluetooth / USB device names, `_jasper-control._tcp` TXT `name=` |

The display name is independent and wizard-owned after its one-time installer
seed (`jts4.local` seeds `JTS4`). An existing `speaker_name.env` is
authoritative and deploys never rewrite it; wizard saves remain the single
restart fan-out. It needs no reconciliation.

**The first three are the fragile set: nothing keeps them in sync.** What drift
costs:

- **Collision rename** (two devices claim `jts`): Avahi silently renames the
  loser to `jts-2.local`. The OS hostname does not change, no log says it
  happened, and the only name the speaker still answers to is one the
  management-host allowlist used to reject — a full UI lockout.
- **Manual `hostnamectl` rename**: `<new>.local` resolves (Avahi follows the OS
  hostname) but `JASPER_HOSTNAME` is stale — the TLS cert warns on
  `/correction/`, OAuth bounces land on the old name, cues speak the old URL.
- **Stale laptop state**: `.env.local` / ssh aliases still point at the old
  name, so deploys target whoever owns it now.

## The identity reconciler

[`deploy/bin/jasper-identity-reconcile`](../deploy/bin/jasper-identity-reconcile)
— a `Type=oneshot` unit run at boot **and every 5 minutes**
(`jasper-identity-reconcile.timer`), because a collision rename lands when the
*other* device joins the LAN, not when we boot. Zero resident RAM; each run is a
handful of subprocesses (~10 ms).

It is deliberately a **pure observer**: the single writer of
`/var/lib/jasper/identity.env`, and it never rewrites `jasper.env`, never renames
the host, never restarts daemons. Convergence is the operator's deliberate act
(`scripts/rename-speaker.sh`); the reconciler makes drift visible and keeps the
UI reachable meanwhile. A wrong automated write here could fight an operator
mid-rename.

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

Avahi's effective name comes from `busctl call org.freedesktop.Avahi /
org.freedesktop.Avahi.Server GetHostNameFqdn`; if avahi/busctl is unavailable the
script falls back to assuming `<os>.local` and flags `AVAHI_AVAILABLE=0`.

## How the management UI stays reachable

Two layers in [`jasper/http_security.py`](../jasper/http_security.py), both
additive to the existing allowlist (configured name, OS hostname, private IPs,
`JASPER_MANAGEMENT_ALLOWED_HOSTS`):

1. **Avahi-suffix family, pure logic** — `_is_avahi_suffix_of_local_hostname`
   accepts `<os-hostname>-N` / `<os-hostname>-N.local` for numeric N. Closes the
   lockout window *instantly* for the collision-rename case: no file, no
   subprocess. Scoped tight — our own hostname base plus a purely numeric suffix
   only, and `.local` cannot be attacker public DNS (RFC 6762 reserves it).
2. **Reconciler-observed names** —
   [`jasper/identity_state.py`](../jasper/identity_state.py)'s
   `effective_hostnames()` reads `identity.env` (mtime-cached, one `stat()` per
   request, fail-soft empty set when absent), so the allowlist accepts anything
   the speaker verifiably answers to.

**Long-lived daemons must use `identity_state`** (fresh file reads — the
[`provider_state`](../jasper/voice/provider_state.py) lesson), never cache
identity from `os.environ` at startup.

## Observability

```sh
# Live state (status: ok | drift | collision | absent):
curl -s http://jts3.local:8780/state | jq .resilience.identity

# Reconciler journal:
journalctl -u jasper-identity-reconcile | grep event=identity_reconcile

# Doctor (identity coherence + cert SAN vs advertised name):
sudo /opt/jasper/.venv/bin/jasper-doctor | grep -E "identity|cert"

# Manual run (always logs the full answer, even when unchanged):
sudo /usr/local/sbin/jasper-identity-reconcile --reason manual
```

**Journal discipline:** the timer ticks every 5 minutes forever, so
`event=identity_reconcile.*` lines record **transitions only** — a steady,
unchanged identity logs nothing, though the file is still rewritten each tick so
doctor's snapshot-staleness probe stays honest. Persistent conditions live on
the surfaces built for them: `/state.resilience.identity` and the doctor
warnings repeat for as long as a collision or drift exists; the journal shows
when it *started*.

Doctor checks:

- `check_identity_coherence` ([network.py](../jasper/cli/doctor/network.py)) —
  collision/drift warnings with remediation, plus snapshot staleness (timer
  dead?).
- `check_correction_cert_hostname`
  ([correction.py](../jasper/cli/doctor/correction.py)) — the leaf cert's SAN
  must cover the advertised name; warn → redeploy regenerates.
- `check_hostname_avahi_consistency` (network.py) — live avahi-resolve probe of
  `<os>.local` against our own IPs.
- `check_management_surface` ([web.py](../jasper/cli/doctor/web.py)) —
  end-to-end browser-path probe (nginx → wizard → control guard), also run by
  every deploy.

## Renaming a speaker — the supported way

```sh
bash scripts/rename-speaker.sh jts4          # from the laptop
bash scripts/rename-speaker.sh jts4 --no-deploy
```

One operation converges everything: collision-probe the new name via avahi *from
the Pi*, `hostnamectl` + `/etc/hosts`, `JASPER_HOSTNAME` in `jasper.env`, avahi
restart, immediate identity-reconcile, laptop `.env.local`/`CLAUDE.local.md`
flip, then a full deploy under the new name (TLS leaf-cert SAN regeneration,
daemon restarts, management-surface verification probe). **Renaming by hand
leaves the derived surfaces drifted** — don't; if you did, doctor and the
dashboard will say so, and a `rename-speaker.sh` to the *same* name re-converges
them. Other checkouts pointing at the old name: `bash scripts/use <new>.local`.

**Collision playbook** (two speakers, one name). Symptom: a speaker stops
answering at its name, `/state.resilience.identity` on the renamed one shows
`status=collision`, doctor warns, and the UI is still reachable at the suffixed
name (`http://jts-2.local/`). Fix: pick a unique name for one of them —
`bash scripts/rename-speaker.sh <unique-name>` against the renamed speaker (its
`.env.local` checkout, or `PI_HOST=jts-2.local`).

## Addressing a *specific* speaker — peer_id

**Names are transport; `peer_id` is identity.** Every speaker advertises its
stable UUID (`/var/lib/jasper/peer_id`, written once by the peering layer,
surviving renames, IP churn, and collision renames) as a `peer_id=` TXT record on
the always-on `_jasper-control._tcp` advert
([control_advert.py](../jasper/control_advert.py)). mDNS is unauthenticated, so
treat peer_id as a stable handle, not a security boundary — confirm
trust-sensitive operations over HTTP against the speaker itself.

The consumer today is the **laptop deploy guard**: `deploy-to-pi.sh` records the
target's peer_id into `.env.local` on first contact (`PI_PEER_ID=…`, TOFU) and
**aborts before rsync** when a later deploy's target identity does not match —
after a collision rename or a re-image, `PI_HOST` can resolve to a different
speaker than the checkout means. Deliberate re-image:
`JTS_ACCEPT_NEW_IDENTITY=1 bash scripts/deploy-to-pi.sh`. `scripts/use` resets
the recorded identity, since switching targets is a new TOFU. It requires
passwordless sudo: under the interactive-sudo fallback the guard skips with a
printed notice, because `ssh -tt` merges the password prompt into the captured
peer_id and verifying there would record garbage. Helper and outcome tokens:
`verify_or_record_peer_id` in [scripts/_lib.sh](../scripts/_lib.sh).

Last verified: 2026-08-25 (triage pass — reconciler unit/timer paths,
`identity_state.effective_hostnames`, `_is_avahi_suffix_of_local_hostname`, the
four doctor check names, and `verify_or_record_peer_id` rechecked against the
code. Speculative "planned consumers" of peer_id removed; they are the owning
subsystems' to describe when they exist.)
