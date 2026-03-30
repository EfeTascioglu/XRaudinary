"""
Sound localization utilities.
CSP: Cross-Power-Spectrum (Phase) comparison of two signals via a single FT.
Caller is responsible for windowing the signals before passing them in.
"""

import numpy as np
from typing import Tuple, Optional, Union

from audio_util import visualize_waveforms
import pdb

# Set before find_delay to plot a red dot at the true delay (seconds) on the peak plot
TRUE_DELAY_SEC: Optional[float] = None


def CSP(
    signal_a: np.ndarray,
    signal_b: np.ndarray,
    fs: float = 44200,
    use_phase_only: bool = False,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Cross-Power-Spectrum Phase (CSP): compare two sound signals using a single FT and power.

    Computes one FFT over the entire length of each signal, then the cross-power spectrum
    S_ab(f) = G_a(f) * conj(G_b(f)). Optionally uses phase-only weighting (GCC-PHAT style):
    S_ab / |S_ab|.

    Windowing must be applied by the caller before passing the signals.

    Parameters
    ----------
    signal_a, signal_b : np.ndarray
        One-dimensional sound signals (samples,). Same length recommended;
        shorter one is zero-padded to match the longer.
    fs : float, optional
        Sample rate in Hz. If given, freqs are in Hz; otherwise in cycles/sample.
    use_phase_only : bool
        If True, normalize by magnitude so only phase is used (PHAT weighting).

    Returns
    -------
    cross_power : np.ndarray
        Cross-power spectrum, shape (n_freqs,), complex.
    freqs : np.ndarray
        Frequency bin centers in Hz (if fs given) or in cycles/sample.
    """
    a = np.asarray(signal_a, dtype=float)
    b = np.asarray(signal_b, dtype=float)
    if a.ndim != 1 or b.ndim != 1:
        raise ValueError("signal_a and signal_b must be 1D arrays")

    assert len(a) == len(b)
    n = len(a)

    # Single FT over the entire window (caller does windowing)
    G_a = np.fft.rfft(a)
    G_b = np.fft.rfft(b)

    # Cross-power spectrum: G_a * conj(G_b)
    cross_power = G_a * np.conj(G_b)

    if use_phase_only:
        magnitude = np.abs(cross_power)
        magnitude[magnitude == 0] = 1.0  # avoid division by zero
        cross_power = cross_power / magnitude
    else:
        raise ValueError("Not Implemented")

    n_freqs = cross_power.shape[0]
    freqs = np.fft.rfftfreq(n, 1.0 / fs)
    
    return cross_power, freqs


def find_delay(
    signal_a: np.ndarray,
    signal_b: np.ndarray,
    window_len: int,
    step: int,
    use_phase_only: bool = True,
    fs: float = 48000,
    top_k: int = 2,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Sliding-window CSP; returns the top_k delay candidates and their relative powers.
    Powers are normalized so the strongest peak = 1 and the weakest of the top_k = 0.

    Returns
    -------
    delays_samples : (top_k,) int array
    delays_seconds : (top_k,) float array
    powers : (top_k,) float array, in [0, 1], strongest=1, weakest=0.
    """
    a = np.asarray(signal_a, dtype=float)
    b = np.asarray(signal_b, dtype=float)
    if a.ndim != 1 or b.ndim != 1:
        raise ValueError("signal_a and signal_b must be 1D arrays")
    n = len(a)
    if len(b) != n:
        raise ValueError("signal_a and signal_b must have the same length")

    win = np.hanning(window_len)
    n_fft = 2 * window_len
    avg_corrs = np.zeros(n_fft)
    for start in range(0, n - window_len + 1, step):
        w_a = a[start : start + window_len] * win
        w_b = b[start : start + window_len] * win
        w_a_pad = np.pad(w_a, (0, window_len), mode="constant", constant_values=0.0)
        w_b_pad = np.pad(w_b, (0, window_len), mode="constant", constant_values=0.0)

        cross_power, _ = CSP(w_a_pad, w_b_pad, fs=48000, use_phase_only=use_phase_only)
        # cross_power length is n_fft//2 + 1; irfft gives n_fft samples
        corr = np.fft.irfft(cross_power, n=n_fft)
        avg_corrs += np.abs(corr)


    reordered = np.zeros(n_fft)
    reordered[:window_len] = avg_corrs[window_len:]
    reordered[window_len:] = avg_corrs[:window_len]
    search_radius = 128
    search = reordered[window_len - search_radius : window_len + search_radius + 1]
    size = len(search)

    import matplotlib.pyplot as plt
    n_fft = len(reordered)
    x_plot = (np.arange(n_fft // 16) - n_fft / 32) / fs
    y_plot = reordered[n_fft * 15 // 32 : n_fft * 17 // 32]
    # plt.figure(figsize=(10, 5))
    # plt.plot(x_plot, y_plot)
    # if TRUE_DELAY_SEC is not None:
    #     j = int(round(TRUE_DELAY_SEC * fs + n_fft / 32 - n_fft * 15 / 32))
    #     if 0 <= j < len(y_plot):
    #         plt.scatter([TRUE_DELAY_SEC], [y_plot[j]], color="red", zorder=5)
    # plt.title("Delay Between Microphones using CSP")
    # plt.xlabel("Delay (s)")
    # plt.ylabel("Correlation")
    # plt.show()

    delays_samp = []
    delays_sec = []
    peak_vals = []
    work = search.copy()
    min_val = work.min()
    for _ in range(top_k):
        idx = int(np.argmax(work))
        lag = idx - search_radius
        delay_samp = -lag
        delays_samp.append(delay_samp)
        delays_sec.append(delay_samp / fs)
        peak_vals.append(float(work[idx]))
        work[idx] = 0
    peak_vals = np.array(peak_vals, dtype=float)
    peak_max = peak_vals.max()
    powers = (peak_vals - min_val) / (peak_max - min_val)  # strongest = 1, others relative to it

    return np.array(delays_samp, dtype=int), np.array(delays_sec), powers


def fake_audio_stream_test(delay=8) -> None:
    """Generate a single source, two delayed+noisy streams, and estimate delay with find_delay."""
    rng = np.random.default_rng(42)
    fs = 48000
    duration_sec = 2.0
    n_total = int(fs * duration_sec)

    # Single source: mixture of tones + noise
    t = np.linspace(0, duration_sec, n_total, endpoint=False)
    source = (
        1 * np.sin(2 * np.pi * (80) * t)
        + 0.3 * np.sin(2 * np.pi * 120 * t)
        + 0.2 * rng.standard_normal(n_total)
    )

    # Delays in samples (stream_b is delayed relative to stream_a by delay_b - delay_a)
    delay_a = 0
    delay_b = int(delay)  
    max_delay = max(delay_a, delay_b)
    n = n_total + max_delay

    stream_a = np.zeros(n)
    stream_a[delay_a : delay_a + n_total] = source
    stream_a += 0.1 * rng.standard_normal(n)

    stream_b = np.zeros(n)
    stream_b[delay_b : delay_b + n_total] = source
    stream_b += 0.15 * rng.standard_normal(n)

    stream_a = stream_a[:n_total]
    stream_b = stream_b[:n_total]

    visualize_waveforms(stream_a, stream_b, fs=fs)

    window_len = 4800  # 100ms
    global TRUE_DELAY_SEC
    TRUE_DELAY_SEC = (delay_b - delay_a) / fs
    delays_samp, delays_sec, powers = find_delay(
        stream_a, stream_b, window_len=window_len, step=window_len//2, fs=fs, use_phase_only=True
    )
    true_delay = delay_b - delay_a
    print("Generated: stream_b delayed relative to stream_a by", true_delay, "samples (", true_delay / fs * 1000, "ms )")
    print("Estimated delays (samples):", delays_samp, "powers:", powers)


def fake_dual_audio_stream_test(
    delays_1: tuple = (3, 0, 5),
    delays_2: tuple = (0, 7, 1),
    delays_3: tuple = (2, 4, 0),
    noise_scale: float = 0.1,
) -> None:
    """Generate three synthetic sources, mix across 3 streams with per-source delays, run find_delay + localize_sources_top3."""
    rng = np.random.default_rng(42)
    fs = 48000
    duration_sec = 2.0
    n_total = int(fs * duration_sec)
    t = np.linspace(0, duration_sec, n_total, endpoint=False)
    source1 = (
        1.0 * np.sin(2 * np.pi * 80 * t)
        + 0.3 * np.sin(2 * np.pi * 120 * t)
        + 0.2 * rng.standard_normal(n_total)
    )
    source2 = (
        0.8 * np.sin(2 * np.pi * 200 * t)
        + 0.4 * np.sin(2 * np.pi * 250 * t)
        + 0.2 * rng.standard_normal(n_total)
    )
    source3 = (
        0.7 * np.sin(2 * np.pi * 350 * t)
        + 0.3 * np.sin(2 * np.pi * 400 * t)
        + 0.2 * rng.standard_normal(n_total)
    )
    d1 = np.asarray(delays_1, dtype=int)
    d2 = np.asarray(delays_2, dtype=int)
    d3 = np.asarray(delays_3, dtype=int)
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

    window_len = 4800
    step = window_len // 2
    pair_delays_sec = []
    pair_powers = []
    for s1, s2 in [(stream_a, stream_b), (stream_a, stream_c)]:
        delays_samp, delays_sec, powers = find_delay(s1, s2, window_len, step, fs=fs)
        pair_delays_sec.append(delays_sec)
        pair_powers.append(powers)
        print(f"Est. delays (samples): {delays_samp}  powers: {powers}")
    from TDOA import localize_sources_top3, tdoa_using_grid_search
    sources = localize_sources_top3(pair_delays_sec, pair_powers, loc_fn=tdoa_using_grid_search)
    print("True delays: Source1", d1, "Source2", d2, "Source3", d3)
    for i, (pos, strength) in enumerate(sources):
        print(f"Source {i+1} (m): ({float(pos[0]):.4f}, {float(pos[1]):.4f}, {float(pos[2]):.4f})  strength: {strength:.4f}")


if __name__ == "__main__":
    #fake_audio_stream_test()
    fake_dual_audio_stream_test()