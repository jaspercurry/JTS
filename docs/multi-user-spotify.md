# Multi-user Spotify on the speaker

How household members share one speaker with separate Spotify accounts —
the architecture, the one-time setup, and the gotchas you'll forget in
six months.

## What this solves

The speaker is a household device. Two or more people may want to
issue voice commands and have those commands hit *their* Spotify
account, not somebody else's. There's no shared family token in
Spotify's world — every API call is scoped to one OAuth refresh
token. So we maintain one token per household member and pick the
right one based on **what's actually playing right now**.

The keystone signal: the **track title pushed over AirPlay**, read
from shairport-sync's MPRIS `xesam:title`. The router cross-references
that against each account's Spotify Web API `current_playback.item.name`
and routes to whoever's playing the same song. No per-device setup,
no fragile name-matching.

```
┌──────────────────────────┐       ┌──────────────────────────┐
│  shairport-sync MPRIS    │       │  Spotify Web API         │
│  xesam:title =           │       │  current_playback.item   │
│   "Hey Jude"             │       │   .name (per account)    │
└──────────────┬───────────┘       └──────────────┬───────────┘
               │                                  │
               └─────────────┐         ┌──────────┘
                             ▼         ▼
                  ┌──────────────────────────────┐
                  │  Router.resolve_for_         │
                  │   transport(client, title)   │
                  │  → the account whose         │
                  │    current track matches     │
                  └──────────────┬───────────────┘
                                 ▼
                    transport / spotify_play tools
                    issue commands against that
                    account's spotipy client
```

Secret state lives in the WS1 Phase 4b integration-secret compartment:
```
/var/lib/jasper-intsecrets/spotify/
    accounts.json              registry index
    caches/<name>.json         per-user OAuth refresh tokens (PKCE)
/var/lib/jasper-intsecrets/spotify_credentials.env
                               SPOTIFY_CLIENT_ID + SPOTIFY_OAUTH_MODE
                               (written by the wizard)
```

## OAuth flow

The speaker uses **Authorization Code with PKCE**. PKCE was designed
for clients that can't keep secrets — which a smart speaker on a
home network definitely is. The user pastes only the Client ID into
the wizard; no Client Secret is needed.

Spotify's redirect-URI rules (post-November-2025) require HTTPS for
any non-loopback host. We side-step the cert problem two ways; the
wizard offers both as a radio-group choice.

### Bounce mode (default)

Spotify is given a redirect URI on a host that already has a real
cert. The wizard composes:

```
https://jaspercurry.github.io/spotify-oauth-callback/?host=${JASPER_HOSTNAME}
```

That URL serves a tiny static page from a separate public repo
(`jaspercurry/spotify-oauth-callback`); the page parses `code`,
`state`, and `host` from its query string, validates `host` against
an mDNS regex (`*.local`), and `window.location.href`s the browser
to `http://${host}/spotify/oauth-callback?code=…&state=…` over plain
HTTP. Cross-scheme navigation (HTTPS → HTTP) is a normal cross-origin
redirect; mixed-content rules apply to subresource fetches, not
navigations.

The page is hostname-agnostic by design: a single hosted page works
for any speaker hostname, with no fork-and-redeploy required. If you
rename your speaker via `JASPER_HOSTNAME=foo.local`, the wizard's
default redirect URI becomes
`https://jaspercurry.github.io/spotify-oauth-callback/?host=foo.local`
automatically — you just register that exact value in your Spotify
Developer App.

If the bounce-back fails (different Wi-Fi, mDNS broken on the device
that did the OAuth, cellular, etc.), the bounce page's 4-second
timeout surfaces a fallback: it shows the full speaker callback URL
so the user can open it on any device that *is* on the home network.
One tap finishes the flow.

### Manual paste mode

No external infrastructure. The redirect URI is the loopback
exception Spotify still allows, `http://127.0.0.1:8888/callback`.
The user's phone obviously can't reach 127.0.0.1 (that's the phone,
not the speaker), so Safari shows "cannot connect" — the wizard
pre-warns about this on the page that launches the flow, so it
doesn't look like an error. The user copies the URL from the
address bar and pastes it back into the speaker. The wizard parses
out the code and state and exchanges via PKCE.

This mode is purely UX-ier in exchange for zero dependencies on
GitHub Pages or any other third party.

### CSRF state

Each `/start` generates a fresh random nonce, stashed server-side in
a 10-minute pending-flows map keyed to the account name. The nonce
is sent as Spotify's `state` parameter. On callback, the nonce is
looked up to recover the account name; unknown or expired nonces are
rejected. This protects against cross-site request forgery on the
callback endpoint.

The PKCE code verifier itself lives in the per-account spotipy cache
file between `/start` and the callback — spotipy writes it during
`get_authorize_url()` and reads it during `get_access_token(code)`.

## Setup, end-to-end

### 1. Spotify Developer App (one human, one time, ever)

The owner of the speaker creates a single Spotify Developer App at
https://developer.spotify.com/dashboard. This is the same pattern
Sonos uses — Sonos owns one Spotify app, and every Sonos owner
OAuths their personal account against it.

- Name: anything ("Jasper Smart Speaker")
- Redirect URI: copy whichever one the wizard's settings page shows
  for your chosen mode:
  - bounce: `https://jaspercurry.github.io/spotify-oauth-callback/?host=${JASPER_HOSTNAME}`
  - manual: `http://127.0.0.1:8888/callback`
- APIs: just "Web API"
- Save → copy **Client ID** (you do NOT need the Client Secret —
  PKCE doesn't use it)
- User Management → add each household member's Spotify-account email
  (the one they log in with). Development Mode allows up to 5 named
  users (was 25 before February 2026). Past 5, you'd need to apply
  for Extended Quota — and as of May 2025 that's only available to
  registered businesses with 250k+ MAUs.

### 2. Each household member visits the setup page

Each person, on their own phone or laptop on the home Wi-Fi:

1. Open `http://jts.local/spotify` in their browser. **Plain HTTP** —
   no cert warning to click through.
2. Pick a label name — a short identifier the speaker uses
   internally. Lowercase, no spaces. Not a display name.
3. "Continue with Spotify" → Spotify login → "Agree" → bounced back
   to the speaker page (or, in manual mode, paste the URL from the
   "cannot connect" page). Account now appears in the list.

That's it. No per-device setup. No "what's the AirPlay name on this
device" form to fill in.

### 3. No manual restart needed

The wizard's OAuth-callback handler restarts `jasper-voice` for you
on every successful link — including the very first. The daemon's
router also rebuilds itself lazily on the next voice command if its
clients dict is empty (see "Refresh-token revocation & recovery"
below), so even an out-of-band re-link (e.g., manual cache file
edit) recovers without intervention.

## Refresh-token revocation & recovery

Spotify can revoke a refresh token at any time, on the server side,
with no notice to us. Known triggers:

- Password change
- "Sign out everywhere" from Spotify account settings
- A new OAuth grant for the same `(user, client_id, scopes)` from
  another device superseding the older one
- Refresh-token rotation race (two simultaneous refreshes of the
  same token — the second's refresh is invalidated by the first's
  rotation)
- Spotify-side security sweeps (undocumented; community-reported
  ~60-day inactivity windows)

There is no Spotify API to refresh a refresh token without user
interaction once it's revoked — this is an OAuth 2.0 + PKCE
constraint, not something we can engineer around. The user must
re-link via the wizard.

**What you'll see when this happens:**

- `/spotify` shows a red ⚠ "signed out" badge on the affected
  account's card. The card auto-opens, exposing an inline **Re-link
  *name*** button that POSTs to `/start` with the same account name
  (the OAuth callback then overwrites the existing cache file).
- Voice commands targeting that account return *"spotify signed
  *name* out. tell the user to re-link at http://jts.local/spotify."*
  — spoken by the LLM during the active turn, not as a pre-rendered
  cue. When multiple accounts are revoked, all are named (e.g.
  *"spotify signed jasper and brittany out"*) so the user knows the
  full re-link scope.
- Daemon startup log (`journalctl -u jasper-voice`) includes
  `event=spotify.startup_empty statuses=[('jasper', 'revoked')] setup_url=...`
  if the token was already revoked when the daemon started.
- Account-build failures emit `event=spotify.account_unavailable` at WARN
  once per account/state/detail window, then
  `event=spotify.account_unavailable_suppressed` at DEBUG for repeats. This
  keeps a revoked token from dumping the flight recorder on every dashboard
  poll while preserving the first transition.

**How recovery works (no daemon restart needed):**

1. User clicks Re-link in the wizard, completes the OAuth flow.
2. The wizard's `_exchange_and_finish` handler writes the new token
   to the cache file, invalidates its own probe cache, and restarts
   `jasper-voice`. The restart is the fast path.
3. As a backup, the next voice command after a re-link triggers
   `Router.refresh_if_empty()` — which re-runs `build_clients` and
   atomically replaces the router's `clients` + `statuses` +
   `default_name`. This covers cases where the restart fails or
   the cache file was updated via some other path.

**Implementation pointers:**

- `jasper.spotify_router.build_clients` — returns a `BuildResult`
  with `clients`, per-account `statuses` (`ACCOUNT_OK`,
  `ACCOUNT_NEEDS_OAUTH`, `ACCOUNT_REVOKED`, `ACCOUNT_ERROR`), and
  the registry's `default_name`. `_classify_oauth_error` inspects
  the `SpotifyOauthError.error` attribute first (and falls back to
  text substring search) so the classification survives spotipy
  format changes.
- `jasper.spotify_router.Router.refresh_if_empty()` — lazy rebuild
  on first voice command after the clients dict went empty.
  Rate-limited to once per `_REFRESH_MIN_INTERVAL_SEC` (30s) to
  avoid hammering Spotify's `/api/token` on a persistently-revoked
  account. Transient rebuild failures (exception raised) do NOT
  advance the cooldown — only completed builds do.
- `jasper.spotify_router.Router.empty_reason()` — classifies why
  `clients` is empty (`"revoked"`, `"needs_oauth"`, `"no_accounts"`,
  or `""` when non-empty). Tool layer reads this to pick the right
  user-facing message.
- `jasper.web.spotify_setup._probe_all_health` — wizard's per-page
  health probe, calls `build_clients` and caches the result for 60s.
  Cache is busted on every mutation (OAuth callback, account remove,
  credentials reset, credentials change).
- `jasper.control.volume_ops._build_spotify_router_or_none` — control-side
  best-effort router for dial/web volume and transport. Empty builds are cached
  for 30s, keyed by the account-cache file mtimes, so a persistently revoked
  account does not hit Spotify's token endpoint on every `/state`/`/volume`
  poll but an OAuth re-link takes effect as soon as the cache file changes.
- `jasper.accounts.build_cache_handler` — spotipy cache adapter used by
  every Spotify OAuth client. It publishes refreshed token JSON via a
  tempfile + `os.replace` at mode `0640`, instead of spotipy's stock
  in-place writer, so any `jasper-intsecrets` member can replace an
  older cache file owned by another service while the final file stays
  group-readable rather than group-writable.

**Debugging "Spotify isn't working":**

1. Open `/spotify` — if any card is red, that's your answer.
2. `journalctl -u jasper-voice | grep -E "spotify|event=spotify"` —
   look for `event=spotify.startup_empty` or
   `router: lazy rebuild produced no clients` lines.
3. `sudo /opt/jasper/.venv/bin/python -c "from jasper.config import Config; from jasper.voice_daemon import _build_router; r = _build_router(Config.from_env()); print('clients:', list(r.clients.keys()) if r else 'no router'); print('statuses:', [(s.name, s.state) for s in (r.statuses if r else [])])"` for the live state.

`sudo systemctl restart jasper-voice` should NOT be the first
debugging step — the wizard already restarts on link, and the lazy
rebuild covers any path that doesn't go through the wizard.

## How routing actually works

When you say "next song" / "previous" / "pause" / "resume":

1. `_detect_source` asks mux for `RendererClient.selected_source()` and
   uses the effective audible source chosen by manual source selection
   or auto handoff policy. If mux is unavailable, it falls back to raw
   renderer flags. The result is `airplay`, `spotify` (Connect),
   `bluetooth`, `usbsink`, or `none` (nothing playing).
2. For **AirPlay**: read shairport's MPRIS `xesam:title` and the
   AirPlay `ClientName`. Then call
   `Router.resolve_for_transport(client_name, mpris_title)`:
   - For each configured account, fetch `current_playback.item.name`
     in parallel.
   - The account whose normalized title equals the MPRIS title
     wins.
   - If multiple accounts queue the same track, prefer the one with
     `is_playing=True` (that's the AirPlay sender; others are paused
     or stalled). Still tied → default account.
   - Cache the decision, keyed on `(client_name, normalized_title)`.
     Re-resolve on track change, sender change, or 1h TTL.
3. If a Spotify account matched → call Next/Previous/Pause/Play on
   that account's Web API targeting its active device. iOS Spotify
   (and any other Spotify-AirPlay session) is controllable via this
   path; iOS 17.4+ broke the DACP/MPRIS path for AirPlay 2 (shairport
   #1822), making Spotify Web API the canonical answer.
4. If no account matched (AirPlay sender is Apple Music, a podcast
   app, a browser tab, etc.) → fall back to DACP via shairport's
   MPRIS `Next/Previous/Pause/Play`. Works for legacy AirPlay 1 and
   older Apple Music builds; silently no-ops on iOS 17.4+ for non-
   Spotify senders.
5. If DACP isn't available either → tell the user to use the controls
   on their device, or to link their account at jts.local/spotify.

For **Spotify Connect** (no AirPlay) and `spotify_play` cold-starts,
the title cross-reference doesn't apply (no track to match) — fall
through to whichever account reports `is_playing=true`, then to the
configured default.

## Why the router cross-references titles instead of device names

The first iteration of this used per-device-name patterns ("Jasper's
iPhone" → account `jasper`). It broke in three ways:

- Same person, different device. AirPlaying from your Mac when only
  your iPhone was registered → no match → command refused.
- Devices get renamed. iOS lets you rename your phone freely; macOS
  device names drift over time. Patterns went stale silently.
- Speaker owner is out of the house, guest is AirPlaying. Pattern
  matched no account → command refused. Even though there's exactly
  one Spotify account currently playing, we couldn't route.

Title cross-reference fixes all three: who you are doesn't matter,
what device you're on doesn't matter, only what you're playing right
now. Self-correcting on every track change. The only real failure
mode is two household members listening to the exact same song at
the exact same instant on different devices, which is rare enough to
ignore (and tiebroken by `is_playing` then default-account in any
case).

## What gets routed where

| Voice command | When AirPlay is active | When AirPlay isn't active |
|---|---|---|
| "Next song" / "Skip" / "Previous" / "Pause" / "Resume" | `resolve_for_transport` matches sender's track to an account → that account's Spotify Web API. AirPlay stream content updates seamlessly. | Active account's Web API → its active device |
| "Play [song]" / etc. | Title-match resolves the active listener; falls back to is_playing → default | Active account searches + start_playback (target resolved by `spotify_routing`) |
| "What's playing?" | Matched account's `current_playback` (proper title/artist) | Active account's `current_playback` |
| Volume / mute | Source-aware via `VolumeCoordinator`: AirPlay uses CamillaDSP as the speaker volume; Spotify Connect uses Spotify Web API; Bluetooth uses AVRCP. See [HANDOFF-volume.md](HANDOFF-volume.md). | Same |

## Verifying a route landed correctly

Look for `router:` lines in the daemon log:
```
journalctl -u jasper-voice -n 50 | grep -E "router:"
```
You'll see one of:
- `router: airplay sender 'Jasper's Mac Studio' playing 'Hey Jude' matched account jasper (1 title-matches, 1 playing)`
- `router: airplay sender 'Jasper's iPhone' playing 'Hey Jude' matched no account (0 of 2 accounts have this title)`
- `router: account jasper reports is_playing=true` (cold-start path)
- `router: falling back to default account jasper`

To inspect the AirPlay state directly:
```
# Sender ClientName
dbus-send --system --print-reply \
    --dest=org.gnome.ShairportSync /org/gnome/ShairportSync \
    org.freedesktop.DBus.Properties.Get \
    string:org.gnome.ShairportSync.RemoteControl string:ClientName

# Currently-playing track (xesam:title shows what we cross-reference against)
dbus-send --system --print-reply \
    --dest=org.mpris.MediaPlayer2.ShairportSync /org/mpris/MediaPlayer2 \
    org.freedesktop.DBus.Properties.Get \
    string:org.mpris.MediaPlayer2.Player string:Metadata
```

## Why iOS Spotify needs the Web API path (not just DACP)

shairport-sync exposes an MPRIS DBus interface that nominally lets
you send Next/Previous/Pause/Play to the AirPlay sender via DACP.
This worked on iOS 17.3 and earlier. **iOS 17.4 (March 2024) stopped
sending the DACP-ID and Active-Remote RTSP headers in AirPlay 2 mode
for every sender app** — Apple Music, Spotify, all of them. shairport
maintainer Mike Brady documented it as a "permanent change at Apple's
end" in [issue #1822](https://github.com/mikebrady/shairport-sync/issues/1822).

So shairport's DBus `Next` is a silent no-op for any modern iOS
session. HomePods sidestep this via Apple's proprietary MRP-over-
AirPlay-2 protocol, which is closed-source and not implemented in
shairport. The title-match-then-Web-API path is the only working
alternative for controlling iOS Spotify from the receiver side.

DACP/MPRIS is still wired up as a fallback for non-Spotify AirPlay
sources that DO expose it (older iOS, Apple Music app on macOS pre-
14.4, some non-Apple AirPlay senders), so the speaker degrades
gracefully when somebody's casting a podcast or YouTube tab.

**Note on MPRIS metadata reliability:** shairport's MPRIS
`xesam:title` is populated by both Mac Spotify and iOS Spotify
(verified on iOS 18 / Spotify 9.x as of 2026-05). Earlier versions
of these docs incorrectly conflated DACP-broken-on-iOS with
MPRIS-metadata-broken-on-iOS; only DACP is broken. Metadata flows
fine.

## Migrating from the old Code+Secret flow

Earlier installs used Authorization Code with a Client Secret pasted
into the wizard, served over HTTPS with a self-signed cert at
`/etc/nginx/ssl/jasper.{crt,key}`. The cert tripped scary "connection
not private" warnings on every browser. The migration to PKCE +
plain HTTP was a deliberate trade.

What this means for upgrading installs:

- The cert + key files are removed by the install script
  (`remove_legacy_https_artifacts` in `deploy/install.sh`); nginx is
  reconfigured to plain HTTP only.
- The wizard's `spotify_credentials.env` schema changed from
  `SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET` to
  `SPOTIFY_CLIENT_ID + SPOTIFY_OAUTH_MODE`. The wizard re-prompts
  for the Client ID on first visit after the upgrade; the old
  Client Secret is no longer needed.
- **Existing per-account refresh tokens become unusable.** They were
  issued under Code+Secret, which Spotify validates against the
  Authorization header on refresh. PKCE refresh sends `client_id`
  in the body and no Authorization header — Spotify rejects this
  for non-PKCE-issued tokens. Each household member needs to re-link
  their account once via the wizard.

## Adding / removing accounts

`http://jts.local/spotify`:
- Add: enter a label, OAuth, done.
- Remove: click "Remove" next to the account. Wipes the cache file
  and removes the registry entry.
- Set default: click "Set default" — picked when no AirPlay is
  active and no other account is `is_playing` (cold-start commands
  like "play Beyoncé" from silence).

The wizard restarts `jasper-voice` automatically after each change.

## Files / locations cheat-sheet

```
/etc/jasper/jasper.env                       env vars (paths, etc.)
/etc/nginx/sites-enabled/jasper.conf         nginx /spotify/ + /voice/ + /dial/
/etc/systemd/system/jasper-web.service       setup web server (port 8765)
/etc/systemd/system/jasper-voice.service     voice daemon
/var/lib/jasper-intsecrets/spotify_credentials.env      SPOTIFY_CLIENT_ID + SPOTIFY_OAUTH_MODE
/var/lib/jasper-intsecrets/spotify/accounts.json        registry index
/var/lib/jasper-intsecrets/spotify/caches/<name>.json   per-user OAuth refresh tokens
```

Code:
```
jaspercurry/spotify-oauth-callback    GitHub Pages bounce page (separate
                                       public repo, static, hostname-agnostic
                                       via `?host=` query param)
jasper/accounts.py                    Registry / Account
jasper/spotify_router.py              Router.resolve_for_transport / Router.active /
                                       build_clients (PKCE)
jasper/spotify_routing.py             resolve_target (cold-start device picker, _normalise)
jasper/web/spotify_setup.py           jasper-web HTTP service (PKCE wizard)
jasper/cli/spotify_auth.py            CLI bootstrap (PKCE)
jasper/tools/transport.py             AirPlay / Spotify / Bluetooth / no-source dispatch
jasper/tools/spotify.py               spotify_play / spotify_queue (router-aware)
deploy/nginx-jasper.conf              /spotify/ + /voice/ + /dial/ proxy (HTTP only)
deploy/jasper-web.service             systemd unit for jasper-web
```
