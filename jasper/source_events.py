# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Best-effort wake hints for the source arbiter.

These adapters never decide which source should play. They translate native
producer notifications into ``notify(source, via)`` hints; :mod:`jasper.mux`
then re-reads every authoritative source state and runs its one policy path.
Notifications may be duplicated, stale, or lost. The mux coalesces duplicates
and its fixed 1 Hz patrol repairs lost alerts.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import logging
import os
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .log_event import log_event
from .music_sources import Source

logger = logging.getLogger(__name__)

Notify = Callable[[Source, str], None]

_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_NONBLOCK = os.O_NONBLOCK
_IN_CLOEXEC = os.O_CLOEXEC
_INOTIFY_EVENT = struct.Struct("iIII")

_PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
_OBJECT_MANAGER_IFACE = "org.freedesktop.DBus.ObjectManager"
_AIRPLAY_PATH = "/org/mpris/MediaPlayer2"
_AIRPLAY_PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
_BT_INTERFACES = frozenset({"org.bluez.MediaTransport1", "org.bluealsa.PCM1"})
_RETRY_INITIAL_SEC = 1.0
_RETRY_MAX_SEC = 30.0


def classify_source_signal(
    *,
    interface: str | None,
    member: str | None,
    path: str | None,
    body: list[Any],
) -> tuple[Source, ...]:
    """Classify a system-D-Bus signal into source hints, with no I/O.

    The classifier deliberately ignores the value carried by a signal. A value
    is only a reason to wake; the subsequent reconciler probe is authoritative.
    """
    if interface == _PROPERTIES_IFACE and member == "PropertiesChanged":
        if not body or not isinstance(body[0], str):
            return ()
        changed_iface = body[0]
        changed = body[1] if len(body) > 1 and isinstance(body[1], dict) else {}
        invalidated = body[2] if len(body) > 2 and isinstance(body[2], list) else []
        keys = {str(key) for key in changed} | {str(key) for key in invalidated}
        if (
            path == _AIRPLAY_PATH
            and changed_iface == _AIRPLAY_PLAYER_IFACE
            and keys.intersection({"PlaybackStatus", "Metadata"})
        ):
            return (Source.AIRPLAY,)
        if changed_iface in _BT_INTERFACES and (not keys or "State" in keys):
            return (Source.BLUETOOTH,)
        return ()

    if interface == _OBJECT_MANAGER_IFACE and member in {
        "InterfacesAdded",
        "InterfacesRemoved",
    }:
        if len(body) < 2:
            return ()
        interfaces = body[1]
        if isinstance(interfaces, dict):
            names = {str(name) for name in interfaces}
        elif isinstance(interfaces, list):
            names = {str(name) for name in interfaces}
        else:
            return ()
        if names.intersection(_BT_INTERFACES):
            return (Source.BLUETOOTH,)
    return ()


def inotify_changed_names(data: bytes) -> tuple[str, ...]:
    """Decode names from one or more Linux ``inotify_event`` records."""
    names: list[str] = []
    offset = 0
    while offset + _INOTIFY_EVENT.size <= len(data):
        _wd, _mask, _cookie, name_len = _INOTIFY_EVENT.unpack_from(data, offset)
        offset += _INOTIFY_EVENT.size
        end = offset + name_len
        if end > len(data):
            break
        raw = data[offset:end].split(b"\0", 1)[0]
        offset = end
        if raw:
            names.append(raw.decode("utf-8", "replace"))
    return tuple(names)


async def watch_spotify_state(path: str, notify: Notify) -> None:
    """Watch librespot's atomic state-file replacement with Linux inotify."""
    delay = _RETRY_INITIAL_SEC
    warned = False
    while True:
        try:
            await _watch_spotify_state_once(Path(path), notify)
            delay = _RETRY_INITIAL_SEC
            warned = False
        except asyncio.CancelledError:
            raise
        except OSError as exc:
            # A missing RuntimeDirectory is normal while Spotify is disabled;
            # keep retrying quietly so enabling it later self-heals.
            if isinstance(exc, FileNotFoundError):
                level = logging.DEBUG
            else:
                level = logging.WARNING if not warned else logging.DEBUG
                warned = True
            log_event(
                logger,
                "source_event.adapter_unavailable",
                level=level,
                adapter="spotify_inotify",
                retry_sec=delay,
                detail=exc,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, _RETRY_MAX_SEC)


async def _watch_spotify_state_once(path: Path, notify: Notify) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    inotify_init1 = libc.inotify_init1
    inotify_init1.argtypes = [ctypes.c_int]
    inotify_init1.restype = ctypes.c_int
    inotify_add_watch = libc.inotify_add_watch
    inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    inotify_add_watch.restype = ctypes.c_int
    fd = int(inotify_init1(_IN_NONBLOCK | _IN_CLOEXEC))
    if fd < 0:
        errno = ctypes.get_errno()
        raise OSError(errno, os.strerror(errno))
    loop = asyncio.get_running_loop()
    stopped: asyncio.Future[None] = loop.create_future()
    try:
        parent = os.fsencode(path.parent)
        mask = (
            _IN_CLOSE_WRITE
            | _IN_MOVED_TO
            | _IN_CREATE
            | _IN_DELETE_SELF
            | _IN_MOVE_SELF
        )
        wd = int(inotify_add_watch(fd, parent, mask))
        if wd < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, os.strerror(errno), str(path.parent))

        def _ready() -> None:
            try:
                data = os.read(fd, 64 * 1024)
            except BlockingIOError:
                return
            except OSError as exc:
                if not stopped.done():
                    stopped.set_exception(exc)
                return
            if path.name in inotify_changed_names(data):
                notify(Source.SPOTIFY, "spotify_inotify")
            if not path.parent.is_dir() and not stopped.done():
                stopped.set_result(None)

        loop.add_reader(fd, _ready)
        log_event(
            logger,
            "source_event.adapter_ready",
            adapter="spotify_inotify",
        )
        await stopped
    finally:
        loop.remove_reader(fd)
        os.close(fd)


async def watch_dbus_sources(notify: Notify) -> None:
    """Subscribe to AirPlay and Bluetooth state-change signals on system D-Bus."""
    delay = _RETRY_INITIAL_SEC
    warned = False
    while True:
        bus = None
        try:
            from dbus_next import BusType, Message, MessageType  # type: ignore
            from dbus_next.aio import MessageBus  # type: ignore
            from dbus_next.errors import AuthError, DBusError  # type: ignore
        except ImportError as exc:
            level = logging.WARNING if not warned else logging.DEBUG
            log_event(
                logger,
                "source_event.adapter_unavailable",
                level=level,
                adapter="dbus",
                retry_sec=delay,
                detail=exc,
            )
            warned = True
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, _RETRY_MAX_SEC)
            continue

        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            rules = (
                "type='signal',interface='org.freedesktop.DBus.Properties',"
                "member='PropertiesChanged',path='/org/mpris/MediaPlayer2'",
                "type='signal',interface='org.freedesktop.DBus.Properties',"
                "member='PropertiesChanged',arg0='org.bluez.MediaTransport1'",
                "type='signal',interface='org.freedesktop.DBus.Properties',"
                "member='PropertiesChanged',arg0='org.bluealsa.PCM1'",
                "type='signal',interface='org.freedesktop.DBus.ObjectManager'",
            )
            for rule in rules:
                reply = await bus.call(Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface="org.freedesktop.DBus",
                    member="AddMatch",
                    signature="s",
                    body=[rule],
                ))
                if reply.message_type == MessageType.ERROR:
                    raise RuntimeError(reply.error_name or "D-Bus AddMatch failed")

            def _message(message: Any) -> None:
                if message.message_type != MessageType.SIGNAL:
                    return
                for source in classify_source_signal(
                    interface=message.interface,
                    member=message.member,
                    path=message.path,
                    body=list(message.body or ()),
                ):
                    notify(source, "dbus")

            bus.add_message_handler(_message)
            log_event(logger, "source_event.adapter_ready", adapter="dbus")
            delay = _RETRY_INITIAL_SEC
            warned = False
            await bus.wait_for_disconnect()
            raise ConnectionError("system D-Bus disconnected")
        except asyncio.CancelledError:
            raise
        except (AuthError, DBusError, OSError, RuntimeError) as exc:
            level = logging.WARNING if not warned else logging.DEBUG
            log_event(
                logger,
                "source_event.adapter_unavailable",
                level=level,
                adapter="dbus",
                retry_sec=delay,
                detail=exc,
            )
            warned = True
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, _RETRY_MAX_SEC)
        finally:
            if bus is not None:
                with contextlib.suppress(Exception):
                    bus.disconnect()


def start_source_event_tasks(
    notify: Notify,
    *,
    spotify_state_path: str,
) -> list[asyncio.Task[None]]:
    """Start optional wake adapters; callers own cancellation and awaiting.

    Expected resource failures retry inside each adapter. An unexpected task
    exit cannot break mux policy (the patrol remains authoritative), but it is
    surfaced as one stable ERROR event instead of becoming an unobserved task
    exception.
    """
    tasks = [
        asyncio.create_task(
            watch_spotify_state(spotify_state_path, notify),
            name="mux-spotify-events",
        ),
        asyncio.create_task(
            watch_dbus_sources(notify),
            name="mux-dbus-events",
        ),
    ]
    for task in tasks:
        task.add_done_callback(_report_adapter_task_exit)
    return tasks


def _report_adapter_task_exit(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exception = task.exception()
    if exception is None:
        log_event(
            logger,
            "source_event.adapter_stopped",
            level=logging.ERROR,
            adapter=task.get_name(),
            detail="unexpected clean exit",
        )
        return
    log_event(
        logger,
        "source_event.adapter_stopped",
        level=logging.ERROR,
        adapter=task.get_name(),
        detail=exception,
    )
