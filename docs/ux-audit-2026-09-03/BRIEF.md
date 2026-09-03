# JTS web UI — UX / information-architecture audit brief

You are auditing the JTS management web UI (repo at /home/user/JTS). Read this
whole brief before opening any file. You are READ-ONLY: do not edit repo files,
do not commit. Write your report to the path given in your task prompt.

## Context you must absorb first (in this order, skim fast)

1. `AGENTS.md` (repo root) — operating defaults. Note especially: smallest
   change that works; no new JASPER_* knobs; no narration comments; duplication
   is converged not added to.
2. `docs/design-language.md` — the VISUAL craft layer (type ladder, text ramp,
   depth, radius, touch targets, motion, writing rules, false-positive filter).
   Its §2 "Already decided" and §13 "Deliberately not adopted" are ratified:
   NEVER file a finding that proposes dark mode, focus rings, a new palette,
   a framework (React/Tailwind/etc), a new type size, or changes to the landing
   page `deploy/index.html` (the protected reference — the owner is happy with
   it; sub-pages are brought up to ITS bar).
3. `deploy/assets/app.css` — the shared design tokens + primitives. Every
   sub-page should be built from these; a page re-implementing a primitive is a
   hygiene finding.
4. `jasper/web/_common.py` — `canonical_page`, `canonical_header`,
   `canonical_banner`, `json_island`, flash/CSRF helpers. This is the page shell
   every wizard should use.
5. `deploy/assets/shared/js/` — shared browser modules.

How a page is built: a Python module `jasper/web/<name>_setup.py` (or `_flow.py`)
renders HTML (usually f-strings) via `canonical_page`, plus a static per-page
stylesheet `deploy/assets/<name>/<name>.css` and ES modules under
`deploy/assets/<name>/js/`. nginx routes `/<name>/` to that module's daemon.
The Pi is NOT reachable from this environment and there is no browser
automation; audit from the source (Python renderer + JS + CSS). Reconstruct
what the screen actually shows, in order, before judging it.

## The principles (the audit lens)

The owner supplied a set of UX laws. Apply them as LENSES for judgement, not as
a mechanical checklist. In JTS terms, the ones that matter most for these
settings/setup screens:

- **One purpose per screen (Hick's).** What is this page FOR? Can a user say it
  in one sentence? Irrelevant/low-priority options should be removed, moved, or
  folded behind disclosure.
- **Primary action is obvious (Von Restorff).** One dominant call-to-action per
  section; everything else visually quieter. Buttons that all compete = finding.
- **Actions sit beside what they affect (Fitts / proximity).** Submit near the
  last input; toggles beside the thing they toggle; related label+control+hint
  grouped by spacing, not borders.
- **Essentials first, order by importance (serial position).** Status and the
  thing you came for at the top; diagnostics/advanced/rarely-used at the bottom
  or behind "Advanced".
- **Chunking (Miller).** Long pages broken into named sections with concise
  labels; forms broken into steps when they are genuinely multi-step; never
  make the user remember something from another screen.
- **Progressive disclosure (Tesler).** Essential controls first; advanced or
  internal-technical detail revealed only when relevant. Do not expose
  implementation vocabulary (unit names, daemon names, env keys) as UI copy
  unless the page is explicitly a diagnostics page.
- **Feedback within ~400 ms (Doherty).** Every click acknowledges immediately;
  loading/processing/success states exist for anything slower; the user never
  wonders whether it registered. Full-page-reload-then-flash-cookie patterns
  are acceptable when they are fast, but silent submits are a finding.
- **Sensible defaults.** Safest/most-common option preselected; context reused;
  no default that creates a surprise commitment.
- **Prevent errors; make them recoverable (Postel).** Accept reasonable input
  variations; explain requirements before submit; disable impossible actions;
  warn before destructive ones; preserve work on error; error copy says what
  happened + what to do next (see design-language §11).
- **Fewest steps (Parkinson).** Remove unnecessary confirmations/screens;
  prefill known information; shortcuts for repeated actions.
- **Visible progress + satisfying completion (Zeigarnik / goal-gradient /
  peak-end).** For genuinely multi-step flows only: show which steps are done,
  confirm what was accomplished, say what happens next. Do NOT invent stepper
  chrome for a page that isn't a flow.
- **Consistency (similarity / uniform connectedness / Jakob).** Same component
  looks and behaves the same everywhere; navigation/back/settings where users
  expect them; reuse app.css primitives before inventing.
- **Prägnanz.** Remove decoration and noise; obvious hierarchy; understandable
  at a glance.

Visual craft (type/colour/depth/targets) is already owned by design-language.md
and partly test-enforced; mention visual issues only when they are gross or
they ARE the IA problem (e.g. three equal-weight buttons).

## Code-hygiene lens (the owner cares about this as much as UX)

The owner's direction: **vanilla JavaScript, HTML, CSS; modular; best-practice
hygiene.** For each page also note:

- Is the markup built in Python f-strings with logic tangled into string
  building? How large are the render functions? Would a cleaner split (Python
  supplies data via `json_island`; a static ES module renders/updates) be
  reasonable, or is server-rendered HTML the right call for that page?
- Inline `<script>`/`<style>` in Python strings vs static files under
  `deploy/assets/` (static is preferred; inline is a finding unless trivial).
- JS module size and structure: one giant main.js vs cohesive modules;
  duplicated fetch/poll/format helpers that `shared/js/` already provides.
- CSS: per-page rules that duplicate `app.css` primitives; off-system values.
- Dead UI: controls/sections with no reachable backend or no user.

## Severity and the false-positive filter

Severity: **High** (user cannot tell what to do / wrong mental model / silent
failure / destructive without warning), **Medium** (clear friction or clutter
that a small restructure fixes), **Low** (polish). Apply design-language §12's
filter before writing a finding: taste is not a defect; a ratified decision is
not a finding; a deliberate choice working as intended is not a finding.
Prefer FEWER, well-evidenced findings over an exhaustive list. Every finding
cites `file:line`.

Remember the product: a hobbyist smart speaker with ONE owner-user who is
technical, on a phone or laptop on the LAN. "Users" = that person. A settings
page can be dense; it must not be confusing.

## Report format (Markdown, write to the path in your task prompt)

For EACH page/route in your cluster:

```
## /route/  —  <one-line purpose as the page currently presents it>
Files: jasper/web/x.py (N lines), deploy/assets/x/x.css (N), deploy/assets/x/js/*.js (N)
Primary user task(s): ...
Screen inventory (top to bottom): numbered list of sections/cards with the
  controls and actions in each, and which states exist (loading/empty/error/
  success/in-progress).
Verdict: KEEP (fine) | TIDY (small local fixes) | RESTRUCTURE (IA rework)
Findings:
- [H|M|L] <id> — <principle> — <what is wrong, with file:line evidence> →
  <concrete proposed fix> (effort S/M/L)
Hygiene:
- ... (same shape)
```

Then a closing section:

```
## Cluster summary
- Ranked list: which pages need work most, and why, in 1 line each.
- Cross-page patterns you saw in THIS cluster (things repeated across pages
  that a shared fix would solve).
- Estimated PR breakdown: a list of single-concern PRs (< 400 changed lines
  each, additions not deletions count) that would deliver the fixes, with
  which model tier could do each (Sonnet for mechanical/contained; Opus for
  judgement-heavy restructures).
```

Be concrete and terse. No preamble, no restating this brief.
