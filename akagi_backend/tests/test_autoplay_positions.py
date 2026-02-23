from akagi_ng.autoplay.positions import (
    Positions,
    candidate_kan_pos_index,
    candidate_pos_index,
)


def test_candidate_pos_index_in_range():
    for n in range(1, 12):  # Positions.CANDIDATES has 11 slots
        for i in range(n):
            idx = candidate_pos_index(n, i)
            assert 0 <= idx < len(Positions.CANDIDATES)


def test_candidate_kan_pos_index_in_range():
    for n in range(1, 8):  # Positions.CANDIDATES_KAN has 7 slots
        for i in range(n):
            idx = candidate_kan_pos_index(n, i)
            assert 0 <= idx < len(Positions.CANDIDATES_KAN)

