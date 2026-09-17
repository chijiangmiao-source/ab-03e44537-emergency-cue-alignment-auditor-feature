"""Exhaustive and fuzz comparison of the alignment engine against an
independent brute-force enumerator.

The brute force enumerates every legal alignment path recursively and picks
the minimum by ``(total_cost, operation_string)`` where operations rank
MATCH < DELETE < INSERT.  It shares no code with the dynamic-programming
implementation under test, so it serves as an independent oracle.
"""

from __future__ import annotations

import itertools
import random

import pytest

from app.align import (
    DELETE_COST,
    DRIFT_TOLERANCE_MS,
    INSERT_COST,
    MATCH_MAX_DRIFT_MS,
    Item,
    align,
)

OP_RANK = {"MATCH": 0, "DELETE": 1, "INSERT": 2}


def brute_force(planned: list[Item], actual: list[Item]) -> tuple[int, tuple[int, ...]]:
    """Return ``(min_cost, lexicographically smallest ranked op string)``."""
    best: tuple[int, tuple[int, ...]] | None = None

    def rec(i: int, j: int, cost: int, ops: list[int]) -> None:
        nonlocal best
        if best is not None and cost > best[0]:
            return
        if i == len(planned) and j == len(actual):
            candidate = (cost, tuple(ops))
            if best is None or candidate < best:
                best = candidate
            return
        if i < len(planned) and j < len(actual):
            p, a = planned[i], actual[j]
            drift = abs(p.at_ms - a.at_ms)
            if p.code == a.code and drift <= MATCH_MAX_DRIFT_MS:
                rec(i + 1, j + 1, cost + drift, ops + [OP_RANK["MATCH"]])
        if i < len(planned):
            rec(i + 1, j, cost + DELETE_COST, ops + [OP_RANK["DELETE"]])
        if j < len(actual):
            rec(i, j + 1, cost + INSERT_COST, ops + [OP_RANK["INSERT"]])

    rec(0, 0, 0, [])
    assert best is not None
    return best


def assert_alignment_matches_oracle(planned: list[Item], actual: list[Item]) -> None:
    result = align(planned, actual)
    expected_cost, expected_ops = brute_force(planned, actual)

    got_ops = tuple(OP_RANK[pair.op] for pair in result.pairs)
    assert result.total_cost == expected_cost, (planned, actual)
    assert got_ops == expected_ops, (planned, actual)

    # The reported pairs must cover every item exactly once, in order.
    planned_idx = [p.planned_index for p in result.pairs if p.planned_index is not None]
    actual_idx = [p.actual_index for p in result.pairs if p.actual_index is not None]
    assert planned_idx == list(range(len(planned)))
    assert actual_idx == list(range(len(actual)))
    assert sum(p.cost for p in result.pairs) == result.total_cost

    for pair in result.pairs:
        if pair.op == "MATCH":
            p, a = planned[pair.planned_index], actual[pair.actual_index]
            assert pair.code == p.code == a.code
            assert pair.drift_ms == abs(p.at_ms - a.at_ms) <= MATCH_MAX_DRIFT_MS
            assert pair.cost == pair.drift_ms
        elif pair.op == "DELETE":
            assert pair.cost == DELETE_COST
            assert pair.code == planned[pair.planned_index].code
        else:
            assert pair.op == "INSERT"
            assert pair.cost == INSERT_COST
            assert pair.code == actual[pair.actual_index].code

    # Compliance must agree with the pairs, and the first defect must be the
    # leftmost non-compliant operation.
    expected_compliant = all(
        pair.op == "MATCH" and pair.drift_ms <= DRIFT_TOLERANCE_MS
        for pair in result.pairs
    )
    assert result.compliant == expected_compliant
    if expected_compliant:
        assert result.first_defect is None
    else:
        defect = result.first_defect
        assert defect is not None
        for earlier in result.pairs[: defect.pair_index]:
            assert earlier.op == "MATCH" and earlier.drift_ms <= DRIFT_TOLERANCE_MS
        bad = result.pairs[defect.pair_index]
        if bad.op == "DELETE":
            assert defect.code == "MISS"
        elif bad.op == "INSERT":
            assert defect.code == "EXTRA"
        else:
            assert defect.code == "DRIFT"
            assert bad.drift_ms > DRIFT_TOLERANCE_MS


# Time grid chosen so that consecutive timestamps differ by exactly 500 or
# exactly 2000 (both decision boundaries) as well as 1500/2500.
GRID_TIMES = [0, 500, 2000, 2500]
GRID_CODES = ["A", "B"]


def _grid_sequences() -> list[list[Item]]:
    sequences: list[list[Item]] = [[]]
    for length in range(1, len(GRID_TIMES) + 1):
        for codes in itertools.product(GRID_CODES, repeat=length):
            for times in itertools.combinations(GRID_TIMES, length):
                sequences.append([Item(c, t) for c, t in zip(codes, times)])
    return sequences


GRID_SEQUENCES = _grid_sequences()


@pytest.mark.parametrize("p_idx", range(len(GRID_SEQUENCES)))
def test_exhaustive_grid(p_idx: int) -> None:
    planned = GRID_SEQUENCES[p_idx]
    for actual in GRID_SEQUENCES:
        assert_alignment_matches_oracle(planned, actual)


def _random_sequence(rng: random.Random, max_len: int) -> list[Item]:
    length = rng.randint(0, max_len)
    times = sorted(rng.sample(range(0, 9000, 250), length))
    return [Item(rng.choice("AABBC"), t) for t in times]


def test_seeded_fuzz() -> None:
    rng = random.Random(20260915)
    for _ in range(400):
        planned = _random_sequence(rng, max_len=5)
        actual = _random_sequence(rng, max_len=5)
        assert_alignment_matches_oracle(planned, actual)


def test_repeated_calls_are_identical() -> None:
    planned = [Item("A", 0), Item("A", 1000), Item("A", 2000)]
    actual = [Item("A", 500), Item("A", 1500)]
    first = align(planned, actual)
    for _ in range(5):
        assert align(planned, actual) == first
