# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Boot-wiring contracts for the source-intent coordinator oneshot."""

from pathlib import Path

from jasper import source_intent
from jasper.control import restart_broker
from jasper.local_sources import local_source_lifecycles
from jasper.multiroom.effective_role import FOLLOWER_STATUS_FILE


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy/systemd/jasper-source-intent-reconcile.service"
GROUPING_UNIT = ROOT / "deploy/systemd/jasper-grouping-reconcile.service"
ROLE_OWNER_UNITS = (
    UNIT,
    ROOT / "deploy/systemd/jasper-accessory-reconcile.service",
    ROOT / "deploy/systemd/jasper-fanin-coupling-auto.service",
)
NGINX_CONFIGS = (
    ROOT / "deploy/nginx-jasper.conf",
    ROOT / "deploy/nginx-jasper-streambox.conf",
)
SOURCE_UNIT_FILES = {
    "shairport-sync.service": ROOT / "deploy/systemd/shairport-sync.service",
    "nqptp.service": ROOT / "deploy/systemd/nqptp.service",
    "librespot.service": ROOT / "deploy/systemd/librespot.service",
    "bluealsa.service": (ROOT / "deploy/systemd/bluealsa.service.d/jts-restart.conf"),
    "bluealsa-aplay.service": (
        ROOT / "deploy/systemd/bluealsa-aplay.service.d/jts-output.conf"
    ),
    "bt-agent.service": ROOT / "deploy/systemd/bt-agent.service",
    "jasper-usbgadget.service": ROOT / "deploy/systemd/jasper-usbgadget.service",
    "jasper-usbsink.service": ROOT / "deploy/systemd/jasper-usbsink.service",
    "jasper-usbsink-volume.service": (
        ROOT / "deploy/systemd/jasper-usbsink-volume.service"
    ),
}
CONTROL_UNIT_FILES = {
    "bluetooth.service": (ROOT / "deploy/systemd/bluetooth.service.d/jts-timeout.conf"),
}


def _directives() -> list[tuple[str, str]]:
    directives: list[tuple[str, str]] = []
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith(("#", "[")) or "=" not in text:
            continue
        key, _, value = text.partition("=")
        directives.append((key, value))
    return directives


def test_source_intent_reconcile_runs_at_boot_after_rfkill_restore() -> None:
    pairs = set(_directives())
    after = next(value.split() for key, value in pairs if key == "After")
    assert {
        "systemd-rfkill.service",
        "hciuart.service",
        "network.target",
        "network-online.target",
        "sound.target",
        "avahi-daemon.service",
        "jasper-control.service",
        "jasper-fanin.service",
        "jasper-outputd.service",
        "jasper-camilla.service",
        "jasper-usbgadget.service",
    } <= set(after)
    assert ("WantedBy", "multi-user.target") in pairs


def test_grouping_effective_role_fact_has_root_owned_persistent_parent() -> None:
    directives = _service_directives(GROUPING_UNIT)

    assert FOLLOWER_STATUS_FILE == "/var/lib/jasper-grouping/effective-role.json"
    assert directives["StateDirectory"] == "jasper-grouping"
    assert directives["StateDirectoryMode"] == "0755"
    assert "RuntimeDirectory" not in directives
    text = GROUPING_UNIT.read_text(encoding="utf-8")
    assert "User=" not in text
    assert "Group=" not in text


def test_source_intent_reconcile_is_bounded_without_bluez_ordering_cycle() -> None:
    pairs = set(_directives())
    assert ("Type", "oneshot") in pairs
    assert (
        "TimeoutStartSec",
        str(int(source_intent.RECONCILE_SYSTEMD_TIMEOUT_SECONDS)),
    ) in pairs
    assert not any(key == "Restart" for key, _value in pairs)
    assert not any(key == "RestartSec" for key, _value in pairs)
    assert ("StartLimitIntervalSec", "0") in pairs
    assert not any(key == "StartLimitBurst" for key, _value in pairs)
    assert (
        "ExecStart",
        "/opt/jasper/.venv/bin/jasper-source-intent-reconcile --reason systemd",
    ) in pairs
    assert ("RuntimeDirectory", "jasper-source-intent") in pairs
    assert ("RuntimeDirectoryMode", "0755") in pairs
    assert ("RuntimeDirectoryPreserve", "yes") in pairs
    unit_text = UNIT.read_text(encoding="utf-8")
    assert "\nUser=" not in unit_text
    assert "\nGroup=" not in unit_text

    # The coordinator manages Bluetooth runtime resources itself. Ordering it
    # Before=/After= bluetooth.service while synchronously starting/stopping
    # those resources can create a transaction cycle. RF-kill restore is the
    # only boot-order dependency; bluetooth.service remains control-plane
    # infrastructure and is not pulled or gated by this unit.
    for key, value in pairs:
        if key in {"After", "Before", "Wants", "Requires"}:
            assert "bluetooth.service" not in value.split()


def test_source_reconcile_timeout_hierarchy_covers_all_owner_waits() -> None:
    """One pass fits owned source actions plus two BT and one USB owner waits."""
    owner_waits = source_intent._OWNER_UNIT_ACTION_TIMEOUT_SEC
    required = (
        source_intent._NON_OWNER_RECONCILE_BUDGET_SEC
        + 2 * owner_waits[source_intent._ACCESSORY_RECONCILE_UNIT]
        + owner_waits[source_intent._USB_COUPLING_UNIT]
    )
    assert source_intent.RECONCILE_SYSTEMD_TIMEOUT_SECONDS >= required
    assert (
        source_intent.RECONCILE_BROKER_TIMEOUT_SECONDS
        > source_intent.RECONCILE_SYSTEMD_TIMEOUT_SECONDS
    )
    assert (
        restart_broker._clamp_exec_timeout(
            source_intent.RECONCILE_BROKER_TIMEOUT_SECONDS,
            verb="start",
            units=[source_intent.RECONCILE_UNIT],
            no_block=False,
        )
        == source_intent.RECONCILE_BROKER_TIMEOUT_SECONDS
    )
    assert (
        "TimeoutStartSec",
        str(int(source_intent.RECONCILE_SYSTEMD_TIMEOUT_SECONDS)),
    ) in set(_directives())
    installer = (ROOT / "deploy/lib/install/systemd-units.sh").read_text(
        encoding="utf-8"
    )
    assert (
        f"--kill-after=5s {int(source_intent.RECONCILE_BROKER_TIMEOUT_SECONDS)}s"
    ) in installer


def _seconds(raw: str) -> float:
    return float(raw.removesuffix("s"))


def _service_directives(path: Path) -> dict[str, str]:
    section = ""
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text.startswith("[") and text.endswith("]"):
            section = text
            continue
        if section == "[Service]" and "=" in text and not text.startswith("#"):
            key, _, value = text.partition("=")
            result[key] = value
    return result


def test_owned_source_unit_client_bounds_outlast_explicit_systemd_contracts() -> None:
    """No blocking client may return while its owned systemd job is still legal."""

    declared = {
        unit
        for lifecycle in local_source_lifecycles()
        for unit in lifecycle.runtime_units
    }
    assert set(SOURCE_UNIT_FILES) == declared
    assert set(source_intent._SOURCE_UNIT_SYSTEMD_TIMEOUT_SEC) == declared

    for unit, path in SOURCE_UNIT_FILES.items():
        directives = _service_directives(path)
        start = _seconds(directives["TimeoutStartSec"])
        stop = _seconds(directives["TimeoutStopSec"])
        assert source_intent._SOURCE_UNIT_SYSTEMD_TIMEOUT_SEC[unit] == (start, stop)
        assert source_intent._unit_action_timeout_sec(unit, "start") > start
        assert source_intent._unit_action_timeout_sec(unit, "stop") > stop
        assert source_intent._unit_action_timeout_sec(unit, "restart") > start + stop


def test_source_cold_start_dependencies_are_preordered_or_budgeted() -> None:
    coordinator_after = set(
        next(value.split() for key, value in _directives() if key == "After")
    )

    def dependencies(path: Path) -> set[str]:
        result: set[str] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(("After=", "Requires=", "Wants=")):
                result.update(line.partition("=")[2].split())
        return result

    shairport = SOURCE_UNIT_FILES["shairport-sync.service"].read_text(encoding="utf-8")
    requires = next(
        line.partition("=")[2].split()
        for line in shairport.splitlines()
        if line.startswith("Requires=")
    )
    after = next(
        line.partition("=")[2].split()
        for line in shairport.splitlines()
        if line.startswith("After=")
    )
    assert "nqptp.service" in requires
    assert "nqptp.service" in after

    shairport_start = source_intent._SOURCE_UNIT_SYSTEMD_TIMEOUT_SEC[
        "shairport-sync.service"
    ][0]
    nqptp_start = source_intent._SOURCE_UNIT_SYSTEMD_TIMEOUT_SEC["nqptp.service"][0]
    usb_volume_start = source_intent._SOURCE_UNIT_SYSTEMD_TIMEOUT_SEC[
        "jasper-usbsink-volume.service"
    ][0]
    assert source_intent._SOURCE_UNIT_START_DEPENDENCY_TIMEOUT_SEC == {
        "shairport-sync.service": nqptp_start,
        "jasper-usbsink.service": usb_volume_start,
    }
    assert (
        source_intent._unit_action_timeout_sec("shairport-sync.service", "start")
        > shairport_start + nqptp_start
    )

    # Shared boot-owned prerequisites are complete before the coordinator
    # starts source transactions. Source-specific edges are the only client
    # additions or are explicitly sequenced by the concrete applier.
    assert (
        dependencies(SOURCE_UNIT_FILES["shairport-sync.service"])
        - {
            "nqptp.service",
        }
        <= coordinator_after
    )
    assert dependencies(SOURCE_UNIT_FILES["nqptp.service"]) <= coordinator_after
    assert dependencies(SOURCE_UNIT_FILES["librespot.service"]) <= coordinator_after
    assert dependencies(SOURCE_UNIT_FILES["bluealsa-aplay.service"]) <= (
        coordinator_after
    )
    assert dependencies(SOURCE_UNIT_FILES["bt-agent.service"]) == {
        "bluetooth.service",
    }
    assert (
        dependencies(SOURCE_UNIT_FILES["jasper-usbsink.service"])
        - {
            "jasper-usbgadget.service",
            "jasper-usbsink-volume.service",
        }
        <= coordinator_after
    )
    assert (
        dependencies(SOURCE_UNIT_FILES["jasper-usbsink-volume.service"])
        - {
            "jasper-usbsink.service",
        }
        <= coordinator_after
    )

    for dependency in (
        "jasper-control.service",
        "jasper-fanin.service",
        "jasper-outputd.service",
        "jasper-camilla.service",
        "jasper-usbgadget.service",
    ):
        path = ROOT / "deploy/systemd" / dependency
        assert source_intent.RECONCILE_UNIT not in path.read_text(encoding="utf-8")


def test_control_unit_client_bounds_match_packaged_dropins() -> None:
    assert set(source_intent._CONTROL_UNIT_SYSTEMD_TIMEOUT_SEC) == set(
        CONTROL_UNIT_FILES
    )
    for unit, path in CONTROL_UNIT_FILES.items():
        directives = _service_directives(path)
        start = _seconds(directives["TimeoutStartSec"])
        stop = _seconds(directives["TimeoutStopSec"])
        assert source_intent._CONTROL_UNIT_SYSTEMD_TIMEOUT_SEC[unit] == (
            start,
            stop,
        )
        assert source_intent._unit_action_timeout_sec(unit, "start") > start
        assert source_intent._unit_action_timeout_sec(unit, "stop") > stop
        assert source_intent._unit_action_timeout_sec(unit, "restart") > start + stop


def test_bluetooth_control_timeout_dropin_is_installed_for_both_profiles() -> None:
    installer = (ROOT / "deploy/lib/install/systemd-units.sh").read_text(
        encoding="utf-8"
    )
    source = "deploy/systemd/bluetooth.service.d/jts-timeout.conf"
    destination = 'bluetooth.service.d/jts-timeout.conf"'
    assert installer.count(source) == 2
    assert installer.count(destination) == 4


def test_source_action_budget_keeps_outer_ceiling_honest() -> None:
    stops = sum(
        source_intent._unit_action_timeout_sec(unit, verb)
        for unit, verb in source_intent._WORST_CASE_ORDINARY_STOP_ACTIONS
    )
    enablement = (
        source_intent._MAX_ENABLEMENT_TRANSITIONS
        * source_intent._ENABLEMENT_TRANSITION_BUDGET_SEC
    )
    assert source_intent._NON_OWNER_RECONCILE_BUDGET_SEC == (
        source_intent._NON_SYSTEMD_RECONCILE_BUDGET_SEC
        + enablement
        + source_intent._FAILED_RESET_BUDGET_SEC
        + source_intent._ACTIVE_TRANSITION_BUDGET_SEC
        + source_intent._unit_action_timeout_sec("jasper-usbgadget.service", "restart")
        + source_intent._BLUETOOTH_CONTROL_BUDGET_SEC
        + source_intent._USB_DIRECT_WAIT_BUDGET_SEC
        + source_intent._USB_FAILED_ON_CLEANUP_BUDGET_SEC
    )
    assert (
        source_intent._NON_SYSTEMD_RECONCILE_BUDGET_SEC + enablement + stops
        <= source_intent._NON_OWNER_RECONCILE_BUDGET_SEC
    )
    owner_waits = source_intent._OWNER_UNIT_ACTION_TIMEOUT_SEC
    assert (
        source_intent._NON_OWNER_RECONCILE_BUDGET_SEC
        + 2 * owner_waits[source_intent._ACCESSORY_RECONCILE_UNIT]
        + owner_waits[source_intent._USB_COUPLING_UNIT]
        + source_intent._RECONCILE_TIMEOUT_MARGIN_SEC
    ) == source_intent.RECONCILE_SYSTEMD_TIMEOUT_SECONDS


def _location_block(text: str, route: str) -> str:
    marker = f"    location {route} {{"
    start = text.index(marker)
    end = text.find("\n    location ", start + len(marker))
    return text[start:] if end < 0 else text[start:end]


def test_source_http_routes_outlast_two_bounded_broker_passes() -> None:
    # A stale first acknowledgement may trigger one retry. request_restart waits
    # the broker's explicit exec bound plus its socket margin for each pass.
    minimum_http_timeout = int(
        2
        * (
            source_intent.RECONCILE_BROKER_TIMEOUT_SECONDS
            + restart_broker._CLIENT_SOCKET_MARGIN_SEC
        )
    )
    handler_margin = 60
    for path in NGINX_CONFIGS:
        text = path.read_text(encoding="utf-8")
        for route in ("/sources/", "/bluetooth/", "/speaker/"):
            block = _location_block(text, route)
            timeout_line = next(
                line.strip()
                for line in block.splitlines()
                if line.strip().startswith("proxy_read_timeout ")
            )
            timeout = int(
                timeout_line.removeprefix("proxy_read_timeout ").removesuffix("s;")
            )
            assert timeout >= minimum_http_timeout, (path, route, timeout)
            if route in {"/sources/", "/bluetooth/"}:
                assert timeout >= minimum_http_timeout + handler_margin
            if route == "/speaker/":
                # Rename can also spend 60 s on bluetoothd and 60 s on the
                # active-source refresh before the two coordinator passes.
                assert timeout >= minimum_http_timeout + 180


def test_grouping_boot_reconcile_runs_after_source_intent_convergence() -> None:
    text = GROUPING_UNIT.read_text(encoding="utf-8")
    after = next(
        line.partition("=")[2].split()
        for line in text.splitlines()
        if line.startswith("After=")
    )
    assert "jasper-source-intent-reconcile.service" in after


def test_streambox_systemd_verify_covers_source_and_usb_owner_graph() -> None:
    installer = (ROOT / "deploy/lib/install/systemd-units.sh").read_text(
        encoding="utf-8"
    )
    block = installer.split("validate_streambox_systemd_units() {", 1)[1].split(
        "\n}\n",
        1,
    )[0]
    assert "${SYSTEMD_DIR}/jasper-source-intent-reconcile.service" in block
    assert "${SYSTEMD_DIR}/jasper-fanin-coupling-auto.service" in block


def test_grouping_role_owner_handoffs_have_no_systemd_dependency_cycle() -> None:
    """Grouping may briefly join a running owner, so owners cannot wait on it."""
    grouping_name = GROUPING_UNIT.name
    for path in ROLE_OWNER_UNITS:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(("After=", "Before=", "Wants=", "Requires=")):
                assert grouping_name not in line.split("=", 1)[1].split(), path


def test_root_fanin_owners_do_not_inherit_group_writable_fanin_env() -> None:
    for name in (
        "jasper-fanin-coupling-auto.service",
        "jasper-fanin-combo-health.service",
    ):
        unit = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
        assert "EnvironmentFile=-/var/lib/jasper/fanin.env" not in unit, name
        assert "EnvironmentFile=-/etc/jasper/jasper.env" in unit, name
        assert (
            "Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:"
            "/sbin:/bin"
        ) in unit, name
        assert (
            "UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT "
            "GLIBC_TUNABLES PYTHONPATH PYTHONHOME"
        ) in unit, name


def test_root_grouping_owner_parses_intent_instead_of_importing_it() -> None:
    grouping = GROUPING_UNIT.read_text(encoding="utf-8")
    assert "EnvironmentFile=-/var/lib/jasper/grouping.env" not in grouping
    assert "EnvironmentFile=-/etc/jasper/jasper.env" in grouping
    assert (
        "UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT "
        "GLIBC_TUNABLES PYTHONPATH PYTHONHOME"
    ) in grouping


def test_root_snapcast_units_consume_only_reconciler_derived_grouping_args() -> None:
    for name in ("jasper-snapserver.service", "jasper-snapclient.service"):
        unit = (ROOT / "deploy/systemd" / name).read_text(encoding="utf-8")
        assert "EnvironmentFile=-/var/lib/jasper/grouping.env" not in unit
        assert "EnvironmentFile=-/run/jasper-grouping/snapcast-args.env" in unit
        assert (
            "UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT GLIBC_TUNABLES"
        ) in unit


def test_rapid_source_transactions_are_not_rate_limited() -> None:
    """Two starts per request remain legal across rapid multi-row changes."""
    pairs = set(_directives())
    assert ("StartLimitIntervalSec", "0") in pairs
    assert not any(key == "StartLimitBurst" for key, _value in pairs)

    # Each coordinator pass starts these non-restarting owner oneshots. They
    # need no failure-loop limiter of their own; the caller remains bounded.
    for path in ROLE_OWNER_UNITS:
        text = path.read_text(encoding="utf-8")
        assert "StartLimitIntervalSec=0" in text, path
        assert "Restart=" not in "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("#")
        ), path
