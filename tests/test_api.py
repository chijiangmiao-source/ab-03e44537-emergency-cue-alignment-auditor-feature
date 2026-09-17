"""HTTP-level tests: contract, boundaries, defects and 422 machine codes."""


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
