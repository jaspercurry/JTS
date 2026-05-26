"""Test install.sh weather-location migration helper."""
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
            rf"sed -n '/^migrate_weather_config()/,/^}}/p' '{INSTALL_SH}'",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "migrate_weather_config()" in helper

    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "ENV_DIR": str(env_dir),
        "STATE_DIR": str(state_dir),
    }
    return subprocess.run(
        ["/bin/bash", "-c", f"{helper}\nmigrate_weather_config"],
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


def test_migrate_weather_moves_legacy_jasper_env_keys(tmp_path):
    env_dir = tmp_path / "etc"
    env_dir.mkdir()
    (env_dir / "jasper.env").write_text(
        "JASPER_DEFAULT_LOCATION=11232\n"
        "JASPER_WEATHER_UNITS=fahrenheit\n"
    )
    proc = _run_migrate(tmp_path)
    assert proc.returncode == 0, proc.stderr
    weather = _read_env(tmp_path / "state" / "weather.env")
    assert weather["JASPER_DEFAULT_LOCATION"] == "11232"
    assert weather["JASPER_WEATHER_UNITS"] == "fahrenheit"
    assert "JASPER_DEFAULT_LOCATION" not in (env_dir / "jasper.env").read_text()


def test_migrate_weather_seeds_weather_from_transit_coords(tmp_path):
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    (env_dir / "jasper.env").write_text("")
    (state_dir / "transit.env").write_text(
        "JASPER_TRANSIT_LAT=40.653\n"
        "JASPER_TRANSIT_LON=-74.007\n"
        "JASPER_TRANSIT_DISPLAY_NAME=Sunset Park, Brooklyn\n"
    )
    proc = _run_migrate(tmp_path)
    assert proc.returncode == 0, proc.stderr
    weather = _read_env(state_dir / "weather.env")
    assert weather["JASPER_WEATHER_LAT"] == "40.653"
    assert weather["JASPER_WEATHER_LON"] == "-74.007"
    assert weather["JASPER_WEATHER_DISPLAY_NAME"] == "Sunset Park, Brooklyn"
    assert weather["JASPER_DEFAULT_LOCATION"] == "Sunset Park, Brooklyn"


def test_migrate_weather_seeds_transit_from_weather_coords(tmp_path):
    env_dir = tmp_path / "etc"
    state_dir = tmp_path / "state"
    env_dir.mkdir()
    state_dir.mkdir()
    (env_dir / "jasper.env").write_text("")
    (state_dir / "weather.env").write_text(
        "JASPER_WEATHER_LAT=48.857\n"
        "JASPER_WEATHER_LON=2.352\n"
        "JASPER_WEATHER_DISPLAY_NAME=Paris, France\n"
    )
    proc = _run_migrate(tmp_path)
    assert proc.returncode == 0, proc.stderr
    transit = _read_env(state_dir / "transit.env")
    assert transit["JASPER_TRANSIT_LAT"] == "48.857"
    assert transit["JASPER_TRANSIT_LON"] == "2.352"
    assert transit["JASPER_TRANSIT_DISPLAY_NAME"] == "Paris, France"
