"""Hardware-free tests for jasper.web.dial_setup.

Pins the firmware-status surface that the /dial/ wizard uses to tell
users whether Force Flash will actually flash anything — the symptom
this guards against is the silent "auto mode short-circuit" where a
missing /opt/jasper/firmware/dial/jasper-dial.bin caused the wizard
to skip flashing without any user-facing warning."""
from __future__ import annotations

import os
from pathlib import Path

# The /dial/ setup page is now the canonical shell; its scan/provision
# behaviour ships as this ES module rather than an inline <script>. Tests that
# guard the device-rendering JS read the module file directly.
_DIAL_MAIN_JS = (
    Path(__file__).resolve().parent.parent
    / "deploy" / "assets" / "dial" / "js" / "main.js"
)


def test_read_firmware_status_present(tmp_path):
    """File exists → present=True with size/mtime fields populated."""
    from jasper.web.dial_setup import _read_firmware_status

    bin_path = tmp_path / "jasper-dial.bin"
    bin_path.write_bytes(b"\x00" * 2048)
    # Set a known mtime so the formatted output is predictable.
    os.utime(bin_path, (1716470400, 1716470400))  # 2024-05-23 12:00 UTC

    status = _read_firmware_status(str(bin_path), source_root=str(tmp_path / "missing-src"))
    assert status["present"] is True
    assert status["path"] == str(bin_path)
    assert status["size_bytes"] == 2048
    assert "2024-05-23" in status["mtime_iso"]
    assert status["mtime_iso"].endswith("UTC")
    assert status["source_newer"] is False


def test_read_firmware_status_missing(tmp_path):
    """Missing file → present=False, all other fields None. The wizard
    relies on this to render the "no .bin staged" warning."""
    from jasper.web.dial_setup import _read_firmware_status

    bin_path = tmp_path / "does-not-exist.bin"
    status = _read_firmware_status(str(bin_path), source_root=str(tmp_path / "missing-src"))
    assert status["present"] is False
    assert status["size_bytes"] is None
    assert status["mtime_iso"] is None
    assert status["source_newer"] is False


def test_read_firmware_status_marks_source_newer(tmp_path):
    """If source files are newer than the staged bin, /dial/ should warn
    that Force Flash would use stale firmware until rebuilt."""
    from jasper.web.dial_setup import _read_firmware_status

    bin_path = tmp_path / "jasper-dial.bin"
    bin_path.write_bytes(b"\x00" * 2048)
    src_dir = tmp_path / "srcroot" / "src"
    src_dir.mkdir(parents=True)
    source_file = src_dir / "main.cpp"
    source_file.write_text("// newer source\n")
    os.utime(bin_path, (1716470400, 1716470400))
    os.utime(source_file, (1716556800, 1716556800))

    status = _read_firmware_status(str(bin_path), source_root=str(tmp_path / "srcroot"))
    assert status["present"] is True
    assert status["source_newer"] is True
    assert "2024-05-24" in status["source_mtime_iso"]


def test_setup_html_present_renders_ready_banner():
    """When the .bin exists, the wizard shows a green "ready to flash"
    banner with the bin's size + mtime. Force Flash will actually do
    something."""
    from jasper.web.dial_setup import _setup_html

    firmware = {
        "present": True,
        "path": "/opt/jasper/firmware/dial/jasper-dial.bin",
        "size_bytes": 2_056_607,
        "mtime_iso": "2026-05-23 15:45 UTC",
    }
    html_bytes = _setup_html(ssid="HomeWiFi", firmware=firmware)
    html_str = html_bytes.decode("utf-8")

    assert "fw-banner ok" in html_str
    assert "Firmware ready to flash" in html_str
    assert "2008" in html_str  # 2_056_607 // 1024 = 2008 KB
    assert "2026-05-23" in html_str
    # Don't show the install command when firmware is present.
    assert "pip install platformio" not in html_str


def test_setup_html_stale_renders_rebuild_warning():
    """When source is newer than the staged bin, the wizard should not
    imply Force Flash is current."""
    from jasper.web.dial_setup import _setup_html

    firmware = {
        "present": True,
        "path": "/opt/jasper/firmware/dial/jasper-dial.bin",
        "size_bytes": 2_056_607,
        "mtime_iso": "2026-05-23 15:45 UTC",
        "source_newer": True,
        "source_mtime_iso": "2026-05-27 16:30 UTC",
    }
    html_bytes = _setup_html(ssid="HomeWiFi", firmware=firmware)
    html_str = html_bytes.decode("utf-8")

    assert "fw-banner warn" in html_str
    assert "Firmware staged, but source is newer" in html_str
    assert "Force Flash will use" in html_str
    assert "bash /opt/jasper/firmware/dial/build.sh" in html_str
    assert "Firmware ready to flash" not in html_str


def test_setup_html_missing_renders_warning_with_install_command():
    """When the .bin is missing, the wizard surfaces a copy-paste
    install command. This closes the silent-skip-flash failure mode."""
    from jasper.web.dial_setup import _setup_html

    firmware = {
        "present": False,
        "path": "/opt/jasper/firmware/dial/jasper-dial.bin",
        "size_bytes": None,
        "mtime_iso": None,
    }
    html_bytes = _setup_html(ssid="HomeWiFi", firmware=firmware)
    html_str = html_bytes.decode("utf-8")

    assert "fw-banner warn" in html_str
    assert "No firmware staged" in html_str
    assert "skips optional accessory firmware builds" in html_str
    # `sudo` is required — pip can't write to the root-owned venv otherwise.
    assert "sudo /opt/jasper/.venv/bin/pip install platformio" in html_str
    assert "firmware/dial/build.sh" in html_str
    # Don't surface size/mtime when firmware is absent.
    assert "Firmware ready" not in html_str


def test_setup_html_html_escapes_paths():
    """Defensive: the firmware path goes through html.escape so a
    pathological path (e.g. with `<` in it) can't break the page."""
    from jasper.web.dial_setup import _setup_html

    firmware = {
        "present": True,
        "path": "/tmp/<weird>/jasper-dial.bin",
        "size_bytes": 1024,
        "mtime_iso": "2026-05-23 15:45 UTC",
    }
    html_bytes = _setup_html(ssid="x", firmware=firmware)
    html_str = html_bytes.decode("utf-8")
    assert "&lt;weird&gt;" in html_str
    assert "<weird>" not in html_str


def test_setup_html_escapes_usb_device_fields_before_rendering():
    """USB descriptors come from attached hardware, so the browser-side
    template must escape them before assigning innerHTML.

    Post-migration the device-rendering template lives in the
    /assets/dial/js/main.js ES module, not the page's inline <script>. The
    page HTML still carries the CSRF token in its meta tag (the module reads
    it); the escape-before-innerHTML guarantee and the data-action provision
    wiring (vs. an inline onclick carrying an untrusted port) are asserted
    against the module."""
    from jasper.web.dial_setup import _setup_html

    firmware = {
        "present": True,
        "path": "/opt/jasper/firmware/dial/jasper-dial.bin",
        "size_bytes": 1024,
        "mtime_iso": "2026-05-23 15:45 UTC",
    }
    html_str = _setup_html(
        ssid="HomeWiFi", firmware=firmware, csrf_token="csrf-token",
    ).decode("utf-8")

    # CSRF token still rides in the page meta tag for the module to read.
    assert 'meta name="jts-csrf" content="csrf-token"' in html_str

    # The untrusted-field escaping + delegated provision wiring moved into
    # the ES module — assert it there, same guarantee, new location.
    js = _DIAL_MAIN_JS.read_text()
    assert "${escapeHtml(d.port)}" in js
    assert "${escapeHtml(d.description)}" in js
    assert 'data-action="provision"' in js
    assert 'onclick="provision' not in js
