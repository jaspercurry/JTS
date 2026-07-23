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
The Pi binds the exact spec bytes to that fragment with HMAC-SHA-256, and
protocol-v2 phone events are likewise authenticated before the Pi interprets
page identity, acknowledgement, or `armed`; see
[`js/transport-integrity.js`](js/transport-integrity.js). The relay may deny
service, but it cannot silently rewrite those controls.
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
| `js/transport-integrity.js` | Fragment-key-derived spec + phone-event HMAC | `capture_transport_integrity_test.mjs` |
| `js/relay-client.js` | Phone-side relay requests (upload_token) | `capture_relay_client_test.mjs` |
| `js/capture-protocol.js` | Public-page/Pi protocol compatibility (including the one legacy-v1 mapping) | `capture_protocol_test.mjs` |
| `js/setup-store.js` | Privacy-bounded frozen setup reuse (sliding 20-minute idle, fixed 2-hour absolute expiry) | `capture_setup_store_test.mjs` |
| `js/return-url.js` | Sanitized local-Pi return URL for the done CTA | `capture_return_url_test.mjs` |
| `js/fragment.js` | Parse `#s=&u=&k=&a=` (key/spec MAC never leave the fragment) | `capture_fragment_test.mjs` |
| `js/constraints.js` | Realized-constraints verify/degrade per the spec's per-kind policy | `capture_constraints_test.mjs` |
| `js/wakelock.js` | Screen Wake Lock + `visibilitychange` abort | `capture_wakelock_test.mjs` |
| `js/level-events.js` | Batched phone-side mic-level events for the level-match ramp | `capture_level_events_test.mjs` |
| `js/ambient-stats.js` | Per-octave-band ambient-noise stats for a driver sweep's quiet window (Wave 2) | `capture_ambient_stats_test.mjs`, `test_capture_page_ambient_stats_bridge.py` |
| `js/config.js` | `RELAY_BASE` (one relay origin for the fleet) | — |
| `js/main.js` | Browser orchestration: one tap → record + arm → encrypt → upload; session-spanning capture plans (protocol v3) | `capture_plan_loop_test.mjs`, on-device |
| `index.html` | Static shell + CSP + base styles | `node --check` |
| `version.json` | Live page build + supported capture-protocol versions | `test_capture_page_js.py` |

The page **reuses** the canonical JTS browser capture helper
(`deploy/assets/shared/js/measurement-audio.js`) — the build copies it into the
bundle rather than forking it (single source of truth).

## Build + deploy

```sh
cd capture-page
bash build.sh                                   # -> capture-page/dist/
npx wrangler pages deploy dist --project-name jts-capture-page
```

### Release order (page before Pi)

The Pages site and Pi packages are independent releases. A capture-protocol
change must use this order so an upgraded Pi never reaches a stale public page:

1. Add the new protocol to `version.json`'s
   `supported_capture_protocol_versions` **without removing the currently
   deployed protocol**. The page treats an old spec with no explicit version as
   legacy protocol 1; this is the only implicit compatibility rule.
2. Build and test the page: `bash capture-page/build.sh` and
   `python3 -m pytest -q tests/test_capture_page_js.py`.
3. Publish `capture-page/dist` to the production Pages project.
4. Verify the public artifact before touching any Pi:
   `curl -fsS https://capture.jasper.tech/version.json`. Confirm the expected
   `capture_page_build` and that the new protocol is in
   `supported_capture_protocol_versions`.
5. Only then deploy the Pi code that emits the new
   `CaptureSpec.capture_protocol_version`.
6. After the fleet is upgraded, a later page-only release may remove the old
   protocol from the supported list.

Every phone control event carries the loaded page identity. The Pi validates it
before setup or `armed` can invoke tone playback and logs
`event=capture_relay.page_compatible` or
`event=capture_relay.page_incompatible`. Incompatibility is also posted back to
the phone as a visible terminal error. `version.json` is therefore the stable
public release-verification surface; the event log proves the live page/Pi pair
that actually opened a session.

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
node tests/js/capture_return_url_test.mjs    # sanitized local-Pi return URL
node tests/js/capture_level_events_test.mjs  # batched phone-side level events
node tests/js/capture_setup_store_test.mjs   # sliding + absolute setup expiry
node tests/js/capture_protocol_test.mjs      # page/Pi release compatibility
node tests/js/capture_ambient_stats_test.mjs # per-octave-band ambient stats (Wave 2)
node tests/js/capture_plan_loop_test.mjs     # session-spanning capture plan loop (protocol v3)
node tests/js/capture_calibration_confirm_test.mjs  # one-tap household-mic confirm (Wave 2)
node tests/js/capture_defect_fixes_test.mjs  # run-19 field-telemetry defect fixes
```

All harnesses run in CI through `tests/test_capture_page_js.py` (pytest) and
`scripts/check-js-syntax.sh` (`node --check`).

## Needs on-device validation

`main.js` (mic capture, iOS `AudioContext` resume in the tap handler, the
record-window timing, the encrypt+upload leg) is browser-only and **must be
exercised on a real iPhone (Safari) and Android phone (Chrome)** — the
pure modules above are unit-tested, but the live `getUserMedia` path is not.
Screen Wake Lock + `visibilitychange` abort and the realized-constraints
verify/degrade gates land in build steps 6–7.

**#1658 follow-up (session wake lock + one mic stream per session).** The v3
capture-plan loop (`onPlanStart`/`runPlanCapture`) now holds a SINGLE screen
wake lock and a SINGLE mic stream/`AudioContext` graph for the whole session
instead of re-acquiring per capture — the plan-loop harness
(`capture_plan_loop_test.mjs`) pins the call-count contract (one
`createMonoRecorder`, one wake-lock acquire, one close/release per session)
against stubbed browser APIs, but the real iOS behaviors it cannot exercise —
whether the actual level step between captures is gone, whether
`navigator.wakeLock` genuinely keeps an iPhone screen on for a multi-minute
session, and the real `visibilitychange`/re-acquire timing — need a real
iPhone pass before this is trusted end-to-end. Also needs an **Android
Chrome suspend-without-track-end** pass: backgrounding a tab can auto-suspend
the reused `AudioContext` without its mic track ever reaching `ended` (the
signal `wireTrackEndedRecovery` relies on), which is why each round now
explicitly `resume()`s the context before recording — confirm on a real
Android Chrome that this actually recovers audio after a background/
foreground cycle rather than silently timing out on the next `stop()`.
