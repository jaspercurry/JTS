# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""The deploy-ordering guard for a content-lane WIDTH flip (wide-output-path PR-6).

THE HAZARD. snd-aloop locks a substream pair's rate/format/channels to its FIRST
opener. CamillaDSP owns the playback half of the outputd content lane and holds
it for as long as it runs, so a deploy that changes that lane's width has a
window where the still-running PREVIOUS CamillaDSP pins the pair at the old width
while the freshly-restarted jasper-outputd asks for the new one. That open fails
at ``snd_pcm_hw_params`` — an ordinary error, NOT outputd's EX_CONFIG park — so
``Restart=on-failure`` retries it and ``jasper-outputd.service``'s
``StartLimitBurst=5`` / ``StartLimitAction=reboot`` turns a format flip into a
REBOOT in the middle of an install.

Two halves, tested at their own level:

- ``jasper.camilla_config_contract.outputd_content_lane_format_delta`` — the
  decision (which states are a delta and which are deliberately not).
- ``deploy/lib/install/systemd-units.sh``'s
  ``release_camilla_content_lane_for_format_flip`` /
  ``restart_core_camilla_after_dsp_reconcile`` — the shell behaviour, exercised
  by sourcing the fragment with a shimmed ``systemctl`` that records calls,
  mirroring tests/test_install_core_audio_graph_loop.py's harness.

The ORDERING contract (release before the reconciler that restarts outputd;
restart after the DSP reconcile) lives in
tests/test_install_ring_platform_sequencing.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jasper.camilla_config_contract import (
    DEFAULT_PIPE_SINK_FORMAT,
    DEFAULT_PLAYBACK_FORMAT,
    outputd_content_lane_format_delta,
)

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"


def _config(device: str, fmt: str | None, *, playback_type: str = "Alsa") -> str:
    lines = [
        "---",
        "devices:",
        "  samplerate: 48000",
        "  capture:",
        "    type: Alsa",
        "    channels: 2",
        '    device: "plug:jasper_capture"',
        "    format: S32_LE",
        "  playback:",
        f"    type: {playback_type}",
        "    channels: 2",
        f'    device: "{device}"',
    ]
    if fmt is not None:
        lines.append(f"    format: {fmt}")
    lines.append("filters:")
    return "\n".join(lines) + "\n"


def _statefile(tmp_path: Path, config_text: str | None) -> Path:
    statefile = tmp_path / "outputd-statefile.yml"
    if config_text is None:
        statefile.write_text("volume: [-20.0]\n", encoding="utf-8")
        return statefile
    config = tmp_path / "sound_current.yml"
    config.write_text(config_text, encoding="utf-8")
    statefile.write_text(f"config_path: {config}\nvolume: [-20.0]\n", encoding="utf-8")
    return statefile


# --- the decision ------------------------------------------------------------


@pytest.mark.parametrize(
    "device",
    ["outputd_content_playback", "outputd_active_content_playback"],
)
def test_delta_reported_for_a_narrow_aloop_lane_on_both_lanes(tmp_path, device):
    """The state that reboots the Pi: CamillaDSP holding an snd-aloop content
    lane — passive OR active — at the pre-flip width."""
    statefile = _statefile(tmp_path, _config(device, "S16_LE"))
    delta = outputd_content_lane_format_delta(statefile)
    assert delta is not None
    config_path, live, emitted = delta
    assert live == "S16_LE"
    assert emitted == DEFAULT_PLAYBACK_FORMAT == "S32_LE"
    assert config_path.endswith("sound_current.yml")


def test_no_delta_once_the_lane_already_carries_the_emitted_width(tmp_path):
    """The second and every later deploy pays no churn — this is what keeps the
    guard from becoming an unconditional camilla stop on every deploy forever."""
    statefile = _statefile(
        tmp_path, _config("outputd_content_playback", DEFAULT_PLAYBACK_FORMAT)
    )
    assert outputd_content_lane_format_delta(statefile) is None


def test_no_delta_for_a_ring_box(tmp_path):
    """An armed shm_ring box writes jts_ring_playback, which is not an snd-aloop
    pair: nobody is holding a lane, so there is nothing to release. Its ring also
    stays coherently narrow (the PR-6 ring ruling), so the narrow format here is
    correct and must not read as a delta."""
    statefile = _statefile(tmp_path, _config("jts_ring_playback", "S16_LE"))
    assert outputd_content_lane_format_delta(statefile) is None


def test_no_delta_for_a_pipe_sink_leader(tmp_path):
    """A bonded leader's CamillaDSP writes a File/pipe sink pinned to
    DEFAULT_PIPE_SINK_FORMAT (D4). No snd-aloop lane, and the narrow format is
    the correct one for that sink."""
    statefile = _statefile(
        tmp_path,
        _config(
            "/run/jasper-snapserver/snapfifo",
            DEFAULT_PIPE_SINK_FORMAT,
            playback_type="File",
        ),
    )
    assert outputd_content_lane_format_delta(statefile) is None


@pytest.mark.parametrize(
    "config_text",
    [None, _config("outputd_content_playback", None)],
    ids=["no_config_path", "no_format_field"],
)
def test_no_delta_when_nothing_is_proven(tmp_path, config_text):
    """Fail direction: an unreadable statefile or a config with no declared
    format proves no delta, so the guard stays quiet rather than adding a stop to
    every deploy. In practice neither state can hold an aloop lane at the wrong
    width, and outputd's own loud failure remains the backstop."""
    assert outputd_content_lane_format_delta(_statefile(tmp_path, config_text)) is None


def test_no_delta_for_a_missing_statefile(tmp_path):
    assert outputd_content_lane_format_delta(tmp_path / "absent.yml") is None
    assert outputd_content_lane_format_delta(None) is None


# --- the shell behaviour -----------------------------------------------------


def _harness(
    tmp_path: Path,
    *,
    camilla_active: bool,
    delta: str,
    call_restart: bool = True,
) -> str:
    """A bash script that sources the fragment and drives the two helpers with a
    recording `systemctl`. `delta` is what the format probe reports (empty ==
    no delta); the probe is stubbed rather than run so this exercises the SHELL
    decision — the Python decision has its own tests above."""
    log = tmp_path / "systemctl.log"
    active_rc = 0 if camilla_active else 1
    restart_call = "restart_core_camilla_after_dsp_reconcile" if call_restart else ":"
    return f"""
set -euo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path}/systemd"
INSTALL_DIR="{tmp_path}"
systemctl() {{
  if [[ "$1" == "is-active" ]]; then
    echo "is-active $*" >> "{log}"
    return {active_rc}
  fi
  echo "$*" >> "{log}"
  return 0
}}
source "{FRAGMENT}"
camilla_content_lane_format_delta() {{ printf '%s' '{delta}'; }}
release_camilla_content_lane_for_format_flip
{restart_call}
"""


def _run(script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )


def _log(tmp_path: Path) -> list[str]:
    return (tmp_path / "systemctl.log").read_text(encoding="utf-8").splitlines()


def test_shell_stops_then_starts_camilla_on_a_real_delta(tmp_path):
    result = _run(_harness(tmp_path, camilla_active=True, delta="S16_LE -> S32_LE (x)"))
    assert result.returncode == 0, result.stderr
    calls = _log(tmp_path)
    assert "stop jasper-camilla.service" in calls
    # `start`, NOT `try-restart`: try-restart is a no-op on a stopped unit and
    # would leave the box with no DSP at all.
    assert "start jasper-camilla.service" in calls
    assert "try-restart jasper-camilla.service" not in calls
    assert calls.index("stop jasper-camilla.service") < calls.index(
        "start jasper-camilla.service"
    )
    assert "Releasing the outputd content lane" in result.stdout


def test_shell_leaves_camilla_alone_when_there_is_no_delta(tmp_path):
    result = _run(_harness(tmp_path, camilla_active=True, delta=""))
    assert result.returncode == 0, result.stderr
    calls = _log(tmp_path)
    assert "stop jasper-camilla.service" not in calls
    assert "try-restart jasper-camilla.service" in calls
    assert "start jasper-camilla.service" not in calls
    assert "Releasing the outputd content lane" not in result.stdout


def test_shell_never_starts_a_camilla_it_did_not_stop(tmp_path):
    """A deliberately-stopped CamillaDSP stays stopped: the probe is not even
    consulted, and the late step remains the historical `try-restart` (a no-op on
    an inactive unit)."""
    result = _run(
        _harness(tmp_path, camilla_active=False, delta="S16_LE -> S32_LE (x)")
    )
    assert result.returncode == 0, result.stderr
    calls = _log(tmp_path)
    assert "stop jasper-camilla.service" not in calls
    assert "start jasper-camilla.service" not in calls
    assert "try-restart jasper-camilla.service" in calls


def test_shell_clears_the_release_flag_after_starting(tmp_path):
    """The flag is per-install state, not sticky: a second restart call must not
    re-`start` a camilla the guard is no longer responsible for."""
    script = _harness(
        tmp_path, camilla_active=True, delta="S16_LE -> S32_LE (x)"
    ) + "\nrestart_core_camilla_after_dsp_reconcile\n"
    result = _run(script)
    assert result.returncode == 0, result.stderr
    calls = _log(tmp_path)
    assert calls.count("start jasper-camilla.service") == 1
    assert calls.count("try-restart jasper-camilla.service") == 1


def test_shell_probe_is_quiet_without_an_installed_interpreter(tmp_path):
    """The real probe (not the stub) with no venv python present: prints nothing,
    so the guard reads it as no-delta rather than erroring out mid-install."""
    script = f"""
set -euo pipefail
REPO_DIR="{ROOT}"
SYSTEMD_DIR="{tmp_path}/systemd"
INSTALL_DIR="{tmp_path / 'absent'}"
source "{FRAGMENT}"
out="$(camilla_content_lane_format_delta)"
echo "probe=[${{out}}]"
"""
    result = _run(script)
    assert result.returncode == 0, result.stderr
    assert "probe=[]" in result.stdout
