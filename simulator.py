#!/usr/bin/env python3
"""
Voice Simulator — gRPC client that captures Mac microphone audio and streams
it to the Virtual Agent server, playing back the response through the speaker.

Usage:
    python simulator.py [--host localhost] [--port 8086]
    Press Ctrl+C to end the conversation.

Dependencies:
    pip install pyaudio grpcio grpcio-tools
    brew install portaudio   # macOS only
"""

import argparse
import io
import queue
import struct
import sys
import threading
import time
import uuid
import wave

import audioop

import grpc
import pyaudio

from app.proto.voicevirtualagent_pb2 import (
    VoiceVARequest,
    VoiceVAResponse,
    VoiceInput,
)
from app.proto.byova_common_pb2 import EventInput, OutputEvent

# ---------------------------------------------------------------------------
# Audio constants — match Webex CC format
# ---------------------------------------------------------------------------
SAMPLE_RATE = 44100
CHANNELS = 2
SAMPLE_WIDTH = 2  # 16-bit PCM
FRAME_BYTES = CHANNELS * SAMPLE_WIDTH  # 4 bytes per frame
CHUNK_DURATION_MS = 20
FRAMES_PER_CHUNK = int(SAMPLE_RATE * CHUNK_DURATION_MS / 1000)  # 882 frames
CHUNK_BYTES = FRAMES_PER_CHUNK * FRAME_BYTES  # 3528 bytes


def build_wav_header(data_size: int = 0x0C3598) -> bytes:
    """
    Build a WAV header identical to what Webex CC sends:
      RIFF header + fmt chunk + LIST/INFO chunk + data chunk header.
    Total: 78 bytes.
    """
    buf = bytearray()

    # RIFF header
    file_size = data_size + 78 - 8  # total file size minus RIFF+size fields
    buf += b'RIFF'
    buf += struct.pack('<I', file_size)
    buf += b'WAVE'

    # fmt chunk (16 bytes payload)
    buf += b'fmt '
    buf += struct.pack('<I', 16)                 # chunk size
    buf += struct.pack('<H', 1)                  # PCM format
    buf += struct.pack('<H', CHANNELS)           # channels
    buf += struct.pack('<I', SAMPLE_RATE)        # sample rate
    buf += struct.pack('<I', SAMPLE_RATE * FRAME_BYTES)  # byte rate
    buf += struct.pack('<H', FRAME_BYTES)        # block align
    buf += struct.pack('<H', SAMPLE_WIDTH * 8)   # bits per sample

    # LIST/INFO chunk (matches Webex: "Lavf61.7.100")
    info_payload = b'INFOISFT\x0d\x00\x00\x00Lavf61.7.100\x00'
    buf += b'LIST'
    buf += struct.pack('<I', len(info_payload))
    buf += info_payload

    # data chunk header
    buf += b'data'
    buf += struct.pack('<I', data_size)

    return bytes(buf)


# ---------------------------------------------------------------------------
# Mic audio encoding — transcode to 8kHz mono mu-law for production servers
# ---------------------------------------------------------------------------
MIC_RATE = 8000       # production expects 8kHz
MIC_CHANNELS = 1      # production expects mono


def encode_mic_to_ulaw(pcm16_data: bytes, mic_rate: int, mic_channels: int) -> bytes:
    """Transcode mic PCM16 to 8kHz mono mu-law for production servers."""
    # stereo -> mono if needed
    if mic_channels == 2:
        pcm16_data = audioop.tomono(pcm16_data, SAMPLE_WIDTH, 1, 1)
    # resample to 8kHz if needed
    if mic_rate != MIC_RATE:
        pcm16_data, _ = audioop.ratecv(pcm16_data, SAMPLE_WIDTH, 1, mic_rate, MIC_RATE, None)
    # PCM16 -> mu-law
    return audioop.lin2ulaw(pcm16_data, SAMPLE_WIDTH)


def decode_ulaw_raw(ulaw_bytes: bytes) -> bytes:
    """Decode raw 8kHz mono mu-law bytes to 44.1kHz stereo PCM16 for playback."""
    # mu-law -> 16-bit PCM
    pcm = audioop.ulaw2lin(ulaw_bytes, 2)
    # 8kHz -> 44.1kHz
    pcm, _ = audioop.ratecv(pcm, 2, 1, 8000, SAMPLE_RATE, None)
    # mono -> stereo
    pcm = audioop.tostereo(pcm, 2, 1, 1)
    return pcm


def decode_wav(wav_bytes: bytes) -> bytes:
    """Decode a WAV file (8kHz mono mu-law or other) to 44.1kHz stereo PCM16.

    Parses the WAV header to detect format, strips it, and transcodes.
    """
    if len(wav_bytes) < 12 or wav_bytes[:4] != b'RIFF':
        # Not a WAV — try as raw mu-law
        return decode_ulaw_raw(wav_bytes)

    # Parse fmt chunk
    fmt_offset = wav_bytes.find(b'fmt ')
    if fmt_offset < 0:
        return decode_ulaw_raw(wav_bytes)

    audio_format = struct.unpack_from('<H', wav_bytes, fmt_offset + 8)[0]
    n_channels = struct.unpack_from('<H', wav_bytes, fmt_offset + 10)[0]
    framerate = struct.unpack_from('<I', wav_bytes, fmt_offset + 12)[0]
    bits_per_sample = struct.unpack_from('<H', wav_bytes, fmt_offset + 20)[0]

    # Find raw audio after 'data' chunk header
    data_offset = wav_bytes.find(b'data')
    if data_offset < 0:
        return decode_ulaw_raw(wav_bytes)
    audio_data = wav_bytes[data_offset + 8:]

    is_ulaw = (audio_format == 7) or (bits_per_sample == 8 and framerate == 8000)

    print(f"[WAV: {framerate}Hz, {n_channels}ch, {bits_per_sample}bit, "
          f"{'mu-law' if is_ulaw else 'PCM'}, {len(audio_data)} bytes]")

    if is_ulaw:
        audio_data = audioop.ulaw2lin(audio_data, 2)
        bits_per_sample = 16

    sampwidth = bits_per_sample // 8

    if framerate != SAMPLE_RATE:
        audio_data, _ = audioop.ratecv(audio_data, sampwidth, n_channels, framerate, SAMPLE_RATE, None)

    if n_channels == 1:
        audio_data = audioop.tostereo(audio_data, sampwidth, 1, 1)

    return audio_data


def make_session_start_request(conversation_id: str, va_id: str) -> VoiceVARequest:
    return VoiceVARequest(
        conversation_id=conversation_id,
        virtual_agent_id=va_id,
        event_input=EventInput(event_type=EventInput.EventType.SESSION_START),
    )


def make_session_end_request(conversation_id: str, va_id: str) -> VoiceVARequest:
    return VoiceVARequest(
        conversation_id=conversation_id,
        virtual_agent_id=va_id,
        event_input=EventInput(event_type=EventInput.EventType.SESSION_END),
    )


def make_audio_request(conversation_id: str, va_id: str, audio: bytes,
                       encoding=VoiceInput.VoiceEncoding.LINEAR16_FORMAT,
                       sample_rate: int = SAMPLE_RATE) -> VoiceVARequest:
    return VoiceVARequest(
        conversation_id=conversation_id,
        virtual_agent_id=va_id,
        audio_input=VoiceInput(
            caller_audio=audio,
            encoding=encoding,
            sample_rate_hertz=sample_rate,
            language_code="en-US",
        ),
    )


class VoiceSimulator:
    def __init__(self, host: str = "localhost", port: int = 8086, va_id: str = "google-live", ssl: bool = False, token: str = ""):
        self.host = host
        self.port = port
        self.va_id = va_id
        self.ssl = ssl
        self.token = token
        self.conversation_id = str(uuid.uuid4())

        self._pa = pyaudio.PyAudio()
        self._mic_stream: pyaudio.Stream | None = None
        self._spk_stream: pyaudio.Stream | None = None

        # gRPC
        self._channel: grpc.Channel | None = None
        self._stub = None
        self._call = None  # bidirectional stream

        # Outgoing request queue — mic thread pushes, sender thread pops
        self._send_queue: queue.Queue[VoiceVARequest | None] = queue.Queue()

        # Playback queue — response thread pushes audio bytes, playback thread pops
        self._play_queue: queue.Queue[bytes | None] = queue.Queue()

        # State
        self._mic_paused = threading.Event()
        self._mic_paused.set()  # start unpaused (will pause until welcome done)
        self._running = True
        self._wav_header_sent = False

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run(self):
        print(f"Voice Simulator — press Ctrl+C to end")
        print(f"Conversation ID: {self.conversation_id}")
        print(f"Connecting to {self.host}:{self.port}...")

        if self.ssl:
            credentials = grpc.ssl_channel_credentials()
            self._channel = grpc.secure_channel(f"{self.host}:{self.port}", credentials)
        else:
            self._channel = grpc.insecure_channel(f"{self.host}:{self.port}")
        from app.proto.voicevirtualagent_pb2_grpc import VoiceVirtualAgentStub
        self._stub = VoiceVirtualAgentStub(self._channel)

        # Open bidirectional stream
        tracking_id = f"dialog_connector_simulator_{uuid.uuid4().hex}"
        metadata = [("trackingid", tracking_id)]
        if self.token:
            metadata.append(("authorization", self.token))
        print(f"trackingId: {tracking_id}")

        self._call = self._stub.ProcessCallerInput(
            self._request_iterator(),
            metadata=metadata,
        )

        # Send SESSION_START
        print("Connected. Sending SESSION_START...")
        self._send_queue.put(make_session_start_request(self.conversation_id, self.va_id))

        # Start threads
        resp_thread = threading.Thread(target=self._response_loop, daemon=True, name="resp")
        play_thread = threading.Thread(target=self._playback_loop, daemon=True, name="play")
        resp_thread.start()
        play_thread.start()

        # Open speaker output stream
        self._spk_stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=CHANNELS,
            rate=SAMPLE_RATE,
            output=True,
            frames_per_buffer=FRAMES_PER_CHUNK,
        )

        # Pause mic until welcome audio finishes
        self._mic_paused.clear()

        # Wait a moment for welcome audio, then start mic
        # (mic will be resumed by response loop after FINAL)
        try:
            self._mic_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self):
        print("\nShutting down...")
        self._running = False

        # Send SESSION_END
        try:
            self._send_queue.put(make_session_end_request(self.conversation_id, self.va_id))
            time.sleep(0.5)
        except Exception:
            pass

        # Signal sender to stop
        self._send_queue.put(None)
        self._play_queue.put(None)

        if self._mic_stream:
            try:
                self._mic_stream.stop_stream()
                self._mic_stream.close()
            except Exception:
                pass

        if self._spk_stream:
            try:
                self._spk_stream.stop_stream()
                self._spk_stream.close()
            except Exception:
                pass

        self._pa.terminate()

        if self._channel:
            self._channel.close()

        print("Done.")

    # ------------------------------------------------------------------
    # Request iterator — feeds the bidirectional gRPC stream
    # ------------------------------------------------------------------

    def _request_iterator(self):
        """Generator that yields VoiceVARequest objects from the send queue."""
        while self._running:
            try:
                req = self._send_queue.get(timeout=1.0)
                if req is None:
                    break
                yield req
            except queue.Empty:
                continue

    # ------------------------------------------------------------------
    # Mic capture loop — runs on main thread
    # ------------------------------------------------------------------

    def _mic_loop(self):
        """Capture mic audio and push to send queue."""
        mic_info = self._pa.get_default_input_device_info()
        native_channels = min(int(mic_info["maxInputChannels"]), 2)
        capture_rate = SAMPLE_RATE
        capture_frames = FRAMES_PER_CHUNK

        self._mic_stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=native_channels,
            rate=capture_rate,
            input=True,
            frames_per_buffer=capture_frames,
        )

        print("[Waiting for welcome audio...]")

        while self._running:
            # Block until mic is unpaused
            self._mic_paused.wait(timeout=0.5)
            if not self._running:
                break
            if not self._mic_paused.is_set():
                continue

            try:
                data = self._mic_stream.read(capture_frames, exception_on_overflow=False)
            except Exception:
                break

            # Always send 8kHz mono mu-law (Webex format)
            data = encode_mic_to_ulaw(data, capture_rate, native_channels)
            self._send_queue.put(
                make_audio_request(self.conversation_id, self.va_id, data,
                                   encoding=VoiceInput.VoiceEncoding.MULAW_FORMAT,
                                   sample_rate=MIC_RATE)
            )

    # ------------------------------------------------------------------
    # Response loop — runs on daemon thread
    # ------------------------------------------------------------------

    def _response_loop(self):
        """Read responses from the gRPC stream."""
        audio_buffer = bytearray()
        playing_response = False

        try:
            for response in self._call:
                if not self._running:
                    break

                # Check output events (SOI, EOI, etc.)
                for event in response.output_events:
                    if event.event_type == OutputEvent.EventType.START_OF_INPUT:
                        print("[SOI] User speaking detected")
                    elif event.event_type == OutputEvent.EventType.END_OF_INPUT:
                        print("[EOI] User finished speaking")
                    elif event.event_type == OutputEvent.EventType.SESSION_END:
                        print("[Session ended by server]")
                        self._running = False
                        break

                # Handle response types
                if response.response_type == VoiceVAResponse.ResponseType.CHUNK:
                    # CHUNK = raw 8kHz mono mu-law bytes → decode and play immediately
                    for prompt in response.prompts:
                        if prompt.audio_content:
                            pcm = decode_ulaw_raw(prompt.audio_content)
                            self._play_queue.put(pcm)
                    if not playing_response:
                        playing_response = True
                        print("[Playing response...]")

                elif response.response_type == VoiceVAResponse.ResponseType.FINAL:
                    # FINAL = complete WAV (8kHz mono mu-law with header)
                    for prompt in response.prompts:
                        if prompt.audio_content:
                            audio_buffer.extend(prompt.audio_content)
                    if audio_buffer:
                        pcm_data = decode_wav(bytes(audio_buffer))
                        duration = len(pcm_data) / (SAMPLE_RATE * FRAME_BYTES)
                        print(f"[Response complete: {duration:.1f}s audio]")
                        for i in range(0, len(pcm_data), CHUNK_BYTES):
                            self._play_queue.put(pcm_data[i:i + CHUNK_BYTES])
                        self._play_queue.put(b"__FINAL__")
                        audio_buffer.clear()
                        playing_response = False
                    else:
                        # FINAL with no audio — signal end of response
                        if playing_response:
                            self._play_queue.put(b"__FINAL__")
                            playing_response = False
                        elif self._play_queue.empty():
                            print("[Listening — speak into your mic]")
                            self._wav_header_sent = False
                            self._mic_paused.set()

        except grpc.RpcError as e:
            if self._running:
                print(f"[gRPC error: {e.code()} — {e.details()}]")
        except Exception as e:
            if self._running:
                print(f"[Response error: {e}]")

    # ------------------------------------------------------------------
    # Playback loop — runs on daemon thread
    # ------------------------------------------------------------------

    def _playback_loop(self):
        """Play audio chunks through the speaker in real-time."""
        while self._running:
            try:
                data = self._play_queue.get(timeout=1.0)
                if data is None:
                    break

                # Sentinel: response complete — resume mic
                if data == b"__FINAL__":
                    print("[Listening — speak into your mic]")
                    self._wav_header_sent = False
                    self._mic_paused.set()
                    continue

                # Play audio chunk immediately
                if self._spk_stream and self._spk_stream.is_active():
                    self._spk_stream.write(data)

            except queue.Empty:
                continue
            except Exception as e:
                print(f"[Playback error: {e}]")


def main():
    parser = argparse.ArgumentParser(description="Voice Simulator for Virtual Agent")
    parser.add_argument("--host", default="localhost", help="gRPC server host")
    parser.add_argument("--port", type=int, default=8086, help="gRPC server port")
    parser.add_argument("--ssl", action="store_true", help="Use SSL/TLS for gRPC connection")
    parser.add_argument("--token", default="", help="Authorization token for gRPC metadata")
    parser.add_argument("--va-id", default="2", help="Virtual agent ID")
    args = parser.parse_args()

    sim = VoiceSimulator(host=args.host, port=args.port, va_id=args.va_id, ssl=args.ssl, token=args.token)
    sim.run()


if __name__ == "__main__":
    main()
