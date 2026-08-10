# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Pi-side helpers for the jasper-fanin (Rust) summing daemon.

The daemon itself lives in ``rust/jasper-fanin``; this package holds the
small Python-side reconcilers that write its wizard-owned env file
(``/var/lib/jasper/fanin.env``) and ask the restart broker to bounce the
daemon so the new env takes effect. See ``coupling_reconcile`` for the
fan-in -> CamillaDSP coupling and the USB low-latency combo.
"""
