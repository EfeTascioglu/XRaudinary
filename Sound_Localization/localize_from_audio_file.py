import numpy as np
from scipy.io import wavfile

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from sound_localization import find_delay
from audio_util import visualize_waveforms
from TDOA import tdoa_using_ls, tdoa_using_ls_2D, tdoa_using_grid_search, localize_sources_top3


def _load_mono(wav_path: str) -> tuple:
    fs, data = wavfile.read(wav_path)
    s = np.asarray(data, dtype=np.float64)
    if s.ndim > 1:
        s = s[:, 0]
    if np.issubdtype(data.dtype, np.integer):
        s = s / np.iinfo(data.dtype).max
    return fs, s


def test_from_mono_audio(
    wav_path_1: str,
    wav_path_2: str,
    wav_path_3: str,
    delays_1: tuple = (3, 0, 5),
    delays_2: tuple = (0, 7, 1),
    delays_3: tuple = (2, 4, 0),
    noise_scale: float = 0.05,
) -> None:
    """
    Three sources: load three mono WAVs, mix them across 3 channels with per-source delays.
    delays_k[i] = delay of source k on channel i (samples).
    """
    fs1, source1 = _load_mono(wav_path_1)
    fs2, source2 = _load_mono(wav_path_2)
    fs3, source3 = _load_mono(wav_path_3)
    if not (fs1 == fs2 == fs3):
        raise ValueError(f"Sample rate mismatch: {fs1}, {fs2}, {fs3}")
    fs = fs1
    n_total = max(len(source1), len(source2), len(source3))
    if len(source1) < n_total:
        source1 = np.pad(source1, (0, n_total - len(source1)), mode="constant", constant_values=0)
    if len(source2) < n_total:
        source2 = np.pad(source2, (0, n_total - len(source2)), mode="constant", constant_values=0)
    if len(source3) < n_total:
        source3 = np.pad(source3, (0, n_total - len(source3)), mode="constant", constant_values=0)
    d1 = np.asarray(delays_1, dtype=int)
    d2 = np.asarray(delays_2, dtype=int)
    d3 = np.asarray(delays_3, dtype=int)
    rng = np.random.default_rng(42)
    max_d = max(d1.max(), d2.max(), d3.max())
    n = n_total + max_d
    streams = []
    for ch in range(3):
        ch_sig = np.zeros(n)
        ch_sig[d1[ch] : d1[ch] + n_total] += source1
        ch_sig[d2[ch] : d2[ch] + n_total] += source2
        ch_sig[d3[ch] : d3[ch] + n_total] += source3
        ch_sig += noise_scale * rng.standard_normal(n)
        streams.append(ch_sig[:n_total])
    stream_a, stream_b, stream_c = streams

    window_len = int(0.1 * fs)
    step = window_len // 2
    pair_delays_sec = []
    pair_powers = []
    for s1, s2 in [(stream_a, stream_b), (stream_a, stream_c)]:
        delays_samp, delays_sec, powers = find_delay(s1, s2, window_len, step, fs=fs)
        pair_delays_sec.append(delays_sec)
        pair_powers.append(powers)
        print(f"Est. delays (samples): {delays_samp}  powers: {powers}")
    sources = localize_sources_top3(pair_delays_sec, pair_powers, loc_fn=tdoa_using_grid_search)
    print("True relative delays: Source1", d1, "Source2", d2, "Source3", d3)
    for i, (pos, strength) in enumerate(sources):
        print(f"Source {i+1} (m): ({float(pos[0]):.4f}, {float(pos[1]):.4f}, {float(pos[2]):.4f})  strength: {strength:.4f}")


def main(data, fs, third_channel_hardcoded_delay=0) -> None:
    print(f"Running localize from audio \n")

    #info = sf.info(wav_path)
    #print(info)
    #data, fs = sf.read(wav_path, always_2d=True)

    print(f"shape after reading {data.shape}") 

    # fs, data = wavfile.read(wav_path)
    print(f"Shape of data{data.shape}")
    if data.ndim == 1:
        data = data[:, None]
    if np.issubdtype(data.dtype, np.integer):
        data = data.astype(np.float64) / np.iinfo(data.dtype).max
    else:
        data = data.astype(np.float64)
    channels = [data[:, i] for i in range(data.shape[1])]
    if third_channel_hardcoded_delay != 0:
        channels[2] = np.roll(channels[2], third_channel_hardcoded_delay)

    for i in range(len(channels)):
        print(f"Channel {i}: {channels[i][0:10]}")

    print(f"Number of channels {data.shape[1]} \n")
    window_len = int(0.1 * fs)
    step = window_len // 2
    pair_delays_sec = []
    pair_powers = []
    for i in range(len(channels) - 1):
        delays_samp, delays_sec, powers = find_delay(
            channels[i], channels[i + 1], window_len, step, fs=fs
        )
        pair_delays_sec.append(delays_sec)
        pair_powers.append(powers)
        print(f"Ch {i}–{i + 1}: delays {delays_samp} samples  powers: {powers}")
    if len(channels) == 3 and len(pair_delays_sec) == 2:
        sources = localize_sources_top3(pair_delays_sec, pair_powers, loc_fn=tdoa_using_grid_search)
        for i, (pos, strength) in enumerate(sources):
            print(f"Source {i+1} (m): ({float(pos[0]):.4f}, {float(pos[1]):.4f}, {float(pos[2]):.4f})  strength: {strength:.4f}")

    return pos

if __name__ == "__main__":
    import sys
    fs, data = wavfile.read("audio_2026-02-27T18-58-13-713Z.wav")
    main(data, fs, 1)
    #test_from_mono_audio("test_audio.wav", "test_audio_2.wav", "test_audio.wav")