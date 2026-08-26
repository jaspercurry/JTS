# Historical: the incidents behind the install/update transaction

Archived 2026-08-26 from `docs/HANDOFF-install-update-transaction.md`. These
are the forensic records that produced the current design; the mechanisms they
argue for are in that doc, and the decisions are
[ADR-0172](../adr/0172-full-a-b-install-generations-stay-deferred.md),
[ADR-0173](../adr/0173-post-deploy-health-is-surfaced-never-gating.md), and
[ADR-0174](../adr/0174-install-window-oom-kills-are-surfaced-not-gated.md).
Nothing here is current operational truth.

## The manifest written first (problem #4)

`write_build_manifest` used to run at the *start* of `install_jasper` — before
the OOM-prone WebRTC build and the Rust builds. On jts2 the build OOM-killed
mid-run, `set -e` aborted `install.sh`, and the manifest had *already* recorded
the new SHA. The box advertised a successful update it had not done, and the
next deploy's direction-guard treated it as up-to-date.

The fix made the manifest the final mutation in both `main()` paths, so that
under `set -euo pipefail` reaching that line is itself the proof that every
build/install/migration step above succeeded. A knock-on: the landing-page
`app.css` cache-bust used to read the manifest mid-run, which after the move
would have read the *prior* SHA and shipped a stale cache key — hence
`resolve_build_sha_short`.

## Collateral OOM kills, unreported (problems #2/#5)

A build OOM-killed nginx *and* jasper-voice on a 1 GB Pi and the tooling exited
silently. The operator discovered it from the speaker, not the transcript. This
produced `report_oom_collateral` and the two-field victim parsing (cgroup
`task_memcg=` for the unit, `comm` for the process name and for build tools in
a transient ssh scope).

## Cargo freshness and the stale-binary lie (bit twice)

Cargo's freshness check is mtime-based: a unit recompiles only when a source
file is *newer* than the fingerprint from the last compile. The old staging
chain preserved mtimes end to end (`rsync -a` laptop → checkout →
`/var/cache/<name>-build`), so a changed source whose checkout mtime predated
the cache's last build landed "in the past", cargo declared the crate
**Fresh**, and the install shipped the stale binary while the manifest honestly
said `ok`. The box lied at the *binary* layer, below both honest-claim layers.

- **2026-07-02** — stale `jasper-usbsink-audio`: endpoints 404ing against code
  that had shipped.
- **2026-07-10** — stale `jasper-outputd`: a merged journal-spam fix never went
  live. `cargo build -v` in the poisoned cache reported `Fresh` in 0.03 s while
  the staged source contained the fix. Three same-day deploys: the first
  compiled pre-fix source at 17:23; the later two staged the fixed source with
  a preserved 17:14 mtime.

The fix is content-based staging (`--checksum`, `-rlpgoD` = `-a` minus `-t`)
plus a one-time `RUST_BUILD_CACHE_FORMAT` purge to heal already-poisoned
caches — which cost one slow deploy per box when it shipped.

## Workstream C, the no-mic crash loop (problem #6)

Before 2026-06-21 (#924, `f662622c`), a speaker with no mic attached crash-
looped `jasper-voice` until systemd's start limit, and post-deploy doctor read
that as a failure. The AEC reconciler now writes
`/var/lib/jasper/voice-input-absent` and the unit's `ConditionPathExists=!…`
makes systemd skip the start cleanly, so the unit reads `inactive` rather than
`failed`/`activating`.
