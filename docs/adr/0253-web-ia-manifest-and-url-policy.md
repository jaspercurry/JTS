# ADR-0253: Web IA — manifest ownership, hub scope, and URL-move policy

- **Date:** 2026-09-07
- **Status:** Accepted

## Context

The P11 web-UI program (issue #4212) moved most management pages onto a
shared manifest, shared front-end primitives, and a small set of hubs, and
moved several URLs while doing it — the P11 PRs on #4212. Two of those
moves, #4404 and the in-flight #4430, landed under different redirect
rules with no record of why, and the site map itself has a shape
(`jasper/web/nav.py`'s `hub_paths()`) that a plan or a new page can
misread as "every group is a hub." This ADR is the record; the living
conventions stay in [`docs/web-ia.md`](../web-ia.md), which owns the page
shell, submit model, and reuse table and is not restated here.

## Decision

1. **One manifest, one owner.** `jasper/web/nav.py` is the single writer
   of a row's title, its place in the nav tree, and its URL
   (`docs/web-ia.md` §1-§2). It stays stdlib-only: `install.sh` renders it
   under the system Python at install time, before the venv exists.

2. **Two hubs today, not one per landing group.** `nav.hub_paths()` —
   every path some row's `parent` names, other than `/` — returns exactly
   `/sound/` and `/assistant/`; those are the only rows `render_hub`
   builds a static settings-group page for. "Sources" and "System" are
   landing groups, not hubs: their rows (`/sources/`, `/system/`,
   `/wifi/`, …) hang directly off `/`. A third shape exists and is not a
   hub either: a row can hang under a daemon-served page that renders its
   own link to it — `/sound/pair/sync/`'s parent is `/sound/pair/`, a live
   wizard, not a static hub page. Do not add a hub for a group that is
   merely crowded; a hub is for a settings-group page with no daemon of
   its own.

3. **A URL move: both confs, one slash-normaliser, and a default of no
   redirect.** `deploy/nginx-jasper.conf` and
   `deploy/nginx-jasper-streambox.conf` move together, `:80` and `:443`
   blocks alike, and a moved page gets a
   `location = /new/path { return 308 /new/path/; }` block beside its
   prefix block. The default is that the **old path simply stops being
   served** — no 301, no compat window. Two precedents, reconciled by one
   rule:
   - `/sync/` → `/sound/pair/sync/` (#4404) shipped with no redirect: it
     was reachable only by URL bar, nothing in the product linked it.
   - The eight-page move under `/assistant/` (C.A1, in flight as #4430)
     instead pairs every old prefix with a
     `location /old/ { return 301 /assistant$request_uri; }` block: those
     paths are linked from the landing page, get bookmarked, and one is
     spoken aloud (`jasper/voice/prompt.py`'s setup nudges). Drop each
     compat block once server logs show nothing hitting it — the same
     removal-condition discipline AGENTS.md asks of any guard.
   - **The rule:** redirect only a path something outside this move could
     still be pointed at — a bookmark, a spoken URL, an operator doc.
     Cut everything else outright; the mover decides explicitly per row.
   - `/google/callback` is neither: an **exact** block, pinned ahead of
     any `/google/` prefix move, because the OAuth redirect URI is
     registered *outside this repo* (a bounce page under a different
     project) and nothing here can change where it points. No removal
     condition — the external registration is permanent by construction.

4. **Shrink-only allowlists, deleted on zero.** `_TITLE_ALLOWLIST` and
   `_INLINE_STYLE_ALLOWLIST` (`tests/test_web_wizard_conventions.py`) are
   ratchets: a page's entry comes out the PR that fixes it, and nothing may
   add an entry without a ledger row. `NO_APP_HEADER_ALLOWLIST`
   (`tests/test_web_design_system.py`) and `_LEGACY_HIDDEN_CLASS_PAGES`
   (`tests/test_web_wizard_conventions.py`) were the same shape, reached
   zero, and were **deleted outright** — dict, exemption branch, and all —
   in `50eba81d0` and `3080d3a6a`; the corresponding tests now assert zero
   offenders with no allowlist in the loop at all. The rule: an allowlist
   only ever shrinks, and it does not outlive its last entry — once empty
   it is deleted along with the branch that consulted it, leaving the
   plain assertion as the permanent guard.

5. **One front-end standard, not a second implementation per page.**
   `dom.js` builders instead of `innerHTML`, `startPolling` (hidden-tab
   backoff) instead of an ad-hoc `setInterval`, `postJSON` for fetch+JSON
   pages, the `hidden` attribute rather than a `.hidden` class or
   `display:none`, and one page-CSS file per page linked by
   `page_css_href` rather than an inline `style=` or an inline
   `page_css=`. `docs/web-ia.md` §3 is the living table of which shared
   module owns which concern; this program's job was convergence onto it,
   not a new list.

6. **Phase D pages get a routes table, not a hand-rolled dispatcher.** A
   wizard's `do_GET`/`do_POST` becomes a `_GET_ROUTES`/`_POST_ROUTES`
   mapping plus a small `_make_handler_class` factory — the shape
   `jasper/web/wake_corpus_setup.py` already used — and its tests assert
   `"/path" in _GET_ROUTES` instead of pinning `inspect.getsource` against
   an `if`/`elif` ladder.

7. **New guard: conf parity.** Both confs' `location` path sets must be
   equal once the documented streambox difference is subtracted. Today
   that difference is `/mic`, `/source`, `/wake/`, `/wake-corpus/` (the
   streambox profile ships no wake stack) and `/source` vs `/source/` (a
   pre-existing trailing-slash mismatch, not a page difference). Removal
   condition: narrow the exception list the day streambox gains wake
   support or the trailing-slash mismatch is fixed; delete the guard's
   exception branch entirely if the streambox profile is ever retired.
   The plan's other candidate — a stdlib-only AST import-closure check
   over `jasper.web.{nav,landing}` — was **not** added: no box reading
   showed the landing render path pulling a non-stdlib module today, so
   it would have guarded a hypothetical with no incident behind it
   (AGENTS.md's guard triage; ADR-0001 §5).

## Consequences

A future page gets exactly one manifest row (title, URL, parent decided
once), a matching pair of conf blocks added to both confs in the same PR
(or none, if it hangs under an existing hub or daemon page), its own page
CSS file, and a routes-table + membership-check test rather than a
bespoke markup or source pin. A future URL move adds both confs' blocks
plus a slash-normaliser and makes one explicit call per old path — orphan
it, or redirect it with a removal condition — instead of defaulting either
way by habit. Given up: a blanket compat window for every moved URL: only
a path with a real external or bookmark tie gets one, so most moves are a
clean cut with no redirect to prune later. Superseded: the per-page
`inspect.getsource` route pins Phase D replaces, and the inline
conf-comment slice check the conf-parity guard replaces.
