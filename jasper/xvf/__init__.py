"""Vendored XMOS XVF3800 control interface.

xvf_host.py is a verbatim copy of
github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY/python_control/xvf_host.py
(MIT licensed). We vendor rather than git-clone at install time so
the daemon has a fixed dependency that survives upstream renames /
deletions, and so deploy/install.sh has no network dependency.

Update procedure: re-fetch from upstream when XMOS publishes a new
firmware release that adds parameters we want. Diff carefully —
the parameter table at the top of xvf_host.py is the contract
between firmware and this script.
"""
