# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Cross-language / cross-process wire-contract guards.

These tests pin the *names and shapes* that cross a language or process
boundary — Rust daemon → Python consumer, bash writer → Rust reader,
Python HTTP payload → dashboard ES module. Every consumer on these seams
is fail-soft by design (a missing key degrades to null / a blank card /
a silently-ignored env var), so drift never throws at runtime; it just
quietly blanks a surface.

Producers are executed wherever they can be. outputd's STATUS key set is
pinned by `snapshot_json_emits_every_key_the_python_status_consumers_read`
in `rust/jasper-outputd/src/state.rs`.
"""
from __future__ import annotations

import re
from pathlib import Path

from jasper.cli.aec_init import RECENT_WRITES_KEY, _reference_writes

REPO = Path(__file__).resolve().parents[1]
FANIN_STATE_RS = REPO / "rust" / "jasper-fanin" / "src" / "state.rs"
FANIN_CONFIG_RS = REPO / "rust" / "jasper-fanin" / "src" / "config.rs"
OUTPUTD_STATE_RS = REPO / "rust" / "jasper-outputd" / "src" / "state.rs"


def _strip_comment_lines(text: str, *, markers: tuple[str, ...]) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith(markers)
    )


def _rust_emitted_json_keys(path: Path) -> set[str]:
    """Key names a hand-rolled Rust JSON emitter produces.

    Matches helper calls (``push_kv_*(&mut buf, "key", ...)``, ``_opt``
    variants included) and inline object/array openers
    (``buf.push_str(r#""key":...``). Flat — nesting is not modeled, which is
    all a fail-soft ``.get()`` consumer needs. Test-only assertions are cut at
    ``#[cfg(test)]`` so they cannot satisfy the production contract.
    """
    src = path.read_text().split("#[cfg(test)]", 1)[0]
    src = _strip_comment_lines(src, markers=("//",))
    keys: set[str] = set()
    keys.update(re.findall(
        r'push_kv_\w+\(\s*(?:&mut\s+)?\w+,\s*"(\w+)"',
        src,
    ))
    keys.update(re.findall(r'\w+\.push_str\(r#""(\w+)":', src))
    return keys


# ---------------------------------------------------------------------------
# 1. fan-in STATUS JSON — Rust emitter vs Python consumers
#
# jasper-fanin answers `STATUS\n` on its control UDS with hand-rolled JSON.
# Python consumers read it with fail-soft .get() chains, so a renamed Rust key
# silently turns into None on /state, in the doctor, and in correction
# integrity snapshots. outputd's half of this contract executes in its own
# crate (`snapshot_json_emits_every_key_the_python_status_consumers_read` in
# rust/jasper-outputd/src/state.rs); fan-in's own `#[test]`s pin its emitted
# shape but never look at the Python call sites, so fan-in's emitter is
# scanned HERE instead — this is the only place that pins which Python
# reader depends on which emitted key.
# ---------------------------------------------------------------------------

FANIN_STATUS_CONSUMERS: dict[str, set[str]] = {
    # _fanin_summary / _read_fanin_status
    "jasper/correction/runtime_integrity.py": {
        "selected_input", "selection_mode", "input_buffer_frames",
        "output", "frames_written", "xrun_count",
        "inputs", "label", "frames_read",
    },
    # AirPlayHealthSampler._sample_fanin
    "jasper/control/airplay_health.py": {
        "inputs", "label", "frames_read", "xrun_count",
        "output", "frames_written", "sample_rate", "period_frames",
        "input_buffer_frames", "selected_input",
        "watchdog", "last_progress_age_ms", "pings_skipped",
    },
    # check_fanin_service
    "jasper/cli/doctor/audio_runtime.py": {
        "output", "pcm", "frames_written", "xrun_count",
        "inputs", "label", "input_buffer_frames",
        "watchdog", "last_progress_age_ms",
    },
}


def test_fanin_status_keys_match_python_consumers():
    emitted = _rust_emitted_json_keys(FANIN_STATE_RS)
    assert emitted, f"no JSON keys extracted from {FANIN_STATE_RS} — extractor broke?"
    problems: list[str] = []
    for consumer_rel, keys in FANIN_STATUS_CONSUMERS.items():
        src = (REPO / consumer_rel).read_text()
        for key in sorted(keys):
            if key not in emitted:
                problems.append(
                    f"{consumer_rel} reads STATUS key {key!r} that "
                    "rust/jasper-fanin/src/state.rs no longer emits"
                )
            if f'"{key}"' not in src and f"'{key}'" not in src:
                problems.append(
                    f"contract pin stale: {consumer_rel} no longer "
                    f"references {key!r} — update this test's pins"
                )
    assert not problems, "\n".join(problems)


def test_aec_init_reads_the_chip_ref_sample_ring_outputd_publishes():
    """The chip-ref writer's per-write observation ring (#2253).

    The one STATUS surface whose ABSENCE is a hard refusal rather than a blank
    card: `jasper-aec-init` cannot assemble a K window from the single latest
    reading at outputd's ~2 reads/s, so a missing ring parks the box. Read the
    shape outputd's `snapshot_json_reports_the_chip_ref_writers_recent_
    observations` builds; the ring depth its Python stand-in models must match
    the daemon's declared capacity, a constant no Python path can reach.
    """
    writes = _reference_writes({
        RECENT_WRITES_KEY: [
            {
                "frames_written": 128, "snd_pcm_delay_frames": 400,
                "reference_sequence": None, "age_ms": 12,
            },
            {
                "frames_written": 256, "snd_pcm_delay_frames": 401,
                "reference_sequence": 1, "age_ms": 4,
            },
        ],
    })
    assert [write.frames for write in writes] == [128, 256]
    assert [write.delay for write in writes] == [400, 401]
    assert [write.sequence for write in writes] == [None, 1]
    assert [write.age_ms for write in writes] == [12, 4]

    capacity = re.search(
        r"pub const CHIP_REF_RECENT_WRITES: usize = (\d+);",
        OUTPUTD_STATE_RS.read_text(),
    )
    fixture = re.search(
        r"^RING_CAPACITY = (\d+)$",
        (REPO / "tests" / "test_aec_init.py").read_text(),
        re.MULTILINE,
    )
    assert capacity and fixture
    assert capacity.group(1) == fixture.group(1)


def test_fanin_control_command_vocabulary_matches_mux():
    """mux drives fan-in's source gate over the UDS with a one-line text
    command. Pin the verbs on both sides, plus the error-shape key mux
    raises on."""
    state_rs = FANIN_STATE_RS.read_text()
    mux_py = (REPO / "jasper" / "mux.py").read_text()
    control_py = (REPO / "jasper" / "fanin" / "control.py").read_text()
    for verb in ('"STATUS"', '"AUTO"', '"NONE"', '"SELECT '):
        assert verb in state_rs, f"fanin state.rs no longer handles {verb}"
    assert 'socket_path=FANIN_CONTROL_SOCKET' in mux_py
    assert 'f"SELECT {label}", socket_path=FANIN_CONTROL_SOCKET' in mux_py
    assert 'fanin_command("AUTO", socket_path=FANIN_CONTROL_SOCKET)' in mux_py
    assert 'fanin_command("NONE", socket_path=FANIN_CONTROL_SOCKET)' in mux_py
    # state.rs error responses carry {"error": ...}; mux raises on it.
    assert '"error":' in state_rs
    assert '"error" in payload' in control_py


def test_control_socket_paths_agree_across_processes(monkeypatch):
    """fan-in's control socket path is a hardcoded Rust constant (its
    config.rs reads no env override) and every Python consumer hardcodes the
    same literal; outputd's unit pins the env explicitly. If either daemon
    moves its socket, every consumer here moves with it in the same PR.
    """
    from jasper.correction.runtime_integrity import FANIN_CONTROL_SOCKET
    from jasper.fanin.status import FANIN_STATUS_SOCKET
    from jasper.peering.config import PEERING_UDS_PATH

    from .doctor_test_support import _fresh_cfg

    fanin_sock = "/run/jasper-fanin/control.sock"
    outputd_sock = "/run/jasper-outputd/control.sock"

    assert f'"{fanin_sock}"' in FANIN_CONFIG_RS.read_text()
    assert FANIN_STATUS_SOCKET == fanin_sock
    assert FANIN_CONTROL_SOCKET == FANIN_STATUS_SOCKET
    for rel in (
        "jasper/mux.py",
        "jasper/control/airplay_health.py",
        "jasper/cli/doctor/audio_runtime.py",
        "jasper/cli/system_soak.py",
    ):
        assert fanin_sock in (REPO / rel).read_text(), (
            f"{rel} no longer pins the fan-in control socket {fanin_sock}"
        )

    unit = (REPO / "deploy" / "systemd" / "jasper-outputd.service").read_text()
    assert f'Environment="JASPER_OUTPUTD_CONTROL_SOCKET={outputd_sock}"' in unit
    for rel in (
        "jasper/audio_validation.py",
        "jasper/cli/doctor/audio_runtime_outputd.py",
        "jasper/cli/system_soak.py",
    ):
        assert outputd_sock in (REPO / rel).read_text(), (
            f"{rel} no longer pins the outputd control socket {outputd_sock}"
        )

    # voice connects where jasper-control's peering daemon binds.
    monkeypatch.delenv("JASPER_PEERING_UDS", raising=False)
    cfg = _fresh_cfg(monkeypatch, GEMINI_API_KEY="AIzaSyTest")
    assert cfg.peering_uds_socket == PEERING_UDS_PATH

    control_unit = (REPO / "deploy/systemd/jasper-control.service").read_text()
    voice_unit = (REPO / "deploy/systemd/jasper-voice.service").read_text()
    assert "User=jasper-control" in control_unit
    assert "Group=jasper" in control_unit
    assert "RuntimeDirectory=jasper-control" in control_unit
    assert "RuntimeDirectoryMode=0750" in control_unit
    assert "Group=jasper" in voice_unit


async def test_state_aggregate_probes_both_daemon_control_sockets(
    monkeypatch, tmp_path,
):
    """`/state` reaches both daemons over the paths their units bind.

    Run the real aggregate with a recording status probe: a probe that moved
    to a different socket, or stopped being called at all, is the drift this
    exists to catch.
    """
    from jasper.control import state_aggregate

    probed: list[str] = []

    async def record_status(path, *_args, **_kwargs):
        probed.append(path)
        return None

    async def no_status(*_args, **_kwargs):
        return None

    monkeypatch.setattr(state_aggregate, "_audio_graph_state", lambda **_kw: None)
    monkeypatch.setenv("JASPER_VOLUME_STATE_PATH", str(tmp_path / "volume.json"))
    monkeypatch.setenv("JASPER_LIBRESPOT_STATE", str(tmp_path / "spotify.env"))
    await state_aggregate._get_state(
        camilla_host="127.0.0.1",
        camilla_port=1234,
        voice_socket_path=str(tmp_path / "voice.sock"),
        voice_socket_command=no_status,
        mux_socket_command=no_status,
        local_status_json=record_status,
        aec_full_status=lambda: {},
        read_transit_state_func=lambda: {"packs": []},
        ha_status_snapshot=lambda: {"configured": False, "connected": False},
    )

    assert "/run/jasper-fanin/control.sock" in probed
    assert "/run/jasper-outputd/control.sock" in probed


# ---------------------------------------------------------------------------
# 2. JASPER_OUTPUTD_* / JASPER_FANIN_* env name-set drift
#
# The bash reconcilers, systemd units, install.sh, the wizard-owned env
# stagers in Python, and .env.example all spell these names by hand; the
# only readers are the two Rust daemons' from_env. An env var written
# with a name Rust doesn't read is a silent no-op — the deploy "works"
# and the knob does nothing. Pin: every non-comment mention of a
# JASPER_OUTPUTD_*/JASPER_FANIN_* name anywhere outside rust/ must be a
# name the Rust readers know, or carry a documented exception below.
# ---------------------------------------------------------------------------

# Names mentioned outside rust/ that the Rust daemons intentionally do
# NOT read today. Each entry must stay accurate in both directions: the
# guard fails if an exception becomes dead (no longer mentioned) or
# becomes live (Rust starts reading it) — remove the entry then.
ENV_CONTRACT_EXCEPTIONS: dict[str, str] = {
    # (The former JASPER_OUTPUTD_SNAPFIFO_PATH exception was dropped
    # 2026-06-11: the outputd-as-producer machinery was REMOVED — the
    # canonical design feeds the snapserver pipe from the leader's
    # CamillaDSP, so the env is no longer written anywhere. The former
    # JASPER_OUTPUTD_DAC_CONTENT_FIFO exception was dropped the same day
    # in the opposite direction: Increment 3 landed the outputd reader,
    # so the name is now LIVE Rust-read config, exactly as this guard's
    # bidirectional contract demands.)
    # Python-consumer-side override of where mux CONNECTS; fanin's own
    # bind path is a hardcoded Rust constant (see
    # test_control_socket_paths_agree_across_processes). Setting this
    # alone cannot move fan-in's socket.
    "JASPER_FANIN_CONTROL_SOCKET": "mux connect-path knob, not a fanin knob",
    # AirPlay receiver-side timing/offset helper knobs. These change where the
    # shell helper PROBES STATUS; they do not move either daemon's bind socket.
    "JASPER_FANIN_STATUS_SOCKET": "AirPlay helper probe path, not a fanin knob",
    "JASPER_OUTPUTD_STATUS_SOCKET": "AirPlay helper probe path, not an outputd knob",
    # outputd failure-reconcile helper state. These tune the stamp that bounds
    # the helper to one reconcile per window across every failure class; they
    # are consumed only by deploy/bin/jasper-outputd-failure-reconcile, not by
    # the Rust daemon.
    "JASPER_OUTPUTD_CONFIG_RETRY_STATE": "outputd failure helper reconcile stamp path; script-only",
    "JASPER_OUTPUTD_CONFIG_RETRY_WINDOW_SEC": "outputd failure helper reconcile window; script-only",
    # The retired content lane's capture PCM. outputd no longer reads it
    # (ADR-0100 deleted the lane) and nothing writes it any more: the reconciler
    # sweep removed the last writes and now actively REMOVES the key line from
    # outputd.env, so one reconcile heals a box that carried a stale one and
    # ABSENT is the steady state. The ONE surviving mention is
    # jasper/audio_runtime_plan.py's retired-route describer, which reads the
    # key with an absent-key default — the state every reconciled box is in.
    # REMOVAL CONDITION: goes when that describer goes. Retiring the describer
    # ALSO means retiring the reconciler's endpoint-contract gate, which
    # resolves the retired pairing map on every pass and exits 66 when it
    # misses: dropping that map entry while the gate stands parks every box on
    # every reconcile (see jasper/camilla_config_contract.py). Delete this entry
    # then, and this guard fails until someone does.
    "JASPER_OUTPUTD_CONTENT_PCM": "retired lane; read with a default by the park describer, written by nothing",
    # The removed transport_pipe coupling's outputd key. The Rust
    # local_content_pipe path was deleted with the coupling, so it is not
    # Rust-read anymore; it survives as the reconciler's legacy migration-sweep
    # UNSET target (_LEGACY_OUTPUTD_LOCAL_CONTENT_PIPE_ENV) so a migrating box
    # converges clean. (Its sibling JASPER_FANIN_CAMILLA_PIPE was excepted while
    # the fanin_coupling removal docstring still named it; ADR-0100 deleted that
    # docstring, so this guard demanded the dead entry back — removed here, in
    # the direction the guard exists to force.)
    "JASPER_OUTPUTD_LOCAL_CONTENT_PIPE": "removed transport_pipe coupling; reconciler migration-sweep unset target, not Rust-read",
    # (JASPER_OUTPUTD_DAC_FORMAT was excepted for one PR while the registry
    # declared it and no consumer existed. PR-2 landed outputd's read, so the
    # entry was removed in the direction this guard demands — the contract is
    # live Rust-read config now.)
}

# Script-local variables that *name the env file path itself* (e.g.
# OUTPUTD_ENV_FILE="${JASPER_OUTPUTD_ENV_FILE:-...}") — deploy plumbing,
# not daemon env.
_ENV_FILE_KNOB_SUFFIX = "_ENV_FILE"

_ENV_NAME_RE = re.compile(r"JASPER_(?:OUTPUTD|FANIN)_[A-Z0-9_]*[A-Z0-9]")


def _env_names_in(text: str) -> set[str]:
    return set(_ENV_NAME_RE.findall(text))


def _rust_read_env_names() -> set[str]:
    names: set[str] = set()
    for crate in ("jasper-outputd", "jasper-fanin"):
        for rs in (REPO / "rust" / crate / "src").glob("*.rs"):
            names |= _env_names_in(
                _strip_comment_lines(rs.read_text(), markers=("//",))
            )
    return names


def _non_rust_env_mentions() -> dict[str, set[str]]:
    """Map env-var name -> set of repo-relative files mentioning it,
    across the writer/spelling surfaces (comment lines stripped)."""
    surfaces: list[Path] = [REPO / ".env.example"]
    surfaces += sorted((REPO / "jasper").rglob("*.py"))
    surfaces += [
        p for p in sorted((REPO / "deploy").rglob("*"))
        if p.is_file() and p.suffix not in {".png", ".jpg", ".woff2", ".bin"}
        and "assets" not in p.parts
    ]
    mentions: dict[str, set[str]] = {}
    for path in surfaces:
        try:
            text = path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        stripped = _strip_comment_lines(text, markers=("#", "//"))
        for name in _env_names_in(stripped):
            mentions.setdefault(name, set()).add(
                str(path.relative_to(REPO))
            )
    return mentions


def test_outputd_fanin_env_names_are_read_by_rust_or_excepted():
    rust_names = _rust_read_env_names()
    assert rust_names, "no env names extracted from Rust sources — extractor broke?"
    problems: list[str] = []
    for name, files in sorted(_non_rust_env_mentions().items()):
        if name.endswith(_ENV_FILE_KNOB_SUFFIX):
            continue
        if name in rust_names:
            continue
        if name in ENV_CONTRACT_EXCEPTIONS:
            continue
        problems.append(
            f"{name} is spelled in {sorted(files)} but no Rust daemon "
            f"(rust/jasper-outputd, rust/jasper-fanin) reads it — "
            f"silent no-op env. Fix the name, or add a documented "
            f"exception in {Path(__file__).name}."
        )
    assert not problems, "\n".join(problems)


def test_env_contract_exceptions_stay_accurate():
    rust_names = _rust_read_env_names()
    mentions = _non_rust_env_mentions()
    problems: list[str] = []
    for name, reason in ENV_CONTRACT_EXCEPTIONS.items():
        if name in rust_names:
            problems.append(
                f"exception {name} ({reason}) is now READ by a Rust "
                f"daemon — the contract is live; remove the exception."
            )
        if name not in mentions:
            problems.append(
                f"exception {name} ({reason}) is no longer mentioned "
                f"anywhere — dead entry; remove it."
            )
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# 3. /system/snapshot payload — jasper-control vs the dashboard ES module
#
# The /system/ dashboard polls data.json (proxied to jasper-control's
# /system/snapshot). renderSection() is fail-soft: a renamed payload key
# silently blanks a section. Every name the ES modules read is checked against
# the payload the real builder/sampler/composer returns.
# ---------------------------------------------------------------------------

_SYSTEM_STATUS_JS_DIR = REPO / "deploy" / "assets" / "system-status" / "js"


def _system_status_js_text() -> str:
    return "\n".join(
        p.read_text() for p in sorted(_SYSTEM_STATUS_JS_DIR.glob("*.js"))
    )


def _payload_key_names(payload: object) -> set[str]:
    """Every key name a JSON-shaped payload carries, at any nesting depth."""
    names: set[str] = set()
    if isinstance(payload, dict):
        for name, child in payload.items():
            names.add(name)
            names |= _payload_key_names(child)
    elif isinstance(payload, list):
        for item in payload:
            names |= _payload_key_names(item)
    return names


def _system_snapshot_payload() -> dict:
    """Run the real /system/snapshot builder with its samplers absent.

    Every sampler slot is optional by design (the route answers a direct CLI
    invocation too), so a bare handler exercises the payload assembly itself.
    """
    from jasper.control.handlers.system import SystemRoutes

    class _Probe(SystemRoutes):
        def __init__(self) -> None:
            self._sampler = None
            self._audio_health_sampler = None
            self._airplay_health_sampler = None
            self._ha_status_cache = type(
                "_HaCache", (), {"snapshot": staticmethod(dict)},
            )
            self.payload: dict = {}

        def _send_json(self, payload, **_status) -> None:
            self.payload = payload

    probe = _Probe()
    probe._get_system_snapshot()
    return probe.payload


def test_dashboard_snapshot_top_level_keys_exist_in_server_payload():
    js = _system_status_js_text()
    snap_keys = set(re.findall(r"\bsnap\.([a-z_0-9]+)\b", js))
    assert snap_keys, "no snap.* reads found in system-status JS — extractor broke?"
    payload = _system_snapshot_payload()
    problems = [
        f"dashboard JS reads snap.{key} but /system/snapshot builds no {key!r} "
        f"key — that card goes silently blank"
        for key in sorted(snap_keys)
        if key not in payload
    ]
    assert not problems, "\n".join(problems)


# Metric names the vitals / network / software cards read from
# snap.metrics.current (vitalsCards / networkList / softwareList in
# sections.js + views.js).
DASHBOARD_METRICS_CURRENT_KEYS = {
    "mem_total_mb", "temp_c", "throttled_now", "throttled_history",
    "fan_present", "fan_rpm", "fan_pwm", "disk_used_pct", "disk_total_gb",
    "uptime_sec", "net_rx_bytes", "net_tx_bytes", "per_core_cpu_pct",
}


def test_dashboard_metrics_current_keys_exist_in_sampler():
    from jasper.control.system_metrics import SystemSampler

    js = _system_status_js_text()
    current = SystemSampler().snapshot()["current"]
    problems: list[str] = []
    for key in sorted(DASHBOARD_METRICS_CURRENT_KEYS):
        if key not in current:
            problems.append(
                f"dashboard reads metrics.current.{key} but the system sampler "
                f"snapshot produces no {key!r}"
            )
        if f"cur.{key}" not in js:
            problems.append(
                f"contract pin stale: system-status JS no longer reads "
                f"cur.{key} — update this test's pins"
            )
    assert not problems, "\n".join(problems)


# audio-view.js / audio-sections.js render only this normalized presentation
# model; raw AirPlay counters stay a compatibility/technical surface.
DASHBOARD_AUDIO_HEALTH_KEYS = {
    "schema_version", "sampled_at", "overall", "signal_path", "latency",
    "sources", "issues", "technical", "status", "headline", "detail",
    "active_source", "since", "applicable", "kind",
    "runtime", "state", "started_at", "last_seen_at", "recovered_at",
    "current_stream", "current_incident", "recent_incidents", "media",
    "processing", "output", "signal", "session", "summary", "details",
    "id", "key", "severity", "title", "source_id", "duration_seconds",
    "duration_label", "count", "recurrence", "impact", "observed",
    "likely_area", "evidence", "first_at", "last_at", "window_seconds",
    "interruptions", "latency_events", "sync_events", "degraded_seconds",
    "last_incident_at",
}
_HEALTH_SAMPLED_AT = 1000.0


def test_dashboard_audio_health_keys_exist_in_normalized_sampler():
    """Every name the Audio view reads must survive producer → payload.

    The incident and session halves are driven through `IssueTracker` /
    `SessionRollup` rather than hand-built: the session dict reaches the
    payload verbatim, so a rename there would otherwise stay green while the
    card blanks, and the incident lifecycle (recovered records, coalesced
    counts) is what puts `recurrence` and the duration fields on it at all.
    """
    from jasper.control.audio_health import _issue, compose_audio_health
    from jasper.control.audio_incidents import IssueTracker, SessionRollup

    # An active USB source with one recurring, recovered incident — the one box
    # shape that reaches every optional block of the composed contract.
    candidate = _issue(
        "audio.dropout", scope="source", source_id="usbsink",
        impact="continuity", severity="warn", title="Playback interrupted",
        detail="The stream stopped briefly.",
    )
    tracker = IssueTracker()
    tracker.update([candidate], _HEALTH_SAMPLED_AT - 300.0)
    tracker.update([candidate], _HEALTH_SAMPLED_AT - 295.0)
    tracker.update([], _HEALTH_SAMPLED_AT - 290.0)
    tracker.update([candidate], _HEALTH_SAMPLED_AT - 10.0)
    rollup = SessionRollup()
    rollup.reset("usbsink", _HEALTH_SAMPLED_AT - 300.0)
    rollup.observe_state([candidate], _HEALTH_SAMPLED_AT - 300.0)

    health = compose_audio_health(
        airplay={
            "current": {
                "fanin": {
                    "selected_input": "usbsink",
                    "inputs": {"usbsink": {"rms_dbfs": -20.0}},
                },
                "camilla": {"capture_rate": 48000},
            },
            "mux_status": {"sources": {"usbsink": {"playing": True}}},
        },
        outputd={"backend": "alsa", "dac": {"sample_rate": 48000}},
        route={"fixed_sample_rate": 48000},
        issues=tracker.snapshot(),
        sampled_at=_HEALTH_SAMPLED_AT,
        session=rollup.snapshot(_HEALTH_SAMPLED_AT),
    )
    missing = sorted(DASHBOARD_AUDIO_HEALTH_KEYS - _payload_key_names(health))
    assert not missing, (
        "dashboard Audio view reads keys the composed audio-health snapshot "
        f"does not build: {missing}"
    )


# The transport-park card (sections.js transportParkCard) reads NESTED names,
# which the top-level `snap.*` sweep above cannot see. Each entry is the JS
# access spelling -> the payload key it must find, so a rename on either side
# fails here instead of silently blanking the one surface a browser can learn
# about a park from.
DASHBOARD_TRANSPORT_PARK_READS = {
    "park.status": "status",
    "park.parks": "parks",
    "park.converge_refused": "converge_refused",
    "park.error": "error",
    "entry.park_class": "park_class",
    "entry.issue": "issue",
    "entry.remedy": "remedy",
    "entry.detail": "detail",
}


#: A complete `<object>.<property>` access, bounded on both ends. Whole
#: tokens rather than a substring scan, because `park.converge_refused` is
#: CONTAINED IN `park.converge_refused_text` — containment would wave an
#: appending rename straight through the pin it exists to hold.
_JS_PROPERTY_ACCESS = re.compile(r"\b([A-Za-z_$][\w$]*\.[A-Za-z_$][\w$]*)(?![\w$])")


def _js_property_accesses(js: str) -> set[str]:
    return set(_JS_PROPERTY_ACCESS.findall(js))


def test_dashboard_transport_park_keys_exist_in_park_snapshot():
    """Every name the park card reads must survive the park reader → payload.

    Both snapshot shapes are driven from the real module: the healthy envelope
    (which carries `error` only on the unavailable branch, so a deliberately
    unreadable topology produces that one) plus a real `TransportPark` record
    for the per-park names.

    Both halves compare WHOLE names — a token set on the JS side, the payload's
    own key set on the other — so neither direction can be satisfied by a
    longer name that merely contains the pinned one.
    """
    from jasper.control import transport_park

    js = _js_property_accesses(_system_status_js_text())
    names = _payload_key_names(transport_park.snapshot(env={}))
    # The unavailable branch: an object `_assess` cannot classify.
    names |= _payload_key_names(transport_park.snapshot(topology=object(), env={}))
    names |= _payload_key_names(
        transport_park.TransportPark(
            park_class="a_shape", issue="#1", remedy="run this", detail="why",
        ).to_dict()
    )

    problems: list[str] = []
    for spelling, key in sorted(DASHBOARD_TRANSPORT_PARK_READS.items()):
        if spelling not in js:
            problems.append(
                f"contract pin stale: system-status JS no longer reads "
                f"{spelling} — update this test's pins"
            )
        if key not in names:
            problems.append(
                f"the park card reads {spelling} but transport_park.snapshot() "
                f"builds no {key!r} key — that row goes silently blank"
            )
    assert not problems, "\n".join(problems)
