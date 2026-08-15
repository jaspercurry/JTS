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
# The ACTIVE half of the same class: a roleful box whose aloop pair-5 PCMs no
# longer exist, so the open fails on the NAME rather than on a width.
#
# TWO SINKS OPEN THIS LANE AND THEY SPELL THE CONTEXT DIFFERENTLY. The
# composite (dual-Apple) sink says "outputd ACTIVE content capture PCM"; the
# single-ALSA sink — the roleful shape the fleet runs armed, DAC8x/jts3 —
# opens the same ACTIVE lane through the lane-agnostic "outputd content
# capture PCM". Both carry the PCM NAME, which is what the selector reads.
# Keying on the composite's prefix gave a rolled-back DAC8x the falsified
# width remedy; both fixtures exist so that regression cannot return.
ACTIVE_CONTENT_LANE_JOURNAL = (
    "event=outputd.alsa.opening content_pcm=outputd_active_content_capture\n"
    "Error: opening outputd active content capture PCM "
    "outputd_active_content_capture\n"
    "\n"
    "Caused by:\n"
    "    0: ALSA function 'snd_pcm_open' failed with error 'ENOENT: "
    "No such file or directory'\n"
)
SINGLE_ALSA_ACTIVE_CONTENT_LANE_JOURNAL = (
    "event=outputd.alsa.opening content_pcm=outputd_active_content_capture\n"
    "Error: opening outputd content capture PCM "
    "outputd_active_content_capture\n"
    "\n"
    "Caused by:\n"
    "    0: ALSA function 'snd_pcm_open' failed with error 'ENOENT: "
    "No such file or directory'\n"
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
        self.state_snapshot = tmp_path / "state-snapshot"
        fake_reconcile = _write_executable(
            tmp_path / "jasper-audio-hardware-reconcile",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$JASPER_TEST_LOG\"\n"
            # The reconcile runs BETWEEN the streak write and the park write,
            # so copying the record here is how a test sees the mid-run window.
            'cp "$JASPER_OUTPUTD_CONTENT_LANE_STATE" "$JASPER_TEST_STATE_SNAPSHOT" '
            "2>/dev/null || rm -f \"$JASPER_TEST_STATE_SNAPSHOT\"\n"
            'exit "$(cat "$JASPER_TEST_RECONCILE_RC")"\n',
        )
        fake_systemctl = _write_executable(
            tmp_path / "systemctl",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$JASPER_SYSTEMCTL_LOG\"\n",
        )
        self.journalctl_log = tmp_path / "journalctl.log"
        fake_journalctl = _write_executable(
            tmp_path / "journalctl",
            "#!/usr/bin/env bash\n"
            "printf '%s\\n' \"$*\" >> \"$JASPER_TEST_JOURNALCTL_LOG\"\n"
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
            "JASPER_TEST_JOURNALCTL_LOG": str(self.journalctl_log),
            "JASPER_TEST_STATE_SNAPSHOT": str(self.state_snapshot),
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
        invocation_id: str | None = "0" * 32,
        **env: str,
    ) -> subprocess.CompletedProcess[str]:
        self.journal.write_text(journal, encoding="utf-8")
        run_env = dict(self.env)
        run_env.update({"SERVICE_RESULT": result, "EXIT_STATUS": exit_status})
        if invocation_id is None:
            run_env.pop("INVOCATION_ID", None)
        else:
            run_env["INVOCATION_ID"] = invocation_id
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

    def journalctl_calls(self) -> list[str]:
        if not self.journalctl_log.exists():
            return []
        return self.journalctl_log.read_text(encoding="utf-8").splitlines()

    def state_record(self, path: Path | None = None) -> dict[str, str]:
        record: dict[str, str] = {}
        for line in (path or self.state).read_text(encoding="utf-8").splitlines():
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


def _content_lane_active_signature() -> re.Pattern[str]:
    """The helper's ACTIVE-lane discriminator, as an ERE."""
    match = re.search(
        r"^CONTENT_LANE_ACTIVE_SIGNATURE='([^']+)'$",
        FAILURE_RECONCILE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match is not None, "CONTENT_LANE_ACTIVE_SIGNATURE is no longer parseable"
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


def _park_with(harness: "_FailureReconcileHarness", journal: str):
    """Drive a harness to its park and return the final run."""
    for _ in range(_park_after() - 1):
        harness.run(journal=journal)
    return harness.run(journal=journal)


def test_active_lane_park_names_the_ring_not_a_width(tmp_path: Path) -> None:
    """The ACTIVE lane has no pair to width-match: its remedy is the ring.

    P9-C deleted the pair-5 PCM definitions, so "match the content-lane width
    on both halves of the pair" names a pair that does not exist. An operator
    who follows it changes a format nothing reads and the speaker stays parked.
    """
    harness = _FailureReconcileHarness(tmp_path)
    result = _park_with(harness, ACTIVE_CONTENT_LANE_JOURNAL)

    assert "event=outputd.failure_reconcile.park" in result.stderr
    assert "lane=active" in result.stderr

    action = harness.state_record()["action"]
    assert "baseline-reemit --endpoint ring" in action
    assert "jasper-fanin-coupling-reconcile shm_ring" in action
    # The falsified remedy must be absent, not merely out-ranked.
    assert "match the content-lane width" not in action
    assert "JASPER_OUTPUTD_CONTENT_FORMAT" not in action


def test_passive_lane_park_keeps_the_width_remedy(tmp_path: Path) -> None:
    """Pair 6 still has both halves, so its width-shear remedy is unchanged."""
    harness = _FailureReconcileHarness(tmp_path)
    result = _park_with(harness, CONTENT_LANE_JOURNAL)

    assert "lane=passive" in result.stderr
    action = harness.state_record()["action"]
    assert "match the content-lane width" in action
    assert "systemctl restart jasper-outputd" in action
    assert "--endpoint ring" not in action


def test_single_alsa_active_lane_park_names_the_ring_not_a_width(
    tmp_path: Path,
) -> None:
    """A roleful SINGLE-DAC box gets the ring remedy too — the B1 regression.

    `AlsaBackend` carries the ACTIVE lane on a roleful single DAC (DAC8x/jts3,
    the shape the fleet runs armed) and opens it through the LANE-AGNOSTIC
    context "opening outputd content capture PCM <name>". A selector keyed on
    the composite sink's "outputd ACTIVE content capture PCM" prefix therefore
    missed it entirely and handed that box the width remedy for the pair this
    PR deleted. The selector reads the trailing PCM NAME instead, which both
    sinks carry.
    """
    harness = _FailureReconcileHarness(tmp_path)
    result = _park_with(harness, SINGLE_ALSA_ACTIVE_CONTENT_LANE_JOURNAL)

    assert "lane=active" in result.stderr
    action = harness.state_record()["action"]
    assert "baseline-reemit --endpoint ring" in action
    assert "match the content-lane width" not in action


def test_a_lane_agnostic_context_follows_the_pcm_name(tmp_path: Path) -> None:
    """`starting capture PCM` names no lane in its prefix — but it names the PCM.

    Both sinks emit this one context, so it is the case that proves the
    selector reads the name: the same prefix routes to opposite remedies
    depending only on which lane's PCM failed.
    """
    passive_dir = tmp_path / "passive"
    passive_dir.mkdir()
    active_dir = tmp_path / "active"
    active_dir.mkdir()

    passive = _FailureReconcileHarness(passive_dir)
    passive_result = _park_with(
        passive,
        "Error: starting capture PCM outputd_content_capture\n",
    )
    assert "lane=passive" in passive_result.stderr
    assert "match the content-lane width" in passive.state_record()["action"]

    active = _FailureReconcileHarness(active_dir)
    active_result = _park_with(
        active,
        "Error: starting capture PCM outputd_active_content_capture\n",
    )
    assert "lane=active" in active_result.stderr
    assert "baseline-reemit --endpoint ring" in active.state_record()["action"]


def test_park_detail_claims_no_transport_that_may_not_exist(
    tmp_path: Path,
) -> None:
    """The record's detail line is lane-agnostic prose.

    It used to say "could not open its snd-aloop content lane", which is false
    for the ACTIVE lane on this build — that lane has no snd-aloop transport.
    """
    harness = _FailureReconcileHarness(tmp_path)
    _park_with(harness, ACTIVE_CONTENT_LANE_JOURNAL)

    detail = harness.state_record()["detail"]
    assert "snd-aloop" not in detail
    assert "content lane" in detail
    assert "consecutive starts" in detail


def test_content_lane_active_signature_covers_both_sinks() -> None:
    """The ACTIVE discriminator must fire on EVERY context, from EITHER sink.

    Derived from outputd's own context literals rather than a hand-list, and
    crossed with both lane names — because the previous version of this guard
    checked only the composite sink's two spellings and so RATIFIED the gap
    that let a roleful single-DAC rollback take the passive remedy.
    """
    contexts = _open_context_literals(ALSA_BACKEND.read_text(encoding="utf-8"))
    active = _content_lane_active_signature()
    signature = _content_lane_signature()

    # Both sinks' spellings, all three stages. `X` is the extractor's
    # placeholder for the interpolated PCM name.
    expected = {
        "opening outputd content capture PCM X",
        "configuring outputd content capture PCM X",
        "opening outputd active content capture PCM X",
        "configuring outputd active content capture PCM X",
        "starting capture PCM X",
    }
    assert expected <= contexts, (
        "content-lane open contexts moved in "
        "rust/jasper-outputd/src/alsa_backend.rs; update "
        "CONTENT_LANE_SIGNATURE / CONTENT_LANE_ACTIVE_SIGNATURE in "
        "deploy/bin/jasper-outputd-failure-reconcile to match. "
        f"missing={sorted(expected - contexts)}"
    )

    for context in sorted(expected):
        active_line = f"Error: {context.replace('X', 'outputd_active_content_capture')}"
        passive_line = f"Error: {context.replace('X', 'outputd_content_capture')}"
        # Every context, either sink, carrying the ACTIVE name -> active remedy.
        assert active.search(active_line), f"active discriminator missed: {active_line}"
        # The same context carrying the PASSIVE name must NOT claim active, or
        # every passive width shear is sent to re-arm a ring it does not use.
        assert not active.search(passive_line), (
            f"active discriminator over-claimed: {passive_line}"
        )
        # And the park gate itself must fire on both, or neither remedy is
        # reached at all.
        assert signature.search(active_line), active_line
        assert signature.search(passive_line), passive_line

    # Non-vacuity: the discriminator is narrower than "any outputd open".
    assert not active.search("Error: opening outputd DAC PCM outputd_dac")


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


def test_outputd_failure_reconcile_scopes_the_journal_to_the_invocation(
    tmp_path: Path,
) -> None:
    """With INVOCATION_ID set, the read is scoped to the run that just died."""
    harness = _FailureReconcileHarness(tmp_path)

    harness.run(journal=CONTENT_LANE_JOURNAL, invocation_id="a" * 32)

    assert harness.journalctl_calls() == [
        "--no-pager --output=cat --lines=40 "
        "_SYSTEMD_INVOCATION_ID=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    ]


def test_outputd_failure_reconcile_journal_fallback_without_invocation_id(
    tmp_path: Path,
) -> None:
    """Without it the read falls back to a unit + time bounded tail, and works."""
    harness = _FailureReconcileHarness(tmp_path)
    park_after = _park_after()

    for _ in range(park_after):
        result = harness.run(journal=CONTENT_LANE_JOURNAL, invocation_id=None)

    assert harness.journalctl_calls() == [
        "--no-pager --output=cat --lines=40 "
        "--unit=jasper-outputd.service --since=2 minutes ago"
    ] * park_after
    # The fallback is not decorative: classification still reaches the park.
    assert "event=outputd.failure_reconcile.park" in result.stderr


def test_outputd_failure_reconcile_skips_an_exec_condition_park(
    tmp_path: Path,
) -> None:
    """`exec-condition` is systemd's spelling; `condition` matched nothing."""
    harness = _FailureReconcileHarness(tmp_path)

    result = harness.run(result="exec-condition", exit_status="1")

    assert not harness.reconcile_log.exists()
    assert "event=outputd.failure_reconcile.skip" in result.stderr
    assert "reason=non_retrying_stop" in result.stderr


def test_outputd_failure_reconcile_park_line_carries_the_remediation(
    tmp_path: Path,
) -> None:
    """The journal line stands alone: it names the fix, not just the state file."""
    harness = _FailureReconcileHarness(tmp_path)

    for _ in range(_park_after()):
        result = harness.run(journal=CONTENT_LANE_JOURNAL)

    park_line = next(
        line
        for line in result.stderr.splitlines()
        if "event=outputd.failure_reconcile.park" in line
    )
    assert "JASPER_OUTPUTD_CONTENT_FORMAT" in park_line
    assert "systemctl restart jasper-outputd" in park_line
    # One remediation sentence, one owner: the record and the line agree.
    assert harness.state_record()["action"] in park_line


def test_outputd_failure_reconcile_park_record_never_loses_its_reason(
    tmp_path: Path,
) -> None:
    """A later failure of the same streak re-parks without a count-only window."""
    harness = _FailureReconcileHarness(tmp_path)
    park_after = _park_after()

    for _ in range(park_after):
        harness.run(journal=CONTENT_LANE_JOURNAL)

    # Something restarted outputd (deploy, udev, operator) into the same broken
    # width: the record must never regress to count-only.
    harness.run(journal=CONTENT_LANE_JOURNAL)

    record = harness.state_record()
    assert record["count"] == str(park_after + 1)
    assert record["reason"] == "content_lane_open_repeated"
    assert "systemctl restart jasper-outputd" in record["action"]
    assert "udev" in record["re_arm"]

    # And not only at the end: the snapshot the reconciler took mid-run — after
    # the streak write, before the park write — still carries the remediation.
    # A count-only rewrite at or above the bound would blank it for as long as
    # the reconcile runs.
    mid_run = harness.state_record(harness.state_snapshot)
    assert mid_run["reason"] == "content_lane_open_repeated"
    assert "systemctl restart jasper-outputd" in mid_run["action"]


# A PCM-open context literal in either of the file's two `format!` idioms: the
# trailing-positional `"... PCM {}"` the content lane uses today, and the inline
# capture `"... PCM {pcm_name}"` / `"opening outputd {role} ... PCM"` used
# elsewhere in the same file. Keying only on the trailing-positional form made
# direction 2 below unfalsifiable — an opener in the inline idiom was invisible
# to the guard and could escape the signature with every test green.
_OPEN_CONTEXT_RE = re.compile(
    r'"((?:opening|configuring|starting)[^"\\]*?PCM[^"\\]*?)"'
)
_PLACEHOLDER_RE = re.compile(r"\{[a-z_]*\}")


def _open_context_literals(rust_source: str) -> set[str]:
    """PCM-open context literals, placeholders normalized to a single token.

    Scans the whole file, comments and Rust tests included. That over-collects
    on purpose: a drift guard that fires on a hypothetical wording written in a
    comment costs one look, while one that misses a real opener costs a reboot.
    """
    return {
        _PLACEHOLDER_RE.sub("X", literal).strip()
        for literal in _OPEN_CONTEXT_RE.findall(rust_source)
    }


def test_open_context_extraction_sees_the_inline_capture_idiom() -> None:
    """The extractor is not blind to the idiom the guard below must police."""
    inline = (
        'let x = PCM::new(a, b, true).with_context(|| {\n'
        '    format!("opening outputd {role} content lane PCM {pcm_name}")\n'
        "});\n"
    )
    literals = _open_context_literals(inline)
    assert literals == {"opening outputd X content lane PCM X"}
    # And it is a real escape: the shipped signature does not match it, so the
    # guard below fails the moment such an opener lands.
    assert not _content_lane_signature().search(
        "Error: opening outputd content content lane PCM outputd_content_capture"
    )


def test_content_lane_signature_covers_outputd_open_contexts() -> None:
    """The bash signature tracks the context strings outputd actually attaches."""
    contexts = _open_context_literals(ALSA_BACKEND.read_text(encoding="utf-8"))
    expected = {
        "opening outputd content capture PCM X",
        "configuring outputd content capture PCM X",
        "opening outputd active content capture PCM X",
        "configuring outputd active content capture PCM X",
        "starting capture PCM X",
    }
    # Direction 1: a reworded Rust context fails here, next to the pattern that
    # has to learn the new wording.
    assert expected <= contexts, (
        "content-lane open contexts moved in rust/jasper-outputd/src/alsa_backend.rs; "
        "update CONTENT_LANE_SIGNATURE in deploy/bin/jasper-outputd-failure-reconcile "
        f"to match. missing={sorted(expected - contexts)}"
    )

    signature = _content_lane_signature()
    # Direction 2: every context that names the content lane at all — keyed on
    # `content`, not on today's exact `content capture` wording, so a rename
    # cannot slip past — must be matched by the helper's signature.
    covered = {c for c in contexts if "content" in c} | expected
    for context in sorted(covered):
        assert signature.search(f"Error: {context}"), (
            f"{context!r} names the content lane but CONTENT_LANE_SIGNATURE in "
            "deploy/bin/jasper-outputd-failure-reconcile does not match it"
        )

    # Non-vacuity: the signature is narrower than "any outputd open failure".
    assert "opening outputd DAC PCM X" in contexts
    assert not signature.search("Error: opening outputd DAC PCM outputd_dac")
