# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free coverage for the manual USB-turntable experiment."""

import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = ROOT / "experiments" / "usb-turntable"
SCRIPT = EXPERIMENT / "jts_turntable.py"
VENDOR = EXPERIMENT / "vendor"
AUTOSTOP_RULE = ROOT / "deploy" / "udev" / "99-jasper-turntable-autostop.rules"
AUTOSTOP_UNIT = (
    ROOT / "deploy" / "systemd" / "jasper-turntable-autostop@.service"
)


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


def command_result(stdout: str, *, returncode: int = 0, stderr: str = ""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def healthy_power(*args, **kwargs):
    return command_result("throttled=0x0\n")


class FakeController:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.probe_result = SimpleNamespace(connected=True, firmware="1.2.3")
        self.operation_result = SimpleNamespace(acknowledged=True, completed=True)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        pass

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


def parse_json_lines(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_vendored_snapshot_provenance_is_current() -> None:
    manifest = json.loads((VENDOR / "UPSTREAM.json").read_text())
    assert manifest == {
        "repository": "https://github.com/jaspercurry/USB-Turntable",
        "commit": "1fc3bd72094f7ed919e8d95a000bb7c7eefd9a8e",
        "version": "0.1.0",
        "aggregate_sha256": (
            "800a4af7103ecf43707e23c9f69d07e32b068bd7973b2420f80a86522c37eb35"
        ),
        "license": "Apache-2.0",
    }

    sources = sorted((VENDOR / "usb_turntable").glob("*.py"))
    assert len(sources) == 9
    digest_lines = [
        f"{hashlib.sha256(source.read_bytes()).hexdigest()}  "
        f"usb_turntable/{source.name}\n"
        for source in sources
    ]
    assert hashlib.sha256("".join(digest_lines).encode("ascii")).hexdigest() == (
        manifest["aggregate_sha256"]
    )
    assert all((VENDOR / name).is_file() for name in ("LICENSE", "NOTICE.md"))


@pytest.mark.parametrize(
    ("raw", "safe", "current", "history"),
    [
        ("0x0", True, (), ()),
        ("0x50005", False, ("under_voltage", "throttled"), ("under_voltage", "throttled")),
        ("0x50000", True, (), ("under_voltage", "throttled")),
    ],
)
def test_power_preflight_decodes_current_and_historical_flags(
    turntable,
    raw: str,
    safe: bool,
    current: tuple[str, ...],
    history: tuple[str, ...],
) -> None:
    status = turntable.read_power_status(
        lambda *args, **kwargs: command_result(f"throttled={raw}\n")
    )

    assert status.available is True
    assert status.safe_for_motion is safe
    assert status.current_flags == current
    assert status.history_flags == history


def test_power_preflight_call_is_bounded(turntable) -> None:
    calls = []

    def record_call(*args, **kwargs):
        calls.append((args, kwargs))
        return command_result("throttled=0x0\n")

    assert turntable.read_power_status(record_call).safe_for_motion is True
    assert calls == [
        (
            (["vcgencmd", "get_throttled"],),
            {"capture_output": True, "text": True, "timeout": 2.0},
        )
    ]


@pytest.mark.parametrize(
    "run_command",
    [
        lambda *args, **kwargs: command_result("garbage\n"),
        lambda *args, **kwargs: command_result("", returncode=1),
        lambda *args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["vcgencmd", "get_throttled"], 2.0)
        ),
    ],
)
def test_power_preflight_fails_closed_when_unknown(turntable, run_command) -> None:
    status = turntable.read_power_status(run_command)

    assert status.available is False
    assert status.safe_for_motion is False


@pytest.mark.parametrize("devices, expected_exit", [([], 1), ([{"path": "/dev/ttyUSB0"}], 0)])
def test_detect_returns_structured_discovery(
    turntable,
    capsys,
    devices,
    expected_exit: int,
) -> None:
    api, factory, _controller = fake_api(turntable, devices=devices)

    assert turntable.main(["--json", "detect"], api=api) == expected_exit
    assert parse_output(capsys) == {"devices": devices, "ok": bool(devices)}
    assert factory.open_calls == []


def test_probe_uses_optional_port_without_power_preflight(turntable, capsys) -> None:
    api, factory, controller = fake_api(turntable)

    def unexpected_power_probe(*args, **kwargs):
        raise AssertionError("probe must not run the power preflight")

    assert turntable.main(
        ["--json", "--port", "/dev/serial/by-id/example", "probe"],
        api=api,
        run_command=unexpected_power_probe,
    ) == 0
    assert parse_output(capsys)["result"]["firmware"] == "1.2.3"
    assert factory.open_calls == [{"port": "/dev/serial/by-id/example"}]
    assert controller.calls == [("probe",)]


@pytest.mark.parametrize(("command", "direction"), [("left", "cw"), ("right", "ccw")])
def test_vendor_labels_map_to_upstream_directions(
    turntable,
    capsys,
    command: str,
    direction: str,
) -> None:
    api, _factory, controller = fake_api(turntable)

    assert turntable.main(
        ["--json", command, "12.5"], api=api, run_command=healthy_power
    ) == 0
    assert parse_output(capsys)["power"]["motion_allowed"] is True
    assert ("turn_relative", direction, 12.5) in controller.calls


@pytest.mark.parametrize(
    ("target", "expected_move"),
    [
        ("-45", ("turn_relative", "cw", 45.0)),
        ("-10", ("turn_relative", "cw", 10.0)),
        ("0", None),
        ("10", ("turn_relative", "ccw", 10.0)),
        ("45", ("turn_relative", "ccw", 45.0)),
    ],
)
def test_guarded_position_homes_then_moves_in_one_open(
    turntable,
    capsys,
    target: str,
    expected_move,
) -> None:
    api, factory, controller = fake_api(turntable)

    assert turntable.main(
        [
            "--json",
            "position",
            target,
            "--confirm-rig-clear",
            "--confirm-zero-valid",
        ],
        api=api,
        run_command=healthy_power,
    ) == 0

    output = parse_output(capsys)
    assert output["ok"] is True
    assert output["result"]["target_degrees"] == float(target)
    assert factory.open_calls == [{"port": None}]
    expected_calls = [("return_to_zero",)]
    if expected_move is not None:
        expected_calls.append(expected_move)
    assert controller.calls == expected_calls


@pytest.mark.parametrize(
    "argv",
    [
        ["position", "10"],
        ["position", "10", "--confirm-rig-clear"],
        ["position", "10", "--confirm-zero-valid"],
    ],
)
def test_guarded_position_requires_both_confirmations_before_open(
    turntable,
    capsys,
    argv,
) -> None:
    api, factory, _controller = fake_api(turntable)

    def unexpected_power_probe(*args, **kwargs):
        raise AssertionError("missing confirmations must fail before power preflight")

    assert turntable.main(
        ["--json", *argv], api=api, run_command=unexpected_power_probe
    ) == 1
    assert parse_output(capsys)["ok"] is False
    assert factory.open_calls == []


def test_guarded_position_requires_completed_home_before_relative_move(
    turntable,
    capsys,
) -> None:
    api, factory, controller = fake_api(turntable)
    controller.operation_result = SimpleNamespace(
        acknowledged=True,
        completed=False,
    )

    assert turntable.main(
        [
            "--json",
            "position",
            "20",
            "--confirm-rig-clear",
            "--confirm-zero-valid",
        ],
        api=api,
        run_command=healthy_power,
    ) == 1

    output = parse_output(capsys)
    assert output["ok"] is False
    assert "not attempted" in output["error"]
    assert factory.open_calls == [{"port": None}]
    assert controller.calls == [("return_to_zero",)]


def test_guarded_position_requires_completed_target_move(turntable, capsys) -> None:
    api, _factory, controller = fake_api(turntable)

    def incomplete_move(direction: str, degrees: float):
        controller.calls.append(("turn_relative", direction, degrees))
        return SimpleNamespace(acknowledged=True, completed=False)

    controller.turn_relative = incomplete_move

    assert turntable.main(
        [
            "--json",
            "position",
            "20",
            "--confirm-rig-clear",
            "--confirm-zero-valid",
        ],
        api=api,
        run_command=healthy_power,
    ) == 1
    assert parse_output(capsys)["ok"] is False
    assert controller.calls == [
        ("return_to_zero",),
        ("turn_relative", "ccw", 20.0),
    ]


def test_hotplug_stop_exits_after_first_completed_stop(turntable, capsys) -> None:
    api, factory, controller = fake_api(turntable)
    controller.probe_result.product = turntable.AUTOSTOP_PRODUCT

    def unexpected_sleep(_seconds: float) -> None:
        raise AssertionError("successful stop must not retry")

    assert turntable.main(
        [
            "--json",
            "--port",
            "/dev/ttyUSB7",
            "hotplug-stop",
        ],
        api=api,
        sleep=unexpected_sleep,
    ) == 0

    output = parse_output(capsys)
    assert output["event"] == "turntable_autostop.stopped"
    assert output["attempt"] == 1
    assert controller.calls == [("probe",), ("stop",)]
    assert factory.open_calls == [
        {
            "port": "/dev/ttyUSB7",
            "response_timeout": turntable.AUTOSTOP_IO_TIMEOUT,
            "startup_timeout": turntable.AUTOSTOP_IO_TIMEOUT,
        }
    ]


def test_hotplug_stop_retry_budget_is_bounded(turntable, capsys) -> None:
    api, factory, controller = fake_api(turntable)
    controller.probe_result.product = turntable.AUTOSTOP_PRODUCT
    controller.operation_result.completed = False
    sleeps = []

    assert turntable.main(
        ["--json", "--port", "/dev/ttyUSB0", "hotplug-stop"],
        api=api,
        sleep=sleeps.append,
    ) == 1

    output = parse_json_lines(capsys)
    assert [record["event"] for record in output] == [
        "turntable_autostop.retry",
        "turntable_autostop.retry",
        "turntable_autostop.retry",
        "turntable_autostop.exhausted",
    ]
    assert output[-1]["attempts"] == turntable.AUTOSTOP_ATTEMPTS
    assert sleeps == [turntable.AUTOSTOP_RETRY_SECONDS] * 3
    assert len(factory.open_calls) == turntable.AUTOSTOP_ATTEMPTS


def test_hotplug_stop_ignores_other_ch340_product(turntable, capsys) -> None:
    api, factory, controller = fake_api(turntable)
    controller.probe_result.product = "unrelated-device"
    sleeps = []

    assert turntable.main(
        ["--json", "--port", "/dev/ttyUSB2", "hotplug-stop"],
        api=api,
        sleep=sleeps.append,
    ) == 0

    assert parse_output(capsys)["event"] == "turntable_autostop.ignored"
    assert controller.calls == [("probe",)]
    assert len(factory.open_calls) == 1
    assert sleeps == []


def test_hotplug_stop_rejects_non_event_port_before_open(turntable, capsys) -> None:
    api, factory, _controller = fake_api(turntable)

    assert turntable.main(
        ["--json", "--port", "/dev/serial/by-id/not-the-event-tty", "hotplug-stop"],
        api=api,
    ) == 1

    assert parse_output(capsys)["event"] == "turntable_autostop.rejected"
    assert factory.open_calls == []


@pytest.mark.parametrize("command", ["left", "right", "home", "position"])
def test_motion_is_blocked_before_open_when_power_is_unsafe(
    turntable,
    capsys,
    command: str,
) -> None:
    api, factory, _controller = fake_api(turntable)
    argv = ["--json", command, *(["10"] if command != "home" else [])]
    if command == "position":
        argv.extend(["--confirm-rig-clear", "--confirm-zero-valid"])

    assert turntable.main(
        argv,
        api=api,
        run_command=lambda *args, **kwargs: command_result("throttled=0x1\n"),
    ) == 1
    assert parse_output(capsys)["power"]["motion_allowed"] is False
    assert factory.open_calls == []


def test_power_override_allows_motion_when_status_is_unknown(turntable, capsys) -> None:
    api, _factory, controller = fake_api(turntable)

    assert turntable.main(
        ["--json", "--allow-power-risk", "left", "10"],
        api=api,
        run_command=lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    ) == 0
    output = parse_output(capsys)
    assert output["power"]["status"]["available"] is False
    assert output["power"]["override"] is True
    assert ("turn_relative", "cw", 10.0) in controller.calls


@pytest.mark.parametrize(
    ("command", "expected_call"),
    [("set-zero", "set_zero"), ("stop", "stop")],
)
def test_non_motion_commands_skip_power_preflight(
    turntable,
    capsys,
    command: str,
    expected_call: str,
) -> None:
    api, _factory, controller = fake_api(turntable)

    def unexpected_power_probe(*args, **kwargs):
        raise AssertionError("non-motion command must not run power preflight")

    assert turntable.main(
        ["--json", command], api=api, run_command=unexpected_power_probe
    ) == 0
    parse_output(capsys)
    assert (expected_call,) in controller.calls


def test_incomplete_operation_is_not_success(turntable, capsys) -> None:
    api, _factory, controller = fake_api(turntable)
    controller.operation_result.completed = False

    assert turntable.main(
        ["--json", "home"], api=api, run_command=healthy_power
    ) == 1
    assert parse_output(capsys)["ok"] is False


@pytest.mark.parametrize("degrees", ["-45.1", "45.1", "nan", "inf", "-inf"])
def test_guarded_position_rejects_out_of_range_before_open(
    turntable,
    degrees: str,
) -> None:
    api, factory, _controller = fake_api(turntable)

    def unexpected_power_probe(*args, **kwargs):
        raise AssertionError("invalid target must fail before power preflight")

    with pytest.raises(SystemExit) as exc_info:
        turntable.main(
            [
                "position",
                degrees,
                "--confirm-rig-clear",
                "--confirm-zero-valid",
            ],
            api=api,
            run_command=unexpected_power_probe,
        )
    assert exc_info.value.code == 2
    assert factory.open_calls == []


@pytest.mark.parametrize("degrees", ["0", "-1", "nan", "inf"])
def test_relative_turn_rejects_invalid_degrees(turntable, degrees: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        turntable.build_parser().parse_args(["left", degrees])
    assert exc_info.value.code == 2


def test_docs_keep_manual_safety_and_provenance_boundaries() -> None:
    readme = " ".join((EXPERIMENT / "README.md").read_text().split())
    vendor_readme = " ".join((VENDOR / "README.md").read_text().split())

    assert "not a physical emergency stop" in readme
    assert "hardware power cutoff" in readme
    assert "does not cancel or stop platform motion" in readme
    assert "`-45` to `+45` degree envelope" in readme
    assert "always finish by commanding position `0`" in readme
    assert "Zero persistence across a controller power cycle is unverified" in readme
    assert "There is no timer or resident process" in readme
    assert "turntable_autostop.stopped" in readme
    assert "development-time provenance" in vendor_readme
    assert "does not authenticate files at runtime" in vendor_readme


def test_turntable_product_surface_is_only_the_hotplug_stop_hook() -> None:
    markers = (
        "usb-turntable",
        "usb_turntable",
        "jts_turntable",
        "turntable-autostop",
    )
    files = [ROOT / "pyproject.toml"]
    for root in (ROOT / "deploy", ROOT / "jasper"):
        files.extend(path for path in root.rglob("*") if path.is_file())

    def has_marker(path: Path) -> bool:
        searchable = path.relative_to(ROOT).as_posix() + path.read_text(errors="ignore")
        return any(marker in searchable for marker in markers)

    matches = {path.relative_to(ROOT).as_posix() for path in files if has_marker(path)}
    assert matches == {
        "deploy/lib/install/python-runtime.sh",
        "deploy/lib/install/systemd-units.sh",
        "deploy/systemd/jasper-turntable-autostop@.service",
        "deploy/udev/99-jasper-turntable-autostop.rules",
    }


def test_hotplug_stop_udev_systemd_and_install_wiring() -> None:
    rule = AUTOSTOP_RULE.read_text()
    unit = AUTOSTOP_UNIT.read_text()
    units_install = (ROOT / "deploy/lib/install/systemd-units.sh").read_text()
    runtime_install = (ROOT / "deploy/lib/install/python-runtime.sh").read_text()

    assert 'ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523"' in rule
    assert 'KERNEL=="ttyUSB*"' in rule
    assert 'SYSTEMD_WANTS}+="jasper-turntable-autostop@%k.service"' in rule
    assert "BindsTo=dev-%i.device" in unit
    assert "ConditionPathExists=/dev/%I" in unit
    assert "ExecStart=/usr/bin/python3 /opt/jasper/experiments/usb-turntable/" in unit
    assert "--port /dev/%I --json hotplug-stop" in unit
    assert "/bin/sh" not in unit
    assert "TimeoutStartSec=40s" in unit
    assert "DeviceAllow=/dev/%I rw" in unit
    assert "jasper-turntable-autostop@.service" in units_install
    assert "99-jasper-turntable-autostop.rules" in units_install
    assert '"${REPO_DIR}/experiments/usb-turntable"' in runtime_install

    streambox_units = units_install.split(
        "install_streambox_systemd_units() {", 1
    )[1].split("\n}\n\ninstall_systemd_units()", 1)[0]
    assert "turntable-autostop" not in streambox_units

    full_runtime = runtime_install.split("install_jasper() {", 1)[1].split(
        "\n}\n\ninstall_streambox_jasper()", 1
    )[0]
    streambox_runtime = runtime_install.split("install_streambox_jasper() {", 1)[1]
    assert "experiments/usb-turntable" in full_runtime
    assert "experiments/usb-turntable" not in streambox_runtime


def test_jts_adapter_contains_no_serial_protocol() -> None:
    tree = ast.parse(SCRIPT.read_text())
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

    assert not any(name == "serial" or name.startswith("serial.") for name in imported_modules)
    assert not any(
        (value.decode(errors="ignore") if isinstance(value, bytes) else value).startswith("CT")
        for value in constants
    )
