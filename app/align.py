"""Weighted sequence alignment between a planned and an actual broadcast log.

Cost model
----------
- MATCH: a planned item and an actual item may match only when they carry the
  same ``code`` and their timestamps differ by at most ``MATCH_MAX_DRIFT_MS``
  (2000 ms).  The cost of a match is the absolute time difference.
- DELETE: a planned item with no counterpart costs ``DELETE_COST`` (2500).
- INSERT: an actual item with no counterpart costs ``INSERT_COST`` (2500).

The alignment minimises the total cost.  Ties are broken by the
lexicographic order of the complete operation string with the fixed
operation order MATCH < DELETE < INSERT, which makes the optimal path
unique even when codes repeat (an operation string uniquely determines a
path, and the lexicographically smallest one is well defined).

Compliance
----------
An alignment is compliant only when the path consists solely of matches and
every match drifts by at most ``DRIFT_TOLERANCE_MS`` (500 ms).  Otherwise the
first defect scanning the path left to right is reported: ``MISS`` for a
deleted planned item, ``EXTRA`` for an inserted actual item and ``DRIFT``
for a match whose drift exceeds the tolerance.
"""

from __future__ import annotations

from dataclasses import dataclass

MATCH_MAX_DRIFT_MS = 2000
DRIFT_TOLERANCE_MS = 500
DELETE_COST = 2500
INSERT_COST = 2500

OP_MATCH = "MATCH"
OP_DELETE = "DELETE"
OP_INSERT = "INSERT"

DEFECT_MISS = "MISS"
DEFECT_EXTRA = "EXTRA"
DEFECT_DRIFT = "DRIFT"


@dataclass(frozen=True)
class Item:
    """A single planned or actual broadcast entry."""

    code: str
    at_ms: int


@dataclass(frozen=True)
class Pair:
    """One operation of the alignment path."""

    op: str
    cost: int
    code: str
    planned_index: int | None = None
    actual_index: int | None = None
    planned_at_ms: int | None = None
    actual_at_ms: int | None = None
    drift_ms: int | None = None


@dataclass(frozen=True)
class Defect:
    """The leftmost non-compliant operation of the path."""

    code: str
    pair_index: int
    pair: Pair


@dataclass(frozen=True)
class Alignment:
    total_cost: int
    pairs: tuple[Pair, ...]
    compliant: bool
    first_defect: Defect | None


def _match_drift(planned: Item, actual: Item) -> int | None:
    """Return the match cost when the two items may match, else ``None``."""
    if planned.code != actual.code:
        return None
    drift = abs(planned.at_ms - actual.at_ms)
    if drift > MATCH_MAX_DRIFT_MS:
        return None
    return drift


def _min_cost_table(planned: list[Item], actual: list[Item]) -> list[list[int]]:
    """Suffix dynamic program: ``dp[i][j]`` is the minimum cost of aligning
    ``planned[i:]`` with ``actual[j:]``."""
    n, m = len(planned), len(actual)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        dp[i][m] = dp[i + 1][m] + DELETE_COST
    for j in range(m - 1, -1, -1):
        dp[n][j] = dp[n][j + 1] + INSERT_COST
    for i in range(n - 1, -1, -1):
        row, nxt = dp[i], dp[i + 1]
        p = planned[i]
        for j in range(m - 1, -1, -1):
            best = DELETE_COST + nxt[j]
            inserted = INSERT_COST + row[j + 1]
            if inserted < best:
                best = inserted
            drift = _match_drift(p, actual[j])
            if drift is not None:
                matched = drift + nxt[j + 1]
                if matched < best:
                    best = matched
            row[j] = best
    return dp


def _reconstruct(
    planned: list[Item], actual: list[Item], dp: list[list[int]]
) -> list[Pair]:
    """Walk the table emitting the lexicographically smallest minimal-cost
    operation string (MATCH < DELETE < INSERT at every step)."""
    n, m = len(planned), len(actual)
    pairs: list[Pair] = []
    i = j = 0
    while i < n or j < m:
        if i < n and j < m:
            drift = _match_drift(planned[i], actual[j])
            if drift is not None and dp[i][j] == drift + dp[i + 1][j + 1]:
                pairs.append(
                    Pair(
                        op=OP_MATCH,
                        cost=drift,
                        code=planned[i].code,
                        planned_index=i,
                        actual_index=j,
                        planned_at_ms=planned[i].at_ms,
                        actual_at_ms=actual[j].at_ms,
                        drift_ms=drift,
                    )
                )
                i += 1
                j += 1
                continue
        if i < n and dp[i][j] == DELETE_COST + dp[i + 1][j]:
            pairs.append(
                Pair(
                    op=OP_DELETE,
                    cost=DELETE_COST,
                    code=planned[i].code,
                    planned_index=i,
                    planned_at_ms=planned[i].at_ms,
                )
            )
            i += 1
            continue
        a = actual[j]
        pairs.append(
            Pair(
                op=OP_INSERT,
                cost=INSERT_COST,
                code=a.code,
                actual_index=j,
                actual_at_ms=a.at_ms,
            )
        )
        j += 1
    return pairs


def _first_defect(pairs: list[Pair]) -> Defect | None:
    for index, pair in enumerate(pairs):
        if pair.op == OP_DELETE:
            return Defect(code=DEFECT_MISS, pair_index=index, pair=pair)
        if pair.op == OP_INSERT:
            return Defect(code=DEFECT_EXTRA, pair_index=index, pair=pair)
        if pair.drift_ms is not None and pair.drift_ms > DRIFT_TOLERANCE_MS:
            return Defect(code=DEFECT_DRIFT, pair_index=index, pair=pair)
    return None


def align(planned: list[Item], actual: list[Item]) -> Alignment:
    """Align ``planned`` against ``actual`` deterministically."""
    dp = _min_cost_table(planned, actual)
    pairs = _reconstruct(planned, actual, dp)
    defect = _first_defect(pairs)
    return Alignment(
        total_cost=dp[0][0],
        pairs=tuple(pairs),
        compliant=defect is None,
        first_defect=defect,
    )
