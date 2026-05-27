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

Preferred path: use GitHub private vulnerability reporting if it is
enabled for the repository. Until a private channel is configured, open
a minimal public issue titled `Security report` with no sensitive
details so the maintainer can move the discussion to a private channel.

Include:
- the affected commit or approximate date of the checkout;
- which surface is affected (`jasper-control`, a web wizard, deploy
  scripts, diagnostics bundle, wake-event corpus, etc.);
- the impact and reproduction steps, with secrets redacted;
- whether physical access, LAN access, or internet access is required.

## Current Security Model

JTS assumes a trusted household LAN and local physical ownership of the
speaker. It does not currently ship full authentication or HTTPS for
local setup pages. Management endpoints should still reject obvious
browser-origin and DNS-rebinding abuse, cap request sizes, avoid logging
credentials, and keep secrets in root-owned files where possible.

Diagnostic scripts redact environment-style secret assignments in their
log/config snapshots before writing logs or bundles to disk. Wake-event
audio stays local to the speaker unless an operator explicitly exports it.

## Out of Scope

Security reports are welcome even when the current fix is only a
documented limitation. The project does not offer a bug bounty, SLA, or
guaranteed embargo timeline.
