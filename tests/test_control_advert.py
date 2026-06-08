"""Unit tests for jasper.control_advert.

This module renders the always-on ``_jasper-control._tcp`` mDNS advert
from ``deploy/avahi/jasper-control.service.template``, substituting the
speaker's friendly display name into a ``name=`` TXT record so the
``/rooms/`` directory shows the same name peers see on Spotify / AirPlay
/ Bluetooth / USB. The contract these tests pin:

  - The advert is ALWAYS on and the rotary dial discovers the speaker
    through it (``MDNS.queryService("jasper-control", "tcp")`` + read the
    address). The change vs. the historical static
    ``deploy/avahi/jasper-control.service`` is therefore **purely
    additive**: the ``<service>``/``<type>``/``<port>`` block must stay
    byte-for-byte identical, with only the ``<txt-record>`` added. A
    drift there could move/rename the service and break the dial.

  - The ONLY structural hazard a free-form name introduces is malformed
    XML, which would make Avahi reject the whole ``<service-group>`` and
    take ``_jasper-control._tcp`` offline. So the name is XML-escaped
    before substitution, and these tests assert the rendered file parses
    as valid XML — including with a hostile name (``A & <b> "x" '``) —
    and that the hostile name round-trips through the TXT value intact.

  - Every failure is fail-soft: a missing/unreadable template, a stray
    placeholder, or a write failure logs and returns ``False`` — it must
    NEVER raise into the caller (``/speaker`` save, ``deploy/install.sh``).
    An empty/unset name falls back to the hostname so the TXT is never
    empty and the service always advertises something addressable.

Renders into ``tmp_path`` so we never touch real ``/etc/avahi/services``;
the avahi-daemon reload subprocess is mocked so tests never shell out.
The real shipped template + static service file are read from the repo so
the byte-equivalence and "renders valid XML" checks track what actually
deploys (not a hand-copied approximation that could silently diverge).
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

import pytest

from jasper import avahi_service
from jasper import control_advert as ca

_REPO = Path(__file__).resolve().parent.parent
_TEMPLATE_SRC = _REPO / "deploy" / "avahi" / "jasper-control.service.template"
_STATIC_SRC = _REPO / "deploy" / "avahi" / "jasper-control.service"

# Captured at import time, before the autouse reload-mock fixture replaces it.
# One swallow-path test restores this real implementation to exercise
# avahi_service.reload_avahi's own OSError/SubprocessError handling.
_REAL_RELOAD_AVAHI = avahi_service.reload_avahi

# A name exercising every XML metacharacter at once: ampersand, angle
# brackets, double quote, single quote. If escaping is wrong, minidom /
# ElementTree parsing below raises and the whole service-group would be
# rejected by Avahi at runtime (dial goes offline).
HOSTILE_NAME = 'A & <b> "x" \''


@pytest.fixture(autouse=True)
def _mock_avahi_reload(monkeypatch):
    """Mock the avahi-daemon reload for every test so none shells out.
    Returns the recorder so a test can assert it was (or wasn't) called.

    ``render_control_advert`` no longer drives its own reload — it delegates
    to ``jasper.avahi_service.render_service``, which OWNS the reload and
    fires it (only on ``RenderResult.WROTE``) through the shared
    ``jasper.avahi_service.reload_avahi``. So we patch *that* boundary, not
    ``subprocess.run`` (control_advert doesn't import subprocess anymore).
    Mirrors tests/test_peering_avahi.py's render-path reload patch.
    """

    class _Recorder:
        def __init__(self):
            self.calls: list = []

        def __call__(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    rec = _Recorder()
    monkeypatch.setattr(avahi_service, "reload_avahi", rec)
    return rec


@pytest.fixture
def template(tmp_path) -> Path:
    """The real shipped advert template copied into tmp_path."""
    p = tmp_path / "jasper-control.service.template"
    p.write_text(_TEMPLATE_SRC.read_text())
    return p


def _txt_records(xml_text: str) -> list[str]:
    """All ``<txt-record>`` text values, parsed with minidom (which does
    NOT fetch the external ``avahi-service.dtd``, so the DOCTYPE is fine).
    Parsing at all is the load-bearing assertion: it proves the rendered
    file is well-formed XML."""
    doc = minidom.parseString(xml_text)
    return [
        n.firstChild.data if n.firstChild else ""
        for n in doc.getElementsByTagName("txt-record")
    ]


def _service_block(xml_text: str) -> str:
    m = re.search(r"<service>.*?</service>", xml_text, re.DOTALL)
    assert m, "no <service> block found"
    return m.group(0)


# ----------------------------------------------------------------------
# Module constants — the install + runtime paths other code depends on.
# ----------------------------------------------------------------------


def test_module_path_constants_are_pinned():
    # install.sh drops the template here; render writes the live advert here.
    assert ca.CONTROL_AVAHI_TEMPLATE == "/etc/jasper/avahi-templates/jasper-control.service"
    assert ca.CONTROL_AVAHI_SERVICE == "/etc/avahi/services/jasper-control.service"


# ----------------------------------------------------------------------
# Happy path — a name fills the TXT and the file is valid XML.
# ----------------------------------------------------------------------


def test_render_fills_name_into_valid_xml(template, tmp_path, _mock_avahi_reload):
    out = tmp_path / "rendered.service"
    ok = ca.render_control_advert("Kitchen", template=str(template), out=str(out), reload=True)
    assert ok is True
    text = out.read_text()

    # Parses as well-formed XML (minidom + ElementTree, two independent
    # parsers) — this is what proves Avahi will accept the service-group.
    assert _txt_records(text) == ["name=Kitchen"]
    et = ET.fromstring(text)
    assert et.find("./service/txt-record").text == "name=Kitchen"

    # The placeholder is fully consumed.
    assert "__SPEAKER_NAME__" not in text
    # Reload was attempted (reload=True).
    assert len(_mock_avahi_reload.calls) == 1


# ----------------------------------------------------------------------
# Hostile name — still valid XML, round-trips through the TXT value.
# ----------------------------------------------------------------------


def test_hostile_name_yields_valid_xml_and_round_trips(template, tmp_path):
    """A name with `& < > " '` must render to WELL-FORMED XML (escaping
    works) and the un-escaped name must come back out of the parsed TXT
    value unchanged. This is the load-bearing safety test: a botched
    escape would drop the whole <service-group> and take the dial offline.
    """
    out = tmp_path / "rendered.service"
    ok = ca.render_control_advert(HOSTILE_NAME, template=str(template), out=str(out), reload=False)
    assert ok is True
    text = out.read_text()

    # The raw bytes must NOT contain the unescaped metacharacters from the
    # name (they'd break the parse). The literal "<b>" the name carries is
    # the canary — it must have been escaped to "&lt;b&gt;".
    assert "<b>" not in text
    assert "&lt;b&gt;" in text

    # Both parsers accept it (would raise on malformed XML).
    txts = _txt_records(text)  # minidom
    et_txt = ET.fromstring(text).find("./service/txt-record").text  # ElementTree

    # And the hostile name round-trips out of the (now-unescaped) TXT value.
    assert txts == ["name=" + HOSTILE_NAME]
    assert et_txt == "name=" + HOSTILE_NAME


def test_hostile_name_does_not_break_out_of_service_group(template, tmp_path):
    """Beyond well-formedness: the escaped name stays *inside* the single
    txt-record element. There is exactly one <service>, one <txt-record>,
    and the document root is still <service-group> — the hostile string
    didn't inject a sibling element."""
    out = tmp_path / "rendered.service"
    ca.render_control_advert(HOSTILE_NAME, template=str(template), out=str(out), reload=False)
    root = ET.fromstring(out.read_text())
    assert root.tag == "service-group"
    assert len(root.findall("./service")) == 1
    assert len(root.findall("./service/txt-record")) == 1


# ----------------------------------------------------------------------
# Empty / unset name — hostname default keeps the TXT non-empty.
# ----------------------------------------------------------------------


def test_empty_name_falls_back_to_hostname(template, tmp_path, monkeypatch):
    """An empty name must not yield an empty `name=` TXT — fall back to the
    hostname so the service always advertises something addressable."""
    monkeypatch.setenv("JASPER_HOSTNAME", "myhost.local")
    out = tmp_path / "rendered.service"
    ok = ca.render_control_advert("", template=str(template), out=str(out), reload=False)
    assert ok is True
    txts = _txt_records(out.read_text())
    assert txts == ["name=myhost.local"]
    # The value after `name=` is non-empty.
    assert txts[0].split("name=", 1)[1] != ""


def test_blank_whitespace_name_falls_back_to_hostname(template, tmp_path, monkeypatch):
    """A name that is only whitespace is treated as empty (stripped) and
    falls back to the hostname — never a blank TXT."""
    monkeypatch.setenv("JASPER_HOSTNAME", "ws.local")
    out = tmp_path / "rendered.service"
    ok = ca.render_control_advert("   ", template=str(template), out=str(out), reload=False)
    assert ok is True
    assert _txt_records(out.read_text()) == ["name=ws.local"]


def test_unset_hostname_uses_default_jts_local(template, tmp_path, monkeypatch):
    """With both the name AND JASPER_HOSTNAME unset, the TXT still resolves
    to a non-empty default (jts.local) rather than an empty value."""
    monkeypatch.delenv("JASPER_HOSTNAME", raising=False)
    out = tmp_path / "rendered.service"
    ok = ca.render_control_advert("", template=str(template), out=str(out), reload=False)
    assert ok is True
    assert _txt_records(out.read_text()) == ["name=jts.local"]


# ----------------------------------------------------------------------
# name=None — reads the canonical speaker name (speaker_name module).
# ----------------------------------------------------------------------


def test_none_name_reads_speaker_name_module(template, tmp_path, monkeypatch):
    """`render_control_advert()` with no name reads the canonical speaker
    display name through the single identity reader
    (jasper.identity.read_identity().name, which IS
    jasper.speaker_name.runtime_name today) so a name change flows through
    without the caller re-passing it, and control_advert is a real
    identity consumer."""
    monkeypatch.setattr("jasper.speaker_name.runtime_name", lambda: "Living Room")
    out = tmp_path / "rendered.service"
    ok = ca.render_control_advert(None, template=str(template), out=str(out), reload=False)
    assert ok is True
    assert _txt_records(out.read_text()) == ["name=Living Room"]


def test_none_name_reader_failure_still_advertises_identity_default(template, tmp_path, monkeypatch):
    """If the underlying name read raises, the render must NOT propagate it
    and must still advertise a non-empty name.

    The name now resolves through jasper.identity.read_identity(), which is
    TOTAL: it swallows a raising runtime_name internally and returns the
    identity default ("JTS"). So a broken read advertises "name=JTS" — a
    sensible non-empty value — rather than reaching control_advert's
    empty->hostname fallback (which only fires if read_identity itself
    returned blank). The invariant this pins is unchanged: a broken name
    read never raises and never yields an empty TXT."""
    def _boom():
        raise RuntimeError("state file unreadable")

    monkeypatch.setattr("jasper.speaker_name.runtime_name", _boom)
    monkeypatch.setenv("JASPER_HOSTNAME", "fallback.local")
    out = tmp_path / "rendered.service"
    ok = ca.render_control_advert(None, template=str(template), out=str(out), reload=False)
    assert ok is True
    txts = _txt_records(out.read_text())
    assert txts == ["name=JTS"]
    # The load-bearing invariant: never an empty TXT, never a raise.
    assert txts[0].split("name=", 1)[1] != ""


# ----------------------------------------------------------------------
# Byte-equivalence — only the TXT record was added vs the static service.
# ----------------------------------------------------------------------


def test_rendered_service_block_byte_equivalent_to_static(template, tmp_path):
    """The rendered <service>/<type>/<port> must be byte-for-byte identical
    to the historical static deploy/avahi/jasper-control.service — with ONLY
    the <txt-record> line added. The dial keys off type + address; any other
    drift in this block could rename/move the service and break discovery.
    """
    out = tmp_path / "rendered.service"
    ca.render_control_advert("Whatever", template=str(template), out=str(out), reload=False)

    rendered_block = _service_block(out.read_text())
    static_block = _service_block(_STATIC_SRC.read_text())

    # Strip exactly the one added txt-record line (and the leading newline +
    # indentation it owns). What remains must equal the static block byte-
    # for-byte: same <type>, same <port>, same whitespace.
    stripped = re.sub(r"\n[ \t]*<txt-record>name=.*?</txt-record>", "", rendered_block)
    assert stripped == static_block

    # And, positively, the rendered block carries the type + port verbatim
    # and exactly one txt-record more than the static block.
    assert "<type>_jasper-control._tcp</type>" in rendered_block
    assert "<port>8780</port>" in rendered_block
    assert rendered_block.count("<txt-record>") == static_block.count("<txt-record>") + 1


def test_template_service_block_only_adds_txt_record():
    """Guard the shipped *files* directly (independent of the renderer): the
    template's <service> block is the static one plus exactly the
    name=__SPEAKER_NAME__ txt-record. Catches an edit to either file that
    diverges the always-on advert from its static byte-for-byte baseline."""
    tmpl_block = _service_block(_TEMPLATE_SRC.read_text())
    static_block = _service_block(_STATIC_SRC.read_text())
    stripped = re.sub(
        r"\n[ \t]*<txt-record>name=__SPEAKER_NAME__</txt-record>", "", tmpl_block
    )
    assert stripped == static_block


# ----------------------------------------------------------------------
# Fail-soft — missing / unreadable template, stray token, write failure.
# Every path returns False and NEVER raises.
# ----------------------------------------------------------------------


def test_missing_template_returns_false_never_raises(tmp_path, _mock_avahi_reload):
    """A missing template (fresh install before install.sh staged it) must
    return False, not write an output file, not reload, and never raise."""
    out = tmp_path / "rendered.service"
    ok = ca.render_control_advert(
        "x", template=str(tmp_path / "absent.service"), out=str(out), reload=True,
    )
    assert ok is False
    assert not out.exists()
    assert _mock_avahi_reload.calls == []  # no reload on the failure path


def test_unreadable_template_returns_false(tmp_path, monkeypatch):
    """A template that exists but raises on read (e.g. permissions) is
    fail-soft too: returns False, never raises."""
    template = tmp_path / "t.service"
    template.write_text(_TEMPLATE_SRC.read_text())
    out = tmp_path / "rendered.service"

    real_read_text = Path.read_text

    def _boom(self, *a, **k):
        if self == template:
            raise OSError("permission denied")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _boom)
    ok = ca.render_control_advert("x", template=str(template), out=str(out), reload=False)
    assert ok is False
    assert not out.exists()


def test_stray_placeholder_refuses_to_install(template, tmp_path, _mock_avahi_reload):
    """Template drift — a new __FOO__ token with no substitution must be
    refused (returns False, writes nothing, doesn't reload) rather than
    installing a half-rendered file that Avahi would reject wholesale."""
    bad = _TEMPLATE_SRC.read_text().replace(
        "</service-group>", "  <x>__UNKNOWN_TOKEN__</x>\n</service-group>",
    )
    template.write_text(bad)
    out = tmp_path / "rendered.service"
    ok = ca.render_control_advert("x", template=str(template), out=str(out), reload=True)
    assert ok is False
    assert not out.exists()
    assert _mock_avahi_reload.calls == []


def test_write_failure_returns_false_never_raises(template, tmp_path, monkeypatch):
    """If the atomic write fails (disk full, read-only /etc), the render is
    fail-soft: returns False, never raises into the caller."""
    out = tmp_path / "rendered.service"
    real_open = open

    def _boom_open(path, *a, **k):
        if str(path).endswith(".tmp"):
            raise OSError("disk full")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", _boom_open)
    ok = ca.render_control_advert("x", template=str(template), out=str(out), reload=True)
    assert ok is False
    assert not out.exists()


# ----------------------------------------------------------------------
# Reload control + idempotence.
# ----------------------------------------------------------------------


def test_reload_false_does_not_shell_out(template, tmp_path, _mock_avahi_reload):
    """reload=False writes the advert but skips the avahi-daemon reload
    (install.sh batches its own reload; the wizard wants the live reload)."""
    out = tmp_path / "rendered.service"
    ok = ca.render_control_advert("x", template=str(template), out=str(out), reload=False)
    assert ok is True
    assert out.exists()
    assert _mock_avahi_reload.calls == []


def test_unchanged_render_skips_write_and_reload(template, tmp_path, _mock_avahi_reload):
    """A byte-identical re-render returns True but does NOT reload — critical
    for the dial, since a needless write+reload tears down and re-adds the
    service-group, opening a discovery gap."""
    out = tmp_path / "rendered.service"
    ca.render_control_advert("Stable", template=str(template), out=str(out), reload=True)
    assert len(_mock_avahi_reload.calls) == 1
    first = out.read_text()

    # Second identical render: no write change, no second reload.
    ok = ca.render_control_advert("Stable", template=str(template), out=str(out), reload=True)
    assert ok is True
    assert out.read_text() == first
    assert len(_mock_avahi_reload.calls) == 1  # still just the first


def test_reload_subprocess_failure_is_swallowed(template, tmp_path, monkeypatch):
    """Even the reload itself is fail-soft: if `systemctl reload` raises
    (OSError / SubprocessError), the render still returns True (the file is
    on disk; Avahi's inotify will pick it up) and never propagates.

    The reload is owned by ``avahi_service.render_service`` now, so we restore
    the REAL ``avahi_service.reload_avahi`` (the autouse fixture stubs it) and
    make the underlying ``avahi_service.subprocess.run`` raise — exercising
    the actual swallow path in ``avahi_service.reload_avahi``."""
    monkeypatch.setattr(avahi_service, "reload_avahi", _REAL_RELOAD_AVAHI)

    def _boom_run(*a, **k):
        raise OSError("systemctl not found")

    monkeypatch.setattr(avahi_service.subprocess, "run", _boom_run)
    out = tmp_path / "rendered.service"
    ok = ca.render_control_advert("x", template=str(template), out=str(out), reload=True)
    assert ok is True
    assert out.exists()


# ----------------------------------------------------------------------
# Public surface.
# ----------------------------------------------------------------------


def test_public_surface_is_stable():
    assert callable(ca.render_control_advert)
    assert isinstance(ca.CONTROL_AVAHI_TEMPLATE, str)
    assert isinstance(ca.CONTROL_AVAHI_SERVICE, str)
