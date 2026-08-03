# Handoff: optional enhanced WebRTC AEC

Canonical operational reference for installing, updating, authorizing, and
reporting the optional WebRTC AEC3 v2 / BEST_A engine.

## The boundary

JTS has two software-AEC capabilities with intentionally different delivery
contracts:

| Capability | Delivery | Availability promise |
|---|---|---|
| `jasper_aec3._aec3` (Debian WebRTC audio-processing v1) | Built during every full-profile install | Mandatory software fallback; a normal deploy fails if it cannot be built and imported |
| `jasper_aec3._aec3_v2` (vendored WebRTC v2.1 / BEST_A) | Explicit opt-in background job | Optional enhancement; download, build, staleness, or activation failure never removes v1 or fails a core deploy |

This split is the fast-time-to-value compromise. A new Pi can stream, run the
voice path, measure, and use software AEC without waiting several minutes for
the vendored C++ build. Operators who need the deeper experimental AEC3 knobs
can install the enhancement later from the `/system/` Software surface.

Hardware chip AEC is a third path. When the applied input profile is
`xvf_chip_aec` or `xvf_chip_aec_testing`, the optional software enhancement is
reported as `not_needed`; the bridge remains the mic carrier but does not
instantiate either software canceller.

## Sources of truth

No single state file is allowed to mean both “wanted” and “working”:

| Fact | Owner |
|---|---|
| Exact WebRTC version, commit, archive URL, and SHA-256 | `jasper_aec3/enhanced-aec-source.env` |
| Durable operator intent | `/var/lib/jasper/enhanced-aec-intent.json` |
| Background job progress / last failure | `/var/lib/jasper/enhanced-aec-install.json` |
| Last verified activation proof | `/var/lib/jasper-enhanced-aec/installed.json` |
| Live installer activity | `jasper-enhanced-aec-install.service` |
| Stable state/API policy and runtime authorization | `jasper/enhanced_aec.py` |
| Privileged build and activation mechanics | `jasper/cli/enhanced_aec_install.py` |

The installed marker contains the exact build fingerprint, activated extension
filename, and SHA-256 of that extension. The fingerprint covers:

- the checked-in target manifest;
- all native-binding Python/C++ build inputs, including the exact-pinned
  PEP 517 build requirements in `jasper_aec3/pyproject.toml`;
- the Python major/minor version and extension ABI (`SOABI`).

A marker is proof only while all of those facts and the installed extension
digest still match.

Unknown or missing persisted schema versions fail closed. Intent then means
“not requested,” progress is ignored, and an unrecognized marker never
authorizes v2.

## Runtime authorization: marker last, import last

An `_aec3_v2*.so` file existing on disk is never sufficient to use it.
`jasper.enhanced_aec.runtime_v2_verified()` must first confirm:

1. a complete installed marker exists;
2. its fingerprint matches the currently installed JTS binding source and
   Python ABI;
3. its named extension exists; and
4. that extension's SHA-256 matches the marker.

Only then may `jasper-aec-bridge` access `jasper_aec3.Aec3V2`. The package
initialization is deliberately lazy: importing or constructing mandatory
`Aec3` does not import `_aec3_v2`, even if an orphan or stale native file is
discoverable. `jasper_aec3.HAS_V2` means only “a module is discoverable,” not
“runtime is authorized to use it.”

This closes the activation crash window. If power is lost after the atomic
extension replacement but before the marker write, the next bridge starts on
v1. It cannot accidentally execute the uncommitted extension.

## User and service flow

1. The versioned status is read from `GET /aec/enhanced-aec`.
2. The token-gated `POST /aec/enhanced-aec/install` atomically saves intent
   and a short-lived `queued` state.
3. `jasper-control` asks the existing restart broker to start the allowlisted
   root oneshot with `--no-block`. It never runs a compiler itself.
4. `jasper-enhanced-aec-install.service` performs the work as a bounded,
   low-priority, preferred-OOM-victim job.
5. Success schedules `jasper-aec-reconcile.service` without blocking. The
   reconciler restarts the bridge onto the newly verified capability. If this
   scheduling step fails, installation still succeeds and v2 is selected on
   the next ordinary bridge restart.

Persisting intent comes before starting the unit. If the broker handoff fails,
the API returns an error with `intent_saved=true`; boot or the next successful
deploy retries the request.

## Build and activation transaction

The installer does the expensive work outside the deploy/activation lock:

1. Under `/var/lib/jasper-enhanced-aec/.install.lock`, fingerprint and copy an
   exact `jasper_aec3` source snapshot. The lock and installed proof share a
   root-owned sibling directory, not group-writable `/var/lib/jasper`.
2. Release the lock.
3. Fetch the HTTPS archive with bounded size/time/retries and verify the
   checked-in SHA-256. A provenance-matched static build cache may be reused.
4. Run Meson and C++ compilation through `/usr/local/sbin/jasper-contained-build`.
   That helper sources the same RAM-aware job-count and low-memory temporary
   swap policy used by `install.sh`. For this unit, nested transient scopes are
   disabled: every compiler child stays in the outer oneshot cgroup so
   `systemctl stop` drains the entire tree.
5. Build a v2-only wheel with default PEP 517 isolation. Its build environment
   provisions exact-pinned setuptools and pybind11; it does not assume those
   packages exist in the runtime venv.
6. Extract only `_aec3_v2*.so` into job staging. In a clean subprocess, make
   both v1 and staged v2 process a real 10 ms zero frame.
7. Reacquire the shared lock and recompute the fingerprint against the live
   installed source. If a deploy changed it, refuse activation and record
   `stale`.
8. Copy into the package as a hidden staging file, hard-link any previous v2
   for rollback, atomically replace the extension, and probe active v1 + v2.
9. Roll back the extension on probe failure. On success, remove ABI-stale v2
   siblings and atomically write the verified marker **last**.

The outer oneshot is capped at 30 minutes. Individual network and subprocess
calls also have bounds; the source archive is capped at 64 MiB. Compiler phases
have a 14-minute process-group cap in the helper. The oneshot applies the
canonical low CPU/I/O weights, soft memory throttle, and preferred-OOM-victim
posture to the whole tree; the direct helper cap remains a second bound.

## Deploy, boot, and fast-moving `main`

A normal full-profile deploy builds only `JASPER_AEC3_BUILD_MODE=v1-only`.
It never downloads or compiles vendored WebRTC v2, and optional failure is not
part of the verified build-manifest transaction.

Deploy and optional activation share one short critical-section lock:

- the deploy holds it while replacing the binding source/package and building
  mandatory v1;
- the optional job holds it only while taking its source snapshot and while
  activating;
- the multi-minute optional build never holds it.

Deploy always kills an active optional oneshot's complete cgroup before
changing source or the live venv, then asks systemd to stop the dead unit
without waiting. It allows at most two seconds for the root-only package lock
to drain; a surviving lock means an unmanaged root process—not the managed
optional service—is concurrently mutating the native package, so deploy fails
closed. The final manifest change retries the still-durable enhancement
intent. Core install therefore never waits for optional compilation, overlaps
an optional pip/build job, or trades update correctness for an optional
capability.

After `install.sh` publishes `/var/lib/jasper/build.txt` as its final
successful mutation, `jasper-enhanced-aec-reconcile.path` uses `PathChanged=`
to retry durable intent asynchronously. It deliberately does not use
`PathExists=`, which would create a service/path restart loop. The installer
service is also enabled for one idempotent attempt on each boot. If already
current it returns immediately.

Upgrades from the former mandatory-v2 behavior preserve user expectations:
when an old `_aec3_v2` exists but no intent file does, the core installer
migrates that implicit preference into durable opt-in intent. Runtime still
uses v1 until the new marker/fingerprint/digest contract is satisfied.

## Status contract

The schema-v1 states are:

- `not_installed`
- `installing`
- `installed`
- `stale`
- `failed`
- `unavailable`
- `not_needed`

Live systemd activity is authoritative for a running installation. A fresh
`queued` file is treated as installing only for the short control-to-systemd
handoff grace; an old inactive `queued`/`building`/`verifying` state is
reported as interrupted/failed and remains retryable. A dead service can never
leave the UI saying “Installing” forever.

Each phase change emits one stable `event=enhanced_aec.install_phase` record;
terminal outcomes emit `event=enhanced_aec.install_terminal`. Only the
phase/result vocabulary enters structured fields. Bounded, redacted compiler
details stay in the technical status document rather than inflating or leaking
into journald event fields.

`engine` is a compatibility field describing the verified capability
(`v1`, `v2`, or `chip`), not an observation of what a currently running bridge
instantiated. `engine_semantics="verified_capability"` makes that explicit.
Bridge stats/logs remain authoritative for live runtime truth. UI copy should
say “Verified enhancement,” never “Current engine.”

The ordinary `GET /aec` hot polling path does not embed this status and does
not hash optional build inputs. Only the dedicated enhanced-AEC endpoint pays
that cost.

## Failure behavior

- Download/build/probe failure records `failed`; v1 remains active.
- Source changing during the job records `stale`; the next successful deploy
  manifest event retries durable intent.
- A crash before marker publication leaves an unauthorized orphan; v1 is used.
- A bad active probe restores the previous extension and publishes no marker.
- Failure to schedule the post-activation reconciler records
  `runtime_refresh=next_restart`; it does not roll back a verified install.
- Subprocess output is spooled outside process memory. Durable failures retain
  only a bounded, control-character-cleaned, credential-redacted tail and never
  persist the command argv.
- There is no disable/uninstall action yet. Intent is install-only.

## Redistribution boundary

On-device building avoids putting the resulting static-linked v2 extension
inside a bootstrap image, but it does **not** erase redistribution:
`WEBRTC_AEC3_ARCHIVE_URL` is a JTS-hosted mirror. Hosting/downloading that
archive redistributes source, and the exact archive must retain its upstream
license, patent grant, and bundled third-party notices.

The pinned v2.1 archive was inspected on 2026-07-27 and retains those notices;
see `LICENSE-third-party.md`. A future image or prebuilt extension still needs
automated notice closure for the exact linked/runtime dependency graph,
including system Abseil and OS packages. This optional installer narrows the
default-image surface; it is not a blanket binary-redistribution clearance.

## Verification

Hardware-free contracts live in:

- `tests/test_enhanced_aec.py` — state machine, fingerprint/digest gate, lazy
  native import, isolated build argv, source-change refusal, probe-before-marker,
  and rollback;
- `tests/test_enhanced_aec_systemd.py` — bounded root unit, boot/deploy retry,
  fail-soft enablement, shared containment helper, and v1-only core deploy;
- `tests/test_control_server.py`, `tests/test_control_aec_state.py`,
  `tests/test_restart_broker.py`, and `tests/test_polkit_jasper_control.py` —
  dedicated/token-gated API and narrow privilege allowlist;
- `tests/test_system_setup.py` plus the system UI module tests — progressively
  disclosed management surface and proxy contract.
- `tests/test_doctor_aec.py` — requested-only advisory health: an absent request,
  current install, chip-AEC bypass, or active background job is healthy;
  requested failure/staleness remains a warning with `/system/` retry guidance.

Still validate on a Pi before calling the feature production-proven:

1. clean full install reaches usable v1 without a v2 build;
2. opt-in survives a browser refresh and completes while audio remains usable;
3. v2 is selected after reconcile and survives reboot;
4. a deploy during the build produces `stale`, never activates old code, and
   retries after the final manifest change;
5. forced build/probe failure leaves the bridge operating on v1; and
6. the 1 GB path contains memory pressure and removes temporary build swap.

Last verified: 2026-07-27
