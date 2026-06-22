# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Regression guards for the 2026-06-04 documentation-staleness sweep.

Each test pins a doc/comment claim that had drifted from the code to the
live code value, so the same drift re-reddens if either side moves. All
hardware-free — pure source / docstring inspection, no device or network.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_config_wake_threshold_default_is_0_30(monkeypatch):
    """AGENTS.md item 1: the wake-threshold default is 0.30, not 0.50."""
    from jasper.config import Config

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.delenv("JASPER_WAKE_THRESHOLD", raising=False)
    cfg = Config.from_env()
    assert cfg.wake_threshold == 0.3

    agents = _read("AGENTS.md")
    assert "default 0.30" in agents
    assert "default 0.50" not in agents


def test_gmail_unread_summary_param_is_limit_not_max():
    """gmail.py item 8: the module docstring names the real param `limit`."""
    import jasper.tools.gmail as gmail

    doc = gmail.__doc__ or ""
    assert "gmail_unread_summary(limit=" in doc
    assert "gmail_unread_summary(max=" not in doc

    # And the real factory-built tool really takes `limit`, not `max`.
    src = inspect.getsource(gmail.make_gmail_tools)
    assert "async def gmail_unread_summary(limit:" in src
    assert "async def gmail_unread_summary(max" not in src


def test_wake_corpus_default_bind_is_loopback_and_doc_uses_nginx_path():
    """wake_corpus_setup.py item 11: default bind is loopback; the usage
    example points at the nginx-fronted /wake-corpus/ path, not :8782."""
    import jasper.web.wake_corpus_setup as wc

    assert wc.DEFAULT_HOST == "127.0.0.1"
    doc = wc.__doc__ or ""
    assert "http://jts.local/wake-corpus/" in doc
    assert "http://jts.local:8782/" not in doc


def test_system_supervisor_rationale_says_any_fail_not_all_fail():
    """system_supervisor.py item 9: _run_all_probes returns on the FIRST
    failing probe, and the module docstring rationale matches that."""
    import jasper.control.system_supervisor as ss

    # The code: returns on first failure (any-fail).
    src = inspect.getsource(ss.SystemSupervisor._run_all_probes)
    assert src.count("return False,") >= 2  # one per probe, short-circuit

    doc = ss.__doc__ or ""
    assert "any-fail, not all-fail" in doc
    # The stale "all 3 probes are failing" framing must be gone.
    assert "all 3 probes are failing" not in doc


def test_fanin_canonical_lane_list_is_compiled_default_in_config_rs():
    """audio-paths.md item 7: the canonical lane list lives as the
    compiled-in default arrays in rust/jasper-fanin/src/config.rs."""
    cfg_rs = _read("rust/jasper-fanin/src/config.rs")
    # The defaults really are baked into from_env.
    assert "pub fn from_env()" in cfg_rs
    assert '"JASPER_FANIN_INPUT_PCMS"' in cfg_rs
    assert '"spotify", "airplay"' in cfg_rs  # default renderer list

    doc = _read("docs/audio-paths.md")
    assert "rust/jasper-fanin/src/config.rs" in doc
    assert "optional override" in doc


def test_wake_telemetry_doc_uses_1gb_not_500mb(monkeypatch):
    """HANDOFF-wake-telemetry.md item 4: ring buffer is 1 GB (the
    production default), with no stray 500 MB or 'once per hour' claims."""
    from jasper.config import Config

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("JASPER_VOICE_PROVIDER", "gemini")
    monkeypatch.delenv("JASPER_WAKE_EVENTS_MAX_AUDIO_BYTES", raising=False)
    cfg = Config.from_env()
    assert cfg.wake_events_max_audio_bytes == 1024 * 1024 * 1024

    doc = _read("docs/HANDOFF-wake-telemetry.md")
    assert "500 MB" not in doc
    assert "once per hour" not in doc
    # Footer must be at least as fresh as the 2026-06-04 sweep's
    # re-verification. Not an exact-date pin: legitimate later
    # re-verifications bump the footer (AGENTS.md doc rule 3).
    m = re.search(r"Last verified: (\d{4}-\d{2}-\d{2})", doc)
    assert m, "HANDOFF-wake-telemetry.md is missing its Last verified footer"
    assert m.group(1) >= "2026-06-05"

    import jasper.wake_events as we

    assert we.DEFAULT_MAX_AUDIO_BYTES == 1024 * 1024 * 1024
    assert "500 MB cap" not in (we.__doc__ or "")


def test_correction_init_points_at_real_apply_path():
    """correction/__init__.py item 6: the public-surface comment names
    the real apply path (emit_sound_config), not the removed legacy emitter."""
    import jasper.correction as corr

    doc = corr.__doc__ or ""
    assert "emit_sound_config" in doc
    assert "emit_correction_config" not in doc


def test_security_doc_scopes_dns_rebinding_to_control_api():
    """SECURITY.md item 5: the jasper-control API and wizard read/write
    surfaces all name the DNS-rebinding/browser-origin guards.

    (Originally this asserted the wizards had NO guard. The mutating-POST
    gap was since closed by wiring `mutating_request_allowed` into the
    shared `guard_mutating_request()` chokepoint; the GET gap was then closed
    through `guard_read_request()`, so the invariant moved with it again.)"""
    server = _read("jasper/control/server.py")
    assert "from ..http_security import" in server
    # The wizard read and mutating chokepoints now apply the same guard family.
    common = _read("jasper/web/_common.py")
    assert "mutating_request_allowed" in common
    assert "management_read_allowed" in common
    sec = _read("SECURITY.md")
    assert "jasper-control" in sec
    assert "state-changing requests and GET routes" in sec
    assert "read masked" in sec


def test_ci_comment_credits_conftest_fixture_for_env_leak():
    """tests.yml + conftest item 10: CI comment credits the autouse
    fixture (#256) for containing the OPENAI_API_KEY env leak."""
    ci = _read(".github/workflows/tests.yml")
    assert "#256" in ci
    assert "cost discipline" in ci

    conftest = _read("tests/conftest.py")
    assert "#256" in conftest


def test_readme_atlas_lists_canonical_ui_migration_doc():
    """README item 12: the historical canonical-ui-migration handoff is
    no longer an orphan — it has an atlas entry."""
    readme = _read("README.md")
    assert "HANDOFF-canonical-ui-migration.md" in readme
    # The doc file it references actually exists.
    assert (REPO / "docs" / "HANDOFF-canonical-ui-migration.md").exists()
