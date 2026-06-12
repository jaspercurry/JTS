import ast
import re
import struct
from pathlib import Path

import pytest

from jasper.xvf import xvf_host

REPO = Path(__file__).resolve().parents[1]

_COMMAND_NAME_RE = re.compile(
    r"(?:AEC|SHF|AUDIO_MGR|I2S|GPO|LED|BLD|BOOT|VERSION|USB_BIT_DEPTH|"
    r"CLEAR_CONFIGURATION|REBOOT)(?:_[A-Z0-9]+)*"
)

_FORBIDDEN_COMMANDS = {
    # AGENTS.md / HANDOFF-xvf3800 brick-hazard guard: do not re-enable
    # persistent or destructive upstream demo commands in the JTS subset.
    "SAVE_CONFIGURATION",
    "TEST_CORE_BURN",
    "TEST_AEC_DISABLE_CONTROL",
}

_EXPECTED_XVF_COMMANDS_BY_CALLER = {
    "jasper/cli/aec_init.py": {
        "AEC_AECEMPHASISONOFF",
        "AEC_ASROUTGAIN",
        "AEC_ASROUTONOFF",
        "AEC_FAR_EXTGAIN",
        "AEC_FIXEDBEAMSAZIMUTH_VALUES",
        "AEC_FIXEDBEAMSELEVATION_VALUES",
        "AEC_FIXEDBEAMSGATING",
        "AEC_FIXEDBEAMSONOFF",
        "AEC_HPFONOFF",
        "AUDIO_MGR_OP_L",
        "AUDIO_MGR_OP_R",
        "AUDIO_MGR_SYS_DELAY",
        "SHF_BYPASS",
        "VERSION",
    },
    "jasper/audio_validation.py": {
        "AEC_AECCONVERGED",
        "AEC_ASROUTONOFF",
        "AEC_FIXEDBEAMSGATING",
        "AEC_FIXEDBEAMSONOFF",
        "AUDIO_MGR_SYS_DELAY",
        "SHF_BYPASS",
    },
    "deploy/bin/jasper-aec-reconcile": set(),
}


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


class _CommandStringVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.commands: set[str] = set()

    def visit_Expr(self, node: ast.Expr) -> None:
        # Ignore module/function docstrings and bare prose guard strings.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and _COMMAND_NAME_RE.fullmatch(node.value):
            self.commands.add(node.value)


def _python_command_literals(path: Path) -> set[str]:
    visitor = _CommandStringVisitor()
    visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return visitor.commands


def _shell_command_literals(path: Path) -> set[str]:
    commands: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "xvf_host" not in stripped:
            continue
        commands.update(_COMMAND_NAME_RE.findall(line))
    return commands


def _command_table_keys_from_source() -> set[str]:
    path = REPO / "jasper/xvf/xvf_host.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        value = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "COMMANDS":
                value = node.value
        elif isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "COMMANDS"
                   for target in node.targets):
                value = node.value
        if isinstance(value, ast.Dict):
            return {
                key.value
                for key in value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    raise AssertionError("COMMANDS dict not found in jasper/xvf/xvf_host.py")


def _caller_command_literals(relpath: str) -> set[str]:
    path = REPO / relpath
    if path.suffix == ".py":
        return _python_command_literals(path)
    return _shell_command_literals(path)


def _aec_init_profile_writes() -> tuple[tuple[str, list[int | float]], ...]:
    from jasper.cli import aec_init

    return (
        *aec_init._CHIP_CORPUS_PROFILE,
        *aec_init._CHIP_PRODUCTION_PROFILE,
        ("AEC_HPFONOFF", [0]),
    )


def test_static_guard_keeps_unsafe_xvf_commands_out_of_command_table() -> None:
    source_keys = _command_table_keys_from_source()

    assert _FORBIDDEN_COMMANDS.isdisjoint(source_keys)
    assert _FORBIDDEN_COMMANDS.isdisjoint(xvf_host.COMMANDS)


def test_production_xvf_callers_use_only_registered_commands() -> None:
    observed = {
        relpath: _caller_command_literals(relpath)
        for relpath in _EXPECTED_XVF_COMMANDS_BY_CALLER
    }

    assert observed == _EXPECTED_XVF_COMMANDS_BY_CALLER

    for relpath, command_names in observed.items():
        missing = command_names - xvf_host.COMMANDS.keys()
        assert not missing, (
            f"{relpath} uses XVF command(s) not present in "
            f"jasper.xvf.xvf_host.COMMANDS: {sorted(missing)}"
        )
        write_only_reads = {
            name
            for name in command_names
            if name not in {"REBOOT", "CLEAR_CONFIGURATION"}
            and xvf_host.COMMANDS[name].access == "wo"
        }
        assert not write_only_reads, (
            f"{relpath} reads or read-verifies write-only XVF command(s): "
            f"{sorted(write_only_reads)}"
        )

    for command_name, values in _aec_init_profile_writes():
        command = xvf_host.COMMANDS[command_name]
        assert command.access != "ro", f"{command_name} is read-only"
        assert len(values) == command.count, (
            f"{command_name} writes {len(values)} value(s), but COMMANDS "
            f"declares count={command.count}"
        )


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
