# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
FAILURE_RECONCILE = REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile"
OUTPUTD_UNIT = REPO / "deploy" / "systemd" / "jasper-outputd.service"
ALSA_BACKEND = REPO / "rust" / "jasper-outputd" / "src" / "alsa_backend.rs"

# A real journal tail from the failure this helper classifies: anyhow prints the
# context chain, and the top line is the context outputd attached to the open.
CONTENT_LANE_JOURNAL = (
    "event=outputd.alsa.opening content_pcm=outputd_content_capture\n"
    "Error: configuring outputd content capture PCM outputd_content_capture\n"
    "\n"
    "Caused by:\n"
    "    0: set_format(S32LE)\n"
    "    1: Invalid argument (os error 22)\n"
)
# An exit-1 failure that is NOT a content-lane open.
OTHER_FAILURE_JOURNAL = (
    "event=outputd.alsa.opened content_pcm=outputd_content_capture\n"
    "Error: writing DAC period\n"
)


def _write_executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


class _FailureReconcileHarness:
    """Run jasper-outputd-failure-reconcile against fake collaborators."""

    def __init__(self, tmp_path: Path) -> None:
        self.reconcile_log = tmp_path / "reconcile.log"
        self.systemctl_log = tmp_path / "systemctl.log"
        self.journal = tmp_path / "journal.txt"
        self.state = tmp_path / "content-lane.state"
        self.reconcile_rc = tmp_path / "reconcile.rc"
        self.reconcile_rc.write_text("0", encoding="utf-8")
        fake_reconcile = _write_executable(
            tmp_path / "jasper-audio-hardware-reconcile",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$JASPER_TEST_LOG\"\n"
            'exit "$(cat "$JASPER_TEST_RECONCILE_RC")"\n',
        )
        fake_systemctl = _write_executable(
            tmp_path / "systemctl",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n",
        )
        fake_journalctl = _write_executable(
            tmp_path / "journalctl",
            "#!/usr/bin/env bash\n"
            'cat "$JASPER_TEST_JOURNAL" 2>/dev/null || true\n',
        )
        self.env = os.environ.copy()
        self.env.update({
            "JASPER_AUDIO_HARDWARE_RECONCILE": str(fake_reconcile),
            "JASPER_SYSTEMCTL": str(fake_systemctl),
            "JASPER_JOURNALCTL": str(fake_journalctl),
            "JASPER_TEST_LOG": str(self.reconcile_log),
            "JASPER_TEST_RECONCILE_RC": str(self.reconcile_rc),
            "JASPER_SYSTEMCTL_LOG": str(self.systemctl_log),
            "JASPER_TEST_JOURNAL": str(self.journal),
            "JASPER_OUTPUTD_CONTENT_LANE_STATE": str(self.state),
            "JASPER_OUTPUTD_CONFIG_RETRY_STATE": str(tmp_path / "config-retry.stamp"),
        })
        # systemd exports this to ExecStopPost; the fake journalctl ignores it,
        # but running with it set exercises the invocation-scoped read path.
        self.env["INVOCATION_ID"] = "0" * 32

    def run(
        self,
        *,
        journal: str = "",
        result: str = "exit-code",
        exit_status: str = "1",
        **env: str,
    ) -> subprocess.CompletedProcess[str]:
        self.journal.write_text(journal, encoding="utf-8")
        run_env = dict(self.env)
        run_env.update({"SERVICE_RESULT": result, "EXIT_STATUS": exit_status})
        run_env.update(env)
        return subprocess.run(
            [str(FAILURE_RECONCILE)],
            env=run_env,
            text=True,
            capture_output=True,
            check=True,
        )

    def systemctl_calls(self) -> list[str]:
        if not self.systemctl_log.exists():
            return []
        return self.systemctl_log.read_text(encoding="utf-8").splitlines()

    def state_record(self) -> dict[str, str]:
        record: dict[str, str] = {}
        for line in self.state.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            record[key] = value
        return record


def test_output_hardware_hotplug_requests_reconcile_without_blocking(
    tmp_path: Path,
) -> None:
    log = tmp_path / "systemctl.log"
    fake_systemctl = _write_executable(
        tmp_path / "systemctl",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_TEST_LOG\"\n",
    )

    env = os.environ.copy()
    env.update({
        "JASPER_SYSTEMCTL": str(fake_systemctl),
        "JASPER_TEST_LOG": str(log),
        "ACTION": "remove",
        "SUBSYSTEM": "usb",
        "PRODUCT": "5ac/110a/100",
    })

    result = subprocess.run(
        [str(REPO / "deploy" / "bin" / "jasper-output-hardware-hotplug")],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert log.read_text(encoding="utf-8").strip() == (
        "--no-block start jasper-audio-hardware-reconcile.service"
    )
    assert "event=audio_hardware_hotplug.reconcile_requested" in result.stderr


def test_outputd_failure_reconcile_refreshes_env_for_retry(tmp_path: Path) -> None:
    log = tmp_path / "reconcile.log"
    fake_reconcile = _write_executable(
        tmp_path / "jasper-audio-hardware-reconcile",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_TEST_LOG\"\n",
    )

    env = os.environ.copy()
    env.update({
        "JASPER_AUDIO_HARDWARE_RECONCILE": str(fake_reconcile),
        "JASPER_TEST_LOG": str(log),
        "JASPER_JOURNALCTL": "/bin/false",
        "JASPER_OUTPUTD_CONTENT_LANE_STATE": str(tmp_path / "content-lane.state"),
        "SERVICE_RESULT": "exit-code",
        "EXIT_STATUS": "1",
    })

    result = subprocess.run(
        [str(REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile")],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert log.read_text(encoding="utf-8").strip() == (
        "--reason outputd-failure --no-restart"
    )
    assert "event=outputd.failure_reconcile.ok" in result.stderr


def test_outputd_failure_reconcile_retries_config_exit_once(tmp_path: Path) -> None:
    log = tmp_path / "reconcile.log"
    systemctl_log = tmp_path / "systemctl.log"
    fake_reconcile = _write_executable(
        tmp_path / "jasper-audio-hardware-reconcile",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_TEST_LOG\"\n",
    )
    fake_systemctl = _write_executable(
        tmp_path / "systemctl",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n",
    )

    env = os.environ.copy()
    env.update({
        "JASPER_AUDIO_HARDWARE_RECONCILE": str(fake_reconcile),
        "JASPER_SYSTEMCTL": str(fake_systemctl),
        "JASPER_TEST_LOG": str(log),
        "JASPER_SYSTEMCTL_LOG": str(systemctl_log),
        "JASPER_OUTPUTD_CONFIG_RETRY_STATE": str(tmp_path / "config-retry.stamp"),
        "JASPER_OUTPUTD_CONTENT_LANE_STATE": str(tmp_path / "content-lane.state"),
        "SERVICE_RESULT": "exit-code",
        "EXIT_STATUS": "78",
    })

    result = subprocess.run(
        [str(REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile")],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert log.read_text(encoding="utf-8").strip() == (
        "--reason outputd-config-failure --no-restart"
    )
    assert systemctl_log.read_text(encoding="utf-8").splitlines() == [
        "reset-failed jasper-outputd.service",
        "--no-block restart jasper-outputd.service",
    ]
    assert "event=outputd.failure_reconcile.retry" in result.stderr
    assert "reason=config_reconciled" in result.stderr

    log.write_text("", encoding="utf-8")
    systemctl_log.write_text("", encoding="utf-8")
    second = subprocess.run(
        [str(REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile")],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert log.read_text(encoding="utf-8") == ""
    assert systemctl_log.read_text(encoding="utf-8") == ""
    assert "event=outputd.failure_reconcile.skip" in second.stderr
    assert "reason=config_retry_already_attempted" in second.stderr


def test_outputd_failure_reconcile_skips_normal_stops(tmp_path: Path) -> None:
    log = tmp_path / "reconcile.log"
    fake_reconcile = _write_executable(
        tmp_path / "jasper-audio-hardware-reconcile",
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$JASPER_TEST_LOG\"\n",
    )

    env = os.environ.copy()
    env.update({
        "JASPER_AUDIO_HARDWARE_RECONCILE": str(fake_reconcile),
        "JASPER_TEST_LOG": str(log),
        "SERVICE_RESULT": "success",
        "EXIT_STATUS": "0",
    })

    result = subprocess.run(
        [str(REPO / "deploy" / "bin" / "jasper-outputd-failure-reconcile")],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )

    assert not log.exists()
    assert "event=outputd.failure_reconcile.skip" in result.stderr
    assert "reason=non_retrying_stop" in result.stderr


def _park_after() -> int:
    """The helper's own consecutive-failure bound."""
    match = re.search(
        r'^CONTENT_LANE_PARK_AFTER="\$\{JASPER_OUTPUTD_CONTENT_LANE_PARK_AFTER:-(\d+)\}"$',
        FAILURE_RECONCILE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match is not None, "CONTENT_LANE_PARK_AFTER default is no longer parseable"
    return int(match.group(1))


def _content_lane_signature() -> re.Pattern[str]:
    """The helper's own content-lane journal signature, as an ERE."""
    match = re.search(
        r"^CONTENT_LANE_SIGNATURE='([^']+)'$",
        FAILURE_RECONCILE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match is not None, "CONTENT_LANE_SIGNATURE is no longer parseable"
    return re.compile(match.group(1))


def test_outputd_failure_reconcile_keeps_content_lane_transient_restarting(
    tmp_path: Path,
) -> None:
    """Category A: the routine wait for CamillaDSP stays on the restart ladder."""
    harness = _FailureReconcileHarness(tmp_path)

    for attempt in range(1, _park_after()):
        result = harness.run(journal=CONTENT_LANE_JOURNAL)
        assert f"content_lane_failures={attempt}" in result.stderr
        assert "event=outputd.failure_reconcile.park" not in result.stderr

    # Nothing was asked of systemd: systemd's own Restart=on-failure is what
    # carries a transient, and the reconcile still refreshed the env each time.
    assert harness.systemctl_calls() == []
    assert harness.reconcile_log.read_text(encoding="utf-8").splitlines() == [
        "--reason outputd-failure --no-restart"
    ] * (_park_after() - 1)
    assert harness.state_record()["count"] == str(_park_after() - 1)


def test_outputd_failure_reconcile_parks_repeated_content_lane_failures(
    tmp_path: Path,
) -> None:
    """Category B: a permanent mismatch parks instead of reaching the reboot."""
    harness = _FailureReconcileHarness(tmp_path)
    park_after = _park_after()

    for _ in range(park_after - 1):
        harness.run(journal=CONTENT_LANE_JOURNAL)
    assert harness.systemctl_calls() == []

    result = harness.run(journal=CONTENT_LANE_JOURNAL)

    assert "event=outputd.failure_reconcile.park" in result.stderr
    assert "reason=content_lane_open_repeated" in result.stderr
    assert f"failures={park_after}" in result.stderr
    # Stop, never restart: a restart here would spend the burst the park exists
    # to leave unspent.
    assert harness.systemctl_calls() == [
        "reset-failed jasper-outputd.service",
        "--no-block stop jasper-outputd.service",
    ]

    record = harness.state_record()
    assert record["count"] == str(park_after)
    assert record["reason"] == "content_lane_open_repeated"
    assert "consecutive starts" in record["detail"]
    assert "systemctl restart jasper-outputd" in record["action"]
    assert record["parked_utc"].endswith("Z")


def test_outputd_failure_reconcile_park_bound_leaves_a_start_unspent() -> None:
    """The bound has to beat the unit's StartLimitAction=reboot ladder."""
    unit = OUTPUTD_UNIT.read_text(encoding="utf-8")
    burst = re.search(r"^StartLimitBurst=(\d+)$", unit, re.MULTILINE)
    assert burst is not None
    assert re.search(r"^StartLimitAction=reboot$", unit, re.MULTILINE) is not None
    # Parking on failure N spends N starts; the reboot fires on start burst+1.
    # Staying strictly below the burst leaves at least one start in hand for a
    # stop job that lands late.
    assert _park_after() < int(burst.group(1))


def test_outputd_failure_reconcile_clears_streak_on_other_exit_one(
    tmp_path: Path,
) -> None:
    """A different exit-1 breaks the streak: only consecutive opens park."""
    harness = _FailureReconcileHarness(tmp_path)
    park_after = _park_after()

    for _ in range(park_after - 1):
        harness.run(journal=CONTENT_LANE_JOURNAL)

    interrupting = harness.run(journal=OTHER_FAILURE_JOURNAL)
    assert "content_lane_failures=0" in interrupting.stderr
    assert not harness.state.exists()

    result = harness.run(journal=CONTENT_LANE_JOURNAL)
    assert "content_lane_failures=1" in result.stderr
    assert "event=outputd.failure_reconcile.park" not in result.stderr
    assert harness.systemctl_calls() == []


def test_outputd_failure_reconcile_clears_streak_on_clean_stop(
    tmp_path: Path,
) -> None:
    """Recovery: outputd running again is what retires the park record."""
    harness = _FailureReconcileHarness(tmp_path)

    for _ in range(_park_after()):
        harness.run(journal=CONTENT_LANE_JOURNAL)
    assert harness.state.exists()

    harness.run(result="success", exit_status="0")
    assert not harness.state.exists()

    # The next content-lane failure starts a fresh streak rather than parking
    # immediately off the retired record.
    harness.systemctl_log.unlink(missing_ok=True)
    result = harness.run(journal=CONTENT_LANE_JOURNAL)
    assert "content_lane_failures=1" in result.stderr
    assert harness.systemctl_calls() == []


def test_outputd_failure_reconcile_ignores_stale_content_lane_record(
    tmp_path: Path,
) -> None:
    """Failures spread beyond the window are separate events, not a streak."""
    harness = _FailureReconcileHarness(tmp_path)
    harness.state.write_text(
        f"count={_park_after() - 1}\nlast_failure_epoch=1\n", encoding="utf-8"
    )

    result = harness.run(journal=CONTENT_LANE_JOURNAL)

    assert "content_lane_failures=1" in result.stderr
    assert harness.systemctl_calls() == []


def test_outputd_failure_reconcile_never_parks_a_config_exit(tmp_path: Path) -> None:
    """EX_CONFIG keeps its own bounded retry and clears the content streak."""
    harness = _FailureReconcileHarness(tmp_path)
    for _ in range(_park_after() - 1):
        harness.run(journal=CONTENT_LANE_JOURNAL)
    harness.systemctl_log.unlink(missing_ok=True)

    result = harness.run(journal=CONTENT_LANE_JOURNAL, exit_status="78")

    assert not harness.state.exists()
    assert "event=outputd.failure_reconcile.retry" in result.stderr
    assert harness.systemctl_calls() == [
        "reset-failed jasper-outputd.service",
        "--no-block restart jasper-outputd.service",
    ]


def test_outputd_failure_reconcile_needs_journal_evidence_to_park(
    tmp_path: Path,
) -> None:
    """An unreadable journal reads as transient — never as a park."""
    harness = _FailureReconcileHarness(tmp_path)

    for _ in range(_park_after() + 1):
        result = harness.run(journal="")

    assert "content_lane_failures=0" in result.stderr
    assert not harness.state.exists()
    assert harness.systemctl_calls() == []


def test_content_lane_signature_covers_outputd_open_contexts() -> None:
    """The bash signature tracks the context strings outputd actually attaches."""
    backend = ALSA_BACKEND.read_text(encoding="utf-8")
    contexts = set(
        re.findall(r'"((?:opening|configuring|starting)[^"]*PCM) \{\}"', backend)
    )
    expected = {
        "opening outputd content capture PCM",
        "configuring outputd content capture PCM",
        "opening outputd active content capture PCM",
        "configuring outputd active content capture PCM",
        "starting capture PCM",
    }
    # Direction 1: a reworded Rust context fails here, next to the pattern that
    # has to learn the new wording.
    assert expected <= contexts, (
        "content-lane open contexts moved in rust/jasper-outputd/src/alsa_backend.rs; "
        "update CONTENT_LANE_SIGNATURE in deploy/bin/jasper-outputd-failure-reconcile "
        f"to match. missing={sorted(expected - contexts)}"
    )

    signature = _content_lane_signature()
    # Direction 2: every content-lane context the file carries — including one
    # added later — must be matched by the helper's signature.
    covered = {c for c in contexts if "content capture" in c} | expected
    for context in sorted(covered):
        assert signature.search(f"Error: {context} outputd_content_capture"), context

    # Non-vacuity: the signature is narrower than "any outputd open failure".
    assert "opening outputd DAC PCM" in contexts
    assert not signature.search("Error: opening outputd DAC PCM outputd_dac")
