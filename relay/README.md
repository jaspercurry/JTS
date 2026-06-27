<!--
SPDX-FileCopyrightText: 2026 Jasper Curry
SPDX-License-Identifier: Apache-2.0
-->

# JTS phone-mic capture relay (Cloudflare Worker + R2)

A **stateless, dumb, opaque** dead-drop relay. One Worker + one R2 bucket serves
the entire JTS fleet identically: no per-device record, no cert, nothing to
renew, no powerful secret to guard. This is the O(1) half of the phone-mic
capture transport — see [`docs/phone-mic-relay-plan.md`](../docs/phone-mic-relay-plan.md)
§§2, 4, 7, 8 for why it wins over per-Pi certs.

It carries two things between a phone (on a trusted cloud capture page) and a Pi
(behind home NAT, outbound-only): a per-session **opaque capture spec** and one
**end-to-end-encrypted blob**. It never interprets either, and it never receives
the encryption key.

## What it is NOT

- It is **not** an analysis pipeline. The Pi pulls the blob and runs the same
  `correction_setup.py` analysis it always has.
- It does **not** parse the `capture_spec` or the blob. Adding a measurement
  kind needs **zero** changes here (pinned by `tests/js/relay_worker_test.mjs`).
- It cannot read room audio: the AES-256-GCM `content_key` rides the page URL
  **fragment** (never transmitted to any server), so the relay stores ciphertext
  only.

## Endpoints (`relay/src/worker.js`)

| Method + path | Auth | Purpose |
|---|---|---|
| `POST /sessions` | — | Pi registers `{session_id, capture_spec (opaque string), upload_token, pull_token, ttl_s, max_upload_bytes}`. Tokens stored as SHA-256 hashes. |
| `GET /sessions/:id/spec` | upload | Phone fetches the opaque spec (served verbatim). |
| `POST /sessions/:id/event` | upload | Phone posts the relay-control envelope, e.g. `{armed:true}`. |
| `PUT /sessions/:id/blob` | upload | Phone uploads `IV‖ciphertext` (octet-stream) + `X-Plaintext-Length` / `X-Plaintext-Sha256` integrity headers. |
| `GET /sessions/:id/status` | pull | Pi polls `{state, size, integrity, event, expires_at}`. |
| `GET /sessions/:id/blob` | pull | Pi pulls ciphertext (+ integrity headers). Non-destructive. |
| `DELETE /sessions/:id` | pull | Pi purges after a verified decrypt. |

`GET /healthz` → `ok`. Sessions auto-expire at `ttl_s` (default 900 s, clamped
60–3600) and self-delete on the next access past expiry.

## Hardening (plan §8)

- **Opaque** spec + blob — never parsed (behavioural + structural tests).
- **Hashed tokens** — only SHA-256 hashes are stored; bearer tokens are compared
  in constant time. The two tokens must differ (the privilege split is the
  point).
- **Dual size cap** — the upload is rejected by declared `Content-Length` before
  buffering *and* by actual bytes, at both the Worker (`min(per-session cap,
  64 MiB hard ceiling)`) and the Pi.
- **Per-session rate limit** on the phone-facing endpoints so a leaked
  `upload_token` cannot hammer the bucket within the TTL. Production uses the
  atomic `RELAY_RATELIMIT` binding; absent it, a best-effort in-meta fixed window
  applies.
- **Short TTL + delete-after-pull** — the relay holds no powerful secret; its
  compromise is bounded to short-lived ciphertext + a non-secret spec.

## Deploy (one-time)

```sh
cd relay
npx wrangler r2 bucket create jts-capture-relay          # object store
# TTL backstop: auto-delete anything older than ~1 h (in case a Pi never pulls)
npx wrangler r2 bucket lifecycle add jts-capture-relay \
    --expire-days 1 --prefix ""                          # or set in the dashboard
npx wrangler deploy                                      # publishes the Worker
```

Then attach a custom domain (e.g. `relay.jasper.tech`) in the Cloudflare
dashboard (Workers & Pages → this worker → Settings → Domains & Routes), and set
`CAPTURE_ORIGIN` in `wrangler.toml` to the capture page's origin so CORS allows
it. The `RELAY_RATELIMIT` binding is declared in `wrangler.toml`.

## Test

```sh
node tests/js/relay_worker_test.mjs        # or: cd relay && npm test
```

Runs in CI through `tests/test_relay_worker_js.py` (pytest) and
`scripts/check-js-syntax.sh` (`node --check`). No Cloudflare account needed — the
router is exercised against an in-memory store.

## Storage consistency note

R2 gives strong read-after-write consistency per object, which covers the Pi's
poll loop (it reads `meta/<id>` after the phone wrote it). Phone-side mutations
(event, blob) are sequential, so the `armed`/`ready` state never races. If a
future build wants to tighten the ~1 s poll latency to real-time, the upgrade is
Durable Objects / long-poll (plan §5) — layered on this same session/spec
machinery, no relay-contract change.
