#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# make-camilla-ring-config.sh — build the HAND CamillaDSP config variant
# for the Ring B prototype: an exact copy of the box's currently-loaded
# config with ONLY devices.playback.device swapped to jts_ring_playback.
#
# This NEVER touches the product emitters (jasper/camilla_emit.py,
# jasper/active_speaker/camilla_yaml.py, etc.) or any packaged config
# under /etc/camilladsp/ — it reads whatever config the live statefile
# currently points at, copies it byte-for-byte apart from one device
# string, writes the copy to a prototype-only path, and validates it
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
#   sudo /opt/jasper/.venv/bin/python \
#     scripts/ring-proto/make-camilla-ring-config.py --help
#   (this .sh wrapper is what arm.sh calls; see the module docstring in
#   the embedded Python below for the exact fields it writes)
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

RING_DEVICE="${JASPER_RING_PROTO_ALSA_DEVICE:-jts_ring_playback}"
OUT_CONFIG_REMOTE="${JASPER_RING_PROTO_CAMILLA_CONFIG:-/var/lib/camilladsp/ring_proto.yml}"

echo "=== Ring B prototype: build hand Camilla config on ${PI_USER}@${PI_HOST} ==="
echo "Ring ALSA device name: ${RING_DEVICE}"
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
    JASPER_RING_PROTO_ALSA_DEVICE="${RING_DEVICE}" \
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

ring_device = os.environ["JASPER_RING_PROTO_ALSA_DEVICE"]
out_path = os.environ["JASPER_RING_PROTO_CAMILLA_CONFIG"]
statefile_path = str(DEFAULT_CAMILLA_STATEFILE)

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
if not isinstance(devices, dict) or "playback" not in devices:
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

# THE ONLY EDIT: swap the playback device string. Everything else in the
# document — samplerate, chunksize, target_level, capture device, every
# filter/mixer/pipeline entry, volume_limit — is preserved byte-for-byte
# via round-tripping the parsed structure. This does not add or remove a
# 'filename' key, so jasper-camilla-pipe-guard's playback-filename probe
# still sees an empty playback_filename and logs solo_config, exactly as
# it does for the live Alsa config today.
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
