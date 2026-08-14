# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free coverage for the manual USB-turntable experiment."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "experiments" / "usb-turntable" / "jts_turntable.py"
README = SCRIPT.with_name("README.md")


def load_script():
    spec = importlib.util.spec_from_file_location("jts_turntable_experiment", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def turntable():
    return load_script()


@pytest.fixture
def isolated_usb_turntable_modules():
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "usb_turntable" or name.startswith("usb_turntable.")
    }
    for name in saved:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name in tuple(sys.modules):
            if name == "usb_turntable" or name.startswith("usb_turntable."):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


def command_result(stdout: str, *, returncode: int = 0, stderr: str = ""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def healthy_power(*args, **kwargs):
    return command_result("throttled=0x0\n")


class FakeController:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.probe_result = SimpleNamespace(
            connected=True,
            product="USB Turntable",
            firmware="1.2.3",
            port="/dev/ttyUSB0",
            frames=[],
        )
        self.operation_result = SimpleNamespace(
            operation="test",
            command="owned upstream",
            acknowledged=True,
            completed=True,
            pause_seen=True,
            frames=[],
        )

    def __enter__(self):
        self.calls.append(("enter",))
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.calls.append(("exit",))

    def probe(self):
        self.calls.append(("probe",))
        return self.probe_result

    def turn_relative(self, direction: str, degrees: float):
        self.calls.append(("turn_relative", direction, degrees))
        return self.operation_result

    def set_zero(self):
        self.calls.append(("set_zero",))
        return self.operation_result

    def return_to_zero(self):
        self.calls.append(("return_to_zero",))
        return self.operation_result

    def stop(self):
        self.calls.append(("stop",))
        return self.operation_result


class FakeControllerFactory:
    def __init__(self, controller: FakeController) -> None:
        self.instance = controller
        self.open_calls: list[dict[str, object]] = []

    def open(self, **kwargs):
        self.open_calls.append(kwargs)
        return self.instance


def fake_api(turntable, *, devices=None):
    controller = FakeController()
    factory = FakeControllerFactory(controller)
    api = turntable.TurntableApi(
        discover_devices=lambda: list(devices or []),
        controller=factory,
    )
    return api, factory, controller


def parse_output(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_vendored_upstream_snapshot_matches_manifest(turntable) -> None:
    manifest = turntable.verify_vendor()

    assert turntable.VENDOR_MANIFEST_SHA256 == (
        "371ae63ab8ada0866b5efce93d013b5d592950066adb38d43e59b823d8c32fd3"
    )
    assert manifest["commit"] == "64a37c31161ffc02b9467c26d4fa06d7fb8b3334"
    assert manifest["aggregate_sha256"] == (
        "ae0660b4f67eb31ec56ae4a10d681ae347b5abfeb9ca516b67821a08304ab7aa"
    )
    assert len(manifest["files"]) == 9
    assert manifest["runtime_dependencies"] == []
    assert manifest["license"] == {"status": "declared", "spdx": "Apache-2.0"}
    assert sorted(manifest["license_files"]) == ["LICENSE", "NOTICE.md"]


def test_vendor_verification_fails_on_local_protocol_drift(
    turntable,
    tmp_path: Path,
) -> None:
    vendor_copy = tmp_path / "vendor"
    shutil.copytree(turntable.VENDOR_ROOT, vendor_copy)
    controller = vendor_copy / "usb_turntable" / "controller.py"
    controller.write_text(controller.read_text() + "\n# local drift\n")

    with pytest.raises(RuntimeError, match="hash mismatch for.*controller.py"):
        turntable.verify_vendor(vendor_copy, vendor_copy / "UPSTREAM.json")


def test_coordinated_source_and_manifest_rewrite_cannot_move_trust_root(
    turntable,
    tmp_path: Path,
) -> None:
    vendor_copy = tmp_path / "vendor"
    shutil.copytree(turntable.VENDOR_ROOT, vendor_copy)
    controller = vendor_copy / "usb_turntable" / "controller.py"
    controller.write_text(controller.read_text() + "\n# coordinated drift\n")

    manifest_path = vendor_copy / "UPSTREAM.json"
    manifest = json.loads(manifest_path.read_text())
    relative_path = "usb_turntable/controller.py"
    manifest["files"][relative_path] = hashlib.sha256(controller.read_bytes()).hexdigest()
    digest_lines = [
        f"{manifest['files'][path]}  {path}\n" for path in sorted(manifest["files"])
    ]
    manifest["aggregate_sha256"] = hashlib.sha256(
        "".join(digest_lines).encode("ascii")
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    with pytest.raises(RuntimeError, match="manifest trust-root mismatch"):
        turntable.verify_vendor(vendor_copy, manifest_path)


def test_vendor_verification_fails_on_license_drift(
    turntable,
    tmp_path: Path,
) -> None:
    vendor_copy = tmp_path / "vendor"
    shutil.copytree(turntable.VENDOR_ROOT, vendor_copy)
    notice = vendor_copy / "NOTICE.md"
    notice.write_text(notice.read_text() + "\nlocal drift\n")

    with pytest.raises(RuntimeError, match="license hash mismatch for NOTICE.md"):
        turntable.verify_vendor(vendor_copy, vendor_copy / "UPSTREAM.json")


def test_vendor_rejects_unlisted_platform_extension_before_import(
    turntable,
    tmp_path: Path,
) -> None:
    vendor_copy = tmp_path / "vendor"
    shutil.copytree(turntable.VENDOR_ROOT, vendor_copy)
    extension_suffix = sysconfig.get_config_var("EXT_SUFFIX") or ".so"
    (vendor_copy / f"usb_turntable{extension_suffix}").write_bytes(b"not loadable")

    with pytest.raises(RuntimeError, match="vendor root inventory"):
        turntable.verify_vendor(vendor_copy, vendor_copy / "UPSTREAM.json")


def test_vendor_rejects_unlisted_bytecode_directory(
    turntable,
    tmp_path: Path,
) -> None:
    vendor_copy = tmp_path / "vendor"
    shutil.copytree(turntable.VENDOR_ROOT, vendor_copy)
    cache = vendor_copy / "usb_turntable" / "__pycache__"
    cache.mkdir()
    (cache / "controller.cpython-312.pyc").write_bytes(b"not bytecode")

    with pytest.raises(RuntimeError, match="package inventory"):
        turntable.verify_vendor(vendor_copy, vendor_copy / "UPSTREAM.json")


@pytest.mark.parametrize("extra_name", ["extra.py", "extra_package"])
def test_vendor_rejects_extra_package_modules_or_directories(
    turntable,
    tmp_path: Path,
    extra_name: str,
) -> None:
    vendor_copy = tmp_path / "vendor"
    shutil.copytree(turntable.VENDOR_ROOT, vendor_copy)
    extra = vendor_copy / "usb_turntable" / extra_name
    if extra.suffix:
        extra.write_text("raise AssertionError('must not import')\n")
    else:
        extra.mkdir()

    with pytest.raises(RuntimeError, match="package inventory"):
        turntable.verify_vendor(vendor_copy, vendor_copy / "UPSTREAM.json")


def test_vendor_rejects_symlinked_source(
    turntable,
    tmp_path: Path,
) -> None:
    vendor_copy = tmp_path / "vendor"
    shutil.copytree(turntable.VENDOR_ROOT, vendor_copy)
    controller = vendor_copy / "usb_turntable" / "controller.py"
    original = tmp_path / "controller.py"
    controller.replace(original)
    controller.symlink_to(original)

    with pytest.raises(RuntimeError, match="missing or a symlink"):
        turntable.verify_vendor(vendor_copy, vendor_copy / "UPSTREAM.json")


def test_canonical_api_loads_from_the_pinned_vendor_tree(
    turntable,
    isolated_usb_turntable_modules,
) -> None:
    api = turntable.load_upstream_api()

    assert api.controller.__module__ == "usb_turntable.controller"
    assert api.discover_devices.__module__ == "usb_turntable.discovery"
    module_path = Path(sys.modules["usb_turntable"].__file__).resolve()
    assert module_path.is_relative_to(turntable.VENDOR_ROOT.resolve())


def test_verified_import_writes_no_bytecode_and_supports_repeated_use(
    turntable,
    isolated_usb_turntable_modules,
    monkeypatch,
    tmp_path: Path,
) -> None:
    vendor_copy = tmp_path / "vendor"
    shutil.copytree(turntable.VENDOR_ROOT, vendor_copy)
    monkeypatch.setattr(turntable, "VENDOR_ROOT", vendor_copy)
    monkeypatch.setattr(turntable, "VENDOR_MANIFEST", vendor_copy / "UPSTREAM.json")
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    prior_cache_prefix = str(tmp_path / "process-cache")
    monkeypatch.setattr(sys, "pycache_prefix", prior_cache_prefix)

    first = turntable.load_upstream_api()
    second = turntable.load_upstream_api()

    assert first.controller is second.controller
    assert first.discover_devices is second.discover_devices
    assert sys.dont_write_bytecode is False
    assert sys.pycache_prefix == prior_cache_prefix
    assert not list(vendor_copy.rglob("*.pyc"))
    assert not list(vendor_copy.rglob("__pycache__"))


def test_foreign_sys_modules_entry_cannot_override_verified_package(
    turntable,
    isolated_usb_turntable_modules,
    monkeypatch,
    tmp_path: Path,
) -> None:
    foreign = SimpleNamespace(
        __file__=str(tmp_path / "usb_turntable.py"),
        TurntableController=object(),
        discover_devices=lambda: [],
    )
    monkeypatch.setitem(sys.modules, "usb_turntable", foreign)

    with pytest.raises(RuntimeError, match="outside the pinned sources"):
        turntable.load_upstream_api()


def test_verified_vendor_wins_path_precedence_and_restores_exact_sys_path(
    turntable,
    isolated_usb_turntable_modules,
    monkeypatch,
    tmp_path: Path,
) -> None:
    foreign_root = tmp_path / "foreign"
    foreign_package = foreign_root / "usb_turntable"
    foreign_package.mkdir(parents=True)
    execution_marker = tmp_path / "foreign-executed"
    (foreign_package / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(execution_marker)!r}).write_text('executed')\n"
        "raise AssertionError('foreign package must never execute')\n"
    )
    vendor_path = str(turntable.VENDOR_ROOT)
    prior_sys_path = [
        str(foreign_root),
        vendor_path,
        *sys.path,
        vendor_path,
        str(foreign_root),
    ]
    active_sys_path = prior_sys_path.copy()
    monkeypatch.setattr(sys, "path", active_sys_path)

    api = turntable.load_upstream_api()

    assert api.controller.__module__ == "usb_turntable.controller"
    assert not execution_marker.exists()
    assert sys.path is active_sys_path
    assert sys.path == prior_sys_path


def test_power_preflight_accepts_healthy_status(turntable) -> None:
    status = turntable.read_power_status(healthy_power)

    assert status.available is True
    assert status.safe_for_motion is True
    assert status.raw == "0x0"
    assert status.current_flags == ()
    assert status.history_flags == ()


def test_power_preflight_uses_exact_bounded_subprocess_call(turntable) -> None:
    calls = []

    def record_call(*args, **kwargs):
        calls.append((args, kwargs))
        return command_result("throttled=0x0\n")

    status = turntable.read_power_status(record_call)

    assert status.safe_for_motion is True
    assert calls == [
        (
            (["vcgencmd", "get_throttled"],),
            {"capture_output": True, "text": True, "timeout": 2.0},
        )
    ]


def test_power_preflight_blocks_current_flags(turntable) -> None:
    status = turntable.read_power_status(
        lambda *args, **kwargs: command_result("throttled=0x50005\n")
    )

    assert status.available is True
    assert status.safe_for_motion is False
    assert status.current_flags == ("under_voltage", "throttled")
    assert status.history_flags == ("under_voltage", "throttled")


def test_power_preflight_warns_but_allows_since_boot_flags(turntable) -> None:
    status = turntable.read_power_status(
        lambda *args, **kwargs: command_result("throttled=0x50000\n")
    )

    assert status.available is True
    assert status.safe_for_motion is True
    assert status.current_flags == ()
    assert status.history_flags == ("under_voltage", "throttled")
    assert "currently healthy" in status.detail


@pytest.mark.parametrize(
    "run_command, detail_fragment",
    [
        (lambda *args, **kwargs: command_result("garbage\n"), "unexpected"),
        (
            lambda *args, **kwargs: command_result(
                "", returncode=1, stderr="firmware unavailable"
            ),
            "exited 1",
        ),
        (
            lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
            "unavailable",
        ),
        (
            lambda *args, **kwargs: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(["vcgencmd", "get_throttled"], 2.0)
            ),
            "TimeoutExpired",
        ),
    ],
)
def test_power_preflight_fails_closed_when_status_is_unknown(
    turntable,
    run_command,
    detail_fragment: str,
) -> None:
    status = turntable.read_power_status(run_command)

    assert status.available is False
    assert status.safe_for_motion is False
    assert detail_fragment in status.detail


def test_detect_returns_upstream_device_records(turntable, capsys) -> None:
    device = {
        "path": "/dev/ttyUSB0",
        "stable_path": "/dev/serial/by-id/example",
        "vid": "1a86",
        "pid": "7523",
        "serial": "abc",
        "manufacturer": "Example",
        "product": "USB Turntable",
    }
    api, factory, _controller = fake_api(turntable, devices=[device])

    exit_code = turntable.main(["--json", "detect"], api=api)

    assert exit_code == 0
    assert parse_output(capsys) == {"devices": [device], "ok": True}
    assert factory.open_calls == []


def test_detect_returns_failure_when_no_device_matches(turntable, capsys) -> None:
    api, factory, _controller = fake_api(turntable)

    exit_code = turntable.main(["--json", "detect"], api=api)

    assert exit_code == 1
    assert parse_output(capsys) == {"devices": [], "ok": False}
    assert factory.open_calls == []


def test_probe_is_read_only_and_bypasses_power_preflight(turntable, capsys) -> None:
    api, factory, controller = fake_api(turntable)

    def unexpected_power_probe(*args, **kwargs):
        raise AssertionError("probe must not require Pi power status")

    exit_code = turntable.main(
        [
            "--json",
            "--port",
            "/dev/serial/by-id/example",
            "--response-timeout",
            "4",
            "--motion-timeout",
            "31",
            "--startup-timeout",
            "5",
            "probe",
        ],
        api=api,
        run_command=unexpected_power_probe,
    )

    assert exit_code == 0
    assert parse_output(capsys)["result"]["firmware"] == "1.2.3"
    assert controller.calls == [("enter",), ("probe",), ("exit",)]
    assert factory.open_calls == [
        {
            "port": "/dev/serial/by-id/example",
            "response_timeout": 4.0,
            "motion_timeout": 31.0,
            "startup_timeout": 5.0,
        }
    ]


@pytest.mark.parametrize(
    ("command", "upstream_direction"),
    [("left", "cw"), ("right", "ccw")],
)
def test_vendor_left_and_right_labels_map_to_verified_directions(
    turntable,
    capsys,
    command: str,
    upstream_direction: str,
) -> None:
    api, _factory, controller = fake_api(turntable)

    exit_code = turntable.main(
        ["--json", command, "12.5"],
        api=api,
        run_command=healthy_power,
    )

    assert exit_code == 0
    output = parse_output(capsys)
    assert output["ok"] is True
    assert output["power"]["motion_allowed"] is True
    assert ("turn_relative", upstream_direction, 12.5) in controller.calls


@pytest.mark.parametrize("command", ["left", "right", "home"])
def test_motion_is_blocked_before_open_when_power_is_unsafe(
    turntable,
    capsys,
    command: str,
) -> None:
    api, factory, _controller = fake_api(turntable)
    argv = ["--json", command]
    if command in {"left", "right"}:
        argv.append("10")

    exit_code = turntable.main(
        argv,
        api=api,
        run_command=lambda *args, **kwargs: command_result("throttled=0x1\n"),
    )

    assert exit_code == 1
    output = parse_output(capsys)
    assert output["ok"] is False
    assert output["power"]["motion_allowed"] is False
    assert output["power"]["status"]["current_flags"] == ["under_voltage"]
    assert factory.open_calls == []


def test_explicit_power_override_allows_manual_motion(turntable, capsys) -> None:
    api, _factory, controller = fake_api(turntable)

    exit_code = turntable.main(
        ["--json", "--allow-power-risk", "left", "10"],
        api=api,
        run_command=lambda *args, **kwargs: command_result("throttled=0x1\n"),
    )

    assert exit_code == 0
    output = parse_output(capsys)
    assert output["power"]["override"] is True
    assert output["power"]["motion_allowed"] is True
    assert ("turn_relative", "cw", 10.0) in controller.calls


def test_explicit_power_override_allows_motion_when_status_is_unreadable(
    turntable,
    capsys,
) -> None:
    api, _factory, controller = fake_api(turntable)

    exit_code = turntable.main(
        ["--json", "--allow-power-risk", "left", "10"],
        api=api,
        run_command=lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["vcgencmd", "get_throttled"], 2.0)
        ),
    )

    assert exit_code == 0
    output = parse_output(capsys)
    assert output["power"]["status"]["available"] is False
    assert output["power"]["override"] is True
    assert output["power"]["motion_allowed"] is True
    assert ("turn_relative", "cw", 10.0) in controller.calls


@pytest.mark.parametrize(
    ("command", "expected_call"),
    [("set-zero", "set_zero"), ("stop", "stop")],
)
def test_non_motion_commands_do_not_require_power_status(
    turntable,
    capsys,
    command: str,
    expected_call: str,
) -> None:
    api, _factory, controller = fake_api(turntable)

    def unavailable_power_probe(*args, **kwargs):
        raise AssertionError(f"{command} must not run the power preflight")

    exit_code = turntable.main(
        ["--json", command],
        api=api,
        run_command=unavailable_power_probe,
    )

    assert exit_code == 0
    parse_output(capsys)
    assert (expected_call,) in controller.calls


def test_incomplete_operation_is_not_reported_as_success(turntable, capsys) -> None:
    api, _factory, controller = fake_api(turntable)
    controller.operation_result = SimpleNamespace(
        operation="return_to_zero",
        command="owned upstream",
        acknowledged=True,
        completed=False,
        pause_seen=False,
        frames=[],
    )

    exit_code = turntable.main(
        ["--json", "home"],
        api=api,
        run_command=healthy_power,
    )

    assert exit_code == 1
    output = parse_output(capsys)
    assert output["ok"] is False
    assert output["result"]["acknowledged"] is True
    assert output["result"]["completed"] is False


@pytest.mark.parametrize("degrees", ["0", "-1", "nan", "inf"])
def test_relative_turn_rejects_non_positive_or_non_finite_degrees(
    turntable,
    degrees: str,
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        turntable.build_parser().parse_args(["left", degrees])

    assert exc_info.value.code == 2


def test_stop_and_motion_timeout_language_preserves_safety_boundary(turntable) -> None:
    help_text = " ".join(turntable.build_parser().format_help().split())
    readme = " ".join(README.read_text().split())

    assert "send the vendor stop request" in help_text
    assert "expiry does not stop platform motion" in help_text
    assert "not a physical emergency stop" in readme
    assert "delay or prevent transmission" in readme
    assert "does not cancel or stop platform motion" in readme
    assert "hardware power cutoff" in readme
    assert "classifies them after 20 ms of inter-byte quiet" in readme
    assert "Exact `###` reports remote link loss" in readme
    assert "Fresh-open recovery is bounded by `--startup-timeout`" in readme
    assert (
        "runtime pre-command and post-settle recovery is bounded by "
        "`--response-timeout`"
    ) in readme
    assert "platform may already have acted" in readme
    assert "request an immediate stop" not in help_text
    assert "# Always available" not in readme
    assert "**Emergency stop.**" not in readme


def test_jts_adapter_contains_no_serial_protocol_implementation() -> None:
    source = SCRIPT.read_text()
    tree = ast.parse(source)
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_modules.update(
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    )
    constants = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))
    }

    assert not any(
        module == "serial" or module.startswith("serial.")
        for module in imported_modules
    )
    assert not any(
        (value.decode(errors="ignore") if isinstance(value, bytes) else value).startswith(
            "CT"
        )
        for value in constants
    )
