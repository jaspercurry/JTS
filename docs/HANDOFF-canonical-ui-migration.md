# Handoff: canonical design-system migration (all wizards)

> **Status: historical.** Snapshot from 2026-05-31 when the
> restyle-in-place migration was being handed off. Preserved for
> primary-source archaeology — specific facts about uncommitted
> worktrees, conflicts, branch state, and verification gaps have drifted.
> The 16-wizard migration later landed on main as `b38d643` and the
> correction preflight / HTTPS asset fixes landed as `c7da1db`. Current
> operational truth for the design system lives in
> [HANDOFF-management-ui.md](HANDOFF-management-ui.md).

> **Follow-up (2026-05-31, separate session).** Two gaps this checklist left
> open are now closed: (1) the plain-HTTP `/correction/` **preflight** — a
> static, nginx-served page the §3 "correction" row never covered — is now on
> the canonical look; (2) the HTTPS measurement UI's `/assets/` were
> 308-redirected to HTTP and **mixed-content-blocked** (CSS unstyled, JS
> dead), which would have failed §6 step 6's on-Pi `/correction/` check — the
> nginx 443 block now serves `/assets/` directly. The dead, unlinked
> `/integrations` page was also removed. Details:
> [HANDOFF-correction.md](HANDOFF-correction.md) (Decision 1 / 3) and
> [HANDOFF-management-ui.md](HANDOFF-management-ui.md) ("`/assets` is served on
> both the HTTP and HTTPS server blocks").

---

## 1. What this was

Goal: take the canonical look already shipped on `/sound/` and
`/system/` and apply it to **every other** `jts.local` wizard so the
management UI is visually and structurally cohesive — same oklch
sage/beige palette, Figtree/Outfit type, the sticky `.app-header` top
bar with the back chevron, and the shared card/button/form vocabulary.

**Architecture decision (important): restyle-in-place, NOT a
client-side rewrite.** Two independent axes were separated:

- *Where HTML comes from* — left unchanged. Request/response form
  wizards stay server-rendered (the server already holds the state; a
  form needs zero JS and can't desync). Only the genuinely-live pages
  (`/system/` polling, `/sound/` EQ drag, `/correction/` getUserMedia +
  measurement) are client-rendered, and they already were.
- *Where behavior lives* — moved to the shared layer. Each page swaps
  `wrap_page()`/hand-rolled `<!doctype>` → `canonical_page()`, applies
  the canonical CSS classes, and any inline `<script>` is extracted into
  an ES module under `deploy/assets/<page>/js/`.

This gets 100% of the visual cohesion and the JS-modularization you
wanted, with zero rewrite of working request/response or hardware logic.

---

## 2. Shared foundation added (the reusable layer)

These are the promotions that let 16 pages stop reinventing chrome:

- **`jasper/web/_common.py`**
  - `canonical_header(title, *, back_href="/", back_label="Home", right_html="")`
    — single source of truth for the `.app-header` top bar (back
    icon-button + centered title). No page hand-rolls the header anymore.
  - `canonical_banner(message)` — canonical flash banner; mirrors the
    `wrap_page` status-message classing (`ok` / `info` / `danger`).
- **`deploy/assets/shared/js/http.js`** — NEW shared ES module exporting
  `csrfHeaders()`, `jsonHeaders()`, `getJSON()`, `postJSON()`. No page
  module re-declares CSRF/fetch plumbing. (`deploy/assets/system-status/js/api.js`
  was refactored to re-export from it.)
- **`deploy/assets/app.css`** — added shared component classes only
  (`.field`, `.form-actions`, `.form-hint`, `.banner*`, canonical
  `.toggle*`). **No `:root` token VALUES changed** (the drift guard in
  `tests/test_web_design_system.py` stayed green).
- **`deploy/install.sh`** — the per-page asset-copy loop is now
  **dynamic**: it iterates every `deploy/assets/*/` dir (excluding
  `fonts`) instead of a hard-coded list. Adding a future page needs no
  installer edit — closes a silent-404 class (a new page's CSS/JS never
  reaching the Pi).
- **`jasper/cli/doctor.py`** — `check_web_design_assets` now also pins
  the `/correction/` page assets.

The existing shared `deploy/assets/shared/js/dialog.js`
(`jtsConfirm`/`jtsAlert`) is reused by every page that needs a
confirm/alert (never `window.confirm/alert/prompt`).

---

## 3. Pages migrated (16, plus the 2 pre-existing = 18 total)

All now render via `canonical_page()`:

| Page | URL | Notes / risk |
|---|---|---|
| speaker | `/speaker/` | reference impl; simplest form |
| peering | `/peers/` | form |
| weather | `/weather/` | form + geocode probe |
| sources | `/sources/` | toggles (USB dtoverlay gating preserved) |
| airplay | `/airplay/` | toggle + status |
| wake | `/wake/` | model picker + sensitivity slider + AEC leg toggles |
| voice | `/voice/` | provider radio + per-provider key/model/voice |
| spotify | `/spotify/` | 3-state OAuth + multi-account |
| google | `/google/` | OAuth (sibling of spotify) |
| home-assistant | `/ha/` | 3-state URL/token/connected |
| transit | `/transit/` | per-provider cards (subway/bus/citibike) |
| dial | `/dial/` | USB-dial provision flow |
| **wifi** | `/wifi/` | **HARDWARE-CRITICAL** — lockout rollback + radio-kill confirm preserved |
| **bluetooth** | `/bluetooth/` | **HARDWARE-CRITICAL** — pairing flow |
| **correction** | `/correction/` | **HW/BROWSER-CRITICAL** — getUserMedia + HTTPS + measurement sweep; JS relocated verbatim into one module |
| **wake-corpus** | `/wake-corpus/` | **HEAVY** — lazy-loaded (NumPy on first use); capture logic preserved |
| (sound) | `/sound/` | already canonical |
| (system) | `/system/` | already canonical |

Each migrated page has a new `tests/test_web_<page>_setup.py` (16 files)
asserting canonical render bytes (`/assets/app.css`, `.app-header`, CSRF)
+ route/behavior preservation.

---

## 4. Test state

Full hardware-free suite (excluding the pre-existing AEC merge-conflict
files and the paid `voice_eval` suite):

```
3412 passed, 6 skipped  (≈169s)
```

Run it yourself with the **main checkout's** interpreter — this worktree
has no `.venv` of its own (see Issue A):

```sh
/Users/jaspercurry/Code/JTS/.venv/bin/python -m pytest -q -p no:cacheprovider tests/ \
  --ignore=tests/test_aec_bridge_stall.py \
  --ignore=tests/test_aec_init.py \
  --ignore=tests/voice_eval
```

---

## 5. Issues we ran into (read before cleanup)

**A. This worktree has no `.venv`.** `.venv/bin/pytest` does not exist
here; the working interpreter is the main checkout's
`/Users/jaspercurry/Code/JTS/.venv/bin/python`. Early in the session
this produced a string of *phantom* "command not found" outputs that
were misread as test failures and sent me chasing non-existent bugs.
**Always use the absolute interpreter above.** (Note: that venv is an
editable install pointing at the *main* repo's `jasper/`, so run pytest
from this worktree's cwd — sys.path[0] wins — or you'll import the
unmigrated copy.)

**B. Pre-existing AEC merge conflicts block deploy/commit.** Four files
are in unmerged (`UU`) state from an earlier abandoned merge —
**unrelated to this migration**:
- `jasper/cli/aec_bridge.py` (3 conflict markers, does not parse)
- `jasper/cli/aec_init.py` (12 conflict markers, does not parse)
- `docs/CHIP-AEC-EXPERIMENT.md`
- `docs/HANDOFF-xvf3800.md`

While these have `<<<<<<<`/`>>>>>>>` markers, the two `.py` files won't
import, so `deploy-to-pi.sh` would ship unparseable Python (jasper-aec-bridge
+ AEC init would SyntaxError on the Pi). They also break collection of
`tests/test_aec_bridge_stall.py` and `tests/test_aec_init.py` (hence the
`--ignore` flags above). **These must be resolved before any commit,
rebase, or deploy.** `origin/main` is 24 commits ahead and includes
newer AEC work (`aec-bridge-stall-fix`, "Harden AEC bridge against
slow-drip mic stalls"), so taking main's version of the AEC files during
the rebase is likely the correct resolution — but that's your call since
you reserved AEC.

**C. Two migration test-harness bugs (fixed).** The `spotify` and
`home_assistant` migrations changed `do_GET`/`do_POST` to delegate to
instance helpers (`self._render_index` / `self._send_html`), but their
new test files used a standalone `_FakeHandler` lacking those methods →
`AttributeError`. Fixed by mirroring the proven `tests/test_web_wifi_setup.py`
idiom (instantiate the real handler via `handler_cls.__new__`). Product
code was correct; only the fakes were incomplete.

**D. Stale assertions in pre-existing test files (fixed).** Several
already-tracked test files asserted the *old* markup the migration
changed (`tests/test_peering_setup.py`, `tests/test_wifi_setup_ui.py`,
`tests/test_system_setup.py`, plus `test_doctor.py` for the new
correction assets). Updated to the canonical equivalents, intent
preserved. `test_system_setup.py`'s CSRF-in-module check now also scans
the shared `http.js` (where CSRF-reading was promoted).

**E. Process issues (my fault, noting for honesty).** The session
initially used background workflows + parallel agents. One workflow
stalled mid-run; a second was launched redundantly and had to be killed;
a test-fix agent was killed but had already finished cleanly. This caused
churn and wasted tokens. The *output* is sound and verified, but if you
see odd artifacts, that's the cause. Switched to a single direct session
at your request.

---

## 6. Cleanup checklist (yours)

1. **Resolve the 4 AEC conflicts** (Issue B). Likely `git checkout
   origin/main -- jasper/cli/aec_bridge.py jasper/cli/aec_init.py
   docs/CHIP-AEC-EXPERIMENT.md docs/HANDOFF-xvf3800.md` (take main's
   newer AEC), then confirm both `.py` files parse.
2. **Commit the migration.** Note: **0 commits exist** — all migration
   work is uncommitted (29 modified tracked + ~34 new files). Commit on
   this feature branch before anything else so it's durable.
3. **Rebase onto `origin/main`** (24 behind). Expect conflicts in shared
   files the migration touched that main also changed — most likely
   `jasper/cli/doctor.py` (main added outputd checks), possibly
   `deploy/install.sh`, `jasper/web/_common.py`, `__main__.py`.
4. **Re-run the suite** (command in §4) — should be green again post-rebase.
5. **Deploy:** `bash scripts/deploy-to-pi.sh` (the only supported path).
6. **On-Pi verification (the things tests can't cover):**
   - `/wifi/` — scan + connect; **radio-off with no Ethernet must still
     fire the "TURNING WI-FI OFF WILL DISCONNECT THIS PI" danger
     confirm**; a failed connect must roll back to the prior network.
   - `/bluetooth/` — scan → pair → connect → forget.
   - `/correction/` — load over **HTTPS** (`https://jts.local/correction/`),
     mic level meter moves (getUserMedia), a measurement sweep runs.
   - `/wake-corpus/` — "Begin a session" incl. "Also capture raw mic 0".
   - `/dial/`, `/voice/`, `/sources/`, `/wake/` — provision, provider
     save, source toggles, sensitivity slider.
   - `sudo /opt/jasper/.venv/bin/jasper-doctor` — confirm
     `check_web_design_assets` reports OK.
7. **PR** per the repo's PR-flow rule (no direct push to main).
8. **Touched-subsystem docs:** `docs/HANDOFF-management-ui.md` was
   updated with the restyle-in-place section + archetype recipes; verify
   it reads correctly and bump its `Last verified` footer.

---

## 7. Change inventory (for PR review scope)

**Product code modified:** `deploy/assets/app.css`,
`deploy/assets/system-status/js/api.js`, `deploy/install.sh`,
`jasper/cli/doctor.py`, `jasper/web/_common.py`, and the 14 migrated
`jasper/web/*_setup.py` (airplay, bluetooth, google, home_assistant,
peering, sound, sources, speaker, spotify, transit, voice, wake,
weather, wifi). (correction, dial, wake_corpus `*_setup.py` are also
modified — confirm all appear in your final diff.)

**New asset dirs:** `deploy/assets/{airplay,bluetooth,correction,dial,
google,home-assistant,peering,sources,speaker,spotify,transit,voice,
wake,wake-corpus,weather,wifi}/` (each `<page>.css` + `js/main.js`),
plus `deploy/assets/shared/js/http.js`.

**Tests:** 16 new `tests/test_web_*_setup.py`; updated
`tests/test_{peering,system}_setup.py`,
`tests/test_wifi_setup_ui.py`, `tests/test_bluetooth_setup_ui.py`,
`tests/test_sources_setup_usbsink.py`, `tests/test_voice_setup.py`,
`tests/test_correction_setup.py`,
`tests/test_correction_status_and_bundles.py`, `tests/test_doctor.py`,
`tests/test_web_common.py`.

**Docs:** `docs/HANDOFF-management-ui.md` (updated), this file (new).

**Do NOT attribute to this migration:** the 4 AEC conflict files (§5.B).

---

Last verified: 2026-06-02 (historical snapshot only; current
operational truth lives in `docs/HANDOFF-management-ui.md`).
