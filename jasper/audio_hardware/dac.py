# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Static DAC profile registry for JTS output hardware.

This module is deliberately IO-free. It describes known output hardware
capabilities and quirks; it does not probe ALSA, read env files, render
system config, or restart services. Runtime ownership stays with
``jasper.output_topology``, ``jasper.output_hardware`` once landed,
``jasper-audio-hardware-reconcile``, and ``jasper-outputd``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from jasper.camilla_config_contract import CamillaFloor

from .hat_eeprom import HatEeprom


APPLE_USB_C_DONGLE_ID = "apple_usb_c_dongle"
HIFIBERRY_DAC8X_ID = "hifiberry_dac8x"
HIFIBERRY_DAC8X_STUDIO_ID = "hifiberry_dac8x_studio"
INNOMAKER_HIFI_AMP_PRO_ID = "innomaker_hifi_amp_pro"
DUAL_APPLE_USB_C_DAC_4CH_ID = "dual_apple_usb_c_dac_4ch"

DAC8X_OUTPUTD_STABILITY_PROFILE = "hifiberry_dac8x_outputd_stability"

DacKind = Literal["single", "composite"]
DacConnection = Literal["usb", "i2s"]
ClockDomainContract = Literal[
    "single_device",
    "independent",
    "measured_sync_required",
]
ChipAecQualification = Literal["approved", "needs_calibration"]
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")


@dataclass(frozen=True)
class MixerControl:
    """A mixer control policy a runtime owner may enforce.

    The registry only declares intent. Scripts such as
    ``jasper-dac-init`` and ``jasper-headphone-monitor`` remain the
    components that actually apply or monitor mixer state.

    Exactly one target is declared, and the target KIND also picks the ALSA
    namespace ``name`` is resolved in — the two are not independent knobs.
    ``target_percent`` (with optional ``unmute``) names a SIMPLE mixer
    element, the alsa-lib abstraction ``amixer sset`` addresses, whose name is
    the kcontrol name minus its " Playback Volume"/" Playback Switch" suffix.
    ``target_db`` and ``target_enum`` name a raw kcontrol exactly as the
    driver declares it, which is what ``amixer cget``/``cset name=`` and
    ``amixer contents`` use; the dB target is converted to the control's own
    index through the TLV the control publishes, so no scale data is
    duplicated here.
    """

    name: str
    target_percent: int | None = None
    target_db: float | None = None
    target_enum: str | None = None
    unmute: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("mixer control name is required")
        declared = [
            target
            for target in (self.target_percent, self.target_db, self.target_enum)
            if target is not None
        ]
        if len(declared) != 1:
            raise ValueError(
                f"{self.name}: exactly one mixer target must be declared"
            )
        if self.target_percent is not None and not 0 <= self.target_percent <= 100:
            raise ValueError("mixer target_percent must be 0..100")
        if self.unmute and self.target_percent is None:
            raise ValueError(
                f"{self.name}: unmute is a simple-mixer switch and only rides "
                "with target_percent"
            )


@dataclass(frozen=True)
class LatencyFloor:
    """A DAC's measured stable outputd buffer floor.

    The lowest jasper-outputd period / DAC-buffer pair a board runs xrun-free,
    captured as DATA on the profile so a fresh box reproduces it with no
    per-user config. CamillaDSP's own buffering is the separate
    :class:`~jasper.camilla_config_contract.CamillaFloor`: it crosses the ring,
    not the DAC.
    """

    outputd_period_frames: int
    outputd_dac_buffer_frames: int

    def __post_init__(self) -> None:
        for name, value in (
            ("outputd_period_frames", self.outputd_period_frames),
            ("outputd_dac_buffer_frames", self.outputd_dac_buffer_frames),
        ):
            if value <= 0:
                raise ValueError(f"{name} must be > 0, got {value}")
        # The DAC ring must hold at least two periods so the writer is never
        # one short read from an underrun (mirrors outputd's own min-buffer
        # guard in rust/jasper-outputd/src/config.rs).
        if self.outputd_dac_buffer_frames < 2 * self.outputd_period_frames:
            raise ValueError(
                "outputd_dac_buffer_frames must be >= 2 x outputd_period_frames "
                f"({2 * self.outputd_period_frames}), got "
                f"{self.outputd_dac_buffer_frames}"
            )


@dataclass(frozen=True)
class ChannelMapEntry:
    """One CamillaDSP-output → physical-DAC-channel routing hop for the active lane.

    Pure routing: "CamillaDSP active-output channel ``camilla_out_index`` drives
    physical DAC channel ``physical_dac_channel``." It carries **no gain** —
    CamillaDSP owns the gain stage — so a `dac_channel_map` is a permutation, not
    a mixer. This keeps lane→pin assignment as declarative data the transport
    reads rather than a per-DAC code branch.
    """

    camilla_out_index: int
    physical_dac_channel: int

    def __post_init__(self) -> None:
        if self.camilla_out_index < 0:
            raise ValueError("camilla_out_index must be >= 0")
        if self.physical_dac_channel < 0:
            raise ValueError("physical_dac_channel must be >= 0")


@dataclass(frozen=True)
class DacProfile:
    """One supported final-output DAC shape.

    ``supported_card_matches`` are case-insensitive regex fragments used
    by detector/reconciler code to recognize ALSA card listings. They
    are data hints, not active probes.
    """

    id: str
    label: str
    kind: DacKind
    physical_output_count: int
    coherent_clock_domain: bool
    clock_domain_label: str
    clock_domain_contract: ClockDomainContract
    outputd_sink: str
    supported_card_matches: tuple[str, ...]
    # Card labels a driver emits for MORE THAN ONE board, routable only when
    # the fitted HAT's EEPROM product is one of `hat_products`. Kept apart
    # from `supported_card_matches` so the label-only path can never route on
    # an ambiguous name (#2258).
    eeprom_gated_card_matches: tuple[str, ...] = ()
    # HAT ID EEPROM `product` strings this profile accepts, compared whole and
    # case-insensitively — a substring test would let "StudioDAC8xPro" satisfy
    # "StudioDAC8x".
    hat_products: tuple[str, ...] = ()
    # Physical host interface consumed by the final-output DAC. USB-role
    # resolution reads this declaration; it must not infer I2S from the
    # temporary absence of a USB device.
    connection: DacConnection = "usb"
    usb_ids: tuple[str, ...] = ()
    child_profile_ids: tuple[str, ...] = ()
    requires_same_usb_bus: bool = False
    supports_active_outputd_lane: bool = False
    active_outputd_lane_channels: int | None = None
    supports_active_crossover_commissioning: bool = False
    dac_channel_map: tuple[ChannelMapEntry, ...] | None = None
    mixer_controls: tuple[MixerControl, ...] = ()
    validation_profile: str | None = None
    chip_aec_qualification: ChipAecQualification = "needs_calibration"
    chip_aec_detail: str = ""
    udev_rule: str | None = None
    dtoverlay: str | None = None
    # The DECLARED sample format the DAC's hw device should open at, at the
    # final ALSA edge, and it is now what outputd ASKS ALSA for.
    # jasper-audio-hardware-reconcile shells into final_edge_format_for() to
    # emit this as JASPER_OUTPUTD_DAC_FORMAT, which jasper-outputd READS: it
    # accepts exactly {S16_LE, S24_3LE, S32_LE} and parks at exit 78 otherwise,
    # requests that format on its DAC PCM, and reports what its client edge
    # negotiated as STATUS dac.format, which the chip-AEC alignment identity
    # records for forensics only — ADR-0190 excludes it from comparison, so
    # changing a profile's declared format never nags a fleet's disclosure.
    # A declaration the hardware cannot install now parks the speaker instead
    # of being silently converted, so this field is load-bearing.
    #
    # S24_3LE is 24 bits in three PACKED bytes — not ALSA's 4-byte-word S24_LE,
    # which is NOT accepted. outputd carries it on the coherent single-DAC sink
    # only: `AlsaBackend::write_dac_period`'s packed arm stages bytes and writes
    # through `io_bytes()`. The paired COMPOSITE sink has no packed-24 child
    # write path, so `ChildPeriods::new` refuses an S24_3LE child edge and parks
    # the unit at EX_CONFIG 78 before any PCM opens (#2249).
    #
    # That capability gap is why a composite profile and a child profile may
    # declare DIFFERENT widths, and why that divergence is safe rather than a
    # registry lie: this value is read by id, off whichever profile is ARMED
    # (final_edge_format_for -> by_id, emitted once as JASPER_OUTPUTD_DAC_FORMAT),
    # so a composite's own declaration is what outputd asks BOTH its children
    # for — a child profile's declaration is never consulted while the composite
    # is armed, and vice versa. The invariant that survives is narrower than
    # equality: no composite declares a width its transport refuses. Pinned by
    # test_a_composite_never_declares_a_width_its_transport_refuses.
    final_edge_format: str = "S16_LE"
    # The DAC's measured stable buffer floor, or None to use the global
    # default (non-breaking: an undeclared DAC keeps shipping the conservative
    # global default). The reconciler emits this profile floor into the
    # wizard-owned env, so a fresh box reproduces the tuned floor with no
    # per-user config (#27).
    latency_floor: LatencyFloor | None = None
    # CamillaDSP's floor, measured on a box fitted with this DAC. A board with
    # no measurement declares none and takes the transport default.
    camilla_floor: CamillaFloor | None = None

    def __post_init__(self) -> None:
        if not _ID_RE.match(self.id):
            raise ValueError(f"unsupported DAC profile id: {self.id!r}")
        if not self.label.strip():
            raise ValueError(f"{self.id}: label is required")
        if self.kind not in ("single", "composite"):
            raise ValueError(f"{self.id}: unsupported kind {self.kind!r}")
        if self.connection not in ("usb", "i2s"):
            raise ValueError(
                f"{self.id}: unsupported DAC connection {self.connection!r}"
            )
        if self.connection == "usb" and self.dtoverlay:
            raise ValueError(
                f"{self.id}: USB DAC profiles cannot declare dtoverlay"
            )
        if self.connection == "i2s" and not self.dtoverlay:
            raise ValueError(
                f"{self.id}: I2S DAC profiles must declare dtoverlay"
            )
        if self.connection == "i2s" and self.usb_ids:
            raise ValueError(
                f"{self.id}: I2S DAC profiles cannot declare usb_ids"
            )
        if self.physical_output_count < 0:
            raise ValueError(f"{self.id}: physical_output_count must be >= 0")
        if not self.clock_domain_label.strip():
            raise ValueError(f"{self.id}: clock_domain_label is required")
        if self.clock_domain_contract not in (
            "single_device",
            "independent",
            "measured_sync_required",
        ):
            raise ValueError(
                f"{self.id}: unsupported clock_domain_contract "
                f"{self.clock_domain_contract!r}"
            )
        if self.chip_aec_qualification not in ("approved", "needs_calibration"):
            raise ValueError(
                f"{self.id}: unsupported chip_aec_qualification "
                f"{self.chip_aec_qualification!r}"
            )
        if self.final_edge_format not in ("S16_LE", "S24_3LE", "S32_LE"):
            raise ValueError(
                f"{self.id}: unsupported final_edge_format {self.final_edge_format!r}"
            )
        if not self.outputd_sink.strip():
            raise ValueError(f"{self.id}: outputd_sink is required")
        if not self.supported_card_matches and not self.child_profile_ids:
            raise ValueError(
                f"{self.id}: supported_card_matches or child_profile_ids required"
            )
        for pattern in self.supported_card_matches:
            re.compile(pattern, re.IGNORECASE)
        for pattern in self.eeprom_gated_card_matches:
            re.compile(pattern, re.IGNORECASE)
        if self.eeprom_gated_card_matches and not self.hat_products:
            raise ValueError(
                f"{self.id}: eeprom_gated_card_matches requires hat_products"
            )
        if self.kind == "single" and self.child_profile_ids:
            raise ValueError(f"{self.id}: single DAC profile cannot have children")
        if self.kind == "composite" and len(self.child_profile_ids) < 2:
            raise ValueError(f"{self.id}: composite DAC profile needs children")
        if self.kind == "composite" and self.mixer_controls:
            raise ValueError(
                f"{self.id}: composite mixer controls must come from children"
            )
        if self.requires_same_usb_bus and self.kind != "composite":
            raise ValueError(f"{self.id}: same-bus requirement only fits composites")
        if self.kind == "single" and self.clock_domain_contract != "single_device":
            raise ValueError(
                f"{self.id}: single DAC profiles use single_device clock contract"
            )
        if self.kind == "composite" and self.clock_domain_contract == "single_device":
            raise ValueError(
                f"{self.id}: composite DAC profile cannot use single_device "
                "clock contract"
            )
        if self.coherent_clock_domain and self.clock_domain_contract != "single_device":
            raise ValueError(
                f"{self.id}: coherent_clock_domain only describes single-device "
                "clock domains"
            )
        if self.supports_active_outputd_lane:
            if self.active_outputd_lane_channels is None:
                raise ValueError(
                    f"{self.id}: active_outputd_lane_channels is required when "
                    "supports_active_outputd_lane is true"
                )
            if self.active_outputd_lane_channels <= 0:
                raise ValueError(
                    f"{self.id}: active_outputd_lane_channels must be > 0"
                )
            if self.active_outputd_lane_channels > self.physical_output_count:
                raise ValueError(
                    f"{self.id}: active_outputd_lane_channels cannot exceed "
                    "physical_output_count"
                )
        elif self.active_outputd_lane_channels is not None:
            raise ValueError(
                f"{self.id}: active_outputd_lane_channels requires "
                "supports_active_outputd_lane"
            )
        if self.supports_active_crossover_commissioning and not (
            self.is_coherent_single() and self.supports_active_outputd_lane
        ):
            raise ValueError(
                f"{self.id}: active crossover commissioning requires one "
                "coherent active-output device"
            )
        if self.dac_channel_map is not None:
            # The channel map routes the active lane; it only means something
            # for a DAC that has one. Validate it is a clean permutation of the
            # transport width onto distinct, in-range physical channels — a
            # malformed map is fail-closed at import, before any deploy.
            if not self.supports_active_outputd_lane:
                raise ValueError(
                    f"{self.id}: dac_channel_map requires supports_active_outputd_lane"
                )
            width = self.active_outputd_lane_channels
            if len(self.dac_channel_map) != width:
                raise ValueError(
                    f"{self.id}: dac_channel_map needs one entry per active-lane "
                    f"channel ({width}), got {len(self.dac_channel_map)}"
                )
            camilla_indexes = sorted(e.camilla_out_index for e in self.dac_channel_map)
            if camilla_indexes != list(range(width)):
                raise ValueError(
                    f"{self.id}: dac_channel_map camilla_out_index values must be "
                    f"exactly 0..{width - 1} with no gaps or duplicates"
                )
            physical = [e.physical_dac_channel for e in self.dac_channel_map]
            if len(set(physical)) != len(physical):
                raise ValueError(
                    f"{self.id}: dac_channel_map maps two lanes to the same "
                    "physical_dac_channel"
                )
            for channel in physical:
                if channel >= self.physical_output_count:
                    raise ValueError(
                        f"{self.id}: dac_channel_map physical_dac_channel {channel} "
                        f"exceeds physical_output_count {self.physical_output_count}"
                    )

    def is_coherent_single(self) -> bool:
        """True when this is one device on a single coherent clock domain.

        The shape that takes the simple single-PCM transport: one ALSA device,
        one clock, no inter-device drift correction. Folds the
        ``kind == "single" and coherent_clock_domain`` check that active-route
        resolution would otherwise inline.
        """

        return self.kind == "single" and self.coherent_clock_domain


APPLE_HEADPHONE_CONTROL = MixerControl(
    name="Headphone",
    target_percent=100,
    unmute=True,
)

# The Studio driver (sound/soc/bcm/hifiberry_studio_dac8x.c) exposes a hardware
# gain stage and writes NO defaults into it: the level after a boot is whatever
# the board's MCU happens to hold, and "Master Playback Volume" reaches +24 dB.
# JTS owns gain in CamillaDSP, so this profile pins its stages at unity and
# unmuted. Names are the driver's kcontrol names verbatim (what
# `amixer -c0 contents` prints). The driver registers one Output Ch control per
# output channel the board's EEPROM reports, so these eight are this profile's
# board, not a driver constant.
HIFIBERRY_STUDIO_MIXER_CONTROLS = (
    MixerControl(name="Master Playback Volume", target_db=0.0),
    *(
        MixerControl(name=f"Output Ch{channel} Playback Volume", target_db=0.0)
        for channel in range(8)
    ),
    MixerControl(name="DAC Mute", target_enum="unmuted"),
)

APPLE_USB_C_DONGLE = DacProfile(
    id=APPLE_USB_C_DONGLE_ID,
    label="Apple USB-C audio adapter",
    kind="single",
    physical_output_count=2,
    coherent_clock_domain=True,
    clock_domain_label="Single Apple USB audio device clock",
    clock_domain_contract="single_device",
    outputd_sink="alsa",
    supported_card_matches=("usb-c to 3.5mm",),
    usb_ids=("05ac:110a",),
    mixer_controls=(APPLE_HEADPHONE_CONTROL,),
    # A single Apple dongle can carry a mono active 2-way graph over the same
    # width-aware single-ALSA active lane used by wider coherent DACs.
    supports_active_outputd_lane=True,
    active_outputd_lane_channels=2,
    chip_aec_qualification="approved",
    chip_aec_detail="Apple USB-C dongle is the measured known-good chip-AEC baseline",
    udev_rule="deploy/udev/99-jasper-apple-dongle.rules",
    # Measured stable floor on Apple-dongle lab boxes: CamillaDSP chunk 256 /
    # target 1536, outputd period 128 / dac_buffer 256. The exact 4x Camilla
    # target (1024) caused USB bridge playback xruns on jts.local under the
    # usb_low_latency_48k path; outputd period 64 / dac_buffer 128 also produced
    # bridge xruns. Keep the Camilla cushion and the 128-frame outputd period.
    latency_floor=LatencyFloor(
        outputd_period_frames=128,
        outputd_dac_buffer_frames=256,
    ),
    camilla_floor=CamillaFloor(chunksize=256, target_level=1536),
    # Hardware evidence: on jts.local's Apple dongle,
    # `aplay -D hw:A --dump-hw-params` reports FORMAT `S16_LE S24_3LE` at
    # CHANNELS 2 / RATE 48000 — the device advertises exactly two widths and
    # S24_3LE is the wider — and a live `aplay -D hw:A -f S24_3LE -c 2 -r 48000`
    # open succeeded, with outputd stopped for the probe and recovering active
    # afterwards at zero DAC xruns (banked 2026-08-08, wide-output-path PR-8 b3).
    # The dongle exposes no 32-bit width at all, so S24_3LE is the widest edge
    # this silicon has: declaring it moves outputd's i32 program spine to within
    # 8 bits of the wire instead of 16, quantizing once to 24 significant bits
    # (round-to-nearest, saturating) rather than to 16.
    #
    # This declaration governs a SINGLE armed dongle only. The dual-dongle
    # composite that lists this profile as its child declares its own width and
    # stays where its transport can drive it — see DUAL_APPLE_USB_C_DAC_4CH.
    #
    # Consequence: `AlignmentIdentity.output_format` records this field for
    # forensics only — ADR-0190 excludes it from comparison, so it never
    # diverges or nags a fleet holding the old S16_LE edge.
    final_edge_format="S24_3LE",
)

HIFIBERRY_DAC8X = DacProfile(
    id=HIFIBERRY_DAC8X_ID,
    label="HiFiBerry DAC8x",
    kind="single",
    physical_output_count=8,
    coherent_clock_domain=True,
    clock_domain_label="Single HiFiBerry DAC8x device clock",
    clock_domain_contract="single_device",
    outputd_sink="alsa",
    connection="i2s",
    # Exactly the one string the kernel emits for this board, and nothing
    # fuzzier. `rpi-simple-soundcard.c` binds compatible
    # `hifiberry,hifiberry-dac8x` to `drvdata_hifiberry_dac8x`, whose
    # `.card_name` is this literal; the ADC8x-detected path renames only the
    # DAI, never the card. So no kernel produces a bare "DAC8x" or a loose
    # "hifiberry … dac8x" card label for this profile.
    #
    # The previous family patterns (`hifiberry.*dac8x`, `\bdac8x\b`) matched
    # labels no driver emits while swallowing every real Studio label: a
    # trailing `(?!.*studio)` only excludes "studio" AFTER the match, and the
    # kernel puts "Studio" BEFORE "DAC8x" ("HiFiBerry Studio DAC8x"). That
    # ordering mismatch is what routed real Studio silicon into this profile
    # and silently handed it this row's `chip_aec_qualification="approved"`
    # and `final_edge_format="S32_LE"` (#2250). Narrowing to the emitted
    # literal removes the whole fuzzy-family class rather than adding a
    # second lookahead to it.
    supported_card_matches=(r"\bsnd_rpi_hifiberry_dac8x\b",),
    # The DAC-agnostic active-output transport (Stage 1) can now carry a
    # coherent single DAC of any width, so the 8-channel DAC8x rides the
    # active-crossover lane end-to-end. The transport builds an identity
    # channel map when dac_channel_map is None (one coherent clock domain,
    # no permutation needed). Width is DATA, not a per-DAC code branch.
    supports_active_outputd_lane=True,
    active_outputd_lane_channels=8,
    supports_active_crossover_commissioning=True,
    validation_profile=DAC8X_OUTPUTD_STABILITY_PROFILE,
    chip_aec_qualification="approved",
    chip_aec_detail=(
        "HiFiBerry DAC8x is a measured chip-AEC profile: jts3, Studio "
        "silicon under the base overlay/driver, per HiFiBerry's datasheet"
    ),
    dtoverlay="hifiberry-dac8x",
    # This row keys on driver stack (overlay -> driver -> card label), not on
    # silicon identity: HiFiBerry's own datasheet prescribes this overlay for
    # DAC8x Studio boards too, so the evidence below is Studio silicon running
    # the base driver, per that datasheet — see ADR-0232.
    #
    # Hardware evidence: the same four values the Apple dongle declares, here
    # measured on I2S silicon rather than transferred. A three-window jts3 soak
    # (2026-08-11; operator-local record `captures/r7-jts3-20260811T051852Z/`,
    # untracked like every capture) ran 30 minutes at the shipped global
    # default (Camilla 1024/2048, outputd 1024/3072), 30 at Camilla 256/1536
    # alone, then 30 at the full floor, on the live active 2-way with real
    # program material. Every window: zero DAC xruns, zero CamillaDSP clipped
    # samples, zero DAC-clock unlock and zero fan-in xrun delta. The content
    # lane's counters at the full floor were indistinguishable from the
    # baseline window's own rate (1 xrun / 2 empty / <=2 partial / 1 eagain per
    # 30 minutes, against this box's ~3.4 content-xruns/hour steady state), so
    # the 128-frame period costs the content capture nothing measurable here
    # even though it multiplies that lane's wakeups. DAC presentation latency
    # 63.833 ms -> 5.167 ms, a 58.67 ms reduction.
    #
    # The (256, 1536) pair keeps a 6x cushion instead of the validator's 4x
    # minimum, and this profile DECLINES TO RE-TEST the exact-4x (256, 1024)
    # pair rather than claiming it would fail here: the recorded 1024 failure
    # is the Apple profile's USB bridge playback xruns, which is transport-
    # specific evidence about a USB path and not an I2S result. It transfers as
    # caution — a reason not to spend a soak window probing downward — not as
    # evidence about this board.
    #
    # Consequence: this also shifts jts3's corpus-mode chip-AEC alignment
    # ~58.7 ms, since the chip's reference geometry (16 kHz, 128/256) is
    # untouched while the DAC leg's presentation latency moves — corpus mode
    # has no alignment check, so its fixed JASPER_AEC_CORPUS_CHIP_SYS_DELAY
    # (picked at the old geometry) needs re-deriving. Tracked as #2327.
    latency_floor=LatencyFloor(
        outputd_period_frames=128,
        outputd_dac_buffer_frames=256,
    ),
    camilla_floor=CamillaFloor(chunksize=256, target_level=1536),
    # Hardware evidence: `aplay --dump-hw-params` on jts3 — Studio silicon
    # under this base overlay/driver, see ADR-0232 — reports FORMAT
    # S16_LE/S24_LE/S32_LE at rates up to 192 kHz, and a raw `hw:` S32_LE
    # 2ch open succeeded with a clean recovery. The DAC8x uses four
    # 192kHz/24-bit Burr-Brown DAC chips (HiFiBerry's published
    # datasheet); the S32_LE word's bottom byte beyond that 24-bit
    # resolution spans <= -138.5 dBFS — sub-analog at any plausible silicon
    # depth, so this datasheet inference is not load-bearing for safety even
    # if the chip's real resolution differs from spec. What the probe did NOT
    # cover is width: this profile's CAPABILITY is 8 channels
    # (`active_outputd_lane_channels` above), while the one lab box on it runs
    # a 2-channel active 2-way (`JASPER_OUTPUTD_ACTIVE_CHANNELS=2`), so no
    # channel count above 2 has been paired with S32_LE on this silicon. That
    # pairing fails closed at the ALSA open rather than being pre-verified: if
    # (S32_LE, 8ch) turns out not to be jointly satisfiable, outputd parks at
    # exit 78 instead of converting silently.
    #
    # Declaring S32_LE here lets outputd's i32 program spine reach the DAC
    # edge with zero narrowing (wide-output-path PR-7) — the intended fix
    # for the horn-lane undithered-16-bit-requantization crackle (acoustic
    # verdict pending the conductor's post-merge listen).
    #
    # Consequence: `AlignmentIdentity.output_format` records this field for
    # forensics only — ADR-0190 excludes it from comparison, so it never
    # diverges or nags a fleet holding the old S16_LE edge. jts3, the lab box
    # on this profile, reaches chip-AEC through the corpus escape
    # (JASPER_AEC_CORPUS_CHIP_AEC_ENABLED=1) rather than a commissioned
    # artifact at all.
    final_edge_format="S32_LE",
)

HIFIBERRY_DAC8X_STUDIO = DacProfile(
    id=HIFIBERRY_DAC8X_STUDIO_ID,
    label="HiFiBerry DAC8x Studio",
    kind="single",
    physical_output_count=8,
    coherent_clock_domain=True,
    clock_domain_label="Single HiFiBerry DAC8x Studio device clock",
    clock_domain_contract="single_device",
    outputd_sink="alsa",
    connection="i2s",
    # Both token orders, because the kernel and HiFiBerry disagree on it: the
    # driver emits "HiFiBerry Studio DAC8x" (studio first) while HiFiBerry's
    # own product naming is "DAC8x Studio". Deliberately NOT `\b`-bounded
    # around the tokens, so the run-together slug forms both match
    # ("StudioDAC8x", "DAC8XStudio") — #2250's own scope bullet called out
    # covering the "StudioDAC8x" slug form explicitly. (The ALSA card-id's
    # own alphanumeric-stripped, length-capped shorthand — "HiFiBerryStudio"
    # in this file's own test fixture — is a DIFFERENT, shorter string that
    # matches neither pattern; it needs no accommodation here because the
    # joined `/proc/asound/cards` line carries the full "HiFiBerry Studio
    # DAC8x" long name alongside it, which these patterns already catch.)
    #
    # `(?!.*pro)` excludes the Studio DAC8x PRO on purpose, and is
    # deliberately NOT `\b`-bounded either: a `\b`-bounded `(?!.*\bpro\b)`
    # cannot see "pro" in the run-together slug form "StudioDAC8xPro" — "x"
    # and "P" are both word characters, so no boundary exists between them,
    # and the exclusion silently failed to fire for exactly the slug shape
    # this profile deliberately matches. The Pro is a different board with
    # its own overlay (`hifiberry-studio-dac8x-pro`) and the OPPOSITE clock
    # role — its overlay targets `i2s_clk_consumer` and sets `clk-provider`,
    # so the CARD drives the I2S clocks rather than the Pi. Matching it here
    # would hand it this row's clock-domain contract and overlay, which is
    # the same silent-inheritance defect as #2250 one board over. No Pro
    # exists in the fleet, so it routes to "unknown" and parks loudly
    # instead of being quietly approximated.
    supported_card_matches=(
        r"^(?!.*pro).*studio.*dac8x",
        r"^(?!.*pro).*dac8x.*studio",
    ),
    # rpi-6.18.y's `hifiberry_studio.c` names EVERY Studio-family card
    # "Hifiberry Studio Soundcard" — the DAC8x token and the width are both
    # gone from the label, so a 2-channel Studio Digi presents the same string
    # as this 8-channel board. That name is claimable only alongside the HAT
    # EEPROM product below, which does carry the board identity; with no
    # EEPROM it stays unroutable and the speaker parks (#2258).
    eeprom_gated_card_matches=(r"^(?!.*pro).*hifiberry.*studio.*soundcard",),
    hat_products=("StudioDAC8x",),
    # Same active-lane shape as the base DAC8x: a coherent 8-channel single
    # device on the DAC-agnostic transport (Stage 1). dac_channel_map None =>
    # identity map.
    supports_active_outputd_lane=True,
    active_outputd_lane_channels=8,
    validation_profile=DAC8X_OUTPUTD_STABILITY_PROFILE,
    chip_aec_detail=(
        "HiFiBerry DAC8x Studio needs per-profile chip-AEC timing "
        "calibration before arming production chip AEC"
    ),
    # The Studio has its OWN overlay — it does not share the base DAC8x's.
    # `hifiberry-studio-dac8x-overlay.dts` binds compatible
    # `hifiberry,hifiberry-studio-dac8x` to a dedicated machine driver
    # (`sound/soc/bcm/hifiberry_studio_dac8x.c`), both added to
    # raspberrypi/linux on 2026-01-15. HiFiBerry's own StudioDAC8x datasheet
    # still prints `dtoverlay=hifiberry-dac8x`; it predates that support, and
    # the kernel is the authority for what a board actually presents.
    #
    # `render_i2s_hat_boot_config` can manage this overlay too (any
    # `connection == "i2s"` profile is eligible; the per-box intent file
    # picks one, explicit opt-in only). It also feeds
    # `configured_i2s_overlays()`, the registered-overlay set USB port-role
    # resolution intersects config.txt against — so with the wrong value a
    # correctly-configured Studio box read as "no I2S HAT present".
    dtoverlay="hifiberry-studio-dac8x",
    mixer_controls=HIFIBERRY_STUDIO_MIXER_CONTROLS,
    # NOT flipped to S32_LE alongside the base DAC8x above, deliberately: the
    # base DAC8x's S32 capability was confirmed by an `aplay --dump-hw-params`
    # open test on real jts3 hardware, and this program's own norm is a
    # hardware gate before a format declaration. The Studio driver stack has
    # never been loaded on a fleet box, so that probe has not run against it —
    # jts3 migrates to this driver stack in Phase 1 (owner present; see
    # ADR-0232), and the probe runs then. The two boards share a DAC-chip
    # family (HiFiBerry's datasheets describe both as four 192kHz/24-bit
    # Burr-Brown DACs, differing only in the analog output stage and an added
    # hardware volume-control chip, neither of which touches the digital I2S
    # format this field declares) but do NOT share a driver, so that shared
    # family is a plausible expectation, not proof.
    #
    # On Trixie's rpi-6.12.y kernel, `supported_card_matches` above claims
    # this profile directly: the driver names the card "HiFiBerry Studio
    # DAC8x". On rpi-6.18.y and later, the renamed `hifiberry_studio.c`
    # driver presents every board in the Studio family — the 8-channel
    # Studio DAC8x and the 2-channel Studio Digi/AES alike — under the
    # single shared card name "Hifiberry Studio Soundcard", carrying no
    # DAC8x token and no width, so the label alone cannot tell them apart.
    # `eeprom_gated_card_matches` claims that shared label for this profile
    # ONLY when the HAT EEPROM product string is in `hat_products` (see
    # ADR-0232) — a 2-channel Digi's different EEPROM product never matches,
    # so it cannot be classified as this 8-channel profile. Without a
    # readable EEPROM match, a 6.18.y Studio DAC8x resolves to "unknown" and
    # parks rather than being guessed from the shared label.
    #
    # One case is irreducible by label matching: a Studio board configured
    # with `dtoverlay=hifiberry-dac8x` (what HiFiBerry's own datasheet
    # prescribes) loads the base driver and presents the base card name, so it
    # classifies as `hifiberry_dac8x` and inherits that row's S32_LE and
    # approved chip-AEC. That is not a misroute: the box genuinely IS running
    # the base driver, on the vendor-documented config — see the base row's
    # own evidence.
    #
    # NO latency_floor is declared, so this profile ships the conservative
    # global CamillaDSP/outputd default rather than a measured one. It is the
    # standing floorless case the no-floor doctor branch and the floorless-DAC
    # contract tests are written against, and the conf.d ring period is
    # reachable here only through the operator env seam
    # (`JASPER_OUTPUTD_PERIOD_FRAMES` in `/etc/jasper/jasper.env`).
    #
    # Removal condition (ADR-0232): flip floors/format/commissioning/chip-AEC
    # on this row once the jts3 Studio soak (Phase 1) completes.
)

INNOMAKER_HIFI_AMP_PRO = DacProfile(
    id=INNOMAKER_HIFI_AMP_PRO_ID,
    label="InnoMaker HiFi AMP Pro",
    kind="single",
    physical_output_count=2,
    coherent_clock_domain=True,
    clock_domain_label="Single InnoMaker HiFi AMP Pro device clock",
    clock_domain_contract="single_device",
    outputd_sink="alsa",
    connection="i2s",
    supported_card_matches=(
        r"\bsnd_rpi_merus_amp\b",
        r"\bmerus audio amp ma120x0p-amp-0\b",
    ),
    # Why this board declares S32_LE: the kernel DAI (ma120x0p.c)
    # advertises only S24_LE|S32_LE at continuous 44.1-192 kHz rates — a
    # driver-advertisement limit, not a documented silicon one (the driver's own
    # hw_params has an unadvertised S16 branch) — and JTS pins 48 kHz/2ch.
    #
    # It now carries the active-output lane, on the same width-2 shape the
    # single Apple USB-C dongle already runs (one coherent ALSA device, one
    # mono active 2-way, identity channel map). What retired the blockers:
    # outputd asks ALSA for the declared S32_LE itself (PR-3) and opens the card
    # RAW — deploy/lib/jasper-asound-render.sh renders `outputd_dac` as a plain
    # `type hw` alias like every other registered single DAC (PR-4,
    # format-foundation) — so no conversion layer sits at the final edge and
    # outputd's own client-edge readback IS the hardware edge. That edge is
    # self-proving at runtime rather than assumed: outputd parks at exit 78 if
    # the device installs a format other than the one requested, so a box that
    # is playing is a box whose native S32 open succeeded.
    #
    # Declaring the lane is not arming it. jasper-audio-hardware-reconcile
    # enters active mode only when a LEGAL active graph is already the live
    # CamillaDSP config (active_graph_status -> outputd_active_lane_decision),
    # which only commissioning produces — so this flag makes the layout
    # SELECTABLE at /sound/setup/ and leaves a running box byte-identically
    # passive until it is commissioned.
    #
    # Remaining per-board work, which does not gate the lane: chip-AEC
    # qualification, which stays needs_calibration below. (The latency floor was
    # the other item and is now declared — see below.)
    supports_active_outputd_lane=True,
    active_outputd_lane_channels=2,
    final_edge_format="S32_LE",
    # Hardware evidence, and the two halves have DIFFERENT standing — read the
    # split before transferring any of it.
    #
    # MEASURED (jts4, Pi Zero 2 W + InnoMaker HiFi AMP Pro, 2026-08-14): the
    # outputd pair. A 440 Hz -20 dBFS tone through the correction lane at period
    # 128 / dac_buffer 256 — the pair BOTH other declaring profiles use — took 1
    # DAC xrun in 5 minutes on this board, so 256 is not the floor here. At period
    # 128 / dac_buffer 512: zero DAC xruns in 5 minutes, and zero again in a
    # 3-minute run through the armed ring. DAC presentation latency 10.58 ms. The
    # deeper DAC ring is this board's own result and not a transfer: the Zero 2 W
    # is a 4x-slower quad-A53 than the Pi 5 the other two profiles were measured
    # on, and the writer needs more than two periods of slack to stay ahead of it.
    # The 128-frame PERIOD is what makes shm_ring reachable at all (it must equal
    # fan-in's compile-time RING_SLOT_FRAMES, which is 128).
    #
    # The pair jts4 runs: the ring clamp (#3542) brought this board's former
    # 1024/4096 CamillaFloor down to 256, scaling the target with it to 1024.
    # CamillaDSP validated that pair and the box plays on it — not a soak, so
    # tightening below it needs one on this silicon.
    latency_floor=LatencyFloor(
        outputd_period_frames=128,
        outputd_dac_buffer_frames=512,
    ),
    camilla_floor=CamillaFloor(chunksize=256, target_level=1024),
    chip_aec_detail=(
        "InnoMaker HiFi AMP Pro needs per-profile chip-AEC timing calibration"
    ),
    # Boot overlay SSOT consumed by the root audio-hardware reconciler.
    dtoverlay="merus-amp",
)

DUAL_APPLE_USB_C_DAC_4CH = DacProfile(
    id=DUAL_APPLE_USB_C_DAC_4CH_ID,
    label="Dual Apple USB-C DAC 4-channel pair",
    kind="composite",
    physical_output_count=4,
    coherent_clock_domain=False,
    clock_domain_label="Dual Apple USB-C DAC pair (measured sync required)",
    clock_domain_contract="measured_sync_required",
    outputd_sink="dual_apple",
    supported_card_matches=("usb-c to 3.5mm",),
    usb_ids=("05ac:110a",),
    child_profile_ids=(APPLE_USB_C_DONGLE_ID, APPLE_USB_C_DONGLE_ID),
    requires_same_usb_bus=True,
    supports_active_outputd_lane=True,
    active_outputd_lane_channels=4,
    chip_aec_detail=(
        "dual Apple dongle profile has a measured-sync contract and needs "
        "calibration before arming production chip AEC"
    ),
    # THE CHILDREN'S FLOOR, INHERITED VERBATIM. Both children ARE
    # APPLE_USB_C_DONGLE (``child_profile_ids``), so the empirically-measured
    # Apple floor is this composite's floor: the same silicon, driven at the
    # same rate, twice. Declaring a DIFFERENT number here would claim a timing
    # property no dongle was measured at — pinned by
    # ``test_composite_floor_equals_its_children``.
    #
    # What declaring it BUYS is the conf.d render, not the period. The shipped
    # ring conf.d is already ``period_frames 128``, so the period axis is a
    # no-op for this profile. But ``jasper-audio-config render-ring-conf-wire``
    # renders ONLY from a declared floor — an absent floor short-circuits to
    # ``result skipped / reason no_declared_floor`` before it ever resolves the
    # wire (``jasper/cli/audio_config.py::_cmd_render_ring_conf_wire``), so the
    # ACTIVE block would keep the ioplug's default 2 channels no matter what the
    # topology resolved. An undeclared floor ALSO makes the planner
    # ``jasper.audio_runtime_plan.outputd_latency_floor_actions`` emit ``unset``
    # for ``JASPER_OUTPUTD_PERIOD_FRAMES`` (its "no floor, or no recognized
    # profile, REMOVES stale generated values so the packaged defaults apply"
    # arm) — outputd then falls to ``DEFAULT_PERIOD_FRAMES`` (1024), a conf.d
    # period 128 vs outputd period 1024 divergence that fails CamillaDSP's ring
    # ``open()`` hard rather than a clean refusal. (NOT
    # ``_fallback_latency_floor_actions`` — that shell function is the
    # interpreter-unavailable / command-failure fallback and never fires for a
    # merely floorless profile.)
    #
    # ``outputd_period_frames`` is 128 == ``RING_SLOT_FRAMES``, so this composite
    # clears the ``ring_slot_fixed_128`` refusal without needing issue #2147.
    # That equality is load-bearing and is pinned WITH ITS REASON by
    # ``test_composite_floor_period_equals_ring_slot`` so the pin fails if
    # either number moves.
    #
    # THIS CHANGES A LIVE ALOOP COMPOSITE TOO, AND THAT IS NOT COSMETIC. While
    # this profile was floor-LESS the planner
    # (``jasper.audio_runtime_plan.outputd_latency_floor_actions``) emitted
    # ``unset`` for the period/buffer keys, so a composite ran the PACKAGED
    # defaults: period 1024 / dac_buffer 3072.
    # Declaring the floor moves it to 128 / 256 — on the aloop lane, before any
    # ring is armed. The numbers are the children's own measured floor, but
    # measured on ONE dongle: "period 128 is stable on an Apple dongle" is
    # evidence, not proof, for TWO of them sharing a bus. The single-dongle
    # measurement that produced these values also found period 64 / buffer 128
    # producing bridge xruns, so the margin below 128 is known to be thin.
    # Item 6's on-box buffering-regime check owns confirming this on real
    # hardware.
    latency_floor=LatencyFloor(
        outputd_period_frames=128,
        outputd_dac_buffer_frames=256,
    ),
    camilla_floor=CamillaFloor(chunksize=256, target_level=1536),
    # Stays at the S16_LE default while its child profile
    # (APPLE_USB_C_DONGLE, same silicon) declares S24_3LE, and the divergence is
    # a TRANSPORT fact, not a hardware one. outputd's paired composite sink has
    # no packed-24 child write path: `ChildPeriods` holds an i16 pair or an i32
    # pair, `deinterleave_4ch_to_dual_stereo<T: ChildEdgeSample>` is generic over
    # the sample type, and Rust has no 3-byte integer for `T` to be. So
    # `ChildPeriods::new` refuses an S24_3LE child edge outright and
    # `PairedCompositeSink::new` parks the unit at EX_CONFIG 78 before opening
    # either dongle (#2249). Declaring the packed edge here would ship a profile
    # this daemon cannot run — a silent speaker on every dual-Apple box.
    #
    # Nothing leaks across that boundary: the reconciler emits
    # JASPER_OUTPUTD_DAC_FORMAT from the ARMED profile's own id
    # (final_edge_format_for -> by_id), and outputd asks both children for that
    # one value, so an armed composite opens both dongles here at S16_LE
    # regardless of what the child profile declares for its own single-dongle
    # case.
    #
    # What moves this: a packed-24 child write path in the paired sink (#2257).
    # Until then the surviving invariant is the narrower one stated on
    # final_edge_format's doc: no composite declares a width its transport
    # refuses.
)


REGISTRY: tuple[DacProfile, ...] = (
    APPLE_USB_C_DONGLE,
    HIFIBERRY_DAC8X,
    HIFIBERRY_DAC8X_STUDIO,
    INNOMAKER_HIFI_AMP_PRO,
    DUAL_APPLE_USB_C_DAC_4CH,
)


def _build_index(profiles: tuple[DacProfile, ...]) -> dict[str, DacProfile]:
    out: dict[str, DacProfile] = {}
    for profile in profiles:
        if profile.id in out:
            raise ValueError(f"duplicate DAC profile id: {profile.id}")
        out[profile.id] = profile
    for profile in profiles:
        for child_id in profile.child_profile_ids:
            if child_id not in out:
                raise ValueError(
                    f"{profile.id}: unknown child DAC profile id {child_id!r}"
                )
    return out


_BY_ID = _build_index(REGISTRY)


def all_profiles() -> tuple[DacProfile, ...]:
    """Return all known DAC profiles in stable display order."""

    return REGISTRY


def is_boot_managed_i2s_profile(profile: DacProfile) -> bool:
    """Whether a profile is an I2S HAT eligible for the boot overlay line.

    ``dtoverlay`` is required alongside ``connection == "i2s"``: a profile
    can declare the I2S interface without owning a boot line (none do
    today, but the two fields are independent, not implied by each other).
    """

    return profile.connection == "i2s" and bool(profile.dtoverlay)


def by_id(profile_id: str) -> DacProfile | None:
    """Lookup a DAC profile by stable id."""

    return _BY_ID.get(profile_id)


def known_profile_ids() -> tuple[str, ...]:
    """Return known DAC profile ids in stable display order."""

    return tuple(profile.id for profile in REGISTRY)


def is_known_profile_id(profile_id: str) -> bool:
    """Return True when ``profile_id`` is a registered DAC profile."""

    return profile_id in _BY_ID


def physical_output_count_for(profile_id: str) -> int | None:
    """Return the declared physical output count for a known profile."""

    profile = by_id(profile_id)
    if profile is None:
        return None
    return profile.physical_output_count


def label_for(profile_id: str) -> str | None:
    """Return the display label for a known profile."""

    profile = by_id(profile_id)
    if profile is None:
        return None
    return profile.label


def clock_domain_label_for(profile_id: str) -> str | None:
    """Return the clock-domain label for a known profile."""

    profile = by_id(profile_id)
    if profile is None:
        return None
    return profile.clock_domain_label


def clock_domain_contract_for(profile_id: str) -> ClockDomainContract | None:
    """Return the clock-domain contract for a known profile."""

    profile = by_id(profile_id)
    if profile is None:
        return None
    return profile.clock_domain_contract


def _matches_any(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)


def _declares_hat_product(profile: DacProfile, hat: HatEeprom) -> bool:
    product = hat.product.strip().casefold()
    return bool(product) and any(
        product == declared.casefold() for declared in profile.hat_products
    )


def profile_for_card_label(
    label: str,
    *,
    hat: HatEeprom | None = None,
) -> DacProfile | None:
    """Return the first single-device profile matching an ALSA/sysfs label.

    ``hat`` is the fitted HAT's EEPROM identity when one was read. It unlocks
    nothing but ``eeprom_gated_card_matches``, and only after every unambiguous
    label pattern in the registry has already failed — so a label a driver
    emits for exactly one board keeps routing by label alone, EEPROM or not,
    and an EEPROM can never redirect a card away from the profile its own
    driver stack names.
    """

    text = label.strip()
    if not text:
        return None
    for profile in REGISTRY:
        if profile.kind != "single":
            continue
        if _matches_any(profile.supported_card_matches, text):
            return profile
    if hat is None:
        return None
    for profile in REGISTRY:
        if profile.kind != "single":
            continue
        if not _declares_hat_product(profile, hat):
            continue
        if _matches_any(profile.eeprom_gated_card_matches, text):
            return profile
    return None


def profile_for_hat(hat: HatEeprom | None) -> DacProfile | None:
    """Return the profile whose ``hat_products`` claims a fitted HAT.

    The EEPROM product is the board's own declaration of what it is, so a
    match identifies the fitted HAT before any driver has bound a card --
    which is what lets the boot overlay be resolved from it (ADR-0234).
    """

    if hat is None:
        return None
    return next(
        (
            profile
            for profile in REGISTRY
            if profile.kind == "single" and _declares_hat_product(profile, hat)
        ),
        None,
    )


def supports_physical_output_count(profile_id: str, output_count: int) -> bool:
    """Return whether a known profile has exactly ``output_count`` outputs."""

    profile = by_id(profile_id)
    return profile is not None and profile.physical_output_count == output_count


def active_outputd_lane_channels_for(profile_id: str) -> int | None:
    """Return the profile-declared active outputd transport width.

    This is the protected transport capacity between CamillaDSP and outputd for
    the current implementation. It is deliberately separate from physical DAC
    outputs: a DAC can expose more analog lanes than outputd can safely consume
    through the active-speaker handoff today.
    """

    profile = by_id(profile_id)
    if profile is None or not profile.supports_active_outputd_lane:
        return None
    return profile.active_outputd_lane_channels


def final_edge_format_for(profile_id: str) -> str | None:
    """Return the profile-declared final-edge ALSA sample format, or None.

    The reconciler shells out to this (mirroring
    :func:`active_outputd_lane_channels_for`) to emit the recognized DAC's
    format into the wizard-owned env as JASPER_OUTPUTD_DAC_FORMAT. None means
    an unrecognized profile id — every known profile declares a format
    (default "S16_LE"), so unlike :func:`latency_floor_for`'s None (which
    also covers "no floor declared"), None here is purely an unknown-id
    signal.
    """

    profile = by_id(profile_id)
    if profile is None:
        return None
    return profile.final_edge_format


def latency_floor_for(profile_id: str) -> LatencyFloor | None:
    """Return the profile-declared stable buffer floor, or None.

    The reconciler shells out to this (mirroring
    :func:`active_outputd_lane_channels_for`) to emit the active DAC's floor
    into the wizard-owned env. None means "use the global default" — the
    non-breaking path for any DAC that has not declared a measured floor.
    """

    profile = by_id(profile_id)
    if profile is None:
        return None
    return profile.latency_floor


def camilla_floor_for(profile_id: str) -> CamillaFloor | None:
    """The profile-declared CamillaDSP floor, or None (the transport default)."""

    profile = by_id(profile_id)
    return None if profile is None else profile.camilla_floor


def mixer_control_groups_for(
    profile_id: str,
) -> tuple[tuple[MixerControl, ...], ...] | None:
    """Return mixer policies grouped by physical DAC child.

    A single DAC returns one group. A composite profile returns one
    group for each child profile, preserving cardinality for callers
    that need to pin or monitor child-device controls.
    """

    profile = by_id(profile_id)
    if profile is None:
        return None
    if profile.kind == "single":
        return (profile.mixer_controls,)
    groups: list[tuple[MixerControl, ...]] = []
    for child_id in profile.child_profile_ids:
        child = by_id(child_id)
        if child is None:
            return None
        groups.append(child.mixer_controls)
    return tuple(groups)


__all__ = [
    "APPLE_HEADPHONE_CONTROL",
    "APPLE_USB_C_DONGLE",
    "APPLE_USB_C_DONGLE_ID",
    "ChannelMapEntry",
    "ClockDomainContract",
    "DAC8X_OUTPUTD_STABILITY_PROFILE",
    "DUAL_APPLE_USB_C_DAC_4CH",
    "DUAL_APPLE_USB_C_DAC_4CH_ID",
    "DacKind",
    "DacProfile",
    "HIFIBERRY_DAC8X",
    "HIFIBERRY_DAC8X_ID",
    "HIFIBERRY_DAC8X_STUDIO",
    "HIFIBERRY_DAC8X_STUDIO_ID",
    "HIFIBERRY_STUDIO_MIXER_CONTROLS",
    "INNOMAKER_HIFI_AMP_PRO",
    "INNOMAKER_HIFI_AMP_PRO_ID",
    "LatencyFloor",
    "MixerControl",
    "REGISTRY",
    "all_profiles",
    "active_outputd_lane_channels_for",
    "by_id",
    "camilla_floor_for",
    "clock_domain_contract_for",
    "clock_domain_label_for",
    "final_edge_format_for",
    "is_boot_managed_i2s_profile",
    "is_known_profile_id",
    "known_profile_ids",
    "label_for",
    "latency_floor_for",
    "mixer_control_groups_for",
    "profile_for_card_label",
    "profile_for_hat",
    "physical_output_count_for",
    "supports_physical_output_count",
]
