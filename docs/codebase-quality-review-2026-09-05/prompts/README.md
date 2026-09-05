# Prompts to take each non-A attribute to A

One self-contained prompt per attribute from the review's grade table. Paste a file's full contents as
the kickoff message of a fresh Fable session on this repo. Each prompt makes the three rules the owner
cares most about impossible to miss — Fable delegates to Opus/Sonnet and does no lane work; every PR
gets `/code-review` and `/simplify` before merge; every finding is re-verified at HEAD — and each ends
at a plan gate the owner triages before code is written.

| File | Attribute | From |
|---|---|---|
| `P1-secrets.md` | Secrets (NN-3) | C+ |
| `P2-deploy-integrity.md` | Deploy integrity (NN-4, NN-8) | C+ |
| `P3-resilience.md` | Resilience | B |
| `P4-observability.md` | Observability | B− |
| `P5-structure-and-god-files.md` | Separation & SSOT, followability, god files | C+ / C |
| `P6-right-sizing.md` | Right-sizing | C |
| `P7-tests.md` | Tests | B− |
| `P8-docs.md` | Docs and prose | B |

Hardware/audio safety is already A− and needs only R-016's belt-and-braces row (in P4's doctor work
and P3's clamp event). The tuning zone is another agent's; every prompt hands tuning-zone items over
as suggestions. Suggested order: P1 and P2 first (they close the two non-negotiable seams and are
small), P5 next (its layers contract guards everything after it), then P3/P4 together, then P6, P7,
P8 opportunistically.
