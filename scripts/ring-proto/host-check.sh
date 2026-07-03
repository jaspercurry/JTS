#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# host-check.sh — laptop-side (macOS/Linux) gate for the Ring B prototype's
# portable core, before anything touches a Pi.
#
# Ring B has two portable-core pieces that are NOT this script's job to
# build (they belong to the parallel Rust/C track on this same branch):
#
#   rust/jasper-ring/          — pure crate, no ALSA dep. `cargo test` runs
#                                 anywhere (mirrors rust/jasper-clock).
#   c/jts-ring-ioplug/         — the ioplug .so is Pi-only (needs
#                                 alsa-lib headers + links libasound), but
#                                 its pure-logic core (jts_ring_shm.c) is
#                                 plain C11 with no ALSA include and is
#                                 compiled + exercised here via
#                                 test_ring_core.c.
#
# This script only CHECKS what already exists. If either piece hasn't
# landed yet on this branch, the corresponding section is SKIPPED (not
# failed) with a clear note — the two tracks develop in parallel and
# this gate must not block on ordering.
#
# What it runs, when present:
#   1. rust/jasper-ring: `cargo fmt --all -- --check` + `cargo test`
#      with the pinned 1.85.0 toolchain (falls back to `cargo` on PATH
#      with a warning if 1.85.0 isn't installed locally — CI is the
#      source of truth for the pinned version either way).
#   2. c/jts-ring-ioplug: `make test` (builds + runs test_ring_core, the
#      host-side proxy for the ioplug's shared-memory contract logic)
#      and `make bench` (builds ring_writer_bench, build-only — the
#      bench needs a real ring to write into, so it is not RUN here).
#      Both targets are the Makefile's own host-safe recipes (no ALSA
#      linkage — the ioplug glue in pcm_jts_ring.c, which does link
#      libasound, only builds on the Pi via build-on-pi.sh's `make
#      plugin`). Delegating to `make` here instead of hand-rolling a
#      `cc` invocation keeps this gate from drifting out of sync with
#      the Makefile's actual flags.
#
# Also prints where the Linux-only pieces get verified:
#   - `cargo test -p jasper-outputd` (config-parse / default-off /
#     shm_ring guard tests) needs the `alsa` crate's system dep
#     (libasound2-dev) and is Linux-only — CI's `rust` job covers it;
#     this script does not attempt it on macOS.
#   - the ioplug .so itself only builds on the Pi (build-on-pi.sh).
#
# Usage:
#   bash scripts/ring-proto/host-check.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RING_CRATE_DIR="${REPO_ROOT}/rust/jasper-ring"
IOPLUG_DIR="${REPO_ROOT}/c/jts-ring-ioplug"
RUST_TOOLCHAIN="${RING_PROTO_RUST_TOOLCHAIN:-1.85.0}"

pass_count=0
skip_count=0
fail_count=0

note() { printf '%s\n' "$*"; }
section() { printf '\n=== %s ===\n' "$*"; }
ok() { note "  OK   $*"; pass_count=$((pass_count + 1)); }
skip() { note "  SKIP $*"; skip_count=$((skip_count + 1)); }
fail() { note "  FAIL $*"; fail_count=$((fail_count + 1)); }

section "rust/jasper-ring (pure crate, no ALSA dep)"
if [[ ! -d "${RING_CRATE_DIR}" ]]; then
    skip "rust/jasper-ring not present yet on this branch — nothing to check"
else
    cargo_bin="cargo"
    if command -v rustup >/dev/null 2>&1 && rustup toolchain list 2>/dev/null \
        | grep -q "^${RUST_TOOLCHAIN}"; then
        cargo_bin="cargo +${RUST_TOOLCHAIN}"
    else
        note "  NOTE pinned toolchain ${RUST_TOOLCHAIN} not installed locally;" \
            "falling back to whatever 'cargo' resolves to. CI pins ${RUST_TOOLCHAIN}" \
            "and is the source of truth for the exact version."
    fi

    if ! command -v cargo >/dev/null 2>&1; then
        fail "cargo not found on PATH — cannot check rust/jasper-ring"
    else
        note "  running: (cd rust/jasper-ring && ${cargo_bin} fmt --all -- --check)"
        if (cd "${RING_CRATE_DIR}" && eval "${cargo_bin} fmt --all -- --check"); then
            ok "cargo fmt --check clean"
        else
            fail "cargo fmt --check found unformatted code — run" \
                "'(cd rust/jasper-ring && ${cargo_bin} fmt --all)' and re-commit"
        fi

        note "  running: (cd rust/jasper-ring && ${cargo_bin} test --locked)"
        test_status=0
        test_output="$(cd "${RING_CRATE_DIR}" && eval "${cargo_bin} test --locked" 2>&1)" \
            || test_status=$?
        printf '%s\n' "${test_output}" | sed 's/^/    /'
        if [[ "${test_status}" -eq 0 ]]; then
            ok "cargo test passed"
        elif printf '%s' "${test_output}" | grep -q -- "--locked was passed"; then
            fail "Cargo.lock missing/stale in rust/jasper-ring — run" \
                "'(cd rust/jasper-ring && ${cargo_bin} generate-lockfile)' and commit the lock file" \
                "(CI runs with --locked, same as this script)"
        else
            fail "cargo test failed in rust/jasper-ring"
        fi
    fi
fi

section "c/jts-ring-ioplug pure-logic core (host-compilable C11, via its own Makefile)"
if [[ ! -f "${IOPLUG_DIR}/Makefile" ]]; then
    skip "c/jts-ring-ioplug/Makefile not present yet on this branch"
elif ! command -v make >/dev/null 2>&1; then
    fail "'make' not found on PATH — cannot check the C core"
else
    # `make clean` first so a stale binary from a prior host-check.sh run
    # (or a manual `make plugin` attempt) cannot mask a real build failure.
    (cd "${IOPLUG_DIR}" && make clean) >/dev/null 2>&1 || true

    note "  running: (cd c/jts-ring-ioplug && make test)"
    test_output="$(cd "${IOPLUG_DIR}" && make test 2>&1)"
    test_status=$?
    printf '%s\n' "${test_output}" | sed 's/^/    /'
    if [[ "${test_status}" -eq 0 ]]; then
        ok "make test: test_ring_core built and passed"
    else
        fail "make test failed in c/jts-ring-ioplug — see output above"
    fi

    note "  running: (cd c/jts-ring-ioplug && make bench)  [build only, not run —" \
        "the bench writer needs a real ring to write into]"
    bench_output="$(cd "${IOPLUG_DIR}" && make bench 2>&1)"
    bench_status=$?
    printf '%s\n' "${bench_output}" | sed 's/^/    /'
    if [[ "${bench_status}" -eq 0 ]]; then
        ok "make bench: ring_writer_bench built cleanly"
    else
        fail "make bench failed in c/jts-ring-ioplug — see output above"
    fi

    (cd "${IOPLUG_DIR}" && make clean) >/dev/null 2>&1 || true
fi

section "Linux-only checks NOT run by this script"
note "  These need a Linux host with libasound2-dev (CI's 'rust' job, or a"
note "  Pi) and are intentionally out of scope here:"
note "    - cargo test -p jasper-outputd  (config-parse / default-off /"
note "      shm_ring guard tests — the 'alsa' crate needs libasound)"
note "    - the ioplug .so itself         (scripts/ring-proto/build-on-pi.sh,"
note "      Pi-only: aarch64 + alsa-lib headers)"

printf '\n=== Summary: %d ok, %d skipped, %d failed ===\n' \
    "${pass_count}" "${skip_count}" "${fail_count}"

if [[ "${fail_count}" -gt 0 ]]; then
    exit 1
fi
exit 0
