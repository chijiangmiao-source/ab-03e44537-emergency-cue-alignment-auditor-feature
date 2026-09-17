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

When ``alternative_limit`` is given, the engine additionally returns the
next-best legal paths as review candidates.  A suffix dynamic program keeps
at most ``alternative_limit + 1`` candidates per cell.  Every candidate is a
compact ``(edge cost, successor cell, successor rank)`` node rather than a
path, so complete operation strings are never copied into the table and the
legal paths are never enumerated; only the requested paths are materialised,
during backtracking.

The three candidate groups at a cell (first move MATCH / DELETE / INSERT)
each inherit a successor cell's ranking with one constant edge prepended, so
they arrive already ordered by ``(cost, operation string)``.  They are
combined with a stable, cost-keyed merge in the fixed run order
MATCH < DELETE < INSERT, which is exactly the global tie-break order without
ever comparing two operation strings.

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

# Fixed lexicographic order used to break equal-cost ties.
OP_RANK = {OP_MATCH: 0, OP_DELETE: 1, OP_INSERT: 2}

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
class Alternative:
    """A runner-up legal path reported next to the primary alignment."""

    total_cost: int
    cost_gap: int
    pairs: tuple[Pair, ...]
    compliant: bool
    first_defect: Defect | None
    first_divergence_index: int


@dataclass(frozen=True)
class Alignment:
    total_cost: int
    pairs: tuple[Pair, ...]
    compliant: bool
    first_defect: Defect | None
    # ``None`` means alternatives were not requested; an empty tuple means the
    # feature was enabled but no further legal path exists.
    alternatives: tuple[Alternative, ...] | None = None


# Candidate tuple layout.  A candidate stores only the path's first edge and a
# pointer into a successor cell's ranking -- never a path or op string.  The
# move type is encoded by the coordinate delta: (1, 1) MATCH, (1, 0) DELETE,
# (0, 1) INSERT.
_C_COST = 0
_C_EDGE = 1
_C_DI = 2
_C_DJ = 3
_C_RANK = 4


def _match_drift(planned: Item, actual: Item) -> int | None:
    """Return the match cost when the two items may match, else ``None``."""
    if planned.code != actual.code:
        return None
    drift = abs(planned.at_ms - actual.at_ms)
    if drift > MATCH_MAX_DRIFT_MS:
        return None
    return drift


class _KBestTables:
    """Suffix k-best dynamic program.

    ``table[i][j]`` holds up to ``keep`` best legal paths aligning
    ``planned[i:]`` with ``actual[j:]``, ordered by ``(cost, op string)``.
    Paths from a cell split into up to three groups by their first move; each
    group is the successor cell's ranking with one constant-cost edge
    prepended, which shifts every cost uniformly and prepends one identical
    operation, so each group stays ordered and only the inter-group merge is
    needed.
    """

    def __init__(self, planned: list[Item], actual: list[Item], keep: int):
        self._planned = planned
        self._actual = actual
        self._keep = keep

    @staticmethod
    def _merge(moves: list[tuple], keep: int) -> list[tuple]:
        """Stable k-way merge of the first-move groups of one cell.

        Each move is ``(edge_cost, di, dj, successors)`` in first-operation
        order MATCH < DELETE < INSERT; ``successors`` is the successor cell's
        ranking, already cost-nondecreasing.  Adding the constant edge cost
        keeps the group ordered, so merging by cost reproduces the global
        ``(cost, operation string)`` order: within a group the successor
        ranking is inherited, and equal costs across groups resolve by group
        order (the scan keeps the first, i.e. lowest-indexed, minimum).

        Candidate paths cannot repeat: heads from different groups differ in
        their first operation, while successive heads of one group point at
        distinct successor ranks.  At most ``keep`` heads are pulled, so each
        successor ranking is traversed lazily.
        """
        position = [0] * len(moves)
        merged: list[tuple] = []
        append = merged.append
        while len(merged) < keep:
            best_cost: int | None = None
            best_index = -1
            for move_index, (edge_cost, _, _, successors) in enumerate(moves):
                rank = position[move_index]
                if rank < len(successors):
                    cost = successors[rank][_C_COST] + edge_cost
                    if best_cost is None or cost < best_cost:
                        best_cost = cost
                        best_index = move_index
            if best_index < 0:
                break
            rank = position[best_index]
            edge_cost, di, dj, _ = moves[best_index]
            position[best_index] = rank + 1
            append((best_cost, edge_cost, di, dj, rank))
        return merged

    def build(self) -> list[list[list[tuple]]]:
        n, m = len(self._planned), len(self._actual)
        table: list[list[list[tuple]]] = [
            [[] for _ in range(m + 1)] for _ in range(n + 1)
        ]
        # The empty suffix has exactly one path, cost 0; its edge fields are
        # never read because backtracking stops on reaching this cell.
        table[n][m] = [(0, 0, 0, 0, -1)]

        for i in range(n, -1, -1):
            for j in range(m, -1, -1):
                if i == n and j == m:
                    continue
                # Move order is the MATCH < DELETE < INSERT tie-break used by
                # the stable merge.
                moves: list[tuple] = []
                if i < n and j < m:
                    drift = _match_drift(self._planned[i], self._actual[j])
                    if drift is not None:
                        moves.append(
                            (drift, 1, 1, table[i + 1][j + 1])
                        )
                if i < n:
                    moves.append((DELETE_COST, 1, 0, table[i + 1][j]))
                if j < m:
                    moves.append((INSERT_COST, 0, 1, table[i][j + 1]))
                table[i][j] = self._merge(moves, self._keep)
        return table


def _backtrack(
    planned: list[Item],
    actual: list[Item],
    table: list[list[list[tuple]]],
    start_rank: int,
) -> list[Pair]:
    """Materialise one ranked root path by following successor pointers."""
    n, m = len(planned), len(actual)
    pairs: list[Pair] = []
    i = j = 0
    rank = start_rank
    while i < n or j < m:
        candidate = table[i][j][rank]
        edge_cost = candidate[_C_EDGE]
        di, dj, next_rank = (
            candidate[_C_DI],
            candidate[_C_DJ],
            candidate[_C_RANK],
        )
        if di == 1 and dj == 1:
            p, a = planned[i], actual[j]
            pairs.append(
                Pair(
                    op=OP_MATCH,
                    cost=edge_cost,
                    code=p.code,
                    planned_index=i,
                    actual_index=j,
                    planned_at_ms=p.at_ms,
                    actual_at_ms=a.at_ms,
                    drift_ms=edge_cost,
                )
            )
        elif di == 1:
            p = planned[i]
            pairs.append(
                Pair(
                    op=OP_DELETE,
                    cost=DELETE_COST,
                    code=p.code,
                    planned_index=i,
                    planned_at_ms=p.at_ms,
                )
            )
        else:
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
        i += di
        j += dj
        rank = next_rank
    return pairs


def _first_divergence_ops(primary_ops: list[str], candidate_ops: list[str]) -> int:
    """First index at which the two operation strings differ.

    Paths always diverge by operation kind (one cell admits a single move per
    operation); if one string is a prefix of the other that boundary is the
    divergence point."""
    for index, (left, right) in enumerate(zip(primary_ops, candidate_ops)):
        if left != right:
            return index
    return min(len(primary_ops), len(candidate_ops))


def _first_defect(pairs: list[Pair]) -> Defect | None:
    for index, pair in enumerate(pairs):
        if pair.op == OP_DELETE:
            return Defect(code=DEFECT_MISS, pair_index=index, pair=pair)
        if pair.op == OP_INSERT:
            return Defect(code=DEFECT_EXTRA, pair_index=index, pair=pair)
        if pair.drift_ms is not None and pair.drift_ms > DRIFT_TOLERANCE_MS:
            return Defect(code=DEFECT_DRIFT, pair_index=index, pair=pair)
    return None


def align(
    planned: list[Item],
    actual: list[Item],
    alternative_limit: int | None = None,
) -> Alignment:
    """Align ``planned`` against ``actual`` deterministically.

    With ``alternative_limit`` (1..20) the suffix DP keeps one extra rank per
    requested alternative and the response carries up to that many runner-up
    paths after the primary optimum.
    """
    keep = 1 if alternative_limit is None else alternative_limit + 1
    table = _KBestTables(planned, actual, keep).build()

    ranked = table[0][0]
    primary_cost = ranked[0][_C_COST]
    primary_pairs = _backtrack(planned, actual, table, 0)
    primary_defect = _first_defect(primary_pairs)

    alternatives: tuple[Alternative, ...] | None = None
    if alternative_limit is not None:
        primary_ops = [pair.op for pair in primary_pairs]
        runners: list[Alternative] = []
        for candidate in ranked[1:]:
            pairs = _backtrack(planned, actual, table, len(runners) + 1)
            defect = _first_defect(pairs)
            ops = [pair.op for pair in pairs]
            runners.append(
                Alternative(
                    total_cost=candidate[_C_COST],
                    cost_gap=candidate[_C_COST] - primary_cost,
                    pairs=tuple(pairs),
                    compliant=defect is None,
                    first_defect=defect,
                    first_divergence_index=_first_divergence_ops(primary_ops, ops),
                )
            )
        alternatives = tuple(runners)

    return Alignment(
        total_cost=primary_cost,
        pairs=tuple(primary_pairs),
        compliant=primary_defect is None,
        first_defect=primary_defect,
        alternatives=alternatives,
    )
