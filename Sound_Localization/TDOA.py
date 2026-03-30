import numpy as np
from scipy.optimize import least_squares
from typing import Optional, List, Tuple as Tup

_DEFAULT_MICS = np.array([
                          [-0.08, -0.1385, 0.0],
                          [-0.08, 0.1385, 0.0],
                          [0.16, 0.0, 0.0],
                          ])

_DEFAULT_MICS_2D = np.array([[0.16, 0.0], [-0.1385, -0.08], [-0.1385, 0.08]])

def tdoa_using_ls(
    tdoa: np.ndarray,
    mic_positions: Optional[np.ndarray] = None,
    c: float = 343.0,
    prior_mean: Optional[np.ndarray] = None,
    prior_cov: Optional[np.ndarray] = None,   # covariance, not inverse
    x_min: Optional[float] = None,
    z_prior_sigma: Optional[float] = None,    # meters
    x0: Optional[np.ndarray] = None,
    robust: bool = True,
) -> np.ndarray:
    mics = np.asarray(mic_positions if mic_positions is not None else _DEFAULT_MICS, dtype=float)
    tdoa = np.asarray(tdoa, dtype=float).reshape(-1)
    n_mics = mics.shape[0]
    if mics.shape[1] != 3:
        raise ValueError(f"mic_positions must be (N, 3), got {mics.shape}")
    if n_mics < 3:
        raise ValueError(f"Need at least 3 mics, got {n_mics}")
    if tdoa.shape != (n_mics - 1,):
        raise ValueError(f"tdoa must be (n_mics-1,) = ({n_mics - 1},), got {tdoa.shape}")

    if x0 is None:
        x0 = np.mean(mics, axis=0) + np.array([0.0, 0.5, 0.0])

    # Precompute whitening factor for prior if provided
    prior_L = None
    if prior_mean is not None:
        prior_mean = np.asarray(prior_mean, dtype=float).reshape(3)
        if prior_cov is None:
            raise ValueError("prior_cov must be provided when prior_mean is set")
        prior_cov = np.asarray(prior_cov, dtype=float)
        prior_L = np.linalg.cholesky(prior_cov)

    def residuals(p: np.ndarray) -> np.ndarray:
        d0 = np.linalg.norm(p - mics[0])
        r = np.array([(np.linalg.norm(p - mics[i]) - d0) / c - tdoa[i - 1] for i in range(1, n_mics)], dtype=float)

        if prior_L is not None:
            diff = p - prior_mean
            # whiten: y = L^{-1} diff
            y = np.linalg.solve(prior_L, diff)
            r = np.concatenate([r, y])

        if z_prior_sigma is not None and z_prior_sigma > 0:
            r = np.concatenate([r, [p[2] / z_prior_sigma]])

        return r

    kw = {}
    if x_min is not None:
        kw["bounds"] = ([x_min, -np.inf, -np.inf], [np.inf, np.inf, np.inf])

    if robust:
        kw["loss"] = "soft_l1"

    res = least_squares(residuals, x0, **kw)
    return res.x


def tdoa_using_ls_2D(
    tdoa: np.ndarray,
    mic_positions: Optional[np.ndarray] = None,
    c: float = 343.0,
    prior_mean: Optional[np.ndarray] = None,
    prior_cov: Optional[np.ndarray] = None,
    x_min: Optional[float] = None,
    x0: Optional[np.ndarray] = None,
    robust: bool = True,
) -> np.ndarray:
    """TDOA source localization in 2D (x, y). Mics (3, 2), source (2,)."""
    mics = np.asarray(mic_positions if mic_positions is not None else _DEFAULT_MICS_2D, dtype=float)
    tdoa = np.asarray(tdoa, dtype=float).reshape(-1)
    if mics.shape != (3, 2):
        raise ValueError(f"mic_positions must be (3,2), got {mics.shape}")
    if tdoa.shape != (2,):
        raise ValueError(f"tdoa must be (2,), got {tdoa.shape}")

    if x0 is None:
        x0 = np.mean(mics, axis=0) + np.array([0.0, 0.5])
    m0, m1, m2 = mics

    prior_L = None
    if prior_mean is not None:
        prior_mean = np.asarray(prior_mean, dtype=float).reshape(2)
        if prior_cov is None:
            raise ValueError("prior_cov must be provided when prior_mean is set")
        prior_L = np.linalg.cholesky(np.asarray(prior_cov, dtype=float))

    def residuals(p: np.ndarray) -> np.ndarray:
        d0 = np.linalg.norm(p - m0)
        d1 = np.linalg.norm(p - m1)
        d2 = np.linalg.norm(p - m2)
        r = np.array([(d1 - d0) / c - tdoa[0], (d2 - d0) / c - tdoa[1]], dtype=float)
        if prior_L is not None:
            r = np.concatenate([r, np.linalg.solve(prior_L, p - prior_mean)])
        return r

    kw = {}
    if x_min is not None:
        kw["bounds"] = ([x_min, -np.inf], [np.inf, np.inf])
    if robust:
        kw["loss"] = "soft_l1"
    res = least_squares(residuals, x0, **kw)
    return res.x


def tdoa_using_grid_search(
    tdoa: np.ndarray,
    mic_positions: Optional[np.ndarray] = None,
    c: float = 343.0,
    distance: float = 1,
    n_points: int = 360,
) -> tuple[np.ndarray, float]:
    """
    SRP-PHAT style: grid of candidate positions at z=0, x>0, radius=distance.
    Returns the grid point whose theoretical TDOA best matches observed tdoa, and a similarity in [0,1].
    """
    mics = np.asarray(mic_positions if mic_positions is not None else _DEFAULT_MICS, dtype=float)
    tdoa = np.asarray(tdoa, dtype=float).reshape(2)
    if mics.shape != (3, 3):
        raise ValueError(f"mic_positions must be (3,3), got {mics.shape}")

    # 9 points at z=0, x>0, on circle of radius distance (angles -90 to +90 deg in xy)
    angles = np.linspace(-np.pi + np.pi/n_points * 2, np.pi, n_points)
    grid = np.column_stack([distance * np.cos(angles), distance * np.sin(angles), np.zeros(n_points)])

    m0, m1, m2 = mics
    best_idx = 0
    best_err = np.inf
    for i, p in enumerate(grid):
        d0 = np.linalg.norm(p - m0)
        d1 = np.linalg.norm(p - m1)
        d2 = np.linalg.norm(p - m2)
        tdoa_pred = np.array([(d1 - d0) / c, (d2 - d0) / c])
        err = np.linalg.norm(tdoa_pred - tdoa)
        if err < best_err:
            best_err = err
            best_idx = i

    best_point = grid[best_idx]
    similarity = 1.0 / (1.0 + best_err)
    return best_point, similarity


def localize_sources_top3(
    pair_delays_sec: List[np.ndarray],
    pair_powers: List[np.ndarray],
    loc_fn=None,
    top_k: int = 3,
    **loc_kw,
) -> List[Tup[np.ndarray, float]]:
    """
    From per-pair top-2 delays (seconds) and powers, form combinations, localize each,
    and return the top_k sources (position, strength). Strength combines powers (mean).
    For 3 mics: 2 pairs, 2 candidates each → 4 TDOA vectors → localize 4, return top 3.
    """
    if loc_fn is None:
        loc_fn = tdoa_using_grid_search
    n_pairs = len(pair_delays_sec)
    assert n_pairs == len(pair_powers)
    # Each pair has 2 candidates (delays_sec[i], powers[i] length 2)
    indices = [0, 1]  # top 2 per pair
    from itertools import product
    candidates = []
    for choice in product(indices, repeat=n_pairs):
        tdoa = np.array([pair_delays_sec[p][choice[p]] for p in range(n_pairs)])
        strength = float(np.mean([pair_powers[p][choice[p]] for p in range(n_pairs)]))
        try:
            out = loc_fn(tdoa, **loc_kw)
            pos = out[0] if isinstance(out, tuple) else out
            pos = np.asarray(pos, dtype=float).ravel()
            candidates.append((pos, strength))
        except Exception:
            continue
    candidates.sort(key=lambda x: -x[1])
    return candidates[:top_k]


if __name__ == "__main__":
    c = 343.0
    fs = 48000
    mics = _DEFAULT_MICS
    n_mics = mics.shape[0]
    source_true = np.array([1.2, 0.5, 0])
    


    d0 = np.linalg.norm(source_true - mics[0])
    tdoa = np.array([(np.linalg.norm(source_true - mics[i]) - d0) / c for i in range(1, n_mics)])
    x0 = np.array([1.0, 0.0, 0.0])
    pos, similarity = tdoa_using_grid_search(tdoa)
    pos = np.asarray(pos, dtype=float).ravel()
    print("True sound source:", source_true)
    print("Microphone positions: ")
    print(mics)
    print(f"Perfect Precision delays: [{tdoa[0]:.6f}, {tdoa[1]:.6f}]")
    tdoa = (tdoa * fs).astype(int) / fs
    print(f"Integer delays: [{tdoa[0]:.6f}, {tdoa[1]:.6f}]")
    print("-----------------------------")
    print("Estimated sound source position:  ", f"({float(pos[0]):.4f}, {float(pos[1]):.4f}, {float(pos[2]):.4f})")
    print("Similarity: ", similarity)
    angle_true = np.arctan2(source_true[1], source_true[0])
    angle_est = np.arctan2(float(pos[1]), float(pos[0]))
    error_rad = np.abs((angle_est - angle_true + np.pi) % (2 * np.pi) - np.pi)
    print("Angle error (deg):", np.degrees(error_rad))

    # 2D plot (ignore z): green = true source, red = identified, blue = grid points
    import matplotlib.pyplot as plt
    n_points = 36
    distance = 1.0
    angles = np.linspace(-np.pi + (np.pi / n_points * 2), np.pi, n_points)
    grid = np.column_stack([distance * np.cos(angles), distance * np.sin(angles), np.zeros(n_points)])
    plt.figure(figsize=(8, 8))
    plt.scatter(grid[:, 0], grid[:, 1], c="blue", s=5, label="Grid points")
    plt.scatter(source_true[0], source_true[1], c="green", s=80, label="Audio Source", zorder=5)
    plt.scatter(pos[0], pos[1], c="red", s=80, label="Identified", zorder=5)
    plt.scatter(mics[:, 0], mics[:, 1], c="black", s=40, marker="x", label="Microphoness")
    plt.title("Localization of Sound Source using TDOA grid point simulation")
    plt.xlabel("x (m)")
    plt.ylabel("y (m)")
    plt.axis("equal")
    plt.legend()
    plt.show()

