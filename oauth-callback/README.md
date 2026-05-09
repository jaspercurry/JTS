# Spotify OAuth bounce page

Static HTML that forwards a Spotify authorization code from
`https://<github-user>.github.io/JTS/oauth-callback/` back to a local
speaker at `http://jts.local/spotify/oauth-callback`.

## Why it exists

Spotify's OAuth policy (post-November 2025) requires HTTPS for redirect
URIs unless they are loopback IP literals (`http://127.0.0.1:PORT`).
LAN IPs and `.local` mDNS names are rejected. A speaker on the home
network can't get a trusted HTTPS cert without dragging in
infrastructure (LE + DDNS, a custom CA the user installs, Tailscale,
etc.).

The bounce-page workaround: register an HTTPS URL on a host that
already has a real cert (GitHub Pages), and have it `window.location`
the user back to the speaker over plain HTTP. Cross-scheme navigation
(HTTPS → HTTP) works in modern Safari and Chrome — mixed-content
restrictions apply to subresource loads (scripts, images, fetch), not
top-level navigations.

The page does nothing except forward the query string. No analytics,
no third-party scripts, no data storage. The full source is the file
next to this README.

## Hosting

The canonical deployment for `jaspercurry/JTS` is GitHub Pages on the
`main` branch with `/oauth-callback` as the directory:

- Repo: `jaspercurry/JTS`
- Pages source: `main` branch, `/` (root)
- Public URL: `https://jaspercurry.github.io/JTS/oauth-callback/`

Spotify Developer App "Redirect URI" field gets that URL verbatim.

## Forks running on a different hostname

If you renamed the speaker from the default `jts.local`, change two
lines near the top of the `<script>` block in `index.html`:

```js
const SPEAKER_HOST = 'jts.local';            // ← your hostname
const SPEAKER_CALLBACK_PATH = '/spotify/oauth-callback';
```

Then either deploy this directory to your own GitHub Pages site (free,
no DNS to manage — `https://<your-handle>.github.io/JTS/oauth-callback/`)
or use the speaker's manual-paste OAuth mode and skip the bounce page
entirely. Both are documented in the wizard at
`http://<your-host>/spotify/`.

## Pinning

Once deployed, the published file should not be modified except to
change `SPEAKER_HOST` for forks. Trust depends on the page being
inspectable and obviously inert. Treat it like a checked-in artifact;
review any diff before deploying.
