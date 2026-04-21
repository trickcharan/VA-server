"""
Google Gemini Live API WebSocket session manager.
Handles connection, audio/text streaming, and response reception.
"""

import asyncio
import logging
import os

from google import genai
from google.genai import types

logger = logging.getLogger("google-live")

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "")
MODEL = "gemini-2.5-flash-native-audio-latest"

# Audio config — 16-bit PCM, 16 kHz mono is the expected input format
INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000


class GoogleLiveSession:
    """Manages a single Google Gemini Live API WebSocket session."""

    def __init__(self, system_instruction: str | None = None):
        if not GOOGLE_API_KEY:
            raise ValueError("GOOGLE_API_KEY environment variable is not set")

        self._client = genai.Client(api_key=GOOGLE_API_KEY)
        self._system_instruction = system_instruction or (
            "You are a helpful virtual agent. Respond concisely."
        )
        self._session = None
        self._running = False

    async def connect(self):
        """Open the Live API WebSocket connection."""
        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=self._system_instruction,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )

        logger.info("Connecting to Google Live API (model=%s)...", MODEL)
        self._session_ctx = self._client.aio.live.connect(
            model=MODEL, config=config
        )
        self._session = await self._session_ctx.__aenter__()
        self._running = True
        logger.info("Connected to Google Live API")

    async def send_audio(self, audio_bytes: bytes, mime_type: str = "audio/pcm"):
        """
        Send raw audio bytes to Google.

        Args:
            audio_bytes: Raw audio data (PCM 16-bit, 16 kHz mono expected).
            mime_type: MIME type of the audio. Default is audio/pcm.
        """
        if not self._session:
            raise RuntimeError("Session not connected. Call connect() first.")

        await self._session.send_realtime_input(
            audio=types.Blob(data=audio_bytes, mime_type=mime_type)
        )

    async def send_text(self, text: str):
        """Send a text message to the model."""
        if not self._session:
            raise RuntimeError("Session not connected. Call connect() first.")

        await self._session.send_client_content(
            turns=types.Content(
                role="user",
                parts=[types.Part(text=text)],
            ),
            turn_complete=True,
        )

    async def receive_responses(
        self,
        on_audio=None,
        on_text=None,
        on_input_transcription=None,
        on_output_transcription=None,
        on_turn_complete=None,
        on_interrupted=None,
    ):
        """
        Listen for responses from Google Live API.

        Hybrid SOI/EOI strategy (auto-VAD stays enabled):
          - SOI is inferred from the first inputTranscription (user started speaking)
          - EOI is implicit — Google's auto-VAD handles it internally,
            and the model responds; turnComplete signals the end of the model turn.

        Args:
            on_audio: async callback(audio_bytes: bytes) for audio chunks.
            on_text: async callback(text: str) for text parts.
            on_input_transcription: async callback(text: str) for user speech transcription.
                                    First call = SOI proxy.
            on_output_transcription: async callback(text: str) for model speech transcription.
            on_turn_complete: async callback() fired when model finishes its response turn.
            on_interrupted: async callback() fired when user barged in and model was interrupted.
        """
        if not self._session:
            raise RuntimeError("Session not connected. Call connect() first.")

        logger.info("Listening for responses from Google Live API...")
        try:
            async for message in self._session.receive():
                if not self._running:
                    break

                # Setup complete acknowledgement
                if message.setup_complete:
                    logger.info("Session setup complete")
                    continue

                # Server content (model responses)
                if message.server_content:
                    content = message.server_content

                    # Model turn contains parts (audio/text)
                    if content.model_turn and content.model_turn.parts:
                        for part in content.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                if on_audio:
                                    await on_audio(part.inline_data.data)
                            if part.text:
                                if on_text:
                                    await on_text(part.text)

                    # Input transcription (what user said) — serves as SOI proxy
                    if content.input_transcription:
                        transcript_text = content.input_transcription.text if hasattr(content.input_transcription, 'text') else str(content.input_transcription)
                        logger.info("Input transcript: %s", transcript_text)
                        if on_input_transcription:
                            await on_input_transcription(transcript_text)

                    # Output transcription (what model said)
                    if content.output_transcription:
                        transcript_text = content.output_transcription.text if hasattr(content.output_transcription, 'text') else str(content.output_transcription)
                        logger.info("Output transcript: %s", transcript_text)
                        if on_output_transcription:
                            await on_output_transcription(transcript_text)

                    # Model finished responding — turn is done
                    if content.turn_complete:
                        logger.info("Turn complete")
                        if on_turn_complete:
                            await on_turn_complete()

                    # User barged in — model was interrupted
                    if content.interrupted:
                        logger.info("Model response interrupted by user (barge-in)")
                        if on_interrupted:
                            await on_interrupted()

                # Usage metadata
                if message.usage_metadata:
                    logger.debug("Usage: %s", message.usage_metadata)

        except Exception as e:
            if self._running:
                logger.error("Error receiving responses: %s", e, exc_info=True)
            raise

    async def close(self):
        """Close the Live API session."""
        self._running = False
        if self._session:
            try:
                await self._session_ctx.__aexit__(None, None, None)
            except Exception as e:
                logger.warning("Error closing session: %s", e)
            self._session = None
            logger.info("Google Live API session closed")


class SessionManager:
    """Manages Google Live sessions for concurrent calls."""

    def __init__(self, system_instruction: str | None = None):
        self._sessions: dict[str, GoogleLiveSession] = {}
        self._system_instruction = system_instruction

    async def create_session(self, call_id: str) -> GoogleLiveSession:
        session = GoogleLiveSession(self._system_instruction)
        await session.connect()
        self._sessions[call_id] = session
        return session

    async def close_session(self, call_id: str):
        session = self._sessions.pop(call_id, None)
        if session:
            await session.close()

    async def close_all(self):
        for call_id in list(self._sessions):
            await self.close_session(call_id)

    def get_session(self, call_id: str) -> GoogleLiveSession | None:
        return self._sessions.get(call_id)
