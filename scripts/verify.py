"""One-shot acceptance checks against a running alignment API.

Uses only the standard library so the runtime image needs no extra
dependencies.  Configure the target with ``API_BASE_URL`` (default
``http://localhost:8000``).  Exits 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
HEALTH_TIMEOUT_S = float(os.environ.get("VERIFY_HEALTH_TIMEOUT_S", "60"))


class CheckFailed(AssertionError):
    pass


def _request(method: str, path: str, payload: object = None, raw: bytes | None = None):
    if raw is not None:
        data = raw
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
    else:
        data = None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _align(payload):
    status, body = _request("POST", "/align", payload=payload)
    if status != 200:
        raise CheckFailed(f"expected 200, got {status}: {body}")
    return body


def _expect_equal(name: str, got, want) -> None:
    if got != want:
        raise CheckFailed(f"{name}\n  want: {want!r}\n  got:  {got!r}")


def check_health() -> None:
    status, body = _request("GET", "/healthz")
    _expect_equal("health status", status, 200)
    _expect_equal("health body", body, {"status": "ok"})


def check_compliant_exact() -> None:
    body = _align(
        {
            "planned": [
                {"code": "ADS1", "at_ms": 1000},
                {"code": "NEWS", "at_ms": 2000},
            ],
            "actual": [
                {"code": "ADS1", "at_ms": 1000},
                {"code": "NEWS", "at_ms": 2000},
            ],
        }
    )
    _expect_equal(
        "exact replay",
        body,
        {
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
                },
                {
                    "op": "MATCH",
                    "cost": 0,
                    "code": "NEWS",
                    "planned_index": 1,
                    "actual_index": 1,
                    "planned_at_ms": 2000,
                    "actual_at_ms": 2000,
                    "drift_ms": 0,
                },
            ],
            "first_defect": None,
        },
    )


def check_compliant_boundary_500() -> None:
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 1500}],
        }
    )
    _expect_equal("drift exactly 500 stays compliant", body["compliant"], True)
    _expect_equal("drift exactly 500 cost", body["total_cost"], 500)
    _expect_equal("drift exactly 500 defect", body["first_defect"], None)


def check_drift_501_is_defect() -> None:
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 1501}],
        }
    )
    _expect_equal("drift 501 compliant", body["compliant"], False)
    _expect_equal("drift 501 cost", body["total_cost"], 501)
    defect = body["first_defect"]
    _expect_equal("drift 501 defect code", defect["code"], "DRIFT")
    _expect_equal("drift 501 defect index", defect["pair_index"], 0)
    _expect_equal("drift 501 defect drift", defect["pair"]["drift_ms"], 501)


def check_match_gate_2000() -> None:
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 3000}],
        }
    )
    _expect_equal("drift 2000 matches", [p["op"] for p in body["pairs"]], ["MATCH"])
    _expect_equal("drift 2000 cost", body["total_cost"], 2000)
    _expect_equal("drift 2000 defect", body["first_defect"]["code"], "DRIFT")


def check_match_gate_2001() -> None:
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 3001}],
        }
    )
    _expect_equal(
        "drift 2001 cannot match",
        [p["op"] for p in body["pairs"]],
        ["DELETE", "INSERT"],
    )
    _expect_equal("drift 2001 cost", body["total_cost"], 5000)
    _expect_equal("drift 2001 first defect", body["first_defect"]["code"], "MISS")


def check_duplicate_code_tie_planned() -> None:
    # planned A@0, A@1000 vs actual A@500: both alignments cost 3000; the
    # lexicographic rule must keep MATCH first, so the leftmost planned item
    # wins the actual slot and the second planned item is deleted.
    body = _align(
        {
            "planned": [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
            ],
            "actual": [{"code": "A", "at_ms": 500}],
        }
    )
    _expect_equal(
        "duplicate planned codes resolve to a single path",
        body,
        {
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
        },
    )


def check_duplicate_code_tie_actual() -> None:
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 500}],
            "actual": [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
            ],
        }
    )
    _expect_equal(
        "duplicate actual codes resolve to a single path",
        [p["op"] for p in body["pairs"]],
        ["MATCH", "INSERT"],
    )
    _expect_equal("duplicate actual cost", body["total_cost"], 3000)
    _expect_equal("duplicate actual defect", body["first_defect"]["code"], "EXTRA")


def check_delete_precedes_insert_on_tie() -> None:
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 0}],
            "actual": [{"code": "B", "at_ms": 0}],
        }
    )
    _expect_equal(
        "unmatchable items order DELETE before INSERT",
        [p["op"] for p in body["pairs"]],
        ["DELETE", "INSERT"],
    )
    _expect_equal("unmatchable cost", body["total_cost"], 5000)
    _expect_equal("unmatchable defect", body["first_defect"]["code"], "MISS")


def check_multi_equal_cost_paths() -> None:
    # Three minimal alignments cost 3500; only MATCH,MATCH,DELETE is the
    # lexicographically smallest operation string.
    body = _align(
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
        }
    )
    _expect_equal(
        "three-way tie picks MATCH,MATCH,DELETE",
        [p["op"] for p in body["pairs"]],
        ["MATCH", "MATCH", "DELETE"],
    )
    _expect_equal("three-way tie cost", body["total_cost"], 3500)
    _expect_equal(
        "three-way tie pairs",
        [(p.get("planned_index"), p.get("actual_index")) for p in body["pairs"]],
        [(0, 0), (1, 1), (2, None)],
    )


def check_first_defect_is_leftmost() -> None:
    body = _align(
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
        }
    )
    _expect_equal(
        "mixed scenario ops",
        [p["op"] for p in body["pairs"]],
        ["MATCH", "DELETE", "MATCH", "INSERT"],
    )
    _expect_equal("mixed scenario cost", body["total_cost"], 6000)
    defect = body["first_defect"]
    _expect_equal("leftmost defect is the DELETE, not the later DRIFT/INSERT",
                  defect["code"], "MISS")
    _expect_equal("leftmost defect index", defect["pair_index"], 1)


def check_empty_arrays() -> None:
    body = _align({"planned": [], "actual": []})
    _expect_equal(
        "empty alignment",
        body,
        {"compliant": True, "total_cost": 0, "pairs": [], "first_defect": None},
    )


def check_alternatives_unique_optimum_positive_gap() -> None:
    # MATCH at drift 2000 (cost 2000) is the unique optimum; the next path
    # DELETE+INSERT costs 5000, so the cost gap is strictly positive.
    body = _align(
        {
            "planned": [{"code": "A", "at_ms": 1000}],
            "actual": [{"code": "A", "at_ms": 3000}],
            "alternative_limit": 5,
        }
    )
    _expect_equal("positive-gap primary ops", [p["op"] for p in body["pairs"]], ["MATCH"])
    alts = body["alternatives"]
    _expect_equal("positive-gap runner count", len(alts), 2)
    first = alts[0]
    _expect_equal(
        "positive-gap alternative keys",
        list(first),
        [
            "total_cost",
            "cost_gap",
            "pairs",
            "compliant",
            "first_defect",
            "first_divergence_index",
        ],
    )
    _expect_equal("positive-gap cost", first["total_cost"], 5000)
    _expect_equal("positive-gap gap", first["cost_gap"], 3000)
    _expect_equal(
        "positive-gap runner ops",
        [p["op"] for p in first["pairs"]],
        ["DELETE", "INSERT"],
    )
    _expect_equal("positive-gap divergence", first["first_divergence_index"], 0)
    _expect_equal("positive-gap defect", first["first_defect"]["code"], "MISS")


def check_alternatives_duplicate_code_tie() -> None:
    # Duplicate planned codes split the optimum across two equal-cost paths:
    # MATCH,DELETE and DELETE,MATCH both cost 3000, so the gap is 0 and the
    # first defect moves from pair index 1 to pair index 0 -- the reviewer can
    # see whether the verdict depends on the tie-break.
    body = _align(
        {
            "planned": [
                {"code": "A", "at_ms": 0},
                {"code": "A", "at_ms": 1000},
            ],
            "actual": [{"code": "A", "at_ms": 500}],
            "alternative_limit": 1,
        }
    )
    alts = body["alternatives"]
    _expect_equal("duplicate-code runner count", len(alts), 1)
    alt = alts[0]
    _expect_equal("duplicate-code zero gap", alt["cost_gap"], 0)
    _expect_equal(
        "duplicate-code runner ops",
        [p["op"] for p in alt["pairs"]],
        ["DELETE", "MATCH"],
    )
    _expect_equal("duplicate-code divergence", alt["first_divergence_index"], 0)
    _expect_equal("primary defect index", body["first_defect"]["pair_index"], 1)
    _expect_equal("runner defect index", alt["first_defect"]["pair_index"], 0)


def check_alternatives_insufficient_paths() -> None:
    # Two empty sequences have exactly one legal path; limit asks for 20
    # alternatives but only the real (zero) count may come back.
    body = _align({"planned": [], "actual": [], "alternative_limit": 20})
    _expect_equal("insufficient paths field present", "alternatives" in body, True)
    _expect_equal("insufficient paths value", body["alternatives"], [])


def check_legacy_request_byte_compatible() -> None:
    # Requests without alternative_limit must be answered by the exact legacy
    # contract: no alternatives key at all, byte-stable across calls.
    payload = {
        "planned": [
            {"code": "A", "at_ms": 0},
            {"code": "A", "at_ms": 1000},
        ],
        "actual": [{"code": "A", "at_ms": 500}],
    }
    _, first = _request("POST", "/align", payload=payload)
    if "alternatives" in first:
        raise CheckFailed("legacy response must not carry alternatives")
    _expect_equal(
        "legacy response keys",
        list(first),
        ["compliant", "total_cost", "pairs", "first_defect"],
    )
    for _ in range(2):
        _, again = _request("POST", "/align", payload=payload)
        _expect_equal("legacy response must be byte-stable", again, first)


def check_alternative_limit_validation() -> None:
    base = {"planned": [], "actual": []}
    for bad in (0, 21, -1, True, False, 1.5, "1", None):
        status, body = _request("POST", "/align", payload={**base, "alternative_limit": bad})
        _expect_equal(f"alternative_limit={bad!r} status", status, 422)
        codes = [d["code"] for d in body["error"]["details"]]
        _expect_equal(
            f"alternative_limit={bad!r} code",
            "INVALID_ALTERNATIVE_LIMIT" in codes,
            True,
        )
    for good in (1, 20):
        status, body = _request(
            "POST", "/align", payload={**base, "alternative_limit": good}
        )
        _expect_equal(f"alternative_limit={good} status", status, 200)


def check_determinism() -> None:
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
    }
    first = _align(payload)
    for _ in range(3):
        _expect_equal("repeated calls must be identical", _align(payload), first)


def _expect_422(payload=None, raw: bytes | None = None, want_detail: str = ""):
    status, body = _request("POST", "/align", payload=payload, raw=raw)
    _expect_equal("validation status", status, 422)
    if body.get("error", {}).get("code") != "VALIDATION_FAILED":
        raise CheckFailed(f"unexpected error envelope: {body}")
    codes = [d["code"] for d in body["error"]["details"]]
    if want_detail and want_detail not in codes:
        raise CheckFailed(f"expected detail {want_detail} in {codes}")
    # The same request must always produce the identical body.
    again_status, again = _request("POST", "/align", payload=payload, raw=raw)
    _expect_equal("422 repeat status", again_status, 422)
    _expect_equal("422 body must be stable", again, body)
    return codes


def check_validation_codes() -> None:
    _expect_422(
        {"planned": [{"code": "bad", "at_ms": 0}], "actual": []},
        want_detail="INVALID_CODE",
    )
    _expect_422(
        {"planned": [{"code": "TOOLONGCODE1234567X", "at_ms": 0}], "actual": []},
        want_detail="INVALID_CODE",
    )
    _expect_422(
        {"planned": [{"code": "A", "at_ms": -1}], "actual": []},
        want_detail="INVALID_AT_MS",
    )
    _expect_422(
        {"planned": [{"code": "A", "at_ms": True}], "actual": []},
        want_detail="INVALID_AT_MS",
    )
    _expect_422(
        {"planned": [{"code": "A", "at_ms": 1.5}], "actual": []},
        want_detail="INVALID_AT_MS",
    )
    _expect_422(
        {
            "planned": [
                {"code": "A", "at_ms": 1000},
                {"code": "B", "at_ms": 1000},
            ],
            "actual": [],
        },
        want_detail="NOT_STRICTLY_INCREASING",
    )
    _expect_422(
        {
            "planned": [],
            "actual": [
                {"code": "A", "at_ms": 2000},
                {"code": "B", "at_ms": 1999},
            ],
        },
        want_detail="NOT_STRICTLY_INCREASING",
    )
    _expect_422(
        {"planned": [{"code": "A", "at_ms": 0, "note": "x"}], "actual": []},
        want_detail="UNEXPECTED_FIELD",
    )
    _expect_422({"planned": [{"at_ms": 0}], "actual": []}, want_detail="MISSING_FIELD")
    _expect_422({"planned": {}, "actual": []}, want_detail="INVALID_TYPE")
    _expect_422({"actual": []}, want_detail="MISSING_FIELD")
    _expect_422(raw=b"not json", want_detail="INVALID_PAYLOAD")
    _expect_422(payload=[1, 2, 3], want_detail="INVALID_PAYLOAD")


CHECKS = [
    ("health", check_health),
    ("compliant exact replay", check_compliant_exact),
    ("compliant at exactly 500ms drift", check_compliant_boundary_500),
    ("drift defect at 501ms", check_drift_501_is_defect),
    ("match gate at exactly 2000ms", check_match_gate_2000),
    ("match gate rejects 2001ms", check_match_gate_2001),
    ("duplicate planned codes tie", check_duplicate_code_tie_planned),
    ("duplicate actual codes tie", check_duplicate_code_tie_actual),
    ("DELETE before INSERT on tie", check_delete_precedes_insert_on_tie),
    ("multiple equal-cost paths", check_multi_equal_cost_paths),
    ("first defect is leftmost", check_first_defect_is_leftmost),
    ("empty arrays", check_empty_arrays),
    ("alternatives positive gap", check_alternatives_unique_optimum_positive_gap),
    ("alternatives duplicate-code tie", check_alternatives_duplicate_code_tie),
    ("alternatives insufficient paths", check_alternatives_insufficient_paths),
    ("legacy request byte compatibility", check_legacy_request_byte_compatible),
    ("alternative_limit validation", check_alternative_limit_validation),
    ("determinism across calls", check_determinism),
    ("validation machine codes", check_validation_codes),
]


def wait_for_api() -> bool:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(BASE_URL + "/healthz", timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    return False


def main() -> int:
    print(f"verify: targeting {BASE_URL}", flush=True)
    if not wait_for_api():
        print("verify: API did not become healthy in time", flush=True)
        return 1
    failures = 0
    for name, fn in CHECKS:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - report every failure
            failures += 1
            print(f"FAIL {name}: {exc}", flush=True)
        else:
            print(f"PASS {name}", flush=True)
    total = len(CHECKS)
    print(f"verify: {total - failures}/{total} checks passed", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
