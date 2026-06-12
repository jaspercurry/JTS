"""JTS-owned XMOS XVF3800 control interface.

`xvf_host.py` implements the small USB vendor-control command surface
JTS needs for boot-time chip setup, chip-AEC experiments, and operator
diagnostics. It is not a vendored copy of the ReSpeaker Python helper.

When a future firmware release exposes a command JTS needs, add only
that command to `xvf_host.py` from XMOS-published protocol facts and
hardware validation. Do not re-vendor upstream demo code without a
verified redistribution license.
"""
