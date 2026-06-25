# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free coverage for deploy/bin/jasper-camilla-recover."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "bin" / "jasper-camilla-recover"


def _write_exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _fake_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    calls = tmp_path / "systemctl.calls"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state_dir = tmp_path / "state"
    asound = tmp_path / "asound"
    status_dir = asound / "card0" / "pcm0p" / "sub0"
    status_dir.mkdir(parents=True)
    (status_dir / "status").write_text("state: RUNNING\nowner: fake\n", encoding="utf-8")
    dev_snd = tmp_path / "dev_snd"
    dev_snd.mkdir()
    (dev_snd / "pcmC0D0p").write_text("", encoding="utf-8")

    _write_exe(
        bin_dir / "fake-systemctl",
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {calls}\nexit 0\n",
    )
    _write_exe(
        bin_dir / "fuser",
        "#!/usr/bin/env bash\necho 'fake-holder fuser'\nexit 0\n",
    )
    _write_exe(
        bin_dir / "lsof",
        "#!/usr/bin/env bash\necho 'fake-holder lsof'\nexit 0\n",
    )

    env = os.environ.copy()
    env.update(
        {
            "JASPER_SYSTEMCTL": str(bin_dir / "fake-systemctl"),
            "JASPER_CAMILLA_RECOVER_STATE_DIR": str(state_dir),
            "JASPER_CAMILLA_RECOVER_RUN_DIR": str(run_dir),
            "JASPER_ASOUND_ROOT": str(asound),
            "JASPER_DEV_SND_ROOT": str(dev_snd),
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
        }
    )
    return env, calls


def test_camilla_recover_captures_evidence_and_restarts_core_graph(tmp_path: Path):
    env, calls = _fake_env(tmp_path)

    result = subprocess.run(
        [str(SCRIPT), "--reason", "pytest"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert "event=camilla.recover.start" in result.stderr
    assert "event=camilla.recover.capture_line label=fuser" in result.stderr
    assert "event=camilla.recover.asound_status_line" in result.stderr
    assert "event=camilla.recover.recovered action=core_graph_restarted" in result.stderr

    call_text = calls.read_text(encoding="utf-8")
    assert "stop jasper-outputd.service" in call_text
    assert "reset-failed jasper-camilla.service" in call_text
    assert "restart jasper-fanin.service" in call_text
    assert "start jasper-camilla.service" in call_text
    assert "restart jasper-outputd.service" in call_text
    assert "reboot" not in call_text


def test_camilla_recover_cooldown_parks_without_retrying_graph(tmp_path: Path):
    env, calls = _fake_env(tmp_path)
    env["JASPER_CAMILLA_RECOVER_COOLDOWN_SEC"] = "999"

    first = subprocess.run(
        [str(SCRIPT), "--reason", "first"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert first.returncode == 0
    calls.write_text("", encoding="utf-8")

    second = subprocess.run(
        [str(SCRIPT), "--reason", "second"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert second.returncode == 0
    assert "event=camilla.recover.suppressed reason=cooldown" in second.stderr
    call_text = calls.read_text(encoding="utf-8")
    assert "status jasper-camilla.service" in call_text
    assert "start jasper-camilla.service" not in call_text
    assert "restart jasper-outputd.service" not in call_text
    assert "reboot" not in call_text
