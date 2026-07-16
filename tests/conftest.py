# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pytest configuration.

Two pieces here, both load-bearing:

- A Python version guard (module-level) that fires before any collection
  so a wrong-version venv errors with a clear fix message instead of a
  TypeError deep in jasper/peering/ (which uses 3.10+ dataclass slots=).

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
