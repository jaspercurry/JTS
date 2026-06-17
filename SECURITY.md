# Security Policy

JTS is a household LAN appliance that can control audio, wake-word
recording, Wi-Fi setup, Home Assistant, and system power actions. Treat
security reports seriously even though this is not yet a product with
formal release support.

## Supported Versions

Only the current `main` branch is supported. There are no long-lived
release branches or patch trains yet.

## Reporting a Vulnerability

Please do not publish exploit details, secrets, wake-event audio, or
private network data in a public issue.

Preferred path today: email `jc@jasper.tech` with a short subject such
as `JTS security report`. Do not include secrets, wake-event audio,
Wi-Fi PSKs, API keys, OAuth tokens, or private network data unless the
maintainer asks for a safer transfer path.

GitHub private vulnerability reporting may become the preferred path
once it is enabled for the repository. Until then, use the email path
above rather than filing sensitive details in a public issue.

Include:
- the affected commit or approximate date of the checkout;
- which surface is affected (`jasper-control`, a web wizard, deploy
  scripts, diagnostics bundle, wake-event corpus, etc.);
- the impact and reproduction steps, with secrets redacted;
- whether physical access, LAN access, or internet access is required.

## Current Security Model

JTS assumes a trusted household LAN and local physical ownership of the
speaker. It does not currently ship full authentication or HTTPS for
local setup pages.

The `jasper-control` API (`127.0.0.1:8780`, fronted by nginx) rejects
obvious browser-origin and DNS-rebinding abuse via
`jasper/http_security.py` (`management_read_allowed` /
`mutating_request_allowed`), caps request sizes, and avoids logging
credentials. The nginx-fronted setup wizards under `jasper/web/` share
those guards on state-changing requests and GET routes. A
DNS-rebinding / cross-site browser should not be able to read masked
wizard pages, write Wi-Fi PSKs, Home Assistant tokens, or API keys, or
trigger reboots through a wizard.

### Threat model — what network position gets you

Any device already on the trusted LAN can use the low-impact management
APIs without authentication: changing volume (`POST /volume/set`,
`/volume/adjust`, `/volume/mute`) and AEC mode/profile/threshold (`POST
/aec/toggle`, `/aec/leg`, `/aec/profile`, `/aec/threshold`). This is
deliberate: the dial, Home Assistant, Shortcuts-style automations, and
other household integrations use the same trusted-LAN posture. The
highest-impact mutations — reboot / poweroff, voice/audio restart, the
privacy mic mute, and multiroom rewiring — require an auto-generated
control token (see "Control token" below). Browser-origin attacks are a
different class and are blocked with Host / Origin / Fetch Metadata
checks plus CSRF.

Setup wizards submit API keys, Home Assistant tokens, and Wi-Fi PSKs
over plain HTTP on the LAN. nginx serves the management UI over HTTP;
only `/correction/` has HTTPS because phone browsers require it for
microphone capture. Do setup from a trusted network. A guest VLAN,
rogue access point, or hostile device on the same Wi-Fi can observe or
send LAN traffic unless the network itself isolates it. Server-side,
secrets are kept in root-owned files where possible, usually mode
`0600` or group-readable `0640` when sibling non-root daemons must share
them.

Bluetooth pairing uses Just Works auto-accept, but only inside an
explicit 300-second pairing window opened from `/bluetooth/`. This is
the same usability trade-off as common smart speakers: pairing is easy
while a local operator has opened the window. At rest, non-pairability
is enforced at runtime by the pairing agent's window-scoped adapter
toggling, not by BlueZ `main.conf`; already-paired devices can still
reconnect without reopening pairing.

The peer-discovery gossip (mDNS/multicast) is unauthenticated LAN
traffic today; a device on the same LAN can spoof those messages. The
cross-device multiroom **grouping** control path (`POST /grouping/set`)
is separately authenticated by a shared **household credential** minted
at the bond — a static bearer in an `X-JTS-Household` header (HMAC
request-signing was considered and rejected for that HTTP path; see
[HANDOFF-control-plane-auth.md](docs/HANDOFF-control-plane-auth.md)). A
future HMAC over the peering gossip could reuse that same household
secret.

### Control token

The highest-impact `jasper-control` mutations require a shared **control
token**, and it is **on by default and invisible** to the household:
`jasper-control` auto-generates the token at startup
(`/var/lib/jasper/control_token`, mode `0640` group `jasper`, a
`secrets.token_urlsafe(32)` value, never logged), and the management UI
delivers it to the dashboard
automatically (embedded in each page behind the read guard, read by
`http.js`) — nobody sees or types anything. The gated routes:

- `POST /system/poweroff` / `POST /system/reboot` — power off / reboot.
- `POST /system/restart/voice` / `POST /system/restart/audio` — restart the
  assistant / the audio chain.
- `POST /mic/mute` — the privacy mic mute, the one promise a household relies
  on to know the mic is off.
- `POST /grouping/set` — rewires multiroom output routing. This route
  **additionally** accepts a distinct **household credential**
  (`X-JTS-Household`) for the cross-device bond fan-out — a paired peer
  or the autonomous re-group path authenticates with that instead of the
  per-device token; see
  [HANDOFF-control-plane-auth.md](docs/HANDOFF-control-plane-auth.md).

A gated request without a matching `X-JTS-Token` header gets a `403
{"error":"control_token_required"}` (compared in constant time via
`hmac.compare_digest`). **Volume, transport, source, and the AEC knobs stay
ungated** — the dial's low-impact controls, which it relies on and which never
call the gated routes.

**What it does and does not protect.** Because the token is auto-delivered to
the dashboard over the same LAN, a determined device on the LAN that fetches the
page can read it too. So the token is **defense-in-depth against drive-by, CSRF,
and casual curl** of the destructive (annoyance-class) routes — not a hard
boundary against a compromised LAN device, and not a substitute for network
isolation or HTTPS. The serious threats (secret theft, persistence, pivot) are
contained by the daemon hardening / privilege separation
([docs/HANDOFF-privilege-separation.md](docs/HANDOFF-privilege-separation.md)),
not by this token.

Operators can inspect or rotate the value with `sudo jasper-control-token
--show` / `--disable` (disabling reverts to the unauthenticated trusted-LAN
posture); curl / Home Assistant / scripts pass `-H "X-JTS-Token: <token>"`.
`jasper-doctor` reports the gate posture, never the secret.

Diagnostic scripts redact environment-style secret assignments in their
log/config snapshots before writing logs or bundles to disk. Wake-event
audio stays local to the speaker unless an operator explicitly exports it.

## Out of Scope

Security reports are welcome even when the current fix is only a
documented limitation. The project does not offer a bug bounty, SLA, or
guaranteed embargo timeline.
