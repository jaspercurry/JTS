# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the UAC2 gadget device-name byte patcher.

The module under test lives in deploy/usbsink/ (it runs at early boot
under the system python3, outside the jasper package / venv), so we
load it by path. Pure-stdlib, no hardware.
"""

import importlib.util
from pathlib import Path

_MOD_PATH = (
    Path(__file__).resolve().parents[1]
    / "deploy"
    / "usbsink"
    / "uac2_name_patch.py"
)
_spec = importlib.util.spec_from_file_location("uac2_name_patch", _MOD_PATH)
uac2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(uac2)


def _fake_module(
    primary: bytes = b"Playback Inactive\x00",
    secondary: bytes = b"Playback Active\x00",
) -> bytes:
    """A blob shaped like .rodata: the two AS strings surrounded by
    other null-terminated strings, with alignment slack between them."""
    return (
        b"\x7fELF\x00\x00\x00\x00"          # fake ELF-ish header
        + b"Topology Control\x00"
        + primary
        + b"\x00\x00\x00"                    # alignment slack
        + secondary
        + b"\x00"
        + b"Capture Inactive\x00"            # must remain untouched
        + b"Playback Volume\x00"
    )


def test_replaces_both_strings_preserving_length():
    blob = _fake_module()
    res = uac2.patch_module_bytes(blob, "JTS")
    assert res.ok
    assert set(res.replaced) == {"Playback Inactive", "Playback Active"}
    assert res.missing == []
    assert res.ambiguous == []
    # Total length is unchanged (in-place overwrite).
    assert len(res.blob) == len(blob)
    # Stock strings are gone; new name present.
    assert b"Playback Inactive\x00" not in res.blob
    assert b"Playback Active\x00" not in res.blob
    assert b"JTS\x00" in res.blob
    # Neighbours untouched.
    assert b"Topology Control\x00" in res.blob
    assert b"Capture Inactive\x00" in res.blob
    assert b"Playback Volume\x00" in res.blob


def test_name_fills_slot_with_null_padding():
    blob = _fake_module()
    res = uac2.patch_module_bytes(blob, "Living Room")  # 11 chars
    # The "Playback Active" slot was 16 bytes (15 + NUL); "Living Room"
    # (11) + 5 NUL padding keeps that exact width.
    idx = res.blob.find(b"Living Room")
    assert idx != -1
    assert res.blob[idx : idx + 16] == b"Living Room" + b"\x00" * 5


def test_truncates_to_fifteen_bytes():
    res = uac2.patch_module_bytes(_fake_module(), "Brittany's Speaker")  # 18
    assert res.name == "Brittany's Spea"  # 15 chars
    assert len(res.name) == uac2.MAX_NAME_BYTES
    assert res.ok
    assert b"Brittany's Spea\x00" in res.blob


def test_sanitize_strips_disallowed_and_non_ascii():
    assert uac2.sanitize_name("Café/Kitchen!") == "CafKitchen"
    assert uac2.sanitize_name("  spaced   out  ") == "spaced out"
    assert uac2.sanitize_name("") == "JTS"
    assert uac2.sanitize_name("///") == "JTS"
    # Allowed punctuation survives.
    assert uac2.sanitize_name("A&B + C") == "A&B + C"


def test_string_not_found_leaves_blob_and_reports_missing():
    blob = b"\x7fELF\x00 no audiostreaming strings here \x00"
    res = uac2.patch_module_bytes(blob, "JTS")
    assert not res.ok
    assert set(res.missing) == {"Playback Inactive", "Playback Active"}
    assert res.blob == blob  # untouched


def test_ambiguous_match_is_not_patched():
    # Two copies of the primary token → refuse to guess.
    blob = b"Playback Inactive\x00xxxx Playback Inactive\x00 Playback Active\x00"
    res = uac2.patch_module_bytes(blob, "JTS")
    assert "Playback Inactive" in res.ambiguous
    assert not res.ok  # primary not replaced
    assert b"Playback Inactive\x00" in res.blob  # left intact


def test_already_patched_blob_reports_missing_not_double_patch():
    # A previously-patched module has no stock strings — the patcher
    # must not invent matches. (In production we always patch from the
    # stock module, so this path documents the no-op behavior.)
    patched = uac2.patch_module_bytes(_fake_module(), "JTS").blob
    res = uac2.patch_module_bytes(patched, "Kitchen")
    assert not res.ok
    assert set(res.missing) == {"Playback Inactive", "Playback Active"}


def test_cli_roundtrip_success(tmp_path):
    stock = tmp_path / "usb_f_uac2.ko"
    out = tmp_path / "override.ko"
    stock.write_bytes(_fake_module())
    rc = uac2._main(["prog", str(stock), "Kitchen", str(out)])
    assert rc == 0
    assert out.exists()
    assert b"Kitchen\x00" in out.read_bytes()
    assert b"Playback Inactive\x00" not in out.read_bytes()


def test_cli_no_match_returns_3_and_writes_nothing(tmp_path):
    stock = tmp_path / "usb_f_uac2.ko"
    out = tmp_path / "override.ko"
    stock.write_bytes(b"no strings here")
    rc = uac2._main(["prog", str(stock), "Kitchen", str(out)])
    assert rc == 3
    assert not out.exists()
