"""
Google Gemini Live API WebSocket session manager.
Handles connection, audio/text streaming, and response reception.
"""

import asyncio
import json
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


def _load_base_tools() -> list[dict]:
    """Load base/common tool definitions from the google_live tools.json.

    These tools (end_call, transfer_to_agent) are always present
    regardless of customer configuration.
    """
    tools_path = os.path.join(os.path.dirname(__file__), "tools.json")
    try:
        with open(tools_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.warning("No base tools.json found at %s", tools_path)
        return []


class GoogleLiveSession:
    """Manages a single Google Gemini Live API WebSocket session."""

    def __init__(self, system_instruction: str | None = None,
                 customer_tools: list[dict] | None = None):
        if not GOOGLE_API_KEY:
            raise ValueError("GOOGLE_API_KEY environment variable is not set")

        self._client = genai.Client(api_key=GOOGLE_API_KEY)
        self._system_instruction = system_instruction or (
            "You are a helpful virtual agent. Respond concisely."
        )
        self._session = None
        self._running = False

        # Merge base tools (end_call, transfer_to_agent) with customer-specific tools
        base_tools = _load_base_tools()
        extra_tools = customer_tools or []
        self._tools = base_tools + extra_tools
        if self._tools:
            logger.info("Tools loaded: %d base + %d customer = %d total",
                        len(base_tools), len(extra_tools), len(self._tools))

    async def connect(self):
        """Open the Live API WebSocket connection."""
        # Convert tool definitions to Google function declarations
        google_tools = None
        if self._tools:
            declarations = []
            for tool_def in self._tools:
                declarations.append(types.FunctionDeclaration(
                    name=tool_def["name"],
                    description=tool_def.get("description", ""),
                    parameters=tool_def.get("parameters"),
                ))
            google_tools = [types.Tool(function_declarations=declarations)]
            logger.info("Registered %d tools: %s", len(declarations),
                        [d.name for d in declarations])

        config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=self._system_instruction,
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Aoede")
                ),
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            tools=google_tools,
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

    async def send_tool_response(self, function_call_id: str, name: str, result: dict):
        """Send a function call result back to the model."""
        if not self._session:
            raise RuntimeError("Session not connected. Call connect() first.")

        response = types.FunctionResponse(
            id=function_call_id,
            name=name,
            response=result,
        )
        logger.info("Sending tool response for %s (id=%s)", name, function_call_id)
        await self._session.send_tool_response(function_responses=[response])

    async def receive_responses(
        self,
        on_audio=None,
        on_text=None,
        on_input_transcription=None,
        on_output_transcription=None,
        on_turn_complete=None,
        on_interrupted=None,
        on_tool_call=None,
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
            on_tool_call: async callback(function_call_id, name, args) fired when model invokes a tool.
        """
        if not self._session:
            raise RuntimeError("Session not connected. Call connect() first.")

        logger.info("Listening for responses from Google Live API...")
        try:
            # The SDK's receive() generator ends after each turnComplete,
            # so we wrap it in an outer loop to keep listening for subsequent turns.
            while self._running:
                async for message in self._session.receive():
                    if not self._running:
                        logger.info("Receiver stopped (running=False)")
                        return

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

                    # Tool call (function calling)
                    if message.tool_call:
                        for fc in message.tool_call.function_calls:
                            logger.info("Tool call: %s(id=%s, args=%s)",
                                        fc.name, fc.id, fc.args)
                            if on_tool_call:
                                await on_tool_call(fc.id, fc.name, fc.args)

                    # Usage metadata
                    if message.usage_metadata:
                        logger.debug("Usage: %s", message.usage_metadata)

                logger.info("Turn receive loop ended, re-entering for next turn...")

            logger.info("Receiver loop ended normally")
        except asyncio.CancelledError:
            logger.info("Receiver cancelled")
            raise
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
