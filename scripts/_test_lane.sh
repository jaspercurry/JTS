# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# shellcheck shell=bash
# Shared tool resolution for the executable test lanes (scripts/test-fast,
# scripts/test-merge). Sourced, never executed.
#
# Resolves explicitly and refuses loudly when nothing can be resolved, rather
# than falling back to a bare command that may not exist (issue #1836).
# Announcements and errors go to stderr so a lane's stdout stays pure test
# output for callers that capture it.
#
# The FATAL block is printed with `printf`, a bash builtin, rather than a `cat`
# heredoc. The likeliest reason a tool could not be resolved is a mangled
# `$PATH` -- and an external `cat` would make the message vanish in exactly the
# case it matters most. For the same "the reader may only see a fragment"
# reason the block repeats its headline on the last line: under `| tail -3` the
# opening sentence is truncated away, and the surviving line still has to say
# that nothing ran.
#
# Portability: macOS ships bash 3.2, so this file stays inside that dialect --
# no `${var^^}`, no associative arrays. The override's variable NAME is passed
# in rather than derived from the tool name for exactly that reason; its value
# is then read indirectly with `${!name-}`, which bash 3.2 does support under
# `set -u`.

# resolve_lane_tool <lane> <tool> <override-var-name>
#
# Echoes the resolved executable on stdout. Returns nonzero (after printing a
# named FATAL block to stderr) when the tool cannot be resolved. `set -e` does
# abort the lane on that return -- an assignment whose value is a command
# substitution takes the substitution's status -- but the call sites keep an
# explicit `|| exit 1` anyway: `set -e` is silently suppressed in contexts a
# later edit could easily introduce (an `if`, a `&&` chain, a caller that
# sources a lane), and "no tests ran" must not become survivable by accident.
resolve_lane_tool() {
  local lane="$1"
  local tool="$2"
  local override_var="$3"
  local override="${!override_var-}"
  local resolved="" origin=""

  if [[ -n "${override}" ]]; then
    resolved="${override}"
    origin="\$${override_var} override"
  elif [[ -x ".venv/bin/${tool}" ]]; then
    resolved=".venv/bin/${tool}"
    origin="repo .venv"
  else
    resolved="$(command -v "${tool}" 2>/dev/null || true)"
    origin="\$PATH (not in ./.venv)"
  fi

  if [[ -z "${resolved}" ]] || ! command -v "${resolved}" >/dev/null 2>&1; then
    printf "%s: FATAL: could not resolve '%s' -- NO TESTS WERE RUN.\n" \
      "${lane}" "${tool}" >&2
    # Report only what was actually consulted. An override short-circuits the
    # search, so listing .venv/$PATH there would describe a search that never
    # happened and bury the one fact that fixes it: the rejected value.
    if [[ -n "${override}" ]]; then
      printf "  rejected:  \$%s=%s (not an executable)\n" \
        "${override_var}" "${override}" >&2
    else
      printf "  looked in: ./.venv/bin/%s, then '%s' on \$PATH\n" \
        "${tool}" "${tool}" >&2
    fi
    printf "  repo root: %s\n" "$(pwd)" >&2
    printf "Do not read this run as a pass. If this is an agent worktree it has no\n" >&2
    printf ".venv of its own; create one (uv sync --extra full --extra streambox)\n" >&2
    printf "or point \$%s at an interpreter that has the project's dependencies.\n" \
      "${override_var}" >&2
    printf "%s: FATAL: '%s' unresolved -- NO TESTS WERE RUN. (issue #1836)\n" \
      "${lane}" "${tool}" >&2
    return 1
  fi

  printf '==> %s: %s (%s)\n' "${tool}" "${resolved}" "${origin}" >&2
  printf '%s\n' "${resolved}"
}

# issue #1850: the functions below make a lane always print a single
# `==> <lane>: ...` verdict as the provably LAST line of stdout, via an EXIT
# trap that fires on every exit path (normal completion, an explicit `exit`,
# or `set -e` aborting mid-script) -- so a caller piping through `tail -N`
# still sees the real verdict even when the shell lacks `set -o pipefail` and
# `tail`'s own exit status would otherwise hide a failed lane's. lane_emit_verdict
# below owns the set of shapes that line can take.
#
# N counts passing EXECUTIONS across phases, not distinct tests: test-fast
# can run the same node id more than once (a changed test file matches both
# the changed-file-selection phase and the always-on guards), and each
# passing run adds to N. An honest count of what actually ran, not a claim
# about how many distinct tests exist.

# Counts pytest phases whose captured output actually carried a recognisable
# `-q` summary line. Owned entirely by this file: incremented in
# lane_pipe_pytest, read in lane_emit_verdict, and never touched by a lane.
# It is what stops `N passed` from being printed for a run in which nothing
# pytest-shaped was ever parsed -- `0 passed` reads like "nothing to run",
# which is the one thing a verdict line must never say by accident.
_lane_summary_seen=0

# lane_pipe_pytest <output-file> <command...>
#
# Runs a pytest invocation (or a stand-in in tests) through `tee` so the
# caller can read back its summary line afterwards, while still streaming it
# live to whoever is watching. PYTHONUNBUFFERED is required: once a Python
# process's stdout is not a tty (`tee` makes it not a tty), CPython switches
# from line- to block-buffered writes, turning a live-scrolling run into
# silence followed by a wall of dots. The exit status comes from
# `PIPESTATUS[0]` rather than relying on `pipefail` alone, so this helper
# behaves the same even if a future caller sources it without
# `set -o pipefail`.
#
# PIPESTATUS[0] (pytest), not [1] (tee): pytest's own exit code is the
# authoritative signal for whether the TESTS passed, and a `tee` failure is a
# different failure mode (infrastructure, not test outcome) -- though in
# practice a dead/unresolvable `tee` breaks the pipe pytest is writing to and
# pytest itself then exits nonzero too, so a failed tee still reads FAILED.
#
# `_lane_summary_seen` is counted here rather than in
# lane_extract_passed_count because this function runs in the CURRENT shell
# and can set a global; every lane_extract_passed_count call site is a
# command substitution (a subshell) whose variable writes die with it.
lane_pipe_pytest() {
  local output_file="$1"
  shift
  PYTHONUNBUFFERED=1 "$@" | tee "${output_file}"
  # Read before any other command: a simple command updates PIPESTATUS too,
  # so the pipeline's own statuses are readable only right here.
  local status="${PIPESTATUS[0]}"
  # The real pytest `-q` summary shapes -- a count plus an outcome word, or
  # the wordless "no tests ran". Deliberately broad: the question this
  # answers is "did a pytest actually report back?", not "did it pass",
  # which the exit status already settled. Under `set -e` + pipefail a
  # failing phase aborts the shell at the pipeline above and never reaches
  # here; that path prints FAILED and never consults this counter.
  if grep -Eq \
    '[0-9]+ (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)|no tests ran' \
    "${output_file}" 2>/dev/null; then
    _lane_summary_seen=$(( ${_lane_summary_seen:-0} + 1 ))
  fi
  return "${status}"
}

# lane_extract_passed_count <output-file>
#
# Pulls the passed count off pytest's own `-q` summary line ("12 passed in
# 1.2s", "3 passed, 1 skipped in 0.4s", ...). Echoes 0 when pytest reported no
# passes at all (a bare failure/error summary, or nothing collected) rather
# than erroring -- a phase that ran zero passing tests is not a lane failure
# by itself, and the caller has already decided that from the exit status.
lane_extract_passed_count() {
  local output_file="$1"
  grep -Eo '[0-9]+ passed' "${output_file}" 2>/dev/null | tail -1 | grep -Eo '[0-9]+' || echo 0
}

# lane_emit_verdict <lane-name> <exit-status>
#
# The tail of a lane's EXIT trap. Takes the pending exit status as an
# EXPLICIT parameter rather than reading `$?` itself: the trap handler runs a
# cleanup `rm` first, and by the time this function's last line runs `$?`
# would be the `rm`'s status, not the original failure that fired the trap.
# The caller must capture `$?` as ITS OWN first statement, before running
# anything else, and pass that value through. The running total is read from
# the fixed global `_lane_passed_total` rather than accepted as a third
# parameter: bash 3.2 (macOS's shipped version, and this file's portability
# floor) has no namerefs, so there is no portable way to pass a variable BY
# REFERENCE into a function, and each lane script has only one running total
# to report.
#
# The passed status is necessary but NOT sufficient, which is why the two
# globals below are consulted too: bash's terminating-signal handler still
# runs the EXIT trap, but hands it a STALE `$?` of 0 for SIGTERM and SIGHUP,
# so the trap can't distinguish a genuine pass from a killed lane by status
# alone. Per-signal behavior (bash 3.2.57), not one rule:
#
#   SIGTERM, SIGHUP     trap runs, `$?` is a stale zero. Only the
#                       `_lane_finished` marker can catch this -- a `>= 128`
#                       test cannot, because the status is 0.
#   SIGINT              trap runs with an honest 130. This is what the
#                       `>= 128` branch is for, and why it is tested FIRST:
#                       it is the one path that can name the signal.
#   SIGQUIT             no sentinel at all -- bash never runs the EXIT trap.
#                       An ABSENT verdict, which is not a false one.
#
# A second signal landing inside the trap's own execution window (its first
# few milliseconds) can still lose the line entirely; that race is not
# exempt from this fix and is regression-tested, not assumed away.
#
# So the success shape is gated on a POSITIVE "the lane reached its end"
# marker, with the signal test ahead of it for the honest-status case.
# Fail-closed by construction: a future lane that forgets to set
# `_lane_finished` prints INTERRUPTED, never a false pass. `>= 128` is
# bash's own encoding of child signal death, so a program that deliberately
# exits e.g. 143 is reported as a signal too -- an acceptable ambiguity,
# since both readings are "not a verdict" and neither is a success shape.
lane_emit_verdict() {
  local lane="$1"
  local status="$2"
  if [[ "${status}" -ge 128 ]]; then
    printf '==> %s: INTERRUPTED (signal %s) -- NO VERDICT\n' \
      "${lane}" "$(( status - 128 ))"
  elif [[ "${status}" -ne 0 ]]; then
    printf '==> %s: FAILED\n' "${lane}"
  elif [[ "${_lane_finished:-false}" != true ]]; then
    # The observed case. The zero is untrustworthy, so there is no signal
    # number to report and none is invented.
    printf '==> %s: INTERRUPTED -- NO VERDICT\n' "${lane}"
  elif [[ "${_lane_summary_seen:-0}" -eq 0 ]]; then
    printf '==> %s: NO VERDICT (no pytest summary parsed)\n' "${lane}"
  else
    printf '==> %s: %s passed\n' "${lane}" "${_lane_passed_total:-0}"
  fi
}
