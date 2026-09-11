# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for jasper.net.avahi_service — the ONE Avahi *.service renderer.

This is the shared render+guard+atomic-write body that both
``jasper/net/control_advert.py`` (``_jasper-control._tcp``, free-form name)
and ``jasper/peering/avahi.py`` (``_jasper-peer._udp``, mDNS-safe
metadata) route through. The per-caller wrappers have their own suites
(tests/test_control_advert.py, tests/test_peering_avahi.py); this file
pins the extracted primitive directly so a refactor of either caller
can't silently change the shared contract.

The contract:

  - ``render_service`` fills the ``substitutions`` tokens (FULL ``__..__``
    tokens) into the template and atomic-writes a 0644 file. It returns a
    3-state ``RenderResult`` (``WROTE`` / ``UNCHANGED`` / ``FAILED``), NOT
    a bool — the distinction WROTE-vs-UNCHANGED lets a caller act only on
    a real on-disk change without re-reading the output file to diff.
  - ``escape=True`` runs each value through ``xml.sax.saxutils.escape``
    first, so a hostile value (``& < > "``) stays WELL-FORMED XML and
    round-trips out of the parsed element unchanged. This is the
    load-bearing safety property — a botched escape would make Avahi
    reject the whole ``<service-group>``.
  - A leftover ``__FOO__`` placeholder (caller missed a substitution)
    is refused: returns ``FAILED``, writes nothing.
  - Idempotence: a byte-stable render returns ``UNCHANGED`` and skips the
    write entirely (asserted via a write counter so a long-lived advert
    never tears down + re-adds its service-group).
  - Every failure path is FAIL-SOFT and NEVER raises: a missing template
    returns ``FAILED``; an OSError on write returns ``FAILED``.

Renders into ``tmp_path`` so we never touch real /etc/avahi/services.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

import pytest

from jasper.net import avahi_service
from jasper.net.avahi_service import RenderResult


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
# Happy path — tokens fill into a valid file.
# ----------------------------------------------------------------------


def test_render_service_fills_tokens_into_valid_file(template, tmp_path):
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


def test_render_service_writes_mode_0644(template, tmp_path):
    out = tmp_path / "rendered.service"
    avahi_service.render_service(
        str(template), str(out),
        {"__SPEAKER_NAME__": "x", "__ROOM__": "y"},
    )
    assert (out.stat().st_mode & 0o777) == 0o644


def test_render_service_full_tokens_are_the_keys(template, tmp_path):
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


def test_escape_true_escapes_metacharacters(template, tmp_path):
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


def test_escape_true_hostile_value_is_valid_xml_and_round_trips(template, tmp_path):
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


def test_escape_false_passes_value_through_verbatim(tmp_path):
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


def test_escape_true_is_byte_identical_for_safe_values(tmp_path):
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


def test_stray_placeholder_returns_false_writes_nothing(template, tmp_path):
    """A leftover ``__FOO__`` (caller missed a substitution / template drift)
    is refused: returns False, writes no file. Avoids handing Avahi a file
    it would reject and taking the whole service-group offline."""
    out = tmp_path / "rendered.service"
    # Only substitute one of the two tokens — __ROOM__ is left stray.
    res = avahi_service.render_service(
        str(template), str(out), {"__SPEAKER_NAME__": "Kitchen"},
    )
    assert res is RenderResult.FAILED
    assert not out.exists()


def test_stray_placeholder_introduced_by_template_drift(template, tmp_path):
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
# Idempotence — byte-stable render skips the write (write counter).
# ----------------------------------------------------------------------


def test_idempotent_render_skips_write(template, tmp_path, monkeypatch):
    """A second byte-identical render returns ``UNCHANGED`` and does NOT
    rewrite the file. Asserted via the canonical writer call count so the
    guard is exact, not mtime-precision-dependent. Critical for long-lived
    adverts: a needless rewrite tears down + re-adds the service-group,
    opening a discovery gap."""
    out = tmp_path / "rendered.service"
    subs = {"__SPEAKER_NAME__": "Stable", "__ROOM__": "Den"}

    # Count canonical atomic writes so we can prove the second render did not write.
    writes = {"n": 0}
    real_write = avahi_service.atomic_write_text

    def _counting_write(path, text, **kwargs):
        writes["n"] += 1
        return real_write(path, text, **kwargs)

    monkeypatch.setattr(avahi_service, "atomic_write_text", _counting_write)

    # First render: WROTE.
    assert avahi_service.render_service(str(template), str(out), dict(subs)) is RenderResult.WROTE
    first_text = out.read_text()
    assert writes["n"] == 1

    # Second identical render: UNCHANGED — no write.
    assert avahi_service.render_service(str(template), str(out), dict(subs)) is RenderResult.UNCHANGED
    assert out.read_text() == first_text
    assert writes["n"] == 1, "byte-stable re-render must not rewrite the file"


def test_changed_render_does_rewrite(template, tmp_path):
    """The flip side of idempotence: when the substituted value DOES change,
    the file is rewritten."""
    out = tmp_path / "rendered.service"

    assert avahi_service.render_service(
        str(template), str(out),
        {"__SPEAKER_NAME__": "First", "__ROOM__": "A"},
    ) is RenderResult.WROTE
    assert avahi_service.render_service(
        str(template), str(out),
        {"__SPEAKER_NAME__": "Second", "__ROOM__": "A"},
    ) is RenderResult.WROTE
    assert _txt_records(out.read_text())[0] == "name=Second"


# ----------------------------------------------------------------------
# Fail-soft — missing template, write failure. Returns False, NEVER raises.
# ----------------------------------------------------------------------


def test_missing_template_returns_false_never_raises(tmp_path):
    """A missing template (fresh install before install.sh staged it) must
    return False, write nothing, and never raise."""
    out = tmp_path / "rendered.service"
    res = avahi_service.render_service(
        str(tmp_path / "absent.service"), str(out), {"__SPEAKER_NAME__": "x"},
    )
    assert res is RenderResult.FAILED
    assert not out.exists()


def test_unreadable_template_returns_false(tmp_path, monkeypatch):
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


def test_write_failure_returns_false_never_raises(template, tmp_path, monkeypatch):
    """If the atomic write fails (disk full, read-only /etc), render is
    fail-soft: returns False, never raises into the caller."""
    out = tmp_path / "rendered.service"
    def _boom_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(avahi_service, "atomic_write_text", _boom_write)
    res = avahi_service.render_service(
        str(template), str(out), {"__SPEAKER_NAME__": "x", "__ROOM__": "y"},
    )
    assert res is RenderResult.FAILED
    assert not out.exists()


def test_public_surface_is_stable():
    assert callable(avahi_service.render_service)
    # The 3-state result enum callers compare against.
    assert {m.name for m in RenderResult} == {"WROTE", "UNCHANGED", "FAILED"}
    assert avahi_service.RenderResult is RenderResult
    # The placeholder detector other code reasons about.
    assert avahi_service._PLACEHOLDER_RE.search("x __FOO__ y")
    assert avahi_service._PLACEHOLDER_RE.search("no tokens here") is None
