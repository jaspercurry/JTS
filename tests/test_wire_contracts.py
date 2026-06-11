"""Cross-language / cross-process wire-contract guards.

These tests pin the *names and shapes* that cross a language or process
boundary — Rust daemon → Python consumer, bash writer → Rust reader,
Python HTTP payload → dashboard ES module. Every consumer on these seams
is fail-soft by design (a missing key degrades to null / a blank card /
a silently-ignored env var), so drift never throws at runtime; it just
quietly blanks a surface. The guards make that drift a loud test
failure that names both sides of the seam.

Style: static greps of source text (the established `test_outputd_wiring.py`
technique) — no cargo, no daemons. Each contract entry is pinned twice:
the producing side must emit the name, and the consuming side must still
reference it (so a stale pin in this file is itself caught).
"""
from __future__ import annotations

import re
from pathlib import Path

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

    Matches both helper calls (``push_kv_*(&mut buf, "key", ...)``,
    including the ``_opt`` variants) and inline object/array openers
    (``buf.push_str(r#""key":...``). Flat set — nesting is not modeled;
    the contract below pins "this name exists somewhere in the
    snapshot", which is what a fail-soft ``.get()`` consumer needs.
    """
    src = _strip_comment_lines(path.read_text(), markers=("//",))
    keys: set[str] = set()
    keys.update(re.findall(r'push_kv_\w+\(\s*&mut buf,\s*"(\w+)"', src))
    keys.update(re.findall(r'push_str\(r#""(\w+)":', src))
    return keys


# ---------------------------------------------------------------------------
# 1. fan-in / outputd STATUS JSON — Rust emitter vs Python consumers
#
# jasper-fanin and jasper-outputd answer `STATUS\n` on their control UDS
# with hand-rolled JSON (rust/*/src/state.rs). Python consumers read it
# with fail-soft .get() chains, so a renamed Rust key silently turns
# into None on /state, in the doctor, and in correction integrity
# snapshots. Pin: every key a Python consumer reads must be emitted by
# the Rust snapshot, and must still appear in the consumer source.
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
        "buffer_frames", "input_buffer_frames", "selected_input",
        "watchdog", "last_progress_age_ms", "pings_skipped",
    },
    # check_fanin_service
    "jasper/cli/doctor/audio.py": {
        "output", "pcm", "frames_written", "xrun_count", "buffer_frames",
        "inputs", "label", "input_buffer_frames",
        "watchdog", "last_progress_age_ms",
    },
}

OUTPUTD_STATUS_CONSUMERS: dict[str, set[str]] = {
    # _outputd_dac_status_check / _dac_reference_check / counter deltas
    "jasper/audio_validation.py": {
        "dac", "pcm", "sample_rate", "period_frames", "buffer_frames",
        "frames_written", "xrun_count",
        "reference_outputs", "speaker_reference_source",
        "speaker_reference_active", "speaker_reference_channels",
        "chip_ref_pcm", "chip_ref_sample_rate", "chip_ref_period_frames",
        "chip_ref_buffer_frames", "udp_target",
    },
    # check_outputd_service
    "jasper/cli/doctor/audio.py": {
        "backend", "sink_mode", "content", "dac", "pcm",
        "reference_outputs", "speaker_reference_source",
        "speaker_reference_active", "speaker_reference_channels",
        "udp_target", "chip_ref_pcm",
    },
}


def _assert_status_contract(
    emitter: Path, consumers: dict[str, set[str]],
) -> None:
    emitted = _rust_emitted_json_keys(emitter)
    assert emitted, f"no JSON keys extracted from {emitter} — extractor broke?"
    problems: list[str] = []
    for consumer_rel, keys in consumers.items():
        consumer = REPO / consumer_rel
        src = consumer.read_text()
        for key in sorted(keys):
            if key not in emitted:
                problems.append(
                    f"{consumer_rel} reads STATUS key {key!r} that "
                    f"{emitter.relative_to(REPO)} no longer emits"
                )
            if f'"{key}"' not in src and f"'{key}'" not in src:
                problems.append(
                    f"contract pin stale: {consumer_rel} no longer "
                    f"references {key!r} — update this test's pins"
                )
    assert not problems, "\n".join(problems)


def test_fanin_status_keys_match_python_consumers():
    _assert_status_contract(FANIN_STATE_RS, FANIN_STATUS_CONSUMERS)


def test_outputd_status_keys_match_python_consumers():
    _assert_status_contract(OUTPUTD_STATE_RS, OUTPUTD_STATUS_CONSUMERS)


def test_fanin_control_command_vocabulary_matches_mux():
    """mux drives fan-in's source gate over the UDS with a one-line text
    command. Pin the verbs on both sides, plus the error-shape key mux
    raises on."""
    state_rs = FANIN_STATE_RS.read_text()
    mux_py = (REPO / "jasper" / "mux.py").read_text()
    for verb in ('"STATUS"', '"AUTO"', '"NONE"', '"SELECT '):
        assert verb in state_rs, f"fanin state.rs no longer handles {verb}"
    assert '_fanin_command(f"SELECT {label}")' in mux_py
    assert '_fanin_command("AUTO")' in mux_py
    assert '_fanin_command("NONE")' in mux_py
    # state.rs error responses carry {"error": ...}; mux raises on it.
    assert '"error":' in state_rs
    assert '"error" in payload' in mux_py


def test_control_socket_paths_agree_across_processes():
    """The fan-in control socket path is a hardcoded Rust constant (no
    env override is read by fanin's config.rs); every Python consumer
    hardcodes the same literal. Same for outputd, where the unit pins
    the env explicitly. If either daemon moves its socket, every
    consumer here must move with it in the same PR.
    """
    fanin_sock = "/run/jasper-fanin/control.sock"
    outputd_sock = "/run/jasper-outputd/control.sock"

    assert f'"{fanin_sock}"' in FANIN_CONFIG_RS.read_text()
    for rel in (
        "jasper/mux.py",
        "jasper/control/airplay_health.py",
        "jasper/control/server.py",
        "jasper/cli/doctor/audio.py",
        "jasper/cli/system_soak.py",
        "jasper/correction/runtime_integrity.py",
    ):
        assert fanin_sock in (REPO / rel).read_text(), (
            f"{rel} no longer pins the fan-in control socket {fanin_sock}"
        )

    unit = (REPO / "deploy" / "systemd" / "jasper-outputd.service").read_text()
    assert f'Environment="JASPER_OUTPUTD_CONTROL_SOCKET={outputd_sock}"' in unit
    for rel in (
        "jasper/audio_validation.py",
        "jasper/control/server.py",
        "jasper/cli/doctor/audio.py",
        "jasper/cli/system_soak.py",
    ):
        assert outputd_sock in (REPO / rel).read_text(), (
            f"{rel} no longer pins the outputd control socket {outputd_sock}"
        )


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
    # CamillaDSP, so the env is no longer written anywhere. See
    # HANDOFF-multiroom.md §2 "Stranded by this design".)
    # Staged inv-2 leader content lane (LEADER_CONTENT_LANE_GATE in
    # jasper/multiroom/reconcile.py); the outputd reader is a future
    # Rust PR (Increment 3).
    "JASPER_OUTPUTD_DAC_CONTENT_FIFO": "staged inv-2 member DAC lane",
    # Python-consumer-side override of where mux CONNECTS; fanin's own
    # bind path is a hardcoded Rust constant (see
    # test_control_socket_paths_agree_across_processes). Setting this
    # alone cannot move fan-in's socket.
    "JASPER_FANIN_CONTROL_SOCKET": "mux connect-path knob, not a fanin knob",
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
# silently blanks the card. Pin: every `snap.<key>` the ES modules read
# must be a key `_get_system_snapshot` builds, and the metric names the
# vitals/network cards read from `metrics.current` must exist in the
# system_metrics sampler. The airplay card's nested reads are pinned
# against the AirPlayHealthSampler snapshot.
# ---------------------------------------------------------------------------

_SYSTEM_STATUS_JS_DIR = REPO / "deploy" / "assets" / "system-status" / "js"


def _system_status_js_text() -> str:
    return "\n".join(
        p.read_text() for p in sorted(_SYSTEM_STATUS_JS_DIR.glob("*.js"))
    )


def _server_snapshot_region() -> str:
    src = (REPO / "jasper" / "control" / "server.py").read_text()
    start = src.index("def _get_system_snapshot")
    end = src.index("def _get_system_diagnostics")
    return src[start:end]


def test_dashboard_snapshot_top_level_keys_exist_in_server_payload():
    js = _system_status_js_text()
    snap_keys = set(re.findall(r"\bsnap\.([a-z_0-9]+)\b", js))
    assert snap_keys, "no snap.* reads found in system-status JS — extractor broke?"
    region = _server_snapshot_region()
    problems = [
        f"dashboard JS reads snap.{key} but _get_system_snapshot in "
        f"jasper/control/server.py builds no {key!r} key — that card "
        f"goes silently blank"
        for key in sorted(snap_keys)
        if f'"{key}"' not in region
    ]
    assert not problems, "\n".join(problems)


# Metric names the vitals / network / software cards read from
# snap.metrics.current (vitalsCards / networkList / softwareList in
# sections.js + views.js). The airplay card's `cur` is a different
# object (snap.airplay_health.current) — pinned separately below.
DASHBOARD_METRICS_CURRENT_KEYS = {
    "mem_total_mb", "temp_c", "throttled_now", "throttled_history",
    "fan_present", "fan_rpm", "fan_pwm", "disk_used_pct", "disk_total_gb",
    "uptime_sec", "net_rx_bytes", "net_tx_bytes", "per_core_cpu_pct",
}


def test_dashboard_metrics_current_keys_exist_in_sampler():
    js = _system_status_js_text()
    sampler = (REPO / "jasper" / "control" / "system_metrics.py").read_text()
    problems: list[str] = []
    for key in sorted(DASHBOARD_METRICS_CURRENT_KEYS):
        if f'"{key}":' not in sampler:
            problems.append(
                f"dashboard reads metrics.current.{key} but "
                f"jasper/control/system_metrics.py produces no {key!r}"
            )
        if f"cur.{key}" not in js:
            problems.append(
                f"contract pin stale: system-status JS no longer reads "
                f"cur.{key} — update this test's pins"
            )
    assert not problems, "\n".join(problems)


# airplayBody(hp, ...) reads hp.current.{fanin,mpris,camilla} plus these
# nested fields; all are built by AirPlayHealthSampler.
DASHBOARD_AIRPLAY_CURRENT_KEYS = {
    "fanin", "mpris", "camilla", "available", "input_buffer_frames",
    "output_buffer_frames", "frames_per_sec", "xrun_count",
    "buffer_frames",
}


def test_dashboard_airplay_card_keys_exist_in_health_sampler():
    sampler = (REPO / "jasper" / "control" / "airplay_health.py").read_text()
    missing = [
        key for key in sorted(DASHBOARD_AIRPLAY_CURRENT_KEYS)
        if f'"{key}"' not in sampler
    ]
    assert not missing, (
        "dashboard airplay card reads keys the AirPlayHealthSampler "
        f"snapshot does not build: {missing} "
        "(jasper/control/airplay_health.py vs "
        "deploy/assets/system-status/js/sections.js airplayBody)"
    )
