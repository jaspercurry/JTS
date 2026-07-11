#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# make-camilla-ring-config.sh — build the HAND CamillaDSP config variant
# for the SHM ring prototype. Two modes:
#
#   default (Ring B): swap ONLY devices.playback.device -> jts_ring_playback
#     (CamillaDSP writes the ring, outputd reads). Output S16LE is implicit —
#     the playback device string is the only edit.
#
#   --ring-a (Ring A): swap ONLY devices.capture -> {type: Alsa, device:
#     jts_ring_capture, format: S16_LE} (fan-in writes the ring, CamillaDSP
#     reads). The capture format is pinned to S16_LE (the SHM ring's wire
#     format — fan-in is S16 native, no widening) which is a delta from the
#     box's current S32 dsnoop capture; CamillaDSP floats internally so there
#     is no downstream effect. The device name + format are the SSOT shared
#     with jasper.fanin_coupling.capture_kwargs_for_coupling("shm_ring").
#
# This NEVER touches the product emitters (jasper/camilla_emit.py,
# jasper/active_speaker/camilla_yaml.py, etc.) or any packaged config
# under /etc/camilladsp/ — it reads whatever config the live statefile
# currently points at, copies it byte-for-byte apart from the one device
# entry, writes the copy to a prototype-only path, and validates it
# with CamillaDSP's own --check (plus the JTS volume_limit safety-
# ceiling check) before telling the caller it's safe to load.
#
# It does NOT point the statefile at the new config and does NOT
# restart CamillaDSP — arm.sh does that, after this script's --check
# gate has passed, and only after the resolvability probe + bench
# writer test have already proven the ring end works.
#
# Run this ON THE PI as the jasper user (or via sudo) — it needs to read
# /var/lib/camilladsp/outputd-statefile.yml and write under
# /var/lib/camilladsp/, and it shells out to the *installed*
# /opt/jasper/.venv/bin/python so it reuses the exact statefile-parsing
# and --check safety logic the product ships (jasper.active_speaker
# .environment.parse_camilla_statefile_config_path,
# jasper.dsp_apply.validate_camilla_config) rather than re-implementing
# YAML/CLI parsing here.
#
# Usage (on the Pi):
#   sudo bash scripts/ring-proto/make-camilla-ring-config.sh [--ring-a|--ring-b]
#   (this is what arm.sh calls; the "Python" above is an inline heredoc
#   embedded in this .sh file, invoked over SSH — there is no separate
#   .py file. See the embedded Python's module docstring below for the
#   exact fields it writes.)
#
# Output config: /var/lib/camilladsp/ring_proto.yml (mode 0644, same
# ownership convention as every other CamillaDSP config under
# /var/lib/camilladsp/).
#
# Exit codes:
#   0  — wrote /var/lib/camilladsp/ring_proto.yml and it passed --check
#   1  — usage / precondition error (see stderr)
#   2  — the copied config, with only the device swapped, FAILED
#        CamillaDSP's own --check or the JTS volume_limit ceiling check.
#        This is a hard stop: arm.sh must not proceed past this script
#        on exit 2. Nothing on disk changes in a way that affects the
#        live chain — this script never touches the statefile.

set -euo pipefail

# Captured BEFORE sourcing _lib.sh — see _guard.sh for why this matters.
# When arm.sh invokes this script internally, PI_HOST is already exported
# by arm.sh's own validated resolution, so the guard passes silently
# (correct — the target was already checked once); a standalone
# `bash make-camilla-ring-config.sh` with no PI_HOST set still refuses.
export JASPER_RING_PROTO_CALLER_PI_HOST="${PI_HOST:-}"

# RING_PROTO_DIR, not SCRIPT_DIR: _lib.sh (sourced below) defines its own
# SCRIPT_DIR pointing at scripts/, and sourcing clobbers a same-named
# variable in this shell scope — see the full explanation in arm.sh.
RING_PROTO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${RING_PROTO_DIR}/../.." && pwd)"
# shellcheck source=../_lib.sh
. "${REPO_ROOT}/scripts/_lib.sh"
# shellcheck source=./_guard.sh
. "${RING_PROTO_DIR}/_guard.sh"
require_explicit_ring_proto_target

# Mode: Ring B (playback swap, default) or Ring A (capture swap, --ring-a).
RING_MODE="ring_b"
for arg in "$@"; do
    case "${arg}" in
        --ring-a) RING_MODE="ring_a" ;;
        --ring-b) RING_MODE="ring_b" ;;
        *)
            echo "usage: $(basename "$0") [--ring-a|--ring-b]" >&2
            exit 1
            ;;
    esac
done

if [[ "${RING_MODE}" == "ring_a" ]]; then
    # Ring A capture-swap: the SSOT capture device + format are duplicated here
    # from jasper.fanin_coupling (RING_CAPTURE_DEVICE / RING_WIRE_FORMAT) because
    # this bash runs the config surgery on the Pi before importing Python; the
    # embedded Python below asserts they match capture_kwargs_for_coupling so a
    # drift fails loud.
    # CamillaDSP capture format uses the underscore form (the live config
    # captures S32_LE), matching jasper.fanin_coupling.RING_WIRE_FORMAT="S16_LE".
    RING_DEVICE="${JASPER_RING_PROTO_ALSA_DEVICE:-jts_ring_capture}"
    RING_CAPTURE_FORMAT="${JASPER_RING_PROTO_CAPTURE_FORMAT:-S16_LE}"
    OUT_CONFIG_REMOTE="${JASPER_RING_PROTO_CAMILLA_CONFIG:-/var/lib/camilladsp/ring_proto_a.yml}"
else
    RING_DEVICE="${JASPER_RING_PROTO_ALSA_DEVICE:-jts_ring_playback}"
    RING_CAPTURE_FORMAT=""
    OUT_CONFIG_REMOTE="${JASPER_RING_PROTO_CAMILLA_CONFIG:-/var/lib/camilladsp/ring_proto.yml}"
fi

if [[ "${RING_MODE}" == "ring_a" ]]; then
    echo "=== Ring A prototype: build hand Camilla config on ${PI_USER}@${PI_HOST} ==="
    echo "Capture ring device:  ${RING_DEVICE} (format ${RING_CAPTURE_FORMAT})"
else
    echo "=== Ring B prototype: build hand Camilla config on ${PI_USER}@${PI_HOST} ==="
    echo "Playback ring device: ${RING_DEVICE}"
fi
echo "Output config path:    ${OUT_CONFIG_REMOTE}"

if ! ssh -o BatchMode=yes -o ConnectTimeout=8 "${PI_USER}@${PI_HOST}" true; then
    echo "error: cannot reach ${PI_USER}@${PI_HOST} over SSH (BatchMode)" >&2
    exit 1
fi

# The embedded Python does the actual work on the Pi, reusing the
# product's own statefile-parsing + --check safety functions instead of
# re-implementing YAML surgery or CLI-flag handling in bash. It is
# read-only against the live statefile (never writes it) and only
# writes the new prototype config path.
remote_exit=0
ssh -o BatchMode=yes "${PI_USER}@${PI_HOST}" sudo \
    JASPER_RING_PROTO_MODE="${RING_MODE}" \
    JASPER_RING_PROTO_ALSA_DEVICE="${RING_DEVICE}" \
    JASPER_RING_PROTO_CAPTURE_FORMAT="${RING_CAPTURE_FORMAT}" \
    JASPER_RING_PROTO_CAMILLA_CONFIG="${OUT_CONFIG_REMOTE}" \
    /opt/jasper/.venv/bin/python <<'PY' || remote_exit=$?
import os
import sys

from jasper.active_speaker.environment import (
    DEFAULT_CAMILLA_STATEFILE,
    parse_camilla_statefile_config_path,
)
from jasper.dsp_apply import ValidationStatus, validate_camilla_config

try:
    import yaml
except ImportError as e:
    print(f"error: PyYAML not importable in /opt/jasper/.venv: {e}", file=sys.stderr)
    sys.exit(1)

ring_mode = os.environ.get("JASPER_RING_PROTO_MODE", "ring_b")
ring_device = os.environ["JASPER_RING_PROTO_ALSA_DEVICE"]
ring_capture_format = os.environ.get("JASPER_RING_PROTO_CAPTURE_FORMAT", "")
out_path = os.environ["JASPER_RING_PROTO_CAMILLA_CONFIG"]
statefile_path = str(DEFAULT_CAMILLA_STATEFILE)

# Ring A SSOT cross-check: the capture device + format the bash chose MUST match
# jasper.fanin_coupling.capture_kwargs_for_coupling("shm_ring") so the hand
# config, the Rust writer, and Python never drift. Fail loud on a mismatch.
if ring_mode == "ring_a":
    from jasper.fanin_coupling import COUPLING_SHM_RING, capture_kwargs_for_coupling

    kw = capture_kwargs_for_coupling(COUPLING_SHM_RING)
    want_device = kw.get("capture_device")
    want_format = kw.get("capture_format")
    if ring_device != want_device or ring_capture_format != want_format:
        print(
            "error: Ring A capture SSOT drift — the script chose "
            f"device={ring_device!r} format={ring_capture_format!r} but "
            "jasper.fanin_coupling.capture_kwargs_for_coupling('shm_ring') says "
            f"device={want_device!r} format={want_format!r}. Reconcile the two "
            "(the fanin_coupling constants are canonical).",
            file=sys.stderr,
        )
        sys.exit(1)

try:
    statefile_text = open(statefile_path, encoding="utf-8").read()
except OSError as e:
    print(f"error: cannot read statefile {statefile_path}: {e}", file=sys.stderr)
    sys.exit(1)

source_config_path = parse_camilla_statefile_config_path(statefile_text)
if not source_config_path:
    print(
        f"error: statefile {statefile_path} has no config_path — cannot "
        "determine which config to copy",
        file=sys.stderr,
    )
    sys.exit(1)

print(f"source config (currently live): {source_config_path}")

try:
    source_text = open(source_config_path, encoding="utf-8").read()
except OSError as e:
    print(f"error: cannot read source config {source_config_path}: {e}", file=sys.stderr)
    sys.exit(1)

try:
    doc = yaml.safe_load(source_text)
except yaml.YAMLError as e:
    print(f"error: source config is not valid YAML: {e}", file=sys.stderr)
    sys.exit(1)

if not isinstance(doc, dict) or "devices" not in doc:
    print("error: source config has no top-level 'devices' block", file=sys.stderr)
    sys.exit(1)

devices = doc["devices"]
if not isinstance(devices, dict):
    print("error: source config's 'devices' is not a mapping", file=sys.stderr)
    sys.exit(1)

if ring_mode == "ring_a":
    # RING A capture-swap. THE ONLY EDIT: point devices.capture at the ring
    # ioplug as an Alsa device with the pinned S16_LE format. Everything else —
    # samplerate, chunksize, target_level, the playback block, every
    # filter/mixer/pipeline entry, volume_limit — is preserved byte-for-byte.
    if "capture" not in devices:
        print("error: source config has no 'devices.capture' block", file=sys.stderr)
        sys.exit(1)
    capture = devices["capture"]
    if not isinstance(capture, dict):
        print("error: 'devices.capture' is not a mapping", file=sys.stderr)
        sys.exit(1)
    original_type = capture.get("type")
    original_device = capture.get("device")
    original_format = capture.get("format")
    # Rewrite to the ring capture device. Preserve the channels the config
    # already declares (2 for the solo stereo path); set type/device/format.
    capture["type"] = "Alsa"
    capture["device"] = ring_device
    capture["format"] = ring_capture_format
    print(
        "swapped devices.capture: "
        f"type {original_type!r}->'Alsa' device {original_device!r}->{ring_device!r} "
        f"format {original_format!r}->{ring_capture_format!r}"
    )
else:
    # RING B playback-swap. THE ONLY EDIT: swap the playback device string.
    # Everything else — samplerate, chunksize, target_level, capture device,
    # every filter/mixer/pipeline entry, volume_limit — is preserved byte-for-
    # byte via round-tripping the parsed structure. This does not add or remove a
    # 'filename' key, so jasper-camilla-pipe-guard's playback-filename probe
    # still sees an empty playback_filename and logs solo_config, exactly as it
    # does for the live Alsa config today.
    if "playback" not in devices:
        print("error: source config has no 'devices.playback' block", file=sys.stderr)
        sys.exit(1)
    playback = devices["playback"]
    if not isinstance(playback, dict):
        print("error: 'devices.playback' is not a mapping", file=sys.stderr)
        sys.exit(1)
    original_device = playback.get("device")
    original_type = playback.get("type")
    if original_type != "Alsa":
        print(
            f"error: source config's playback.type is {original_type!r}, not "
            "'Alsa' — this script only swaps an Alsa device string. A File-sink "
            "config (bonded/transport-pipe topology) is out of scope for this "
            "prototype and would need a different arm procedure.",
            file=sys.stderr,
        )
        sys.exit(1)
    playback["device"] = ring_device
    print(f"swapped devices.playback.device: {original_device!r} -> {ring_device!r}")

new_text = yaml.safe_dump(doc, sort_keys=False)

tmp_path = out_path + ".tmp"
with open(tmp_path, "w", encoding="utf-8") as f:
    f.write(new_text)
os.chmod(tmp_path, 0o644)
os.replace(tmp_path, out_path)
print(f"wrote: {out_path}")

result = validate_camilla_config(out_path)
print(f"validation status: {result.status.value}")
if result.stdout_tail:
    print(f"  camilladsp --check stdout (tail): {result.stdout_tail}")
if result.stderr_tail:
    print(f"  camilladsp --check stderr (tail): {result.stderr_tail}")

if result.status != ValidationStatus.VALID:
    print(
        f"error: {out_path} FAILED validation ({result.status.value}) — "
        f"{result.error or 'see stdout/stderr above'}. Not safe to load.",
        file=sys.stderr,
    )
    # Remove the invalid config so a stray file can't be mistaken for a
    # validated one on a later run.
    try:
        os.remove(out_path)
    except OSError:
        pass
    sys.exit(2)

print(f"OK: {out_path} passed camilladsp --check + the JTS volume_limit ceiling")
PY

if [[ "${remote_exit}" -eq 2 ]]; then
    echo "error: the ring config FAILED CamillaDSP validation on ${PI_HOST} — see output above. Not proceeding." >&2
    exit 2
elif [[ "${remote_exit}" -ne 0 ]]; then
    echo "error: building the ring config failed on ${PI_HOST} (exit ${remote_exit}) — see output above." >&2
    exit 1
fi

echo ""
echo "=== Ring config ready: ${OUT_CONFIG_REMOTE} on ${PI_HOST} ==="
echo "Not yet loaded — the live statefile is untouched. arm.sh points the"
echo "statefile at this config and restarts jasper-camilla only after the"
echo "earlier arm steps (asound snippet, resolvability probe, bench writer)"
echo "have already succeeded."
