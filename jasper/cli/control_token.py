"""jasper-control-token — manage jasper-control's mutation token.

JTS is a trusted-LAN appliance: ``jasper-control`` (``0.0.0.0:8780``) has
no auth, so any LAN device can ``curl`` the high-impact mutations
(``/system/poweroff``, ``/system/reboot``, ``/mic/mute``,
``/grouping/set``). ``jasper-control`` auto-generates this token at startup so
those routes are gated with no operator action; this CLI lets an operator
inspect, rotate, or temporarily remove that ``X-JTS-Token``. See SECURITY.md for
the threat model and the gated routes.

Usage::

    jasper-control-token --show      # print the current token (or "disabled")
    jasper-control-token --enable    # generate + write a token (refuses to clobber)
    jasper-control-token --enable --force   # overwrite an existing token
    jasper-control-token --disable   # remove it until jasper-control starts again

The token lives at :data:`jasper.control.control_token.TOKEN_FILE`
(default ``/var/lib/jasper/control_token``, overridable via
``JASPER_CONTROL_TOKEN_FILE``), mode ``0640`` group ``jasper`` so the non-root
``jasper-control`` and ``jasper-web`` daemons can read it. ``jasper-control``
reads the file fresh on every request, so a rotate/remove takes effect without a
daemon restart; startup will recreate the token if it is absent.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys

from ..control import control_token


def _write_token(token: str) -> None:
    """Atomically write the token file at mode 0640.

    tmp + chmod + os.replace so a reader never sees a half-written file
    and the secret is never briefly world-readable. The directory is
    created if missing (matches the wizard-file pattern in
    jasper/cli/airplay_mode.py)."""
    path = control_token.TOKEN_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    # Open with 0600 from the start so the secret is never momentarily
    # readable by other users between create and chmod.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token + "\n")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # WS1 Phase 3b-2: publish 0640 group jasper (chmod-before-rename) so the
    # non-root jasper-control/jasper-web can read the gate token — the file is
    # created 0600 above so it's never group/world-readable mid-write. Matches
    # control_token.ensure_token's mode; see docs/HANDOFF-privilege-separation.md.
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def _enable(force: bool) -> int:
    path = control_token.TOKEN_FILE
    if control_token.token_enforced() and not force:
        print(
            "jasper-control-token: a token already exists at "
            f"{path}; pass --force to overwrite it (this invalidates the "
            "old token for every client that has it).",
            file=sys.stderr,
        )
        return 1
    token = secrets.token_urlsafe(32)
    try:
        _write_token(token)
    except PermissionError:
        print(
            f"jasper-control-token: cannot write {path} — run with sudo.",
            file=sys.stderr,
        )
        return 1
    except OSError as e:
        print(f"jasper-control-token: could not write token: {e}", file=sys.stderr)
        return 1
    print(token)
    print(
        "control token gate ENABLED. Provision this to the /system "
        "dashboard (you will be prompted once) or send it as the "
        "X-JTS-Token header from curl/scripts.",
        file=sys.stderr,
    )
    return 0


def _show() -> int:
    # _stored_token() resolves "" for absent/empty/unreadable. An
    # unreadable file (exists but no permission) is indistinguishable
    # from absent here; tell the operator to use sudo for the secret.
    path = control_token.TOKEN_FILE
    if not control_token.token_enforced():
        if os.path.exists(path) and not os.access(path, os.R_OK):
            print(
                f"jasper-control-token: {path} exists but is not readable "
                "— run with sudo to print the token.",
                file=sys.stderr,
            )
            return 1
        print("disabled (no token file; mutations are open on the trusted LAN)")
        return 0
    print(control_token._stored_token())
    return 0


def _disable() -> int:
    path = control_token.TOKEN_FILE
    try:
        os.unlink(path)
    except FileNotFoundError:
        print("already disabled (no token file)")
        return 0
    except PermissionError:
        print(
            f"jasper-control-token: cannot remove {path} — run with sudo.",
            file=sys.stderr,
        )
        return 1
    except OSError as e:
        print(f"jasper-control-token: could not remove token: {e}", file=sys.stderr)
        return 1
    print("control token gate DISABLED — mutations are open on the trusted LAN.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jasper-control-token",
        description=(
            "Manage the jasper-control mutation token "
            "(gates /system/poweroff, /system/reboot, /mic/mute, "
            "/grouping/set). See SECURITY.md."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--enable", action="store_true",
        help="Generate and write a token (refuses to overwrite without --force).",
    )
    group.add_argument(
        "--show", action="store_true",
        help="Print the current token, or 'disabled' when the gate is off.",
    )
    group.add_argument(
        "--disable", action="store_true",
        help="Remove the token file until jasper-control starts and recreates it.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="With --enable, overwrite an existing token.",
    )
    args = parser.parse_args(argv)

    if args.enable:
        return _enable(args.force)
    if args.show:
        return _show()
    if args.disable:
        return _disable()
    return 0  # unreachable — the group is required


if __name__ == "__main__":
    raise SystemExit(main())
