# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""/speaker/ — user-facing renderer display name.

Single setting: the name shown in AirPlay, Spotify Connect, Bluetooth,
and USB Audio pickers. This is deliberately separate from
``JASPER_HOSTNAME``; renaming the speaker does not change the address
(``JASPER_HOSTNAME``, e.g. ``jts.local``) used to reach it. The hint
shows this speaker's actual configured hostname, not a hardcoded one.

URL surface (after nginx strips /speaker/):
  GET  /         page render
  POST /save     validate, duplicate-check, write state, restart services
"""

from __future__ import annotations

import asyncio
import functools
import html
import logging
import re
import urllib.parse
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..speaker_name import (
    DEFAULT_SPEAKER_NAME,
    MAX_SPEAKER_NAME_CHARS,
    SpeakerNameError,
    read_state,
    validate_name,
    validate_room,
    write_state,
)
from ..atomic_io import atomic_write_text
from ..identity import resolve_hostname
from ..control.restart_broker import manage_units
from ..log_event import log_event
from ..speaker_name_discovery import NameConflict, find_name_conflicts
from ..source_intent import kick_source_reconcile
from ._common import (
    begin_request,
    canonical_banner,
    canonical_header,
    canonical_page,
    csrf_field_html,
    read_form,
    reject_csrf,
    send_html_response,
    send_rejected_form,
    send_see_other,
    guard_read_request,
    guard_mutating_request,
)
from ._service_state import unit_active as _unit_active

logger = logging.getLogger(__name__)

SPEAKER_NAME_FILE = "/var/lib/jasper/speaker_name.env"
BLUEZ_MAIN_CONF = "/etc/bluetooth/main.conf"

RESTART_UNITS = [
    "jasper-voice.service",
    "jasper-control.service",
    "jasper-mux.service",
]

# Renderer/advertising units are optional household sources.  A plain
# `restart` starts an inactive unit even when it is disabled, so rename must
# use systemd's active-only `try-restart` for this set.
SOURCE_TRY_RESTART_UNITS = [
    "librespot.service",
    "shairport-sync.service",
    "bluealsa.service",
    "bluealsa-aplay.service",
    "bt-agent.service",
]

# 33.64 s is the worst FULL RESTART (Stopping -> Finished) measured for
# jasper-usbgadget.service alone -- the same derivation
# deploy/usbsink/jasper-usbgadget-snapshot's REPAIR_RESTART_TIMEOUT_SEC and
# deploy/systemd/jasper-usbmic-apply.service's TimeoutStartSec document
# (also see tests/test_source_intent_systemd.py). Every blocking restart in
# this module needs the same "did it actually finish" answer, not merely
# "did systemd accept the job", so they all share this one bound rather than
# repeating the derivation at five call sites.
_BLOCKING_RESTART_TIMEOUT_SEC = 60.0


def _restart_units(
    units: list[str],
    *,
    verb: str = "restart",
    no_block: bool = True,
    timeout: float = 5.0,
) -> bool:
    # WS1 Phase 3: route through jasper-control's restart broker (the
    # read-only `_systemctl` probes elsewhere in this file stay direct).
    resp = manage_units(
        *units,
        verb=verb,
        reason="speaker rename",
        no_block=no_block,
        timeout=timeout,
    )
    ok = bool(resp.get("ok"))
    if not ok:
        log_event(
            logger,
            "speaker_name.restart_failed",
            verb=verb,
            units=",".join(units),
            detail=str(resp.get("error") or f"rc={resp.get('rc')}"),
            level=logging.WARNING,
        )
    return ok


def _write_bluez_main_conf_name(name: str, path: str = BLUEZ_MAIN_CONF) -> None:
    conf = Path(path)
    try:
        original = conf.read_text(encoding="utf-8")
    except FileNotFoundError:
        log_event(
            logger, "speaker_name.bluez_conf_missing", path=path, level=logging.WARNING
        )
        return
    except (OSError, UnicodeError) as e:
        log_event(
            logger,
            "speaker_name.bluez_conf",
            path=path,
            result="failed",
            operation="read",
            error=e,
            level=logging.WARNING,
        )
        return

    replacement = f"Name = {name}"
    updated, count = re.subn(
        r"(?m)^#?\s*Name\s*=.*$",
        replacement,
        original,
        count=1,
    )
    if count == 0:
        updated = original.rstrip() + "\n" + replacement + "\n"
    if updated == original:
        return

    try:
        atomic_write_text(
            conf,
            updated,
            mode=0o644,
        )
    except (OSError, UnicodeError) as e:
        log_event(
            logger,
            "speaker_name.bluez_conf",
            path=path,
            result="failed",
            operation="write",
            error=e,
            level=logging.WARNING,
        )
        return
    log_event(logger, "speaker_name.bluez_conf", path=path, result="ok")


def _format_conflicts(conflicts: list[NameConflict]) -> str:
    if not conflicts:
        return ""
    first = conflicts[0]
    if len(conflicts) == 1:
        return (
            f'"{first.name}" is already in use on {first.protocol}. '
            "Choose a different speaker name."
        )
    protocols = ", ".join(sorted({c.protocol for c in conflicts}))
    return (
        f"That name is already in use ({protocols}). Choose a different speaker name."
    )


def _find_conflicts(name: str) -> list[NameConflict]:
    try:
        return asyncio.run(find_name_conflicts(name))
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "speaker_name.duplicate_check_failed",
            error=e,
            level=logging.WARNING,
        )
        return []


def _refresh_gadget_consumers_after_rebuild(name_changed: bool) -> bool:
    """Rebuild the USB gadget for a name change, then refresh the consumers a
    real rebuild leaves stale.

    The composite USB gadget owns the host-visible device strings (product =
    speaker name; the name-patch reruns as its ExecStartPre). Only a NAME
    change touches those strings -- room is display-only and never reaches
    the name-patch (deploy/usbsink/jasper-usbgadget-compose.sh's identity
    reader resolves ``JASPER_SPEAKER_NAME`` alone), so a room-only edit must
    never pay for a gadget rebuild plus a fan-in/usbmic bounce. The audio
    label re-applies on a bound gadget's restart
    only once the module index already names the override, which is the
    steady state on any box that has composed USB audio before. On a box
    where it does not yet (first enable, first boot after a kernel update)
    the 10.3 s depmod runs out of band in jasper-usbsink-name-index.service
    and the audio label lands on the following restart; jasper-doctor's
    `usbsink name` check warns for that window rather than reporting a name
    the host is not showing (#2176).

    This restart is descriptor-string-only, never a composition change, so
    jasper-usbgadget-converge would read it as already converged and skip
    its own rebuild -- the token it reconciles on doesn't cover product/
    audio strings. That makes this the only path that rebuilds the gadget
    for a rename, so it also owes the converger's post-rebuild refresh: a
    real rebuild destroys and recreates the UAC2Gadget ALSA card, and
    jasper-fanin/jasper-usbmic hold the stale handle until bounced.

    Active state is read fresh here, not from an earlier snapshot: by the
    time this runs, _apply_name has already spent up to ~120 s in the
    blocking bluetooth/source restarts above it, and the gadget's state at
    rename-start is not pinned to its state now.

    Returns False only when a rebuild was attempted and failed; True when
    nothing needed doing (room-only edit, or gadget not active) or the
    rebuild succeeded, so the caller can fold this straight into its own
    success return.
    """
    if not name_changed or not _unit_active("jasper-usbgadget.service"):
        return True
    # Blocking, like the bluetooth restart in _apply_name: the caller must
    # know the rebuild actually finished -- not merely that systemd accepted
    # the job -- before it is safe to touch the consumers below.
    gadget_restarted = _restart_units(
        ["jasper-usbgadget.service"],
        no_block=False,
        timeout=_BLOCKING_RESTART_TIMEOUT_SEC,
    )
    if not gadget_restarted:
        # A failed rebuild never gets a refresh -- there is no new card for
        # fan-in/usbmic to pick up; _restart_units already logged the
        # failure above.
        return False
    # Same order as jasper-usbgadget-converge's post-rebuild refresh
    # (fan-in before usbmic). Each call WARNs on its own failure via
    # _restart_units and never blocks the other unit's try-restart; that
    # outcome is best-effort, like the bluetooth/source restarts in
    # _apply_name, and does not change this function's own return.
    _restart_units(
        ["jasper-fanin.service"],
        verb="try-restart",
        no_block=False,
        timeout=_BLOCKING_RESTART_TIMEOUT_SEC,
    )
    _restart_units(
        ["jasper-usbmic.service"],
        verb="try-restart",
        no_block=False,
        timeout=_BLOCKING_RESTART_TIMEOUT_SEC,
    )
    return True


def _apply_name(name: str, *, name_changed: bool) -> bool:
    units = list(RESTART_UNITS)

    _write_bluez_main_conf_name(name)
    bluetooth_alias_applied = False
    try:
        from ..bluetooth.adapter import set_alias as set_bluetooth_alias

        asyncio.run(set_bluetooth_alias(name))
        bluetooth_alias_applied = True
        log_event(logger, "speaker_name.bluetooth_alias", name=repr(name), result="ok")
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "speaker_name.bluetooth_alias",
            name=repr(name),
            result="failed",
            error=e,
            level=logging.WARNING,
        )

    try:
        from ..control_advert import render_control_advert

        ok = render_control_advert(name)
        log_event(
            logger,
            "speaker_name.avahi",
            name=repr(name),
            result="ok" if ok else "soft_fail",
        )
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "speaker_name.avahi",
            name=repr(name),
            result="failed",
            error=e,
            level=logging.WARNING,
        )

    log_event(
        logger,
        "speaker_name.restart",
        units=",".join(units),
        name_changed=name_changed,
        try_restart_units=",".join(SOURCE_TRY_RESTART_UNITS),
    )
    # A successful D-Bus alias update needs no bluetoothd restart. If it failed,
    # reload the persisted main.conf first and WAIT: a later source pass must be
    # the final Bluetooth lifecycle mutation so Requires= cannot strand the
    # agent/dependents after they were restored.
    if not bluetooth_alias_applied:
        _restart_units(
            ["bluetooth.service"],
            no_block=False,
            timeout=_BLOCKING_RESTART_TIMEOUT_SEC,
        )

    # Refresh active source advertisements with a blocking active-only
    # try-restart. Then synchronously re-assert canonical desired/effective
    # source state; inactive/Off sources stay Off and a restarted Bluetooth
    # control plane cannot leave desired-On dependents down.
    _restart_units(
        SOURCE_TRY_RESTART_UNITS,
        verb="try-restart",
        no_block=False,
        timeout=_BLOCKING_RESTART_TIMEOUT_SEC,
    )
    # A start can join a source oneshot that was already activating before the
    # rename refresh. The first bounded call drains that snapshot; the second
    # guarantees a pass began after the Bluetooth/control-plane mutations above.
    source_result: Mapping[str, Any] = {"ok": False, "error": "not run"}
    for _ in range(2):
        source_result = kick_source_reconcile(reason="speaker rename")
    if not source_result.get("ok"):
        log_event(
            logger,
            "speaker_name.source_reconcile_failed",
            detail=str(source_result.get("error") or source_result),
            level=logging.WARNING,
        )

    gadget_ok = _refresh_gadget_consumers_after_rebuild(name_changed)

    # Core services restart separately because jasper-control's broker has
    # special self-restart handling for the ordinary non-blocking restart verb.
    _restart_units(units)
    return bool(source_result.get("ok")) and gadget_ok


def _index_html(
    *,
    current_name: str,
    current_room: str,
    hostname: str,
    csrf_token: str,
    status_msg: str = "",
) -> bytes:
    value = html.escape(current_name, quote=True)
    room_value = html.escape(current_room, quote=True)
    default_attr = html.escape(DEFAULT_SPEAKER_NAME, quote=True)
    default_text = html.escape(DEFAULT_SPEAKER_NAME)
    host_text = html.escape(hostname)
    body = f"""
{canonical_header("Speaker name")}
<main class="page">
  {canonical_banner(status_msg)}
  <p class="form-hint">Change the name shown in AirPlay, Spotify Connect,
  Bluetooth, and USB Audio. The address stays <code>{host_text}</code>.</p>

  <form method="post" action="./save" id="speaker-name-form"
        data-default="{default_attr}">
    {csrf_field_html(csrf_token)}
    <div class="field">
      <label for="speaker-name">Speaker name</label>
      <input id="speaker-name" type="text" name="name" value="{value}"
             maxlength="{MAX_SPEAKER_NAME_CHARS}"
             autocomplete="off" autocapitalize="words" spellcheck="false">
      <p class="form-hint">Default: {default_text}. Use {MAX_SPEAKER_NAME_CHARS}
      characters or fewer.</p>
    </div>
    <div class="field">
      <label for="speaker-room">Room (optional)</label>
      <input id="speaker-room" type="text" name="room" value="{room_value}"
             maxlength="{MAX_SPEAKER_NAME_CHARS}"
             autocomplete="off" autocapitalize="words" spellcheck="false">
      <p class="form-hint">Which room this speaker is in, e.g. Kitchen.
      Leave blank to clear.</p>
    </div>
    <div class="form-actions">
      <button type="submit" class="btn btn--primary">Save and restart</button>
    </div>
  </form>
</main>
<script type="module" src="/assets/speaker/js/main.js"></script>
"""
    return canonical_page("Speaker name", body, csrf_token=csrf_token)


def _make_handler(cfg: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path == "/":
                if not guard_read_request(self):
                    return
                ctx = begin_request(self)
                state = read_state(cfg["state_path"])
                send_html_response(
                    self,
                    _index_html(
                        current_name=state.name,
                        current_room=state.room,
                        hostname=resolve_hostname(),
                        csrf_token=ctx["csrf_token"],
                        status_msg=ctx["flash"],
                    ),
                )
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            url = urllib.parse.urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            if path != "/save":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            form = read_form(self)
            if not guard_mutating_request(self, form):
                reject_csrf(self)
                return

            name = form.get("name", "")
            room = form.get("room", "")
            page = functools.partial(
                _index_html,
                current_name=name,
                current_room=room,
                hostname=resolve_hostname(),
            )

            try:
                requested = validate_name(name)
                # Room is optional; "" is a valid "unset" answer, not an error.
                requested_room = validate_room(room)
            except SpeakerNameError as e:
                send_rejected_form(self, page, flash=str(e))
                return

            state = read_state(cfg["state_path"])
            current = state.name
            if requested == current and requested_room == state.room:
                send_see_other(self, "./", flash="Name unchanged.")
                return

            # Conflict-check only the renderer-visible name. The room label
            # is local-only (no AirPlay/Bluetooth collision), so a room-only
            # edit skips the network probe.
            if requested != current:
                conflicts = _find_conflicts(requested)
                if conflicts:
                    log_event(
                        logger,
                        "speaker_name.conflict",
                        requested=repr(requested),
                        conflicts=",".join(
                            f"{c.protocol}:{c.detail}" for c in conflicts
                        ),
                    )
                    send_rejected_form(
                        self, page, flash=_format_conflicts(conflicts),
                    )
                    return

            try:
                saved = write_state(
                    requested,
                    requested_room,
                    path=cfg["state_path"],
                    mode=0o644,
                )
            except (OSError, SpeakerNameError) as e:
                logger.exception("speaker name save failed")
                send_rejected_form(self, page, flash=f"Could not save: {e}")
                return

            log_event(
                logger,
                "speaker_name.save",
                previous=repr(current),
                requested=repr(requested),
                saved=repr(saved),
                room=repr(requested_room),
            )
            sources_ok = _apply_name(saved, name_changed=(saved != current))
            if sources_ok:
                flash = (
                    f'Saved. Speaker renamed to "{saved}". Services restarting.'
                )
            else:
                flash = (
                    f'Saved the name "{saved}", but some audio sources could '
                    "not restart. Try again or check System status."
                )
            send_see_other(self, "./", flash=flash)

    return Handler


def make_server(target, *, state_path: str = SPEAKER_NAME_FILE) -> ThreadingHTTPServer:
    from . import _systemd

    cfg = {"state_path": state_path}
    return _systemd.make_http_server(target, _make_handler(cfg))
