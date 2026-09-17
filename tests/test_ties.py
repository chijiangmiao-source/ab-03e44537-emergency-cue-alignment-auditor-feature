"""Explicit tie-breaking tests: equal-cost paths must collapse to one
deterministic answer via the MATCH < DELETE < INSERT lexicographic rule."""

import pytest


@pytest.mark.parametrize(
    "planned,actual,expected_ops,expected_cost",
    [
        # Two planned copies of A, one actual: either copy could match for
        # the same total cost; the leftmost planned item must win.
        (
            [{"code": "A", "at_ms": 0}, {"code": "A", "at_ms": 1000}],
            [{"code": "A", "at_ms": 500}],
            ["MATCH", "DELETE"],
            3000,
        ),
        # One planned, two actual copies: the earliest actual slot wins.
        (
            [{"code": "A", "at_ms": 500}],
            [{"code": "A", "at_ms": 0}, {"code": "A", "at_ms": 1000}],
            ["MATCH", "INSERT"],
            3000,
        ),
        # Unmatchable codes: DELETE and INSERT tie; DELETE sorts first.
        (
            [{"code": "A", "at_ms": 0}],
            [{"code": "B", "at_ms": 0}],
            ["DELETE", "INSERT"],
            5000,
        ),
        # Three minimal paths (M,M,D / M,D,M / D,M,M) all cost 3500; only
        # the lexicographically smallest operation string may be returned.
        (
            [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
                {"code": "A", "at_ms": 2000},
            ],
            [{"code": "A", "at_ms": 500}, {"code": "A", "at_ms": 1500}],
            ["MATCH", "MATCH", "DELETE"],
            3500,
        ),
        # Mirror image with inserts: I,M,M / M,I,M / M,M,I tie at 3500.
        (
            [{"code": "A", "at_ms": 500}, {"code": "A", "at_ms": 1500}],
            [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
                {"code": "A", "at_ms": 2000},
            ],
            ["MATCH", "MATCH", "INSERT"],
            3500,
        ),
        # A match at the 2000ms gate (cost 2000) beats DELETE+INSERT (5000),
        # so no tie here: matching is strictly cheaper.
        (
            [{"code": "A", "at_ms": 0}],
            [{"code": "A", "at_ms": 2000}],
            ["MATCH"],
            2000,
        ),
    ],
)
def test_tie_breaks_to_single_path(
    client, planned, actual, expected_ops, expected_cost
):
    payload = {"planned": planned, "actual": actual}
    body = client.post("/align", json=payload).json()
    assert [p["op"] for p in body["pairs"]] == expected_ops
    assert body["total_cost"] == expected_cost
    # Repeating the request must reproduce the identical body byte for byte.
    again = client.post("/align", json=payload)
    assert again.text == client.post("/align", json=payload).text
    assert again.json() == body


def test_duplicate_codes_pin_first_violation(client):
    # With duplicate codes a naive left-to-right search could pair the later
    # copy with the earlier slot; the tie-break must instead keep the
    # leftmost alignment so the first defect stays reviewable.
    body = client.post(
        "/align",
        json={
            "planned": [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
            ],
            "actual": [{"code": "A", "at_ms": 500}],
        },
    ).json()
    assert body["pairs"][0]["op"] == "MATCH"
    assert body["pairs"][0]["planned_index"] == 0
    assert body["pairs"][1] == {
        "op": "DELETE",
        "cost": 2500,
        "code": "A",
        "planned_index": 1,
        "planned_at_ms": 1000,
    }
    assert body["first_defect"] == {
        "code": "MISS",
        "pair_index": 1,
        "pair": body["pairs"][1],
    }
