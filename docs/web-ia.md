# JTS web IA — where a page lives and what it reuses

[design-language.md](design-language.md) owns the craft layer; this file owns
navigation, the page shell, the submit model, and what you must reuse.

## 1. One parent, one path

Every page has exactly one parent, and its URL is the path through the menu. An
area whose children would crowd the landing page gets a hub page built from its
settings-group markup; one purpose means a leaf row. `deploy/index.html`'s
`<nav class="groups">` is the site map today; a `jasper/web/nav.py` manifest
(label, path, parent) will own it. `/sound/*` is canonical for measurement; the
legacy `/correction/*` aliases are slated for deletion and take no new links.

## 2. One name, four places, and a way back

The row label, the `<title>`, the `.app-header__title`, and the manifest entry
say the **same words**, and the back link targets the manifest parent. If they
disagree the row label wins — it is what the user tapped. Name the surface,
never the daemon or the module. No "— JTS speaker" suffix.

`canonical_page()` + `canonical_header()` from `jasper/web/_common.py` own the
shell; a client-rendered page renders the same `.app-header`. A deep-linked
page passes `?return_to=`, which the target resolves via `safe_back_href()`.

## 3. Reuse before you write

Grep before adding a class or a helper. A second implementation is a defect.

| Concept | Owner |
|---|---|
| page shell, header, banner, toggle, JSON island, flash, 303, rejected-POST re-render (`send_rejected_form`) | `jasper/web/_common.py` |
| button, info-card, badge, segmented, field, form-actions, spinner, dialog | `deploy/assets/app.css` |
| CSRF fetch, control token, stale-session reload, confirm / alert / prompt, element building, escaping, page header (client-rendered), confirm before submit, copy to clipboard | `deploy/assets/shared/js/`: `http.js`, `dialog.js`, `dom.js`, `escape.js`, `chrome.js` (`appHeader`), `confirm-forms.js` (`data-confirm`), `copy.js` (`data-copy`) |
| mic capture, worklet, WAV encode | `deploy/assets/shared/js/measurement-audio.js` |

Page CSS is a static sheet under `deploy/assets/<page>/` passed as
`page_css_href=`, never an inline `page_css=`, and never re-declares an
`app.css` selector. Page behaviour is one ES module, never an inline
`<script>`. Build DOM with `dom.js`, not `innerHTML`; hide with `hidden`, not
`display:none` or a `.hidden` class; no inline `style=`.

## 4. Two submit models. Pick one per page; never mix silently

- **Form → POST → 303 → flash → `canonical_banner`**, for pages that are a
  form. If you `send_see_other(flash=…)` the page **must** render it; a
  rejected POST re-renders with the user's values, never a bare flash.
- **fetch + JSON + in-place update**, for live surfaces. The click disables its
  control and says what is happening, re-enabling on failure with what to do
  next. Anything that polls backs off when `document.hidden`.

## 5. False-positive filter — on top of design-language.md §12

- A dense settings page is not a confusing one. Density is the brief.
- The landing page's structure, hero, and typography are ratified
  (design-language.md §2); its copy, row labels, and grouping change with the
  IA, on that section's owner-approval path.
- A row a user cannot find, a page with no way back, or a save with no
  acknowledgement is a finding; a grouping you'd have made differently is not.
