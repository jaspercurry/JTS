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
