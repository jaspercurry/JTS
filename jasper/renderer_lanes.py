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
* ``JASPER_<RENDERER>_DEVICE`` — read by the renderer unit's ``ExecStart``;
  selects the ALSA PCM it writes.

Writing them together is what makes a half-flip unrepresentable. Neither end
is restarted by the write: the values take effect on each unit's next start,
so a deploy (which bounces both) or an explicit restart pair is the arm, and
there is no window in which one end has moved and the other has not.

Why the CamillaDSP coupling is deliberately NOT an input
--------------------------------------------------------

It would be natural to key this on ``JASPER_FANIN_CAMILLA_COUPLING`` — a
ring-coupled box gets ring renderers. That is wrong on both correctness and
safety grounds:

* **They are independent transports.** A renderer ring carries
  renderer → fan-in; the coupling describes fan-in → CamillaDSP. A
  loopback-coupled box can ring-ingress a renderer and a ring-coupled box can
  keep aloop renderers; neither combination is incoherent. The only real
  precondition is the ring PLATFORM (the ioplug ``.so`` and
  ``/dev/shm/jts-ring``), which ships on every box, and which
  :func:`arm_refusal_reason` checks at arm time.
* **Keying on the coupling would arm the fleet by deploy.** Every
  ring-coupled box would flip its renderer the moment this code landed,
  with no per-box source pass — the opposite of how every other per-box
  arming in this campaign works (the active ring is explicit-CLI-only, and
  ring width activation is per box and owner-gated).

So membership is explicit and per box, and ``jasper-fanin`` consults nothing
else either (see ``Config::lane_is_renderer_ring``).
"""

from __future__ import annotations

from dataclasses import dataclass

from jasper.atomic_io import atomic_write_text

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


@dataclass(frozen=True)
class RendererLane:
    """One migratable renderer lane.

    Holds every per-lane fact the flip needs, so adding the next lane (P6b
    bluealsa, P6c correction, P6d shairport) is one entry here plus its conf.d
    block and unit edit — not a new rule.
    """

    #: The fan-in lane LABEL. This is the identity fan-in already has for the
    #: lane, and the identity the ring path derives from; it is NOT always the
    #: renderer's name (librespot's lane is labelled ``spotify``).
    label: str
    #: Human name of the renderer that writes this lane, for messages.
    renderer: str
    #: The systemd unit whose ``ExecStart`` reads :attr:`device_key`.
    unit: str
    #: The env key the renderer unit substitutes into ``--device``.
    device_key: str
    #: The ALSA PCM name the renderer writes on an UNARMED box — the shipped
    #: snd-aloop path, which stays defined until P9 because it is the
    #: not-yet-migrated boxes' path, not a fallback mode.
    aloop_device: str
    #: The ALSA PCM name the renderer writes on an ARMED box: a ``plug:``
    #: wrapper over this lane's ``jts_ring`` PCM, so the renderer's native rate
    #: still converts through ``defaults.pcm.rate_converter``.
    ring_device: str


#: Every lane this mechanism knows about. P6a migrates Spotify only; the
#: remaining three rows land with their own PRs (and their own on-box source
#: passes), so they are deliberately absent rather than pre-declared.
RENDERER_LANES: tuple[RendererLane, ...] = (
    RendererLane(
        label="spotify",
        renderer="librespot",
        unit="librespot.service",
        device_key="JASPER_LIBRESPOT_DEVICE",
        aloop_device="librespot_substream",
        ring_device="librespot_ring_lane",
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


def parse_armed_labels(value: str | None) -> tuple[str, ...]:
    """Parse the armed-lane env value into labels.

    Whitespace is trimmed and empty entries are dropped, so ``None``, ``""``,
    ``" "`` and ``",,"`` all mean "nothing armed" — the fail-safe direction for
    a key whose empty value must never be read as "arm everything". Order is
    preserved and duplicates are NOT collapsed, matching fan-in's own
    ``env_csv_labels`` so the two parsers cannot disagree about what a
    malformed value means.
    """
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


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
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    found: str | None = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if name.strip() == key:
            # Last assignment wins, matching systemd's EnvironmentFile.
            found = value.strip()
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
        "# lane are derived from it together so a half-flip cannot be expressed.",
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


def arm_refusal_reason(
    label: str,
    *,
    assets_present: bool,
    missing_assets: tuple[str, ...] = (),
) -> str | None:
    """Why ``label`` may not be armed right now, or ``None`` if it may.

    The only precondition is the ring PLATFORM: the compiled ioplug and the
    ``/dev/shm/jts-ring`` directory. Without them the renderer's ``jts_ring``
    PCM cannot resolve, so arming would silence that source with a message
    nobody would connect to this action — fail closed here instead.

    Takes the presence answer as an argument rather than probing, so the
    decision is testable without a Pi and so the caller reads
    :func:`jasper.ring_assets.ring_asset_presence` once for both this and its
    own reporting.
    """
    if lane_by_label(label) is None:
        return f"unknown lane {label!r} (known: {', '.join(MIGRATABLE_LABELS)})"
    if not assets_present:
        detail = ", ".join(missing_assets) if missing_assets else "ring platform"
        return (
            f"ring platform assets missing ({detail}) — arming would leave the "
            "renderer unable to resolve its ring PCM; redeploy to install them"
        )
    return None


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
