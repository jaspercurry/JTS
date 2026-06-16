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
A security-conscious operator can opt into requiring a shared token on
the four highest-impact of those routes — see "Opt-in control token"
below.

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

### Opt-in control token

By default the raw `jasper-control` mutations above are open on the
trusted LAN — that is the posture the dial, Home Assistant, and
Shortcuts rely on, and it is the right default for most households. A
security-conscious operator who shares the LAN with less-trusted devices
(a guest VLAN, roommates, IoT gear) can opt into requiring a shared
token on the four highest-impact routes:

- `POST /system/poweroff` — powers the speaker off (it stays off until
  someone physically re-plugs it).
- `POST /system/reboot` — reboots the speaker.
- `POST /mic/mute` — toggles the privacy mic mute, the one promise a
  household relies on to know the mic is off.
- `POST /grouping/set` — rewires multiroom output routing.

Enable it on the speaker:

```sh
sudo jasper-control-token --enable      # prints a generated token
sudo jasper-control-token --show        # reprint it later
sudo jasper-control-token --disable     # back to default-off
```

`--enable` writes `/var/lib/jasper/control_token` (mode `0600`, root)
with a `secrets.token_urlsafe(32)` value. While that file exists with
non-empty content, the four routes above require a matching `X-JTS-Token`
request header; a missing or wrong token gets a `403
{"error":"control_token_required"}` (compared in constant time via
`hmac.compare_digest`, and the token value is never logged). With no
file — the default — the gate is a complete no-op and behaviour is
exactly as before.

The gate is deliberately narrow. **Volume, transport, source, and the
AEC knobs stay ungated** — they are the dial's bread-and-butter
low-impact controls, the dial never calls the gated four, and gating
them would break the trusted-LAN accessories for little security gain.
The token is one shared household secret, not per-user auth; it is a
speed-bump against a casual LAN device, not a substitute for network
isolation, and it does not add HTTPS or stop on-path observation of
plain-HTTP setup traffic.

Legitimate clients supply the token two ways:

- **Browser.** The `/system/` and `/rooms/` dashboards prompt once for
  the token on the first gated action, store it in that browser's
  `localStorage`, and attach it as `X-JTS-Token` on subsequent actions.
  Because those pages proxy to control server-side, the wizard forwards
  the browser-supplied header through to control (and, for grouping,
  to each member speaker) — it never injects a token from disk, so the
  secret stays in the operator's browser.
- **curl / scripts / Home Assistant.** Send `-H "X-JTS-Token: <token>"`
  on the gated requests.

`jasper-doctor` reports the gate posture (enabled vs disabled), never
the secret.

Diagnostic scripts redact environment-style secret assignments in their
log/config snapshots before writing logs or bundles to disk. Wake-event
audio stays local to the speaker unless an operator explicitly exports it.

## Out of Scope

Security reports are welcome even when the current fix is only a
documented limitation. The project does not offer a bug bounty, SLA, or
guaranteed embargo timeline.
