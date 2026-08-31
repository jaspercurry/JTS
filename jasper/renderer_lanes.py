# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Renderer-ingress lane map — which renderers reach fan-in over an SHM ring.

This module owns ONE fact and everything derived from it: **which renderer
lanes on this box ingress over a per-renderer SHM slot ring instead of an
snd-aloop capture substream** (the audio-graph consolidation campaign's U3 /
P6). It is the single place that rule is stated, and
:func:`render_renderer_lanes_env` is the single writer of the file both ends
read.

The rule, in one sentence
-------------------------

    A renderer lane is ring-ingress **iff its fan-in label appears in the
    armed set**; the armed set is empty unless an operator explicitly armed
    it, so an unarmed box is byte-identical to one on which this mechanism
    does not exist.

Two ends, one file, one writer
------------------------------

A lane flip has to move BOTH ends — the renderer must write the ring and
fan-in must read it — so the two values live in ONE file
(:data:`RENDERER_LANES_ENV`) written by ONE writer, and both units load it
last so it beats their in-unit defaults:

* ``JASPER_FANIN_RENDERER_RING_LANES`` — read by ``jasper-fanin``; selects the
  lane's read path (``rust/jasper-fanin/src/mixer/ring_capture.rs``).
* ``JASPER_<RENDERER>_DEVICE`` — selects the ALSA PCM the renderer writes.
  Three delivery shapes, one per lane kind: a unit-ful lane's ``ExecStart``
  substitutes it from the loaded-last env chain (librespot, bluealsa); a
  CONF-RENDERER lane's per-start renderer script reads the rendered line out
  of this file and substitutes it into the renderer's own config artifact
  (shairport — see :attr:`RendererLane.conf_renderer`); a unitless lane's
  spawn helper derives the same value at call time and the line is the map's
  own record (correction).

Writing them together is what makes a half-flip unrepresentable **by this
writer**. Be precise about the scope, because two other things could still
produce one: a hand-edit of the file (which is why the rendered header says not
to), and the restart window below. What this module guarantees is narrower and
worth stating exactly: *no render this function performs can leave the two keys
disagreeing.*

Neither end is restarted by the write, so both keep running their OLD values
until each is restarted. The transition is atomic ON DISK and NOT atomic in the
running system — between the two restarts one end has moved and the other has
not, and that window is silence on the affected lane.

That is deliberate, and it is why nothing here restarts anything. A writer that
restarted one unit would be choosing which end leads and making the window a
property of this code; leaving both restarts to the operator (or to a deploy,
which bounces both) keeps the window short, visible, and owned by whoever is
watching. So the arm is: write -> restart jasper-fanin AND the renderer. The
CLI prints exactly which units that means.

Why the CamillaDSP coupling is deliberately NOT an input
--------------------------------------------------------

It would be natural to key this on ``JASPER_FANIN_CAMILLA_COUPLING`` — a
ring-coupled box gets ring renderers. That is wrong on both correctness and
safety grounds:

* **They are independent transports, so "armed" names two independent facts
  about a box.** A renderer ring carries renderer → fan-in; the coupling
  describes fan-in → CamillaDSP. A loopback-coupled box can ring-ingress a
  renderer and a ring-coupled box can keep aloop renderers; neither combination
  is incoherent. The only real precondition is the ring PLATFORM (the ioplug
  ``.so`` and ``/dev/shm/jts-ring``), which ships on every box, and which
  :func:`arm_refusal_reason` checks at arm time.
* **Keying on the coupling would arm the fleet by deploy.** Every
  ring-coupled box would flip its renderer the moment this code landed,
  with no per-box source pass — the opposite of how every other per-box
  arming in this campaign works (no deploy arms the active ring on coupling
  alone — since P6 its unattended arm still requires a per-box proven graph,
  and ring width activation is per box and owner-gated).

So membership is explicit and per box, and ``jasper-fanin`` consults nothing
else either (see ``Config::lane_is_renderer_ring``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from jasper.atomic_io import atomic_write_text
from jasper.audio_measurement.correction_lane import CORRECTION_SUBSTREAM
from jasper.log_event import log_event

_LOG = logging.getLogger(__name__)

# The file both ``jasper-fanin`` and each migrated renderer unit load LAST, so
# its values beat the in-unit ``Environment=`` defaults. Mode 0644: it carries
# no secret, and every renderer user must be able to read it.
RENDERER_LANES_ENV = "/var/lib/jasper/renderer_lanes.env"
RENDERER_LANES_ENV_MODE = 0o644

# fan-in's env key for the armed lane set. Mirrored in Rust by
# ``Config::renderer_ring_lanes``.
FANIN_RING_LANES_KEY = "JASPER_FANIN_RENDERER_RING_LANES"

# The SHM ring directory and the renderer-ring filename prefix. Mirrored by
# value in ``rust/jasper-fanin/src/config.rs`` (``RING_SHM_DIR`` /
# ``RENDERER_RING_PREFIX``); ``tests/test_renderer_ring_lanes.py`` pins the two
# spellings against each other, because a silent divergence here would leave
# fan-in reading a ring nothing writes.
RING_SHM_DIR = "/dev/shm/jts-ring"
RENDERER_RING_PREFIX = "lane-"

# The conf.d drop-in that declares every renderer-lane ring PCM and its `plug:`
# wrapper. Distinct from :data:`jasper.ring_assets.RING_CONF_D` (the program /
# content / active rings), and the attach authority for a renderer lane's
# geometry — so every geometry comparison on this side passes it explicitly
# rather than defaulting to the other file's.
RENDERER_LANES_CONF_D = "/etc/alsa/conf.d/61-jts-renderer-lanes.conf"

# The group that owns /dev/shm/jts-ring (deploy/tmpfiles/jts-ring.conf). A
# renderer must be a member for its ioplug to create the lane's ring file.
RING_GROUP = "jts-ring"

# Mirrors ``jasper_ring::{MIN,MAX}_N_SLOTS`` and fan-in's RING_SLOTS_{MIN,MAX};
# the ring header validates the same range at attach.
RING_SLOTS_MIN = 2
RING_SLOTS_MAX = 16


@dataclass(frozen=True)
class RendererLane:
    """One migratable renderer lane.

    Holds every per-lane fact the flip needs, so adding a lane is one entry
    here plus its conf.d block and unit/renderer edit — not a new rule. P6b–d
    each landed that way; the per-lane variation lives in the optional fields
    (``unit=None`` for ephemeral writers, ``conf_renderer`` for a
    conf-rendered device, ``arm_advisory`` for arm-time operator notes).
    """

    #: The fan-in lane LABEL. This is the identity fan-in already has for the
    #: lane, and the identity the ring path derives from; it is NOT always the
    #: renderer's name (librespot's lane is labelled ``spotify``).
    label: str
    #: Human name of the renderer that writes this lane, for messages.
    renderer: str
    #: The systemd unit whose ``ExecStart`` reads :attr:`device_key` — or
    #: ``None`` for a lane whose writers are EPHEMERAL spawns with no unit
    #: (the ``correction`` lane: wizard/CLI ``aplay`` children via
    #: :mod:`jasper.audio_measurement.correction_lane`, which resolves the
    #: device per spawn through ``correction_play_device()`` instead of a
    #: unit env key). A unitless lane needs no renderer restart on arm —
    #: only jasper-fanin's.
    unit: str | None
    #: The env key rendered into the lane map. For a unit-ful lane the unit's
    #: ``ExecStart`` substitutes it; for a conf-renderer lane
    #: (:attr:`conf_renderer`) the renderer script reads the rendered line and
    #: substitutes the value into the renderer's own config artifact; for a
    #: unitless lane the line is the map's own record of the resolved device
    #: (the spawn path derives the same value through ``device_for`` at call
    #: time and reads no env key).
    device_key: str
    #: The ALSA PCM name the renderer writes on an UNARMED box — the shipped
    #: snd-aloop path, which stays defined until P9 because it is the
    #: not-yet-migrated boxes' path, not a fallback mode.
    aloop_device: str
    #: The ALSA PCM name the renderer writes on an ARMED box: a ``plug:``
    #: wrapper over this lane's ``jts_ring`` PCM, so the renderer's native rate
    #: still converts through ``defaults.pcm.rate_converter``.
    ring_device: str
    #: Unitless lanes only: the NON-root writer identity whose ``jts-ring``
    #: membership the arm preflights (``renderer_user_in_ring_group``). A
    #: unit-ful lane leaves this ``None`` — its user is parsed from the unit
    #: file. The correction lane's other writer identities
    #: (jasper-correction-web, the streambox variant, operator CLIs) are
    #: root and pass the predicate trivially, so preflighting the one
    #: non-root identity is the complete check.
    arm_preflight_user: str | None = None
    #: Unit-ful lanes whose renderer reads a STATIC CONFIG ARTIFACT rather
    #: than argv/env: the repo-relative path of the per-start renderer script
    #: that derives that artifact — the script reads this lane's
    #: :attr:`device_key` line out of the rendered map (falling back to
    #: :attr:`aloop_device` when no map exists) and substitutes the value
    #: into the artifact it is the single writer of. The flip therefore still
    #: lands on the unit's next start, exactly like the ``ExecStart``
    #: substitution lanes, because the unit's ``ExecStartPre`` runs the
    #: script. ``None`` for lanes whose unit reads :attr:`device_key`
    #: directly and for unitless lanes.
    conf_renderer: str | None = None
    #: Optional operator note the arm CLI prints when this label is NEWLY
    #: armed. Plain data, no machinery: for facts the operator must carry to
    #: the box's source pass (AirPlay's sync constants are derived from the
    #: lane's ring geometry, and deploy/shairport-sync.conf.template owns
    #: the values). Never a refusal — advisories inform, gates gate.
    arm_advisory: str | None = None


#: Every lane this mechanism knows about — one row per MIGRATED renderer.
#:
#: A lane appears here when its own PR lands, together with its conf.d block and
#: its unit edit; a lane that has not been migrated is absent rather than
#: pre-declared, because a row here is what makes a label armable and arming a
#: renderer whose ring PCM does not exist would be a silent source. All four
#: renderer lanes are registered as of P6d (shairport); adding another is one
#: entry plus those two files — not a new rule.
RENDERER_LANES: tuple[RendererLane, ...] = (
    RendererLane(
        label="spotify",
        renderer="librespot",
        unit="librespot.service",
        device_key="JASPER_LIBRESPOT_DEVICE",
        aloop_device="librespot_substream",
        ring_device="librespot_ring_lane",
    ),
    RendererLane(
        # The label is `bluealsa`, NOT `bluetooth`. fan-in's
        # `JASPER_FANIN_INPUT_RENDERERS` owns the lane vocabulary and spells this
        # lane `bluealsa`; a mismatch is refused at config by fan-in's own
        # armed-label guard, so the two cannot silently disagree. (`bluetooth` is
        # the SOURCE name — `Source.BLUETOOTH`, `/sources/` — which is a
        # different vocabulary from the fan-in lane set.)
        label="bluealsa",
        renderer="bluealsa-aplay",
        unit="bluealsa-aplay.service",
        device_key="JASPER_BLUEALSA_DEVICE",
        aloop_device="bluealsa_substream",
        ring_device="bluealsa_ring_lane",
    ),
    RendererLane(
        # The correction/commissioning measurement lane — U3/P6c-ii, and the
        # first UNITLESS lane: its writers are ephemeral `aplay` spawns from
        # four process identities (jasper-web wizards, the root
        # jasper-correction-web, the root streambox variant, root operator
        # CLIs), all routed through
        # jasper.audio_measurement.correction_lane's helpers, whose
        # `correction_play_device()` resolves THIS row's device_for() at
        # every spawn. No unit reads the device_key line; it stays in the
        # rendered map as the file's own record of the resolved device.
        # `aloop_device` imports the lane-name SSOT rather than respelling
        # the literal (tests/test_correction_substream_ssot.py's guard).
        label="correction",
        renderer="correction-lane ephemeral aplay (wizards, correction web, CLIs)",
        unit=None,
        device_key="JASPER_CORRECTION_DEVICE",
        aloop_device=CORRECTION_SUBSTREAM,
        ring_device="correction_ring_lane",
        arm_preflight_user="jasper-web",
    ),
    RendererLane(
        # The AirPlay lane — U3/P6d, the LAST renderer lane, and the first
        # CONF-RENDERER lane: shairport-sync reads `output_device` from
        # /etc/shairport-sync.conf, a derived artifact that
        # jasper-apply-airplay-mode re-renders from its template on EVERY
        # unit start (ExecStartPre) and at install/CLI/wizard time. That
        # script — the artifact's single writer from all four invocation
        # sites — reads THIS row's device_key line out of the rendered map
        # and falls back to the aloop device when no map exists, so the
        # armed-set fact is never re-derived in a second language (the bash
        # side reads the RESOLVED value, never the armed set). The Tier-3
        # wedge supervisor's recovery restart runs the same ExecStartPre, so
        # the recovery path follows this map for free.
        label="airplay",
        renderer="shairport-sync",
        unit="shairport-sync.service",
        device_key="JASPER_SHAIRPORT_DEVICE",
        aloop_device="shairport_substream",
        ring_device="shairport_ring_lane",
        conf_renderer="deploy/bin/jasper-apply-airplay-mode",
        # Printed at arm time. Restates the template's shipped sync values;
        # deploy/shairport-sync.conf.template owns them.
        arm_advisory=(
            "AirPlay's sync values are sized against this lane's geometry: "
            "drift_tolerance=0.010 clears the 5.333 ms delay grain, and "
            "audio_backend_buffer_desired_length at 0.045 sits mid-lane "
            "inside the lane's ~90.6 ms saturation ceiling so the servo "
            "keeps two-sided headroom. Keep resync_threshold at 0.2. "
            "Re-check ring occupancy and A/V sync on the box after arming."
        ),
    ),
)

#: Labels that can be armed, in declaration order.
MIGRATABLE_LABELS: tuple[str, ...] = tuple(lane.label for lane in RENDERER_LANES)


def lane_by_label(label: str) -> RendererLane | None:
    """The :class:`RendererLane` with this fan-in label, or ``None``."""
    for lane in RENDERER_LANES:
        if lane.label == label:
            return lane
    return None


def renderer_ring_path(label: str) -> str:
    """The ring file a lane's renderer writes and fan-in reads.

    Derived from the lane LABEL and nothing else — there is no per-ring env
    key, so the writer's path and the reader's path cannot drift through a
    second knob. Mirrors ``jasper_fanin::config::renderer_ring_path``.
    """
    return f"{RING_SHM_DIR}/{RENDERER_RING_PREFIX}{label}.ring"


#: Characters Python's ``str.strip()`` removes but Rust's ``str::trim()`` does
#: NOT. Rust trims the Unicode ``White_Space`` set; Python additionally strips
#: the C0 information separators. Trimming them here would make the Python
#: parser accept a label Rust keeps un-trimmed — the two would then disagree
#: about which lanes are armed, which is a half-flip with no file to blame.
_RUST_KEEPS = "\x1c\x1d\x1e\x1f"


def _rust_trim(s: str) -> str:
    """``str::trim()`` semantics, exactly — Unicode ``White_Space`` only."""
    return s.strip("".join(c for c in s if c.isspace() and c not in _RUST_KEEPS)) \
        if s else s


def parse_armed_labels(value: str | None) -> tuple[str, ...]:
    """Parse the armed-lane env value into labels.

    Whitespace is trimmed and empty entries are dropped, so ``None``, ``""``,
    ``" "`` and ``",,"`` all mean "nothing armed" — the fail-safe direction for
    a key whose empty value must never be read as "arm everything". Order is
    preserved and duplicates are NOT collapsed.

    **The trim is Rust's, not Python's.** Swept across all 1,114,112
    codepoints, exactly FOUR characters diverge: the C0 information separators
    ``\x1c`` FS, ``\x1d`` GS, ``\x1e`` RS and ``\x1f`` US, which
    ``str.strip()`` removes and ``str::trim()`` does not (they are not Unicode
    ``White_Space``). That is 8 (character, position) combinations — leading
    and trailing for each — of which the parametrized test below pins 5. Those
    four characters are enough for this parser to report a lane armed that
    fan-in reports un-armed, off the same bytes. So the trim is matched
    deliberately here, and
    ``tests/test_renderer_ring_lanes.py`` pins the two against each other on
    exactly those characters.
    """
    if not value:
        return ()
    out = []
    for part in value.split(","):
        trimmed = _rust_trim(part)
        if trimmed:
            out.append(trimmed)
    return tuple(out)


def read_armed_labels(path: str | None = None) -> tuple[str, ...]:
    """The armed lane set recorded on this box.

    The written file is its own intent record: there is no second
    "intent" file that could disagree with what was rendered. A missing or
    unreadable file means nothing is armed, which is the shipped fleet state.

    ``path`` defaults to :data:`RENDERER_LANES_ENV` resolved AT CALL TIME, not
    bound as a default argument. A default argument would capture the module
    constant at import, so redirecting the constant (tests, a future per-box
    override) would silently keep reading the production path — the shape that
    makes a redirect look applied while nothing moved.
    """
    return parse_armed_labels(
        _env_file_value(path or RENDERER_LANES_ENV, FANIN_RING_LANES_KEY)
    )


def _env_file_value(path: str, key: str) -> str | None:
    """Read one ``KEY=value`` from a simple env file, or ``None``.

    Deliberately a tiny local reader rather than a shell-out or a general env
    parser: this file is written by exactly one writer in exactly one shape,
    and a permissive parser would invite hand-editing the file it is the sole
    writer of.

    **Every trim here is** :func:`_rust_trim`, not ``str.strip()``.
    Matching the trim in :func:`parse_armed_labels` alone was not enough: this
    reader runs FIRST, so a Python-strip here ate the C0 separators before the
    matched parser could ever see them, and the two languages disagreed off the
    same bytes after all. Measured end to end through a real file, a value of
    ``spotify\x1c`` made :func:`read_armed_labels` report the lane ARMED while
    ``jasper-fanin``'s ``env_csv_labels`` saw an unknown label and refused with
    a config-class park — and :func:`fanin_env_expectations`, reading the same
    file, reported armed-and-consistent while the daemon would not start. Rust
    is the authority for this vocabulary, so the whole read path defers to it.

    Severity, scoped honestly: this file has a single validated writer, so the
    only ingress for such a value is a hand-edit, and whether systemd would
    even deliver a raw 0x1C through ``EnvironmentFile=`` is unverified (no Pi
    available). It is fixed regardless, because the claim in
    :func:`parse_armed_labels` that the two parsers agree has to be TRUE, not
    true-of-one-layer.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    found: str | None = None
    for line in lines:
        # `_rust_trim` removes the line terminator too (\n and \r are Unicode
        # White_Space), so this still strips the newline without eating a
        # trailing separator that belongs to the value.
        stripped = _rust_trim(line)
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if _rust_trim(name) == key:
            # Last assignment wins, matching systemd's EnvironmentFile.
            found = _rust_trim(value)
    return found


def device_for(lane: RendererLane, armed: bool) -> str:
    """The ALSA PCM this lane's renderer writes, given whether it is armed."""
    return lane.ring_device if armed else lane.aloop_device


def render_env_text(armed: tuple[str, ...]) -> str:
    """The complete contents of :data:`RENDERER_LANES_ENV` for ``armed``.

    Every known lane gets a device line — including the unarmed ones, which get
    their aloop device explicitly. Writing the unarmed value rather than
    omitting it is what makes a DISARM take effect: an omitted key would leave
    the previous armed value in place on a box whose file already had one, and
    "disarm did nothing" is the worst possible failure for a rollback path.
    """
    lines = [
        "# Renderer-ingress lane map — audio-graph consolidation U3 / P6.",
        "#",
        "# WRITTEN BY jasper.renderer_lanes.render_renderer_lanes_env (via",
        "# `jasper-audio-config renderer-lanes`). Do not hand-edit: this file is",
        "# the single writer's own record of the armed set, and both ends of every",
        "# lane are derived from it together, so no render can leave the two keys",
        "# disagreeing. A HAND-EDIT can — which is what this line is for.",
        "# The RUNNING system still transitions one unit at a time: both keep their",
        "# old values until restarted, and the gap between the two restarts is",
        "# silence on that lane. Restart jasper-fanin and the renderer together.",
        "#",
        "# Loaded LAST by jasper-fanin.service and by each migrated renderer unit,",
        "# so these values beat the in-unit Environment= defaults. Neither end is",
        "# restarted by the write; the flip lands on each unit's next start.",
        "",
        f"{FANIN_RING_LANES_KEY}={','.join(armed)}",
    ]
    for lane in RENDERER_LANES:
        lines.append(f"{lane.device_key}={device_for(lane, lane.label in armed)}")
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class RenderOutcome:
    """What :func:`render_renderer_lanes_env` did."""

    armed: tuple[str, ...]
    changed: bool
    path: str


def render_renderer_lanes_env(
    armed: tuple[str, ...],
    *,
    path: str | None = None,
) -> RenderOutcome:
    """Publish the lane map. **The single writer of** :data:`RENDERER_LANES_ENV`.

    Write-on-change: an unchanged map leaves the file genuinely untouched (no
    rewrite, no mtime churn), so repeated reconcile passes on a converged box
    do not churn it. Otherwise the whole file is published through
    :func:`jasper.atomic_io.atomic_write_text`, so a renderer or fan-in
    starting concurrently never loads a half-written map.

    Raises ``ValueError`` for an unknown label — arming a label no lane
    declares would write a fan-in key naming a lane that does not exist, which
    fan-in itself refuses at startup. Better to refuse here, where the operator
    is watching.
    """
    for label in armed:
        if lane_by_label(label) is None:
            raise ValueError(
                f"unknown renderer lane {label!r}; known lanes: "
                f"{', '.join(MIGRATABLE_LABELS)}"
            )
    path = path or RENDERER_LANES_ENV
    text = render_env_text(armed)
    try:
        with open(path, encoding="utf-8") as fh:
            current = fh.read()
    except OSError:
        current = None
    if current == text:
        return RenderOutcome(armed=armed, changed=False, path=path)
    atomic_write_text(path, text, mode=RENDERER_LANES_ENV_MODE)
    return RenderOutcome(armed=armed, changed=True, path=path)


def renderer_user_in_ring_group(
    unit_user: str | None, group: str = RING_GROUP
) -> bool | None:
    """Whether ``unit_user`` can write the ring directory via ``group``.

    ``None`` when the question cannot be answered on this host (no such user or
    group — a dev laptop, or a box where the installer has not run). The caller
    treats `None` as "unknown, do not refuse": refusing on an unanswerable
    question would make the arm impossible to run anywhere but a live Pi.

    **ROOT (and an absent ``User=``, which systemd means as root) is always
    capable, and must answer True rather than False.** Root bypasses the group
    check entirely — a 2775 root-owned directory is writable by uid 0 whatever
    the group says.

    **Reachability, stated honestly.** Before P6b this function answered
    ``False`` for the literal ``"root"``, and it is tempting to call that a
    fleet-breaker. It was not, and the difference matters. The only production
    caller (``jasper.cli.audio_config``) short-circuited on a falsy user and
    passed ``None``, which :func:`arm_refusal_reason` never refuses on — it
    refuses only on ``is False``. And no registered lane's unit spells
    ``User=root``: ``bluealsa-aplay`` sets no ``User=`` at all, so the literal
    was never produced. So the bluealsa arm would have PASSED under P6a. This is
    hardening and future-proofing — a unit that ever *does* spell ``User=root``,
    or any caller that stops short-circuiting — not a caught outage.

    The caller no longer short-circuits (that was Nit-A1 of the P6b round), so
    the ``None`` branch here is now live from production and the value it
    returns is honest about why it does not refuse, rather than collapsing
    "root, definitely capable" into the same ``None`` as "could not tell".
    """
    if unit_user is None or unit_user == "root" or unit_user == "0":
        return True
    try:
        import grp
        import pwd
    except ImportError:  # pragma: no cover - POSIX only
        return None
    try:
        entry = grp.getgrnam(group)
    except KeyError:
        return None
    if unit_user in entry.gr_mem:
        return True
    try:
        pw = pwd.getpwnam(unit_user)
    except KeyError:
        return None
    # uid 0 under another name is still root.
    if pw.pw_uid == 0:
        return True
    # A primary-group match counts too: gr_mem lists only SUPPLEMENTARY members.
    return pw.pw_gid == entry.gr_gid


def arm_refusal_reason(
    label: str,
    *,
    assets_present: bool,
    missing_assets: tuple[str, ...] = (),
    lane_conf_present: bool | None = None,
    user_in_ring_group: bool | None = None,
    input_buffer_frames: int | None = None,
    period_frames: int | None = None,
) -> str | None:
    """Why ``label`` may not be armed right now, or ``None`` if it may.

    Everything checked here is a precondition whose absence would make the arm
    produce SILENCE rather than an error the operator could connect to this
    command. That is the whole selection rule: fail closed where the failure
    would otherwise be quiet.

    * **ring platform** — the compiled ioplug and ``/dev/shm/jts-ring``. Without
      them the renderer's ``jts_ring`` PCM cannot resolve.
    * **the lane conf.d** — ``61-jts-renderer-lanes.conf``, which declares the
      lane's ring PCM and its ``plug:`` wrapper. The platform check above does
      not cover it (it looks at ``60-``), and without this file the renderer's
      device name resolves to nothing.
    * **group membership** — the renderer user must be in ``jts-ring`` or its
      ioplug cannot create the ring under a 2775 directory it does not have
      write access to.
    * **the geometry** — the derived slot count must be expressible. fan-in
      REFUSES an inexpressible one at config with a park, which is correct but
      lands in the journal after the fact; surfacing it here puts it where the
      operator is actually watching.

    Presence answers are passed IN rather than probed, so the decision is
    testable without a Pi and the caller reads each surface once. A ``None``
    answer means "could not determine" and never refuses — an unanswerable
    question must not make the arm impossible to run off-box.
    """
    if lane_by_label(label) is None:
        return f"unknown lane {label!r} (known: {', '.join(MIGRATABLE_LABELS)})"
    if not assets_present:
        detail = ", ".join(missing_assets) if missing_assets else "ring platform"
        return (
            f"ring platform assets missing ({detail}) — arming would leave the "
            "renderer unable to resolve its ring PCM; redeploy to install them"
        )
    if lane_conf_present is False:
        return (
            f"{RENDERER_LANES_CONF_D} is missing — it declares this lane's ring "
            "PCM and its plug: wrapper, so the renderer's device would resolve "
            "to nothing; redeploy to install it"
        )
    if user_in_ring_group is False:
        lane = lane_by_label(label)
        # A unitless lane names its writers instead of a unit.
        unit = (lane.unit or lane.renderer) if lane else "the renderer"
        return (
            f"{unit}'s runtime user is not in group {RING_GROUP!r} — its ioplug "
            f"could not create the ring under {RING_SHM_DIR} (mode 2775). "
            "Redeploy (the installer adds it) and restart the unit"
        )
    if input_buffer_frames is not None and period_frames is not None:
        slots = renderer_ring_slots(input_buffer_frames, period_frames)
        if slots is None:
            return (
                f"this box's lane geometry has no whole-slot expression: "
                f"input_buffer_frames={input_buffer_frames} / "
                f"period_frames={period_frames} must divide evenly into "
                f"{RING_SLOTS_MIN}..={RING_SLOTS_MAX} slots. Arming would park "
                "jasper-fanin at its next start (config-class, exit 78)"
            )
    return None


#: fan-in's env-file chain, in the order `jasper-fanin.service` loads it. LATER
#: wins, so this list is read back-to-front when resolving an effective value.
#: Mirrors the unit; `tests/test_renderer_ring_lanes.py` pins it against the
#: unit's own `EnvironmentFile=` lines.
FANIN_ENV_CHAIN = (
    "/etc/jasper/jasper.env",
    "/var/lib/jasper/fanin.env",
    RENDERER_LANES_ENV,
)

#: Defaults that apply when no file in the chain sets the key, with the SOURCE
#: named — the two are genuinely different places, and conflating them would
#: make this model claim the unit sets something it does not.
#:
#: ``FANIN_UNIT_DEFAULTS`` come from ``Environment=`` lines in
#: jasper-fanin.service; ``FANIN_RUST_DEFAULTS`` come from ``Config::from_env``'s
#: own fallbacks in ``rust/jasper-fanin/src/config.rs``. Both are pinned against
#: their real sources by ``tests/test_renderer_ring_lanes.py``.
FANIN_UNIT_DEFAULTS = {
    "JASPER_FANIN_INPUT_BUFFER_FRAMES": 4096,
}
FANIN_RUST_DEFAULTS = {
    "JASPER_FANIN_PERIOD_FRAMES": 256,
}


def resolve_effective_fanin_value(key: str, *, chain=None) -> tuple[int | None, str]:
    """The value `jasper-fanin` will actually see for `key`, and where from.

    Models the SAME precedence the unit applies — later `EnvironmentFile=` beats
    earlier, and every file beats the in-unit `Environment=` default — because
    reading only one file is exactly how a preflight ends up approving a box
    whose next daemon start uses a different number. That is the failure the
    Ring-A resolver (`resolve_effective_fanin_ring_slots`) was written for; this
    is the same shape for the lane-geometry keys.

    Returns `(value, source)`. `value` is `None` only when a present value
    cannot be parsed as an int, which the caller treats as unknown rather than
    guessing.
    """
    for path in reversed(tuple(chain if chain is not None else FANIN_ENV_CHAIN)):
        raw = _env_file_value(path, key)
        if raw is None or raw == "":
            continue
        try:
            return int(raw), path
        except ValueError:
            return None, path
    if key in FANIN_UNIT_DEFAULTS:
        return FANIN_UNIT_DEFAULTS[key], "unit-default"
    return FANIN_RUST_DEFAULTS.get(key), "rust-default"


def effective_lane_geometry(*, chain=None) -> tuple[int | None, int | None, str]:
    """`(input_buffer_frames, period_frames, provenance)` for THIS box."""
    buf, buf_src = resolve_effective_fanin_value(
        "JASPER_FANIN_INPUT_BUFFER_FRAMES", chain=chain
    )
    per, per_src = resolve_effective_fanin_value(
        "JASPER_FANIN_PERIOD_FRAMES", chain=chain
    )
    return buf, per, f"buffer={buf_src} period={per_src}"


def renderer_ring_slots(input_buffer_frames: int, period_frames: int) -> int | None:
    """Slots for a renderer ring — MIRRORS ``jasper_fanin::config::renderer_ring_slots``.

    Kept in Python so the arm can refuse an inexpressible geometry where the
    operator is watching, instead of only as a fan-in park at the next start.
    ``tests/test_renderer_ring_lanes.py`` pins this against the Rust source.
    """
    if period_frames <= 0 or input_buffer_frames % period_frames != 0:
        return None
    slots = input_buffer_frames // period_frames
    if not (RING_SLOTS_MIN <= slots <= RING_SLOTS_MAX):
        return None
    return slots


def expected_fanin_lane_pcm(label: str, aloop_pcm: str, armed: tuple[str, ...]) -> str:
    """What fan-in's STATUS should report as this lane's ``pcm``.

    A ring lane reports its ring PATH (where its audio actually comes from),
    not the aloop name it ignores. The doctor's lane-roster check reads this so
    an armed box is not diagnosed as drifted.
    """
    return renderer_ring_path(label) if label in armed else aloop_pcm


def fanin_env_expectations(path: str | None = None) -> dict[str, str]:
    """The env values a correctly-rendered map implies, for drift checks.

    ``path`` resolves at call time — see :func:`read_armed_labels`.
    """
    armed = read_armed_labels(path)
    out = {FANIN_RING_LANES_KEY: ",".join(armed)}
    for lane in RENDERER_LANES:
        out[lane.device_key] = device_for(lane, lane.label in armed)
    return out


def ring_conf_pcm_name(label: str) -> str:
    """The `jts_ring` PCM block in the lane conf.d that declares this lane's ring."""
    return f"jts_ring_lane_{label}"


def stale_ring_verdict(label: str, *, conf_d: str | None = None):
    """Compare this lane's on-disk ring header against its conf.d block.

    Delegates to :func:`jasper.ring_assets.ring_header_matches_conf` — the ONE
    comparator Ring A/B already use — so "coherent" cannot mean two things
    across the ring platform. Scoped to :data:`RENDERER_LANES_CONF_D`, which is
    the attach authority for a renderer lane.
    """
    from jasper.ring_assets import ring_header_matches_conf

    return ring_header_matches_conf(
        renderer_ring_path(label),
        ring_conf_pcm_name(label),
        conf_d=conf_d or RENDERER_LANES_CONF_D,
    )


def delete_stale_ring(label: str, *, conf_d: str | None = None) -> str | None:
    """Delete this lane's ring file when its header cannot attach. Best-effort.

    Mirrors Ring A/B's `_delete_stale_ring_files` for renderer lanes, and exists
    for the same reason: a ring file left over from a PRIOR geometry is a
    create-or-attach ERROR, not something either end recovers from. The renderer
    would fail its `snd_pcm_open` and the lane would be silent until someone ran
    `rm` by hand — so a re-arm after any geometry change would be a one-shot
    lever, exactly the trap the format axis sprang on Ring A's rollback.

    Safe because these files are pure transport state on tmpfs, recreated by
    whichever end opens next — never user data. A header that is absent, invalid
    (magic-less, which the writer reclaims itself), or COHERENT is left
    untouched.

    The COHERENT early-return means a DISARMED lane's geometry-matching ring
    lingers in tmpfs. Benign, and deliberate: ~16.5 KB, cleared by reboot,
    and a re-arm at the same geometry re-attaches with read_seq resynced by
    the attach protocol — no stale slot replays. A geometry CHANGE across
    the disarm/re-arm is exactly what the comparator above does catch.

    Returns a human-readable reason when it deleted something, else ``None``.
    """
    verdict = stale_ring_verdict(label, conf_d=conf_d)
    if not verdict.present or verdict.ok:
        return None
    path = renderer_ring_path(label)
    try:
        os.unlink(path)
    except OSError as e:
        # The writer's own attach error is the backstop; never raise from a
        # best-effort self-heal. But a delete that FAILED is worth a line: the
        # lane will keep refusing its attach and nothing else says why.
        log_event(
            _LOG,
            "renderer_lane.stale_ring_delete_failed",
            level=logging.WARNING,
            label=label,
            path=path,
            errno=getattr(e, "errno", "?"),
            detail=str(e),
        )
        return None
    # DELIBERATELY unlinks the ring FILE ONLY.
    #
    # NOT `<path>.open.lock`, and NOT `<path>.writer.lock`. The named precedent
    # (`_delete_stale_ring_files`) unlinks only the ring for the same reason:
    # those two files are the cross-language MUTEXES that make a concurrent
    # create-or-attach safe, and deleting one opens an inode-tear window — a
    # holder keeps its flock on the now-unlinked inode while the next opener
    # creates a fresh file and locks THAT, so two processes hold "exclusive"
    # locks on different inodes and neither excludes the other. It buys nothing:
    # a lock file carries no geometry, so it is never the thing that is stale.
    # A ring from a prior geometry cannot attach; cleared so the next open
    # recreates it.
    log_event(
        _LOG,
        "renderer_lane.stale_ring_cleared",
        level=logging.WARNING,
        label=label,
        path=path,
        axis=verdict.axis or "geometry",
        detail=verdict.detail or "",
    )
    axis = verdict.axis or "geometry"
    return f"{axis} mismatch{f': {verdict.detail}' if verdict.detail else ''}"


def ring_writer_pid(label: str) -> int | None:
    """The pid stamped in this lane's ring header as its WRITER, if any.

    Reads the ``jts_ring`` header's ``writer_pid`` field directly — a fixed
    little-endian ``u64`` at byte offset 56 of the 128-byte header, the layout
    ``rust/jasper-ring/src/layout.rs`` pins as ``OFF_WRITER_PID``. Returns
    ``None`` when the ring does not exist, is too short, or names no writer.

    Why read the header rather than ask something: the doctor's renderer probe
    needs to know whether an EBUSY came from the LEGITIMATE owner (the renderer
    that is supposed to hold this ring) or from something else, and the header
    is the only place that fact exists. It is the ring's exact analogue of the
    ``/proc/asound/.../status`` ``owner_pid`` read the aloop lanes use.
    """
    try:
        with open(renderer_ring_path(label), "rb") as fh:
            header = fh.read(128)
    except OSError:
        return None
    if len(header) < 64:
        return None
    pid = int.from_bytes(header[56:64], "little")
    return pid or None
