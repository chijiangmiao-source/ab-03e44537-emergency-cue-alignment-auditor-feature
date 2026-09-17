"""Response models used for the OpenAPI schema and serialization."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .align import Alignment, Alternative, Defect, Pair


class PairOut(BaseModel):
    op: Literal["MATCH", "DELETE", "INSERT"]
    cost: int
    code: str
    planned_index: int | None = None
    actual_index: int | None = None
    planned_at_ms: int | None = None
    actual_at_ms: int | None = None
    drift_ms: int | None = None


class DefectOut(BaseModel):
    code: Literal["MISS", "EXTRA", "DRIFT"]
    pair_index: int
    pair: PairOut


class AlternativeOut(BaseModel):
    total_cost: int
    cost_gap: int
    pairs: list[PairOut]
    compliant: bool
    first_defect: DefectOut | None
    first_divergence_index: int


class AlignResponse(BaseModel):
    compliant: bool
    total_cost: int
    pairs: list[PairOut]
    first_defect: DefectOut | None
    # Appended only when the request carries ``alternative_limit``; absent
    # otherwise so legacy responses stay byte-for-byte unchanged.
    alternatives: list[AlternativeOut] | None = None


class ErrorDetailOut(BaseModel):
    code: str
    path: str
    message: str


class ErrorBody(BaseModel):
    code: Literal["VALIDATION_FAILED"]
    message: str
    details: list[ErrorDetailOut]


class ErrorResponse(BaseModel):
    error: ErrorBody


def pair_to_dict(pair: Pair) -> dict:
    """Serialize a pair, omitting fields that do not apply to its op."""
    data: dict = {"op": pair.op, "cost": pair.cost, "code": pair.code}
    if pair.planned_index is not None:
        data["planned_index"] = pair.planned_index
    if pair.actual_index is not None:
        data["actual_index"] = pair.actual_index
    if pair.planned_at_ms is not None:
        data["planned_at_ms"] = pair.planned_at_ms
    if pair.actual_at_ms is not None:
        data["actual_at_ms"] = pair.actual_at_ms
    if pair.drift_ms is not None:
        data["drift_ms"] = pair.drift_ms
    return data


def defect_to_dict(defect: Defect) -> dict:
    return {
        "code": defect.code,
        "pair_index": defect.pair_index,
        "pair": pair_to_dict(defect.pair),
    }


def alternative_to_dict(alternative: Alternative) -> dict:
    return {
        "total_cost": alternative.total_cost,
        "cost_gap": alternative.cost_gap,
        "pairs": [pair_to_dict(pair) for pair in alternative.pairs],
        "compliant": alternative.compliant,
        "first_defect": (
            None
            if alternative.first_defect is None
            else defect_to_dict(alternative.first_defect)
        ),
        "first_divergence_index": alternative.first_divergence_index,
    }


def alignment_to_dict(result: Alignment) -> dict:
    data = {
        "compliant": result.compliant,
        "total_cost": result.total_cost,
        "pairs": [pair_to_dict(pair) for pair in result.pairs],
        "first_defect": (
            None
            if result.first_defect is None
            else defect_to_dict(result.first_defect)
        ),
    }
    # Appended only when requested, keeping the legacy body byte-identical.
    if result.alternatives is not None:
        data["alternatives"] = [
            alternative_to_dict(alternative) for alternative in result.alternatives
        ]
    return data
