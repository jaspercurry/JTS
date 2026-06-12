import struct

import pytest

from jasper.xvf import xvf_host


class _FakeUsbDevice:
    def __init__(self, responses: list[bytes] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls = []

    def ctrl_transfer(
        self,
        request_type,
        request,
        value,
        index,
        data_or_w_length,
        timeout,
    ):
        self.calls.append(
            (request_type, request, value, index, data_or_w_length, timeout)
        )
        if self.responses:
            return self.responses.pop(0)
        return None


def test_write_packs_uint8_vendor_control_payload() -> None:
    fake = _FakeUsbDevice()
    dev = xvf_host.ReSpeaker(fake)

    dev.write("AUDIO_MGR_OP_L", [8, 0])

    assert fake.calls == [
        (
            0x40,
            0,
            15,
            35,
            b"\x08\x00",
            xvf_host.DEFAULT_TIMEOUT_MS,
        )
    ]


def test_write_accepts_integral_float_uint8_values() -> None:
    fake = _FakeUsbDevice()
    dev = xvf_host.ReSpeaker(fake)

    dev.write("AUDIO_MGR_OP_L", [1.0, 0])

    assert fake.calls[0][4] == b"\x01\x00"


@pytest.mark.parametrize("values", [[-1, 0], [256, 0], [1.5, 0]])
def test_write_rejects_invalid_uint8_values(values) -> None:
    fake = _FakeUsbDevice()
    dev = xvf_host.ReSpeaker(fake)

    with pytest.raises(ValueError, match="uint8"):
        dev.write("AUDIO_MGR_OP_L", values)

    assert fake.calls == []


def test_read_unpacks_int32_vendor_control_response() -> None:
    fake = _FakeUsbDevice([bytes([0]) + struct.pack("<i", 1)])
    dev = xvf_host.ReSpeaker(fake)

    assert dev.read("AEC_AECCONVERGED") == (1,)
    assert fake.calls == [
        (
            0xC0,
            0,
            0x83,
            33,
            5,
            xvf_host.DEFAULT_TIMEOUT_MS,
        )
    ]


def test_read_retries_while_chip_reports_busy() -> None:
    fake = _FakeUsbDevice(
        [
            bytes([xvf_host.CONTROL_RETRY, 0]),
            bytes([0]) + struct.pack("<BBB", 2, 0, 8),
        ]
    )
    dev = xvf_host.ReSpeaker(fake)

    assert dev.read("VERSION") == (2, 0, 8)
    assert len(fake.calls) == 2


def test_read_unpacks_char_response_as_single_string() -> None:
    fake = _FakeUsbDevice([bytes([0]) + b"Jof" + b"\0" * 3])
    dev = xvf_host.ReSpeaker(fake)

    assert dev.read("BOOT_STATUS") == ("Jof",)


def test_write_rejects_read_only_commands() -> None:
    fake = _FakeUsbDevice()
    dev = xvf_host.ReSpeaker(fake)

    with pytest.raises(ValueError, match="read-only"):
        dev.write("VERSION", [2, 0, 8])
    assert fake.calls == []


def test_read_rejects_write_only_commands() -> None:
    fake = _FakeUsbDevice()
    dev = xvf_host.ReSpeaker(fake)

    with pytest.raises(ValueError, match="write-only"):
        dev.read("REBOOT")
    assert fake.calls == []


def test_find_reports_missing_usb_dependency(monkeypatch) -> None:
    monkeypatch.setattr(xvf_host.sys, "platform", "linux")
    monkeypatch.setattr(xvf_host, "usb", None)
    monkeypatch.setattr(
        xvf_host,
        "_USB_IMPORT_ERROR",
        ModuleNotFoundError("No module named 'usb'"),
    )

    with pytest.raises(xvf_host.XvfControlError, match="dependencies missing"):
        xvf_host.find()
