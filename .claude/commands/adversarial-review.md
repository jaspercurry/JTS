# Adversarial review — non-negotiable tier only

Run this only when the diff touches the charter's closed non-negotiable list
(AGENTS.md): the hearing clamps (CamillaDSP `volume_limit`, `set_volume_db`
clamp, commissioning SPL stop), driver caps or XVF brick-hazard operations,
DSP math on the output path, secrets handling, or `deploy/install.sh` /
deploy guards. For everything else use the built-in `/code-review` (medium)
— not this.

## Posture

Read-only, fresh eyes, evidence before judgment. Re-read the actual diff and
its callers; run the cheap checks (`scripts/test-fast`, a targeted pytest)
when they'd settle a question. Cite `file:function` for every finding. You
are hunting real failures, not manufacturing findings: report **nothing**
when the change is sound, and say so plainly.

## The checklist (all of it — nothing more)

1. **Correctness of the change itself.** Does the code do what the diff
   claims, across its failure branches? Any state that can be left
   inconsistent (partial write, lock not released, unit restarted at the
   wrong moment)?
2. **Hearing / hardware safety.** Could any path raise output above the
   clamps, bypass the SPL stop, exceed a driver cap, or touch a brick-hazard
   firmware op? Is the 0 dB ceiling still enforced where this diff plays?
3. **Secrets.** Could a key/PSK/token reach a log, `/state`, doctor output,
   an error message, or a committed file?
4. **Single source of truth.** Does the diff create a second writer for an
   env file/state file, restate an owned fact, or fork an existing helper or
   vocabulary instead of consuming it?
5. **Unrequested additions.** Flag anything speculative the diff adds — a
   guard for a hypothetical, a knob nobody asked for, a test welded to
   implementation text, narrated prose — as a finding to *remove*, never to
   expand.

## Output

- **Blockers** — would cause the failure classes above; must be fixed before
  merge. Each: claim, evidence (`file:function`), smallest fix.
- **Notes** — everything else worth the owner's eyes, including removals
  under item 5. The owner triages; there is no zero-findings requirement and
  no re-review round unless a blocker fix itself touches a non-negotiable.

Do not request new tests, docs, comments, or defensive layers except where
item 2 or 3 demands one. Report-only: never push, merge, or edit.
