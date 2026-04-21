"""
Adapter: bridges the downstream gRPC server with the Google Live API.

The gRPC server is synchronous (threaded), while Google Live is async.
This adapter runs a dedicated asyncio event loop in a background thread
and uses thread-safe signals/queues to pass events back to the gRPC stream.

Hybrid SOI/EOI strategy (Google auto-VAD stays enabled):
  1. Audio arrives from gRPC → forward to Google
  2. Google sends first inputTranscription → infer SOI → yield START_OF_INPUT
  3. Google auto-VAD detects silence → model starts responding
  4. Google sends turnComplete → infer EOI → yield END_OF_INPUT
  5. Drain buffered AI audio → yield CHUNKs + FINAL
  6. Reset state for next conversation turn
"""

import asyncio
import logging
import queue
import threading

from downstream.proto.voicevirtualagent_pb2 import VoiceVAResponse
from downstream.proto.byova_common_pb2 import OutputEvent
from downstream.utils.EventUtils import EventUtils
from downstream.google_live.session import GoogleLiveSession

logger = logging.getLogger("adapter")

# Sentinel to signal end of response turn
_TURN_COMPLETE = object()

# Audio chunk size for streaming back to gRPC
# 640 bytes = 80ms at 8kHz mu-law (1 byte/sample)
GRPC_AUDIO_CHUNK_SIZE = 640


class GoogleLiveAdapter:
    """
    Per-conversation adapter that wires a gRPC call to a Google Live session.

    Hybrid SOI/EOI using Google's auto-VAD + transcription signals:
      - First inputTranscription  → SOI (user started speaking)
      - turnComplete              → EOI + stream AI response + FINAL
      - interrupted               → barge-in: flush + reset
    """

    def __init__(self, conversation_id: str, barge_in_enabled: bool = False,
                 system_instruction: str | None = None):
        self.conversation_id = conversation_id
        self.barge_in_enabled = barge_in_enabled
        self._system_instruction = system_instruction

        # Asyncio event loop in a background thread
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._session: GoogleLiveSession | None = None
        self._receiver_task: asyncio.Task | None = None

        # Threading events — Google async callbacks signal, gRPC thread checks
        self._soi_event = threading.Event()           # First inputTranscription arrived
        self._turn_complete_event = threading.Event()  # Model finished responding

        # Audio response queue — Google pushes audio, gRPC drains after turn_complete
        self._audio_queue: queue.Queue = queue.Queue()

        # State tracking
        self._start_of_input_sent = False
        self._cleaned_up = False

        # Transcript accumulation
        self._input_transcript_parts: list[str] = []
        self._output_transcript_parts: list[str] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_session_start(self):
        """
        Called on SESSION_START event.
        Spins up the async loop, connects to Google, starts the receiver.
        Waits for Google's initial greeting, then yields it as the welcome prompt.
        """
        logger.info("[%s] Session starting — connecting to Google Live", self.conversation_id)

        # Start dedicated event loop in background thread
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop, daemon=True, name=f"google-live-{self.conversation_id}"
        )
        self._loop_thread.start()

        # Connect and start receiver (blocks until connected)
        future = asyncio.run_coroutine_threadsafe(self._async_start(), self._loop)
        future.result(timeout=30)

        logger.info("[%s] Google Live session connected, receiver started", self.conversation_id)

        # Wait for Google's initial greeting (turn_complete signals it's done)
        logger.info("[%s] Waiting for welcome prompt from Google...", self.conversation_id)
        if self._turn_complete_event.wait(timeout=30):
            logger.info("[%s] Welcome prompt received, streaming to caller", self.conversation_id)
            # Drain greeting audio and yield as welcome prompt
            welcome_audio = self._collect_audio_from_queue()
            transcript = " ".join(self._output_transcript_parts) or "Welcome"
            if welcome_audio:
                yield EventUtils.get_audio_output_events_bytes(
                    welcome_audio, transcript,
                    self.barge_in_enabled,
                    VoiceVAResponse.ResponseType.FINAL,
                )
            else:
                logger.warning("[%s] No greeting audio received", self.conversation_id)
            self._reset_turn_state()
        else:
            logger.warning("[%s] Timed out waiting for welcome prompt", self.conversation_id)

    def on_session_end(self):
        """
        Called on SESSION_END event.
        Delegates to cleanup() and yields an empty response to close the gRPC stream.
        """
        self.cleanup()
        yield VoiceVAResponse()

    def cleanup(self):
        """
        Idempotent cleanup — safe to call multiple times.
        Closes Google WebSocket, stops the async loop, joins the thread.
        Called from:
          - on_session_end() when SESSION_END event arrives
          - RequestProcessor.cleanup() when gRPC stream ends (for loop exits)
        """
        if self._cleaned_up:
            return
        self._cleaned_up = True

        logger.info("[%s] Cleaning up Google Live session", self.conversation_id)

        # Unblock any waiting gRPC thread
        self._soi_event.set()
        self._turn_complete_event.set()

        if self._loop and self._loop.is_running():
            future = asyncio.run_coroutine_threadsafe(self._async_stop(), self._loop)
            try:
                future.result(timeout=10)
            except Exception as e:
                logger.warning("[%s] Error during async stop: %s", self.conversation_id, e)

            self._loop.call_soon_threadsafe(self._loop.stop)

        if self._loop_thread:
            self._loop_thread.join(timeout=5)

        self._session = None
        self._loop = None
        self._loop_thread = None

        logger.info("[%s] Google Live session cleaned up", self.conversation_id)

    def on_audio(self, audio_bytes: bytes):
        """
        Called when audio_input arrives from the gRPC stream.
        Hybrid SOI/EOI pattern:
          1. Forward audio to Google
          2. First inputTranscription → yield START_OF_INPUT (SOI)
          3. turnComplete → yield END_OF_INPUT (EOI) + drain audio CHUNKs + FINAL
        """
        if not self._loop or not self._session or self._cleaned_up:
            return

        # Forward caller audio to Google
        asyncio.run_coroutine_threadsafe(
            self._session.send_audio(audio_bytes), self._loop
        )

        # SOI: first inputTranscription means user started speaking (non-blocking check)
        if not self._start_of_input_sent and self._soi_event.is_set():
            self._start_of_input_sent = True
            logger.info("[%s] Sending START_OF_INPUT (from inputTranscription)", self.conversation_id)
            yield EventUtils.get_va_response_for_output_event(
                EventUtils.get_output_event(OutputEvent.EventType.START_OF_INPUT)
            )

        # EOI: turnComplete means Google's VAD detected silence, model responded,
        # and the turn is done (non-blocking check)
        if self._start_of_input_sent and self._turn_complete_event.is_set():
            logger.info("[%s] Sending END_OF_INPUT (from turnComplete)", self.conversation_id)
            yield EventUtils.get_va_response_for_output_event(
                EventUtils.get_output_event(OutputEvent.EventType.END_OF_INPUT)
            )

            # Drain all buffered AI audio as CHUNK responses, then FINAL
            yield from self._stream_response_chunks()

            # Reset state for next conversation turn
            self._reset_turn_state()

    # ------------------------------------------------------------------
    # Response streaming — called from gRPC thread after turn complete
    # ------------------------------------------------------------------

    def _collect_audio_from_queue(self) -> bytes:
        """Drain audio queue into a single bytes buffer (up to _TURN_COMPLETE sentinel)."""
        audio_parts = []
        while True:
            try:
                item = self._audio_queue.get_nowait()
                if item is _TURN_COMPLETE:
                    break
                audio_parts.append(item)
            except queue.Empty:
                break
        return b"".join(audio_parts)

    def _stream_response_chunks(self):
        """Drain audio queue and yield CHUNKs, then yield FINAL."""
        while True:
            try:
                item = self._audio_queue.get_nowait()
                if item is _TURN_COMPLETE:
                    break
                # item is raw audio bytes from Google
                for i in range(0, len(item), GRPC_AUDIO_CHUNK_SIZE):
                    chunk = item[i:i + GRPC_AUDIO_CHUNK_SIZE]
                    yield EventUtils.get_audio_output_events_bytes(
                        chunk,
                        None,
                        self.barge_in_enabled,
                        VoiceVAResponse.ResponseType.CHUNK,
                    )
            except queue.Empty:
                break

        # Send FINAL to signal end of this response turn
        yield EventUtils.get_audio_output_events_bytes(
            None, None, self.barge_in_enabled, VoiceVAResponse.ResponseType.FINAL
        )

    def _reset_turn_state(self):
        """Reset state for the next conversation turn."""
        self._start_of_input_sent = False
        self._soi_event.clear()
        self._turn_complete_event.clear()

    # ------------------------------------------------------------------
    # Async internals — run in background event loop
    # ------------------------------------------------------------------

    def _run_loop(self):
        """Run the asyncio event loop in the background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _async_start(self):
        """Connect to Google Live and start receiver task."""
        self._session = GoogleLiveSession(self._system_instruction)
        await self._session.connect()

        # Start listening for responses in background
        self._receiver_task = asyncio.ensure_future(
            self._session.receive_responses(
                on_audio=self._on_google_audio,
                on_text=self._on_google_text,
                on_input_transcription=self._on_google_input_transcription,
                on_output_transcription=self._on_google_output_transcription,
                on_turn_complete=self._on_google_turn_complete,
                on_interrupted=self._on_google_interrupted,
            )
        )

    async def _async_stop(self):
        """Close session and cancel receiver."""
        if self._receiver_task and not self._receiver_task.done():
            self._receiver_task.cancel()
            try:
                await self._receiver_task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # Google Live callbacks — run in async context, signal gRPC thread
    # ------------------------------------------------------------------

    async def _on_google_audio(self, audio_bytes: bytes):
        """Google sent model audio — enqueue for gRPC thread to drain after turn complete."""
        self._audio_queue.put(audio_bytes)

    async def _on_google_text(self, text: str):
        """Google sent text response — log it (audio is primary)."""
        logger.info("[%s] Google text: %s", self.conversation_id, text)

    async def _on_google_input_transcription(self, text: str):
        """
        Google transcribed user speech — this is our SOI proxy.
        First transcription in a turn = user started speaking.
        """
        self._input_transcript_parts.append(text)
        if not self._soi_event.is_set():
            logger.info("[%s] Input transcription (SOI proxy): %s", self.conversation_id, text)
            self._soi_event.set()

    async def _on_google_output_transcription(self, text: str):
        """Google transcribed model speech — accumulate for session summary."""
        self._output_transcript_parts.append(text)

    async def _on_google_turn_complete(self):
        """
        Google model finished its response turn.
        This implies: user spoke → Google VAD detected silence → model responded → done.
        Signal the gRPC thread to send EOI + drain audio.
        """
        logger.info("[%s] Google: Turn complete", self.conversation_id)
        self._audio_queue.put(_TURN_COMPLETE)
        self._turn_complete_event.set()

    async def _on_google_interrupted(self):
        """
        User barged in — model was interrupted mid-response.
        Signal turn complete so gRPC thread can flush partial audio and reset.
        """
        logger.info("[%s] Google: Barge-in interrupted", self.conversation_id)
        self._audio_queue.put(_TURN_COMPLETE)
        self._turn_complete_event.set()
