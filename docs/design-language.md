# JTS design language — the craft layer

Distilled from [`Dammyjay93/interface-design`](https://github.com/Dammyjay93/interface-design)
(MIT) and Anthropic's `frontend-design` skill, reconciled against our shipped
components on 2026-07-31. Neither is installed; we own every rule below.

## 1. Scope — what this is for

The craft reference for **every management page** — type, colour, depth, radii,
touch targets, motion, copy — applied as each page is touched, never as a
standalone compliance sweep. The jasper.tech capture page
and the Sound measurement wizards at `/sound/room/`, `/sound/crossover/`
(**Active speaker**), and `/sound/bass/` (#1941's world) carry the most design
attention.

| Question | Read |
|---|---|
| Which shared helper / primitive do I use? | [web-ia.md](web-ia.md) |
| How big, how far apart, what colour, how fast? | **this file** |

## 2. Already decided — do not re-derive

A session that proposes a new palette, a typeface pairing, a per-screen
"signature element", or a dark mode has misread the assignment.

- **Palette and fonts** — the oklch sage/beige tokens, Figtree (body) and
  Outfit (display), declared once in
  [`deploy/assets/app.css`](../deploy/assets/app.css), the single source of
  truth. `tests/test_web_design_system.py` fails the landing page if it
  re-declares the tokens or `@font-face` rules.
- **Radius scale** — `--radius` 10px, `-sm` 6px / `-md` 8px / `-lg` 10px.
- **Light only** — `color-scheme: light`. No dark mode, none planned.
- **One accent** — `--primary` carries interaction; status colour comes from
  the `--status-*` tones via a component's `--tone`.
- **No focus rings** — suppressed on purpose; selected state is carried by
  component state (`.active`, `[aria-pressed]`, checked toggles), and a test
  fails any page reintroducing `:focus-visible` / `:focus-within`. A review
  flagging a missing focus ring is producing a false positive.
- **The landing page is the protected reference implementation.**
  [`deploy/index.html`](../deploy/index.html) is the screen the owner is happy
  with; sub-pages are brought up to *its* bar, never the reverse. Changing it
  needs provable visual invisibility — before/after capture at phone and
  desktop widths showing no delta — or explicit owner approval.

## 3. The type ladder

**11 / 12 / 13 / 14 / 16 px. Nothing else** — in `px` or `rem`. Relative `em`
sizing inside a prose element (`.form-hint code` at `0.95em`) is exempt: it
tracks its parent rather than naming a step, so it can't drift off the ladder.
The guard's self-test pins both halves of that rule. The steps are tight —
this is a dense settings surface, and a wider ratio would push every card taller
for no gain. So **hierarchy comes from case, weight and colour, not size**:
region headers and field
labels are uppercase display (`.eyebrow`, `.deflist dt`), card titles cased
display (`.section__title`), values plain body text. Reach for those three
before reaching for a bigger number.

- A new size gets added here first. If you can't justify it in one sentence,
  one of the five already works.
- **No off-ladder rem values.** `0.92rem` is 14.72px — a sixth size nobody
  chose, invisible in review and impossible to keep consistent. Known
  offenders, all in `deploy/index.html`: `.pair-banner__text` (`0.92rem`),
  `.pair-banner__links a` (`0.88rem`), `.volume-safety-note` (`0.86rem`).
  Correcting them to 14px reflows the pair banner, so under §2 they are
  **held for owner review** ([#1961](https://github.com/jaspercurry/JTS/issues/1961)).

## 4. The text ramp — three tiers

| Token | Use |
|---|---|
| `--text` | the thing you came to read: titles, values, primary copy |
| `--muted` | supporting text: subtitles, hints, units, field labels |
| `--muted-faint` | decoration that must not compete: chevrons, disclosure arrows |

Two tiers read flat; four read noisy. Three is the working set, and **pages do
not mix their own.** A `color:` built from `color-mix()` on a foreground token
is a fourth tier invented in one file, invisible to every other page, and drift
by construction. If you need a shade that isn't here, that's a conversation
about the ramp, not a local override. Mixing belongs in the token layer, where
`--muted-faint` is derived from `--muted` so the ramp has one hue owner.

## 5. Depth — one named strategy

**Surface tint + hairline inset ring.** A raised surface is a tinted background
(`color-mix` toward `--secondary`) plus `box-shadow: inset 0 0 0 1px
var(--border)`. That is the whole vocabulary. The single `--shadow`
(`0 1px 2px rgba(0,0,0,0.06)`) is reserved for elements that genuinely sit on
top — a selected segment, a primary button, a toggle knob — and the modal
backdrop is the one deliberate exception. No drop shadows for depth, no layered
elevation scale, no mixing strategies within a screen — picking one and
committing is what makes a UI feel built rather than assembled.

## 6. Concentric radius

**Outer radius = inner radius + the padding between them.** An 8px control
inside 16px of padding wants a 24px container; equal radii on nested boxes is
the most common reason an interface looks subtly wrong. Two shipped pairs
don't satisfy this — `.settings-list` (10px) around `.row-icon` (8px) across
16px padding, and `.control-panel` around `.source-buttons`. Both sit on the
protected landing page, so they're a proposal, not a fix in passing (§13).

## 7. Numbers

**Any number that repaints in place gets `font-variant-numeric: tabular-nums`.**
Proportional digits change width as the value changes, so a live readout
twitches its neighbours on every update. "Repaints in place" means polled
values, drag readouts, timers, live metrics — not a number rendered once.

`.deflist dd` has it. The live gaps — `.status-val` (carrying `#vol-percent`,
repainting on every drag frame and a 2 Hz poll) and `#system-summary`
(CPU · temp · disk, every 20 s) — are all on the protected landing page, so
they're ledgered rather than fixed here.

## 8. Touch targets

**44px preferred, 40px hard floor**, on every interactive control. Visual size
is a separate decision: a 32px disc can be right for a 56px header row — what
must not be 32px is the *hit* area. Grow it with an absolutely-positioned,
paint-free `::after` overlay (or by sizing the transparent input that already
carries the hit), never with padding, which reflows the layout you liked:

```css
.icon-button { position: relative; }          /* visual stays 32x32 */
.icon-button::after { content: ""; position: absolute; inset: -6px; }
```

No grandfathered-exceptions list: the three controls that were under the floor
— `.mic-action` (30px), `.icon-button` (32×32), `.toggle` (44×24) — were
brought to 44px this way, rendered pixels unchanged. Check that stacked
controls sit ≥44px apart **centre to centre** — below that, grown areas
overlap and a tap near the boundary reaches the wrong control. The tightest
shipped case is `/sources/` at 53.8px.

## 9. Motion

**150 ms, `ease`, on `transform` / `opacity` / colour** — the default, and it
covers almost everything we ship. An appliance control surface should feel
immediate, not animated.

- Never `transition: all` — it animates properties you didn't think about and
  costs layout work on every change. Name the properties.
- Never `ease-in` on something the user is waiting for: it starts slow, which
  reads as lag.
- Longer bands exist for surfaces that travel further — ~150–250 ms for a
  dropdown, 200–500 ms for a modal. Only when something moves that far.
- Reduced motion is handled globally in `app.css`. Don't re-implement it per
  page; do check your animation is covered by it.

## 10. Small polish

- `text-wrap: balance` on headings, `text-wrap: pretty` on body copy. Cheap,
  and it removes the one-word last line. Currently unused anywhere.
- Font smoothing and `text-rendering` are set globally — leave them alone.
- Optical beats mathematical alignment for icons beside text: trust the
  screenshot over the box model.

## 11. Writing

Copy is design. The rules most often broken:

- **An action keeps its name across the whole flow.** *Measure* leads to
  *Measuring* and then *Measured*. Renaming an action mid-flow makes people
  wonder what changed.
- **Name what people control, not how it's built.** "The microphone", not
  "the capture relay". The user's model of the system is the product.
- **Errors say what happened and what to do next. They don't apologise and
  they're never vague.** "Sorry, something went wrong" is three words of
  nothing; "too quiet — move the phone closer and retake" can be acted on.
- **Empty states invite an action.** An empty screen is a direction, not a
  mood: say what would fill it and give the control that starts it.
- **One element, one job.** A label labels; an example demonstrates; a hint
  hints. A line doing two of those does neither.
- **IA over copy.** When a screen is confusing the fix is almost always
  hierarchy, grouping and signposting — not more sentences. The acceptance
  criterion is a **net word count that goes down**. If a clarity pass added
  words, it wasn't a clarity pass.

## 12. Visual-craft review lenses

[`.claude/commands/adversarial-review.md`](../.claude/commands/adversarial-review.md)
owns *when* these apply — it names the paths in its JTS checklist, so the
trigger has one home and cannot drift from this list. This section owns *what*
to look at. Same one gate, same severity ladder — **Blocker / Should-fix / Nit,
to a 0/0 bar**; there is no second review command. Look at the whole rendered
screen before reading a line of CSS. Then:

1. **Hierarchy** — squint. If three things compete, nothing is primary.
2. **Type** — every size on §3's ladder; hierarchy from case/weight/colour.
3. **Colour** — text colours from §4's ramp; one accent; status via `--tone`.
4. **Depth and shape** — §5's strategy, §6's radii, tokenised radii/borders.
5. **Spacing** — consistent rhythm, symmetrical padding, related things closer
   together than unrelated ones.
6. **States and targets** — hover / active / disabled / loading / empty / error
   all exist; §8's target floor; §9's motion policy.

**False-positive filter — apply before writing a finding:**

- **Taste is not a defect.** "I'd have done it differently" is not a finding,
  and neither is a deliberate choice working as intended.
- **A ratified decision is not a finding.** No focus rings, light-only, the
  tight ladder, the protected landing page (§2). Flagging these is noise, and
  it trains people to skim reviews.
- **Out of the diff is out of scope.** Pre-existing conditions the diff didn't
  touch belong in the ledger issue, not in the gate.

## 13. Deliberately not adopted

Written down so a future session doesn't re-import them:

- **`.interface-design/system.md` memory file** — a second design SSOT that
  would restate what `app.css` owns and drift on the first token change.
- **Dark mode** — light-only by decision (§2).
- **React / Radix / shadcn / CVA / Tailwind mechanics** — we're stdlib Python
  plus static ES modules; a fifth of the source material is inapplicable.
- **A signature element per screen** — wrong pressure for an appliance surface.
  #1941's thesis is that restraint and IA are the fix, not expression.
- **The per-component checkpoint block** (restating intent / palette / depth /
  spacing on every UI edit) — token burn against a locked design system.
- **Focus rings** — suppressed on purpose and test-enforced (§2); the source
  material requires them, which would open every review on a false positive.
- **Type-scale ratios (1.2 / 1.25 / 1.333)** — sound reasoning, and why §3
  exists, but our five-step ladder is already chosen and shipped.

**Known gaps ledger — [#1961](https://github.com/jaspercurry/JTS/issues/1961).**
The §3 off-ladder sizes, §6 concentric pairs, §7 tabular-nums sites,
`text-wrap` adoption, and the one `transition: all` in `sound.css` are tracked
there: a parking lot, not a work queue. Design attention belongs on the
correction/crossover flow.

---

Verification scope (2026-08-04): route-scope only: the Room, Active-speaker, and Bass
measurement surfaces are canonical under `/sound/*`; direct `/correction/*`
aliases render the same contract. No craft-system rule changed. Prior
2026-07-31.

Last verified: 2026-08-04
