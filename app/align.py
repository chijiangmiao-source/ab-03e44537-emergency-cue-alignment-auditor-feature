"""Weighted sequence alignment between a planned and an actual broadcast log.

Cost model
----------
- MATCH: a planned item and an actual item may match only when they carry
  the same ``code`` and their timestamps differ by at most ``MATCH_MAX_DRIFT_MS``
  (2000 ms).  The cost of a match is the absolute time difference.
- DELETE: a planned item with no counterpart costs ``DELETE_COST`` (2500).
- INSERT: an actual item with no counterpart costs ``INSERT_COST`` (2500).

The alignment minimises the total cost.  Ties are broken by the
lexicographic order of the complete operation string with the fixed
operation order MATCH < DELETE < INSERT, which makes the optimal path
unique even when codes repeat (an operation string uniquely determines a
path, and the lexicographically smallest one is well defined).

Runner-up paths
---------------
``align_with_alternatives`` additionally reports the best runner-up paths
under the very same ``(total_cost, operation_string)`` order, so reviewers
can judge whether the reported first defect hinges on the adjudication
between near-tied paths.  The ranking is produced by a suffix dynamic
program that keeps at most ``limit + 1`` candidates per cell; each
candidate stores only its cost, its first operation and a link to the
continuation candidate, so no operation string is ever copied while the
table is built.  Backtracking the linked candidates from the root cell
then yields the globally top-ranked complete paths without enumerating
the path space.

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


@dataclass(frozen=True)
class Alternative:
    """A runner-up alignment ranked just after the preferred path."""

    total_cost: int
    cost_gap: int
    pairs: tuple[Pair, ...]
    compliant: bool
    first_defect: Defect | None
    first_divergence_index: int


@dataclass(frozen=True)
class _Candidate:
    """One ranked suffix solution inside the dynamic program.

    ``cost`` is the total cost of aligning the remaining suffixes, ``op``
    the first operation of the path (``None`` only for the empty terminal
    suffix) and ``src_rank`` the rank of the continuation candidate inside
    the cell the first operation leads to.  The source cell itself is
    implied by ``op`` (MATCH -> diagonal, DELETE -> down, INSERT -> right),
    so a candidate is a constant-size link and no operation string is ever
    materialised while the table is built.
    """

    cost: int
    op: str | None
    src_rank: int


def _match_drift(planned: Item, actual: Item) -> int | None:
    """Return the match cost when the two items may match, else ``None``."""
    if planned.code != actual.code:
        return None
    drift = abs(planned.at_ms - actual.at_ms)
    if drift > MATCH_MAX_DRIFT_MS:
        return None
    return drift


def _top_suffix_table(
    planned: list[Item], actual: list[Item], keep: int
) -> list[list[list[_Candidate]]]:
    """Suffix dynamic program keeping the ``keep`` best candidates per cell.

    ``table[i][j]`` lists the up to ``keep`` best ways to align
    ``planned[i:]`` with ``actual[j:]``, best first, under the
    ``(total_cost, operation_string)`` order.  A global top-``keep`` path
    only ever chains top-``keep`` suffixes: prepending one shared prefix to
    two suffixes preserves their order, so a path whose suffix fell outside
    the cell's top ``keep`` would be beaten by ``keep`` other complete
    paths and could not be in the global top ``keep`` itself.
    """
    n, m = len(planned), len(actual)
    table: list[list[list[_Candidate]]] = [
        [[] for _ in range(m + 1)] for _ in range(n + 1)
    ]
    table[n][m] = [_Candidate(0, None, 0)]
    for i in range(n - 1, -1, -1):
        table[i][m] = [_Candidate(DELETE_COST + table[i + 1][m][0].cost, OP_DELETE, 0)]
    for j in range(m - 1, -1, -1):
        table[n][j] = [_Candidate(INSERT_COST + table[n][j + 1][0].cost, OP_INSERT, 0)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            # One sorted stream per operation, best continuation first.
            # Prepending a fixed operation to an already sorted candidate
            # list keeps it sorted, and candidates from different streams
            # differ in the first operation, so merging by
            # ``(cost, operation rank)`` reproduces the full
            # ``(cost, operation string)`` order without comparing strings.
            streams: list[tuple[int, str, int, list[_Candidate]]] = []
            drift = _match_drift(planned[i], actual[j])
            if drift is not None:
                streams.append((0, OP_MATCH, drift, table[i + 1][j + 1]))
            streams.append((1, OP_DELETE, DELETE_COST, table[i + 1][j]))
            streams.append((2, OP_INSERT, INSERT_COST, table[i][j + 1]))
            merged: list[_Candidate] = []
            positions = [0] * len(streams)
            while len(merged) < keep:
                best_stream = -1
                best_key: tuple[int, int] | None = None
                for stream, (rank, _op, base, source) in enumerate(streams):
                    pos = positions[stream]
                    if pos >= len(source):
                        continue
                    key = (base + source[pos].cost, rank)
                    if best_key is None or key < best_key:
                        best_key = key
                        best_stream = stream
                if best_stream < 0:
                    break
                _rank, op, base, source = streams[best_stream]
                pos = positions[best_stream]
                merged.append(_Candidate(base + source[pos].cost, op, pos))
                positions[best_stream] += 1
            table[i][j] = merged
    return table


def _reconstruct(
    planned: list[Item],
    actual: list[Item],
    table: list[list[list[_Candidate]]],
    rank: int,
) -> list[Pair]:
    """Walk the linked candidates emitting the ``rank``-th best path."""
    n, m = len(planned), len(actual)
    pairs: list[Pair] = []
    i = j = 0
    while i < n or j < m:
        candidate = table[i][j][rank]
        if candidate.op == OP_MATCH:
            p, a = planned[i], actual[j]
            # The table only links items that passed the match gate.
            drift = abs(p.at_ms - a.at_ms)
            pairs.append(
                Pair(
                    op=OP_MATCH,
                    cost=drift,
                    code=p.code,
                    planned_index=i,
                    actual_index=j,
                    planned_at_ms=p.at_ms,
                    actual_at_ms=a.at_ms,
                    drift_ms=drift,
                )
            )
            i += 1
            j += 1
        elif candidate.op == OP_DELETE:
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
            j += 1
        rank = candidate.src_rank
    return pairs


def _first_defect(pairs: list[Pair] | tuple[Pair, ...]) -> Defect | None:
    for index, pair in enumerate(pairs):
        if pair.op == OP_DELETE:
            return Defect(code=DEFECT_MISS, pair_index=index, pair=pair)
        if pair.op == OP_INSERT:
            return Defect(code=DEFECT_EXTRA, pair_index=index, pair=pair)
        if pair.drift_ms is not None and pair.drift_ms > DRIFT_TOLERANCE_MS:
            return Defect(code=DEFECT_DRIFT, pair_index=index, pair=pair)
    return None


def _first_divergence_index(
    preferred: tuple[Pair, ...], other: tuple[Pair, ...]
) -> int:
    """First position where the two operation strings differ.

    Distinct complete paths always differ within their shared length: one
    cannot be a strict prefix of the other, because a complete path ends
    exactly when every planned and actual item has been consumed.
    """
    for index, (left, right) in enumerate(zip(preferred, other)):
        if left.op != right.op:
            return index
    return min(len(preferred), len(other))  # unreachable for distinct paths


def _top_alignments(
    planned: list[Item], actual: list[Item], keep: int
) -> list[Alignment]:
    """Return the up to ``keep`` best alignments, best first."""
    table = _top_suffix_table(planned, actual, keep)
    ranked: list[Alignment] = []
    for rank, candidate in enumerate(table[0][0]):
        pairs = _reconstruct(planned, actual, table, rank)
        defect = _first_defect(pairs)
        ranked.append(
            Alignment(
                total_cost=candidate.cost,
                pairs=tuple(pairs),
                compliant=defect is None,
                first_defect=defect,
            )
        )
    return ranked


def align(planned: list[Item], actual: list[Item]) -> Alignment:
    """Align ``planned`` against ``actual`` deterministically."""
    return _top_alignments(planned, actual, 1)[0]


def align_with_alternatives(
    planned: list[Item], actual: list[Item], limit: int
) -> tuple[Alignment, list[Alternative]]:
    """Align and also report the up to ``limit`` best runner-up paths.

    Every legal complete path is ranked by ``(total_cost, operation
    string)``; the preferred alignment is rank zero and the alternatives
    follow in rank order, each annotated with its cost gap against the
    preferred path and the index of the first operation where the two
    paths diverge.  Fewer than ``limit`` alternatives are returned when
    not enough distinct legal paths exist; paths are never duplicated.
    """
    ranked = _top_alignments(planned, actual, limit + 1)
    preferred = ranked[0]
    alternatives: list[Alternative] = []
    for runner_up in ranked[1:]:
        alternatives.append(
            Alternative(
                total_cost=runner_up.total_cost,
                cost_gap=runner_up.total_cost - preferred.total_cost,
                pairs=runner_up.pairs,
                compliant=runner_up.compliant,
                first_defect=runner_up.first_defect,
                first_divergence_index=_first_divergence_index(
                    preferred.pairs, runner_up.pairs
                ),
            )
        )
    return preferred, alternatives
