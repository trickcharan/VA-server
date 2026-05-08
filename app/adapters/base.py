"""Abstract base class for Speech-to-Speech (STS) adapters.

Every STS provider (Google Gemini Live, OpenAI Realtime, etc.) implements
this interface so that RequestProcessor can work with any provider without
knowing the implementation details.
"""

from abc import ABC, abstractmethod
from typing import Generator


class STSAdapter(ABC):
    """Base interface for Speech-to-Speech providers."""

    @abstractmethod
    def on_session_start(self, welcome_audio: bytes | None = None) -> Generator:
        """Connect to the STS provider and optionally send welcome audio.

        Yields VoiceVAResponse objects (e.g. welcome audio chunks).
        """
        ...

    @abstractmethod
    def on_session_end(self) -> Generator:
        """Disconnect from the STS provider and clean up.

        Yields VoiceVAResponse objects (e.g. final response).
        """
        ...

    @abstractmethod
    def on_audio(self, audio_bytes: bytes) -> None:
        """Forward caller audio (8 kHz mono mu-law) to the STS provider.

        This is a fire-and-forget call; responses come back via
        drain_responses().
        """
        ...

    @abstractmethod
    def drain_responses(self) -> Generator:
        """Yield any queued VoiceVAResponse objects from the provider.

        Called by the gRPC stream after every audio chunk to pick up
        asynchronous responses (SOI, EOI, audio chunks, FINAL, etc.).
        """
        ...

    @abstractmethod
    def cleanup(self) -> None:
        """Release all resources. Must be idempotent."""
        ...
