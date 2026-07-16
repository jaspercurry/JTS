<!--
SPDX-FileCopyrightText: 2026 Jasper Curry
SPDX-License-Identifier: Apache-2.0
-->

# JTS phone-mic capture relay (Cloudflare Worker + R2)

A **stateless, dumb, opaque** dead-drop relay. One Worker + one R2 bucket serves
the entire JTS fleet identically: no per-device record, no cert, and nothing to
renew. Production can add one shared Pi registration secret without changing the
session model; the secret only gates `POST /sessions` and never decrypts room
audio. This is the O(1) half of the phone-mic capture transport — see
[`docs/phone-mic-relay-plan.md`](../docs/phone-mic-relay-plan.md) §§2, 4, 7, 8
for why it wins over per-Pi certs.

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
| `POST /sessions` | optional registration secret | Pi registers `{session_id, capture_spec (opaque string), upload_token, pull_token, ttl_s, max_upload_bytes}`. Tokens stored as SHA-256 hashes. If Worker secret `RELAY_REGISTRATION_TOKEN` is set, the Pi must send matching header `X-JTS-Relay-Registration-Token`. |
| `GET /sessions/:id/spec` | upload | Phone fetches the opaque spec (served verbatim). |
| `POST /sessions/:id/event` | upload | Phone posts the relay-control envelope, e.g. `{armed:true}`, to its OWN `event/<id>` object (see "Storage layout" below). |
| `GET /sessions/:id/phone-status` | upload | Phone polls `{state, host_event, expires_at}` (backs `fetchPhoneStatus` in `capture-page/js/relay-client.js`). |
| `PUT /sessions/:id/blob` | upload | Phone uploads `IV‖ciphertext` (octet-stream) + `X-Plaintext-Length` / `X-Plaintext-Sha256` integrity headers. |
| `GET /sessions/:id/status` | pull | Pi polls `{state, size, integrity, event, host_event, expires_at}`. |
| `POST /sessions/:id/host-event` | pull | Pi posts a host-side control event to its OWN `hostevent/<id>` object. |
| `GET /sessions/:id/blob` | pull | Pi pulls ciphertext (+ integrity headers). Non-destructive. |
| `DELETE /sessions/:id` | pull | Pi purges after a verified decrypt (meta + blob + event + host_event). |

`GET /healthz` → `ok`. Sessions auto-expire at `ttl_s` (default 900 s, clamped
60–3600) and self-delete on the next access past expiry.

## Hardening (plan §8)

- **Opaque** spec + blob — never parsed (behavioural + structural tests).
- **Hashed tokens** — only SHA-256 hashes are stored; bearer tokens are compared
  in constant time. The two tokens must differ (the privilege split is the
  point).
- **Optional Pi-only registration secret** — when `RELAY_REGISTRATION_TOKEN` is
  set as a Cloudflare Worker secret, `POST /sessions` requires matching
  `X-JTS-Relay-Registration-Token` from the Pi. This prevents arbitrary
  internet clients from minting sessions in your bucket while keeping the actual
  secret out of the open-source repo.
- **Dual size cap** — the upload is rejected by declared `Content-Length` before
  buffering *and* by actual bytes, at both the Worker (`min(per-session cap,
  64 MiB hard ceiling)`) and the Pi.
- **Per-session rate limit** on the phone-facing endpoints so a leaked
  `upload_token` cannot hammer the bucket within the TTL. Production uses the
  Cloudflare-managed `RELAY_RATELIMIT` binding; absent it, a best-effort
  per-isolate fixed window applies.
- **Short TTL + delete-after-pull** — the registration secret cannot decrypt or
  pull captures; relay compromise is still bounded to short-lived ciphertext +
  a non-secret spec.

## Deploy (one-time)

```sh
cd relay
npx wrangler r2 bucket create jts-capture-relay          # object store
# COARSE TTL backstop in case a Pi never pulls. This is a *floor* of 1 day (R2
# lifecycle granularity), 24x looser than the session TTL (<=1 h, MAX_TTL_S). The
# real reclaim is the Worker's on-access self-delete past `expires_at`
# (loadLive); the lifecycle rule only catches sessions never touched again.
npx wrangler r2 bucket lifecycle add jts-capture-relay \
    --expire-days 1 --prefix ""                          # or set in the dashboard
openssl rand -hex 32                                      # generate a private value
npx wrangler secret put RELAY_REGISTRATION_TOKEN          # paste that value
npx wrangler deploy                                      # publishes the Worker
```

Then attach a custom domain (e.g. `relay.jasper.tech`) in the Cloudflare
dashboard (Workers & Pages → this worker → Settings → Domains & Routes), and set
`CAPTURE_ORIGIN` in `wrangler.toml` to the capture page's origin so CORS allows
it. The `RELAY_RATELIMIT` binding (declared in `wrangler.toml` under
`[[ratelimits]]`; `namespace_id` must be unique within your account) backs BOTH
the per-session limit and the per-IP **registration** limit
(`reg:<cf-connecting-ip>`) that bounds open `POST /sessions` flooding. Absent
the binding, the Worker falls back to a per-isolate in-memory counter that never
writes R2 (so it can neither amplify writes nor clobber session state).

Fresh JTS installs default to the Jasper Tech deployment:

```sh
JASPER_CAPTURE_RELAY_BASE=https://relay.jasper.tech
JASPER_CAPTURE_ORIGIN=capture.jasper.tech
```

This default exists because phone microphone access (`getUserMedia`) requires a
secure context with a publicly trusted HTTPS certificate. A LAN-only Raspberry Pi
with a self-signed cert is fragile on iOS and blocked for microphone access by
Android Chrome; the trusted capture page records on `capture.jasper.tech`, while
the Pi stays behind NAT and pulls only E2E-encrypted blobs over outbound HTTPS.

To self-host, deploy this Worker from `relay/`, deploy the trusted capture page
from [`capture-page/`](../capture-page/README.md), then override the Pi's
`/etc/jasper/jasper.env` values (or export them for `scripts/deploy-to-pi.sh` /
`deploy/install.sh`):

```sh
JASPER_CAPTURE_RELAY_BASE=https://relay.example.com
JASPER_CAPTURE_ORIGIN=capture.example.com
JASPER_CAPTURE_RELAY_REGISTRATION_TOKEN=<same hex value>
```

Leave the token blank only for a self-hosted/dev relay whose Worker does not set
`RELAY_REGISTRATION_TOKEN`.

## Test

```sh
node tests/js/relay_worker_test.mjs        # or: cd relay && npm test
```

Runs in CI through `tests/test_relay_worker_js.py` (pytest) and
`scripts/check-js-syntax.sh` (`node --check`). No Cloudflare account needed — the
router is exercised against an in-memory store.

## Storage layout — one R2 object per session, plus two split control fields

Each session has up to four objects, all keyed by the same `<id>`:

| Object | Written by | Read by |
|---|---|---|
| `meta/<id>` | `registerSession` (create), `putBlob` (state/integrity/size) | every handler that needs session state |
| `blob/<id>` | phone `PUT /blob` | Pi `GET /blob` |
| `event/<id>` | phone `POST /event` (sole writer) | Pi `GET /status` (`event` field) |
| `hostevent/<id>` | Pi `POST /host-event` (sole writer) | Pi/phone `GET /status` / `GET /phone-status` (`host_event` field) |

`event` and `host_event` are **not** stored inside `meta/<id>`. They used to
be, and that was the bug: `postEvent`/`postHostEvent` each read the whole
`meta/<id>` object at request start and wrote the whole object back
(`putMeta`) — last-write-wins on the ENTIRE object, not just the field being
set. A phone `POST /event` whose request-start read predated the Pi's
terminal `POST /host-event` would land afterward and silently revert
`host_event` to the phone's stale snapshot. This is exactly what happened on
JTS3 on 2026-07-15 (see
[`docs/HANDOFF-correction.md`](../docs/HANDOFF-correction.md)): a locked
level-match ramp's terminal host-event was reverted by an interleaved phone
event post, and the phone's own deadline fired even though the server had
already succeeded. The Pi's terminal re-post latch (5 attempts) made a lost
terminal *unlikely* but not impossible. Splitting `event`/`host_event` into
their own keys makes the clobber **structurally impossible** — `postEvent`
never touches `hostevent/<id>` and vice versa, regardless of request timing.

**Compat window for in-flight sessions.** A session registered by pre-deploy
code may still carry `event`/`host_event` embedded in its `meta/<id>` object
(the old write path), with no split key ever created for it. The status
readers (`GET /status`, `GET /phone-status`) prefer the split key and fall
back to the legacy `meta.event` / `meta.host_event` field only when the split
key is absent. Once any post-deploy write lands, the split key exists and
wins from then on. No version flag or migration step — every session's TTL
is <=3600 s (`MAX_TTL_S`), so the legacy shape retires on its own within an
hour of deploy.

**Lifecycle/cleanup covers all four objects.** The on-access self-delete in
`loadLive` (past `expires_at`) and the explicit `DELETE /sessions/:id` path
both purge `meta`, `blob`, `event`, and `hostevent` for the id — no orphaned
objects. The R2 bucket lifecycle rule set up below uses an empty `--prefix`,
i.e. it already applies bucket-wide, so `event/` and `hostevent/` are covered
by the same 1-day backstop as `meta/` and `blob/` with no dashboard change.

**Consistency.** R2 gives strong read-after-write consistency per object,
which covers the Pi's poll loop (it reads `event/<id>` after the phone wrote
it) and the phone's `phone-status` poll of `host_event` (it reads
`hostevent/<id>` after the Pi wrote it via `host-event`). Each of the four
objects above is an independent read-modify-write unit, so a write to one can
never revert a concurrent write to another. If a future build wants to
tighten the ~1 s poll latency to real-time, the upgrade is Durable Objects /
long-poll (plan §5) — layered on this same session/spec machinery, no
relay-contract change.
