# XRaudinary: Spatially Anchored Live XR-Captions
> **Spatially anchored live captioning for individuals with hearing loss — rendered directly in your field of view, at the location of the speaker.**

Authors: **Andy Gong\* · Matthew Tamura\* · Efe Tascioglu\* · Andrew Wu\*** · Alexander Vicol · Steve Mann

\* Contributed Equally

*Paper describing this work, in the context of the current space is currently in review, and coming soon*

## What we do
XRaudinary is a wearable XR system that solves a fundamental problem with live captioning: **captions don't tell you *who* is speaking or *where* they are.**

People with hearing loss often face high cognitive
load and reduced participation in group conversations, especially
in noisy or reverberant environments. XRaudinary is an in-progress XR system that spatially anchors live captions to
the direction of sound within the user’s vision, by combining
a wearable, low-cost microphone array with the user’s realtime vision. An ESP32 Microcontroller Unit (MCU) ingests
synchronized I2S MEMS microphone streams, forwarding them
to a server that estimates time-differences-of-arrival for directionof-arrival inference, and forwards post-processed directionality
and captions to a VR/AR headset. Constraining the sound along
the planar axis of the user’s vision resolves geometric ambiguities
inherent to small arrays, enabling real-time captions that appear
at the correct location in the user’s field of view. We describe
the system architecture, sound source localization approach, and
appropriateness for real-time conversational contexts.

## Demo
<img width="400" height="400" alt="AudinaryBreadboard" src="https://github.com/user-attachments/assets/54e0eab5-eb39-4c55-b751-ad988de1330c" />
<img width="478" height="380" alt="AudinaryBreadboard" src="https://github.com/user-attachments/assets/af55e14e-7bdd-4bbb-ad78-fca82819d6f8" />

## System
<img width="1849" height="563" alt="image" src="https://github.com/user-attachments/assets/755c1900-eca8-405a-85f2-b9d29980bcfa" />

### 1. Hardware — Low-Cost Microphone Array

Three **SPH0645LM4H I2S MEMS microphones** (Adafruit breakout boards) are arranged in a triangle on a solderless breadboard, spaced ~45mm apart. They connect via GPIO to an **ESP32-PICO-KIT-1** microcontroller.

- Microphones sample at **48 kHz**, 18-bit, 2's complement
- Two mics share one I2S channel (stereo L/R); the third uses a second I2S channel (mono)
- A startup clap-based calibration aligns the two I2S channels to within ~4 samples

### 2. Data Transmission — UDP over LAN

The ESP32 streams synchronized audio to a local Python/Flask server over UDP:

- Each sample is tagged with **source mic ID** (bits 1–2) and **frame ID** (bits 3–5)
- Server validates and reassembles groups of 3 time-aligned samples
- UDP chosen over TCP for throughput: TCP achieved only ~300 kbit/s vs. UDP's >4,600 kbit/s

### 3. Sound Source Localization — TDOA + Visual Constraint

Direction-of-arrival (DOA) is estimated in two steps:

1. **Cross-Power Spectrum Phase (CSP)** computes time delays between each microphone pair — peaks in the correlation output correspond to distinct audio sources (see figure below).
2. **Grid search**: 360 candidate sources are simulated in a 1m circle; the best TDOA match selects the speaker's direction.

A key insight: constraining the search to the **horizontal eye-level plane** resolves the geometric ambiguity inherent to 3-mic planar arrays, turning an under-determined 3D problem into a single, robust 2D result.

<img width="600" height="500" alt="Delay_Between_Microphones" src="https://github.com/user-attachments/assets/715d0639-51be-4a7f-bb25-b08a5b358160" />

*Time delay peaks from CSP analysis — each peak corresponds to a distinct audio source.*

<img width="600" height="800" alt="TDOA_36" src="https://github.com/user-attachments/assets/40b9ea83-0634-4102-a3c5-fa5ac615bcfe" />

*Grid-point TDOA simulation used to identify the direction of the sound source.*

### 4. Transcription — faster-whisper

OpenAI **Whisper** (via the `faster-whisper` implementation) handles speech-to-text for low-latency, high-accuracy transcription. The **Silero VAD** filter was evaluated but found to increase WER significantly with low-cost microphones.

### 5. Caption Rendering — Unity on Meta Quest

A Unity app on the **Meta Quest 2 / 3S** receives WebSocket packets containing:
- Estimated localization vector (yaw angle)
- Transcription string

Captions are placed at a fixed anchor distance along the speaker's direction. Temporal smoothing reduces jitter. If the speaker is outside the user's field of view, a **directional arrow indicator** guides the user. Captions fade out gradually as new ones arrive.



## Key Results

| Metric | Result |
|---|---|
| **Localization accuracy** | ±4° angular error |
| **Word Error Rate (WER)** — VAD off | 9.45% |
| **Word Error Rate (WER)** — VAD on | 37.04% |
| **Hardware cost** | < $100 CAD |
| **Data packet integrity** | > 99% |
| **UDP throughput** | > 4,600 kbit/s |

## Limitations & Future Work

**Hardware**
- The ESP32-PICO-3 lacks TDM support, limiting the array to 3 synchronized mics across 2 I2S channels. A TDM-capable MCU (e.g. ESP32-C6) would allow up to 16 mics on a single channel, improving 3D localization.
- Clap-based calibration drifts beyond ~1–2 samples, which can degrade localization — future back-referencing against known sound sources could address this.

**Face Detection**
- Incorporating a lightweight face detection model (via Unity Sentis) would constrain candidate speakers to visible faces, shrinking the search space from 360° to detected regions and enabling caption placement that avoids occluding speakers' faces.

**Multiple Speakers**
- The CSP method can detect multiple simultaneous speakers, but the current Whisper model doesn't support speaker diarization without GPU hardware. GPU-backed multi-speaker Whisper would enable deployment in group/public settings.

**Beamforming**
- Directional audio beamforming could suppress background noise and improve transcription quality in noisy environments, extending usability to public spaces.

