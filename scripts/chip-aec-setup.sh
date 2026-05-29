#!/usr/bin/env bash
# Phase 1 setup for the chip-aec-experiment.
# See docs/CHIP-AEC-EXPERIMENT.md for the full plan and revert procedure.
#
# What this does, in order:
#   1. Pre-flight: read-only checks that the topology assumptions still
#      hold (chip on 6-ch firmware, plug:jasper_capture resolves, daemon
#      module imports, SHF_BYPASS=1 baseline, bridge active, all 4 units
#      exist). Fails loudly BEFORE any destructive op if drift found.
#   2. Rsyncs experimental code to the Pi (SKIP_INSTALL=1, so install.sh
#      doesn't re-trigger the reconciler chain).
#   3. Stops + masks the production AEC service chain — all four units:
#      jasper-aec-{bridge,reconcile,init,dongle-recover}. Masking only
#      the bridge is insufficient; see the comment block above the mask
#      loop for why.
#   4. Snapshots /etc/jasper/jasper.env and comments out
#      JASPER_MIC_DEVICE_RAW + JASPER_MIC_DEVICE_DTLN (the dual/triple-
#      stream wake telemetry env vars). Teardown restores from backup.
#   5. Writes a UNIX-timestamp sentinel under /var/lib/jasper/wake-events/
#      so teardown can print a SQL one-liner labelling experiment-window
#      wake events.
#   6. Sets chip params: SHF_BYPASS=0, AUDIO_MGR_SYS_DELAY=12.
#   7. Starts the experiment daemon (reference feeder + UDP mic pump).
#   8. Restarts jasper-voice to clear any prior state.
#
# jasper-voice continues reading udp://127.0.0.1:9876 — same input
# contract as production with the WebRTC bridge. No voice-daemon
# changes are made.
#
# Revert: bash scripts/chip-aec-teardown.sh (unmasks the four units,
# restores chip params + amixer, restores jasper.env, prints SQL for
# wake-event labelling).

set -euo pipefail

PI_HOST="${PI_HOST:-${JASPER_HOSTNAME:-jts.local}}"
PI_USER="${PI_USER:-pi}"
REF_DELAY_MS="${REF_DELAY_MS:-${JASPER_CHIP_AEC_REF_DELAY_MS:-0}}"
MIC_CHANNEL="${MIC_CHANNEL:-${JASPER_CHIP_AEC_MIC_CHANNEL:-0}}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== chip-aec-experiment: Phase 1 setup ==="
echo "Reference delay: ${REF_DELAY_MS} ms"
echo "Mic channel: ${MIC_CHANNEL}"
echo

# Pre-flight runs first, before the rsync, because a failure here
# means we should NOT touch the Pi at all. Every check is read-only;
# nothing destructive happens until "Stopping + masking the production
# AEC service chain" below.
#
# Tripwire philosophy: this experiment is shelved infrastructure that
# may sit unused for months. When someone revives it, the most likely
# failure mode is "the topology drifted under it." Each check
# corresponds to an assumption the rest of the script makes; if any
# of them is wrong, the rest of the script will leave the Pi in a
# confusing half-broken state. Fail loudly here instead.
preflight_check() {
  echo "==> Pre-flight: verifying topology assumptions"
  ssh "${PI_USER}@${PI_HOST}" '
    set -eu
    fail() { echo "  FAIL: $1"; echo; echo "Aborting before any destructive operation. Fix the issue above, then re-run."; exit 1; }
    pass() { echo "  ok: $1"; }

    # 1. ALSA can see the chip.
    if [[ ! -d /proc/asound/Array ]]; then
      fail "XVF3800 not present as ALSA card \"Array\" (is the chip plugged in? lsusb)"
    fi
    pass "XVF3800 enumerated as ALSA card Array"

    # 2. Chip on 6-channel firmware. The experiment captures the full
    # endpoint during baseline checks and then emits one selected channel
    # to UDP. The 2-ch firmware lacks the raw channels we use for delay
    # sanity checks.
    ch=$(awk "/^Capture:/{c=1} c && /Channels:/{print \$2; exit}" /proc/asound/Array/stream0 2>/dev/null || echo "")
    if [[ "$ch" != "6" ]]; then
      fail "Chip on ${ch}-channel firmware, but experiment needs 6-channel (ua-io16-6ch-sqr v2.0.8 or newer). See BRINGUP.md Phase 2A.5 for DFU flash."
    fi
    pass "chip on 6-channel firmware"

    # 3. plug:jasper_capture resolves as the pi user. PR #223 moved
    # asoundrc to /etc/asound.conf at mode 0644 specifically so non-
    # root processes can resolve user-space PCM names. The capture-
    # comparison script needs this; verify it works before relying
    # on it.
    if ! timeout 3 arecord -D plug:jasper_capture --dump-hw-params -d 1 /dev/null > /dev/null 2>&1; then
      fail "plug:jasper_capture does not resolve as pi user. Is /etc/asound.conf in place? (Run: sudo bash deploy/install.sh on a regular branch.)"
    fi
    pass "plug:jasper_capture resolves"

    # 4. Daemon module imports from the runtime venv. The experiment
    # invokes the daemon as `python -m jasper.chip_aec_experiment`
    # using /opt/jasper/.venv/bin/python — which only sees the
    # source after install.sh has run (this setup script uses
    # SKIP_INSTALL=1 to avoid re-triggering the reconciler, so the
    # daemon file is NOT updated by this script). First-time setup
    # requires a regular deploy of THIS branch first.
    if ! sudo /opt/jasper/.venv/bin/python -c "import jasper.chip_aec_experiment" 2>/dev/null; then
      fail "jasper.chip_aec_experiment module does not import from /opt/jasper/.venv. First-time setup on this Pi? Run a full deploy of this branch first (without SKIP_INSTALL): bash scripts/deploy-to-pi.sh"
    fi
    pass "jasper.chip_aec_experiment module imports"

    # 5. SHF_BYPASS currently 1 (production baseline). If it is
    # already 0, either a previous chip-aec experiment didn'\''t
    # teardown cleanly, or something else is mutating chip state.
    # Either way we should not silently overwrite — operator
    # investigates first.
    shf=$(sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS 2>/dev/null | grep -oE "\[[0-9]+\]" | tr -d "[]" || echo "")
    if [[ "$shf" != "1" ]]; then
      fail "SHF_BYPASS=${shf}, expected 1 (production baseline). Chip is in unexpected state — either a prior teardown failed or something else is mutating params. Investigate manually before re-running."
    fi
    pass "SHF_BYPASS=1 (production baseline)"

    # 6. jasper-aec-bridge currently active. Otherwise we are not
    # masking anything real — either AEC mode is disabled or the
    # bridge has crashed, both of which the experiment'\''s setup
    # script does not handle.
    if ! systemctl is-active jasper-aec-bridge > /dev/null 2>&1; then
      fail "jasper-aec-bridge.service is not active. Check: systemctl status jasper-aec-bridge. Either JASPER_AEC_MODE=disabled, or the bridge crashed."
    fi
    pass "jasper-aec-bridge is active"

    # 7. All four units exist as systemd units. If any have been
    # renamed in main, the mask block below would log noise but
    # silently fail to mask the right thing.
    for unit in jasper-aec-bridge jasper-aec-reconcile jasper-aec-init jasper-dongle-recover; do
      if ! systemctl list-unit-files "${unit}.service" --no-legend 2>/dev/null | grep -q "^${unit}\.service"; then
        fail "${unit}.service not found as a systemd unit. Has it been renamed or removed? Check deploy/systemd/ on main."
      fi
    done
    pass "all four service units present (bridge, reconcile, init, dongle-recover)"

    echo
    echo "  pre-flight passed; safe to proceed."
  '
}

preflight_check
echo

echo "==> Rsync (SKIP_INSTALL=1, no reconciler re-eval)"
SKIP_INSTALL=1 PI_HOST="$PI_HOST" PI_USER="$PI_USER" bash "${SCRIPT_DIR}/deploy-to-pi.sh"

# From here onward, every step mutates Pi state (masks services,
# changes chip params, edits jasper.env, starts the daemon). If any
# step fails midway, the Pi is left in a half-broken state that
# requires manual intervention. Arm a trap that invokes the teardown
# script to recover. The trap is intentionally NOT armed during
# pre-flight or rsync — those are non-destructive, and triggering
# teardown for a pre-flight failure would needlessly restart
# services on an unmodified Pi.
auto_teardown_on_failure() {
  local exit_code=$?
  # Disable the trap so a failure inside teardown doesn't recurse.
  trap '' ERR
  echo
  echo "!!! chip-aec-setup failed (exit ${exit_code}) — attempting auto-teardown to restore production state"
  echo
  if bash "${SCRIPT_DIR}/chip-aec-teardown.sh"; then
    echo
    echo "  auto-teardown completed; Pi should be back on the WebRTC bridge."
    echo "  re-run chip-aec-setup.sh after fixing the underlying issue."
  else
    echo
    echo "!!! auto-teardown ALSO failed — manual recovery required."
    echo "    SSH in and run:"
    echo "      sudo systemctl unmask jasper-aec-bridge jasper-aec-reconcile jasper-aec-init jasper-dongle-recover"
    echo "      sudo systemctl start jasper-aec-reconcile.service"
    echo "      sudo /opt/jasper/.venv/bin/jasper-doctor"
  fi
  exit "${exit_code}"
}
trap auto_teardown_on_failure ERR

echo
echo "==> Stopping + masking the production AEC service chain"
# We mask FOUR units, not one. Masking only the bridge leaves three
# silent paths that can re-flip SHF_BYPASS=1 mid-experiment:
#   - jasper-aec-reconcile.service: ANY controlC* hotplug fires the
#     udev rule deploy/udev/99-jasper-aec-reconcile.rules, which
#     calls enable_start_aec() → restart jasper-aec-init.service
#     (which unconditionally writes SHF_BYPASS=1 per
#     jasper/cli/aec_init.py).
#   - jasper-aec-init.service: even if reconcile is masked, an
#     install.sh run or manual `systemctl start jasper-aec-init`
#     re-writes SHF_BYPASS=1 + AEC_HPFONOFF + amixer.
#   - jasper-dongle-recover.service: dongle replug chains into
#     `systemctl start jasper-aec-reconcile.service` and bypasses
#     a reconciler-only mask.
ssh "${PI_USER}@${PI_HOST}" 'set -e
for unit in jasper-aec-bridge jasper-aec-reconcile jasper-aec-init jasper-dongle-recover; do
  sudo systemctl stop "${unit}.service" 2>/dev/null || true
  sudo systemctl mask --runtime "${unit}.service"
  echo "  ${unit} runtime-masked"
done'

echo
echo "==> Snapshotting voice-input env for clean dual/triple-stream behaviour"
# JASPER_MIC_DEVICE_RAW (port 9877) and JASPER_MIC_DEVICE_DTLN (port
# 9878) are default-off, but if the user has them set in
# /etc/jasper/jasper.env, jasper-voice will bind those ports and
# silently starve on them (the experiment only feeds 9876). The
# OFF/DTLN wake legs never fire and every wake-event row gets a
# misleading score_off=none. Comment them out for the experiment
# duration; teardown restores from the backup.
ssh "${PI_USER}@${PI_HOST}" 'set -e
ENV=/etc/jasper/jasper.env
BAK=/etc/jasper/jasper.env.chip-aec.bak
if [[ -f "$ENV" ]] && sudo grep -qE "^(JASPER_MIC_DEVICE_RAW|JASPER_MIC_DEVICE_DTLN)=" "$ENV"; then
  if [[ ! -f "$BAK" ]]; then
    sudo cp -a "$ENV" "$BAK"
    echo "  backed up $ENV → $BAK"
  fi
  sudo sed -i -E "s/^(JASPER_MIC_DEVICE_RAW|JASPER_MIC_DEVICE_DTLN)=/# chip-aec.bak: \1=/" "$ENV"
  echo "  commented dual/triple-stream env vars"
else
  echo "  no dual/triple-stream env to snapshot (clean single-stream config)"
fi'

echo
echo "==> Marking wake-events timestamp for post-hoc corpus filtering"
# WakeEventStore (jasper/wake_events.py) writes every wake to
# /var/lib/jasper/wake-events/wake-events.sqlite3 + WAVs into a 1 GB
# ring. There is no env knob to disable. During the experiment the
# corpus will mix chip-AEC fires with the production WebRTC-AEC
# baseline. We write a UNIX-timestamp sentinel so the teardown can
# print a SQL one-liner that labels (or deletes) experiment-window
# events. Production code never reads this file — purely a marker.
ssh "${PI_USER}@${PI_HOST}" 'sudo mkdir -p /var/lib/jasper/wake-events
sudo bash -c "date +%s > /var/lib/jasper/wake-events/.chip-aec-experiment-start.ts"
echo "  start ts: $(sudo cat /var/lib/jasper/wake-events/.chip-aec-experiment-start.ts)"'

echo
echo "==> Setting chip params (SHF_BYPASS=0, AUDIO_MGR_SYS_DELAY=12)"
ssh "${PI_USER}@${PI_HOST}" 'set -e
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS --values 0 > /dev/null
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AUDIO_MGR_SYS_DELAY --values 12 > /dev/null
echo "  current values:"
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host SHF_BYPASS | grep "SHF_BYPASS:"
sudo /opt/jasper/.venv/bin/python -m jasper.xvf.xvf_host AUDIO_MGR_SYS_DELAY | grep "AUDIO_MGR_SYS_DELAY:"'

echo
echo "==> Killing any prior experiment daemon"
ssh "${PI_USER}@${PI_HOST}" 'sudo pkill -f "[j]asper.chip_aec_experiment" 2>/dev/null || true; sleep 0.5'

echo
echo "==> Starting experiment daemon"
ssh "${PI_USER}@${PI_HOST}" "REF_DELAY_MS='${REF_DELAY_MS}' MIC_CHANNEL='${MIC_CHANNEL}' bash -s" <<'REMOTE'
sudo bash -c "nohup /opt/jasper/.venv/bin/python -m jasper.chip_aec_experiment --ref-delay-ms \"${REF_DELAY_MS}\" --mic-channel \"${MIC_CHANNEL}\" > /var/log/chip-aec-experiment.log 2>&1 < /dev/null &"
sleep 2
if pgrep -f '[j]asper.chip_aec_experiment' > /dev/null; then
  echo "  daemon started (PID $(pgrep -f '[j]asper.chip_aec_experiment'))"
else
  echo '  daemon FAILED to start. Last 30 lines of log:'
  sudo tail -30 /var/log/chip-aec-experiment.log
  exit 1
fi
REMOTE

echo
echo "==> Restarting jasper-voice to clear any prior state"
ssh "${PI_USER}@${PI_HOST}" 'sudo systemctl restart jasper-voice
sleep 2
systemctl is-active jasper-voice'

echo
echo "=== setup complete ==="
echo
echo "Next steps:"
echo "  1. Start music playing through the speaker at production volume"
echo "  2. Wait 30 s for chip AEC to converge"
echo "  3. Verify: bash scripts/chip-aec-poll-convergence.sh"
echo "  4. Capture ear-test recordings: bash scripts/chip-aec-capture-comparison.sh"
echo
echo "Live monitoring:"
echo "  ssh ${PI_USER}@${PI_HOST} 'sudo tail -f /var/log/chip-aec-experiment.log'"
echo "  ssh ${PI_USER}@${PI_HOST} 'sudo journalctl -u jasper-voice -f'"
echo
echo "Revert: bash scripts/chip-aec-teardown.sh"
