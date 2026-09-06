# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for bash helpers in deploy/install.sh.

The helpers can be sourced cleanly because install.sh's `main` call
is guarded by `if [[ "${BASH_SOURCE[0]}" == "${0:-}" ]]`. Tests source
the file and invoke individual functions.

Coverage started with `_compute_min_free_kbytes` (Concern 9 of the
staff-eng review) and now also pins upgrade cleanup behavior. These
bash helpers are small, but easy to regress
because they sit on the deploy path.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.install_surface import installer_text


_INSTALL_SH = Path(__file__).parent.parent / "deploy" / "install.sh"
REPO_ROOT = _INSTALL_SH.parent.parent
_INSTALL_LIB_DIR = Path(__file__).parent.parent / "deploy" / "lib" / "install"
_RENDERERS_LIB = _INSTALL_LIB_DIR / "renderers.sh"
_MODEL_DOWNLOADS = Path(__file__).parent.parent / "jasper" / "model_downloads.py"
_ENV_EXAMPLE = Path(__file__).parent.parent / ".env.example"


def _installer_shell_texts() -> dict[Path, str]:
    """install.sh plus the deploy/lib/install/*.sh libs it sources.

    Invariant-style tests (bounded curl flags, no unpinned git
    fetches, …) must keep covering function groups that the
    install.sh decomposition moved into sourced libs."""
    paths = [_INSTALL_SH, *sorted(_INSTALL_LIB_DIR.glob("*.sh"))]
    assert _RENDERERS_LIB in paths
    return {p: p.read_text(encoding="utf-8") for p in paths}


def _compute_min_free_kbytes(memtotal_kb: int) -> int:
    """Invoke the bash helper via subprocess; return its integer
    output. Discards any stdout from the sourcing step so we only
    capture the helper's output."""
    # Then the bare invocation of _compute_min_free_kbytes goes to the
    # outer stdout, which we capture.
    result = subprocess.run(
        ["bash", "-c",
         f"source {_INSTALL_SH} >/dev/null && _compute_min_free_kbytes {memtotal_kb}"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"helper failed (rc={result.returncode}): {result.stderr}"
        )
    return int(result.stdout.strip())


def _cpp_compile_jobs(memtotal_kb: int, ncpu: int) -> int:
    """Invoke the canonical C++ job-budget policy used by the runtime helper."""
    result = subprocess.run(
        ["bash", "-c",
         f"source {_BUILD_SANDBOX_LIB} >/dev/null && "
         f"_ram_bounded_jobs {memtotal_kb} {ncpu} "
         '"${BUILD_SANDBOX_KB_PER_JOB_CPP}"'],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"helper failed (rc={result.returncode}): {result.stderr}"
        )
    return int(result.stdout.strip())


def _render_install_asound_template(
    tmp_path: Path,
    *,
    output_dac_id: str,
    output_dac_card: str,
    output_dac_recognized: str = "1",
) -> tuple[str, str]:
    source = tmp_path / "asoundrc.jasper"
    dest = tmp_path / "asoundrc.rendered"
    source.write_text(
        "__OUTPUTD_DAC_PCM_BLOCK__\n"
        "__OUTPUTD_DAC_CTL_BLOCK__\n",
        encoding="utf-8",
    )
    script = (
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
        f"OUTPUT_DAC_CARD={shlex.quote(output_dac_card)} && "
        f"OUTPUT_DAC_ID={shlex.quote(output_dac_id)} && "
        f"OUTPUT_DAC_RECOGNIZED={shlex.quote(output_dac_recognized)} && "
        f"jasper_asound_render_template {shlex.quote(str(source))} {shlex.quote(str(dest))}"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"render helper failed (rc={result.returncode}): {result.stderr}"
        )
    return dest.read_text(encoding="utf-8"), result.stderr


def _assert_no_empty_alsa_card(rendered: str) -> None:
    assert not re.search(r"(?m)^\s*card\s*$", rendered)
    assert not re.search(r"\bcard\s+}", rendered)


def _run_install_helper(
    helper_name: str,
    tmp_path: Path,
) -> subprocess.CompletedProcess[str]:
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(exist_ok=True)
    script = (
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
        f"ENV_DIR={shlex.quote(str(env_dir))} && "
        f"STATE_DIR={shlex.quote(str(state_dir))} && "
        f"{helper_name}"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )


def _run_speaker_name_seed(
    tmp_path: Path,
    *,
    deploy_hostname: str | None = None,
    saved_hostname: str | None = None,
    os_hostname: str = "raspberrypi",
    fail_write: bool = False,
    fail_chmod: bool = False,
    publish_behavior: str = "normal",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the creation-only installer seed against scratch state."""

    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir(parents=True, exist_ok=True)
    if saved_hostname is not None:
        (env_dir / "jasper.env").write_text(
            f'JASPER_HOSTNAME="{saved_hostname}"\n',
            encoding="utf-8",
        )
    commands = [
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null",
        f"ENV_DIR={shlex.quote(str(env_dir))}",
        f"STATE_DIR={shlex.quote(str(state_dir))}",
        "unset JASPER_HOSTNAME JASPER_SYSTEM_PYTHON",
        f"hostname() {{ printf '%s\\n' {shlex.quote(os_hostname)}; }}",
    ]
    if deploy_hostname is not None:
        commands.append(f"export JASPER_HOSTNAME={shlex.quote(deploy_hostname)}")
    if fail_write:
        commands.append("printf() { builtin printf partial; return 1; }")
    if fail_chmod:
        commands.append(
            "chmod() { [[ \"$2\" == *'.speaker_name.env.seed.'* ]] "
            "&& return 1; command chmod \"$@\"; }"
        )
    if publish_behavior != "normal":
        real_python = shlex.quote(sys.executable)
        publish_actions = {
            "wizard_file": (
                "builtin printf '%s' 'JASPER_SPEAKER_NAME=\"Wizard Save\"' > \"$3\""
            ),
            "directory": 'mkdir "$3"',
            "directory_symlink": (
                'mkdir "${3}.target"; command ln -s "${3}.target" "$3"'
            ),
            "fail": "return 23",
        }
        action = publish_actions[publish_behavior]
        commands.append(
            "python3() { "
            "if [[ \"$2\" == *'.speaker_name.env.seed.'* ]]; then "
            f"{action}; "
            "fi; "
            f"command {real_python} \"$@\"; }}"
        )
    commands.append("seed_speaker_name_env")
    result = subprocess.run(
        ["bash", "-c", " && ".join(commands)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result, state_dir / "speaker_name.env"


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [
        ("jts.local", 'JASPER_SPEAKER_NAME="JTS"\n'),
        ("jts4.local", 'JASPER_SPEAKER_NAME="JTS4"\n'),
        ("kitchen.local", 'JASPER_SPEAKER_NAME="Kitchen"\n'),
    ],
)
def test_fresh_speaker_name_seed_uses_deploy_hostname(
    tmp_path: Path,
    hostname: str,
    expected: str,
):
    result, state_file = _run_speaker_name_seed(
        tmp_path,
        deploy_hostname=hostname,
    )
    assert result.returncode == 0, result.stderr
    assert state_file.read_text(encoding="utf-8") == expected


def test_speaker_name_seed_falls_back_to_saved_then_os_hostname(tmp_path: Path):
    saved_result, saved_file = _run_speaker_name_seed(
        tmp_path / "saved",
        saved_hostname="den.local",
        os_hostname="ignored",
    )
    assert saved_result.returncode == 0, saved_result.stderr
    assert saved_file.read_text(encoding="utf-8") == (
        'JASPER_SPEAKER_NAME="Den"\n'
    )

    os_result, os_file = _run_speaker_name_seed(
        tmp_path / "os",
        os_hostname="workshop",
    )
    assert os_result.returncode == 0, os_result.stderr
    assert os_file.read_text(encoding="utf-8") == (
        'JASPER_SPEAKER_NAME="Workshop"\n'
    )


def test_speaker_name_seed_preserves_existing_file_byte_for_byte(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_file = state_dir / "speaker_name.env"
    original = b'# household formatting\n  JASPER_SPEAKER_NAME = "My Kitchen"'
    state_file.write_bytes(original)

    result, resolved = _run_speaker_name_seed(
        tmp_path,
        deploy_hostname="jts99.local",
    )
    assert result.returncode == 0, result.stderr
    assert resolved == state_file
    assert state_file.read_bytes() == original


def test_speaker_name_seed_write_failure_is_clean_and_retryable(tmp_path: Path):
    failed, state_file = _run_speaker_name_seed(
        tmp_path,
        deploy_hostname="jts4.local",
        fail_write=True,
    )
    assert failed.returncode != 0
    assert "ERROR: could not write fresh speaker name" in failed.stderr
    assert not state_file.exists()
    assert list(state_file.parent.glob(".speaker_name.env.seed.*")) == []

    retried, retry_file = _run_speaker_name_seed(
        tmp_path,
        deploy_hostname="jts4.local",
    )
    assert retried.returncode == 0, retried.stderr
    assert retry_file.read_text(encoding="utf-8") == (
        'JASPER_SPEAKER_NAME="JTS4"\n'
    )
    assert list(retry_file.parent.glob(".speaker_name.env.seed.*")) == []


def test_speaker_name_seed_does_not_clobber_concurrent_wizard_save(tmp_path: Path):
    result, state_file = _run_speaker_name_seed(
        tmp_path,
        deploy_hostname="jts4.local",
        publish_behavior="wizard_file",
    )
    assert result.returncode == 0, result.stderr
    assert state_file.read_bytes() == b'JASPER_SPEAKER_NAME="Wizard Save"'
    assert list(state_file.parent.glob(".speaker_name.env.seed.*")) == []


@pytest.mark.parametrize("publish_behavior", ["directory", "directory_symlink"])
def test_speaker_name_seed_directory_race_is_preserved_without_nested_link(
    tmp_path: Path,
    publish_behavior: str,
):
    result, state_file = _run_speaker_name_seed(
        tmp_path,
        deploy_hostname="jts4.local",
        publish_behavior=publish_behavior,
    )
    assert result.returncode == 0, result.stderr
    assert f"speaker name: preserving {state_file}" in result.stdout
    assert 'speaker name: "JTS4"' not in result.stdout
    assert list(state_file.parent.glob(".speaker_name.env.seed.*")) == []

    target_dir = (
        Path(f"{state_file}.target")
        if publish_behavior == "directory_symlink"
        else state_file
    )
    assert target_dir.is_dir()
    assert list(target_dir.iterdir()) == []
    assert state_file.is_symlink() is (publish_behavior == "directory_symlink")


def test_speaker_name_seed_chmod_failure_cleans_temp_and_fails(tmp_path: Path):
    result, state_file = _run_speaker_name_seed(
        tmp_path,
        deploy_hostname="jts4.local",
        fail_chmod=True,
    )
    assert result.returncode != 0
    assert "ERROR: could not set fresh speaker-name permissions" in result.stderr
    assert not state_file.exists()
    assert list(state_file.parent.glob(".speaker_name.env.seed.*")) == []


def test_speaker_name_seed_publish_failure_cleans_temp_and_fails(tmp_path: Path):
    result, state_file = _run_speaker_name_seed(
        tmp_path,
        deploy_hostname="jts4.local",
        publish_behavior="fail",
    )
    assert result.returncode != 0
    assert "ERROR: could not publish fresh speaker name" in result.stderr
    assert not state_file.exists()
    assert list(state_file.parent.glob(".speaker_name.env.seed.*")) == []


def test_speaker_name_seed_runs_once_before_shared_renderer_consumers():
    renderers = _RENDERERS_LIB.read_text(encoding="utf-8")
    body = renderers[
        renderers.index("install_renderers() {"):
        renderers.index("\n}\n\nreconcile_usb_data_role()")
    ]
    assert body.count("seed_speaker_name_env") == 1
    assert body.index("seed_speaker_name_env") < body.index(
        "/usr/local/sbin/jasper-apply-airplay-mode"
    )
    assert body.index("seed_speaker_name_env") < body.index(
        'bash "${REPO_DIR}/deploy/configure-bluez.sh"'
    )

    main = _INSTALL_SH.read_text(encoding="utf-8")
    assert sum(line.strip() == "install_renderers" for line in main.splitlines()) == 2
    runtime = (_INSTALL_LIB_DIR / "python-runtime.sh").read_text(
        encoding="utf-8",
    )
    assert runtime.count('JASPER_SPEAKER_NAME="JTS"') == 0


# Pi 5 SKU memory sizes (real values from /proc/meminfo on each
# variant — approximate; actual values vary by ~5 MB per board).
_PI5_1GB_MEMTOTAL_KB = 1014768   # 991 MB
_PI5_2GB_MEMTOTAL_KB = 2031264   # 1983 MB (some hardware-reserved memory)
_PI5_4GB_MEMTOTAL_KB = 4063920   # 3968 MB
_PI5_8GB_MEMTOTAL_KB = 8128464   # 7938 MB
_PI5_16GB_MEMTOTAL_KB = 16264848 # 15883 MB


@pytest.mark.parametrize(
    "memtotal_kb, expected",
    [
        # Unreadable/garbage MemTotal: awk yields 0, the floor catches it.
        (0, 16384),
        (1024, 16384),
        (458_752, 16384),      # Pi Zero 2 W, 512 MB board: 2% is only ~9 MB
        (819_200, 16384),      # 2% lands exactly on the floor
        (_PI5_1GB_MEMTOTAL_KB, 20295),
        (_PI5_2GB_MEMTOTAL_KB, 40625),
        (_PI5_4GB_MEMTOTAL_KB, 81278),
        (_PI5_8GB_MEMTOTAL_KB, 162569),
        (8_192_050, 163841),   # awk's int(x + 0.5) rounds half up
        (13_000_000, 260000),  # just below the ceiling: still proportional
        (13_107_200, 262144),  # 2% lands exactly on the ceiling
        (_PI5_16GB_MEMTOTAL_KB, 262144),  # capped
    ],
)
def test_compute_min_free_kbytes_clamps_two_percent_between_16mb_and_256mb(
    memtotal_kb, expected
):
    """min_free_kbytes is 2% of RAM, floored at the 16 MB Raspberry Pi OS
    itself ships in /etc/sysctl.d/98-rpi.conf and capped at 256 MB."""
    assert _compute_min_free_kbytes(memtotal_kb) == expected


# --- optional enhanced-AEC: canonical C++ build parallelism -----------
# Regression: the unbounded `meson compile` fanned out to nproc (4 on a
# Pi 5) -O3 cc1plus jobs and the OOM killer aborted the deploy on a 1 GB
# Pi (jts2, 2026-06-21), taking nginx + jasper-voice down with it. The optional
# installer now calls jasper-contained-build, which owns this same shared
# ~1.5 GB/job budget and clamps to [1, nproc].

def test_enhanced_aec_jobs_1gb_pi_is_single_job():
    """The load-bearing case: a 1 GB Pi must build at -j1 (the OOM we
    fixed). 991 MB / 1.5 GB-per-job floors to 0 → clamped up to 1."""
    assert _cpp_compile_jobs(_PI5_1GB_MEMTOTAL_KB, 4) == 1


def test_enhanced_aec_jobs_2gb_pi_is_single_job():
    """2 GB / 1.5 GB-per-job = 1."""
    assert _cpp_compile_jobs(_PI5_2GB_MEMTOTAL_KB, 4) == 1


def test_enhanced_aec_jobs_4gb_pi_is_two_jobs():
    """4 GB / 1.5 GB-per-job = 2 (under the 4-core cap)."""
    assert _cpp_compile_jobs(_PI5_4GB_MEMTOTAL_KB, 4) == 2


def test_enhanced_aec_jobs_8gb_pi_uses_full_nproc():
    """A roomy Pi isn't throttled: 8 GB budgets 5 jobs, clamped to the
    4 cores available."""
    assert _cpp_compile_jobs(_PI5_8GB_MEMTOTAL_KB, 4) == 4


def test_enhanced_aec_jobs_clamped_to_nproc_not_ram():
    """RAM allows more jobs than cores → clamp to nproc."""
    assert _cpp_compile_jobs(_PI5_16GB_MEMTOTAL_KB, 2) == 2


def test_enhanced_aec_jobs_never_zero_on_garbage_input():
    """A meson `-j0` would be invalid (or unbounded). Zero/garbage
    MemTotal must still floor to 1 job."""
    assert _cpp_compile_jobs(0, 4) == 1
    assert _cpp_compile_jobs(1024, 4) == 1


def test_ensure_state_dir_uses_voice_state_directory_mode(tmp_path):
    state_dir = tmp_path / "state"
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source "
            + shlex.quote(str(_INSTALL_SH))
            + " >/dev/null && "
            + "STATE_DIR="
            + shlex.quote(str(state_dir))
            + " && ensure_state_dir",
        ],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o750


def test_ensure_state_dir_does_not_rechmod_an_existing_dir(tmp_path):
    """`install -d -m` re-chmods an EXISTING dir, briefly narrowing an
    already-widened 0770 STATE_DIR back to 0750 on every call. Pin that an
    existing STATE_DIR never reaches `install` at all, via a fake `install`
    on PATH that records its invocations."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o770)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "install.calls"
    fake_install = bin_dir / "install"
    fake_install.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> "
        + shlex.quote(str(calls))
        + "\nexit 0\n",
        encoding="utf-8",
    )
    fake_install.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source "
            + shlex.quote(str(_INSTALL_SH))
            + " >/dev/null && "
            + "STATE_DIR="
            + shlex.quote(str(state_dir))
            + " && ensure_state_dir",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert not calls.exists(), (
        "ensure_state_dir called `install -d` on an existing STATE_DIR: "
        f"{calls.read_text(encoding='utf-8') if calls.exists() else ''}"
    )


def test_persist_install_profile_does_not_rechmod_an_existing_state_dir(tmp_path):
    """`persist_install_profile`'s marker defaults under STATE_DIR, so its
    `install -d -m 0750 "$(dirname "$marker")"` targets STATE_DIR itself on
    the production path — the same re-chmod trap `ensure_state_dir` closed
    (#3879), left open here. Pin that an existing parent dir never reaches
    `install` at all, via a fake `install` on PATH that records invocations."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_dir.chmod(0o770)  # mkdir(mode=...) is masked by umask; chmod isn't
    marker = state_dir / "install_profile"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "install.calls"
    fake_install = bin_dir / "install"
    fake_install.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> "
        + shlex.quote(str(calls))
        + "\nexit 0\n",
        encoding="utf-8",
    )
    fake_install.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    result = subprocess.run(
        [
            "bash",
            "-c",
            "source "
            + shlex.quote(str(_INSTALL_SH))
            + " >/dev/null && persist_install_profile full "
            + shlex.quote(str(marker)),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert not calls.exists(), (
        "persist_install_profile called `install -d` on an existing parent dir: "
        f"{calls.read_text(encoding='utf-8') if calls.exists() else ''}"
    )
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o770


def _run_install_streambox_jasper(
    tmp_path: Path,
    *,
    stubs: tuple[str, ...] = ("install",),
    state_dir_mode: int | None = None,
    env_file_text: str | None = None,
    epilogue: str = "",
) -> tuple[subprocess.CompletedProcess[str], dict[str, Path]]:
    """Run install_streambox_jasper against a scratch root.

    `install` is always stubbed: its `-o root -g root` needs privileges CI
    lacks. Stubbing `rsync` too lets the run reach the env step; leaving it
    real makes rsync fail on the empty REPO_DIR, so only dir-prep has run.
    Every stub appends its argv to the returned `calls` path.
    """
    paths = {
        "state": tmp_path / "state",
        "env": tmp_path / "etc",
        "install": tmp_path / "opt/jasper",
        "repo": tmp_path / "repo",
        "calls": tmp_path / "stub.calls",
    }
    for key in ("state", "env", "repo"):
        paths[key].mkdir()
    if state_dir_mode is not None:
        # mkdir(mode=...) is masked by umask; chmod isn't.
        paths["state"].chmod(state_dir_mode)
    (paths["install"] / ".venv/bin").mkdir(parents=True)
    pip = paths["install"] / ".venv/bin/pip"
    pip.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    pip.chmod(0o755)
    if env_file_text is not None:
        env_file = paths["env"] / "jasper.env"
        env_file.write_text(env_file_text, encoding="utf-8")
        env_file.chmod(0o600)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in stubs:
        stub = bin_dir / name
        stub.write_text(
            "#!/usr/bin/env bash\n"
            'printf \'%s\\n\' "$*" >> "$JTS_STUB_CALLS"\nexit 0\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["JTS_STUB_CALLS"] = str(paths["calls"])
    env["JASPER_HOSTNAME"] = "jts.local"
    steps = [
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null",
        f"REPO_DIR={shlex.quote(str(paths['repo']))}",
        f"STATE_DIR={shlex.quote(str(paths['state']))}",
        f"ENV_DIR={shlex.quote(str(paths['env']))}",
        f"INSTALL_DIR={shlex.quote(str(paths['install']))}",
        "install_streambox_jasper >/dev/null",
    ]
    if epilogue:
        steps.append(epilogue)
    result = subprocess.run(
        ["bash", "-c", " && ".join(steps)],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    return result, paths


def test_install_streambox_jasper_does_not_rechmod_an_existing_state_dir(tmp_path):
    """`install_streambox_jasper` used to run `install -d -m 0750
    "${STATE_DIR}"` unconditionally before rsync/pip — the same re-chmod
    trap #3879 (ensure_state_dir), #3930 (persist_install_profile), and
    #3931 (record_ring_ioplug_provenance) closed elsewhere, narrowing an
    already-widened 0770 STATE_DIR back to 0750 on every streambox
    install/upgrade. Fixed by delegating to ensure_state_dir, same as
    install_jasper. Pin that an existing STATE_DIR survives the streambox
    dir-prep step untouched and is never passed to `install -d` directly."""
    result, paths = _run_install_streambox_jasper(tmp_path, state_dir_mode=0o770)

    # rsync is real here and fails past dir-prep — expected, and irrelevant.
    assert result.returncode != 0

    assert stat.S_IMODE(paths["state"].stat().st_mode) == 0o770
    call_lines = (
        paths["calls"].read_text(encoding="utf-8").splitlines()
        if paths["calls"].exists()
        else []
    )
    assert all(line.split()[-1] != str(paths["state"]) for line in call_lines), (
        "install_streambox_jasper called `install -d` directly on STATE_DIR: "
        f"{call_lines}"
    )


def test_streambox_env_refresh_writes_the_profile_through_the_shared_lib(tmp_path):
    """The streambox refresh re-asserts JASPER_INSTALL_PROFILE on an EXISTING
    jasper.env. It used to `sed -i` the key out and append it back unquoted,
    leaving a window with the key absent from a file systemd reads via
    EnvironmentFile=; it now goes through jasper_env_file_set, so the key must
    land parsable by `source`, keep every other key, and keep mode 0640."""
    result, paths = _run_install_streambox_jasper(
        tmp_path,
        stubs=("install", "rsync"),
        env_file_text="JASPER_HOSTNAME=jts.local\nJASPER_INSTALL_PROFILE=full\n",
        epilogue=(
            'source "$ENV_DIR/jasper.env" && '
            'printf "%s|%s" "$JASPER_INSTALL_PROFILE" "$JASPER_HOSTNAME"'
        ),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "streambox|jts.local"
    env_file = paths["env"] / "jasper.env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o640
    assert not list(paths["env"].glob(".JASPER_INSTALL_PROFILE.*"))


def test_retired_esp32_python_packages_are_uninstalled_from_jts_venv(tmp_path):
    install_root = tmp_path / "opt/jasper"
    pip = install_root / ".venv/bin/pip"
    pip.parent.mkdir(parents=True)
    calls = tmp_path / "pip.calls"
    pip.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> \"$PIP_CALLS\"\n"
    )
    pip.chmod(0o755)
    env = os.environ.copy()
    env["PIP_CALLS"] = str(calls)

    result = subprocess.run(
        [
            "bash",
            "-c",
            "source "
            + shlex.quote(str(_INSTALL_LIB_DIR / "python-runtime.sh"))
            + " && INSTALL_DIR="
            + shlex.quote(str(install_root))
            + " && retire_esp32_accessory_python_packages",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0, result.stderr
    call = calls.read_text()
    assert call.startswith("uninstall -y ")
    for package in (
        "platformio",
        "esptool",
        "pyserial",
        "bitarray",
        "bitstring",
        "click",
        "intelhex",
        "markdown-it-py",
        "mdurl",
        "reedsolo",
        "rich",
        "rich-click",
        "tibs",
    ):
        assert package in call.split()

    runtime = (_INSTALL_LIB_DIR / "python-runtime.sh").read_text()
    pip_upgrade_pos = runtime.index('pip" install --upgrade')
    retire_pos = runtime.index(
        "retire_esp32_accessory_python_packages",
        pip_upgrade_pos,
    )
    assert pip_upgrade_pos < retire_pos
    streambox = runtime[runtime.index("install_streambox_jasper() {"):]
    streambox_pip_upgrade_pos = streambox.index('pip" install --upgrade')
    streambox_retire_pos = streambox.index(
        "retire_esp32_accessory_python_packages",
        streambox_pip_upgrade_pos,
    )
    assert streambox_pip_upgrade_pos < streambox_retire_pos


def test_active_speaker_tone_artifacts_are_writable_by_web_service():
    """The /sound/ combined-test path writes bounded WAV artifacts as jasper-web."""

    text = _INSTALL_LIB_DIR.joinpath("python-runtime.sh").read_text(
        encoding="utf-8"
    )
    assert (
        'install -d -m 2770 -o root -g jasper '
        '"${STATE_DIR}/active_speaker_tone_artifacts"'
    ) in text


def test_spotify_wizard_owned_values_are_not_seeded_into_jasper_env():
    """Fresh installs must not write stale empty Spotify overrides."""
    env_example = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "\nSPOTIFY_CLIENT_ID=" not in env_example
    assert "\nSPOTIFY_REDIRECT_URI=" not in env_example

    install_sh = "\n".join(_installer_shell_texts().values())
    assert "/^SPOTIFY_CLIENT_ID=/d" in install_sh
    assert "/^SPOTIFY_OAUTH_MODE=/d" in install_sh
    assert "/^SPOTIFY_REDIRECT_URI=/d" in install_sh
    assert "/^SPOTIPY_REDIRECT_URI=/d" in install_sh


@pytest.mark.parametrize(
    "key",
    ["JASPER_AEC_CHIP_AEC_DAC_AUTO", "JASPER_AEC_CHIP_AEC_DAC_TRIAL"],
)
def test_retired_chip_aec_gate_keys_leave_no_reader_writer_or_seeded_line(
    key: str,
) -> None:
    """The DAC gate's status IS its verdict, so the per-selection keys retired.

    An upgraded box keeps whatever its last pass wrote, and a key nothing
    rewrites is a value that can only rot — the reconciler's carry would be
    reading a verdict from a build that no longer computes one.
    """
    reconciler = _INSTALL_LIB_DIR.parent.parent.joinpath(
        "bin", "jasper-aec-reconcile"
    ).read_text(encoding="utf-8")
    assert key not in reconciler
    assert f"/^{key}=/d" in "\n".join(_installer_shell_texts().values())


@pytest.mark.parametrize(
    ("seeded_value", "expected_line"),
    [
        ("1073741824", None),  # exact pre-PR frozen-seed default: migrated away
        ("268435456", "JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES=268435456"),
    ],
    ids=("stale-default-stripped", "deliberate-override-preserved"),
)
def test_migrate_wake_events_cap_seed(
    tmp_path: Path, seeded_value: str, expected_line: str | None,
) -> None:
    """The pre-PR frozen seed's exact stale value migrates away (line
    removed, `_env_int` falls back to the new code default); any other
    operator-set value survives untouched."""
    env_dir = tmp_path / "etc"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / "jasper.env").write_text(
        "JASPER_HOSTNAME=jts.local\n"
        f"JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES={seeded_value}\n"
    )

    proc = _run_install_helper("migrate_wake_events_cap_seed", tmp_path)
    assert proc.returncode == 0, proc.stderr

    lines = (env_dir / "jasper.env").read_text().splitlines()
    assert "JASPER_HOSTNAME=jts.local" in lines
    if expected_line is None:
        assert not any(
            line.startswith("JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES=")
            for line in lines
        )
    else:
        assert expected_line in lines


def test_mic_device_candidates_are_template_owned_for_fresh_install():
    """The install-time seed must not duplicate hot-swap mic candidates."""
    env_example = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert (
        env_example.count("\nJASPER_MIC_DEVICE_CANDIDATES=Array,L16K6Ch\n")
        == 1
    )

    python_runtime = _INSTALL_LIB_DIR.joinpath("python-runtime.sh").read_text(
        encoding="utf-8"
    )
    assert "JASPER_MIC_DEVICE_CANDIDATES=Array|" not in python_runtime


def test_wifi_tuning_persists_retry_forever_and_power_save_disable():
    """AirPlay's Wi-Fi tweak also owns NetworkManager retry resilience."""
    text = _RENDERERS_LIB.read_text(encoding="utf-8")
    match = re.search(
        r"tune_wifi_for_airplay\(\) \{(?P<body>.*?)\n\}",
        text,
        flags=re.S,
    )
    assert match is not None
    body = match.group("body")
    assert "connection.autoconnect yes" in body
    assert "connection.autoconnect-retries 0" in body
    assert "802-11-wireless.powersave 2" in body


_SYSTEMD_UNITS_LIB = _INSTALL_LIB_DIR / "systemd-units.sh"


def test_mask_distro_background_units_masks_present_timers_only(tmp_path):
    """Only the timers the image actually carries are masked (`mask --now` on
    an absent unit would fail the install); each masked unit's stale
    Result=resources failed-state is cleared; and cloud-init is opted out via
    its own sentinel rather than by removing the package."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "systemctl.log"
    present = "apt-daily.timer\napt-daily-upgrade.timer\nman-db.timer\n"
    stub = bindir / "systemctl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'if [[ "$1" == "list-unit-files" ]]; then printf %s {shlex.quote(present)};'
        " exit 0; fi\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(log))}\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    cloud_dir = tmp_path / "cloud"
    cloud_dir.mkdir()

    result = subprocess.run(
        [
            "bash",
            "-c",
            f"export PATH={shlex.quote(str(bindir))}:$PATH && "
            f"source {_SYSTEMD_UNITS_LIB} >/dev/null 2>&1 && "
            "mask_distro_background_units",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "JTS_CLOUD_INIT_DIR": str(cloud_dir)},
    )

    assert result.returncode == 0, result.stderr
    assert (cloud_dir / "cloud-init.disabled").exists()
    calls = log.read_text(encoding="utf-8").splitlines()
    masked = [line.split()[-1] for line in calls if line.startswith("mask ")]
    assert masked == [
        "apt-daily.timer",
        "apt-daily-upgrade.timer",
        "man-db.timer",
    ]
    reset = [
        line.split()[-1] for line in calls if line.startswith("reset-failed ")
    ]
    assert reset == masked


def _run_tune_nginx_worker_processes(conf: Path) -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"source {_INSTALL_SH} >/dev/null 2>&1; tune_nginx_worker_processes",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "JTS_NGINX_MAIN_CONF": str(conf)},
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "packaged",
    [
        "user www-data;\nworker_processes auto;\npid /run/nginx.pid;\n",
        "worker_processes 4;  # tuned\nevents { worker_connections 768; }\n",
        "user www-data;\nevents { worker_connections 768; }\n",
        "worker_processes 1;\n",
    ],
)
def test_tune_nginx_worker_processes_pins_one_worker(tmp_path, packaged):
    """One main-context worker_processes directive, value 1, stable on re-run.

    A second copy would make nginx reject the config as duplicate, so the
    count matters as much as the value."""
    conf = tmp_path / "nginx.conf"
    conf.write_text(packaged, encoding="utf-8")
    _run_tune_nginx_worker_processes(conf)
    once = conf.read_text(encoding="utf-8")
    _run_tune_nginx_worker_processes(conf)
    assert conf.read_text(encoding="utf-8") == once

    directives = [
        line.strip()
        for line in once.splitlines()
        if re.match(r"^\s*worker_processes\s", line)
    ]
    assert len(directives) == 1
    assert directives[0].startswith("worker_processes 1;")


def _run_reconcile_headless_boot_config(cfg_path: Path) -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"source {_RENDERERS_LIB} >/dev/null 2>&1; "
            "reconcile_headless_boot_config",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "JTS_BOOT_CONFIG_FILE": str(cfg_path)},
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "stock",
    [
        "[all]\ndtparam=audio=on\ndtoverlay=vc4-kms-v3d\nmax_framebuffers=2\n",
        "dtoverlay=vc4-kms-v3d,cma-256\ngpu_mem=64\n[pi5]\ngpu_mem=128\n",
        "dtparam=audio  # bare form means on\ngpu_mem_512=64\n",
        "dtparam=audio=on # codec\ndtoverlay=vc4-kms-v3d  # display\n",
        "arm_64bit=1",
    ],
)
def test_reconcile_headless_boot_config_owns_directives_and_is_idempotent(
    tmp_path, stock
):
    """Headless trim: one copy of each directive, stable across re-runs.

    The bare ``dtoverlay=`` must precede ``dtparam=audio=off``; without it
    the firmware offers the parameter to whatever overlay was declared last
    instead of to the base DTB.
    """
    cfg = tmp_path / "config.txt"
    cfg.write_text(stock, encoding="utf-8")
    _run_reconcile_headless_boot_config(cfg)
    once = cfg.read_text(encoding="utf-8")
    _run_reconcile_headless_boot_config(cfg)
    assert cfg.read_text(encoding="utf-8") == once

    lines = [line.strip() for line in once.splitlines()]
    assert lines.count("gpu_mem=16") == 1
    assert lines.count("dtparam=audio=off") == 1
    assert lines.count("dtoverlay=") == 1
    assert lines.index("dtoverlay=") == lines.index("dtparam=audio=off") - 1
    assert not [line for line in lines if line.startswith("dtparam=audio=on")]
    assert not [
        line
        for line in lines
        if line.startswith("gpu_mem") and line != "gpu_mem=16"
    ]
    assert not [line for line in lines if line.startswith("dtparam=audio")][1:]
    vc4 = [line for line in lines if line.startswith("dtoverlay=vc4-kms-v3d")]
    assert len(vc4) == (1 if "vc4-kms-v3d" in stock else 0)
    assert all(line.startswith("dtoverlay=vc4-kms-v3d,cma-64") for line in vc4)


def _run_reconcile_usb_data_role(
    cfg_path: Path,
    *,
    model_text: str,
) -> str:
    """Run the installer role owner against hermetic hardware inputs."""

    model = cfg_path.parent / "model"
    udc = cfg_path.parent / "udc"
    model.write_text(model_text, encoding="utf-8")
    udc.mkdir(exist_ok=True)
    result = subprocess.run(
        [
            "bash",
            "-c",
            f"source {_RENDERERS_LIB} >/dev/null 2>&1; reconcile_usb_data_role",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        env={
            **os.environ,
            "REPO_DIR": str(REPO_ROOT),
            "JTS_BOOT_CONFIG_FILE": str(cfg_path),
            "JASPER_PI_MODEL_FILE": str(model),
            "JASPER_UDC_CLASS_DIR": str(udc),
        },
    )
    if result.returncode != 0:
        raise RuntimeError(
            "reconcile_usb_data_role failed "
            f"(rc={result.returncode}): {result.stderr}"
        )
    return result.stdout


def test_reconcile_usb_data_role_keeps_pi5_peripheral_and_power_fix(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("# stock\ndtparam=audio=on\n", encoding="utf-8")
    _run_reconcile_usb_data_role(
        cfg,
        model_text="Raspberry Pi 5 Model B Rev 1.0",
    )
    text = cfg.read_text(encoding="utf-8")
    assert "dtoverlay=dwc2,dr_mode=peripheral" in text
    assert "usb_max_current_enable=1" in text


def test_reconcile_usb_data_role_migrates_zero_to_output_safe_host(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text(
        "dtparam=audio=on\n[all]\ndtoverlay=dwc2,dr_mode=peripheral\n",
        encoding="utf-8",
    )
    _run_reconcile_usb_data_role(
        cfg,
        model_text="Raspberry Pi Zero 2 W Rev 1.0",
    )
    text = cfg.read_text(encoding="utf-8")
    assert "dtoverlay=dwc2,dr_mode=host" in text
    assert "dtoverlay=dwc2,dr_mode=peripheral" not in text
    assert "usb_max_current_enable=1" in text


def test_reconcile_usb_data_role_zero_i2s_keeps_peripheral(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("[all]\ndtoverlay=hifiberry-dac8x\n", encoding="utf-8")
    _run_reconcile_usb_data_role(
        cfg,
        model_text="Raspberry Pi Zero 2 W Rev 1.0",
    )
    assert "dtoverlay=dwc2,dr_mode=peripheral" in cfg.read_text(encoding="utf-8")


def test_reconcile_usb_data_role_is_idempotent(tmp_path):
    cfg = tmp_path / "config.txt"
    cfg.write_text("dtparam=audio=on\n", encoding="utf-8")
    kwargs = {"model_text": "Raspberry Pi 5 Model B Rev 1.0"}
    _run_reconcile_usb_data_role(cfg, **kwargs)
    once = cfg.read_text(encoding="utf-8")
    _run_reconcile_usb_data_role(cfg, **kwargs)
    twice = cfg.read_text(encoding="utf-8")
    assert once == twice  # a re-run adds nothing
    assert twice.count("dtoverlay=dwc2,dr_mode=peripheral") == 1
    assert twice.count("usb_max_current_enable=1") == 1


def test_install_enables_wifi_recover_timer_with_now():
    """The low-footprint Wi-Fi recovery loop must be live from first deploy."""
    install_sh = installer_text()
    assert "jasper-wifi-recover.service" in install_sh
    assert "jasper-wifi-recover.timer" in install_sh
    assert "jasper-wifi-scan-repair.service" in install_sh
    assert "systemctl enable --now jasper-wifi-recover.timer" in install_sh



def test_install_dry_run_exits_before_root_and_lists_major_surfaces():
    """The install plan is contributor-facing safety gear: it must run
    without sudo and summarize the installer blast radius."""
    result = subprocess.run(
        ["bash", str(_INSTALL_SH), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("==> JTS install plan (dry run)\n")
    assert "this script must be run as root" not in result.stderr
    for expected in [
        "JTS install plan (dry run)",
        "No host changes are made in this mode",
        "apt-get update",
        "CamillaDSP:",
        "Raspotify/librespot deb:",
        "openWakeWord ONNX assets",
        "cargo build --release --locked",
        "/var/lib/jasper/build.txt",
        "WiFi guardian recovery",
        "Reload udev and systemd",
        "python3 scripts/check-provenance.py",
    ]:
        assert expected in result.stdout


def test_install_dry_run_env_alias_and_plan_flag_match():
    """Both documented entry points should use the same no-mutation plan."""
    by_flag = subprocess.run(
        ["bash", str(_INSTALL_SH), "--plan"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    env = os.environ.copy()
    env["JASPER_INSTALL_DRY_RUN"] = "1"
    by_env = subprocess.run(
        ["bash", str(_INSTALL_SH)],
        capture_output=True,
        text=True,
        timeout=5,
        env=env,
    )

    assert by_flag.returncode == 0
    assert by_env.returncode == 0
    assert by_flag.stdout == by_env.stdout


def test_model_downloads_are_bounded_and_split_by_runtime_need():
    """Model fetches should use the shared bounded helper. Required
    openWakeWord runtime assets and the active stock fallback fail the
    install; inactive stock rows are allowed to stay unavailable."""
    shell_text = "\n".join(_installer_shell_texts().values())
    model_text = _MODEL_DOWNLOADS.read_text(encoding="utf-8")

    assert "urllib.request.urlretrieve" not in shell_text + model_text
    assert "python\" -m jasper.model_downloads" in shell_text
    assert "stage --registry openwakeword --required" in shell_text
    assert "stage --registry wake --optional" in shell_text
    assert "stage --registry dtln --optional" in shell_text
    assert "download_model_file(" in model_text
    assert "timeout_seconds=" in model_text
    assert "retries=" in model_text
    assert "max_bytes" in model_text
    assert "required_openwakeword_assets" in model_text
    assert "fallback_openwakeword_assets" in model_text
    assert "openwakeword_asset_for_model(active_model)" in model_text
    assert "required_failures" in model_text
    assert "optional_failures" in model_text
    assert "unavailable rows will be disabled in /wake/" in model_text


def test_base_source_builds_use_hash_checked_archives():
    """Base Pi installs should consume pinned archives, not require git
    just to fetch source-build inputs."""
    text = _INSTALL_SH.read_text(encoding="utf-8")

    for expected in [
        "NQPTP_ARCHIVE_URL",
        "NQPTP_SHA256",
        "SHAIRPORT_SYNC_ARCHIVE_URL",
        "SHAIRPORT_SYNC_SHA256",
        "fetch_verified_source_archive",
    ]:
        assert expected in text

    enhanced_target = (
        REPO_ROOT / "jasper_aec3" / "enhanced-aec-source.env"
    ).read_text(encoding="utf-8")
    enhanced_installer = (
        REPO_ROOT / "jasper" / "cli" / "enhanced_aec_install.py"
    ).read_text(encoding="utf-8")
    assert "WEBRTC_AEC3_ARCHIVE_URL=https://" in enhanced_target
    assert "WEBRTC_AEC3_SHA256=" in enhanced_target
    assert "_download_archive(" in enhanced_installer
    assert "extension_sha256(destination)" in enhanced_installer

    for path, source_text in _installer_shell_texts().items():
        for forbidden in [
            "git clone --depth 1",
            "git init ",
            "git -C \"${tmpdir}",
            "verify_git_head",
        ]:
            assert forbidden not in source_text, (path, forbidden)


def test_install_help_is_clean_and_non_root():
    """Agentic flows often probe commands with --help; keep it quiet
    and usable without sudo."""
    result = subprocess.run(
        ["bash", str(_INSTALL_SH), "--help"],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout.startswith("Usage: bash deploy/install.sh")
    assert "install.sh starting" not in result.stdout
    assert "this script must be run as root" not in result.stderr


def test_install_asound_renderer_renders_direct_outputd_dac(tmp_path: Path):
    rendered, stderr = _render_install_asound_template(
        tmp_path,
        output_dac_id="hifiberry_dac8x",
        output_dac_card="sndrpihifiberry",
    )

    assert stderr == ""
    assert "type hw" in rendered
    assert "card sndrpihifiberry" in rendered
    assert "device 0" in rendered
    assert "ctl.outputd_dac" in rendered
    assert "card sndrpihifiberry" in rendered
    _assert_no_empty_alsa_card(rendered)


def test_install_asound_renderer_dual_apple_parks_outputd_dac(tmp_path: Path):
    rendered, stderr = _render_install_asound_template(
        tmp_path,
        output_dac_id="dual_apple_usb_c_dac_4ch",
        output_dac_card="",
    )

    assert stderr == ""
    assert "type null" in rendered
    assert "type route" not in rendered
    assert "ctl.outputd_dac" not in rendered
    _assert_no_empty_alsa_card(rendered)


def test_install_asound_renderer_unknown_output_parks_outputd_dac(tmp_path: Path):
    rendered, stderr = _render_install_asound_template(
        tmp_path,
        output_dac_id="A",
        output_dac_card="",
        output_dac_recognized="0",
    )

    assert stderr == ""
    assert "pcm.outputd_dac" in rendered
    assert "type null" in rendered
    assert "ctl.outputd_dac" not in rendered
    _assert_no_empty_alsa_card(rendered)


def test_install_asound_renderer_rejects_empty_direct_outputd_dac_card(
    tmp_path: Path,
):
    source = tmp_path / "asoundrc.jasper"
    dest = tmp_path / "asoundrc.rendered"
    source.write_text(
        "__OUTPUTD_DAC_PCM_BLOCK__\n"
        "__OUTPUTD_DAC_CTL_BLOCK__\n",
        encoding="utf-8",
    )
    script = (
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
        "OUTPUT_DAC_CARD='' && "
        "OUTPUT_DAC_ID=hifiberry_dac8x && "
        "OUTPUT_DAC_RECOGNIZED=1 && "
        f"jasper_asound_render_template {shlex.quote(str(source))} {shlex.quote(str(dest))}"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 64
    assert "OUTPUT_DAC_CARD is required for hifiberry_dac8x" in result.stderr
    assert not dest.exists()


def _sha256_of(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_source_archive(tmp_path: Path) -> tuple[Path, str]:
    """Build a small tar.gz shaped like a GitHub source archive
    (one top-level dir, stripped by --strip-components=1)."""
    tree = tmp_path / "pkg-1.0"
    tree.mkdir()
    (tree / "hello.c").write_text("int main(void) { return 0; }\n")
    archive = tmp_path / "source.tar.gz"
    subprocess.run(
        ["tar", "-czf", str(archive), "-C", str(tmp_path), "pkg-1.0"],
        check=True,
        timeout=10,
    )
    return archive, _sha256_of(archive)


def _run_fetch_verified(
    url: str, sha: str, dest: Path,
) -> subprocess.CompletedProcess[str]:
    script = (
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
        f"fetch_verified_source_archive {shlex.quote(url)} "
        f"{shlex.quote(sha)} {shlex.quote(str(dest))} test-archive"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_fetch_verified_source_archive_populates_dest(tmp_path):
    archive, sha = _make_source_archive(tmp_path)
    dest = tmp_path / "dest"

    result = _run_fetch_verified(f"file://{archive}", sha, dest)

    assert result.returncode == 0, result.stderr
    assert (dest / "hello.c").exists()


def test_fetch_verified_source_archive_preserves_dest_on_bad_hash(tmp_path):
    """Fetch-to-temp-then-swap: a failed verification must NOT destroy
    the previously-extracted source at dest (the pre-fix shape rm -rf'd
    dest before curl, stranding the install under set -e)."""
    archive, _ = _make_source_archive(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    sentinel = dest / "previous-source.c"
    sentinel.write_text("previous good tree\n")

    result = _run_fetch_verified(f"file://{archive}", "0" * 64, dest)

    assert result.returncode != 0
    assert sentinel.exists()
    assert sentinel.read_text() == "previous good tree\n"


def test_fetch_verified_source_archive_preserves_dest_on_fetch_failure(tmp_path):
    """A download failure (here: nonexistent file:// URL) must also
    leave the destination untouched."""
    dest = tmp_path / "dest"
    dest.mkdir()
    sentinel = dest / "previous-source.c"
    sentinel.write_text("previous good tree\n")

    result = _run_fetch_verified(
        f"file://{tmp_path}/no-such-archive.tar.gz", "0" * 64, dest,
    )

    assert result.returncode != 0
    assert sentinel.exists()


def test_shairport_build_completes_before_old_binary_is_removed():
    """install_renderers must fetch + compile the shairport-sync
    replacement BEFORE stopping the service and apt-removing the old
    binary. Under set -e, the old order stranded the Pi with no AirPlay
    when the download or build failed. install_renderers lives in the
    sourced deploy/lib/install/renderers.sh since the decomposition."""
    text = _RENDERERS_LIB.read_text(encoding="utf-8")

    idx_fetch = text.index('"${tmpdir}/sps"')
    idx_stop = text.index("systemctl stop shairport-sync")
    idx_remove = text.index("apt-get remove -y shairport-sync")
    idx_make_install = text.index("make install || true")

    # The compile (the contained shairport-sync build after the sps fetch)
    # must precede the stop/remove, which must precede `make install`.
    # The heavy build now routes through run_contained_build (RAM-bounded
    # + cgroup-contained) instead of a hardcoded `make -j4`.
    idx_build = text.index('run_contained_build "shairport-sync"', idx_fetch)
    assert idx_fetch < idx_build < idx_stop < idx_remove < idx_make_install


def test_shairport_configure_enables_airplay2_and_pipe_backend():
    """The shairport-sync source build must compile in BOTH AirPlay 2 and
    the pipe output backend. AirPlay 2 is the whole reason we source-build
    (Trixie apt is AP1-only); --with-pipe ships the pipe backend dormant so a
    future shairport->pipe->reader low-latency path needs no rebuild. The flag
    lives only on the ./configure line in renderers.sh; this contract test is
    what keeps a refactor of that long multi-line invocation from silently
    dropping a backend, and couples the flag to its rebuild-force trigger."""
    text = _RENDERERS_LIB.read_text(encoding="utf-8")

    # Isolate the shairport ./configure invocation (a backslash-continued
    # multi-line command) so we don't match nqptp's separate ./configure.
    match = re.search(
        r"\./configure --sysconfdir=/etc(?P<flags>(?:.*\\\n)*.*--with-mpris-interface)",
        text,
    )
    assert match is not None, "shairport ./configure line not found in renderers.sh"
    flags = match.group("flags")

    assert "--with-airplay-2" in flags
    assert "--with-pipe" in flags
    # --with-stdout is intentionally NOT built (no planned stdout pipeline;
    # the design target is a named-pipe reader, not shairport-as-a-pipe-stage).
    assert "--with-stdout" not in flags

    # The rebuild trigger must feature-detect the pipe backend, or a flag-only
    # change is a silent no-op on already-built Pis (-V already has "AirPlay2").
    # The pattern is anchored so the "pipe" token matches whether it is
    # followed by another feature token or sits at the end of the -V string,
    # so a future trim of the feature list can't cause an infinite rebuild.
    assert "grep -qE -- '-pipe(-|$)'" in text


def test_install_curl_fetches_are_bounded_and_retried():
    """Every direct multi-MB curl in install.sh (and its sourced
    deploy/lib/install/ libs) carries bounded retries and a transfer
    cap so flaky Pi WiFi doesn't abort (or hang) the install."""
    for path, text in _installer_shell_texts().items():
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(("curl ", "if ! curl ")) and "-o" in stripped:
                assert "--retry 3" in stripped, (path, stripped)
                assert "--retry-connrefused" in stripped, (path, stripped)
                assert "--max-time" in stripped, (path, stripped)


def test_pip_toolchain_bootstrap_is_exact_pinned():
    """The venv's pip/wheel bootstrap must be pinned exactly — an
    unpinned `--upgrade pip wheel` re-resolved the installer toolchain
    on every deploy."""
    text = "\n".join(_installer_shell_texts().values())
    assert re.search(
        r'pip" install --upgrade pip==\d+\.\d+(\.\d+)? wheel==\d+\.\d+(\.\d+)?',
        text,
    )
    assert 'install --upgrade pip wheel' not in text


def _run_require_build_user(tmp_path: Path, getent_rc: int) -> subprocess.CompletedProcess[str]:
    """Run require_build_user with a stub `getent` ahead of PATH so the
    test controls whether the build user 'exists'."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "getent"
    stub.write_text(f"#!/usr/bin/env bash\nexit {getent_rc}\n", encoding="utf-8")
    stub.chmod(0o755)
    script = (
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
        f"export PATH={shlex.quote(str(bindir))}:$PATH && "
        "require_build_user"
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )


def test_require_build_user_passes_when_user_exists(tmp_path):
    result = _run_require_build_user(tmp_path, getent_rc=0)
    assert result.returncode == 0, result.stderr


def test_require_build_user_fails_fast_with_remediation(tmp_path):
    """Missing 'pi' must fail with an actionable message — this runs
    before any host mutation so a custom-user host stops at second zero
    instead of 15 minutes in, mid-apt."""
    result = _run_require_build_user(tmp_path, getent_rc=2)
    assert result.returncode != 0
    assert "build user 'pi' does not exist" in result.stderr
    assert "adduser" in result.stderr
    assert "before any packages or services were modified" in result.stderr


def test_main_preflights_build_user_before_mutation():
    """require_build_user must run in main() before install_deps (the
    first host-mutating step)."""
    text = _INSTALL_SH.read_text(encoding="utf-8")
    main_body = text[text.index("\nmain() {"):]
    assert main_body.index("require_build_user") < main_body.index("install_deps")


# ----------------------------------------------------------------------
# Pi-generated pip constraints (deploy/constraints-pi.pins)
# ----------------------------------------------------------------------

_GENERATE_CONSTRAINTS_SH = (
    Path(__file__).parent.parent / "scripts" / "generate-pi-constraints.sh"
)


def _run_constraints_helper(repo_dir: Path) -> str:
    """Source install.sh with REPO_DIR overridden; return the helper's
    stdout (the constraints path, or empty when absent)."""
    script = (
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
        f"REPO_DIR={shlex.quote(str(repo_dir))} && "
        "jasper_pip_constraints_file"
    )
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_install_and_generate_scripts_parse():
    """bash -n over both shell surfaces of the constraints feature."""
    for path in (_INSTALL_SH, _GENERATE_CONSTRAINTS_SH):
        result = subprocess.run(
            ["bash", "-n", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, f"{path}: {result.stderr}"


def test_pip_constraints_file_absent_is_graceful_noop(tmp_path):
    """No deploy/constraints-pi.pins → empty output, rc 0 — installs run
    open-range exactly as before the feature existed."""
    (tmp_path / "deploy").mkdir()
    assert _run_constraints_helper(tmp_path) == ""


def test_pip_constraints_file_present_is_echoed(tmp_path):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    constraints = deploy / "constraints-pi.pins"
    constraints.write_text("httpx==0.28.1\n", encoding="utf-8")
    assert _run_constraints_helper(tmp_path) == str(constraints)


def test_unpinned_pip_installs_carry_the_constraints_args():
    """Open-range pip installs in full/streambox installs must expand the
    pip_constraints array; the exact-pinned installs (pip/wheel,
    openwakeword --no-deps) intentionally don't need it."""
    text = "\n".join(_installer_shell_texts().values())
    # full: requests/scipy/... + -e [full]; streambox: -e [streambox] = 3.
    assert text.count('pip" install "${pip_constraints[@]}"') == 3
    assert 'install "${pip_constraints[@]}" -e "${INSTALL_DIR}[full]"' in text
    assert 'install "${pip_constraints[@]}" \\\n        -e "${INSTALL_DIR}[streambox]"' in text


def test_pi_constraints_do_not_pin_non_pypi_flatbuffers_release():
    constraints = (Path(__file__).parent.parent / "deploy" / "constraints-pi.pins")
    text = constraints.read_text(encoding="utf-8")
    generator = _GENERATE_CONSTRAINTS_SH.read_text(encoding="utf-8")

    assert "flatbuffers==20181003210633" not in text
    assert "grep -v -Fx 'flatbuffers==20181003210633'" in generator


def test_full_install_editable_pip_install_keeps_full_extra():
    """Full speaker installs must pull the runtime dependency stack."""
    text = installer_text()

    assert (
        text.count('install "${pip_constraints[@]}" -e "${INSTALL_DIR}[full]"')
        == 1
    )
    # With the endpoint tier removed there is no bare base-package editable
    # install any more — every profile pulls an extras-tagged install.
    assert text.count('install "${pip_constraints[@]}" -e "${INSTALL_DIR}"') == 0


# ----------------------------------------------------------------------
# Build manifest = the VERIFIED-INSTALL success marker (See ADR-0172):
# written as the FINAL mutation in main(), gated by set -e, so a
# mid-install abort leaves the prior good manifest untouched.
# ----------------------------------------------------------------------

_PYTHON_RUNTIME_LIB = _INSTALL_LIB_DIR / "python-runtime.sh"


def _run_install_snippet(
    snippet: str,
    *,
    state_dir: Path | None = None,
    repo_dir: Path | None = None,
    deploy_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Source install.sh and run a snippet with a deterministic build-SHA
    environment. Clears any inherited JASPER_DEPLOY_* so precedence tests
    don't pick up a value from the CI runner, then exports the requested
    ones and overrides STATE_DIR/REPO_DIR to scratch paths."""
    prelude = [
        f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null",
        "unset JASPER_DEPLOY_SHA JASPER_DEPLOY_SHA_FULL JASPER_DEPLOY_BRANCH",
    ]
    for key, val in (deploy_env or {}).items():
        prelude.append(f"export {key}={shlex.quote(val)}")
    if state_dir is not None:
        prelude.append(f"STATE_DIR={shlex.quote(str(state_dir))}")
    if repo_dir is not None:
        prelude.append(f"REPO_DIR={shlex.quote(str(repo_dir))}")
    script = " && ".join([*prelude, snippet])
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_write_build_manifest_records_sha_and_verified_status(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    result = _run_install_snippet(
        "write_build_manifest",
        state_dir=state,
        deploy_env={
            "JASPER_DEPLOY_SHA": "abc1234",
            "JASPER_DEPLOY_SHA_FULL": "abc1234deadbeef",
            "JASPER_DEPLOY_BRANCH": "main",
        },
    )
    assert result.returncode == 0, result.stderr
    manifest = (state / "build.txt").read_text(encoding="utf-8")
    assert "JASPER_GIT_SHA=abc1234" in manifest
    assert "JASPER_GIT_SHA_FULL=abc1234deadbeef" in manifest
    assert "JASPER_GIT_BRANCH=main" in manifest
    assert "JASPER_INSTALL_AT=" in manifest
    # The honest "install process completed" claim the deploy verifier reads.
    assert "JASPER_INSTALL_STATUS=ok" in manifest


def test_write_build_manifest_preserves_prior_full_and_branch(tmp_path):
    """With no deploy env and no git, the full SHA + branch fall back to a
    prior manifest. Exercises the `[[ -z x ]] && x=$(grep …)` fallback
    lines under set -euo pipefail (sourcing install.sh enables it), proving
    they don't abort, and that the marker is still re-stamped status=ok."""
    state = tmp_path / "state"
    state.mkdir()
    (state / "build.txt").write_text(
        "JASPER_GIT_SHA=cafe123\nJASPER_GIT_SHA_FULL=cafe123456789\n"
        "JASPER_GIT_BRANCH=release\nJASPER_INSTALL_AT=2026-01-01T00:00:00-00:00\n"
        "JASPER_INSTALL_STATUS=ok\n",
        encoding="utf-8",
    )
    result = _run_install_snippet(
        "write_build_manifest",
        state_dir=state,
        repo_dir=tmp_path / "not-a-git-repo",  # no env, no git → prior manifest
    )
    assert result.returncode == 0, result.stderr
    manifest = (state / "build.txt").read_text(encoding="utf-8")
    assert "JASPER_GIT_SHA=cafe123" in manifest
    assert "JASPER_GIT_SHA_FULL=cafe123456789" in manifest
    assert "JASPER_GIT_BRANCH=release" in manifest
    assert "JASPER_INSTALL_STATUS=ok" in manifest


def test_write_build_manifest_is_atomic_tempfile_rename():
    """The success marker must be written tempfile-then-rename so a torn
    write (power loss mid-`cat`) can't leave a half-line the direction
    guard misreads. Mirrors persist_install_profile."""
    text = _INSTALL_SH.read_text(encoding="utf-8")
    assert "build.txt.tmp.$$" in text
    assert 'mv -f "${tmp}" "${STATE_DIR}/build.txt"' in text


@pytest.mark.parametrize(
    "seeded,preserved",
    [
        (None, False),
        ("", False),
        ("3f2a91c4-88de-4b", False),
        ("not-a-uuid\n", False),
        ("3f2a91c4-88de-4b0c-9a71-2d5e6f70a1b2\n", True),
        ("3f2a91c4-88de-4b0c-9a71-2d5e6f70a1b2\r\n", True),
        ("3f2a91c4-88de-4b0c-9a71-2d5e6f70a1b2 \n", True),
    ],
)
def test_ensure_peer_id_publishes_a_valid_id(tmp_path, seeded, preserved):
    """peer_id is the whole evidence base of the deploy direction guard
    (scripts/_lib.sh verify_or_record_peer_id), and its writer used to be a
    bare redirect behind an EXISTENCE check — so a truncated id survived every
    later install and aborted every later deploy with an identity mismatch.
    Unusable content is now replaced with a fresh UUID, published atomically;
    a valid id is still left exactly as it was."""
    state = tmp_path / "state"
    state.mkdir()
    peer_id = state / "peer_id"
    if seeded is not None:
        # Bytes, not write_text/read_text: universal-newline translation would
        # hide whether a seeded CR actually survived.
        peer_id.write_bytes(seeded.encode())

    result = _run_install_snippet("ensure_peer_id", state_dir=state)

    assert result.returncode == 0, result.stderr
    written = peer_id.read_bytes().decode()
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        written.strip(),
    )
    assert not list(state.glob(".peer_id.*")), "tempfile left behind"
    if preserved:
        assert written == seeded
    else:
        assert written != seeded
        assert stat.S_IMODE(peer_id.stat().st_mode) == 0o644


def test_resolve_build_sha_short_prefers_deploy_env(tmp_path):
    result = _run_install_snippet(
        "resolve_build_sha_short",
        state_dir=tmp_path,
        repo_dir=tmp_path,  # non-git, so the env var must be what's used
        deploy_env={"JASPER_DEPLOY_SHA": "feedface"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "feedface"


def test_resolve_build_sha_short_falls_back_to_prior_manifest(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "build.txt").write_text(
        "JASPER_GIT_SHA=deadbee\nJASPER_GIT_SHA_FULL=deadbeef00\n"
        "JASPER_GIT_BRANCH=main\nJASPER_INSTALL_STATUS=ok\n",
        encoding="utf-8",
    )
    result = _run_install_snippet(
        "resolve_build_sha_short",
        state_dir=state,
        repo_dir=tmp_path / "not-a-git-repo",  # no env, no git → prior manifest
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "deadbee"


def test_resolve_build_sha_short_unknown_when_nothing_available(tmp_path):
    result = _run_install_snippet(
        "resolve_build_sha_short",
        state_dir=tmp_path,  # no build.txt
        repo_dir=tmp_path / "not-a-git-repo",  # no env, no git
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unknown"


def test_build_manifest_not_written_during_python_runtime_install():
    """The early write (the bug) is gone: neither install_jasper nor
    install_streambox_jasper *calls* write_build_manifest — it's main()'s
    final step now. (Comment references to it are fine; a bare-command
    invocation is the regression we forbid.)"""
    python_runtime = _PYTHON_RUNTIME_LIB.read_text(encoding="utf-8")
    call_lines = [
        ln for ln in python_runtime.splitlines()
        if ln.strip() == "write_build_manifest"
    ]
    assert not call_lines, (
        "write_build_manifest must not be CALLED from the python-runtime "
        "install steps (it is main()'s final, verified-success step): "
        f"{call_lines}"
    )


def test_aec3_fingerprint_is_content_based_not_mtime_based(tmp_path):
    install_dir = tmp_path / "opt" / "jasper"
    package_dir = install_dir / "jasper_aec3"
    src_dir = package_dir / "src"
    py_dir = package_dir / "jasper_aec3"
    venv_bin = install_dir / ".venv" / "bin"
    src_dir.mkdir(parents=True)
    py_dir.mkdir(parents=True)
    venv_bin.mkdir(parents=True)
    (package_dir / "pyproject.toml").write_text("[project]\nname='x'\n")
    (src_dir / "aec3_binding.cpp").write_text("int one = 1;\n")
    (py_dir / "__init__.py").write_text("HAS_V2 = True\n")
    (venv_bin / "python").symlink_to(sys.executable)

    first = _run_install_snippet(
        "INSTALL_DIR="
        + shlex.quote(str(install_dir))
        + "; jasper_aec3_source_fingerprint",
        deploy_env={
            "WEBRTC_AEC3_COMMIT": "commit",
            "WEBRTC_AEC3_SHA256": "sha",
        },
    )
    assert first.returncode == 0, first.stderr
    baseline = first.stdout.strip()
    assert baseline.startswith("content-v1:")

    os.utime(src_dir / "aec3_binding.cpp", None)
    second = _run_install_snippet(
        "INSTALL_DIR="
        + shlex.quote(str(install_dir))
        + "; jasper_aec3_source_fingerprint",
        deploy_env={
            "WEBRTC_AEC3_COMMIT": "commit",
            "WEBRTC_AEC3_SHA256": "sha",
        },
    )
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == baseline

    (src_dir / "aec3_binding.cpp").write_text("int one = 2;\n")
    changed = _run_install_snippet(
        "INSTALL_DIR="
        + shlex.quote(str(install_dir))
        + "; jasper_aec3_source_fingerprint",
        deploy_env={
            "WEBRTC_AEC3_COMMIT": "commit",
            "WEBRTC_AEC3_SHA256": "sha",
        },
    )
    assert changed.returncode == 0, changed.stderr
    assert changed.stdout.strip() != baseline


def test_aec3_rebuild_cache_migrates_legacy_marker_once():
    python_runtime = _PYTHON_RUNTIME_LIB.read_text(encoding="utf-8")
    assert "content-v1:" in python_runtime
    assert "legacy cache marker imported cleanly" in python_runtime
    assert "! grep -q '^content-v1:'" in python_runtime
    assert "jasper_aec3_import_probe" in python_runtime


def test_build_manifest_is_the_final_main_mutation():
    """In both main() paths the manifest is written immediately before the
    (non-mutating) run_doctor_summary, after the build steps, and nothing
    mutating runs after it — so reaching it means every build/install step
    succeeded under set -e (the load-bearing invariant behind ADR-0172)."""
    text = _INSTALL_SH.read_text(encoding="utf-8")
    # Adjacency: write_build_manifest directly precedes run_doctor_summary
    # in both the full and streambox paths (whitespace-tolerant).
    adjacency = re.findall(r"write_build_manifest\n\s*run_doctor_summary", text)
    assert len(adjacency) == 2, adjacency

    # Bounded main() body (def line to its column-0 closing brace), so the
    # file's trailing `if main "$@"` guard isn't counted.
    match = re.search(r"\nmain\(\) \{\n(.*?)\n\}", text, re.DOTALL)
    assert match is not None, "could not locate main() body"
    body = match.group(1)
    # The Rust final-output owner (a required, OOM-capable build) is built
    # before the manifest is stamped.
    assert body.index("build_install_jasper_outputd") < body.index(
        "write_build_manifest"
    )
    # run_doctor_summary is the TERMINAL step: nothing mutating follows the
    # manifest stamp. After the last run_doctor_summary call, only branch-
    # closing tokens remain (return 0 / fi / comments / whitespace).
    tail = body[body.rindex("run_doctor_summary") + len("run_doctor_summary"):]
    leftover = [
        ln.strip()
        for ln in tail.splitlines()
        if ln.strip()
        and not ln.strip().startswith("#")
        and ln.strip() not in {"return 0", "fi"}
    ]
    assert not leftover, f"unexpected steps after run_doctor_summary: {leftover}"


def test_landing_page_app_css_version_uses_resolved_build_sha():
    """Now that build.txt is written last, the landing-page cache-bust must
    resolve the SHA directly (deploy env → git → prior manifest), not read
    the not-yet-updated manifest — or a deploy would ship the prior SHA's
    cache key and browsers wouldn't bust the /assets cache."""
    text = _INSTALL_SH.read_text(encoding="utf-8")
    start = text.index("install_management_static_assets() {")
    fn = text[start: text.index("\ninstall_nginx_site() {", start)]
    assert 'app_css_ver="$(resolve_build_sha_short)"' in fn
    # The old direct manifest read for the cache-bust version is gone.
    assert "grep -E '^JASPER_GIT_SHA='" not in fn


# build-sandbox.sh — unified RAM-aware + cgroup-contained build policy
# ----------------------------------------------------------------------
# See ADR-0163. These tests pin the two levers that generalize the
# point-fix: RAM-aware `-j` (_ram_bounded_jobs) and cgroup containment
# (run_contained_build), and the inverse-of-audio-daemon policy that
# makes a build the OOM victim instead of a daemon.

_BUILD_SANDBOX_LIB = _INSTALL_LIB_DIR / "build-sandbox.sh"
_RUST_DAEMONS_LIB = _INSTALL_LIB_DIR / "rust-daemons.sh"


def _ram_bounded_jobs(memtotal_kb: int, ncpu: int, kb_per_job: int) -> int:
    """Invoke the generalized `_ram_bounded_jobs` helper."""
    result = subprocess.run(
        ["bash", "-c",
         f"source {_INSTALL_SH} >/dev/null && "
         f"_ram_bounded_jobs {memtotal_kb} {ncpu} {kb_per_job}"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helper failed (rc={result.returncode}): {result.stderr}")
    return int(result.stdout.strip())


def _build_sandbox_props(
    tmp_path: Path,
    label: str = "demo",
    memtotal_kb: int = 1_014_768,
    extra_env: dict | None = None,
) -> str:
    """Return the systemd-run property list build_sandbox_props emits,
    with a synthetic MemTotal injected so MemoryHigh is deterministic."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal:       {memtotal_kb} kB\n", encoding="utf-8")
    env = os.environ.copy()
    env["JASPER_BUILD_MEMINFO_FILE"] = str(meminfo)
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["bash", "-c",
         f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
         f"build_sandbox_props {shlex.quote(label)}"],
        capture_output=True, text=True, timeout=5, env=env,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _run_contained_build(
    script_tail: str, extra_env: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c",
         f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && {script_tail}"],
        capture_output=True, text=True, timeout=10, env=env,
    )


# --- _ram_bounded_jobs: generalized RAM-aware parallelism --------------

def test_ram_bounded_jobs_cpp_budget_matches_webrtc_pin():
    """C++ -O3 budget (1.5 GB/job): the exact PR #899 values, now via the
    generalized helper."""
    assert _ram_bounded_jobs(_PI5_1GB_MEMTOTAL_KB, 4, 1_500_000) == 1
    assert _ram_bounded_jobs(_PI5_4GB_MEMTOTAL_KB, 4, 1_500_000) == 2
    assert _ram_bounded_jobs(_PI5_8GB_MEMTOTAL_KB, 4, 1_500_000) == 4


def test_ram_bounded_jobs_c_budget_is_gentler_than_old_j4_on_1gb():
    """C autotools budget (0.4 GB/job): a 1 GB Pi builds shairport at -j2,
    not the old hardcoded -j4 that helped drive the OOM. A ~400 MB
    Zero-class box drops to -j1."""
    assert _ram_bounded_jobs(_PI5_1GB_MEMTOTAL_KB, 4, 400_000) == 2
    assert _ram_bounded_jobs(409_600, 4, 400_000) == 1
    assert _ram_bounded_jobs(_PI5_4GB_MEMTOTAL_KB, 4, 400_000) == 4  # nproc cap


def test_ram_bounded_jobs_clamps_to_nproc_and_floors_to_one():
    assert _ram_bounded_jobs(_PI5_16GB_MEMTOTAL_KB, 2, 1_500_000) == 2
    assert _ram_bounded_jobs(0, 4, 400_000) == 1
    assert _ram_bounded_jobs(1024, 4, 1_500_000) == 1


def test_enhanced_aec_jobs_use_the_shared_cpp_budget():
    """The runtime helper stays value-identical to the shared C++ policy."""
    for m in (
        _PI5_1GB_MEMTOTAL_KB, _PI5_2GB_MEMTOTAL_KB,
        _PI5_4GB_MEMTOTAL_KB, _PI5_8GB_MEMTOTAL_KB,
    ):
        assert _cpp_compile_jobs(m, 4) == _ram_bounded_jobs(m, 4, 1_500_000)


# --- build_sandbox_props: the inverse-of-audio-daemon policy -----------

def test_build_sandbox_props_is_inverse_of_audio_daemon_policy(tmp_path):
    """A build is the opposite of an audio daemon: preferred OOM victim,
    yields CPU/IO, and — the load-bearing invariant — is ALLOWED to swap.
    jts-audio.slice and pi-run-diagnostic.sh forbid swap; a build must
    not, or a big -O3 TU can't complete on a 1 GB Pi."""
    props = _build_sandbox_props(tmp_path)
    # OOMScoreAdjust is an exec property that a `systemd-run --scope` unit
    # REJECTS ("Unknown assignment"), which broke every deploy; the positive
    # "kill me first" oom_score_adj is applied to the build process via choom
    # instead (see test_build_sandbox_oom_prefix_*). Pin its ABSENCE from the
    # scope property list so the regression cannot return.
    assert "OOMScoreAdjust" not in props
    assert "--property=CPUWeight=20" in props
    assert "--property=IOWeight=20" in props
    assert "--property=MemoryAccounting=yes" in props
    assert "MemorySwapMax" not in props  # swap allowed — the inverse invariant


@pytest.mark.skipif(
    shutil.which("choom") is None,
    reason="choom (util-linux) absent on this host; verified on the Linux Pi/CI",
)
def test_build_sandbox_oom_prefix_uses_choom_kill_me_first():
    """The build's positive "kill me first" oom_score_adj is applied via choom
    — the scope-compatible substitute for the OOMScoreAdjust exec property a
    `systemd-run --scope` unit cannot take (the #922 deploy-breaker). choom
    ships in util-linux, present on the Linux CI runner and the Pi.
    (Fail-open when choom is absent is a one-line `command -v` guard in
    build_sandbox_oom_prefix — the build runs without the bias, never blocked.)"""
    r = _run_contained_build("build_sandbox_oom_prefix")
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == ["choom", "-n", "900", "--"]
    r2 = _run_contained_build(
        "build_sandbox_oom_prefix",
        extra_env={"JASPER_BUILD_SANDBOX_OOM_SCORE_ADJ": "750"},
    )
    assert r2.stdout.split() == ["choom", "-n", "750", "--"]


def test_build_sandbox_props_soft_memory_high_leaves_headroom(tmp_path):
    """MemoryHigh is a soft throttle at ~85% MemTotal (not a kill), so the
    build leans on swap rather than dying and ~15% stays for PID1/sshd."""
    props = _build_sandbox_props(tmp_path, memtotal_kb=1_000_000)
    m = re.search(r"--property=MemoryHigh=(\d+)K", props)
    assert m, props
    high_kb = int(m.group(1))
    assert 840_000 <= high_kb <= 860_000


def test_build_sandbox_props_hard_caps_are_opt_in(tmp_path):
    """MemoryMax (hard kill) and RuntimeMaxSec are off by default — a
    too-low cap would kill a legit single-TU compile or a slow Zero-2-W
    build — but available when an operator opts in."""
    default = _build_sandbox_props(tmp_path)
    assert "MemoryMax" not in default
    assert "RuntimeMaxSec" not in default

    opted = _build_sandbox_props(tmp_path, extra_env={
        "JASPER_BUILD_SANDBOX_MEMORY_MAX": "700M",
        "JASPER_BUILD_SANDBOX_RUNTIME_MAX": "45min",
    })
    assert "--property=MemoryMax=700M" in opted
    assert "--property=RuntimeMaxSec=45min" in opted


def test_build_sandbox_props_without_meminfo_still_protects(tmp_path):
    """If MemTotal is unreadable, MemoryHigh is omitted but the cgroup-
    controller-independent scope properties (CPU/IO weight + accounting) are
    always present. (The OOM bias rides choom, not a property — see
    test_build_sandbox_oom_prefix_uses_choom_kill_me_first.)"""
    env = os.environ.copy()
    env["JASPER_BUILD_MEMINFO_FILE"] = str(tmp_path / "does-not-exist")
    result = subprocess.run(
        ["bash", "-c",
         f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && build_sandbox_props x"],
        capture_output=True, text=True, timeout=5, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "--property=CPUWeight=20" in result.stdout
    assert "--property=IOWeight=20" in result.stdout
    assert "OOMScoreAdjust" not in result.stdout
    assert "MemoryHigh" not in result.stdout
    assert "MemorySwapMax" not in result.stdout


def test_build_swap_auto_follows_low_memory_threshold(tmp_path):
    low = tmp_path / "low-meminfo"
    low.write_text("MemTotal:       1014768 kB\n", encoding="utf-8")
    standard = tmp_path / "standard-meminfo"
    standard.write_text("MemTotal:       2031616 kB\n", encoding="utf-8")

    low_result = _run_contained_build(
        "build_swap_required",
        extra_env={"JASPER_BUILD_MEMINFO_FILE": str(low)},
    )
    standard_result = _run_contained_build(
        "build_swap_required",
        extra_env={"JASPER_BUILD_MEMINFO_FILE": str(standard)},
    )

    assert low_result.returncode == 0
    assert standard_result.returncode == 1


def test_build_swap_setup_and_cleanup_use_high_priority_swap(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "calls.log"
    swap_path = tmp_path / "jasper-build.swap"
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       1014768 kB\n", encoding="utf-8")

    for name in ("mkswap", "swapon", "swapoff"):
        script = fake_bin / name
        script.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s %s\\n' {name!r} \"$*\" >> {shlex.quote(str(log))}\n",
            encoding="utf-8",
        )
        script.chmod(0o755)

    fallocate = fake_bin / "fallocate"
    fallocate.write_text(
        "#!/usr/bin/env bash\n"
        "while [[ $# -gt 0 ]]; do\n"
        "  case \"$1\" in -l) shift 2 ;; *) path=\"$1\"; shift ;;\n"
        "  esac\n"
        "done\n"
        "printf x > \"$path\"\n",
        encoding="utf-8",
    )
    fallocate.chmod(0o755)

    result = _run_contained_build(
        "setup_build_swap_if_needed && "
        "[[ -f ${JASPER_BUILD_SWAP_PATH} ]] && "
        "cleanup_build_swap && "
        "[[ ! -e ${JASPER_BUILD_SWAP_PATH} ]]",
        extra_env={
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "JASPER_BUILD_MEMINFO_FILE": str(meminfo),
            "JASPER_BUILD_SWAP_PATH": str(swap_path),
            "JASPER_BUILD_SWAP_SIZE_MB": "1",
            "JASPER_BUILD_SWAP_PRIORITY": "201",
        },
    )

    assert result.returncode == 0, result.stderr + result.stdout
    calls = log.read_text(encoding="utf-8")
    assert f"mkswap {swap_path}" in calls
    assert f"swapon -p 201 {swap_path}" in calls
    assert f"swapoff {swap_path}" in calls


def test_low_memory_build_parks_runtime_units_before_python_and_rust_builds():
    install_sh = _INSTALL_SH.read_text(encoding="utf-8")
    systemd_units = (_INSTALL_LIB_DIR / "systemd-units.sh").read_text(
        encoding="utf-8"
    )
    assert "park_low_memory_build_units" in systemd_units
    assert "jasper-control.service" in systemd_units
    assert "jasper-system-web.service" in systemd_units
    assert "jasper-fanin.service" in systemd_units
    main_body = re.search(r"\nmain\(\) \{\n(.*?)\n\}", install_sh, re.DOTALL)
    assert main_body is not None
    assert (
        main_body.group(1).index("park_low_memory_build_units")
        < main_body.group(1).index("install_jasper")
    )
    assert (
        main_body.group(1).index("park_low_memory_build_units")
        < main_body.group(1).index("build_install_jasper_fanin")
    )


def test_low_memory_install_uses_lightweight_health_probe_instead_of_doctor():
    text = _INSTALL_SH.read_text(encoding="utf-8")
    start = text.index("run_doctor_summary()")
    end = text.index("\n}\n\nmain()", start)
    body = text[start:end]

    assert "build_swap_required" in body
    assert "jasper-deploy-health" in body
    low_memory_body = body[body.index("if build_swap_required; then"):body.index("fi\n\n    echo \"=== jasper-doctor")]
    assert "jasper-doctor" not in low_memory_body


# --- run_contained_build: graceful degradation, no double-run ----------

def test_build_sandbox_active_false_when_disabled():
    r = _run_contained_build(
        "_build_sandbox_active && echo active || echo inactive",
        extra_env={"JASPER_BUILD_SANDBOX": "0"})
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip().endswith("inactive")


def test_build_sandbox_disabled_runs_command_directly():
    """When disabled (CI/macOS/container path), the command runs verbatim
    and unmodified."""
    r = _run_contained_build(
        "run_contained_build demo -- bash -c 'echo ran $((1+1))'",
        extra_env={"JASPER_BUILD_SANDBOX": "0"})
    assert r.returncode == 0, r.stderr
    assert "ran 2" in r.stdout


def test_build_sandbox_propagates_failure_without_double_run():
    """A real build failure surfaces verbatim and is never masked by a
    second uncontained run (the no-retry contract)."""
    r = _run_contained_build(
        "run_contained_build demo -- bash -c 'echo once; exit 3'",
        extra_env={"JASPER_BUILD_SANDBOX": "0"})
    assert r.returncode == 3
    assert r.stdout.count("once") == 1


# --- call-site wiring: every heavy build is bounded + contained --------

def test_install_sources_build_sandbox_lib():
    text = _INSTALL_SH.read_text(encoding="utf-8")
    assert 'source "${REPO_DIR}/deploy/lib/install/build-sandbox.sh"' in text


def test_heavy_builds_route_through_run_contained_build():
    renderers = _RENDERERS_LIB.read_text(encoding="utf-8")
    python_runtime = _PYTHON_RUNTIME_LIB.read_text(encoding="utf-8")
    rust = _RUST_DAEMONS_LIB.read_text(encoding="utf-8")
    runtime_helper = (
        REPO_ROOT / "deploy" / "bin" / "jasper-contained-build"
    ).read_text(encoding="utf-8")
    enhanced_installer = (
        REPO_ROOT / "jasper" / "cli" / "enhanced_aec_install.py"
    ).read_text(encoding="utf-8")
    assert 'run_contained_build "${label}"' in runtime_helper
    assert '"webrtc-aec3"' in enhanced_installer
    assert "_run_contained(" in enhanced_installer
    assert 'run_contained_build "jasper-aec3"' in python_runtime
    assert 'run_contained_build "nqptp"' in renderers
    assert 'run_contained_build "shairport-sync"' in renderers
    assert 'run_contained_build "${name}"' in rust


def test_renderer_make_is_ram_bounded_not_hardcoded_j4():
    """The old hardcoded `make -j4` (which helped drive the 1 GB OOM) is
    gone; both renderer C builds derive -j from build_sandbox_jobs at the
    named C budget (not a bare literal)."""
    renderers = _RENDERERS_LIB.read_text(encoding="utf-8")
    assert "make -j4" not in renderers
    assert renderers.count(
        'make -j"$(build_sandbox_jobs "${BUILD_SANDBOX_KB_PER_JOB_C}")"'
    ) == 2


def test_build_sandbox_budget_constants_are_named_once():
    """The per-toolchain RAM/job budgets are the policy — named once in the
    lib (cf. RUST_LOW_MEMORY_BUILD_THRESHOLD_KB), not bare literals scattered
    across call sites."""
    lib = _BUILD_SANDBOX_LIB.read_text(encoding="utf-8")
    assert "BUILD_SANDBOX_KB_PER_JOB_CPP=1500000" in lib
    assert "BUILD_SANDBOX_KB_PER_JOB_C=400000" in lib
    # The installed helper uses the named C++ budget, not a bare literal.
    helper = (
        REPO_ROOT / "deploy" / "bin" / "jasper-contained-build"
    ).read_text(encoding="utf-8")
    assert 'BUILD_SANDBOX_KB_PER_JOB_CPP' in helper


def test_build_sandbox_lib_parses():
    result = subprocess.run(
        ["bash", "-n", str(_BUILD_SANDBOX_LIB)],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr


def _build_sandbox_jobs(kb_per_job_expr: str, memtotal_kb: int, ncpu: int,
                        tmp_path: Path) -> int:
    """Invoke the build_sandbox_jobs wrapper with MemTotal + nproc injected
    (mirrors rust-daemons.sh's JASPER_RUST_MEMINFO_FILE test pattern)."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal:       {memtotal_kb} kB\n", encoding="utf-8")
    env = os.environ.copy()
    env["JASPER_BUILD_MEMINFO_FILE"] = str(meminfo)
    env["JASPER_BUILD_NPROC"] = str(ncpu)
    result = subprocess.run(
        ["bash", "-c",
         f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
         f"build_sandbox_jobs {kb_per_job_expr}"],
        capture_output=True, text=True, timeout=5, env=env,
    )
    assert result.returncode == 0, result.stderr
    return int(result.stdout.strip())


def test_build_sandbox_jobs_reads_injected_meminfo_and_nproc(tmp_path):
    """The wrapper resolves the host's MemTotal/nproc and applies the named
    budget — the path the renderer makes actually call."""
    # C budget on a 1 GB Pi, 4 cores -> 2 jobs; 1 core clamps to 1.
    assert _build_sandbox_jobs(
        '"${BUILD_SANDBOX_KB_PER_JOB_C}"', 1_014_768, 4, tmp_path) == 2
    assert _build_sandbox_jobs(
        '"${BUILD_SANDBOX_KB_PER_JOB_C}"', 1_014_768, 1, tmp_path) == 1
    # C++ budget on the same box -> 1 job (the OOM we fixed).
    assert _build_sandbox_jobs(
        '"${BUILD_SANDBOX_KB_PER_JOB_CPP}"', 1_014_768, 4, tmp_path) == 1


def test_run_contained_build_invokes_systemd_run_scope_with_policy(tmp_path):
    """Force containment with a stub systemd-run on PATH and assert the
    invocation contract: --scope, the inverse-policy properties, the --
    separator, and the verbatim command. Without this, a refactor could
    silently re-add the OOMScoreAdjust property a --scope unit rejects, or
    add a swap cap, and the rest of the suite would stay green."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "systemd-run"
    stub.write_text(
        '#!/usr/bin/env bash\nfor a in "$@"; do printf "ARG=%s\\n" "$a"; done\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:       1014768 kB\n", encoding="utf-8")
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["JASPER_BUILD_SANDBOX"] = "1"  # force on; the stub satisfies command -v
    env["JASPER_BUILD_MEMINFO_FILE"] = str(meminfo)
    result = subprocess.run(
        ["bash", "-c",
         f"source {shlex.quote(str(_INSTALL_SH))} >/dev/null && "
         "run_contained_build demo -- echo HELLO WORLD"],
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert result.returncode == 0, result.stderr
    args = [ln[len("ARG="):] for ln in result.stdout.splitlines()
            if ln.startswith("ARG=")]
    assert "--scope" in args
    # The OOMScoreAdjust exec property a --scope unit rejects must NOT be here
    # (the #922 deploy-breaker); the OOM bias rides choom in the command instead.
    assert not any("OOMScoreAdjust" in a for a in args)
    assert "--property=MemoryAccounting=yes" in args
    # Inverse-policy: a build must never get a swap cap.
    assert not any("MemorySwapMax" in a for a in args)
    # Command forwarded verbatim, after the -- separator.
    assert "--" in args
    assert args[-3:] == ["echo", "HELLO", "WORLD"]


# --------------------------------------------------------------------------
# Low-memory build park / unpark (issue #2178).
#
# These are BEHAVIOURAL: a fake `systemctl` on PATH models unit active and
# enablement state, the real park/unpark/trap functions are sourced from the
# installer libs, and the assertions read the OBSERVED final unit states. A
# text-presence test cannot tell "restores the speaker" from "restores half of
# it and leaves the box looking healthy while dead", which is exactly the shape
# the first cut of this fix shipped.
# --------------------------------------------------------------------------

# Models only the verbs the park path uses. Flags are ignored except `--now`
# (which makes `disable` also stop the unit), so `is-active --quiet` and a bare
# `is-active` behave alike. Exit codes follow systemd: `is-active` exits 3 when
# inactive, `is-enabled` exits non-zero for a disabled unit while still printing
# the state on stdout. `is-enabled` prints `enabled` unless the state dir says
# otherwise, matching a normally-installed box.
_FAKE_SYSTEMCTL = """#!/usr/bin/env bash
state="${JTS_FAKE_SYSTEMCTL_STATE}"
printf '%s\\n' "$*" >>"${state}/calls"

verb=""
units=""
for arg in "$@"; do
    case "${arg}" in
        -*) continue ;;
    esac
    if [[ -z "${verb}" ]]; then
        verb="${arg}"
    else
        units="${units} ${arg}"
    fi
done

case "${verb}" in
    is-active)
        for unit in ${units}; do
            [[ -e "${state}/active/${unit}" ]] || exit 3
        done
        exit 0
        ;;
    is-enabled)
        rc=0
        for unit in ${units}; do
            if [[ -e "${state}/enabled/${unit}" ]]; then
                value="$(cat "${state}/enabled/${unit}")"
                echo "${value}"
                # Real systemd exits non-zero for disabled/masked while still
                # printing the state, so the caller must read stdout, not $?.
                # static/indirect/generated exit 0.
                case "${value}" in
                    disabled|masked|masked-runtime) rc=1 ;;
                esac
            else
                echo enabled
            fi
        done
        exit "${rc}"
        ;;
    start|restart|try-restart)
        for unit in ${units}; do
            if grep -qxF "${unit}" "${state}/failstart" 2>/dev/null; then
                echo "Job for ${unit} failed." >&2
                exit 1
            fi
            # Real systemd refuses outright to start a masked unit, so a
            # caller that decides to try one must see the failure. `masked*`
            # covers `masked-runtime`, systemd's /run-scoped mask.
            if [[ "$(cat "${state}/enabled/${unit}" 2>/dev/null)" == masked* ]]; then
                echo "Failed to start ${unit}: Unit ${unit} is masked." >&2
                exit 1
            fi
            : >"${state}/active/${unit}"
        done
        exit 0
        ;;
    stop)
        for unit in ${units}; do
            rm -f "${state}/active/${unit}"
        done
        exit 0
        ;;
    disable|mask)
        for unit in ${units}; do
            case "${verb}" in
                disable) echo disabled >"${state}/enabled/${unit}" ;;
                mask)
                    # `mask --runtime` masks into /run rather than /etc, and
                    # systemd reports that unit as `masked-runtime`.
                    case " $* " in
                        *" --runtime "*)
                            echo masked-runtime >"${state}/enabled/${unit}" ;;
                        *) echo masked >"${state}/enabled/${unit}" ;;
                    esac
                    ;;
            esac
            case " $* " in
                *" --now "*) rm -f "${state}/active/${unit}" ;;
            esac
        done
        exit 0
        ;;
esac
exit 0
"""

# Sources the real libs, forces the low-memory park on (it is gated on the
# build swap, which depends on host MemTotal), arms the real EXIT trap, parks,
# then runs the scenario. `mark_trap` lets a scenario split the call log into
# "what the install did" and "what the trap did".
_PARK_DRIVER = """set -euo pipefail
# systemd-units.sh interpolates SYSTEMD_DIR into a top-level array literal, so
# it must be set before the source (install.sh sets it at its own top).
SYSTEMD_DIR="/etc/systemd/system"
# install.sh also sets STATE_DIR; park_streambox_brain_units removes a marker
# under it, so point it at the sandbox rather than the host's /var/lib/jasper.
STATE_DIR="${JTS_FAKE_SYSTEMCTL_STATE}"
source "${REPO_DIR}/deploy/lib/install/build-sandbox.sh"
source "${REPO_DIR}/deploy/lib/install/systemd-units.sh"
build_swap_required() { return 0; }
mark_trap() { printf 'SENTINEL\\n' >>"${JTS_FAKE_SYSTEMCTL_STATE}/calls"; }
trap install_exit_cleanup EXIT
park_low_memory_build_units
eval "${JTS_SCENARIO}"
"""


class _ParkRun:
    """Result of one park/abort/unpark cycle against the fake systemctl."""

    def __init__(self, returncode: int, stdout: str, stderr: str,
                 calls: list[str], active: set[str], syslog: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls = calls
        self.active = active
        self.syslog = syslog

    def trap_calls(self) -> list[str]:
        """Calls made after the scenario marked its exit — i.e. by the trap."""
        assert "SENTINEL" in self.calls, "scenario did not call mark_trap"
        return self.calls[self.calls.index("SENTINEL") + 1:]

    def started_by_trap(self) -> list[str]:
        return [
            call.split()[-1]
            for call in self.trap_calls()
            if call.startswith("start ")
        ]


def _run_park_cycle(
    tmp_path: Path,
    *,
    active: list[str],
    scenario: str,
    enablement: dict[str, str] | None = None,
    failstart: list[str] | None = None,
) -> _ParkRun:
    state = tmp_path / "state"
    (state / "active").mkdir(parents=True)
    (state / "enabled").mkdir(parents=True)
    (state / "calls").write_text("", encoding="utf-8")
    for unit in active:
        (state / "active" / unit).write_text("", encoding="utf-8")
    for unit, value in (enablement or {}).items():
        (state / "enabled" / unit).write_text(f"{value}\n", encoding="utf-8")
    if failstart:
        (state / "failstart").write_text(
            "\n".join(failstart) + "\n", encoding="utf-8"
        )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "systemctl"
    fake.write_text(_FAKE_SYSTEMCTL, encoding="utf-8")
    fake.chmod(0o755)
    # `_build_sandbox_log` mirrors every event to syslog via `logger`; capture
    # what an operator would later grep out of the journal.
    syslog = state / "syslog"
    logger = bin_dir / "logger"
    logger.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"{syslog}"\nexit 0\n',
        encoding="utf-8",
    )
    logger.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "REPO_DIR": str(REPO_ROOT),
            "JTS_FAKE_SYSTEMCTL_STATE": str(state),
            "JTS_SCENARIO": scenario,
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
        }
    )
    result = subprocess.run(
        ["bash", "-c", _PARK_DRIVER],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = [
        line for line in
        (state / "calls").read_text(encoding="utf-8").splitlines() if line
    ]
    # Harness self-check. Without it a driver that dies while sourcing (an
    # unbound variable, a moved lib) never parks anything, every unit is
    # trivially still active, and the assertions below pass for the wrong
    # reason.
    assert "unbound variable" not in result.stderr, result.stderr
    assert any(call.startswith("stop ") for call in calls), (
        f"driver never reached the park; stderr={result.stderr!r}"
    )
    return _ParkRun(
        result.returncode,
        result.stdout,
        result.stderr,
        calls,
        {p.name for p in (state / "active").iterdir()},
        syslog.read_text(encoding="utf-8") if syslog.exists() else "",
    )


def test_aborted_install_restores_units_from_both_park_phases(tmp_path):
    """An install that aborts must put back everything it stopped.

    `park_low_memory_build_units` stops units in two phases:
    `park_audio_clients_for_core_graph_restart` (the renderers, the output
    owner and mux) and then its own loop (the graph and the control plane).
    Restoring only the second phase is the failure this fix exists to prevent
    and is *worse* than restoring nothing: /system/ answers 200 so the box
    looks healthy while it has no output owner and has vanished from AirPlay,
    Spotify and Bluetooth. Observed twice on a Pi Zero 2 W (issue #2178).
    """
    phase_one = [
        "shairport-sync.service",
        "nqptp.service",
        "librespot.service",
        "bluealsa-aplay.service",
        "jasper-outputd.service",
        "jasper-mux.service",
        "jasper-voice.service",
    ]
    phase_two = [
        "jasper-fanin.service",
        "jasper-camilla.service",
        "jasper-control.service",
        "jasper-system-web.service",
    ]
    run = _run_park_cycle(
        tmp_path,
        active=phase_one + phase_two,
        scenario="mark_trap; exit 1",
    )

    assert run.returncode == 1, run.stderr
    still_dead = sorted(set(phase_one + phase_two) - run.active)
    assert not still_dead, (
        f"aborted install left these units stopped: {still_dead}"
    )


def test_park_stops_fanin_before_outputd(tmp_path):
    """jasper-fanin must be stopped before jasper-outputd during the park.

    With outputd gone and CamillaDSP free-running, fanin's mixer loop loses
    its downstream pacer and its RT thread trips RLIMIT_RTTIME (SIGKILL)
    within ~1s if it is still running when outputd stops. Regression pin for
    park_low_memory_build_units's explicit early fanin stop.
    """
    run = _run_park_cycle(
        tmp_path,
        active=["jasper-fanin.service", "jasper-outputd.service"],
        scenario="mark_trap; exit 1",
    )

    stops = [call.split()[-1] for call in run.calls if call.startswith("stop ")]
    assert stops.index("jasper-fanin.service") < stops.index(
        "jasper-outputd.service"
    ), f"jasper-fanin must be stopped before jasper-outputd; got {stops}"


def test_aborted_install_restores_the_graph_before_its_renderers(tmp_path):
    """Restore order follows the units' own declared After= chain.

    jasper-camilla is After=jasper-outputd jasper-fanin and jasper-mux is
    After=jasper-fanin, so the output owner and the graph go back before the
    renderers and the control plane that attach to them.
    """
    run = _run_park_cycle(
        tmp_path,
        active=[
            "shairport-sync.service",
            "librespot.service",
            "jasper-mux.service",
            "jasper-outputd.service",
            "jasper-fanin.service",
            "jasper-camilla.service",
            "jasper-control.service",
        ],
        scenario="mark_trap; exit 1",
    )

    started = run.started_by_trap()
    for earlier, later in (
        ("jasper-outputd.service", "jasper-mux.service"),
        ("jasper-fanin.service", "jasper-mux.service"),
        ("jasper-camilla.service", "jasper-mux.service"),
        ("jasper-mux.service", "shairport-sync.service"),
        ("jasper-mux.service", "librespot.service"),
        ("jasper-mux.service", "jasper-control.service"),
    ):
        assert started.index(earlier) < started.index(later), (
            f"{earlier} must be restored before {later}; got {started}"
        )


def test_unpark_does_not_start_units_that_were_already_stopped(tmp_path):
    """Restore exactly what was taken away.

    A household that turned Spotify off at /sources/ has librespot stopped
    before the install begins. A recovery path that starts every unit it knows
    about would turn a source back on behind the household's back.
    """
    run = _run_park_cycle(
        tmp_path,
        active=["shairport-sync.service", "jasper-control.service"],
        scenario="mark_trap; exit 1",
    )

    assert "librespot.service" not in run.started_by_trap()
    assert "librespot.service" not in run.active
    assert "shairport-sync.service" in run.active


def test_unpark_leaves_units_the_install_deliberately_disabled_alone(tmp_path):
    """A recovery path must not undo a deliberate decision.

    On a full-speaker -> streambox conversion, `park_streambox_brain_units`
    `disable --now`s the brain surfaces *after* the low-memory snapshot was
    taken. Restarting them from the trap would resurrect exactly what the
    profile change just parked — and it fires on the SUCCESS path too, where
    the trap always runs.
    """
    brain = [
        "jasper-aec-init.service",
        "jasper-aec-reconcile.service",
    ]
    run = _run_park_cycle(
        tmp_path,
        active=brain + ["jasper-control.service", "shairport-sync.service"],
        scenario="park_streambox_brain_units; mark_trap; exit 1",
    )

    started = run.started_by_trap()
    for unit in brain:
        assert unit not in started, f"{unit} was deliberately disabled"
        assert unit not in run.active
    # The rest of the recovery still happened.
    assert "jasper-control.service" in run.active
    assert "shairport-sync.service" in run.active


@pytest.mark.parametrize(
    ("verb", "state"),
    [
        ("disable", "disabled"),
        ("mask", "masked"),
        ("mask --runtime", "masked-runtime"),
    ],
)
def test_unpark_skips_a_unit_this_install_turned_off(tmp_path, verb, state):
    """Every deliberate-off transition skips; `static` is not one of them.

    `park_streambox_brain_units` only ever produces `disabled`, so `masked`
    (an operator's own decision) needs its own case. `static` is what a
    socket-activated unit with no [Install] directives reports, and must still
    be restored — that is what every wizard `.service` unit reports, so
    `enable_streambox_web_sockets`' `disable` cannot reach this branch at all.

    `masked-runtime` is systemd's /run-scoped mask. No in-repo path produces
    it today and it was NOT reproduced on hardware — it is carried purely so
    the off-state vocabulary is complete, and this case is what stops a future
    `mask --runtime` in the install from having its unit start-attempted and
    reported as a failed restore instead of skipped as deliberate.

    The state change happens *during* the scenario, i.e. after the park
    snapshot. That is the shape the skip exists for: intent changed under the
    install's own hand. A unit that was already off when it was parked is a
    different case entirely — see the runtime-managed test below.
    """
    run = _run_park_cycle(
        tmp_path,
        active=[
            "librespot.service",
            "jasper-system-web.service",
            "jasper-control.service",
        ],
        enablement={"jasper-system-web.service": "static"},
        scenario=f"systemctl {verb} librespot.service; mark_trap; exit 1",
    )

    started = run.started_by_trap()
    assert "librespot.service" not in started
    assert "librespot.service" not in run.active
    assert f"state={state}" in run.syslog, run.syslog
    # `static` is not a deliberate-off state.
    assert "jasper-system-web.service" in run.active
    assert "jasper-control.service" in run.active


def test_unpark_restores_a_running_unit_that_was_always_disabled(tmp_path):
    """`disabled` is not evidence of intent when something else does the start.

    `jasper-snapclient`/`-snapserver` ship disabled and are never `enable`d —
    on a bonded speaker `jasper-grouping-reconcile` starts them, so they are
    RUNNING while `is-enabled` reports `disabled`. Skipping every unit that is
    off at restore time strands exactly those: the reconciler is a
    `WantedBy=multi-user.target` oneshot with no timer and no path unit, and
    its in-install run is far past the abort point, so a bonded speaker stays
    silent until someone reboots it (the 2026-06-11 jts3 incident shape).

    What disqualifies a unit is its enablement CHANGING during the install,
    not its being off. Unchanged here, so it must come back.
    """
    snapcast = ["jasper-snapclient.service", "jasper-snapserver.service"]
    run = _run_park_cycle(
        tmp_path,
        active=snapcast + ["jasper-outputd.service", "jasper-control.service"],
        enablement=dict.fromkeys(snapcast, "disabled"),
        scenario="mark_trap; exit 1",
    )

    started = run.started_by_trap()
    for unit in snapcast:
        assert unit in started, f"{unit} was parked while running and dropped"
        assert unit in run.active
    assert "left off on purpose" not in run.syslog, run.syslog
    assert "jasper-outputd.service" in run.active
    assert "jasper-control.service" in run.active


@pytest.mark.parametrize(
    ("mask_state", "remask"),
    [
        ("masked", "systemctl mask jasper-voice.service"),
        ("masked-runtime", "systemctl mask --runtime jasper-voice.service"),
    ],
)
def test_unpark_reports_a_masked_unit_it_cannot_put_back(
    tmp_path, mask_state, remask
):
    """Stopping a running-but-masked unit is not something to hide.

    Masking does not stop a unit, so an operator can mask one and leave it
    running. The park stops it anyway, and systemd then refuses to start a
    masked unit. Since the enablement did not change under the install's hand,
    this is not the deliberate-off skip — it is a unit the install took away
    and cannot return, which has to reach the journal rather than pass as
    `left off on purpose`.

    The recovery the operator is handed must also be one that can work:
    `systemctl start` on a masked unit fails exactly as the trap's attempt
    just did, so the hint has to unmask first — and re-mask afterwards, or it
    hands back a unit whose mask this recovery quietly deleted and which a
    boot, a reconciler or another unit's `Wants=` can then start. The mask's
    scope has to survive too: re-masking a `masked-runtime` unit without
    `--runtime` would promote a /run-scoped mask to a permanent /etc one.

    `masked-runtime` is systemd's documented /run-scoped mask and is exercised
    here only through this fake — no in-repo path produces the state and it was
    NOT reproduced on hardware. What the parameter pins is that all three
    enablement case sites (park-time record, restore-time skip, and the
    recovery hint's scope) share one vocabulary: drop `masked-runtime` from the
    first two and this unit reads as "left off on purpose" instead of being
    reported; drop it from the third and the recovery over-masks.
    """
    run = _run_park_cycle(
        tmp_path,
        active=["jasper-voice.service", "jasper-control.service"],
        enablement={"jasper-voice.service": mask_state},
        scenario="mark_trap; exit 1",
    )

    assert "jasper-voice.service" not in run.active
    assert "left off on purpose" not in run.syslog, run.syslog
    assert (
        "event=build_sandbox.low_memory_build_unpark_failed "
        "unit=jasper-voice.service recover=systemctl unmask "
        "jasper-voice.service && systemctl start jasper-voice.service "
        f"&& {remask}" in run.syslog
    ), run.syslog
    assert "could not restart jasper-voice.service" in run.stderr
    assert (
        "recover with: sudo systemctl unmask jasper-voice.service "
        "&& sudo systemctl start jasper-voice.service "
        f"&& sudo {remask}" in run.stderr
    ), run.stderr
    # The rest of the recovery still happened.
    assert "jasper-control.service" in run.active


def test_unpark_is_a_no_op_when_the_install_already_restarted_everything(
    tmp_path,
):
    """The success path must not churn a live graph.

    A normal install restarts these units itself; the trap then runs with
    everything already back. It must issue no starts at all — the trap is
    recovery, not a second restart pass.
    """
    units = [
        "shairport-sync.service",
        "jasper-outputd.service",
        "jasper-fanin.service",
        "jasper-control.service",
    ]
    restart = "; ".join(f"systemctl start {unit}" for unit in units)
    run = _run_park_cycle(
        tmp_path,
        active=units,
        scenario=f"{restart}; mark_trap; exit 0",
    )

    assert run.returncode == 0, run.stderr
    assert run.started_by_trap() == []
    assert run.active == set(units)


def test_unpark_logs_a_stable_event_token_when_a_unit_will_not_restart(
    tmp_path,
):
    """"My speaker is dead" must leave something greppable behind.

    A bare warning is invisible to `journalctl | grep event=`, so a failed
    restore has its own stable token, and the summary carries the counts even
    when nothing needed restoring.
    """
    run = _run_park_cycle(
        tmp_path,
        active=["shairport-sync.service", "jasper-control.service"],
        scenario="mark_trap; exit 1",
        failstart=["shairport-sync.service"],
    )

    assert "shairport-sync.service" not in run.active
    assert "could not restart shairport-sync.service" in run.stderr
    assert (
        "event=build_sandbox.low_memory_build_unpark_failed "
        "unit=shairport-sync.service recover=systemctl start "
        "shairport-sync.service" in run.syslog
    ), run.syslog
    # Not masked, so the hint stays the plain start — the unmask branch must
    # not leak onto every failure.
    assert "unmask" not in run.stderr, run.stderr
    assert "unmask" not in run.syslog, run.syslog
    # The summary carries counts even when the failure above happened.
    assert "event=build_sandbox.low_memory_build_unpark " in run.syslog
    assert "restored=1 failed=1" in run.syslog, run.syslog
    # jasper-control was restored despite the earlier failure.
    assert "jasper-control.service" in run.active


def test_unpark_summary_records_the_zero_restored_case(tmp_path):
    """`restored=0` must be distinguishable from the trap never running."""
    units = ["shairport-sync.service", "jasper-control.service"]
    restart = "; ".join(f"systemctl start {unit}" for unit in units)
    run = _run_park_cycle(
        tmp_path, active=units, scenario=f"{restart}; mark_trap; exit 0"
    )
    assert "event=build_sandbox.low_memory_build_unpark " in run.syslog
    assert "restored=0 failed=0" in run.syslog, run.syslog


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("exit 0", 0),
        ("exit 1", 1),
        ("exit 7", 7),
        ("false", 1),  # set -e abort
        ("(exit 42)", 42),
    ],
)
def test_install_exit_cleanup_preserves_the_installers_exit_status(
    tmp_path, scenario, expected
):
    """The EXIT trap must not change the installer's exit status.

    scripts/deploy-to-pi.sh gates the whole deploy on install.sh's status, so a
    trap that turned a failed install green would be strictly worse than the
    bug being fixed. This pins the observable contract across the exit shapes
    the installer actually produces, not the implementation — note that the
    `local rc=$?` capture inside `install_exit_cleanup` is deliberately NOT
    falsifiable here, because every command in the trap is `|| true`-guarded
    and the trap already ends 0 either way (see the comment on that function
    for the measured A/B and the shape the capture does guard).
    """
    run = _run_park_cycle(
        tmp_path,
        active=["shairport-sync.service", "jasper-control.service"],
        scenario=scenario,
    )
    assert run.returncode == expected, run.stderr
    # The trap still ran, whatever the status.
    assert "shairport-sync.service" in run.active


def test_exit_trap_still_unparks_when_swap_cleanup_fails(tmp_path):
    """The `|| true` guards in the trap are what keep the unpark reachable.

    `install_exit_cleanup` asserts in prose that its guards are load-bearing:
    under `set -e` an unguarded failing command ABORTS the trap function, so
    every step after it — including the unpark — simply never runs, and an
    aborted install leaves the speaker dead. That is the exact failure the
    handler exists to prevent, and nothing asserted it: removing the `|| true`
    from `cleanup_build_swap || true` passed the whole suite.

    It is not a hypothetical edit. `cleanup_build_swap` returns 0 on every path
    today, but its `swapoff` step can genuinely fail on the low-memory box this
    whole path exists for — it already logs `swapoff_failed` for that case — so
    "make a failed swapoff fatal" is a plausible future change. This forces the
    guard to survive it.

    Measured on the harness's own shell (bash 3.2.57): with the guard, exit 5
    and every unit back; with `cleanup_build_swap` bare, the trap aborts at
    that line, the installer reports 1 instead of 5, and no unit is restored.
    """
    units = [
        "shairport-sync.service",
        "jasper-outputd.service",
        "jasper-control.service",
    ]
    run = _run_park_cycle(
        tmp_path,
        active=units,
        scenario="cleanup_build_swap() { return 1; }; mark_trap; exit 5",
    )

    # The installer's status still survives a failing cleanup step.
    assert run.returncode == 5, run.stderr
    # ...and, the point of the guard, the unpark still ran.
    for unit in units:
        assert unit in run.active, (
            f"{unit} was left dead: a failing cleanup step aborted the trap "
            f"before the unpark could run. stderr={run.stderr!r}"
        )


def test_exit_trap_finishes_the_unpark_when_its_own_logging_fails(tmp_path):
    """The trap's *second* `|| true` is what keeps the restore loop running.

    The two guards in `install_exit_cleanup` do different jobs. The one on
    `cleanup_build_swap` keeps the unpark REACHABLE (pinned above). The one on
    `unpark_low_memory_build_units` keeps it COMPLETABLE: a caller's `|| true`
    suspends `set -e` for the callee's entire body, and that is the only reason
    the unpark's three bare `_build_sandbox_log` calls — the deliberate-off
    skip, the failed-restart report, and the closing summary — cannot abort the
    loop. Remove it and the first failing log strands every unit after it.

    The failed-restart log is the worst place for that to happen: it fires from
    inside the per-unit helper, so the one unit that would not come back also
    takes the whole remaining restore down with it, which is the #2178 failure
    with extra steps. A `_build_sandbox_log` that logs and then returns
    non-zero is the smallest faithful stand-in — as shipped it ends in
    `logger ... || true` and cannot fail, so nothing else in the suite reaches
    this path.

    Measured on the harness's own shell (bash 3.2.57), six parked units with
    `jasper-fanin.service` refusing to start: with the guard, exit 5, five of
    six back and the summary emitted; with `unpark_low_memory_build_units`
    bare, exit 1, ONE of six back and no summary at all. Deliberately NOT
    measured on the bash 5.2.x the installer runs under (no 5.x available on
    the authoring host) — 3.2's `set -e` is the looser of the two, so an abort
    seen here is expected to hold there, but that step is inference.
    """
    # Restore order is the JASPER_LOW_MEMORY_UNPARK_FIRST entries that were
    # parked — here jasper-outputd, jasper-fanin, jasper-camilla, jasper-mux
    # (jasper-camilla-crossover is in that list but not running) — then the
    # rest in park order. Failing the SECOND one restored puts four units
    # behind the abort point.
    units = [
        "jasper-outputd.service",
        "jasper-fanin.service",
        "jasper-camilla.service",
        "jasper-mux.service",
        "jasper-control.service",
        "shairport-sync.service",
    ]
    run = _run_park_cycle(
        tmp_path,
        active=units,
        failstart=["jasper-fanin.service"],
        scenario=(
            "_build_sandbox_log() { "
            'echo "  build-sandbox: $2"; '
            'logger -t jasper-install -- "event=build_sandbox.$1 $2"; '
            "return 1; }; "
            "mark_trap; exit 5"
        ),
    )

    # The failing branch really was exercised — otherwise the log stub never
    # runs from inside the loop and this test proves nothing.
    assert (
        "event=build_sandbox.low_memory_build_unpark_failed "
        "unit=jasper-fanin.service" in run.syslog
    ), run.syslog
    # Every unit ordered AFTER the failure still came back.
    for unit in units:
        if unit == "jasper-fanin.service":
            continue
        assert unit in run.active, (
            f"{unit} was left dead: a failing log inside the unpark aborted "
            f"the restore loop before reaching it. stderr={run.stderr!r}"
        )
    # The summary is the last statement in the unpark, so its presence proves
    # the loop ran to the end rather than unwinding.
    assert "restored=5 failed=1" in run.syslog, run.syslog
    # The operator still gets a hint for the unit that would not come back. An
    # abort lands BETWEEN that log line and this warning, so the unguarded
    # shape returns an entirely empty stderr — the one unit that needed a
    # recovery command is the one whose recovery command is lost.
    assert "could not restart jasper-fanin.service" in run.stderr, run.stderr
    # ...and the abort did not clobber the installer's status either.
    assert run.returncode == 5, run.stderr


def test_both_install_paths_trap_the_unparking_handler():
    """The recovery is worthless if only one profile arms it."""
    install_sh = _INSTALL_SH.read_text(encoding="utf-8")
    assert "trap cleanup_build_swap EXIT" not in install_sh, (
        "install.sh must trap the combined handler so an aborted install "
        "unparks; the swap-only trap leaves the speaker dead"
    )
    assert install_sh.count("trap install_exit_cleanup EXIT") == 2, (
        "both the streambox and full install paths need the unparking trap"
    )


def test_low_memory_park_reuses_the_shared_core_graph_park_list():
    """Phase one's names have one owner.

    `JASPER_CORE_GRAPH_PARK_UNITS` is the canonical list shared with the
    runtime recovery handler. The snapshot must iterate it rather than copy
    the names, or a new renderer added there would silently stop being
    restored here.
    """
    systemd_units = (_INSTALL_LIB_DIR / "systemd-units.sh").read_text(
        encoding="utf-8"
    )
    body = systemd_units.split("park_low_memory_build_units() {", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert '"${JASPER_CORE_GRAPH_PARK_UNITS[@]}"' in body
    for renderer in ("shairport-sync.service", "librespot.service"):
        assert renderer not in body, (
            f"{renderer} was re-inlined into park_low_memory_build_units; "
            "iterate JASPER_CORE_GRAPH_PARK_UNITS instead"
        )


def test_install_removes_the_retired_audio_topology_state():
    """The doctor's remedy for the ghost topology file must name a real remover.

    `jasper-doctor`'s `check_fanin_asound_wiring` WARNs when
    `/var/lib/jasper/audio_topology.env` is present and tells the operator to
    re-run the installer. #2285 deleted the migration that used to remove it and
    very nearly shipped the WARN with nothing behind it — a warning no operator
    action could ever clear, which is worse than either half alone.

    So this pins the PAIR, not the function: the doctor's promise on one side,
    an installer step that actually deletes the file on the other, wired into
    BOTH profiles. Asserting only that the function exists would still pass if it
    were never called.
    """
    migrations = (_INSTALL_LIB_DIR / "env-migrations.sh").read_text(
        encoding="utf-8"
    )
    body = migrations.split("remove_retired_audio_topology_state() {", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert 'rm -f "${STATE_DIR}/audio_topology.env"' in body

    install_sh = _INSTALL_SH.read_text(encoding="utf-8")
    # Both profiles, because a box only carries the ghost file if it predates
    # the switch's retirement, and that is true on either profile.
    assert install_sh.count("\n        remove_retired_audio_topology_state") == 1
    assert install_sh.count("\n    remove_retired_audio_topology_state") == 1

    # The doctor's side of the pair: the remedy still names re-running the
    # installer, which the step above is what makes true.
    doctor_src = (
        REPO_ROOT / "jasper" / "cli" / "doctor" / "audio_runtime_fanin.py"
    ).read_text(encoding="utf-8")
    assert "audio_topology.env" in doctor_src
    assert "Re-run " in doctor_src
