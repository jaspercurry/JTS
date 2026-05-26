"""Import-cheap wake-capture UDP port defaults."""
from __future__ import annotations

# Match jasper.cli.aec_bridge's default emit ports.
DEFAULT_AEC_ON_PORT = 9876
DEFAULT_AEC_OFF_PORT = 9877
DEFAULT_AEC_DTLN_PORT = 9878

# Truly-raw mic 0 (chip channel 2; no chip DSP applied). The bridge
# always emits here; consumers opt in by binding the port.
DEFAULT_AEC_RAW0_PORT = 9879

# Corpus-only experiment legs emitted by jasper-aec-bridge when
# explicitly enabled. These are never production wake-detection inputs.
DEFAULT_AEC_REF_PORT = 9880
DEFAULT_AEC_USB_RAW_PORT = 9881
DEFAULT_AEC_USB_WEBRTC_PORT = 9882
DEFAULT_AEC_USB_DTLN_PORT = 9883


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
    include_dtln: bool = True,
    include_usb: bool = True,
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
    return ports
