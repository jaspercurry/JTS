# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hardware-free coverage for the manual USB-turntable experiment."""

import argparse
import ast
import hashlib
import importlib.util
import json
import subprocess
import sys
import tomllib
from decimal import Decimal
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


def test_help_lists_internal_hotplug_stop_without_argparse_placeholder(turntable):
    help_text = turntable.build_parser().format_help()

    assert "hotplug-stop" in help_text
    assert argparse.SUPPRESS not in help_text


def test_json_flag_is_accepted_before_or_after_the_subcommand(turntable):
    """``--json`` after the subcommand used to be an argparse "unrecognized
    arguments" error, easy to misread as a tool fault mid-incident."""
    parser = turntable.build_parser()

    assert parser.parse_args(["--json", "left", "5"]).json is True
    assert parser.parse_args(["left", "5", "--json"]).json is True
    assert parser.parse_args(["left", "5"]).json is False


class FakeProtocolError(Exception):
    """Stand-in for the vendored ``usb_turntable.ProtocolError``.

    Injected via ``TurntableApi.protocol_error`` so retry tests never need
    to import the real vendor package; the wrapper only needs *a* type to
    catch, and dependency-injecting it is the same pattern the rest of this
    fixture module already uses for ``discover_devices``/``controller``.
    """


def command_result(stdout: str, *, returncode: int = 0, stderr: str = ""):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def make_queue(*outcomes):
    """Return a callable that pops through ``outcomes`` in call order.

    Each outcome is either an exception instance (raised) or a plain value
    (returned). Calling past the end of the queue is a test-authoring bug,
    not a code-under-test outcome, so it raises loudly. Invocation count is
    tracked on ``.call_count`` for tests that don't otherwise observe calls.
    """

    remaining = list(outcomes)

    def _next(*_args, **_kwargs):
        if not remaining:
            raise AssertionError("queued fake invoked more times than expected")
        _next.call_count += 1
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    _next.call_count = 0
    return _next


def record_and_queue(controller, name, *outcomes):
    """Like ``make_queue``, but also appends ``(name, *args)`` to
    ``controller.calls`` per invocation, matching ``FakeController``'s own
    recording convention. Needed when overriding one method mid-test so
    ordering assertions spanning overridden and un-overridden methods
    still hold.
    """

    queue = make_queue(*outcomes)

    def _wrapped(*args, **kwargs):
        controller.calls.append((name, *args))
        return queue(*args, **kwargs)

    return _wrapped


def healthy_power(*args, **kwargs):
    return command_result("throttled=0x0\n")


class FakeController:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.probe_result = SimpleNamespace(connected=True, firmware="1.2.3")
        self.operation_result = SimpleNamespace(acknowledged=True, completed=True)
        self.offset_result = SimpleNamespace(
            acknowledged=True, degrees=Decimal("0.0"), frames=("OA=0.0\N{DEGREE SIGN}",)
        )
        self.close_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close_calls += 1

    def probe(self):
        self.calls.append(("probe",))
        return self.probe_result

    def turn_relative(self, direction: str, degrees: float):
        self.calls.append(("turn_relative", direction, degrees))
        return self.operation_result

    def set_zero(self):
        self.calls.append(("set_zero",))
        return self.operation_result

    def offset_angle(self):
        self.calls.append(("offset_angle",))
        return self.offset_result

    def return_to_zero(self):
        self.calls.append(("return_to_zero",))
        return self.operation_result

    def stop(self):
        self.calls.append(("stop",))
        return self.operation_result


class FakeControllerFactory:
    """Stand-in for ``TurntableApi.controller``.

    Accepts either one ``FakeController`` (the default -- every ``.open()``
    call returns the same instance, matching every pre-#2516 test's
    assumption) or a sequence of them (one per ``.open()`` call, holding
    the last if called more times than scripted) -- used by the session-
    retry tests to prove the wrapper opens a genuinely DIFFERENT controller
    on retry, mirroring a fresh CLI invocation, not the same one reused. An
    exception instance in the sequence is RAISED from ``.open()`` instead
    of returned (same value-or-exception convention as ``make_queue``) --
    used to prove a second-attempt ``.open()`` failure (not just an
    ``operation()`` failure) still surfaces as a retried failure.
    """

    def __init__(
        self, controller: "FakeController | list[FakeController | BaseException]"
    ) -> None:
        self._controllers = (
            controller if isinstance(controller, list) else [controller]
        )
        self.instance = self._controllers[0]
        self.open_calls: list[dict[str, object]] = []

    def open(self, **kwargs):
        self.open_calls.append(kwargs)
        index = min(len(self.open_calls) - 1, len(self._controllers) - 1)
        item = self._controllers[index]
        if isinstance(item, BaseException):
            raise item
        return item


def fake_api(turntable, *, devices=None):
    controller = FakeController()
    factory = FakeControllerFactory(controller)
    api = turntable.TurntableApi(
        discover_devices=lambda: list(devices or []),
        controller=factory,
        protocol_error=FakeProtocolError,
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
        "turntable_autostop.retry"
    ] * (turntable.AUTOSTOP_ATTEMPTS - 1) + ["turntable_autostop.exhausted"]
    assert output[-1]["attempts"] == turntable.AUTOSTOP_ATTEMPTS
    assert sleeps == [turntable.AUTOSTOP_RETRY_SECONDS] * (turntable.AUTOSTOP_ATTEMPTS - 1)
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
    ("argv", "expected_call"),
    [
        (["set-zero", "--confirm-redefine-zero"], "set_zero"),
        (["stop"], "stop"),
    ],
)
def test_non_motion_commands_skip_power_preflight(
    turntable,
    capsys,
    argv: list[str],
    expected_call: str,
) -> None:
    api, _factory, controller = fake_api(turntable)

    def unexpected_power_probe(*args, **kwargs):
        raise AssertionError("non-motion command must not run power preflight")

    assert turntable.main(
        ["--json", *argv], api=api, run_command=unexpected_power_probe
    ) == 0
    parse_output(capsys)
    assert (expected_call,) in controller.calls


def test_set_zero_without_confirm_flag_is_rejected_before_open(
    turntable, capsys
) -> None:
    api, factory, _controller = fake_api(turntable)

    def unexpected_power_probe(*args, **kwargs):
        raise AssertionError("missing confirmation must fail before power preflight")

    with pytest.raises(SystemExit) as exc_info:
        turntable.main(
            ["--json", "set-zero"], api=api, run_command=unexpected_power_probe
        )
    assert exc_info.value.code == 2
    assert factory.open_calls == []
    # No parseable "soft failure" on stdout: an automated caller that only
    # checks stdout for {"ok": false} must not be able to mistake this
    # destructive-write refusal for an ordinary operational failure.
    assert capsys.readouterr().out == ""


def test_set_zero_direct_run_call_is_guarded_without_argparse(
    turntable, capsys
) -> None:
    """A bare ``run()`` call (bypassing argparse) is guarded too, not just the CLI."""
    api, factory, _controller = fake_api(turntable)
    namespace = argparse.Namespace(
        command="set-zero", json=True, port=None, allow_power_risk=False
    )

    with pytest.raises(SystemExit) as exc_info:
        turntable.run(namespace, api=api)
    assert exc_info.value.code == 2
    assert factory.open_calls == []
    assert capsys.readouterr().out == ""


def test_set_zero_with_confirm_flag_redefines_zero(turntable, capsys) -> None:
    api, factory, controller = fake_api(turntable)

    def unexpected_power_probe(*args, **kwargs):
        raise AssertionError("set-zero must not run the power preflight")

    assert turntable.main(
        ["--json", "set-zero", "--confirm-redefine-zero"],
        api=api,
        run_command=unexpected_power_probe,
    ) == 0
    assert parse_output(capsys)["ok"] is True
    assert factory.open_calls == [{"port": None}]
    assert controller.calls == [("set_zero",)]


def test_offset_reads_without_motion_or_power_preflight(turntable, capsys) -> None:
    api, factory, controller = fake_api(turntable)
    controller.offset_result = SimpleNamespace(
        acknowledged=True, degrees=Decimal("0.5"), frames=("OA=0.5\N{DEGREE SIGN}",)
    )

    def unexpected_power_probe(*args, **kwargs):
        raise AssertionError("offset must not run the power preflight")

    assert turntable.main(
        ["--json", "offset"], api=api, run_command=unexpected_power_probe
    ) == 0

    output = parse_output(capsys)
    assert output["ok"] is True
    assert output["result"]["offset_degrees"] == 0.5
    assert output["result"]["frames"] == ["OA=0.5\N{DEGREE SIGN}"]
    assert factory.open_calls == [{"port": None}]
    assert controller.calls == [("offset_angle",)]


# --- The travel envelope: one cap, checked at the move's endpoint ----------
#
# `left`/`right` were uncapped relative moves: nothing stopped a typo from
# driving the arm into the rig and wrapping its cable -- component damage,
# not a bad measurement. Each now predicts the offset from saved zero it
# would end at and refuses to leave the envelope, with one exception so a
# stranded platform can always be walked back in. No override exists.


def seed_offset(controller, degrees: str) -> None:
    """Set the controller's believed offset from saved zero."""

    controller.offset_result = SimpleNamespace(
        acknowledged=True,
        degrees=Decimal(degrees),
        frames=(f"OA={degrees}\N{DEGREE SIGN}",),
    )


def test_the_travel_envelope_is_one_constant_the_whole_tool_derives_from(
    turntable,
) -> None:
    """Changing this number is an owner ruling, not a runtime decision.

    `position`'s argument bound and the `left`/`right` endpoint gate both
    derive from it, so they can never disagree about how far the arm goes.
    """
    assert turntable.TRAVEL_ENVELOPE_DEGREES == 45.0
    assert turntable.MEASUREMENT_MAX_DEGREES == turntable.TRAVEL_ENVELOPE_DEGREES
    assert turntable.MEASUREMENT_MIN_DEGREES == -turntable.TRAVEL_ENVELOPE_DEGREES


@pytest.mark.parametrize(
    ("command", "degrees", "seed", "endpoint", "allowed"),
    [
        ("left", "10", "0.0", 10.0, True),
        ("right", "10", "0.0", -10.0, True),
        # Inclusive, matching `position`'s argument bound.
        ("left", "45", "0.0", 45.0, True),
        ("right", "45", "0.0", -45.0, True),
        ("left", "45.1", "0.0", 45.1, False),
        # A legal-looking nudge that lands past the cap only because of
        # where the platform already is -- why the gate checks the
        # ENDPOINT and not the requested magnitude.
        ("left", "10", "40.0", 50.0, False),
        ("right", "10", "-40.0", -50.0, False),
        # Today's literal rig state: jts3 is parked at -80.87, outside the
        # envelope. Verified on hardware -- vendor `left` INCREASES the
        # offset reading (`left 5` moved -80.87 to -75.87) -- so from a
        # negative offset `left` recovers inward and `right` drives out.
        ("left", "5", "-80.87", -75.87, True),
        ("right", "5", "-80.87", -85.87, False),
        # The same recovery, mirrored past the other end.
        ("right", "5", "80.87", 75.87, True),
        ("left", "5", "80.87", 85.87, False),
        # Inward but not all the way back inside: still allowed. Recovery
        # is a direction, not a single jump.
        ("left", "1", "-80.87", -79.87, True),
        # Inward far enough to cross zero and land outside the FAR side.
        # It DOES end nearer saved zero (49.13 < 80.87), so a rule written
        # on distance alone admits it -- a full swing through the envelope
        # and back out. Recovery must also stay on its own side of zero.
        ("left", "130", "-80.87", 49.13, False),
        ("right", "130", "80.87", -49.13, False),
    ],
)
def test_relative_moves_are_gated_at_their_predicted_endpoint(
    turntable,
    capsys,
    command: str,
    degrees: str,
    seed: str,
    endpoint: float,
    allowed: bool,
) -> None:
    api, factory, controller = fake_api(turntable)
    seed_offset(controller, seed)

    exit_code = turntable.main(
        ["--json", command, degrees], api=api, run_command=healthy_power
    )
    output = parse_output(capsys)
    moved = [call for call in controller.calls if call[0] == "turn_relative"]

    assert exit_code == (0 if allowed else 1)
    assert output["ok"] is allowed
    # One session either way: the gate reads the offset in the very session
    # that would issue the motion, so the belief it decides on is the one
    # the controller holds immediately before the move -- and no retry.
    assert factory.open_calls == [{"port": None}]
    assert controller.calls[0] == ("offset_angle",)
    if allowed:
        assert len(moved) == 1
        assert "reason" not in output
    else:
        assert moved == []
        assert output["reason"] == turntable.TRAVEL_ENVELOPE_EXCEEDED
        assert output["predicted_offset_degrees"] == pytest.approx(endpoint)
        assert output["current_offset_degrees"] == pytest.approx(float(seed))
        assert output["travel_envelope_degrees"] == turntable.TRAVEL_ENVELOPE_DEGREES


@pytest.mark.parametrize(
    ("seed", "inward"),
    [("-80.87", "left"), ("80.87", "right"), ("0.0", None)],
)
def test_a_refusal_names_the_one_direction_that_is_still_legal(
    turntable, capsys, seed: str, inward: str | None
) -> None:
    """Stranded outside the envelope, exactly one direction recovers.
    Inside it none is privileged -- a smaller move or `home` -- so the
    field is null rather than naming a guess.
    """
    api, _factory, controller = fake_api(turntable)
    seed_offset(controller, seed)

    assert (
        turntable.main(["--json", "left", "200"], api=api, run_command=healthy_power)
        == 1
    )
    assert parse_output(capsys)["inward_command"] == inward


def test_the_envelope_has_no_override(turntable, capsys) -> None:
    """The tool's only motion override covers the Pi's supply, never the
    hardware-protection cap. No other flag, environment variable, or
    config widens it either -- by design.
    """
    api, _factory, controller = fake_api(turntable)
    seed_offset(controller, "-80.87")

    assert (
        turntable.main(
            ["--json", "--allow-power-risk", "right", "5"],
            api=api,
            run_command=healthy_power,
        )
        == 1
    )
    output = parse_output(capsys)
    assert output["reason"] == turntable.TRAVEL_ENVELOPE_EXCEEDED
    assert output["power"]["override"] is True
    assert ("turn_relative", "ccw", 5.0) not in controller.calls


@pytest.mark.parametrize(
    "reading",
    [
        SimpleNamespace(acknowledged=False, degrees=Decimal("0.0"), frames=()),
        SimpleNamespace(acknowledged=True, degrees=None, frames=()),
        SimpleNamespace(acknowledged=True, degrees="not a number", frames=()),
        # NaN is the dangerous one: every comparison against it is false,
        # so an unguarded gate would wave it straight past the cap.
        SimpleNamespace(acknowledged=True, degrees=float("nan"), frames=()),
        SimpleNamespace(acknowledged=True, degrees=float("inf"), frames=()),
    ],
)
def test_an_unusable_offset_reading_refuses_the_move(
    turntable, capsys, reading
) -> None:
    api, _factory, controller = fake_api(turntable)
    controller.offset_result = reading

    assert (
        turntable.main(["--json", "left", "5"], api=api, run_command=healthy_power) == 1
    )
    output = parse_output(capsys)
    assert output["reason"] == turntable.TRAVEL_OFFSET_UNREADABLE
    assert controller.calls == [("offset_angle",)]


def test_a_failed_offset_query_sends_no_motion(turntable, capsys) -> None:
    """Same posture as the power preflight: if the envelope cannot be
    checked, nothing moves. The read is not retried -- `left`/`right` are
    zero-retry by contract.
    """
    api, factory, controller = fake_api(turntable)
    controller.offset_angle = record_and_queue(
        controller,
        "offset_angle",
        FakeProtocolError("heartbeat byte appeared inside a protocol frame"),
    )

    assert (
        turntable.main(["--json", "left", "5"], api=api, run_command=healthy_power) == 1
    )
    output = parse_output(capsys)
    assert output["ok"] is False
    assert "retried" not in output
    assert controller.calls == [("offset_angle",)]
    assert factory.open_calls == [{"port": None}]


@pytest.mark.parametrize(
    ("argv", "expected_calls"),
    [
        # `home` ends at zero, inside every envelope.
        (["home"], [("return_to_zero",)]),
        # `stop` is what an operator reaches for when something is already
        # wrong -- it must never need a readable offset to work.
        (["stop"], [("stop",)]),
    ],
)
def test_home_and_stop_are_never_gated_even_from_outside_the_envelope(
    turntable, capsys, argv: list[str], expected_calls
) -> None:
    api, _factory, controller = fake_api(turntable)
    seed_offset(controller, "-80.87")

    assert turntable.main(["--json", *argv], api=api, run_command=healthy_power) == 0
    assert parse_output(capsys)["ok"] is True
    # No offset read at all -- these commands do not consult the envelope.
    assert controller.calls == expected_calls


# --- Issue #2516: one bounded retry on the vendored ProtocolError -----------
#
# Fix round (adversarial gate on PR #2524): the vendored parser's own
# `_prepare_command` fails closed on the very next command after the race
# (its FrameParser buffer is left non-empty), so an IN-SESSION retry cannot
# recover -- only a fresh controller session (fresh `.open()`, fresh
# parser) does, mirroring a brand-new CLI invocation. Retry eligibility is
# also the EXACT `ProtocolError` base class only: `CompletionTimeout`,
# `CommandRejected`, `CommunicationTimeout`, and
# `StartupSynchronizationError` are subclasses that report a REAL command
# outcome and must never be retried. `offset`, `probe`, and the guarded
# `position` retry the whole operation exactly once against a fresh
# session; `detect` never touches the serial link so it can't raise this
# error at all; everything else stays zero-retry. The tests below cover
# wrapper-level orchestration with a fake exception type; a later section
# exercises the same retry path against the REAL vendored parser.


def test_probe_retries_once_via_a_fresh_session_and_succeeds(turntable, capsys) -> None:
    api, factory, controller = fake_api(turntable)
    controller.probe = make_queue(
        FakeProtocolError("heartbeat byte appeared inside a protocol frame"),
        controller.probe_result,
    )

    assert turntable.main(["--json", "probe"], api=api) == 0

    output = parse_output(capsys)
    assert output["ok"] is True
    assert output["retried"] is True
    assert controller.probe.call_count == 2
    # .open() was called twice -- one fresh session per attempt.
    assert factory.open_calls == [{"port": None}, {"port": None}]


def test_offset_retries_once_via_a_fresh_session_and_succeeds(turntable, capsys) -> None:
    api, factory, controller = fake_api(turntable)
    controller.offset_angle = make_queue(
        FakeProtocolError("heartbeat byte appeared inside a protocol frame"),
        controller.offset_result,
    )

    assert turntable.main(["--json", "offset"], api=api) == 0

    output = parse_output(capsys)
    assert output["ok"] is True
    assert output["retried"] is True
    assert controller.offset_angle.call_count == 2
    assert factory.open_calls == [{"port": None}, {"port": None}]


def test_retry_opens_a_genuinely_different_controller_and_closes_the_first(
    turntable, capsys
) -> None:
    """The retry must open a NEW session object, not reuse the failed one.

    Uses a two-controller factory directly (bypassing the single-instance
    ``fake_api()`` helper) so the two attempts are provably different
    objects -- the closest a fake-based test can get to proving "fresh
    session" without the real vendored parser (see the real-parser section
    below for that proof).
    """
    first_controller = FakeController()
    first_controller.probe = record_and_queue(
        first_controller,
        "probe",
        FakeProtocolError("heartbeat byte appeared inside a protocol frame"),
    )
    second_controller = FakeController()
    factory = FakeControllerFactory([first_controller, second_controller])
    api = turntable.TurntableApi(
        discover_devices=lambda: [],
        controller=factory,
        protocol_error=FakeProtocolError,
    )

    assert turntable.main(["--json", "probe"], api=api) == 0

    output = parse_output(capsys)
    assert output["ok"] is True
    assert output["retried"] is True
    assert factory.open_calls == [{"port": None}, {"port": None}]
    # Attempt 1 raced and was torn down; only attempt 2 actually recorded
    # a successful probe call, on a genuinely different controller object.
    assert first_controller.calls == [("probe",)]
    assert first_controller.close_calls == 1
    assert second_controller.calls == [("probe",)]


def test_detect_is_excluded_from_retryable_commands(turntable) -> None:
    """Discovery never touches the serial link -- it can't raise the race."""
    assert "detect" not in turntable.RETRYABLE_COMMANDS


def test_retryable_commands_are_exactly_offset_probe_position(turntable) -> None:
    assert turntable.RETRYABLE_COMMANDS == {"offset", "probe", "position"}


def test_guarded_position_retries_whole_operation_when_home_races(
    turntable, capsys
) -> None:
    """A ProtocolError during home re-runs home *and* the move on retry."""
    api, factory, controller = fake_api(turntable)
    controller.return_to_zero = record_and_queue(
        controller,
        "return_to_zero",
        FakeProtocolError("heartbeat byte appeared inside a protocol frame"),
        controller.operation_result,
    )

    assert (
        turntable.main(
            [
                "--json",
                "position",
                "10",
                "--confirm-rig-clear",
                "--confirm-zero-valid",
            ],
            api=api,
            run_command=healthy_power,
        )
        == 0
    )

    output = parse_output(capsys)
    assert output["ok"] is True
    assert output["retried"] is True
    # Two fresh-session opens: attempt 1 (raced) and attempt 2 (recovered).
    assert factory.open_calls == [{"port": None}, {"port": None}]
    # Attempt 1's home races and is retried; attempt 2 re-homes cleanly
    # before the single move.
    assert controller.calls == [
        ("return_to_zero",),
        ("return_to_zero",),
        ("turn_relative", "ccw", 10.0),
    ]


def test_guarded_position_retries_whole_operation_when_move_races(
    turntable, capsys
) -> None:
    """A ProtocolError during the move re-homes on retry -- it cannot double-move."""
    api, factory, controller = fake_api(turntable)
    controller.turn_relative = record_and_queue(
        controller,
        "turn_relative",
        FakeProtocolError("heartbeat byte appeared inside a protocol frame"),
        controller.operation_result,
    )

    assert (
        turntable.main(
            [
                "--json",
                "position",
                "10",
                "--confirm-rig-clear",
                "--confirm-zero-valid",
            ],
            api=api,
            run_command=healthy_power,
        )
        == 0
    )

    output = parse_output(capsys)
    assert output["ok"] is True
    assert output["retried"] is True
    assert factory.open_calls == [{"port": None}, {"port": None}]
    # Attempt 1: home succeeds, then the move races. Attempt 2 re-homes
    # from scratch before moving again -- the guard that makes the whole
    # operation safe to retry (a partially-completed attempt 1 is undone
    # by attempt 2's fresh home, so the arm can't be double-moved).
    assert controller.calls == [
        ("return_to_zero",),
        ("turn_relative", "ccw", 10.0),
        ("return_to_zero",),
        ("turn_relative", "ccw", 10.0),
    ]


def test_guarded_position_reports_retried_on_home_incomplete_early_return(
    turntable, capsys
) -> None:
    api, factory, controller = fake_api(turntable)
    incomplete = SimpleNamespace(acknowledged=True, completed=False)
    controller.return_to_zero = record_and_queue(
        controller,
        "return_to_zero",
        FakeProtocolError("heartbeat byte appeared inside a protocol frame"),
        incomplete,
    )

    assert (
        turntable.main(
            [
                "--json",
                "position",
                "10",
                "--confirm-rig-clear",
                "--confirm-zero-valid",
            ],
            api=api,
            run_command=healthy_power,
        )
        == 1
    )

    output = parse_output(capsys)
    assert output["ok"] is False
    assert output["retried"] is True
    assert "not attempted" in output["error"]
    assert controller.calls == [("return_to_zero",), ("return_to_zero",)]
    assert factory.open_calls == [{"port": None}, {"port": None}]


def test_retry_does_not_fire_for_non_retryable_commands(turntable, capsys) -> None:
    """set-zero is owner-only and destructive -- it stays zero-retry."""
    api, factory, controller = fake_api(turntable)
    controller.set_zero = record_and_queue(
        controller,
        "set_zero",
        FakeProtocolError("heartbeat byte appeared inside a protocol frame"),
        controller.operation_result,
    )

    assert (
        turntable.main(
            ["--json", "set-zero", "--confirm-redefine-zero"],
            api=api,
        )
        == 1
    )

    output = parse_output(capsys)
    assert output["ok"] is False
    assert "retried" not in output
    assert output["error_type"] == "FakeProtocolError"
    assert controller.calls == [("set_zero",)]
    # Exactly one session -- this is the mutation-detecting assertion: if a
    # future change ever routes "set-zero" through RETRYABLE_COMMANDS, the
    # real dispatch gate in run() sends it through the fresh-session retry
    # helper, .open() gets called twice, and this line fails.
    assert factory.open_calls == [{"port": None}]


@pytest.mark.parametrize(
    ("argv", "expected_call"),
    [
        (["left", "10"], "turn_relative"),
        (["home"], "return_to_zero"),
        (["stop"], "stop"),
    ],
)
def test_retry_does_not_fire_for_unguarded_motion_commands(
    turntable, capsys, argv: list[str], expected_call: str
) -> None:
    api, factory, controller = fake_api(turntable)
    error = FakeProtocolError("heartbeat byte appeared inside a protocol frame")
    setattr(
        controller,
        expected_call,
        record_and_queue(controller, expected_call, error, controller.operation_result),
    )

    assert (
        turntable.main(["--json", *argv], api=api, run_command=healthy_power) == 1
    )

    output = parse_output(capsys)
    assert output["ok"] is False
    assert "retried" not in output
    # The failing call happened exactly once -- counted by name rather than
    # by total length because `left` legitimately reads the offset first to
    # gate its endpoint against the travel envelope.
    assert [call[0] for call in controller.calls].count(expected_call) == 1
    assert factory.open_calls == [{"port": None}]


def test_second_protocol_error_propagates_with_both_attempts_visible(
    turntable, capsys
) -> None:
    api, factory, controller = fake_api(turntable)
    first = FakeProtocolError("heartbeat byte appeared inside a protocol frame")
    second = FakeProtocolError("forbidden control byte 0x1b in protocol stream")
    controller.probe = make_queue(first, second)

    assert turntable.main(["--json", "probe"], api=api) == 1

    output = parse_output(capsys)
    assert output["ok"] is False
    assert output["retried"] is True
    assert output["error_type"] == "FakeProtocolError"
    assert "after one retry" in output["error"]
    assert str(first) in output["error"]
    assert str(second) in output["error"]
    assert controller.probe.call_count == 2
    assert factory.open_calls == [{"port": None}, {"port": None}]


def test_non_protocol_error_never_triggers_retry(turntable, capsys) -> None:
    api, factory, controller = fake_api(turntable)

    def _raise(*_args, **_kwargs):
        controller.calls.append(("probe",))
        raise RuntimeError("some other transport failure")

    controller.probe = _raise

    assert turntable.main(["--json", "probe"], api=api) == 1

    output = parse_output(capsys)
    assert output["ok"] is False
    assert "retried" not in output
    assert output["error_type"] == "RuntimeError"
    assert "after one retry" not in output["error"]
    assert controller.calls == [("probe",)]
    assert factory.open_calls == [{"port": None}]


class _FakeProtocolErrorSubclass(FakeProtocolError):
    """Stand-in for a real subclass like CompletionTimeout/CommandRejected."""


def test_protocol_error_subclass_is_never_retried(turntable, capsys) -> None:
    """A subclass instance must propagate on the first attempt, no retry.

    This is the fake-level version of fix-round blocker 2's proof
    (CompletionTimeout must never be silently re-driven); the real-parser
    section below repeats it with the actual vendored exception classes.
    """
    api, factory, controller = fake_api(turntable)
    controller.turn_relative = record_and_queue(
        controller,
        "turn_relative",
        _FakeProtocolErrorSubclass("acknowledged but no TB_END arrived"),
        controller.operation_result,
    )

    assert (
        turntable.main(
            [
                "--json",
                "position",
                "10",
                "--confirm-rig-clear",
                "--confirm-zero-valid",
            ],
            api=api,
            run_command=healthy_power,
        )
        == 1
    )

    output = parse_output(capsys)
    assert output["ok"] is False
    assert "retried" not in output
    assert output["error_type"] == "_FakeProtocolErrorSubclass"
    # Exactly one home + one move -- the subclass failure must not trigger
    # a second, silently re-issued motion command.
    assert controller.calls == [
        ("return_to_zero",),
        ("turn_relative", "ccw", 10.0),
    ]
    assert factory.open_calls == [{"port": None}]


class _UnrelatedSecondAttemptFailure(Exception):
    """Stand-in for a real StartupSynchronizationError from the fresh open."""


def test_second_attempt_open_failure_of_a_different_type_is_still_reported_as_retried(
    turntable, capsys
) -> None:
    """Gate's delta probe (second round): attempt 1 races AFTER issuing
    both return_to_zero and turn_relative; the fresh ``.open()`` on
    attempt 2 then fails with a DIFFERENT exception type entirely (e.g. a
    real ``StartupSynchronizationError`` if the second session genuinely
    can't synchronize). This must still surface as a retried failure with
    both attempts' errors visible -- attempt 1 may already have moved the
    platform, so silently reporting only the second failure would be
    indistinguishable from a cold failure where nothing moved, which is
    exactly what the README tells the operator to check before retrying
    by hand.
    """
    controller = FakeController()
    controller.turn_relative = record_and_queue(
        controller,
        "turn_relative",
        FakeProtocolError("heartbeat byte appeared inside a protocol frame"),
    )
    # The SECOND .open() call itself raises -- a different exception type
    # than the FakeProtocolError that triggered the retry, and not even a
    # subclass of it.
    factory = FakeControllerFactory(
        [controller, _UnrelatedSecondAttemptFailure("startup did not become synchronized")]
    )
    api = turntable.TurntableApi(
        discover_devices=lambda: [],
        controller=factory,
        protocol_error=FakeProtocolError,
    )

    assert (
        turntable.main(
            [
                "--json",
                "position",
                "10",
                "--confirm-rig-clear",
                "--confirm-zero-valid",
            ],
            api=api,
            run_command=healthy_power,
        )
        == 1
    )

    output = parse_output(capsys)
    assert output["ok"] is False
    assert output["retried"] is True
    assert output["error_type"] == "_UnrelatedSecondAttemptFailure"
    assert "startup did not become synchronized" in output["error"]
    assert "after one retry" in output["error"]
    assert "heartbeat byte appeared inside a protocol frame" in output["error"]
    # Attempt 1 fully ran home + move before it raced -- that's the fact
    # the operator needs, and it must not be silently dropped.
    assert controller.calls == [
        ("return_to_zero",),
        ("turn_relative", "ccw", 10.0),
    ]
    assert factory.open_calls == [{"port": None}, {"port": None}]


def test_should_fix_3_vendor_style_cause_chaining_never_implies_a_retry(
    turntable, capsys
) -> None:
    """Regression for fix-round should-fix 3.

    The vendored package chains exceptions internally with `raise ... from
    exc` in several places unrelated to this wrapper's own retry (e.g.
    ProtocolSession.synchronize re-raising a parse error as
    StartupSynchronizationError). A never-retryable command (`left`) that
    fails with a chained exception must NOT be reported as retried --
    that fact must come only from an actual `_RetryExhausted`, never from
    inspecting `exc.__cause__`.
    """
    api, factory, controller = fake_api(turntable)

    def _raise_with_vendor_style_cause(*_args, **_kwargs):
        controller.calls.append(("turn_relative", "cw", 10.0))
        try:
            raise ValueError("some unrelated internal vendor parse failure")
        except ValueError as inner:
            raise RuntimeError("link did not return to a normal heartbeat") from inner

    controller.turn_relative = _raise_with_vendor_style_cause

    assert (
        turntable.main(["--json", "left", "10"], api=api, run_command=healthy_power)
        == 1
    )

    output = parse_output(capsys)
    assert output["ok"] is False
    assert "retried" not in output
    assert "after one retry" not in output["error"]
    assert output["error_type"] == "RuntimeError"
    # One envelope-gate read, one motion attempt, no second of either.
    assert controller.calls == [("offset_angle",), ("turn_relative", "cw", 10.0)]
    assert factory.open_calls == [{"port": None}]


# --- Fix round: exercised against the REAL vendored parser -----------------
#
# The adversarial gate's core criticism of the first round was that its
# retry was validated only against a fake exception type, and its in-session
# design was never checked against how the vendor's own FrameParser and
# ProtocolSession actually behave after the race. These tests build REAL
# `usb_turntable.protocol.ProtocolSession` / `FrameParser` /
# `usb_turntable.controller.TurntableController` instances (no mocking of
# vendor internals) backed by a scripted `Transport` (the same `Transport`
# Protocol the vendor's `ProtocolSession` accepts), so every claim below is
# checked against the actual shipped parser, not a stand-in for it.


def _vendor_protocol_modules():
    """Import the REAL vendored protocol/controller modules for these tests."""
    vendor_path = str(VENDOR)
    if vendor_path not in sys.path:
        sys.path.insert(0, vendor_path)
    from usb_turntable import commands as vendor_commands
    from usb_turntable import controller as vendor_controller
    from usb_turntable import errors as vendor_errors
    from usb_turntable import protocol as vendor_protocol

    return vendor_protocol, vendor_errors, vendor_commands, vendor_controller


class _FakeMonotonicClock:
    """Advances synthetic time on every call -- no real sleeping, no flakes.

    The vendored ``ProtocolSession`` polls ``monotonic()`` many times per
    exchange (pacing, settle waits, timeouts); a small fixed step lets every
    real deadline (``SETTLE_SECONDS``, ``response_timeout``,
    ``motion_timeout``) elapse deterministically in a handful of calls.
    """

    def __init__(self, start: float = 1_000.0, step: float = 0.05) -> None:
        self._now = start
        self._step = step

    def __call__(self) -> float:
        self._now += self._step
        return self._now


class _ScriptedTransport:
    """A real ``Transport`` (see ``protocol.Transport``) that only releases
    a command's scripted response bytes AFTER that command is written --
    otherwise the vendor's own pre-command quiescence check
    (``_prepare_command``) would drain the next command's response before
    the current command was even sent.
    """

    def __init__(self, response_batches: list[list[bytes]]) -> None:
        self._batches = list(response_batches)
        self._pending: list[bytes] = []
        self.writes: list[bytes] = []

    def write(self, payload: bytes, timeout: float = 2.0) -> None:
        self.writes.append(payload)
        if self._batches:
            self._pending = list(self._batches.pop(0))

    def read_chunk(self, timeout: float) -> bytes:
        if self._pending:
            return self._pending.pop(0)
        return b""


def _real_session(vendor_protocol, response_batches, **timing):
    """A REAL, already-``synchronized`` ProtocolSession over a scripted
    transport. Synchronization itself is 100% vendor-owned and untouched by
    this PR, so these tests set ``synchronized`` directly (a plain public
    attribute) rather than re-simulating the vendor's startup handshake --
    what's under test is the parser/session behavior during a live command
    exchange and the wrapper's own session-teardown-and-reopen retry.
    """
    timing.setdefault("response_timeout", 0.3)
    timing.setdefault("motion_timeout", 0.3)
    timing.setdefault("startup_timeout", 1.0)
    session = vendor_protocol.ProtocolSession(
        _ScriptedTransport(response_batches),
        heartbeat=False,
        monotonic=_FakeMonotonicClock(),
        **timing,
    )
    session.synchronized = True
    return session


def _real_controller(vendor_protocol, vendor_controller, response_batches, *, firmware=None, **timing):
    session = _real_session(vendor_protocol, response_batches, **timing)
    controller = vendor_controller.TurntableController(session, port="/dev/fake0")
    if firmware is not None:
        controller._firmware = firmware
    return controller


class _RealControllerFactory:
    """``TurntableApi.controller`` stand-in whose ``.open()`` hands out
    pre-built REAL ``TurntableController`` instances in order -- one per
    scripted session, so a retry's second ``.open()`` call gets a genuinely
    different, freshly-synchronized controller, exactly like a second CLI
    invocation would.
    """

    def __init__(self, controllers) -> None:
        self._controllers = list(controllers)
        self.open_calls: list[dict[str, object]] = []

    def open(self, **kwargs):
        self.open_calls.append(kwargs)
        if not self._controllers:
            raise AssertionError("controller.open() called more times than scripted")
        return self._controllers.pop(0)


def test_real_parser_reproduces_the_heartbeat_mid_frame_race(turntable) -> None:
    """The gate's exact probe shape: a partial frame + a heartbeat byte
    raises the bare ProtocolError base class with the parser buffer (and
    therefore ``pending``) left non-empty.
    """
    vendor_protocol, vendor_errors, _commands, _controller = _vendor_protocol_modules()

    parser = vendor_protocol.FrameParser()
    with pytest.raises(vendor_errors.ProtocolError) as excinfo:
        parser.feed(b"PARTIALFRAME#")

    assert str(excinfo.value) == "heartbeat byte appeared inside a protocol frame"
    assert type(excinfo.value) is vendor_errors.ProtocolError
    assert parser.pending == "PARTIALFRAME"


def test_real_parser_in_session_retry_fails_closed(turntable) -> None:
    """Root-cause proof for fix-round blocker 1: re-calling the SAME
    session after the race does not recover -- the vendor's own
    ``_prepare_command`` fails closed on the leftover parser state with a
    SECOND bare ProtocolError, for an entirely different reason than the
    first.
    """
    vendor_protocol, vendor_errors, vendor_commands, _controller = _vendor_protocol_modules()

    # "connection" (CT();) expects a bare "CR+OK" frame; feeding a heartbeat
    # byte while "CR+O" is still accumulating in the parser's normal frame
    # buffer reproduces the exact issue #2516 race during a live exchange.
    session = _real_session(vendor_protocol, [[b"CR+O", b"#"]])

    with pytest.raises(vendor_errors.ProtocolError) as first:
        session.execute(vendor_commands.COMMANDS["connection"])
    assert type(first.value) is vendor_errors.ProtocolError
    assert "heartbeat byte appeared inside a protocol frame" in str(first.value)
    assert session.parser.pending == "CR+O"

    with pytest.raises(vendor_errors.ProtocolError) as second:
        session.execute(vendor_commands.COMMANDS["connection"])
    assert type(second.value) is vendor_errors.ProtocolError
    assert "pre-command receive was not quiescent" in str(second.value)
    assert "CR+O" in str(second.value)


def test_wrapper_recovers_the_real_race_via_a_fresh_session(turntable, capsys) -> None:
    """End-to-end: the wrapper's actual retry path, run against REAL
    ProtocolSession/FrameParser/TurntableController instances, recovers
    exactly where the in-session retry above could not -- because it opens
    a brand-new controller instead of re-calling the raced one.
    """
    vendor_protocol, vendor_errors, _commands, vendor_controller = _vendor_protocol_modules()

    session1_controller = _real_controller(vendor_protocol, vendor_controller, [[b"CR+O", b"#"]])
    session2_controller = _real_controller(
        vendor_protocol,
        vendor_controller,
        [
            [b"CR+OK;"],
            [b"FWV=V2R05C02;CR+OK;"],
            [b"PN=MT320RUBL40ProV3;CR+OK;"],
        ],
    )
    factory = _RealControllerFactory([session1_controller, session2_controller])
    api = turntable.TurntableApi(
        discover_devices=lambda: [],
        controller=factory,
        protocol_error=vendor_errors.ProtocolError,
    )

    assert turntable.main(["--json", "probe"], api=api) == 0

    output = parse_output(capsys)
    assert output["ok"] is True
    assert output["retried"] is True
    assert output["result"]["connected"] is True
    assert output["result"]["product"] == "MT320RUBL40ProV3"
    assert len(factory.open_calls) == 2
    # Only the raced attempt used session 1; the recovered probe ran
    # entirely on session 2's fresh parser.
    assert session1_controller.session.transport.writes == [b"CT();"]
    assert len(session2_controller.session.transport.writes) == 3


def test_real_completion_timeout_is_never_retried(turntable, capsys) -> None:
    """Fix-round blocker 2's proof, against the REAL vendor exception
    hierarchy: a motion command acknowledged but never confirmed complete
    raises ``CompletionTimeout`` -- a ``ProtocolError`` SUBCLASS -- which
    must propagate on the very first attempt. Retrying it would silently
    re-issue an unconfirmed motion command.
    """
    vendor_protocol, vendor_errors, _commands, vendor_controller = _vendor_protocol_modules()

    # Guarded `position` homes first; give it a no-op home (offset already
    # zero) so the single execute_motion() exchange below is the move,
    # acknowledged (CR+OK) but never completed (no CR+EVENT=TB_END).
    controller = _real_controller(
        vendor_protocol,
        vendor_controller,
        [
            [b"OffsetAngle= +0.00\xa1\xe3\r\nCR+OK;"],  # return_to_zero's offset check
            [b"CR+OK;"],  # TURNSINGLE acknowledged, then nothing -- no TB_END
        ],
        firmware="V2R05C02",
    )
    factory = _RealControllerFactory([controller])
    api = turntable.TurntableApi(
        discover_devices=lambda: [],
        controller=factory,
        protocol_error=vendor_errors.ProtocolError,
    )

    assert (
        turntable.main(
            [
                "--json",
                "position",
                "10",
                "--confirm-rig-clear",
                "--confirm-zero-valid",
            ],
            api=api,
            run_command=healthy_power,
        )
        == 1
    )

    output = parse_output(capsys)
    assert output["ok"] is False
    assert "retried" not in output
    assert output["error_type"] == "CompletionTimeout"
    assert isinstance(vendor_errors.CompletionTimeout(""), vendor_errors.ProtocolError)
    assert type(vendor_errors.CompletionTimeout("")) is not vendor_errors.ProtocolError
    # Exactly one fresh session, one motion command written -- no retry, no
    # silently re-issued move.
    assert len(factory.open_calls) == 1
    assert controller.session.transport.writes == [
        b"CT+GETOFFSETANGLE();",
        b"CT+TURNSINGLE(1,10);",
    ]


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
    assert "moving the cable to another USB port disables automatic stop" in readme
    assert "without receiving STOP or any motion command" in readme
    assert "destroys the saved acoustic-axis zero" in readme
    assert "FORBIDDEN in automated measurement" in readme
    assert "never sends a motion command" in readme
    assert "`--confirm-redefine-zero` is required" in readme
    assert "never whether that belief is still the acoustic axis" in readme
    assert "There is no override" in readme
    assert "travel_envelope_exceeded" in readme
    assert "stays on the same side of saved zero" in readme
    assert "caps commanded runaway, not a corrupted zero" in readme
    assert "python3 -m usb_turntable set-zero" in readme
    assert "development-time provenance" in vendor_readme
    assert "does not authenticate files at runtime" in vendor_readme


def test_turntable_product_surface_is_the_stop_hook_and_the_opt_in_walk() -> None:
    """The turntable reaches product code at exactly these seven places.

    Four of them are the hot-plug stop hook (a udev rule, its unit, and the
    install steps that ship both). The rest are the opt-in lab harness
    ``jasper-arm-walk`` — its loop and its CLI, which drive the adapter as a
    SUBPROCESS at the installed path and never import it, the CLI naming the
    stop unit only to cite the `User=pi` identity it borrows — plus the one
    comment in the angle seam that says where its +/-45 arm envelope comes
    from. Nothing here starts on its own: no timer, no daemon, no voice tool.
    """
    markers = (
        "usb-turntable",
        "usb_turntable",
        "jts_turntable",
        "turntable-autostop",
    )
    files: list[Path] = []
    for root in (ROOT / "deploy", ROOT / "jasper"):
        files.extend(
            path for path in root.rglob("*")
            # Compiled bytecode inlines the source's own string constants, so a
            # stale __pycache__ entry would report its module twice under a
            # second name. Only tracked source is the surface.
            if path.is_file() and "__pycache__" not in path.parts
        )

    def has_marker(path: Path) -> bool:
        searchable = path.relative_to(ROOT).as_posix() + path.read_text(errors="ignore")
        return any(marker in searchable for marker in markers)

    matches = {path.relative_to(ROOT).as_posix() for path in files if has_marker(path)}
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"].get("scripts", {})
    script_entries = "\n".join((*scripts.keys(), *scripts.values()))
    if any(marker in script_entries for marker in markers):
        matches.add("pyproject.toml")
    assert matches == {
        "deploy/lib/install/python-runtime.sh",
        "deploy/lib/install/systemd-units.sh",
        "deploy/systemd/jasper-turntable-autostop@.service",
        "deploy/udev/99-jasper-turntable-autostop.rules",
        "jasper/active_speaker/arm_walk.py",
        "jasper/active_speaker/angle_capture.py",
        "jasper/cli/arm_walk.py",
    }


def test_hotplug_stop_udev_systemd_and_install_wiring() -> None:
    rule = AUTOSTOP_RULE.read_text()
    unit = AUTOSTOP_UNIT.read_text()
    units_install = (ROOT / "deploy/lib/install/systemd-units.sh").read_text()
    runtime_install = (ROOT / "deploy/lib/install/python-runtime.sh").read_text()

    assert 'ATTRS{idVendor}=="1a86", ATTRS{idProduct}=="7523"' in rule
    assert 'ENV{ID_PATH}=="platform-xhci-hcd.1-usb-0:2:1.0"' in rule
    assert 'KERNEL=="ttyUSB*"' in rule
    assert 'SYSTEMD_WANTS}+="jasper-turntable-autostop@%k.service"' in rule
    assert "BindsTo=dev-%i.device" in unit
    assert "ConditionPathExists=/dev/%I" in unit
    assert "ExecStart=/usr/bin/python3 /opt/jasper/experiments/usb-turntable/" in unit
    assert "--port /dev/%I --json hotplug-stop" in unit
    assert "/bin/sh" not in unit
    assert "TimeoutStartSec=90s" in unit
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
