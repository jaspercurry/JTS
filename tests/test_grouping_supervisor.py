# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.control.grouping_supervisor.

Tests drive `_tick` directly with the four IO hooks (config load /
outputd STATUS / binding pin / reconciler kick) overridden on a
subclass. This sidesteps the `run()` sleep loop entirely and pins the
policy contract:

  - Solo / bonded-but-invalid → no probes, counters reset
  - Healthy bonded member → counter stays zero, no kick
  - Threshold consecutive starved polls → exactly one reconciler kick
  - "outputd unreachable" and "lane not armed" both count as starved
  - Recovery resets the counter
  - Rate limit blocks a second kick in-window, allows it after
  - Binding read-repair runs on the leader only; totals accumulate;
    a binding crash never blocks the starvation watch
  - Leader autonomous reassertion uses the persisted pair roster and
    includes X-JTS-Household when the secret exists
  - Kick failure is swallowed (logged), never raises out of _tick
"""
from __future__ import annotations

from jasper.control.grouping_supervisor import (
    GroupingSupervisor,
    snapshot,
)
from jasper.multiroom.config import GroupingConfig


def _cfg(
    *,
    enabled: bool = True,
    role: str = "follower",
    channel: str = "left",
    error: str | None = None,
    peer_addr: str = "",
    peer_name: str = "",
) -> GroupingConfig:
    return GroupingConfig(
        enabled=enabled,
        role=role,
        channel=channel,
        bond_id="bond-1" if enabled else "",
        leader_addr="jts.local" if role == "follower" else "",
        buffer_ms=400,
        codec="flac",
        error=error,
        peer_addr=peer_addr,
        peer_name=peer_name,
    )


SERVING = {"dac_content": {"enabled": True, "serving_fifo": True}}
STARVED = {"dac_content": {"enabled": True, "serving_fifo": False}}
NOT_ARMED = {"dac_content": {"enabled": False}}


class _FakeSupervisor(GroupingSupervisor):
    """Drives `_tick` with scripted config/status/binding/kick outcomes."""

    def __init__(self, **kw) -> None:
        super().__init__(
            interval_sec=0.0,
            jitter_sec=0.0,
            cold_start_sec=0.0,
            **kw,
        )
        self.cfg: GroupingConfig = _cfg()
        self.status_results: list = []
        self.binding_results: list = []
        self.binding_calls = 0
        self.peer_grouping_results: list = []
        self.reassert_posts: list[tuple[str, dict, dict[str, str] | None]] = []
        self.household_secret = ""
        self.leader_hostname = "jts.local"
        self.kick_calls = 0
        self.kick_error: BaseException | None = None
        self.now: float = 0.0

    def load_grouping(self) -> GroupingConfig:
        return self.cfg

    async def outputd_status(self) -> dict | None:
        result = self.status_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def repair_bindings(self) -> dict:
        self.binding_calls += 1
        if not self.binding_results:
            return {"reachable": True, "groups": 1, "fixed": 0, "failed": 0}
        result = self.binding_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def read_peer_grouping(self, peer_addr: str) -> dict | None:
        if not self.peer_grouping_results:
            return {
                "enabled": True,
                "role": "follower",
                "channel": "right",
                "bond_id": "bond-1",
                "leader_addr": self.leader_hostname,
                "peer_addr": "",
                "peer_name": "",
            }
        result = self.peer_grouping_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def post_peer_grouping(
        self, peer_addr: str, body: dict,
    ) -> tuple[bool, str]:
        self.reassert_posts.append((peer_addr, body, self.household_headers()))
        return True, "HTTP 200"

    def household_headers(self) -> dict[str, str] | None:
        if not self.household_secret:
            return None
        return {"X-JTS-Household": self.household_secret}

    def leader_handle(self) -> str:
        return self.leader_hostname

    async def kick_reconciler(self) -> None:
        self.kick_calls += 1
        if self.kick_error is not None:
            raise self.kick_error

    def _now(self) -> float:
        return self.now


class _FileBackedHouseholdSupervisor(_FakeSupervisor):
    household_headers = GroupingSupervisor.household_headers


# ---------- watch gating ----------


async def test_solo_config_skips_all_probes():
    sup = _FakeSupervisor()
    sup.cfg = _cfg(enabled=False)
    # No status_results scripted — a probe would pop from an empty
    # list and raise, so completing cleanly proves nothing probed.
    await sup._tick()
    assert sup.watching is False
    assert sup.binding_calls == 0
    assert sup.consecutive_starved == 0
    assert sup.last_poll_starved is None


async def test_bonded_but_invalid_config_skips_all_probes():
    sup = _FakeSupervisor()
    sup.cfg = _cfg(error="JASPER_GROUPING_BOND_ID is empty (grouping is on)")
    await sup._tick()
    assert sup.watching is False
    assert sup.binding_calls == 0


async def test_returning_to_solo_resets_starved_counter():
    sup = _FakeSupervisor(starved_threshold=3)
    sup.status_results = [STARVED, STARVED]
    await sup._tick()
    await sup._tick()
    assert sup.consecutive_starved == 2
    sup.cfg = _cfg(enabled=False)
    await sup._tick()
    assert sup.consecutive_starved == 0
    assert sup.watching is False


# ---------- starvation policy ----------


async def test_healthy_member_keeps_counter_zero():
    sup = _FakeSupervisor()
    sup.status_results = [SERVING, SERVING, SERVING]
    for _ in range(3):
        await sup._tick()
    assert sup.watching is True
    assert sup.consecutive_starved == 0
    assert sup.kick_calls == 0
    assert sup.last_poll_starved is False


async def test_threshold_triggers_exactly_one_kick():
    sup = _FakeSupervisor(starved_threshold=3)
    sup.status_results = [STARVED] * 3
    for _ in range(3):
        await sup._tick()
    assert sup.kick_calls == 1
    assert sup.kick_count == 1
    assert sup.consecutive_starved == 0  # reset after action


async def test_below_threshold_does_not_kick():
    sup = _FakeSupervisor(starved_threshold=3)
    sup.status_results = [STARVED, STARVED]
    for _ in range(2):
        await sup._tick()
    assert sup.kick_calls == 0
    assert sup.consecutive_starved == 2


async def test_unreachable_outputd_counts_as_starved():
    sup = _FakeSupervisor(starved_threshold=3)
    sup.status_results = [None] * 3
    for _ in range(3):
        await sup._tick()
    assert sup.kick_calls == 1


async def test_lane_not_armed_counts_as_starved():
    sup = _FakeSupervisor(starved_threshold=3)
    sup.status_results = [NOT_ARMED] * 3
    for _ in range(3):
        await sup._tick()
    assert sup.kick_calls == 1


async def test_status_exception_counts_as_starved():
    sup = _FakeSupervisor(starved_threshold=3)
    sup.status_results = [RuntimeError("boom")] * 3
    for _ in range(3):
        await sup._tick()
    assert sup.kick_calls == 1


async def test_recovery_resets_counter_between_starved_polls():
    sup = _FakeSupervisor(starved_threshold=3)
    sup.status_results = [STARVED, STARVED, SERVING, STARVED, STARVED]
    for _ in range(5):
        await sup._tick()
    assert sup.consecutive_starved == 2
    assert sup.kick_calls == 0


async def test_rate_limit_blocks_second_kick_in_window():
    sup = _FakeSupervisor(starved_threshold=3, kick_rate_limit_sec=600.0)
    sup.status_results = [STARVED] * 6
    sup.now = 0.0
    for _ in range(3):
        await sup._tick()
    assert sup.kick_calls == 1
    sup.now = 300.0  # half-way through the window
    for _ in range(3):
        await sup._tick()
    assert sup.kick_calls == 1  # still blocked
    assert sup.rate_limited_count == 1


async def test_rate_limit_allows_second_kick_after_window():
    sup = _FakeSupervisor(starved_threshold=3, kick_rate_limit_sec=600.0)
    sup.status_results = [STARVED] * 6
    sup.now = 0.0
    for _ in range(3):
        await sup._tick()
    assert sup.kick_calls == 1
    sup.now = 700.0  # past window
    for _ in range(3):
        await sup._tick()
    assert sup.kick_calls == 2


async def test_kick_failure_is_swallowed():
    sup = _FakeSupervisor(starved_threshold=3)
    sup.status_results = [STARVED] * 3
    sup.kick_error = RuntimeError("systemctl boom")
    for _ in range(3):
        await sup._tick()  # must not raise
    assert sup.kick_calls == 1
    assert sup.kick_count == 1  # the attempt is still counted/visible


# ---------- binding read-repair ----------


async def test_follower_never_runs_binding_repair():
    sup = _FakeSupervisor()
    sup.cfg = _cfg(role="follower")
    sup.status_results = [SERVING]
    await sup._tick()
    assert sup.binding_calls == 0


async def test_leader_runs_binding_repair_each_tick():
    sup = _FakeSupervisor()
    sup.cfg = _cfg(role="leader")
    sup.status_results = [SERVING, SERVING]
    for _ in range(2):
        await sup._tick()
    assert sup.binding_calls == 2
    assert sup.binding_last_reachable is True


async def test_binding_repair_totals_accumulate():
    sup = _FakeSupervisor()
    sup.cfg = _cfg(role="leader")
    sup.status_results = [SERVING, SERVING]
    sup.binding_results = [
        {"reachable": True, "groups": 2, "fixed": 1, "failed": 0},
        {"reachable": False, "groups": 0, "fixed": 0, "failed": 0},
    ]
    await sup._tick()
    assert sup.binding_fixed_total == 1
    assert sup.binding_last_repair_at is not None
    await sup._tick()
    assert sup.binding_last_reachable is False
    assert sup.binding_fixed_total == 1


async def test_binding_crash_does_not_block_starvation_watch():
    sup = _FakeSupervisor(starved_threshold=3)
    sup.cfg = _cfg(role="leader")
    sup.status_results = [STARVED] * 3
    sup.binding_results = [RuntimeError("rpc boom")] * 3
    for _ in range(3):
        await sup._tick()  # must not raise
    assert sup.kick_calls == 1


# ---------- autonomous reassertion ----------


async def test_leader_reasserts_rostered_follower_with_household_secret(
    monkeypatch, tmp_path,
):
    """Phase D: a leader repairing its roster peer presents X-JTS-Household."""
    import jasper.control.household_credential as hc

    secret_path = tmp_path / "household_secret"
    secret_path.write_text("hh-secret\n")
    monkeypatch.setattr(hc, "SECRET_FILE", str(secret_path))

    sup = _FileBackedHouseholdSupervisor()
    sup.cfg = _cfg(role="leader", channel="left", peer_addr="192.168.1.9")
    sup.peer_grouping_results = [None]
    sup.status_results = [SERVING]

    await sup._tick()

    assert sup.reassert_posts == [(
        "192.168.1.9",
        {
            "enabled": True,
            "role": "follower",
            "channel": "right",
            "bond_id": "bond-1",
            "leader_addr": "jts.local",
            "peer_addr": "",
            "peer_name": "",
        },
        {"X-JTS-Household": "hh-secret"},
    )]
    assert sup.reassert_attempt_count == 1
    assert sup.reassert_last_ok is True


async def test_leader_reassert_omits_header_when_household_secret_absent(
    monkeypatch, tmp_path,
):
    """Absent local secret stays safe: no fake header, no crash."""
    import jasper.control.household_credential as hc

    monkeypatch.setattr(hc, "SECRET_FILE", str(tmp_path / "missing_secret"))

    sup = _FileBackedHouseholdSupervisor()
    sup.cfg = _cfg(role="leader", channel="left", peer_addr="192.168.1.9")
    sup.peer_grouping_results = [None]
    sup.status_results = [SERVING]

    await sup._tick()

    assert sup.reassert_posts[0][2] is None


async def test_leader_reassert_skips_already_converged_follower():
    sup = _FakeSupervisor()
    sup.cfg = _cfg(role="leader", channel="left", peer_addr="192.168.1.9")
    sup.peer_grouping_results = [{
        "enabled": True,
        "role": "follower",
        "channel": "right",
        "bond_id": "bond-1",
        "leader_addr": "jts.local",
        "peer_addr": "",
        "peer_name": "",
    }]
    sup.status_results = [SERVING]

    await sup._tick()

    assert sup.reassert_posts == []
    assert sup.reassert_attempt_count == 0
    assert sup.reassert_last_ok is True


async def test_leader_reassert_skips_unrostered_or_non_pair_shape():
    sup = _FakeSupervisor()
    sup.cfg = _cfg(role="leader", channel="mono", peer_addr="192.168.1.9")
    sup.status_results = [SERVING]

    await sup._tick()

    assert sup.reassert_posts == []


async def test_reassert_read_failure_does_not_block_starvation_watch():
    sup = _FakeSupervisor(starved_threshold=1)
    sup.cfg = _cfg(role="leader", channel="left", peer_addr="192.168.1.9")
    sup.status_results = [STARVED]
    sup.peer_grouping_results = [RuntimeError("peer read boom")]

    await sup._tick()

    assert sup.kick_calls == 1
    assert sup.reassert_attempt_count == 1
    assert sup.reassert_last_ok is True


# ---------- snapshot ----------


async def test_snapshot_keys_and_values():
    sup = _FakeSupervisor()
    sup.cfg = _cfg(role="leader")
    sup.status_results = [SERVING]
    await sup._tick()
    snap = sup.snapshot()
    assert set(snap.keys()) == {
        "enabled", "watching", "last_poll_at", "last_poll_starved",
        "consecutive_starved", "kick_count", "last_kick_at",
        "rate_limited_count", "binding", "reassert",
    }
    assert snap["enabled"] is True
    assert snap["watching"] is True
    assert snap["last_poll_starved"] is False
    assert set(snap["binding"].keys()) == {
        "last_reachable", "fixed_total", "failed_total", "last_repair_at",
    }
    assert set(snap["reassert"].keys()) == {
        "attempt_total", "failed_total", "last_attempt_at", "last_ok",
        "last_detail",
    }


def test_module_snapshot_when_disabled():
    """`snapshot()` returns enabled=False when no supervisor has been
    started — the /state default for fresh installs and for
    JASPER_GROUPING_SUPERVISOR=disabled."""
    assert snapshot() == {"enabled": False}


async def test_unbond_resets_the_journal_noise_latches():
    """The once-per-streak WARN latch ends with the streak: going solo
    clears it, so a later re-bond's first starvation logs its full WARN
    buildup again instead of arriving pre-silenced at DEBUG."""
    sup = _FakeSupervisor(starved_threshold=2)
    sup.status_results = [STARVED, STARVED]
    for _ in range(2):
        await sup._tick()
    assert sup._streak_warned is True  # latched after the threshold kick
    sup.cfg = _cfg(enabled=False)
    await sup._tick()
    assert sup._streak_warned is False
    assert sup._rate_limit_warned_window is None
