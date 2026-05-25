"""Tests for jasper-audio-topology — the dmix ↔ fanin CLI switch.

The script's destructive paths (stop/start daemons) need a real
system to exercise; these tests cover everything that's testable
hardware-free:

  - `status` and `--dry-run` modes (no root, no systemd, no audio)
  - default-mode detection when the env file is absent
  - dry-run output covers every step it would take
  - the fanin asoundrc template renders cleanly
  - install.sh wiring (binary + template land in the right place)

The actual switch (start/stop daemons + verify health) gets
validated manually on hardware via `sudo jasper-audio-topology fanin`
during Tier 2A Phase 3 testing.
"""
from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "deploy" / "bin" / "jasper-audio-topology"


def _run(
    args: list[str],
    *,
    topology_env_path: Path | None = None,
    asound_conf_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the script with the given args, redirecting env-file
    locations to test fixtures. Read-only modes (status, dry-run) are
    the only ones safe to run in unit tests."""
    env = os.environ.copy()
    if topology_env_path is not None:
        env["JASPER_AUDIO_TOPOLOGY_ENV"] = str(topology_env_path)
    if asound_conf_path is not None:
        env["JASPER_ASOUND_CONF"] = str(asound_conf_path)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_script_exists_and_is_executable():
    assert SCRIPT.exists(), f"missing {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), (
        f"{SCRIPT} must be executable (install.sh installs with "
        f"mode 0755)"
    )


def test_bash_syntax_clean():
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"bash syntax check failed:\n{result.stderr}"
    )


def test_help_flag(tmp_path: Path):
    result = _run(["--help"], topology_env_path=tmp_path / "missing.env")
    assert result.returncode == 0
    assert "jasper-audio-topology" in result.stdout
    assert "dmix" in result.stdout
    assert "fanin" in result.stdout


def test_status_when_env_file_absent_shows_dmix(tmp_path: Path):
    """No /var/lib/jasper/audio_topology.env → default = dmix."""
    missing = tmp_path / "audio_topology.env"
    asound = tmp_path / "asound.conf"
    asound.write_text('pcm "hw:CARD=A,DEV=0"\n')
    result = _run(
        ["status"],
        topology_env_path=missing,
        asound_conf_path=asound,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "active topology: dmix" in result.stdout
    assert "absent" in result.stdout  # mentions missing env file


def test_status_when_env_says_fanin(tmp_path: Path):
    env_path = tmp_path / "audio_topology.env"
    env_path.write_text("JASPER_AUDIO_TOPOLOGY=fanin\n")
    asound = tmp_path / "asound.conf"
    asound.write_text('pcm "hw:CARD=A,DEV=0"\n')
    result = _run(
        ["status"],
        topology_env_path=env_path,
        asound_conf_path=asound,
    )
    assert result.returncode == 0
    assert "active topology: fanin" in result.stdout


def test_no_args_is_status(tmp_path: Path):
    missing = tmp_path / "audio_topology.env"
    asound = tmp_path / "asound.conf"
    asound.write_text('pcm "hw:CARD=A,DEV=0"\n')
    result = _run(
        [],
        topology_env_path=missing,
        asound_conf_path=asound,
    )
    assert result.returncode == 0
    assert "active topology: dmix" in result.stdout


def test_dry_run_dmix_to_fanin_shows_full_plan(tmp_path: Path):
    """Dry-run from dmix → fanin must enumerate every step the
    actual switch would take, so the operator can sanity-check
    before committing."""
    missing = tmp_path / "audio_topology.env"  # dmix default
    asound = tmp_path / "asound.conf"
    asound.write_text('pcm "hw:CARD=A,DEV=0"\n')
    result = _run(
        ["fanin", "--dry-run"],
        topology_env_path=missing,
        asound_conf_path=asound,
    )
    assert result.returncode == 0
    assert "DRY RUN" in result.stdout
    # Plan must mention: stop chain, write env, install asoundrc,
    # regenerate shairport conf, daemon-reload, enable fanin, start
    # chain, verify.
    assert "Stop renderers" in result.stdout
    assert "JASPER_AUDIO_TOPOLOGY=fanin" in result.stdout
    assert "JASPER_LIBRESPOT_DEVICE=librespot_substream" in result.stdout
    assert "JASPER_BLUEALSA_DEVICE=bluealsa_substream" in result.stdout
    assert "JASPER_USBSINK_PLAYBACK_DEVICE=usbsink_substream" in result.stdout
    assert "jasper-apply-airplay-mode" in result.stdout
    assert "daemon-reload" in result.stdout
    assert "enable --now jasper-fanin" in result.stdout
    assert "Verify health" in result.stdout
    # And it explicitly says no changes were made.
    assert "no changes made" in result.stdout


def test_dry_run_fanin_to_dmix_disables_fanin_service(tmp_path: Path):
    env_path = tmp_path / "audio_topology.env"
    env_path.write_text("JASPER_AUDIO_TOPOLOGY=fanin\n")
    asound = tmp_path / "asound.conf"
    asound.write_text('pcm "hw:CARD=A,DEV=0"\n')
    result = _run(
        ["dmix", "--dry-run"],
        topology_env_path=env_path,
        asound_conf_path=asound,
    )
    assert result.returncode == 0
    assert "DRY RUN" in result.stdout
    assert "JASPER_LIBRESPOT_DEVICE=jasper_renderer_in" in result.stdout
    assert "disable jasper-fanin" in result.stdout


def test_already_in_target_mode_is_idempotent_noop(tmp_path: Path):
    """`jasper-audio-topology dmix` when already in dmix is a no-op
    and doesn't require root (because no system changes happen)."""
    missing = tmp_path / "audio_topology.env"  # dmix default
    asound = tmp_path / "asound.conf"
    asound.write_text('pcm "hw:CARD=A,DEV=0"\n')
    result = _run(
        ["dmix"],
        topology_env_path=missing,
        asound_conf_path=asound,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "Already in 'dmix' mode" in result.stdout


def test_unknown_argument_errors(tmp_path: Path):
    result = _run(["sideways"])
    assert result.returncode != 0
    assert "unknown argument: sideways" in result.stderr


def test_fanin_asound_conf_template_renders_cleanly(tmp_path: Path):
    """The fanin asoundrc template must contain __DONGLE_CARD__ and
    nothing else that needs substitution. (Sanity that the
    template author didn't introduce a new placeholder the switch
    script doesn't know about.)"""
    template = (
        REPO / "deploy" / "audio-topology" / "fanin"
        / "asound.conf.template"
    )
    body = template.read_text()
    placeholders = set(re.findall(r"__[A-Z_]+__", body))
    assert placeholders == {"__DONGLE_CARD__"}, (
        f"unexpected placeholders in fanin asoundrc template: "
        f"{placeholders}. Only __DONGLE_CARD__ is supported by "
        f"jasper-audio-topology."
    )


def test_fanin_asoundrc_omits_dmix_blocks():
    """The fanin variant must NOT define `pcm.jasper_renderer_mix {`
    or `pcm.jasper_renderer_in {` (the block-definition syntax) —
    those are the dmix-mode constructs that Tier 2A deletes.
    The names may appear in comments documenting what was removed;
    we check for the block opener specifically, not the bare name."""
    body = (
        REPO / "deploy" / "audio-topology" / "fanin"
        / "asound.conf.template"
    ).read_text()
    # Strip comment lines so a doc-only mention of the name doesn't
    # false-positive.
    non_comment_lines = [
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    ]
    non_comment_body = "\n".join(non_comment_lines)
    # Block definitions are `pcm.NAME {` (whitespace-tolerant).
    assert not re.search(
        r"^pcm\.jasper_renderer_mix\s*\{", non_comment_body, re.MULTILINE
    ), (
        "fanin asoundrc must not define the renderer-side dmix block "
        "(its point is to delete that layer)"
    )
    assert not re.search(
        r"^pcm\.jasper_renderer_in\s*\{", non_comment_body, re.MULTILINE
    ), (
        "fanin asoundrc must not define the renderer-side dmix plug "
        "wrapper block"
    )


def test_fanin_asoundrc_dsnoop_targets_substream_7():
    """In fanin mode, pcm.jasper_capture dsnoop reads from
    hw:Loopback,1,7 (the summed-music substream that jasper-fanin
    writes to), not hw:Loopback,1,0 (the dmix slave)."""
    body = (
        REPO / "deploy" / "audio-topology" / "fanin"
        / "asound.conf.template"
    ).read_text()
    assert 'pcm "hw:Loopback,1,7"' in body, (
        "fanin pcm.jasper_capture must dsnoop on substream 7 "
        "(jasper-fanin's output)"
    )
    assert 'pcm "hw:Loopback,1,0"' not in body, (
        "fanin pcm.jasper_capture must NOT still point at "
        "substream 0 (the dmix slave)"
    )


def test_install_sh_installs_topology_script_and_template():
    install_sh = (REPO / "deploy" / "install.sh").read_text()
    assert "jasper-audio-topology" in install_sh, (
        "install.sh must install /usr/local/sbin/jasper-audio-topology"
    )
    assert "/usr/local/sbin/jasper-audio-topology" in install_sh
    assert "/etc/jasper/audio-topology/fanin/asound.conf.template" in install_sh, (
        "install.sh must install the fanin asoundrc template"
    )


def test_renderer_units_source_audio_topology_env():
    """Each renderer's systemd unit must source
    /var/lib/jasper/audio_topology.env via EnvironmentFile=- so the
    JASPER_<RENDERER>_DEVICE env var actually reaches the daemon."""
    units_with_topology_env = [
        REPO / "deploy" / "systemd" / "librespot.service",
        REPO / "deploy" / "systemd" / "bluealsa-aplay.service.d"
            / "jts-output.conf",
        REPO / "deploy" / "systemd" / "jasper-usbsink.service",
    ]
    for unit_path in units_with_topology_env:
        body = unit_path.read_text()
        assert "EnvironmentFile=-/var/lib/jasper/audio_topology.env" in body, (
            f"{unit_path.name} must source "
            f"/var/lib/jasper/audio_topology.env via "
            f"EnvironmentFile=- so the topology switch picks up the "
            f"new device value without a redeploy"
        )


def test_librespot_uses_topology_env_var():
    """librespot.service's ExecStart must reference
    ${JASPER_LIBRESPOT_DEVICE}, not the literal jasper_renderer_in.
    Without the env var, the topology switch can't redirect the
    renderer."""
    body = (
        REPO / "deploy" / "systemd" / "librespot.service"
    ).read_text()
    assert "${JASPER_LIBRESPOT_DEVICE}" in body, (
        "librespot.service must use ${JASPER_LIBRESPOT_DEVICE} "
        "(set by /var/lib/jasper/audio_topology.env) for the "
        "topology switch to work"
    )
    # And a default Environment= value so the absence of the env
    # file falls back to dmix mode.
    assert 'Environment="JASPER_LIBRESPOT_DEVICE=jasper_renderer_in"' in body, (
        "librespot.service must declare a JASPER_LIBRESPOT_DEVICE "
        "default of jasper_renderer_in so the absence of the "
        "topology env file = dmix mode"
    )


def test_bluealsa_aplay_uses_topology_env_var():
    body = (
        REPO / "deploy" / "systemd" / "bluealsa-aplay.service.d"
        / "jts-output.conf"
    ).read_text()
    assert "${JASPER_BLUEALSA_DEVICE}" in body
    assert 'Environment="JASPER_BLUEALSA_DEVICE=jasper_renderer_in"' in body
