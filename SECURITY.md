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

Diagnostic scripts redact environment-style secret assignments in their
log/config snapshots before writing logs or bundles to disk. Wake-event
audio stays local to the speaker unless an operator explicitly exports it.

## Out of Scope

Security reports are welcome even when the current fix is only a
documented limitation. The project does not offer a bug bounty, SLA, or
guaranteed embargo timeline.
