# SPDX-FileCopyrightText: 2026 Jasper Curry
#
# SPDX-License-Identifier: Apache-2.0

"""Shared assistant-TTS socket/env contract.

The TTS wire protocol is outputd-compatible, but the default solo owner is
``jasper-fanin`` so assistant audio enters before CamillaDSP. Multiroom can
temporarily point voice at ``jasper-outputd`` for member-local playout.
"""

FANIN_TTS_SOCKET_ENV = "JASPER_FANIN_TTS_SOCKET"
FANIN_TTS_SOCKET = "/run/jasper-fanin/tts.sock"

OUTPUTD_TTS_SOCKET_ENV = "JASPER_OUTPUTD_TTS_SOCKET"
OUTPUTD_TTS_SOCKET = "/run/jasper-outputd/tts.sock"

VOICE_TTS_SOCKET_ENV = "JASPER_TTS_OUTPUTD_SOCKET"
TTS_TRANSPORT_ENV = "JASPER_TTS_TRANSPORT"
DUCK_TRANSPORT_ENV = "JASPER_DUCK_TRANSPORT"
