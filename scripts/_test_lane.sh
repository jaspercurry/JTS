# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# shellcheck shell=bash
# Shared tool resolution for the executable test lanes (scripts/test-fast,
# scripts/test-merge). Sourced, never executed.
#
# Why this exists (issue #1836): both lanes resolved their interpreter with a
# bare `pytest` fallback and no existence check. In an agent worktree there is
# no `.venv` -- `git rev-parse --show-toplevel` resolves to the worktree root,
# not the main checkout -- so the fallback is the COMMON case there, not an
# exotic one. When the fallback also missed, `test-merge`'s `exec` left only
# bash's terse one-line "exec: pytest: not found" before the shell exited, and
# `test-fast` surfaced the same 127 through an arithmetic status check. Neither
# lane ever said which interpreter it had chosen, so a passing-looking
# transcript could not be distinguished from one where nothing ran.
#
# The contract here: resolve explicitly, announce the choice and its
# provenance, and refuse loudly with a named error when nothing can be
# resolved. Announcements and errors go to stderr so a lane's stdout stays
# pure test output for callers that capture it.
#
# Portability: macOS ships bash 3.2, so this file stays inside that dialect --
# no `${var^^}`, no associative arrays. The override's variable NAME is passed
# in rather than derived from the tool name for exactly that reason.

# resolve_lane_tool <lane> <tool> <override-var-name> <override-value>
#
# Echoes the resolved executable on stdout. Returns nonzero (after printing a
# named FATAL block to stderr) when the tool cannot be resolved. Callers must
# use `|| exit 1` rather than relying on `set -e` through the command
# substitution: an `exit` inside `$(...)` leaves only the subshell.
resolve_lane_tool() {
  local lane="$1"
  local tool="$2"
  local override_var="$3"
  local override="$4"
  local resolved="" origin=""

  if [[ -n "${override}" ]]; then
    resolved="${override}"
    origin="\$${override_var} override"
  elif [[ -x ".venv/bin/${tool}" ]]; then
    resolved=".venv/bin/${tool}"
    origin="repo .venv"
  else
    resolved="$(command -v "${tool}" 2>/dev/null || true)"
    origin="\$PATH (no ./.venv found)"
  fi

  if [[ -z "${resolved}" ]] || ! command -v "${resolved}" >/dev/null 2>&1; then
    cat >&2 <<EOF
${lane}: FATAL: could not resolve '${tool}' -- NO TESTS WERE RUN.
  looked for: \$${override_var} override, ./.venv/bin/${tool}, '${tool}' on \$PATH
  repo root:  $(pwd)
Do not read this run as a pass. If this is an agent worktree it has no .venv
of its own; create one (uv sync --all-groups) or point \$${override_var} at an
interpreter that has the project's dependencies. (issue #1836)
EOF
    return 1
  fi

  printf '==> %s: %s (%s)\n' "${tool}" "${resolved}" "${origin}" >&2
  printf '%s\n' "${resolved}"
}
