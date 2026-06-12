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
credentials. The ~18 nginx-fronted setup wizards under `jasper/web/`
now share that guard on every **state-changing (POST)** request:
`verify_csrf()` in `jasper/web/_common.py` runs `mutating_request_allowed`
before any mutation, so a DNS-rebinding / cross-site browser cannot write
WiFi PSKs, HA tokens, or API keys, or trigger reboots through a wizard.
The wizard **read (GET)** surface is not yet behind the same Host check
(there is no single shared GET chokepoint today), so a rebinding read
could still render a wizard page — though not change state; closing that
read-side gap is a known, deferred follow-up. Secrets are kept in
root-owned files where possible.

### Threat model — what network position gets you

Any device already on the trusted LAN can use the raw management APIs
without authentication. That includes changing volume (`POST
/volume/set`, `/volume/adjust`, `/volume/mute`), toggling the privacy
mic mute (`POST /mic/mute`), changing AEC mode/profile/threshold
(`POST /aec/toggle`, `/aec/leg`, `/aec/profile`, `/aec/threshold`),
rebooting or powering off the speaker (`POST /system/reboot`,
`/system/poweroff`), and rewiring multiroom bonds (`POST
/grouping/set`). This is deliberate today: the dial, Home Assistant,
Shortcuts-style automations, and other household integrations use the
same trusted-LAN posture. Browser-origin attacks are a different class
and are blocked with Host / Origin / Fetch Metadata checks plus CSRF.

Setup wizards submit API keys, Home Assistant tokens, and Wi-Fi PSKs
over plain HTTP on the LAN. nginx serves the management UI over HTTP;
only `/correction/` has HTTPS because phone browsers require it for
microphone capture. Do setup from a trusted network. A guest VLAN,
rogue access point, or hostile device on the same Wi-Fi can observe or
send LAN traffic unless the network itself isolates it. Server-side,
secrets are kept in root-owned files where possible, usually mode
`0600`.

Bluetooth pairing uses Just Works auto-accept, but only inside an
explicit 300-second pairing window opened from `/bluetooth/`. This is
the same usability trade-off as common smart speakers: pairing is easy
while a local operator has opened the window. At rest, non-pairability
is enforced at runtime by the pairing agent's window-scoped adapter
toggling, not by BlueZ `main.conf`; already-paired devices can still
reconnect without reopening pairing.

Peering and multiroom control messages are unauthenticated LAN
multicast today. A device on the same LAN can spoof those control
messages. The planned follow-up is to add an HMAC over peering messages
using a shared household secret.

Future work under consideration: an opt-in shared token for the highest
impact raw control mutations, especially power and mic-mute operations
on port 8780. That is not implemented today.

Diagnostic scripts redact environment-style secret assignments in their
log/config snapshots before writing logs or bundles to disk. Wake-event
audio stays local to the speaker unless an operator explicitly exports it.

## Out of Scope

Security reports are welcome even when the current fix is only a
documented limitation. The project does not offer a bug bounty, SLA, or
guaranteed embargo timeline.
