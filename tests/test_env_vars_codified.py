"""Guard: every JASPER_* env var read in jasper/ must be codified.

AGENTS.md ("Codify, don't memorise"): if a runtime value matters, it
must be set somewhere the next fresh Pi picks up automatically — a
documented line in .env.example, an install.sh step, a wizard /
reconciler that writes a /var/lib/jasper/*.env file, or a systemd
unit. An env var that exists only as an `os.environ.get` in code is a
hidden runtime dependency: an operator can set it live on one Pi and
the next rebuild silently behaves differently, and nobody browsing the
config surfaces can discover the knob exists.

This guard extracts every JASPER_* name read in jasper/**/*.py and
requires each to appear on at least one codification surface:

  * .env.example — assignment or prose comment (the template doubles
    as the documentation page per the house rule).
  * deploy/**     — install.sh migrations/seeds, systemd units,
                    reconciler scripts in deploy/bin/.
  * scripts/**    — checked-in operator tooling that sets the var.
  * jasper/web/** and jasper/control/** — the wizard / control-server
    writers of the /var/lib/jasper/*.env files.

Known coarseness: the surfaces are token scans, so a var that is read
*only* inside jasper/web or jasper/control self-passes, and a prose
mention counts as codification. That is deliberate — the guard's job
is to catch knobs with NO discoverable surface at all, and a token
scan is the durable shape (it survives refactors of how each surface
spells its writes).

_UNCODIFIED is the explicit allowlist and it is two-sided: an entry
that gains a codification surface (or stops being read) fails, so the
list can only shrink. Adding to it is a deliberate decision that the
var is internal (a test seam, a state-file path override, an
experiment knob) — say which, in the comment.
"""
from __future__ import annotations

import re
from pathlib import Path

from tests.test_voice_provider_ssot_reader import code_only

ROOT = Path(__file__).resolve().parents[1]

# An env *read* of a JASPER_ name: os.environ access, the config
# module's _env* helpers, or a Mapping read (`env.get(...)` — the
# transit-pattern plugins receive os.environ as a plain Mapping).
_READ_RE = re.compile(
    r"(?:os\.environ\.get\(|os\.getenv\(|os\.environ\[|_env[a-z_]*\(|\benv\.get\()"
    r"\s*['\"](JASPER_[A-Z0-9_]+)['\"]"
)

_TOKEN_RE = re.compile(r"JASPER_[A-Z0-9_]+")

# Codification surfaces, scanned as raw token text.
_SURFACES = (".env.example", "deploy", "scripts", "jasper/web", "jasper/control")

# Vars read in jasper/ with no codification surface, accepted as
# internal-only. Grouped by why. Surfaced when this guard first ran
# (2026-06-10); JASPER_AEC_BINDING and JASPER_FLIGHT_RECORDER were
# real orphans and got .env.example prose in the same change.
_UNCODIFIED = {
    # -- State-file / artifact / DB / binary *path* overrides. Defaults
    #    are the canonical /var/lib/jasper (or /run, /usr) locations;
    #    the override exists for tests and ad-hoc diagnostics, never
    #    operator config.
    "JASPER_ACTIVE_SPEAKER_SAFE_PLAYBACK_STATE",
    "JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_DIR",
    "JASPER_ACTIVE_SPEAKER_TONE_ARTIFACT_RETENTION",
    "JASPER_AEC_BRIDGE_STATS_PATH",
    "JASPER_AUDIO_VALIDATION_ARTIFACT",
    "JASPER_AUDIO_VALIDATION_DIR",
    "JASPER_BUILD_MANIFEST",
    "JASPER_CAMILLADSP_BIN",
    "JASPER_CORRECTION_ROOT",
    "JASPER_CORRECTION_SESSIONS_DIR",
    "JASPER_DSP_APPLY_STATE_PATH",
    "JASPER_DTLN_MODEL_DIR",
    "JASPER_MIC_MUTE_STATE_PATH",
    "JASPER_MUX_MODE_STATE_PATH",
    "JASPER_PEERING_UDS",
    "JASPER_SOUNDS_DIR",
    "JASPER_SOUND_SETTINGS_PATH",
    "JASPER_SYSTEM_ENV_FILE",
    "JASPER_TIMER_DB",
    "JASPER_USBSINK_PREEMPT_STATE_PATH",
    "JASPER_VOLUME_DIAGNOSTICS_PATH",
    "JASPER_WAKE_CORPUS_BRIDGE_ENV",
    "JASPER_WAKE_EVENTS_DIR",
    # -- /proc & /sys mount-point / probe-command overrides — pure test
    #    seams for the doctor / hardware probes.
    "JASPER_ASOUND_RENDER_COMMAND",
    "JASPER_PROC_ASOUND",
    "JASPER_SYS_CLASS_SOUND",
    # -- Internal AEC bridge / wake-corpus experiment knobs. Developer
    #    surface only; the corpus booleans are stamped into the bridge
    #    env by jasper/wake_corpus/bridge_session.py at session start,
    #    the UDP port numbers are loopback wiring with paired defaults
    #    on both ends.
    "JASPER_AEC_CHIP_AEC_PRIMARY_LEG",
    "JASPER_AEC_CHIP_SYS_DELAY",
    "JASPER_AEC_CORPUS_CHIP_AEC_ENABLED",
    "JASPER_AEC_CORPUS_XVF_RAW0_DTLN_ENABLED",
    "JASPER_AEC_CORPUS_XVF_RAW0_WEBRTC_AEC3_ENABLED",
    "JASPER_AEC_DTLN_SIZE",
    "JASPER_AEC_RAW_AGC_ENABLED",
    "JASPER_AEC_RAW_AGC_MAX_GAIN_DB",
    "JASPER_AEC_RAW_AGC_TARGET_DBFS",
    "JASPER_AEC_REF_HPF_HZ",
    "JASPER_AEC_UDP_PORT_RAW0",
    "JASPER_AEC_UDP_PORT_REF",
    "JASPER_AEC_UDP_PORT_USB_DTLN",
    "JASPER_AEC_UDP_PORT_USB_RAW",
    "JASPER_AEC_UDP_PORT_USB_WEBRTC",
    "JASPER_AEC_UDP_PORT_XVF_RAW0_DTLN",
    "JASPER_AEC_UDP_PORT_XVF_RAW0_WEBRTC_AEC3",
    "JASPER_AEC_USB_DTLN_SIZE",
    "JASPER_AEC_USB_MIC_DEVICE",
    "JASPER_AEC_USB_MIC_RATE",
    "JASPER_AEC_USB_MIXER_CARD",
    "JASPER_AEC_XVF_RAW0_DTLN_SIZE",
    # -- Active-follower round-trip snd-aloop loopback device overrides
    #    (distributed-active Slice 3). Paired snd-aloop substream wiring
    #    with safe code defaults on both ends (snapclient writes
    #    hw:Loopback,0,5; the follower's CamillaDSP captures hw:Loopback,1,5).
    #    The reconciler is the single writer of the daemon-facing device env;
    #    these env overrides exist only for on-device tuning if the default
    #    substream ever collides with the fan-in layout. Not operator config.
    "JASPER_GROUPING_LOOPBACK_CAPTURE",
    "JASPER_GROUPING_LOOPBACK_PLAYBACK",
    # -- USB-sink internals: localhost wiring between mux and the
    #    usbsink daemon plus daemon-local knobs. The operator-facing
    #    members of the family (JASPER_USBSINK_PREEMPT,
    #    _CAPTURE_DEVICE, _MIXER_CARD) are documented in .env.example
    #    and are not in this list.
    "JASPER_USBSINK_CHANNELS",
    "JASPER_USBSINK_CONTROL_URL",
    "JASPER_USBSINK_LOG_LEVEL",
    "JASPER_USBSINK_PREEMPT_HOST",
    "JASPER_USBSINK_PREEMPT_PORT",
    "JASPER_USBSINK_SAMPLE_RATE",
    # -- Internal timing / safety tunables with code defaults, below
    #    the operator surface. Promote one to .env.example (with the
    #    required prose comment) if it ever becomes household-relevant.
    "JASPER_GROK_PROACTIVE_BUFFER_SEC",
    "JASPER_GROK_SESSION_MAX_SEC",
    "JASPER_OPENAI_PROACTIVE_BUFFER_SEC",
    "JASPER_OPENAI_SESSION_MAX_SEC",
    "JASPER_SOURCE_HANDOFF_SETTLE_SEC",
    "JASPER_SOURCE_PUSH_SETTLE_SEC",
    "JASPER_TTS_DRAIN_TAIL_SEC",
    "JASPER_VOLUME_FIRST_BOOT_DEFAULT_PCT",
    "JASPER_VOLUME_REGRESS_AFTER_SEC",
    "JASPER_VOLUME_REGRESS_SAFE_HIGH_PCT",
    "JASPER_VOLUME_REGRESS_SAFE_LOW_PCT",
    # -- Derived-default URL override (defaults derive from
    #    JASPER_HOSTNAME like JASPER_MANAGEMENT_URL; override is for
    #    nonstandard reverse-proxy setups only).
    "JASPER_GOOGLE_SETUP_URL",
    # -- Calibration-agent advisor LLM selection — lab CLI
    #    (jasper/calibration_agent/cli.py), not a speaker daemon; its
    #    --advisor-* flags are the primary interface.
    "JASPER_CALIBRATION_ADVISOR_MODEL",
    "JASPER_CALIBRATION_ADVISOR_OPENAI_BASE_URL",
    "JASPER_CALIBRATION_ADVISOR_PROVIDER",
    "JASPER_CALIBRATION_ADVISOR_TIMEOUT_SEC",
    # -- Debug capture toggles (off unless an operator exports one for
    #    a single diagnosis session; raw OpenAI session audio dumps,
    #    correction measurement bundle retention).
    "JASPER_CORRECTION_SAVE_BUNDLES",
    "JASPER_DEBUG_OPENAI_AUDIO_DIR",
    "JASPER_DEBUG_RECORD_OPENAI_AUDIO",
}


def _read_vars() -> set[str]:
    names: set[str] = set()
    for py in sorted((ROOT / "jasper").rglob("*.py")):
        names.update(_READ_RE.findall(code_only(py.read_text(encoding="utf-8"))))
    return names


def _codified_vars() -> set[str]:
    names: set[str] = set()
    for surface in _SURFACES:
        path = ROOT / surface
        files = [path] if path.is_file() else sorted(
            p for p in path.rglob("*") if p.is_file()
        )
        for f in files:
            names.update(_TOKEN_RE.findall(f.read_text(encoding="utf-8", errors="ignore")))
    return names


def test_scan_still_sees_the_real_surface():
    """Pin floors so a regex / layout change can't make the other
    assertions pass vacuously."""
    assert len(_read_vars()) >= 150, "env-read extraction collapsed — fix _READ_RE"
    assert len(_codified_vars()) >= 150, "codified-surface scan collapsed"


def test_every_env_var_read_is_codified():
    orphans = sorted(_read_vars() - _codified_vars() - _UNCODIFIED)
    assert not orphans, (
        f"JASPER_* env var(s) read in jasper/ with no codification surface: "
        f"{orphans}. Per AGENTS.md 'Codify, don't memorise', give each a "
        "discoverable home — a prose-commented .env.example entry for an "
        "operator knob, or the wizard/reconciler/install.sh step that writes "
        "it — or, if it is genuinely internal (test seam, path override, "
        "experiment knob), add it to _UNCODIFIED with a comment saying which."
    )


def test_uncodified_allowlist_is_not_stale():
    read, codified = _read_vars(), _codified_vars()
    gone = sorted(v for v in _UNCODIFIED if v not in read)
    assert not gone, (
        f"_UNCODIFIED entries no longer read anywhere in jasper/: {gone} — "
        "remove them."
    )
    now_codified = sorted(v for v in _UNCODIFIED if v in codified)
    assert not now_codified, (
        f"_UNCODIFIED entries now appear on a codification surface: "
        f"{now_codified} — the orphan is fixed; remove the allowlist entry."
    )
