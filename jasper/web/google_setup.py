"""Google OAuth setup wizard at /google/.

Multi-account, household-aware — mirrors `jasper.web.spotify_setup` but
simpler (no playlist management, no transport-routing logic; just paste
CLIENT_ID/SECRET, OAuth each member, manage defaults).

Three states, single index page renders the appropriate one:
  1. No CLIENT_ID/SECRET → guided setup wizard (paste creds, link to
     Google Cloud Console).
  2. Creds set, no accounts → redirect-URI instructions + add-account
     form.
  3. Creds set, accounts exist → management UI (list, default, remove,
     add more).

Routes (paths the app sees AFTER nginx strips the /google/ prefix):
  GET  /                    state-aware setup/management UI
  POST /setup-credentials   save CLIENT_ID/SECRET, restart jasper-voice
  POST /reset-credentials   clear creds (back to state 1)
  POST /start               begin OAuth for `name` (303 redirect to
                            Google's authorize endpoint)
  GET  /callback            OAuth callback — exchange code, write
                            token JSON, hit OIDC userinfo for email +
                            display name, redirect back to /
  POST /remove              remove an account by name
  POST /default             change the default account

Presentation: this page renders through the canonical design system
(`canonical_page` + `canonical_header` + `canonical_banner`,
`/assets/app.css`, page CSS at `/assets/google/google.css`). All page
behaviour — the setup-wizard progress tracker, the copy-to-clipboard
buttons, the Client-ID reveal, and the destructive-action confirms —
lives in the ES module `/assets/google/js/main.js`; there is no inline
`<script>` here. The forms stay server-rendered request/response, same
as before the restyle.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..google_creds import (
    GOOGLE_SCOPES,
    GoogleAccount,
    GoogleRegistry,
    default_token_path_for,
    save_token,
)
from ..log_event import log_event
from ._common import (
    begin_request,
    canonical_banner,
    canonical_header,
    canonical_page,
    csrf_field_html,
    delete_env_file,
    read_env_file,
    read_form,
    reject_csrf,
    restart_voice_daemon,
    send_html_response,
    send_see_other,
    guard_read_request,
    guard_mutating_request,
    write_env_file,
)

logger = logging.getLogger(__name__)

# Page-specific stylesheet served from /assets/google/google.css. App.css
# carries the shared tokens/primitives (.page, .info-card, .deflist,
# .badge, .field, .form-actions, .form-hint, .btn--*); google.css adds
# only what's unique to this wizard (the account list, the multi-step
# setup walkthrough, callouts, and the copy-row widget).
_PAGE_CSS_HREF = "/assets/google/google.css"


# Persisted CLIENT_ID/SECRET. Same shape as spotify_credentials.env so
# the systemd unit picks both up via `EnvironmentFile=`. Mode 0600 by
# write_env_file's default.
CREDS_FILE = "/var/lib/jasper/google_credentials.env"

# Google OAuth client IDs end in `.apps.googleusercontent.com`.
# Loose pattern — Google has changed the leading numeric chunk's
# length over time, and a strict regex would reject perfectly-valid
# IDs the next time the format shifts.
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+\.apps\.googleusercontent\.com$")

_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"

# Google rejects mDNS hostnames (`.local`) and bare LAN IPs as OAuth
# redirect URIs — only public TLDs or `localhost` are accepted, neither
# of which fits a Pi accessed from household phones. So we register a
# public GitHub Pages page that reads `?host=<hostname>` and bounces
# the browser back to the speaker over HTTP+mDNS. Source repo:
# https://github.com/jaspercurry/google-oauth-callback. Same pattern
# as the Spotify wizard (jaspercurry/spotify-oauth-callback).
_BOUNCE_REDIRECT_URI_BASE = (
    "https://jaspercurry.github.io/google-oauth-callback/"
)


def default_redirect_uri() -> str:
    """Default OAuth redirect URI for this speaker — the bounce page
    parameterised with our mDNS hostname."""
    hostname = os.environ.get("JASPER_HOSTNAME", "jts.local")
    return f"{_BOUNCE_REDIRECT_URI_BASE}?host={hostname}"


# ----------------------------------------------------------------------
# Persistence helpers — env file + token writes.
# ----------------------------------------------------------------------


def _read_creds_file(path: str = CREDS_FILE) -> dict[str, str]:
    return read_env_file(path)


def _write_creds_file(client_id: str, client_secret: str, *, path: str = CREDS_FILE) -> None:
    write_env_file(path, {
        "GOOGLE_CLIENT_ID": client_id,
        "GOOGLE_CLIENT_SECRET": client_secret,
    })


def _delete_creds_file(path: str = CREDS_FILE) -> None:
    delete_env_file(path)


def _restart_voice_daemon() -> None:
    restart_voice_daemon()


# ----------------------------------------------------------------------
# HTML rendering.
#
# Each of the three states (`_setup_wizard_html`, `_redirect_uri_page_html`,
# `_management_html`) builds its body string and wraps it via
# `_render_page`, which emits the canonical shell + header + flash banner
# + the page's ES module. Page behaviour is in /assets/google/js/main.js.
# ----------------------------------------------------------------------


def _render_page(title: str, body: str, *, csrf_token: str, status_msg: str = "") -> bytes:
    """Wrap a state's body in the canonical document shell.

    Mirrors the reference migration (`speaker_setup._index_html`): a
    `canonical_header` with the back-to-home button, the flash
    `canonical_banner`, the body inside `<main class="page">`, then the
    page's ES module loaded by `src`. The CSRF token rides in the
    `<meta name="jts-csrf">` tag (emitted by `canonical_page` when
    `csrf_token` is given) so the cached module can read it; the
    server-rendered forms still carry their own hidden `csrf_token`
    field via `csrf_field_html`."""
    page_body = f"""
{canonical_header(title)}
<main class="page">
  {canonical_banner(status_msg)}
{body}
</main>
<script type="module" src="/assets/google/js/main.js"></script>
"""
    return canonical_page(
        title, page_body, csrf_token=csrf_token, page_css_href=_PAGE_CSS_HREF,
    )


def _setup_wizard_html(redirect_uri: str, csrf_token: str = "", *, status_msg: str = "") -> bytes:
    """State 1: no CLIENT_ID/SECRET configured. Wraps `_setup_wizard_body`
    with the page chrome. The body itself is also rendered, in
    read-only mode, inside the state-3 management page as a
    "View setup guide" disclosure — see `_setup_wizard_body`."""
    body = _setup_wizard_body(redirect_uri, csrf_token, read_only=False)
    return _render_page(
        "Set up Google", body, csrf_token=csrf_token, status_msg=status_msg,
    )


def _setup_wizard_body(redirect_uri: str, csrf_token: str = "", *, read_only: bool = False) -> str:
    """The 4-step setup walkthrough body. In active mode (read_only=False)
    rendered as the State 1 wizard with localStorage progress tracking
    + the paste-creds form at step 4. In read-only mode (rendered
    inside the state-3 management page) it's a static reference: no
    Reset Progress button, no "I've done this →" buttons, no script,
    no paste-creds form, and the redirect URI in step 4 displays as
    inline text rather than a copy widget (the management page has
    its own redirect URI section with a copy button).

    The wizard's interactive behaviour (progress tracking, "mark done",
    reset-progress confirm, copy-to-clipboard) is delegated to
    /assets/google/js/main.js — this function emits only markup with
    `data-*` hooks the module binds to.

    The four steps mirror Google's actual UI as of May 2026:
      1. Create a Cloud project.
      2. Configure the Google Auth Platform — a single linear setup
         wizard launched from "Get started" on the Branding tab
         (App Information → Audience → Contact Information → Finish),
         followed by clicking Publish App on the Audience tab.
      3. Enable the Calendar and Gmail APIs.
      4. Create an OAuth client and paste creds here. The registered
         redirect URI is a GitHub Pages bounce page because Google
         rejects mDNS hostnames — see `default_redirect_uri` above.
    """
    redirect_safe = html.escape(redirect_uri)
    if read_only:
        progress_button = ""
        intro = (
            '<p class="form-hint">Reference copy of the 4-step setup. Credentials are already saved on this speaker — see <strong>Connection details</strong> above for which Cloud project the wizard pointed at. Use these steps to re-verify any decision, or to share the setup story with someone building their own JTS.</p>'
        )
        mark_done = ""
        redirect_widget = f'<p style="margin-top:0.3em"><code>{redirect_safe}</code></p>'
        creds_form = ""
    else:
        # The reset-progress confirm and click handler live in the ES
        # module; this button just carries the data-action hook.
        progress_button = """<button type="button" class="btn btn--ghost wizard-progress-reset"
        data-action="reset-progress">Reset progress</button>"""
        intro = '<p class="form-hint">Connect this speaker to Google Calendar + Gmail. Takes about 5 minutes the first time. Each step has a link to the right Google page — open them in new tabs and click <strong>I\'ve done this →</strong> when each is finished.</p>'
        mark_done = '<button class="btn btn--primary mark-done" type="button">I\'ve done this →</button>'
        # data-copy points the delegated copy handler at the input by id.
        redirect_widget = f"""<div class="copy-row" style="margin-top:0.3em">
              <input id="step4-redirect" type="text" readonly value="{redirect_safe}" data-select-on-click>
              <button type="button" class="btn btn--default" data-copy="step4-redirect" data-copy-feedback="step4-redirect-fb">Copy</button>
              <span id="step4-redirect-fb" class="copy-feedback">Copied!</span>
            </div>"""
        creds_form = f"""<div class="creds-form-wrap">
          <form method="post" action="setup-credentials">
            {csrf_field_html(csrf_token) if csrf_token else ''}
            <div class="field">
              <label for="client_id">Client ID</label>
              <input id="client_id" name="client_id" type="text" required
                     autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"
                     placeholder="123456789012-abc….apps.googleusercontent.com">
            </div>
            <div class="field">
              <label for="client_secret">Client Secret</label>
              <input id="client_secret" name="client_secret" type="password" required
                     autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false"
                     placeholder="GOCSPX-…">
              <p class="form-hint">Stored on this speaker only at <code>/var/lib/jasper/google_credentials.env</code>. Never sent anywhere except Google.</p>
            </div>
            <div class="form-actions">
              <button type="submit" class="btn btn--primary">Save credentials →</button>
            </div>
          </form>
        </div>"""
    return f"""
{progress_button}
{intro}

<ol class="wizard-steps">

  <!-- ===== Step 1: Create or pick a Cloud project ===== -->
  <li class="wizard-step" data-step="1">
    <details>
      <summary>
        <span class="step-num"><span>1</span></span>
        <span class="step-title">Create a Google Cloud project</span>
        <span class="step-status">~30 seconds</span>
      </summary>
      <div class="step-body">
        <p>The OAuth client lives inside a Google Cloud project. If you already have one, you can reuse it — otherwise:</p>
        <ol>
          <li>Open <a href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noopener">the New Project page ↗</a> in a new tab.</li>
          <li><strong>Project name:</strong> anything you'll recognise — e.g. "JTS Speaker". The <strong>Project ID</strong> below auto-fills; leave it. <strong>Parent resource</strong> stays as "No organization" (personal Gmail accounts don't have one).</li>
          <li>Click <strong>CREATE</strong>. Provisioning takes 10–30 seconds; the page auto-switches into the new project when it's ready.</li>
        </ol>
        <div class="callout">
          <strong>Multi-account heads-up:</strong> if you're signed into more than one Google account in this browser, the page uses whatever account loaded first. Mismatch is the #1 setup failure here. Open the link above in an incognito window and sign in with just the account you want to own this project.
        </div>
        {mark_done}
      </div>
    </details>
  </li>

  <!-- ===== Step 2: Configure the Google Auth Platform ===== -->
  <li class="wizard-step" data-step="2">
    <details>
      <summary>
        <span class="step-num"><span>2</span></span>
        <span class="step-title">Configure the Google Auth Platform</span>
        <span class="step-status">~3 minutes</span>
      </summary>
      <div class="step-body">
        <p>This is what each household member will see when they grant the speaker access. Google runs a single short setup wizard for fresh projects — you do it once.</p>

        <ol>
          <li>Open the <a href="https://console.cloud.google.com/auth/branding" target="_blank" rel="noopener">Google Auth Platform ↗</a>. You'll see a "Google Auth Platform not configured yet" placeholder — click <strong>GET STARTED</strong>.</li>
          <li><strong>App Information</strong>
            <ul>
              <li><strong>App name:</strong> "JTS Speaker". Avoid generic names like "test" — Google's heuristics flag them.</li>
              <li><strong>User support email:</strong> pick your own Gmail from the dropdown.</li>
            </ul>
            Click <strong>NEXT</strong>.
          </li>
          <li><strong>Audience:</strong> pick <strong>External</strong>. (Internal is selectable but requires a Google Workspace organisation — on a personal Gmail it'll fail downstream.) Click <strong>NEXT</strong>.</li>
          <li><strong>Contact Information → Email addresses:</strong> same Gmail again (Google uses this for project-change notices). Click <strong>NEXT</strong>.</li>
          <li><strong>Finish:</strong> tick <strong>"I agree to the Google API Services: User Data Policy"</strong>. Click <strong>CONTINUE</strong>, then <strong>CREATE</strong>.</li>
        </ol>

        <div class="callout">
          <strong>Publish the app — skips the 7-day refresh-token expiry.</strong>
          Open the <a href="https://console.cloud.google.com/auth/audience" target="_blank" rel="noopener">Audience tab ↗</a>. Under <strong>Publishing status</strong> (will say "Testing"), click <strong>PUBLISH APP</strong>. The "Push to production?" modal mentions submitting for verification — that doesn't apply under Google's <a href="https://support.google.com/cloud/answer/13464323" target="_blank" rel="noopener">personal-use exception ↗</a> (fewer than 100 users). Click <strong>CONFIRM</strong>. After publish, refresh tokens stop expiring; the FIRST time anyone signs in they'll see a "Google hasn't verified this app" warning — click <strong>Advanced → Go to JTS Speaker (unsafe)</strong> once per household member; afterwards it's invisible.
        </div>

        <p class="form-hint"><strong>Don't visit the "Data Access" tab.</strong> Scopes are requested at consent regardless of what's there, and that tab is for submitting your app for verification — Google won't let you add Gmail-readonly without a written justification and demo video. Not what we want.</p>

        {mark_done}
      </div>
    </details>
  </li>

  <!-- ===== Step 3: Enable Calendar + Gmail APIs ===== -->
  <li class="wizard-step" data-step="3">
    <details>
      <summary>
        <span class="step-num"><span>3</span></span>
        <span class="step-title">Enable the Calendar and Gmail APIs</span>
        <span class="step-status">~30 seconds</span>
      </summary>
      <div class="step-body">
        <p>Each API is a separate "Enable" click. Both are free at personal volume — quotas are 1M+ requests per day, no billing account needed.</p>
        <ol>
          <li>Open <a href="https://console.cloud.google.com/apis/library/calendar-json.googleapis.com" target="_blank" rel="noopener">the Calendar API page ↗</a>. Confirm the project picker in the top bar shows the project from Step 1, then click the blue <strong>ENABLE</strong> button. Takes ~10 seconds.</li>
          <li>Open <a href="https://console.cloud.google.com/apis/library/gmail.googleapis.com" target="_blank" rel="noopener">the Gmail API page ↗</a>. Same: confirm project, click <strong>ENABLE</strong>.</li>
        </ol>
        <p class="form-hint">If you navigated to either page without a project context, a "Select a project" modal appears first — pick the one from Step 1.</p>
        {mark_done}
      </div>
    </details>
  </li>

  <!-- ===== Step 4: Create OAuth client + paste creds ===== -->
  <li class="wizard-step" data-step="4">
    <details>
      <summary>
        <span class="step-num"><span>4</span></span>
        <span class="step-title">Create the OAuth client and paste it here</span>
        <span class="step-status">~2 minutes</span>
      </summary>
      <div class="step-body">
        <ol>
          <li>Open <a href="https://console.cloud.google.com/auth/clients" target="_blank" rel="noopener">the Clients page ↗</a>. Click <strong>+ CREATE CLIENT</strong> at the top.</li>
          <li><strong>Application type:</strong> select <strong>Web application</strong> from the dropdown — the form expands when you pick this.</li>
          <li><strong>Name:</strong> anything cosmetic, e.g. "JTS Speaker Web Client".</li>
          <li>Leave <strong>Authorized JavaScript origins</strong> blank.</li>
          <li><strong>Authorized redirect URIs:</strong> click <strong>+ ADD URI</strong> and paste this URL:
            {redirect_widget}
            <div class="callout">
              <strong>Why a github.io URL?</strong> Google's OAuth client requires redirect URIs to use a public TLD (<code>.com</code>, <code>.io</code>, …) or <code>localhost</code> — it rejects mDNS names like <code>jts.local</code> and bare LAN IPs outright. The URL above points at <a href="https://github.com/jaspercurry/google-oauth-callback" target="_blank" rel="noopener">a tiny static page</a> that reads <code>?host=jts.local</code> and bounces the browser back to this speaker. No data passes through it. Same trick the Spotify wizard uses.
            </div>
          </li>
          <li>Click <strong>CREATE</strong> at the bottom of the form.</li>
          <li>The success modal shows your <strong>Client ID</strong> and <strong>Client Secret</strong>.
            <div class="callout">
              <strong>The Client Secret is only shown once.</strong> Click <strong>DOWNLOAD JSON</strong> in the modal as a backup before you dismiss it — the Clients page will only show the last 4 characters of the secret afterwards, and you'd have to reset (which invalidates the old secret) if you lose it.
            </div>
          </li>
          <li>Paste the Client ID and Client Secret below. Saving here finishes the setup; the page will move on to linking the first household member.</li>
        </ol>

        {creds_form}
      </div>
    </details>
  </li>
</ol>
"""


def _redirect_uri_section_html(redirect_uri: str) -> str:
    """The 'add this redirect URL to your OAuth client' block.
    Used as a re-reference in state 2 (when sign-in fails with
    redirect_uri_mismatch) and state 3 (collapsed under "OAuth
    client settings"). The setup wizard's step 4 includes this URL
    inline so the user adds it during initial client creation;
    this section exists for the cases where they need to re-add
    or fix it after the fact. The Copy button is wired by the
    delegated copy handler in /assets/google/js/main.js."""
    redirect_safe = html.escape(redirect_uri)
    return f"""
<h3>The redirect URL</h3>
<p>Your OAuth client needs this URL in its <strong>Authorized redirect URIs</strong> list. The setup wizard's step 4 included this when you created the client; if you skipped it or recreated the client, add it now.</p>

<div class="copy-row">
  <input id="redirect-uri" type="text" readonly value="{redirect_safe}" data-select-on-click>
  <button type="button" class="btn btn--default" data-copy="redirect-uri" data-copy-feedback="copy-feedback">Copy</button>
  <span id="copy-feedback" class="copy-feedback">Copied!</span>
</div>

<p class="form-hint" style="margin-top:0.6em">It's a github.io URL because Google rejects <code>.local</code> mDNS names and bare LAN IPs. The page at that URL is a tiny static bouncer (<a href="https://github.com/jaspercurry/google-oauth-callback" target="_blank" rel="noopener">source</a>) that redirects the browser back here. No data passes through it.</p>

<ol class="steps">
  <li>Open <a href="https://console.cloud.google.com/auth/clients" target="_blank" rel="noopener">the Clients page ↗</a> and click your OAuth 2.0 Client ID. (If you bookmarked the old <code>/apis/credentials</code> URL, it still works — Google redirects it here.)</li>
  <li>You'll land on a page titled <strong>Client ID for Web application</strong>. Scroll to <strong>Authorized redirect URIs</strong>.</li>
  <li>Click <strong>+ ADD URI</strong>, paste the URL above, then <strong>SAVE</strong> at the bottom.</li>
</ol>

<p class="form-hint">
  <strong>Heads up — propagation delay:</strong> Google says redirect-URI changes can take "5 minutes to a few hours" to take effect, though usually it's well under a minute. If sign-in fails with <code>redirect_uri_mismatch</code>, wait 60 seconds and retry.
</p>
"""


def _project_number_from_client_id(client_id: str) -> str | None:
    """Google OAuth Client IDs look like
    `123456789012-randomstring.apps.googleusercontent.com` — the leading
    numeric chunk before the first hyphen is the Cloud project number,
    which the Cloud Console accepts as a `?project=` query value (same
    URL surface as project IDs like `jts-speaker-496013`). Return it
    so the wizard can deep-link into the right project's Branding /
    Audience / Clients tabs without remembering URLs. Returns None
    for malformed Client IDs."""
    if not client_id or "-" not in client_id:
        return None
    head = client_id.split("-", 1)[0]
    return head if head.isdigit() else None


def _connection_details_html(client_id: str) -> str:
    """Read-only inspection panel for a configured connection: scopes,
    Client ID (masked + reveal-on-click), and deep-links into the
    Cloud Console for this specific project. Rendered in state 2 and
    state 3 — anywhere the wizard knows there are credentials to
    inspect. Distinct from the existing 'OAuth client settings'
    details, which is for destructive ops (reset credentials).

    The "Show full" button reveals the Client ID without leaking it
    into inline JS: the full value rides in a `data-full` attribute
    (escaped for an HTML attribute) that the delegated reveal handler
    in /assets/google/js/main.js reads and writes into the display."""
    masked = (
        html.escape(client_id[:8] + "…" + client_id[-30:])
        if len(client_id) > 38 else "configured"
    )
    full_attr = html.escape(client_id, quote=True)
    project_number = _project_number_from_client_id(client_id)
    if project_number:
        n = urllib.parse.quote(project_number)
        project_links_html = f"""
<ul>
  <li><a href="https://console.cloud.google.com/auth/branding?project={n}" target="_blank" rel="noopener">Branding tab ↗</a> — app name, support email, publishing-related branding</li>
  <li><a href="https://console.cloud.google.com/auth/audience?project={n}" target="_blank" rel="noopener">Audience tab ↗</a> — confirm <em>In production</em>, see your 100-user cap, revert to Testing if you need to</li>
  <li><a href="https://console.cloud.google.com/auth/clients?project={n}" target="_blank" rel="noopener">OAuth Clients ↗</a> — redirect URIs, regenerate the client secret if it leaks</li>
  <li><a href="https://console.cloud.google.com/apis/dashboard?project={n}" target="_blank" rel="noopener">Enabled APIs ↗</a> — confirm Calendar + Gmail are still enabled, see request volume</li>
</ul>"""
    else:
        project_links_html = (
            '<p class="form-hint">Couldn\'t auto-detect the project from the Client ID. '
            'Open <a href="https://console.cloud.google.com/" target="_blank" rel="noopener">'
            'console.cloud.google.com ↗</a> and pick the project from the top-bar switcher.</p>'
        )
    return f"""
<details class="disclosure">
  <summary>Connection details (scopes, project, OAuth client)</summary>
  <div class="disclosure-body">
    <h3>What this app reads</h3>
    <ul>
      <li>Google Calendar — <strong>read-only</strong></li>
      <li>Gmail — <strong>read-only</strong>; the speaker cannot send, modify, or delete email</li>
      <li>Profile + email — used once at link time to label the linked account</li>
    </ul>
    <p class="form-hint">When you ask about mail or calendar, matching message or event content is sent to the household's configured voice AI provider so it can answer.</p>
    <p class="form-hint">To revoke access from Google's side at any time, visit <a href="https://myaccount.google.com/permissions" target="_blank" rel="noopener">myaccount.google.com/permissions</a> on each linked account and remove the app (its name is whatever you set on the Branding tab — "JTS Speaker" by default). Removing here only deletes the speaker's local refresh token; revoking at Google invalidates it everywhere.</p>

    <h3>OAuth client</h3>
    <p>Client ID: <code id="client-id-display">{masked}</code>
       <button type="button" id="reveal-client-id" class="btn btn--default"
               data-action="reveal-client-id" data-full="{full_attr}"
               style="padding:0.2em 0.7em">Show full</button>
    </p>
    <p>Client Secret: never displayed. To rotate it, regenerate in the Cloud Console (link below) and re-paste it via Reset credentials below.</p>

    <h3>Cloud Console — audit this project</h3>
    {project_links_html}
  </div>
</details>
"""


def _add_account_form_html(csrf_token: str = "") -> str:
    csrf = csrf_field_html(csrf_token) if csrf_token else ""
    return f"""
<h2 class="section__title">Add a Google account</h2>
<form method="post" action="start">
  {csrf}
  <div class="field">
    <label for="name">Your name (label only)</label>
    <input id="name" name="name" type="text" required pattern="[a-zA-Z0-9_-]+"
           placeholder="brittany" autocapitalize="off" autocorrect="off">
    <p class="form-hint">Lowercase, no spaces. Used by voice ('what's on Brittany's calendar') and shown in the list below.</p>
  </div>
  <div class="form-actions">
    <button type="submit" class="btn btn--primary">Continue with Google →</button>
  </div>
  <p class="form-hint">You'll be sent to Google to sign in once. The refresh token stays on this speaker — read access only (Calendar + Gmail).</p>
</form>
"""


def _redirect_uri_page_html(
    redirect_uri: str, client_id: str, csrf_token: str = "",
    *, status_msg: str = "",
) -> bytes:
    """State 2: credentials saved, no accounts linked yet. The user
    already added the redirect URI during the wizard's step 4, so the
    primary action here is "link a household member's account". The
    redirect URI section lives in a collapsible <details> as a
    fallback for the redirect_uri_mismatch case.

    The destructive "Reset Google credentials" form carries a
    `data-confirm` message that the delegated submit-guard in
    /assets/google/js/main.js confirms (via the shared <dialog>)
    before letting the native POST proceed."""
    masked = (
        client_id[:8] + "…" + client_id[-30:]
        if len(client_id) > 38 else "configured"
    )
    csrf = csrf_field_html(csrf_token) if csrf_token else ""
    body = f"""
<p class="form-hint">Credentials saved (Client ID: <code>{html.escape(masked)}</code>). One step left — link your first Google account.</p>

{_add_account_form_html(csrf_token)}

{_connection_details_html(client_id)}

<details class="disclosure">
  <summary>OAuth client troubleshooting (redirect URI, reset credentials)</summary>
  <div class="disclosure-body">
    <p>If sign-in fails with <code>redirect_uri_mismatch</code>, your OAuth client doesn't have the redirect URL in its allow-list yet — add it here.</p>
    {_redirect_uri_section_html(redirect_uri)}
    <form method="post" action="reset-credentials" style="margin-top:2em"
          data-confirm="Clear the saved Client ID and Secret? You'll need to paste them again." data-confirm-danger>
      {csrf}
      <button type="submit" class="btn btn--danger">Reset Google credentials</button>
    </form>
  </div>
</details>
"""
    return _render_page(
        "Link a Google account", body, csrf_token=csrf_token, status_msg=status_msg,
    )


def _account_li_html(account: GoogleAccount, *, is_default: bool, csrf_token: str = "") -> str:
    """One linked-account row. The account name is untrusted-ish (user
    label, validated to `[a-zA-Z0-9_-]+` on save) and the email comes
    from Google; both are HTML-escaped before interpolation. The
    remove-confirm message rides in `data-confirm` (escaped for an
    attribute) — never inline JS — so the delegated submit-guard can
    confirm before the native POST."""
    name = html.escape(account.name)
    email = html.escape(account.email or "(unknown email)")
    badge = '<span class="badge" style="--tone: var(--status-ok)">default</span>' if is_default else ""
    set_default = (
        '<button class="btn btn--default" type="submit" disabled>Default</button>'
        if is_default
        else '<button class="btn btn--default" type="submit">Set default</button>'
    )
    csrf = csrf_field_html(csrf_token) if csrf_token else ""
    remove_confirm = html.escape(
        f"Remove {account.name}? The refresh token will be deleted from this speaker.",
        quote=True,
    )
    return f"""
<li>
  <span class="name">{name}</span>
  <span class="email">{email}</span>
  {badge}
  <span class="actions">
    <form method="post" action="default">
      {csrf}
      <input type="hidden" name="name" value="{name}">
      {set_default}
    </form>
    <form method="post" action="remove"
          data-confirm="{remove_confirm}" data-confirm-danger>
      {csrf}
      <input type="hidden" name="name" value="{name}">
      <button class="btn btn--danger" type="submit">Remove</button>
    </form>
  </span>
</li>"""


def _management_html(
    registry: GoogleRegistry, redirect_uri: str, client_id: str,
    csrf_token: str = "", *, status_msg: str = "",
) -> bytes:
    items = [
        _account_li_html(
            a, is_default=(a.name == registry.default_name),
            csrf_token=csrf_token,
        )
        for a in registry.accounts
    ]
    csrf = csrf_field_html(csrf_token) if csrf_token else ""
    # Clarification copy above the Add account form — only meaningful
    # when at least one account is already linked (state 3). State 2
    # uses _redirect_uri_page_html, which has its own intro framing.
    add_account_clarification = (
        '<p class="form-hint" style="margin-top:1.6em"><strong>Adding another '
        'household member?</strong> They just sign in with their own '
        'Google account below — no Google Cloud setup to redo. The '
        "speaker's OAuth client serves everyone (up to the 100-user "
        'cap on this Cloud project).</p>'
    )
    body = f"""
<p class="form-hint">Each household member links their Google account once. The voice loop reads Calendar + Gmail data per-account on demand — say "what's on Brittany's calendar" or "any new emails for Jasper" to disambiguate; bare requests use the default account.</p>

<h2 class="section__title">Linked accounts</h2>
<ul class="accounts">
{''.join(items)}
</ul>

{add_account_clarification}
{_add_account_form_html(csrf_token)}

{_connection_details_html(client_id)}

<details class="disclosure">
  <summary>View setup guide (re-read the original 4-step instructions)</summary>
  <div class="disclosure-body">
    {_setup_wizard_body(redirect_uri, csrf_token, read_only=True)}
  </div>
</details>

<details class="disclosure">
  <summary>OAuth client settings (redirect URI, reset credentials)</summary>
  <div class="disclosure-body">
    {_redirect_uri_section_html(redirect_uri)}
    <form method="post" action="reset-credentials" style="margin-top:2em"
          data-confirm="Clear the saved Client ID and Secret? Existing OAuthed accounts will keep working until their refresh tokens are revoked." data-confirm-danger>
      {csrf}
      <button type="submit" class="btn btn--danger">Reset Google credentials</button>
    </form>
  </div>
</details>
"""
    return _render_page(
        "Google accounts", body, csrf_token=csrf_token, status_msg=status_msg,
    )


# ----------------------------------------------------------------------
# OAuth helpers.
# ----------------------------------------------------------------------


def _build_flow(cfg: dict[str, Any], *, state: str | None = None):
    """Construct a google_auth_oauthlib Flow with our Web-application
    client config. Imported lazily so the module is importable in
    unit tests without the google-auth-oauthlib wheel installed."""
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "auth_uri": _AUTH_URI,
                "token_uri": _TOKEN_URI,
                "redirect_uris": [cfg["redirect_uri"]],
            }
        },
        scopes=GOOGLE_SCOPES,
        state=state,
    )
    flow.redirect_uri = cfg["redirect_uri"]
    return flow


def _fetch_userinfo(access_token: str) -> dict[str, Any]:
    """Hit Google's OIDC userinfo endpoint with the freshly-issued
    access token. Returns ``{}`` on any error — the wizard falls back
    to "(unknown email)" rather than failing the whole link."""
    if not access_token:
        return {}
    req = urllib.request.Request(
        _USERINFO_URI,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        return data if isinstance(data, dict) else {}
    except Exception as e:  # noqa: BLE001
        logger.warning("userinfo fetch failed: %s", e)
        return {}


# ----------------------------------------------------------------------
# HTTP handler.
# ----------------------------------------------------------------------


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    """Returns a request handler class closed over the config dict.

    `cfg` is mutated when the user submits credentials so the running
    process can serve the OAuth flow without needing a restart."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def _redirect(self, location: str) -> None:
            # Compat shim: hoists ?msg=… into a flash cookie so the
            # browser lands on a clean URL without query pollution.
            # Same pattern as spotify_setup; lets the ~25 existing
            # `?msg=...` redirects work without per-callsite edits.
            parsed = urllib.parse.urlparse(location)
            if parsed.query:
                qs = urllib.parse.parse_qs(
                    parsed.query, keep_blank_values=True,
                )
                msgs = qs.pop("msg", None)
                flash = (msgs[0] if msgs else "").strip()
                if flash:
                    clean_query = urllib.parse.urlencode(qs, doseq=True)
                    clean = urllib.parse.urlunparse(
                        parsed._replace(query=clean_query),
                    )
                    send_see_other(self, clean, flash=flash)
                    return
            send_see_other(self, location)

        def _send_html(self, body: bytes, *, status: int = 200) -> None:
            send_html_response(self, body, status=status)

        # --- routes ---

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(url.query)

            if path == "/":
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                self._render_index(
                    ctx["csrf_token"], status_msg=ctx["flash"],
                )
                return

            if path == "/callback":
                if not guard_read_request(self, allow_cross_site_navigation=True):
                    return
                code = qs.get("code", [""])[0]
                state = qs.get("state", [""])[0]  # account name
                err = qs.get("error", [""])[0]
                if err:
                    self._redirect(
                        f"./?msg=Google+returned+error:+{urllib.parse.quote(err)}"
                    )
                    return
                if not (code and state):
                    self._redirect("./?msg=Missing+code+or+state+from+Google")
                    return
                if not (cfg["client_id"] and cfg["client_secret"]):
                    self._redirect(
                        "./?msg=Credentials+were+cleared+mid-flow.+Start+over."
                    )
                    return
                try:
                    self._exchange_code(state, code)
                except Exception as e:  # noqa: BLE001
                    logger.exception("oauth exchange failed")
                    self._redirect(
                        f"./?msg=Auth+exchange+failed:+{urllib.parse.quote(str(e))}"
                    )
                    return
                _restart_voice_daemon()
                # No account name / token in the line — personal data + secret.
                log_event(logger, "google.link", client=self.address_string())
                self._redirect(
                    f"./?msg=Linked+{urllib.parse.quote(state)}+successfully"
                )
                return

            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            CSRF_POST_ROUTES = (
                "/setup-credentials", "/reset-credentials",
                "/start", "/remove", "/default",
            )
            if path not in CSRF_POST_ROUTES:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            form = read_form(self)
            if not guard_mutating_request(self, form):
                reject_csrf(self)
                return

            if path == "/setup-credentials":
                self._handle_setup_credentials(form)
                return
            if path == "/reset-credentials":
                self._handle_reset_credentials()
                return
            if path == "/start":
                self._handle_start(form)
                return
            if path == "/remove":
                self._handle_remove(form)
                return
            if path == "/default":
                self._handle_default(form)
                return

        # --- route bodies ---

        def _render_index(self, csrf_token: str = "", *, status_msg: str = "") -> None:
            has_creds = bool(cfg["client_id"] and cfg["client_secret"])
            if not has_creds:
                self._send_html(_setup_wizard_html(
                    cfg["redirect_uri"], csrf_token, status_msg=status_msg,
                ))
                return
            registry = GoogleRegistry.load(cfg["registry_path"])
            if not registry.accounts:
                self._send_html(_redirect_uri_page_html(
                    cfg["redirect_uri"], cfg["client_id"], csrf_token,
                    status_msg=status_msg,
                ))
                return
            self._send_html(_management_html(
                registry, cfg["redirect_uri"], cfg["client_id"], csrf_token,
                status_msg=status_msg,
            ))

        def _handle_setup_credentials(self, form: dict[str, str]) -> None:
            client_id = form.get("client_id", "").strip()
            client_secret = form.get("client_secret", "").strip()
            if not (client_id and client_secret):
                self._redirect(
                    "./?msg=Both+Client+ID+and+Client+Secret+are+required."
                )
                return
            if not _CLIENT_ID_RE.fullmatch(client_id):
                self._redirect(
                    "./?msg=Client+ID+should+end+in+.apps.googleusercontent.com"
                    "+-+double-check+the+value+from+Google+Cloud+Console."
                )
                return
            try:
                _write_creds_file(client_id, client_secret)
            except OSError as e:
                logger.exception("could not write credentials file")
                self._redirect(
                    f"./?msg=Could+not+save+credentials:+{urllib.parse.quote(str(e))}"
                )
                return
            cfg["client_id"] = client_id
            cfg["client_secret"] = client_secret
            _restart_voice_daemon()
            # Action + requester only — never the client_id/secret.
            log_event(logger, "google.credentials", client=self.address_string())
            self._redirect(
                "./?msg=Credentials+saved.+Now+add+the+redirect+URL+to+your+"
                "OAuth+client."
            )

        def _handle_reset_credentials(self) -> None:
            _delete_creds_file()
            cfg["client_id"] = ""
            cfg["client_secret"] = ""
            _restart_voice_daemon()
            log_event(logger, "google.reset", client=self.address_string())
            self._redirect("./?msg=Credentials+cleared.")

        def _handle_start(self, form: dict[str, str]) -> None:
            if not (cfg["client_id"] and cfg["client_secret"]):
                self._redirect("./?msg=Set+up+Google+credentials+first.")
                return
            name = form.get("name", "").strip()
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
                self._redirect(
                    "./?msg=Invalid+name+(letters/digits/_-+only)"
                )
                return
            registry = GoogleRegistry.load(cfg["registry_path"])
            token_path = default_token_path_for(name)
            registry.add_or_update(GoogleAccount(name=name, token_path=token_path))
            registry.save()
            try:
                flow = _build_flow(cfg, state=name)
                # `prompt='consent'` forces Google to issue a refresh
                # token even if the user has already consented — the
                # one we get on first consent is the only one we'll
                # ever see otherwise, and a re-link would silently
                # fail.
                auth_url, _state = flow.authorization_url(
                    access_type="offline",
                    prompt="consent",
                    include_granted_scopes="true",
                )
            except Exception as e:  # noqa: BLE001
                logger.exception("authorize-url build failed")
                self._redirect(
                    f"./?msg=Could+not+start+OAuth:+{urllib.parse.quote(str(e))}"
                )
                return
            # google-auth-oauthlib defaults autogenerate_code_verifier=True,
            # so authorization_url() generated a PKCE verifier and stored
            # it on this Flow instance. The /callback handler will build a
            # fresh Flow (no shared state across requests), so the verifier
            # has to ride along in `cfg`, keyed by the account name (which
            # is also the OAuth `state` param Google round-trips back).
            cfg.setdefault("pending_verifiers", {})[name] = flow.code_verifier
            self._redirect(auth_url)

        def _handle_remove(self, form: dict[str, str]) -> None:
            name = form.get("name", "")
            registry = GoogleRegistry.load(cfg["registry_path"])
            token_path = ""
            a = registry.get(name)
            if a is not None:
                token_path = a.token_path
            if registry.remove(name):
                registry.save()
                if token_path and os.path.isfile(token_path):
                    try:
                        os.unlink(token_path)
                    except OSError:
                        pass
                _restart_voice_daemon()
                log_event(logger, "google.unlink", client=self.address_string())
                self._redirect(f"./?msg=Removed+{urllib.parse.quote(name)}")
            else:
                self._redirect("./?msg=Account+not+found")

        def _handle_default(self, form: dict[str, str]) -> None:
            name = form.get("name", "")
            registry = GoogleRegistry.load(cfg["registry_path"])
            if registry.get(name) is not None:
                registry.default_name = name
                registry.save()
                # Account-identity config — symmetric with spotify.default.
                # (No restart here: Google's default is read lazily, but the
                # config change is still worth the audit line.)
                log_event(logger, "google.default", client=self.address_string())
                self._redirect(
                    f"./?msg=Default+set+to+{urllib.parse.quote(name)}"
                )
            else:
                self._redirect("./?msg=Account+not+found")

        def _exchange_code(self, account_name: str, code: str) -> None:
            registry = GoogleRegistry.load(cfg["registry_path"])
            account = registry.get(account_name)
            if account is None:
                raise RuntimeError(f"unknown account: {account_name}")
            flow = _build_flow(cfg, state=account_name)
            # Restore the PKCE verifier that /start stashed in cfg. Pop
            # so a redo (user clicks Continue again) doesn't reuse a
            # stale verifier — the next /start will create a new one.
            pending = cfg.get("pending_verifiers", {})
            verifier = pending.pop(account_name, None)
            if verifier is not None:
                flow.code_verifier = verifier
            flow.fetch_token(code=code)
            creds = flow.credentials
            if not creds.refresh_token:
                # `prompt='consent'` should always produce one. If we
                # get here Google probably rate-limited the consent
                # screen for this user — the user can retry.
                raise RuntimeError(
                    "Google did not return a refresh token. Try again "
                    "in a moment, or revoke this app at "
                    "myaccount.google.com/permissions and re-link."
                )
            save_token(
                account.token_path,
                refresh_token=creds.refresh_token,
                scopes=list(creds.scopes or GOOGLE_SCOPES),
                token_uri=creds.token_uri or _TOKEN_URI,
            )
            # Best-effort identity lookup so the management page can
            # show "jasper@gmail.com" next to the label. Failure here
            # is non-fatal — the account still works for tools.
            info = _fetch_userinfo(creds.token or "")
            email = (info.get("email") or "").strip()
            display = (info.get("name") or "").strip()
            if email or display:
                account.email = email
                account.display_name = display
                registry.save()

    return Handler


# ----------------------------------------------------------------------
# Entry points.
# ----------------------------------------------------------------------


def make_server(
    target,
    *,
    registry_path: str = "/var/lib/jasper/google/accounts.json",
    redirect_uri: str | None = None,
) -> ThreadingHTTPServer:
    """Build a configured server. `target` is socket/tuple/int per
    _systemd.make_http_server's contract."""
    from . import _systemd
    creds_from_file = _read_creds_file()
    client_id = (
        os.environ.get("GOOGLE_CLIENT_ID", "")
        or creds_from_file.get("GOOGLE_CLIENT_ID", "")
    )
    client_secret = (
        os.environ.get("GOOGLE_CLIENT_SECRET", "")
        or creds_from_file.get("GOOGLE_CLIENT_SECRET", "")
    )
    cfg = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri or default_redirect_uri(),
        "registry_path": registry_path,
    }
    return _systemd.make_http_server(target, _make_handler(cfg))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-google-web",
        description=(
            "Google Calendar + Gmail OAuth setup web server "
            "for the Jasper smart speaker"
        ),
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("JASPER_GOOGLE_WEB_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("JASPER_GOOGLE_WEB_PORT", "8768")),
    )
    parser.add_argument(
        "--registry",
        default=os.environ.get(
            "JASPER_GOOGLE_ACCOUNTS_PATH",
            "/var/lib/jasper/google/accounts.json",
        ),
    )
    parser.add_argument(
        "--redirect-uri",
        default=os.environ.get(
            "GOOGLE_REDIRECT_URI",
            default_redirect_uri(),
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    server = make_server(
        (args.host, args.port),
        registry_path=args.registry,
        redirect_uri=args.redirect_uri,
    )
    logger.info(
        "jasper-google-web listening on http://%s:%d (state=%s, redirect_uri=%s)",
        args.host, args.port, args.registry, args.redirect_uri,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
