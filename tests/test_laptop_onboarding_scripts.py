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
ISOLATED_SCRIPTS = (DEPLOY, ONBOARD, LIB, USE)


def git_head(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", *args],
        text=True,
    ).strip()


FAKE_SSH = r"""#!/usr/bin/env bash
set -euo pipefail
printf 'SSH' >> "$FAKE_LOG"
for arg in "$@"; do printf ' %q' "$arg" >> "$FAKE_LOG"; done
printf '\n' >> "$FAKE_LOG"

# A re-imaged Pi answers on the same hostname with a new host key;
# StrictHostKeyChecking=accept-new refuses it before authentication.
if [[ "${FAKE_HOST_KEY_CHANGED:-0}" == "1" ]]; then
  cat >&2 <<'BANNER'
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @
@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@
IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!
Host key verification failed.
BANNER
  exit 255
fi

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
  sudo*cat\ /var/lib/jasper/peer_id*)
    # Each fake speaker owns a stable, distinct identity, so a deploy
    # redirected to another host reads another host's peer_id.
    for a in "$@"; do
      case "$a" in *@*) printf 'peer-%s\n' "${a#*@}"; break ;; esac
    done
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
  cat\ /var/lib/jasper/install_profile*)
    [[ "${FAKE_METADATA_PROBES_FAIL:-0}" == "1" ]] && exit 1
    printf '%s\n' "${FAKE_INSTALL_PROFILE:-full}"
    ;;
  cat\ /run/jasper-install/reboot_required*)
    # Empty by default (no reboot pending); tests that want the reboot
    # branch set FAKE_REBOOT_REQUIRED.
    [[ -n "${FAKE_REBOOT_REQUIRED:-}" ]] && printf '%s\n' "${FAKE_REBOOT_REQUIRED}"
    ;;
  tr\ -d*\/proc\/device-tree\/model*)
    [[ "${FAKE_METADATA_PROBES_FAIL:-0}" == "1" ]] && exit 1
    printf '%s\n' "${FAKE_PI_MODEL:-Raspberry Pi 5 Model B Rev 1.0}"
    ;;
  *jasper.output_hardware*load_state*)
    [[ "${FAKE_METADATA_PROBES_FAIL:-0}" == "1" ]] && exit 1
    printf '%s\n' "${FAKE_OUTPUT_STATUS:-ready}"
    ;;
  *\/deploy\/install.sh*)
    exit "${FAKE_INSTALL_SSH_RC:-0}"
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


FAKE_PING = r"""#!/usr/bin/env bash
exit 0
"""


# Logged, never functional: nothing in these scripts may edit the
# operator's known_hosts for them.
FAKE_SSH_KEYGEN = r"""#!/usr/bin/env bash
printf 'SSH-KEYGEN' >> "$FAKE_LOG"
for arg in "$@"; do printf ' %q' "$arg" >> "$FAKE_LOG"; done
printf '\n' >> "$FAKE_LOG"
exit 0
"""


@contextmanager
def isolated_checkout(env_local: str | None, *, dirty: bool = False):
    """Clone a disposable checkout with independent state and index."""
    with tempfile.TemporaryDirectory(prefix="jts-laptop-scripts-") as tmp:
        checkout = Path(tmp) / "checkout"
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--shared",
                "--no-tags",
                str(ROOT),
                str(checkout),
            ],
            check=True,
        )

        scripts = checkout / "scripts"
        for source in ISOLATED_SCRIPTS:
            shutil.copy2(source, scripts / source.name)

        if env_local is not None:
            (checkout / ".env.local").write_text(env_local, encoding="utf-8")
        if dirty:
            with (scripts / "_lib.sh").open("a", encoding="utf-8") as stream:
                stream.write(
                    "\nprintf '%s\\n' 'dirty live-script overlay executed'\n"
                )

        # Refresh the disposable index's stat cache without treating a
        # legitimate live-script overlay as a harness failure.
        subprocess.run(
            ["git", "status", "--short"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        yield checkout


class FakeRemote:
    def __init__(self, test_case: unittest.TestCase):
        self.test_case = test_case
        self.tmp = Path(tempfile.mkdtemp(prefix="jts-fake-remote-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        self._write_executable(self.bin / "ssh", FAKE_SSH)
        self._write_executable(self.bin / "rsync", FAKE_RSYNC)
        self._write_executable(self.bin / "ping", FAKE_PING)
        self._write_executable(self.bin / "ssh-keygen", FAKE_SSH_KEYGEN)
        test_case.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    @staticmethod
    def _write_executable(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    def env(self, **overrides: str) -> dict[str, str]:
        env = os.environ.copy()
        # An explicitly-set targeting value outranks .env.local, so an
        # ambient one would silently retarget every deploy driven here.
        for key in ("PI_HOST", "PI_USER", "JASPER_HOSTNAME"):
            env.pop(key, None)
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
        dirty_checkout: bool = False,
        **env_overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        env = fake.env(**env_overrides)
        with isolated_checkout(env_local, dirty=dirty_checkout) as checkout:
            deploy = checkout / "scripts" / "deploy-to-pi.sh"
            if use_pty:
                return run_with_pty(
                    ["bash", str(deploy)], cwd=checkout, env=env
                )
            return subprocess.run(
                ["bash", str(deploy)],
                cwd=checkout,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

    def run_onboard(
        self,
        fake: FakeRemote,
        *,
        profile: str,
        model: str,
        output_status: str,
        metadata_probes_fail: bool = False,
        **env_overrides: str,
    ) -> subprocess.CompletedProcess[str]:
        home = fake.tmp / "home"
        ssh_dir = home / ".ssh"
        ssh_dir.mkdir(parents=True)
        (ssh_dir / "id_ed25519.pub").write_text(
            "ssh-ed25519 fake onboarding test key\n", encoding="utf-8"
        )
        env = fake.env(
            HOME=str(home),
            FAKE_INSTALL_PROFILE=profile,
            FAKE_PI_MODEL=model,
            FAKE_OUTPUT_STATUS=output_status,
            FAKE_METADATA_PROBES_FAIL="1" if metadata_probes_fail else "0",
            **env_overrides,
        )
        with isolated_checkout(None) as checkout:
            return subprocess.run(
                [
                    "bash",
                    str(checkout / "scripts" / "onboard.sh"),
                    "jts4.local",
                ],
                cwd=checkout,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

    def test_laptop_onboarding_scripts_are_valid_bash(self):
        for script in (LIB, ONBOARD, DEPLOY, USE):
            subprocess.run(["bash", "-n", str(script)], check=True)

    def test_onboard_help_leads_with_adopt_beginner_path(self):
        with isolated_checkout(None) as checkout:
            result = subprocess.run(
                ["bash", str(checkout / "scripts" / "onboard.sh"), "--help"],
                cwd=checkout,
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

    def test_streambox_completion_is_capability_and_hardware_aware(self):
        fake = FakeRemote(self)
        result = self.run_onboard(
            fake,
            profile="streambox",
            model="Raspberry Pi Zero 2 W Rev 1.0",
            output_status="missing",
        )

        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, combined)
        self.assertIn("Raspberry Pi Zero 2 W Rev 1.0", result.stdout)
        self.assertIn("Install profile:   streambox", result.stdout)
        self.assertIn("This Streambox provides AirPlay", result.stdout)
        self.assertIn(
            "intentionally omits the local\nvoice and microphone brain",
            result.stdout,
        )
        for page in ("sources", "spotify", "sound", "sound/pair", "system"):
            self.assertIn(f"http://jts4.local/{page}/", result.stdout)
        self.assertNotIn("http://jts4.local/voice/", result.stdout)
        self.assertNotIn("http://jts4.local/transit/", result.stdout)
        self.assertIn("Audio output is safely parked", result.stdout)
        self.assertIn("Apple\nUSB-C to 3.5 mm dongle", result.stdout)

    def test_full_completion_retains_voice_guidance(self):
        fake = FakeRemote(self)
        result = self.run_onboard(
            fake,
            profile="full",
            model="Raspberry Pi 5 Model B Rev 1.0",
            output_status="ready",
        )

        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, combined)
        self.assertIn("Install profile:   full", result.stdout)
        self.assertIn("http://jts4.local/sound/setup/", result.stdout)
        self.assertIn("choose mono/stereo + passive/active", result.stdout)
        self.assertIn("audio stays off until saved", result.stdout)
        self.assertIn("http://jts4.local/voice/", result.stdout)
        self.assertIn("http://jts4.local/transit/", result.stdout)
        self.assertNotIn("This Streambox provides", result.stdout)
        self.assertNotIn("Audio output is safely parked", result.stdout)

    def test_metadata_probe_failures_keep_neutral_completion_successful(self):
        fake = FakeRemote(self)
        result = self.run_onboard(
            fake,
            profile="streambox",
            model="stale model must not leak",
            output_status="missing",
            metadata_probes_fail=True,
        )

        combined = result.stdout + result.stderr
        calls = fake.calls()
        self.assertEqual(result.returncode, 0, combined)
        self.assertIn("cat\\ /var/lib/jasper/install_profile", calls)
        self.assertIn("/proc/device-tree/model", calls)
        self.assertIn("jasper.output_hardware", calls)
        self.assertIn("Install profile:   unknown", result.stdout)
        self.assertIn(
            "Raspberry Pi:      unknown Raspberry Pi model",
            result.stdout,
        )
        self.assertIn("http://jts4.local/system/", result.stdout)
        for page in (
            "sources",
            "spotify",
            "sound",
            "sound/pair",
            "voice",
            "transit",
        ):
            self.assertNotIn(f"http://jts4.local/{page}/", result.stdout)
        self.assertNotIn("Audio output is safely parked", result.stdout)
        self.assertNotIn("stale model must not leak", result.stdout)
        self.assertIn(
            "completion guidance will stay capability-neutral",
            result.stderr,
        )
        self.assertIn(
            "event=onboard.profile status=warn profile=unknown",
            result.stdout,
        )

    def test_reboot_marker_present_takes_the_reboot_branch(self):
        """install.sh's reboot-required marker (#2110) makes onboard reboot
        and wait before validating, instead of running doctor against
        pre-reboot state."""
        fake = FakeRemote(self)
        result = self.run_onboard(
            fake,
            profile="full",
            model="Raspberry Pi 5 Model B Rev 1.0",
            output_status="ready",
            FAKE_REBOOT_REQUIRED="zram=resize pending",
            JTS_ONBOARD_REBOOT_DOWN_ATTEMPTS="1",
            JTS_ONBOARD_REBOOT_DOWN_SLEEP="0",
        )

        combined = result.stdout + result.stderr
        calls = fake.calls()
        self.assertEqual(result.returncode, 0, combined)
        self.assertIn("cat\\ /run/jasper-install/reboot_required", calls)
        self.assertIn("reboot required: zram=resize pending", result.stdout)
        self.assertIn("waiting for jts4.local to go down and come back", result.stdout)
        self.assertIn("jts4.local is back up", result.stdout)
        self.assertIn("sudo\\ -n\\ reboot", calls)
        self.assertIn("event=onboard.reboot status=ok", result.stdout)

    def test_no_reboot_marker_goes_straight_to_doctor(self):
        """The common case: no boot-only migration ran, so onboard never
        reboots and validates immediately."""
        fake = FakeRemote(self)
        result = self.run_onboard(
            fake,
            profile="full",
            model="Raspberry Pi 5 Model B Rev 1.0",
            output_status="ready",
        )

        combined = result.stdout + result.stderr
        calls = fake.calls()
        self.assertEqual(result.returncode, 0, combined)
        self.assertIn("cat\\ /run/jasper-install/reboot_required", calls)
        self.assertNotIn("reboot required:", result.stdout)
        self.assertNotIn("waiting for jts4.local", result.stdout)
        self.assertNotIn("sudo\\ -n\\ reboot", calls)

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

    def test_changed_host_key_names_the_manual_remedy_and_removes_nothing(self):
        # A re-imaged Pi answers on the same hostname with a new host key,
        # which accept-new refuses (issue #2114). Both operator scripts
        # must surface the one-command fix and leave the removal — and so
        # the trust decision — to the operator.
        deploy_fake = FakeRemote(self)
        deploy = self.run_deploy(
            deploy_fake,
            env_local=None,
            PI_HOST="jts3.local",
            PI_USER="pi",
            JASPER_HOSTNAME="jts3.local",
            FAKE_HOST_KEY_CHANGED="1",
        )
        self.assertNotEqual(deploy.returncode, 0, deploy.stdout + deploy.stderr)
        self.assertIn("ssh-keygen -R jts3.local", deploy.stderr)
        self.assertNotIn("SSH-KEYGEN", deploy_fake.calls())
        self.assertNotIn("RSYNC", deploy_fake.calls())

        onboard_fake = FakeRemote(self)
        onboard = self.run_onboard(
            onboard_fake,
            profile="full",
            model="Raspberry Pi 5 Model B Rev 1.0",
            output_status="ready",
            FAKE_HOST_KEY_CHANGED="1",
        )
        self.assertNotEqual(
            onboard.returncode, 0, onboard.stdout + onboard.stderr
        )
        self.assertIn("ssh-keygen -R jts4.local", onboard.stderr)
        self.assertIn("status=fail reason=host_key_changed", onboard.stdout)
        self.assertNotIn("SSH-KEYGEN", onboard_fake.calls())

    def test_deploy_preserves_ssh_status_255_and_reports_unknown_outcome(self):
        fake = FakeRemote(self)
        result = self.run_deploy(
            fake,
            env_local=None,
            PI_HOST="jts3.local",
            PI_USER="pi",
            JASPER_HOSTNAME="jts3.local",
            FAKE_INSTALL_SSH_RC="255",
        )

        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 255, combined)
        self.assertIn("DEPLOY OUTCOME UNKNOWN", result.stderr)
        self.assertIn("ssh exited 255 while install.sh was", result.stderr)
        self.assertIn("no trustworthy remote completion", result.stderr)
        self.assertIn("build manifest was not verified", result.stderr)
        self.assertNotIn("build manifest was NOT advanced", result.stderr)
        self.assertNotIn("==> Done.", combined)

    def test_deploy_preserves_ordinary_install_failure(self):
        fake = FakeRemote(self)
        result = self.run_deploy(
            fake,
            env_local=None,
            PI_HOST="jts3.local",
            PI_USER="pi",
            JASPER_HOSTNAME="jts3.local",
            FAKE_INSTALL_SSH_RC="42",
        )

        combined = result.stdout + result.stderr
        self.assertEqual(result.returncode, 42, combined)
        self.assertIn(
            "DEPLOY FAILED: install.sh exited 42 on jts3.local.",
            result.stderr,
        )
        self.assertIn("build manifest was NOT advanced", result.stderr)
        self.assertNotIn("DEPLOY OUTCOME UNKNOWN", result.stderr)
        self.assertNotIn("==> Done.", combined)

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
        # The short sha's auto-abbreviated length differs between this
        # checkout and the disposable clone the script runs in; pin only
        # that it is a prefix of the full sha.
        sha_full = git_head("HEAD")
        self.assertRegex(
            result.stdout, rf"sha:    {sha_full[:7]}[0-9a-f]* \({sha_full}\)"
        )
        self.assertNotIn("-dirty", result.stdout)
        self.assertIn("alice@jts3.local:/home/alice/jts/", calls)
        self.assertIn("sudo\\ -n\\ JASPER_DEPLOY_SHA=", calls)
        self.assertIn("/home/alice/jts/deploy/install.sh", calls)
        self.assertNotIn("SSH -tt", calls)

    def test_deploy_prints_reboot_marker_without_rebooting(self):
        """Ordinary deploy-to-pi.sh redeploys keep the cautious no-surprise
        posture (#2110): read and print install.sh's reboot-required
        marker, but never reboot on their own — only onboard.sh does."""
        fake = FakeRemote(self)
        result = self.run_deploy(
            fake,
            env_local=None,
            PI_HOST="jts3.local",
            PI_USER="pi",
            JASPER_HOSTNAME="jts3.local",
            FAKE_REBOOT_REQUIRED="cgroup_memory=cmdline.txt updated",
        )

        calls = fake.calls()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("cat\\ /run/jasper-install/reboot_required", calls)
        self.assertIn(
            "reboot required (not applied by this deploy): "
            "cgroup_memory=cmdline.txt updated",
            result.stdout,
        )
        self.assertNotIn("sudo\\ -n\\ reboot", calls)
        self.assertNotIn("sudo\\ reboot", calls)

    def test_deploy_runs_and_marks_an_overlaid_live_script_dirty(self):
        fake = FakeRemote(self)
        result = self.run_deploy(
            fake,
            env_local=None,
            dirty_checkout=True,
            PI_HOST="jts3.local",
            PI_USER="pi",
            JASPER_HOSTNAME="jts3.local",
        )

        sha_full = git_head("HEAD")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("dirty live-script overlay executed", result.stdout)
        self.assertRegex(
            result.stdout,
            rf"sha:    {sha_full[:7]}[0-9a-f]*-dirty \({sha_full}\)",
        )

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
        repo_state_before = (
            ENV_LOCAL.read_bytes() if ENV_LOCAL.exists() else None
        )
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
        repo_state_after = (
            ENV_LOCAL.read_bytes() if ENV_LOCAL.exists() else None
        )
        self.assertEqual(repo_state_after, repo_state_before)

    def test_explicitly_set_targeting_outranks_env_local(self):
        """The caller's target moves as one record; the checkout's never mixes.

        Sourcing .env.local under `set -a` used to overwrite the PI_HOST an
        operator passed on the command line, so a deploy aimed at one speaker
        went to the checkout's usual one while the identity guard — reading
        PI_PEER_ID from that same clobbered file — called it verified.
        Per-key precedence leaves the other half of that hazard: the SSH
        target taken from one source and the cert CN/SAN from the other
        deploys to one speaker under another speaker's name.
        """
        env_local = textwrap.dedent(
            """\
            PI_HOST=jts3.local
            PI_USER=pi
            JASPER_HOSTNAME=jts3.local
            """
        )
        callers = (
            {
                "PI_HOST": "jts9.local",
                "PI_USER": "operator",
                "JASPER_HOSTNAME": "jts9.local",
            },
            {"JASPER_HOSTNAME": "jts9.local"},
            {"PI_HOST": "jts9.local"},
        )
        for caller in callers:
            with self.subTest(**caller):
                fake = FakeRemote(self)
                result = self.run_deploy(fake, env_local=env_local, **caller)

                calls = fake.calls()
                self.assertEqual(
                    result.returncode, 0, result.stdout + result.stderr
                )
                user = caller.get("PI_USER", "pi")
                self.assertIn(f"{user}@jts9.local", calls)
                self.assertIn("JASPER_HOSTNAME=jts9.local", calls)
                self.assertNotIn("jts3.local", calls)

    def test_a_caller_redirect_leaves_the_checkouts_identity_record_alone(self):
        """A deploy you aimed elsewhere must not rewrite this checkout's TOFU.

        The recorded peer_id describes the speaker `.env.local` names. Letting
        a redirected deploy record the OTHER Pi's id — or re-record it under
        JTS_ACCEPT_NEW_IDENTITY=1 — leaves the checkout still pointed at its
        own speaker while claiming that speaker's identity is the other one,
        so the next plain deploy aborts against the Pi it has always used.
        """
        env_local = textwrap.dedent(
            """\
            PI_HOST=jts.local
            PI_USER=pi
            JASPER_HOSTNAME=jts.local
            """
        )
        with isolated_checkout(env_local) as checkout:
            deploy = checkout / "scripts" / "deploy-to-pi.sh"
            state = checkout / ".env.local"
            before = state.read_bytes()

            def run(**env_overrides: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["bash", str(deploy)],
                    cwd=checkout,
                    env=FakeRemote(self).env(**env_overrides),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )

            # The checkout's own speaker records on first contact and
            # verifies against that record on the next deploy...
            for _ in range(2):
                plain = run()
                self.assertEqual(
                    plain.returncode, 0, plain.stdout + plain.stderr
                )
            recorded = state.read_bytes()
            self.assertIn("PI_PEER_ID=peer-jts.local", recorded.decode())
            self.assertNotEqual(recorded, before)

            # ...and with that record standing, a redirect neither reads
            # nor rewrites it — including under the re-record override.
            redirect = run(PI_HOST="jts2.local")
            self.assertEqual(
                redirect.returncode, 0, redirect.stdout + redirect.stderr
            )
            self.assertEqual(state.read_bytes(), recorded)

            accepted = run(PI_HOST="jts2.local", JTS_ACCEPT_NEW_IDENTITY="1")
            self.assertEqual(
                accepted.returncode, 0, accepted.stdout + accepted.stderr
            )
            self.assertEqual(state.read_bytes(), recorded)

    def test_naming_the_checkouts_own_host_still_records_and_verifies(self):
        """The setup scripts' own shape: write .env.local, then deploy to it.

        onboard.sh and rename-speaker.sh both call write_laptop_state and then
        invoke deploy-to-pi.sh with PI_HOST/PI_USER/JASPER_HOSTNAME set to the
        values they just wrote. Naming the host the file already names is the
        checkout's own speaker, not a redirect, so it is how the record gets
        established at all — and a plain `PI_HOST=<own host>` deploy is
        verified rather than silently unguarded.
        """
        env_local = textwrap.dedent(
            """\
            PI_HOST=jts.local
            PI_USER=pi
            JASPER_HOSTNAME=jts.local
            """
        )
        with isolated_checkout(env_local) as checkout:
            deploy = checkout / "scripts" / "deploy-to-pi.sh"
            state = checkout / ".env.local"

            for _ in range(2):
                onboard_shape = subprocess.run(
                    ["bash", str(deploy)],
                    cwd=checkout,
                    env=FakeRemote(self).env(
                        PI_HOST="jts.local",
                        PI_USER="pi",
                        JASPER_HOSTNAME="jts.local",
                    ),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(
                    onboard_shape.returncode,
                    0,
                    onboard_shape.stdout + onboard_shape.stderr,
                )
            self.assertIn("PI_PEER_ID=peer-jts.local", state.read_text())

    def test_lib_keeps_jasper_hostname_as_legacy_pi_host_fallback(self):
        env = os.environ.copy()
        env.pop("PI_HOST", None)
        env.pop("PI_USER", None)
        env["JASPER_HOSTNAME"] = "legacy-speaker.local"

        with isolated_checkout(None) as checkout:
            lib = checkout / "scripts" / "_lib.sh"
            script = textwrap.dedent(
                f"""\
                set -euo pipefail
                . {lib}
                printf '%s\\n' "$PI_HOST"
                printf '%s\\n' "$PI_USER"
                """
            )
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=checkout,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["legacy-speaker.local", "pi"])

    def test_write_laptop_state_persists_ip_and_speaker_separately(self):
        with isolated_checkout(None) as checkout:
            lib = checkout / "scripts" / "_lib.sh"
            script = textwrap.dedent(
                f"""\
                set -euo pipefail
                JTS_LIB_TARGET_OPTIONAL=1 . {lib}
                write_laptop_state 192.168.1.42 pi "" jts3.local
                """
            )
            subprocess.run(["bash", "-c", script], cwd=checkout, check=True)
            env_text = (checkout / ".env.local").read_text(encoding="utf-8")

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
