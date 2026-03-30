#!/usr/bin/env python3
"""
transcribe_send_whisperlivekit.py

Alternative to transcribe_send_main.py. Key differences:
  - Ingests audio as a live UDP byte stream from the ESP32 (no WAV file queue)
  - Uses WhisperLiveKit (MLX / Apple Metal backend) for real-time transcription
  - Uses localize_from_bytes for sound source localization

ESP32 byte-stream format (UDP port 30000):
  Packets of 512 frames × 3 channels × 4 bytes = 6 144 bytes
  Each int32 sample encodes:
    bits 31-6  — audio data (18-bit left-justified in 32-bit)
    bits  5-3  — timing index (0-7); all 3 samples in a frame must share the same value
    bits  2-1  — channel ID  (0=MIC0 Left, 1=MIC0 Right, 2=MIC1 Center)
    bit   0    — padding

WebSocket output (port 8765):
  JSON {"localization": [x,y,z], "transcription": "..."}
"""

import asyncio
import json
import logging
import socket
import sys
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import websockets

ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(ROOT))

from Sound_Localization.localize_from_bytes import main as localize_from_bytes
from whisperlivekit import AudioProcessor, TranscriptionEngine

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger().setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
UDP_PORT            = 30001
WS_PORT             = 8765
ESP32_SAMPLE_RATE   = 48000
WHISPER_SAMPLE_RATE = 16000
NUM_CHANNELS        = 3
DOWNSAMPLE_RATIO    = ESP32_SAMPLE_RATE // WHISPER_SAMPLE_RATE  # 3

# Accumulate ~0.5 s of 3-channel audio before each localization run
LOCALIZATION_MIN_FRAMES = ESP32_SAMPLE_RATE // 2  # 24 000 frames

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_ws_clients: set        = set()
_latest_localization    = None   # [x, y, z] of strongest source
_latest_transcription   = ""
_loc_buffer: bytearray  = bytearray()  # 3-ch int16 PCM accumulator for localization


# ---------------------------------------------------------------------------
# ESP32 byte-stream helpers
# ---------------------------------------------------------------------------

def validate_and_strip_tags(data: bytes) -> Tuple[bytes, int, int]:
    """
    Validate timing alignment and strip channel-ID / timing-index tag bits.

    A valid frame is a triplet [L, R, C] where all three int32 samples share
    the same timing index (bits 3-5).  Frames that fail this check are
    discarded (same strategy as PlatformIO/server/app.py).

    After validation, bits 1-5 are zeroed so downstream code sees clean audio.

    Returns
    -------
    cleaned_bytes    : validated int32 samples with tag bits cleared
    frames_kept      : number of valid frames retained
    frames_discarded : number of frames dropped due to timing mismatch
    """
    samples = np.frombuffer(data, dtype=np.int32).copy()
    n_frames = len(samples) // NUM_CHANNELS
    if n_frames == 0:
        return b"", 0, 0

    frames = samples[: n_frames * NUM_CHANNELS].reshape(n_frames, NUM_CHANNELS)
    timing = (frames >> 3) & 0x7  # bits 3-5

    valid     = (timing[:, 0] == timing[:, 1]) & (timing[:, 1] == timing[:, 2])
    kept      = frames[valid].copy()
    discarded = n_frames - len(kept)

    if len(kept) == 0:
        return b"", 0, discarded

    # Zero bits 1-5: channel tag (bits 1-2) and timing index (bits 3-5)
    # 0x3E = 0b0011_1110 covers exactly bits 1-5
    kept &= ~np.int32(0x3E)
    return kept.flatten().tobytes(), len(kept), discarded


def int32_to_int16_3ch(cleaned_i32_bytes: bytes) -> np.ndarray:
    """
    Convert cleaned 3-channel interleaved int32 samples to int16 by
    right-shifting 16 bits (takes the top 16 bits of the 18-bit audio).
    Returns a 1-D int16 array, still interleaved as L R C L R C …
    """
    i32 = np.frombuffer(cleaned_i32_bytes, dtype=np.int32)
    return np.clip(i32 >> 16, -32768, 32767).astype(np.int16)


def extract_mono_for_whisper(cleaned_i32_bytes: bytes) -> bytes:
    """
    Extract channel 0 (Left mic) from cleaned 3-channel int32 data,
    decimate 48 kHz → 16 kHz (factor-of-3 decimation is sufficient for
    speech-band content), and return as s16le bytes for WhisperLiveKit.
    """
    i32     = np.frombuffer(cleaned_i32_bytes, dtype=np.int32)
    ch0     = i32[0::NUM_CHANNELS]           # Left channel (every 3rd sample)
    ch0_dn  = ch0[::DOWNSAMPLE_RATIO]        # 48 kHz → 16 kHz
    return np.clip(ch0_dn >> 16, -32768, 32767).astype(np.int16).tobytes()


# ---------------------------------------------------------------------------
# UDP receiver
# ---------------------------------------------------------------------------

async def udp_receiver(audio_q: asyncio.Queue) -> None:
    """Receive ESP32 UDP packets, validate + strip tags, push to audio_q."""
    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", UDP_PORT))
    sock.setblocking(False)
    logger.info(f"UDP receiver bound to port {UDP_PORT}")

    while True:
        try:
            data = await loop.sock_recv(sock, 65536)
            if len(data) % (NUM_CHANNELS * 4) != 0:
                logger.debug(f"Skipping malformed packet: {len(data)} bytes")
                continue
            cleaned, kept, dropped = validate_and_strip_tags(data)
            if dropped > 0:
                logger.debug(f"Timing mismatch: {dropped} frames dropped")
            if kept > 0:
                await audio_q.put(cleaned)
        except Exception as exc:
            logger.error(f"UDP receiver error: {exc}")
            await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# Audio pipeline — feeds both WhisperLiveKit and the localization accumulator
# ---------------------------------------------------------------------------

async def audio_pipeline(audio_q: asyncio.Queue, processor: AudioProcessor) -> None:
    """
    For each validated packet:
      1. Feed mono 16 kHz s16le PCM to WhisperLiveKit (transcription).
      2. Accumulate 3-channel int16 PCM in _loc_buffer (localization).
      3. Once ~0.5 s is buffered, run localization off-thread and update
         _latest_localization.
    """
    global _latest_localization, _loc_buffer

    while True:
        cleaned: bytes = await audio_q.get()

        # 1. Transcription — feed mono 16 kHz s16le to WhisperLiveKit
        await processor.process_audio(extract_mono_for_whisper(cleaned))

        # 2. Localization accumulator — 3-channel int16 at 48 kHz
        _loc_buffer.extend(int32_to_int16_3ch(cleaned).tobytes())

        # 3. Run localization when enough audio is available
        min_bytes = LOCALIZATION_MIN_FRAMES * NUM_CHANNELS * 2  # 2 bytes per int16
        if len(_loc_buffer) < min_bytes:
            continue

        pcm_snapshot = bytes(_loc_buffer)
        _loc_buffer.clear()

        result = await asyncio.to_thread(
            localize_from_bytes,
            pcm_snapshot,
            ESP32_SAMPLE_RATE,
            NUM_CHANNELS,
            2,  # bytes_per_sample — int16
        )
        if result:
            _latest_localization = list(result[0][0])  # strongest source position only
            logger.info(f"Localization updated: {_latest_localization}")


# ---------------------------------------------------------------------------
# WebSocket server
# ---------------------------------------------------------------------------

async def ws_handler(websocket) -> None:
    """Track connected clients; they receive all broadcasts."""
    _ws_clients.add(websocket)
    logger.info(f"[WS] Client connected:    {websocket.remote_address}")
    try:
        await websocket.wait_closed()
    finally:
        _ws_clients.discard(websocket)
        logger.info(f"[WS] Client disconnected: {websocket.remote_address}")


async def ws_broadcaster(broadcast_q: asyncio.Queue) -> None:
    """Forward messages from broadcast_q to every connected WebSocket client."""
    while True:
        message = await broadcast_q.get()
        if _ws_clients:
            await asyncio.gather(
                *[ws.send(message) for ws in list(_ws_clients)],
                return_exceptions=True,
            )


# ---------------------------------------------------------------------------
# Transcription result consumer
# ---------------------------------------------------------------------------

async def transcription_consumer(results_generator, broadcast_q: asyncio.Queue) -> None:
    """
    Consume FrontData objects emitted by WhisperLiveKit and broadcast a
    combined JSON payload (transcription + latest localization result) to
    all connected WebSocket clients.
    """
    global _latest_transcription, _latest_localization

    async for front_data in results_generator:
        confirmed = " ".join(seg.text for seg in front_data.lines if seg.text)
        buffer    = front_data.buffer_transcription or ""
        full_text = (confirmed + " " + buffer).strip()

        # Only broadcast when something has changed
        if full_text == _latest_transcription and _latest_localization is None:
            continue

        _latest_transcription = full_text
        message = json.dumps({
            "localization":  _latest_localization,
            "transcription": full_text,
        })
        await broadcast_q.put(message)
        open("log.txt", "a").write(message + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async() -> None:
    broadcast_q: asyncio.Queue = asyncio.Queue()
    audio_q:     asyncio.Queue = asyncio.Queue()

    # TranscriptionEngine is a singleton — initialised once, shared across tasks.
    # backend="mlx-whisper" selects the Apple Metal (MLX) GPU backend.
    # pcm_input=True bypasses FFmpeg and expects raw s16le PCM at 16 kHz mono.
    engine = TranscriptionEngine(
        backend    = "auto",
        model_size = "small",
        pcm_input  = True,
        lan        = "auto",
        vac        = True,
    )
    processor   = AudioProcessor(transcription_engine=engine)
    results_gen = await processor.create_tasks()

    logger.info(f"WebSocket server starting on port {WS_PORT}")
    await asyncio.gather(
        udp_receiver(audio_q),
        audio_pipeline(audio_q, processor),
        ws_broadcaster(broadcast_q),
        transcription_consumer(results_gen, broadcast_q),
        websockets.serve(ws_handler, "0.0.0.0", WS_PORT),
    )


if __name__ == "__main__":
    # Determine local IP (the address the ESP32 should target)
    _s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        _s.connect(("8.8.8.8", 80))
        local_ip = _s.getsockname()[0]
    except Exception:
        local_ip = "unknown"
    finally:
        _s.close()

    print("AudioVision — WhisperLiveKit pipeline")
    print(f"  UDP input : 0.0.0.0:{UDP_PORT}  (ESP32 3-ch int32 @ {ESP32_SAMPLE_RATE} Hz)")
    print(f"  WebSocket : 0.0.0.0:{WS_PORT}   (JSON output to Unity/AR client)")
    print(f"  Your IP   : {local_ip}  ← set this as SERVER_IP in main.cpp")
    asyncio.run(main_async())
