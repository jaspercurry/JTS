from __future__ import annotations

from typing import AsyncIterator, Protocol, runtime_checkable

from ..tools import ToolRegistry


@runtime_checkable
class VoiceSession(Protocol):
    """Provider-agnostic interface for a bidirectional voice session.

    v1 ships one implementation (Gemini Live). Adding OpenAI Realtime / xAI
    later means writing another adapter against this Protocol — daemon code
    in voice_daemon.py imports only this interface.
    """

    async def connect(self, registry: ToolRegistry, system_instruction: str) -> None:
        ...

    async def send_audio(self, pcm_16khz_int16: bytes) -> None:
        ...

    async def end_input(self) -> None:
        ...

    def audio_out(self) -> AsyncIterator[bytes]:
        ...

    async def close(self) -> None:
        ...

    def usage_tokens(self) -> dict[str, int]:
        ...

    def turn_count(self) -> int:
        """Return the number of completed model turns observed."""
        ...

    def interrupted(self) -> bool:
        """True if the model reported being interrupted by user audio."""
        ...
