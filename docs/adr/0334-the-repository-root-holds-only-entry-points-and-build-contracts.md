# ADR-0334: The repository root holds only entry points and build contracts

- **Date:** 2026-09-22
- **Status:** Accepted

## Context

The root had 21 tracked files. Operator manuals, community policy, and a
changelog sat beside build contracts, and readers pinned their exact root
paths. The doc map had grown a classification layer that required document
registration without checking truth. Audit findings I1-01, I1-02, I1-03,
I1-05, I5-05, and I5-06 identified this clutter and stale setup prose
([#5061](https://github.com/jaspercurry/JTS/issues/5061),
[#4808](https://github.com/jaspercurry/JTS/issues/4808)).

## Decision

- Keep README.md, AGENTS.md, CLAUDE.md, LICENSE, NOTICE,
  LICENSE-third-party.md, SECURITY.md, PLAN.md, pyproject.toml, uv.lock,
  and the dotfiles at root.
- Put operator documents in `docs/` and community files in `.github/`,
  where GitHub recognizes them. Update link targets everywhere, including
  frozen documents, when a target moves. Leave no forwarding stubs.
- The doc map routes code to docs and validates paths, nothing more.

## Consequences

The manuals move to `docs/bringup.md` and `docs/quickstart.md`; privacy and
selected release notes move to `docs/privacy.md` and `docs/changelog.md`.
CONTRIBUTING.md and CODE_OF_CONDUCT.md move to `.github/`. The empty logs
placeholder is deleted; `scripts/fetch-pi-logs.sh` creates the directory.

CI classification follows the new paths and keeps its full-CI fallback.
The six-file root-existence test becomes one privacy-visibility pin:
a non-empty privacy file exists and README links it directly. Packaging
already enforces LICENSE and NOTICE. Doc-map classification tests are
removed; routing and existing-path checks remain.

PLAN.md stays as the roadmap entry point. SECURITY.md stays because CLI
and control responses name it. The three license files stay where
pyproject.toml packages them. Old repository file paths stop working;
public URLs stay the same. Frozen prose keeps its content, with only
broken link targets eligible for a mechanical rename.
