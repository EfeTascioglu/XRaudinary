import asyncio
import logging
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional

import numpy as np
import os
import glob
import json

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.append(str(ROOT))

from Sound_Localization.localize_from_audio_file import main as localization_main

from faster_whisper import WhisperModel

import websockets

# -----------------------------
#  Logging
# -----------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger().setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


# -----------------------------
# Select oldest WAV in directory
# -----------------------------
def get_oldest_wav(directory: str) -> str | None:
    files = sorted(glob.glob(os.path.join(directory, "*.wav")), key=os.path.getctime)
    return files[0] if files else None


# -----------------------------
# WAV → RAW conversion
# -----------------------------
def convert_wav_to_raw(wav_path: str, raw_path: str = "audio.raw") -> tuple[np.ndarray, int]:
    """
    Converts a 3-channel WAV file to a .raw float32 file and returns
    a reshaped array along with its sample rate.
    """
    import subprocess

    # Force 3 channels, float32, 48000 Hz
    cmd = [
        "ffmpeg",
        "-y",
        "-i", wav_path,
        "-f", "f32le",
        "-acodec", "pcm_f32le",
        "-ac", "3",
        "-ar", "48000",
        raw_path
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Load raw data into NumPy
    fs = 48000
    data = np.fromfile(raw_path, dtype=np.float32)
    data = data.reshape(-1, 3)
    return data, fs

# -----------------------------
# Format output JSON
# -----------------------------
def format_output(localization_vector: np.ndarray, transcription_segments) -> str:
    """
    Formats the localization vector and transcription segments into a JSON string.
    Transcription is concatenated into a single string.
    """
    full_text = " ".join(seg.text for seg in transcription_segments)

    output = {
        "localization": localization_vector.tolist(),  # convert NumPy array to list
        "transcription": full_text
    }
    return json.dumps(output, indent=2)


# -----------------------------
# Model Initialization
# -----------------------------
def initilaize_model() -> WhisperModel:
    '''Initilize small Whisper model'''
    model = WhisperModel("small", device="cpu", compute_type="int8") 
    return model 


# -----------------------------
# Audio Processing + Server
# -----------------------------
queue = asyncio.Queue()


async def process_wavs(whisper_model, directory: str, third_channel_hardcoded_delay=0):
    while True:
        wav_path = get_oldest_wav(directory)
        if not wav_path:
            await asyncio.sleep(0.5)  # non-blocking sleep
            continue
        
        print(f"Processing: {wav_path}")

        ## 1. Convert to raw
        data, fs = convert_wav_to_raw(wav_path)

        ## 2. Run localization
        localization_vector = localization_main(data, fs, third_channel_hardcoded_delay=third_channel_hardcoded_delay)
        print(f"Localization vector: {localization_vector}")

        ## 3. Run transcription
        segments, info = whisper_model.transcribe(wav_path)
        segments = list(segments)  # materialize generator

        ## 4. Format JSON
        output_json = format_output(localization_vector, segments)
        print(f"Output JSON: \n{output_json}")

        ## 5. Push JSON to the queue for WebSocket
        await queue.put(output_json)

        ## 6. Delete processed file
        os.remove(wav_path)
        print(f"Deleted: {wav_path} \n")


async def ws_handler(websocket, path):
    """Connection handler for websocket"""
    while True:
        # Wait for new JSON from queue
        output_json = await queue.get()
        await websocket.send(output_json)


async def main_async():
    third_channel_hardcoded_delay = 0
    whisper_model = initilaize_model()
    directory = "./wav_queue"

    # Run WAV processing and WebSocket server concurrently
    server = websockets.serve(ws_handler, "0.0.0.0", 8765) # init a WebSocket Server in python

    await asyncio.gather(   # gather -> run concurrently in the event loop so the processing of the wavs and the server is running concurrently
        process_wavs(whisper_model, directory, third_channel_hardcoded_delay=third_channel_hardcoded_delay),
        server
    )

if __name__ == "__main__":
    asyncio.run(main_async())
