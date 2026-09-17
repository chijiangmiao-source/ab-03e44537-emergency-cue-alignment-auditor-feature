"""Tests for the ``alternative_limit`` feature: runner-up paths, cost gaps,
divergence positions, validation and legacy-request compatibility."""

import json

import pytest


def post(client, payload):
    return client.post("/align", json=payload)


def test_unique_optimum_yields_positive_cost_gap(client):
    # Only one cheap path exists (MATCH at 500); every runner-up must carry
    # a strictly positive cost gap.
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 1500}],
            "alternative_limit": 5,
        },
    ).json()
    assert body["compliant"] is True
    assert body["total_cost"] == 500
    alternatives = body["alternatives"]
    # DELETE+INSERT and INSERT+DELETE, both at cost 5000, DELETE first.
    assert len(alternatives) == 2
    first, second = alternatives
    assert first["total_cost"] == 5000
    assert first["cost_gap"] == 4500 > 0
    assert [p["op"] for p in first["pairs"]] == ["DELETE", "INSERT"]
    assert first["compliant"] is False
    assert first["first_defect"]["code"] == "MISS"
    assert first["first_defect"]["pair_index"] == 0
    assert first["first_divergence_index"] == 0
    assert second["total_cost"] == 5000
    assert second["cost_gap"] == 4500
    assert [p["op"] for p in second["pairs"]] == ["INSERT", "DELETE"]
    assert second["compliant"] is False
    assert second["first_defect"]["code"] == "EXTRA"
    assert second["first_divergence_index"] == 0


def test_equal_cost_alternative_with_duplicate_codes(client):
    # planned A@0, A@1000 vs actual A@500: MATCH,DELETE and DELETE,MATCH tie
    # at 3000; the runner-up shows the adjudication did matter for the defect.
    body = post(
        client,
        {
            "planned": [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
            ],
            "actual": [{"code": "A", "at_ms": 500}],
            "alternative_limit": 1,
        },
    ).json()
    assert body["total_cost"] == 3000
    assert [p["op"] for p in body["pairs"]] == ["MATCH", "DELETE"]
    assert body["alternatives"] == [
        {
            "total_cost": 3000,
            "cost_gap": 0,
            "pairs": [
                {
                    "op": "DELETE",
                    "cost": 2500,
                    "code": "A",
                    "planned_index": 0,
                    "planned_at_ms": 0,
                },
                {
                    "op": "MATCH",
                    "cost": 500,
                    "code": "A",
                    "planned_index": 1,
                    "actual_index": 0,
                    "planned_at_ms": 1000,
                    "actual_at_ms": 500,
                    "drift_ms": 500,
                },
            ],
            "compliant": False,
            "first_defect": {
                "code": "MISS",
                "pair_index": 0,
                "pair": {
                    "op": "DELETE",
                    "cost": 2500,
                    "code": "A",
                    "planned_index": 0,
                    "planned_at_ms": 0,
                },
            },
            "first_divergence_index": 0,
        }
    ]


def test_equal_cost_alternatives_follow_operation_string_order(client):
    # Three minimal alignments cost 3500; the runner-ups must appear in
    # lexicographic operation-string order with MATCH < DELETE < INSERT.
    body = post(
        client,
        {
            "planned": [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
                {"code": "A", "at_ms": 2000},
            ],
            "actual": [
                {"code": "A", "at_ms": 500},
                {"code": "A", "at_ms": 1500},
            ],
            "alternative_limit": 2,
        },
    ).json()
    assert [p["op"] for p in body["pairs"]] == ["MATCH", "MATCH", "DELETE"]
    assert body["total_cost"] == 3500
    alternatives = body["alternatives"]
    assert len(alternatives) == 2
    assert [[p["op"] for p in a["pairs"]] for a in alternatives] == [
        ["MATCH", "DELETE", "MATCH"],
        ["DELETE", "MATCH", "MATCH"],
    ]
    assert [a["cost_gap"] for a in alternatives] == [0, 0]
    assert [a["first_divergence_index"] for a in alternatives] == [1, 0]
    assert alternatives[0]["first_defect"] == {
        "code": "MISS",
        "pair_index": 1,
        "pair": alternatives[0]["pairs"][1],
    }
    assert alternatives[1]["first_defect"]["pair_index"] == 0
    # No duplicate paths across the whole response.
    op_strings = [tuple(p["op"] for p in body["pairs"])]
    op_strings += [tuple(p["op"] for p in a["pairs"]) for a in alternatives]
    assert len(set(op_strings)) == len(op_strings)


def test_fewer_paths_than_limit_returns_actual_count(client):
    # Unmatchable codes allow exactly two paths; asking for 20 alternatives
    # must return the single actual runner-up.
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 0}],
            "actual": [{"code": "B", "at_ms": 0}],
            "alternative_limit": 20,
        },
    ).json()
    assert body["total_cost"] == 5000
    assert [p["op"] for p in body["pairs"]] == ["DELETE", "INSERT"]
    assert len(body["alternatives"]) == 1
    alternative = body["alternatives"][0]
    assert [p["op"] for p in alternative["pairs"]] == ["INSERT", "DELETE"]
    assert alternative["cost_gap"] == 0
    assert alternative["first_divergence_index"] == 0
    assert alternative["first_defect"]["code"] == "EXTRA"


def test_single_possible_path_yields_no_alternatives(client):
    for payload in (
        {"planned": [], "actual": []},
        {"planned": [{"code": "A", "at_ms": 0}], "actual": []},
        {"planned": [], "actual": [{"code": "A", "at_ms": 0}]},
    ):
        body = post(client, {**payload, "alternative_limit": 10}).json()
        assert body["alternatives"] == []


def test_alternatives_appended_after_legacy_fields(client):
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 0}],
            "actual": [{"code": "A", "at_ms": 100}],
            "alternative_limit": 1,
        },
    ).json()
    assert list(body) == [
        "compliant",
        "total_cost",
        "pairs",
        "first_defect",
        "alternatives",
    ]
    for alternative in body["alternatives"]:
        assert list(alternative) == [
            "total_cost",
            "cost_gap",
            "pairs",
            "compliant",
            "first_defect",
            "first_divergence_index",
        ]


def test_preferred_result_matches_legacy_request(client):
    base = {
        "planned": [
            {"code": "A", "at_ms": 0},
            {"code": "B", "at_ms": 1000},
            {"code": "C", "at_ms": 2000},
        ],
        "actual": [
            {"code": "A", "at_ms": 100},
            {"code": "C", "at_ms": 2900},
            {"code": "D", "at_ms": 3000},
        ],
    }
    legacy = post(client, base).json()
    with_alternatives = post(client, {**base, "alternative_limit": 4}).json()
    for key in ("compliant", "total_cost", "pairs", "first_defect"):
        assert with_alternatives[key] == legacy[key]


def test_omitted_alternative_limit_keeps_legacy_bytes(client):
    payload = {
        "planned": [
            {"code": "A", "at_ms": 0},
            {"code": "A", "at_ms": 1000},
        ],
        "actual": [{"code": "A", "at_ms": 500}],
    }
    resp = post(client, payload)
    assert resp.status_code == 200
    expected = {
        "compliant": False,
        "total_cost": 3000,
        "pairs": [
            {
                "op": "MATCH",
                "cost": 500,
                "code": "A",
                "planned_index": 0,
                "actual_index": 0,
                "planned_at_ms": 0,
                "actual_at_ms": 500,
                "drift_ms": 500,
            },
            {
                "op": "DELETE",
                "cost": 2500,
                "code": "A",
                "planned_index": 1,
                "planned_at_ms": 1000,
            },
        ],
        "first_defect": {
            "code": "MISS",
            "pair_index": 1,
            "pair": {
                "op": "DELETE",
                "cost": 2500,
                "code": "A",
                "planned_index": 1,
                "planned_at_ms": 1000,
            },
        },
    }
    assert resp.json() == expected
    assert list(resp.json()) == ["compliant", "total_cost", "pairs", "first_defect"]
    # Byte for byte: compact JSON, legacy key order, no alternatives key.
    assert resp.text == json.dumps(expected, separators=(",", ":"))


def test_alternatives_response_is_deterministic(client):
    payload = {
        "planned": [
            {"code": "A", "at_ms": 0},
            {"code": "A", "at_ms": 1000},
            {"code": "A", "at_ms": 2000},
        ],
        "actual": [
            {"code": "A", "at_ms": 500},
            {"code": "A", "at_ms": 1500},
        ],
        "alternative_limit": 3,
    }
    bodies = {post(client, payload).text for _ in range(3)}
    assert len(bodies) == 1


@pytest.mark.parametrize("limit", [1, 2, 19, 20])
def test_alternative_limit_accepts_in_range_integers(client, limit):
    resp = post(client, {"planned": [], "actual": [], "alternative_limit": limit})
    assert resp.status_code == 200
    assert resp.json()["alternatives"] == []


@pytest.mark.parametrize(
    "bad", [0, -1, 21, 100, 1.5, 2.0, "3", True, False, None, [3], {"limit": 3}]
)
def test_alternative_limit_rejects_invalid_values(client, bad):
    payload = {"planned": [], "actual": [], "alternative_limit": bad}
    resp = post(client, payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    details = body["error"]["details"]
    assert [d["code"] for d in details] == ["INVALID_ALTERNATIVE_LIMIT"]
    assert details[0]["path"] == "alternative_limit"
    assert set(details[0]) == {"code", "path", "message"}
    # The 422 envelope stays deterministic for the same payload.
    assert post(client, payload).text == resp.text


def test_alternative_limit_error_combines_with_structural_errors(client):
    body = post(client, {"actual": [], "alternative_limit": 0}).json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    codes = [d["code"] for d in body["error"]["details"]]
    assert codes == ["MISSING_FIELD", "INVALID_ALTERNATIVE_LIMIT"]
