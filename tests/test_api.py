"""API tests for the M2 FastAPI application (app/main.py)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_ok():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["driver"] in {"duckdb", "databricks"}
    assert body["llm"] in {"anthropic", "keyword"}
    assert "data_max_ts" in body


def test_health_reports_model_tiers():
    """Module M7a: /health additionally reports the resolved model per
    tier and which tier is used when a request doesn't name one."""
    resp = client.get("/health")
    body = resp.json()
    assert set(body["tiers"]) == {"fast", "balanced", "max"}
    assert all(isinstance(v, str) and v for v in body["tiers"].values())
    assert body["default_tier"] == "max"


@pytest.mark.parametrize(
    "question",
    [
        "Where is the biggest drop-off?",
        "Compare pairing rate iOS vs Android by market",
        "Which channel drives downloads vs actual pairings?",
    ],
)
def test_ask_returns_rows_and_answer(question):
    resp = client.post("/api/ask", json={"question": question, "lang": "en"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["question"] == question
    assert body["rows"], f"{question!r} returned no rows"
    assert body["answer"] and isinstance(body["answer"], str)
    assert body["lang"] == "en"
    assert body["mode"] not in {"error"}


def test_ask_tr_lang_echoed_and_still_answers():
    resp = client.post(
        "/api/ask", json={"question": "Weekly trend of test starts", "lang": "tr"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["lang"] == "tr"
    assert body["rows"]
    assert body["answer"]


def test_ask_sql_is_pretty_printed_for_display():
    """Module M6: the sql field a client renders is reindented + upper-cased.

    "funnel overview" deterministically resolves to the registered
    funnel_overview metric under the hermetic KeywordLLM used in tests
    (see tests/conftest.py), so this exercises the FunnelAgent.ask() ->
    api_ask() display-formatting path end-to-end.
    """
    resp = client.post(
        "/api/ask", json={"question": "Give me the funnel overview", "lang": "en"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "metric"
    sql = body["sql"]
    assert sql
    assert "SELECT" in sql
    assert "\n" in sql


def test_ask_rejects_empty_question():
    resp = client.post("/api/ask", json={"question": "", "lang": "en"})
    assert resp.status_code == 422
    assert "error" in resp.json()


class TestAskModelTierAndMemoryParams:
    """Module M7a: /api/ask gains optional tier/session_id/memory params.
    All optional and backward-compatible — the pre-M7a request body
    (question + lang only) must keep working exactly as before."""

    def test_pre_m7a_request_body_still_works(self):
        resp = client.post(
            "/api/ask", json={"question": "Weekly trend of test starts", "lang": "en"}
        )
        assert resp.status_code == 200
        assert resp.json()["answer"]

    @pytest.mark.parametrize("tier", ["fast", "balanced", "max"])
    def test_valid_tier_accepted(self, tier):
        resp = client.post(
            "/api/ask",
            json={"question": "Weekly trend of test starts", "lang": "en", "tier": tier},
        )
        assert resp.status_code == 200
        assert resp.json()["answer"]

    def test_invalid_tier_returns_422(self):
        resp = client.post(
            "/api/ask",
            json={"question": "Weekly trend of test starts", "tier": "ultra"},
        )
        assert resp.status_code == 422
        assert "error" in resp.json()

    def test_invalid_memory_value_returns_422(self):
        resp = client.post(
            "/api/ask",
            json={"question": "Weekly trend of test starts", "memory": "maybe"},
        )
        assert resp.status_code == 422

    def test_session_id_and_memory_off_accepted(self):
        resp = client.post(
            "/api/ask",
            json={
                "question": "Weekly trend of test starts",
                "session_id": "test-session-1",
                "memory": "off",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["answer"]

    def test_session_id_records_a_turn_into_conversation_memory(self):
        from app.main import conversation_memory

        resp = client.post(
            "/api/ask",
            json={
                "question": "Give me the funnel overview",
                "session_id": "test-session-record",
            },
        )
        assert resp.status_code == 200
        turns = conversation_memory.get_turns("test-session-record")
        assert len(turns) >= 1
        assert turns[-1]["question"] == "Give me the funnel overview"


def test_metrics_returns_twelve():
    resp = client.get("/api/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 12
    for m in body:
        assert {"key", "title", "description", "consent_note", "chart"} <= set(m)


def test_dashboard_returns_twelve_cards_with_rows():
    resp = client.post("/api/dashboard", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 12
    for card in body:
        assert {"key", "title", "consent_note", "chart", "rows", "answer", "sql"} <= set(card)
        assert card["rows"], f"{card['key']} returned no rows"
        assert card["sql"], f"{card['key']} has no sql"


def test_dashboard_with_subset_of_keys():
    resp = client.post("/api/dashboard", json={"keys": ["funnel_overview"]})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["key"] == "funnel_overview"


def test_dashboard_unknown_key_returns_json_error():
    resp = client.post("/api/dashboard", json={"keys": ["not_a_real_metric"]})
    assert resp.status_code == 400
    assert "error" in resp.json()


class TestFilteredDashboardM11:
    """Module M11: POST /api/dashboard's optional `filters` field — the
    ~10-KPI dimensional-gold-cube path, additive over the legacy behaviour
    exercised by the tests above (which stays entirely unchanged)."""

    def test_empty_filters_object_uses_the_cube_registry(self):
        resp = client.post("/api/dashboard", json={"filters": {}})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) >= 10
        for card in body:
            assert {"key", "title", "consent_note", "chart", "rows", "answer", "sql"} <= set(card)
            assert card["key"].startswith("dash_")
            assert card["sql"], f"{card['key']} has no sql"

    def test_filters_by_known_market_narrows_results(self):
        resp = client.post("/api/dashboard", json={"filters": {"market": "DE"}})
        assert resp.status_code == 200
        body = resp.json()
        funnel_card = next(c for c in body if c["key"] == "dash_funnel_stages")
        starts = next(r["users"] for r in funnel_card["rows"] if r["stage"] == "test_starts")
        assert 0 < starts < 100000
        # M11 addendum 2: the card's sql is the composed WHERE actually
        # applied, not a generic/unfiltered template.
        assert "DE" in funnel_card["sql"]

    def test_relative_date_filters_are_accepted(self):
        resp = client.post(
            "/api/dashboard",
            json={"filters": {"date_start": "2026-06-01", "date_end": "2026-08-30"}},
        )
        assert resp.status_code == 200
        assert len(resp.json()) >= 10

    def test_unknown_market_returns_422_naming_the_field(self):
        resp = client.post("/api/dashboard", json={"filters": {"market": "Atlantis"}})
        assert resp.status_code == 422
        body = resp.json()
        assert "error" in body
        assert "market" in body["error"]

    def test_unknown_channel_returns_422(self):
        resp = client.post("/api/dashboard", json={"filters": {"channel": "not_a_channel"}})
        assert resp.status_code == 422
        assert "channel" in resp.json()["error"]

    def test_malformed_date_returns_422(self):
        resp = client.post("/api/dashboard", json={"filters": {"date_start": "not-a-date"}})
        assert resp.status_code == 422

    def test_sql_injection_attempt_in_filter_returns_422_not_500(self):
        resp = client.post(
            "/api/dashboard",
            json={"filters": {"market": "DE' OR '1'='1"}},
        )
        assert resp.status_code == 422
        assert "error" in resp.json()

    def test_legacy_dashboard_unaffected_when_filters_omitted(self):
        """Backward compatibility: the pre-M11 tests above already cover
        this, but assert it here too as a single load-bearing regression
        check right next to the new filtered-path tests."""
        resp = client.post("/api/dashboard", json={})
        assert resp.status_code == 200
        assert len(resp.json()) == 12


def test_rate_limiter_triggers_429_after_burst():
    hit_429 = False
    for _ in range(30):
        resp = client.post(
            "/api/ask", json={"question": "Weekly trend of test starts", "lang": "en"}
        )
        if resp.status_code == 429:
            hit_429 = True
            assert "error" in resp.json()
            break
    assert hit_429, "Expected a 429 after bursting /api/ask"


def test_unknown_api_route_returns_json_error():
    resp = client.get("/api/this-does-not-exist")
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body


def test_catalog_lists_medallion_layers():
    """Module M8: GET /api/catalog backs the frontend's data catalog panel —
    layers present, each entry qualified, gold has all 12 governed marts."""
    resp = client.get("/api/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert "layers" in body
    layers = body["layers"]
    assert set(layers) == {"bronze", "silver", "gold"}
    assert len(layers["gold"]) >= 12
    for layer_name, entries in layers.items():
        assert entries, f"{layer_name} layer should not be empty"
        for entry in entries:
            assert entry["name"].startswith(f"{layer_name}.")
            if "comment" in entry:
                assert isinstance(entry["comment"], str) and entry["comment"]
