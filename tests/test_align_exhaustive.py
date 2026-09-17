"""Exhaustive and fuzz comparison of the alignment engine against an
independent brute-force enumerator.

The brute force enumerates every legal alignment path recursively and picks
the minimum by ``(total_cost, operation_string)`` where operations rank
MATCH < DELETE < INSERT.  A second enumerator collects *every* legal path and
is used to check the candidate ranking returned with ``alternative_limit``
item by item.  Both share no code with the dynamic-programming implementation
under test, so they serve as independent oracles.
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


def brute_force_all(planned: list[Item], actual: list[Item]) -> list[tuple[int, tuple[str, ...]]]:
    """Enumerate every legal complete path as ``(cost, op string)`` pairs.

    An operation string uniquely determines a path (each op fixes which cell
    comes next), so deduplicating op strings removes no distinct alignment.
    """
    paths: set[tuple[int, tuple[str, ...]]] = set()

    def rec(i: int, j: int, cost: int, ops: list[str]) -> None:
        if i == len(planned) and j == len(actual):
            paths.add((cost, tuple(ops)))
            return
        if i < len(planned) and j < len(actual):
            p, a = planned[i], actual[j]
            drift = abs(p.at_ms - a.at_ms)
            if p.code == a.code and drift <= MATCH_MAX_DRIFT_MS:
                rec(i + 1, j + 1, cost + drift, ops + ["MATCH"])
        if i < len(planned):
            rec(i + 1, j, cost + DELETE_COST, ops + ["DELETE"])
        if j < len(actual):
            rec(i, j + 1, cost + INSERT_COST, ops + ["INSERT"])

    rec(0, 0, 0, [])
    return sorted(paths, key=lambda entry: (entry[0], tuple(OP_RANK[op] for op in entry[1])))


def _path_is_well_formed(planned, actual, pairs, total_cost) -> None:
    # The reported pairs must cover every item exactly once, in order.
    planned_idx = [p.planned_index for p in pairs if p.planned_index is not None]
    actual_idx = [p.actual_index for p in pairs if p.actual_index is not None]
    assert planned_idx == list(range(len(planned)))
    assert actual_idx == list(range(len(actual)))
    assert sum(p.cost for p in pairs) == total_cost

    for pair in pairs:
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


def _assert_path_compliance(pairs, alignment_like) -> None:
    # Compliance must agree with the pairs, and the first defect must be the
    # leftmost non-compliant operation.
    expected_compliant = all(
        pair.op == "MATCH" and pair.drift_ms <= DRIFT_TOLERANCE_MS
        for pair in pairs
    )
    assert alignment_like.compliant is expected_compliant
    if expected_compliant:
        assert alignment_like.first_defect is None
        return
    defect = alignment_like.first_defect
    assert defect is not None
    for earlier in pairs[: defect.pair_index]:
        assert earlier.op == "MATCH" and earlier.drift_ms <= DRIFT_TOLERANCE_MS
    bad = pairs[defect.pair_index]
    if bad.op == "DELETE":
        assert defect.code == "MISS"
    elif bad.op == "INSERT":
        assert defect.code == "EXTRA"
    else:
        assert defect.code == "DRIFT"
        assert bad.drift_ms > DRIFT_TOLERANCE_MS


def assert_alignment_matches_oracle(planned: list[Item], actual: list[Item]) -> None:
    result = align(planned, actual)
    expected_cost, expected_ops = brute_force(planned, actual)

    got_ops = tuple(OP_RANK[pair.op] for pair in result.pairs)
    assert result.total_cost == expected_cost, (planned, actual)
    assert got_ops == expected_ops, (planned, actual)

    _path_is_well_formed(planned, actual, result.pairs, result.total_cost)
    _assert_path_compliance(result.pairs, result)


def assert_alternatives_match_oracle(
    planned: list[Item], actual: list[Item], limit: int
) -> None:
    """Compare every reported rank against the fully enumerated ranking."""
    oracle = brute_force_all(planned, actual)
    result = align(planned, actual, alternative_limit=limit)

    # Enabling candidates must never disturb the primary optimum.
    plain = align(planned, actual)
    assert result.total_cost == plain.total_cost
    assert result.pairs == plain.pairs
    assert result.compliant == plain.compliant
    assert result.first_defect == plain.first_defect

    all_paths = [(result.total_cost, tuple(p.op for p in result.pairs))]
    all_paths += [
        (alt.total_cost, tuple(p.op for p in alt.pairs)) for alt in result.alternatives
    ]

    expected_count = min(limit, len(oracle) - 1)
    assert len(result.alternatives) == expected_count, (planned, actual, limit)

    ranked_oracle = oracle[: expected_count + 1]
    assert [path for path in all_paths] == [
        (cost, ops) for cost, ops in ranked_oracle
    ], (planned, actual, limit)

    # No path may appear twice.
    assert len(set(all_paths)) == len(all_paths)

    primary_ops = all_paths[0][1]
    for offset, alt in enumerate(result.alternatives, start=1):
        expected_cost, expected_ops = oracle[offset]
        assert alt.total_cost == expected_cost
        assert alt.cost_gap == expected_cost - oracle[0][0]
        assert alt.cost_gap >= 0

        pairs = list(alt.pairs)
        _path_is_well_formed(planned, actual, pairs, alt.total_cost)
        _assert_path_compliance(pairs, alt)

        divergence = next(
            (
                index
                for index, (left, right) in enumerate(zip(primary_ops, expected_ops))
                if left != right
            ),
            min(len(primary_ops), len(expected_ops)),
        )
        assert alt.first_divergence_index == divergence


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
        for limit in (1, 3, 20):
            assert_alternatives_match_oracle(planned, actual, limit)


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
        for limit in (1, 5, 20):
            assert_alternatives_match_oracle(planned, actual, limit)


def test_seeded_fuzz_truncated_prefix() -> None:
    # Longer lattices (up to 8x8 -> 12,870 monotone paths) force the per-cell
    # cap to prune most of the legal ranking; the surviving prefix must still
    # match the exhaustive ordering item by item.
    rng = random.Random(20260917)
    for _ in range(30):
        planned = _random_sequence(rng, max_len=8)
        actual = _random_sequence(rng, max_len=8)
        for limit in (1, 2, 20):
            assert_alternatives_match_oracle(planned, actual, limit)


def test_repeated_calls_are_identical() -> None:
    planned = [Item("A", 0), Item("A", 1000), Item("A", 2000)]
    actual = [Item("A", 500), Item("A", 1500)]
    first = align(planned, actual)
    for _ in range(5):
        assert align(planned, actual) == first
    first_alternatives = align(planned, actual, alternative_limit=20)
    for _ in range(5):
        assert align(planned, actual, alternative_limit=20) == first_alternatives
