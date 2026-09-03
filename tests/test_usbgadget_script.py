# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Hermetic subprocess tests for the composite USB gadget scripts.

deploy/usbsink/jasper-usbgadget-{up,down,wanted} are pure-bash policy scripts.
They are driven here against a TEMP ConfigFS tree + fake UDC dir + an injected
canonical audio-allowed probe — exactly the seams the scripts expose via env
(JASPER_CONFIGFS_ROOT / JASPER_UDC_CLASS_DIR /
JASPER_USBGADGET_AUDIO_ALLOWED_CMD / JASPER_CPUINFO_FILE). No real ConfigFS,
libcomposite, or systemd — mirrors tests/test_wifi_guardian_script.py.

Each test asserts on:
  1. the structured `event=usb_gadget.<outcome>` line in stderr;
  2. the ConfigFS tree the script wrote (which functions were linked);
  3. the exit code (0 = composed/bound, 1 = ExecCondition "skip").

The truth table (JASPER_USB_NETWORK x audio-intent) is exercised row by row.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jasper.local_sources.markers import marker_path
from jasper.music_sources import Source

ROOT = Path(__file__).resolve().parents[1]
UP = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-up"
DOWN = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-down"
WANTED = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-wanted"
CONVERGE = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-converge"
COMPOSE = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-compose.sh"
INSTALL_FRAGMENT = ROOT / "deploy" / "lib" / "install" / "systemd-units.sh"
NAME_PATCH = ROOT / "deploy" / "usbsink" / "jasper-usbsink-name-patch"
SNAPSHOT = ROOT / "deploy" / "usbsink" / "jasper-usbgadget-snapshot"

# `true` always succeeds (exit 0); `false` always fails (exit 1). The scripts
# run the canonical guard command and branch on its exit status, so these are
# the cleanest injectable probes.
TRUE = "/usr/bin/true"
FALSE = "/usr/bin/false"

# jasper_usbgadget_refresh_consumers (jasper-usbgadget-compose.sh) wraps both
# its try-restarts in the fixed path /usr/bin/timeout, so a test asserting a
# real "ok" outcome from it needs that binary to exist -- present on the
# Pi/Ubuntu-CI target, absent on a bare macOS dev machine with no GNU
# coreutils. Shared by both the converge and the snapshot repair tests below.
_HAS_USR_BIN_TIMEOUT = Path("/usr/bin/timeout").exists()
_NO_USR_BIN_TIMEOUT_REASON = (
    "jasper_usbgadget_refresh_consumers shells out to the fixed path "
    "/usr/bin/timeout; present on the Pi/Ubuntu-CI target, absent on a bare "
    "macOS dev machine with no GNU coreutils"
)


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
    hardware_allowed: str = TRUE,
    udc_present: bool = True,
    configfs: Path | None = None,
    cpuinfo_serial: str = "10000000abcdef01",
    speaker_name_file: Path | None = None,
    usb_mic: bool = False,
    usb_mic_intent_file: Path | None = None,
    env_file: Path | None = None,
    env_extra: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    configfs = configfs if configfs is not None else _configfs(tmp_path)
    udc = _udc_dir(tmp_path, present=udc_present)
    env = os.environ.copy()
    audio_allowed = TRUE if audio_intent == TRUE and audio_gate == TRUE else FALSE
    if usb_mic_intent_file is None:
        usb_mic_intent_file = tmp_path / "usb_mic.env"
        tmp_path.mkdir(parents=True, exist_ok=True)
        usb_mic_intent_file.write_text(
            f"JASPER_USB_MIC={'enabled' if usb_mic else 'disabled'}\n",
            encoding="utf-8",
        )
    env.update({
        "JASPER_CONFIGFS_ROOT": str(configfs),
        "JASPER_UDC_CLASS_DIR": str(udc),
        "JASPER_USBGADGET_AUDIO_ALLOWED_CMD": audio_allowed,
        "JASPER_USBGADGET_HARDWARE_ALLOWED_CMD": hardware_allowed,
        "JASPER_USB_MIC_INTENT_FILE": str(usb_mic_intent_file),
        "JASPER_CPUINFO_FILE": str(_cpuinfo(tmp_path, cpuinfo_serial)),
        # Keep the speaker-name source deterministic + absent by default.
        "JASPER_SPEAKER_NAME_FILE": str(
            speaker_name_file or (tmp_path / "no-such-name.env")
        ),
        # Absent by default, so the rows below exercise the resolver seam above
        # rather than the reconciler's marker short circuit.
        "JASPER_USBGADGET_MANAGEMENT_TRANSPORT_MARKER": str(
            tmp_path / "no-such-management-transport.ok"
        ),
        # Absent by default so a developer box that HAS /etc/jasper/jasper.env
        # cannot leak its kill switch into these rows.
        "JASPER_USBGADGET_ENV_FILE": str(
            env_file or (tmp_path / "no-such-jasper.env")
        ),
    })
    if network is None:
        env.pop("JASPER_USB_NETWORK", None)
    else:
        env["JASPER_USB_NETWORK"] = network
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        ["bash", str(script), *(args or [])],
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
        usb_mic=True,
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
        usb_mic=True,
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


def test_up_rebuilds_a_bound_descriptor_whose_composition_drifted(tmp_path):
    """Bound is not converged. A bound descriptor carrying the WRONG function
    set takes the teardown-and-rebuild branch, not the idempotent fast path.

    Only the DECISION is asserted here: the rebuild's ``mkdir`` needs the real
    ConfigFS teardown to have completed, which a plain-file temp tree cannot do
    (see test_down_tears_down_both_functions)."""
    proc1, cfg = _run(UP, tmp_path, network="enabled", audio_intent=FALSE)
    assert proc1.returncode == 0, proc1.stderr
    assert not _linked(cfg, "uac2.usb0")
    # Same bound descriptor, but audio is now wanted.
    proc2, _ = _run(
        UP, tmp_path, network="enabled", audio_intent=TRUE, audio_gate=TRUE,
        configfs=cfg,
    )
    assert "already_bound" not in proc2.stderr
    assert (
        "event=usb_gadget.recompose reason=composition_drift "
        "live_network=1 live_audio=0 live_usb_mic=0 "
        "want_network=1 want_audio=1 want_usb_mic=0"
    ) in proc2.stderr
    # The teardown half is observable: the old function set is gone and the
    # descriptor is unbound.
    assert not _linked(cfg, "ncm.usb0")
    assert (_gadget_dir(cfg) / "UDC").read_text().strip() == ""


# ---------- jasper-usbgadget-converge (the one caller-facing entry) ---------


def _systemctl_shim(
    tmp_path: Path,
    *,
    restart_rc: int = 0,
    tears_down: Path | None = None,
) -> tuple[Path, Path]:
    """A fake systemctl that logs its argv, injected through the converger's
    documented JASPER_USBGADGET_SYSTEMCTL seam. ``tears_down`` models the unit's
    ExecStop actually removing the descriptor, which a restart against an
    already-inactive unit does NOT do."""
    log = tmp_path / "systemctl.log"
    shim = tmp_path / "systemctl-shim"
    teardown = (
        f'rm -rf "{_gadget_dir(tears_down)}"\n' if tears_down is not None else ""
    )
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{log}"\n'
        'if [[ "${1:-}" == "restart" ]]; then\n'
        f"  {teardown}"
        f"  exit {restart_rc}\n"
        "fi\n"
        "exit 0\n"
    )
    shim.chmod(0o755)
    return shim, log


def _tree(configfs: Path) -> dict[str, str]:
    """Every path under the gadget dir plus the bytes of each regular file, so
    a test can prove ZERO ConfigFS writes rather than merely no unit job."""
    root = _gadget_dir(configfs)
    if not root.exists():
        return {}
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = f"link:{os.readlink(path)}"
        elif path.is_file():
            out[rel] = path.read_text()
        else:
            out[rel] = "dir"
    return out


def _jasper_env(tmp_path: Path, body: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "jasper.env"
    path.write_text(body)
    return path


def _converge(
    tmp_path: Path,
    configfs: Path,
    *,
    restart_rc: int = 0,
    tears_down: bool = False,
    **kwargs,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    shim, log = _systemctl_shim(
        tmp_path,
        restart_rc=restart_rc,
        tears_down=configfs if tears_down else None,
    )
    proc, _ = _run(
        CONVERGE, tmp_path, configfs=configfs,
        env_extra={"JASPER_USBGADGET_SYSTEMCTL": str(shim)},
        args=["deploy"],
        **kwargs,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def test_converge_on_matching_composition_writes_nothing_and_runs_no_job(tmp_path):
    """#3194: a deploy over a converged gadget must not re-enumerate the host."""
    proc1, cfg = _run(UP, tmp_path, network="enabled", audio_intent=TRUE, audio_gate=TRUE)
    assert proc1.returncode == 0, proc1.stderr
    before = _tree(cfg)

    proc, calls = _converge(
        tmp_path, cfg, network="enabled", audio_intent=TRUE, audio_gate=TRUE,
    )

    assert proc.returncode == 0, proc.stderr
    assert "event=usb_gadget.converge state=unchanged" in proc.stderr
    assert calls == []
    assert _tree(cfg) == before


@pytest.mark.skipif(not _HAS_USR_BIN_TIMEOUT, reason=_NO_USR_BIN_TIMEOUT_REASON)
def test_converge_on_changed_composition_rebuilds_then_refreshes_in_order(tmp_path):
    """A real difference costs one rebuild and the ordered consumer refresh."""
    proc1, cfg = _run(UP, tmp_path, network="enabled", audio_intent=FALSE)
    assert proc1.returncode == 0, proc1.stderr

    proc, calls = _converge(
        tmp_path, cfg, network="enabled", audio_intent=TRUE, audio_gate=TRUE,
    )

    assert proc.returncode == 0, proc.stderr
    assert "event=usb_gadget.converge state=rebuilding" in proc.stderr
    assert "event=usb_gadget.converge state=rebuilt" in proc.stderr
    assert "fanin=ok usbmic=ok" in proc.stderr
    assert calls == [
        "restart jasper-usbgadget.service",
        "try-restart jasper-fanin.service",
        "try-restart jasper-usbmic.service",
    ]


def test_converge_reads_the_kill_switch_from_the_env_file_it_did_not_inherit(
    tmp_path,
):
    """#3194 round 2 (B1): the kill switch reaches the gadget UNIT through its
    EnvironmentFile=, but the converger runs from install.sh and from
    jasper-usbmic-apply, neither of which loads it. If the converger could not
    see it, a kill-switched box would compute desired network=1 against a live
    network=0 forever — every deploy and every mic toggle a rebuild."""
    env_file = _jasper_env(
        tmp_path / "env",
        "# JASPER_USB_NETWORK=enabled\nJASPER_USB_NETWORK=disabled\n",
    )
    # The converged box: kill switch on, audio on -> uac2 only.
    up, cfg = _run(
        UP, tmp_path, network="disabled", audio_intent=TRUE, audio_gate=TRUE,
    )
    assert up.returncode == 0, up.stderr
    assert not _linked(cfg, "ncm.usb0")
    before = _tree(cfg)

    proc, calls = _converge(
        tmp_path / "converge", cfg,
        network=None, env_file=env_file,
        audio_intent=TRUE, audio_gate=TRUE,
    )

    assert proc.returncode == 0, proc.stderr
    assert "state=unchanged" in proc.stderr
    assert "network=0 audio=1 usb_mic=0" in proc.stderr
    assert calls == []
    assert _tree(cfg) == before


def test_env_file_kill_switch_is_read_the_way_the_python_parser_reads_it(tmp_path):
    """One key, parsed as jasper.env_load.parse_env_text parses it: comments
    skipped, quotes stripped, surrounding whitespace trimmed, last wins. An
    inherited assignment still beats the file, and an unreadable or keyless file
    falls back to the default-on network rather than dropping it."""
    cases = [
        ("JASPER_USB_NETWORK=disabled\n", 0),
        ('JASPER_USB_NETWORK="disabled"\n', 0),
        ("JASPER_USB_NETWORK='disabled'\n", 0),
        ("  JASPER_USB_NETWORK =  disabled  \n", 0),
        ("JASPER_USB_NETWORK=DISABLED\n", 0),
        ("JASPER_USB_NETWORK=disabled\nJASPER_USB_NETWORK=enabled\n", 1),
        ("# JASPER_USB_NETWORK=disabled\n", 1),
        ("JASPER_USB_NETWORK_EXTRA=disabled\n", 1),
        ("JASPER_USB_NETWORK=\n", 1),
        ("", 1),
    ]
    for index, (body, expect_network) in enumerate(cases):
        env_file = _jasper_env(tmp_path / f"env{index}", body)
        proc, _cfg = _run(
            UP, tmp_path / f"case{index}",
            network=None, env_file=env_file, audio_intent=TRUE, audio_gate=TRUE,
        )
        assert proc.returncode == 0, (body, proc.stderr)
        assert f"network={expect_network} audio=1" in proc.stderr, (
            body, proc.stderr,
        )

    # An inherited assignment wins over the file, as it does for the unit.
    env_file = _jasper_env(tmp_path / "envwin", "JASPER_USB_NETWORK=disabled\n")
    proc, _cfg = _run(
        UP, tmp_path / "inherited",
        network="enabled", env_file=env_file, audio_intent=TRUE, audio_gate=TRUE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "network=1 audio=1" in proc.stderr

    # A missing file leaves the default-on network rather than dropping it.
    proc, _cfg = _run(
        UP, tmp_path / "absent",
        network=None, env_file=tmp_path / "nope.env",
        audio_intent=TRUE, audio_gate=TRUE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "network=1 audio=1" in proc.stderr


def test_converge_treats_the_microphone_as_part_of_the_composition(tmp_path):
    """The usb_mic field is why a /wake toggle reaches the gadget at all — and
    why re-applying the shape it already carries does not."""
    proc1, cfg = _run(
        UP, tmp_path, network="enabled", audio_intent=TRUE, audio_gate=TRUE,
        usb_mic=True,
    )
    assert proc1.returncode == 0, proc1.stderr

    same, same_calls = _converge(
        tmp_path / "same", cfg, network="enabled", audio_intent=TRUE,
        audio_gate=TRUE, usb_mic=True,
    )
    assert same.returncode == 0, same.stderr
    assert "state=unchanged" in same.stderr
    assert same_calls == []

    off, off_calls = _converge(
        tmp_path / "off", cfg, network="enabled", audio_intent=TRUE,
        audio_gate=TRUE, usb_mic=False,
    )
    assert off.returncode == 0, off.stderr
    assert "state=rebuilding" in off.stderr
    assert off_calls[0] == "restart jasper-usbgadget.service"


def test_converge_withdraws_a_descriptor_no_longer_wanted(tmp_path):
    """Desired "no descriptor" against a bound one is still a difference."""
    proc1, cfg = _run(UP, tmp_path, network="enabled", audio_intent=FALSE)
    assert proc1.returncode == 0, proc1.stderr

    proc, calls = _converge(
        tmp_path, cfg, network="disabled", audio_intent=FALSE, tears_down=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "state=rebuilding" in proc.stderr
    assert "want_bound=0" in proc.stderr
    assert calls[0] == "restart jasper-usbgadget.service"


def test_converge_is_a_noop_when_nothing_is_wanted_and_nothing_exists(tmp_path):
    proc, calls = _converge(
        tmp_path, _configfs(tmp_path), network="disabled", audio_intent=FALSE,
    )

    assert proc.returncode == 0, proc.stderr
    assert "state=unchanged" in proc.stderr
    assert "want_bound=0 network=0 audio=0 usb_mic=0" in proc.stderr
    assert calls == []


def test_converge_clears_an_unbound_leftover_when_nothing_is_wanted(tmp_path):
    """Presence, not just binding, is part of the comparison: a descriptor the
    host cannot see is still not the desired "no descriptor" end state."""
    _up, cfg = _run(UP, tmp_path, network="enabled", audio_intent=FALSE)
    down, _ = _run(DOWN, tmp_path, configfs=cfg)
    assert down.returncode == 0, down.stderr
    assert _gadget_dir(cfg).exists()
    assert (_gadget_dir(cfg) / "UDC").read_text().strip() == ""

    proc, calls = _converge(
        tmp_path, cfg, network="disabled", audio_intent=FALSE, tears_down=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert "state=rebuilding" in proc.stderr
    assert "live_bound=0 " in proc.stderr
    assert calls[0] == "restart jasper-usbgadget.service"


def test_converge_fails_closed_and_skips_the_refresh_when_the_rebuild_fails(tmp_path):
    """Consumers are refreshed for a rebuild that happened, never a failed one,
    and the caller must be able to stop on a composition it could not reach."""
    proc1, cfg = _run(UP, tmp_path, network="enabled", audio_intent=FALSE)
    assert proc1.returncode == 0, proc1.stderr

    proc, calls = _converge(
        tmp_path, cfg, network="enabled", audio_intent=TRUE, audio_gate=TRUE,
        restart_rc=1,
    )

    assert proc.returncode == 1
    assert "event=usb_gadget.converge state=failed" in proc.stderr
    assert "phase=restart" in proc.stderr
    assert calls == ["restart jasper-usbgadget.service"]


def test_converge_fails_closed_when_a_withdrawal_leaves_the_descriptor_behind(tmp_path):
    """A restart against an already-inactive unit runs only the start half, so a
    leftover descriptor survives still advertised. Reporting that as `rebuilt`
    would hand the caller a stale endpoint; the converge reports it and fails."""
    _up, cfg = _run(UP, tmp_path, network="enabled", audio_intent=FALSE)

    proc, calls = _converge(tmp_path, cfg, network="disabled", audio_intent=FALSE)

    assert proc.returncode == 1
    assert "event=usb_gadget.converge state=failed" in proc.stderr
    assert "phase=verify" in proc.stderr
    assert "want_bound=0 live_present=1 live_bound=1" in proc.stderr
    assert calls == ["restart jasper-usbgadget.service"]


def test_converge_fails_closed_when_a_rebuild_leaves_nothing_bound(tmp_path):
    """The composing direction gets the same post-condition: a start half that
    skipped or failed leaves no descriptor, and a `rebuilt` line asserting the
    desired shape would be a claim the converge never checked."""
    _up, cfg = _run(UP, tmp_path, network="enabled", audio_intent=FALSE)

    proc, calls = _converge(
        tmp_path, cfg, network="enabled", audio_intent=TRUE, audio_gate=TRUE,
        tears_down=True,
    )

    assert proc.returncode == 1
    assert "event=usb_gadget.converge state=failed" in proc.stderr
    assert "phase=verify" in proc.stderr
    assert "want_bound=1 live_present=0 live_bound=0" in proc.stderr
    assert calls == ["restart jasper-usbgadget.service"]


def test_converge_reports_the_composition_it_read_back_not_the_one_it_wanted(
    tmp_path,
):
    """A rebuild that landed on a different shape than the truth table computed
    has to name itself, so the rebuilt line carries LIVE values."""
    _up, cfg = _run(UP, tmp_path, network="enabled", audio_intent=FALSE)

    proc, _calls = _converge(
        tmp_path, cfg, network="enabled", audio_intent=TRUE, audio_gate=TRUE,
    )

    assert proc.returncode == 0, proc.stderr
    # want_audio=1 was asked for; the descriptor still carries audio=0.
    assert "want_audio=1" in proc.stderr
    assert "state=rebuilt" in proc.stderr
    assert "network=1 audio=0 usb_mic=0 order=fanin,usbmic" in proc.stderr


def test_converge_withdraws_the_gadget_when_hardware_is_unavailable(tmp_path):
    proc1, cfg = _run(UP, tmp_path, network="enabled", audio_intent=FALSE)
    assert proc1.returncode == 0, proc1.stderr

    proc, calls = _converge(tmp_path, cfg, hardware_allowed=FALSE, tears_down=True)

    assert proc.returncode == 0, proc.stderr
    assert "state=rebuilding" in proc.stderr
    assert "want_bound=0" in proc.stderr
    assert calls[0] == "restart jasper-usbgadget.service"


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
    """Canonical authority is the ONE audio probe, defined once.

    It used to be written out in both gadget-up and gadget-wanted, and a third
    time in the installer. It now lives only in the shared truth table every
    consumer sources, so the copies cannot drift.

    The readiness-mirror and direct-lane probes that once sat beside it are
    gone (ADR-0191): derived state is disclosed, never composed on. Their env
    names are asserted absent, so re-adding either gate fails here.
    """

    audio_marker_path = marker_path(Source.USBSINK.value)
    direct_armed_probe = (
        "/opt/jasper/.venv/bin/python -m jasper.fanin.status "
        "--usbsink-direct-armed"
    )
    compose = COMPOSE.read_text()
    assert audio_marker_path in compose
    assert "JASPER_USBGADGET_AUDIO_INTENT_CMD" not in compose
    assert "JASPER_USBGADGET_AUDIO_GATE_CMD" not in compose
    assert "JASPER_USBGADGET_AUDIO_READY_CMD" not in compose
    assert "JASPER_USBGADGET_AUDIO_DATA_READY_CMD" not in compose
    assert direct_armed_probe not in compose

    source_line = 'source "$(dirname "$0")/jasper-usbgadget-compose.sh"'
    for script in (UP, WANTED, CONVERGE):
        text = script.read_text()
        assert source_line in text, script.name
        assert audio_marker_path not in text, script.name

    # Negative proof for the deleted #3198 machinery: the installer no longer
    # carries its own copy of the direct-lane probe, nor the recompose gate and
    # consumer refresh that copy existed to serve. The converge subsumes all
    # three.
    installer = INSTALL_FRAGMENT.read_text()
    assert direct_armed_probe not in installer
    assert "usbsink_direct_lane_armed" not in installer
    assert "refresh_usb_stream_consumers" not in installer
    assert "usb_gadget_baseline_recompose_skipped" not in installer


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
            "JASPER_MODULES_ROOT": str(modules_root),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "event=usbsink_name.no_stock_module" in result.stderr
    assert not marker.exists()


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
                "JASPER_MODULES_ROOT": str(modules_root),
            },
            text=True,
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert patch_result.returncode == 0
        assert "event=usbsink_name.no_stock_module" in patch_result.stderr


def _compose_fact(fact: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{COMPOSE}"\n{fact}\n'],
        check=False, cwd=ROOT, env={**os.environ, **env}, text=True,
        capture_output=True, timeout=30,
    )


@pytest.mark.parametrize("body", [
    'JASPER_SPEAKER_NAME="Kitchen"\n',
    "JASPER_SPEAKER_NAME=Kitchen\n",
    "JASPER_SPEAKER_NAME='Living Room #2'\n",
    'JASPER_SPEAKER_NAME="A"\nJASPER_SPEAKER_NAME="B"\n',
    'JASPER_SPEAKER_ROOM="Hall"\nJASPER_SPEAKER_NAME="Kitchen Two"\n',
    '  JASPER_SPEAKER_NAME="  Kitchen   Two "\n',
    'JASPER_SPEAKER_NAME=""\n',
    'JASPER_SPEAKER_NAME="' + "A" * 33 + '"\n',
    'JASPER_SPEAKER_NAME="Kitchen/Bath"\n',
    'JASPER_SPEAKER_NAME="-Kitchen-"\n',
    'JASPER_SPEAKER_NAME="Café"\n',
    'JASPER_SPEAKER_ROOM="Kitchen"\n',
    # Hand-edited shapes, where the shell must follow shlex rather than the
    # looser env_load parse the network key uses.
    "JASPER_SPEAKER_NAME=Kitchen # home\n",
    "  JASPER_SPEAKER_NAME = Kitchen \n",
    "JASPER_SPEAKER_NAME=Living Room\n",
    "",
])
def test_shell_and_python_name_readers_cannot_disagree(tmp_path: Path, body: str):
    """One answer from both readers: quoting, whitespace, length, character
    policy and the JTS fallback. A quoted value with trailing text is the one
    shape left out — it lands on JTS, as an unreadable file does."""
    from jasper.speaker_name import read_state

    state = tmp_path / "speaker_name.env"
    state.write_text(body, encoding="utf-8")

    proc = _compose_fact(
        'printf %s "$(jasper_usbgadget_speaker_name)"',
        {"JASPER_SPEAKER_NAME_FILE": str(state)},
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == read_state(str(state)).name


# jasper.usb_mic.read_intent's own cap, tighter than the identity reader's
# 64 KiB. The rows either side of it are the shapes a byte count taken after
# shell mangling (stripped trailing newlines, deleted NULs) would misjudge.
_INTENT_CAP = 4096
_INTENT_ON = b"JASPER_USB_MIC=enabled\n"


@pytest.mark.parametrize("body", [
    b"JASPER_USB_MIC=enabled\n",
    b'JASPER_USB_MIC="enabled"\n',
    b"  JASPER_USB_MIC = 'enabled'  \n",
    b"JASPER_USB_MIC=enabled\nJASPER_USB_MIC=disabled\n",
    b"JASPER_USB_MIC=disabled\nJASPER_USB_MIC=enabled\n",
    b"JASPER_USB_MIC=disabled\n",
    b"JASPER_USB_MIC=ENABLED\n",
    b"JASPER_USB_MIC=\n",
    b"JASPER_USB_MIC_LEG=primary\n",
    b"\x00\xff not an env file at all\n",
    _INTENT_ON + b"#" * (_INTENT_CAP - len(_INTENT_ON) - 1) + b"\n",
    _INTENT_ON + b"#" * (_INTENT_CAP + 1 - len(_INTENT_ON) - 1) + b"\n",
    _INTENT_ON + b"#" * (_INTENT_CAP + 2 - len(_INTENT_ON) - 2) + b"\n\n",
    _INTENT_ON + b"\x00" * (_INTENT_CAP + 1),
    b"",
])
def test_shell_and_python_intent_readers_cannot_disagree(tmp_path: Path, body: bytes):
    """Only an explicit, valid `enabled` composes p_chmask=1; every other
    reading, the over-cap file included, is Off. An unbalanced quote
    (`="enabled`) is the one shape left out — it resolves Off."""
    from jasper.usb_mic import usb_mic_enabled

    intent = tmp_path / "usb_mic.env"
    intent.write_bytes(body)

    proc = _compose_fact(
        "jasper_usbgadget_usb_mic_enabled", {"JASPER_USB_MIC_INTENT_FILE": str(intent)}
    )

    assert (proc.returncode == 0) is usb_mic_enabled(intent)


@pytest.mark.parametrize("readable", [False, True])
def test_readers_refuse_state_they_cannot_bound(tmp_path: Path, readable: bool):
    """A symlink, a FIFO, or a file over the reader's cap is refused rather
    than followed or truncated — the same rejection
    jasper.atomic_io.read_regular_bytes_nofollow makes."""
    target = tmp_path / "target.env"
    target.write_text('JASPER_SPEAKER_NAME="Forged"\n', encoding="utf-8")
    cases = [tmp_path / "link.env", tmp_path / "fifo.env", tmp_path / "big.env"]
    cases[0].symlink_to(target)
    os.mkfifo(cases[1])
    cases[2].write_bytes(b'JASPER_SPEAKER_NAME="Forged"\n' + b"#" * (64 * 1024))
    if readable:
        # The same inode shapes, minus the one property under test, must still
        # resolve — otherwise "refused" could be an unconditional JTS.
        cases = [target]

    for state in cases:
        proc = _compose_fact(
            'printf %s "$(jasper_usbgadget_speaker_name)"',
            {"JASPER_SPEAKER_NAME_FILE": str(state)},
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == ("Forged" if readable else "JTS")


def test_bring_up_stops_probing_the_resolver_once_the_reconciler_has_spoken(
    tmp_path: Path,
):
    """jasper-usbgadget-wanted and jasper-usbgadget-up each answer the same
    hardware question, once per bring-up. The reconciler's marker answers both
    without an interpreter; an absent marker still falls back to the resolver,
    because reading absence as `unavailable` would strand a deploy riding
    ncm.usb0."""
    calls = tmp_path / "probe.log"
    probe = tmp_path / "hardware-probe"
    _write_python_shim(
        probe,
        "import pathlib\n"
        f"pathlib.Path({str(calls)!r}).open('a').write('probed\\n')\n",
    )
    marker = tmp_path / "management-transport.ok"

    def _bring_up(run: Path) -> tuple[int, Path]:
        before = len(calls.read_text().splitlines()) if calls.exists() else 0
        for script in (WANTED, UP):
            proc, cfg = _run(
                script, run, network="enabled", audio_intent=FALSE,
                hardware_allowed=str(probe),
                env_extra={"JASPER_USBGADGET_MANAGEMENT_TRANSPORT_MARKER": str(marker)},
            )
            assert proc.returncode == 0, proc.stderr
        return len(calls.read_text().splitlines()) - before, cfg

    spawned, cfg = _bring_up(tmp_path / "fallback")
    assert (spawned, _linked(cfg, "ncm.usb0")) == (2, True)

    marker.touch()
    spawned, cfg = _bring_up(tmp_path / "marker")
    assert (spawned, _linked(cfg, "ncm.usb0")) == (0, True)


# A minimal blob shaped like usb_f_uac2.ko's .rodata: the four AudioStreaming
# alt-setting strings uac2_name_patch.py's real patcher rewrites, surrounded
# by neighbour strings and alignment slack -- mirrors _fake_module() in
# tests/test_uac2_name_patch.py.
_FAKE_STOCK_MODULE = (
    b"\x7fELF\x00\x00\x00\x00"
    b"Topology Control\x00"
    b"Playback Inactive\x00"
    b"\x00\x00\x00"
    b"Playback Active\x00"
    b"\x00"
    b"Capture Inactive\x00"
    b"Capture Active\x00"
    b"Playback Volume\x00"
)


def _write_python_shim(path: Path, body: str) -> None:
    """A fake PATH executable, in Python to sidestep shell-quoting entirely.
    Used for depmod/lsmod/stat, none of which exist (or behave GNU-style) on
    a macOS dev sandbox, and none of which a hermetic test should invoke for
    real even on Linux (depmod/lsmod would read the actual running kernel).

    The shebang names this interpreter by absolute path, never `env python3`,
    so a shim dir may also carry a counting `python3` without the shims
    recursing through it."""
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _fake_modules_tree(
    tmp_path: Path,
    *,
    stock_body: bytes = _FAKE_STOCK_MODULE,
    indexed: bool = True,
    dep_style: str = "relative",
    preexisting_override: bool = False,
) -> dict[str, Path]:
    """A temp /lib/modules/<kver> tree for the name-patch script.

    ``indexed`` picks which usb_f_uac2.ko modules.dep names. That file -- not
    the presence of a .ko under updates/ -- is what request_module() follows
    when gadget-up creates the uac2 function, so it decides whether publishing
    the override is enough for the very next bring-up to load it.

    ``dep_style`` writes the entry in either dialect depmod may emit. The
    script must recognise both: reading an indexed override as un-indexed is
    only a wasted rebuild, but it would also mean the completion marker is
    never promoted and doctor warns forever."""
    kver = os.uname().release
    modules_root = tmp_path / "modules"
    moddir = modules_root / kver
    kernel_dir = moddir / "kernel" / "drivers" / "usb" / "gadget"
    kernel_dir.mkdir(parents=True)
    (kernel_dir / "usb_f_uac2.ko").write_bytes(stock_body)

    resolved = (
        "updates/usb_f_uac2.ko"
        if indexed
        else "kernel/drivers/usb/gadget/usb_f_uac2.ko"
    )
    assert dep_style in ("relative", "absolute"), dep_style
    if dep_style == "absolute":
        resolved = f"{moddir}/{resolved}"
    (moddir / "modules.dep").write_text(
        f"{resolved}: kernel/drivers/usb/gadget/u_audio.ko\n", encoding="utf-8"
    )

    updates = moddir / "updates"
    if preexisting_override:
        updates.mkdir(parents=True, exist_ok=True)
        (updates / "usb_f_uac2.ko").write_bytes(b"\x7fELF stale override")
    return {
        "modules_root": modules_root,
        "stock": kernel_dir / "usb_f_uac2.ko",
        "override": updates / "usb_f_uac2.ko",
        "marker": updates / ".jasper-usbsink-name.marker",
        "pending": updates / ".jasper-usbsink-name.pending",
    }


def _name_patch_env(
    tmp_path: Path,
    modules_root: Path,
    *,
    depmod_sleep: float = 0.0,
    depmod_rc: int = 0,
    inflate_override_size: bool = False,
) -> dict:
    """PATH shims + env for a hermetic name-patch run.

    Returns the env plus the sentinels the assertions read: whether the depmod
    stand-in was ever executed, what the script asked systemctl to do, and how
    many interpreters it started."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    depmod_ran = tmp_path / "depmod-ran"
    systemctl_log = tmp_path / "systemctl.log"
    interpreters = tmp_path / "interpreters.log"

    for interpreter in ("python", "python3"):
        _write_python_shim(
            bin_dir / interpreter,
            "import pathlib, sys\n"
            f"with pathlib.Path({str(interpreters)!r}).open('a') as fh:\n"
            "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n",
        )

    _write_python_shim(
        bin_dir / "depmod",
        "import pathlib, sys, time\n"
        # Sentinel written BEFORE the sleep: a detached background invocation
        # (`depmod ... &`) returns control to the caller without waiting for
        # this process to exit, so a check ordered the other way around could
        # race a caller that already moved on. Writing first makes "the
        # sentinel is absent" true immediately on invocation, independent of
        # scheduling, backgrounding, or how long the sleep below takes.
        f"pathlib.Path({str(depmod_ran)!r}).write_text(sys.argv[1])\n"
        f"time.sleep({depmod_sleep!r})\n"
        f"sys.exit({depmod_rc!r})\n",
    )
    _write_python_shim(
        bin_dir / "systemctl",
        "import pathlib, sys\n"
        f"with pathlib.Path({str(systemctl_log)!r}).open('a') as fh:\n"
        "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n",
    )
    _write_python_shim(bin_dir / "lsmod", "print('Module Size Used by')\n")
    _write_python_shim(
        bin_dir / "stat",
        # Minimal GNU "stat -c%s FILE" shim -- BSD/macOS stat has no -c.
        "import pathlib, sys\n"
        "if len(sys.argv) >= 3 and sys.argv[1] == '-c%s':\n"
        "    size = pathlib.Path(sys.argv[2]).stat().st_size\n"
        f"    if {inflate_override_size!r} and sys.argv[2].endswith('.override.ko'):\n"
        "        size += 1\n"
        "    print(size)\n"
        "else:\n"
        "    sys.exit(1)\n",
    )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "JASPER_MODULES_ROOT": str(modules_root),
        "JASPER_SPEAKER_NAME_FILE": str(tmp_path / "no-such-name.env"),
    }
    return {
        "env": env,
        "depmod_ran": depmod_ran,
        "systemctl_log": systemctl_log,
        "interpreters": interpreters,
    }


# #2176 depmod stand-in cost. The depmod shim in _name_patch_env writes its
# sentinel file before this sleep runs, so nothing about the #2176 guard's
# correctness depends on how long the stand-in takes -- this duration exists
# only to make a regression that reintroduces depmod on the publish path
# obvious in CI logs (the run visibly hangs), not to prove anything by
# itself.
_SLOW_DEPMOD_SEC = 4.0


@pytest.mark.parametrize(
    "case, stock_body, inflate, expect_event",
    [
        ("success", _FAKE_STOCK_MODULE, False, "queued"),
        ("patch_failed", b"\x7fELF nothing patchable in here", False, "patch_failed"),
        ("override_invalid", _FAKE_STOCK_MODULE, True, "override_invalid"),
    ],
)
def test_publish_phase_never_runs_depmod_on_the_gadget_start_path(
    tmp_path: Path,
    case: str,
    stock_body: bytes,
    inflate: bool,
    expect_event: str,
) -> None:
    """#2176: depmod alone measures 10.3 s on a Pi Zero 2 W, over 2x
    jasper-usbgadget.service's 5 s TimeoutStartSec. The publish phase -- which
    IS that unit's ExecStartPre -- must therefore never invoke depmod on any
    of its three exit paths; it hands the rebuild to
    jasper-usbsink-name-index.service instead.

    All three paths are pinned because the script header states the invariant
    for all three. The depmod stand-in writes its sentinel file as the FIRST
    thing it does, before it sleeps -- so "the sentinel is absent after the
    process exits" is a complete, timing-independent proof that depmod was
    never reached, on any host under any load. (A prior version of this test
    additionally asserted a wall-clock bound on the whole subprocess as a
    second, "independent" check of the same invariant; it was strictly
    weaker -- a contended CI host can blow a fixed wall-clock bound on
    ordinary subprocess/scheduling overhead having nothing to do with
    depmod, which is exactly what made #2443 flaky -- so it added false
    failures without adding coverage and was dropped.)"""
    tree = _fake_modules_tree(
        tmp_path, stock_body=stock_body, indexed=True, preexisting_override=True
    )
    shims = _name_patch_env(
        tmp_path,
        tree["modules_root"],
        depmod_sleep=_SLOW_DEPMOD_SEC,
        inflate_override_size=inflate,
    )

    proc = subprocess.run(
        [str(NAME_PATCH)],
        env=shims["env"],
        capture_output=True,
        text=True,
        # Hang guard only. If a regression reintroduces a synchronous depmod
        # call, the sentinel assertion below fails the instant the stand-in
        # returns; this generous bound only protects the suite from a
        # genuinely wedged process, and is far too loose to itself flake.
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert f"event=usbsink_name.{expect_event}" in proc.stderr, proc.stderr
    assert not shims["depmod_ran"].exists(), (
        f"the {case} path ran depmod inside jasper-usbgadget's 5s "
        "TimeoutStartSec -- #2176 regression"
    )
    # The slow half must actually be handed off, not silently dropped.
    assert shims["systemctl_log"].exists(), (
        f"the {case} path never queued the index rebuild"
    )
    assert (
        "--no-block start jasper-usbsink-name-index.service"
        in shims["systemctl_log"].read_text()
    )


def test_publish_withholds_the_marker_when_the_override_is_not_indexed(
    tmp_path: Path,
) -> None:
    """Honesty guard. modprobe follows modules.dep, not the filesystem. When
    updates/ is NOT yet in the index (first enable, first boot after a kernel
    update) gadget-up autoloads the STOCK module for this session, so arming
    the completion marker would make jasper-doctor report `ok` while the host
    shows 'Playback Inactive'. Publish must decline to promise convergence in
    that case, leaving doctor on its honest `warn`."""
    tree = _fake_modules_tree(tmp_path, indexed=False)
    shims = _name_patch_env(
        tmp_path, tree["modules_root"], depmod_sleep=_SLOW_DEPMOD_SEC
    )

    proc = subprocess.run(
        [str(NAME_PATCH)], env=shims["env"], capture_output=True, text=True, timeout=60
    )

    assert proc.returncode == 0, proc.stderr
    assert "loads=stock_until_restart" in proc.stderr, proc.stderr
    assert tree["override"].exists(), "the override is still published"
    assert not tree["pending"].exists(), (
        "publish armed a completion marker for a bring-up that will load the "
        "stock module -- doctor would report a label the host is not showing"
    )
    assert not tree["marker"].exists()


@pytest.mark.parametrize("dep_style", ["relative", "absolute"])
def test_publish_arms_the_marker_when_the_override_is_already_indexed(
    tmp_path: Path, dep_style: str,
) -> None:
    """The rename case: modules.dep already names updates/usb_f_uac2.ko, so
    the gadget-up milliseconds after this ExecStartPre loads exactly the bytes
    just published. Convergence is genuine, so the marker is armed.

    Run over both modules.dep path dialects: this repo has no Linux host to
    confirm which one the Pi's depmod emits, so the script recognises both
    rather than asserting one."""
    tree = _fake_modules_tree(tmp_path, indexed=True, dep_style=dep_style)
    shims = _name_patch_env(
        tmp_path, tree["modules_root"], depmod_sleep=_SLOW_DEPMOD_SEC
    )

    proc = subprocess.run(
        [str(NAME_PATCH)], env=shims["env"], capture_output=True, text=True, timeout=60
    )

    assert proc.returncode == 0, proc.stderr
    assert "loads=override" in proc.stderr, proc.stderr
    assert tree["pending"].exists()
    fields = tree["pending"].read_text().split("\t")
    assert fields[0] == "3"                       # patch schema
    assert fields[1] == os.uname().release
    assert fields[2] == "JTS"                     # no name file -> default
    assert fields[3] == "JTS Mic"
    # Not yet the real marker: only the index phase may promote it.
    assert not tree["marker"].exists()


def test_steady_state_publish_reads_the_name_and_then_does_nothing(
    tmp_path: Path,
) -> None:
    """The unchanged boot: an override whose marker already describes the
    desired kernel/name/stock triple costs no rebuild and no index kick, and
    the name in that marker is read here, which pins that the shell reader is
    what fills it in.

    "No interpreter" is asserted the only way this script can prove it: the
    patcher it would otherwise run is invoked by absolute path, so the counting
    shim on PATH cannot see it — an untouched override plus an unkicked index
    unit is what says it never ran."""
    tree = _fake_modules_tree(tmp_path, indexed=True, preexisting_override=True)
    shims = _name_patch_env(tmp_path, tree["modules_root"])
    name_file = tmp_path / "speaker_name.env"
    name_file.write_text('JASPER_SPEAKER_NAME="Kitchen Two"\n', encoding="utf-8")
    shims["env"]["JASPER_SPEAKER_NAME_FILE"] = str(name_file)
    stock_hash = hashlib.sha256(tree["stock"].read_bytes()).hexdigest()
    tree["marker"].write_text(
        "\t".join(
            ("3", os.uname().release, "Kitchen Two", "Kitchen Two Mic", stock_hash)
        ),
        encoding="utf-8",
    )
    before = tree["override"].read_bytes()

    proc = subprocess.run(
        [str(NAME_PATCH)], env=shims["env"], capture_output=True, text=True, timeout=60
    )

    assert proc.returncode == 0, proc.stderr
    assert "event=usbsink_name.steady_state" in proc.stderr
    assert tree["override"].read_bytes() == before
    assert not tree["pending"].exists()
    assert not shims["depmod_ran"].exists()
    assert not shims["systemctl_log"].exists()
    assert not shims["interpreters"].exists()


def test_index_phase_promotes_the_pending_marker_after_depmod_succeeds(
    tmp_path: Path,
) -> None:
    """The deferred half converges on its own: depmod, then an atomic rename
    of the armed marker. Splitting publish/index is what lets a boot that
    starts a rebuild finish it instead of repeating it forever (#2176)."""
    tree = _fake_modules_tree(tmp_path, indexed=True, preexisting_override=True)
    tree["pending"].write_text("3\tsome-kver\tKitchen\tKitchen Mic\tdeadbeef")
    shims = _name_patch_env(tmp_path, tree["modules_root"])

    proc = subprocess.run(
        [str(NAME_PATCH), "--index"],
        env=shims["env"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert "event=usbsink_name.applied" in proc.stderr, proc.stderr
    assert shims["depmod_ran"].read_text() == os.uname().release
    assert tree["marker"].read_text() == "3\tsome-kver\tKitchen\tKitchen Mic\tdeadbeef"
    assert not tree["pending"].exists(), "pending marker was promoted, not copied"


def test_index_phase_withholds_the_marker_when_depmod_fails(tmp_path: Path) -> None:
    """A failed depmod leaves the override unreachable by modprobe, so the
    marker must stay absent: doctor keeps warning and the next gadget start
    rebuilds. The pre-fix script swallowed depmod's exit status entirely."""
    tree = _fake_modules_tree(tmp_path, indexed=True, preexisting_override=True)
    tree["pending"].write_text("3\tsome-kver\tKitchen\tKitchen Mic\tdeadbeef")
    shims = _name_patch_env(tmp_path, tree["modules_root"], depmod_rc=1)

    proc = subprocess.run(
        [str(NAME_PATCH), "--index"],
        env=shims["env"],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    assert "event=usbsink_name.index_failed" in proc.stderr, proc.stderr
    assert not tree["marker"].exists()
    assert tree["pending"].exists(), "the armed marker survives for a later pass"


def test_failure_path_reindexes_a_dangling_index_with_no_override_left(
    tmp_path: Path,
) -> None:
    """Self-heal: when modules.dep still names updates/usb_f_uac2.ko but a
    previous pass already deleted that file, modprobe fails and gadget-up
    (set -euo pipefail) takes the USB management network down with it. A later
    failed pass must re-index to repair that, even with no override on disk."""
    tree = _fake_modules_tree(
        tmp_path,
        stock_body=b"\x7fELF nothing patchable in here",
        indexed=True,
        preexisting_override=False,
    )
    shims = _name_patch_env(
        tmp_path, tree["modules_root"], depmod_sleep=_SLOW_DEPMOD_SEC
    )

    proc = subprocess.run(
        [str(NAME_PATCH)], env=shims["env"], capture_output=True, text=True, timeout=60
    )

    assert proc.returncode == 0, proc.stderr
    assert "event=usbsink_name.patch_failed" in proc.stderr, proc.stderr
    assert (
        "--no-block start jasper-usbsink-name-index.service"
        in shims["systemctl_log"].read_text()
    )


def test_up_canonical_off_dominates_stale_enabled_mirror(tmp_path):
    """Derived enablement cannot authorize UAC2 after the household turns it Off."""
    proc, cfg = _run(
        UP,
        tmp_path,
        network="enabled",
        audio_intent=FALSE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "network=1 audio=0" in proc.stderr
    assert "audio_reason=intent_disabled_or_parked" in proc.stderr
    assert _linked(cfg, "ncm.usb0")
    assert not _linked(cfg, "uac2.usb0")


def test_wanted_canonical_off_dominates_stale_enabled_mirror(tmp_path):
    proc, _ = _run(
        WANTED,
        tmp_path,
        network="enabled",
        audio_intent=FALSE,
    )
    assert proc.returncode == 0, proc.stderr
    assert "network=1 audio=0" in proc.stderr
    assert "audio_reason=intent_disabled_or_parked" in proc.stderr


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
    state = _poll_status(run_dir, lambda s: s.get("sample_count", 0) >= 10)
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


# ---------- forensics repair action (the forced-rebuild consumer refresh) ---


def _poll_status(run_dir: Path, predicate, deadline_sec: float = 3.0) -> dict:
    deadline = time.monotonic() + deadline_sec
    state: dict = {}
    while time.monotonic() < deadline:
        try:
            state = json.loads((run_dir / "status.json").read_text())
        except (OSError, json.JSONDecodeError):
            pass
        if predicate(state):
            break
        time.sleep(0.02)
    return state


def _run_repair_request(
    tmp_path: Path, *, restart_rc: int, tears_down: bool = False,
) -> tuple[dict, list[str], str]:
    """Drive `watch` mode through one repair request against a fake systemctl
    (the converger's own JASPER_USBGADGET_SYSTEMCTL seam), returning the final
    status snapshot, the shim's recorded argv lines in call order, and the
    watcher's stderr.

    ``tears_down=True`` models an ExecCondition-skipped start: `restart`
    still exits 0 (the STOP half ran; the START half's ExecCondition decided
    nothing is wanted), but ConfigFS ends up with no bound descriptor --
    exactly the false-success shape a repair must not read as a rebuild."""
    _proc, incident_dir = _run_snapshot(tmp_path)
    for artifact in incident_dir.glob("usb-gadget-*.txt"):
        artifact.unlink()
    enabled = tmp_path / "forensics.env"
    enabled.write_text("JASPER_USB_GADGET_FORENSICS=1\n")
    run_dir = tmp_path / "forensics-run"
    configfs = tmp_path / "configfs"
    shim, log = _systemctl_shim(
        tmp_path, restart_rc=restart_rc,
        tears_down=configfs if tears_down else None,
    )
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
        "JASPER_USBGADGET_SYSTEMCTL": str(shim),
    })
    watcher = subprocess.Popen(
        ["bash", str(SNAPSHOT), "watch"], cwd=ROOT, env=env,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    _poll_status(run_dir, lambda s: s.get("sample_count", 0) >= 1)
    (run_dir / "request.repair").touch()
    state = _poll_status(
        run_dir, lambda s: s.get("last_action") in {"repair", "repair_failed"},
    )
    enabled.unlink()
    _stdout, stderr = watcher.communicate(timeout=3)

    assert watcher.returncode == 0, stderr
    calls = log.read_text().splitlines() if log.exists() else []
    return state, calls, stderr


@pytest.mark.skipif(not _HAS_USR_BIN_TIMEOUT, reason=_NO_USR_BIN_TIMEOUT_REASON)
def test_repair_action_refreshes_fanin_then_usbmic_in_order(tmp_path):
    """The /system repair button's forced rebuild bypasses the converger the
    same way the speaker-rename path does, so it owes fan-in/usbmic the same
    ordered post-rebuild refresh (see jasper/web/speaker_setup.py)."""
    state, calls, stderr = _run_repair_request(tmp_path, restart_rc=0)

    assert state.get("last_action") == "repair"
    assert calls == [
        "restart jasper-usbgadget.service",
        "try-restart jasper-fanin.service",
        "try-restart jasper-usbmic.service",
    ]
    assert "event=usb_gadget.forensics action=repair_refresh" in stderr
    assert "order=fanin,usbmic fanin=ok usbmic=ok" in stderr


@pytest.mark.skipif(not _HAS_USR_BIN_TIMEOUT, reason=_NO_USR_BIN_TIMEOUT_REASON)
def test_repair_action_detects_a_false_success_and_skips_the_refresh(tmp_path):
    """rc=0 from `systemctl restart` also fires when the unit's own
    ExecCondition skipped the start half (nothing wanted) -- the exact
    false-success shape jasper-usbgadget-converge itself guards against by
    reading ConfigFS back rather than trusting the exit code. A repair that
    trusted rc=0 alone would refresh fan-in/usbmic for a rebuild that never
    happened."""
    state, calls, stderr = _run_repair_request(tmp_path, restart_rc=0, tears_down=True)

    assert state.get("last_action") == "repair_failed"
    assert calls == ["restart jasper-usbgadget.service"]
    assert "action=repair_refresh" not in stderr


@pytest.mark.skipif(not _HAS_USR_BIN_TIMEOUT, reason=_NO_USR_BIN_TIMEOUT_REASON)
def test_repair_action_skips_refresh_when_rebuild_fails(tmp_path):
    """Fail-closed, mirroring jasper-usbgadget-converge: a rebuild that did
    not happen must not be followed by a fan-in/usbmic refresh, and the
    refresh must never mask the rebuild's own repair_failed result."""
    state, calls, stderr = _run_repair_request(tmp_path, restart_rc=1)

    assert state.get("last_action") == "repair_failed"
    assert calls == ["restart jasper-usbgadget.service"]
    assert "action=repair_refresh" not in stderr

# No test pins REPAIR_RESTART_TIMEOUT_SEC's numeric value: that would be
# asserting on source text (AGENTS.md), and a real timed-out-restart run
# costs ~34s of wall time to exercise honestly. The floor is stated instead,
# beside the constant in deploy/usbsink/jasper-usbgadget-snapshot.
