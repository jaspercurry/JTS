"""Test install.sh city-pack seeding in migrate_transit_config.

The transit rewiring (jasper.transit city packs) added a JASPER_TRANSIT_CITIES
toggle. migrate_transit_config seeds it to "nyc" for existing households that
already use NYC transit, so the /transit/ wizard renders the right toggle
state and the value is codified rather than relying on the "unset = all packs"
fallback. The seeding must be: gated on real NYC config, idempotent, and never
presumptuous (no seed when no transit is configured).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = ROOT / "deploy" / "install.sh"


def _run_migrate(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir(exist_ok=True)
    state_dir.mkdir(exist_ok=True)

    helper = subprocess.run(
        [
            "bash",
            "-c",
            rf"sed -n '/^migrate_transit_config()/,/^}}/p' '{INSTALL_SH}'",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "migrate_transit_config()" in helper

    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "ENV_DIR": str(env_dir),
        "STATE_DIR": str(state_dir),
    }
    return subprocess.run(
        ["/bin/bash", "-c", f"{helper}\nmigrate_transit_config"],
        env=env,
        capture_output=True,
        text=True,
    )


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key] = value
    return out


def test_seeds_nyc_when_subway_configured(tmp_path):
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    (env_dir / "jasper.env").write_text("")  # must exist or the fn returns early
    (state_dir / "transit.env").write_text("JASPER_SUBWAY_STATION_ID=127\n")

    proc = _run_migrate(tmp_path)
    assert proc.returncode == 0, proc.stderr
    transit = _read_env(state_dir / "transit.env")
    assert transit["JASPER_TRANSIT_CITIES"] == "nyc"


def test_no_seed_when_no_transit_configured(tmp_path):
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    (env_dir / "jasper.env").write_text("")
    # Only the geocode scaffolding is present — no subway/bus/bike mode set up.
    (state_dir / "transit.env").write_text(
        "JASPER_TRANSIT_LAT=40.653\nJASPER_TRANSIT_LON=-74.007\n"
    )

    proc = _run_migrate(tmp_path)
    assert proc.returncode == 0, proc.stderr
    transit = _read_env(state_dir / "transit.env")
    assert "JASPER_TRANSIT_CITIES" not in transit


def test_empty_config_values_do_not_trigger_seed(tmp_path):
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    (env_dir / "jasper.env").write_text("")
    # Present-but-empty keys are "not configured" — must not seed.
    (state_dir / "transit.env").write_text(
        "JASPER_SUBWAY_STATION_ID=\nJASPER_BUS_STOPS=\n"
    )

    proc = _run_migrate(tmp_path)
    assert proc.returncode == 0, proc.stderr
    transit = _read_env(state_dir / "transit.env")
    assert "JASPER_TRANSIT_CITIES" not in transit


def test_idempotent_keeps_explicit_value(tmp_path):
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    (env_dir / "jasper.env").write_text("")
    (state_dir / "transit.env").write_text(
        "JASPER_SUBWAY_STATION_ID=127\nJASPER_TRANSIT_CITIES=berlin\n"
    )

    # Two runs: an explicit (even if odd) value is never overwritten, and no
    # duplicate line accumulates.
    assert _run_migrate(tmp_path).returncode == 0
    assert _run_migrate(tmp_path).returncode == 0
    lines = (state_dir / "transit.env").read_text().splitlines()
    city_lines = [ln for ln in lines if ln.startswith("JASPER_TRANSIT_CITIES=")]
    assert city_lines == ["JASPER_TRANSIT_CITIES=berlin"]


def test_seeds_after_migrating_bus_key_from_jasper_env(tmp_path):
    """An operator who pasted JASPER_BUS_STOPS into jasper.env gets it moved to
    transit.env by the key loop, then the seed step sees the configured mode and
    writes the city toggle in the same run. Bus labels contain spaces, so this
    also guards the migration's space-preserving value handling."""
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    (env_dir / "jasper.env").write_text(
        "JASPER_BUS_STOPS=MTA_304213|39 ST/4 AV SE\n"
        "JASPER_MTA_BUSTIME_KEY=abc123\n"
    )

    proc = _run_migrate(tmp_path)
    assert proc.returncode == 0, proc.stderr
    transit = _read_env(state_dir / "transit.env")
    assert transit["JASPER_BUS_STOPS"] == "MTA_304213|39 ST/4 AV SE"
    assert transit["JASPER_TRANSIT_CITIES"] == "nyc"
