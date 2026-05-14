"""jasper-airplay-mode — toggle shairport's drift-correction mode.

Synced (default) keeps AirPlay video A/V sync and multi-room timing
intact. Free-running is a fallback for unforeseen DAC-specific issues.

Usage:
    jasper-airplay-mode show
    jasper-airplay-mode set free-running
    jasper-airplay-mode set synced

The setting is persisted at /var/lib/jasper/airplay_mode.env as
`JASPER_AIRPLAY_FREE_RUNNING=yes|no`. After a `set`, this command
re-renders /etc/shairport-sync.conf via jasper-apply-airplay-mode
and restarts shairport-sync.

Same env file backs the /airplay/ web UI. Either path is fine; they
write atomically and converge.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

MODE_ENV_FILE = "/var/lib/jasper/airplay_mode.env"
ENV_VAR = "JASPER_AIRPLAY_FREE_RUNNING"


def _read_mode() -> str:
    """Return 'free-running' or 'synced' based on the env file. Default
    synced when the file is missing or the value is unrecognized.
    Raises PermissionError if the file exists but we can't read it
    (caller surfaces a clean message)."""
    try:
        with open(MODE_ENV_FILE) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() != ENV_VAR:
                    continue
                v = v.strip()
                if v.lower() in ("no", "false", "0"):
                    return "synced"
                if v.lower() in ("yes", "true", "1"):
                    return "free-running"
                return "synced"
    except FileNotFoundError:
        pass
    return "synced"


def _write_mode(mode: str) -> None:
    """Atomically write the env file. `mode` is 'free-running' or 'synced'."""
    if mode == "free-running":
        value = "yes"
    elif mode == "synced":
        value = "no"
    else:
        raise ValueError(f"unknown mode {mode!r}")
    os.makedirs(os.path.dirname(MODE_ENV_FILE), exist_ok=True)
    tmp = MODE_ENV_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(f"{ENV_VAR}={value}\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, MODE_ENV_FILE)


def _apply_and_restart() -> int:
    """Restart shairport-sync — its ExecStartPre runs
    jasper-apply-airplay-mode against the env file we just wrote, so
    /etc/shairport-sync.conf is re-rendered before shairport starts.
    Returns 0 on success."""
    r = subprocess.run(
        ["systemctl", "restart", "shairport-sync"],
        check=False, timeout=15,
    )
    if r.returncode != 0:
        print(
            "jasper-airplay-mode: systemctl restart shairport-sync failed",
            file=sys.stderr,
        )
        return r.returncode
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-airplay-mode",
        description="Toggle shairport-sync drift-correction (free-running vs synced).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="Print the current mode and exit.")
    p_set = sub.add_parser("set", help="Switch mode and restart shairport-sync.")
    p_set.add_argument("mode", choices=["free-running", "synced"])
    args = parser.parse_args(argv)

    if args.cmd == "show":
        try:
            print(_read_mode())
        except PermissionError:
            print(
                "jasper-airplay-mode: cannot read /var/lib/jasper/airplay_mode.env "
                "without root (the directory is 0750 because it also holds OAuth "
                "tokens). Run with sudo.",
                file=sys.stderr,
            )
            return 1
        return 0

    if args.cmd == "set":
        try:
            if _read_mode() == args.mode:
                print(f"already in {args.mode}; nothing to do")
                return 0
        except PermissionError:
            # Continue to the write; the write attempt will give a
            # clearer error if it's also denied.
            pass
        try:
            _write_mode(args.mode)
        except PermissionError:
            print(
                "jasper-airplay-mode: cannot write /var/lib/jasper/airplay_mode.env. "
                "Run with sudo.",
                file=sys.stderr,
            )
            return 1
        except OSError as e:
            print(f"jasper-airplay-mode: could not write env file: {e}", file=sys.stderr)
            return 1
        return _apply_and_restart()

    return 0  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
