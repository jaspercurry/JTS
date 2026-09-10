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
import contextlib
import io
import itertools
import logging
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


# --- subprocesses must import the tree under test ----------------------
#
# 141 test files spawn a subprocess that imports `jasper` (a scripts/ entry
# point or a console script); 9 of them pass an explicit PYTHONPATH. The
# other ~132 inherit the parent env, and `jasper` then resolves through the
# venv's EDITABLE install — a .pth finder pinned to the checkout the venv was
# built in. That is the right tree in a plain clone and the WRONG one in a
# git worktree, where the venv belongs to the main checkout and the main
# checkout sits on whatever branch its own agent left it on.
#
# The failure mode that matters is not the loud one. On 2026-09-01 a worktree
# lane reported 5 failures in test_jasper_pipe_probe_script because the main
# checkout was mid-refactor on another branch and had no `analytic_signal` —
# an ImportError, so it was noticed. Had that branch merely CHANGED the
# function's behavior instead of removing it, those tests would have PASSED
# while validating code that is not the code under test, in a lane whose
# whole job is to gate a merge.
#
# Setting it here rather than in the lane scripts covers every entry point
# (scripts/test-fast, scripts/test-merge, a bare pytest, an IDE runner) and
# every spawn shape, and prepending keeps an operator's own PYTHONPATH.
# No-op in CI and in a plain clone, where this path is already the one the
# editable install points at.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["PYTHONPATH"] = os.pathsep.join(
    [_REPO_ROOT, *(p for p in [os.environ.get("PYTHONPATH", "")] if p)]
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


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--regenerate-goldens",
        action="store_true",
        default=False,
        help=(
            "rewrite golden fixtures from the code under test, then fail the "
            "golden tests so the run is visibly a regeneration; review with "
            "`git diff` and re-run without the flag"
        ),
    )


@pytest.fixture
def regenerate_goldens(request: pytest.FixtureRequest) -> bool:
    """True under ``--regenerate-goldens``: write fixtures instead of comparing."""
    return bool(request.config.getoption("--regenerate-goldens"))


@pytest.fixture(autouse=True)
def _reset_doctor_evidence():
    """The doctor's per-run evidence memo must not leak between tests. Only
    touched when a test already imported the doctor, so the fixture costs
    nothing elsewhere."""
    import sys

    module = sys.modules.get("jasper.cli.doctor._evidence")
    if module is not None:
        module.evidence.reset()
    yield
    module = sys.modules.get("jasper.cli.doctor._evidence")
    if module is not None:
        module.evidence.reset()


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
def _isolate_tts_wire_width_cache():
    """Clear the per-process assistant-width answer before AND after each test.

    ``jasper.tts_playout.tts_wire_is_wide`` is ``lru_cache``'d on purpose: the two
    callers that ask (the playout's quantizer and the daemon's earcon bake) must
    get ONE answer, and in production the daemon is restarted by anything that
    could change it. In a test process there is no restart, so the cache is a
    channel between tests — including ACROSS FILES, which no per-file inline
    clear can close. A test that monkeypatches the box declaration to wide and
    warms the cache would otherwise leave every later test quantizing and baking
    at spine scale.

    Both sides matter. Clearing AFTER stops a test from handing its answer
    forward; clearing BEFORE means a test does not inherit one from a file that
    forgot to clean up, so this fixture is not itself a thing to remember.

    IT MUST NOT IMPORT ``jasper.tts_playout``, and that is a CI constraint rather
    than a preference. This fixture is autouse, so its body runs at the setup of
    EVERY test in the repo — including the ``python-policy`` job, which installs
    only the ``fast-landing`` dependency group and therefore has no numpy, while
    ``jasper/tts_playout.py`` imports numpy at module level. An unconditional import
    here errored all 93 of that job's tests at setup, and because ``pytest-matrix``
    runs ``needs: python-policy``, one fixture took the entire Python matrix down
    with it.

    Consulting ``sys.modules`` instead is not merely lighter — it is the more
    precise statement of the invariant. The cache can only hold a stale answer
    if something already imported the module, so an absent module means there is
    nothing to clear, in a minimal environment exactly as in a full one.
    """

    def _clear() -> None:
        module = sys.modules.get("jasper.tts_playout")
        if module is not None:
            module.tts_wire_is_wide.cache_clear()

    _clear()
    try:
        yield
    finally:
        _clear()


_host_state_dirs = itertools.count()

# (env var, file name) — reader module + why absent is the hermetic baseline.
_HOST_STATE_FILES = (
    # startup_hold: /run/jasper-active-speaker is unwritable on a test host; absent = no hold.
    ("JASPER_ACTIVE_SPEAKER_STARTUP_HOLD_MARKER", "staged-startup-hold"),
    # capture_entry_anchor durably stashes the prod CamillaDSP path under /var/lib/jasper;
    # absent avoids writing (or failing to write) real host state.
    ("JASPER_ACTIVE_SPEAKER_CAPTURE_ENTRY_STATE", "capture_entry.json"),
    # session_measurement_volume_db's reference half; absent falls back to the codified
    # MEASUREMENT_REFERENCE_VOLUME_DB.
    ("JASPER_ACTIVE_SPEAKER_SEAT_LEVEL_REFERENCE_STATE", "seat_level_reference.json"),
    # identity.reader.resolve_hostname's JASPER_HOSTNAME source; absent falls back to the
    # env-or-DEFAULT_HOSTNAME baseline.
    ("JASPER_IDENTITY_FILE", "identity.env"),
    # baseline_profile._measured_level_trims prefers this over guided captures; absent falls
    # back to the guided captures and then the datasheet estimate.
    ("JASPER_ACTIVE_SPEAKER_DRIVER_BASE_TRIM_STATE", "driver_base_trim.json"),
    # output_hardware.load_state defaults to /run/jasper-output-hardware/...; absent means
    # load_state() returns None, the hermetic baseline.
    ("JASPER_OUTPUT_HARDWARE_STATE_PATH", "output_hardware.json"),
    # per-test absent marker so a host's real tripped boot-loop guard can't leak into drift
    # tests.
    ("JASPER_BOOTLOOP_MARKER_FILE", "state.json"),
)


@pytest.fixture(autouse=True)
def _isolate_host_state_paths(tmp_path_factory, monkeypatch):
    """One absent per-test path per host-state file the product reads.

    Named by a process counter, not ``tmp_path_factory.mktemp``: pytest's numbered-dir
    scan lists the whole basetemp on every call, which at seven calls per test over ~7k
    tests per xdist worker cost more than the tests themselves.
    """
    root = tmp_path_factory.getbasetemp() / "host-state"
    root.mkdir(exist_ok=True)
    scratch = root / str(next(_host_state_dirs))
    scratch.mkdir()
    for env_var, name in _HOST_STATE_FILES:
        monkeypatch.setenv(env_var, str(scratch / name))


@pytest.fixture(autouse=True)
def _isolate_canonical_target_provider():
    """Reset the process-global canonical main_volume target around each test.

    ``jasper.camilla.set_canonical_target_db_provider`` is per process by
    design: a graph swap's duck release runs on ad-hoc ``primary_controller()``
    instances that no ``VolumeCoordinator`` ever sees, so the target is
    registered once per daemon rather than passed down. A test process has no
    such boundary. Any test that enters a daemon's ``main()`` installs a real
    provider for the rest of that xdist worker —
    ``tests/test_web_correction_setup.py``'s
    ``test_main_wires_idle_tracker_to_capture_entry_restore`` calls
    ``correction_setup.main()`` — and every later duck release answers THAT
    provider's level instead of its own fixture's. Observed as two failures
    sharing ``percent_to_db(50)`` = −25.2525 under full-suite ordering while
    every targeted subset stayed green.

    Both sides matter, as with the width cache above: clearing BEFORE stops a
    test inheriting a provider, restoring AFTER stops it handing one forward.
    """
    from jasper import camilla

    saved = camilla._canonical_target_db_provider
    camilla.set_canonical_target_db_provider(None)
    try:
        yield
    finally:
        camilla.set_canonical_target_db_provider(saved)


@pytest.fixture(autouse=True)
def _isolate_process_volume_owner():
    """Reset the process-global fader owner around each test.

    The sibling of the fixture above, for the same reason and the same
    processes: ``install_env_canonical_target_provider`` registers both, so any
    test that enters one of those daemons' ``main()`` installs a real owner for
    the rest of that xdist worker. An owner is worse to inherit than a target
    reader — it carries a CLAIM LEDGER, so a leaked one would let one test's
    held claim decide what a later test's fader write is allowed to do.

    Both sides matter, as above: clearing BEFORE stops a test inheriting an
    owner, restoring AFTER stops it handing one forward.
    """
    from jasper import volume_owner

    saved = volume_owner.volume_owner()
    volume_owner.install_volume_owner(None)
    try:
        yield
    finally:
        volume_owner.install_volume_owner(saved)


@pytest.fixture(autouse=True)
def _isolate_jasper_logger_level():
    """Restore the process-global Jasper logger level after each test."""
    logger = logging.getLogger("jasper")
    level = logger.level
    try:
        yield
    finally:
        logger.setLevel(level)


def seat_process_volume_owner(monkeypatch, set_fader_db, get_fader_db) -> None:
    """Seat a real ``VolumeOwner`` over one (set, get) fader pair.

    Through the module global rather than ``install_volume_owner``, so
    monkeypatch puts the process back at teardown. The FADER stays the
    caller's — what a suite drives the owner over is its subject, so only the
    seating is shared.
    """
    import jasper.volume_owner as volume_owner_module

    monkeypatch.setattr(
        volume_owner_module,
        "_process_owner",
        volume_owner_module.VolumeOwner(
            set_fader_db=set_fader_db, get_fader_db=get_fader_db,
        ),
    )


@pytest.fixture
def a_process_with_a_volume_owner(monkeypatch):
    """Stand up the precondition every crossover-v2 session has in production.

    After W5-c1 the session claims the fader through
    :class:`~jasper.volume_owner.VolumeOwner`, and ``bind_v2_engine_seams``
    REFUSES when no owner is installed rather than minting a second authority
    over one fader. ``jasper.web.__main__`` installs one before serving, so a
    process without one is a registration defect — but a test module driving
    that wiring has to stand the same precondition up, or it exercises the
    refusal instead of its subject.

    **Opted into by name, never autouse.** A suite that wants to pin the
    no-owner refusal itself must not have an owner seated underneath it, so
    modules declare ``pytestmark = pytest.mark.usefixtures(...)`` rather than
    getting one whether they want it or not.
    """
    fader = {"db": -20.0}

    async def _set(db: float) -> bool:
        fader["db"] = float(db)
        return True

    async def _get() -> float:
        return fader["db"]

    seat_process_volume_owner(monkeypatch, _set, _get)


@pytest.fixture
def no_real_pi_paths(tmp_path, monkeypatch):
    """Point ``round_inputs``' three on-Pi SSOT defaults at absent temp files.

    A LIVE session bundle resolves its flow state, design draft and applied
    profile to real ``/var/lib/jasper`` paths, so the two CLI suites that read
    one -- ``jasper-crossover-prescriber`` and ``jasper-round-views`` -- would
    otherwise answer differently on a box that is a speaker. Absent here is the
    hermetic baseline; a test that wants one of the three populated re-points
    the same attribute at a file it wrote.

    **Opted into by name, never autouse.** Only those two suites resolve a live
    bundle, so modules declare ``pytestmark = pytest.mark.usefixtures(...)``
    rather than the whole tree paying for it.
    """
    from jasper.active_speaker.crossover_v2 import round_inputs

    for name in (
        "STATE_DEFAULT_PATH",
        "DRIVERS_DEFAULT_PATH",
        "APPLIED_PROFILE_DEFAULT_PATH",
    ):
        monkeypatch.setattr(round_inputs, name, tmp_path / f"unset-{name}.json")


@contextlib.contextmanager
def bare_root_logger():
    """The root and ``jasper`` loggers as a fresh interpreter has them, both
    restored on exit. Yields the root logger. With pytest's caplog handler
    still on root, ``basicConfig`` no-ops and ``set_console_debug`` finds the
    wrong handler, so every test of the logging bootstrap starts from here."""
    root, jasper = logging.getLogger(), logging.getLogger("jasper")
    saved = (root.handlers[:], root.level, jasper.handlers[:], jasper.level)
    root.handlers[:], jasper.handlers[:] = [], []
    jasper.setLevel(logging.NOTSET)
    try:
        yield root
    finally:
        root.handlers[:], root.level, jasper.handlers[:], jasper.level = saved


@pytest.fixture
def logging_sandbox(monkeypatch):
    """A deterministic single 'journal' StreamHandler on a clean root, yielded
    so tests can assert its level."""
    from jasper import flight_recorder as fr

    with bare_root_logger() as root:
        console = logging.StreamHandler(io.StringIO())
        root.addHandler(console)
        root.setLevel(logging.INFO)
        monkeypatch.setattr(fr, "_ring", None, raising=False)
        yield console
