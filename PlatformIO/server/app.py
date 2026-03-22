from __future__ import annotations

import argparse
import os
import base64
import io
import json
import math
import socket
import struct
import threading
import time
import urllib.request
import wave

from collections import deque
from queue import Queue
from typing import Dict, Optional, Tuple


import numpy as np
from flask import Flask, Response, jsonify, request, send_file

from typing import List

from audio_transcription.whisper_live_kit.audio_main import run as run_audio_main


app = Flask(__name__)

# UDP settings
UDP_PORT_COMBINED = 30001  # Single port for all 3 channels combined
packet_counter = 0         # Track total packets received
sample_counter = 0         # Track total samples received
packet_errors = 0          # Track errors

# Diagnostics for playback speed issues
diagnostics_lock = threading.Lock()
total_input_frames = 0     # Total frames received from ESP32
total_output_frames = 0    # Total frames after validation
total_retention_ratio = 0.0
total_playback_speed_ratio = 0.0
playback_diagnostics_count = 0
last_stop_reason = "none"
total_frames_skipped = 0

# Metrics for sample-perfect alignment
packet_arrival_times = []  # Track packet arrival times for latency analysis
sync_lock = threading.Lock()

latest_lock = threading.Lock()
latest_packet: Dict[str, object] = {
    "device_id": "udp-combined",
    "sample_rate": 48000,
    "channels": 3,
    "bits": 32,
    "format": "interleaved",
    "timestamp": None,
    "data": None,
    "rms_l": 0.0,         # Left channel (MIC0 left)
    "rms_r": 0.0,         # Right channel (MIC0 right)
    "rms_c": 0.0,         # Center channel (MIC1 mono)
}

subscribers: "list[Queue]" = []

# Circular buffer for last 5 seconds of audio - combined 3-channel interleaved
audio_buffer_lock = threading.Lock()
audio_buffer: deque = deque(maxlen=3000)  # Combined 3-channel interleaved [L,R,C,L,R,C,...]
buffer_sample_rate = 48000  # ESP32 uses 48kHz
buffer_channels = 3         # 3-channel interleaved output
buffer_bits = 32            # ESP32 sends 32-bit samples


def _is_port_available(port: int) -> bool:
    """Check if a port is available for binding."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        sock.close()
        return True
    except OSError:
        return False


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _compute_rms(data: bytes, channels: int, bits_per_sample: int = 16) -> Tuple[float, ...]:
    if not data or channels <= 0:
        return tuple()
    bytes_per_sample = bits_per_sample // 8
    samples = len(data) // bytes_per_sample
    frames = samples // channels
    if frames == 0:
        return tuple()

    acc = [0.0] * channels
    idx = 0
    for _ in range(frames):
        for ch in range(channels):
            sample = int.from_bytes(data[idx : idx + bytes_per_sample], "little", signed=True)
            acc[ch] += float(sample * sample)
            idx += bytes_per_sample

    return tuple((acc[ch] / frames) ** 0.5 for ch in range(channels))


def _diagnose_packet_structure(data: bytes, max_samples: int = 10) -> str:
    """
    Diagnostic tool: Analyze packet structure and display in hex/decimal.
    
    Shows:
    - Byte-by-byte hex and decimal dump
    - Frame boundaries and calculations
    - Detected anomalies (extra bytes, misaligned data)
    - Sample tags and values
    
    Args:
        data: Raw packet bytes
        max_samples: Number of samples to display (default 10)
    
    Returns:
        Diagnostic string report
    """
    try:
        packet_size = len(data)
        expected_frames = packet_size // 12  # 3 channels * 4 bytes
        remainder = packet_size % 12
        
        report = []
        report.append(f"\n=== PACKET STRUCTURE DIAGNOSTIC ===")
        report.append(f"Total bytes: {packet_size}")
        report.append(f"Expected frames (bytes/12): {expected_frames}")
        report.append(f"Remainder bytes: {remainder}")
        
        if remainder > 0:
            report.append(f"⚠ ANOMALY: {remainder} extra bytes (not multiple of 12)")
            report.append(f"  Last {min(remainder, 16)} bytes (hex): {data[-min(remainder, 16):].hex(' ')}")
            report.append(f"  Last {min(remainder, 16)} bytes (dec): {', '.join(str(b) for b in data[-min(remainder, 16):])}")
        
        # Show first few samples with tags
        samples_to_show = min(max_samples, packet_size // 4)
        report.append(f"\nFirst {samples_to_show} samples:")
        report.append("Idx | Hex (LE)       | Decimal      | Tag | Value (tag cleared)")
        report.append("-" * 70)
        
        for i in range(samples_to_show):
            offset = i * 4
            sample_bytes = data[offset:offset+4]
            if len(sample_bytes) < 4:
                break
            
            sample_int = int.from_bytes(sample_bytes, byteorder='little', signed=True)
            tag = (sample_int >> 1) & 0x3
            value_cleaned = sample_int & ~0x6
            hex_str = sample_bytes.hex().upper()
            
            report.append(f"{i:3d} | {hex_str} | {sample_int:12d} | {tag} | {value_cleaned:12d}")
        
        report.append(f"\nTag legend: 0=Ch0(L), 1=Ch1(R), 2=Ch2(C), 3=Invalid")
        report.append("=" * 70)
        
        return "\n".join(report)
    except Exception as e:
        return f"Diagnostic error: {e}"


def _detect_packet_format(data: bytes) -> str:
    """
    Detect if packet has an unexpected header or format.
    
    Analyzes first 16 bytes to identify patterns that might indicate:
    - A packet header (magic number, frame counter, etc.)
    - Protocol overhead
    - Alignment padding
    
    Returns:
        Analysis string
    """
    try:
        if len(data) < 16:
            return "Packet too small for format detection"
        
        first_16 = data[:16]
        report = []
        report.append(f"\n=== PACKET FORMAT DETECTION ===")
        report.append(f"First 16 bytes (hex): {first_16.hex(' ').upper()}")
        report.append(f"First 16 bytes (dec): {', '.join(str(b) for b in first_16)}")
        
        # Check for common magic numbers or patterns
        magic_32 = int.from_bytes(first_16[0:4], byteorder='little')
        magic_16 = int.from_bytes(first_16[0:2], byteorder='little')
        
        report.append(f"\nFirst 4 bytes as uint32 (LE): 0x{magic_32:08X} ({magic_32})")
        report.append(f"First 2 bytes as uint16 (LE): 0x{magic_16:04X} ({magic_16})")
        
        # Check if it looks like a header (values that are not typical audio samples)
        if magic_32 == 0xDEADBEEF:
            report.append(f"⚠ DETECTED: 0xDEADBEEF header (packet validation marker)")
        elif magic_32 > 0x10000000:  # Very large value (unlikely for a 32-bit audio sample)
            report.append(f"⚠ POSSIBLE HEADER: First value looks like metadata (0x{magic_32:08X})")
        
        # Check if data is 4-byte aligned
        size = len(data)
        if size % 4 == 0:
            report.append(f"✓ 4-byte aligned: {size} bytes")
        else:
            report.append(f"✗ NOT 4-byte aligned: {size} bytes (remainder: {size % 4})")
        
        if size % 12 == 0:
            report.append(f"✓ 12-byte aligned (3-channel frames): {size // 12} frames")
        else:
            remainder = size % 12
            report.append(f"✗ NOT 12-byte aligned: {remainder} extra bytes")
        
        report.append("=" * 70)
        return "\n".join(report)
    except Exception as e:
        return f"Format detection error: {e}"


def _validate_and_strip_channel_tags(data: bytes) -> tuple: # TODO: Look into manually. no copilot
    """
    Extract channel identification tags and timing index from each sample.
    Validates frames (triplets of L, R, C) with intelligent resynchronization.
    
    The ESP32 encodes:
    - Bits 1-2: 2-bit channel ID (0=MIC0L, 1=MIC0R, 2=MIC1C, 3=reserved/invalid)
    - Bits 3-5: 3-bit timing index (increments per packet, marks which transmission cluster)
    
    Frame validation criterion: All 3 samples in a frame must share the same timing index
    (indicates they came from the same synchronized I2S read). Channel tags are validated
    but not required to be [0,1,2] - timing alignment is primary criterion.
    
    Intelligent resynchronization:
    1. Process frames sequentially
    2. When a frame fails timing validation, enter "resync mode"
    3. In resync mode, skip frames until finding matching timing indices
    4. Resume accepting frames from the resync point
    5. This prevents discarding entire packets during corruption bursts
    
    Returns:
        (cleaned_data_bytes, frames_kept, frames_discarded)
    """
    try:
        samples = np.frombuffer(data, dtype=np.int32).copy()  # Copy to safely modify
        
        if len(samples) == 0:
            return (data, 0, 0)
        
        # Extract channel tags (bits 1-2) and timing indices (bits 3-5) for ALL samples
        channels = (samples >> 1) & 0x3      # 0=L, 1=R, 2=C
        timing_indices = (samples >> 3) & 0x7
        
        # Input diagnostics
        input_frame_count = len(samples) // 3
        input_bytes = len(samples) * 4
        
        # Build timing sequence string for diagnostics
        timing_str = ''.join([str(ti) for ti in timing_indices[:min(100, len(timing_indices))]])
        channel_str = ''.join(['LRC'[ch] if ch < 3 else '?' for ch in channels[:min(100, len(channels))]])
        
        # Algorithm:
        # 1. Find first cluster of 3 samples with: same timing index + all L,R,C channels
        # 2. Every 3 samples afterwards should form a frame (same timing index + L,R,C)
        # 3. Stop when no valid frame is found or end of data
        
        kept_frames = []
        frame_idx = 0
        first_frame_found = False
        first_frame_position = -1
        frames_skipped_finding_first = 0
        frames_skipped_processing = 0
        stop_reason = "none"
        
        # Step 1: Search for first valid frame
        while frame_idx <= len(samples) - 3:
            frame_samples = samples[frame_idx:frame_idx+3]
            frame_channels = channels[frame_idx:frame_idx+3]
            frame_timings = timing_indices[frame_idx:frame_idx+3]
            
            # Check if all 3 samples have same timing index
            if frame_timings[0] == frame_timings[1] == frame_timings[2]:
                # Check if we have all three channels (L=0, R=1, C=2)
                channels_present = set(frame_channels)
                if channels_present == {0, 1, 2}:
                    # Found first valid frame!
                    frame_dict = {ch: s for ch, s in zip(frame_channels, frame_samples)}
                    frame = np.array([frame_dict[0], frame_dict[1], frame_dict[2]], dtype=np.int32)
                    kept_frames.append(frame)
                    first_frame_found = True
                    first_frame_position = frame_idx
                    frames_skipped_finding_first = frame_idx // 3
                    frame_idx += 3
                    break
            
            frame_idx += 1
        
        # Step 2: Process remaining frames (every 3 samples should form a valid frame)
        if first_frame_found:
            while frame_idx <= len(samples) - 3:
                frame_samples = samples[frame_idx:frame_idx+3]
                frame_channels = channels[frame_idx:frame_idx+3]
                frame_timings = timing_indices[frame_idx:frame_idx+3]
                
                # Check if all 3 samples have same timing index
                if frame_timings[0] == frame_timings[1] == frame_timings[2]:
                    # Check if we have all three channels
                    channels_present = set(frame_channels)
                    if channels_present == {0, 1, 2}:
                        # Valid frame
                        frame_dict = {ch: s for ch, s in zip(frame_channels, frame_samples)}
                        frame = np.array([frame_dict[0], frame_dict[1], frame_dict[2]], dtype=np.int32)
                        kept_frames.append(frame)
                        frame_idx += 3
                    else:
                        # Missing a channel type, stop processing
                        frames_skipped_processing = (len(samples) - frame_idx) // 3
                        stop_reason = "missing_channel_type"
                        break
                else:
                    # Timing mismatch across samples, stop processing
                    frames_skipped_processing = (len(samples) - frame_idx) // 3
                    stop_reason = "timing_mismatch"
                    break
        
        if len(kept_frames) == 0:
            return (b'', 0, len(samples) // 3)
        
        # Output diagnostics
        output_frame_count = len(kept_frames)
        output_bytes = output_frame_count * 3 * 4
        retention_ratio = output_bytes / input_bytes if input_bytes > 0 else 0.0
        expected_duration_sec = input_frame_count / 48000.0
        actual_duration_sec = output_frame_count / 48000.0
        playback_speed_ratio = expected_duration_sec / actual_duration_sec if actual_duration_sec > 0 else float('inf')
        
        # Log diagnostics
        # print(f"\n[FRAME VALIDATION]")
        # print(f"  Input: {input_frame_count} frames ({input_bytes} bytes)")
        # print(f"  Output: {output_frame_count} frames ({output_bytes} bytes)")
        # print(f"  Retention: {retention_ratio*100:.1f}%")
        # print(f"  First frame position: sample {first_frame_position} (skipped {frames_skipped_finding_first} frames)")
        # print(f"  Stop reason: {stop_reason} (remaining frames skipped: {frames_skipped_processing})")
        # print(f"\n[PLAYBACK ANALYSIS]")
        # print(f"  Expected duration: {expected_duration_sec:.4f} sec ({input_frame_count} frames @ 48kHz)")
        # print(f"  Actual duration:   {actual_duration_sec:.4f} sec ({output_frame_count} frames @ 48kHz)")
        # print(f"  Playback speed ratio: {playback_speed_ratio:.2f}x (1.0 = normal, 2.0 = double-speed)")
        # print(f"\n[TIMING SEQUENCE]")
        # print(f"  Timing indices: {timing_str}...")
        # print(f"  Channels: {channel_str}...")
        
        # Stack kept frames and clear both tag bits (1-2) and timing index bits (3-5)
        kept_samples = np.array(kept_frames)
        kept_samples = kept_samples & ~0x6              # Clear bits 1-2 (channel tags)
        kept_samples = kept_samples & ~0x38             # Clear bits 3-5 (timing index)
        
        # Flatten back to 1D
        cleaned_samples = kept_samples.flatten()
        cleaned_data = cleaned_samples.astype(np.int32).tobytes()
        
        frames_kept = len(kept_frames)
        frames_discarded = (len(samples) // 3) - frames_kept
        
        # Update global diagnostics
        global total_input_frames, total_output_frames, total_retention_ratio, total_playback_speed_ratio
        global playback_diagnostics_count, last_stop_reason, total_frames_skipped
        with diagnostics_lock:
            total_input_frames += input_frame_count
            total_output_frames += output_frame_count
            total_retention_ratio += retention_ratio
            total_playback_speed_ratio += playback_speed_ratio
            playback_diagnostics_count += 1
            last_stop_reason = stop_reason
            total_frames_skipped += frames_discarded
        
        return (cleaned_data, frames_kept, frames_discarded)
    except Exception as e:
        # On error, return original data with tags cleared
        try:
            samples = np.frombuffer(data, dtype=np.int32).copy()
            samples = samples & ~0x6
            return (samples.astype(np.int32).tobytes(), (len(samples) // 3), 0)
        except:
            return (data, 0, 0)


def udp_receiver_combined(port: int = 30000):
    """
    Consolidated UDP receiver for 3-channel audio in interleaved format.
    
    Replaces three separate UDP receivers with a single atomic thread that:
    - Listens on a single UDP port for 3-channel packets
    - Receives interleaved 32-bit samples: [L,R,C,L,R,C,...]
    - Validates packets are multiples of 12 bytes (3 channels × 4 bytes per sample)
    - Computes RMS for all 3 channels from interleaved data
    - Stores packets in audio_buffer deque
    - Updates latest_packet dict with timestamp, base64 data, and RMS values
    - Thread-safe with audio_buffer_lock and latest_lock
    - Logs statistics every 3 seconds
    - Updates packet_counter, sample_counter, packet_errors counters
    
    Args:
        port: UDP port to listen on (default 30000)
    """
    global packet_counter, sample_counter, packet_errors
    
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    
    # For Windows compatibility, also try SO_REUSEPORT if available
    if hasattr(socket, 'SO_REUSEPORT'):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (OSError, AttributeError):
            pass
    
    # Exponential backoff retry logic for binding
    max_retries = 3
    for attempt in range(max_retries):
        try:
            sock.bind(("0.0.0.0", port))
            sock.settimeout(1.0)
            print(f"[Combined UDP Receiver] Listening on port {port} for 3-channel interleaved audio")
            break
        except OSError as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # 1s, 2s, 4s
                print(f"[Combined UDP] Failed to bind port {port} (attempt {attempt+1}/{max_retries}): {e}")
                print(f"[Combined UDP] Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                print(f"[Combined UDP] FATAL: Could not bind port {port} after {max_retries} attempts: {e}")
                print(f"[Combined UDP] Port may be in use or requires elevated privileges")
                packet_errors += 1000  # Mark as critical error
                return
    
    last_log_time = time.time()
    
    while True:
        try:
            data, addr = sock.recvfrom(65536)  # Max UDP packet size
            packet_size = len(data)
            
            if packet_size > 0:
                # # ===== RAW UDP SOCKET BYTE LOGGING (NO PROCESSING YET) =====
                # print(f"\n[RAW UDP] Received {packet_size} bytes from {addr[0]}:{addr[1]}")
                
                # # Show hex dump of first 48 bytes (12 samples)
                # hex_dump = data[:48].hex()
                # hex_formatted = ' '.join([hex_dump[i:i+2] for i in range(0, len(hex_dump), 2)])
                # print(f"[RAW HEX] {hex_formatted}...")
                
                # # Show decimal of first 12 bytes (3 samples)
                # decimal_vals = list(data[:12])
                # print(f"[RAW DEC] {decimal_vals}")
                
                # # Parse and show first 3 samples (1 frame) with channel tags and timing index
                # if packet_size >= 12:
                #     samples = []
                #     for i in range(3):
                #         byte_offset = i * 4
                #         # Little-endian 32-bit signed integer
                #         sample_bytes = data[byte_offset:byte_offset+4]
                #         sample = int.from_bytes(sample_bytes, byteorder='little', signed=True)
                #         channel_tag = (sample >> 1) & 0x3  # Extract bits 1-2
                #         timing_idx = (sample >> 3) & 0x7   # Extract bits 3-5
                #         samples.append((sample, channel_tag, timing_idx))
                    
                #     tag_names = {0: "L", 1: "R", 2: "C", 3: "?"}
                #     print(f"[FRAME 0] Sam0: tag={tag_names[samples[0][1]]}, timing={samples[0][2]}, raw=0x{samples[0][0]:08x}")
                #     print(f"[FRAME 0] Sam1: tag={tag_names[samples[1][1]]}, timing={samples[1][2]}, raw=0x{samples[1][0]:08x}")
                #     print(f"[FRAME 0] Sam2: tag={tag_names[samples[2][1]]}, timing={samples[2][2]}, raw=0x{samples[2][0]:08x}")
                    
                # # Expected frame count
                # expected_frames = packet_size // 12
                # print(f"[PKT INFO] Packet size: {packet_size} bytes → {expected_frames} frames (TODO: validate after tagging)")
                # # ===== END RAW LOGGING =====
                
                # # ===== EXTRACT AND LOG CHANNEL TAG SEQUENCE =====
                # # Show which mics are in the packet in order (before validation strips tags)
                # tag_sequence = []
                # tag_names = {0: "L", 1: "R", 2: "C"}
                
                # try:
                #     raw_samples = np.frombuffer(data, dtype=np.int32)
                #     for sample_idx, sample in enumerate(raw_samples):
                #         tag = (sample >> 1) & 0x3
                #         tag_names_lookup = {0: "L", 1: "R", 2: "C", 3: "?"}
                #         tag_sequence.append(tag_names_lookup.get(tag, "?"))
                    
                #     if tag_sequence:
                #         first_tag = tag_sequence[0]
                #         last_tag = tag_sequence[-1]
                #         tag_string = "".join(tag_sequence)
                #         print(f"[TAG SEQUENCE] {tag_string}")
                #         print(f"[TAG ANALYSIS] First: {first_tag}, Last: {last_tag}, Total samples: {len(tag_sequence)}")
                # except Exception as e:
                #     print(f"[TAG SEQUENCE] Error extracting: {e}")
                # # ===== END TAG LOGGING =====
                
                # # ===== EXTRACT AND LOG TIMING INDEX SEQUENCE =====
                # # Show which timing indices are in the packet in order (before validation strips them)
                # timing_sequence = []
                
                # try:
                #     raw_samples = np.frombuffer(data, dtype=np.int32)
                #     for sample_idx, sample in enumerate(raw_samples):
                #         timing_idx = (sample >> 3) & 0x7  # Extract bits 3-5
                #         timing_sequence.append(str(timing_idx))
                    
                #     if timing_sequence:
                #         first_timing = timing_sequence[0]
                #         last_timing = timing_sequence[-1]
                #         timing_string = "".join(timing_sequence)
                #         print(f"[TIMING SEQUENCE] {timing_string}")
                #         print(f"[TIMING ANALYSIS] First: {first_timing}, Last: {last_timing}, Total samples: {len(timing_sequence)}")
                # except Exception as e:
                #     print(f"[TIMING SEQUENCE] Error extracting: {e}")
                # # ===== END TIMING LOGGING =====
                
                # Validate packet size: must be multiple of 12 bytes
                # (3 channels × 4 bytes per 32-bit sample)
                if packet_size % 4 != 0:
                    packet_errors += 1
                    print(f"[Combined UDP] Invalid packet size: {packet_size} bytes (not multiple of 12)")
                    continue
                
                # Calculate number of frames (samples per channel)
                bytes_per_sample = 4  # 32-bit samples
                total_samples = packet_size // bytes_per_sample
                num_frames = total_samples // 3  # 3 channels interleaved
                num_samples = total_samples
                
                # Validate and strip channel tags from bits 1-2
                # Frame-level validation: process frame-by-frame and keep all valid frames
                # Only frames with BOTH matching timing indices AND correct channel tags [0,1,2] are kept
                cleaned_data, frames_kept, frames_discarded = _validate_and_strip_channel_tags(data)
                # print(f"\n[VALIDATED] {frames_kept} frames kept, {frames_discarded} frames discarded")
                # print(f"[CLEANED DATA] First 48 bytes (12 samples): {cleaned_data[:48].hex()}...")
                
                # # Detailed validation summary
                # if frames_discarded > 0:
                #     if frames_discarded < 100:  # Avoid spam on huge frame losses
                #         print(f"[Frame Validation] STRICT mode: only frames with timing sync + channel tags [0,1,2] kept")
                #         print(f"[Frame Validation] Discarded: {frames_discarded} frames (timing mismatch or tag corruption)")
                
                # # Verify the cleaned data format
                # if frames_kept > 0:
                #     # Show which mics are present by sampling first frame
                #     first_frame_bytes = cleaned_data[:12] if len(cleaned_data) >= 12 else cleaned_data
                #     if len(first_frame_bytes) == 12:
                #         l_sample = int.from_bytes(first_frame_bytes[0:4], 'little', signed=True)
                #         r_sample = int.from_bytes(first_frame_bytes[4:8], 'little', signed=True)
                #         c_sample = int.from_bytes(first_frame_bytes[8:12], 'little', signed=True)
                #         print(f"[Data Integrity] First frame: L={l_sample:10d}, R={r_sample:10d}, C={c_sample:10d}")
                #         print(f"[Data Format] Buffer format: interleaved 3-channel [L0,R0,C0, L1,R1,C1, ...]")
                
                # Update sample counts based on kept frames
                num_frames = frames_kept
                num_samples = frames_kept * 3
                
                # If no valid frames in this packet, skip RMS calculation but continue processing
                if num_frames == 0:
                    print(f"[Combined UDP] Packet has no valid frames - skipping")
                    continue
                
                # Pre-compute RMS outside lock (CPU-intensive, variable duration)
                # This prevents lock contention and timing skew
                try:
                    rms_values = _compute_rms(cleaned_data, 3, 32)
                    rms_l = rms_values[0] if len(rms_values) > 0 else 0.0
                    rms_r = rms_values[1] if len(rms_values) > 1 else 0.0
                    rms_c = rms_values[2] if len(rms_values) > 2 else 0.0
                except Exception as e:
                    packet_errors += 1
                    print(f"[Combined UDP] RMS computation error: {e}")
                    continue
                
                # Encode cleaned data as base64 outside lock (variable duration - CPU intensive)
                try:
                    data_b64 = base64.b64encode(cleaned_data).decode('utf-8')
                except Exception as e:
                    packet_errors += 1
                    print(f"[Combined UDP] Base64 encoding error: {e}")
                    continue
                
                # Generate timestamp
                packet_time_us = int(time.time() * 1_000_000)
                packet_timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
                
                # Log statistics every 3 seconds
                now = time.time()
                if now - last_log_time >= 3.0:
                    print(f"[Combined UDP] packets={packet_counter}, "
                          f"errors={packet_errors}, size={packet_size}B, "
                          f"frames={num_frames}, rms=[{rms_l:.0f}, {rms_r:.0f}, {rms_c:.0f}] | from {addr}")
                    last_log_time = now
                
                # Update global counters (before lock - atomic operations)
                packet_counter += 1
                sample_counter += num_samples
                
                # NOW acquire lock with minimal hold time (all pre-computation done)
                with audio_buffer_lock:
                    # Store in circular buffer for download/playback (with tags stripped)
                    audio_buffer.append(cleaned_data)
                
                # Update latest packet info
                with latest_lock:
                  latest_packet["timestamp"] = packet_timestamp
                  latest_packet["data"] = cleaned_data  # Store cleaned data (tags removed, only valid frames)
                  latest_packet["sample_rate"] = 48000
                  latest_packet["channels"] = 3
                  latest_packet["bits"] = 32
                  latest_packet["format"] = "interleaved"
                  latest_packet["frames_kept"] = frames_kept  # Track frame validation status
                  latest_packet["frames_discarded"] = frames_discarded
                  latest_packet["rms_l"] = rms_l
                  latest_packet["rms_r"] = rms_r
                  latest_packet["rms_c"] = rms_c
                  latest_packet["time_us"] = packet_time_us
                
                # Notify subscribers with combined 3-channel data
                packet_info = {
                    "device_id": "udp-combined",
                    "sample_rate": 48000,
                    "channels": 3,
                    "bits": 32,
                    "format": "interleaved",
                    "timestamp": packet_timestamp,
                    "data": data_b64,
                    "frames_kept": frames_kept,
                    "frames_discarded": frames_discarded,
                    "rms_l": rms_l,
                    "rms_r": rms_r,
                    "rms_c": rms_c,
                }
                for queue in list(subscribers):
                  queue.put(packet_info)

                with sync_lock:
                  packet_arrival_times.append(packet_time_us)
                  if len(packet_arrival_times) > 100:
                    packet_arrival_times.pop(0)
        
        except socket.timeout:
            continue
        except Exception as e:
            packet_errors += 1
            print(f"[Combined UDP] Receiver error: {e}")
            time.sleep(0.1)




@app.after_request
def add_cors_headers(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type,X-Device-Id,X-Sample-Rate,X-Channels,X-Bits,X-Format"
    return resp


@app.route("/")
def index() -> Response:
    html = """
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8" />
        <title>AudioVision Debug</title>
        <style>
          body { font-family: monospace; background: #1e1e1e; color: #d4d4d4; padding: 1rem; margin: 0; }
          h1 { margin: 0 0 1rem 0; color: #4ec9b0; }
          .status { padding: 0.5rem; background: #2d2d30; margin-bottom: 1rem; border-left: 3px solid #007acc; }
          .section { background: #252526; padding: 1rem; margin-bottom: 1rem; border: 1px solid #3e3e42; }
          .section h2 { margin: 0 0 0.5rem 0; font-size: 1rem; color: #569cd6; }
          .data { display: grid; grid-template-columns: 150px auto; gap: 0.5rem; }
          .label { color: #9cdcfe; }
          .value { color: #ce9178; }
          .log { max-height: 300px; overflow-y: auto; font-size: 0.875rem; }
          .log-entry { padding: 0.25rem; border-bottom: 1px solid #3e3e42; }
          .log-entry:hover { background: #2d2d30; }
          .rms { display: flex; gap: 1rem; }
          .rms-item { flex: 1; }
          .rms-label { font-size: 0.75rem; color: #9cdcfe; margin-bottom: 0.25rem; }
          .rms-value { font-size: 1.25rem; color: #4ec9b0; font-weight: bold; margin-bottom: 0.5rem; }
          .rms-bar-bg { height: 120px; background: #3e3e42; border-radius: 4px; position: relative; overflow: hidden; }
          .rms-bar-fill { width: 100%; background: linear-gradient(to top, #4ec9b0, #569cd6, #c586c0); 
                          position: absolute; bottom: 0; transition: height 0.1s ease-out; }
          .controls { margin-top: 1rem; text-align: center; display: flex; gap: 0.75rem; justify-content: center; flex-wrap: wrap; }
          .btn { padding: 0.75rem 1.5rem; background: #007acc; color: #fff; border: none; border-radius: 4px; font-size: 1rem; cursor: pointer; font-family: monospace; }
          .btn.secondary { background: #3a3d41; }
          .btn:disabled { opacity: 0.6; cursor: not-allowed; }
        </style>
      </head>
      <body>
        <h1>AudioVision Debug</h1>
        <div class="status" id="status">Waiting for data...</div>
        
        <div class="section">
          <h2>Latest Packet</h2>
          <div class="data">
            <div class="label">Device ID:</div><div class="value" id="device">-</div>
            <div class="label">Sample Rate:</div><div class="value" id="rate">-</div>
            <div class="label">Channels:</div><div class="value" id="channels">-</div>
            <div class="label">Bits:</div><div class="value" id="bits">-</div>
            <div class="label">Bytes:</div><div class="value" id="bytes">-</div>
            <div class="label">Timestamp:</div><div class="value" id="ts">-</div>
            <div class="label">Packets Received:</div><div class="value" id="count">0</div>
          </div>
        </div>

        <div class="section">
          <h2>RMS Levels</h2>
          <div class="rms" id="rms"></div>
          <div class="controls">
            <select id="listenMode" class="btn secondary" style="width: auto; padding: 0.75rem;">
              <option value="3ch">Listen: 3-Channel (Combined)</option>
              <option value="mic0">Listen: Mic 0 (Left)</option>
              <option value="mic1">Listen: Mic 1 (Right)</option>
              <option value="mic2">Listen: Mic 2 (Center)</option>
            </select>
            <button id="liveBtn" class="btn secondary">Listen Live</button>
            <button id="saveBtn" class="btn">Save Last 5 Seconds</button>
            <button id="textBtn" class="btn secondary">Export as Text</button>
            <button id="diagBtn" class="btn secondary">Check Diagnostics</button>
          </div>
        </div>

        <div class="section">
          <h2>Packet Diagnostics</h2>
          <div class="data" id="diagnostics">
            <div class="label">Total Packets:</div><div class="value" id="total-packets">-</div>
            <div class="label">Total Errors:</div><div class="value" id="total-errors">-</div>
            <div class="label">Buffer Packets:</div><div class="value" id="buffer-packets">-</div>
            <div class="label">Last Packet:</div><div class="value" id="last-packet">-</div>
            <div class="label">Packet Size:</div><div class="value" id="last-size">-</div>
          </div>
        </div>

        <script>
          let packetCount = 0;
          let liveEnabled = false;
          let audioCtx = null;
          let eventSource = null;
          let nextPlayTime = 0;

          function updateRMS(rms, channels) {
            const rmsDiv = document.getElementById('rms');
            if (!rmsDiv) return;
            
            const labels = ['Left', 'Right', 'Mono'];
            const MAX_32BIT = 2147483648;  // Max value for 32-bit signed int
            
            rmsDiv.innerHTML = rms.map((val, i) => {
              // Scale 0 to max 32-bit as 0 to 100%
              const percent = Math.max(0, Math.min(100, (val / MAX_32BIT) * 100));
              return `
              <div class="rms-item">
                <div class="rms-label">${labels[i] || 'Ch' + (i+1)}</div>
                <div class="rms-value">${val.toFixed(0)}</div>
                <div class="rms-bar-bg">
                  <div class="rms-bar-fill" style="height: ${percent}%"></div>
                </div>
              </div>
            `}).join('');
          }

          function base64ToBytes(b64) {
            const binary = atob(b64);
            const bytes = new Uint8Array(binary.length);
            for (let i = 0; i < binary.length; i++) {
              bytes[i] = binary.charCodeAt(i);
            }
            return bytes;
          }

          function scheduleAudio(packet) {
            if (!audioCtx) {
              console.error('[Audio] No audio context');
              return;
            }
            
            console.log('[Audio] Received packet:', {
              device_id: packet.device_id,
              channels: packet.channels,
              bits: packet.bits,
              sample_rate: packet.sample_rate,
              data_length: packet.data ? packet.data.length : 0
            });
            
            const bytes = base64ToBytes(packet.data || '');
            if (bytes.length === 0) {
              console.warn('[Audio] No data after base64 decode');
              return;
            }
            console.log('[Audio] Decoded', bytes.length, 'bytes');

            const channels = packet.channels || 1;  // May be 1 (single UDP packet) or 3 (assembled)
            const bits = packet.bits || 32;  // ESP32 sends 32-bit
            
            // Support 16-bit and 32-bit
            let samples;
            if (bits === 32) {
              samples = new Int32Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 4);
            } else {
              samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
            }
            console.log('[Audio] Parsed', samples.length, 'samples');
            
            const frames = Math.floor(samples.length / channels);
            if (frames === 0) {
              console.warn('[Audio] Empty packet received');
              return;
            }
            console.log('[Audio] Processing', frames, 'frames with', channels, 'channels');

            const sampleRate = packet.sample_rate || 48000;  // ESP32 uses 48kHz
            console.log('[Audio] Creating buffer:', channels, 'ch,', frames, 'frames,', sampleRate, 'Hz');
            
            const buffer = audioCtx.createBuffer(channels, frames, sampleRate);

            // Process samples: normalize to [-1, 1]
            if (channels === 1) {
              const channelData = buffer.getChannelData(0);
              for (let i = 0; i < frames; i++) {
                const sample = samples[i];
                const normalized = bits === 32 
                  ? Math.max(-1, Math.min(1, sample / 2147483648))
                  : sample / 32768;
                channelData[i] = normalized;
              }
              console.log('[Audio] First sample value:', samples[0], '→ normalized:', channelData[0]);
            } else if (channels === 3) {
              // 3-channel interleaved: [L, R, C, L, R, C, ...]
              for (let ch = 0; ch < 3; ch++) {
                const channelData = buffer.getChannelData(ch);
                for (let i = 0; i < frames; i++) {
                  const sample = samples[i * 3 + ch];
                  const normalized = bits === 32 
                    ? Math.max(-1, Math.min(1, sample / 2147483648))
                    : sample / 32768;
                  channelData[i] = normalized;
                }
              }
              console.log('[Audio] First frame: L=', samples[0], 'R=', samples[1], 'C=', samples[2]);
            } else {
              // Generic multi-channel handling
              for (let ch = 0; ch < channels; ch++) {
                const channelData = buffer.getChannelData(ch);
                for (let i = 0; i < frames; i++) {
                  const sample = samples[i * channels + ch];
                  const normalized = bits === 32 
                    ? Math.max(-1, Math.min(1, sample / 2147483648))
                    : sample / 32768;
                  channelData[i] = normalized;
                }
              }
            }

            const source = audioCtx.createBufferSource();
            source.buffer = buffer;
            source.connect(audioCtx.destination);
            console.log('[Audio] Buffer duration:', buffer.duration, 'sec');

            // Minimal jitter buffer: schedule playback with 10ms lookahead to reduce latency
            // while still smoothing out minor packet arrival jitter
            const jitterBufferMs = 10;  // Reduced from 50ms: minimal smoothing for low latency
            const minScheduleTime = audioCtx.currentTime + (jitterBufferMs / 1000);
            
            if (nextPlayTime < minScheduleTime) {
              nextPlayTime = minScheduleTime;
            }
            
            console.log('[Audio] Scheduling playback at', nextPlayTime, '(current:', audioCtx.currentTime, ')');
            source.start(nextPlayTime);
            nextPlayTime += buffer.duration;
            console.log('[Audio] Next play time:', nextPlayTime);
          }

          function startLive() {
            if (liveEnabled) {
              console.warn('[Live] Already enabled');
              return;
            }
            console.log('[Live] Starting live playback...');
            liveEnabled = true;
            document.getElementById('liveBtn').textContent = 'Stop Listening';

            try {
              audioCtx = new (window.AudioContext || window.webkitAudioContext)();
              console.log('[Live] AudioContext created, sample rate:', audioCtx.sampleRate);
              nextPlayTime = audioCtx.currentTime + 0.02;  // Reduced from 0.1s: minimal initial buffer for faster response
              console.log('[Live] Initial play time:', nextPlayTime);
            } catch (err) {
              console.error('[Live] Failed to create AudioContext:', err);
              alert('Failed to create audio context: ' + err.message);
              return;
            }

            const listenMode = document.getElementById('listenMode').value;
            let streamUrl = '/api/stream_3ch';
            if (listenMode.startsWith('mic')) {
              const selectedMic = parseInt(listenMode.substring(3), 10);
              streamUrl = `/api/stream_mic/${selectedMic}`;
            }
            console.log('[Live] Connecting to:', streamUrl);

            eventSource = new EventSource(streamUrl);
            eventSource.onopen = () => {
              console.log('[Live] EventSource connected');
            };
            eventSource.onmessage = (event) => {
              try {
                console.log('[Live] Received event, data length:', event.data.length);
                const packet = JSON.parse(event.data);
                console.log('[Live] Parsed packet:', packet.device_id);
                
                scheduleAudio(packet);
              } catch (err) {
                console.error('[Live] Stream parse error:', err, 'Data:', event.data.substring(0, 100));
              }
            };
            eventSource.onerror = (err) => {
              console.error('[Live] Stream error:', err);
              console.log('[Live] EventSource readyState:', eventSource.readyState);
            };
          }

          async function stopLive() {
            if (!liveEnabled) return;
            liveEnabled = false;
            document.getElementById('liveBtn').textContent = 'Listen Live';

            if (eventSource) {
              eventSource.close();
              eventSource = null;
            }
            if (audioCtx) {
              await audioCtx.close();
              audioCtx = null;
            }
            nextPlayTime = 0;
          }

          async function refresh() {
            try {
              const resp = await fetch('/api/latest');
              if (!resp.ok) {
                document.getElementById('status').textContent = 'Error: ' + resp.status;
                return;
              }
              const data = await resp.json();
              
              console.log('Received data:', data);
              
              if (!data || !data.packet) {
                document.getElementById('status').textContent = 'Waiting for data...';
                return;
              }
              
              packetCount++;
              
              const packet = data.packet;
              document.getElementById('status').textContent = '✓ Receiving: 3-Channel Combined (polls: ' + packetCount + ')';
              document.getElementById('device').textContent = packet.device_id || 'udp-combined';
              document.getElementById('rate').textContent = (packet.sample_rate || '-') + ' Hz';
              document.getElementById('channels').textContent = (packet.channels || 3) + ' (combined)';
              document.getElementById('bits').textContent = (packet.bits || '-') + ' bit';
              document.getElementById('bytes').textContent = packet.byte_count || 0;
              document.getElementById('ts').textContent = packet.timestamp || '-';
              document.getElementById('count').textContent = packetCount;
              
              const rmsValues = packet.rms || [0, 0, 0];
              updateRMS(rmsValues, 3);
            } catch (e) {
              console.error('Fetch error:', e);
              document.getElementById('status').textContent = 'Error: ' + e.message;
            }
          }

          document.getElementById('saveBtn').addEventListener('click', async () => {
            try {
              const resp = await fetch('/api/download_buffer');
              if (!resp.ok) {
                alert('No audio data available');
                return;
              }
              const blob = await resp.blob();
              const url = window.URL.createObjectURL(blob);
              const a = document.createElement('a');
              a.href = url;
              a.download = 'audio_' + new Date().toISOString().replace(/[:.]/g, '-') + '.wav';
              document.body.appendChild(a);
              a.click();
              document.body.removeChild(a);
              window.URL.revokeObjectURL(url);
            } catch (e) {
              console.error('Download error:', e);
              alert('Download failed: ' + e.message);
            }
          });

          document.getElementById('liveBtn').addEventListener('click', async () => {
            if (liveEnabled) {
              await stopLive();
            } else {
              startLive();
            }
          });

          document.getElementById('textBtn').addEventListener('click', async () => {
            try {
              const resp = await fetch('/api/download_buffer_text');
              if (!resp.ok) {
                alert('No audio data available');
                return;
              }
              const blob = await resp.blob();
              const url = window.URL.createObjectURL(blob);
              const a = document.createElement('a');
              a.href = url;
              a.download = 'audio_' + new Date().toISOString().replace(/[:.]/g, '-') + '.csv';
              document.body.appendChild(a);
              a.click();
              document.body.removeChild(a);
              window.URL.revokeObjectURL(url);
            } catch (e) {
              console.error('Download error:', e);
              alert('Download failed: ' + e.message);
            }
          });

          document.getElementById('diagBtn').addEventListener('click', async () => {
            try {
              const resp = await fetch('/api/diagnostics');
              if (!resp.ok) {
                alert('Failed to fetch diagnostics');
                return;
              }
              const data = await resp.json();
              console.log('Diagnostics:', data);
              
              document.getElementById('total-packets').textContent = data.packet_counters.total;
              document.getElementById('total-errors').textContent = data.packet_errors.total;
              document.getElementById('buffer-packets').textContent = data.buffer_size;
              document.getElementById('last-packet').textContent = data.latest_packet.last_update || 'Never';
              document.getElementById('last-size').textContent = data.latest_packet.size || 0;
            } catch (e) {
              console.error('Diagnostics error:', e);
            }
          });

          setInterval(refresh, 40);
          refresh();
        </script>
      </body>
    </html>
    """
    return Response(html, mimetype="text/html")


@app.route("/api/upload", methods=["POST", "OPTIONS"])
def api_upload() -> Response:
    if request.method == "OPTIONS":
        return Response("", status=200)

    data = request.get_data(cache=False) or b""
    packet = {
        "device_id": request.headers.get("X-Device-Id"),
        "sample_rate": _parse_int(request.headers.get("X-Sample-Rate")),
        "channels": _parse_int(request.headers.get("X-Channels")) or 1,
        "bits": _parse_int(request.headers.get("X-Bits")) or 16,
        "format": request.headers.get("X-Format") or "pcm16le",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data": data,
    }

    rms_values = _compute_rms(data, packet["channels"], packet["bits"]) if data else tuple()
    rms_list = list(rms_values) if rms_values else [0.0] * packet["channels"]
    while len(rms_list) < 3:
      rms_list.append(0.0)

    with latest_lock:
      latest_packet.update({
        "device_id": packet["device_id"] or "http-upload",
        "sample_rate": packet["sample_rate"] or buffer_sample_rate,
        "channels": packet["channels"],
        "bits": packet["bits"],
        "format": packet["format"],
        "timestamp": packet["timestamp"],
        "data": packet["data"],
        "rms_l": rms_list[0],
        "rms_r": rms_list[1],
        "rms_c": rms_list[2],
      })

    # Add to circular buffer
    with audio_buffer_lock:
        # Store raw bytes; we'll handle WAV formatting on download
        audio_buffer.append(data)
        # global buffer_sample_rate, buffer_channels, buffer_bits
        # buffer_sample_rate = packet["sample_rate"] or 48000
        # buffer_channels = packet["channels"] or 3
        # buffer_bits = packet["bits"] or 32

    for queue in list(subscribers):
        queue.put(packet)

    return Response("OK", status=200)


@app.route("/api/latest")
def api_latest() -> Response:
  with latest_lock:
    packet_snapshot = dict(latest_packet)

  data_bytes = packet_snapshot.get("data") or b""
  byte_count = len(data_bytes) if isinstance(data_bytes, (bytes, bytearray)) else len(data_bytes)

  return jsonify(
    {
      "packet": {
        "device_id": packet_snapshot.get("device_id"),
        "sample_rate": packet_snapshot.get("sample_rate"),
        "channels": packet_snapshot.get("channels"),
        "bits": packet_snapshot.get("bits"),
        "timestamp": packet_snapshot.get("timestamp"),
        "byte_count": byte_count,
        "rms": [
          packet_snapshot.get("rms_l", 0.0),
          packet_snapshot.get("rms_r", 0.0),
          packet_snapshot.get("rms_c", 0.0),
        ],
      }
    }
  )


def reassemble_3channel_audio():
  """Reassemble 3-channel interleaved audio from the combined buffer.

  Returns tuple (interleaved_pcm_bytes, num_frames) where interleaved_pcm_bytes
  contains 3-channel 32-bit PCM interleaved as: [L, R, C, L, R, C, ...]
  """
  with audio_buffer_lock:
    packets = list(audio_buffer)

  if not packets:
    return b"", 0

  interleaved_bytes = b"".join(packets)
  # Save to a csv file with correct headers
  with open("buffer_dump.csv", "w") as f:
    f.write("sample_index,value\n")
    for i in range(0, len(interleaved_bytes), 4):
      sample = int.from_bytes(interleaved_bytes[i:i+4], "little", signed=True)
      # Save in hex
      f.write(f"{i//4},{sample:08x}, {sample}\n")
  with open("buffer_dump_rawhex.csv", "w") as f:
    f.write("byte_index,hex_value\n")
    for i in range(0, len(interleaved_bytes), 4):
      byte_val = interleaved_bytes[i:i+4]
      f.write(f"{i},{byte_val.hex()}\n")
  
  bytes_per_frame = 3 * 4  # 3 channels, 32-bit
  num_frames = len(interleaved_bytes) // bytes_per_frame

  return interleaved_bytes, num_frames


@app.route("/api/process_latest", methods=["POST"])
def api_process_latest() -> Response:
    """
    Run transcription + localization on the most recent audio in the 3 mic buffers.
    Body (optional JSON):
      {
        "max_packets": 40,
        "convert_to_int16": true
      }
    """
    body = request.get_json(silent=True) or {}
    max_packets = int(body.get("max_packets", 40))
    convert_to_int16 = bool(body.get("convert_to_int16", True))

    chunks = build_latest_3ch_chunks_from_buffers(
        max_packets=max_packets,
        convert_to_int16=convert_to_int16,
    )

    if not chunks:
        return jsonify({"ok": False, "error": "No aligned audio available in buffers"}), 404

    try:
        # IMPORTANT: if you convert to int16 above, tell downstream bits=16 if you pass it
        # (depends on how your engine_kwargs are used inside TranscriptionLocalizationSession).
        segments = audio_main.run(
            chunks=chunks,
            num_channels=3,
            # Example engine kwargs you might need (only if your stack uses them):
            # sample_rate=48000,
            # sample_width=2 if convert_to_int16 else 4,
        )
        return jsonify({"ok": True, "segments": segments, "num_chunks": len(chunks)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _int32_3ch_to_int16_bytes(interleaved_i32: np.ndarray) -> bytes:
    """
    Convert interleaved int32 samples to int16.
    Common for pipelines that assume 16-bit PCM.
    """
    # Scale: take the top 16 bits (fast) and clip to int16 range.
    i16 = np.clip((interleaved_i32 >> 16), -32768, 32767).astype(np.int16)
    return i16.tobytes()


def build_latest_3ch_chunks_from_buffers(
    *,
    max_packets: int = 40,
    convert_to_int16: bool = True,
) -> List[bytes]:
    """
    Build multi-channel chunks suitable for audio_main.run(chunks, num_channels=3).

    Each chunk corresponds to one 'packet time slice' assembled from mic0/mic1/mic2.
    """
    with audio_buffer_lock:
        b0 = list(audio_buffer_mic0)
        b1 = list(audio_buffer_mic1)
        b2 = list(audio_buffer_mic2)

    n = min(len(b0), len(b1), len(b2))
    if n == 0:
        return []

    # take the most recent aligned packets
    n_take = min(n, max_packets)
    b0 = b0[-n_take:]
    b1 = b1[-n_take:]
    b2 = b2[-n_take:]

    chunks: List[bytes] = []

    for p0, p1, p2 in zip(b0, b1, b2):
        s0 = np.frombuffer(p0, dtype=np.int32)
        s1 = np.frombuffer(p1, dtype=np.int32)
        s2 = np.frombuffer(p2, dtype=np.int32)

        # Keep packets aligned by trimming to the shortest packet length
        m = min(len(s0), len(s1), len(s2))
        if m == 0:
            continue
        s0 = s0[:m]
        s1 = s1[:m]
        s2 = s2[:m]

        interleaved = np.empty(m * 3, dtype=np.int32)
        interleaved[0::3] = s0
        interleaved[1::3] = s1
        interleaved[2::3] = s2

        if convert_to_int16:
            chunks.append(_int32_3ch_to_int16_bytes(interleaved))
        else:
            chunks.append(interleaved.tobytes())

    return chunks



@app.route("/api/download_buffer")
def api_download_buffer() -> Response:
    """Download buffered audio as WAV file.
    
    Returns 3-channel 32-bit PCM at 48kHz with interleaved channel data:
    [mic0_sample0, mic1_sample0, mic2_sample0, mic0_sample1, ...]
    """
    # Reassemble 3-channel interleaved audio from per-mic buffers
    interleaved_bytes, num_frames = reassemble_3channel_audio()
    
    if num_frames == 0:
        return Response("No audio data buffered", status=404)
    
    # ===== DIAGNOSTIC LOGGING =====
    print(f"\n[WAV EXPORT]")
    print(f"  Total bytes: {len(interleaved_bytes)}")
    print(f"  Total samples: {len(interleaved_bytes) // 4}")
    print(f"  Num frames: {num_frames}")
    print(f"  Duration at 48kHz: {num_frames / 48000:.4f} sec")
    
    # Sample first 4 frames to verify format
    if len(interleaved_bytes) >= 48:  # 4 frames = 48 bytes
        samples = np.frombuffer(interleaved_bytes[:48], dtype=np.int32)
        print(f"  First 4 frames (12 samples):")
        for frame_idx in range(4):
            l_val = samples[frame_idx * 3 + 0]
            r_val = samples[frame_idx * 3 + 1]
            c_val = samples[frame_idx * 3 + 2]
            print(f"    Frame {frame_idx}: L={l_val:10d}, R={r_val:10d}, C={c_val:10d}")
    # ===== END DIAGNOSTIC =====
    
    # Create WAV file in memory
    wav_io = io.BytesIO()
    
    try:
        with wave.open(wav_io, "wb") as wav_file:
            wav_file.setnchannels(3)           # 3-channel mic array (mic0, mic1, mic2)
            wav_file.setsampwidth(4)            # 32-bit = 4 bytes per sample
            wav_file.setframerate(48000)        # ESP32 uses 48kHz
            
            # write interleaved bytes as unsigned.
            unsigned_bytes = bytearray()
            # for i in range(0, len(interleaved_bytes), 4):
            #     sample = int.from_bytes(interleaved_bytes[i:i+4], "little", signed=True)
            #     unsigned_sample = (sample + 2147483648)  # Convert signed to unsigned
            #     unsigned_bytes.extend(unsigned_sample.to_bytes(4, "little", signed=False))
            wav_file.writeframes(interleaved_bytes)
        
        wav_io.seek(0)
        
        # Verify WAV file was created correctly
        with wave.open(wav_io, "rb") as wav_verify:
            print(f"  WAV verification:")
            print(f"    Channels: {wav_verify.getnchannels()}")
            print(f"    Sample width: {wav_verify.getsampwidth()}")
            print(f"    Frame rate: {wav_verify.getframerate()}")
            print(f"    Frames in WAV: {wav_verify.getnframes()}")
            print(f"    Duration: {wav_verify.getnframes() / wav_verify.getframerate():.4f} sec")
        
        wav_io.seek(0)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"audio_{timestamp}_{buffer_bits}bit.wav"
        
        return send_file(
            wav_io,
            mimetype="audio/wav",
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        print(f"Error creating WAV: {e}")
        return Response(f"Error creating WAV: {e}", status=500)


@app.route("/api/download_buffer_text")
def api_download_buffer_text() -> Response:
    with audio_buffer_lock:
        if len(audio_buffer) == 0:
            return Response("No audio data buffered", status=404)
        
        # Concatenate all buffered chunks
        combined = b"".join(audio_buffer)
        
        # Parse as signed integers based on bit depth
        bytes_per_sample = buffer_bits // 8
        num_samples = len(combined) // bytes_per_sample
        
        csv_lines = ["sample_index,value"]
        for i in range(num_samples):
            start = i * bytes_per_sample
            end = start + bytes_per_sample
            sample = int.from_bytes(combined[start:end], "little", signed=True)
            csv_lines.append(f"{i},{sample}")
        
        csv_text = "\n".join(csv_lines)
        
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        filename = f"audio_{timestamp}_{buffer_bits}bit.csv"
        
        return Response(
            csv_text,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

@app.route("/api/buffer_to_local_wav")
def api_buffer_to_local_wav() -> Response:
    with audio_buffer_lock:
        if len(audio_buffer) == 0:
            return Response("No audio data buffered", status=404)
        
        # Concatenate all buffered chunks
        combined = b"".join(audio_buffer)
        
        # Create WAV file in memory
        wav_io = io.BytesIO()
        
        try:
            with wave.open(wav_io, "wb") as wav_file:
                wav_file.setnchannels(3)           # 3-channel mic array (mic0, mic1, mic2)
                wav_file.setsampwidth(4)            # 32-bit = 4 bytes per sample
                wav_file.setframerate(48000)        # ESP32 uses 48kHz
                
                # write interleaved bytes as unsigned.
                unsigned_bytes = bytearray()
                # for i in range(0, len(interleaved_bytes), 4):
                #     sample = int.from_bytes(interleaved_bytes[i:i+4], "little", signed=True)
                #     unsigned_sample = (sample + 2147483648)  # Convert signed to unsigned
                #     unsigned_bytes.extend(unsigned_sample.to_bytes(4, "little", signed=False))
                wav_file.writeframes(combined)
            
            wav_io.seek(0)
            
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"audio_{timestamp}_{buffer_bits}bit.wav"
            
            localdir = "serverwavs"
            os.makedirs(localdir, exist_ok=True)
            localpath = os.path.join(localdir, filename)
            with open(localpath, "wb") as f:
                f.write(wav_io.read())
                
            # Clear the buffer after saving to avoid duplicate saves
            audio_buffer.clear()
            
            return Response(f"Saved WAV to {localpath}", status=200)
        except Exception as e:
            print(f"Error creating WAV: {e}")
            return Response(f"Error creating WAV: {e}", status=500)


@app.route("/api/buffer_diagnostics")
def api_buffer_diagnostics() -> Response:
    """Detailed diagnostics about the audio buffer contents."""
    with audio_buffer_lock:
        num_packets = len(audio_buffer)
        if num_packets == 0:
            return jsonify({"error": "Buffer is empty"})
        
        # Get total buffered data
        all_bytes = b"".join(audio_buffer)
        total_bytes = len(all_bytes)
        total_samples = total_bytes // 4
        total_frames = total_samples // 3
        duration_sec = total_frames / 48000.0
        
        # Sample first few values
        first_samples = []
        if total_bytes >= 48:
            samples = np.frombuffer(all_bytes[:48], dtype=np.int32)
            for i in range(0, min(12, len(samples)), 3):
                first_samples.append({
                    "frame": i // 3,
                    "L": int(samples[i]),
                    "R": int(samples[i + 1]) if i + 1 < len(samples) else 0,
                    "C": int(samples[i + 2]) if i + 2 < len(samples) else 0
                })
        
        # Get packet sizes
        packet_sizes = [len(p) for p in audio_buffer]
        
        return jsonify({
            "buffer_stats": {
                "num_packets": num_packets,
                "total_bytes": total_bytes,
                "total_samples": total_samples,
                "total_frames": total_frames,
                "duration_seconds": round(duration_sec, 4),
                "avg_packet_size": round(sum(packet_sizes) / len(packet_sizes), 2) if packet_sizes else 0,
                "min_packet_size": min(packet_sizes) if packet_sizes else 0,
                "max_packet_size": max(packet_sizes) if packet_sizes else 0
            },
            "format": {
                "channels": 3,
                "sample_rate": 48000,
                "bits_per_sample": 32,
                "bytes_per_sample": 4,
                "bytes_per_frame": 12
            },
            "first_frames": first_samples
        })


@app.route("/api/diagnostics")
def api_diagnostics() -> Response:
  """Return packet reception diagnostics for combined 3-channel stream."""
  with latest_lock:
    last_ts = latest_packet.get("timestamp")
    data_bytes = latest_packet.get("data") or b""
    last_size = len(data_bytes) if isinstance(data_bytes, (bytes, bytearray)) else len(data_bytes)

  return jsonify({
    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    "packet_counters": {
      "total": packet_counter,
    },
    "sample_counters": {
      "total": sample_counter,
    },
    "packet_errors": {
      "total": packet_errors,
    },
    "latest_packet": {
      "last_update": last_ts,
      "size": last_size,
    },
    "buffer_size": len(audio_buffer),
    "subscribers": len(subscribers),
  })


@app.route("/api/diagnostics_playback", methods=["GET"])
def api_diagnostics_playback() -> Response:
  """Return playback speed diagnostics to track double-speed audio issue."""
  with diagnostics_lock:
    if playback_diagnostics_count == 0:
      avg_retention = 0.0
      avg_speed = 0.0
    else:
      avg_retention = total_retention_ratio / playback_diagnostics_count
      avg_speed = total_playback_speed_ratio / playback_diagnostics_count
  
  return jsonify({
    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    "frame_validation": {
      "total_input_frames": total_input_frames,
      "total_output_frames": total_output_frames,
      "total_frames_skipped": total_frames_skipped,
      "packets_processed": playback_diagnostics_count,
    },
    "retention_analysis": {
      "average_retention_ratio": avg_retention,
      "retention_percentage": f"{avg_retention*100:.1f}%",
    },
    "playback_speed": {
      "average_speed_ratio": avg_speed,
      "status": "NORMAL" if 0.95 <= avg_speed <= 1.05 else ("DOUBLE_SPEED" if avg_speed >= 1.8 else "SLOW_SPEED"),
      "description": f"Playback running at {avg_speed:.2f}x speed (1.0 = normal, 2.0 = double)"
    },
    "last_stop_reason": last_stop_reason,
  })


@app.route("/api/debug_packet", methods=["GET"])
def api_debug_packet() -> Response:
  """Debug endpoint: Analyze the latest packet structure in hex/decimal."""
  with latest_lock:
    data_bytes = latest_packet.get("data") or b""
  
  if isinstance(data_bytes, str):
    # If it's base64, decode it
    try:
      data_bytes = base64.b64decode(data_bytes)
    except:
      data_bytes = b""
  
  if len(data_bytes) == 0:
    return Response("No packet data available", status=404)
  
  # Generate diagnostics
  diagnostic_report = _diagnose_packet_structure(data_bytes, max_samples=15)
  format_analysis = _detect_packet_format(data_bytes)
  
  full_report = diagnostic_report + "\n" + format_analysis
  
  return Response(full_report, mimetype="text/plain")


@app.route("/api/stream")
def api_stream() -> Response:
    queue: Queue = Queue()
    subscribers.append(queue)

    def gen():
        try:
            while True:
                packet = queue.get()
                data = packet.get("data") or ""
                # Data should already be base64 encoded from UDP receiver
                if isinstance(data, bytes):
                    data = base64.b64encode(data).decode("ascii")
                payload = {
                    "device_id": packet.get("device_id"),
                    "sample_rate": packet.get("sample_rate"),
                    "channels": packet.get("channels"),
                    "bits": packet.get("bits"),
                    "format": packet.get("format"),
                    "timestamp": packet.get("timestamp"),
                    "data": data,
                }
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            if queue in subscribers:
                subscribers.remove(queue)

    return Response(gen(), mimetype="text/event-stream")


def _extract_channel_bytes(interleaved_bytes: bytes, channel: int) -> bytes:
    """Extract a single channel from interleaved 3-channel data."""
    if channel < 0 or channel > 2:
        return b""
    samples = np.frombuffer(interleaved_bytes, dtype=np.int32)
    frame_count = len(samples) // 3
    if frame_count == 0:
        return b""
    samples = samples[: frame_count * 3]
    channel_samples = samples[channel::3]
    return channel_samples.astype(np.int32).tobytes()


def _extract_mic0_stereo(interleaved_bytes: bytes) -> bytes:
    """Extract MIC0 stereo (Left + Right) from interleaved 3-channel data.
    
    Returns interleaved stereo [L, R, L, R, ...] from source [L, R, C, L, R, C, ...]
    """
    if not interleaved_bytes:
        return b""
    
    samples = np.frombuffer(interleaved_bytes, dtype=np.int32)
    frame_count = len(samples) // 3
    if frame_count == 0:
        return b""
    
    samples = samples[: frame_count * 3]
    # Extract LEFT (channel 0) and RIGHT (channel 1) in interleaved stereo format
    left_samples = samples[0::3]   # Every 3rd starting at 0 = L, L, L, ...
    right_samples = samples[1::3]  # Every 3rd starting at 1 = R, R, R, ...
    
    # Interleave them: [L0, R0, L1, R1, L2, R2, ...]
    stereo_samples = np.empty((len(left_samples) * 2,), dtype=np.int32)
    stereo_samples[0::2] = left_samples
    stereo_samples[1::2] = right_samples
    
    return stereo_samples.astype(np.int32).tobytes()


def _extract_mic1_mono(interleaved_bytes: bytes) -> bytes:
    """Extract MIC1 mono (Center) from interleaved 3-channel data.
    
    Returns mono [C, C, C, ...] from source [L, R, C, L, R, C, ...]
    """
    if not interleaved_bytes:
        return b""
    
    samples = np.frombuffer(interleaved_bytes, dtype=np.int32)
    frame_count = len(samples) // 3
    if frame_count == 0:
        return b""
    
    samples = samples[: frame_count * 3]
    # Extract CENTER (channel 2)
    center_samples = samples[2::3]  # Every 3rd starting at 2 = C, C, C, ...
    
    return center_samples.astype(np.int32).tobytes()


@app.route("/api/stream_mic/<int:mic_id>")
def api_stream_mic(mic_id: int) -> Response:
    """Stream individual microphone audio.
    
    mic_id 0: MIC0 (stereo - Left + Right)
    mic_id 1: MIC1 (mono - Center)
    """
    if mic_id == 0:
        # MIC0 stereo (2 channels)
        channel_count = 2
        extract_fn = _extract_mic0_stereo
        device_name = "udp-mic0-stereo"
    elif mic_id == 1:
        # MIC1 mono (1 channel)
        channel_count = 1
        extract_fn = _extract_mic1_mono
        device_name = "udp-mic1-mono"
    else:
        return Response("Invalid mic_id (use 0 or 1)", status=400)

    queue: Queue = Queue()
    subscribers.append(queue)

    def gen():
        try:
            while True:
                packet = queue.get()
                data = packet.get("data") or ""
                if not data:
                    continue

                if isinstance(data, bytes):
                    raw = data
                else:
                    raw = base64.b64decode(data)

                channel_bytes = extract_fn(raw)
                if not channel_bytes:
                    continue

                payload = {
                    "device_id": device_name,
                    "sample_rate": packet.get("sample_rate"),
                    "channels": channel_count,
                    "bits": packet.get("bits"),
                    "format": "pcm32le",
                    "timestamp": packet.get("timestamp"),
                    "data": base64.b64encode(channel_bytes).decode("ascii"),
                }
                yield f"data: {json.dumps(payload)}\n\n"
        finally:
            if queue in subscribers:
                subscribers.remove(queue)

    return Response(gen(), mimetype="text/event-stream")



@app.route("/api/diagnostics_sync")
def api_diagnostics_sync() -> Response:
  """Synchronization diagnostics for combined 3-channel stream."""
  with sync_lock:
    recent_times = list(packet_arrival_times[-10:])

  interarrival_us = []
  for i in range(1, len(recent_times)):
    interarrival_us.append(recent_times[i] - recent_times[i - 1])

  avg_interarrival_us = int(sum(interarrival_us) / len(interarrival_us)) if interarrival_us else 0

  return jsonify({
    "synchronization_status": {
      "combined_stream": "sample-perfect (single UDP packet)",
      "buffer_packets": len(audio_buffer),
      "avg_interarrival_us": avg_interarrival_us,
    },
    "packet_stats": {
      "packets_received": packet_counter,
      "packet_errors": packet_errors,
      "total_samples": sample_counter,
    },
    "diagnostics": {
      "description": "Single-port combined stream eliminates inter-mic packet skew",
      "expected_packet_interval_ms": round((512 / 48000) * 1000, 3),
    },
  })


@app.route("/api/stream_3ch")
def api_stream_3ch() -> Response:
  """Stream 3-channel interleaved audio directly from combined packets."""
  return api_stream()

def _generate_audio_data(frames: int, channels: int, phase: float = 0.0) -> bytes:
    """Generate sine wave test audio data at 48kHz 16-bit"""
    frequencies = [440.0, 554.37, 659.25]  # A4, C#5, E5
    samples = []
    for i in range(frames):
        for ch in range(channels):
            freq = frequencies[ch % len(frequencies)]
            t = (i + phase) / 48000.0
            amplitude = 8000 + 4000 * math.sin(2 * math.pi * 0.5 * t)  # varying amplitude
            value = int(amplitude * math.sin(2 * math.pi * freq * t))
            samples.append(max(-32768, min(32767, value)))
    return struct.pack(f"<{len(samples)}h", *samples)


def simulate_post(frames: int = 256, sample_rate: int = 48000, channels: int = 3, phase: float = 0.0) -> Dict[str, object]:
    bytes_per_sample = 2
    data = _generate_audio_data(frames, channels, phase)
    headers = {
        "X-Device-Id": "test-device",
        "X-Sample-Rate": str(sample_rate),
        "X-Channels": str(channels),
        "X-Bits": str(bytes_per_sample * 8),
        "X-Format": "pcm16le",
        "Content-Type": "application/octet-stream",
    }

    with app.test_client() as client:
        resp = client.post("/api/upload", data=data, headers=headers)

    return {
        "status_code": resp.status_code,
        "byte_count": len(data),
        "device_id": headers["X-Device-Id"],
    }


def simulate_post_remote(url: str, frames: int = 256, sample_rate: int = 48000, channels: int = 3, phase: float = 0.0) -> Dict[str, object]:
    bytes_per_sample = 2
    data = _generate_audio_data(frames, channels, phase)
    headers = {
        "X-Device-Id": "test-device",
        "X-Sample-Rate": str(sample_rate),
        "X-Channels": str(channels),
        "X-Bits": str(bytes_per_sample * 8),
        "X-Format": "pcm16le",
        "Content-Type": "application/octet-stream",
    }

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        status = resp.status

    return {
        "status_code": status,
        "byte_count": len(data),
        "device_id": headers["X-Device-Id"],
        "url": url,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AudioVision server")
    parser.add_argument("--simulate", action="store_true", help="send a test upload to /api/upload")
    parser.add_argument("--remote", default="", help="remote upload URL for simulation")
    parser.add_argument("--loop", action="store_true", help="continuously send data (use with --simulate)")
    parser.add_argument("--interval", type=float, default=0.016, help="seconds between packets in loop mode (default: 16ms)")
    parser.add_argument("--frames", type=int, default=256, help="frames to send when simulating")
    parser.add_argument("--rate", type=int, default=48000, help="sample rate for simulation")
    parser.add_argument("--channels", type=int, default=3, help="channels for simulation")
    parser.add_argument("--host", default="0.0.0.0", help="host to bind the server")
    parser.add_argument("--port", type=int, default=30000, help="port to bind the server")
    parser.add_argument("--debug", action="store_true", help="enable Flask debug")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.simulate:
        phase = 0.0
        count = 0
        try:
            while True:
                if args.remote:
                    result = simulate_post_remote(args.remote, frames=args.frames, sample_rate=args.rate, channels=args.channels, phase=phase)
                else:
                    result = simulate_post(frames=args.frames, sample_rate=args.rate, channels=args.channels, phase=phase)
                count += 1
                phase += args.frames
                print(f"\r[{count}] {json.dumps(result)}", end="", flush=True)
                if not args.loop:
                    print()
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print(f"\n\nSent {count} packets")
    else:
      # Start single combined UDP receiver thread
      print("Starting combined UDP receiver...")
        
      # Check if port is available
      print("Checking port availability...")
      if _is_port_available(UDP_PORT_COMBINED):
        print(f"✓ Port {UDP_PORT_COMBINED} available")
      else:
        print(f"✗ Port {UDP_PORT_COMBINED} IN USE - will retry with SO_REUSEADDR")
        
      print("Giving system 2 seconds to clean up old sockets...")
      time.sleep(2)
        
      thread = threading.Thread(target=udp_receiver_combined, args=(UDP_PORT_COMBINED,), daemon=True)
      print(f"Starting combined UDP receiver on port {UDP_PORT_COMBINED}...")
      thread.start()
        
      print(f"Combined UDP receiver active on port {UDP_PORT_COMBINED}")
      app.run(host=args.host, port=args.port, debug=args.debug)
