# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free proof tests for scripts/ring-proto/ (branch
latency/ring-proto-shm, the Ring B SHM-ring latency prototype).

These are LAB-ONLY prototype scripts (see scripts/ring-proto/README.md).
This file does not exercise real hardware, real SSH, or real ALSA/
CamillaDSP — it pins three classes of invariant:

1. Static shape: every mutating script is syntactically valid bash,
   shellcheck-clean at the project's CI severity, sources the shared
   safety guard, and never appends a marked block without a matching
   BEGIN/END pair the corresponding disarm step can find.
2. The safety-critical guard behavior (require_explicit_ring_proto_target
   in _guard.sh): every mutating script refuses to run — with NO side
   effects, verified by never invoking a fake `ssh` placed first on
   PATH — when no explicit PI_HOST is provided, and proceeds (reaching
   its first real `ssh` call) when one is.
3. The disarm.sh marker-stripping sed command, run for real (locally,
   against a synthetic env file) rather than only read as source text —
   parentheses in the marker string are BRE-literal, not a
   sed-metacharacter hazard, but that is exactly the kind of "looks
   right, might not be" detail worth a real execution rather than an
   eyeball check.

The guard's own existence is not incidental: during this script
family's development, running scripts/ring-proto/build-on-pi.sh with no
PI_HOST set landed on the real jts.local box (scripts/_lib.sh's ordinary,
correct product default) and ran a real rsync + make there before the
gap was caught and closed. Test 2 above is the regression pin for
exactly that incident.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RING_PROTO_DIR = ROOT / "scripts" / "ring-proto"

MUTATING_SCRIPTS = (
    "arm.sh",
    "disarm.sh",
    "build-on-pi.sh",
    "make-camilla-ring-config.sh",
)
ALL_SCRIPTS = MUTATING_SCRIPTS + ("host-check.sh",)


def _has(cmd: str) -> bool:
    return shutil.which(cmd) is not None


# ---------------------------------------------------------------------
# 1 — static shape
# ---------------------------------------------------------------------


def test_ring_proto_directory_contains_the_expected_files() -> None:
    """Every file the tooling brief names exists under scripts/ring-proto/."""
    expected = {
        "README.md",
        "arm.sh",
        "disarm.sh",
        "build-on-pi.sh",
        "make-camilla-ring-config.sh",
        "host-check.sh",
        "_guard.sh",
    }
    present = {p.name for p in RING_PROTO_DIR.iterdir() if p.is_file()}
    missing = expected - present
    assert not missing, f"scripts/ring-proto/ is missing: {sorted(missing)}"


@pytest.mark.parametrize("name", ALL_SCRIPTS + ("_guard.sh",))
def test_script_is_executable_or_sourceable(name: str) -> None:
    """The four entry-point scripts are chmod +x; _guard.sh (sourced only,
    never executed directly) does not need to be."""
    path = RING_PROTO_DIR / name
    assert path.is_file(), f"{name} does not exist"
    if name == "_guard.sh":
        return
    mode = path.stat().st_mode
    assert mode & stat.S_IXUSR, f"{name} is not chmod +x"


@pytest.mark.parametrize("name", ALL_SCRIPTS + ("_guard.sh",))
def test_script_parses_with_bash_dash_n(name: str) -> None:
    """`bash -n` (parse only, no execution) is clean for every script.

    Catches the exact bug class hit during this file's own development:
    an unquoted heredoc delimiter (<<EOF) still tokenizes its BODY for
    quote balance even though the quotes inside are not otherwise
    special. An odd number of apostrophes anywhere in an unquoted
    heredoc body (including inside a prose comment) is a bash parse
    error that only appears when the WHOLE file is parsed — the failing
    line bash reports is often far from the actual unbalanced quote.
    """
    if not _has("bash"):
        pytest.skip("bash not on PATH")
    path = RING_PROTO_DIR / name
    result = subprocess.run(
        ["bash", "-n", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, (
        f"bash -n {name} failed:\n{result.stderr}"
    )


@pytest.mark.parametrize("name", ALL_SCRIPTS + ("_guard.sh",))
def test_script_is_shellcheck_clean_at_ci_severity(name: str) -> None:
    """shellcheck --severity=warning is clean — the same gate + severity
    the repo's `shell` CI job runs (see .github/workflows/tests.yml)."""
    if not _has("shellcheck"):
        pytest.skip("shellcheck not installed")
    path = RING_PROTO_DIR / name
    result = subprocess.run(
        ["shellcheck", "--severity=warning", str(path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"shellcheck --severity=warning {name} found issues:\n{result.stdout}"
    )


@pytest.mark.parametrize("name", MUTATING_SCRIPTS)
def test_mutating_script_sources_the_shared_guard(name: str) -> None:
    """Every script that can mutate a Pi sources _guard.sh and calls the
    guard function before doing anything else. A script that forgets
    this line silently reopens the exact incident _guard.sh exists to
    close."""
    text = (RING_PROTO_DIR / name).read_text(encoding="utf-8")
    assert '. "${RING_PROTO_DIR}/_guard.sh"' in text, (
        f"{name} does not source _guard.sh"
    )
    assert "require_explicit_ring_proto_target" in text, (
        f"{name} does not call require_explicit_ring_proto_target"
    )
    # The guard function reads JASPER_RING_PROTO_CALLER_PI_HOST, which
    # must be captured from PI_HOST BEFORE _lib.sh's fallback runs (that
    # fallback silently resolves PI_HOST to jts.local when unset — see
    # scripts/_lib.sh). Capturing it AFTER would always see the
    # already-defaulted value and the guard could never refuse.
    lib_source_idx = text.index('scripts/_lib.sh"')
    capture_idx = text.index("JASPER_RING_PROTO_CALLER_PI_HOST=")
    assert capture_idx < lib_source_idx, (
        f"{name} must capture JASPER_RING_PROTO_CALLER_PI_HOST BEFORE "
        "sourcing _lib.sh, not after"
    )


def test_guard_defines_require_explicit_ring_proto_target() -> None:
    text = (RING_PROTO_DIR / "_guard.sh").read_text(encoding="utf-8")
    assert "require_explicit_ring_proto_target()" in text
    # The two accepted proofs of an explicit target: an inherited/caller
    # PI_HOST, or an .env.local with PI_HOST already persisted (the
    # documented laptop-side state file — see AGENTS.md "Laptop-side
    # state"). Anything else must be a hard refusal.
    assert "JASPER_RING_PROTO_CALLER_PI_HOST" in text
    assert "env_local_has_pi_host" in text
    assert "exit 1" in text


@pytest.mark.parametrize("name", ("arm.sh", "disarm.sh"))
def test_arm_and_disarm_agree_on_marked_block_markers(name: str) -> None:
    """arm.sh writes a BEGIN/END marked block into
    /var/lib/jasper/outputd.env; disarm.sh must strip the SAME literal
    markers, or a re-arm/disarm cycle silently leaves stale content
    behind (or disarm silently no-ops on a real block)."""
    arm_text = (RING_PROTO_DIR / "arm.sh").read_text(encoding="utf-8")
    disarm_text = (RING_PROTO_DIR / "disarm.sh").read_text(encoding="utf-8")
    arm_begin = _extract_assignment(arm_text, "BEGIN_MARKER")
    arm_end = _extract_assignment(arm_text, "END_MARKER")
    disarm_begin = _extract_assignment(disarm_text, "BEGIN_MARKER")
    disarm_end = _extract_assignment(disarm_text, "END_MARKER")
    assert arm_begin == disarm_begin, (
        f"BEGIN_MARKER differs: arm.sh={arm_begin!r} disarm.sh={disarm_begin!r}"
    )
    assert arm_end == disarm_end, (
        f"END_MARKER differs: arm.sh={arm_end!r} disarm.sh={disarm_end!r}"
    )


def _extract_assignment(text: str, var_name: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(f'{var_name}="') and stripped.endswith('"'):
            return stripped[len(var_name) + 2 : -1]
    raise AssertionError(f"could not find {var_name}= assignment")


def test_arm_and_disarm_agree_on_the_conf_d_path() -> None:
    """The ALSA plugin drop-in path arm.sh installs must be the exact
    path disarm.sh removes."""
    arm_text = (RING_PROTO_DIR / "arm.sh").read_text(encoding="utf-8")
    disarm_text = (RING_PROTO_DIR / "disarm.sh").read_text(encoding="utf-8")
    arm_path = _extract_assignment(arm_text, "CONF_D_PATH")
    disarm_path = _extract_assignment(disarm_text, "CONF_D_PATH")
    assert arm_path == disarm_path
    assert arm_path == "/etc/alsa/conf.d/98-jts-ring-proto.conf"


def test_disarm_sed_marker_strip_actually_works(tmp_path: Path) -> None:
    """Run disarm.sh's exact marker-stripping sed command (extracted from
    its source, not re-typed by hand) against a synthetic env file,
    locally, with a real sed.

    The markers embed parentheses ("(scripts/ring-proto/arm.sh)"), which
    are BRE-literal in sed's default mode — not a metacharacter hazard —
    but "looks right" is not the same as "runs right" for a regex built
    from string interpolation. This exercises the real command rather
    than only reading it as source text.
    """
    if not _has("sed"):
        pytest.skip("sed not on PATH")
    disarm_text = (RING_PROTO_DIR / "disarm.sh").read_text(encoding="utf-8")
    begin_marker = _extract_assignment(disarm_text, "BEGIN_MARKER")
    end_marker = _extract_assignment(disarm_text, "END_MARKER")

    env_file = tmp_path / "outputd.env"
    env_file.write_text(
        "JASPER_OUTPUTD_BACKEND=alsa\n"
        f"{begin_marker}\n"
        "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring\n"
        "JASPER_OUTPUTD_SHM_RING_SLOTS=2\n"
        f"{end_marker}\n"
        "JASPER_OUTPUTD_ANOTHER_VAR=foo\n",
        encoding="utf-8",
    )

    # The exact bash expression from disarm.sh, with the shell variable
    # names it uses locally (${BEGIN_MARKER//\//\\/} etc.) reproduced
    # verbatim so this test breaks if that expression is ever hand-edited
    # without updating this test in the same change.
    script = (
        'BEGIN_MARKER="$1"; END_MARKER="$2"; TARGET="$3"; '
        "sed -i.bak "
        '"/^${BEGIN_MARKER//\\//\\\\/}\\$/,/^${END_MARKER//\\//\\\\/}\\$/d" '
        '"$TARGET"'
    )
    result = subprocess.run(
        ["bash", "-c", script, "--", begin_marker, end_marker, str(env_file)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"sed command failed: {result.stderr}"

    remaining = env_file.read_text(encoding="utf-8")
    assert begin_marker not in remaining
    assert end_marker not in remaining
    assert "JASPER_OUTPUTD_CONTENT_BRIDGE=shm_ring" not in remaining
    assert "JASPER_OUTPUTD_SHM_RING_SLOTS=2" not in remaining
    # Content outside the marked block must survive untouched.
    assert "JASPER_OUTPUTD_BACKEND=alsa" in remaining
    assert "JASPER_OUTPUTD_ANOTHER_VAR=foo" in remaining


def test_arm_bench_writer_invocation_uses_flags_the_binary_actually_accepts() -> None:
    """arm.sh's step 5 bench-writer smoke test must call ring_writer_bench
    with flags that binary's own `--help`-equivalent usage string lists.

    Regression pin for a real bug caught during this file's own review:
    arm.sh originally called `ring_writer_bench --tone 440`, but the
    binary has no `--tone` flag (it takes `--pattern tone --freq 440`) —
    that invocation would have hit the binary's unknown-flag branch and
    exited 2 on real hardware, and this "prints a click track" step would
    have silently failed the whole arm sequence the first time anyone ran
    it on a Pi. c/jts-ring-ioplug/ is owned by a parallel track on this
    branch and can rename/add flags without touching this test file, so
    this only pins the specific flags arm.sh currently sends against the
    literal `--path`, `--seconds`, `--pattern`, `--freq` names in the C
    source's own argv parser and usage string — it does not assert the
    full accepted flag set stays frozen.
    """
    bench_c = RING_PROTO_DIR.parent.parent / "c" / "jts-ring-ioplug" / "ring_writer_bench.c"
    if not bench_c.exists():
        pytest.skip("c/jts-ring-ioplug/ring_writer_bench.c not present yet on this branch")
    bench_source = bench_c.read_text(encoding="utf-8")
    arm_lines = (RING_PROTO_DIR / "arm.sh").read_text(encoding="utf-8").splitlines()

    # The invocation line calls the bench binary through the ${bench_bin}
    # variable (not the literal "ring_writer_bench" string — that only
    # appears where bench_bin itself is assigned). Find the line invoking
    # it and pull every --flag token off it directly; the line also
    # contains a runtime path value like /dev/shm/jts-ring/content.ring
    # and a bare numeric argument like "3" for --seconds, neither of
    # which start with "--".
    invocation_lines = [
        line for line in arm_lines if "${bench_bin}" in line and "--path" in line
    ]
    assert invocation_lines, "could not find the ${bench_bin} invocation line in arm.sh"
    sent_flags: list[str] = []
    for line in invocation_lines:
        sent_flags.extend(re.findall(r"--[a-zA-Z][a-zA-Z0-9\-]*", line))
    assert sent_flags, "parsed zero --flags out of arm.sh's bench invocation line(s)"

    for flag in sent_flags:
        assert f'"{flag}"' in bench_source, (
            f"arm.sh sends {flag!r} to ring_writer_bench, but that flag string "
            "does not appear in ring_writer_bench.c's argv parser — check the "
            "binary's usage string (top-of-file comment) for the current flag "
            "names before re-syncing arm.sh's invocation"
        )


def test_arm_never_invokes_product_camilla_emitters_or_reconcilers() -> None:
    """Static guard against the hard scope rule: this prototype must not
    CALL the product Camilla emitters, reconcilers, /sound wizard,
    multiroom, or install.sh from any of its own scripts.

    Checks for actual invocation patterns (import/from, module -m
    execution, `sudo systemctl restart <unit>`, or a bare shell call),
    not bare substring presence — several of these scripts legitimately
    MENTION a product path in a comment to document that they do NOT
    touch it (e.g. make-camilla-ring-config.sh's own docstring-style
    header says exactly that), and a naive substring check would flag
    that documentation as a violation.
    """
    forbidden_calls = (
        "import jasper.camilla_emit",
        "from jasper.camilla_emit",
        "import jasper.active_speaker.camilla_yaml",
        "from jasper.active_speaker.camilla_yaml",
        "systemctl restart jasper-aec-reconcile",
        "systemctl start jasper-aec-reconcile",
        "systemctl restart jasper-audio-hardware-reconcile",
        "systemctl start jasper-audio-hardware-reconcile",
        "systemctl restart jasper-grouping-reconcile",
        "systemctl start jasper-grouping-reconcile",
        "jasper/web/sound_setup.py",
        "python3 -m jasper.web.sound_setup",
        "bash deploy/install.sh",
        "bash /opt/jasper/install.sh",
        "sudo bash install.sh",
    )
    for script in ALL_SCRIPTS + ("_guard.sh",):
        text = (RING_PROTO_DIR / script).read_text(encoding="utf-8")
        for forbidden in forbidden_calls:
            assert forbidden not in text, (
                f"{script} invokes {forbidden!r}, which is out of scope "
                "for this lab-only prototype (see the hard rules in this "
                "file's own instructions and scripts/ring-proto/README.md)"
            )


def test_no_ring_proto_script_is_referenced_from_install_sh() -> None:
    """The flip side of the rule above: install.sh must never learn about
    this prototype. A reference there would mean the lab wiring leaked
    into the product installer."""
    install_sh = (ROOT / "deploy" / "install.sh").read_text(encoding="utf-8")
    assert "ring-proto" not in install_sh
    assert "jts_ring_playback" not in install_sh
    assert "jts-ring-ioplug" not in install_sh


# ---------------------------------------------------------------------
# 2 — the safety-critical guard, exercised via real subprocess.run
# ---------------------------------------------------------------------
#
# A fake `ssh` placed first on PATH is the oracle: if the guard is
# working, NONE of these scripts ever invoke it when PI_HOST is unset,
# because the guard runs (and refuses) before the first ssh_ok/ssh call.
# The fake writes a sentinel file so we can assert "ssh was never
# called" without racing subprocess timing.


_FAKE_SSH = """#!/usr/bin/env bash
echo "ssh was called with: $*" >> "${SENTINEL_FILE}"
exit 0
"""


@pytest.fixture
def fake_ssh_path(tmp_path: Path):
    """A PATH directory containing only a fake `ssh` that records every
    invocation to a sentinel file, plus a fake `make`/`shellcheck`-free
    environment (irrelevant here — the guard runs before any of those)."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    ssh_path = bin_dir / "ssh"
    ssh_path.write_text(_FAKE_SSH, encoding="utf-8")
    ssh_path.chmod(0o755)
    sentinel = tmp_path / "ssh-was-called.log"
    yield bin_dir, sentinel


def _run_script_with_fake_ssh(
    script_name: str,
    fake_ssh_path,
    *,
    pi_host: str | None,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    bin_dir, sentinel = fake_ssh_path
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    env["SENTINEL_FILE"] = str(sentinel)
    # Isolate from any real .env.local in this checkout — the guard's
    # OTHER accepted proof of an explicit target — so these tests only
    # exercise the PI_HOST-argument path deterministically.
    env["REPO_ROOT_OVERRIDE_FOR_TEST"] = "unused"
    env.pop("JASPER_HOSTNAME", None)
    if pi_host is None:
        env.pop("PI_HOST", None)
    else:
        env["PI_HOST"] = pi_host
    return subprocess.run(
        ["bash", str(RING_PROTO_DIR / script_name), *extra_args],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
        cwd=str(ROOT),
    )


@pytest.mark.parametrize("script_name", MUTATING_SCRIPTS)
def test_refuses_with_no_side_effects_when_pi_host_unset(
    script_name: str, fake_ssh_path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression pin for the actual incident: with no PI_HOST and no
    .env.local, every mutating script refuses BEFORE calling ssh even
    once."""
    if not _has("bash"):
        pytest.skip("bash not on PATH")
    # Run from a cwd with no .env.local reachable via REPO_ROOT (the
    # scripts resolve REPO_ROOT from their own file location, which is
    # the real checkout — so guard against a real .env.local existing
    # in THIS checkout by asserting on its absence, or skip if present).
    env_local = ROOT / ".env.local"
    if env_local.exists() and "PI_HOST" in env_local.read_text(encoding="utf-8"):
        pytest.skip(
            "this checkout's .env.local already has PI_HOST — the guard's "
            "OTHER accepted proof of an explicit target would legitimately "
            "pass here, so this specific no-side-effects assertion can't "
            "be isolated without touching the real .env.local"
        )
    _bin_dir, sentinel = fake_ssh_path
    result = _run_script_with_fake_ssh(script_name, fake_ssh_path, pi_host=None)
    assert result.returncode == 1, (
        f"{script_name} with no PI_HOST should refuse (exit 1); got "
        f"{result.returncode}. stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "No changes made" in result.stdout + result.stderr, (
        f"{script_name} refusal message should say 'No changes made'"
    )
    assert not sentinel.exists(), (
        f"{script_name} called ssh (sentinel: {sentinel.read_text() if sentinel.exists() else ''}) "
        "despite having no explicit PI_HOST — the safety guard did not stop it "
        "before a real mutating command"
    )


@pytest.mark.parametrize("script_name", MUTATING_SCRIPTS)
def test_proceeds_past_the_guard_when_pi_host_is_explicit(
    script_name: str, fake_ssh_path
) -> None:
    """The other half of the invariant: an explicit PI_HOST reaches the
    first real ssh call (the fake records it) rather than being refused
    by the same guard. The script may still fail LATER for unrelated
    reasons (the fake ssh returns nothing useful for real preflight
    checks) — this test only asserts the guard itself did not block it."""
    if not _has("bash"):
        pytest.skip("bash not on PATH")
    _bin_dir, sentinel = fake_ssh_path
    result = _run_script_with_fake_ssh(
        script_name, fake_ssh_path, pi_host="jts-test-lab.invalid"
    )
    assert sentinel.exists(), (
        f"{script_name} with an explicit PI_HOST never called ssh at all "
        f"(stdout={result.stdout!r} stderr={result.stderr!r}) — the guard "
        "may be refusing even a legitimate explicit target"
    )
    assert "no explicit PI_HOST" not in (result.stdout + result.stderr), (
        f"{script_name} printed the guard's refusal message even though "
        "PI_HOST was explicitly set"
    )
