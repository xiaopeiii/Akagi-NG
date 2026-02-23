from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Positions:
    """
    Majsoul UI coordinates in a normalized 16x9 space.

    We convert (x16, y9) -> viewport pixels by:
      x_px = x16 / 16 * viewport_width
      y_px = y9  /  9 * viewport_height
    """

    # Bottom hand tiles (14 slots). The last slot corresponds to the right-most tile position.
    TEHAI_X = [
        2.23125,
        3.021875,
        3.8125,
        4.603125,
        5.39375,
        6.184375,
        6.975,
        7.765625,
        8.55625,
        9.346875,
        10.1375,
        10.928125,
        11.71875,
        12.509375,
    ]
    TEHAI_Y = 8.3625
    TSUMO_GAP = 0.246875  # extra gap for the drawn tile

    # Action buttons (pass/chi/pon/...): index order matches filling priority.
    # Layout:
    #   5   4   3
    #   2   1   0
    BUTTONS = [
        (10.875, 7.0),  # 0 (Pass / None)
        (8.6375, 7.0),  # 1
        (6.4, 7.0),  # 2
        (10.875, 5.9),  # 3
        (8.6375, 5.9),  # 4
        (6.4, 5.9),  # 5
    ]

    # Candidate selection positions (chi/pon/daiminkan combos).
    CANDIDATES = [
        (3.6625, 6.3),
        (4.49625, 6.3),
        (5.33, 6.3),
        (6.16375, 6.3),
        (6.9975, 6.3),
        (7.83125, 6.3),  # mid
        (8.665, 6.3),
        (9.49875, 6.3),
        (10.3325, 6.3),
        (11.16625, 6.3),
        (12.0, 6.3),
    ]

    # Candidate selection positions for kan (kakan/ankan).
    CANDIDATES_KAN = [
        (4.325, 6.3),
        (5.4915, 6.3),
        (6.6583, 6.3),
        (7.825, 6.3),  # mid
        (8.9917, 6.3),
        (10.1583, 6.3),
        (11.325, 6.3),
    ]

    CENTER = (8.0, 4.5)


def candidate_pos_index(n: int, idx: int) -> int:
    """
    Map (n candidates, candidate idx) to centered slot index in Positions.CANDIDATES.

    This mirrors the centering strategy used by MahjongCopilot.
    """
    if n <= 0:
        raise ValueError("n must be > 0")
    if idx < 0 or idx >= n:
        raise IndexError("idx out of range")
    # index = (-(len/2)+idx+0.5)*2+5
    return int((-(n / 2) + idx + 0.5) * 2 + 5)


def candidate_kan_pos_index(n: int, idx: int) -> int:
    """
    Map (n kan candidates, candidate idx) to centered slot index in Positions.CANDIDATES_KAN.
    """
    if n <= 0:
        raise ValueError("n must be > 0")
    if idx < 0 or idx >= n:
        raise IndexError("idx out of range")
    # idx_kan = int((-(len/2)+idx+0.5)*2+3)
    return int((-(n / 2) + idx + 0.5) * 2 + 3)

