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


def fake_peer_id(host: str) -> str:
    """The identity FAKE_SSH reports for a given fake speaker."""
    hexed = (host.encode().hex() + "0" * 32)[:32]
    return "-".join(
        (hexed[:8], hexed[8:12], hexed[12:16], hexed[16:20], hexed[20:32])
    )


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

# Each fake speaker owns a stable, distinct UUID (install.sh writes a
# uuid4), so a deploy redirected to another host reads another identity.
# FAKE_PEER_ID overrides it; set-but-empty models a pre-identity Pi.
fake_peer_id() {
  local host="" hex
  if [[ -n "${FAKE_PEER_ID+x}" ]]; then printf '%s' "$FAKE_PEER_ID"; return 0; fi
  for a in "$@"; do case "$a" in *@*) host="${a#*@}"; break ;; esac; done
  hex="$(printf '%s' "$host" | od -An -tx1 | tr -d ' \n')00000000000000000000000000000000"
  printf '%s-%s-%s-%s-%s' "${hex:0:8}" "${hex:8:4}" "${hex:12:4}" "${hex:16:4}" "${hex:20:12}"
}

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
case " $* " in *" -tt "*) tty=1 ;; *) tty=0 ;; esac
if [[ "$tty" == "1" && "$cmd" == sudo* ]]; then
  printf '[sudo] password for pi: \n'
fi

case "$cmd" in
  mktemp*jts-deploy-facts*)
    rm -rf "$FAKE_FACTS_DIR"
    mkdir -m 0700 -p "$FAKE_FACTS_DIR"
    printf '%s\n' "$FAKE_FACTS_DIR"
    ;;
  sudo*jts-deploy-facts*)
    # publish_root_facts: root stages what this run will read back.
    if [[ "${FAKE_PUBLISH_RC:-0}" != "0" ]]; then exit "$FAKE_PUBLISH_RC"; fi
    mkdir -p "$FAKE_FACTS_DIR"
    id="$(fake_peer_id "$@")"
    if [[ -n "$id" ]]; then printf '%s\n' "$id" > "$FAKE_FACTS_DIR/peer_id"; fi
    if [[ -f "${FAKE_MANIFEST:-}" ]]; then cp "$FAKE_MANIFEST" "$FAKE_FACTS_DIR/build.txt"; fi
    printf '%s\n' "${FAKE_INSTALL_PROFILE:-full}" > "$FAKE_FACTS_DIR/install_profile"
    : > "$FAKE_FACTS_DIR/journal"
    ;;
  *jts-deploy-facts*)
    # The reads and the cleanup are plain, unprivileged file access.
    eval "$cmd"
    ;;
  'printf "%s\n" "$HOME"')
    printf '%s\n' "${FAKE_HOME:-/home/pi}"
    ;;
  'date +%s')
    printf '%s\n' "${FAKE_PI_EPOCH:-1750000000}"
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
    id="$(fake_peer_id "$@")"
    if [[ -n "$id" ]]; then printf '%s\n' "$id"; fi
    ;;
  sudo*cat\ /var/lib/jasper/build.txt*)
    cat "${FAKE_MANIFEST:-}" 2>/dev/null || true
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
  *jts-install-prepare*)
    rm -f "$FAKE_INSTALL_LOG" "$FAKE_INSTALL_RC_FILE" "$FAKE_INSTALL_POLLS"
    : > "$FAKE_INSTALL_LOG"
    : > "$FAKE_INSTALL_RC_FILE"
    printf '%s\n' "$FAKE_INSTALL_LOG"
    ;;
  *jts-install-poll*)
    # The wrapper passes the count of transcript lines it has printed last.
    seen="${cmd##* }"
    polls=$(( $(cat "$FAKE_INSTALL_POLLS" 2>/dev/null || printf 0) + 1 ))
    printf '%s\n' "$polls" > "$FAKE_INSTALL_POLLS"
    # A severed transport during the poll, not during the install.
    [[ "$polls" == "${FAKE_POLL_DROP_AT:-}" ]] && exit 255
    tail -n +"$((seen + 1))" "$FAKE_INSTALL_LOG"
    if (( polls > ${FAKE_INSTALL_POLLS_UNTIL_DONE:-0} )); then
      printf 'JTS_INSTALL_RC=%s\n' "$(cat "$FAKE_INSTALL_RC_FILE")"
    fi
    ;;
  *\/deploy\/install.sh*)
    # Detached: the launch returns 0 immediately and the install's own
    # status lands in the marker file the poll arm serves.
    rc="${FAKE_INSTALL_RC:-0}"
    printf 'fake install.sh transcript\n' >> "$FAKE_INSTALL_LOG"
    printf '%s\n' "$rc" >> "$FAKE_INSTALL_RC_FILE"
    if [[ "$rc" == "0" && -n "${FAKE_MANIFEST:-}" ]]; then
      # install.sh writes the build manifest ONLY as its final step.
      sha="${cmd#*JASPER_DEPLOY_SHA_FULL=}"
      printf 'JASPER_GIT_SHA_FULL=%s\nJASPER_INSTALL_STATUS=ok\n' \
        "${sha%%[\\ ]*}" > "$FAKE_MANIFEST"
    fi
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


# The install poll waits INSTALL_POLL_INTERVAL_SEC between ticks; the fake
# remote decides when the install is done, so the wait is dead time here.
FAKE_SLEEP = r"""#!/usr/bin/env bash
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
        self._write_executable(self.bin / "sleep", FAKE_SLEEP)
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
                "FAKE_MANIFEST": str(self.tmp / "build.txt"),
                # Named like the real one: the fake's arms key off it.
                "FAKE_FACTS_DIR": str(self.tmp / ".jts-deploy-facts.fake"),
                "FAKE_INSTALL_LOG": str(self.tmp / ".jts-install.log"),
                "FAKE_INSTALL_RC_FILE": str(self.tmp / ".jts-install.rc"),
                "FAKE_INSTALL_POLLS": str(self.tmp / "install-polls"),
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

    def test_identity_mismatch_aborts_before_rsync_on_every_sudo_and_flag_path(self):
        """No path reaches `rsync --delete` with the identity guard unrun.

        .env.local records another speaker's peer_id, so every combination
        of sudo channel and SKIP_INSTALL must abort before rsync and leave
        the recorded identity alone.

        Removal condition: delete when the identity and direction guards
        move out of scripts/deploy-to-pi.sh.
        """
        env_local = textwrap.dedent(
            """\
            PI_HOST=jts3.local
            PI_USER=pi
            JASPER_HOSTNAME=jts3.local
            PI_PEER_ID=00000000-0000-0000-0000-00000000beef
            """
        )
        for use_pty in (False, True):
            for skip_install in ({}, {"SKIP_INSTALL": "1"}):
                with self.subTest(use_pty=use_pty, **skip_install):
                    fake = FakeRemote(self)
                    # Attended sudo is reachable only from a tty: sudo -n
                    # fails and stdin is the pty.
                    env = fake.env(
                        FAKE_SUDO_N_RC="1" if use_pty else "0", **skip_install
                    )
                    with isolated_checkout(env_local) as checkout:
                        cmd = ["bash", str(checkout / "scripts" / "deploy-to-pi.sh")]
                        if use_pty:
                            result = run_with_pty(cmd, cwd=checkout, env=env)
                        else:
                            result = subprocess.run(
                                cmd,
                                cwd=checkout,
                                env=env,
                                capture_output=True,
                                text=True,
                                timeout=10,
                            )
                        recorded = (checkout / ".env.local").read_text(
                            encoding="utf-8"
                        )

                    self.assertNotEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertNotIn("RSYNC", fake.calls())
                    self.assertIn(
                        "PI_PEER_ID=00000000-0000-0000-0000-00000000beef", recorded
                    )

    def test_a_non_uuid_read_is_reported_unavailable_and_never_recorded(self):
        """Attended sudo prompts inside every new ssh session, so a captured
        read can pick up prompt text where a peer_id should be. Only a
        UUID is an identity: anything else is `unavailable` — nothing is
        recorded, and the deploy is not blocked by it.

        Removal condition: delete with the peer_id TOFU guard.
        """
        env_local = textwrap.dedent(
            """\
            PI_HOST=jts3.local
            PI_USER=pi
            JASPER_HOSTNAME=jts3.local
            """
        )
        for peer_id in ("", "[sudo] password for pi:"):
            with self.subTest(peer_id=peer_id):
                fake = FakeRemote(self)
                env = fake.env(FAKE_SUDO_N_RC="1", FAKE_PEER_ID=peer_id)
                with isolated_checkout(env_local) as checkout:
                    result = run_with_pty(
                        ["bash", str(checkout / "scripts" / "deploy-to-pi.sh")],
                        cwd=checkout,
                        env=env,
                    )
                    recorded = (checkout / ".env.local").read_text(
                        encoding="utf-8"
                    )

                combined = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, combined)
                self.assertIn("DEPLOY_IDENTITY=unavailable", result.stdout)
                self.assertNotIn("PI_PEER_ID", recorded)
                self.assertIn("RSYNC", fake.calls())

    def test_a_fact_that_cannot_be_staged_stops_the_deploy_before_rsync(self):
        """A root fact the Pi has but will not hand over must abort the
        deploy, not read back as absent — an unstaged peer_id that reported
        `unavailable` would skip the identity guard silently.

        Removal condition: delete when the guards stop reading staged facts.
        """
        fake = FakeRemote(self)
        env = fake.env(
            PI_HOST="jts3.local",
            PI_USER="pi",
            JASPER_HOSTNAME="jts3.local",
            FAKE_SUDO_N_RC="1",
            FAKE_PUBLISH_RC="1",
        )
        with isolated_checkout(None) as checkout:
            result = run_with_pty(
                ["bash", str(checkout / "scripts" / "deploy-to-pi.sh")],
                cwd=checkout,
                env=env,
            )

        self.assertNotEqual(
            result.returncode, 0, result.stdout + result.stderr
        )
        self.assertNotIn("RSYNC", fake.calls())

    def test_skip_restart_skips_the_restarts_and_still_runs_the_gates(self):
        """SKIP_RESTART=1 leaves the daemons on prior code — it is not a
        verification switch, so the post-deploy probes still run.

        Removal condition: delete with the SKIP_RESTART flag.
        """
        fake = FakeRemote(self)
        result = self.run_deploy(
            fake,
            env_local=None,
            PI_HOST="jts3.local",
            PI_USER="pi",
            JASPER_HOSTNAME="jts3.local",
            SKIP_RESTART="1",
        )

        calls = fake.calls()
        sha_full = git_head("HEAD")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("systemctl\\ restart", calls)
        self.assertIn("/system/data.json", calls)
        # The asset probe carries the cache key of the build just deployed;
        # the character class absorbs the log's shell quoting of `?`.
        self.assertRegex(
            calls, rf"/assets/app\.css[\\?]+v={sha_full[:7]}[0-9a-f]*"
        )

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

    def test_install_is_detached_and_the_deploy_exits_on_its_polled_status(
        self,
    ):
        """install.sh is launched into its own session and its own exit
        status is read back from the Pi, so a severed transport can no
        longer decide the deploy's outcome (#4190). The fake's launch
        call always returns 0, so any non-zero exit here came from the
        polled marker file; a poll that dies with ssh's 255 is one
        reconnect, never the deploy's status.

        Removal condition: delete with the detached install launch.
        """
        for install_rc, drop_at, expect_done in (
            ("0", "", True),
            ("3", "", False),
            ("0", "2", True),
        ):
            with self.subTest(install_rc=install_rc, drop_at=drop_at):
                fake = FakeRemote(self)
                result = self.run_deploy(
                    fake,
                    env_local=None,
                    PI_HOST="jts3.local",
                    PI_USER="pi",
                    JASPER_HOSTNAME="jts3.local",
                    FAKE_INSTALL_RC=install_rc,
                    FAKE_INSTALL_POLLS_UNTIL_DONE="1",
                    FAKE_POLL_DROP_AT=drop_at,
                )

                combined = result.stdout + result.stderr
                # The call log %q-quotes each argument; the shape, not the
                # quoting, is what this pins.
                calls = fake.calls().replace("\\", "").splitlines()
                launch = next(c for c in calls if "/deploy/install.sh" in c)
                polls = [c for c in calls if "jts-install-poll" in c]

                self.assertEqual(
                    result.returncode, int(install_rc), combined
                )
                self.assertIn("setsid --fork nohup sh -c", launch)
                self.assertIn("</dev/null", launch)
                self.assertIn(".jts-install.log 2>&1", launch)
                self.assertIn("echo $? >>", launch)
                # The status can only have come from a poll: the fake's
                # launch arm exits 0 and withholds the rc for one tick.
                self.assertGreaterEqual(len(polls), 2)
                self.assertLess(calls.index(launch), calls.index(polls[0]))
                # The install transcript reaches the operator live.
                self.assertIn("fake install.sh transcript", result.stdout)
                # The OOM window opens before the install it bounds, and a
                # failed install still gets its collateral scanned.
                clock = next(c for c in calls if c.endswith("date +%s"))
                self.assertLess(calls.index(clock), calls.index(launch))
                self.assertTrue(
                    any("journalctl -k" in c for c in calls), combined
                )
                self.assertEqual("==> Done." in combined, expect_done)

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
        self.assertIn("sudo\\ -n\\ setsid", calls)
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
        # The attended channel still runs the identity guard, and reports
        # its outcome on the branch that has nothing to compare against.
        self.assertIn("DEPLOY_IDENTITY=no_state_file", result.stdout)
        self.assertIn("SSH -tt", calls)
        self.assertIn("sudo\\ -v", calls)
        self.assertIn("sudo\\ setsid", calls)
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
            self.assertIn(
                f"PI_PEER_ID={fake_peer_id('jts.local')}", recorded.decode()
            )
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
            self.assertIn(
                f"PI_PEER_ID={fake_peer_id('jts.local')}", state.read_text()
            )

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
