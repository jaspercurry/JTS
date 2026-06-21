# Launch readiness — verified backlog

> **Status: current source of truth (verified 2026-06-21).** This is the live,
> evidence-checked open-source-launch backlog. It **supersedes** the
> point-in-time audit snapshots `docs/REVIEW-2026-06-04-*.md`,
> `docs/REVIEW-2026-06-12-oss-due-diligence.md`, and
> `docs/REVIEW-google-oss-readiness.md` — those are tagged historical and kept
> only for archaeology (they list work that has since shipped). Drive cleanup
> agents from THIS doc, not those.

The list was verified against `origin/main` — every "done" line was confirmed in
the tree (the symbol / CI step / tag cited), not trusted from an older doc (the
earlier audit docs surfaced already-fixed items as open, which is exactly why
this doc exists). **The open list is now empty** — every tracked launch item
has shipped (see the archiving note at the bottom).

## ✅ Done (verified on `main`)

- **Daemon privilege separation — the one launch blocker — COMPLETE.** All five
  Tier-A daemons run non-root (`User=jasper-* Group=jasper`): hardened-root
  stanza (#722), invisible control token (#728), the user drop
  (#763 control, #768/#773 web, + voice/mux/input), and secret
  compartmentalization (#776 `jasper-secrets` for LLM/Google keys, `jasper-intsecrets`
  for HA/Spotify). The group-perm-clobber the drop introduces is fixed (#827/#834)
  and guard-tested (`test_systemd_hardening.py`, `test_aec_reconcile.py`). Design
  of record: [HANDOFF-privilege-separation.md](HANDOFF-privilege-separation.md).
- **CI / type-safety hardening** — landed across the 2026-06-18→19 cleanup pass:
  a lenient mypy baseline in CI (the "Type check (mypy; lenient baseline)" step +
  `jasper/py.typed` + `[tool.mypy]` config); a Python **3.11 / 3.12 / 3.13
  `pytest-matrix`** with an aggregate gate (the declared `requires-python` floor
  is now actually tested); CI consumes the lockfile (`uv sync --locked`); and
  `cargo clippy -D warnings` + `rustfmt --check` on every crate plus a
  `.pre-commit-config.yaml`.
- **Resilience hardening (control-server + doctor)** — `jasper-control` now has a
  bounded-concurrency + single-flight TTL response cache (`_SingleFlightTTLCache`;
  `STATE_RESPONSE_CACHE_TTL_SEC=1.0`, `_DIAGNOSTICS_CACHE_TTL_SECONDS=60.0`), and
  `jasper-doctor` parallelizes its subprocess checks (`asyncio.to_thread` +
  per-check `wait_for` in `run_async`), closing the 30s-ceiling `/system/diagnostics`
  502 risk on a 1 GB Pi.
- **OSS governance scaffolding** — `LICENSE`, `SECURITY.md`, `PRIVACY.md`,
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `.github/CODEOWNERS`, issue templates,
  PR template. Branch protection live (required `pytest` + `rust`).
- **Supply chain** — all install fetches SHA-pinned (`deploy/provenance.toml`),
  Actions pinned to SHAs, `Cargo.lock` + `uv.lock` committed, Rust CI uses `--locked`.
- **Structured logging** — `jasper.log_event` migration complete + CI-enforced.
- **The 11 §5.5 runtime defects** — fixed with regressions (from the 2026-06 review).
- **Release tag** — `v0.1.0` was created as an annotated tag on `5ad21856`
  (the `origin/main` tip at tagging time) on 2026-06-19. This docs follow-up
  records the release marker after the fact; no additional Pi/on-device
  validation was run in the Codex tag session, so hardware confidence comes
  from owner checks outside that session.
- **SPDX license headers** — every first-party source file now carries an
  `Apache-2.0` SPDX header (#910): the bulk implementation of CONTRIBUTING's
  "first-party JTS source is Apache-2.0" convention via `reuse annotate`
  (~996 files, comments-only, full `test-merge` green). First-party headers
  only, **no CI gate** — a green-CI REUSE action is a standing
  annotate-or-break tax that outweighs the badge on a solo repo. The genuinely
  third-party in-tree assets (OFL fonts, LVGL `lv_conf.h`, `mta_stations.csv`,
  presets) stay unstamped and are inventoried in
  [LICENSE-third-party.md](../LICENSE-third-party.md). The bulk commit is
  recorded in `.git-blame-ignore-revs`.

## 🟢 Open — none

The launch backlog is empty. The privilege-separation blocker, the
CI/type-safety + resilience batch, governance/supply-chain, and the SPDX header
sweep have all shipped (see Done). If new launch-blocking work surfaces, give it
a fresh bullet here with a ready-to-paste agent prompt.

## Deferred by design (not "open")

- **TLS on the secret-bearing wizards** — PSKs/tokens still cross the LAN over
  plain HTTP (only `/correction/` has TLS). This is the documented, accepted
  trusted-LAN trade-off (see SECURITY.md), parity with router admin UIs. Revisit
  only if the threat model changes.

## Maintaining this doc

When an open item ships, move its bullet to **Done** with the PR number (or the
verified symbol/CI step) and delete the agent prompt. **The open list is now
empty (SPDX shipped in #910), so the launch-readiness work is complete** — this
doc is ready to be bannered historical and moved to `docs/historical/` as a
follow-up (that move also touches the README atlas row and the
`docs/doc-map.toml` entry, so it is kept out of the mechanical SPDX PR).

Last verified: 2026-06-21
