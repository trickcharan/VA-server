"""
Shared audio transcoding utilities.

All STS adapters use these functions to convert between the Webex CC
wire format (8 kHz mono mu-law) and whatever their provider expects.
"""

import audioop


# ── mu-law ↔ PCM16 ────────────────────────────────────────────────────

def ulaw_to_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Convert mu-law audio to 16-bit PCM and resample.

    Args:
        data: Raw mu-law bytes.
        src_rate: Source sample rate (e.g. 8000).
        dst_rate: Target sample rate (e.g. 16000).

    Returns:
        Resampled 16-bit mono PCM bytes.
    """
    pcm16 = audioop.ulaw2lin(data, 2)
    if src_rate != dst_rate:
        pcm16, _ = audioop.ratecv(pcm16, 2, 1, src_rate, dst_rate, None)
    return pcm16


def pcm16_to_ulaw(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit PCM and convert to mu-law.

    Args:
        data: Raw 16-bit mono PCM bytes.
        src_rate: Source sample rate (e.g. 24000).
        dst_rate: Target sample rate (e.g. 8000).

    Returns:
        Resampled mu-law bytes.
    """
    if src_rate != dst_rate:
        data, _ = audioop.ratecv(data, 2, 1, src_rate, dst_rate, None)
    return audioop.lin2ulaw(data, 2)


# ── Resampling ─────────────────────────────────────────────────────────

def resample(data: bytes, sampwidth: int, channels: int,
             src_rate: int, dst_rate: int) -> bytes:
    """Resample raw PCM audio.

    Args:
        data: Raw PCM bytes.
        sampwidth: Sample width in bytes (e.g. 2 for 16-bit).
        channels: Number of audio channels.
        src_rate: Source sample rate.
        dst_rate: Target sample rate.

    Returns:
        Resampled PCM bytes.
    """
    if src_rate == dst_rate:
        return data
    resampled, _ = audioop.ratecv(data, sampwidth, channels, src_rate, dst_rate, None)
    return resampled


# ── Channel conversion ─────────────────────────────────────────────────

def mono_to_stereo(data: bytes, sampwidth: int) -> bytes:
    """Duplicate a mono signal into both channels."""
    return audioop.tostereo(data, sampwidth, 1, 1)


def stereo_to_mono(data: bytes, sampwidth: int) -> bytes:
    """Mix stereo down to mono (average both channels)."""
    return audioop.tomono(data, sampwidth, 0.5, 0.5)


# ── WAV header extraction ─────────────────────────────────────────────

def strip_wav_header(data: bytes) -> bytes:
    """Strip the RIFF/WAV header and return raw audio data.

    If the data does not start with a RIFF header, it is returned as-is.
    """
    if data[:4] != b'RIFF':
        return data
    marker = data.find(b'data')
    if marker < 0:
        return data
    return data[marker + 8:]
