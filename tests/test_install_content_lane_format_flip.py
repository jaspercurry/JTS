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

- ``jasper.audio_runtime_plan.outputd_content_format_change`` — the decision
  (what changes the requested width and what deliberately does not).
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

from jasper.audio_runtime_plan import outputd_content_format_change
from jasper.camilla_config_contract import DEFAULT_PLAYBACK_FORMAT
from jasper.fanin_coupling import RING_WIRE_FORMAT

ROOT = Path(__file__).resolve().parents[1]
FRAGMENT = ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"


def _envs(tmp_path: Path, *, outputd: str | None, fanin: str | None) -> dict:
    """Write the two reconciler-owned env files and return the probe's kwargs.

    ``None`` means the file is absent, which is the honest shape of a box that
    has never been reconciled (and of every box before this key existed)."""
    outputd_path = tmp_path / "outputd.env"
    fanin_path = tmp_path / "fanin.env"
    if outputd is not None:
        outputd_path.write_text(outputd, encoding="utf-8")
    if fanin is not None:
        fanin_path.write_text(fanin, encoding="utf-8")
    return {
        "outputd_env_path": str(outputd_path),
        "fanin_env_path": str(fanin_path),
    }


# --- the decision ------------------------------------------------------------


@pytest.mark.parametrize(
    "outputd",
    [None, "", "JASPER_OUTPUTD_CONTENT_FORMAT=\n", "JASPER_OUTPUTD_CONTENT_FORMAT=S16_LE\n"],
    ids=["absent_file", "empty_file", "empty_value", "explicit_s16"],
)
def test_change_reported_when_a_pre_flip_box_is_about_to_widen(tmp_path, outputd):
    """The state that reboots the Pi: outputd currently requesting the narrow
    width — whether the key is absent, empty, or explicitly S16 — while this build
    is about to ask for the wide one. Absent/empty read as S16 because that is
    outputd's own documented default for the env, i.e. what the lane is locked
    at."""
    change = outputd_content_format_change(**_envs(tmp_path, outputd=outputd, fanin=None))
    assert change == ("S16_LE", DEFAULT_PLAYBACK_FORMAT)
    assert DEFAULT_PLAYBACK_FORMAT == "S32_LE"


def test_no_change_once_the_box_already_requests_the_wide_width(tmp_path):
    """The second and every later deploy pays no churn — this is what keeps the
    guard from becoming an unconditional camilla stop on every deploy forever."""
    assert (
        outputd_content_format_change(
            **_envs(
                tmp_path,
                outputd=f"JASPER_OUTPUTD_CONTENT_FORMAT={DEFAULT_PLAYBACK_FORMAT}\n",
                fanin=None,
            )
        )
        is None
    )


@pytest.mark.parametrize(
    "outputd",
    [None, f"JASPER_OUTPUTD_CONTENT_FORMAT={RING_WIRE_FORMAT}\n"],
    ids=["pre_flip", "post_flip"],
)
def test_no_change_on_a_narrow_pinned_ring_box_either_side_of_the_flip(
    tmp_path, monkeypatch, outputd
):
    """RENAMED and RE-POINTED (jasper.fanin_coupling.resolve_ring_wire_format's
    own default flip, 2026-08-11 — a SECOND, LATER flip than the one this
    module's docstring describes). The old test kept this name across the PR-6
    (loopback) flip because shm_ring FORCED the narrow width regardless of what
    that flip did to loopback's own default, so an armed ring box was narrow
    both before and after it, paying no churn either way.

    Now the ring wire's OWN resolver defaults WIDE too, so an UNPINNED armed
    ring box no longer "keeps the coupling-forced narrow width" at all — see
    tests/test_fanin_coupling.py::test_content_lane_format_is_one_definition_for_both_ends_of_the_hop.
    What survives is the coarser invariant this decision function actually
    promises: a box already sitting at the width its coupling will request pays
    no churn. Reproducing THAT now needs an explicit operator narrow pin
    (JASPER_FANIN_RING_WIRE_FORMAT=S16_LE).

    outputd_content_format_change's own fanin_env_path only threads through to
    read_persisted_coupling (the COUPLING token) — content_lane_format_for_coupling's
    ring-wire read (jasper.fanin_coupling.read_declared_ring_wire_format) is
    FILE-FRESH against the module-level FANIN_ENV_PATH /
    var/lib/jasper/fanin.env constant instead, so writing the pin into this
    test's own fanin.env has no effect unless that constant is ALSO patched
    onto the same file — the established pattern this repo already uses
    everywhere else the ring wire format needs a hermetic env (e.g.
    tests/test_doctor_usbsink.py, tests/test_ring_gates_recovery.py).
    """
    from jasper.fanin import coupling_reconcile as cr

    monkeypatch.setattr(cr, "FANIN_ENV_PATH", str(tmp_path / "fanin.env"))
    assert (
        outputd_content_format_change(
            **_envs(
                tmp_path,
                outputd=outputd,
                fanin=(
                    "JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n"
                    "JASPER_FANIN_RING_WIRE_FORMAT=S16_LE\n"
                ),
            )
        )
        is None
    )


def test_change_reported_when_a_wide_box_arms_a_narrow_pinned_ring(
    tmp_path, monkeypatch
):
    """The other direction is a change too: a wide box whose coupling moves to a
    NARROW-PINNED shm_ring will request that pin's width, so the lane's lock has
    to be released just the same. The check is an equality, never a width
    ranking.

    RE-POINTED (see the sibling test above for the full account): arming
    shm_ring UNPINNED no longer diverges from a wide box's current width — both
    now resolve S32_LE, which is byte-identical no-change rather than the
    divergence this test exists to demonstrate. Reproducing a genuine
    divergence needs the same explicit operator narrow pin, with FANIN_ENV_PATH
    patched onto this test's own fanin.env for the same file-fresh-vs-real-path
    reason.
    """
    from jasper.fanin import coupling_reconcile as cr

    monkeypatch.setattr(cr, "FANIN_ENV_PATH", str(tmp_path / "fanin.env"))
    change = outputd_content_format_change(
        **_envs(
            tmp_path,
            outputd=f"JASPER_OUTPUTD_CONTENT_FORMAT={DEFAULT_PLAYBACK_FORMAT}\n",
            fanin=(
                "JASPER_FANIN_CAMILLA_COUPLING=shm_ring\n"
                "JASPER_FANIN_RING_WIRE_FORMAT=S16_LE\n"
            ),
        )
    )
    assert change == (DEFAULT_PLAYBACK_FORMAT, RING_WIRE_FORMAT)


def test_the_probe_reads_only_env_files_never_a_camilla_config(monkeypatch, tmp_path):
    """Both sides come from reconciler-owned env ON PURPOSE. install re-renders the
    flat cutover config BEFORE the bounce, so a config-file read at bounce time can
    already show the NEW width while the running CamillaDSP still holds the lane at
    the old one — stale in the one direction that matters.

    Enforced by POISONING every non-``.env`` read for the duration of the call,
    rather than by writing a decoy config next door: the probe takes no
    config-path parameter, so a decoy in ``tmp_path`` could never be reached and
    a future HARDCODED read of ``/var/lib/camilladsp/...`` would sail past it.
    Both legitimate readers (``read_env_file_state`` and
    ``read_persisted_coupling``) go through ``Path.read_text`` on a ``.env``
    path, so anything else opening a file here is by construction a read this
    probe must not be doing — and the test fails naming it.
    """
    real_read_text = Path.read_text
    opened: list[str] = []

    def poisoned_read_text(self, *args, **kwargs):
        opened.append(str(self))
        if self.suffix != ".env":
            raise AssertionError(
                f"outputd_content_format_change read a non-env file: {self}. "
                "Both sides of this probe must come from reconciler-owned env; "
                "a CamillaDSP config read here is stale by construction."
            )
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", poisoned_read_text)
    change = outputd_content_format_change(**_envs(tmp_path, outputd=None, fanin=None))
    assert change == ("S16_LE", DEFAULT_PLAYBACK_FORMAT)
    # And the poisoning was actually exercised — a probe that read nothing at all
    # would pass the assertion above vacuously.
    assert opened, "the probe read no files; the guard proved nothing"
    assert all(p.endswith(".env") for p in opened), opened


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
    result = _run(_harness(tmp_path, camilla_active=True, delta="S16_LE -> S32_LE"))
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
        _harness(tmp_path, camilla_active=False, delta="S16_LE -> S32_LE")
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
        tmp_path, camilla_active=True, delta="S16_LE -> S32_LE"
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
