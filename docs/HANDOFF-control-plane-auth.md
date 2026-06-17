# Handoff: control-plane authentication (device-to-device / household)

> **Status: design approved 2026-06-16 — Phases A-C landed on `main`,
> hardware-free verified, on-device validation pending; Phase D paused on a
> scope decision.** This is the threat-model + design + execution plan for how
> JTS authenticates *control* across speakers on the household LAN. The
> per-device **CSRF control token** (browser → its own speaker) already
> shipped and is documented in
> [HANDOFF-privilege-separation.md](HANDOFF-privilege-separation.md) Phase 2;
> this doc covers the gap that token does **not** address — **device-to-device
> (machine-to-machine) control**, chiefly the multiroom grouping fan-out — and
> reconciles a live contradiction between two canonical docs (see §2). The
> design (Option 1, §5) was ratified after a counter-proposal advocating HMAC
> request-signing was reviewed and **rejected** (§5 "Rejected: HMAC
> request-signing"). The reconciliation phase (§8 Phase A: rewrite multiroom §7
> + privilege-sep) and build Phases B-C are implemented; Phase D remains
> paused until the owner chooses the autonomous re-grouping scope. Update each
> phase as work lands (see §8 for per-phase status).

Read [HANDOFF-privilege-separation.md](HANDOFF-privilege-separation.md) (the
per-device token + daemon-hardening story) and
[HANDOFF-multiroom.md](HANDOFF-multiroom.md) §7 (the grouping control plane)
first — this doc sits exactly at their seam and supersedes both on the
narrow question of *who may control grouping across devices*.

## 0. TL;DR

- JTS's `control_token` is a **CSRF token**. CSRF tokens verify *request
  origin*, not *caller identity*, and are the wrong primitive for
  machine-to-machine calls (industry-standard guidance, §4).
- WS1 Phase 2 made that token **mandatory** on six routes, including
  `/grouping/set`. That silently broke two flows that predate it:
  1. **Leader → follower `/grouping/set`** (multiroom): each speaker
     auto-generates its *own* token, so the leader has nothing the follower
     will accept → `403 control_token_required`. **(device-to-device gap)**
  2. **The landing-page mic-mute button**: a static client that never sent
     the token at all → `403`, silently reverted. **(delivery bug)**
- Two canonical docs now **contradict** each other about `/grouping/set`
  (§2). That drift is the bug-of-record.
- **Proposal (§5):** stop using the per-device CSRF token for M2M. Introduce
  a **household credential** minted at the human pairing moment (`POST /bond`)
  and presented on the cross-device grouping path — separate from the CSRF
  token. This is the proportionate, prior-art-backed answer (ESPHome-class),
  and unlike the #739 browser-token-forward it survives the **autonomous
  re-grouping** case (leader re-asserts a bond after a follower reboots, with
  no browser in the loop).
- **Scope:** one workstream covering the device-to-device credential *and* the
  mic-mute delivery bug — every token-gated-route client + the M2M path as one
  auditable surface. Phased plan for multiple agents in §8.

## 1. The problem — one root cause, two symptoms

The unifying insight: **`control_token` is a CSRF token, and it is being asked
to do machine-to-machine authentication, which it cannot do.** A CSRF token
proves "this request came from a page on my own origin"; it carries no caller
identity and conveys no authorization (§4). That is fine for *browser → its own
speaker* (the case it was built for), and wrong for everything else.

Two concrete failures fall out of that one mismatch:

| Symptom | Surface | Why it 403s |
|---|---|---|
| **Device-to-device** | leader → follower `POST /grouping/set` (the `/rooms/` bond fan-out, and any autonomous re-grouping) | each speaker's `ensure_token()` mints a *distinct* token; the leader/browser only has *its own* speaker's token, never the follower's → the follower's gate rejects it |
| **Delivery** | the landing page's mic-mute button (`deploy/index.html`) | a hand-rolled static `fetch('/mic/mute')` that sends only `Content-Type`, no `X-JTS-Token`; on any non-OK it silently reverts the toggle with no error |

The delivery symptom is a missing-client bug (the page should carry the token,
like every wizard does). The device-to-device symptom is a **methodology gap**:
there is no credential a speaker can present to a *peer*. This doc is primarily
about the second; the first rides along because it is the same class
("a token-gated route reached by a client that cannot present the token") and
is cheap to close in the same pass.

## 2. Current state + the contradiction to resolve

The same route, `/grouping/set`, is described two incompatible ways in two
canonical docs:

| Doc | Claim about `/grouping/set` |
|---|---|
| [HANDOFF-privilege-separation.md](HANDOFF-privilege-separation.md) Phase 2 | In the **mandatory token-gated set** ("poweroff / reboot / mic-mute / grouping-set" + restart routes); token always armed. Also claims Phase 2 "closes #712's `/rooms/` token gap for free." |
| [HANDOFF-multiroom.md](HANDOFF-multiroom.md) §7 "Grouping control plane — threat model" | "**UNAUTHENTICATED by design**"; the `/rooms/` fan-out "grants no capability a LAN client lacked… not a new privilege." |

Both cannot be true. **Live behavior shows privilege-separation won:** the gate
is armed and `POST /grouping/set` (and `/mic/mute`) returns
`403 {"error":"control_token_required"}` to a tokenless caller (reproduced on
`jts.local`, June 2026, via nginx and directly on `:8780`). Consequences:

- The multiroom doc's "unauthenticated by design" is **now false**.
- The privilege-sep doc's "closes the `/rooms/` gap for free" is true only for
  *browser → own speaker*; the cross-device fan-out is **broken**, not closed.
- `jasper/web/rooms_setup.py`'s fan-out comment ("member checks its own gate;
  **default-off installs pass**") encodes the now-stale assumption that the gate
  is opt-in. Phase 2 made it mandatory; that assumption no longer holds.

**Resolution (this doc is the single source of truth for cross-device control
auth).** When §5 is approved and Phase C lands:
- HANDOFF-multiroom.md §7 changes from "unauthenticated by design" to
  "authenticated via the household credential — see HANDOFF-control-plane-auth.md."
- HANDOFF-privilege-separation.md Phase 2 drops the "closes the `/rooms/` gap"
  claim and points here for the device-to-device path.
- Both keep a one-line pointer to this doc now (added alongside this RFC) so a
  reader of either is told the cross-device story lives here.

## 3. Threat model (what we are actually defending)

JTS's accepted baseline is a **trusted home LAN** ([SECURITY.md](../SECURITY.md)).
The per-device control token is, by the privilege-sep doc's own words,
"defense-in-depth against drive-by / CSRF / casual curl on the annoyance-class
routes, **not a hard boundary** against a compromised LAN device." Our M2M design
must be **proportionate to that same bar** — strong enough to stop drive-by /
CSRF / casual curl from flipping a household's grouping, without pretending to
defend against a fully compromised LAN device (that is the daemon-hardening +
user-drop job, Phases 1/3 of privilege-sep, not this one).

What `/grouping/set` abuse actually buys an attacker: hijack output routing —
silence a speaker, force a speaker into a follower role, or (with the loud-output
paths) push audio across devices. The multiroom doc already flags that bonding
makes "LAN = trusted" *load-bearing across devices*. So the goal is: **once a
household has paired its speakers, a casual/cross-site/curl actor can no longer
reconfigure the group** — while keeping setup frictionless and the autonomous
self-heal paths working.

**Why not "just exempt M2M from the gate"** (the simplest option): that returns
`/grouping/set` to exactly the Snapcast posture (§4) the gate was added to close
— any LAN device curls a grouping change. It is a regression of the Phase 2
intent, so it is rejected (§5, Option 3).

## 4. Prior art (researched June 2026)

The universal pattern across the field: **a human-mediated pairing moment
bootstraps a persistent, machine-usable household/fabric credential; ongoing
device-to-device control authenticates against *that* — never against the
per-device setup/CSRF token.**

| System | Pairing (bootstrap) | Ongoing M2M auth | Weight | Lesson |
|---|---|---|---|---|
| **Matter** | SPAKE2+ PAKE from out-of-band passcode (PASE) | per-device **operational cert (NOC)** from a fabric CA; mutual-cert **CASE** sessions | heavy (PKI) | the gold-standard *shape*: pairing → fabric credential → M2M uses it |
| **HomeKit (HAP)** | SRP PAKE from 8-digit code → exchange & persist long-term Ed25519 keys | `pair-verify` proves possession of stored keys | medium (no CA) | lighter than PKI, per-device + revocable |
| **ESPHome** (HA appliance) | operator copies a 32-byte base64 **PSK** to device + controller | `Noise_NNpsk0` (ChaCha20-Poly1305), no certs | **light — closest crypto shape** | the *crypto shape* fits (symmetric PSK, no PKI). **Caveat (verified):** ESPHome keys are **per-device, never shared** (only Wi-Fi creds are shared) — so ESPHome is precedent for the *symmetric-no-PKI* shape, **not** for one shared household secret. Sharing across the household is a deliberate JTS proportionality trade (§5 / RFC 9257), not ESPHome practice. |
| **Snapcast** (OSS multiroom) | — | **none** — relies on network isolation | trivial | cautionary: "trust the LAN" for a control plane earned it **CVE-2023-36177** (JSON-RPC→RCE; NVD CVSS 9.8 Critical; fixed v0.30.0 by removing the process-stream type) + creds-in-messages |
| **TLS-PSK / IoT general** | manual key config | PSK or mTLS | varies | PSK is explicitly recommended for "closed environments configured in advance"; mTLS is stronger but needs PKI |

Sources: [OWASP CSRF cheat sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
· [TrustedSec — Auth vs CSRF](https://trustedsec.com/blog/basic-authentication-versus-csrf)
· [Microservice auth patterns (arXiv 2009.02114)](https://arxiv.org/pdf/2009.02114)
· [Matter commissioning (einfochips)](https://www.einfochips.com/blog/building-a-smarter-home-an-in-depth-look-at-matter-commissioning/)
· [Silicon Labs Matter security](https://docs.silabs.com/matter/latest/matter-fundamentals-security/)
· [Google Home commissioning primer](https://developers.home.google.com/matter/primer/commissioning)
· [Apple HomeKit communication security](https://support.apple.com/guide/security/communication-security-sec3a881ccb1/web)
· [ESPHome native API encryption](https://esphome.io/components/api/)
· [HA ESPHome integration](https://www.home-assistant.io/integrations/esphome/)
· [Snapcast issue #860 — improve JSON-RPC security](https://github.com/snapcast/snapcast/issues/860)
· [Snapcast CVE-2023-36177 (NVD, canonical)](https://nvd.nist.gov/vuln/detail/CVE-2023-36177) ([researcher writeup, cited as CVE-2023-52261](https://cavefxa.com/posts/snapcast-json-rpc-to-rce/))
· [RFC 9257 — Guidance for External PSK Usage in TLS](https://www.rfc-editor.org/rfc/rfc9257.html)
· [Avnet — IoT device auth](https://my.avnet.com/silica/solutions/security-services/secure-device-management-provisioning/iot-security-series/device-authentication-authorisation/)
· [TLS-PSK](https://en.wikipedia.org/wiki/TLS-PSK)

## 5. Proposal

Keep the gate (a casual/cross-site actor flipping your speakers' grouping is the
real `/grouping/set` threat), but **stop using the per-device CSRF token for the
device-to-device path.** Introduce a distinct **household credential**,
bootstrapped at JTS's existing human pairing moment — `POST /bond` on the
`/rooms/` wizard *is* the "commissioning" step.

- **Option 1 — household shared secret (RECOMMENDED).** At `/bond`, mint a
  single household secret (or reuse the household's existing one), persist it on
  each member (atomic `0640` group-jasper file under `/var/lib/jasper/`, mirroring
  the WS1-widened `control_token` + the Wi-Fi guardian stash), and present it on the
  cross-device grouping path via a **distinct** credential/header (e.g.
  `X-JTS-Household`) that each member verifies independently of its own CSRF
  token. Machine-usable + persistent → survives reboots and the autonomous
  leader-election re-grouping case (§6). No PKI; fits a 1 GB Pi; symmetric-PSK
  crypto shape (ESPHome-class), TLS-PSK guidance. **Named trade (RFC 9257):** one
  shared secret means *no per-device revocation* and a *whole-household blast
  radius* if a member's secret leaks — accepted here because the threat model
  excludes a compromised/observing LAN member, and RFC 9257 explicitly
  contemplates a shared PSK in a closed, manually-provisioned deployment *with
  compensating controls*. Cite ESPHome (per-device keys) only for the
  symmetric-no-PKI shape, never for the *sharing*.
- **Option 2 — per-device keys / mTLS (STRONGER, DEFERRED — and *not* over mkcert).**
  The Matter "operational cert / CASE" analog: per-device identity + revocation.
  Heavier (the real cost is certificate *lifecycle* — expiry, rotation, trust-store
  drift — not handshakes), and disproportionate while the per-device token itself
  is only CSRF-grade. **Do not reuse the `/correction/` mkcert CA for this:**
  mkcert's own README warns its `rootCA-key.pem` "gives complete power to intercept
  secure requests" and that mkcert "is meant for development purposes, not
  production" — a single household-wide MITM key read off any one Pi is strictly
  worse than the bearer it would replace. **Revisit trigger (the only one):** the
  threat model tightens to include an *untrusted LAN co-tenant* (guest VLAN,
  shared/managed network, untrusted member). On that day, stand up a purpose-built
  household CA (not mkcert) and prefer a **pairing code** at `/bond`
  (decision 2, §9) — which also closes the one residual a shared secret cannot: a
  malicious LAN device initiating its own `/bond`. Tracked as Phase E.
- **Option 3 — exempt M2M from the gate (REJECTED).** Allow tokenless LAN calls
  with no browser context. Matches Snapcast, re-opens exactly the
  "any LAN device curls `/grouping/set`" hole Phase 2 added the gate to close.

**Rejected: HMAC request-signing (reviewed 2026-06-16).** A counter-proposal
advocated presenting the household secret as an HMAC request *signature*
(`METHOD+PATH+SHA256(body)+timestamp+nonce`, constant-time verify, reject stale
timestamps + replayed nonces) instead of a static bearer. **Rejected for JTS,
three reasons:**
1. **Incoherent asymmetry.** The shipped `control_token` is *already* a static
   bearer over plain HTTP gating the same destructive routes (`/grouping/set`,
   `/mic/mute`, poweroff…). Hardening *only* the grouping door leaves an attacker
   who can't beat HMAC there free to curl `/system/poweroff` with the
   equally-LAN-readable bearer. The right distinction is a *different credential*
   (household vs per-device), **not a stronger primitive**.
2. **It defends an out-of-scope attacker.** Every HMAC win — secret-off-the-wire,
   body integrity, replay rejection — only bites an attacker who can *observe or
   tamper with* LAN traffic, which §3 explicitly concedes. Against the in-scope
   attacker (drive-by / CSRF / casual curl), a never-logged constant-time bearer
   and an HMAC have *identical* surface: the malicious page can't read a
   cross-origin response or set the header (SOP / Fetch-Metadata), and the curler
   lacks the secret either way.
3. **The timestamp window is *wrong on this hardware*.** Raspberry Pis have no
   battery-backed RTC; JTS configures no NTP itself, and `jasper-control` (which
   would verify the signature) does not gate on `time-sync.target`. On the
   headline self-heal path (leader re-bonds a *just-rebooted* follower), the
   follower's clock is `fake-hwclock`'s stale snapshot — so a 30–60 s freshness
   window would *reject the leader's valid request* and, with fail-closed, strand
   the bond until NTP lands. The machinery meant to protect the resilience path
   breaks it. (Evidence: [`deploy/jasper-web.service`](../deploy/jasper-web.service)
   no-RTC comment; [`jasper/cli/doctor/resilience.py`](../jasper/cli/doctor/resilience.py)
   fake-hwclock note.)

Stripping timestamp+nonce to a clock-free `HMAC(METHOD+PATH+SHA256(body))` leaves
only body-integrity — out-of-scope-defending again — and still forces a body-read
refactor of the gate (verify runs before the one-shot request body is read). So no
version of the signing upgrade earns its keep here. **Optional, not required:** if
leaked-credential blast-radius ever matters, send a per-target
`HMAC(household_secret, target_peer_id)` rather than the raw secret — clock-free,
no body, the verifier checks against its *own* `peer_id`, and a leaked header then
exposes one member's token instead of the household root (relevant because JTS
ships journal logs to the laptop via `fetch-pi-logs.sh`). ~5 lines in the
greenfield module; a nicety, not a fix.

**Chosen:** Option 1, as a **static bearer** in `X-JTS-Household`. It is the
smallest design that fits the existing system (a near-clone of `control_token`),
is the durable generalization of the #739 browser-token-forward stopgap, is the
only option that covers autonomous re-grouping, and — having rejected the HMAC
upgrade — is also the cheapest.

## 6. Design detail (Option 1)

**Credential module.** A new `jasper/control/household_credential.py` mirroring
`jasper/control/control_token.py`: `ensure()/current()/verify()` over
`/var/lib/jasper/household_secret` (`0640` group jasper — TWO non-root daemons
read it, jasper-web mints + jasper-control verifies/adopts/clears, so unlike
`control_token` it can't be 0600; atomic write, constant-time compare, fail-safe
"" on read error, never logged). Distinct file + distinct header from
`control_token` so the two trust domains never blur.

**Fail direction — mirror `control_token` (absent ⇒ accept, present ⇒ require).**
`verify()` returns True when the stored secret is absent/empty/unreadable, and
requires an exact constant-time match when it is present — the *opposite* of a
blanket fail-closed, and deliberate. **Absence is a legitimate "not-yet-paired"
state, not an attack signal.** Fail-closed-on-absent would (a) deadlock first-bond
bootstrap — the secret is distributed *over* the gated `/grouping/set` itself, so
an unbonded follower would 403 the very request that installs it — and (b) lock a
follower out of re-bonding if its secret file is ever lost. Safe within the threat
model: forcing the downgrade needs local write access (out of scope; such an
attacker could read the secret anyway). **Two honest nuances, stated not hidden:**
(1) the open window is not only the transient loss→re-bond case (the 2026-05-23
ext4-loss class) — a speaker that has NEVER been bonded is unpaired *permanently*,
until its first bond, so its `/grouping/set` is open the whole time. (2) This is
genuinely *weaker* than today's status quo for the unpaired case: `control_token`
is always armed (`ensure_token()` at startup) so it has no open window, whereas
this fail-safe re-opens `/grouping/set` to a casual-curl actor on any unpaired
speaker — worst case, output-routing hijack (forcing a solo speaker to follow an
attacker's leader). Accepted only as the trusted-LAN / annoyance-class residual the
multiroom doc already conceded ("unauthenticated by design"), and unavoidable for
TOFU bootstrap: the secret is distributed *over* the gated route, and a
header-gated variant just trades casual-hijack for a pairing-DoS (a curl adopts a
bogus secret and blocks the real bond). The doctor's "bonded but household
credential missing" check + `jasper-doctor` surface this state for a *paired*
speaker that lost its secret; a never-bonded speaker reads `ok` (correctly, it has
no household to protect yet). Pin the semantics
with tests (§8 Phase C) so a refactor can't silently flip it back to fail-closed
and brick re-bonding.

**Bootstrap at the pairing moment (`POST /bond`).** The leader (the speaker
whose `/rooms/` page the human used) ensures a household secret exists, then
distributes it to each member during the existing bond fan-out — the same loop
that already POSTs `/grouping/set` to members (`rooms_setup` `_post_grouping_to_member`).
The **first** distribution is accepted over the trusted LAN: this is *no weaker
than today* (the bond was already "unauthenticated by design"), and it
**upgrades the steady state** — once a household is bonded, every subsequent
grouping change requires the secret, so drive-by / CSRF / casual curl can no
longer flip the group. Residual: a malicious LAN device could still *initiate*
a bond to set its own secret — the same residual the whole LAN-trust posture
accepts, closed only by Option 2. State this honestly; do not over-claim.

**Gate behavior on `/grouping/set`.** Accept **either**:
- a valid **household credential** (`X-JTS-Household`) — the device-to-device
  path (peer fan-out, autonomous re-grouping), **or**
- a valid **control token** (`X-JTS-Token`) — the browser → *its own* speaker
  path (the local `/rooms/` page setting this box's own role; works today).

Both are legitimate ways to set grouping; the gate widens to recognize the M2M
credential rather than overloading the CSRF token.

**Autonomous re-grouping (the case #739 cannot reach).** When the peering /
leader-election path re-asserts a bond after a follower reboots — no browser
present — the leader daemon reads `/var/lib/jasper/household_secret` and presents
it on its `/grouping/set` to followers, who verify against their persisted copy.
This is *why* a persistent, machine-usable credential is required instead of
relaying a browser token: the resilience path has no browser. **Design seam to
call out:** today `rooms_setup._request_control_token` deliberately *relays* the
browser-supplied token and never injects one from disk. The autonomous path has no
browser to relay from, so it must read the household secret from disk daemon-side —
an explicit, intentional break of the relay-only invariant for this one path, not
an oversight.

**Lifecycle.** `POST /unbond` clears the household secret on each member it
dissolves (mirroring the bond fan-out). Rotation is an explicit operator action
(re-bond, or a future `jasper-household-credential` CLI à la
`jasper-control-token`). `install.sh` does **not** seed it — absence simply means
"not yet paired," and a lone speaker never needs it.

**Relationship to the per-device token.** Unchanged for browser → own-speaker.
The household credential is additive and orthogonal: distinct file, distinct
header, distinct trust domain (peer identity, not page origin).

## 7. The mic-mute landing-page gap (same workstream)

A self-contained delivery bug, included here because it is the other instance of
"a token-gated route reached by a client that cannot present the token," and the
audit (below) shows it is the *only* such client besides the M2M path.

- **Bug:** `deploy/index.html` `postMute()` POSTs `/mic/mute` with only
  `Content-Type`, no `X-JTS-Token`; on a 403 it silently calls
  `renderMic(!want_muted, …)` — the button snaps back with no error. Violates
  the project "no silent failure" rule for a *privacy* control.
- **Why it is hard:** the landing page is **static** HTML (nginx serves
  `/usr/share/jasper-web/index.html` directly; no `canonical_page()` render), so
  it gets neither the `<meta name="jts-control-token">` injection nor the shared
  `http.js` token logic the wizards use.
- **Fix:** bake `<meta name="jts-control-token">` into `index.html` at install
  time, reusing the existing `__APP_CSS_VERSION__` / `__JTS_CAPS_JSON__`
  templating seam (`install.sh` calls `control_token.ensure_token()` first so the
  value exists; serve `location = /` `no-store`); have `postMute()` read the meta
  (+ localStorage fallback) and send `X-JTS-Token`, and **surface the 403**
  instead of silently reverting.

  **On the token's exposure (precise — not "equivalent to the wizards"):** baking
  it into `/usr/share/jasper-web/index.html` (mode `0644`) makes it world-readable
  *on disk* — wider than `control_token`'s `0640 group-jasper` file, and unlike the
  wizards, which deliver it per-request from that file. But it is **not new
  exposure**: the management read guard (`management_read_allowed`) permits
  non-browser local requests, so any local process can already
  `curl -H 'Host: jts.local' http://127.0.0.1/voice/` and read the token from a
  wizard's meta tag — the disk file is just another path to an already
  locally-obtainable value, so it doesn't widen what a compromised non-root daemon
  already has. Cross-origin browsers still can't read it (SOP makes the `/`
  response opaque; `no-store` keeps it out of caches). This stays within the
  "annoyance-class defense-in-depth, not a hard boundary" posture
  ([HANDOFF-privilege-separation.md](HANDOFF-privilege-separation.md)); tightening
  the file mode would buy nothing against an attacker who already has the HTTP
  path. If the model ever tightens to contain local non-root readers, deliver `/`
  through `canonical_page()` instead of baking (trading the static page's
  daemon-independent resilience).

**Audit result (June 2026):** across all six token-gated routes, the *only*
clients missing the token are (a) this landing-page button and (b) the M2M
grouping path (§6). Every ES-module wizard uses the shared `http.js`
`csrfHeaders()`/`postControlAction()`; the balance/sync/rooms/system server-side
flows forward the browser token via `forward_control_token_headers()` (#739);
internal restarts go through the WS1 restart broker (not the gated HTTP routes);
the VK-01 accessory maps only to ungated routes (volume/transport).

## 8. Phased execution plan (for multiple agents)

Each phase is independently shippable, branch + PR per
[AGENTS.md](../AGENTS.md#pr-workflow-on-a-fast-moving-main--read-before-you-push).
HW-free unless noted; multiroom phases gate on the two-Pi smoke test
([HANDOFF-peering.md](HANDOFF-peering.md) §8).

- **Phase A — reconcile the docs + pin the contradiction (cheap, no behavior
  change).** Make this doc authoritative; rewrite HANDOFF-multiroom.md §7 and
  HANDOFF-privilege-separation.md Phase 2 to stop contradicting (point here);
  fix the stale `rooms_setup` "default-off installs pass" comment. Add a guard
  test asserting `/grouping/set` ∈ `_TOKEN_GATED_ROUTES` (the contradiction
  cannot silently reappear). *Files:* the three docs, `jasper/web/rooms_setup.py`
  comment, `tests/test_control_server.py`. *Verify:* `pytest tests/test_control_server.py`.
- **Phase B — mic-mute landing-page delivery fix (self-contained, §7).**
  *Status: implemented + HW-free-verified on `main`; on-device deploy probe
  pending.* The token bake is folded into install.sh's
  existing fail-loud `PYBAKE` block, and no-store was added to BOTH nginx sites
  (`nginx-jasper.conf` and `nginx-jasper-streambox.conf` — each serves the same
  token-baked `index.html`). The mute failure path now surfaces the error
  (`failMute`) instead of silently reverting. *Files:* `deploy/index.html`,
  `deploy/install.sh`, `deploy/nginx-jasper.conf`,
  `deploy/nginx-jasper-streambox.conf`, `tests/test_landing_control_token.py`.
  *Verify:* HW-free static test ✅ + on-device deploy probe (the page's Mute
  button actually mutes) — pending.
- **Phase C — household credential: mint + distribute + verify (the core).**
  *Status: implemented + HW-free-verified on `main`; two-Pi smoke pending.*
  `jasper/control/household_credential.py` is a
  **near-line-for-line clone of `control_token.py`** (`ensure`/`current`/`verify`,
  atomic `0640` group-jasper write, `hmac.compare_digest`, absent⇒accept) — a **static bearer**
  in `X-JTS-Household`, **no nonce store, no clock/timestamp handling**
  (HMAC-signing rejected, §5) — plus `adopt()` (trust-on-first-use distribution,
  refuses to overwrite) and `clear()`. Mint lives in `rooms_setup._save_bond`
  (the bond entry — there is **no `/bond` route in server.py**; it's a `/rooms/`
  wizard handler); the receiving `_post_grouping_set` adopts on bond / clears on
  unbond; `/unbond` reads the secret ONCE and passes it explicitly so the
  concurrent per-member clears can't race a peer out of its credential. The
  `/grouping/set` gate accepts household-cred **or** control-token (on that route
  ONLY); the fan-out chokepoint `_post_grouping_to_member` attaches the secret
  (disk read — the intentional break of `_request_control_token`'s relay-only
  invariant); `AsyncControlClient.request/.post` gained the 2-line `headers=`
  kwarg (the Phase D enabler). One consequence to note: an **unpaired** speaker's
  `/grouping/set` is fail-safe-OPEN (the bootstrap window) — so two existing
  control-token gate tests now pair the speaker to keep asserting the closed
  state. *Files:* `jasper/control/household_credential.py`,
  `jasper/control/server.py` (gate + adopt/clear), `jasper/web/rooms_setup.py`,
  `jasper/control/client.py`, `.env.example`, `docs/doc-map.toml`,
  `tests/test_household_credential.py`, `tests/test_control_server.py`,
  `tests/test_web_rooms_setup.py`, `tests/test_control_client.py`.
  **Pin-the-semantics tests (required, all ✅):**
  (1) `verify()` accepts on absent/empty/unreadable; (2) requires on present;
  (3) **bootstrap regression** — an unbonded follower's `/grouping/set` succeeds for
  the secret-distributing fan-out (proves no deadlock); (4) **recovery regression**
  — a follower with a deleted `household_secret` can be autonomously re-bonded
  (proves self-heal survives file loss). *Verify:* unit tests ✅ + **two-Pi smoke**
  (leader→follower `/grouping/set` succeeds with the credential, 403s without) —
  pending.
- **Phase D — autonomous re-grouping uses the credential (resilience).** The
  grouping leader path presents the household secret when re-asserting a bond
  with no browser. *Files:* `jasper/control/grouping_supervisor.py`,
  `jasper/multiroom/*` (not `jasper/peering/`, which owns wake arbitration).
  *Verify:*
  two-Pi reboot smoke — follower reboots, leader re-bonds it automatically.
- **Phase E — per-device keys / mTLS over mkcert CA (DEFERRED, Option 2).** Only
  if the posture is being upgraded; documented here so it is not lost.

## 9. Decisions (resolved 2026-06-16)

These were the open questions; all resolved at design ratification.

1. **Credential shape: a distinct `X-JTS-Household` header carrying a *static
   bearer*** — not a widened `X-JTS-Token`, and **not** an HMAC signature (§5
   "Rejected: HMAC request-signing"). The distinct header keeps the per-device and
   household trust domains cleanly separate.
2. **First-bond bootstrap over the trusted LAN for v1.** A pairing code is the
   later hardening (it also closes the malicious-re-bond residual) — adopt it only
   under the §5 Option-2 revisit trigger.
3. **The household-secret bearer is the ceiling** for the current threat model;
   per-device mTLS (Phase E) is revisited only on the §5 Option-2 trigger (untrusted
   LAN co-tenant), and *not* over mkcert. So no per-device rotation/revocation
   machinery is built now.

**Out of scope (related work, not this doc):** a *hardware* mic-mute that software
cannot override is a worthwhile privacy control, but it is a privacy/hardware
initiative orthogonal to control-plane auth — `/mic/mute` is browser→own-speaker
(handled by the §7 token-delivery fix), not a device-to-device route, so the
household credential does not apply to it. Tracked separately.

## 10. References + related docs

- [HANDOFF-privilege-separation.md](HANDOFF-privilege-separation.md) — the
  per-device CSRF control token (Phase 2) + daemon hardening. **Reconcile with §2.**
- [HANDOFF-multiroom.md](HANDOFF-multiroom.md) §7 — the grouping control plane
  threat model. **Reconcile with §2.**
- [HANDOFF-peering.md](HANDOFF-peering.md) — the (unauthenticated) peer-discovery
  gossip layer; the autonomous re-grouping path Phase D hooks.
- [SECURITY.md](../SECURITY.md) — the LAN-trust posture + operator "Control token"
  summary.
- Prior-art sources: §4.

Last verified: 2026-06-17
