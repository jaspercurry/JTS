# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hermetic subprocess tests for the composite USB gadget scripts.

deploy/usbsink/jasper-usbgadget-{up,down,wanted} are pure-bash policy scripts.
They are driven here against a TEMP ConfigFS tree + fake UDC dir + injected
canonical audio-allowed and derived-lifecycle-readiness probes — exactly the
seams the scripts expose via env (JASPER_CONFIGFS_ROOT /
JASPER_UDC_CLASS_DIR / JASPER_USBGADGET_AUDIO_ALLOWED_CMD /
JASPER_USBGADGET_AUDIO_READY_CMD /
JASPER_USBGADGET_AUDIO_DATA_READY_CMD / JASPER_CPUINFO_FILE). No real ConfigFS,
libcomposite, or systemd — mirrors tests/test_wifi_guardian_script.py.

Each test asserts on:
  1. the structured `event=usb_gadget.<outcome>` line in stderr;
  2. the ConfigFS tree the script wrote (which functions were linked);
  3. the exit code (0 = composed/bound, 1 = ExecCondition "skip").

The truth table (JASPER_USB_NETWORK x audio-intent) is exercised row by row.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UP = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-up"
DOWN = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-down"
WANTED = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-wanted"
NAME_PATCH = ROOT / "deploy" / "usbsink" / "jasper-usbsink-name-patch"
SNAPSHOT = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-snapshot"

# `true` always succeeds (exit 0); `false` always fails (exit 1). The scripts
# run the canonical guard command and branch on its exit status, so these are
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
    audio_ready: str | None = None,
    audio_data_ready: str | None = None,
    hardware_allowed: str = TRUE,
    udc_present: bool = True,
    configfs: Path | None = None,
    cpuinfo_serial: str = "10000000abcdef01",
    speaker_name_file: Path | None = None,
    usb_mic: str = FALSE,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    configfs = configfs if configfs is not None else _configfs(tmp_path)
    udc = _udc_dir(tmp_path, present=udc_present)
    env = os.environ.copy()
    audio_allowed = TRUE if audio_intent == TRUE and audio_gate == TRUE else FALSE
    if audio_ready is None:
        audio_ready = audio_intent
    if audio_data_ready is None:
        audio_data_ready = audio_intent
    env.update({
        "JASPER_CONFIGFS_ROOT": str(configfs),
        "JASPER_UDC_CLASS_DIR": str(udc),
        "JASPER_USBGADGET_AUDIO_ALLOWED_CMD": audio_allowed,
        "JASPER_USBGADGET_AUDIO_READY_CMD": audio_ready,
        "JASPER_USBGADGET_AUDIO_DATA_READY_CMD": audio_data_ready,
        "JASPER_USBGADGET_HARDWARE_ALLOWED_CMD": hardware_allowed,
        "JASPER_USBGADGET_USB_MIC_ENABLED_CMD": usb_mic,
        "JASPER_CPUINFO_FILE": str(_cpuinfo(tmp_path, cpuinfo_serial)),
        # Keep the speaker-name source deterministic + absent by default.
        "JASPER_SPEAKER_NAME_FILE": str(
            speaker_name_file or (tmp_path / "no-such-name.env")
        ),
        "JASPER_SPEAKER_NAME_READER": str(ROOT / ".venv/bin/python"),
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
    assert "audio_reason=intent_disabled_or_parked" in proc.stderr
    assert _linked(cfg, "ncm.usb0")
    assert not _linked(cfg, "uac2.usb0")


def test_up_network_only_when_audio_intent_disabled(tmp_path):
    """network on, audio intent NO -> ncm only (the common default box)."""
    proc, cfg = _run(UP, tmp_path, network="enabled", audio_intent=FALSE)
    assert proc.returncode == 0, proc.stderr
    assert "network=1 audio=0" in proc.stderr
    assert "audio_reason=intent_disabled_or_parked" in proc.stderr
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


def test_up_hardware_unavailable_tears_down_without_composing(tmp_path):
    proc, cfg = _run(
        UP,
        tmp_path,
        network="enabled",
        hardware_allowed=FALSE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "reason=hardware_unavailable" in proc.stderr
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
    """An unset network kill switch defaults On when hardware allows it."""
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


def test_up_killswitch_literal_matrix(tmp_path):
    """Pin every branch of the JASPER_USB_NETWORK literal parser in one
    place: exact-literal `disabled` (any case) is the ONLY way to drop the
    network function; `enabled` (any case) and empty-string are silent
    no-warning enables (the empty case matters — a var present-but-empty in
    an env file is a different shape than unset, and must not warn); any
    other literal (including whitespace-decorated near-misses) warns but
    still stays enabled, so a typo can never silently kill the fallback
    network."""
    cases = [
        # (value, expect_network_on, expect_warning)
        ("disabled", False, False),
        ("Disabled", False, False),
        ("DISABLED", False, False),
        ("enabled", True, False),
        ("Enabled", True, False),
        ("", True, False),
        ("disable", True, True),   # near-miss literal, not the exact word
        (" disabled", True, True),  # leading whitespace breaks the exact match
        ("disabled ", True, True),  # trailing whitespace likewise
        ("0", True, True),
    ]
    for i, (value, expect_on, expect_warning) in enumerate(cases):
        proc, cfg = _run(UP, tmp_path / f"case{i}", network=value, audio_intent=FALSE)
        assert proc.returncode == 0, (value, proc.stderr)
        assert f"network={1 if expect_on else 0} audio=0" in proc.stderr, (value, proc.stderr)
        assert _linked(cfg, "ncm.usb0") is expect_on, (value, proc.stderr)
        assert ("not 'enabled'/'disabled'; staying enabled" in proc.stderr) is expect_warning, (
            value, proc.stderr,
        )


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


def test_up_microphone_terminal_name_derives_from_speaker_name():
    """The configfs fallback label follows the same canonical identity as the
    macOS AudioStreaming label, with one explicit `` Mic`` suffix."""

    text = UP.read_text(encoding="utf-8")
    assert 'MIC_NAME="${SPEAKER_NAME} Mic"' in text
    assert (
        'write_if_present functions/uac2.usb0/p_it_name "${MIC_NAME}"'
        in text
    )
    assert "JTS Microphone" not in text


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


def test_up_usb_microphone_adds_only_the_reverse_uac2_direction(tmp_path):
    proc, cfg = _run(
        UP,
        tmp_path,
        network="enabled",
        audio_intent=TRUE,
        audio_gate=TRUE,
        usb_mic=TRUE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "audio=1 usb_mic=1" in proc.stderr
    fn = _gadget_dir(cfg) / "functions" / "uac2.usb0"
    assert (fn / "c_chmask").read_text().strip() == "3"
    assert (fn / "p_chmask").read_text().strip() == "1"
    assert (_gadget_dir(cfg) / "bcdDevice").read_text().strip() == "0x0210"


def test_up_usb_microphone_never_creates_uac2_without_usb_audio_input(tmp_path):
    proc, cfg = _run(
        UP,
        tmp_path,
        network="enabled",
        audio_intent=FALSE,
        usb_mic=TRUE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "audio=0 usb_mic=0" in proc.stderr
    assert not _linked(cfg, "uac2.usb0")
    assert (_gadget_dir(cfg) / "bcdDevice").read_text().strip() == "0x0200"


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


def test_wanted_skips_when_hardware_transport_is_unavailable(tmp_path):
    proc, _ = _run(
        WANTED,
        tmp_path,
        network="enabled",
        hardware_allowed=FALSE,
    )
    assert proc.returncode == 1
    assert "reason=hardware_unavailable" in proc.stderr


def test_usb_audio_requires_canonical_authority_plus_readiness_mirror():
    """Both paths require intent, lifecycle readiness, and a live direct lane."""

    expected_allowed = (
        "AUDIO_ALLOWED_CMD=\"${JASPER_USBGADGET_AUDIO_ALLOWED_CMD:-"
        "/opt/jasper/.venv/bin/jasper-local-source-allowed --source usbsink}\""
    )
    expected_ready = (
        "AUDIO_READY_CMD=\"${JASPER_USBGADGET_AUDIO_READY_CMD:-"
        "systemctl is-enabled --quiet jasper-usbsink.service}\""
    )
    expected_data_ready = (
        "AUDIO_DATA_READY_CMD=\"${JASPER_USBGADGET_AUDIO_DATA_READY_CMD:-"
        "/opt/jasper/.venv/bin/python -m jasper.fanin.status "
        "--usbsink-direct-armed}\""
    )
    for script in (UP, WANTED):
        text = script.read_text()
        assert expected_allowed in text
        assert expected_ready in text
        assert expected_data_ready in text
        assert "JASPER_USBGADGET_AUDIO_INTENT_CMD" not in text
        assert "JASPER_USBGADGET_AUDIO_GATE_CMD" not in text


def test_name_patch_treats_speaker_state_as_inert_data(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    state = tmp_path / "speaker_name.env"
    state.write_text(
        f'JASPER_SPEAKER_NAME="$(touch {marker})"\n'
        "JASPER_MODULES_ROOT=/should/not/win\n",
        encoding="utf-8",
    )
    modules_root = tmp_path / "modules"
    modules_root.mkdir()

    result = subprocess.run(
        [str(NAME_PATCH)],
        env={
            **os.environ,
            "JASPER_SPEAKER_NAME_FILE": str(state),
            "JASPER_SPEAKER_NAME_READER": str(ROOT / ".venv/bin/python"),
            "JASPER_MODULES_ROOT": str(modules_root),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "event=usbsink_name.no_stock_module" in result.stderr
    assert not marker.exists()
    script = NAME_PATCH.read_text(encoding="utf-8")
    assert 'source "${SPEAKER_NAME_FILE}"' not in script
    assert "\neval " not in script


def test_root_name_readers_reject_malformed_and_unsafe_paths(tmp_path: Path) -> None:
    """Both root scripts consume one bounded regular inode, never raw state."""

    states: list[Path] = []
    for index, raw in enumerate((
        'JASPER_SPEAKER_NAME="' + "A" * 40 + '"\n',
        'JASPER_SPEAKER_NAME="Kitchen\\nforged"\n',
    )):
        case_dir = tmp_path / str(index)
        case_dir.mkdir()
        state = case_dir / "speaker_name.env"
        state.write_text(raw, encoding="utf-8")
        states.append(state)

    oversize_dir = tmp_path / "oversize"
    oversize_dir.mkdir()
    oversize = oversize_dir / "speaker_name.env"
    oversize.write_bytes(b'JASPER_SPEAKER_NAME="Kitchen"\n' + b"#" * (64 * 1024))
    states.append(oversize)

    symlink_dir = tmp_path / "symlink"
    symlink_dir.mkdir()
    symlink_target = symlink_dir / "target.env"
    symlink_target.write_text('JASPER_SPEAKER_NAME="Forged"\n', encoding="utf-8")
    symlink = symlink_dir / "speaker_name.env"
    symlink.symlink_to(symlink_target)
    states.append(symlink)

    fifo_dir = tmp_path / "fifo"
    fifo_dir.mkdir()
    fifo = fifo_dir / "speaker_name.env"
    os.mkfifo(fifo)
    states.append(fifo)

    for state in states:
        case_dir = state.parent
        proc, configfs = _run(
            UP,
            case_dir,
            audio_intent=FALSE,
            speaker_name_file=state,
        )
        assert proc.returncode == 0, proc.stderr
        product = _gadget_dir(configfs) / "strings/0x409/product"
        assert product.read_text().strip() == "JTS"

        modules_root = case_dir / "modules"
        modules_root.mkdir(exist_ok=True)
        patch_result = subprocess.run(
            [str(NAME_PATCH)],
            env={
                **os.environ,
                "JASPER_SPEAKER_NAME_FILE": str(state),
                "JASPER_SPEAKER_NAME_READER": str(ROOT / ".venv/bin/python"),
                "JASPER_MODULES_ROOT": str(modules_root),
            },
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert patch_result.returncode == 0
        assert "event=usbsink_name.no_stock_module" in patch_result.stderr

    for script in (UP, NAME_PATCH):
        text = script.read_text(encoding="utf-8")
        assert '"${SPEAKER_NAME_READER}" -m jasper.speaker_name' in text
        assert 'source "${SPEAKER_NAME_FILE}"' not in text
        assert "wc -c" not in text


def test_up_canonical_off_dominates_stale_enabled_mirror(tmp_path):
    """Derived enablement cannot authorize UAC2 after the household turns it Off."""
    proc, cfg = _run(
        UP,
        tmp_path,
        network="enabled",
        audio_intent=FALSE,
        audio_ready=TRUE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "network=1 audio=0" in proc.stderr
    assert "audio_reason=intent_disabled_or_parked" in proc.stderr
    assert _linked(cfg, "ncm.usb0")
    assert not _linked(cfg, "uac2.usb0")


def test_up_desired_on_but_lifecycle_not_ready_composes_network_only(tmp_path):
    """A failed/stale On transition must not advertise UAC2 without its consumer."""
    proc, cfg = _run(
        UP,
        tmp_path,
        network="enabled",
        audio_intent=TRUE,
        audio_gate=TRUE,
        audio_ready=FALSE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "network=1 audio=0" in proc.stderr
    assert "audio_reason=derived_unit_disabled" in proc.stderr
    assert _linked(cfg, "ncm.usb0")
    assert not _linked(cfg, "uac2.usb0")


def test_up_desired_on_but_direct_lane_unarmed_composes_network_only(tmp_path):
    proc, cfg = _run(
        UP,
        tmp_path,
        network="enabled",
        audio_intent=TRUE,
        audio_gate=TRUE,
        audio_ready=TRUE,
        audio_data_ready=FALSE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "network=1 audio=0" in proc.stderr
    assert "audio_reason=direct_lane_unarmed" in proc.stderr
    assert _linked(cfg, "ncm.usb0")
    assert not _linked(cfg, "uac2.usb0")


def test_wanted_canonical_off_dominates_stale_enabled_mirror(tmp_path):
    proc, _ = _run(
        WANTED,
        tmp_path,
        network="enabled",
        audio_intent=FALSE,
        audio_ready=TRUE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "network=1 audio=0" in proc.stderr
    assert "audio_reason=intent_disabled_or_parked" in proc.stderr


def test_wanted_desired_on_but_lifecycle_not_ready_keeps_network(tmp_path):
    proc, _ = _run(
        WANTED,
        tmp_path,
        network="enabled",
        audio_intent=TRUE,
        audio_gate=TRUE,
        audio_ready=FALSE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "network=1 audio=0" in proc.stderr
    assert "audio_reason=derived_unit_disabled" in proc.stderr


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


# ---------- jasper-usbgadget-snapshot --------------------------------------


def _run_snapshot(
    tmp_path: Path, reason: str = "pre_reset", *, extra_env: dict[str, str] | None = None,
):
    configfs = _configfs(tmp_path)
    gadget = _gadget_dir(configfs)
    (gadget / "functions" / "uac2.usb0").mkdir(parents=True, exist_ok=True)
    (gadget / "UDC").write_text("1000480000.usb\n")
    (gadget / "bcdDevice").write_text("0x0210\n")
    (gadget / "functions" / "uac2.usb0" / "c_chmask").write_text("3\n")
    (gadget / "functions" / "uac2.usb0" / "p_chmask").write_text("1\n")

    udc = tmp_path / "udc" / "1000480000.usb"
    udc.mkdir(parents=True, exist_ok=True)
    (udc / "state").write_text("configured\n")

    debug = tmp_path / "debug" / "1000480000.usb"
    debug.mkdir(parents=True, exist_ok=True)
    (debug / "state").write_text(
        "GINTMSK=0xd0bc3c44, GINTSTS=0x04048038\n"
        "DAINTMSK=0x0003000f, DAINT=0x00000002\n"
    )
    (debug / "regdump").write_text("DIEPINT(1)=0x00000090\n")
    (debug / "ep1in").write_text("request pending res -115\n")

    interrupts = tmp_path / "interrupts"
    interrupts.write_text("34: 3927844733 0 0 0 GICv3 1000480000.usb\n")
    usb_mic_status = tmp_path / "usbmic.json"
    usb_mic_status.write_text('{"host_streaming": false}\n')
    incident_dir = tmp_path / "incidents"

    env = os.environ.copy()
    env.update({
        "JASPER_USBGADGET_SNAPSHOT_CONFIGFS_ROOT": str(configfs),
        "JASPER_USBGADGET_SNAPSHOT_UDC_CLASS_DIR": str(tmp_path / "udc"),
        "JASPER_USBGADGET_SNAPSHOT_DEBUG_ROOT": str(tmp_path / "debug"),
        "JASPER_USBGADGET_SNAPSHOT_PROC_INTERRUPTS": str(interrupts),
        "JASPER_USBGADGET_SNAPSHOT_USB_MIC_STATUS": str(usb_mic_status),
        "JASPER_USBGADGET_SNAPSHOT_DIR": str(incident_dir),
    })
    env.update(extra_env or {})
    proc = subprocess.run(
        ["bash", str(SNAPSHOT), reason],
        check=False,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
    )
    return proc, incident_dir


def test_snapshot_preserves_wedged_controller_evidence(tmp_path):
    proc, incident_dir = _run_snapshot(tmp_path)

    assert proc.returncode == 0, proc.stderr
    snapshots = list(incident_dir.glob("usb-gadget-*.txt"))
    assert len(snapshots) == 1
    body = snapshots[0].read_text()
    assert "reason=pre_reset" in body
    assert "udc_state=configured" in body
    assert "bcdDevice=0x0210" in body
    assert "GINTSTS=0x04048038" in body
    assert "DAINT=0x00000002" in body
    assert "DIEPINT(1)=0x00000090" in body
    assert "request pending res -115" in body
    assert '"host_streaming": false' in body
    assert "event=usb_gadget.snapshot" in proc.stderr
    assert "gintsts=0x04048038" in proc.stderr
    assert "daint=0x00000002" in proc.stderr
    assert len(proc.stderr.splitlines()) == 1


def test_snapshot_rotation_is_bounded_and_preserves_unrelated_files(tmp_path):
    proc, incident_dir = _run_snapshot(tmp_path)
    assert proc.returncode == 0
    unrelated = incident_dir / "keep-me.txt"
    unrelated.write_text("operator evidence\n")
    for index in range(20):
        (incident_dir / f"usb-gadget-20000101T0000{index:02d}Z-old-{index}.txt").write_text(
            "old\n"
        )

    proc, incident_dir = _run_snapshot(tmp_path, reason="post_start")

    assert proc.returncode == 0, proc.stderr
    assert len(list(incident_dir.glob("usb-gadget-*.txt"))) == 12
    assert unrelated.read_text() == "operator evidence\n"


def test_snapshot_sanitizes_reason_before_using_it_in_filename(tmp_path):
    proc, incident_dir = _run_snapshot(tmp_path, reason="../../surprise")

    assert proc.returncode == 0, proc.stderr
    snapshots = list(incident_dir.glob("usb-gadget-*.txt"))
    assert len(snapshots) == 1
    assert "-manual-" in snapshots[0].name
    assert "reason=manual" in snapshots[0].read_text()


def test_forensics_watch_writes_only_to_bounded_ram_timeline(tmp_path):
    # Seed the same fake controller tree used by snapshot tests, then remove
    # that setup capture so the watcher starts with a genuinely empty disk set.
    _proc, incident_dir = _run_snapshot(tmp_path)
    for artifact in incident_dir.glob("usb-gadget-*.txt"):
        artifact.unlink()
    enabled = tmp_path / "forensics.env"
    enabled.write_text("JASPER_USB_GADGET_FORENSICS=1\n")
    run_dir = tmp_path / "forensics-run"
    env = os.environ.copy()
    env.update({
        "JASPER_USBGADGET_SNAPSHOT_CONFIGFS_ROOT": str(tmp_path / "configfs"),
        "JASPER_USBGADGET_SNAPSHOT_UDC_CLASS_DIR": str(tmp_path / "udc"),
        "JASPER_USBGADGET_SNAPSHOT_DEBUG_ROOT": str(tmp_path / "debug"),
        "JASPER_USBGADGET_SNAPSHOT_PROC_INTERRUPTS": str(tmp_path / "interrupts"),
        "JASPER_USBGADGET_SNAPSHOT_DIR": str(incident_dir),
        "JASPER_USBGADGET_FORENSICS_ENABLED_FILE": str(enabled),
        "JASPER_USBGADGET_FORENSICS_RUN_DIR": str(run_dir),
        "JASPER_USBGADGET_FORENSICS_INTERVAL": "0.005",
        "JASPER_USBGADGET_FORENSICS_MAX_BYTES": "256",
    })
    watcher = subprocess.Popen(
        ["bash", str(SNAPSHOT), "watch"], cwd=ROOT, env=env,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 3
    state = {}
    while time.monotonic() < deadline:
        try:
            state = json.loads((run_dir / "status.json").read_text())
        except (OSError, json.JSONDecodeError):
            pass
        if state.get("sample_count", 0) >= 10:
            break
        time.sleep(0.02)
    enabled.unlink()
    _stdout, stderr = watcher.communicate(timeout=3)

    assert watcher.returncode == 0, stderr
    assert state["ram_cap_bytes"] == 512
    assert (run_dir / "timeline.tsv").stat().st_size <= 256
    assert (run_dir / "timeline.previous.tsv").stat().st_size <= 256
    assert not list(incident_dir.glob("usb-gadget-*.txt"))
    assert "event=usb_gadget.forensics state=started" in stderr


def test_snapshot_freezes_only_tail_of_forensics_timeline(tmp_path):
    run_dir = tmp_path / "forensics-run"
    run_dir.mkdir()
    (run_dir / "timeline.previous.tsv").write_text("old-sample\n")
    (run_dir / "timeline.tsv").write_text("new-sample\n")

    proc, incident_dir = _run_snapshot(tmp_path, extra_env={
        "JASPER_USBGADGET_FORENSICS_RUN_DIR": str(run_dir),
    })

    assert proc.returncode == 0, proc.stderr
    body = next(incident_dir.glob("usb-gadget-*.txt")).read_text()
    assert "[forensics_timeline]" in body
    assert body.index("old-sample") < body.index("new-sample")
