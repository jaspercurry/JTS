# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import pty
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "scripts" / "deploy-to-pi.sh"
ONBOARD = ROOT / "scripts" / "onboard.sh"
LIB = ROOT / "scripts" / "_lib.sh"
USE = ROOT / "scripts" / "use"
ENV_LOCAL = ROOT / ".env.local"


FAKE_SSH = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'SSH' >> "$FAKE_LOG"
for arg in "$@"; do printf ' %q' "$arg" >> "$FAKE_LOG"; done
printf '\n' >> "$FAKE_LOG"

cmd="${*: -1}"
case "$cmd" in
  'printf "%s\n" "$HOME"')
    printf '%s\n' "${FAKE_HOME:-/home/pi}"
    ;;
  'hostname -s 2>/dev/null || hostname')
    printf '%s\n' "${FAKE_HOSTNAME:-jts3}"
    ;;
  'sudo -n true')
    exit "${FAKE_SUDO_N_RC:-0}"
    ;;
  'sudo -v')
    exit 0
    ;;
  mkdir\ -p*)
    exit 0
    ;;
  sudo\ -n\ cat\ /var/lib/jasper/build.txt*)
    printf 'fake-build\n'
    ;;
  sudo\ cat\ /var/lib/jasper/build.txt*)
    printf 'fake-build\n'
    ;;
  sudo\ -n\ cat\ /var/lib/jasper/install_profile*)
    printf '%s\n' "${FAKE_INSTALL_PROFILE:-full}"
    ;;
  sudo\ cat\ /var/lib/jasper/install_profile*)
    printf '%s\n' "${FAKE_INSTALL_PROFILE:-full}"
    ;;
  sudo\ -n*)
    exit 0
    ;;
  sudo*)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
"""


FAKE_RSYNC = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'RSYNC' >> "$FAKE_LOG"
for arg in "$@"; do printf ' %q' "$arg" >> "$FAKE_LOG"; done
printf '\n' >> "$FAKE_LOG"
"""


@contextmanager
def repo_env_local(contents: str | None):
    """Temporarily control the checkout's gitignored .env.local."""
    existed = ENV_LOCAL.exists()
    old = ENV_LOCAL.read_bytes() if existed else b""
    try:
        if contents is None:
            ENV_LOCAL.unlink(missing_ok=True)
        else:
            ENV_LOCAL.write_text(contents, encoding="utf-8")
        yield
    finally:
        if existed:
            ENV_LOCAL.write_bytes(old)
        else:
            ENV_LOCAL.unlink(missing_ok=True)


class FakeRemote:
    def __init__(self, test_case: unittest.TestCase):
        self.test_case = test_case
        self.tmp = Path(tempfile.mkdtemp(prefix="jts-fake-remote-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        self._write_executable(self.bin / "ssh", FAKE_SSH)
        self._write_executable(self.bin / "rsync", FAKE_RSYNC)
        test_case.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    @staticmethod
    def _write_executable(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def env(self, **overrides: str) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{env['PATH']}",
                "FAKE_LOG": str(self.log),
                "SKIP_RESTART": "1",
                "SKIP_AIRPLAY_HEALTH_SUPPRESS": "1",
            }
        )
        env.update(overrides)
        return env

    def calls(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""


def run_with_pty(
    cmd: list[str], *, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
        )
    finally:
        os.close(slave_fd)
    try:
        stdout, stderr = proc.communicate(timeout=10)
    finally:
        os.close(master_fd)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


class LaptopOnboardingScriptsTest(unittest.TestCase):
    def run_deploy(
        self,
        fake: FakeRemote,
        *,
        env_local: str | None = None,
        use_pty: bool = False,
        **env_overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        env = fake.env(**env_overrides)
        with repo_env_local(env_local):
            if use_pty:
                return run_with_pty(
                    ["bash", str(DEPLOY)], cwd=ROOT, env=env
                )
            return subprocess.run(
                ["bash", str(DEPLOY)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

    def test_laptop_onboarding_scripts_are_valid_bash(self):
        for script in (LIB, ONBOARD, DEPLOY, USE):
            subprocess.run(["bash", "-n", str(script)], check=True)

    def test_onboard_help_leads_with_adopt_beginner_path(self):
        with repo_env_local(None):
            result = subprocess.run(
                ["bash", str(ONBOARD), "--help"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Beginner/friendly path", result.stdout)
        self.assertIn("Advanced/unattended path", result.stdout)
        self.assertLess(
            result.stdout.index("bash scripts/onboard.sh jts.local --adopt"),
            result.stdout.index("Advanced/unattended path"),
        )

    def test_unattended_sudo_failure_exits_before_mkdir_rsync_or_install(self):
        fake = FakeRemote(self)
        result = self.run_deploy(
            fake,
            env_local=None,
            PI_HOST="jts3.local",
            PI_USER="pi",
            JTS_DEPLOY_SUDO_MODE="unattended",
            FAKE_SUDO_N_RC="1",
        )

        calls = fake.calls()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("sudo\\ -n\\ true", calls)
        self.assertNotIn("mkdir\\ -p", calls)
        self.assertNotIn("RSYNC", calls)
        self.assertNotIn("deploy/install.sh", calls)

    def test_passwordless_sudo_uses_noninteractive_sudo_and_remote_home(self):
        fake = FakeRemote(self)
        result = self.run_deploy(
            fake,
            env_local=None,
            PI_HOST="jts3.local",
            PI_USER="alice",
            JASPER_HOSTNAME="jts3.local",
            FAKE_HOME="/home/alice",
        )

        calls = fake.calls()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("alice@jts3.local:/home/alice/jts/", calls)
        self.assertIn("sudo\\ -n\\ JASPER_DEPLOY_SHA=", calls)
        self.assertIn("/home/alice/jts/deploy/install.sh", calls)
        self.assertNotIn("SSH -tt", calls)

    def test_deploy_forwards_documented_build_sandbox_knobs(self):
        fake = FakeRemote(self)
        result = self.run_deploy(
            fake,
            env_local=None,
            PI_HOST="jts3.local",
            PI_USER="pi",
            JASPER_HOSTNAME="jts3.local",
            JASPER_BUILD_SANDBOX_OOM_SCORE_ADJ="0",
            JASPER_BUILD_SANDBOX_MEMORY_HIGH="900M",
            JASPER_BUILD_SWAP_SIZE_MB="3072",
            JASPER_RUST_LOW_MEMORY_BUILD="1",
        )

        calls = fake.calls()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("JASPER_BUILD_SANDBOX_OOM_SCORE_ADJ=0", calls)
        self.assertIn("JASPER_BUILD_SANDBOX_MEMORY_HIGH=900M", calls)
        self.assertIn("JASPER_BUILD_SWAP_SIZE_MB=3072", calls)
        self.assertIn("JASPER_RUST_LOW_MEMORY_BUILD=1", calls)

    def test_interactive_sudo_fallback_uses_tty_without_password_plumbing(self):
        fake = FakeRemote(self)
        result = self.run_deploy(
            fake,
            env_local=None,
            use_pty=True,
            PI_HOST="jts3.local",
            PI_USER="pi",
            JASPER_HOSTNAME="jts3.local",
            FAKE_SUDO_N_RC="1",
        )

        calls = fake.calls()
        combined = result.stdout + result.stderr + calls
        self.assertEqual(result.returncode, 0, combined)
        self.assertIn("SSH -tt", calls)
        self.assertIn("sudo\\ -v", calls)
        self.assertIn("sudo\\ JASPER_DEPLOY_SHA=", calls)
        for forbidden in (
            "sudo -S",
            "sudo\\ -S",
            "SUDO_ASKPASS",
            "read -s",
            "read\\ -s",
            "password=",
        ):
            self.assertNotIn(forbidden, combined)

    def test_ip_target_resolves_hostname_and_does_not_forward_ip_identity(self):
        fake = FakeRemote(self)
        result = self.run_deploy(
            fake,
            env_local=None,
            PI_HOST="192.168.1.42",
            PI_USER="pi",
            FAKE_HOSTNAME="jts3",
        )

        calls = fake.calls()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("hostname\\ -s", calls)
        self.assertIn("JASPER_HOSTNAME=jts3.local", calls)
        self.assertNotIn("JASPER_HOSTNAME=192.168.1.42", calls)

    def test_env_local_multispeaker_targeting_is_honored(self):
        fake = FakeRemote(self)
        env_local = textwrap.dedent(
            """\
            PI_HOST=jts3.local
            PI_USER=pi
            JASPER_HOSTNAME=jts3.local
            """
        )
        result = self.run_deploy(fake, env_local=env_local)

        calls = fake.calls()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("pi@jts3.local", calls)
        self.assertIn("JASPER_HOSTNAME=jts3.local", calls)
        self.assertNotIn("pi@jts.local", calls)

    def test_lib_keeps_jasper_hostname_as_legacy_pi_host_fallback(self):
        env = os.environ.copy()
        env.pop("PI_HOST", None)
        env.pop("PI_USER", None)
        env["JASPER_HOSTNAME"] = "legacy-speaker.local"
        script = textwrap.dedent(
            f"""\
            set -euo pipefail
            . {LIB}
            printf '%s\\n' "$PI_HOST"
            printf '%s\\n' "$PI_USER"
            """
        )

        with repo_env_local(None):
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["legacy-speaker.local", "pi"])

    def test_write_laptop_state_persists_ip_and_speaker_separately(self):
        with tempfile.TemporaryDirectory(prefix="jts-state-") as tmp:
            script = textwrap.dedent(
                f"""\
                set -euo pipefail
                . {LIB}
                REPO_ROOT={tmp!r}
                write_laptop_state 192.168.1.42 pi "" jts3.local
                """
            )
            subprocess.run(["bash", "-c", script], cwd=ROOT, check=True)
            env_text = (Path(tmp) / ".env.local").read_text(encoding="utf-8")

        self.assertIn("PI_HOST=192.168.1.42\n", env_text)
        self.assertIn("PI_USER=pi\n", env_text)
        self.assertIn("JASPER_HOSTNAME=jts3.local\n", env_text)

    def test_deploy_does_not_hardcode_pi_home_checkout(self):
        text = DEPLOY.read_text(encoding="utf-8")

        self.assertNotIn(":/home/pi/jts/", text)
        self.assertIn('REMOTE_REPO_DIR="${remote_home}/jts"', text)
        self.assertIn(
            'bash $(shell_quote "${REMOTE_REPO_DIR}/deploy/install.sh")',
            text,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
