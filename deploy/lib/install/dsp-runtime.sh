#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# CamillaDSP and jasper-outputd runtime install steps for deploy/install.sh.

_restart_outputd_for_readiness() {
    # Clear earlier transient attempts before spending this install's one
    # readiness restart. A failed reset must not risk the reboot ladder (#5267).
    if ! systemctl reset-failed jasper-outputd.service 2>/dev/null; then
        echo "  ERROR: could not clear jasper-outputd's start-rate state; refusing the readiness restart" >&2
        return 1
    fi
    systemctl restart jasper-outputd.service
}

require_outputd_ready() {
    if [[ ! -x /opt/jasper/bin/jasper-outputd ]]; then
        echo "  ERROR: /opt/jasper/bin/jasper-outputd is missing or not executable" >&2
        return 1
    fi
    _restart_outputd_for_readiness || return 1
    systemctl is-active --quiet jasper-outputd.service || {
        echo "  ERROR: jasper-outputd.service did not become active" >&2
        journalctl -u jasper-outputd.service -n 40 --no-pager >&2 || true
        return 1
    }
    python3 - <<'PY'
import json
import socket
import sys
import time

path = "/run/jasper-outputd/control.sock"
deadline = time.monotonic() + 3.0
last_error = None
while time.monotonic() < deadline:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(path)
            sock.sendall(b"STATUS\n")
            body = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                body += chunk
        data = json.loads(body.decode("utf-8", errors="replace"))
        if data.get("backend") != "alsa":
            raise RuntimeError(f"backend={data.get('backend')!r}, expected 'alsa'")
        sink_mode = data.get("sink_mode") or "single_alsa"
        expected_dac = (
            "dual_apple_usb_c_dac_4ch"
            if sink_mode == "dual_apple"
            else "outputd_dac"
        )
        if data.get("dac", {}).get("pcm") != expected_dac:
            raise RuntimeError(
                f"dac.pcm={data.get('dac', {}).get('pcm')!r}, expected {expected_dac!r}"
            )
        # No content-PCM expectation: under the ring outputd opens none, and
        # jasper-doctor's check_outputd_service owns the ring-side rule.
        sys.exit(0)
    except Exception as e:
        last_error = e
        time.sleep(0.1)
print(f"jasper-outputd STATUS probe failed: {last_error}", file=sys.stderr)
sys.exit(1)
PY
}

install_camilladsp() {
    install -d -m 0755 "${CAMILLA_DIR}" "${CAMILLA_CONF}"
    # State + emitted-correction-config dirs. outputd uses
    # outputd-statefile.yml so corrections survive Pi restarts. The
    # room-correction wizard writes correction_<id>_<unixtime>.yml
    # under configs/.
    install -d -m 0755 /var/lib/camilladsp
    # configs/ is written atomically (temp file in-dir + rename) by the non-root
    # jasper-web user for active-speaker staging and
    # room-correction configs, so it must be group-writable from its FIRST
    # creation — not only after the later widen step below. A deploy that stops
    # between here and that widen (or a future reorder) must not leave it
    # root-only, or non-root staging fails with PermissionError.
    # check_camilla_configs_writable pins this at runtime.
    install -d -m 2775 -g jasper /var/lib/camilladsp/configs
    ensure_state_dir
    # Shared correction/test artifacts are written by the correction web flow and
    # by jasper-web's /sound/ measurement arms. Keep the tree group-writable for
    # the dropped service users instead of root-only.
    #
    # The active_speaker* paths below are the same capture/sweep trees
    # /sound/ and the measurement daemon share; this list must stay in sync with
    # heal_shared_state_modes's allowlist (state-and-secrets.sh), which re-heals
    # the same six paths on every deploy for boxes that pre-date this line.
    install -d -m 2770 -g jasper \
        /var/lib/jasper/correction \
        /var/lib/jasper/correction/calibration_mics \
        /var/lib/jasper/correction/tones \
        /var/lib/jasper/active_speaker \
        /var/lib/jasper/active_speaker/campaigns \
        /var/lib/jasper/active_speaker/sessions \
        /var/lib/jasper/active_speaker_captures \
        /var/lib/jasper/active_speaker_sweeps \
        /var/lib/jasper/active_speaker_stimuli

    if [[ ! -x "${CAMILLA_DIR}/camilladsp" ]]; then
        local tmpdir
        tmpdir="$(mktemp -d)"
        echo "Fetching CamillaDSP ${CAMILLA_VERSION}..."
        # Bounded retries + transfer cap: same rationale as
        # fetch_verified_source_archive (multi-MB fetch on flaky WiFi).
        curl -fsSL --retry 3 --retry-connrefused --max-time 300 \
            -o "${tmpdir}/${CAMILLA_TARBALL}" "${CAMILLA_URL}"
        echo "${CAMILLA_SHA256}  ${tmpdir}/${CAMILLA_TARBALL}" | sha256sum -c -
        tar -xzf "${tmpdir}/${CAMILLA_TARBALL}" -C "${CAMILLA_DIR}" camilladsp
        chmod +x "${CAMILLA_DIR}/camilladsp"
        rm -rf "${tmpdir}"
        echo "Installed CamillaDSP to ${CAMILLA_DIR}/camilladsp"
    fi

    # The flat outputd startup graph is copied here as a fallback/template,
    # then regenerated after the Python package is installed and the current
    # output hardware state has been observed so the active DAC's latency
    # floor reaches fresh first boot. install_alsa() handles the dongle name
    # in /etc/asound.conf.
    install -m 0644 \
        "${REPO_DIR}/deploy/camilladsp/outputd-cutover.yml" \
        "${CAMILLA_CONF}/outputd-cutover.yml"

    # The outputd topology uses a separate Camilla statefile instead of
    # overwriting /var/lib/camilladsp/statefile.yml. Do not repair that
    # statefile here: the safe target depends on the saved output topology.
    # This flat graph maps full-range stereo directly to DAC outputs. It is
    # selectable only for an explicit passive mono/stereo layout; unconfigured,
    # incomplete, and any topology with a tweeter/protected role park instead.
    # After the Python package is installed,
    # ensure_outputd_camilla_statefile asks jasper.active_speaker's runtime
    # contract which graph is legal and fails closed if no protected graph
    # exists.
}

run_captured_command() {
    # run_captured_command <output-variable> <command...>
    # Capture combined stdout/stderr, always replay it, and preserve the
    # command's success/failure for install steps that need the output again.
    local output_variable="$1"
    shift
    local command_output
    if ! command_output="$("$@" 2>&1)"; then
        printf -v "${output_variable}" '%s' "${command_output}"
        printf '%s\n' "${command_output}"
        return 1
    fi
    printf -v "${output_variable}" '%s' "${command_output}"
    printf '%s\n' "${command_output}"
}

ensure_output_hardware_state() {
    # The CamillaDSP latency floor resolver reads the same output-hardware
    # state file the reconciler owns. A fresh install must write it before the
    # flat startup graph is generated or the generator falls back to the
    # conservative global 1024/2048 default.
    local output
    echo "  Writing output hardware state before Camilla statefile seed"
    if ! run_captured_command output env \
        JASPER_OUTPUT_HARDWARE_STATE_PATH=/run/jasper-output-hardware/output_hardware.json \
        JASPER_APLAY="${JASPER_APLAY:-aplay}" \
        /opt/jasper/.venv/bin/python -m jasper.cli.output_hardware --write; then
        return 1
    fi
}

_render_outputd_cutover_configs() {
    # `jasper-sound render-flat-cutover` wraps
    # jasper.sound.camilla_yaml.render_flat_cutover_configs — the ONE writer of
    # this file. The root reconciler (jasper-audio-hardware-reconcile) and
    # jasper-output-topology-reset call the same command, so the graph a box
    # boots cannot depend on which writer ran last. An inline heredoc here is
    # exactly how a second spelling gets born.
    /opt/jasper/.venv/bin/jasper-sound render-flat-cutover
}

render_outputd_cutover_config() {
    # Design call for #27: generate the seeded flat startup config through the
    # production outputd graph, not a bypass or a hand-edited static YAML. The
    # active-speaker runtime contract below still decides whether flat is legal
    # for the saved topology.
    #
    # The active DAC profile's Camilla floor is deliberately NOT applied to this
    # graph: both its halves are the SHM ring, and the ioplug pins the ring's
    # period bytes min==max, so a profile floor would fail the open rather than
    # raise it. The floor still reaches every emit whose playback is an ordinary
    # ALSA device.
    local output
    echo "  Rendering outputd flat startup config (ring geometry)"
    if ! run_captured_command output _render_outputd_cutover_configs; then
        return 1
    fi
}

ensure_outputd_camilla_statefile() {
    # Runtime graph selection belongs to jasper.active_speaker, not install.sh.
    # This flat graph maps full-range stereo directly to DAC outputs. It is
    # selectable only for an explicit passive mono/stereo layout; unconfigured,
    # incomplete, and any topology with a tweeter/protected role park instead.
    local output
    echo "  Checking outputd Camilla statefile against active-speaker runtime contract"
    if ! run_captured_command output install_run_bounded 60 -- \
        /opt/jasper/.venv/bin/jasper-active-speaker runtime-safe-graph \
        --statefile /var/lib/camilladsp/outputd-statefile.yml \
        --flat-config "${CAMILLA_CONF}/outputd-cutover.yml" \
        --write-statefile; then
        return 1
    fi
    if [[ "${JASPER_RESTART_CAMILLA_ON_STATEFILE_REPAIR:-0}" == "1" ]] \
       && [[ "${output}" == *"statefile written: yes"* ]]; then
        echo "  Restarting jasper-camilla.service after statefile repair"
        systemctl restart jasper-camilla.service 2>/dev/null || \
            echo "  WARN: jasper-camilla restart failed after statefile repair. Check logs with: journalctl -u jasper-camilla -e"
    fi
}

reconcile_sound_dsp_state() {
    # Generated CamillaDSP YAML is a cache of saved JTS sound intent. After a
    # deploy changes DSP render semantics, refresh only JTS-owned/re-renderable
    # graphs through the normal sound apply transaction. Fail open: the safety
    # statefile guard above has already ensured the current graph is legal.
    local output
    if [[ ! -x /opt/jasper/.venv/bin/jasper-sound ]]; then
        echo "  WARN: jasper-sound CLI missing; skipping sound DSP reconcile"
        return 0
    fi
    echo "  Reconciling current sound DSP graph"
    local status
    set +e
    output="$(install_run_bounded 30 -- /opt/jasper/.venv/bin/jasper-sound reconcile-current-dsp --fail-open 2>&1)"
    status=$?
    set -e
    if (( status != 0 )); then
        printf '%s\n' "${output}"
        if (( status == 124 || status == 137 )); then
            echo "  WARN: sound DSP reconcile timed out after 30s; leaving current legal graph in place"
        else
            echo "  WARN: sound DSP reconcile command failed; leaving current legal graph in place"
        fi
        return 0
    fi
    printf '%s\n' "${output}"
}

ensure_crossover_camilla_statefile() {
    # Seed camilla#2's OWN statefile (crossover-statefile.yml) so the
    # endpoint-crossover instance (jasper-camilla-crossover.service)
    # has a config to load on first start (the unit has no positional
    # config — same CamillaDSP-v4 statefile-clobber reason as camilla#1).
    #
    # Reuses the SAME active-speaker runtime contract as
    # ensure_outputd_camilla_statefile (jasper-active-speaker
    # runtime-safe-graph), which on a roleful/protected topology — the ONLY
    # topology where camilla#2 is meaningful — selects the DRIVER-DOMAIN
    # (Layer-A-intact) baseline / all-muted active startup graph and NEVER
    # the flat fallback (the contract's `select_flat` branch is gated by
    # `topology_allows_flat_dac_graph`; see
    # jasper/active_speaker/output_contract.py). So an active box gets a
    # tweeter-safe driver-domain seed.
    #
    # PARKED DEFAULT (issue #2135): a roleful box that has staged no startup
    # graph yet seeds the PARKED graph here instead — a File sink to /dev/null
    # with every output hard muted. That seed is benign: camilla#2 is INERT
    # until the grouping reconciler arms it, and `seed_crossover_statefile`
    # (jasper/multiroom/active_leader_config.py, called from the reconciler's
    # active-leader bake arm) repoints this statefile at the re-proven
    # driver-domain config immediately before enabling the unit. If camilla#2
    # ever DID start on the parked pointer it would emit silence, where the
    # flat pointer would send full range to a tweeter.
    #
    # We never restart the unit (it is not enabled), so there is no
    # JASPER_RESTART_* knob here — only the seed write.
    local output
    echo "  Seeding camilla#2 crossover statefile via active-speaker runtime contract"
    if ! run_captured_command output install_run_bounded 60 -- \
        /opt/jasper/.venv/bin/jasper-active-speaker runtime-safe-graph \
        --statefile /var/lib/camilladsp/crossover-statefile.yml \
        --flat-config "${CAMILLA_CONF}/outputd-cutover.yml" \
        --write-statefile; then
        return 1
    fi
}

install_camillagui() {
    # CamillaGUI — official web UI for CamillaDSP. Connects to the same
    # ws://127.0.0.1:1234 control socket the Python daemon already uses,
    # exposes a SPA for live config editing, signal levels, and config-
    # file management. We use the prebuilt PyInstaller bundle from the
    # upstream release rather than a venv/source install — bundle is
    # self-contained (Python 3.12 + frontend assets baked in), no apt
    # deps, no pip resolution. Loopback-only since #2319
    # (deploy/systemd/camillagui.socket binds 127.0.0.1:5005) — unlike
    # the other unauthenticated, home-LAN-only management surfaces, this
    # one can author and live-apply CamillaDSP configs naming any device,
    # so it is not LAN-reachable. The landing page has no link to it (a
    # link that always connection-refuses is a silent failure); reach it
    # with `ssh -L 5005:localhost:5005 <pi-host>`.
    local CAMILLAGUI_VERSION="4.1.0"
    local CAMILLAGUI_DIR="/opt/camillagui"
    local arch bundle bundle_sha256
    arch=$(uname -m)
    case "${arch}" in
        aarch64)
            bundle="bundle_linux_aarch64.tar.gz"
            bundle_sha256="9a5415b44dda58478f18de9fd572edf092f659fd5e45cbe8086ff5648dc089d7"
            ;;
        *)
            echo "  WARNING: no CamillaGUI bundle for ${arch} — skipping"
            return 0
            ;;
    esac

    if [[ -x "${CAMILLAGUI_DIR}/camillagui_backend/camillagui_backend" ]]; then
        echo "  CamillaGUI already at ${CAMILLAGUI_DIR}"
    else
        echo "  Downloading CamillaGUI ${CAMILLAGUI_VERSION} (${arch})..."
        local tmpdir
        tmpdir=$(mktemp -d)
        local url="https://github.com/HEnquist/camillagui-backend/releases/download/v${CAMILLAGUI_VERSION}/${bundle}"
        if ! curl -fsSL --retry 3 --retry-connrefused --max-time 300 \
                -o "${tmpdir}/cg.tar.gz" "${url}"; then
            echo "  WARNING: CamillaGUI download failed — skipping"
            rm -rf "${tmpdir}"
            return 0
        fi
        if ! echo "${bundle_sha256}  ${tmpdir}/cg.tar.gz" | sha256sum -c -; then
            echo "  WARNING: CamillaGUI checksum mismatch — skipping" >&2
            rm -rf "${tmpdir}"
            return 0
        fi
        install -d -m 0755 "${CAMILLAGUI_DIR}"
        tar -xzf "${tmpdir}/cg.tar.gz" -C "${CAMILLAGUI_DIR}"
        rm -rf "${tmpdir}"
        echo "  Installed CamillaGUI to ${CAMILLAGUI_DIR}"
    fi

    # Config + state dirs. /etc/camilladsp/coeffs holds FIR-filter
    # coefficient files the GUI writes when convolving; we create it
    # so the GUI's first save doesn't fail with ENOENT.
    install -d -m 0755 /etc/camillagui /etc/camilladsp/coeffs /var/lib/camillagui
    install -m 0644 \
        "${REPO_DIR}/deploy/camillagui/config.yml" \
        /etc/camillagui/config.yml
    touch /var/log/camillagui.log
    chmod 0644 /var/log/camillagui.log

    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/camillagui.service" \
        "${SYSTEMD_DIR}/camillagui.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/camillagui-proxy.service" \
        "${SYSTEMD_DIR}/camillagui-proxy.service"
    install -m 0644 \
        "${REPO_DIR}/deploy/systemd/camillagui.socket" \
        "${SYSTEMD_DIR}/camillagui.socket"

    # Stop a running backend (the proxy's Requires= stops the proxy with it)
    # so the next request starts the backend just installed.
    systemctl stop camillagui.service 2>/dev/null || true

    systemctl daemon-reload
    systemctl enable camillagui.socket
    # Restart (not just start/enable --now) so a ListenStream= change on
    # upgrade actually takes effect: a bare `start` is a no-op when the socket
    # is already active from a prior install and would silently leave the old
    # bind live until the next reboot. Not swallowed with `|| true` like the
    # wizard-socket loop's restart — a failed rebind here leaves a
    # security-relevant posture unchanged and should abort the install loudly
    # rather than continue past it silently.
    systemctl restart camillagui.socket
    echo "  CamillaGUI listening on 127.0.0.1:5005 via socket-activated proxy"
    echo "  (backend exits 10 min after last access; ~50 MB Pss reclaimed)"
}
