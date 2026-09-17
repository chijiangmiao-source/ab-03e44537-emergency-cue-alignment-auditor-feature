"""HTTP-level tests: contract, boundaries, defects, alternatives and 422
machine codes."""


def post(client, payload):
    return client.post("/align", json=payload)


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_compliant_exact_replay(client):
    resp = post(
        client,
        {
            "planned": [{"code": "ADS1", "at_ms": 1000}],
            "actual": [{"code": "ADS1", "at_ms": 1000}],
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "compliant": True,
        "total_cost": 0,
        "pairs": [
            {
                "op": "MATCH",
                "cost": 0,
                "code": "ADS1",
                "planned_index": 0,
                "actual_index": 0,
                "planned_at_ms": 1000,
                "actual_at_ms": 1000,
                "drift_ms": 0,
            }
        ],
        "first_defect": None,
    }


def test_compliant_at_exactly_500ms(client):
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 1500}],
        },
    ).json()
    assert body["compliant"] is True
    assert body["total_cost"] == 500
    assert body["first_defect"] is None


def test_drift_defect_at_501ms(client):
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 1501}],
        },
    ).json()
    assert body["compliant"] is False
    assert body["total_cost"] == 501
    assert body["first_defect"]["code"] == "DRIFT"
    assert body["first_defect"]["pair_index"] == 0
    assert body["first_defect"]["pair"]["drift_ms"] == 501


def test_match_gate_at_exactly_2000ms(client):
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 3000}],
        },
    ).json()
    assert [p["op"] for p in body["pairs"]] == ["MATCH"]
    assert body["total_cost"] == 2000
    assert body["compliant"] is False
    assert body["first_defect"]["code"] == "DRIFT"


def test_match_gate_rejects_2001ms(client):
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 3001}],
        },
    ).json()
    assert [p["op"] for p in body["pairs"]] == ["DELETE", "INSERT"]
    assert body["total_cost"] == 5000
    assert body["first_defect"]["code"] == "MISS"


def test_empty_arrays_are_compliant(client):
    body = post(client, {"planned": [], "actual": []}).json()
    assert body == {
        "compliant": True,
        "total_cost": 0,
        "pairs": [],
        "first_defect": None,
    }


def test_missed_and_extra_items(client):
    body = post(
        client,
        {
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
        },
    ).json()
    assert [p["op"] for p in body["pairs"]] == ["MATCH", "DELETE", "MATCH", "INSERT"]
    assert body["total_cost"] == 6000
    # The leftmost defect is the missed B, not the later drifted C or extra D.
    assert body["first_defect"]["code"] == "MISS"
    assert body["first_defect"]["pair_index"] == 1
    assert body["first_defect"]["pair"]["planned_index"] == 1


def test_response_is_deterministic(client):
    payload = {
        "planned": [{"code": "A", "at_ms": 0}, {"code": "A", "at_ms": 1000}],
        "actual": [{"code": "A", "at_ms": 500}],
    }
    bodies = {post(client, payload).text for _ in range(3)}
    assert len(bodies) == 1


def _assert_422(resp, *detail_codes):
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_FAILED"
    codes = [d["code"] for d in body["error"]["details"]]
    for expected in detail_codes:
        assert expected in codes, codes
    for detail in body["error"]["details"]:
        assert set(detail) == {"code", "path", "message"}
    return body


def test_422_invalid_code(client):
    for bad in ["abc", "a", "", "CODE-1", "X" * 17, 123, None]:
        _assert_422(
            post(client, {"planned": [{"code": bad, "at_ms": 0}], "actual": []}),
            "INVALID_CODE",
        )


def test_422_valid_code_shapes(client):
    resp = post(
        client,
        {
            "planned": [{"code": "Z9" * 8, "at_ms": 0}],
            "actual": [{"code": "Z9" * 8, "at_ms": 0}],
        },
    )
    assert resp.status_code == 200


def test_422_invalid_at_ms(client):
    for bad in [-1, 1.5, "1000", True, False, None]:
        _assert_422(
            post(client, {"planned": [{"code": "A", "at_ms": bad}], "actual": []}),
            "INVALID_AT_MS",
        )


def test_422_not_strictly_increasing(client):
    body = _assert_422(
        post(
            client,
            {
                "planned": [
                    {"code": "A", "at_ms": 1000},
                    {"code": "B", "at_ms": 1000},
                ],
                "actual": [],
            },
        ),
        "NOT_STRICTLY_INCREASING",
    )
    assert body["error"]["details"][0]["path"] == "planned[1].at_ms"
    _assert_422(
        post(
            client,
            {
                "planned": [],
                "actual": [
                    {"code": "A", "at_ms": 2000},
                    {"code": "B", "at_ms": 1999},
                ],
            },
        ),
        "NOT_STRICTLY_INCREASING",
    )


def test_422_structure_errors(client):
    _assert_422(post(client, {"actual": []}), "MISSING_FIELD")
    _assert_422(post(client, {"planned": {}, "actual": []}), "INVALID_TYPE")
    _assert_422(post(client, {"planned": "nope", "actual": []}), "INVALID_TYPE")
    _assert_422(
        post(client, {"planned": [{"at_ms": 0}], "actual": []}), "MISSING_FIELD"
    )
    _assert_422(
        post(client, {"planned": [{"code": "A"}], "actual": []}), "MISSING_FIELD"
    )
    _assert_422(
        post(client, {"planned": [{"code": "A", "at_ms": 0, "x": 1}], "actual": []}),
        "UNEXPECTED_FIELD",
    )
    _assert_422(post(client, {"planned": [7], "actual": []}), "INVALID_TYPE")
    _assert_422(post(client, [1, 2, 3]), "INVALID_PAYLOAD")


def test_422_malformed_json(client):
    resp = client.post(
        "/align", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    _assert_422(resp, "INVALID_PAYLOAD")


def test_422_body_is_stable(client):
    payload = {
        "planned": [{"code": "bad!", "at_ms": -3}],
        "actual": [{"code": "A", "at_ms": 5}, {"code": "B", "at_ms": 5}],
    }
    bodies = {post(client, payload).text for _ in range(3)}
    assert len(bodies) == 1


def test_422_collects_multiple_details(client):
    body = _assert_422(
        post(
            client,
            {
                "planned": [{"code": "ok", "at_ms": 0}, {"code": "A"}],
                "actual": [{"code": "B", "at_ms": -1}],
            },
        ),
        "INVALID_CODE",
        "MISSING_FIELD",
        "INVALID_AT_MS",
    )
    paths = [d["path"] for d in body["error"]["details"]]
    assert paths == ["planned[0].code", "planned[1].at_ms", "actual[0].at_ms"]


# ---------------------------------------------------------------------------
# alternative_limit
# ---------------------------------------------------------------------------


def test_legacy_request_has_no_alternatives_field(client):
    payload = {
        "planned": [{"code": "A", "at_ms": 0}, {"code": "A", "at_ms": 1000}],
        "actual": [{"code": "A", "at_ms": 500}],
    }
    resp = post(client, payload)
    assert resp.status_code == 200
    body = resp.json()
    # The field is not merely null: it is absent from the serialized body.
    assert list(body) == ["compliant", "total_cost", "pairs", "first_defect"]
    assert "alternatives" not in resp.text
    # Repeated legacy requests are byte-for-byte identical.
    assert post(client, payload).text == resp.text


def test_alternatives_unique_optimum_with_positive_gap(client):
    # MATCH at 2000 ms drift (cost 2000) is the unique optimum; the next
    # legal path DELETE+INSERT costs 5000, a strictly positive gap.
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 3000}],
            "alternative_limit": 5,
        },
    ).json()
    assert [p["op"] for p in body["pairs"]] == ["MATCH"]
    assert body["total_cost"] == 2000
    # Only two runner-up paths exist; both must be returned even though more
    # slots were requested.
    assert len(body["alternatives"]) == 2
    alt = body["alternatives"][0]
    assert list(alt) == [
        "total_cost",
        "cost_gap",
        "pairs",
        "compliant",
        "first_defect",
        "first_divergence_index",
    ]
    assert alt["total_cost"] == 5000
    assert alt["cost_gap"] == 3000
    assert [p["op"] for p in alt["pairs"]] == ["DELETE", "INSERT"]
    assert alt["compliant"] is False
    assert alt["first_defect"]["code"] == "MISS"
    assert alt["first_defect"]["pair_index"] == 0
    assert alt["first_divergence_index"] == 0
    # DELETE < INSERT tie-break orders the two 5000-cost paths.
    second = body["alternatives"][1]
    assert [p["op"] for p in second["pairs"]] == ["INSERT", "DELETE"]
    assert second["cost_gap"] == 3000
    # INSERT pairs carry only the actual_* side.
    assert second["pairs"][0] == {
        "op": "INSERT",
        "cost": 2500,
        "code": "A",
        "actual_index": 0,
        "actual_at_ms": 3000,
    }
    assert second["first_divergence_index"] == 0
    assert second["first_defect"]["code"] == "EXTRA"


def test_alternatives_duplicate_code_tie_shows_zero_gap(client):
    # Two planned copies compete for one actual slot: MATCH,DELETE and
    # DELETE,MATCH tie at 3000.  The tie decides where the first MISS lands,
    # which is exactly what the reviewer needs to inspect.
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
    assert [p["op"] for p in body["pairs"]] == ["MATCH", "DELETE"]
    assert body["first_defect"]["pair_index"] == 1
    assert len(body["alternatives"]) == 1
    alt = body["alternatives"][0]
    assert alt["total_cost"] == 3000
    assert alt["cost_gap"] == 0
    assert [p["op"] for p in alt["pairs"]] == ["DELETE", "MATCH"]
    # Alternative pairs use the same sparse serialization as top-level pairs:
    # no actual_* fields on a DELETE, full field set on a MATCH.
    assert alt["pairs"][0] == {
        "op": "DELETE",
        "cost": 2500,
        "code": "A",
        "planned_index": 0,
        "planned_at_ms": 0,
    }
    assert alt["pairs"][1] == {
        "op": "MATCH",
        "cost": 500,
        "code": "A",
        "planned_index": 1,
        "actual_index": 0,
        "planned_at_ms": 1000,
        "actual_at_ms": 500,
        "drift_ms": 500,
    }
    assert alt["compliant"] is False
    assert alt["first_defect"]["code"] == "MISS"
    assert alt["first_defect"]["pair_index"] == 0
    assert alt["first_defect"]["pair"] == alt["pairs"][0]
    assert alt["first_divergence_index"] == 0


def test_alternatives_three_way_tie_divergence_positions(client):
    # M,M,D / M,D,M / D,M,M all cost 3500 and rank lexicographically.
    body = post(
        client,
        {
            "planned": [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
                {"code": "A", "at_ms": 2000},
            ],
            "actual": [{"code": "A", "at_ms": 500}, {"code": "A", "at_ms": 1500}],
            "alternative_limit": 2,
        },
    ).json()
    assert [p["op"] for p in body["pairs"]] == ["MATCH", "MATCH", "DELETE"]
    assert [
        [p["op"] for p in alt["pairs"]] for alt in body["alternatives"]
    ] == [
        ["MATCH", "DELETE", "MATCH"],
        ["DELETE", "MATCH", "MATCH"],
    ]
    assert [alt["cost_gap"] for alt in body["alternatives"]] == [0, 0]
    assert [alt["first_divergence_index"] for alt in body["alternatives"]] == [1, 0]
    # The first defect is verdict-dependent: index 2 on the primary, 1 on the
    # first candidate, 0 on the second.
    assert body["first_defect"]["pair_index"] == 2
    assert [alt["first_defect"]["pair_index"] for alt in body["alternatives"]] == [
        1,
        0,
    ]
    # No path is reported twice.
    all_ops = [tuple(p["op"] for p in body["pairs"])] + [
        tuple(p["op"] for p in alt["pairs"]) for alt in body["alternatives"]
    ]
    assert len(set(all_ops)) == len(all_ops)


def test_alternatives_truncated_to_limit_and_respects_boundaries(client):
    payload = {
        "planned": [
            {"code": "A", "at_ms": 0},
            {"code": "A", "at_ms": 1000},
            {"code": "A", "at_ms": 2000},
        ],
        "actual": [{"code": "A", "at_ms": 500}, {"code": "A", "at_ms": 1500}],
    }
    one = post(client, {**payload, "alternative_limit": 1}).json()
    assert len(one["alternatives"]) == 1
    twenty = post(client, {**payload, "alternative_limit": 20}).json()
    # A 3x2 lattice has 25 monotone paths, so limit 20 truncates the ranking
    # to exactly 20 alternatives rather than returning the full set.
    assert len(twenty["alternatives"]) == 20
    # Costs and op strings are non-decreasing across the ranking.
    ranked_costs = [twenty["total_cost"]] + [
        alt["total_cost"] for alt in twenty["alternatives"]
    ]
    assert ranked_costs == sorted(ranked_costs)
    ranked_ops = [
        tuple(p["op"] for p in twenty["pairs"])
    ] + [
        tuple(p["op"] for p in alt["pairs"]) for alt in twenty["alternatives"]
    ]
    assert len(set(ranked_ops)) == len(ranked_ops)


def test_alternatives_insufficient_legal_paths(client):
    # Two empty sequences have exactly one legal path (the empty alignment).
    body = post(
        client, {"planned": [], "actual": [], "alternative_limit": 20}
    ).json()
    assert body["pairs"] == []
    assert body["alternatives"] == []
    # Unmatchable singletons admit exactly two legal paths (DELETE,INSERT and
    # INSERT,DELETE); limit 3 asks for more candidates than exist.
    body = post(
        client,
        {
            "planned": [{"code": "A", "at_ms": 0}],
            "actual": [{"code": "B", "at_ms": 0}],
            "alternative_limit": 3,
        },
    ).json()
    assert [p["op"] for p in body["pairs"]] == ["DELETE", "INSERT"]
    assert [
        [p["op"] for p in alt["pairs"]] for alt in body["alternatives"]
    ] == [["INSERT", "DELETE"]]


def test_alternatives_are_deterministic(client):
    payload = {
        "planned": [
            {"code": "A", "at_ms": 0},
            {"code": "B", "at_ms": 1000},
            {"code": "A", "at_ms": 2000},
        ],
        "actual": [
            {"code": "A", "at_ms": 100},
            {"code": "A", "at_ms": 2100},
        ],
        "alternative_limit": 10,
    }
    bodies = {post(client, payload).text for _ in range(3)}
    assert len(bodies) == 1


def test_422_alternative_limit_values(client):
    base = {"planned": [], "actual": []}
    for bad in (0, 21, -1, 1.0, True, False, "1", None, [], {}):
        body = _assert_422(
            post(client, {**base, "alternative_limit": bad}),
            "INVALID_ALTERNATIVE_LIMIT",
        )
        detail = body["error"]["details"][0]
        assert detail["code"] == "INVALID_ALTERNATIVE_LIMIT"
        assert detail["path"] == "alternative_limit"
        assert set(detail) == {"code", "path", "message"}


def test_422_alternative_limit_does_not_mask_other_errors(client):
    # A bad limit alongside a structural error reports both, in deterministic
    # order (array fields are checked before the optional parameter).
    resp = post(
        client,
        {"planned": [], "actual": "nope", "alternative_limit": 99},
    )
    body = _assert_422(resp, "INVALID_TYPE", "INVALID_ALTERNATIVE_LIMIT")
    assert [d["path"] for d in body["error"]["details"]] == [
        "actual",
        "alternative_limit",
    ]
    # Item-level errors alone still surface exactly as before.
    body = _assert_422(
        post(
            client,
            {
                "planned": [{"code": "bad", "at_ms": 0}],
                "actual": [],
                "alternative_limit": 1,
            },
        ),
        "INVALID_CODE",
    )
    assert [d["code"] for d in body["error"]["details"]] == ["INVALID_CODE"]
