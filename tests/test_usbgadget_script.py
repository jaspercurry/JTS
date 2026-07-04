# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hermetic subprocess tests for the composite USB gadget scripts.

deploy/usbsink/jasper-usbgadget-{up,down,wanted} are pure-bash policy scripts.
They are driven here against a TEMP ConfigFS tree + fake UDC dir + injected
audio-intent/gate probes — exactly the seams the scripts expose via env
(JASPER_CONFIGFS_ROOT / JASPER_UDC_CLASS_DIR / JASPER_USBGADGET_AUDIO_INTENT_CMD
/ JASPER_USBGADGET_AUDIO_GATE_CMD / JASPER_CPUINFO_FILE). No real ConfigFS, no
libcomposite, no systemd — mirrors tests/test_wifi_guardian_script.py.

Each test asserts on:
  1. the structured `event=usb_gadget.<outcome>` line in stderr;
  2. the ConfigFS tree the script wrote (which functions were linked);
  3. the exit code (0 = composed/bound, 1 = ExecCondition "skip").

The truth table (JASPER_USB_NETWORK x audio-intent) is exercised row by row.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UP = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-up"
DOWN = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-down"
WANTED = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-wanted"

# `true` always succeeds (exit 0); `false` always fails (exit 1). The scripts
# run the intent/gate commands and branch on their exit status, so these are
# the cleanest injectable probes.
TRUE = "/usr/bin/true"
FALSE = "/usr/bin/false"


def _configfs(tmp_path: Path) -> Path:
    """A temp ConfigFS root with the usb_gadget dir the scripts cd into."""
    root = tmp_path / "configfs"
    (root / "usb_gadget").mkdir(parents=True, exist_ok=True)
    return root


def _udc_dir(tmp_path: Path, *, present: bool) -> Path:
    """A fake /sys/class/udc dir. present=True seeds one controller entry.
    Idempotent so a second _run over the same tmp_path (MAC-determinism /
    idempotency / down tests) does not collide."""
    udc = tmp_path / "udc"
    udc.mkdir(exist_ok=True)
    if present:
        (udc / "3f980000.usb").mkdir(exist_ok=True)
    return udc


def _cpuinfo(tmp_path: Path, serial: str = "10000000abcdef01") -> Path:
    p = tmp_path / "cpuinfo"
    p.write_text(f"processor\t: 0\nSerial\t\t: {serial}\n")
    return p


def _run(
    script: Path,
    tmp_path: Path,
    *,
    network: str | None = "enabled",
    audio_intent: str = FALSE,
    audio_gate: str = TRUE,
    udc_present: bool = True,
    configfs: Path | None = None,
    cpuinfo_serial: str = "10000000abcdef01",
) -> tuple[subprocess.CompletedProcess[str], Path]:
    configfs = configfs if configfs is not None else _configfs(tmp_path)
    udc = _udc_dir(tmp_path, present=udc_present)
    env = os.environ.copy()
    env.update({
        "JASPER_CONFIGFS_ROOT": str(configfs),
        "JASPER_UDC_CLASS_DIR": str(udc),
        "JASPER_USBGADGET_AUDIO_INTENT_CMD": audio_intent,
        "JASPER_USBGADGET_AUDIO_GATE_CMD": audio_gate,
        "JASPER_CPUINFO_FILE": str(_cpuinfo(tmp_path, cpuinfo_serial)),
        # Keep the speaker-name source deterministic + absent (defaults to JTS).
        "JASPER_SPEAKER_NAME_FILE": str(tmp_path / "no-such-name.env"),
    })
    if network is None:
        env.pop("JASPER_USB_NETWORK", None)
    else:
        env["JASPER_USB_NETWORK"] = network
    proc = subprocess.run(
        ["bash", str(script)],
        check=False, cwd=ROOT, env=env, text=True, capture_output=True,
        timeout=60,
    )
    return proc, configfs


def _gadget_dir(configfs: Path) -> Path:
    return configfs / "usb_gadget" / "jts-usb-audio"


def _linked(configfs: Path, fn: str) -> bool:
    """True iff `fn` is symlinked into configs/c.1 (i.e. the function is
    composed onto the gadget). The script uses the kernel-doc idiom
    ``ln -s functions/<fn> configs/c.1/`` whose relative target only resolves
    inside real ConfigFS, so we check the LINK's presence (lexists), not that
    its target resolves."""
    return os.path.lexists(_gadget_dir(configfs) / "configs" / "c.1" / fn)


# ---------- truth table via jasper-usbgadget-up -----------------------------


def test_up_network_and_audio_composes_both(tmp_path):
    """network=enabled + audio intent yes + gate allowed -> ncm + uac2."""
    proc, cfg = _run(UP, tmp_path, network="enabled", audio_intent=TRUE, audio_gate=TRUE)
    assert proc.returncode == 0, proc.stderr
    assert "event=usb_gadget.compose network=1 audio=1" in proc.stderr
    assert "event=usb_gadget.up" in proc.stderr
    assert _linked(cfg, "ncm.usb0")
    assert _linked(cfg, "uac2.usb0")


def test_up_network_only_when_audio_parked(tmp_path):
    """network on, audio intent yes but gate DENIES (parked follower) -> ncm only."""
    proc, cfg = _run(UP, tmp_path, network="enabled", audio_intent=TRUE, audio_gate=FALSE)
    assert proc.returncode == 0, proc.stderr
    assert "network=1 audio=0" in proc.stderr
    assert "audio_reason=parked_follower" in proc.stderr
    assert _linked(cfg, "ncm.usb0")
    assert not _linked(cfg, "uac2.usb0")


def test_up_network_only_when_audio_intent_disabled(tmp_path):
    """network on, audio intent NO -> ncm only (the common default box)."""
    proc, cfg = _run(UP, tmp_path, network="enabled", audio_intent=FALSE)
    assert proc.returncode == 0, proc.stderr
    assert "network=1 audio=0" in proc.stderr
    assert "audio_reason=intent_disabled" in proc.stderr
    assert _linked(cfg, "ncm.usb0")
    assert not _linked(cfg, "uac2.usb0")


def test_up_audio_only_when_network_killswitched(tmp_path):
    """JASPER_USB_NETWORK=disabled + audio yes -> uac2 only (legacy shape)."""
    proc, cfg = _run(UP, tmp_path, network="disabled", audio_intent=TRUE, audio_gate=TRUE)
    assert proc.returncode == 0, proc.stderr
    assert "network=0 audio=1" in proc.stderr
    assert not _linked(cfg, "ncm.usb0")
    assert _linked(cfg, "uac2.usb0")


def test_up_no_function_wanted_skips_and_tears_down(tmp_path):
    """network disabled AND audio off -> nothing to compose; the script emits
    skip and leaves no bound gadget (the ExecCondition normally skips the unit
    before we even reach here, but a hand-start must not publish an empty
    gadget)."""
    proc, cfg = _run(UP, tmp_path, network="disabled", audio_intent=FALSE)
    assert proc.returncode == 0, proc.stderr
    assert "event=usb_gadget.compose network=0 audio=0" in proc.stderr
    assert "event=usb_gadget.skip reason=no_function_wanted" in proc.stderr
    assert not _gadget_dir(cfg).exists()


# ---------- kill-switch literal parsing -------------------------------------


def test_up_killswitch_case_insensitive(tmp_path):
    """The kill switch matches `disabled` case-insensitively (mirrors
    JASPER_SHAIRPORT_SUPERVISOR)."""
    proc, cfg = _run(UP, tmp_path, network="DISABLED", audio_intent=TRUE, audio_gate=TRUE)
    assert proc.returncode == 0, proc.stderr
    assert "network=0 audio=1" in proc.stderr
    assert not _linked(cfg, "ncm.usb0")


def test_up_unset_network_defaults_enabled(tmp_path):
    """An UNSET JASPER_USB_NETWORK defaults to enabled (always-on network)."""
    proc, cfg = _run(UP, tmp_path, network=None, audio_intent=FALSE)
    assert proc.returncode == 0, proc.stderr
    assert "network=1 audio=0" in proc.stderr
    assert _linked(cfg, "ncm.usb0")


def test_up_unknown_network_value_warns_and_stays_enabled(tmp_path):
    """Any value other than the exact literal `disabled` logs a warning and
    STAYS enabled (mirrors JASPER_SHAIRPORT_SUPERVISOR)."""
    proc, cfg = _run(UP, tmp_path, network="off", audio_intent=FALSE)
    assert proc.returncode == 0, proc.stderr
    assert "not 'enabled'/'disabled'; staying enabled" in proc.stderr
    assert "network=1 audio=0" in proc.stderr
    assert _linked(cfg, "ncm.usb0")


# ---------- deterministic serial-derived MACs -------------------------------


def _read_mac(configfs: Path, which: str) -> str:
    return (_gadget_dir(configfs) / "functions" / "ncm.usb0" / which).read_text().strip()


def test_up_ncm_macs_are_locally_administered_and_distinct(tmp_path):
    proc, cfg = _run(UP, tmp_path, network="enabled", audio_intent=FALSE)
    assert proc.returncode == 0, proc.stderr
    host = _read_mac(cfg, "host_addr")
    dev = _read_mac(cfg, "dev_addr")
    # 02: prefix = locally-administered unicast (LAA bit set, multicast clear).
    assert host.startswith("02:"), host
    assert dev.startswith("02:"), dev
    # host and dev must differ so the two ends of the link never collide.
    assert host != dev
    # Well-formed 6-octet MACs.
    for mac in (host, dev):
        octets = mac.split(":")
        assert len(octets) == 6
        assert all(len(o) == 2 for o in octets)


def test_up_ncm_macs_deterministic_for_same_serial(tmp_path):
    """Same CPU serial -> same MACs every boot (load-bearing: otherwise the
    host sees a brand-new adapter on every boot)."""
    p1, c1 = _run(UP, tmp_path / "a", audio_intent=FALSE, cpuinfo_serial="deadbeef00112233")
    p2, c2 = _run(UP, tmp_path / "b", audio_intent=FALSE, cpuinfo_serial="deadbeef00112233")
    assert p1.returncode == 0 and p2.returncode == 0
    assert _read_mac(c1, "host_addr") == _read_mac(c2, "host_addr")
    assert _read_mac(c1, "dev_addr") == _read_mac(c2, "dev_addr")


def test_up_ncm_macs_differ_across_speakers(tmp_path):
    """Two different serials (two speakers) derive different MACs."""
    p1, c1 = _run(UP, tmp_path / "a", audio_intent=FALSE, cpuinfo_serial="1111111111111111")
    p2, c2 = _run(UP, tmp_path / "b", audio_intent=FALSE, cpuinfo_serial="2222222222222222")
    assert p1.returncode == 0 and p2.returncode == 0
    assert _read_mac(c1, "host_addr") != _read_mac(c2, "host_addr")


# ---------- bcdDevice + product string --------------------------------------


def test_up_bumps_bcddevice_to_0200(tmp_path):
    """bcdDevice bumped 0x0100 -> 0x0200 so hosts re-read the composite
    function set."""
    proc, cfg = _run(UP, tmp_path, audio_intent=FALSE)
    assert proc.returncode == 0, proc.stderr
    assert (_gadget_dir(cfg) / "bcdDevice").read_text().strip() == "0x0200"


def test_up_product_string_is_speaker_name_only(tmp_path):
    """Product string is the speaker name WITHOUT 'USB Audio' (the gadget is no
    longer audio-only; the NIC label shouldn't say USB Audio)."""
    proc, cfg = _run(UP, tmp_path, audio_intent=FALSE)
    assert proc.returncode == 0, proc.stderr
    product = (_gadget_dir(cfg) / "strings" / "0x409" / "product").read_text().strip()
    assert product == "JTS"  # default speaker name, no " USB Audio" suffix
    assert "USB Audio" not in product


# ---------- uac2 attribute block is byte-identical (protection list) --------


def test_up_uac2_attribute_block_byte_identical(tmp_path):
    """The uac2 function's attribute writes must be byte-identical to the
    pre-composite gadget (low-latency contract). Assert the exact values."""
    proc, cfg = _run(UP, tmp_path, network="enabled", audio_intent=TRUE, audio_gate=TRUE)
    assert proc.returncode == 0, proc.stderr
    fn = _gadget_dir(cfg) / "functions" / "uac2.usb0"
    assert (fn / "c_srate").read_text().strip() == "48000"
    assert (fn / "c_ssize").read_text().strip() == "4"
    assert (fn / "c_chmask").read_text().strip() == "3"
    assert (fn / "c_volume_present").read_text().strip() == "1"
    assert (fn / "c_mute_present").read_text().strip() == "1"
    assert (fn / "p_chmask").read_text().strip() == "0"
    # write_if_present attrs land because the temp dir has the files created by
    # the mkdir -p (they don't exist until the kernel makes them). On a temp
    # tree they are ABSENT, so write_if_present is a no-op — assert the script
    # did not error and the always-written names are correct.
    assert (fn / "function_name").read_text().strip() == "JTS Capture Endpoint"


# ---------- idempotency + UDC binding ---------------------------------------


def test_up_binds_to_udc(tmp_path):
    proc, cfg = _run(UP, tmp_path, audio_intent=FALSE)
    assert proc.returncode == 0, proc.stderr
    assert (_gadget_dir(cfg) / "UDC").read_text().strip() == "3f980000.usb"


def test_up_no_udc_fails_loud_via_up_script(tmp_path):
    """gadget-up itself (reached only past the ExecCondition) treats a missing
    UDC as a hard failure — the ExecCondition is the clean-skip layer."""
    proc, _cfg = _run(UP, tmp_path, audio_intent=FALSE, udc_present=False)
    assert proc.returncode == 1
    assert "event=usb_gadget.skip reason=no_udc" in proc.stderr


def test_up_idempotent_when_already_bound(tmp_path):
    """A second run over an already-bound descriptor is a no-op exit 0."""
    proc1, cfg = _run(UP, tmp_path, audio_intent=FALSE)
    assert proc1.returncode == 0
    # Re-run against the SAME configfs (already bound).
    proc2, _ = _run(UP, tmp_path, audio_intent=FALSE, configfs=cfg)
    assert proc2.returncode == 0, proc2.stderr
    assert "already_bound" in proc2.stderr


# ---------- jasper-usbgadget-wanted (ExecCondition) -------------------------


def test_wanted_proceeds_when_network_on(tmp_path):
    proc, _ = _run(WANTED, tmp_path, network="enabled", audio_intent=FALSE)
    assert proc.returncode == 0, proc.stderr
    assert "event=usb_gadget.wanted network=1 audio=0" in proc.stderr


def test_wanted_proceeds_when_audio_on(tmp_path):
    proc, _ = _run(WANTED, tmp_path, network="disabled", audio_intent=TRUE, audio_gate=TRUE)
    assert proc.returncode == 0, proc.stderr
    assert "network=0 audio=1" in proc.stderr


def test_wanted_skips_when_no_function_wanted(tmp_path):
    """network disabled AND audio off -> skip (exit 1). This is systemd's
    'condition not met', which restores the zero-RAM contract."""
    proc, _ = _run(WANTED, tmp_path, network="disabled", audio_intent=FALSE)
    assert proc.returncode == 1
    assert "event=usb_gadget.skip reason=no_function_wanted" in proc.stderr


def test_wanted_skips_cleanly_when_no_udc(tmp_path):
    """Fresh install pre-reboot: no UDC yet -> skip (exit 1), NOT a failure.
    The doctor's dtoverlay check tells the user to reboot."""
    proc, _ = _run(WANTED, tmp_path, network="enabled", audio_intent=FALSE, udc_present=False)
    assert proc.returncode == 1
    assert "event=usb_gadget.skip reason=no_udc" in proc.stderr


# ---------- jasper-usbgadget-down (teardown) --------------------------------


def test_down_tears_down_both_functions(tmp_path):
    """Compose both functions, then down unbinds + removes BOTH function
    symlinks generically (uac2 + ncm). The final ``rmdir`` of the gadget dir
    only fully succeeds on real ConfigFS (its attribute files are magic and
    vanish with the function dir); on a plain-file temp tree the symlink
    removal + unbind is the testable teardown, so we assert those."""
    up, cfg = _run(UP, tmp_path, network="enabled", audio_intent=TRUE, audio_gate=TRUE)
    assert up.returncode == 0
    assert _linked(cfg, "ncm.usb0") and _linked(cfg, "uac2.usb0")
    proc, _ = _run(DOWN, tmp_path, configfs=cfg)
    assert proc.returncode == 0, proc.stderr
    assert "event=usb_gadget.down" in proc.stderr
    # Both function symlinks removed (generic teardown of the composite set).
    assert not _linked(cfg, "ncm.usb0")
    assert not _linked(cfg, "uac2.usb0")
    # UDC unbound (empty string written).
    assert (_gadget_dir(cfg) / "UDC").read_text().strip() == ""


def test_down_noop_when_absent(tmp_path):
    """down against a non-existent gadget is a clean no-op exit 0."""
    cfg = _configfs(tmp_path)
    proc, _ = _run(DOWN, tmp_path, configfs=cfg)
    assert proc.returncode == 0, proc.stderr
    assert "nothing to do" in proc.stdout


# ---------- structured event lines land on stderr ---------------------------


def test_events_land_on_stderr_not_stdout(tmp_path):
    """`event=usb_gadget.*` lines go to stderr (journal grep) so `bash -x`
    stdout consumers don't trip on them — mirrors the wifi-guardian idiom."""
    proc, _ = _run(UP, tmp_path, audio_intent=FALSE)
    assert "event=usb_gadget." in proc.stderr
    assert "event=usb_gadget." not in proc.stdout
