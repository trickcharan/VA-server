import logging
import time

from downstream.proto.voicevirtualagent_pb2 import VoiceVAResponse
from downstream.proto.byova_common_pb2 import OutputEvent, EventInput
from downstream.utils.EventUtils import EventUtils
from downstream.service.google_live_adapter import GoogleLiveAdapter

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
            # Create adapter and connect to Google Live API
            try:
                self.adapter = GoogleLiveAdapter(
                    conversation_id=self.conversation_id,
                    barge_in_enabled=self.is_barge_in_enabled,
                )
                yield from self.adapter.on_session_start()
                logger.info("[%s] Adapter ready", self.conversation_id)
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
        print(f"Received audio chunk of size {len(audio_byte)}")
        if self.adapter:
            yield from self.adapter.on_audio(audio_byte)
        else:
            print(f"[{self.conversation_id}] No adapter — ignoring audio")

    def cleanup(self):
        """Release resources when the gRPC stream ends. Idempotent."""
        if self.adapter:
            self.adapter.cleanup()
            self.adapter = None
