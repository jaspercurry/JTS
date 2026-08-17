# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free coverage for the E0 headless capture experiment.

The experiment ships its own offline check suite
(``experiments/e0-capture/test_e0_capture.py``), which its ``--selftest``
mode runs on a lab Mac. ``testpaths = ["tests"]`` means pytest never
collects that file where it lives, so this module imports it and
parametrizes over its ``TESTS`` list -- one pytest node per check, no
second copy of any check -- and adds the pins that only matter inside the
repo: where the tool resolves the repo root from, that the identity it
hardcodes still passes the live validator, and that no product code
depends on it.
"""

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments" / "e0-capture"

# The promoted kit: six files plus the README that names them.
EXPECTED_FILES = {
    "README.md",
    "PROTOCOL.md",
    "e0_capture.py",
    "test_e0_capture.py",
    "preflight_noaudio.py",
    "reset_run.py",
    "overnight_runs.sh",
}

# The experiment's own offline checks, in its own order. Listed rather than
# discovered so that adding one there without adding it here fails loudly
# (test_every_selftest_check_runs_in_this_lane) instead of silently not
# running in CI.
SELFTEST_CHECKS = [
    "test_content_key_b64url_round_trip",
    "test_encrypt_then_pi_decrypt_round_trip",
    "test_authenticated_phone_event_round_trip",
    "test_fragment_parsing",
    "test_spec_mac_round_trip_and_wire_helpers",
    "test_session_start_body_carries_the_tier",
    "test_capture_page_identity_passes_the_pi_validator",
    "test_wav_encoding_matches_the_page",
]


@pytest.fixture(scope="module")
def selftest():
    """The experiment's own check module, loaded exactly as it loads itself.

    It inserts its own directory on ``sys.path`` and imports ``e0_capture``
    at module scope, so reaching the client through ``selftest.e0`` (rather
    than loading the script a second time here) keeps one module instance
    for every assertion below.
    """
    spec = importlib.util.spec_from_file_location(
        "e0_selftest", EXPERIMENT / "test_e0_capture.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_experiment_layout_is_the_promoted_kit() -> None:
    """Every promoted file is present, and nothing has quietly joined them.

    Scoped to source suffixes so a local run's WAVs, JSON summaries, or
    ``__pycache__`` never fail this -- a new *source* file still has to be
    pinned here (or named in the README) on purpose.
    """
    assert EXPERIMENT.is_dir()
    sources = {
        path.name
        for path in EXPERIMENT.iterdir()
        if path.is_file() and path.suffix in {".py", ".md", ".sh"}
    }
    assert sources == EXPECTED_FILES

    readme = (EXPERIMENT / "README.md").read_text()
    for name in EXPECTED_FILES - {"README.md"}:
        assert name in readme, f"README does not mention {name}"


def test_repo_root_resolves_from_the_scripts_own_location(selftest) -> None:
    """Issue #2636's first revival edit, pinned against the real tree.

    The July build hardcoded an absolute path to an agent worktree that was
    later deleted. The fix derives everything from ``__file__``; this fails
    if the directory ever moves to another depth, which is the failure a
    lab Mac would otherwise discover hours later.
    """
    e0 = selftest.e0

    assert e0.REPO_ROOT == ROOT
    assert e0.WT == ROOT / "jasper" / "capture_relay"
    assert (e0.WT / "crypto.py").is_file()
    assert (e0.WT / "integrity.py").is_file()


def test_recorded_output_never_lands_in_the_tracked_experiment_dir(selftest) -> None:
    """The corpus default writes to the gitignored captures/ tree.

    Before promotion the default was the script's own directory, which is
    now tracked source -- a lab run would have dirtied the working tree
    with WAVs.
    """
    corpus = selftest.e0.DEFAULT_CORPUS_DIR

    assert corpus == ROOT / "captures" / "e0-corpus"
    assert EXPERIMENT not in corpus.parents


def test_client_tier_vocabulary_matches_the_live_product(selftest) -> None:
    """The client holds its own copy of the tier names so a typo fails on
    the Mac instead of after a round-trip. A copy is a drift site: this
    pins it to the owner of that vocabulary.
    """
    from jasper.active_speaker.crossover_v2_flow import DEFAULT_TIER, TIERS

    e0 = selftest.e0

    assert e0.TIERS == TIERS
    # The remote tier is reachable only by naming it: the flow's default is
    # something else, which is exactly why the mint body cannot be "{}".
    assert e0.TIER_REMOTE in TIERS
    assert DEFAULT_TIER != e0.TIER_REMOTE
    assert json.loads(e0.session_start_body(e0.TIER_REMOTE)) == {"tier": "remote"}


def test_live_validator_accepts_the_shipped_capture_page_identity(selftest) -> None:
    """The identity the client advertises still satisfies the Pi's validator.

    The experiment's own version of this check silently skips when the
    ``jasper`` package cannot be imported (a lab Mac may not have its
    dependencies). In the repo it always can, so this one imports the
    validator normally and cannot skip -- with a positive control, because
    an acceptance that accepts anything proves nothing.
    """
    from jasper.capture_relay.session import (
        CapturePageIncompatible,
        validate_capture_page,
    )
    from jasper.capture_relay.spec import CaptureSpec

    identity = selftest.e0.DEFAULT_CAPTURE_PAGE_IDENTITY
    spec = CaptureSpec(
        kind="crossover_sweep", duration_ms=20000, pre_roll_ms=0, post_roll_ms=0
    )

    assert validate_capture_page(identity, spec) == identity

    stale = {**identity, "supported_capture_protocol_versions": [1, 2]}
    with pytest.raises(CapturePageIncompatible):
        validate_capture_page(stale, spec)


class _SubprocessSpy:
    """Records every attempted launch and blocks it from ever executing.

    Blocking matters as much as recording: the control below deliberately
    lets the recording path believe ``sox`` is installed, and it must still
    be impossible for this lane to open the machine's input device.
    """

    def __init__(self) -> None:
        self.launches: list[tuple[str, ...]] = []

    def __call__(self, cmd, *_args, **_kwargs):
        self.launches.append(tuple(str(part) for part in cmd))
        raise FileNotFoundError("subprocess launch blocked by the test lane")


def _install_subprocess_spy(selftest, monkeypatch) -> _SubprocessSpy:
    spy = _SubprocessSpy()
    monkeypatch.setattr(selftest.e0.subprocess, "run", spy)
    monkeypatch.setattr(selftest.e0.subprocess, "Popen", spy)
    return spy


@pytest.mark.parametrize("check", SELFTEST_CHECKS)
def test_offline_selftest_check(selftest, monkeypatch, check: str) -> None:
    """Run each of the experiment's own offline checks as its own node.

    ``sox`` is hidden from this lane on purpose: the last check ends with a
    best-effort 0.2 s recording from the machine's default input, and a
    hardware-free lane must not open an input device. The tool's own
    ``--selftest`` keeps that probe.

    Hiding the binary is the mechanism; ZERO launches is the property, so
    the property is what this asserts. A spy stands in for ``subprocess``
    throughout, so no check can reach a shell by some other route this file
    did not anticipate either.
    """
    monkeypatch.setattr(selftest.shutil, "which", lambda _name: None)
    spy = _install_subprocess_spy(selftest, monkeypatch)

    getattr(selftest, check)()

    assert spy.launches == []


def test_without_the_suppression_the_lane_would_launch_sox(
    selftest, monkeypatch
) -> None:
    """Positive control for the assertion above.

    Without it, ``spy.launches == []`` would also pass if the recording path
    had quietly stopped existing -- an empty list proves nothing on its own.
    Here ``sox`` is made to look installed, so the check proceeds to the
    launch the lane normally suppresses; the spy records it and raises
    instead of executing, so no input device opens even in the control.
    """
    monkeypatch.setattr(selftest.shutil, "which", lambda _name: "/usr/bin/sox")
    spy = _install_subprocess_spy(selftest, monkeypatch)

    selftest.test_wav_encoding_matches_the_page()

    assert [cmd[0] for cmd in spy.launches] == ["sox"]
    assert "-t" in spy.launches[0]


def test_every_selftest_check_runs_in_this_lane(selftest) -> None:
    """A check added to the experiment but not to ``SELFTEST_CHECKS`` would
    otherwise never run in CI, and nothing would say so.
    """
    assert [check.__name__ for check in selftest.TESTS] == SELFTEST_CHECKS


def test_the_sox_probe_this_lane_suppresses_is_a_real_target(selftest) -> None:
    """Positive control for the monkeypatch above.

    It patches ``selftest.shutil``; if the experiment ever reached ``sox``
    through some other name, the patch would silently stop suppressing the
    recording and this lane would open an input device again with nothing
    saying so.
    """
    assert selftest.shutil is shutil
    assert "shutil.which(\"sox\")" in (EXPERIMENT / "test_e0_capture.py").read_text()


def test_no_product_code_depends_on_the_experiment() -> None:
    """This is a laptop-side tool: nothing ships, installs, or imports it.

    Unlike experiments/usb-turntable, which install stages onto the Pi for a
    hot-plug hook, e0 has no deploy surface at all -- so the expected match
    set is empty. An empty result proves nothing on its own, so the same
    scan runs against a marker the product certainly does contain.
    """
    files = [ROOT / "pyproject.toml"]
    for directory in (ROOT / "deploy", ROOT / "jasper"):
        files.extend(path for path in directory.rglob("*") if path.is_file())

    def scan(markers: tuple[str, ...]) -> set[str]:
        return {
            path.relative_to(ROOT).as_posix()
            for path in files
            if any(
                marker
                in path.relative_to(ROOT).as_posix() + path.read_text(errors="ignore")
                for marker in markers
            )
        }

    assert scan(("e0-capture", "e0_capture", "preflight_noaudio", "reset_run")) == set()
    # Positive control: the scan can find something.
    assert scan(("capture_relay",))


def test_readme_keeps_the_experimental_and_risk_boundaries() -> None:
    readme = " ".join((EXPERIMENT / "README.md").read_text().split())

    # The ruling this promotion implements, in the words that carry it.
    assert "The browser flow stays first-class" in readme
    assert "retired only when the owner says so" in readme
    assert "EXPERIMENTAL" in readme
    assert "#2636" in readme
    assert "active-speaker-tuning-layers-design.md" in readme
    # The disclosure that survives until a hardware series replaces it.
    assert "not been proven on hardware" in readme
    # The shape of the risk, which is silent degradation and NOT a loud
    # refusal. An earlier version of this file pinned the inverted claim
    # ("the first begin_capture fails LOUDLY"), which would have sent an
    # operator looking for an error the product deliberately never raises:
    # `setup` is read behind a bare isinstance check, `setup_validation` is
    # never armed for a crossover sweep, and an unresolved calibration is
    # annotated and analyzed rather than refused.
    assert "A drifted `setup` payload is not refused" in readme
    assert "completed round of uncalibrated data" in readme
    # The operator's only signal, and how to read its absence.
    assert "crossover_v2_uncalibrated_capture" in readme
    assert "jasper-correction-web" in readme
    # The one command a reader must be able to copy.
    assert "e0_capture.py --selftest" in readme
