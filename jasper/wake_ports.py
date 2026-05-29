"""Import-cheap wake-capture UDP port defaults."""
from __future__ import annotations

from jasper.aec_sweep import AEC3_SWEEP_VARIANTS
from jasper import wake_legs

# Wire ports now have a single definition in jasper.wake_legs.REGISTRY
# (which matches jasper.cli.aec_bridge's OUT_PORT* emit constants). These
# module constants are kept as the stable import surface that build_ports()
# and its callers (web/__main__, wake_corpus_setup, cli/wake_enroll) use.
DEFAULT_AEC_ON_PORT = wake_legs.by_token("on").udp_port
DEFAULT_AEC_OFF_PORT = wake_legs.by_token("off").udp_port
DEFAULT_AEC_DTLN_PORT = wake_legs.by_token("dtln").udp_port

# Truly-raw mic 0 (chip channel 2; no chip DSP applied). The bridge
# always emits here; consumers opt in by binding the port.
DEFAULT_AEC_RAW0_PORT = wake_legs.by_token("raw0").udp_port

# Corpus-only experiment legs emitted by jasper-aec-bridge when
# explicitly enabled. These are never production wake-detection inputs.
DEFAULT_AEC_REF_PORT = wake_legs.by_token("ref").udp_port
DEFAULT_AEC_USB_RAW_PORT = wake_legs.by_token("usb_raw").udp_port
DEFAULT_AEC_USB_WEBRTC_PORT = wake_legs.by_token("usb_webrtc").udp_port
DEFAULT_AEC_USB_DTLN_PORT = wake_legs.by_token("usb_dtln").udp_port

# Corpus-only parallel AEC3 tuning variants. Baseline stays on 9876;
# these are extra same-utterance comparison legs.
DEFAULT_AEC3_SWEEP_PORTS = {
    variant.leg: variant.default_port for variant in AEC3_SWEEP_VARIANTS
}


def build_ports(
    *,
    aec_on_port: int = DEFAULT_AEC_ON_PORT,
    aec_off_port: int = DEFAULT_AEC_OFF_PORT,
    aec_dtln_port: int = DEFAULT_AEC_DTLN_PORT,
    aec_raw0_port: int = DEFAULT_AEC_RAW0_PORT,
    aec_ref_port: int = DEFAULT_AEC_REF_PORT,
    aec_usb_raw_port: int = DEFAULT_AEC_USB_RAW_PORT,
    aec_usb_webrtc_port: int = DEFAULT_AEC_USB_WEBRTC_PORT,
    aec_usb_dtln_port: int = DEFAULT_AEC_USB_DTLN_PORT,
    aec3_sweep_ports: dict[str, int] | None = None,
    include_dtln: bool = True,
    include_usb: bool = True,
    include_aec3_sweep: bool = True,
) -> dict[str, int]:
    """Return the UDP port map used by wake-capture tooling.

    Raw mic 0 is always present so a raw0-enabled session can
    subscribe to it. DTLN and USB/reference remain optional because
    some low-RAM installs deliberately keep those bridge legs disabled.
    """
    ports = {
        "on": aec_on_port,
        "off": aec_off_port,
    }
    if include_dtln:
        ports["dtln"] = aec_dtln_port
    ports["raw0"] = aec_raw0_port
    if include_usb:
        ports["ref"] = aec_ref_port
        ports["usb_raw"] = aec_usb_raw_port
        ports["usb_webrtc"] = aec_usb_webrtc_port
        ports["usb_dtln"] = aec_usb_dtln_port
    if include_aec3_sweep:
        ports.update(aec3_sweep_ports or DEFAULT_AEC3_SWEEP_PORTS)
    return ports
