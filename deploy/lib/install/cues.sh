#!/usr/bin/env bash

# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

# Audio cue regeneration for deploy/install.sh.

regenerate_audio_cues() {
    # Bake the speaker's audible-failure cues so they're ready before
    # the daemon ever needs them. With no voice provider configured yet,
    # jasper-cues falls back to a provider-free chime instead of failing
    # (AGENTS.md non-negotiable 6, issue #4814), so this succeeds even on
    # a genuinely fresh box. The daemon retries on every startup
    # regardless, so a real failure here is still non-fatal — we just
    # warn and continue.
    if [[ ! -x /opt/jasper/.venv/bin/jasper-cues ]]; then
        echo "  (jasper-cues not on PATH yet — will run on first daemon boot)"
        return 0
    fi
    echo "  Regenerating audio cues..."
    # jasper-cues auto-loads /etc/jasper/jasper.env then
    # /var/lib/jasper/voice_provider.env (web-wizard overrides) via
    # jasper.platform.env_load — same precedence as the daemon's systemd unit.
    # We deliberately do NOT pre-source jasper.env here: doing so puts
    # those vars into the shell's environment first, where load_env_files's
    # setdefault preserves them and the wizard file can't override.
    if ! install_run_bounded 300 -- /opt/jasper/.venv/bin/jasper-cues regenerate; then
        echo "  WARNING: cue regenerate failed unexpectedly. " \
             "Daemon will retry at startup. To force a refresh later: " \
             "sudo systemctl restart jasper-voice"
    fi
}
