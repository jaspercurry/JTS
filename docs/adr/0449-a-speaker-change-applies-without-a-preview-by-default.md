# ADR-0449: A speaker change applies without a preview by default

- **Date:** 2026-10-05
- **Status:** Accepted. Supersedes in part
  [ADR-0444](0444-a-speaker-trial-is-optional-and-plays-its-whole-candidate.md) §2: its sentence "The
  default setup fits the speaker and applies it after its preview, with no trial (an apply needs
  none, #6227 rule 6)" and the parenthetical "(apply after the preview; trial only when asked)" that
  follows it. Its §1 and the rest of §2 stand.
- **Context:** ADR-0444 §2 wrote the default speaker setup as fit, preview, apply. A speaker
  `judge --preview` reads one take's woofer impulse, tweeter impulse and sum together
  (`capture_prediction.read_diagnostic`, through `round_captures.select_capture_roles`). The default
  round, `speaker/mark`, holds no such take: its MEASURE takes keep each driver's impulse and no
  sum ([ADR-0397](0397-the-take-views-read-only-records.md) §4), and its timing take keeps only the
  sum. So `judge --preview` on a `speaker/mark` round refuses `round_role_not_recorded`. Only a
  `branches/express` take, each driver's branch and the sum of one saved candidate on one clock at
  the mark, holds all three. The runbook, the playbook and the copied speaker prompt told the owner
  to preview first, and no take of the default plan can answer that step.
- **Decision:**
  1. The default setup fits the speaker from the `speaker/mark` round and applies it with no
     preview and no trial: `judge`, `compose`, `apply`. The copied speaker prompt's note, the
     runbook and the playbook say so.
  2. A preview of a speaker document needs a `branches/express` take of the document's base
     (`jasper-round run --program branches/express --candidates <base fingerprint>`). The owner asks
     for it as for a trial: when a forecast could change the choice.
  3. Nothing else changes. ADR-0444 §1 (a speaker trial, when asked for, plays its whole
     candidate) and the rest of §2 stand. The rear, bass and room previews read other rounds and
     stay as they are.
- **Consequences:**
  - The default speaker setup adds no round to the counts in ADR-0444's Consequences; none included
    a preview take.
  - A speaker preview costs one `branches/express` take at the mark. It forecasts a driver, blend
    or topology change against that base, and a trial still measures the pick.
  - The copied speaker prompt's note is code (`tuning_handoff.PROGRAM_NOTES`); it follows in
    [#6296](https://github.com/jaspercurry/JTS/pull/6296).
  - Rejected: adding a `branches/express` take to the default setup so the preview can run. An
    apply needs no preview (#6227 rule 6), and that take is a round the default plan's count does
    not hold.
