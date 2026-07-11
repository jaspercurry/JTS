# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""JTS-owned USB control helper for the XMOS XVF3800.

This module implements the small XVF3800 control surface JTS uses for
boot-time audio profile setup, chip-AEC experiments, and diagnostics.
It is not copied from the ReSpeaker Python helper. The command numbers,
payload widths, and data types below are hardware protocol facts from the
XMOS XVF3800 control-command documentation plus JTS hardware validation.
"""
from __future__ import annotations

import argparse
import struct
import sys
import time
from dataclasses import dataclass
from typing import Literal

_USB_IMPORT_ERROR: ModuleNotFoundError | None = None

try:  # Optional at import time so ``--list`` works in dev envs.
    import libusb_package
    import usb.core
    import usb.util
except ModuleNotFoundError as exc:
    libusb_package = None
    usb = None  # type: ignore[assignment]
    _USB_IMPORT_ERROR = exc


Access = Literal["ro", "rw", "wo"]
ValueKind = Literal["char", "float", "int32", "radians", "uint8", "uint16", "uint32"]

CONTROL_SUCCESS = 0
CONTROL_RETRY = 64
# Per-transfer USB control timeout. These are SMALL vendor control
# reads/writes (VERSION, AEC_HPFONOFF, beam config, …) that complete in
# milliseconds; DFU firmware writes go through external `dfu-util`, never
# this path. The chip's "I'm still processing" is signalled at the
# application layer via CONTROL_RETRY + the MAX_READ_ATTEMPTS loop below,
# NOT by a long transport timeout. Keep this well under systemd's default
# unit-start timeout (~90 s / DefaultTimeoutStartSec): jasper-aec-init is a
# boot-blocking Type=oneshot that gates the bridge + camilla, so a single
# wedged transfer at the old 100 s default could outlast unit start and
# stall boot audio. 5 s is >1000× a normal transfer yet ~18× under 90 s.
DEFAULT_TIMEOUT_MS = 5_000
MAX_READ_ATTEMPTS = 100
DEFAULT_USB_VID = 0x2886
LEGACY_USB_PID = 0x001A
FLEX_USB_PID = 0x0022
SUPPORTED_USB_PIDS = (LEGACY_USB_PID, FLEX_USB_PID)

_CTRL_IN_VENDOR_DEVICE = 0x80 | 0x40
_CTRL_OUT_VENDOR_DEVICE = 0x00 | 0x40


@dataclass(frozen=True)
class Command:
    resid: int
    cmdid: int
    count: int
    access: Access
    kind: ValueKind


# Narrow JTS command set. Do not grow this into a mirror of upstream demo tools;
# add entries only when JTS code, diagnostics, or docs have a concrete need.
COMMANDS: dict[str, Command] = {
    # Application/version metadata.
    "VERSION": Command(48, 0, 3, "ro", "uint8"),
    "BLD_MSG": Command(48, 1, 50, "ro", "char"),
    "BLD_HOST": Command(48, 2, 30, "ro", "char"),
    "BLD_REPO_HASH": Command(48, 3, 40, "ro", "char"),
    "BLD_MODIFIED": Command(48, 4, 6, "ro", "char"),
    "BOOT_STATUS": Command(48, 5, 3, "ro", "char"),
    "REBOOT": Command(48, 7, 1, "wo", "uint8"),
    "USB_BIT_DEPTH": Command(48, 8, 2, "rw", "uint8"),
    "CLEAR_CONFIGURATION": Command(48, 10, 1, "wo", "uint8"),
    # AEC / SHF controls used by jasper-aec-init and validation.
    "AEC_AECCONVERGED": Command(33, 3, 1, "ro", "int32"),
    "AEC_AECEMPHASISONOFF": Command(33, 4, 1, "rw", "int32"),
    "AEC_FAR_EXTGAIN": Command(33, 5, 1, "rw", "float"),
    "AEC_HPFONOFF": Command(33, 1, 1, "rw", "int32"),
    "AEC_NUM_MICS": Command(33, 71, 1, "ro", "int32"),
    "AEC_NUM_FARENDS": Command(33, 72, 1, "ro", "int32"),
    "AEC_MIC_ARRAY_TYPE": Command(33, 73, 1, "ro", "int32"),
    "SHF_BYPASS": Command(33, 70, 1, "rw", "uint8"),
    "AEC_ASROUTONOFF": Command(33, 35, 1, "rw", "int32"),
    "AEC_ASROUTGAIN": Command(33, 36, 1, "rw", "float"),
    "AEC_FIXEDBEAMSONOFF": Command(33, 37, 1, "rw", "int32"),
    "AEC_FIXEDBEAMSAZIMUTH_VALUES": Command(33, 81, 2, "rw", "radians"),
    "AEC_FIXEDBEAMSELEVATION_VALUES": Command(33, 82, 2, "rw", "radians"),
    "AEC_FIXEDBEAMSGATING": Command(33, 83, 1, "rw", "uint8"),
    # Audio manager routing and gain controls.
    "AUDIO_MGR_MIC_GAIN": Command(35, 0, 1, "rw", "float"),
    "AUDIO_MGR_REF_GAIN": Command(35, 1, 1, "rw", "float"),
    "AUDIO_MGR_SELECTED_CHANNELS": Command(35, 12, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_PACKED": Command(35, 13, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_UPSAMPLE": Command(35, 14, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_L": Command(35, 15, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_R": Command(35, 19, 2, "rw", "uint8"),
    "AUDIO_MGR_OP_ALL": Command(35, 23, 12, "rw", "uint8"),
    "I2S_INACTIVE": Command(35, 24, 1, "ro", "uint8"),
    "AUDIO_MGR_FAR_END_DSP_ENABLE": Command(35, 25, 1, "rw", "uint8"),
    "AUDIO_MGR_SYS_DELAY": Command(35, 26, 1, "rw", "int32"),
    "I2S_DAC_DSP_ENABLE": Command(35, 27, 1, "rw", "uint8"),
    # GPIO / LED reads used by xvf-interrogate diagnostics.
    "GPO_READ_VALUES": Command(20, 0, 5, "ro", "uint8"),
    "LED_EFFECT": Command(20, 12, 1, "rw", "uint8"),
    "LED_BRIGHTNESS": Command(20, 13, 1, "rw", "uint8"),
}

# Compatibility for callers that imported the previous module-level table.
PARAMETERS = COMMANDS


class XvfControlError(RuntimeError):
    """Raised when the XVF3800 rejects or cannot complete a control request."""


def _raise_usb_dependency_error() -> None:
    detail = f": {_USB_IMPORT_ERROR}" if _USB_IMPORT_ERROR is not None else ""
    raise XvfControlError(
        "XVF3800 USB control dependencies missing; install pyusb and "
        f"libusb_package before using hardware commands{detail}"
    ) from _USB_IMPORT_ERROR


def _coerce_integral(
    value: int | float | str,
    *,
    kind: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise ValueError(
                f"{kind} value must be an integer in range "
                f"{minimum}..{maximum}, got {value!r}"
            )
        coerced = int(value)
    elif isinstance(value, str):
        try:
            coerced = int(value, 0)
        except ValueError as exc:
            raise ValueError(
                f"{kind} value must be an integer in range "
                f"{minimum}..{maximum}, got {value!r}"
            ) from exc
    else:
        raise TypeError(f"{kind} value must be int-compatible, got {type(value)!r}")
    if not minimum <= coerced <= maximum:
        raise ValueError(
            f"{kind} value must be an integer in range "
            f"{minimum}..{maximum}, got {value!r}"
        )
    return coerced


def _payload_size(command: Command) -> int:
    if command.kind in {"char", "uint8"}:
        return command.count
    if command.kind == "uint16":
        return command.count * 2
    return command.count * 4


def _command(name: str) -> Command:
    key = name.upper()
    try:
        return COMMANDS[key]
    except KeyError as exc:
        raise ValueError(f"unsupported XVF3800 command: {name}") from exc


def _response_bytes(response: object) -> bytes:
    if hasattr(response, "tobytes"):
        return response.tobytes()
    return bytes(response)  # type: ignore[arg-type]


def _pack_values(command: Command, values: list[int | float | str]) -> bytes:
    if len(values) != command.count:
        raise ValueError(
            f"value count for command is {command.count}, got {len(values)}"
        )
    if command.kind == "char":
        joined = "".join(str(v) for v in values)
        return joined.encode("utf-8")[: command.count].ljust(command.count, b"\0")
    if command.kind == "uint8":
        return bytes(
            _coerce_integral(v, kind="uint8", minimum=0, maximum=0xFF)
            for v in values
        )
    if command.kind == "uint16":
        return struct.pack(
            "<" + ("H" * command.count),
            *(
                _coerce_integral(v, kind="uint16", minimum=0, maximum=0xFFFF)
                for v in values
            ),
        )
    if command.kind == "uint32":
        return struct.pack(
            "<" + ("I" * command.count),
            *(
                _coerce_integral(v, kind="uint32", minimum=0, maximum=0xFFFF_FFFF)
                for v in values
            ),
        )
    if command.kind == "int32":
        return struct.pack(
            "<" + ("i" * command.count),
            *(
                _coerce_integral(
                    v,
                    kind="int32",
                    minimum=-(2**31),
                    maximum=(2**31) - 1,
                )
                for v in values
            ),
        )
    if command.kind in {"float", "radians"}:
        return struct.pack("<" + ("f" * command.count), *(float(v) for v in values))
    raise AssertionError(f"unhandled XVF value kind: {command.kind}")


def _unpack_values(command: Command, payload: bytes) -> tuple[int | float | str, ...]:
    body = payload[1:]
    if command.kind == "char":
        return (body.rstrip(b"\0").decode("utf-8", errors="replace"),)
    if command.kind == "uint8":
        return tuple(struct.unpack("<" + ("B" * command.count), body))
    if command.kind == "uint16":
        return tuple(struct.unpack("<" + ("H" * command.count), body))
    if command.kind == "uint32":
        return tuple(struct.unpack("<" + ("I" * command.count), body))
    if command.kind == "int32":
        return tuple(struct.unpack("<" + ("i" * command.count), body))
    if command.kind in {"float", "radians"}:
        return tuple(struct.unpack("<" + ("f" * command.count), body))
    raise AssertionError(f"unhandled XVF value kind: {command.kind}")


class ReSpeaker:
    """XVF3800 device wrapper exposing read/write by command name."""

    TIMEOUT = DEFAULT_TIMEOUT_MS

    def __init__(self, dev) -> None:
        self.dev = dev

    def write(self, name: str, data_list: list[int | float | str]) -> None:
        command = _command(name)
        if command.access == "ro":
            raise ValueError(f"{name.upper()} is read-only")
        payload = _pack_values(command, data_list)
        self.dev.ctrl_transfer(
            _CTRL_OUT_VENDOR_DEVICE,
            0,
            command.cmdid,
            command.resid,
            payload,
            self.TIMEOUT,
        )

    def read(self, name: str) -> tuple[int | float | str, ...]:
        command = _command(name)
        if command.access == "wo":
            raise ValueError(f"{name.upper()} is write-only")

        response_length = _payload_size(command) + 1
        for attempt in range(MAX_READ_ATTEMPTS):
            response = self.dev.ctrl_transfer(
                _CTRL_IN_VENDOR_DEVICE,
                0,
                0x80 | command.cmdid,
                command.resid,
                response_length,
                self.TIMEOUT,
            )
            payload = _response_bytes(response)
            if not payload:
                raise XvfControlError(f"{name.upper()} returned an empty response")
            status = payload[0]
            if status == CONTROL_SUCCESS:
                return _unpack_values(command, payload)
            if status != CONTROL_RETRY:
                raise XvfControlError(
                    f"{name.upper()} returned status code {status}"
                )
            if attempt + 1 < MAX_READ_ATTEMPTS:
                time.sleep(0.01)
        raise XvfControlError(
            f"{name.upper()} did not complete after {MAX_READ_ATTEMPTS} attempts"
        )

    def close(self) -> None:
        if usb is not None:
            usb.util.dispose_resources(self.dev)


def _find_usb_device(vid: int, pid: int):
    if sys.platform.startswith("win"):
        if libusb_package is None:
            _raise_usb_dependency_error()
        return libusb_package.find(idVendor=vid, idProduct=pid)
    if usb is None:
        _raise_usb_dependency_error()
    return usb.core.find(idVendor=vid, idProduct=pid)


def find(vid: int = DEFAULT_USB_VID, pid: int | None = None) -> ReSpeaker | None:
    """Find a supported XVF3800 runtime USB device.

    ``pid=None`` means auto-discover any JTS-supported Seeed XVF USB
    product: the legacy square USB array (2886:001a) or the ReSpeaker
    Flex linear/circular firmware family (2886:0022). Passing ``pid``
    keeps the old exact-match behavior for diagnostics.
    """
    pids = (pid,) if pid is not None else SUPPORTED_USB_PIDS
    for candidate_pid in pids:
        dev = _find_usb_device(vid, candidate_pid)
        if dev:
            return ReSpeaker(dev)
    return None


def parse_value(value_str: str) -> int | float:
    if value_str.startswith(("0x", "0X")):
        return int(value_str, 16)
    if value_str.startswith("$"):
        return int(value_str[1:], 16)
    try:
        return int(value_str)
    except ValueError:
        return float(value_str)


def case_insensitive_command(value: str) -> str:
    upper = value.upper()
    if upper not in COMMANDS:
        matches = [name for name in COMMANDS if upper in name]
        hint = f" Did you mean one of: {', '.join(matches)}?" if matches else ""
        raise argparse.ArgumentTypeError(f"Invalid command {value!r}.{hint}")
    return upper


def list_commands() -> None:
    print("\nSupported XVF3800 commands:\n")
    print(f"{'Command':<34} {'RESID':<6} {'CMDID':<6} {'Count':<7} {'Type':<9} {'Access'}")
    print("-" * 76)
    for name in sorted(COMMANDS):
        command = COMMANDS[name]
        print(
            f"{name:<34} {command.resid:<6} {command.cmdid:<6} "
            f"{command.count:<7} {command.kind:<9} {command.access}"
        )


def _format_values(values: tuple[int | float | str, ...]) -> str:
    formatted: list[str] = []
    for value in values:
        if isinstance(value, float):
            formatted.append(f"{value:.3f}")
        elif isinstance(value, str):
            formatted.append(repr(value))
        else:
            formatted.append(str(value))
    return f"[{', '.join(formatted)}]"


def main() -> int:
    parser = argparse.ArgumentParser(description="JTS XVF3800 control helper")
    parser.add_argument("-l", "--list", action="store_true", help="list supported commands")
    parser.add_argument("COMMAND", nargs="?", type=case_insensitive_command)
    parser.add_argument("--vid", type=lambda x: int(x, 0), default=DEFAULT_USB_VID)
    parser.add_argument(
        "--pid",
        type=lambda x: int(x, 0),
        default=None,
        help="USB PID to match exactly; default auto-detects supported XVF PIDs",
    )
    parser.add_argument("--values", nargs="+", type=parse_value)
    args = parser.parse_args()

    if args.list:
        list_commands()
        return 0
    if not args.COMMAND:
        parser.error("COMMAND is required unless --list is used")

    try:
        dev = find(vid=args.vid, pid=args.pid)
    except XvfControlError as exc:
        print(f"Error locating XVF3800: {exc}")
        return 1
    if dev is None:
        print("No device found")
        return 1

    try:
        if args.values is not None:
            dev.write(args.COMMAND, args.values)
        else:
            values = dev.read(args.COMMAND)
            print(f"{args.COMMAND}: {_format_values(values)}")
        print("Done!")
        return 0
    except (ValueError, XvfControlError) as exc:
        print(f"Error executing command {args.COMMAND}: {exc}")
        return 1
    finally:
        dev.close()


if __name__ == "__main__":
    sys.exit(main())
