"""Import-cheap wake-capture UDP port defaults."""
from __future__ import annotations

# Match jasper.cli.aec_bridge's default emit ports.
DEFAULT_AEC_ON_PORT = 9876
DEFAULT_AEC_OFF_PORT = 9877
DEFAULT_AEC_DTLN_PORT = 9878

# Truly-raw mic 0 (chip channel 2; no chip DSP applied). The bridge
# always emits here; consumers opt in by binding the port.
DEFAULT_AEC_RAW0_PORT = 9879


def build_ports(
    *,
    aec_on_port: int = DEFAULT_AEC_ON_PORT,
    aec_off_port: int = DEFAULT_AEC_OFF_PORT,
    aec_dtln_port: int = DEFAULT_AEC_DTLN_PORT,
    aec_raw0_port: int = DEFAULT_AEC_RAW0_PORT,
    include_dtln: bool = True,
) -> dict[str, int]:
    """Return the UDP port map used by wake-capture tooling.

    Raw mic 0 is always present so a raw0-enabled session can
    subscribe to it. DTLN remains optional because some low-RAM
    installs deliberately keep that bridge leg disabled.
    """
    ports = {
        "on": aec_on_port,
        "off": aec_off_port,
    }
    if include_dtln:
        ports["dtln"] = aec_dtln_port
    ports["raw0"] = aec_raw0_port
    return ports
