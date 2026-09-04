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

import ast
import re
from functools import lru_cache
from pathlib import Path

from jasper.cli.aec_init import RECENT_WRITES_KEY, _reference_writes

REPO = Path(__file__).resolve().parents[1]
FANIN_STATE_RS = REPO / "rust" / "jasper-fanin" / "src" / "state.rs"
FANIN_CONFIG_RS = REPO / "rust" / "jasper-fanin" / "src" / "config.rs"
FANIN_MAIN_RS = REPO / "rust" / "jasper-fanin" / "src" / "main.rs"
OUTPUTD_STATE_RS = REPO / "rust" / "jasper-outputd" / "src" / "state.rs"


def _strip_comment_lines(text: str, *, markers: tuple[str, ...]) -> str:
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith(markers)
    )


# ---------------------------------------------------------------------------
# The emitter side: the NESTED key tree a hand-rolled Rust STATUS writer
# builds.
#
# LIMITATION: parsed from the writer source, never executed — the pytest lane
# has no cargo, and the daemon crates need ALSA headers to build at all — so
# the tree is the UNION of every conditional block (a key only one config
# reaches is still present), and a subtree that arrives as an opaque fragment
# from another crate is marked OPAQUE and accepts any path below it.
# ---------------------------------------------------------------------------

#: Child marker for a node whose subtree the source cannot show: a value
#: pushed as a pre-rendered fragment (``host_clock``, ``tap``) or an object
#: whose key is a runtime variable (``dac_content``'s per-transport path).
OPAQUE = "*"

_RUST_FN_RE = re.compile(r"^(?P<indent> *)(?:pub )?fn (?P<name>\w+)[(<]", re.MULTILINE)

#: One emitting step of a `String`-building writer, in source order.
_RUST_EMIT_RE = re.compile(
    r"""
      \w+\.push_str\(r\#""(?P<obj>\w+)":\{"\#\)      # opens "key":{
    | \w+\.push_str\(r\#""(?P<arr>\w+)":\["\#\)      # opens "key":[
    | \w+\.push_str\(r\#""(?P<frag>\w+)":"\#\)       # "key": <opaque fragment>
    | (?:push_str|format!)\(&?"\\"(?P<esc>\w+)\\":   # "key": built with format!
    | \w+\.push\('(?P<open>\{)'\)
    | \w+\.push\('(?P<close>[\}\]])'\)
    | (?:self\.)?(?P<helper>push_\w+)\(\s*(?:&mut\s+)?\w+,\s*"(?P<key>\w+)"
    | (?:self\.)?push_kv_\w+\(\s*(?:&mut\s+)?\w+,\s*(?P<dynamic>[a-z_][\w.]*)\s*,
    | (?:self\.)?(?P<inline>push_\w+_json)\(\s*(?:&mut\s+)?\w+\s*\)
    """,
    re.VERBOSE,
)

#: A `fn(buf, key, ..)` helper that nests an object under its `key` argument
#: (`push_dll_rate_diff`), as opposed to the `push_kv_*` scalar writers.
_HELPER_OPENS_OBJECT = re.compile(r'push_str\(r\#"":[\{\[]"\#\)')


def _rust_fn_bodies(src: str) -> dict[str, str]:
    """Every `fn` body in rustfmt'd source, keyed by name.

    Bodies are cut on the closing brace at the signature's own indent, which
    brace counting cannot do here: the writers push literal `{` and `}` as
    JSON text.
    """
    bodies: dict[str, str] = {}
    lines = src.splitlines()
    for match in _RUST_FN_RE.finditer(src):
        start = src[: match.start()].count("\n")
        close = f"{' ' * len(match.group('indent'))}}}"
        for end in range(start + 1, len(lines)):
            if lines[end] == close:
                bodies[match.group("name")] = "\n".join(lines[start + 1 : end])
                break
    return bodies


def _parse_rust_emitter(
    body: str, node: dict, bodies: dict[str, str], seen: frozenset[str],
) -> None:
    stack = [node]
    for match in _RUST_EMIT_RE.finditer(body):
        top = stack[-1]
        opened = match.group("obj") or match.group("arr")
        if opened:
            stack.append(top.setdefault(opened, {}))
        elif match.group("frag"):
            top.setdefault(match.group("frag"), {})[OPAQUE] = {}
        elif match.group("esc"):
            top.setdefault(match.group("esc"), {})
        elif match.group("open"):
            # An anonymous object: the payload's own opening brace, or one
            # array element. Its keys belong to the node already on top.
            stack.append(top)
        elif match.group("close"):
            if len(stack) > 1:
                stack.pop()
        elif match.group("helper"):
            child = top.setdefault(match.group("key"), {})
            helper = bodies.get(match.group("helper"))
            if helper and match.group("helper") not in seen:
                if _HELPER_OPENS_OBJECT.search(helper):
                    _parse_rust_emitter(
                        helper, child, bodies, seen | {match.group("helper")},
                    )
        elif match.group("dynamic"):
            top[OPAQUE] = {}
        elif match.group("inline"):
            helper = bodies.get(match.group("inline"))
            if helper and match.group("inline") not in seen:
                _parse_rust_emitter(
                    helper, top, bodies, seen | {match.group("inline")},
                )


@lru_cache(maxsize=None)
def _rust_status_key_tree(path: Path) -> dict:
    """The nested key structure ``snapshot_json`` emits, as nested dicts.

    Test-only assertions are cut at ``#[cfg(test)]`` so they cannot satisfy
    the production contract.
    """
    src = path.read_text().split("#[cfg(test)]", 1)[0]
    src = _strip_comment_lines(src, markers=("//",))
    bodies = _rust_fn_bodies(src)
    tree: dict = {}
    _parse_rust_emitter(bodies["snapshot_json"], tree, bodies, frozenset())
    return tree


def _rust_emitted_json_keys(path: Path) -> set[str]:
    """Every key name the emitter produces, at any depth — nesting flattened
    away, which is all a consumer read through an already-pinned parent needs.
    """
    def flatten(node: dict) -> set[str]:
        names: set[str] = set()
        for name, child in node.items():
            if name == OPAQUE:
                continue
            names.add(name)
            names |= flatten(child)
        return names

    return flatten(_rust_status_key_tree(path))


def _emits_path(tree: dict, path: tuple[str, ...]) -> bool:
    node = tree
    for name in path:
        if OPAQUE in node:
            return True
        if name not in node:
            return False
        node = node[name]
    return True


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
# scanned HERE instead.
#
# Names only. These are the reads section 1b's AST walk cannot resolve —
# per-input blocks keyed by a runtime label, payloads passed to another
# module's helper — so the key must be pinned to a consumer by hand.
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
    "jasper/cli/doctor/audio_runtime_fanin.py": {
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


# ---------------------------------------------------------------------------
# 1b. STATUS JSON key PATHS — the same seam, with nesting modeled
#
# The flat key-name check above cannot see WHERE a key sits: `/state` read
# outputd's `aec_clock` at the top level while outputd nests it under
# `reference_outputs`, and shipped null fields for months with the flat guard
# green. This half walks each Python consumer's AST for the dotted paths it
# reads out of a STATUS payload and asserts the emitter builds each one at
# that depth. The flat check stays for what the walk cannot resolve — a
# per-input read keyed by a runtime label, a payload handed to another
# module's helper.
# ---------------------------------------------------------------------------

#: Single-argument calls that hand back the same mapping (`_mapping` coerces a
#: non-Mapping to `{}`), and attributes that unwrap an evidence record.
_TRANSPARENT_CALLS = frozenset({"_mapping", "dict"})
_TRANSPARENT_ATTRS = frozenset({"payload"})


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _resolve_read(node: ast.AST, env: dict[str, tuple[str, ...]]) -> tuple[str, ...] | None:
    """The `(root, key, key, ...)` an expression reads, or None.

    Follows the fail-soft spellings these consumers actually use: `.get("k")`
    with or without a default, `["k"]`, `x.get("k") or {}`, and the
    `x.get("k") if isinstance(...) else None` guard.
    """
    if isinstance(node, ast.Name):
        return env.get(node.id, (node.id,))
    if isinstance(node, ast.Attribute):
        if node.attr in _TRANSPARENT_ATTRS:
            return _resolve_read(node.value, env)
        return None
    if isinstance(node, ast.Subscript):
        base = _resolve_read(node.value, env)
        key = node.slice
        if base and isinstance(key, ast.Constant) and isinstance(key.value, str):
            return base + (key.value,)
        return None
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        return _resolve_read(node.values[0], env)
    if isinstance(node, ast.IfExp):
        return _resolve_read(node.body, env) or _resolve_read(node.orelse, env)
    if isinstance(node, ast.Call):
        name = _dotted_name(node.func)
        if name in _TRANSPARENT_CALLS and node.args:
            return _resolve_read(node.args[0], env)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            base = _resolve_read(node.func.value, env)
            return base + (node.args[0].value,) if base else None
        # A zero-argument producer: `evidence.outputd_status()`.
        return (name,) if name and not node.args else None
    return None


def _record_reads(node: ast.AST, env: dict, out: set[tuple[str, ...]]) -> None:
    resolved = _resolve_read(node, env)
    if resolved:
        out.add(resolved)
    for child in ast.iter_child_nodes(node):
        _record_reads(child, env, out)


def _walk_reads(node: ast.AST, env: dict, out: set[tuple[str, ...]]) -> None:
    """Collect every resolvable read, tracking `name = <read>` aliases.

    Aliases are what make the walk see anything at all: every consumer binds
    the block first (`refs = status.get("reference_outputs")`) and reads keys
    off the binding. A rebinding to something unresolvable drops the alias, so
    a name reused for an unrelated value cannot forge a path.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        inner = dict(env)
        args = node.args
        for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
            inner.pop(arg.arg, None)
        for child in ast.iter_child_nodes(node):
            _walk_reads(child, inner, out)
        return
    if isinstance(node, ast.expr):
        _record_reads(node, env, out)
        return
    if (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ):
        _record_reads(node.value, env, out)
        resolved = _resolve_read(node.value, env)
        name = node.targets[0].id
        if resolved is None:
            env.pop(name, None)
        else:
            env[name] = resolved
        return
    for child in ast.iter_child_nodes(node):
        _walk_reads(child, env, out)


def _python_status_paths(path: Path, roots: dict[str, str]) -> dict[str, set[tuple[str, ...]]]:
    """Dotted paths a consumer reads, grouped by the daemon that emitted them."""
    reads: set[tuple[str, ...]] = set()
    _walk_reads(ast.parse(path.read_text()), {}, reads)
    found: dict[str, set[tuple[str, ...]]] = {}
    for read in reads:
        daemon = roots.get(read[0])
        if daemon and len(read) > 1:
            found.setdefault(daemon, set()).add(read[1:])
    return found


#: consumer file -> where a STATUS payload ENTERS it -> the daemon that sent
#: it. A root is a parameter/local name, or a zero-argument producer call
#: (`evidence.outputd_status()`), whose value is the whole payload.
STATUS_PATH_CONSUMERS: dict[str, dict[str, str]] = {
    "jasper/control/state_aggregate.py": {
        "outputd_status": "outputd",
        "fanin_status": "fanin",
    },
    # `outputd` here is the raw payload of `_read_local_status(OUTPUTD_SOCKET)`.
    # Its fan-in half is NOT a root: `current["fanin"]` is the AirPlay
    # sampler's normalized model, not what jasper-fanin emitted.
    "jasper/control/audio_health.py": {"outputd": "outputd"},
    "jasper/audio_validation.py": {
        "outputd_status": "outputd",
        "first_outputd": "outputd",
        "final_outputd": "outputd",
        "preflight_outputd": "outputd",
    },
    "jasper/cli/doctor/audio_runtime_fanin.py": {"evidence.fanin_status": "fanin"},
    "jasper/cli/doctor/audio_runtime_outputd.py": {
        "evidence.outputd_status": "outputd",
    },
    "jasper/cli/doctor/audio_runtime_ring.py": {"evidence.fanin_status": "fanin"},
    "jasper/cli/doctor/aec.py": {
        "evidence.outputd_status": "outputd",
        "outputd_status": "outputd",
    },
    "jasper/cli/doctor/usbsink.py": {
        "evidence.fanin_status": "fanin",
        "fanin_status": "fanin",
    },
    # doctor/grouping.py only FORWARDS `evidence.outputd_status().payload`;
    # the keys are read here, under the parameter it forwards into.
    "jasper/multiroom/state.py": {"local_outputd_status": "outputd"},
}

STATUS_RS = {"outputd": OUTPUTD_STATE_RS, "fanin": FANIN_STATE_RS}

#: (consumer, dotted path) -> why a read of a path no daemon emits stands.
#: Each entry fails once it stops being read, or once the daemon starts
#: emitting the path — remove it then.
STATUS_PATH_EXCEPTIONS: dict[tuple[str, str], str] = {
    # outputd emits `dac.pcm` but never `dac.card`; `_dac_details` falls
    # through to JASPER_AUDIO_DAC_CARD, so the read is dead rather than
    # broken. REMOVAL CONDITION: goes when the dead read goes.
    ("jasper/audio_validation.py", "dac.card"): "always-None read; env fallback owns the value",
}


def test_python_status_reads_match_the_rust_emitters_nesting():
    problems: list[str] = []
    for consumer_rel, roots in STATUS_PATH_CONSUMERS.items():
        src = (REPO / consumer_rel).read_text()
        for root in roots:
            if root not in src:
                problems.append(
                    f"contract pin stale: {consumer_rel} no longer names the "
                    f"STATUS payload {root!r} — update this test's roots"
                )
        found = _python_status_paths(REPO / consumer_rel, roots)
        if not found:
            problems.append(
                f"contract pin stale: no STATUS path resolves in "
                f"{consumer_rel} — its reads moved, or the roots did"
            )
        for daemon, paths in sorted(found.items()):
            tree = _rust_status_key_tree(STATUS_RS[daemon])
            # The one way this guard passes vacuously: an OPAQUE at the root
            # accepts every path under it.
            assert OPAQUE not in tree, f"{daemon} emitter tree — extractor broke?"
            for path in sorted(paths):
                dotted = ".".join(path)
                if _emits_path(tree, path):
                    continue
                if (consumer_rel, dotted) in STATUS_PATH_EXCEPTIONS:
                    continue
                problems.append(
                    f"{consumer_rel} reads STATUS path {dotted!r} that "
                    f"{STATUS_RS[daemon].relative_to(REPO)} does not emit at "
                    f"that depth — a silently null field"
                )
    assert not problems, "\n".join(problems)


def test_status_path_exceptions_stay_accurate():
    for (consumer_rel, dotted), reason in STATUS_PATH_EXCEPTIONS.items():
        roots = STATUS_PATH_CONSUMERS[consumer_rel]
        found = _python_status_paths(REPO / consumer_rel, roots)
        path = tuple(dotted.split("."))
        daemons = [d for d, paths in found.items() if path in paths]
        assert daemons, (
            f"exception ({consumer_rel}, {dotted}) ({reason}) is no longer "
            f"read — dead entry; remove it."
        )
        for daemon in daemons:
            assert not _emits_path(_rust_status_key_tree(STATUS_RS[daemon]), path), (
                f"exception ({consumer_rel}, {dotted}) ({reason}) IS emitted "
                f"now — the contract is live; remove the exception."
            )


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
    config.rs reads no env override), outputd's unit pins the env explicitly,
    and mux's is hardcoded on both sides — fan-in's source-notify thread spells
    it in Rust, so it is pinned against the Python constant here. Every Python
    consumer below resolves the shared constant, so
    the VALUE each process will connect to is asserted here: if either daemon
    moves its socket, every consumer moves with it in the same PR.

    Two are deliberately absent. ``jasper.correction.runtime_integrity`` still
    spells the outputd path itself (measurement corner). ``jasper.mux`` resolves
    ``JASPER_FANIN_CONTROL_SOCKET`` at import time, so an operator exercising
    that documented override would redden this; its default IS the shared
    constant by construction, and ``tests/test_mux.py`` owns the override.
    """
    from jasper import audio_validation, mux, renderer
    from jasper.cli import system_soak
    from jasper.cli.doctor import audio_runtime_fanin, audio_runtime_outputd
    from jasper.control import audio_health, grouping_supervisor, uds
    from jasper.correction import runtime_integrity
    from jasper.fanin import status as fanin_status
    from jasper.peering.config import PEERING_UDS_PATH
    from jasper.route_latency import status_socket, tap_client

    from .doctor_test_support import _fresh_cfg

    fanin_sock = "/run/jasper-fanin/control.sock"
    outputd_sock = "/run/jasper-outputd/control.sock"
    mux_sock = "/run/jasper-mux/control.sock"

    assert f'"{fanin_sock}"' in FANIN_CONFIG_RS.read_text()
    assert f'"{mux_sock}"' in FANIN_MAIN_RS.read_text()
    unit = (REPO / "deploy" / "systemd" / "jasper-outputd.service").read_text()
    assert f'Environment="JASPER_OUTPUTD_CONTROL_SOCKET={outputd_sock}"' in unit

    assert {
        status_socket.FANIN_STATUS_SOCKET,
        fanin_status.FANIN_STATUS_SOCKET,
        runtime_integrity.FANIN_CONTROL_SOCKET,
        tap_client.FANIN_CONTROL_SOCKET,
        audio_runtime_fanin.FANIN_STATUS_SOCKET,
        system_soak.STATUS_SOCKETS["fanin"],
    } == {fanin_sock}
    assert {
        status_socket.OUTPUTD_STATUS_SOCKET,
        runtime_integrity.OUTPUTD_CONTROL_SOCKET,
        grouping_supervisor.OUTPUTD_CONTROL_SOCKET,
        audio_runtime_outputd.OUTPUTD_STATUS_SOCKET,
        str(audio_validation.DEFAULT_OUTPUTD_STATUS_SOCKET),
        system_soak.STATUS_SOCKETS["outputd"],
    } == {outputd_sock}
    assert {
        status_socket.MUX_CONTROL_SOCKET_PATH,
        mux.MUX_CONTROL_SOCKET_PATH,
        renderer.MUX_CONTROL_SOCKET_PATH,
        uds.MUX_CONTROL_SOCKET_PATH,
        audio_health.MUX_CONTROL_SOCKET_PATH,
        system_soak.STATUS_SOCKETS["mux"],
    } == {mux_sock}

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
