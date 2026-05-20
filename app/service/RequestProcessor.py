import logging
import os
import time

from app.adapters import create_adapter
from app.audio.transcoder import strip_wav_header
from app.config.agent_config import AgentConfig
from app.proto.voicevirtualagent_pb2 import VoiceVAResponse
from app.proto.byova_common_pb2 import OutputEvent, EventInput
from app.utils.EventUtils import EventUtils

logger = logging.getLogger("request-processor")


class RequestProcessor:

    def __init__(self, conversation_id, virtual_agent_id, tracking_id=None):
        self.conversation_id = conversation_id
        self.virtual_agent_id = virtual_agent_id
        self.tracking_id = tracking_id
        self.start_time = time.time()
        self.start_of_input_sent = False
        self.can_be_deleted = False
        self.save_audio = False
        self.is_barge_in_enabled = False
        self.adapter = None

    def process_request(self, request):
        event_type = request.WhichOneof("voice_va_input_type")

        if event_type == "dtmf_input":
            print("Received DTMF input")
            yield from self._process_dtmf_event(request.dtmf_input)

        elif event_type == "event_input":
            yield from self._process_event_input(request.event_input)

        elif event_type == "audio_input":
            yield from self._process_audio_event(request.audio_input.caller_audio)

    def _process_dtmf_event(self, dtmf_event):
        if len(dtmf_event.dtmf_events) == 0:
            response = EventUtils.get_va_response_for_output_event(
                EventUtils.get_output_event(OutputEvent.EventType.NO_INPUT)
            )
            yield response
        # Below is an example for single-digit input. For multiple digits, delimit with the termination character.
        for dtmf_digit in dtmf_event.dtmf_events:
            if dtmf_digit == 5:
                response = EventUtils.get_va_response_for_output_event(
                    EventUtils.get_output_event(OutputEvent.EventType.TRANSFER_TO_AGENT)
                )
                yield response
            elif dtmf_digit == 6:
                response = EventUtils.get_va_response_for_output_event(
                    EventUtils.get_output_event(OutputEvent.EventType.SESSION_END)
                )
                yield response
            # More cases can be added based on requirements
            else:
                pass

    def _process_event_input(self, event_input):
        if event_input.event_type == EventInput.EventType.SESSION_START:
            logger.info("[%s] Received SESSION_START", self.conversation_id)
            # Load per-agent config and create the right STS adapter
            try:
                config = AgentConfig.load(self.virtual_agent_id)
                self.adapter = create_adapter(
                    provider=config.provider,
                    conversation_id=self.conversation_id,
                    barge_in_enabled=self.is_barge_in_enabled,
                    system_instruction=config.system_instruction,
                    customer_tools=config.tools,
                    api_base_url=config.api_base_url,
                )
                welcome_audio = self._load_welcome_audio(config.welcome_audio_path)
                yield from self.adapter.on_session_start(welcome_audio=welcome_audio)
                logger.info("[%s] Adapter ready (provider=%s)", self.conversation_id, config.provider)
            except Exception as e:
                logger.error("[%s] Failed to start adapter: %s", self.conversation_id, e, exc_info=True)
                self.adapter = None

        elif event_input.event_type == EventInput.EventType.SESSION_END:
            logger.info("[%s] Received SESSION_END", self.conversation_id)
            self.can_be_deleted = True
            if self.adapter:
                yield from self.adapter.on_session_end()
                self.adapter = None
            else:
                yield VoiceVAResponse()

    def _process_audio_event(self, audio_byte):
        if self.adapter:
            self.adapter.on_audio(audio_byte)
            # Drain any responses Google has queued (SOI, CHUNKs, EOI, FINAL)
            yield from self.adapter.drain_responses()
        else:
            print(f"[{self.conversation_id}] No adapter — ignoring audio")

    def _load_welcome_audio(self, audio_path: str | None) -> bytes | None:
        """Load welcome prompt audio as raw 8kHz mono mu-law.

        Returns None if no welcome_audio_path is configured for the agent.
        """
        if audio_path is None:
            return None
        try:
            with open(audio_path, "rb") as f:
                wav_data = f.read()
        except FileNotFoundError:
            logger.warning("Welcome audio not found at %s", audio_path)
            return None

        ulaw_bytes = strip_wav_header(wav_data)
        logger.info("Welcome audio: %d bytes raw 8kHz mono mu-law", len(ulaw_bytes))
        return ulaw_bytes

    def cleanup(self):
        """Release resources when the gRPC stream ends. Idempotent."""
        if self.adapter:
            self.adapter.cleanup()
            self.adapter = None
