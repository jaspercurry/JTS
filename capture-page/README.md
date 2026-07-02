<!--
SPDX-FileCopyrightText: 2026 Jasper Curry
SPDX-License-Identifier: Apache-2.0
-->

# JTS phone-mic capture page (Cloudflare Pages)

The **static, trusted-origin** capture surface. Hosting it on a real cert
(jasper.tech via Cloudflare Pages) is what makes `getUserMedia` work on **iOS
Safari and Android Chrome with no cert warning and no app** — the whole reason
the relay exists (see [`docs/phone-mic-relay-plan.md`](../docs/phone-mic-relay-plan.md)
§§1–4). Mobile browsers require microphone pages to be secure contexts backed by
a publicly trusted HTTPS certificate; a LAN Pi's self-signed cert is not enough
for Android Chrome microphone access. The page and the Pi never talk directly;
they communicate only through the relay.

## The security boundary (read this)

The page **holds the microphone and the E2E `content_key`** (in its URL
fragment). The `capture_spec` it renders arrives across the **untrusted relay**.
So the page renders that spec as **DATA, never code** ([`js/render.js`](js/render.js)):
a closed component vocabulary mapped to fixed element tags, all text via
`textContent`, theme as allowlisted *tokens* mapped to fixed CSS, and button
actions that *select* a host-provided handler (never carry one). A hostile
payload's worst case is wrong text on screen — never code execution. A strict
CSP in [`index.html`](index.html) is a second layer. Pinned by
`tests/js/capture_render_test.mjs`. The post-upload **Back to speaker** CTA is
also data from the spec (`return_url`); [`js/return-url.js`](js/return-url.js)
sanitizes it again before rendering a plain navigation link to the local Pi page.

## Modules

| File | Role | Tested by |
|---|---|---|
| `js/render.js` | Fixed DATA renderer (the security boundary) | `capture_render_test.mjs` |
| `js/theme.js` | Theme token → fixed CSS value allowlist | (via render) |
| `js/crypto.js` | AES-256-GCM encrypt + plaintext SHA-256 integrity | `capture_crypto_test.mjs` |
| `js/relay-client.js` | Phone-side relay requests (upload_token) | `capture_relay_client_test.mjs` |
| `js/return-url.js` | Sanitized local-Pi return URL for the done CTA | `capture_return_url_test.mjs` |
| `js/fragment.js` | Parse `#s=&u=&k=` (key never leaves the fragment) | `capture_fragment_test.mjs` |
| `js/config.js` | `RELAY_BASE` (one relay origin for the fleet) | — |
| `js/main.js` | Browser orchestration: one tap → record + arm → encrypt → upload | on-device |
| `index.html` | Static shell + CSP + base styles | `node --check` |

The page **reuses** the canonical JTS browser capture helper
(`deploy/assets/shared/js/measurement-audio.js`) — the build copies it into the
bundle rather than forking it (single source of truth).

## Build + deploy

```sh
cd capture-page
bash build.sh                                   # -> capture-page/dist/
npx wrangler pages deploy dist --project-name jts-capture-page
```

Jasper Tech's public default is deployed at `capture.jasper.tech` and points to
`https://relay.jasper.tech`. To self-host, set `js/config.js` `RELAY_BASE` to
your deployed Worker origin (for example `https://relay.example.com`) and point
the Cloudflare Pages custom domain at your capture host (for example
`capture.example.com`). Keep the two origins distinct so the relay's CORS
allowlist (`CAPTURE_ORIGIN`) is meaningful, and set the Pi's
`JASPER_CAPTURE_RELAY_BASE` / `JASPER_CAPTURE_ORIGIN` to those same custom
origins.

## Test

```sh
node tests/js/capture_render_test.mjs        # DATA renderer (XSS-inert)
node tests/js/capture_crypto_test.mjs        # E2E AES-GCM + integrity
node tests/js/capture_relay_client_test.mjs  # phone-side relay requests
node tests/js/capture_fragment_test.mjs      # fragment parse + upload cap
node tests/js/capture_constraints_test.mjs   # realized-constraints verify/degrade
node tests/js/capture_wakelock_test.mjs      # Screen Wake Lock + visibility abort
```

All six run in CI through `tests/test_capture_page_js.py` (pytest) and
`scripts/check-js-syntax.sh` (`node --check`).

## Needs on-device validation

`main.js` (mic capture, iOS `AudioContext` resume in the tap handler, the
record-window timing, the encrypt+upload leg) is browser-only and **must be
exercised on a real iPhone (Safari) and Android phone (Chrome)** — the
pure modules above are unit-tested, but the live `getUserMedia` path is not.
Screen Wake Lock + `visibilitychange` abort and the realized-constraints
verify/degrade gates land in build steps 6–7.
