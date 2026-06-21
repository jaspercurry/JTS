# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.avahi_service — the ONE Avahi *.service renderer.

This is the shared render+guard+atomic-write+reload body that both
``jasper/control_advert.py`` (``_jasper-control._tcp``, free-form name)
and ``jasper/peering/avahi.py`` (``_jasper-peer._udp``, mDNS-safe
metadata) route through. The per-caller wrappers have their own suites
(tests/test_control_advert.py, tests/test_peering_avahi.py); this file
pins the extracted primitive directly so a refactor of either caller
can't silently change the shared contract.

The contract:

  - ``render_service`` fills the ``substitutions`` tokens (FULL ``__..__``
    tokens) into the template and atomic-writes a 0644 file. It returns a
    3-state ``RenderResult`` (``WROTE`` / ``UNCHANGED`` / ``FAILED``), NOT
    a bool — the distinction WROTE-vs-UNCHANGED lets a caller reload only
    on a real on-disk change without re-reading the output file to diff.
  - ``escape=True`` runs each value through ``xml.sax.saxutils.escape``
    first, so a hostile value (``& < > "``) stays WELL-FORMED XML and
    round-trips out of the parsed element unchanged. This is the
    load-bearing safety property — a botched escape would make Avahi
    reject the whole ``<service-group>``.
  - A leftover ``__FOO__`` placeholder (caller missed a substitution)
    is refused: returns ``FAILED``, writes nothing.
  - Idempotence: a byte-stable render returns ``UNCHANGED`` and skips the
    write+reload entirely (asserted via a write counter so a long-lived
    advert never tears down + re-adds its service-group).
  - Every failure path is FAIL-SOFT and NEVER raises: a missing template
    returns ``FAILED``; an OSError on write returns ``FAILED``.
  - ``reload`` is mocked so no test shells out to systemctl, and it fires
    ONLY on ``WROTE``.

Renders into ``tmp_path`` so we never touch real /etc/avahi/services.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

import pytest

from jasper import avahi_service
from jasper.avahi_service import RenderResult


# A minimal but realistic Avahi service template with two placeholder
# tokens. Mirrors the shape of the real jasper-peer template.
_TEMPLATE = """<?xml version="1.0" standalone='no'?>
<service-group>
  <name replace-wildcards="yes">JTS on %h</name>
  <service>
    <type>_jasper-test._tcp</type>
    <port>8780</port>
    <txt-record>name=__SPEAKER_NAME__</txt-record>
    <txt-record>room=__ROOM__</txt-record>
  </service>
</service-group>
"""

# Exercises every XML metacharacter at once. If escaping is wrong the
# minidom/ElementTree parse below raises (and Avahi would reject the group).
_HOSTILE = 'A & <b> "x" \''


@pytest.fixture(autouse=True)
def _mock_reload(monkeypatch):
    """Mock the shared avahi-daemon reload for every test so none shells
    out. Returns a recorder so a test can assert it was / wasn't called.
    ``render_service`` reloads via this module's ``reload_avahi``."""
    calls: list = []
    monkeypatch.setattr(avahi_service, "reload_avahi", lambda: calls.append(1))
    return calls


@pytest.fixture
def template(tmp_path) -> Path:
    p = tmp_path / "template.service"
    p.write_text(_TEMPLATE)
    return p


def _txt_records(xml_text: str) -> list[str]:
    """All ``<txt-record>`` text values, parsed with minidom. Parsing at
    all is the load-bearing assertion: it proves the file is well-formed."""
    doc = minidom.parseString(xml_text)
    return [
        n.firstChild.data if n.firstChild else ""
        for n in doc.getElementsByTagName("txt-record")
    ]


# ----------------------------------------------------------------------
# Happy path — tokens fill into a valid file; reload fires.
# ----------------------------------------------------------------------


def test_render_service_fills_tokens_into_valid_file(template, tmp_path, _mock_reload):
    out = tmp_path / "rendered.service"
    res = avahi_service.render_service(
        str(template), str(out),
        {"__SPEAKER_NAME__": "Kitchen", "__ROOM__": "Upstairs"},
    )
    # First render writes the file → WROTE.
    assert res is RenderResult.WROTE
    text = out.read_text()
    # Both tokens consumed, values present, file is well-formed XML.
    assert "__SPEAKER_NAME__" not in text and "__ROOM__" not in text
    assert _txt_records(text) == ["name=Kitchen", "room=Upstairs"]
    ET.fromstring(text)  # second independent parser — raises if malformed
    # A write happened, so a reload was attempted.
    assert _mock_reload == [1]


def test_render_service_writes_mode_0644(template, tmp_path, _mock_reload):
    out = tmp_path / "rendered.service"
    avahi_service.render_service(
        str(template), str(out),
        {"__SPEAKER_NAME__": "x", "__ROOM__": "y"},
    )
    assert (out.stat().st_mode & 0o777) == 0o644


def test_render_service_full_tokens_are_the_keys(template, tmp_path, _mock_reload):
    """Substitution keys are the FULL ``__..__`` tokens, not bare names —
    a bare-name key would leave the placeholder and trip the stray guard."""
    out = tmp_path / "rendered.service"
    # Correct (full-token) keys succeed; the placeholder-guard test below
    # pins that a missing/partial substitution is refused.
    res = avahi_service.render_service(
        str(template), str(out),
        {"__SPEAKER_NAME__": "Den", "__ROOM__": "Loft"},
    )
    assert res is RenderResult.WROTE


# ----------------------------------------------------------------------
# escape=True — hostile value stays VALID XML and round-trips.
# ----------------------------------------------------------------------


def test_escape_true_escapes_metacharacters(template, tmp_path, _mock_reload):
    """A value with ``& < > "`` is XML-escaped before substitution, so the
    raw bytes carry the escaped forms, not the literals that would break
    the parse."""
    out = tmp_path / "rendered.service"
    avahi_service.render_service(
        str(template), str(out),
        {"__SPEAKER_NAME__": _HOSTILE, "__ROOM__": "ok"},
        escape=True,
    )
    text = out.read_text()
    # The literal "<b>" the value carries is the canary — escaped to "&lt;b&gt;".
    assert "<b>" not in text
    assert "&lt;b&gt;" in text
    # "&amp;" present, raw " & " (with the surrounding spaces from the
    # hostile value) absent.
    assert "&amp;" in text


def test_escape_true_hostile_value_is_valid_xml_and_round_trips(template, tmp_path, _mock_reload):
    """The load-bearing safety test: a hostile value renders to WELL-FORMED
    XML (both parsers accept it) and the un-escaped value comes back out of
    the parsed TXT element unchanged — i.e. it didn't break out of the
    string and Avahi will accept the whole <service-group>."""
    out = tmp_path / "rendered.service"
    res = avahi_service.render_service(
        str(template), str(out),
        {"__SPEAKER_NAME__": _HOSTILE, "__ROOM__": "den"},
        escape=True,
    )
    assert res is RenderResult.WROTE
    text = out.read_text()

    # Both parsers accept it (would raise on malformed XML).
    txts = _txt_records(text)  # minidom
    root = ET.fromstring(text)  # ElementTree

    # Hostile value round-trips out of the (now-unescaped) TXT value.
    assert txts == ["name=" + _HOSTILE, "room=den"]
    assert root.find("./service/txt-record").text == "name=" + _HOSTILE

    # Structure intact: still one <service-group> root, one <service>,
    # exactly the two txt-records — no injected sibling element.
    assert root.tag == "service-group"
    assert len(root.findall("./service")) == 1
    assert len(root.findall("./service/txt-record")) == 2


def test_escape_false_passes_value_through_verbatim(tmp_path, _mock_reload):
    """``escape=False`` substitutes the raw value with no XML-escaping. Used
    only for values already known mDNS-safe; pinned so the knob is honoured."""
    tmpl = tmp_path / "t.service"
    tmpl.write_text("<r>__VAL__</r>\n")
    out = tmp_path / "out.service"
    res = avahi_service.render_service(
        str(tmpl), str(out), {"__VAL__": "a&b"}, escape=False,
    )
    assert res is RenderResult.WROTE
    # Raw '&' is written verbatim (NOT escaped). (This would be malformed
    # XML for a real Avahi file — which is exactly why the real callers all
    # pass escape=True for any free-form value.)
    assert out.read_text() == "<r>a&b</r>\n"


def test_escape_true_is_byte_identical_for_safe_values(tmp_path, _mock_reload):
    """For values with no XML metacharacters (UUID / constrained room /
    0|1 — peering's case), escape=True and escape=False produce the same
    bytes. Pins the claim that routing peering through escape=True is safe."""
    tmpl = tmp_path / "t.service"
    tmpl.write_text("peer=__PEER_ID__ room=__ROOM__ primary=__PRIMARY__\n")
    subs = {
        "__PEER_ID__": "550e8400-e29b-41d4-a716-446655440000",
        "__ROOM__": "kitchen",
        "__PRIMARY__": "1",
    }
    esc = tmp_path / "esc.service"
    raw = tmp_path / "raw.service"
    avahi_service.render_service(str(tmpl), str(esc), dict(subs), escape=True)
    avahi_service.render_service(str(tmpl), str(raw), dict(subs), escape=False)
    assert esc.read_text() == raw.read_text()


# ----------------------------------------------------------------------
# Stray placeholder — refuse to install a half-rendered file.
# ----------------------------------------------------------------------


def test_stray_placeholder_returns_false_writes_nothing(template, tmp_path, _mock_reload):
    """A leftover ``__FOO__`` (caller missed a substitution / template drift)
    is refused: returns False, writes no file, never reloads. Avoids handing
    Avahi a file it would reject and taking the whole service-group offline."""
    out = tmp_path / "rendered.service"
    # Only substitute one of the two tokens — __ROOM__ is left stray.
    res = avahi_service.render_service(
        str(template), str(out), {"__SPEAKER_NAME__": "Kitchen"},
    )
    assert res is RenderResult.FAILED
    assert not out.exists()
    assert _mock_reload == []  # no reload on the refusal path


def test_stray_placeholder_introduced_by_template_drift(template, tmp_path, _mock_reload):
    """A template that grows a NEW token the caller doesn't know about is
    caught the same way — the guard is on the rendered output, not on the
    caller's key set."""
    template.write_text(_TEMPLATE + "<!-- __NEWTOKEN__ -->\n")
    out = tmp_path / "rendered.service"
    res = avahi_service.render_service(
        str(template), str(out),
        {"__SPEAKER_NAME__": "a", "__ROOM__": "b"},
    )
    assert res is RenderResult.FAILED
    assert not out.exists()


# ----------------------------------------------------------------------
# Idempotence — byte-stable render skips the write + reload (write counter).
# ----------------------------------------------------------------------


def test_idempotent_render_skips_write_and_reload(template, tmp_path, monkeypatch):
    """A second byte-identical render returns ``UNCHANGED`` and does NOT
    rewrite the file or reload. Asserted via a write counter (os.replace is
    the commit point) so the guard is exact, not mtime-precision-dependent.
    Critical for long-lived adverts: a needless rewrite tears down + re-adds
    the service-group, opening a discovery gap. The WROTE-vs-UNCHANGED
    return is what lets callers reload only on a real change."""
    out = tmp_path / "rendered.service"
    subs = {"__SPEAKER_NAME__": "Stable", "__ROOM__": "Den"}

    reloads: list = []
    monkeypatch.setattr(avahi_service, "reload_avahi", lambda: reloads.append(1))

    # Count commits (os.replace) so we can prove the second render didn't write.
    writes = {"n": 0}
    real_replace = avahi_service.os.replace

    def _counting_replace(src, dst):
        writes["n"] += 1
        return real_replace(src, dst)

    monkeypatch.setattr(avahi_service.os, "replace", _counting_replace)

    # First render: WROTE → writes + reloads.
    assert avahi_service.render_service(str(template), str(out), dict(subs)) is RenderResult.WROTE
    first_text = out.read_text()
    assert writes["n"] == 1
    assert reloads == [1]

    # Second identical render: UNCHANGED — no write and no reload.
    assert avahi_service.render_service(str(template), str(out), dict(subs)) is RenderResult.UNCHANGED
    assert out.read_text() == first_text
    assert writes["n"] == 1, "byte-stable re-render must not rewrite the file"
    assert reloads == [1], "byte-stable re-render must not reload avahi"


def test_changed_render_does_rewrite_and_reload(template, tmp_path, monkeypatch):
    """The flip side of idempotence: when the substituted value DOES change,
    the file is rewritten and a reload fires."""
    out = tmp_path / "rendered.service"
    reloads: list = []
    monkeypatch.setattr(avahi_service, "reload_avahi", lambda: reloads.append(1))

    assert avahi_service.render_service(
        str(template), str(out),
        {"__SPEAKER_NAME__": "First", "__ROOM__": "A"},
    ) is RenderResult.WROTE
    assert avahi_service.render_service(
        str(template), str(out),
        {"__SPEAKER_NAME__": "Second", "__ROOM__": "A"},
    ) is RenderResult.WROTE
    assert _txt_records(out.read_text())[0] == "name=Second"
    assert reloads == [1, 1]  # both renders changed the file


# ----------------------------------------------------------------------
# reload knob.
# ----------------------------------------------------------------------


def test_reload_false_does_not_reload(template, tmp_path, monkeypatch):
    """reload=False writes the file but skips the avahi reload (install.sh
    batches its own; the wrappers drive their own conditional reload)."""
    out = tmp_path / "rendered.service"
    reloads: list = []
    monkeypatch.setattr(avahi_service, "reload_avahi", lambda: reloads.append(1))
    res = avahi_service.render_service(
        str(template), str(out),
        {"__SPEAKER_NAME__": "x", "__ROOM__": "y"},
        reload=False,
    )
    # Wrote the file (WROTE), but reload was suppressed by reload=False.
    assert res is RenderResult.WROTE
    assert out.exists()
    assert reloads == []


# ----------------------------------------------------------------------
# Fail-soft — missing template, write failure. Returns False, NEVER raises.
# ----------------------------------------------------------------------


def test_missing_template_returns_false_never_raises(tmp_path, _mock_reload):
    """A missing template (fresh install before install.sh staged it) must
    return False, write nothing, not reload, and never raise."""
    out = tmp_path / "rendered.service"
    res = avahi_service.render_service(
        str(tmp_path / "absent.service"), str(out), {"__SPEAKER_NAME__": "x"},
    )
    assert res is RenderResult.FAILED
    assert not out.exists()
    assert _mock_reload == []


def test_unreadable_template_returns_false(tmp_path, monkeypatch, _mock_reload):
    """A template that exists but raises OSError on read (permissions) is
    fail-soft too: False, never raises."""
    tmpl = tmp_path / "t.service"
    tmpl.write_text(_TEMPLATE)
    out = tmp_path / "rendered.service"

    real_read_text = Path.read_text

    def _boom(self, *a, **k):
        if self == tmpl:
            raise OSError("permission denied")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _boom)
    res = avahi_service.render_service(
        str(tmpl), str(out), {"__SPEAKER_NAME__": "x", "__ROOM__": "y"},
    )
    assert res is RenderResult.FAILED
    assert not out.exists()


def test_write_failure_returns_false_never_raises(template, tmp_path, monkeypatch, _mock_reload):
    """If the atomic write fails (disk full, read-only /etc), render is
    fail-soft: returns False, never raises into the caller."""
    out = tmp_path / "rendered.service"
    real_open = open

    def _boom_open(path, *a, **k):
        if str(path).endswith(".tmp"):
            raise OSError("disk full")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", _boom_open)
    res = avahi_service.render_service(
        str(template), str(out), {"__SPEAKER_NAME__": "x", "__ROOM__": "y"},
    )
    assert res is RenderResult.FAILED
    assert not out.exists()
    assert _mock_reload == []  # no reload when the write failed


# ----------------------------------------------------------------------
# reload_avahi — the shared reload is itself fail-soft.
# ----------------------------------------------------------------------


def test_reload_avahi_swallows_subprocess_failure(monkeypatch):
    """``reload_avahi`` shells out best-effort; an OSError (systemctl
    missing) is swallowed, never propagated."""
    def _boom_run(*a, **k):
        raise OSError("systemctl not found")

    monkeypatch.setattr(avahi_service.subprocess, "run", _boom_run)
    # No raise.
    avahi_service.reload_avahi()


def test_public_surface_is_stable():
    assert callable(avahi_service.render_service)
    assert callable(avahi_service.reload_avahi)
    # The 3-state result enum callers compare against.
    assert {m.name for m in RenderResult} == {"WROTE", "UNCHANGED", "FAILED"}
    assert avahi_service.RenderResult is RenderResult
    # The placeholder detector other code reasons about.
    assert avahi_service._PLACEHOLDER_RE.search("x __FOO__ y")
    assert avahi_service._PLACEHOLDER_RE.search("no tokens here") is None
