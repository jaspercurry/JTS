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
right one based on who's currently using the speaker.

The keystone signal: **AirPlay's `ClientName`** (e.g. `"Brittany's
iPhone"`), which shairport-sync exposes via DBus on every active
session. The router matches that against per-account "device name"
patterns to decide who to route a command to. With no AirPlay active,
it falls back to `is_playing` polled across each account's Spotify
Web API session, then to a configured default.

```
┌──────────────────────┐       ┌──────────────────────┐
│  shairport-sync      │       │  Spotify Web API     │
│  ClientName=         │       │  current_playback    │
│   "Brittany's iPhone"│       │  per account         │
└──────────┬───────────┘       └──────────┬───────────┘
           │                              │
           └──────────┐         ┌─────────┘
                     ▼         ▼
              ┌──────────────────────┐
              │  Router.active()     │
              │  1. ClientName match │
              │  2. is_playing       │
              │  3. default account  │
              └──────────┬───────────┘
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

This is one-time-per-household-member work.

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

1. Connect to AirPlay on the Pi (helps the form auto-detect their
   device name — not required, but convenient).
2. Open `https://jasper.local/spotify` in their browser.
3. **Click through the cert warning.** Self-signed certs trip
   "connection not private" warnings. iOS Safari: tap "Show details"
   → "visit anyway." Chrome: "Advanced" → "Proceed." This is
   one-time-per-device — the browser remembers. (To avoid the
   warning entirely, install the cert on the device.)
4. Pick a label name — a short identifier the speaker uses
   internally. Lowercase, no spaces. Not a display name.
5. Confirm/edit the device-name pattern (auto-detected if AirPlay is
   active right now). See **Device-name matching** below.
6. "Continue with Spotify" → Spotify login → "Agree" → bounced back
   to the speaker page. Account now appears in the list.

### 3. Restart `jasper-voice` once

The voice daemon reads the registry at startup. After the first
account is added, restart it once so the router builds clients for
the new accounts:
```
sudo systemctl restart jasper-voice
```
Subsequent additions don't strictly need a restart — the router will
notice the missing client and skip routing to that account until next
restart — but a restart is the cleanest way to make a freshly-added
account fully active.

## Device-name matching

This is the part that's easy to get wrong and worth understanding.

The router looks at shairport's `ClientName` for the active AirPlay
session and matches it against each account's `client_name_patterns`
list. The match is **case-insensitive substring with smart-quote
normalisation**:

| Pattern stored | ClientName seen | Match? |
|---|---|---|
| `Jasper's iPhone` | `Jasper's iPhone` | ✓ (smart `’` ↔ regular `'`) |
| `JASPER` | `Jasper's iPhone` | ✓ (case-insensitive) |
| `iPhone` | `Jasper's iPhone` | ✓ (substring) |
| `Jasper` | `Jasper's iPhone 15 Pro Max` | ✓ |
| `Jasper's iPad` | `Jasper's iPhone` | ✗ (no overlap) |
| `Curry` | `Jasper's iPhone` | ✗ |

So you can be loose. The simplest robust pattern is just your first
name — it'll match `Jasper's iPhone`, `Jasper's Mac Studio`, future
`Jasper's iPad Pro` without re-editing.

Multiple patterns per account are fine and stored as a list. The
web form accepts comma-separated input:

```
Jasper's iPhone, Jasper's Mac Studio
```

If two accounts' patterns both match the same ClientName, the FIRST
account in the registry wins. Don't make ambiguous patterns
(`iPhone` would match every iPhone — bad). Anchor on first names.

**To check what shairport reports right now**, while AirPlay is
active:
```
dbus-send --system --print-reply \
    --dest=org.gnome.ShairportSync /org/gnome/ShairportSync \
    org.freedesktop.DBus.Properties.Get \
    string:org.gnome.ShairportSync.RemoteControl string:ClientName
```

**To check what was matched on the most recent voice command**, look
for `router:` lines in the daemon log:
```
journalctl -u jasper-voice -n 50 | grep -E "router:"
```
You'll see one of:
- `router: airplay sender 'Jasper's iPhone' matched account jasper`
- `router: airplay sender 'Alex's iPhone' matched no configured account`
- `router: account jasper reports is_playing=true`
- `router: falling back to default account jasper`

## What gets routed where

| Voice command | When AirPlay is active | When AirPlay isn't active |
|---|---|---|
| "Next song" / "Skip" / "Previous" / "Pause" / "Resume" | Matched account's Spotify Web API → that user's active device. iOS Spotify is the iPhone's app receiving the command; track changes; AirPlay stream content updates seamlessly. | Active account's Web API → its active device (Pi librespot or wherever) |
| "Play Kanye" / "Play [song]" / etc. | Matched account's account searches + start_playback on its active device | Active account searches + start_playback (target resolved by `spotify_routing`) |
| "What's playing?" | Matched account's `current_playback` (proper title/artist) | Active account's `current_playback` |
| Volume / mute | Source-agnostic — always CamillaDSP main fader. Doesn't touch Spotify. | Same |

If AirPlay is active but the sender doesn't match any account, the
router falls through to DACP (which is dead on iOS 17.4+ for any
sender, see `audit-pending-followups.md`) and ultimately tells the
user to set up their account at `https://jasper.local/spotify`.

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
shairport. The Web API path is the only working alternative for
controlling iOS Spotify from the receiver side.

The legacy DACP/MPRIS path is still wired up as a fallback for
sources that DO expose it (older iOS, Apple Music app on macOS pre-
14.4, some non-Apple AirPlay senders), so the speaker degrades
gracefully when somebody's casting from an unrecognised device —
it'll work for them as long as they're not on iOS 17.4+ Spotify.

## TLS / self-signed cert

Spotify's redirect URI rules require HTTPS for any non-loopback
host. The Pi serves `https://jasper.local` via a self-signed cert
generated at install time:

- Cert: `/etc/nginx/ssl/jasper.crt` (10-year validity, SANs for
  `jasper.local`, `jasper`, `127.0.0.1`)
- Key: `/etc/nginx/ssl/jasper.key`
- nginx site: `/etc/nginx/sites-enabled/jasper-https.conf` (port 443,
  HTTPS-only — moOde's HTTP UI on port 80 is unaffected)

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
- Add: fill the form, OAuth, done.
- Remove: click "Remove" next to the account. Wipes the cache file
  and removes the registry entry.
- Set default: click "Set default" — picked when no AirPlay is
  active and no other account is `is_playing`.

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
jasper/accounts.py          Registry / Account / smart-quote-aware matching
jasper/spotify_router.py    Router.resolve_airplay() / Router.active()
jasper/web/spotify_setup.py jasper-web HTTP service
jasper/tools/transport.py   AirPlay / Spotify / MPD / Bluetooth dispatch
jasper/tools/spotify.py     spotify_play / spotify_queue (router-aware)
deploy/nginx-jasper.conf            /spotify/ proxy block
deploy/nginx-jasper-https.conf      HTTPS site config
deploy/jasper-web.service           systemd unit for jasper-web
```
