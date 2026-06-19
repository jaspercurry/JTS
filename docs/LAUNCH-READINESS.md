# Launch readiness — verified backlog

> **Status: current source of truth (verified 2026-06-18).** This is the live,
> evidence-checked open-source-launch backlog. It **supersedes** the
> point-in-time audit snapshots `docs/REVIEW-2026-06-04-*.md`,
> `docs/REVIEW-2026-06-12-oss-due-diligence.md`, and
> `docs/REVIEW-google-oss-readiness.md` — those are tagged historical and kept
> only for archaeology (they list work that has since shipped). Drive cleanup
> agents from THIS doc, not those.

Each open item below carries a ready-to-paste agent prompt. The list was
verified against `origin/main` — every "done" line was confirmed in the tree,
not trusted from an older doc (the earlier audit docs surfaced already-fixed
items as open, which is exactly why this doc exists).

## ✅ Done (verified on `main`)

- **Daemon privilege separation — the one launch blocker — COMPLETE.** All five
  Tier-A daemons run non-root (`User=jasper-* Group=jasper`): hardened-root
  stanza (#722), invisible control token (#728), the user drop
  (#763 control, #768/#773 web, + voice/mux/input), and secret
  compartmentalization (#776 `jasper-secrets` for LLM/Google keys, `jasper-intsecrets`
  for HA/Spotify). The group-perm-clobber the drop introduces is fixed (#827/#834)
  and guard-tested (`test_systemd_hardening.py`, `test_aec_reconcile.py`). Design
  of record: [HANDOFF-privilege-separation.md](HANDOFF-privilege-separation.md).
- **OSS governance scaffolding** — `LICENSE`, `SECURITY.md`, `PRIVACY.md`,
  `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `.github/CODEOWNERS`, issue templates,
  PR template. Branch protection live (required `pytest` + `rust`).
- **Supply chain** — all install fetches SHA-pinned (`deploy/provenance.toml`),
  Actions pinned to SHAs, `Cargo.lock` + `uv.lock` committed, Rust CI uses `--locked`.
- **Structured logging** — `jasper.log_event` migration complete + CI-enforced.
- **The 11 §5.5 runtime defects** — fixed with regressions (from the 2026-06 review).

## 🟡 Open — fan agents out from here

The privsep blocker is gone; everything below is **hardware-free, low-risk, and
high-signal for an OSS launch** unless noted. Order is by leverage-per-risk.

### 1. Type checker (mypy, lenient/baselined) — M, hardware-free
**Why:** ~3,000 annotated functions are never verified; CI runs only `ruff`.
**Where:** `.github/workflows/tests.yml`, `pyproject.toml [dependency-groups] dev`.
```
Add a lenient, incremental type checker to JTS. Add mypy to pyproject's dev
dependency group + a [tool.mypy] config that starts permissive (ignore_missing_imports,
no disallow-untyped — this is a 136k-LOC partially-typed codebase, do NOT flip
strict). Ship a py.typed marker. Add a non-blocking "mypy" step to the pytest job
in .github/workflows/tests.yml. Baseline the current errors (a committed baseline
file or per-module overrides) so the gate is green on day one and tightens over
time. Pin nothing as strict yet. PR-flow; run mypy locally and show it green.
```

### 2. Python CI matrix 3.11 / 3.12 / 3.13 — S, hardware-free
**Why:** `pyproject` declares `requires-python >=3.11` but CI tests only 3.13, so
the 3.11/3.12 paths (e.g. the `audioop-lts` conditional) are never exercised.
**Where:** `.github/workflows/tests.yml`.
```
Add a Python version matrix (3.11, 3.12, 3.13) to the pytest job in
.github/workflows/tests.yml so the declared requires-python floor is actually
tested. Keep it hardware-free (the suite already excludes voice_eval). Confirm
the suite passes on all three (or document/skip any 3.11/3.12-specific gaps).
```

### 3. Lock Python deps in CI — S, hardware-free
**Why:** CI re-resolves Python deps fresh every run (`pip install -e .[full,dev]`);
Rust already uses `--locked`. `uv.lock` exists but CI doesn't consume it.
**Where:** `.github/workflows/tests.yml`.
```
Make CI install Python deps from the committed lock instead of re-resolving:
point the pytest job's install at `uv sync --locked` (uv.lock is committed and
hash-bearing). Keep behavior identical otherwise. Confirm CI green.
```

### 4. Rust + repo lint gates — S/M, hardware-free
**Why:** no `clippy`, `rustfmt --check`, or `pre-commit`; these are table-stakes
for external contributors.
**Where:** `.github/workflows/tests.yml` (rust job), new `.pre-commit-config.yaml`.
```
Add cargo clippy (`-D warnings`) + `cargo fmt --all -- --check` to the rust CI
job for all four crates, fixing any findings. Add a .pre-commit-config.yaml
running ruff + node --check on the static JS so contributors catch issues
locally. Optionally add cargo-deny/pip-audit as advisory. PR-flow; CI green.
```

### 5. SPDX license headers — S, hardware-free, mechanical
**Why:** 0 of ~804 source files carry an SPDX header — a legal-hygiene signal.
```
Add `# SPDX-License-Identifier: Apache-2.0` (and the matching `// ` form for
Rust/JS) to the top of every first-party source file (~804 .py/.rs/.sh/.js),
skipping vendored/generated files. Optionally add an fsfe/reuse-action CI check.
Purely textual + wide; verify the build/tests are unaffected.
```

### 6. `jasper-control` concurrency cap + `/state` cache — M (build hardware-free)
**Why:** unbounded `ThreadingHTTPServer` + per-request `asyncio.run()`/subprocess
fan-out, and `/state` + `/system/diagnostics` have no response caching — can spawn
unbounded threads/forks under load on a 1 GB Pi. **Verified still open** (0 hits for
`Semaphore`/`ThreadPool`/cache in `server.py`).
**Where:** `jasper/control/server.py`.
```
Bound jasper-control's ThreadingHTTPServer concurrency (cap request_queue_size +
gate handlers with a BoundedSemaphore or a bounded worker pool, fail-fast 429 on
overflow) and add short-TTL single-flight response caching to /state (~1-2s) and
/system/diagnostics (several-seconds TTL). Add a concurrency-limit test in
tests/test_control_server.py. Implementation + unit tests are hardware-free; note
that load behavior under real 1 GB pressure wants an on-device confirm.
```

### 7. `jasper-doctor` parallelization — M, hardware-free
**Why:** ~91 subprocess-bound checks run sequentially; the worst-case sum can
exceed the 30s `/system/diagnostics` ceiling → dashboard 502 under memory
pressure. The checks are an independent flat registry (safe to parallelize).
**Verified still open** (0 hits for `to_thread`/`gather` in the doctor).
**Where:** `jasper/cli/doctor/__init__.py`.
```
Parallelize jasper-doctor's subprocess-bound checks: wrap blocking checks in
asyncio.to_thread, run them under a bounded semaphore (~8-16, not unbounded fan-out
on a 1 GB Pi), with per-check wait_for timeouts, preserving result order. Serve a
background-cached snapshot to /system/diagnostics so the dashboard never blocks on
a live run. Add a test bounding doctor wall-clock. The checks are an independent
flat @doctor_check registry, so parallelism is safe.
```

### 8. Tag `v0.1.0` — owner action
`pyproject` is already `version = 0.1.0` and `CHANGELOG.md` exists; no `v0.1.0`
git tag yet. This is a release-ceremony decision, sequenced after the on-device
checks — not an agent task.

## Deferred by design (not "open")

- **TLS on the secret-bearing wizards** — PSKs/tokens still cross the LAN over
  plain HTTP (only `/correction/` has TLS). This is the documented, accepted
  trusted-LAN trade-off (see SECURITY.md), parity with router admin UIs. Revisit
  only if the threat model changes.

## Maintaining this doc

When an open item ships, move its bullet to **Done** with the PR number and
delete the agent prompt. When this whole list is empty, this doc itself becomes
historical — banner it and move it to `docs/historical/`.

Last verified: 2026-06-18
