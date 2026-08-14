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
phone events are likewise authenticated before the Pi interprets
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
| `js/capture-protocol.js` | Public-page/Pi protocol compatibility handshake | `capture_protocol_test.mjs` |
| `js/setup-store.js` | Privacy-bounded frozen setup reuse (sliding 20-minute idle, fixed 2-hour absolute expiry) | `capture_setup_store_test.mjs` |
| `js/return-url.js` | Sanitized local-Pi return URL for the done CTA | `capture_return_url_test.mjs` |
| `js/fragment.js` | Parse `#s=&u=&k=&a=` (key/spec MAC never leave the fragment) | `capture_fragment_test.mjs` |
| `js/constraints.js` | Realized-constraints verify/degrade per the spec's per-kind policy | `capture_constraints_test.mjs` |
| `js/wakelock.js` | Screen Wake Lock + `visibilitychange` abort | `capture_wakelock_test.mjs` |
| `js/capture-integrity.js` | Per-take focus/visibility log + block-accounting summary (#2151) + the page end of the host's end-to-end frame ledger (#2094) | `capture_integrity_test.mjs`, `capture_plan_loop_test.mjs`, `capture_frame_report_emit.mjs` |
| `js/level-events.js` | Batched phone-side mic-level events for the level-match ramp | `capture_level_events_test.mjs` |
| `js/ambient-stats.js` | Per-octave-band ambient-noise stats for a driver sweep's quiet window (Wave 2) | `capture_ambient_stats_test.mjs`, `test_capture_page_ambient_stats_bridge.py` |
| `js/config.js` | `RELAY_BASE` (one relay origin for the fleet) | — |
| `js/main.js` | Browser orchestration: one tap → record + arm → encrypt → upload; session-spanning capture plans; Pi-owned Room ready-screen delegation | `capture_plan_loop_test.mjs`, `capture_calibration_confirm_test.mjs`, on-device |
| `index.html` | Static shell + CSP + base styles | `node --check` |
| `version.json` | Live page build + the supported capture protocol | `test_capture_page_js.py` |

The page **reuses** the canonical JTS browser capture helper
(`deploy/assets/shared/js/measurement-audio.js`) — the build copies it into the
bundle rather than forking it (single source of truth).

## Step-screen grammar (capture plans)

Every page-owned screen of a v3 capture-plan session renders one grammar, in
the same DOM slots each time (`renderStepScreen` in `js/main.js`;
[`docs/flat-linearization-flow-simplification-plan.md`](../docs/flat-linearization-flow-simplification-plan.md)
§2.1): a small **eyebrow** carrying the ONE counter (`screen.progress`, always
server-derived), the **instruction** as the headline (`screen.title`), at most
one supporting clause (`screen.body`), a single full-width **primary**, an
optional quieter **Retake this measurement** (§2.6), and **Stop** as a text
link behind the page's own `<dialog>` confirm in `index.html`. `#status` is the
transient-state channel only — it carries no counters. The page adds nothing
to the screen vocabulary: keys it does not know are ignored, and keys the Pi
stops sending fall back, which is what keeps a cached bundle and a newer
speaker compatible in both directions.

## Build + deploy

```sh
cd capture-page
bash build.sh                                   # -> capture-page/dist/
npx wrangler pages deploy dist --project-name jts-capture-page --branch=main
```

`--branch=main` is load-bearing: without it wrangler publishes a **preview
alias** and the production domain keeps serving the stale page (the W6.10
Chrome-deadlock bug class). The custom domain lags the deploy by ~5 min.

### Release order (direction matters)

The Pages site and Pi packages are independent releases, and the correct order
depends on the compatibility class. For a protocol-list change, it depends on
**which way the supported list is moving**. Get this backwards and the handshake
refuses every capture, fleet-wide, the moment the page publishes —
`version.json` is fetched `no-store`, so the cut is instant.

- **ADDING a protocol → page first, Pi second.** The page must already
  advertise a protocol before any Pi emits it. Adding is backwards-compatible:
  the page keeps serving the old protocol while the fleet catches up.
- **REMOVING a protocol → Pi first, page second.** Every Pi must have stopped
  emitting a protocol before the page stops advertising it. Removing is *not*
  backwards-compatible: a page that has dropped protocol N strands every Pi
  still emitting N.

Both directions put the *narrowing* side last. A change that adds one and
removes another is two releases, not one.

#### Reinterpreting an existing spec field (Pi first)

The protocol handshake cannot detect a semantic change where a new page starts
using data that an old page ignored. Treat that as a separate compatibility
class:

- **Forward rollout → Pi first, page second.** First deploy the Pi producer that
  emits every newly consumed value, then publish the page that relies on it.
- **Rollback → page first, Pi second.** Restore the old tolerant page before
  rolling back the Pi producer.

#### A new phone event both sides need (page first)

The sharpest class: the page starts *sending* something the Pi requires, on a
plan shape only the new Pi emits. Neither the protocol list nor the build stamp
detects it — `validate_capture_page` checks the stamp's FORMAT, never a minimum
— so a mismatched pair fails at the last capture of a session the household has
already spent ten minutes on.

- **Page first, Pi second**, so no phone is left on a bundle that cannot send
  the event when the new conductor arrives.
- **The new page must therefore be TOLERANT of the old conductor**, and
  tolerance is read off the plan in hand rather than assumed. That is a
  requirement on the page, not a hope about timing.

Build `20260729.2` is the fixture: the group-close confirm's "Continue" posts
`complete_capture_set` on a measure-only plan (two-stage commission D1 — its
final position IS the capture target, so there is no next entry whose begin
could carry the confirmation), and still posts the next entry's begin on a plan
that has one. `renderPlanGroupConfirm` branches on `entryForIndex(spec,
index + 1)`; both halves are pinned in `tests/js/capture_plan_loop_test.mjs`
(tests 30/32 the legacy path, 55-57 the measure-only one). Roll back in the
inverse order.

Build `20260803.4` is the terminal-result fixture (#2097). The conductor's
final allowed position attempt can now publish `capture_result` with
`terminal: true` and return the runner immediately — there is deliberately no
next begin and no later `capture_set_exhausted` event. This is a **new host event
meaning that both sides need**, even though the protocol number did not move:

- **Forward rollout → page first, Pi second.** Publish and verify page build
  `20260803.4` before deploying any Pi that can emit `terminal: true`. The new
  page is tolerant of an old conductor: an old `capture_result` omits
  `terminal`, the page's strict `event.terminal === true` check stays false,
  and the still-live old runner follows its ordinary retry path.
- **The inverse skew is unsafe.** Page build `20260803.3` ignores the new field,
  reads the terminal rejection as an ordinary rejected capture, and renders a
  live **Try again** control after the new runner has already returned. A Pi
  carrying #2097 must therefore never be deployed before `20260803.4` is live
  at `capture.jasper.tech` and verified through `version.json`.
- **Rollback → Pi first, page second.** First roll every Pi back to a conductor
  that cannot emit the terminal result; only then may the page be rolled back
  below `20260803.4`. Rolling the page back first recreates the unsafe skew.

Both behavioral halves — tolerant new page with an old result, and the frozen
`20260803.3` parser misclassifying a new terminal result as retryable — are
pinned in `tests/js/capture_plan_loop_test.mjs`. The build/order/rollback words
are pinned in `tests/test_capture_page_js.py`. **Do not deploy the Pi first.**

DA-0005 is the fixture for the rule above it: build `20260729.1` starts
rendering Room position/trust-repeat copy from the existing `ui.screen`. An old page safely
ignores the newer Pi copy and retains its embedded presentation, but the new
page against an old Pi would render the older generic v3 screen. Therefore
deploy the Pi commit first, verify its capture spec, and only then publish
`20260729.1`. Roll back in the inverse order. This ordering is contract-tested
in `tests/test_capture_page_js.py`; it deliberately avoids reintroducing a
second browser-owned copy.

#### An additive field that degrades on its own (either order)

The mildest class, and the one to reach for by default: the Pi starts sending
something new, the page renders it when present, and **the page's behaviour
without it is already correct** — not merely tolerable. Both directions are then
safe and no ordering rule applies. The test is strict: if the page's no-field
fallback is a sentence you would not ship on its own, this is not that class,
it is the "reinterpreting an existing spec field" class above.

Build `20260730.1` is the fixture, with three of them (two-stage commission D8 /
D9):

- `capture_spec.time_budget` (`{step_s, session_s}`, set at mint time by
  `jasper/capture_relay/correction_adapter.py`) — the page shows the
  how-long-can-I-pause line and names the clock on an expiry. Without it the
  page says nothing about time, which is exactly what it said before. **It
  rides EVERY adapter-minted spec, not just the crossover ones** — room sweep,
  sync, balance and level ramp all mint through `open_capture` — so the budget
  line appears on any plan step screen and the expiry copy is honest fleet-
  wide. The page-owned step screens are the only surface that renders it; a
  spec's own `ui.screen` never mentions it.
- `capture_plan.entries[].screen.noise_note` — per-entry copy for the phone's
  own pre-arm floor window, so CHECK stops asking for quiet before the window
  that deliberately measures an un-hushed room (issue #1835). Without it the
  page keeps its default, which is right for every other entry.
- the `capture_set_exhausted` host event's `budget` field — names which clock
  expired so the page stops calling a timeout an attempt limit. Without it the
  page keeps the attempt-limit copy, which is what the runner's own exhaustion
  event genuinely means.

Build `20260805.1` is the same class in the OTHER direction (#2151): the PAGE
starts sending something new — a `capture_integrity` field on a repeat of the
armed event, posted after the recorder stops and before the blob — and the Pi
records it in the retained operator sidecar when present. Either order is safe,
by the strict test above:

- **New page, old Pi.** The field is an unknown key in the authenticated event
  payload. The payload is any JSON object (only the ENVELOPE's shape is fixed —
  `jasper/capture_relay/integrity.py`), and `classify_status` reads named keys
  only, so an older Pi ignores it. Everything the household sees on a focus loss
  — the live warning and the cause note on the rejection screen — is page-owned
  and works against any Pi.
- **Old page, new Pi.** No report arrives, `CaptureResult.capture_integrity`
  stays `None`, and the sidecar simply has no `capture_integrity` key. That is
  not a degraded fallback, it is exactly what every sidecar written before this
  build looks like.

Build `20260814.1` extends that same field with two more counts (#2094):
`frames` (what the recorder worklet assembled) and `encoded_frames` (what this
page hands the WAV encoder). The Pi closes an end-to-end frame ledger against
its own count of the decoded capture, so a lost render quantum reports itself at
capture time with the losing hop named instead of turning up in WAV forensics
weeks later. Both directions stay safe for the same reason as above — an older
Pi ignores unknown keys, and an older page simply declares no counts, which the
Pi grades as `not_evaluated` and never as loss. `tests/js/capture_frame_report_emit.mjs`
runs this page's real summarizer and hands the result to the Pi's real
reconciler, so a rename on either side fails rather than degrading quietly.

The one thing that is NOT optional in either direction: the field must ride a
repeat of the WHOLE armed payload, never a partial event. The relay's
phone-event slot is last-write-wins, so a partial event could stand a Pi down
from `armed` mid-round. The runner's own arm-once guard makes the repeat a
no-op; `tests/js/capture_plan_loop_test.mjs` pins both halves.

(The relay Worker is a third independent release with its own ordering rule for
relay **capacity** changes — see [`relay/README.md`](../relay/README.md)
"Release order". That rule puts the Pi last, which matches the ADD direction
only; it does not override the REMOVE direction here.)

**There is exactly one capture protocol**, and a spec must state it
explicitly — a spec with no `capture_protocol_version` is incompatible, not
legacy. Protocols 1 and 2 were deleted on 2026-07-27; the published build
`20260712.3` did serve protocol 2, so that deletion was a REMOVAL and shipped
Pi-first.

#### Adding a protocol (page first)

1. Add the new protocol to `version.json`'s
   `supported_capture_protocol_versions` **without removing one that any
   deployed Pi still emits**, and bump `capture_page_build` (plus
   `index.html`'s `main.js?v=` stamp, and the `?v=` on any changed module
   import — those stamps are the page's only cache-invalidation mechanism).
2. Build and test the page: `bash capture-page/build.sh` and
   `python3 -m pytest -q tests/test_capture_page_js.py`.
3. Publish `capture-page/dist` to the production Pages project.
4. Verify the public artifact before touching any Pi:
   `curl -fsS https://capture.jasper.tech/version.json`. Confirm the expected
   `capture_page_build` and that the new protocol is in
   `supported_capture_protocol_versions`.
5. Only then deploy the Pi code that emits the new
   `CaptureSpec.capture_protocol_version`.

#### Removing a protocol (Pi first)

1. Deploy the Pi code that stops emitting the retiring protocol. Verify no Pi
   still emits it — a stale bench Pi counts.
2. Only then drop it from `version.json`'s
   `supported_capture_protocol_versions`, bump `capture_page_build`, and
   publish. There is no safe window here: the moment the page publishes, any Pi
   still emitting the removed protocol fails the handshake loudly on every
   capture until it is redeployed.

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
node tests/js/capture_integrity_test.mjs     # per-take focus log + block accounting
node tests/js/capture_return_url_test.mjs    # sanitized local-Pi return URL
node tests/js/capture_level_events_test.mjs  # batched phone-side level events
node tests/js/capture_setup_store_test.mjs   # sliding + absolute setup expiry
node tests/js/capture_protocol_test.mjs      # page/Pi release compatibility
node tests/js/capture_ambient_stats_test.mjs # per-octave-band ambient stats (Wave 2)
node tests/js/capture_plan_loop_test.mjs     # session-spanning capture plan loop
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
