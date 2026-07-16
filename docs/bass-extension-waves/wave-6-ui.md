# Wave 6 — commissioning UI + low-frequency overview (Codex prompt)

Read `docs/bass-extension-waves/README.md` (binding charter) first,
then this file completely. Prereqs: Wave 4 merged (its HTTP contract
is your API); Wave 5 merged if you wire live status (else render
those fields absent).

## Mission

The household-facing surfaces: a commissioning wizard page driving
Wave 4's endpoints, and the existing Bass Management page upgraded to
the read-only low-frequency overview. Follow the canonical wizard
conventions exactly — the conventions tests will catch you if you
don't.

## Required reading (in order)

1. `docs/HANDOFF-bass-extension-plan.md` §9 (UI placement — read
   carefully), §5.3 (the honesty copy).
2. `docs/bass-extension-waves/wave-4-commissioning-backend.md` — the
   HTTP contract section (your API; render `available_actions`, do
   not re-derive state logic client-side).
3. `AGENTS.md` "Web wizard conventions" + "Canonical design system"
   sections — binding.
4. `jasper/web/correction_bass_flow.py` + its JS
   (`deploy/assets/correction/js/bass/main.js`) — the page you
   extend, and the smallest exemplar of the page/module shape.
5. One richer exemplar flow for server-driven wizard UI (the
   crossover flow's page module under `deploy/assets/correction/js/`)
   — mirror its fetch/poll/render loop.
6. `tests/test_web_wizard_conventions.py` and
   `tests/test_web_json_island.py` — the static gates your files must
   pass.
7. `deploy/lib/install/web-assets.sh` — how per-page JS dirs get
   installed + manifested.

## Preflight facts

- Wave 4's endpoints exist and `GET /bassext/state` returns
  `available_actions`.
- `canonical_page`, `json_island`, `csrf_meta_html`,
  `csrf_fetch_helpers_js`, `jsonHeaders` exist in
  `jasper/web/_common.py`.
- Shared JS modules exist at `/assets/shared/js/{dialog,escape,dom}.js`.
- `resolve_bass_management()` state carries the Wave-2
  `bass_extension` field.

## File allowlist

Create:
- `jasper/web/bassext_flow.py` — page module (~250 lines; page shell +
  `page_css` + json_island of initial state; ALL behavior in the ES
  modules)
- `deploy/assets/correction/js/bassext/main.js` (+ at most two
  sibling modules if main exceeds ~400 lines: `views.js`, `api.js`)
- `tests/test_web_bassext_flow.py`

Modify (additive):
- `jasper/web/correction_bass_flow.py` + `deploy/assets/correction/js/bass/main.js`
  — the overview additions: commissioned range, current target,
  status (accepted/bypassed/stale/absent), link to the commissioning
  flow. Stays display-only.
- `jasper/web/correction_setup.py` — the page's GET route only:
  dispatch lines in the `do_GET` ladder (same shape Wave 4 added for
  `/bassext/*`; handler bodies live in `bassext_flow.py`).
- `deploy/lib/install/web-assets.sh` ONLY if the new JS dir isn't
  covered by its existing per-directory copy loop (check first; it
  likely globs `correction/js/*`).
- Existing tests for the bass overview page (extend).

## Wizard requirements (from plan §9 — all mandatory)

- Margin selector with plain-language consequences (one sentence per
  tier, from `MARGINS` values — render the numbers, don't restate
  them in prose that can drift).
- Family/anchor review: a plain table (target, corner, boost, usable-
  below level, evidence tag) + the predicted-curves data from the
  propose endpoint rendered as an inline SVG polyline (no chart
  library — mirror any existing inline-SVG usage in the correction
  pages; if none fits, a table alone is acceptable — prefer less).
- A persistent red **Stop** button visible during any
  playback-capable state, wired to `POST /bassext/stop`, enabled
  without confirmation dialogs, and functional without a page reload
  (the `/sound/` combined-test wedge lesson — test this explicitly).
- The envelope honesty copy verbatim-class: "measured clean operating
  envelope, not a driver warranty" phrasing from plan §5.3.
- Expert drawer (collapsed by default): Qp, boost cap, rung step,
  subsonic override, impedance `.zma` upload (posts to Wave 4's fit
  endpoint if it accepts one; if Wave 4 shipped without the upload
  field, omit the control — check, don't assume).
- Server-driven: the page renders `available_actions` and state; it
  never decides what's allowed. Poll `GET /bassext/state` at the same
  cadence sibling flows poll (match, don't invent).
- Untrusted strings (device names, refusal details) through
  `escapeHtml`/`h()` — never `innerHTML` concatenation; confirm/alert
  via `jtsConfirm`/`jtsAlert` only.

## Anti-overengineering fences

Do NOT: add a frontend framework, bundler, or shared "component
library" beyond the existing shared modules; duplicate state logic
client-side; add CSS to `app.css` (page css lives in `page_css`);
create new endpoints (if the UI needs data Wave 4 doesn't expose,
stop and report — the contract changes in Wave 4's file first); add
websockets/SSE; build the deep-mode UI beyond a single toggle that
passes through to the backend; touch `correction_setup.py`,
`session.py`, or `correction/js/main.js` internals beyond the named
routing seam (the god-file split is someone else's project).

## Acceptance commands

```
.venv/bin/pytest tests/test_web_bassext_flow.py \
  tests/test_web_wizard_conventions.py tests/test_web_json_island.py -q
bash scripts/check-js-syntax.sh
scripts/test-fast
```

PR description must include a text walkthrough of the wizard states
(idle → … → accepted) and confirmation the Stop button path was
tested with the backend mocked mid-ladder.
