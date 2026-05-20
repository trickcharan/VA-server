"""
Google Gemini Live adapter — implements STSAdapter.

Bridges the gRPC server with the Google Live API.
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
import os
import queue
import threading

import aiohttp

from app.adapters.base import STSAdapter
from app.adapters.google_live.session import GoogleLiveSession
from app.audio.transcoder import ulaw_to_pcm16, pcm16_to_ulaw
from app.proto.voicevirtualagent_pb2 import VoiceVAResponse
from app.proto.byova_common_pb2 import OutputEvent, TextContent
from app.utils.EventUtils import EventUtils

logger = logging.getLogger("adapter")

# Audio chunk size for streaming back to gRPC
# 8000 Hz * 1 channel * 1 byte/sample (mu-law) * 0.02s (20ms) = 160 bytes/frame
GRPC_AUDIO_CHUNK_SIZE = 160

# Google Live API audio format constants
GOOGLE_INPUT_RATE = 16000   # 16 kHz mono PCM16 expected by Google
GOOGLE_OUTPUT_RATE = 24000  # 24 kHz mono PCM16 returned by Google
WEBEX_RATE = 8000           # 8 kHz mono mu-law from/to Webex


class GoogleLiveAdapter(STSAdapter):
    """
    Per-conversation adapter that wires a gRPC call to a Google Live session.

    Hybrid SOI/EOI using Google's auto-VAD + transcription signals:
      - First inputTranscription  → SOI (user started speaking)
      - turnComplete              → EOI + stream AI response + FINAL
      - interrupted               → barge-in: flush + reset
    """

    # Default text sent to the model to trigger a spoken greeting
    DEFAULT_GREETING_TRIGGER = "Greet the customer now."

    def __init__(self, conversation_id: str, barge_in_enabled: bool = True,
                 system_instruction: str | None = None,
                 greeting_trigger: str | None = None,
                 customer_tools: list[dict] | None = None,
                 api_base_url: str | None = None):
        self.conversation_id = conversation_id
        self.barge_in_enabled = barge_in_enabled
        self._system_instruction = system_instruction
        self._greeting_trigger = greeting_trigger if greeting_trigger is not None else self.DEFAULT_GREETING_TRIGGER
        self._customer_tools = customer_tools or []
        self._api_base_url = api_base_url

        # Asyncio event loop in a background thread
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._session: GoogleLiveSession | None = None
        self._receiver_task: asyncio.Task | None = None

        # Thread-safe response queue — Google callbacks push VoiceVAResponse
        # objects here; gRPC thread drains them via drain_responses().
        self._response_queue: queue.Queue = queue.Queue()

        # Event signalled when a turn completes (used to stream greeting)
        self._turn_complete_event = threading.Event()

        # State tracking
        self._start_of_input_sent = False
        self._end_of_input_sent = False
        self._responding = False  # True while Google is generating a response
        self._is_greeting_turn = False  # True during initial AI greeting
        self._cleaned_up = False

        # Transcript accumulation
        self._input_transcript_parts: list[str] = []
        self._output_transcript_parts: list[str] = []

        # Debug: save audio to files for analysis
        _debug_dir = os.path.join(os.path.dirname(__file__), "..", "..", "config", "audio", "debug")
        os.makedirs(_debug_dir, exist_ok=True)
        self._debug_raw_audio_file = open(os.path.join(_debug_dir, f"raw_webex_{conversation_id}.raw"), "wb")
        self._debug_caller_audio_file = open(os.path.join(_debug_dir, f"caller_audio_{conversation_id}.raw"), "wb")
        self._debug_google_audio_file = open(os.path.join(_debug_dir, f"google_audio_{conversation_id}.raw"), "wb")
        logger.info("[%s] Debug audio files in %s", conversation_id, _debug_dir)

    # ------------------------------------------------------------------
    # STSAdapter interface
    # ------------------------------------------------------------------

    def on_session_start(self, welcome_audio: bytes | None = None):
        """
        Called on SESSION_START event.
        Spins up the async loop, connects to Google, starts the receiver.
        Sends a greeting trigger text so the AI speaks first.
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

        # Ask the AI to generate a spoken greeting.
        # We poll the response queue and yield chunks as they arrive
        # so gRPC streams them to the caller in real-time.
        if self._greeting_trigger:
            logger.info("[%s] Sending greeting trigger: %s", self.conversation_id, self._greeting_trigger)
            self._is_greeting_turn = True
            self._responding = True
            self._turn_complete_event.clear()
            asyncio.run_coroutine_threadsafe(
                self._session.send_text(self._greeting_trigger), self._loop
            )
            # Stream greeting chunks to gRPC as they arrive
            while not self._turn_complete_event.is_set():
                try:
                    resp = self._response_queue.get(timeout=0.05)
                    yield resp
                except queue.Empty:
                    continue
            # Drain any remaining chunks queued after the event was set
            while not self._response_queue.empty():
                try:
                    yield self._response_queue.get_nowait()
                except queue.Empty:
                    break
            self._is_greeting_turn = False
            logger.info("[%s] Greeting complete", self.conversation_id)

    def on_session_end(self):
        """
        Called on SESSION_END event.
        Delegates to cleanup() and yields an empty response to close the gRPC stream.
        """
        self.cleanup()
        yield VoiceVAResponse()

    def on_audio(self, audio_bytes: bytes):
        """
        Called when audio_input arrives from the gRPC stream.
        Forwards audio to Google (fire-and-forget, no yields).
        Responses come back via drain_responses() on subsequent streams.
        """
        if not self._loop or not self._session or self._cleaned_up:
            return

        # Only forward meaningful audio (skip near-empty frames)
        if len(audio_bytes) <= 15:
            return

        # Debug: save raw audio BEFORE transcoding
        if self._debug_raw_audio_file:
            self._debug_raw_audio_file.write(audio_bytes)

        # Input is raw 8kHz mono mu-law — transcode to 16kHz mono PCM16 for Google
        pcm16_audio = ulaw_to_pcm16(audio_bytes, WEBEX_RATE, GOOGLE_INPUT_RATE)

        # Debug: save caller audio (transcoded)
        if self._debug_caller_audio_file:
            self._debug_caller_audio_file.write(pcm16_audio)

        # Don't forward audio while Google is responding — prevents echo loop.
        # _responding is set True when Google starts generating audio and
        # cleared in _reset_turn_state() when the turn completes.
        if self._responding:
            return

        # Forward caller audio to Google (fire-and-forget)
        asyncio.run_coroutine_threadsafe(
            self._session.send_audio(pcm16_audio), self._loop
        )

    def drain_responses(self):
        """
        Yield any VoiceVAResponse objects that Google callbacks have queued.
        Called from the gRPC thread on every process_request() invocation.
        This is how responses reach Webex — they get yielded on whatever
        gRPC stream happens to be active at the time.
        """
        while not self._response_queue.empty():
            try:
                resp = self._response_queue.get_nowait()
                yield resp
            except queue.Empty:
                break

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

        # Close debug audio files
        if self._debug_raw_audio_file:
            self._debug_raw_audio_file.close()
            self._debug_raw_audio_file = None
        if self._debug_caller_audio_file:
            self._debug_caller_audio_file.close()
            self._debug_caller_audio_file = None
        if self._debug_google_audio_file:
            self._debug_google_audio_file.close()
            self._debug_google_audio_file = None

        logger.info("[%s] Google Live session cleaned up", self.conversation_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _enqueue_response(self, resp: VoiceVAResponse):
        """Thread-safe: push a response into the queue for gRPC to drain."""
        self._response_queue.put(resp)

    def _send_eoi_if_needed(self):
        """Send END_OF_INPUT once when Google starts generating a response.

        Called from text, output-transcription, and audio callbacks —
        whichever fires first wins.
        """
        if not self._end_of_input_sent:
            self._end_of_input_sent = True
            self._responding = True
            logger.info("[%s] Sending END_OF_INPUT (Google started responding)", self.conversation_id)
            self._enqueue_response(
                EventUtils.get_va_response_for_output_event(
                    EventUtils.get_output_event(OutputEvent.EventType.END_OF_INPUT)
                )
            )

    def _reset_turn_state(self):
        """Reset state for the next conversation turn."""
        self._start_of_input_sent = False
        self._end_of_input_sent = False
        self._responding = False

    # ------------------------------------------------------------------
    # Async internals — run in background event loop
    # ------------------------------------------------------------------

    def _run_loop(self):
        """Run the asyncio event loop in the background thread."""
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _async_start(self):
        """Connect to Google Live and start receiver task."""
        self._session = GoogleLiveSession(self._system_instruction, self._customer_tools)
        await self._session.connect()

        # Start listening for responses in background
        self._receiver_task = asyncio.ensure_future(self._receive_wrapper())

    async def _receive_wrapper(self):
        """Wrap receiver to catch and log any errors."""
        try:
            logger.info("[%s] Receiver task started", self.conversation_id)
            await self._session.receive_responses(
                on_audio=self._on_google_audio,
                on_text=self._on_google_text,
                on_input_transcription=self._on_google_input_transcription,
                on_output_transcription=self._on_google_output_transcription,
                on_turn_complete=self._on_google_turn_complete,
                on_interrupted=self._on_google_interrupted,
                on_tool_call=self._on_google_tool_call,
            )
            logger.info("[%s] Receiver task finished normally", self.conversation_id)
        except asyncio.CancelledError:
            logger.info("[%s] Receiver task cancelled", self.conversation_id)
        except Exception as e:
            # WebSocket 1000 (OK) is expected when we close the session intentionally
            if "1000" in str(e):
                logger.info("[%s] Receiver stopped (session closed)", self.conversation_id)
            else:
                logger.error("[%s] Receiver task crashed: %s", self.conversation_id, e, exc_info=True)

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

    async def _on_google_tool_call(self, function_call_id: str, name: str, args: dict):
        """
        Google model invoked a function — handle it and send result back.

        Supported tools:
          - end_call: Signal session end
          - transfer_to_agent: Signal transfer to human agent
        """
        logger.info("[%s] Tool call: %s(%s)", self.conversation_id, name, args)

        if name == "end_call":
            reason = args.get("reason", "completed")
            logger.info("[%s] AI requested end_call: %s", self.conversation_id, reason)
            # Send success response so AI can say goodbye
            await self._session.send_tool_response(
                function_call_id, name, {"status": "success", "message": "Call will be ended."}
            )
            # Enqueue SESSION_END event for gRPC
            self._enqueue_response(
                EventUtils.get_va_response_for_output_event(
                    EventUtils.get_output_event(OutputEvent.EventType.SESSION_END)
                )
            )
            # Close Google WS — this breaks the receive loop cleanly
            await self._session.close()

        elif name == "transfer_to_agent":
            reason = args.get("reason", "customer_requested")
            summary = args.get("summary", "")
            logger.info("[%s] AI requested transfer_to_agent: reason=%s, summary=%s",
                        self.conversation_id, reason, summary)
            await self._session.send_tool_response(
                function_call_id, name, {"status": "success", "message": "Transferring to agent."}
            )
            # Build response with TRANSFER_TO_AGENT event + session summary
            output_event = EventUtils.get_output_event(OutputEvent.EventType.TRANSFER_TO_AGENT)
            va_response = VoiceVAResponse()
            va_response.output_events.append(output_event)
            if summary:
                session_summary = TextContent()
                session_summary.text = summary
                session_summary.language_code = "en-US"
                va_response.session_summary.CopyFrom(session_summary)
            self._enqueue_response(va_response)
            # Close Google WS — this breaks the receive loop cleanly
            await self._session.close()

        elif name == "execute_api":
            result = await self._execute_api(args)
            logger.info("[%s] execute_api result: %s", self.conversation_id, result)
            await self._session.send_tool_response(function_call_id, name, result)

        else:
            logger.warning("[%s] Unknown tool call: %s", self.conversation_id, name)
            await self._session.send_tool_response(
                function_call_id, name, {"status": "error", "message": f"Unknown tool: {name}"}
            )

    async def _execute_api(self, args: dict) -> dict:
        """Generic HTTP executor — calls the customer's REST API."""
        if not self._api_base_url:
            return {"status": "error", "message": "No API base URL configured for this agent."}

        import json as _json
        method = args.get("method", "GET").upper()
        path = args.get("path", "/")
        raw_body = args.get("body")
        body = None
        if raw_body:
            try:
                body = _json.loads(raw_body) if isinstance(raw_body, str) else raw_body
            except (_json.JSONDecodeError, TypeError):
                body = raw_body
        query = args.get("query", "")

        url = f"{self._api_base_url.rstrip('/')}{path}"
        if query:
            url = f"{url}?{query}"

        logger.info("[%s] execute_api: %s %s (body=%s)", self.conversation_id, method, url, body)

        try:
            async with aiohttp.ClientSession() as session:
                kwargs = {"timeout": aiohttp.ClientTimeout(total=10)}
                if body and method in ("POST", "PUT", "PATCH"):
                    kwargs["json"] = body
                async with session.request(method, url, **kwargs) as resp:
                    status = resp.status
                    try:
                        data = await resp.json()
                    except Exception:
                        data = await resp.text()
                    if status >= 400:
                        return {"status": "error", "http_status": status, "message": str(data)}
                    return {"status": "success", "http_status": status, "data": data}
        except asyncio.TimeoutError:
            return {"status": "error", "message": "API request timed out"}
        except Exception as e:
            logger.error("[%s] execute_api failed: %s", self.conversation_id, e)
            return {"status": "error", "message": f"API call failed: {e}"}

    async def _on_google_audio(self, audio_bytes: bytes):
        """Google sent model audio — transcode and enqueue as CHUNK for gRPC."""
        #logger.info("[%s] Received %d bytes audio from Google", self.conversation_id, len(audio_bytes))

        if not self._is_greeting_turn:
            self._send_eoi_if_needed()

        webex_audio = pcm16_to_ulaw(audio_bytes, GOOGLE_OUTPUT_RATE, WEBEX_RATE)
        if self._debug_google_audio_file:
            self._debug_google_audio_file.write(webex_audio)
        # Split into chunks and enqueue each
        for i in range(0, len(webex_audio), GRPC_AUDIO_CHUNK_SIZE):
            chunk = webex_audio[i:i + GRPC_AUDIO_CHUNK_SIZE]
            self._enqueue_response(
                EventUtils.get_audio_output_events_bytes(
                    chunk, None, self.barge_in_enabled,
                    VoiceVAResponse.ResponseType.CHUNK,
                )
            )

    async def _on_google_text(self, text: str):
        """Google sent text response — this arrives before audio, so trigger EOI."""
        logger.info("[%s] Google text: %s", self.conversation_id, text)
        if not self._is_greeting_turn:
            self._send_eoi_if_needed()

    async def _on_google_input_transcription(self, text: str):
        """
        Google transcribed user speech — this is our SOI proxy.
        First transcription in a turn = user started speaking → enqueue SOI.
        """
        self._input_transcript_parts.append(text)
        if not self._start_of_input_sent:
            self._start_of_input_sent = True
            logger.info("[%s] SOI (input transcription): %s", self.conversation_id, text)
            self._enqueue_response(
                EventUtils.get_va_response_for_output_event(
                    EventUtils.get_output_event(OutputEvent.EventType.START_OF_INPUT)
                )
            )

    async def _on_google_output_transcription(self, text: str):
        """Google transcribed model speech — also serves as EOI trigger."""
        self._output_transcript_parts.append(text)
        if not self._is_greeting_turn:
            self._send_eoi_if_needed()

    async def _on_google_turn_complete(self):
        """
        Google model finished its response turn.
        Enqueue FINAL and reset for next turn.
        (EOI was already sent when first audio arrived.)
        """
        logger.info("[%s] Google: Turn complete", self.conversation_id)
        self._enqueue_response(
            EventUtils.get_audio_output_events_bytes(
                None, None, self.barge_in_enabled,
                VoiceVAResponse.ResponseType.FINAL,
            )
        )
        self._reset_turn_state()
        # Signal the greeting polling loop to stop
        self._turn_complete_event.set()

    async def _on_google_interrupted(self):
        """
        User barged in — model was interrupted mid-response.
        Enqueue FINAL and reset for next turn.
        """
        logger.info("[%s] Google: Barge-in interrupted", self.conversation_id)
        self._enqueue_response(
            EventUtils.get_audio_output_events_bytes(
                None, None, self.barge_in_enabled,
                VoiceVAResponse.ResponseType.FINAL,
            )
        )
        self._reset_turn_state()
