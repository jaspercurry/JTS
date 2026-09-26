"""POSIX serial transport configured for the vendor's 115200/8N1 link."""

# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import array
import errno
import fcntl
import os
import select
import struct
import termios
import time

from .errors import CommunicationTimeout, TurntableError


BAUD_RATE = 115200


class SerialTransport:
    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None
        self._original: list | None = None

    def open(self) -> "SerialTransport":
        if self.fd is not None:
            return self
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise TurntableError(f"Could not open serial port {self.path}: {exc}") from exc
        try:
            if hasattr(termios, "TIOCEXCL"):
                fcntl.ioctl(fd, termios.TIOCEXCL)
            attributes = termios.tcgetattr(fd)
            self._original = list(attributes)
            self._original[6] = list(attributes[6])
            attributes[0] = 0
            attributes[1] = 0
            attributes[2] = termios.CLOCAL | termios.CREAD | termios.CS8
            if hasattr(termios, "CRTSCTS"):
                attributes[2] &= ~termios.CRTSCTS
            attributes[3] = 0
            attributes[4] = termios.B115200
            attributes[5] = termios.B115200
            attributes[6][termios.VMIN] = 0
            attributes[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attributes)
            self._clear_modem_lines(fd)
        except Exception as exc:
            os.close(fd)
            raise TurntableError(f"Could not configure {self.path} for 115200/8N1: {exc}") from exc
        self.fd = fd
        return self

    @staticmethod
    def _clear_modem_lines(fd: int) -> None:
        if not all(hasattr(termios, name) for name in ("TIOCMBIC", "TIOCMGET", "TIOCM_DTR", "TIOCM_RTS")):
            return
        mask = termios.TIOCM_DTR | termios.TIOCM_RTS
        try:
            fcntl.ioctl(fd, termios.TIOCMBIC, struct.pack("I", mask))
            state = array.array("i", [0])
            fcntl.ioctl(fd, termios.TIOCMGET, state, True)
        except OSError as exc:
            if exc.errno in (errno.ENOTTY, errno.EINVAL, errno.ENOSYS):
                return
            raise
        if state[0] & mask:
            raise TurntableError("DTR/RTS remained asserted after serial setup")

    def close(self) -> None:
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            if self._original is not None:
                termios.tcsetattr(fd, termios.TCSANOW, self._original)
        except OSError:
            pass
        finally:
            os.close(fd)

    def __enter__(self) -> "SerialTransport":
        return self.open()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def write(self, payload: bytes, timeout: float = 2.0) -> None:
        if self.fd is None:
            raise TurntableError("Serial port is not open")
        view = memoryview(payload)
        deadline = time.monotonic() + timeout
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CommunicationTimeout("Timed out writing to serial port")
            _, writable, _ = select.select([], [self.fd], [], remaining)
            if not writable:
                continue
            try:
                count = os.write(self.fd, view)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    continue
                raise TurntableError(f"Serial write failed: {exc}") from exc
            view = view[count:]

    def read_chunk(self, timeout: float) -> bytes:
        if self.fd is None:
            raise TurntableError("Serial port is not open")
        readable, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not readable:
            return b""
        try:
            return os.read(self.fd, 4096)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return b""
            raise TurntableError(f"Serial read failed: {exc}") from exc
