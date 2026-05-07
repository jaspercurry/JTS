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

State lives at:
```
/var/lib/jasper/spotify/
    accounts.json              registry index
    caches/<name>.json         per-user OAuth refresh tokens
```

## Setup, end-to-end

### 1. Spotify Developer App (one human, one time, ever)

The owner of the speaker creates a single Spotify Developer App at
https://developer.spotify.com/dashboard. This is the same pattern
Sonos uses — Sonos owns one Spotify app, and every Sonos owner
OAuths their personal account against it. Brittany never sees the
developer dashboard.

- Name: anything ("Jasper Smart Speaker")
- Redirect URI: **`https://jasper.local/spotify/callback`** — must
  be HTTPS. Spotify rejects HTTP for non-loopback hosts as of late
  2024. The Pi serves HTTPS via a self-signed cert (see TLS section
  below).
- APIs: just "Web API"
- Save → copy Client ID + Client Secret → paste into
  `/etc/jasper/jasper.env`:
  ```
  SPOTIFY_CLIENT_ID=…
  SPOTIFY_CLIENT_SECRET=…
  ```
- User Management → add each household member's Spotify-account email
  (the one they log in with). Development Mode allows up to 25 named
  users. Past 25, you'd need to apply for Extended Quota.

### 2. Each household member visits the setup page

Each person, on their own phone:

1. Open `https://jasper.local/spotify` in their browser.
2. **Click through the cert warning.** Self-signed certs trip
   "connection not private" warnings. iOS Safari: tap "Show details"
   → "visit anyway." Chrome: "Advanced" → "Proceed." This is
   one-time-per-device — the browser remembers.
3. Pick a label name — a short identifier the speaker uses
   internally. Lowercase, no spaces. Not a display name.
4. "Continue with Spotify" → Spotify login → "Agree" → bounced back
   to the speaker page. Account now appears in the list.

That's it. No per-device setup. No "what's the AirPlay name on this
device" form to fill in.

### 3. Restart `jasper-voice` once

The voice daemon reads the registry at startup. After the first
account is added, restart it once so the router builds clients for
the new accounts:
```
sudo systemctl restart jasper-voice
```
Subsequent additions don't strictly need a restart — the router will
just skip routing to a freshly-added account until next restart, since
its OAuth token isn't loaded into the in-memory client map yet. A
restart is the cleanest way to make a new account fully active.

## How routing actually works

When you say "next song" / "previous" / "pause" / "resume":

1. `_detect_source` reads the renderer's per-source flags and figures
   out the active source: `airplay`, `spotify` (Connect), `bluetooth`,
   or `mpd`.
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
   on their device, or to link their account at jasper.local/spotify.

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
| Volume / mute | Source-agnostic — always CamillaDSP main fader. Doesn't touch Spotify. | Same |

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
gracefully when somebody's casting a podcast or YouTube tab — it'll
work for them as long as they're not on iOS 17.4+ Spotify (and if
they are, they're already on the title-match path anyway).

**Note on MPRIS metadata reliability:** shairport's MPRIS
`xesam:title` is populated by both Mac Spotify and iOS Spotify (verified
on iOS 18 / Spotify 9.x as of 2026-05). Earlier versions of these
docs incorrectly conflated DACP-broken-on-iOS with MPRIS-metadata-
broken-on-iOS; only DACP is broken. Metadata flows fine.

## TLS / self-signed cert

Spotify's redirect URI rules require HTTPS for any non-loopback
host. The Pi serves `https://jasper.local` via a self-signed cert
generated at install time:

- Cert: `/etc/nginx/ssl/jasper.crt` (10-year validity, SANs for
  `jasper.local`, `jasper`, `127.0.0.1`)
- Key: `/etc/nginx/ssl/jasper.key`
- nginx site: `/etc/nginx/sites-enabled/jasper.conf` (port 80
  redirects to HTTPS; port 443 with the cert above)

Each device clicks through "not private" once. To eliminate the
warning, install the cert on the device's trust store (iOS: AirDrop
the .crt to the phone, Settings → General → VPN & Device
Management → install profile, then Settings → General → About →
Certificate Trust Settings → enable trust).

The cert is generated by `install_self_signed_cert` in
`deploy/install.sh` and re-used on subsequent installs (idempotent).
To regenerate, delete `/etc/nginx/ssl/jasper.{crt,key}` and re-run
the install script.

## Adding / removing accounts

`https://jasper.local/spotify`:
- Add: enter a label, OAuth, done.
- Remove: click "Remove" next to the account. Wipes the cache file
  and removes the registry entry.
- Set default: click "Set default" — picked when no AirPlay is
  active and no other account is `is_playing` (cold-start commands
  like "play Beyoncé" from silence).

After any change, restart `jasper-voice` to rebuild the in-memory
router:
```
sudo systemctl restart jasper-voice
```

## Files / locations cheat-sheet

```
/etc/jasper/jasper.env                       env vars (creds, paths)
/etc/nginx/jasper-locations.conf             nginx /spotify/ proxy block
/etc/nginx/sites-enabled/jasper-https.conf   nginx HTTPS server block
/etc/nginx/ssl/jasper.{crt,key}              self-signed cert
/etc/systemd/system/jasper-web.service       setup web server (port 8765)
/etc/systemd/system/jasper-voice.service     voice daemon
/var/lib/jasper/spotify/accounts.json        registry index
/var/lib/jasper/spotify/caches/<name>.json   per-user OAuth refresh tokens
```

Code:
```
jasper/accounts.py          Registry / Account
jasper/spotify_router.py    Router.resolve_for_transport / Router.active
jasper/spotify_routing.py   resolve_target (cold-start device picker, title _normalise)
jasper/web/spotify_setup.py jasper-web HTTP service
jasper/tools/transport.py   AirPlay / Spotify / MPD / Bluetooth dispatch
jasper/tools/spotify.py     spotify_play / spotify_queue (router-aware)
deploy/nginx-jasper.conf            /spotify/ proxy block
deploy/nginx-jasper-https.conf      HTTPS site config
deploy/jasper-web.service           systemd unit for jasper-web
```
