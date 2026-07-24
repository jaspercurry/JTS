# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pytest configuration.

Three pieces here, all load-bearing:

- A Python version guard (module-level) that fires before any collection
  so a wrong-version venv errors with a clear fix message instead of a
  TypeError deep in jasper/peering/ (which uses 3.10+ dataclass slots=).

- A module-level `socketserver` shutdown-latency shim (see below), which
  has to run before any fixture starts a throwaway HTTP server.

- An autouse os.environ snapshot/restore fixture so any test (or any
  production code under test) that writes to os.environ directly gets
  cleaned up at teardown. pytest's monkeypatch only rolls back changes
  *it* made via setenv/delenv; direct os.environ[...] = ... mutations
  (e.g. by jasper.env_load.load_env_files, which is what production
  ships) silently leak across tests. The leak's most-visible victim
  was tests/voice_eval/ running with OPENAI_API_KEY=wiz-key dragged in
  from a test_doctor case — see #254 / #255 / #256 for context (this
  fixture, which contains that leak, landed in #256).
"""
import os
import socketserver
import sys

import pytest

if sys.version_info < (3, 11):
    have = ".".join(str(n) for n in sys.version_info[:3])
    raise RuntimeError(
        f"JTS requires Python >=3.11; you're on {have}. "
        f"`requires-python` in pyproject.toml only enforces at "
        f"`pip install` time, not at venv creation, so a wrong-version "
        f"venv silently happens (most often on macOS where the default "
        f"`python3` is Apple's 3.9).\n\n"
        f"Rebuild (the extras carry the runtime packages the suite imports;\n"
        f"a bare `uv sync` / `.[dev]` installs only the dev tools):\n"
        f"  rm -rf .venv && uv sync --extra full --extra streambox   # recommended\n"
        f"  # or:\n"
        f"  rm -rf .venv && python3.13 -m venv .venv && \\\n"
        f"    .venv/bin/pip install -e '.[full,dev]'\n"
    )


# --- socketserver shutdown latency -------------------------------------
#
# 23 test files spin a throwaway ThreadingHTTPServer per test (51 call
# sites) to exercise the wizard/control HTTP surfaces. socketserver's
# `shutdown()` blocks until the `serve_forever()` loop notices the
# shutdown flag, and that loop only checks it once per `poll_interval` —
# which defaults to 0.5 s. So every one of those tests paid up to half a
# second of pure teardown sleep.
#
# Measured on this machine, one start/request/shutdown cycle:
#   default (0.5 s poll) : ~500 ms median  (max 502 ms)
#   poll_interval=0.01   :   ~11 ms median (max  13 ms)
#
# Quote the MEDIAN, not the mean: the mean wanders between runs (418-500 ms
# observed) because a fraction of cycles hit a race where the accept loop
# notices the shutdown flag without ever consulting poll_interval. That race
# is real and is why tests/test_server_shutdown_latency.py asserts the
# explicit-override property by interception rather than by timing.
#
# tests/test_control_server.py alone has 233 tests and spent ~90 s almost
# entirely here (~9 s after); the 23 affected files (51 call sites, exactly
# one already passing poll_interval) were together ~23% of LOCAL suite
# runtime, and a local full-suite A/B moved 483 s -> 346 s at -n 4.
#
# The CI gain is smaller, and the reason matters. Measured on the merge
# commit: py3.11 350 s, py3.13 358 s, py3.12 375 s, against a 428-431 s
# baseline. `ci` waits on ALL THREE matrix legs, so the gate improves by the
# SLOWEST leg: 375 s, i.e. about -12%, not the -28% the local A/B suggests.
#
# Fewer cores recover LESS of this, not more. On a 10-core box with -n 4
# there are idle cores, so a worker parked in select() is pure added wall
# time and removing it returns ~1:1. On a 4-vCPU runner with -n 4 a parked
# worker yields its core to a sibling's CPU-bound work, so part of the sleep
# was already hidden behind useful work and cannot be recovered. Do not
# re-derive this as "the sleep is identical regardless of CPU count" — that
# reasoning is backwards and predicts the wrong direction.
#
# Lowering the default is still a pure latency win: poll_interval only
# controls how often the accept loop wakes to re-check the flag, so it
# changes no request handling, no ordering, and no isolation. Tests that
# want a different cadence still pass `poll_interval=` explicitly, which
# continues to win because this only rebinds the DEFAULT
# (tests/test_control_server.py's serve_forever heartbeat test is the live
# example, and tests/test_server_shutdown_latency.py pins the forwarding).
#
# Deliberately scoped to the test suite. Production keeps the lazy 0.5 s
# poll (jasper/web/*_setup.py, jasper/control/server.py): there the trade
# is idle wakeups on a 1 GB Pi against a shutdown latency nobody can
# observe, and the stdlib default is the right call. asyncio servers use a
# different `serve_forever()` with no poll_interval and are untouched.
SERVER_POLL_INTERVAL_SEC = 0.01
_stdlib_serve_forever = socketserver.BaseServer.serve_forever


def _serve_forever_with_fast_shutdown(
    self: socketserver.BaseServer,
    poll_interval: float = SERVER_POLL_INTERVAL_SEC,
) -> None:
    """socketserver.BaseServer.serve_forever with a test-suite default.

    Identical to the stdlib method except that `poll_interval` defaults to
    SERVER_POLL_INTERVAL_SEC instead of 0.5 s. Resolves the original as a
    module global so a test can intercept the forwarding call.
    """
    return _stdlib_serve_forever(self, poll_interval)


socketserver.BaseServer.serve_forever = _serve_forever_with_fast_shutdown


@pytest.fixture(autouse=True)
def _isolate_environ():
    """Snapshot os.environ before each test, restore after.

    Covers the gap that monkeypatch leaves: production code under test
    can mutate os.environ directly (load_env_files is the canonical
    example — its job is exactly that), and monkeypatch only undoes
    its own setenv/delenv calls. Without this, mutations leak forward
    and break later tests' assumptions about a clean environment.
    """
    saved = os.environ.copy()
    try:
        yield
    finally:
        # Drop anything added during the test.
        for k in set(os.environ.keys()) - set(saved.keys()):
            del os.environ[k]
        # Restore anything modified or removed.
        for k, v in saved.items():
            if os.environ.get(k) != v:
                os.environ[k] = v


@pytest.fixture(autouse=True)
def _isolate_capture_entry_anchor(tmp_path_factory, monkeypatch):
    """Point the automatic-capture entry stash at a per-test temp file.

    jasper.active_speaker.capture_entry_anchor durably stashes the production
    CamillaDSP path under /var/lib/jasper by default; any test exercising the
    automatic capture loaders would otherwise write (or fail to write) real
    host state on a dev machine.
    """
    monkeypatch.setenv(
        "JASPER_ACTIVE_SPEAKER_CAPTURE_ENTRY_STATE",
        str(tmp_path_factory.mktemp("capture-entry") / "capture_entry.json"),
    )
